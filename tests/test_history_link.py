"""Integrační testy napojení historie na měřicí smyčku (M3.3b-1).

Vše nad dočasnými databázemi v tempfile.TemporaryDirectory, se simulovanými
hodinami (monotónní i UTC) a náhradním zdrojem synchronizace; žádné skutečné
čekání na minutové dávky a žádná cesta Nexusu. Kolektory čtou vzorové soubory,
takže výsledek nezávisí na zatížení ani síti počítače.

Kolektor nad vzorovými soubory označuje data `simulated=True` (docs/HISTORY.md
4.6), což se tu ověřuje zvlášť. Pro cestu skutečných dat se tytéž výsledky
znovu označí jako neskutečné (`replace(..., simulated=False)`): struktura,
jednotky i `details` jsou pak přesně takové, jaké dává skutečný kolektor.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from unittest import mock

from nexusstat import history, history_link, snapshot, storage
from nexusstat.alarms import AlarmEvaluator
from nexusstat.config import (
    AlarmRule,
    AlarmsConfig,
    CollectorConfig,
    Config,
    HistoryConfig,
    SnapshotConfig,
)
from nexusstat.core import CheckResult, Measurement, State
from nexusstat.snapshot_plan import SnapshotPlanner
from nexusstat.history_link import (
    DATA_LOSSES,
    HISTORY_SNAPSHOT_ID,
    HISTORY_STORAGE_ID,
    HistoryLink,
    _open_writer,
    check_local_path,
    filesystem_type,
    is_network_filesystem,
    open_history,
    prepare_snapshots,
    resolve_path,
    snapshot_state,
    storage_result,
    storage_state,
    stop_lines,
)
from nexusstat.history_worker import (
    DEGRADED,
    FAILED,
    MAX_RESULT_BYTES,
    RUNNING,
    STARTING,
    STOPPED,
    HistoryWorker,
    MaintenanceStatus,
    SnapshotStatus,
    StopReport,
    WorkerStatus,
)
from nexusstat.network_monitor import INTERFACE, IPV4_ID, LINK, LINK_ID, RX_RATE_ID, TX_RATE_ID, NetworkMonitor
from nexusstat.run import Collectors, run_loop
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID, SystemMonitor

HOST = "0123456789abcdef"
BOOT = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"  # vymyšlený boot_id, ne ze stroje
CREATED = 1_800_001_234
BASE = 1_800_003_600  # synchronizovaný čas začátku testů
WRONG = 1_740_000_000  # chybný čas systému před synchronizací (před MIN_TRUSTED_UTC)
WAIT = 5.0  # skutečný limit čekání na vlákno; při správné funkci se nevyčerpá
INTERVAL = CollectorConfig().interval_seconds
VALIDITY = CollectorConfig().validity_seconds

# Vzorové vstupy kolektorů (stejné formáty jako v testech kolektorů).
STAT_1 = "cpu  100 0 50 800 50 0 0 0 0 0\n"
STAT_2 = "cpu  200 0 100 1550 100 0 50 0 30 0\n"  # 20 % vytížení
MEMINFO = "MemTotal:        8000000 kB\nMemFree:         1000000 kB\nMemAvailable:    6000000 kB\n"
ROUTE_HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"


class FakeStatvfs:
    f_frsize, f_blocks, f_bfree, f_bavail = 4096, 50_000_000, 40_000_000, 37_500_000


class Clock:
    """Simulované hodiny: monotónní čas a systémové UTC = monotónní + posun."""

    def __init__(self, mono=1000.0, utc=BASE):
        self._lock = threading.Lock()
        self.mono = mono
        self.offset = utc - mono
        self.sleeps = []
        self.stop_at = None

    def __call__(self):
        return self.monotonic()

    def monotonic(self):
        with self._lock:
            return self.mono

    def utc(self):
        with self._lock:
            return datetime.fromtimestamp(self.mono + self.offset, timezone.utc)

    def advance(self, seconds):
        with self._lock:
            self.mono += seconds

    def set_utc(self, utc):
        """Krok systémového času (např. NTP po startu); monotónní čas běží dál."""
        with self._lock:
            self.offset = utc - self.mono

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) > 1000:
            raise AssertionError("smyčka spí, ale čas neubíhá")
        self.advance(seconds)
        if self.stop_at is not None and self.monotonic() >= self.stop_at:
            raise KeyboardInterrupt


class Sync:
    """Náhradní zdroj synchronizace hodin: posloupnost stavů nebo chyba."""

    def __init__(self, *states, source="test"):
        self.states = list(states) or [True]
        self.source = source
        self.calls = 0
        self.error = None

    def synced(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.states[0] if len(self.states) == 1 else self.states.pop(0)


def mountinfo(*entries):
    """Syntetický obsah /proc/self/mountinfo z položek "zdroj bod typ" v pořadí připojení.

    Rodičem každého mountu je dřív připojený mount na nejdelším bodu, který je
    předponou jeho bodu (při shodě pozdější): pozdější mount na stejném bodě tak
    leží nad dřívějším a vnořený mount nad tím, na který byl připojen. Bod
    s mezerou se zapisuje osmičkově (\\040) jako v jádře. Další pole položky
    (volby ze starého formátu /proc/self/mounts) se ignorují.
    """
    lines, mounted = [], []
    for mount_id, entry in enumerate(entries, start=21):
        source, point, fstype = entry.split()[:3]
        target = PurePosixPath(point.replace("\\040", " "))
        below = [(len(other.parts), other_id) for other_id, other in mounted
                 if other == target or other in target.parents]
        parent = max(below)[1] if below else 1
        mounted.append((mount_id, target))
        lines.append(f"{mount_id} {parent} 0:{mount_id} / {point} rw shared:{mount_id} - {fstype} {source} rw\n")
    return "".join(lines)


def maintenance_status(**changes):
    """Snímek údržby pro testy diagnostiky."""
    return MaintenanceStatus(**changes)


def worker_status(**changes):
    """Snímek stavu zapisovače pro testy kontroly history.storage."""
    fields = dict(state=RUNNING, run_id=1, commits=2, last_commit_monotonic=1000.0, last_commit_utc=BASE,
                  pending_measurements=0, pending_values=0, pending_changes=0, pending_events=0, in_flight=0,
                  confirmed_measurements=5, confirmed_changes=3, confirmed_events=1, held_unsynced=0,
                  time_base="synced", losses={}, unrecorded_losses={}, error_kind=None, last_error=None,
                  consecutive_failures=0, retry_in=None, unconfirmed_since=None,
                  maintenance=MaintenanceStatus())
    fields.update(changes)
    return WorkerStatus(**fields)


def stop_report(**changes):
    fields = dict(completed=True, clean_stop=True, unsaved_measurements=0, unsaved_changes=0,
                  unsaved_events=0, in_doubt=0, status=worker_status(state=STOPPED))
    fields.update(changes)
    return StopReport(**fields)


class FilesystemTest(unittest.TestCase):
    """Databáze historie smí jen na doloženě lokální úložiště (NAS nikdy).

    Testuje se výhradně nad fixturami tabulky připojení a dočasnými adresáři:
    nic se nepřipojuje ani neodpojuje.
    """

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        # realpath: /tmp samo může být symlink a cesty se porovnávají rozvinuté.
        self.folder = Path(os.path.realpath(self._temp.name))
        self.mounts = self.folder / "mounts"

    def write(self, *lines):
        """Tabulka připojení z položek "zdroj bod typ" v pořadí připojení (ve formátu mountinfo)."""
        self.mounts.write_text(mountinfo(*lines), encoding="utf-8")
        return self.mounts

    def nas_table(self, *extra):
        """Lokální korito a síťové úložiště na <folder>/nas."""
        return self.write("/dev/sda1 / ext4 rw 0 0", f"//server/share {self.folder}/nas cifs rw 0 0", *extra)

    def assertRefused(self, path, mounts, reason=None):
        with self.assertRaises(history.HistoryError) as caught:
            check_local_path(path, mounts)
        self.assertEqual(caught.exception.reason, "path")
        if reason is not None:
            self.assertIn(reason, str(caught.exception))
        return str(caught.exception)

    # --- základ ------------------------------------------------------------------------

    def test_longest_mount_point_decides(self):
        mounts = self.write("/dev/sda1 / ext4 rw 0 0", "//nas/data /srv/nas cifs rw 0 0")
        self.assertEqual(filesystem_type("/srv/nas/history.sqlite3", mounts), "cifs")
        self.assertEqual(filesystem_type("/srv/other/history.sqlite3", mounts), "ext4")

    def test_mount_point_itself(self):
        mounts = self.write("/dev/sda1 / ext4 rw 0 0", "//nas/data /srv/nas cifs rw 0 0")
        self.assertEqual(filesystem_type("/srv/nas", mounts), "cifs")

    def test_escaped_mount_point(self):
        mounts = self.write("/dev/sda1 / ext4 rw 0 0", "/dev/sdb1 /mnt/disk\\040kopie ext4 rw 0 0")
        self.assertEqual(filesystem_type("/mnt/disk kopie/history.sqlite3", mounts), "ext4")

    def test_network_filesystems_are_refused(self):
        for kind in ("cifs", "smb3", "nfs", "nfs4", "9p", "ceph", "fuse.sshfs", "sshfs", "lustre"):
            with self.subTest(kind=kind):
                mounts = self.write("/dev/sda1 / ext4 rw 0 0", f"zdroj /mnt/sit {kind} rw 0 0")
                self.assertTrue(is_network_filesystem(kind))
                self.assertRefused("/mnt/sit/history.sqlite3", mounts, "síťovém")

    def test_local_filesystems_pass(self):
        for kind in ("ext4", "btrfs", "xfs", "f2fs", "zfs", "ext2"):
            with self.subTest(kind=kind):
                mounts = self.write(f"/dev/sda1 / {kind} rw 0 0")
                self.assertFalse(is_network_filesystem(kind))
                self.assertEqual(check_local_path("/var/lib/nexusstat/history.sqlite3", mounts), kind)

    def test_similar_name_outside_the_mount_is_local(self):
        # /srv/nas-zaloha neleží pod /srv/nas: porovnávají se části cesty, ne text.
        mounts = self.write("/dev/sda1 / ext4 rw 0 0", "//nas/data /srv/nas cifs rw 0 0")
        self.assertEqual(check_local_path("/srv/nas-zaloha/history.sqlite3", mounts), "ext4")
        self.assertEqual(check_local_path("/srv/nasty.sqlite3", mounts), "ext4")

    # --- neověřitelné úložiště ---------------------------------------------------------

    def test_unreadable_mount_table_is_refused(self):
        # Neověřitelná identita = žádný zápis (dřív se taková cesta propustila).
        self.assertIsNone(filesystem_type("/tmp/x.sqlite3", self.mounts / "neexistuje"))
        self.assertRefused("/tmp/x.sqlite3", self.mounts / "neexistuje", "nelze ověřit")

    def test_table_without_matching_mount_point_is_refused(self):
        mounts = self.write("/dev/sdb1 /jinam ext4 rw 0 0")  # chybí i korito
        self.assertRefused("/srv/history.sqlite3", mounts, "nelze ověřit")

    def test_incomplete_lines_are_not_trusted(self):
        # Neúplná tabulka nic nevylučuje: nečitelný řádek mohl být právě síťový mount nad cestou.
        for broken in ("nesmysl\n", "\n", f"30 21 0:40 / {self.folder}/nas rw - cifs //192.0.2.20/share\n"):
            with self.subTest(broken=broken):
                self.mounts.write_text(mountinfo("/dev/sda1 / ext4") + broken, encoding="utf-8")
                self.assertRefused(self.folder / "nas" / "history.sqlite3", self.mounts, "nelze ověřit")
                self.assertRefused("/srv/history.sqlite3", self.mounts, "nelze ověřit")

    def test_oversized_mount_table_is_refused(self):
        self.mounts.write_bytes(mountinfo("/dev/sda1 / ext4").encode() + b"x" * (4 * 1024 * 1024))
        self.assertRefused("/srv/history.sqlite3", self.mounts, "nelze ověřit")

    def test_autofs_is_refused(self):
        # autofs připojí cíl až při přístupu a bývá to NFS nebo CIFS.
        mounts = self.write("/dev/sda1 / ext4 rw 0 0", "systemd-1 /net autofs rw 0 0")
        self.assertRefused("/net/nexusstat/history.sqlite3", mounts, "autofs")
        self.assertEqual(check_local_path("/jinde/history.sqlite3", mounts), "ext4")

    # --- symlinky a nenormalizované cesty ----------------------------------------------

    def test_symlink_to_network_mount_is_refused(self):
        mounts = self.nas_table()
        (self.folder / "nas").mkdir()
        (self.folder / "odkaz").symlink_to(self.folder / "nas")
        self.assertRefused(self.folder / "odkaz" / "history.sqlite3", mounts, "síťovém")

    def test_symlink_in_parent_directory_is_refused(self):
        mounts = self.nas_table()
        (self.folder / "nas" / "podadresar").mkdir(parents=True)
        (self.folder / "rodic").symlink_to(self.folder / "nas")
        self.assertRefused(self.folder / "rodic" / "podadresar" / "history.sqlite3", mounts, "síťovém")

    def test_symlink_to_missing_target_resolves_to_its_place(self):
        # Viselý symlink do síťového úložiště se také odmítne (rozvine se předpona).
        mounts = self.nas_table()
        (self.folder / "odkaz").symlink_to(self.folder / "nas")
        self.assertRefused(self.folder / "odkaz" / "history.sqlite3", mounts, "síťovém")

    def test_dot_dot_in_path_is_resolved(self):
        mounts = self.nas_table(f"/dev/sdb1 {self.folder}/lokalni ext4 rw 0 0")
        (self.folder / "lokalni").mkdir()
        (self.folder / "nas").mkdir()
        self.assertRefused(self.folder / "lokalni" / ".." / "nas" / "history.sqlite3", mounts, "síťovém")
        self.assertEqual(check_local_path(self.folder / "lokalni" / ".." / "lokalni" / "history.sqlite3", mounts),
                         "ext4")
        # Cesta přes síťový mount se odmítne, i když se `..` vrátí na lokální úložiště:
        # zjistit, kam `nas/..` vede, by znamenalo sáhnout na kořen CIFS (zpřísnění opravy).
        self.assertRefused(self.folder / "nas" / ".." / "lokalni" / "history.sqlite3", mounts, "síťovém")

    def test_local_symlink_stays_allowed(self):
        mounts = self.nas_table(f"/dev/sdb1 {self.folder}/lokalni ext4 rw 0 0")
        (self.folder / "lokalni").mkdir()
        (self.folder / "odkaz").symlink_to(self.folder / "lokalni")
        self.assertEqual(check_local_path(self.folder / "odkaz" / "history.sqlite3", mounts), "ext4")

    # --- bind, vnořené a překrývající se mounty ----------------------------------------

    def test_bind_mount_of_network_storage_is_refused(self):
        # Bind mount má v tabulce vlastní řádek s typem původního systému.
        mounts = self.write("/dev/sda1 / ext4 rw 0 0", "//server/share/sub /srv/zaloha cifs rw 0 0")
        self.assertRefused("/srv/zaloha/history.sqlite3", mounts, "síťovém")

    def test_nested_mounts(self):
        mounts = self.write("/dev/sda1 / ext4 rw 0 0", "//server/share /srv/nas cifs rw 0 0",
                            "/dev/sdb1 /srv/nas/lokalni ext4 rw 0 0")
        # Lokální systém připojený dovnitř síťového se odmítne: každý přístup k cestě vede
        # přes síťový mount, který se může zaseknout (zpřísnění opravy, dřív vyhrál delší bod).
        self.assertRefused("/srv/nas/lokalni/history.sqlite3", mounts, "síťovém")
        self.assertRefused("/srv/nas/jine/history.sqlite3", mounts, "síťovém")
        # Lokální systém vnořený do lokálního zůstává povolený.
        mounts = self.write("/dev/sda1 / ext4 rw 0 0", "/dev/sdb1 /srv/data xfs rw 0 0",
                            "/dev/sdc1 /srv/data/db btrfs rw 0 0")
        self.assertEqual(check_local_path("/srv/data/db/history.sqlite3", mounts), "btrfs")
        self.assertEqual(check_local_path("/srv/data/x/history.sqlite3", mounts), "xfs")

    def test_overlapping_mounts_use_the_later_one(self):
        # Dva body připojení na stejném místě: platí pozdější (překryje starší).
        later_network = self.write("/dev/sda1 / ext4 rw 0 0", "/dev/sdb1 /srv/data ext4 rw 0 0",
                                   "//server/share /srv/data cifs rw 0 0")
        self.assertRefused("/srv/data/history.sqlite3", later_network, "síťovém")
        later_local = self.write("/dev/sda1 / ext4 rw 0 0", "//server/share /srv/data cifs rw 0 0",
                                 "/dev/sdb1 /srv/data ext4 rw 0 0")
        self.assertEqual(check_local_path("/srv/data/history.sqlite3", later_local), "ext4")

    # --- viditelný mount (regrese: dřív rozhodoval nejdelší bod připojení) ------------

    def test_mount_below_a_hidden_parent_is_refused(self):
        # Lokální <folder>/data leží na lokálním <folder>, přes který se později připojil CIFS:
        # <folder>/data pod ním není vidět a cesta ve skutečnosti leží na CIFS.
        (self.folder / "data").mkdir()
        mounts = self.write("/dev/sda1 / ext4", f"/dev/sdb1 {self.folder} ext4", f"/dev/sdc1 {self.folder}/data ext4",
                            f"//192.0.2.20/share {self.folder} cifs")
        self.assertRefused(self.folder / "data" / "history.sqlite3", mounts, "síťovém")
        # Opačně: síťový mount pod skrytým rodičem nebrání zápisu na viditelný lokální mount.
        mounts = self.write("/dev/sda1 / ext4", f"/dev/sdb1 {self.folder} ext4", f"//192.0.2.20/share {self.folder}/data cifs",
                            f"/dev/sdc1 {self.folder} ext4")
        self.assertEqual(check_local_path(self.folder / "data" / "history.sqlite3", mounts), "ext4")

    def test_ambiguous_mount_stack_is_refused(self):
        # Dva mounty se stejným rodičem na stejném bodě: který je vidět, nejde určit.
        self.mounts.write_text(mountinfo("/dev/sda1 / ext4")
                               + f"30 21 8:2 / {self.folder} rw - ext4 /dev/sdb1 rw\n"
                               + f"31 21 0:40 / {self.folder} rw - cifs //192.0.2.20/share rw\n", encoding="utf-8")
        self.assertRefused(self.folder / "history.sqlite3", self.mounts, "nelze ověřit")

    def test_unknown_filesystem_type_is_refused(self):
        # Neznámý typ se nikdy nepovažuje za lokální (dřív prošlo cokoli mimo seznam síťových).
        for kind in ("fuse", "fuse.gvfsd-fuse", "fuse.unknown", "foo"):
            with self.subTest(kind=kind):
                mounts = self.write("/dev/sda1 / ext4 rw 0 0", f"zdroj {self.folder} {kind} rw 0 0")
                self.assertRefused(self.folder / "history.sqlite3", mounts, "není doloženě lokální trvalé úložiště")

    def test_virtual_filesystems_are_refused(self):
        # Virtuální druh se pro SQLite nepovoluje: tmpfs a ramfs nepřežijí restart, overlay nemá
        # doložené podkladové úložiště, proc a sysfs nejsou úložiště (dřív tmpfs prošlo).
        for kind in ("tmpfs", "ramfs", "overlay", "proc", "sysfs", "devtmpfs"):
            with self.subTest(kind=kind):
                mounts = self.write("/dev/sda1 / ext4 rw 0 0", f"zdroj {self.folder} {kind} rw 0 0")
                self.assertRefused(self.folder / "history.sqlite3", mounts, "není doloženě lokální trvalé úložiště")
        # Lokální disk připojený dovnitř tmpfs: cesta vede přes virtuální mount, odmítne se také.
        mounts = self.write("/dev/sda1 / ext4", f"tmpfs {self.folder} tmpfs", f"/dev/sdb1 {self.folder}/data ext4")
        self.assertRefused(self.folder / "data" / "history.sqlite3", mounts, "tmpfs")
        # Lokální ext4 a btrfs dál projdou.
        for kind in ("ext4", "btrfs"):
            mounts = self.write("/dev/sda1 / ext4 rw 0 0", f"/dev/sdb1 {self.folder} {kind} rw 0 0")
            self.assertEqual(check_local_path(self.folder / "history.sqlite3", mounts), kind)

    def test_forbidden_storage_is_never_touched(self):
        # Rozvíjení cesty ani kontrola adresáře nesmí na síťové úložiště ani autofs sáhnout
        # (lstat by se na nedostupném NAS zasekl, pod autofs spustil připojení).
        (self.folder / "nas" / "sub").mkdir(parents=True)
        (self.folder / "auto" / "sub").mkdir(parents=True)
        (self.folder / "odkaz").symlink_to(self.folder / "nas")
        mounts = self.write("/dev/sda1 / ext4", f"//192.0.2.20/share {self.folder}/nas cifs",
                            f"systemd-1 {self.folder}/auto autofs")
        touched = []
        real_lstat, real_stat = os.lstat, os.stat

        def record(function):
            def wrapper(path, *args, **kwargs):
                touched.append(str(path))
                return function(path, *args, **kwargs)
            return wrapper

        calls = (lambda: check_local_path(self.folder / "odkaz" / "sub" / "history.sqlite3", mounts),
                 lambda: check_local_path(self.folder / "auto" / "sub" / "history.sqlite3", mounts),
                 lambda: storage.check_directory(self.folder / "nas" / "sub", "test", mounts),
                 lambda: storage.check_directory(self.folder / "auto" / "sub", "test", mounts))
        with mock.patch("os.lstat", record(real_lstat)), mock.patch("os.stat", record(real_stat)):
            for call in calls:
                with self.assertRaises((history.HistoryError, storage.StorageError)):
                    call()
        forbidden = [path for path in touched
                     if any(path == str(self.folder / name) or path.startswith(f"{self.folder / name}/")
                            for name in ("nas", "auto"))]
        self.assertEqual(forbidden, [])
        self.assertIn(str(self.folder / "odkaz"), touched)  # symlink na lokálním úložišti se přečíst smí

    def test_real_project_path_is_local(self):
        # Jediné čtení skutečné tabulky připojení: vývojový adresář musí projít.
        self.assertIsInstance(check_local_path(Path(__file__).resolve().parent), str)


class StorageStateTest(unittest.TestCase):
    """Kontrola history.storage rozlišuje stavy historie (docs/HISTORY.md 8)."""

    def test_history_off_is_not_applicable(self):
        state, text = storage_state(None)
        self.assertIs(state, State.NA)
        self.assertIn("vypnutá", text)

    def test_running_without_commit_does_not_claim_written_data(self):
        state, text = storage_state(worker_status(commits=0, pending_measurements=2, pending_values=8),
                                    time_synced=True)
        self.assertIs(state, State.INFO)
        self.assertIn("bez potvrzené dávky", text)

    def test_running_reports_confirmed_counts(self):
        state, text = storage_state(worker_status(commits=4, confirmed_measurements=12), time_synced=True)
        self.assertIs(state, State.INFO)
        self.assertIn("4 dávek", text)
        self.assertIn("12 měření", text)

    def test_starting_is_unknown(self):
        state, _ = storage_state(worker_status(state=STARTING, commits=0), time_synced=True)
        self.assertIs(state, State.UNKNOWN)

    def test_degraded_is_warning_with_reason_and_retry(self):
        state, text = storage_state(worker_status(state=DEGRADED, error_kind="io", last_error="chyba I/O",
                                                  retry_in=57.4, pending_measurements=3), time_synced=True)
        self.assertIs(state, State.WARNING)
        self.assertIn("io", text)
        self.assertIn("57 s", text)
        self.assertIn("3 měření", text)

    def test_failed_is_error(self):
        state, text = storage_state(worker_status(state=FAILED, error_kind="corrupt", last_error="quick_check"),
                                    time_synced=True)
        self.assertIs(state, State.ERROR)
        self.assertIn("zastaven", text)

    def test_stopped_thread_is_error(self):
        state, text = storage_state(worker_status(state=STOPPED), time_synced=True)
        self.assertIs(state, State.ERROR)
        self.assertIn("neběží", text)

    def test_confirmed_loss_is_error(self):
        for kind in sorted(DATA_LOSSES):
            with self.subTest(kind=kind):
                state, text = storage_state(worker_status(losses={kind: 2}), time_synced=True)
                self.assertIs(state, State.ERROR)
                self.assertIn(kind, text)

    def test_full_queue_is_error(self):
        state, text = storage_state(worker_status(pending_measurements=360), queue_full=True, time_synced=True)
        self.assertIs(state, State.ERROR)
        self.assertIn("fronta plná", text)

    def test_running_snapshot_is_not_a_stalled_write(self):
        # Snímek drží vlákno zapisovače: je to vidět, ale není to zaseknutý zápis.
        status = worker_status(pending_measurements=5, unconfirmed_since=1000.0,
                               maintenance=maintenance_status(
                                   aggregation_monotonic=1000.0,
                                   snapshot=SnapshotStatus(running=True, started=1000.0)))
        state, text = storage_state(status, stalled=True, snapshot_running=30.0, time_synced=True)
        self.assertIs(state, State.INFO)
        self.assertIn("probíhá snímek historie (30 s)", text)
        self.assertNotIn("nepotvrdil", text)

    def test_long_snapshot_is_a_warning(self):
        status = worker_status(pending_measurements=40, unconfirmed_since=1000.0,
                               maintenance=maintenance_status(
                                   snapshot=SnapshotStatus(running=True, started=1000.0)))
        state, text = storage_state(status, stalled=True,
                                    snapshot_running=history_link.STALE_COMMIT_SECONDS, time_synced=True)
        self.assertIs(state, State.WARNING)
        self.assertIn("probíhá snímek", text)

    def test_snapshot_does_not_hide_a_real_error(self):
        status = worker_status(state=FAILED, error_kind="corrupt", last_error="x",
                               maintenance=maintenance_status(
                                   snapshot=SnapshotStatus(running=True, started=1000.0)))
        state, _ = storage_state(status, snapshot_running=10.0, time_synced=True)
        self.assertIs(state, State.ERROR)

    def test_stalled_write_is_visible(self):
        # Zaseknutý zápis nezmění stav zapisovače; stav historie to přesto přizná.
        state, text = storage_state(worker_status(pending_measurements=12, pending_values=48),
                                    stalled=True, time_synced=True)
        self.assertIs(state, State.WARNING)
        self.assertIn("nepotvrdil", text)
        self.assertIn("12 měření", text)

    def test_stalled_does_not_outrank_a_real_error(self):
        state, _ = storage_state(worker_status(state=FAILED, error_kind="corrupt", last_error="x",
                                               pending_measurements=5), stalled=True, time_synced=True)
        self.assertIs(state, State.ERROR)

    def test_aggregation_conflict_is_error(self):
        state, text = storage_state(worker_status(maintenance=maintenance_status(
            aggregation_monotonic=1000.0, conflict="okno 1800003600 už existuje; doklad zapsán",
            blocked="conflict")), time_synced=True)
        self.assertIs(state, State.ERROR)
        self.assertIn("rozporem", text)
        self.assertIn("doklad", text)

    def test_maintenance_failure_is_warning(self):
        state, text = storage_state(worker_status(maintenance=maintenance_status(
            aggregation_monotonic=1000.0, failures=3, last_error="busy: database is locked")),
            time_synced=True)
        self.assertIs(state, State.WARNING)
        self.assertIn("údržba selhává (3×", text)

    def test_stopped_retention_is_warning(self):
        state, text = storage_state(worker_status(maintenance=maintenance_status(
            aggregation_monotonic=1000.0, retention_monotonic=1000.0,
            retention_stopped={"sample": "aggregation_mismatch"})), time_synced=True)
        self.assertIs(state, State.WARNING)
        self.assertIn("retence zastavena", text)
        self.assertIn("aggregation_mismatch", text)

    def test_large_wal_is_warning(self):
        state, text = storage_state(worker_status(maintenance=maintenance_status(
            aggregation_monotonic=1000.0, wal_bytes=history_link.WAL_WARNING_BYTES + 1)), time_synced=True)
        self.assertIs(state, State.WARNING)
        self.assertIn("WAL má 64 MiB", text)
        small = storage_state(worker_status(maintenance=maintenance_status(
            aggregation_monotonic=1000.0, wal_bytes=history_link.WAL_WARNING_BYTES)), time_synced=True)
        self.assertIs(small[0], State.INFO)

    def test_late_aggregation_is_warning(self):
        maintenance = maintenance_status(aggregation_monotonic=1000.0, done_5m=BASE)
        late = storage_state(worker_status(maintenance=maintenance), time_synced=True,
                             now_utc=BASE + history_link.AGGREGATION_LAG_SECONDS + 60)
        self.assertIs(late[0], State.WARNING)
        self.assertIn("agregace je", late[1])
        fresh = storage_state(worker_status(maintenance=maintenance), time_synced=True,
                              now_utc=BASE + history_link.AGGREGATION_LAG_SECONDS)
        self.assertIs(fresh[0], State.INFO)

    def test_late_aggregation_never_escalates_to_error(self):
        # Zpoždění je podezření, ne doklad poruchy: ERROR z něj nikdy není.
        maintenance = maintenance_status(aggregation_monotonic=1000.0, done_5m=BASE)
        for hours in (2, 24, 24 * 30):
            with self.subTest(hours=hours):
                state, text = storage_state(worker_status(maintenance=maintenance), time_synced=True,
                                            now_utc=BASE + hours * 3600)
                self.assertIs(state, State.WARNING)
                self.assertIn("agregace je", text)

    def test_aggregation_lag_needs_trustworthy_time(self):
        # Bez doloženě synchronizovaného času rozdíl nic neznamená.
        maintenance = maintenance_status(aggregation_monotonic=1000.0, done_5m=BASE)
        state, _ = storage_state(worker_status(time_base="sync_lost", maintenance=maintenance),
                                 time_synced=False, now_utc=BASE + 10 * 3600)
        self.assertIs(state, State.WARNING)  # hlásí se ztráta synchronizace, ne zpoždění
        self.assertIsNone(history_link._aggregation_lag(
            worker_status(time_base="unsynced", maintenance=maintenance), BASE + 10 * 3600))

    def test_blocked_retention_is_not_a_fault(self):
        # Prvních 24 h od startu se nemaže: je to normální stav, ne závada.
        state, text = storage_state(worker_status(maintenance=maintenance_status(
            aggregation_monotonic=1000.0, retention_monotonic=1000.0,
            retention_blocked="clock_unstable", stable_s=3600)), time_synced=True)
        self.assertIs(state, State.INFO)
        self.assertIn("retence blokována (clock_unstable)", text)

    def test_maintenance_summary_is_in_the_description(self):
        state, text = storage_state(worker_status(maintenance=maintenance_status(
            aggregation_monotonic=1000.0, windows_5m=12, windows_1h=1, retention_monotonic=1000.0,
            deleted={"sample": 120, "event": 3})), time_synced=True)
        self.assertIs(state, State.INFO)
        self.assertIn("agregace 12+1 oken", text)
        self.assertIn("retence smazala 123 řádků", text)

    def test_maintenance_not_run_yet_is_stated(self):
        state, text = storage_state(worker_status(), time_synced=True)
        self.assertIs(state, State.INFO)
        self.assertIn("agregace zatím neproběhla", text)
        self.assertIn("retence zatím neproběhla", text)

    def test_conflict_outranks_maintenance_warnings(self):
        state, _ = storage_state(worker_status(maintenance=maintenance_status(
            conflict="rozpor", failures=2, last_error="x", wal_bytes=history_link.WAL_WARNING_BYTES * 2)),
            time_synced=True)
        self.assertIs(state, State.ERROR)

    def test_truncated_text_is_warning_not_loss(self):
        state, text = storage_state(worker_status(losses={"result_truncated": 3}), time_synced=True)
        self.assertIs(state, State.WARNING)
        self.assertIn("zkrácený text 3", text)
        self.assertIn(str(MAX_RESULT_BYTES), text)

    def test_waiting_for_clock_is_warning(self):
        state, text = storage_state(worker_status(time_base="unsynced", held_unsynced=4, commits=0),
                                    time_synced=False, source="adjtimex")
        self.assertIs(state, State.WARNING)
        self.assertIn("nesynchronizované", text)
        self.assertIn("4 položek", text)

    def test_unknown_clock_state_is_named(self):
        state, text = storage_state(worker_status(time_base="unsynced", held_unsynced=1, commits=0),
                                    time_synced=None, source="neznámý zdroj")
        self.assertIs(state, State.WARNING)
        self.assertIn("neznámý", text)

    def test_unknown_clock_state_after_first_synchronization(self):
        state, text = storage_state(worker_status(), time_synced=None, source="neznámý zdroj")
        self.assertIs(state, State.WARNING)
        self.assertIn("nepodařilo zjistit", text)

    def test_lost_synchronization_is_warning(self):
        state, text = storage_state(worker_status(time_base="sync_lost"), time_synced=False)
        self.assertIs(state, State.WARNING)
        self.assertIn("ztracena", text)

    def test_submit_failure_is_error(self):
        state, text = storage_state(worker_status(), submit_failures=2, time_synced=True)
        self.assertIs(state, State.ERROR)
        self.assertIn("2×", text)

    def test_loss_outranks_degraded_and_clock(self):
        state, _ = storage_state(worker_status(state=DEGRADED, error_kind="io", last_error="x",
                                               time_base="unsynced", losses={"queue_full": 1}), time_synced=True)
        self.assertIs(state, State.ERROR)

    def test_history_never_claims_ok_or_critical(self):
        cases = [None, worker_status(), worker_status(state=FAILED, error_kind="corrupt", last_error="x"),
                 worker_status(state=DEGRADED, error_kind="busy", last_error="x", retry_in=5.0),
                 worker_status(state=STARTING), worker_status(losses={"queue_full": 1})]
        for status in cases:
            for synced in (True, False, None):
                state, _ = storage_state(status, time_synced=synced)
                self.assertNotIn(state, (State.OK, State.CRITICAL))

    def test_result_has_identity_and_stable_details(self):
        observed = datetime.fromtimestamp(BASE, timezone.utc)
        first = storage_result(worker_status(commits=1, pending_measurements=1), observed, 100.0)
        second = storage_result(worker_status(commits=9, pending_measurements=7), observed, 100.0)
        self.assertEqual(first.id, HISTORY_STORAGE_ID)
        self.assertTrue(first.summary.startswith("HISTORY:"))
        self.assertFalse(first.simulated)
        self.assertIsNone(first.measurement)
        # Údaje details nesou jen ustálené názvy stavů, ne počty: kontrola historie
        # sama tak nevytváří další změny k zápisu (zpětná vazba, R4).
        self.assertEqual(dict(first.details), {"state": RUNNING, "time": "synced"})
        self.assertEqual(dict(first.details), dict(second.details))
        # Ani měnící se stav údržby nepřidá do details nic nestálého.
        busy = storage_result(worker_status(maintenance=maintenance_status(
            aggregation_monotonic=1.0, windows_5m=7, retention_monotonic=2.0, deleted={"sample": 9})),
            observed, 100.0)
        self.assertEqual(dict(busy.details), dict(first.details))

    def test_check_never_feeds_itself_back(self):
        # Opakovaná kontrola se stejným stavem historie nevytvoří další změnu (R4).
        tracker = history.ChangeTracker()
        observed = datetime.fromtimestamp(BASE, timezone.utc)
        changes = []
        for commits in range(1, 6):
            result = storage_result(worker_status(commits=commits, pending_changes=commits), observed, 100.0)
            change = tracker.observe(result, BASE + commits, "synced")
            if change is not None:
                changes.append(change)
        self.assertEqual(len(changes), 1)  # jen první výchozí řádek

    def test_disabled_result_is_not_applicable(self):
        observed = datetime.fromtimestamp(BASE, timezone.utc)
        result = storage_result(None, observed, 100.0)
        self.assertIs(result.state, State.NA)
        self.assertEqual(dict(result.details), {"state": "off", "time": "off"})



class StopLinesTest(unittest.TestCase):
    """Výpis o ukončení historie netvrdí nic, co není doložené."""

    def test_clean_stop(self):
        lines = stop_lines(stop_report())
        self.assertEqual(len(lines), 1)
        self.assertIn("čisté ukončení zapsáno", lines[0])
        self.assertIn("potvrzeno 2 dávek", lines[0])

    def test_unconfirmed_commit_is_not_clean(self):
        lines = stop_lines(stop_report(in_doubt=4))
        self.assertIn("NENÍ doložené", lines[0])
        self.assertIn("4 položek s nejistým", lines[0])

    def test_unfinished_thread_is_not_clean_and_admits_it(self):
        lines = stop_lines(stop_report(completed=False, clean_stop=False,
                                       status=worker_status(state=RUNNING)), timeout=10)
        self.assertIn("NENÍ doložené", lines[0])
        joined = " ".join(lines)
        self.assertIn("po 10 s stále běží", joined)
        self.assertIn("Bezpečně ho přerušit nelze", joined)

    def test_unsaved_data_is_counted(self):
        lines = stop_lines(stop_report(clean_stop=False, unsaved_measurements=3, unsaved_changes=2,
                                       unsaved_events=1))
        self.assertIn("nezapsáno 3 měření, 2 změn a 1 událostí", lines[0])

    def test_losses_and_unrecorded_losses_are_visible(self):
        lines = stop_lines(stop_report(clean_stop=False,
                                       status=worker_status(state=STOPPED, losses={"queue_full": 7},
                                                            unrecorded_losses={"queue_full": 2},
                                                            error_kind="io", last_error="plný disk")))
        joined = " ".join(lines)
        self.assertIn("queue_full 7", joined)
        self.assertIn("neznámá mezera", joined)
        self.assertIn("plný disk", joined)


class LinkCase(unittest.TestCase):
    """Dočasná vývojová databáze, simulované hodiny a napojení na zapisovače."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.folder = Path(self._temp.name)
        self.db = self.folder / "history.sqlite3"
        history.create_database(self.db, "development", HOST, now_utc=CREATED)
        self.clock = Clock()
        self.lines = []
        self.links = []

    def tearDown(self):
        for link in self.links:
            link.stop()
        self._temp.cleanup()

    def make(self, sync=None, start=True, opener=None, **options):
        options.setdefault("interval_s", INTERVAL)
        worker = HistoryWorker(self.db, "development", HOST, app_version="test", boot_id=BOOT,
                               monotonic=self.clock.monotonic, utc_now=self.clock.utc,
                               opener=opener, **options)
        link = HistoryLink(worker, sync if sync is not None else Sync(True), output=self.lines.append,
                           path=self.db, validity_seconds=VALIDITY, timeout=WAIT)
        self.links.append(link)
        if start:
            link.start()
            self.assertTrue(worker.wait_until(lambda s: s.state != STARTING, WAIT), worker.status())
        return link

    # --- vzorové kolektory -------------------------------------------------------------

    def collectors(self, link_state="up", carrier="1"):
        proc, thermal, net = self.folder / "proc", self.folder / "thermal", self.folder / "net"
        (net / "eth0" / "statistics").mkdir(parents=True, exist_ok=True)
        (net / "eth0" / "device").mkdir(exist_ok=True)
        proc.mkdir(exist_ok=True)
        (proc / "stat").write_text(STAT_1, encoding="ascii")
        (proc / "meminfo").write_text(MEMINFO, encoding="ascii")
        for name, value in (("type", "x86_pkg_temp"), ("temp", "55400")):
            (thermal / "thermal_zone0").mkdir(parents=True, exist_ok=True)
            (thermal / "thermal_zone0" / name).write_text(f"{value}\n", encoding="ascii")
        for name, value in (("operstate", link_state), ("carrier", carrier), ("ifindex", "2")):
            (net / "eth0" / name).write_text(f"{value}\n", encoding="ascii")
        for name, value in (("rx_bytes", "1000"), ("tx_bytes", "2000")):
            (net / "eth0" / "statistics" / name).write_text(f"{value}\n", encoding="ascii")
        route = self.folder / "route"
        route.write_text(ROUTE_HEADER, encoding="ascii")
        self.proc = proc
        return Collectors(
            SystemMonitor(VALIDITY, proc_dir=proc, thermal_dir=thermal, statvfs=lambda path: FakeStatvfs()),
            NetworkMonitor(VALIDITY, interface="eth0", net_dir=net, route_path=route, ipv4_lookup=lambda name: None),
        )

    def second_sample(self):
        """Druhý vzorek /proc/stat a vyšší čítače sítě: rychlosti už jsou známé."""
        (self.proc / "stat").write_text(STAT_2, encoding="ascii")
        net = self.folder / "net" / "eth0" / "statistics"
        (net / "rx_bytes").write_text("3000\n", encoding="ascii")
        (net / "tx_bytes").write_text("5000\n", encoding="ascii")

    def real(self, results):
        """Tytéž výsledky jako skutečné měření (vzorové soubory je značí jako simulaci)."""
        return [replace(result, simulated=False) for result in results]

    def measure(self, collectors, real=True):
        now, observed = self.clock.monotonic(), self.clock.utc()
        results = collectors.collect(now, observed)
        return self.real(results) if real else results

    # --- čtení databáze ----------------------------------------------------------------

    def query(self, sql, params=()):
        with history.open_reader(self.db) as reader:
            return reader.connection.execute(sql, params).fetchall()

    def samples(self, check_id=None):
        sql = ("SELECT m.check_id, m.subject, m.unit, s.ts, s.value FROM sample s "
               "JOIN metric m ON m.id = s.metric_id")
        if check_id is not None:
            return self.query(f"{sql} WHERE m.check_id = ? ORDER BY s.ts", (check_id,))
        return self.query(f"{sql} ORDER BY s.ts, m.check_id")

    def changes(self, check_id=None):
        sql = "SELECT check_id, ts, ts_quality, from_state, to_state, note FROM check_change"
        if check_id is not None:
            return self.query(f"{sql} WHERE check_id = ? ORDER BY id", (check_id,))
        return self.query(f"{sql} ORDER BY id")

    def events(self, kind):
        return [(ts, quality, severity, json.loads(data) if data else None) for ts, quality, severity, data
                in self.query("SELECT ts, ts_quality, severity, data FROM event WHERE kind = ? ORDER BY id",
                              (kind,))]

    def flush(self, link, expected=True):
        self.assertIs(link.worker.flush(WAIT), expected, link.worker.status())


class OpenHistoryTest(LinkCase):
    """Sekce [history] rozhoduje, zda historie vůbec vznikne."""

    def test_disabled_by_default(self):
        self.assertIsNone(open_history(Config(), app_version="test"))

    def test_enabled_builds_worker_without_opening_database(self):
        config = Config(history=HistoryConfig(enabled=True, path=str(self.db)),
                        collector=CollectorConfig(interval_seconds=5))
        link = open_history(config, app_version="test", output=self.lines.append, sync=Sync(True),
                            host_id=HOST, boot_id=BOOT)
        self.assertEqual(link.path, self.db)
        self.assertEqual(link.worker._interval_s, 5)
        self.assertEqual(link.worker._boot_id, BOOT)
        self.assertEqual(link.validity_seconds, 15)
        self.assertIsNone(link.worker._database)  # databázi otevírá až vlákno

    def test_role_is_always_development(self):
        config = Config(history=HistoryConfig(enabled=True, path=str(self.db)))
        link = open_history(config, app_version="test", output=self.lines.append, sync=Sync(True),
                            host_id=HOST, boot_id=BOOT)
        link.start()
        self.links.append(link)
        self.assertTrue(link.worker.wait_until(lambda s: s.state == RUNNING, WAIT), link.worker.status())
        self.assertEqual(self.query("SELECT value FROM meta WHERE key = 'role'"), [("development",)])

    def test_missing_database_is_not_created(self):
        missing = self.folder / "chybi.sqlite3"
        config = Config(history=HistoryConfig(enabled=True, path=str(missing)))
        link = open_history(config, app_version="test", output=self.lines.append, sync=Sync(True),
                            host_id=HOST, boot_id=BOOT)
        link.start()
        self.links.append(link)
        self.assertTrue(link.worker.wait_until(lambda s: s.state == DEGRADED, WAIT), link.worker.status())
        self.assertFalse(missing.exists())
        self.assertFalse(any(path.exists() for path in history.companion_paths(missing)))
        state, _ = storage_state(link.worker.status())
        self.assertIs(state, State.WARNING)

    def test_foreign_database_is_never_replaced(self):
        foreign = self.folder / "cizi.sqlite3"
        foreign.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
        before = foreign.read_bytes()
        config = Config(history=HistoryConfig(enabled=True, path=str(foreign)))
        link = open_history(config, app_version="test", output=self.lines.append, sync=Sync(True),
                            host_id=HOST, boot_id=BOOT)
        link.start()
        self.links.append(link)
        self.assertTrue(link.worker.wait_until(lambda s: s.state == FAILED, WAIT), link.worker.status())
        self.assertEqual(foreign.read_bytes(), before)
        self.assertIs(storage_state(link.worker.status())[0], State.ERROR)

    def test_network_path_is_refused_without_writing(self):
        mounts = self.folder / "mounts"
        mounts.write_text(mountinfo("/dev/sda1 / ext4", f"nas {self.folder} cifs"), encoding="utf-8")
        with mock.patch.object(storage, "MOUNTS_PATH", str(mounts)):
            config = Config(history=HistoryConfig(enabled=True, path=str(self.db)))
            link = open_history(config, app_version="test", output=self.lines.append, sync=Sync(True),
                                host_id=HOST, boot_id=BOOT)
            link.start()
            self.links.append(link)
            self.assertTrue(link.worker.wait_until(lambda s: s.state == DEGRADED, WAIT), link.worker.status())
        self.assertIn("síťovém", link.worker.status().last_error)
        self.assertEqual(self.query("SELECT count(*) FROM run"), [(0,)])

    def test_unreadable_machine_id_does_not_stop_monitoring(self):
        config = Config(history=HistoryConfig(enabled=True, path=str(self.db)))
        with mock.patch.object(history, "local_host_id", side_effect=history.HistoryError("host", "nelze")):
            with self.assertRaises(history.HistoryError):
                open_history(config, app_version="test")


class OpenWriterTest(LinkCase):
    """Ověření úložiště před i po otevření databáze (zúžení okna záměny)."""

    def table_text(self, kind="ext4"):
        return mountinfo("/dev/sda1 / ext4", f"zdroj {os.path.realpath(self.folder)} {kind}")

    def table(self, kind="ext4"):
        mounts = self.folder / "mounts"
        mounts.write_text(self.table_text(kind), encoding="utf-8")
        return mounts

    def test_local_path_opens_and_closes_cleanly(self):
        database = _open_writer(self.db, HOST, self.table())
        database.close()
        # Zámek se uvolnil: druhé otevření projde.
        _open_writer(self.db, HOST, self.table()).close()

    def test_network_path_is_refused_before_opening(self):
        with self.assertRaises(history.HistoryError) as caught:
            _open_writer(self.db, HOST, self.table("cifs"))
        self.assertEqual(caught.exception.reason, "path")
        self.assertEqual(self.query("SELECT count(*) FROM run"), [(0,)])

    def swap_during_open(self, mounts, replacement):
        """Tabulka připojení se vymění mezi kontrolou a otevřením (okno záměny)."""
        real_open = history.open_writer

        def swap(*args, **kwargs):
            mounts.write_text(replacement, encoding="utf-8")
            return real_open(*args, **kwargs)

        return mock.patch.object(history, "open_writer", side_effect=swap)

    def test_network_storage_appearing_during_opening_closes_the_database(self):
        mounts = self.table()
        local = mounts.read_text(encoding="utf-8")
        with self.swap_during_open(mounts, self.table_text("cifs")):
            with self.assertRaises(history.HistoryError) as caught:
                _open_writer(self.db, HOST, mounts)
        self.assertIn("síťovém", str(caught.exception))
        # Spojení i zámek se uvolnily, databáze je dál použitelná.
        mounts.write_text(local, encoding="utf-8")
        _open_writer(self.db, HOST, mounts).close()

    def test_storage_identity_change_during_opening_is_refused(self):
        mounts = self.table()
        local = mounts.read_text(encoding="utf-8")
        # Jiný, ale povolený lokální typ: pozná se samotná změna identity úložiště.
        with self.swap_during_open(mounts, self.table_text("btrfs")):
            with self.assertRaises(history.HistoryError) as caught:
                _open_writer(self.db, HOST, mounts)
        self.assertIn("se během otevírání změnilo", str(caught.exception))
        mounts.write_text(local, encoding="utf-8")
        _open_writer(self.db, HOST, mounts).close()


class RecordTest(LinkCase):
    """Skutečná data kolektorů v historii: vzorky, změny a hranice."""

    def test_results_of_both_collectors_are_recorded(self):
        collectors = self.collectors()
        link = self.make()
        link.record(self.measure(collectors), self.clock.monotonic())
        self.clock.advance(INTERVAL)
        self.second_sample()
        link.record(self.measure(collectors), self.clock.monotonic())
        self.flush(link)
        stored = {(check_id, subject, unit) for check_id, subject, unit, _, _ in self.samples()}
        self.assertEqual(stored, {
            (CPU_USAGE_ID, "", "%"), (CPU_TEMP_ID, "", "°C"), (MEMORY_ID, "", "%"), (DISK_ROOT_ID, "", "%"),
            (RX_RATE_ID, "eth0", "B/s"), (TX_RATE_ID, "eth0", "B/s"),
        })
        # Síťová rychlost patří ke konkrétnímu rozhraní (objekt řady).
        self.assertEqual({row[1] for row in self.samples(RX_RATE_ID)}, {"eth0"})
        # První vzorek rychlosti je UNKNOWN (NULL), druhý už má hodnotu.
        self.assertEqual([row[4] for row in self.samples(RX_RATE_ID)][0], None)
        # Každá kontrola má výchozí řádek změny, i ta bez číselné hodnoty.
        self.assertIn(IPV4_ID, {row[0] for row in self.changes()})

    def test_network_series_keep_the_interface_identity(self):
        # Dvě rozhraní = dvě řady; provoz se nikdy nesčítá ani nemíchá.
        link = self.make()
        for name, value in (("eth0", 100.0), ("wlan0", 200.0), ("eth0", 110.0)):
            observed, expires = self.clock.utc(), self.clock.monotonic() + VALIDITY
            link.record([CheckResult(RX_RATE_ID, State.INFO, f"{name} {value}", observed, expires,
                                     simulated=False, measurement=Measurement(value, "B/s"),
                                     details={INTERFACE: name})], self.clock.monotonic())
            self.clock.advance(INTERVAL)
        self.flush(link)
        rows = [(subject, value) for _, subject, _, _, value in self.samples(RX_RATE_ID)]
        self.assertEqual(rows, [("eth0", 100.0), ("wlan0", 200.0), ("eth0", 110.0)])
        self.assertEqual(self.query("SELECT count(*) FROM metric WHERE check_id = ?", (RX_RATE_ID,)), [(2,)])

    def test_same_measurement_sent_twice_is_a_duplicate(self):
        # Ochrana před duplicitami (M3.1b) platí i přes napojení: žádný druhý vzorek,
        # žádná druhá změna stavu.
        link = self.make()
        observed, expires = self.clock.utc(), self.clock.monotonic() + VALIDITY
        results = [CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", observed, expires, simulated=False,
                               measurement=Measurement(5, "%"))]
        link.record(results, self.clock.monotonic())
        link.record(results, self.clock.monotonic())
        self.flush(link)
        self.assertEqual(len(self.samples(CPU_USAGE_ID)), 1)
        self.assertEqual([row[4] for row in self.changes(CPU_USAGE_ID)], ["INFO"])
        # Opakované stejné měření není ztráta: do ztrát se nepočítá.
        self.assertEqual({kind for kind in link.worker.status().losses} & DATA_LOSSES, set())

    def test_zero_unknown_and_missing_values(self):
        link = self.make()
        observed, expires = self.clock.utc(), self.clock.monotonic() + VALIDITY
        results = [
            CheckResult(CPU_USAGE_ID, State.INFO, "CPU 0 %", observed, expires, simulated=False,
                        measurement=Measurement(0, "%")),
            CheckResult(CPU_TEMP_ID, State.UNKNOWN, "teplota nedostupná", observed, expires, simulated=False),
            CheckResult(MEMORY_ID, State.INFO, "RAM bez hodnoty", observed, expires, simulated=False),
        ]
        link.record(results, self.clock.monotonic())
        self.flush(link)
        self.assertEqual([(row[0], row[4]) for row in self.samples()],
                         [(CPU_TEMP_ID, None), (CPU_USAGE_ID, 0.0)])  # nula je nula, UNKNOWN je NULL
        self.assertEqual(self.samples(MEMORY_ID), [])  # chybějící měření: žádný vzorek
        self.assertEqual(self.events("history.rejected")[0][3]["reasons"]["missing_value"]["count"], 1)

    def test_state_transitions_and_r4(self):
        link = self.make()
        states = [(State.INFO, 50.0), (State.INFO, 51.0), (State.WARNING, 80.0), (State.WARNING, 81.0),
                  (State.INFO, 40.0)]
        for index, (state, value) in enumerate(states):
            observed, expires = self.clock.utc(), self.clock.monotonic() + VALIDITY
            link.record([CheckResult(CPU_TEMP_ID, state, f"CPU {value}", observed, expires, simulated=False,
                                     measurement=Measurement(value, "°C"))], self.clock.monotonic())
            self.clock.advance(INTERVAL)
        self.flush(link)
        self.assertEqual([(row[3], row[4]) for row in self.changes(CPU_TEMP_ID)],
                         [(None, "INFO"), ("INFO", "WARNING"), ("WARNING", "INFO")])
        self.assertEqual(len(self.samples(CPU_TEMP_ID)), len(states))

    def test_network_link_down_and_up(self):
        link = self.make()
        for state in ("UP", "DOWN", "DOWN", "UP"):
            observed, expires = self.clock.utc(), self.clock.monotonic() + VALIDITY
            link.record([CheckResult(LINK_ID, State.INFO, f"eth0 {state}", observed, expires, simulated=False,
                                     details={INTERFACE: "eth0", LINK: state})], self.clock.monotonic())
            self.clock.advance(INTERVAL)
        self.flush(link)
        rows = self.changes(LINK_ID)
        self.assertEqual([json.loads(self.query(
            "SELECT details FROM check_change WHERE check_id = ? ORDER BY id", (LINK_ID,))[index][0])[LINK]
            for index in range(len(rows))], ["UP", "DOWN", "UP"])
        self.assertEqual([row[4] for row in rows], ["INFO", "INFO", "INFO"])  # linka DOWN je platné měření

    def test_first_rate_sample_is_unknown(self):
        collectors = self.collectors()
        link = self.make()
        results = self.measure(collectors)
        link.record(results, self.clock.monotonic())
        self.flush(link)
        by_id = {result.id: result for result in results}
        self.assertIs(by_id[CPU_USAGE_ID].state, State.UNKNOWN)  # první vzorek vytížení CPU
        self.assertIs(by_id[RX_RATE_ID].state, State.UNKNOWN)  # první vzorek rychlosti
        # UNKNOWN je řádek s NULL, ne chybějící řádek: v historii je vidět, že se měřilo.
        self.assertEqual([row[4] for row in self.samples(CPU_USAGE_ID)], [None])
        self.assertEqual([row[4] for row in self.samples(RX_RATE_ID)], [None])
        self.assertEqual([row[4] for row in self.changes(RX_RATE_ID)], ["UNKNOWN"])

    def test_simulated_data_never_reach_history(self):
        collectors = self.collectors()
        link = self.make()
        results = self.measure(collectors, real=False)
        self.assertTrue(all(result.simulated for result in results))
        link.record(results, self.clock.monotonic())
        self.flush(link)
        self.assertEqual(self.samples(), [])
        self.assertEqual(self.changes(), [])
        self.assertTrue(self.events("history.simulated_rejected"))
        self.assertIs(link.check(self.clock.monotonic(), self.clock.utc()).state, State.INFO)

    def test_several_intervals_in_one_batch(self):
        link = self.make()
        for index in range(6):
            observed, expires = self.clock.utc(), self.clock.monotonic() + VALIDITY
            link.record([CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", observed, expires, simulated=False,
                                     measurement=Measurement(5, "%"))], self.clock.monotonic())
            self.clock.advance(INTERVAL)
        self.flush(link)
        times = sorted(row[3] for row in self.samples(CPU_USAGE_ID))
        self.assertEqual(times, [BASE + index * INTERVAL for index in range(6)])
        # Beze změny stavu se měření nezapisují po jednom: první dávka nese změnu,
        # zbytek čeká na minutový interval (nebo na flush při ukončení).
        self.assertLessEqual(link.worker.status().commits, 2)

    def test_restart_keeps_high_water_and_runs_apart(self):
        first = self.make()
        collectors = self.collectors()
        first.record(self.measure(collectors), self.clock.monotonic())
        self.flush(first)
        self.assertIsNotNone(first.stop())
        self.clock.advance(INTERVAL)
        second = self.make()
        self.second_sample()
        second.record(self.measure(collectors), self.clock.monotonic())
        self.flush(second)
        self.assertEqual(self.query("SELECT id, clean_stop FROM run ORDER BY id"), [(1, 1), (2, None)])
        self.assertEqual(len(self.samples(CPU_USAGE_ID)), 2)
        self.assertEqual(self.events("history.unknown_gap"), [])  # čisté ukončení není neznámá mezera

    def test_measurement_monotonic_time_is_used_for_batching(self):
        link = self.make()
        steady = lambda: [CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", self.clock.utc(),
                                      self.clock.monotonic() + VALIDITY, simulated=False,
                                      measurement=Measurement(5, "%"))]
        link.record(steady(), self.clock.monotonic())  # první měření nese změnu: zapíše se hned
        self.assertTrue(link.worker.wait_until(lambda s: s.commits == 1, WAIT), link.worker.status())
        self.clock.advance(INTERVAL)
        link.record(steady(), self.clock.monotonic())
        link.worker.wake()
        time.sleep(0.05)
        self.assertEqual(link.worker.status().pending_measurements, 1)
        self.assertEqual(link.worker.status().commits, 1)  # 60 s od měření ještě neuplynulo
        self.clock.advance(60)  # monotónní čas měření, ne čas zápisu
        link.worker.wake()
        self.assertTrue(link.worker.wait_until(lambda s: s.commits >= 2, WAIT), link.worker.status())


class OutageTest(LinkCase):
    """Výpadek historie nezastaví měření ani alarmy."""

    def test_locked_database_degrades_and_keeps_data_queued(self):
        other = history.open_writer(self.db, "development", HOST)
        self.addCleanup(other.close)
        link = self.make(start=False)
        link.start()
        self.assertTrue(link.worker.wait_until(lambda s: s.state == DEGRADED, WAIT), link.worker.status())
        link.record([CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", self.clock.utc(),
                                 self.clock.monotonic() + VALIDITY, simulated=False,
                                 measurement=Measurement(5, "%"))], self.clock.monotonic())
        status = link.worker.status()
        self.assertEqual(status.pending_measurements, 1)
        self.assertEqual(status.error_kind, "locked")
        self.assertIs(storage_state(status)[0], State.WARNING)

    def test_submit_failure_does_not_raise_into_the_loop(self):
        link = self.make()
        with mock.patch.object(link.worker, "submit", side_effect=RuntimeError("chyba zapisovače")):
            self.assertFalse(link.record([], self.clock.monotonic()))
            self.assertFalse(link.record([], self.clock.monotonic()))
        self.assertEqual(link.submit_failures, 2)
        self.assertEqual(len([line for line in self.lines if "předání výsledků selhalo" in line]), 1)
        self.assertIs(link.check(self.clock.monotonic(), self.clock.utc()).state, State.ERROR)

    def test_frozen_write_stops_being_reported_as_fine(self):
        link = self.make()
        status = worker_status(pending_measurements=4, pending_values=16, commits=1,
                              last_commit_monotonic=self.clock.monotonic(),
                              unconfirmed_since=self.clock.monotonic())
        with mock.patch.object(link.worker, "status", return_value=status):
            fresh = link.check(self.clock.monotonic(), self.clock.utc())
            self.assertIs(fresh.state, State.INFO)  # dávka se právě potvrdila
            self.clock.advance(history_link.STALE_COMMIT_SECONDS)
            stuck = link.check(self.clock.monotonic(), self.clock.utc())
        self.assertIs(stuck.state, State.WARNING)
        self.assertIn("nepotvrdil", stuck.summary)

    def test_empty_queue_is_not_stalled(self):
        link = self.make()
        status = worker_status(pending_measurements=0, commits=1, last_commit_monotonic=0.0,
                               unconfirmed_since=None)
        with mock.patch.object(link.worker, "status", return_value=status):
            self.assertIs(link.check(self.clock.monotonic(), self.clock.utc()).state, State.INFO)

    def test_check_does_not_touch_sqlite(self):
        # Diagnostika v hlavním vlákně čte jen snímek v paměti: žádné nové
        # spojení ani dotaz do databáze (ani kvůli stavu údržby).
        link = self.make()
        link.record([CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", self.clock.utc(),
                                 self.clock.monotonic() + VALIDITY, simulated=False,
                                 measurement=Measurement(5, "%"))], self.clock.monotonic())
        with mock.patch("sqlite3.connect", side_effect=AssertionError("hlavní vlákno nesmí otevřít SQLite")):
            result = link.check(self.clock.monotonic(), self.clock.utc())
        self.assertIsNotNone(result.summary)
        self.assertIn("údržba", result.summary)

    def test_unreadable_status_is_unknown_not_ok(self):
        link = self.make()
        with mock.patch.object(link.worker, "status", side_effect=RuntimeError("nelze")):
            result = link.check(self.clock.monotonic(), self.clock.utc())
        self.assertIs(result.state, State.UNKNOWN)
        self.assertIn("nelze přečíst", result.summary)

    def test_full_queue_drops_oldest_and_reports_loss(self):
        other = history.open_writer(self.db, "development", HOST)  # zámek: zapisovač nic nezapíše
        self.addCleanup(other.close)
        # Interval 3600 s dá kapacitu jednoho měření, takže se fronta zaplní hned.
        link = self.make(interval_s=3600, event_capacity=1)
        self.assertEqual(link.worker.measurement_capacity, 1)
        for _ in range(3):
            link.record([CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", self.clock.utc(),
                                     self.clock.monotonic() + VALIDITY, simulated=False,
                                     measurement=Measurement(5, "%"))], self.clock.monotonic())
            self.clock.advance(1)
        status = link.worker.status()
        self.assertEqual(status.losses.get("queue_full"), 2)
        self.assertTrue(link._queues_full(status))
        self.assertIs(storage_state(status, queue_full=True)[0], State.ERROR)

    def test_oversized_text_is_trimmed_and_counted(self):
        link = self.make()
        observed, expires = self.clock.utc(), self.clock.monotonic() + VALIDITY
        huge = CheckResult(CPU_USAGE_ID, State.INFO, "x" * 100_000, observed, expires, simulated=False,
                           measurement=Measurement(5, "%"), details={"junk": "y" * 50_000})
        link.record([huge], self.clock.monotonic())
        self.flush(link)
        note = self.query("SELECT note, details FROM check_change WHERE check_id = ?", (CPU_USAGE_ID,))[0]
        self.assertLessEqual(len(note[0].encode()), MAX_RESULT_BYTES)
        self.assertIsNone(note[1])  # nadbytečné údaje se vypustily
        self.assertEqual(self.samples(CPU_USAGE_ID)[0][4], 5.0)  # hodnota i stav zůstaly
        event = self.events("history.result_truncated")[0]
        self.assertEqual(event[2], "WARNING")
        self.assertEqual(event[3]["count"], 1)
        self.assertEqual(event[3]["limit_bytes"], MAX_RESULT_BYTES)
        self.assertEqual(event[3]["checks"][CPU_USAGE_ID]["count"], 1)
        self.assertIs(storage_state(link.worker.status())[0], State.WARNING)

    def test_oversized_text_does_not_stop_monitoring(self):
        collectors = self.collectors()
        link = self.make()
        observed, expires = self.clock.utc(), self.clock.monotonic() + VALIDITY
        results = self.measure(collectors) + [
            CheckResult("test.huge", State.INFO, "z" * 200_000, observed, expires, simulated=False)]
        self.assertTrue(link.record(results, self.clock.monotonic()))
        self.flush(link)
        self.assertTrue(self.samples(CPU_USAGE_ID))
        self.assertEqual(link.submit_failures, 0)


class BlockingConnection:
    """Obal spojení zapisovače, který zadrží COMMIT na bariéře (zaseknuté I/O)."""

    def __init__(self, connection, entered, release, skip=1):
        self._connection = connection
        self._entered = entered
        self._release = release
        self._skip = skip  # první COMMIT patří start_run, ten se nezdržuje

    def execute(self, sql, *args):
        if sql == "COMMIT":
            if self._skip:
                self._skip -= 1
            else:
                self._entered.set()
                self._release.wait(WAIT)
        return self._connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class StalledTest(LinkCase):
    """Upozornění na nepotvrzený zápis vzniká až po prokazatelném zpoždění."""

    def results(self):
        observed, expires = self.clock.utc(), self.clock.monotonic() + VALIDITY
        return [CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", observed, expires, simulated=False,
                            measurement=Measurement(5, "%"))]

    def blocking_opener(self):
        """Opener, jehož první COMMIT uvízne; vrací (bariéra vstupu, uvolnění)."""
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def opener():
            database = history.open_writer(self.db, "development", HOST)
            database.connection = BlockingConnection(database.connection, entered, release)
            return database

        return opener, entered, release

    def test_first_measurement_is_not_stalled(self):
        # Běžné čekání na první dávku není závada: žádný commit zatím být nemusí.
        link = self.make()
        status = worker_status(commits=0, last_commit_monotonic=None, last_commit_utc=None,
                               pending_measurements=1, pending_values=1)
        with mock.patch.object(link.worker, "status", return_value=status):
            self.assertIs(link.check(self.clock.monotonic(), self.clock.utc()).state, State.INFO)

    def test_waiting_for_the_first_batch_is_not_stalled(self):
        # Ani po 59 s čekání na minutovou dávku nejde o zaseknutý zápis.
        opener, entered, release = self.blocking_opener()
        link = self.make(opener=opener)
        link.record(self.results(), self.clock.monotonic())
        self.assertTrue(entered.wait(WAIT))  # první dávka uvízla v COMMIT
        self.clock.advance(history_link.STALE_COMMIT_SECONDS - 1)
        self.assertIs(link.check(self.clock.monotonic(), self.clock.utc()).state, State.INFO)

    def test_frozen_first_commit_is_detected(self):
        # Zamrznutí během prvního COMMIT: fronta je prázdná (data jsou v dávce),
        # přesto nesmí stav vypadat v pořádku.
        opener, entered, release = self.blocking_opener()
        link = self.make(opener=opener)
        link.record(self.results(), self.clock.monotonic())
        self.assertTrue(entered.wait(WAIT))
        self.assertEqual(link.worker.status().pending_measurements, 0)  # vzorky jsou v dávce
        self.clock.advance(history_link.STALE_COMMIT_SECONDS)
        result = link.check(self.clock.monotonic(), self.clock.utc())
        self.assertIs(result.state, State.WARNING)
        self.assertIn("nepotvrdil", result.summary)

    def test_repeated_attempt_after_reopen_keeps_the_age(self):
        # Opakování zápisu po znovuotevření nesmí věk nepotvrzené práce vynulovat.
        link = self.make()
        first = worker_status(commits=0, last_commit_monotonic=None, last_commit_utc=None,
                              pending_measurements=1, unconfirmed_since=self.clock.monotonic())
        self.clock.advance(history_link.STALE_COMMIT_SECONDS)
        with mock.patch.object(link.worker, "status", return_value=first):
            stuck = link.check(self.clock.monotonic(), self.clock.utc())
        self.assertIs(stuck.state, State.WARNING)

    def test_measurement_waiting_for_the_clock_is_not_stalled(self):
        # Čekání na synchronizaci hodin má vlastní hlášení, ne „nepotvrzený zápis“.
        self.clock.set_utc(WRONG)
        link = self.make(sync=Sync(None))
        link.record(self.results(), self.clock.monotonic())
        self.clock.advance(history_link.STALE_COMMIT_SECONDS * 2)
        result = link.check(self.clock.monotonic(), self.clock.utc())
        self.assertIs(result.state, State.WARNING)
        self.assertIn("synchroniz", result.summary)
        self.assertNotIn("nepotvrdil", result.summary)


class ClockTest(LinkCase):
    """Čas měření: neznámá synchronizace, odvozený čas, ztráta a obnova."""

    def results(self):
        observed, expires = self.clock.utc(), self.clock.monotonic() + VALIDITY
        return [CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", observed, expires, simulated=False,
                            measurement=Measurement(5, "%"))]

    def test_unknown_synchronization_holds_data(self):
        self.clock.set_utc(WRONG)
        link = self.make(sync=Sync(None))
        link.record(self.results(), self.clock.monotonic())
        status = link.worker.status()
        self.assertEqual(status.time_base, "unsynced")
        self.assertEqual(status.held_unsynced, 2)  # měření i jeho změna
        self.assertIsNone(link.time_synced)
        state, text = storage_state(status, time_synced=link.time_synced, source=link.sync_source)
        self.assertIs(state, State.WARNING)
        self.assertIn("neznámý", text)
        self.assertEqual(self.samples(), [])  # vymyšlený čas se nikdy nezapíše

    def test_first_synchronization_gives_derived_time(self):
        self.clock.set_utc(WRONG)
        sync = Sync(None, None, True)  # start, první měření, pak synchronizace
        link = self.make(sync=sync)
        held = self.clock.monotonic()
        link.record(self.results(), held)
        self.clock.advance(INTERVAL)
        self.clock.set_utc(BASE + INTERVAL)  # NTP srovnalo hodiny
        link.record(self.results(), self.clock.monotonic())
        self.flush(link)
        times = [row[3] for row in self.samples(CPU_USAGE_ID)]
        self.assertEqual(times, [BASE, BASE + INTERVAL])  # čekající vzorek dostal odvozený čas
        synced = self.events("clock.synced")[0]
        self.assertEqual(synced[3]["held_measurements"], 1)
        self.assertEqual(synced[3]["boot_id"], BOOT)
        # Druhé měření má stejný stav i údaje, takže další změnu nevytvoří (R4).
        self.assertEqual([row[2] for row in self.changes(CPU_USAGE_ID)], ["derived"])

    def test_error_of_the_source_is_never_synchronized(self):
        self.clock.set_utc(WRONG)
        sync = Sync(True)
        sync.error = RuntimeError("zdroj selhal")
        link = self.make(sync=sync)
        link.record(self.results(), self.clock.monotonic())
        self.assertIsNone(link.time_synced)
        self.assertEqual(link.worker.status().time_base, "unsynced")

    def test_lost_and_restored_synchronization(self):
        link = self.make(sync=Sync(True, True, False, True))  # start, pak tři měření
        link.record(self.results(), self.clock.monotonic())
        self.clock.advance(INTERVAL)
        link.record(self.results(), self.clock.monotonic())
        status = link.worker.status()
        self.assertEqual(status.time_base, "sync_lost")
        self.assertIs(storage_state(status, time_synced=False)[0], State.WARNING)
        self.clock.advance(INTERVAL)
        link.record(self.results(), self.clock.monotonic())
        self.flush(link)
        self.assertEqual(link.worker.status().time_base, "synced")
        self.assertTrue(self.events("clock.sync_lost"))
        self.assertTrue(self.events("clock.sync_restored"))

    def test_clock_jump_is_recorded(self):
        link = self.make()
        link.record(self.results(), self.clock.monotonic())
        self.clock.advance(INTERVAL)
        self.clock.set_utc(BASE + 3600)  # skok systémového času o hodinu dopředu
        link.record(self.results(), self.clock.monotonic())
        self.flush(link)
        jumps = self.events("clock.jump")
        self.assertEqual(len(jumps), 1)
        self.assertGreater(jumps[0][3]["shift_s"], 3000)

    def test_source_is_asked_every_measurement(self):
        sync = Sync(True)
        link = self.make(sync=sync)
        before = sync.calls
        for _ in range(3):
            link.record(self.results(), self.clock.monotonic())
            self.clock.advance(INTERVAL)
        self.assertEqual(sync.calls - before, 3)


class LifecycleTest(LinkCase):
    """Spuštění, ukončení a jeho doložení."""

    def test_clean_stop_is_reported_once(self):
        link = self.make()
        link.record([CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", self.clock.utc(),
                                 self.clock.monotonic() + VALIDITY, simulated=False,
                                 measurement=Measurement(5, "%"))], self.clock.monotonic())
        report = link.stop()
        self.assertTrue(report.completed and report.clean_stop)
        self.assertEqual(self.query("SELECT clean_stop FROM run"), [(1,)])
        self.assertTrue(any("čisté ukončení zapsáno" in line for line in self.lines))
        self.assertEqual(len(self.samples(CPU_USAGE_ID)), 1)  # fronta se dopsala
        before = list(self.lines)
        self.assertIsNone(link.stop())  # druhé volání nic nedělá a nic netvrdí
        self.assertEqual(self.lines, before)

    def test_only_one_worker_stop_per_lifecycle(self):
        # Opakovaný signál ani jiné vlákno nesmí rozjet druhé ukončování workeru.
        link = self.make()
        attempts = []
        original = link.worker.stop

        def counting(timeout):
            attempts.append(timeout)
            return original(timeout)

        def reenter(line):
            self.lines.append(line)
            self.assertIsNone(link.stop())  # volání během prvního ukončování

        with mock.patch.object(link.worker, "stop", side_effect=counting):
            link._output = reenter
            self.assertIsNotNone(link.stop())
            self.assertIsNone(link.stop())
        self.assertEqual(attempts, [WAIT])
        self.assertEqual(self.query("SELECT count(*) FROM run WHERE clean_stop = 1"), [(1,)])

    def test_double_start_is_refused(self):
        link = self.make()
        with self.assertRaises(RuntimeError):
            link.start()

    def test_stop_without_start_does_nothing(self):
        link = self.make(start=False)
        self.assertIsNone(link.stop())
        self.assertEqual(self.query("SELECT count(*) FROM run"), [(0,)])

    def test_unclean_stop_is_admitted(self):
        other = history.open_writer(self.db, "development", HOST)  # zámek drží jiný zapisovač
        self.addCleanup(other.close)
        link = self.make()
        link.record([CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", self.clock.utc(),
                                 self.clock.monotonic() + VALIDITY, simulated=False,
                                 measurement=Measurement(5, "%"))], self.clock.monotonic())
        report = link.stop()
        self.assertFalse(report.clean_stop)
        self.assertEqual(report.unsaved_measurements, 1)
        joined = " ".join(self.lines)
        self.assertIn("NENÍ doložené", joined)
        self.assertNotIn("čisté ukončení zapsáno", joined)

    def test_stop_failure_claims_nothing(self):
        link = self.make()
        with mock.patch.object(link.worker, "stop", side_effect=RuntimeError("nelze")):
            self.assertIsNone(link.stop())
        self.assertTrue(any("nelze nic tvrdit" in line for line in self.lines))

    def test_data_after_stop_are_counted_not_silently_lost(self):
        link = self.make()
        link.stop()
        self.assertFalse(link.record([CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", self.clock.utc(),
                                                  self.clock.monotonic() + VALIDITY, simulated=False,
                                                  measurement=Measurement(5, "%"))], self.clock.monotonic()))
        self.assertEqual(link.worker.status().losses.get("after_stop"), 1)


class RealCollectors:
    """Obal vzorových kolektorů, jehož výsledky se berou jako skutečné měření.

    Vzorové soubory znamenají `simulated=True` (docs/HISTORY.md 4.6); smyčka
    s historií se ale musí ověřit i na datech, která zapisovač přijímá.
    """

    def __init__(self, collectors):
        self._collectors = collectors
        self.validity_seconds = collectors.validity_seconds
        self.labels = collectors.labels
        self.simulated = False

    def collect(self, now, observed_utc):
        return [replace(result, simulated=False) for result in self._collectors.collect(now, observed_utc)]


class FailingCollectors:
    """Kolektor, jehož měření vždy selže (strukturovaný údaj o selhání)."""

    def __init__(self, collectors, error):
        self.validity_seconds = collectors.validity_seconds
        self.labels = collectors.labels
        self.simulated = False
        self._error = error

    def collect(self, now, observed_utc):
        raise self._error


class LoopTest(LinkCase):
    """Smyčka měření s historií: měření pokračuje, kontrola se nepředává do historie."""

    def loop(self, link, collectors, measurements=3, alarms=AlarmsConfig()):
        self.clock.stop_at = self.clock.monotonic() + measurements * INTERVAL
        return run_loop(collectors, CollectorConfig(), AlarmEvaluator(alarms), clock=self.clock,
                        utc_clock=self.clock.utc, sleep=self.clock.sleep, output=self.lines.append,
                        history_link=link)

    def test_loop_records_and_shows_history_check(self):
        collectors = RealCollectors(self.collectors())
        link = self.make()
        results = self.loop(link, collectors)
        self.flush(link)
        self.assertEqual([result.id for result in results[-2:]],
                         [HISTORY_STORAGE_ID, HISTORY_SNAPSHOT_ID])
        self.assertTrue(any("HISTORY:" in line for line in self.lines))
        self.assertTrue(any("HIST SNAPSHOT: vypnuto" in line for line in self.lines))
        # Čas vzorku patří ke svému měřicímu cyklu: tři cykly v intervalu 10 s.
        self.assertEqual([row[3] for row in self.samples(CPU_USAGE_ID)],
                         [BASE + index * INTERVAL for index in range(3)])
        self.assertEqual({row[2] for row in self.changes()}, {"synced"})
        # Kontrola historie se do historie nepředává: žádná její změna ani vzorek.
        for check_id in (HISTORY_STORAGE_ID, HISTORY_SNAPSHOT_ID):
            self.assertNotIn(check_id, {row[0] for row in self.changes()})
            self.assertNotIn(check_id, {row[0] for row in self.samples()})

    def test_loop_without_history_is_unchanged(self):
        collectors = RealCollectors(self.collectors())
        results = self.loop(None, collectors)
        self.assertNotIn(HISTORY_STORAGE_ID, {result.id for result in results})
        self.assertFalse(any("HISTORY" in line for line in self.lines))
        self.assertEqual(self.query("SELECT count(*) FROM run"), [(0,)])

    def test_disabled_history_does_no_maintenance(self):
        # Vypnutá historie neagreguje ani nemaže: databáze zůstane, jak byla.
        before = (self.query("SELECT level, done_until FROM aggregation_state"),
                  self.query("SELECT count(*) FROM run"), self.query("SELECT count(*) FROM sample"))
        self.loop(None, RealCollectors(self.collectors()))
        self.assertEqual((self.query("SELECT level, done_until FROM aggregation_state"),
                          self.query("SELECT count(*) FROM run"), self.query("SELECT count(*) FROM sample")),
                         before)
        self.assertEqual(history.retained_since(
            history.open_reader(self.db).connection)["sample"], None)

    def test_collector_failure_is_distinguishable_in_history(self):
        # Selhání sběru má v historii strukturovaný údaj; platné UNKNOWN ne.
        link = self.make()
        collectors = RealCollectors(self.collectors())
        self.loop(link, collectors, measurements=1)
        broken = FailingCollectors(collectors, RuntimeError("kolektor spadl"))
        self.clock.stop_at = None
        self.loop(link, broken, measurements=1)
        self.flush(link)
        details = [json.loads(row[0]) if row[0] else None for row in self.query(
            "SELECT details FROM check_change WHERE check_id = ? ORDER BY id", (CPU_USAGE_ID,))]
        self.assertIsNone(details[0])  # platné měření: žádný údaj o selhání
        self.assertEqual(details[-1], {"failure": "RuntimeError"})

    def test_disabled_history_does_no_database_operation(self):
        # Vypnutá historie nesmí vůbec sáhnout na sqlite3: žádné spojení, žádný soubor.
        self.assertIsNone(open_history(Config(), app_version="test", output=self.lines.append))
        collectors = RealCollectors(self.collectors())
        with mock.patch("sqlite3.connect", side_effect=AssertionError("sqlite3 se nesmí otevřít")):
            results = self.loop(None, collectors)
        self.assertEqual(len([line for line in self.lines if "UTC" in line]), 3)
        self.assertNotIn(HISTORY_STORAGE_ID, {result.id for result in results})

    def test_measurement_continues_when_history_fails(self):
        collectors = RealCollectors(self.collectors())
        link = self.make()
        with mock.patch.object(link.worker, "submit", side_effect=RuntimeError("zapisovač spadl")):
            results = self.loop(link, collectors)
        measured = {result.id for result in results}
        for check_id in (CPU_USAGE_ID, MEMORY_ID, DISK_ROOT_ID, LINK_ID):
            self.assertIn(check_id, measured)
        self.assertIs(next(r for r in results if r.id == HISTORY_STORAGE_ID).state, State.ERROR)
        self.assertEqual(len([line for line in self.lines if "UTC" in line]), 3)  # tři měření proběhla

    def test_alarms_are_evaluated_before_history(self):
        rule = AlarmRule(enabled=True, warning=10.0, error=20.0, hysteresis=1.0, confirm_count=1, clear_count=1)
        collectors = RealCollectors(self.collectors())
        link = self.make()
        self.loop(link, collectors, measurements=3, alarms=AlarmsConfig(cpu_temp=rule))
        self.flush(link)
        # Do historie jde finální vyhodnocený stav, ne surové INFO kolektoru.
        self.assertEqual([row[4] for row in self.changes(CPU_TEMP_ID)], ["ERROR"])


class SnapshotScanRaceTest(LinkCase):
    """Souběh úvodního prohledání generací a plánovače (M3.4b-1, review)."""

    def setUp(self):
        super().setUp()
        self.work, self.published = self.folder / "snapshot-work", self.folder / "snapshot"
        self.work.mkdir()
        self.published.mkdir()
        self.plan_time = None

    def link_with_plan(self, when=None, sync=None, start=True):
        """Napojení se zapnutými snímky; plán ve výchozím stavu na aktuální čas."""
        self.plan_time = f"{self.clock.utc():%H:%M}" if when is None else when
        settings = SnapshotConfig(enabled=True, work_dir=str(self.work), published_dir=str(self.published),
                                  daily_time=self.plan_time, timezone="UTC")
        roots, plan, error = prepare_snapshots(settings)
        self.assertIsNone(error)
        worker = HistoryWorker(self.db, "development", HOST, app_version="test", boot_id=BOOT,
                               interval_s=INTERVAL, monotonic=self.clock.monotonic,
                               utc_now=self.clock.utc, snapshot_roots=roots)
        link = HistoryLink(worker, sync if sync is not None else Sync(True), output=self.lines.append,
                           path=self.db, validity_seconds=VALIDITY, timeout=WAIT,
                           snapshot_enabled=True, plan=plan)
        self.links.append(link)
        if start:
            link.start()
            self.assertTrue(worker.wait_until(lambda s: s.state != STARTING, WAIT), worker.status())
        return link

    def today_generation(self):
        """Vytvoří platnou dnešní generaci prvním zapisovačem a ten pak zastaví."""
        link = self.link_with_plan()
        worker = link.worker
        for _ in range(3):
            link.record(self.results(), self.clock.monotonic())
            self.clock.advance(INTERVAL)
        self.assertTrue(worker.flush(WAIT))
        self.assertTrue(worker.request_snapshot())
        self.assertTrue(worker.wait_until(lambda s: s.maintenance.snapshot.generation is not None, WAIT),
                        worker.status())
        name = worker.status().maintenance.snapshot.generation
        link.stop()
        return name

    def results(self):
        observed, expires = self.clock.utc(), self.clock.monotonic() + VALIDITY
        return [CheckResult(CPU_TEMP_ID, State.INFO, "CPU 50", observed, expires, simulated=False,
                            measurement=Measurement(50.0, "°C"))]

    def blocked_scan(self):
        """Zdrží prohledání generací uvnitř souborového I/O, dokud test nepustí."""
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        real = snapshot.generations

        def slow(root):
            entered.set()
            release.wait(WAIT)
            return real(root)

        return mock.patch.object(snapshot, "generations", slow), release, entered

    def test_planner_waits_for_the_scan_and_then_finds_the_day_done(self):
        # Start po dnešním plánovaném čase, v adresáři je platný dnešní snímek:
        # dokud prohledání neskončí, plánovač nic nerozhodne; po něm zjistí, že
        # je plán dne už splněný, a nic nežádá.
        name = self.today_generation()
        self.clock.advance(3600)  # start hodinu po plánovaném čase
        patch, release, entered = self.blocked_scan()
        with patch:
            link = self.link_with_plan(when=self.plan_time)
            self.assertTrue(entered.wait(WAIT))
            for _ in range(3):  # tři měřicí cykly, zatímco prohledání visí
                self.assertFalse(link.plan_snapshot(self.clock.monotonic(), self.clock.utc()))
                self.clock.advance(INTERVAL)
            status = link.worker.status().maintenance.snapshot
            self.assertFalse(status.scanned)
            self.assertEqual(link.planner.requests, 0)
            # Nedoložený stav se nehlásí jako chybějící snímek.
            result = link.snapshot_check(self.clock.monotonic(), self.clock.utc())
            self.assertIs(result.state, State.UNKNOWN)
            self.assertIn("nezjistil", result.summary)
            self.assertFalse(release.is_set())  # hlavní vlákno na prohledání nečekalo
            release.set()
        self.assertTrue(link.worker.wait_until(lambda s: s.maintenance.snapshot.scanned, WAIT))
        self.assertEqual(link.worker.status().maintenance.snapshot.generation, name)
        self.assertFalse(link.plan_snapshot(self.clock.monotonic(), self.clock.utc()))
        self.assertEqual(link.planner.requests, 0)  # dnešní plán je splněný
        self.assertEqual(snapshot.generations(self.published), [name])

    def test_scan_completion_is_required_before_the_first_snapshot(self):
        # Prázdný adresář: plán se vyhodnotí teprve po doloženém prohledání.
        patch, release, entered = self.blocked_scan()
        with patch:
            link = self.link_with_plan()
            self.assertTrue(entered.wait(WAIT))
            self.assertFalse(link.plan_snapshot(self.clock.monotonic(), self.clock.utc()))
            self.assertEqual(link.planner.requests, 0)
            release.set()
        self.assertTrue(link.worker.wait_until(lambda s: s.maintenance.snapshot.scanned, WAIT))
        self.assertTrue(link.plan_snapshot(self.clock.monotonic(), self.clock.utc()))
        self.assertEqual(link.planner.requests, 1)

    def test_manual_snapshot_before_the_planned_time_does_not_count(self):
        """Smysl schváleného pravidla: plán splní jen snímek od plánovaného času dál.

        Ruční snímek **před** plánovaným časem plán nesplní (byl by o celý den
        starší než plánovaná záloha), ruční snímek **po** něm ano.
        """
        later = self.clock.utc() + timedelta(hours=2)
        link = self.link_with_plan(when=f"{later:%H:%M}")
        worker = link.worker
        for _ in range(3):
            link.record(self.results(), self.clock.monotonic())
            self.clock.advance(INTERVAL)
        self.assertTrue(worker.flush(WAIT))
        self.assertTrue(worker.request_snapshot())  # ruční snímek dvě hodiny před plánem
        self.assertTrue(worker.wait_until(lambda s: s.maintenance.snapshot.generation is not None, WAIT))
        early = worker.status().maintenance.snapshot.generation
        self.clock.advance(2 * 3600)  # plánovaný čas nastal
        self.assertTrue(link.plan_snapshot(self.clock.monotonic(), self.clock.utc()))
        self.assertTrue(worker.wait_until(
            lambda s: s.maintenance.snapshot.generation != early, WAIT), worker.status())
        self.assertEqual(len(snapshot.generations(self.published)), 2)
        # Ruční snímek po plánovaném čase už dnešní plán splní.
        second = SnapshotPlanner(link.planner.plan)
        self.assertFalse(second.due(self.clock.utc(), worker.status().maintenance.snapshot))


class SnapshotUnsyncedTest(LinkCase):
    """Rozdíl mezi nevyžádaným, odmítnutým a selhaným snímkem (M3.4b-1, review)."""

    def setUp(self):
        super().setUp()
        self.work, self.published = self.folder / "snapshot-work", self.folder / "snapshot"
        self.work.mkdir()
        self.published.mkdir()

    def link_with_plan(self, sync, when=None):
        settings = SnapshotConfig(enabled=True, work_dir=str(self.work), published_dir=str(self.published),
                                  daily_time=f"{self.clock.utc():%H:%M}" if when is None else when,
                                  timezone="UTC")
        roots, plan, error = prepare_snapshots(settings)
        self.assertIsNone(error)
        worker = HistoryWorker(self.db, "development", HOST, app_version="test", boot_id=BOOT,
                               interval_s=INTERVAL, monotonic=self.clock.monotonic,
                               utc_now=self.clock.utc, snapshot_roots=roots)
        link = HistoryLink(worker, sync, output=self.lines.append, path=self.db,
                           validity_seconds=VALIDITY, timeout=WAIT, snapshot_enabled=True, plan=plan)
        self.links.append(link)
        link.start()
        self.assertTrue(worker.wait_until(lambda s: s.state != STARTING, WAIT), worker.status())
        return link

    def results(self):
        observed, expires = self.clock.utc(), self.clock.monotonic() + VALIDITY
        return [CheckResult(CPU_TEMP_ID, State.INFO, "CPU 50", observed, expires, simulated=False,
                            measurement=Measurement(50.0, "°C"))]

    def scanned(self, link):
        """Počká na doložené prohledání generací (souběh řeší SnapshotScanRaceTest)."""
        self.assertTrue(link.worker.wait_until(lambda s: s.maintenance.snapshot.scanned, WAIT),
                        link.worker.status())
        return link

    def ticks(self, link, count):
        """Několik měřicích cyklů: záznam, plán, kontrola."""
        for _ in range(count):
            link.record(self.results(), self.clock.monotonic())
            link.plan_snapshot(self.clock.monotonic(), self.clock.utc())
            self.clock.advance(INTERVAL)

    def test_unsynced_clock_asks_for_nothing_and_writes_no_event(self):
        # Dlouhý výpadek synchronizace: žádný požadavek a žádná událost,
        # i když měření běží dál každých 10 s.
        link = self.scanned(self.link_with_plan(Sync(False)))
        self.ticks(link, 360)  # hodina měření
        self.assertEqual(link.planner.requests, 0)
        self.assertTrue(link.worker.flush(WAIT))
        self.assertEqual(self.events("history.snapshot_skipped"), [])
        self.assertEqual(self.events("history.snapshot_failed"), [])
        self.assertEqual(snapshot.generations(self.published), [])
        # Prázdný adresář je doložený fakt, ne neznámý stav: hlásí se chybějící
        # generace (nesynchronizovaný čas to nezakrývá a nic se nevydává za OK).
        result = link.snapshot_check(self.clock.monotonic(), self.clock.utc())
        self.assertIs(result.state, State.ERROR)
        self.assertIn("žádná platná generace", result.summary)

    def test_untrusted_clock_is_audited_once_not_every_measurement(self):
        """Zdroj hlásí synchronizaci, ale čas systému je nedůvěryhodný.

        Plánovač o snímek požádá, zapisovač ho odmítne a událost o přeskočení
        vznikne **jednou za plánovaný den**, ne při každém měření. Čas události
        nese `unsynced`, protože doložený není; umístit ji jde podle
        monotónního času a `boot_id`.
        """
        self.clock.set_utc(WRONG)  # před MIN_TRUSTED_UTC
        link = self.scanned(self.link_with_plan(Sync(True)))
        self.ticks(link, 120)  # dvacet minut měření
        self.assertEqual(link.planner.requests, 1)
        self.assertTrue(link.worker.flush(WAIT))
        events = self.events("history.snapshot_skipped")
        self.assertEqual(len(events), 1)
        ts, quality, severity, data = events[0]
        self.assertEqual((quality, severity), ("unsynced", "WARNING"))
        self.assertEqual(data["reason"], "time_unsynced")
        self.assertEqual(data["boot_id"], BOOT)
        self.assertIn("mono", data)
        self.assertEqual(snapshot.generations(self.published), [])
        self.assertIn("time:", link.worker.status().maintenance.snapshot.last_error)

    def test_started_snapshot_that_fails_is_audited_differently(self):
        link = self.scanned(self.link_with_plan(Sync(True)))
        self.ticks(link, 3)
        self.assertTrue(link.worker.flush(WAIT))

        def refuse(*args, **kwargs):
            raise snapshot.SnapshotError("verify", "kopie neprošla kontrolou")

        with mock.patch.object(snapshot, "create", refuse):
            self.assertTrue(link.worker.request_snapshot())
            self.assertTrue(link.worker.wait_until(
                lambda s: s.maintenance.snapshot.failures == 1, WAIT), link.worker.status())
        self.assertTrue(link.worker.flush(WAIT))
        self.assertEqual(self.events("history.snapshot_skipped"), [])
        failed = self.events("history.snapshot_failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][1], "synced")  # čas je doložený, snímek selhal
        self.assertEqual(failed[0][3]["reason"], "verify")

    def test_recovered_clock_takes_the_next_planned_snapshot(self):
        # Po obnovení důvěryhodného času se plán splní normálně. Plán je na
        # hodinu, která nastane v obou obdobích, aby rozhodoval jen čas.
        plan_time = f"{datetime.fromtimestamp(BASE, timezone.utc):%H:%M}"
        self.clock.set_utc(WRONG)
        link = self.scanned(self.link_with_plan(Sync(True), when=plan_time))
        self.ticks(link, 6)
        self.assertEqual(snapshot.generations(self.published), [])
        self.assertEqual(link.planner.requests, 1)  # požádáno, zapisovač odmítl
        self.clock.set_utc(BASE)  # čas se srovnal
        self.ticks(link, 6)
        self.assertTrue(link.worker.wait_until(
            lambda s: s.maintenance.snapshot.generation is not None, WAIT), link.worker.status())
        self.assertEqual(len(snapshot.generations(self.published)), 1)


class SnapshotSetupTest(LinkCase):
    """Podsekce [history.snapshot] rozhoduje, zda se snímky vůbec připraví (M3.4b-1)."""

    def dirs(self):
        work, published = self.folder / "snapshot-work", self.folder / "snapshot"
        work.mkdir(exist_ok=True)
        published.mkdir(exist_ok=True)
        return work, published

    def settings(self, **options):
        roots = options.pop("roots", None)
        work, published = self.dirs() if roots is None else roots
        values = {"enabled": True, "work_dir": str(work), "published_dir": str(published),
                  "daily_time": "02:00", "timezone": "Europe/Prague"}
        values.update(options)
        return SnapshotConfig(**values)

    def config(self, snapshot=None, enabled=True):
        return Config(history=HistoryConfig(enabled=enabled, path=str(self.db),
                                            snapshot=snapshot if snapshot is not None else SnapshotConfig()))

    def open(self, config):
        link = open_history(config, app_version="test", output=self.lines.append, sync=Sync(True),
                            host_id=HOST, boot_id=BOOT)
        if link is not None:
            self.links.append(link)
        return link

    def test_history_off_means_no_snapshots(self):
        self.assertIsNone(self.open(self.config(enabled=False)))

    def test_snapshots_off_by_default(self):
        link = self.open(self.config())
        self.assertFalse(link.snapshot_enabled)
        self.assertIsNone(link.planner)
        self.assertIsNone(link.worker._snapshot_roots)
        self.assertIsNone(link.snapshot_error)
        state, text = snapshot_state(None, enabled=False)
        self.assertIs(state, State.NA)

    def test_enabled_snapshots_get_roots_and_plan(self):
        work, published = self.dirs()
        link = self.open(self.config(self.settings(roots=(work, published))))
        self.assertTrue(link.snapshot_enabled)
        self.assertIsNone(link.snapshot_error)
        self.assertEqual(link.worker._snapshot_roots, (work, published))
        self.assertEqual(link.planner.plan.hour, 2)
        self.assertEqual(str(link.planner.plan.zone), "Europe/Prague")

    def test_missing_directory_is_never_created(self):
        work, published = self.dirs()
        published.rmdir()  # správce ho nevytvořil
        link = self.open(self.config(self.settings(roots=(work, published))))
        self.assertIsNotNone(link.snapshot_error)
        self.assertFalse(published.exists())  # aplikace adresář nezaloží
        self.assertIsNone(link.worker._snapshot_roots)
        self.assertIsNone(link.planner)
        # Historie běží dál, jen snímky nevzniknou a kontrola to hlásí.
        result = link.snapshot_check(self.clock.monotonic(), self.clock.utc())
        self.assertIs(result.state, State.ERROR)
        self.assertIn("snímky nelze připravit", result.summary)

    def test_work_inside_published_is_refused(self):
        work, published = self.dirs()
        inside = published / "work"
        inside.mkdir()
        link = self.open(self.config(self.settings(roots=(inside, published))))
        self.assertIsNotNone(link.snapshot_error)
        self.assertIn("nesmí ležet", link.snapshot_error)

    def test_symlinked_directory_is_refused(self):
        work, published = self.dirs()
        link_dir = self.folder / "odkaz"
        link_dir.symlink_to(published)
        link = self.open(self.config(self.settings(roots=(work, link_dir))))
        self.assertIsNotNone(link.snapshot_error)

    def test_prepare_returns_nothing_when_disabled(self):
        self.assertEqual(prepare_snapshots(SnapshotConfig()), (None, None, None))


class SnapshotLoopTest(LinkCase):
    """Denní snímek v měřicí smyčce od začátku do konce (M3.4b-1)."""

    def setUp(self):
        super().setUp()
        self.work, self.published = self.folder / "snapshot-work", self.folder / "snapshot"
        self.work.mkdir()
        self.published.mkdir()

    def link_with_plan(self, when=None, **options):
        """Napojení se zapnutými snímky; čas plánu se bere z aktuálního času."""
        now = self.clock.utc()
        plan_time = f"{now:%H:%M}" if when is None else when
        settings = SnapshotConfig(enabled=True, work_dir=str(self.work), published_dir=str(self.published),
                                  daily_time=plan_time, timezone="UTC")
        roots, plan, error = prepare_snapshots(settings)
        self.assertIsNone(error)
        worker = HistoryWorker(self.db, "development", HOST, app_version="test", boot_id=BOOT,
                               interval_s=INTERVAL, monotonic=self.clock.monotonic,
                               utc_now=self.clock.utc, snapshot_roots=roots, **options)
        link = HistoryLink(worker, Sync(True), output=self.lines.append, path=self.db,
                           validity_seconds=VALIDITY, timeout=WAIT, snapshot_enabled=True, plan=plan)
        self.links.append(link)
        link.start()
        self.assertTrue(worker.wait_until(lambda s: s.state != STARTING, WAIT), worker.status())
        return link

    def loop(self, link, collectors, measurements=3):
        self.clock.stop_at = self.clock.monotonic() + measurements * INTERVAL
        return run_loop(collectors, CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=self.clock,
                        utc_clock=self.clock.utc, sleep=self.clock.sleep, output=self.lines.append,
                        history_link=link)

    def test_planned_snapshot_is_created_once_in_the_loop(self):
        link = self.link_with_plan()
        results = self.loop(link, RealCollectors(self.collectors()))
        worker = link.worker
        self.assertTrue(worker.wait_until(lambda s: s.maintenance.snapshot.generation is not None, WAIT),
                        worker.status())
        self.assertTrue(worker.flush(WAIT))
        snap = worker.status().maintenance.snapshot
        # Právě jedna generace a jedna auditní událost; druhý den ještě nenastal.
        self.assertEqual(snapshot.generations(self.published), [snap.generation])
        self.assertEqual(len(self.events("history.snapshot")), 1)
        self.assertEqual(link.planner.requests, 1)
        self.assertEqual([result.id for result in results[-2:]],
                         ["history.storage", "history.snapshot"])
        report = snapshot.check_restore(self.published / snap.generation, role="development", host_id=HOST)
        self.assertTrue(report.ok, report.problems)

    def test_snapshot_check_turns_info_after_the_snapshot(self):
        link = self.link_with_plan()
        self.loop(link, RealCollectors(self.collectors()))
        worker = link.worker
        self.assertTrue(worker.wait_until(lambda s: s.maintenance.snapshot.generation is not None, WAIT),
                        worker.status())
        result = link.snapshot_check(self.clock.monotonic(), self.clock.utc())
        self.assertIs(result.state, State.INFO)
        self.assertIn("poslední snímek", result.summary)

    def test_nothing_happens_before_the_planned_time(self):
        # Plán je za dvě hodiny: ve smyčce nevznikne žádná generace.
        later = self.clock.utc() + timedelta(hours=2)
        link = self.link_with_plan(when=f"{later:%H:%M}")
        self.loop(link, RealCollectors(self.collectors()))
        self.assertEqual(snapshot.generations(self.published), [])
        self.assertEqual(link.planner.requests, 0)
        result = link.snapshot_check(self.clock.monotonic(), self.clock.utc())
        self.assertIs(result.state, State.ERROR)  # zapnuté snímky bez jediné generace
        self.assertIn("žádná platná generace", result.summary)

    def test_generations_are_never_deleted(self):
        link = self.link_with_plan()
        self.loop(link, RealCollectors(self.collectors()))
        worker = link.worker
        self.assertTrue(worker.wait_until(lambda s: s.maintenance.snapshot.generation is not None, WAIT),
                        worker.status())
        first = worker.status().maintenance.snapshot.generation
        digest = snapshot.sha256_file(self.published / first / snapshot.SNAPSHOT_DB)
        # Ruční snímek o dvě hodiny později: první generace zůstává nedotčená.
        self.clock.advance(2 * 3600)
        self.assertTrue(worker.request_snapshot())
        self.assertTrue(worker.wait_until(
            lambda s: s.maintenance.snapshot.generation != first, WAIT), worker.status())
        second = worker.status().maintenance.snapshot.generation
        self.assertEqual(snapshot.generations(self.published), sorted([first, second]))
        self.assertEqual(snapshot.sha256_file(self.published / first / snapshot.SNAPSHOT_DB), digest)
        self.assertEqual(list(self.work.iterdir()), [])  # rozpracované nic nenechává

    def test_stop_during_the_loop_starts_no_new_snapshot(self):
        link = self.link_with_plan()
        link.stop()
        self.assertFalse(link.plan_snapshot(self.clock.monotonic(), self.clock.utc()))
        self.assertEqual(snapshot.generations(self.published), [])
