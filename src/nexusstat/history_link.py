"""Volitelné napojení historie na měřicí smyčku (M3.3b-1, docs/HISTORY.md 5.1).

Hlavní vlákno jen předá hotové vyhodnocené výsledky vláknu zapisovače
(`HistoryWorker.submit`) a přečte jeho snímek stavu; SQLite nevolá nikdy
a na zápis nečeká. Výpadek historie proto nezastaví měření ani alarmy: projeví
se jedině kontrolou `history.storage`.

Databáze se nikdy nevytváří ani nepřepisuje. Chybějící, cizí, poškozená nebo
nekompatibilní databáze znamená, že historie nezapisuje a kontrola to hlásí.
Role je vždy `development` (konfigurace jinou nenastaví) a cesta na síťovém
úložišti se odmítne.

Agregaci ani retenci tento krok nespouští (M3.3b-2).
"""

import math
from pathlib import Path

from . import clock_sync, history, history_worker, snapshot, snapshot_plan
from .config import HISTORY_ROLE
from .core import CheckResult, State
# Jediná definice kontrol cest a úložiště je ve storage.py (používá je i snapshot.py);
# jména zůstávají dostupná i přes tento modul, aby se přesunem nic nezměnilo.
from .storage import (  # noqa: F401
    MOUNTS_PATH,
    NETWORK_FILESYSTEMS,
    UNVERIFIABLE_FILESYSTEMS,
    StorageError,
    check_local_path as _check_local_path,
    filesystem_type,
    is_network_filesystem,
    resolve_path,
)
from .history_worker import DEGRADED, FAILED, STARTING, STOPPED, MAX_RESULT_BYTES, HistoryWorker

HISTORY_STORAGE_ID = "history.storage"
HISTORY_SNAPSHOT_ID = "history.snapshot"
LABEL = "HISTORY"  # krátký název pro STATUS a displej (docs/HISTORY.md 8)
SNAPSHOT_LABEL = "HIST SNAPSHOT"

# Stáří posledního snímku podle docs/HISTORY.md 9.3. Hranice jsou včetně:
# přesně 26 h je ještě INFO, přesně 50 h ještě WARNING.
SNAPSHOT_INFO_SECONDS = 26 * 3600
SNAPSHOT_WARNING_SECONDS = 50 * 3600
STOP_TIMEOUT_SECONDS = history_worker.STOP_TIMEOUT_SECONDS

# Druhy ztrát, při kterých data skutečně zmizela. Zkrácený text mezi ně nepatří:
# stav, hodnota ani identita kontroly se nezahodily.
DATA_LOSSES = frozenset({"queue_full", "time_unsynced", "changes_dropped", "events_dropped",
                         "event_too_large", "after_stop"})

# Zaseknutý zápis (visící fsync) nezmění stav zapisovače: zůstane RUNNING, jen se
# nepotvrzuje žádná dávka. Pozná se podle toho, že data čekají a poslední
# potvrzení je starší než několik dávkovacích intervalů (dávka má 60 s).
STALE_COMMIT_SECONDS = 3 * history_worker.BATCH_SECONDS

# Zpoždění agregace, které už není běžný provoz: normálně je hranice hotových oken
# jen minuty za „teď“ (okno 5 min + rezerva 120 s + dávka 60 s, HISTORY.md 6.1).
# Hodina znamená, že agregace nepostupuje, i když se zápis nehlásí jako chybný.
AGGREGATION_LAG_SECONDS = 3600
# Velikost WAL, při které je něco v nepořádku (dlouhá čtecí transakce, HISTORY.md 8, 10.6).
WAL_WARNING_BYTES = 64 * 1024 * 1024


def _counts(losses):
    return ", ".join(f"{kind} {count}" for kind, count in sorted(losses.items()) if count)


def _maintenance_text(maint):
    """Krátký souhrn automatické údržby do popisu kontroly."""
    parts = []
    if maint.aggregation_monotonic is None:
        parts.append("agregace zatím neproběhla")
    elif maint.conflict is not None:
        parts.append("agregace zastavena rozporem")
    elif maint.blocked:
        parts.append(f"agregace čeká ({maint.blocked})")
    else:
        parts.append(f"agregace {maint.windows_5m}+{maint.windows_1h} oken")
    if maint.retention_monotonic is None:
        parts.append("retence zatím neproběhla")
    elif maint.retention_blocked:
        parts.append(f"retence blokována ({maint.retention_blocked})")
    else:
        parts.append(f"retence smazala {sum(maint.deleted.values())} řádků")
    return ", ".join(parts)


def storage_state(status, *, queue_full=False, stalled=False, time_synced=None, submit_failures=0,
                  source=None, now_utc=None, snapshot_running=None):
    """Stav a popis kontroly `history.storage`; `status` None = historie vypnutá.

    Pořadí rozhoduje: doložená ztráta a zastavený zapisovač před přechodnou
    chybou, přechodná chyba před stavem hodin. Historie se nikdy neoznačí za
    v pořádku jen proto, že měření jako celek pokračuje: hlásí výhradně to, co
    dokládá snímek `WorkerStatus`. CRITICAL historie nepoužívá (docs/HISTORY.md 8):
    ztráta historie není havárie Nexusu.
    """
    if status is None:
        return State.NA, "historie vypnutá v konfiguraci"
    pending = (f"ve frontě {status.pending_measurements} měření, {status.pending_changes} změn, "
               f"{status.pending_events} událostí")
    losses = {kind: count for kind, count in status.losses.items() if kind in DATA_LOSSES}
    if submit_failures:
        return State.ERROR, f"předání výsledků do historie selhalo ({submit_failures}×)"
    if status.state == FAILED:
        return State.ERROR, f"zapisovač zastaven ({status.error_kind}: {status.last_error}); {pending}"
    if any(losses.values()):
        return State.ERROR, f"ztráta dat ({_counts(losses)}); {pending}"
    if queue_full:
        return State.ERROR, f"fronta plná, nejstarší data se zahazují; {pending}"
    if status.maintenance.conflict is not None:
        # Hotová agregace se nikdy nepřepíše: rozpor je chyba k zásahu správce
        # a agregace je do konce běhu zastavená (HISTORY.md 6.2).
        return State.ERROR, f"agregace zastavena rozporem: {status.maintenance.conflict}"
    if status.state == STOPPED:
        return State.ERROR, f"vlákno zapisovače neběží; {pending}"
    if status.state == DEGRADED:
        retry = "" if status.retry_in is None else f", další pokus za {status.retry_in:.0f} s"
        return State.WARNING, f"zápis selhává ({status.error_kind}: {status.last_error}){retry}; {pending}"
    if status.state == STARTING:
        return State.UNKNOWN, f"zapisovač zatím neotevřel databázi; {pending}"
    if status.time_base == "unsynced":
        reason = "stav synchronizace hodin neznámý" if time_synced is None else "hodiny nesynchronizované"
        return State.WARNING, (f"{reason} ({source}); {status.held_unsynced} položek čeká na čas; {pending}")
    if snapshot_running is not None:
        # Snímek drží vlákno zapisovače (kopie, kontrola, SHA-256): zápis měření
        # se mezitím hromadí ve frontách. Není to zaseknutý zápis, a dokud snímek
        # běží, nehlásí se jako takový; dlouhý snímek je ale varování.
        state = State.WARNING if snapshot_running >= STALE_COMMIT_SECONDS else State.INFO
        return state, (f"probíhá snímek historie ({snapshot_running:.0f} s), zápis měření zatím čeká; {pending}")
    if stalled:
        # Zápis se dlouho nepotvrdil, i když data čekají: typicky zaseknuté I/O.
        # Netvrdí se, že vlákno visí, jen že potvrzení chybí.
        return State.WARNING, (f"zápis se nepotvrdil déle než {STALE_COMMIT_SECONDS} s, data čekají; {pending}")
    if time_synced is None:
        return State.WARNING, (f"stav synchronizace hodin se nepodařilo zjistit ({source}); "
                               f"zapsáno {status.commits} dávek")
    if status.time_base == "sync_lost":
        return State.WARNING, (f"synchronizace hodin ztracena ({source}), čas běží s poslední korekcí; "
                               f"zapsáno {status.commits} dávek")
    truncated = status.losses.get("result_truncated", 0)
    if truncated:
        return State.WARNING, (f"zkrácený text {truncated} výsledků nad {MAX_RESULT_BYTES} B; "
                               f"zapsáno {status.commits} dávek")
    maint = status.maintenance
    maintenance = _maintenance_text(maint)
    if maint.failures:
        return State.WARNING, f"údržba selhává ({maint.failures}×: {maint.last_error}); {maintenance}"
    if maint.retention_stopped:
        return State.WARNING, (f"retence zastavena: {_counts(maint.retention_stopped)}; {maintenance}")
    if maint.wal_bytes is not None and maint.wal_bytes > WAL_WARNING_BYTES:
        return State.WARNING, (f"WAL má {maint.wal_bytes // 1_048_576} MiB (nad {WAL_WARNING_BYTES // 1_048_576} MiB); "
                               "pravděpodobně dlouhá čtecí transakce")
    lag = _aggregation_lag(status, now_utc)
    if lag is not None and lag > AGGREGATION_LAG_SECONDS:
        return State.WARNING, f"agregace je {lag // 60} min za aktuálním časem; {maintenance}"
    # Blokovaná retence není závada: prvních 24 h běhu se ještě nemaže vždy
    # (doložená stabilita hodin), stejně jako agregace čekající na frontu.
    if status.commits == 0:
        return State.INFO, f"zapisovač běží, zatím bez potvrzené dávky; {pending}; údržba: {maintenance}"
    return State.INFO, (f"zapsáno {status.commits} dávek, potvrzeno {status.confirmed_measurements} měření; "
                        f"{pending}; údržba: {maintenance}")


def _hours(seconds):
    return f"{seconds / 3600:.1f} h"


def _snapshot_notes(snap, status, plan_failures):
    """Rozdělí údaje na doložené závady a na informace, které jen patří do textu.

    Závada zhoršuje stav (poškozená generace v adresáři, selhávající
    plánování). Nepotvrzená auditní událost závada není: v běžném provozu se
    zapíše s další dávkou a zveřejněná generace na ní nezávisí.
    """
    problems, notes = [], []
    if snap.invalid:
        # Starší platná generace se použít smí, ale poškození novější se nesmí ztratit.
        problems.append(f"neověřitelné generace: {snap.invalid} (nejnovější {snap.invalid_name})")
    if plan_failures:
        problems.append(f"plánování snímku selhalo ({plan_failures}×)")
    if snap.event_seq is not None and status is not None and status.confirmed_seq < snap.event_seq:
        notes.append("událost o snímku zatím není potvrzená v databázi")
    return problems, notes


def snapshot_state(status, *, enabled, now_utc=None, prepare_error=None, plan_failures=0, next_run=None):
    """Stav a popis kontroly `history.snapshot` (docs/HISTORY.md 9.3).

    Počítá se jen z neměnného snímku stavu v paměti: hlavní vlákno kvůli
    diagnostice neotevírá SQLite ani nečte adresář generací. Stáří roste i
    tehdy, když se žádný pokus nespustí (zaseknutý zapisovač), protože se
    počítá od času posledního úspěšného snímku, ne od posledního pokusu.
    UNKNOWN se používá jen tam, kde se stáří ani stav doložit nedá, a nikdy
    nepřekryje doloženou závadu.
    """
    if not enabled:
        return State.NA, "vypnuto"  # záměrně vypnuté snímky, ne závada
    if prepare_error is not None:
        # Snímky jsou zapnuté, ale adresáře nelze použít: nic nevznikne.
        return State.ERROR, f"snímky nelze připravit: {prepare_error}"
    if status is None:
        return State.UNKNOWN, "stav zapisovače historie není k dispozici"
    snap = status.maintenance.snapshot
    problems, notes = _snapshot_notes(snap, status, plan_failures)
    plan = "" if next_run is None else f"; příští plán {next_run:%Y-%m-%d %H:%M} UTC"
    extra = ("; " + ", ".join(problems + notes)) if problems or notes else ""
    if snap.scan_error is not None:
        return State.ERROR, f"adresář generací nelze přečíst ({snap.scan_error}){extra}"
    if snap.scanned and snap.utc is None:
        return State.ERROR, f"žádná platná generace snímku{extra}"
    age = None
    if snap.utc is not None and now_utc is not None and status.time_base == "synced":
        age = now_utc - snap.utc
    last = "" if snap.generation is None else f", poslední {snap.generation}"
    if age is not None and age < 0:
        # Čas snímku je v budoucnosti: hodiny se posunuly zpět. Netvrdí se, že
        # je snímek aktuální, ani že chybí.
        return State.WARNING, (f"čas posledního snímku je {_hours(-age)} v budoucnosti{last}; "
                               f"hodiny se posunuly zpět{extra}")
    if snap.failures:
        # Doložený neúspěšný pokus: WARNING, dokud je předchozí snímek ještě
        # použitelný, jinak ERROR. Neznámé stáří závadu nepřekrývá.
        reason = f"poslední pokus o snímek selhal ({snap.failures}×: {snap.last_error})"
        if age is None:
            return State.WARNING, f"{reason}; stáří snímku nelze určit{last}{extra}{plan}"
        if age > SNAPSHOT_WARNING_SECONDS:
            return State.ERROR, f"{reason}; snímek je {_hours(age)} starý{last}{extra}"
        return State.WARNING, f"{reason}; snímek je {_hours(age)} starý{last}{extra}{plan}"
    if age is None:
        if not snap.scanned:
            return State.UNKNOWN, f"zapisovač zatím nezjistil existující generace{extra}"
        return State.UNKNOWN, (f"stáří snímku nelze určit (čas není doložený){last}{extra}")
    if age > SNAPSHOT_WARNING_SECONDS:
        return State.ERROR, f"poslední snímek je {_hours(age)} starý (nad 50 h){last}{extra}"
    if age > SNAPSHOT_INFO_SECONDS:
        return State.WARNING, f"poslední snímek je {_hours(age)} starý (nad 26 h){last}{extra}{plan}"
    if problems:
        # Aktuální snímek s poškozenou novější generací není v pořádku.
        return State.WARNING, f"poslední snímek je {_hours(age)} starý{last}{extra}{plan}"
    return State.INFO, f"poslední snímek je {_hours(age)} starý{last}{extra}{plan}"


def snapshot_result(status, observed_utc, expires_monotonic, **state_args):
    """CheckResult kontroly `history.snapshot` ze snímku stavu (bez SQLite).

    Stejně jako `history.storage` se tento výsledek do historie nikdy
    nepředává: jeho stav se mění podle stavu snímků, takže by se kontrola
    zapisovala do vlastní historie (zpětná vazba, docs/HISTORY.md 9.3).
    """
    state, text = snapshot_state(status, **state_args)
    details = {"snapshots": "off" if state is State.NA else "on"}
    return CheckResult(HISTORY_SNAPSHOT_ID, state, f"{SNAPSHOT_LABEL}: {text}", observed_utc,
                       expires_monotonic, simulated=False, details=details)


def _aggregation_lag(status, now_utc):
    """Zpoždění hranice hotových 5min oken za aktuálním časem, nebo None.

    Počítá se jen s doloženě synchronizovaným časem a známou hranicí: bez nich
    by rozdíl nic neznamenal.
    """
    done = status.maintenance.done_5m
    if now_utc is None or done is None or status.time_base != "synced":
        return None
    return max(0, now_utc - done)


def storage_result(status, observed_utc, expires_monotonic, **state_args):
    """CheckResult kontroly `history.storage` ze snímku stavu (bez SQLite).

    Do historie se tento výsledek nikdy nepředává: jeho stav se mění podle
    stavu historie, takže by zápis sám vytvářel nové změny k zápisu. Údaje
    `details` jsou proto i tak jen ustálené názvy stavů, ne počty z front.
    """
    state, text = storage_state(status, **state_args)
    details = {"state": "off" if status is None else status.state,
               "time": "off" if status is None else status.time_base}
    return CheckResult(HISTORY_STORAGE_ID, state, f"{LABEL}: {text}", observed_utc, expires_monotonic,
                       simulated=False, details=details)


def stop_lines(report, timeout=STOP_TIMEOUT_SECONDS):
    """Řádky o ukončení historie: co je potvrzené, co jistě nezapsané a co nejisté.

    Čisté ukončení se hlásí jen při potvrzeném `finish_run`, skončeném vlákně,
    prázdných frontách a bez nejisté dávky. O datech, jejichž commit nemá známý
    výsledek, se netvrdí, že jsou na disku.
    """
    status = report.status
    unsaved = report.unsaved_measurements + report.unsaved_changes + report.unsaved_events
    confirmed = (f"potvrzeno {status.commits} dávek (měření {status.confirmed_measurements}, "
                 f"změny {status.confirmed_changes}, události {status.confirmed_events})")
    lines = []
    if report.completed and report.clean_stop and not unsaved and not report.in_doubt:
        lines.append(f"Historie: čisté ukončení zapsáno, {confirmed}.")
    else:
        lines.append(f"Historie: čisté ukončení NENÍ doložené, {confirmed}; nezapsáno "
                     f"{report.unsaved_measurements} měření, {report.unsaved_changes} změn a "
                     f"{report.unsaved_events} událostí; {report.in_doubt} položek s nejistým výsledkem "
                     f"commitu; stav zapisovače {status.state}.")
    if status.last_error:
        lines.append(f"Historie: poslední chyba ({status.error_kind}): {status.last_error}")
    if status.losses:
        lines.append(f"Historie: ztráty od startu: {_counts(status.losses)}.")
    if status.unrecorded_losses:
        lines.append(f"Historie: v databázi nejsou zapsané ztráty: {_counts(status.unrecorded_losses)}; "
                     "zůstanou v historii jako neznámá mezera.")
    if not report.completed:
        lines.append(f"Historie: vlákno zapisovače po {timeout} s stále běží (typicky zaseknuté I/O). "
                     "Bezpečně ho přerušit nelze; zámek databáze uvolní až konec procesu.")
    return lines


class HistoryLink:
    """Most mezi měřicí smyčkou a vláknem zapisovače historie.

    `record()` předá výsledky a `check()` vrátí kontrolu `history.storage`;
    ani jedno nesmí vyhodit výjimku do smyčky měření, proto se chyby zachytí
    a započítají. Stav synchronizace hodin se zjišťuje read-only zdrojem
    (`clock_sync`) při každém měření; neznámý stav se nikdy nepředává jako True.
    """

    def __init__(self, worker, sync=None, *, output=print, path=None,
                 validity_seconds=None, timeout=STOP_TIMEOUT_SECONDS,
                 snapshot_enabled=False, plan=None, snapshot_error=None):
        self.worker = worker
        self.sync = sync if sync is not None else clock_sync.ClockSync()
        self.path = path
        self.validity_seconds = validity_seconds
        self.timeout = timeout
        self.submit_failures = 0
        # Snímky historie (M3.4b-1): `plan` je denní plán z konfigurace,
        # `snapshot_error` důvod, proč je zapnuté snímky nelze připravit.
        self.snapshot_enabled = snapshot_enabled
        self.snapshot_error = snapshot_error
        self.planner = None if plan is None else snapshot_plan.SnapshotPlanner(plan)
        self.plan_failures = 0
        self._output = output
        self._synced = None
        self._started = False
        self._stopped = False

    @property
    def sync_source(self):
        return self.sync.source

    @property
    def sync_note(self):
        """Doplňující informace o markeru systemd-timesyncd pro výpis.

        O aktuálním stavu hodin nerozhoduje (dokládá jen dřívější synchronizaci
        v tomto bootu); pomáhá rozpoznat, proč historie čeká na čas.
        """
        return clock_sync.first_sync_note()

    @property
    def time_synced(self):
        """Stav synchronizace z posledního dotazu (True, False, nebo None = neznámý)."""
        return self._synced

    def start(self):
        """Spustí vlákno zapisovače se stavem synchronizace hodin v okamžiku startu."""
        if self._started:
            raise RuntimeError("napojení historie už bylo spuštěno")
        self._started = True
        self._synced = self._read_sync()
        self.worker.start(time_synced=self._synced)

    def _read_sync(self):
        try:
            return self.sync.synced()
        except Exception:
            return None  # chyba zdroje = neznámý stav, nikdy synchronizovaný čas

    def record(self, results, measured_monotonic):
        """Předá vyhodnocené výsledky zapisovači; vrací True při přijetí.

        Výjimku do smyčky měření nepustí: historie nesmí zastavit CPU, RAM,
        disk, síť ani alarmy. Selhání se započítá a `history.storage` je ohlásí.
        """
        try:
            self._synced = self._read_sync()
            return bool(self.worker.submit(results, measured_monotonic, self._synced))
        except Exception as error:
            self.submit_failures += 1
            if self.submit_failures == 1:  # jednou, ne při každém měření
                self._output(f"Historie: předání výsledků selhalo ({type(error).__name__}: {error}); "
                             "měření pokračuje bez historie.")
            return False

    def check(self, now, observed_utc):
        """Kontrola `history.storage` ze snímku stavu zapisovače."""
        expires = now + (self.validity_seconds or 0)
        try:
            status = self.worker.status()
            queue_full = self._queues_full(status)
            stalled = self._stalled(status, now)
            snapshot_running = self._snapshot_running(status, now)
        except Exception as error:
            return CheckResult(HISTORY_STORAGE_ID, State.UNKNOWN,
                               f"{LABEL}: stav zapisovače nelze přečíst ({type(error).__name__}: {error})",
                               observed_utc, expires, simulated=False)
        return storage_result(status, observed_utc, expires, queue_full=queue_full, stalled=stalled,
                              time_synced=self._synced, submit_failures=self.submit_failures,
                              source=self.sync_source, now_utc=math.floor(observed_utc.timestamp()),
                              snapshot_running=snapshot_running)

    def plan_snapshot(self, now, observed_utc):
        """Požádá o denní snímek, když nastal plánovaný čas; vrací True při požádání.

        Rozhoduje se jen z doloženě synchronizovaného času (`None` ani `False`
        nestačí) a jen z neměnného snímku stavu: hlavní vlákno nečte SQLite ani
        adresáře. Snímek sám staví vlákno zapisovače a čekající měření mají
        přednost; při ukončování se nový snímek nezačíná (`request_snapshot`
        po `stop()` odmítne a plán se nepovažuje za vyřízený).
        """
        if self.planner is None or self._stopped or self._synced is not True:
            return False
        try:
            status = self.worker.status()
            if not self.planner.due(observed_utc, status.maintenance.snapshot):
                return False
            if not self.worker.request_snapshot():
                return False
            self.planner.note_requested(observed_utc)
            self._output(f"Historie: vyžádán denní snímek (plán {self.planner.attempted:%Y-%m-%d %H:%M} UTC).")
            return True
        except Exception as error:
            # Plánování nesmí zastavit měření; selhání se započítá a ohlásí.
            self.plan_failures += 1
            if self.plan_failures == 1:
                self._output(f"Historie: plánování snímku selhalo ({type(error).__name__}: {error}); "
                             "měření pokračuje.")
            return False

    def snapshot_check(self, now, observed_utc):
        """Kontrola `history.snapshot` ze snímku stavu zapisovače."""
        expires = now + (self.validity_seconds or 0)
        try:
            status = None if not self.snapshot_enabled else self.worker.status()
            next_run = self._next_run(status, observed_utc)
        except Exception as error:
            return CheckResult(HISTORY_SNAPSHOT_ID, State.UNKNOWN,
                               f"{SNAPSHOT_LABEL}: stav snímků nelze přečíst "
                               f"({type(error).__name__}: {error})", observed_utc, expires, simulated=False)
        return snapshot_result(status, observed_utc, expires, enabled=self.snapshot_enabled,
                               now_utc=math.floor(observed_utc.timestamp()),
                               prepare_error=self.snapshot_error, plan_failures=self.plan_failures,
                               next_run=next_run)

    def _next_run(self, status, observed_utc):
        """Čas příštího plánovaného snímku, nebo None (bez plánu či bez doloženého času)."""
        if self.planner is None or status is None or self._synced is not True:
            return None
        return self.planner.next_run(observed_utc, status.maintenance.snapshot)

    def _stalled(self, status, now):
        """True, když se nejstarší nepotvrzená zapisovatelná práce dlouho nepotvrdila.

        Rozhoduje věk konkrétní čekající práce (nejstarší měření ve frontě nebo
        probíhající pokus o zápis dávky), ne doba od startu vlákna ani od
        posledního potvrzení: první měření ani běžné čekání na minutovou dávku
        proto závada nejsou a zaseknutý první COMMIT se naopak pozná, i když jsou
        fronty prázdné. Data čekající na synchronizaci hodin se hlásí zvlášť.
        Zaseknuté I/O se jinak nepozná: vlákno zůstane ve stavu RUNNING a žádnou
        chybu nenahlásí, protože k jejímu vzniku ani nedojde.
        """
        since = status.unconfirmed_since
        return since is not None and now - since >= STALE_COMMIT_SECONDS

    def _snapshot_running(self, status, now):
        """Jak dlouho už běží snímek, nebo None, když žádný neběží."""
        snapshot = status.maintenance.snapshot
        if not snapshot.running or snapshot.started is None:
            return None
        return max(0.0, now - snapshot.started)

    def _queues_full(self, status):
        """True, když některá fronta dosáhla kapacity: od té chvíle se zahazuje nejstarší."""
        return (status.pending_measurements >= self.worker.measurement_capacity
                or status.pending_values >= history_worker.MAX_QUEUE_VALUES
                or status.pending_changes >= self.worker.change_capacity
                or status.pending_events >= self.worker.event_capacity)

    def stop(self):
        """Řízené ukončení historie s výpisem stavu; druhé volání už nic nedělá.

        Vrací StopReport, nebo None, když zapisovač nebyl spuštěn, ukončení už
        proběhlo, nebo se ho nepodařilo provést (pak se o datech nic netvrdí).
        """
        if self._stopped or not self._started:
            return None
        self._stopped = True
        try:
            report = self.worker.stop(self.timeout)
        except Exception as error:
            self._output(f"Historie: ukončení zapisovače selhalo ({type(error).__name__}: {error}); "
                         "o nezapsaných datech nelze nic tvrdit.")
            return None
        for line in stop_lines(report, self.timeout):
            self._output(line)
        return report


def check_local_path(path, mounts=None):
    """Povolí jen cestu na doloženě lokálním úložišti; vrací jeho typ.

    Obal nad `storage.check_local_path`, který hlásí chybu jako `HistoryError`,
    aby ji vlákno zapisovače zařadilo mezi chyby cesty (odstup, nikdy nový soubor).
    """
    try:
        return _check_local_path(path, mounts)
    except StorageError as error:
        raise history.HistoryError("path", str(error)) from None


def _open_writer(path, host_id, mounts=None):
    """Otevření databáze ve vlákně zapisovače: úložiště se ověří před i po otevření.

    Druhá kontrola zužuje okno mezi ověřením a otevřením (mezitím se mohlo něco
    připojit nebo vyměnit symlink). Odstranit ho úplně by znamenalo izolaci na
    úrovni jádra (Landlock), která na cílovém stroji není; zbytkové riziko je
    popsané v docs/HISTORY.md 11.5. Při změně se spojení i zámek hned uvolní.
    """
    before = check_local_path(path, mounts)
    database = history.open_writer(path, HISTORY_ROLE, host_id)
    try:
        after = check_local_path(path, mounts)
        if after != before:
            raise history.HistoryError(
                "path", f"úložiště cesty {path} se během otevírání změnilo ({before} → {after})")
    except history.HistoryError:
        database.close()
        raise
    return database


def prepare_snapshots(settings, mounts=None):
    """Ověří adresáře snímků podle `[history.snapshot]`; vrací (roots, plan, chyba).

    Volá se **před prvním měřením**, aby se neplatné nastavení poznalo hned a
    aby se nic nevytvářelo: adresáře musí už existovat (`snapshot.prepare`
    kontroluje symlinky, lokální úložiště, shodný souborový systém a atomické
    zveřejnění). Vypnuté snímky vrací (None, None, None); nepoužitelné
    adresáře nezastaví historii, jen se nic nesnímá a kontrola to ohlásí.
    """
    if not settings.enabled:
        return None, None, None
    try:
        roots = snapshot.prepare(settings.work_dir, settings.published_dir, mounts)
        plan = snapshot_plan.DailyPlan(*settings.daily(), settings.zone())
    except (snapshot.SnapshotError, OSError, ValueError) as error:
        return None, None, str(error)
    return roots, plan, None


def open_history(config, *, app_version, output=print, sync=None, host_id=None, boot_id=None, opener=None,
                 mounts=None):
    """Napojení historie podle sekce [history]; vypnutá historie vrací None.

    Databázi tu nic neotevírá (to dělá až vlákno zapisovače) a nic ji
    nevytváří. `HistoryError` nebo `OSError` znamená, že historii nelze ani
    připravit (např. nečitelné /etc/machine-id); volající pokračuje v měření
    bez historie.

    Snímky (`[history.snapshot]`) se připraví tady, tedy ještě před prvním
    měřením; jejich selhání historii nezastaví (kontrola `history.snapshot`).
    """
    if not config.history.enabled:
        return None
    path = Path(config.history.path)
    host = host_id if host_id is not None else history.local_host_id()
    settings = config.history.snapshot
    roots, plan, snapshot_error = prepare_snapshots(settings, mounts)
    worker = HistoryWorker(
        path, HISTORY_ROLE, host, app_version=app_version,
        interval_s=config.collector.interval_seconds,
        boot_id=boot_id if boot_id is not None else clock_sync.read_boot_id(),
        opener=opener if opener is not None else (lambda: _open_writer(path, host)),
        snapshot_roots=roots,
    )
    return HistoryLink(worker, sync, output=output, path=path,
                       validity_seconds=config.collector.validity_seconds,
                       snapshot_enabled=settings.enabled, plan=plan, snapshot_error=snapshot_error)
