"""Vlákno zapisovače historie (M3.3a): omezené fronty, dávky a jediné spojení SQLite.

Podle docs/HISTORY.md 5 (a 4.3): hlavní vlákno volá jen submit(), submit_event(),
status(), flush(), wake() a stop(); nikdy SQLite a nikdy nečeká na I/O. Databázi
otevře (open_writer: zámek flock, ověření identity, nikdy nevytvoří soubor),
používá i zavře výhradně vlákno zapisovače; spojení zůstává s check_same_thread,
takže ho jiné vlákno použít nemůže. Změny stavu (R4) se počítají už při submit(),
takže přetečení fronty vzorků nikdy neztratí přechod alarmu.

Napojení na měřicí smyčku, SIGTERM a skutečný zdroj synchronizace hodin má
`history_link.py` (M3.3b-1). Od M3.3b-2 vlákno samo spouští i údržbu: agregaci
uzavřených oken a hodinovou retenci (HISTORY.md 6, 7, 11.6). Zápis čekajících
měření a změn má vždy přednost; druhé spojení ani druhé vlákno nevzniká.
"""

import math
import os
import sqlite3
import threading
import time
from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType

from . import history
from .config import VALIDITY_INTERVALS
from .core import CheckResult, is_finite_number

STARTING = "STARTING"  # vlákno ještě neotevřelo databázi
RUNNING = "RUNNING"  # zapisuje bez chyby
DEGRADED = "DEGRADED"  # přechodná chyba, data čekají ve frontách a zápis se opakuje
STOPPED = "STOPPED"  # vlákno skončilo po stop()
FAILED = "FAILED"  # nevratná chyba (poškozená nebo cizí databáze, chyba programu): už nezapisuje

BATCH_SECONDS = 60  # nejvyšší stáří nezapsaného měření v bezchybném provozu (HISTORY.md 5.2)
QUEUE_SECONDS = 3600  # fronty drží nejvýš hodinu
# Fronta změn pokryje hodinu, v níž tolik kontrol kmitá při každém měření
# (změřeno ~470 B na změnu: 20 kontrol po 10 s = 7 200 změn ≈ 3,4 MB).
FLAPPING_CHECKS = 20
# Pevný paměťový strop fronty měření: tolik hodnot (výsledků) najednou, ≈ 36 MB (≈ 500 B na výsledek).
# Hodina při 20 výsledcích po 1 s; s víc výsledky na měření pokryje fronta kratší dobu.
MAX_QUEUE_VALUES = 72_000
EVENT_CAPACITY = 500
# Text a údaje jedné události (UTF-8): fronta 500 událostí tak drží nejvýš ≈ 8 MB.
# Větší událost je chyba volajícího; nepřijme se a započte jako ztráta (sledování nezastaví).
MAX_EVENT_BYTES = 16_384
# Text jednoho výsledku (UTF-8): id, summary a údaje details dohromady. Skutečné
# výsledky kolektorů mají desítky bajtů (summary ≈ 30 B, details ≈ 60 B), takže
# 512 B je osminásobná rezerva a zároveň drží paměť front v měřeném rozsahu
# (72 000 hodnot × ≈ 0,7 kB ≈ 50 MB, HISTORY.md 5.2). Delší text se zkrátí;
# identita kontroly, stav ani naměřená hodnota se nikdy nezahodí.
MAX_RESULT_BYTES = 512
# Režie jednoho údaje details v JSON ("klíč":"hodnota",) při měření velikosti.
DETAILS_OVERHEAD = 6
TRUNCATION_MARK = "…"
# Údržba (M3.3b-2): pokus o agregaci nejvýš takhle často, hned po potvrzené dávce
# a při dohánění (more) hned znovu. Retence nejvýš jednou za hodinu monotónního času.
AGGREGATION_INTERVAL_SECONDS = 60
RETENTION_INTERVAL_SECONDS = 3600
# Nejdelší mezera mezi pozorováními synchronizace, která ještě neruší doloženou
# stabilitu hodin: platnost výsledku (3 × interval měření, stejné pravidlo jako
# u zastaralých dat), nejméně ale jedno agregační okno (300 s), aby jediné
# zdržené měření nezahodilo celodenní doklad. Při výchozím intervalu 10 s je to
# 300 s, tedy nejvýš 0,35 % z 24 h bez pozorování na jednu mezeru.
MIN_SYNC_FRESH_SECONDS = history.WINDOW_SECONDS
# Ztráty, po kterých nelze vyloučit, že se nezapsal doklad skoku hodin
# (clock.jump je událost). Dokud nejsou zapsané v databázi, nelze o nich nic
# doložit a retence se nepovažuje za bezpečnou; po zapsání je mezera v historii
# viditelná a skok v tomto běhu hlídá i vlastní detekce posunu času.
CLOCK_EVIDENCE_LOSSES = frozenset({"events_dropped", "event_too_large"})
STOP_TIMEOUT_SECONDS = 10  # HISTORY.md 5.5
MAX_WAIT_SECONDS = 10  # vlákno se probudí nejméně jednou za 10 s (HISTORY.md 5.2)
FINAL_ATTEMPTS = 2  # pokusy o poslední zápis při zastavení (bez čekání na odstup)
# Odstup opakování podle druhu chyby (HISTORY.md 5.3): (první, nejvyšší) v sekundách; zdvojnásobuje se.
BACKOFF = {"busy": (5, 60), "full": (60, 600), "io": (60, 600), "locked": (60, 600), "path": (60, 600)}
FATAL_KINDS = frozenset({"corrupt", "integrity", "identity"})
_IDENTITY = frozenset({"foreign", "schema", "role", "host", "alias", "sqlite", "settings"})
_TRANSIENT_OPEN = ("SQLITE_IOERR", "SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_CANTOPEN", "SQLITE_READONLY", "SQLITE_PERM")
# Druh známé ztráty -> událost, která ji zapíše (HISTORY.md 5.4). after_stop se jen počítá.
LOSS_EVENTS = {"queue_full": "history.gap", "time_unsynced": "history.gap",
               "changes_dropped": "history.changes_dropped", "events_dropped": "history.events_dropped",
               "event_too_large": "history.events_dropped",
               "result_truncated": "history.result_truncated"}
# Ztráta celých položek je ERROR; ostatní WARNING, nad hodinu rozsahu ERROR.
SEVERE_LOSSES = frozenset({"changes_dropped", "events_dropped", "event_too_large"})
# Zkrácený text není ztráta měření (stav, hodnota i identita zůstaly), proto
# zůstává WARNING bez ohledu na délku rozsahu.
TEXT_LOSSES = frozenset({"result_truncated"})


def result_bytes(result):
    """Velikost textu výsledku v bajtech UTF-8: id, summary a údaje details."""
    size = len(result.id.encode()) + len(result.summary.encode())
    for key, value in result.details.items():
        size += len(key.encode()) + len(value.encode()) + DETAILS_OVERHEAD
    return size


def _cut(text, limit):
    """Text zkrácený na `limit` bajtů UTF-8, s koncovou značkou, když se zkracoval."""
    if limit <= 0 or not text:
        return ""
    mark = len(TRUNCATION_MARK.encode())
    if limit <= mark:
        return ""  # na značku ani není místo: text se vypustí celý
    # Řez v bajtech může přepůlit znak UTF-8; neúplný znak se zahodí.
    return text.encode()[:limit - mark].decode(errors="ignore") + TRUNCATION_MARK


def trim_result(result, limit=MAX_RESULT_BYTES):
    """Výsledek zkrácený na `limit` bajtů UTF-8; vrací (výsledek, zkráceno).

    Zkracuje se jen text. Id kontroly, stav, naměřená hodnota a oba časy
    zůstávají vždy: podle nich se pozná, co se měřilo a jak to skončilo.
    Z údajů `details` se udrží ty, které se do limitu vejdou; klíč objektu řady
    (např. rozhraní u RX/TX) má přednost, protože nese identitu řady. Co zbude,
    dostane `summary`. Výsledek s nepřiměřeně dlouhým id tak projde bez textu,
    ale nikdy se nezahodí.
    """
    if result_bytes(result) <= limit:
        return result, False
    spec = history.SERIES.get(result.id)
    subject = None if spec is None else spec.subject_key
    remaining = limit - len(result.id.encode())
    details = {}
    for key in sorted(result.details, key=lambda name: (name != subject, name)):
        value = result.details[key]
        cost = len(key.encode()) + len(value.encode()) + DETAILS_OVERHEAD
        if cost <= remaining:
            details[key] = value
            remaining -= cost
    return replace(result, summary=_cut(result.summary, remaining), details=details), True


def classify(error):
    """Druh chyby HistoryError: přechodný (busy, full, io, locked, path) nebo nevratný (FATAL_KINDS)."""
    name = error.sqlite_error or ""
    if error.reason == "write":
        if name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
            return "busy"
        if name == "SQLITE_FULL":
            return "full"
        if name.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB")):
            return "corrupt"
        if name.startswith("SQLITE_CONSTRAINT"):
            return "integrity"  # chyba programu: opakování stejné dávky by selhalo stejně
        return "io"  # IOERR, READONLY, CANTOPEN, PERM i neznámé: zavřít, znovu otevřít s odstupem
    if error.reason == "corrupt":
        # Chyba I/O při ověřování souboru ještě není poškození; quick_check ≠ ok (bez jména) ano.
        return "io" if name.startswith(_TRANSIENT_OPEN) else "corrupt"
    if error.reason in ("locked", "path"):
        return error.reason  # jiný zapisovač nebo chybějící soubor či právo: čekat, nikdy nevytvářet
    if error.reason in _IDENTITY:
        return "identity"
    return "integrity"  # batch, run: porušené pořadí dávek nebo stav běhu


@dataclass(frozen=True)
class _Measurement:
    seq: int
    results: tuple  # zmrazené CheckResult: snímek v okamžiku předání
    monotonic: float  # monotónní čas měření
    quality: str  # synced, nebo unsynced (před první synchronizací v tomto procesu)


@dataclass(frozen=True)
class _Change:
    seq: int
    change: history.Change
    monotonic: float


@dataclass(frozen=True)
class _Event:
    seq: int
    event: history.Event


@dataclass(frozen=True)
class _Work:
    """Obsah jedné dávky vybraný pod zámkem; SQLite až mimo zámek."""

    measurements: tuple  # _Measurement
    changes: tuple  # _Change
    events: tuple  # history.Event (včetně záznamů ztrát)
    losses: Mapping  # druh -> počet zapisovaný touto dávkou
    offset: float | None  # UTC − monotonic pro odvození času, nebo None

    @property
    def size(self):
        return len(self.measurements) + len(self.changes) + len(self.events)


class _Loss:
    """Známá ztráta jednoho druhu: počet, hodnoty a dostupné časové hranice (nic se neodhaduje)."""

    __slots__ = ("count", "values", "utc_from", "utc_to", "mono_from", "mono_to", "checks")

    def __init__(self):
        self.count = self.values = 0
        self.utc_from = self.utc_to = self.mono_from = self.mono_to = None
        self.checks = None  # check_id -> _Loss u zahozených změn (nespojitost historie kontroly)

    def add(self, count, values, utc, monotonic, check_id=None):
        if check_id is not None:
            if self.checks is None:
                self.checks = {}
            self.checks.setdefault(check_id, _Loss()).add(count, 0, utc, monotonic)
        self.count += count
        self.values += values
        if utc is not None:
            self.utc_from = utc if self.utc_from is None else min(self.utc_from, utc)
            self.utc_to = utc if self.utc_to is None else max(self.utc_to, utc)
        if monotonic is not None:
            self.mono_from = monotonic if self.mono_from is None else min(self.mono_from, monotonic)
            self.mono_to = monotonic if self.mono_to is None else max(self.mono_to, monotonic)


class _Maintenance:
    """Počitadla údržby v paměti; mění je jen vlákno zapisovače pod zámkem."""

    __slots__ = ("aggregation_monotonic", "aggregation_utc", "done_5m", "done_1h", "windows_5m",
                 "windows_1h", "blocked", "conflict", "more", "retention_monotonic",
                 "retention_blocked", "retention_stopped", "deleted", "retention_more",
                 "failures", "last_error", "wal_bytes")

    def __init__(self):
        self.aggregation_monotonic = self.aggregation_utc = None
        self.done_5m = self.done_1h = None
        self.windows_5m = self.windows_1h = 0
        self.blocked = self.conflict = None
        self.more = False
        self.retention_monotonic = self.retention_blocked = None
        self.retention_stopped = {}
        self.deleted = Counter()
        self.retention_more = False
        self.failures = 0
        self.last_error = None
        self.wal_bytes = None


@dataclass(frozen=True)
class MaintenanceStatus:
    """Snímek automatické údržby pro history.storage; čte se bez SQLite."""

    aggregation_monotonic: float | None = None  # poslední dokončený pokus o agregaci
    aggregation_utc: int | None = None  # jen při synchronizovaném čase
    done_5m: int | None = None  # potvrzená hranice agregace z posledního pokusu
    done_1h: int | None = None
    windows_5m: int = 0  # vložená okna od startu
    windows_1h: int = 0
    blocked: str | None = None  # time_unsynced, queue_unknown, conflict
    conflict: str | None = None  # doklad konfliktu; agregace je do konce běhu zastavená
    more: bool = False  # zbývají uzavřená okna na další pokus
    retention_monotonic: float | None = None  # poslední dokončený běh retence
    retention_blocked: str | None = None  # time_unsynced, clock_unstable, clock_jump_recent, …
    retention_stopped: Mapping = field(default_factory=dict)  # tabulka -> důvod zastavení
    deleted: Mapping = field(default_factory=dict)  # tabulka -> smazané řádky od startu
    retention_more: bool = False
    stable_s: int | None = None  # doložená doba souvislé synchronizace hodin
    failures: int = 0  # po sobě jdoucí selhání údržby
    last_error: str | None = None
    wal_bytes: int | None = None  # velikost -wal při posledním zápisu nebo pokusu o údržbu


@dataclass(frozen=True)
class WorkerStatus:
    """Snímek stavu pro budoucí history.storage; čte se bez SQLite."""

    state: str
    run_id: int | None
    commits: int  # potvrzené dávky
    last_commit_monotonic: float | None
    last_commit_utc: int | None  # jen při synchronizovaném čase, jinak None
    pending_measurements: int
    pending_values: int
    pending_changes: int
    pending_events: int
    in_flight: int  # položky zapečetěné dávky, která čeká na potvrzení (výsledek zatím nejistý)
    confirmed_measurements: int  # potvrzené commitem od startu
    confirmed_changes: int
    confirmed_events: int
    held_unsynced: int  # měření a změny čekající na první synchronizaci hodin
    time_base: str  # unsynced (synchronizace zatím nebyla), synced, sync_lost
    losses: Mapping  # druh -> počet od startu (queue_full, changes_dropped, …)
    unrecorded_losses: Mapping  # druh -> počet, který ještě není zapsaný v databázi
    error_kind: str | None
    last_error: str | None
    consecutive_failures: int
    retry_in: float | None  # sekundy do dalšího pokusu (monotónní), nebo None
    # Monotónní čas nejstarší nepotvrzené zapisovatelné práce: nejstarší měření ve
    # frontě, nebo začátek prvního pokusu o zápis dávky (opakování ho nevynuluje).
    # None = nic nepotvrzeného nečeká. Data čekající na synchronizaci hodin se sem
    # nepočítají (hlásí se zvlášť jako held_unsynced).
    unconfirmed_since: float | None = None
    maintenance: MaintenanceStatus = field(default_factory=MaintenanceStatus)


@dataclass(frozen=True)
class StopReport:
    """Výsledek stop(): potvrzená data jsou v status.confirmed_*, nezapsaná a nejistá zvlášť."""

    completed: bool  # vlákno skončilo v limitu; False = dál visí (typicky v systémovém I/O)
    clean_stop: bool  # finish_run potvrzen: run.clean_stop = 1 v databázi
    unsaved_measurements: int  # ve frontě: jistě nezapsané
    unsaved_changes: int
    unsaved_events: int
    in_doubt: int  # položky nepotvrzené dávky: commit mohl i nemusel proběhnout
    status: WorkerStatus


class HistoryWorker:
    """Jediné vlákno zapisovače historie s omezenými frontami (HISTORY.md 5.1–5.6).

    `monotonic` a `utc_now` jsou vyměnitelné hodiny (v testech simulované),
    `opener` otevírá databázi (výchozí open_writer; testy přes něj simulují chyby).
    Kvalitu času nese každé submit() (`time_synced`); skutečný zdroj
    synchronizace dodá M3.3b. Vlákno je démonické: zaseknuté I/O nezablokuje
    ukončení procesu, ale stop() ho nedokáže přerušit (StopReport.completed).
    """

    def __init__(self, path, role, host_id, *, app_version, interval_s, boot_id=None,
                 monotonic=time.monotonic, utc_now=None, opener=None, batch_seconds=BATCH_SECONDS,
                 flapping_checks=FLAPPING_CHECKS, event_capacity=EVENT_CAPACITY):
        if isinstance(interval_s, bool) or not isinstance(interval_s, int) or not 1 <= interval_s <= 3600:
            raise ValueError(f"interval_s musí být celé číslo 1–3600, ne {interval_s!r}")
        self._app_version, self._interval_s, self._boot_id = app_version, interval_s, boot_id
        self._path = path  # jen pro velikost -wal (stat souboru, nikdy SQLite)
        self._monotonic = monotonic
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._opener = opener or (lambda: history.open_writer(path, role, host_id))
        self._batch_seconds = batch_seconds
        # Čerstvost pozorování se odvozuje od skutečné frekvence kolektoru.
        self.sync_fresh_seconds = max(VALIDITY_INTERVALS * interval_s, MIN_SYNC_FRESH_SECONDS)
        per_hour = math.ceil(QUEUE_SECONDS / interval_s)
        self.measurement_capacity = per_hour  # hodina měření (HISTORY.md 5.2)
        self.change_capacity = per_hour * flapping_checks
        self.event_capacity = event_capacity
        self._cond = threading.Condition()
        # Sdílený stav (jen pod self._cond).
        self._measurements, self._changes, self._events = deque(), deque(), deque()
        self._queued_values = 0  # součet výsledků ve frontě měření (paměťový strop)
        self._confirmed = Counter()
        self._tracker = history.ChangeTracker()  # R4 už při submit (hlavní vlákno)
        self._seq = 0
        self._offset = None  # UTC − monotonic z první synchronizace v tomto procesu
        self._synced_last = None
        self._sync_lost = False
        self._unsynced_seen = False
        self._losses = {}  # druh -> _Loss: známé ztráty ještě nezařazené do dávky
        self._loss_totals = Counter()
        self._in_flight_losses = Counter()
        self._state = STARTING
        self._run_id = None
        self._commits = 0
        self._last_commit = (None, None)
        self._in_flight = 0
        self._attempt = None  # pokus o zápis dávky: None, "writing", "rolled_back" (jistě nezapsáno), "in_doubt"
        self._attempt_since = None  # monotónní čas prvního pokusu o zápis této dávky
        self._error = (None, None)
        self._failures = 0
        self._retry_at = None
        self._stop_requested = False
        self._synced_since = None  # monotónní začátek souvislé doložené synchronizace
        self._synced_seen = None  # monotónní čas posledního potvrzení synchronizace
        self._last_seen = None  # (monotónní čas, UTC) posledního měření: detekce skoku
        self._maint = _Maintenance()
        self._aggregate_at = 0.0  # monotónní čas dalšího pokusu o agregaci
        self._retain_at = 0.0
        self._flush_target = 0
        self._clean_stop = False
        self._thread = None
        self._start_synced = False
        # Jen vlákno zapisovače (nikdy pod zámkem při I/O).
        self._database = None
        self._recorder = None
        self._batch = None
        self._work = None

    # --- Hlavní vlákno: bez SQLite, zámek jen na práci v paměti ------------------------

    def start(self, time_synced=None):
        """Spustí vlákno zapisovače; databázi otevře až ono. `time_synced` platí pro process.start."""
        if self._thread is not None:
            raise RuntimeError("zapisovač už byl spuštěn")
        with self._cond:
            if time_synced is True:
                moment = self._utc_now().timestamp()
                if moment >= history.MIN_TRUSTED_UTC:
                    now = self._monotonic()
                    self._offset = moment - now
                    self._synced_last = True
                    # Stabilita se počítá od prvního doloženého pozorování v tomto
                    # procesu: po restartu je nulová, takže retence 24 h nemaže.
                    self._synced_since = self._synced_seen = now
                    self._last_seen = (now, moment)
            self._start_synced = self._offset is not None
        self._thread = threading.Thread(target=self._run, name="nexusstat-history", daemon=True)
        self._thread.start()

    def submit(self, results, measured_monotonic, time_synced):
        """Předá výsledky jednoho měření vláknu zapisovače; nikdy nečeká na SQLite.

        `measured_monotonic` je monotónní čas měření, `time_synced` stav
        synchronizace hodin při něm: True, False nebo None (neznámý = nedůvěryhodný).
        Ukládá se n-tice zmrazených CheckResult, takže pozdější změny v hlavním
        vlákně zařazená data nezmění. Vrací False, když se po stop() už nic nepřijímá.
        """
        results = tuple(results)
        for result in results:
            if not isinstance(result, CheckResult):
                raise TypeError(f"očekáván CheckResult, ne {type(result).__name__}")
        if not is_finite_number(measured_monotonic):
            raise ValueError("measured_monotonic musí být konečné číslo")
        if time_synced is not True and time_synced is not False and time_synced is not None:
            raise ValueError("time_synced musí být True, False nebo None")
        with self._cond:
            if self._stop_requested:
                self._lose("after_stop", 1, len(results))
                return False
            self._seq += 1
            results, truncated = self._trim(results)
            quality = self._time_quality(results, measured_monotonic, time_synced)
            for check_id in truncated:
                # Rozsah ztráty jen z důvěryhodného času; jinak zůstane monotónní.
                moment = None if quality != "synced" else math.floor(results[0].observed_utc.timestamp())
                self._lose("result_truncated", 1, 0, moment, measured_monotonic, check_id)
            for result in results:
                if result.simulated:
                    continue  # simulace nevytváří změny; vzorky odmítne a započte zapisovač
                change = self._tracker.observe(result, math.floor(result.observed_utc.timestamp()), quality)
                if change is not None:
                    self._push(self._changes, _Change(self._seq, change, measured_monotonic), self.change_capacity)
            self._push(self._measurements, _Measurement(self._seq, results, measured_monotonic, quality),
                       self.measurement_capacity)
            self._queued_values += len(results)
            while self._queued_values > MAX_QUEUE_VALUES and len(self._measurements) > 1:
                self._drop(self._measurements.popleft())  # pevný strop paměti: zahodí nejstarší
            self._cond.notify_all()
            return True

    def submit_event(self, event):
        """Důležitá událost: vlastní omezená fronta a okamžité probuzení zapisovače.

        Vrací False, když se událost nepřijala: po stop(), nebo je větší než
        MAX_EVENT_BYTES (započte se jako ztráta event_too_large).
        """
        if not isinstance(event, history.Event):
            raise TypeError(f"očekávána Event, ne {type(event).__name__}")
        row = event.row(None)
        size = sum(len(row[name].encode()) for name in ("kind", "message", "check_id", "data") if row[name])
        with self._cond:
            if self._stop_requested:
                self._lose("after_stop", 1, 0)
                return False
            if size > MAX_EVENT_BYTES:
                self._lose("event_too_large", 1, 0, event.ts if event.ts_quality != "unsynced" else None)
                return False
            self._seq += 1
            self._push(self._events, _Event(self._seq, event), self.event_capacity)
            self._cond.notify_all()
            return True

    def status(self):
        with self._cond:
            return self._status()

    def wake(self):
        """Probudí vlákno, aby znovu vyhodnotilo čas (po posunu simulovaných hodin v testu)."""
        with self._cond:
            self._cond.notify_all()

    def wait_until(self, predicate, timeout):
        """Čeká (bez aktivního dotazování) na stav, pro který predicate(WorkerStatus) platí."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not predicate(self._status()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def flush(self, timeout):
        """Požádá o okamžitý zápis všeho zapisovatelného a čeká na potvrzení; False = nepotvrzeno.

        Měření a změny čekající na synchronizaci hodin zapsat nejdou (zůstávají
        ve frontě); flush na ně nečeká a status() je ukáže v held_unsynced.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            target = self._seq
            self._flush_target = max(self._flush_target, target)
            self._cond.notify_all()
            while self._has_writable(target) or self._in_flight or self._ready_losses(final=False):
                if self._state in (FAILED, STOPPED) or self._thread is None:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def stop(self, timeout=STOP_TIMEOUT_SECONDS):
        """Čisté ukončení: poslední zápis (nejvýš FINAL_ATTEMPTS pokusů), finish_run, zavření.

        Čeká nejvýš `timeout` s. Vlákno zaseknuté v systémovém I/O Python
        násilně ukončit neumí: pak completed = False, zámek databáze drží dál
        a uvolní ho až konec procesu (vlákno je démonické).
        """
        with self._cond:
            self._stop_requested = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
        completed = self._thread is None or not self._thread.is_alive()
        with self._cond:
            status = self._status()
            unsaved = [status.pending_measurements, status.pending_changes, status.pending_events]
            in_doubt = 0
            if self._work is not None:
                if self._attempt in ("writing", "in_doubt"):
                    in_doubt = self._in_flight  # COMMIT probíhá nebo jeho výsledek není znám
                else:  # dávka ještě nezapisovaná nebo vrácená před COMMIT: jistě nezapsaná
                    work = self._work
                    unsaved = [unsaved[0] + len(work.measurements), unsaved[1] + len(work.changes),
                               unsaved[2] + len(work.events)]
            return StopReport(completed, self._clean_stop and completed, *unsaved, in_doubt, status)

    # --- Pomocné pod zámkem ------------------------------------------------------------

    def _trim(self, results):
        """Zkrátí příliš dlouhé výsledky; vrací (výsledky, id zkrácených kontrol).

        Omezení textu platí už při předání, takže fronty nikdy nedrží neomezeně
        velká data a zapečetěná dávka se kvůli velikosti nemusí měnit.
        """
        trimmed, truncated = [], []
        for result in results:
            result, cut = trim_result(result)
            if cut:
                truncated.append(result.id)
            trimmed.append(result)
        return tuple(trimmed), truncated

    def _time_quality(self, results, monotonic, synced):
        """Kvalita času měření; první synchronizace dá posun pro odvození čekajících (HISTORY.md 4.3)."""
        observed = results[0].observed_utc.timestamp() if results else None
        self._track_stability(monotonic, observed, synced)
        if synced and self._offset is None and observed is not None and observed >= history.MIN_TRUSTED_UTC:
            self._offset = observed - monotonic
            if self._unsynced_seen:
                self._clock_synced_event(math.floor(observed))
        if self._offset is None:
            self._unsynced_seen = True
            self._synced_last = synced
            return "unsynced"
        # Po první synchronizaci je ztráta synchronizace jen událost; data se dál zapisují.
        moment = math.floor(observed if observed is not None else self._utc_now().timestamp())
        quality = "synced" if moment >= history.MIN_TRUSTED_UTC else "unsynced"
        if self._synced_last is True and not synced and not self._sync_lost:
            self._sync_lost = True
            self._push(self._events, _Event(self._seq, history.Event(
                moment, quality, "clock.sync_lost", "WARNING", "synchronizace hodin ztracena; čas běží s poslední korekcí")),
                self.event_capacity)
        elif synced and self._sync_lost:
            self._sync_lost = False
            self._push(self._events, _Event(self._seq, history.Event(
                moment, quality, "clock.sync_restored", "INFO", "synchronizace hodin obnovena")), self.event_capacity)
        self._synced_last = synced
        return "synced"

    def _track_stability(self, monotonic, observed, synced):
        """Posune nebo zruší evidenci souvislé synchronizace hodin (pro retenci).

        Stabilita se počítá jen z doložených odpovědí zdroje (`True`): ztráta
        i neznámý stav ji ruší. Zruší ji také skok času, tedy změna posunu
        UTC − monotonic mezi měřeními nad `history.CLOCK_JUMP_SECONDS` (stejný
        práh jako u události `clock.jump`, HISTORY.md 4.3: běžné doladění NTP je
        řádově milisekundy za sekundu), a mezera mezi pozorováními delší než
        `sync_fresh_seconds`: za dobu, kdy se nikdo neptal, není co dokládat.
        Falešně zjištěný skok nebo mezera retenci jen odloží o dalších 24 h,
        nikdy nerozšíří, co se smí smazat.
        """
        jumped = False
        if observed is not None and self._last_seen is not None:
            seen_monotonic, seen_utc = self._last_seen
            drift = (observed - seen_utc) - (monotonic - seen_monotonic)
            jumped = abs(drift) > history.CLOCK_JUMP_SECONDS
        if observed is not None:
            self._last_seen = (monotonic, observed)
        if synced is not True:
            self._synced_since = self._synced_seen = None  # nedoložený stav nic nedokládá
            return
        stale = self._synced_seen is None or monotonic - self._synced_seen > self.sync_fresh_seconds
        if jumped or stale or self._synced_since is None:
            self._synced_since = monotonic  # začátek nového souvislého úseku
        self._synced_seen = monotonic

    def _clock_state(self):
        """ClockState pro retenci: doložená doba **průběžně** potvrzované synchronizace.

        Dokládá se jen rozsah mezi prvním a posledním potvrzením jedné nepřerušené
        řady pozorování (nikdy čas, kdy se nikdo neptal), a to jen tehdy, když je
        poslední potvrzení čerstvé (`sync_fresh_seconds`). Samotný uplynulý
        monotónní čas tedy nic nedokládá: zastavená nebo mrtvá měřicí smyčka
        retenci zablokuje. Po startu procesu, po ztrátě i neznámém stavu
        synchronizace, po skoku času a po dlouhé mezeře v měření začíná řada
        znovu od nuly, takže prvních 24 h běhu retence nemaže nic (HISTORY.md 7).
        Nelze-li vyloučit ztracený doklad skoku hodin (ztráta událostí), je stav
        rovněž neznámý.
        """
        if self._offset is None or self._synced_last is not True:
            return history.ClockState.unknown()
        if self._synced_since is None or self._synced_seen is None:
            return history.ClockState.unknown()
        if self._monotonic() - self._synced_seen > self.sync_fresh_seconds:
            return history.ClockState.unknown()  # poslední potvrzení už není čerstvé
        if any(self._unrecorded_loss(kind) for kind in CLOCK_EVIDENCE_LOSSES):
            return history.ClockState.unknown()  # nezapsaná ztráta mohla být doklad clock.jump
        return history.ClockState.synced_for(max(0, int(self._synced_seen - self._synced_since)))

    def _unrecorded_loss(self, kind):
        """Počet ztrát daného druhu, které ještě nejsou zapsané v databázi."""
        loss = self._losses.get(kind)
        return (loss.count if loss is not None else 0) + self._in_flight_losses.get(kind, 0)

    def _queue_state(self):
        """Stav fronty pro uzavírání oken (HISTORY.md 6.1) a kvalita času „teď“.

        `empty()` jen tehdy, když to lze doložit: synchronizovaný čas, žádné
        čekající měření a žádná vybraná ani zapečetěná dávka. Čekající
        nesynchronizované vzorky znamenají `unknown()`, protože čas UTC nemají;
        stejně tak měření bez výsledků, u kterého čas určit nelze. Z toho, že
        zapisovač momentálně nezapisuje, se úplnost okna nedovozuje.
        """
        quality = "synced" if self._offset is not None and self._synced_last is True else "unsynced"
        if self._offset is None or self._batch is not None or self._work is not None or self._in_flight:
            return history.QueueState.unknown(), quality
        if any(item.quality == "unsynced" for item in self._measurements):
            return history.QueueState.unknown(), quality
        if not self._measurements:
            return history.QueueState.empty(), quality
        moments = [math.floor(item.results[0].observed_utc.timestamp())
                   for item in self._measurements if item.results]
        if not moments or min(moments) < history.MIN_TRUSTED_UTC:
            return history.QueueState.unknown(), quality
        return history.QueueState.waiting_since(min(moments)), quality

    def _maintenance_snapshot(self):
        """Neměnný snímek údržby; doplní doloženou stabilitu hodin."""
        maint = self._maint
        state = self._clock_state()
        return MaintenanceStatus(
            maint.aggregation_monotonic, maint.aggregation_utc, maint.done_5m, maint.done_1h,
            maint.windows_5m, maint.windows_1h, maint.blocked, maint.conflict, maint.more,
            maint.retention_monotonic, maint.retention_blocked,
            MappingProxyType(dict(maint.retention_stopped)), MappingProxyType(dict(maint.deleted)),
            maint.retention_more, state.stable_s if state.synced else None,
            maint.failures, maint.last_error, maint.wal_bytes)

    def _clock_synced_event(self, moment):
        held_m = [item.monotonic for item in self._measurements if item.quality == "unsynced"]
        held_c = [item.monotonic for item in self._changes if item.change.ts_quality == "unsynced"]
        monotonic = held_m + held_c
        data = {"held_measurements": len(held_m), "held_changes": len(held_c), "boot_id": self._boot_id}
        if monotonic:
            data.update(mono_from=round(min(monotonic), 3), mono_to=round(max(monotonic), 3),
                        **{"from": math.floor(min(monotonic) + self._offset), "to": math.floor(max(monotonic) + self._offset)})
        self._push(self._events, _Event(self._seq, history.Event(
            moment, "synced", "clock.synced", "INFO", "hodiny synchronizovány; čekající data dostanou odvozený čas",
            data=data)), self.event_capacity)

    def _push(self, queue, item, capacity):
        if len(queue) >= capacity:
            self._drop(queue.popleft())  # nejstarší; ztráta se započte s hranicemi
        queue.append(item)

    def _drop(self, item):
        if isinstance(item, _Measurement):
            self._queued_values -= len(item.results)
            trusted = item.quality == "synced" and item.results
            utc = math.floor(item.results[0].observed_utc.timestamp()) if trusted else None
            self._lose("queue_full", 1, len(item.results), utc, item.monotonic)
        elif isinstance(item, _Change):
            utc = item.change.ts if item.change.ts_quality == "synced" else None
            self._lose("changes_dropped", 1, 0, utc, item.monotonic, item.change.check_id)
        else:
            utc = item.event.ts if item.event.ts_quality != "unsynced" else None
            self._lose("events_dropped", 1, 0, utc, None)

    def _lose(self, kind, count, values, utc=None, monotonic=None, check_id=None):
        if utc is not None and utc < history.MIN_TRUSTED_UTC:
            utc = None  # nedůvěryhodný čas se do rozsahu ztráty nezapíše
        self._loss_totals[kind] += count
        if kind in LOSS_EVENTS:
            self._losses.setdefault(kind, _Loss()).add(count, values, utc, monotonic, check_id)

    def _ready_losses(self, final):
        """Ztráty, které lze zapsat: s rozsahem UTC, bez rozsahu, nebo už s posunem pro odvození.

        Ztráta jen s monotónním rozsahem čeká na synchronizaci (pak dostane odvozený
        rozsah UTC); při zastavení se zapíše jako unsynced jen s monotónním rozsahem.
        """
        return {kind: loss for kind, loss in self._losses.items()
                if final or loss.utc_from is not None or loss.mono_from is None or self._offset is not None}

    def _has_writable(self, target=None):
        limit = self._seq if target is None else target
        if any(item.seq <= limit for item in self._events):
            return True
        if self._offset is None:
            return False
        return bool((self._measurements and self._measurements[0].seq <= limit)
                    or (self._changes and self._changes[0].seq <= limit))

    def _status(self):
        now = self._monotonic()
        held = 0
        if self._offset is None:
            held = len(self._measurements) + len(self._changes)
        unrecorded = Counter({kind: loss.count for kind, loss in self._losses.items()})
        unrecorded.update(self._in_flight_losses)
        mono, utc = self._last_commit
        kind, text = self._error
        unconfirmed = self._attempt_since
        if self._offset is not None and self._measurements:
            oldest = self._measurements[0].monotonic
            unconfirmed = oldest if unconfirmed is None else min(unconfirmed, oldest)
        return WorkerStatus(
            self._state, self._run_id, self._commits, mono, utc, len(self._measurements),
            sum(len(item.results) for item in self._measurements), len(self._changes), len(self._events),
            self._in_flight, self._confirmed["m"], self._confirmed["c"], self._confirmed["e"], held,
            "unsynced" if self._offset is None else ("sync_lost" if self._sync_lost else "synced"),
            MappingProxyType(dict(self._loss_totals)), MappingProxyType(dict(unrecorded)), kind, text,
            self._failures, None if self._retry_at is None else max(0.0, self._retry_at - now),
            unconfirmed_since=unconfirmed, maintenance=self._maintenance_snapshot())

    def _take(self, final):
        """Vybere obsah dávky z front (pod zámkem). Bez synchronizace čekají měření i změny."""
        measurements, changes = [], []
        if self._offset is not None or final:
            while self._measurements:
                item = self._measurements.popleft()
                self._queued_values -= len(item.results)
                if item.quality == "unsynced" and self._offset is None:
                    # Konec bez synchronizace: vzorky se nezapíší, ztráta jen s monotónním rozsahem.
                    self._lose("time_unsynced", 1, len(item.results), None, item.monotonic)
                    continue
                measurements.append(item)
            changes = list(self._changes)
            self._changes.clear()
        events = [item.event for item in self._events]
        self._events.clear()
        losses = self._ready_losses(final)
        for kind in losses:
            del self._losses[kind]
        events += self._loss_events(losses)
        if not (measurements or changes or events):
            return None
        work = _Work(tuple(measurements), tuple(changes), tuple(events),
                     MappingProxyType({kind: loss.count for kind, loss in losses.items()}), self._offset)
        self._work = work
        self._in_flight = work.size
        self._in_flight_losses = Counter(work.losses)
        return work

    def _range(self, loss):
        """Hranice ztráty: UTC z důvěryhodného času, odvozené po synchronizaci, jinak jen monotónní."""
        data = {}
        if loss.utc_from is not None:
            data.update({"from": loss.utc_from, "to": loss.utc_to})
        elif loss.mono_from is not None and self._offset is not None:
            data.update({"from": math.floor(loss.mono_from + self._offset), "to": math.floor(loss.mono_to + self._offset)})
        if loss.mono_from is not None:
            data.update(mono_from=round(loss.mono_from, 3), mono_to=round(loss.mono_to, 3), boot_id=self._boot_id)
        return data

    def _loss_events(self, losses):
        events = []
        for kind, loss in losses.items():
            data = {"reason": kind, "count": loss.count}
            if loss.values:
                data["values"] = loss.values
            data.update(self._range(loss))
            if loss.checks:  # u zahozených změn po kontrolách: kde je historie nespojitá
                data["checks"] = {check: {"count": item.count, **self._range(item)} for check, item in loss.checks.items()}
            if loss.utc_to is None and loss.mono_from is not None and self._offset is not None:
                # Ztráta před synchronizací: rozsah UTC odvozený stejně jako u čekajících vzorků.
                span, moment, quality = loss.mono_to - loss.mono_from, data["to"], "derived"
            elif loss.utc_to is not None:
                span, moment, quality = loss.utc_to - loss.utc_from, loss.utc_to, "synced"
            else:  # bez důvěryhodného UTC: čas systému s kvalitou unsynced, rozsah jen monotónní
                span = 0 if loss.mono_from is None else loss.mono_to - loss.mono_from
                moment, quality = max(1, math.floor(self._utc_now().timestamp())), "unsynced"
            if kind in TEXT_LOSSES:
                severity = "WARNING"
                message = f"zkrácený text výsledku nad {MAX_RESULT_BYTES} B: {loss.count} výsledků"
                data["limit_bytes"] = MAX_RESULT_BYTES
            else:
                severe = kind in SEVERE_LOSSES or span > QUEUE_SECONDS
                severity = "ERROR" if severe else "WARNING"
                message = f"známá ztráta historie: {kind}, {loss.count} položek"
            events.append(history.Event(moment, quality, LOSS_EVENTS[kind], severity, message, data=data))
        return events

    def _set_failure(self, kind, error):
        self._error = (kind, str(error))
        if kind in FATAL_KINDS:
            self._state = FAILED
            self._retry_at = None
        else:
            self._failures += 1
            base, cap = BACKOFF[kind]
            self._retry_at = self._monotonic() + min(cap, base * 2 ** (self._failures - 1))
            self._state = DEGRADED
        self._cond.notify_all()

    # --- Vlákno zapisovače: jediné, kdo používá SQLite --------------------------------

    def _run(self):
        try:
            self._loop()
        except BaseException as error:
            # Chyba programu ve vlákně: stav FAILED s popisem a výjimka dál (nic se neskrývá).
            with self._cond:
                self._error = ("internal", f"{type(error).__name__}: {error}")
                self._state = FAILED
                self._cond.notify_all()
            raise
        finally:
            self._close_database()
            with self._cond:
                if self._state != FAILED:
                    self._state = STOPPED
                self._cond.notify_all()

    def _loop(self):
        while True:
            with self._cond:
                action = self._next_action()
            if action == "stop":
                self._final()
                return
            if action == "open":
                self._open()
            elif action == "aggregate":
                self._aggregate()
            elif action == "retain":
                self._retain()
            else:
                self._write()

    def _next_action(self):
        while True:
            if self._stop_requested:
                return "stop"
            now = self._monotonic()
            if self._state == FAILED:
                self._cond.wait(MAX_WAIT_SECONDS)  # nevratná chyba: už nic, jen čeká na stop()
                continue
            if self._retry_at is not None and now < self._retry_at:
                self._cond.wait(min(self._retry_at - now, MAX_WAIT_SECONDS))
                continue
            if self._database is None:
                return "open"
            due, wait = self._write_due(now)
            if due:
                return "write"  # čekající měření a změny mají vždy přednost před údržbou
            action, maintenance_wait = self._maintenance_due(now)
            if action is not None:
                return action
            waits = [value for value in (wait, maintenance_wait) if value is not None]
            self._cond.wait(min([MAX_WAIT_SECONDS, *waits]))

    def _write_due(self, now):
        if self._batch is not None or self._events or self._ready_losses(final=False):
            return True, None  # zapečetěná dávka k opakování, událost nebo záznam ztráty: hned
        if self._flush_target and self._has_writable(self._flush_target):
            return True, None
        if self._offset is None:
            return False, None  # měření i změny čekají na synchronizaci hodin
        if self._changes:
            return True, None  # změna stavu nečeká na minutový interval
        if self._measurements:
            age = now - self._measurements[0].monotonic
            return (True, None) if age >= self._batch_seconds else (False, self._batch_seconds - age)
        return False, None

    def _maintenance_due(self, now):
        """Co má údržba udělat teď ("aggregate", "retain") a za jak dlouho zkusit znovu.

        Údržba běží jen s otevřenou databází, bez nepotvrzené nebo nejisté dávky
        a nikdy při ukončování: zapsat čekající data je důležitější. Agregace se
        po konfliktu už nespouští (zapisovač ji do konce běhu odmítá), retence
        dál smí, protože hranici drží potvrzené `done_until` a nemůže se posunout.
        """
        if self._stop_requested or self._database is None or self._recorder is None:
            return None, None
        if self._batch is not None or self._work is not None or self._in_flight:
            return None, None
        if self._recorder.commit_in_doubt:
            return None, None  # výsledek předchozího commitu není znám
        if self._maint.conflict is None and now >= self._aggregate_at:
            return "aggregate", None
        if now >= self._retain_at:
            return "retain", None
        pending = [at - now for at in (self._aggregate_at, self._retain_at) if at > now]
        return None, min(pending) if pending else None

    def _wal_bytes(self):
        """Velikost souboru `-wal` této databáze (jen stat, nikdy SQLite).

        Cesta se rozvine (`realpath`), protože SQLite vytváří `-wal` vedle
        skutečného souboru, ne vedle symlinku. None znamená „nezjištěno“:
        chybějící soubor (zatím žádný WAL) i chyba čtení, nikdy nulová velikost.
        """
        try:
            return os.path.getsize(f"{os.path.realpath(self._path)}-wal")
        except OSError:
            return None

    def _aggregate(self):
        """Jeden pokus o agregaci uzavřených oken; vrací True při dokončení pokusu."""
        wal = self._wal_bytes()
        with self._cond:
            queue, quality = self._queue_state()
            self._maint.wal_bytes = wal
            self._aggregate_at = self._monotonic() + AGGREGATION_INTERVAL_SECONDS
        try:
            result = self._recorder.aggregate(self._utc_now(), quality, queue)
        except history.HistoryError as error:
            if error.reason == "conflict":
                # Rozpor s hotovou agregací: nic se nepřepsalo a do konce běhu se
                # neagreguje. Není to chyba zápisu měření, takže se jen hlásí.
                with self._cond:
                    self._maint.conflict, self._maint.blocked = str(error), "conflict"
                    self._cond.notify_all()
                return False
            self._maintenance_failed(error)
            return False
        with self._cond:
            maint = self._maint
            previous = maint.done_5m, maint.done_1h
            maint.aggregation_monotonic = self._monotonic()
            maint.aggregation_utc = math.floor(self._utc_now().timestamp()) if quality == "synced" else None
            maint.done_5m, maint.done_1h = result.done_5m, result.done_1h
            maint.windows_5m += result.windows_5m
            maint.windows_1h += result.windows_1h
            maint.blocked, maint.more = result.blocked, result.more
            maint.failures, maint.last_error = 0, None
            # Dohánění pokračuje hned, ale až po dalším kole smyčky (zápis a stop
            # mají přednost). Pokrokem je posun potvrzené hranice, ne počet
            # vložených oken: hodina bez měření je také hotová práce. Bez posunu
            # se čeká na interval, aby se dohánění nemohlo zacyklit.
            advanced = previous[0] is None or result.done_5m > previous[0] or result.done_1h > previous[1]
            if result.more and advanced:
                self._aggregate_at = 0.0
            self._cond.notify_all()
        return True

    def _retain(self):
        """Jeden běh retence (nejvýš 24 mazacích transakcí po hodině historie)."""
        wal = self._wal_bytes()
        with self._cond:
            clock = self._clock_state()
            self._maint.wal_bytes = wal
            self._retain_at = self._monotonic() + RETENTION_INTERVAL_SECONDS
        try:
            result = self._recorder.retain(self._utc_now(), clock)
        except history.HistoryError as error:
            self._maintenance_failed(error)
            return False
        with self._cond:
            maint = self._maint
            maint.retention_monotonic = self._monotonic()
            maint.retention_blocked = result.blocked
            maint.retention_stopped = dict(result.stopped)
            for table, count in result.deleted.items():
                maint.deleted[table] += count
            maint.retention_more = result.more
            maint.failures, maint.last_error = 0, None
            if result.more and result.transactions:
                self._retain_at = 0.0  # limit transakcí došel, zbytek po dalším kole
            self._cond.notify_all()
        return True

    def _handle_error(self, error):
        """Reakce na chybu zápisu dávky: druh, případné zavření spojení a odstup."""
        kind = classify(error)
        if kind == "io":
            self._close_database()  # nové spojení až po zavření starého (jediný zapisovač)
        with self._cond:
            self._set_failure(kind, error)

    def _maintenance_failed(self, error):
        """Selhání údržby: zápis měření nezastaví, jen se ohlásí a odloží.

        Agregace ani retence nejsou podmínkou sledování, takže přechodná chyba
        (zámek, plný disk, I/O) nesmí přepnout zapisovač do DEGRADED a zdržet
        zápis čekajících měření; odloží se jen další pokus o údržbu. Nevratná
        chyba (poškozená nebo cizí databáze, porušená integrita) je naopak chyba
        databáze jako celku a zastaví zapisovače stejně jako při zápisu.
        """
        kind = classify(error)
        with self._cond:
            self._maint.failures += 1
            self._maint.last_error = f"{kind}: {error}"
            if kind in FATAL_KINDS:
                self._set_failure(kind, error)
                return
            base, cap = BACKOFF[kind]
            delay = min(cap, base * 2 ** min(self._maint.failures - 1, 10))
            now = self._monotonic()
            self._aggregate_at = max(self._aggregate_at, now + delay)
            self._retain_at = max(self._retain_at, now + delay)
            self._cond.notify_all()

    def _open(self):
        """Otevře databázi (a při prvním otevření zahájí běh); vrací True při úspěchu."""
        try:
            database = self._opener()
        except history.HistoryError as error:
            with self._cond:
                self._set_failure(classify(error), error)
            return False
        if self._recorder is None:
            recorder = history.HistoryRecorder(database)
            quality = "synced" if self._start_synced else "unsynced"
            try:
                run_id = recorder.start_run(self._app_version, self._interval_s, self._utc_now(), self._boot_id, quality)
            except history.HistoryError as error:
                self._database = database
                self._close_database()
                with self._cond:
                    self._set_failure(classify(error), error)
                return False
            self._recorder = recorder
            with self._cond:
                self._run_id = run_id
        else:
            self._recorder.rebind(database)  # stejný běh, nové spojení téhož souboru
        self._database = database
        with self._cond:
            self._retry_at = None
            if self._batch is None:
                self._state, self._failures = RUNNING, 0
            # Otevření samo nic neagreguje ani nemaže: první agregace přijde po
            # první potvrzené dávce (HISTORY.md 5.1), jinak po intervalu, a
            # retence nejdřív za hodinu. Start tak nepřidá žádnou transakci.
            now = self._monotonic()
            self._aggregate_at = max(self._aggregate_at, now + AGGREGATION_INTERVAL_SECONDS)
            self._retain_at = max(self._retain_at, now + RETENTION_INTERVAL_SECONDS)
            self._cond.notify_all()
        return True

    def _write(self):
        """Jedna dávka: nová jen bez nepotvrzené předchozí; zapečetěná se opakuje beze změny."""
        if self._batch is None:
            with self._cond:
                work = self._take(final=self._stop_requested)
            if work is None:
                return True
            self._build(work)
        with self._cond:
            self._attempt = "writing"
            if self._attempt_since is None:
                # Věk pokusu se počítá od prvního zápisu dávky: opakování po chybě
                # ani znovuotevření spojení skutečné zaseknutí neskryje.
                self._attempt_since = self._monotonic()
        try:
            self._recorder.write(self._batch)
        except history.HistoryError as error:
            with self._cond:
                self._attempt = "in_doubt" if self._recorder.commit_in_doubt else "rolled_back"
            self._handle_error(error)
            return False
        wal = self._wal_bytes()
        with self._cond:
            work, self._batch, self._work, self._attempt = self._work, None, None, None
            self._attempt_since = None
            self._confirmed.update(m=len(work.measurements), c=len(work.changes), e=len(work.events))
            self._in_flight, self._in_flight_losses = 0, Counter()
            self._commits += 1
            moment = self._utc_now().timestamp()
            self._last_commit = (self._monotonic(), math.floor(moment) if self._offset is not None else None)
            self._state, self._failures, self._retry_at = RUNNING, 0, None
            self._maint.wal_bytes = wal
            self._aggregate_at = 0.0  # po potvrzené dávce zkusit uzavřít okna (HISTORY.md 5.1)
            self._cond.notify_all()
        return True

    def _build(self, work):
        """Naplní novou dávku mimo zámek; čekající položky dostanou odvozený čas (derived)."""
        recorder = self._recorder
        batch = recorder.begin_batch()
        for item in work.measurements:
            if item.quality == "unsynced":
                moment = datetime.fromtimestamp(item.monotonic + work.offset, timezone.utc)
                results = tuple(replace(result, observed_utc=moment) for result in item.results)
                recorder.add_results(batch, results, "derived", track_changes=False)
            else:
                recorder.add_results(batch, item.results, item.quality, track_changes=False)
        for item in work.changes:
            change = item.change
            if change.ts_quality == "unsynced" and work.offset is not None:
                change = replace(change, ts=math.floor(item.monotonic + work.offset), ts_quality="derived")
            recorder.add_change(batch, change)
        for event in work.events:
            recorder.add_event(batch, event)
        self._batch = batch

    def _final(self):
        """Poslední zápis při stop(): bez čekání na odstup, nejvýš FINAL_ATTEMPTS neúspěchů."""
        failures = 0
        while failures < FINAL_ATTEMPTS:
            with self._cond:
                if self._state == FAILED:
                    return
            if self._database is None and not self._open():
                failures += 1
                continue
            if self._batch is None:
                with self._cond:
                    pending = self._measurements or self._changes or self._events or self._losses
                if not pending:
                    self._finish()
                    return
            if not self._write():
                failures += 1
        # Nepodařilo se: běh zůstane nečistý a příští start ho označí neznámou mezerou.

    def _finish(self):
        quality = "synced" if self._offset is not None else "unsynced"
        try:
            self._recorder.finish_run(self._utc_now(), quality)
        except history.HistoryError as error:
            with self._cond:
                self._error = (classify(error), str(error))
            return
        with self._cond:
            self._clean_stop = True

    def _close_database(self):
        database, self._database = self._database, None
        if database is None:
            return
        try:
            database.close()  # spojení i zámek se uvolní i při chybě (HistoryDatabase.close: finally)
        except sqlite3.Error as error:
            with self._cond:
                if self._error[0] is None:
                    self._error = ("io", f"zavření databáze: {error}")
