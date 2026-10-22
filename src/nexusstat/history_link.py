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

import os
import re
from pathlib import Path, PurePosixPath

from . import clock_sync, history, history_worker
from .config import HISTORY_ROLE
from .core import CheckResult, State
from .history_worker import DEGRADED, FAILED, STARTING, STOPPED, MAX_RESULT_BYTES, HistoryWorker

HISTORY_STORAGE_ID = "history.storage"
LABEL = "HISTORY"  # krátký název pro STATUS a displej (docs/HISTORY.md 8)
STOP_TIMEOUT_SECONDS = history_worker.STOP_TIMEOUT_SECONDS

MOUNTS_PATH = "/proc/self/mounts"

# Typy připojení, u kterých cílové úložiště dopředu ověřit nejde: autofs připojí
# cíl až při prvním přístupu a bývá to NFS nebo CIFS. Odmítají se stejně jako
# síťové úložiště: lépe nezapsat nic než zapsat historii na NAS.
UNVERIFIABLE_FILESYSTEMS = frozenset({"autofs"})

# Síťová úložiště, na která databáze historie nepatří: SQLite tam nemá
# spolehlivé zámky ani fsync a připojení může zmizet uprostřed transakce.
# NAS je navíc mimo rozsah tohoto kroku.
NETWORK_FILESYSTEMS = frozenset({
    "cifs", "smb2", "smb3", "smbfs", "nfs", "nfs3", "nfs4", "afs", "ceph", "glusterfs",
    "9p", "ncpfs", "lustre", "beegfs", "orangefs", "gfs2", "ocfs2",
    "sshfs", "davfs", "webdav", "s3fs", "rclone", "curlftpfs", "smbnetfs",
})

# Zaseknutý zápis (visící fsync) nezmění stav zapisovače: zůstane RUNNING,
# jen se nepotvrzuje žádná dávka. Pozná se podle toho, že data čekají a poslední
# potvrzení je starší než několik dávkovacích intervalů (dávka má 60 s).
STALE_COMMIT_SECONDS = 3 * history_worker.BATCH_SECONDS

# Druhy ztrát, při kterých data skutečně zmizela. Zkrácený text mezi ně nepatří:
# stav, hodnota ani identita kontroly se nezahodily.
DATA_LOSSES = frozenset({"queue_full", "time_unsynced", "changes_dropped", "events_dropped",
                         "event_too_large", "after_stop"})


def _unescape(field):
    # /proc/self/mounts kóduje mezeru, tabulátor, nový řádek a zpětné lomítko osmičkově.
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)


def resolve_path(path):
    """Cesta po rozvinutí symlinků a odstranění `.` a `..` (`os.path.realpath`).

    Rozhoduje skutečné místo souboru, ne zápis cesty: symlink v poslední části
    ani v nadřazeném adresáři nesmí historii propašovat na jiné úložiště.
    Neexistující poslední část se rozvine podle existující předpony.
    """
    return PurePosixPath(os.path.realpath(path))


def filesystem_type(path, mounts=None):
    """Typ souborového systému, na kterém cesta skutečně leží.

    Rozhoduje nejdelší bod připojení, který je předponou cesty **po rozvinutí
    symlinků**; při stejně dlouhém bodu platí poslední záznam, protože pozdější
    připojení překryje starší. None znamená, že typ zjistit nelze (nečitelná
    tabulka připojení, žádný odpovídající bod); volající pak zápis odmítne.
    Tabulka se čte až tady, protože se za běhu mění.
    """
    try:
        lines = Path(mounts or MOUNTS_PATH).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    target = resolve_path(path)
    found = None
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue  # neúplný řádek: z takové tabulky se nic nedovozuje
        point = PurePosixPath(_unescape(fields[1]))
        # Porovnávají se části cesty, ne text: /srv/nas-zaloha neleží pod /srv/nas.
        if target != point and point not in target.parents:
            continue
        if found is None or len(point.parts) >= len(found[0].parts):
            found = (point, _unescape(fields[2]))
    return None if found is None else found[1]


def is_network_filesystem(kind):
    name = kind.split(".", 1)[1] if kind.startswith("fuse.") else kind
    return name in NETWORK_FILESYSTEMS


def check_local_path(path, mounts=None):
    """Povolí jen cestu na doloženě lokálním úložišti; vrací jeho typ.

    Odmítne síťové úložiště (NAS, CIFS, NFS, 9p, sshfs…), autofs (cíl se
    připojí až při přístupu) a stejně tak stav, kdy úložiště **ověřit nelze**
    (nečitelná nebo neúplná tabulka připojení, cesta mimo všechny body
    připojení). Raději se nezapíše nic, než aby historie skončila na NAS.
    Kontroluje se při každém otevření, protože připojení se mění za běhu.
    """
    kind = filesystem_type(path, mounts)
    if kind is None:
        raise history.HistoryError(
            "path", f"úložiště cesty {path} nelze ověřit (tabulka připojení); historie se nezapisuje")
    if is_network_filesystem(kind):
        raise history.HistoryError(
            "path", f"{path} leží na síťovém souborovém systému {kind}; historie se tam nezapisuje")
    if kind in UNVERIFIABLE_FILESYSTEMS:
        raise history.HistoryError(
            "path", f"{path} leží pod {kind}; cílové úložiště se připojí až při přístupu a nelze ho ověřit")
    return kind


def _counts(losses):
    return ", ".join(f"{kind} {count}" for kind, count in sorted(losses.items()) if count)


def storage_state(status, *, queue_full=False, stalled=False, time_synced=None, submit_failures=0,
                  source=None):
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
    if status.commits == 0:
        return State.INFO, f"zapisovač běží, zatím bez potvrzené dávky; {pending}"
    return State.INFO, (f"zapsáno {status.commits} dávek, potvrzeno {status.confirmed_measurements} měření; "
                        f"{pending}")


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
                 validity_seconds=None, timeout=STOP_TIMEOUT_SECONDS):
        self.worker = worker
        self.sync = sync if sync is not None else clock_sync.ClockSync()
        self.path = path
        self.validity_seconds = validity_seconds
        self.timeout = timeout
        self.submit_failures = 0
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
        except Exception as error:
            return CheckResult(HISTORY_STORAGE_ID, State.UNKNOWN,
                               f"{LABEL}: stav zapisovače nelze přečíst ({type(error).__name__}: {error})",
                               observed_utc, expires, simulated=False)
        return storage_result(status, observed_utc, expires, queue_full=queue_full, stalled=stalled,
                              time_synced=self._synced, submit_failures=self.submit_failures,
                              source=self.sync_source)

    def _stalled(self, status, now):
        """True, když data čekají a poslední dávka se dlouho nepotvrdila.

        Zaseknuté I/O se jinak nepozná: vlákno zůstane ve stavu RUNNING a žádnou
        chybu nenahlásí, protože ani nedojde k jejímu vzniku.
        """
        if not status.pending_measurements:
            return False
        last = status.last_commit_monotonic
        return last is None or now - last >= STALE_COMMIT_SECONDS

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


def open_history(config, *, app_version, output=print, sync=None, host_id=None, boot_id=None, opener=None):
    """Napojení historie podle sekce [history]; vypnutá historie vrací None.

    Databázi tu nic neotevírá (to dělá až vlákno zapisovače) a nic ji
    nevytváří. `HistoryError` nebo `OSError` znamená, že historii nelze ani
    připravit (např. nečitelné /etc/machine-id); volající pokračuje v měření
    bez historie.
    """
    if not config.history.enabled:
        return None
    path = Path(config.history.path)
    host = host_id if host_id is not None else history.local_host_id()
    worker = HistoryWorker(
        path, HISTORY_ROLE, host, app_version=app_version,
        interval_s=config.collector.interval_seconds,
        boot_id=boot_id if boot_id is not None else clock_sync.read_boot_id(),
        opener=opener if opener is not None else (lambda: _open_writer(path, host)),
    )
    return HistoryLink(worker, sync, output=output, path=path,
                       validity_seconds=config.collector.validity_seconds)
