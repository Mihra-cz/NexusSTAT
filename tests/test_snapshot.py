"""Testy neměnných generací snímků historie (M3.4a).

Vše jen nad dočasnými adresáři: živá databáze, rozpracovaný a zveřejňovaný
adresář vznikají v `tempfile.TemporaryDirectory`. Žádná cesta Nexusu, žádný
Restic, žádná obnova nad vývojovými daty uživatele. Pády se simulují
v oddělených procesech (`os._exit`), aby zůstal skutečný stav na disku.
"""

import errno
import json
import os
import subprocess
import threading
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from nexusstat import history, snapshot, storage
from nexusstat.core import CheckResult, Measurement, State
from nexusstat.snapshot import SNAPSHOT_DB, SNAPSHOT_RECORD, SnapshotError, SnapshotRecord

HOST = "0123456789abcdef"
CREATED = 1_800_000_000
BASE = 1_800_003_600  # čas prvního vzorku (synchronizovaný)
FIRST = BASE + 1000  # čas první generace
SECOND = BASE + 2000  # čas druhé generace
WRONG = 1_700_000_000  # čas před MIN_TRUSTED_UTC
WAIT = 5  # horní mez čekání ve vláknových testech, aby se test nezasekl

# Dítě pro simulaci pádu: postaví generaci a v zadané fázi proces zabije.
CHILD = r"""
import os, sqlite3, sys
sys.path.insert(0, sys.argv[1])
from nexusstat import snapshot, storage, history

db, work, published, stage, moment = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], int(sys.argv[6])
database = history.open_writer(db, "test", "0123456789abcdef")


def die(*args, **kwargs):
    os._exit(81)


progress = None
if stage == "before_backup":
    snapshot.copy_database = die  # sqlite3.Connection je neměnný typ
elif stage == "during_copy":
    steps = []

    def progress(status, remaining, total):
        steps.append(1)
        if len(steps) >= 1 and remaining > 0:
            os._exit(81)
elif stage == "after_database":
    snapshot.verify_database = die
elif stage == "after_verify":
    snapshot.write_record = die
elif stage == "after_record":
    snapshot.check_record = die
elif stage == "before_publish":
    snapshot.publish = die
elif stage == "after_publish":
    real_rename = storage.rename_noreplace

    def rename_then_die(src, dst):
        result = real_rename(src, dst)
        if snapshot.GENERATION_NAME.fullmatch(os.path.basename(str(dst))):
            os._exit(81)  # jen zveřejnění generace, ne zkouška podpory
        return result

    storage.rename_noreplace = rename_then_die
elif stage == "during_recheck":
    pass

generation = snapshot.create(database.connection, work, published, moment, app_version="child",
                             role="test", host_id="0123456789abcdef", progress=progress,
                             pages=1)
if stage == "during_recheck":
    snapshot.sha256_file = die
    snapshot.check_restore(generation.path, role="test", host_id="0123456789abcdef")
os._exit(0)
"""


def write_samples(database, count, start=BASE, step=10, check="system.cpu_temp"):
    """Zapíše do živé databáze `count` vzorků jedné řady (bez vlákna zapisovače)."""
    recorder = history.HistoryRecorder(database)
    recorder.start_run("test", 10, datetime.fromtimestamp(start, timezone.utc))
    batch = recorder.begin_batch()
    for index in range(count):
        moment = datetime.fromtimestamp(start + index * step, timezone.utc)
        recorder.add_results(batch, [CheckResult(check, State.INFO, "x", moment, 1000.0 + index * step,
                                                 simulated=False, measurement=Measurement(50.0 + index % 5, "°C"))])
    recorder.write(batch)
    now = datetime.fromtimestamp(start + count * step + 3600, timezone.utc)
    for _ in range(24):  # dohánění: jedno volání uzavře nejvýš hodinu oken
        if not recorder.aggregate(now, "synced", history.QueueState.empty()).more:
            break
    return recorder


class SnapshotCase(unittest.TestCase):
    """Živá databáze, rozpracovaný a zveřejňovaný adresář v dočasném adresáři."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(os.path.realpath(self._temp.name))
        self.live, self.work, self.published = self.root / "history", self.root / "work", self.root / "snapshot"
        for folder in (self.live, self.work, self.published):
            folder.mkdir()
        self.db = self.live / "history.sqlite3"
        history.create_database(self.db, "test", HOST, now_utc=CREATED)
        self.database = history.open_writer(self.db, "test", HOST)
        self.addCleanup(self.close_database)

    def close_database(self):
        if self.database is not None:
            self.database.close()
            self.database = None

    def fill(self, count=60):
        return write_samples(self.database, count)

    def create(self, moment=FIRST, **options):
        options.setdefault("app_version", "test")
        options.setdefault("role", "test")
        options.setdefault("host_id", HOST)
        return snapshot.create(self.database.connection, self.work, self.published, moment, **options)

    def names(self):
        return snapshot.generations(self.published)

    def child(self, stage, moment=SECOND):
        """Spustí stavbu generace v odděleném procesu a zabije ji v dané fázi."""
        self.close_database()  # zámek zapisovače patří dítěti
        result = subprocess.run([sys.executable, "-c", CHILD, str(Path(__file__).resolve().parent.parent / "src"),
                                 str(self.db), str(self.work), str(self.published), stage, str(moment)],
                                capture_output=True, text=True, timeout=120)
        return result


class StorageCheckTest(SnapshotCase):
    """Adresáře snímků: existence, symlinky, úložiště, stejný filesystem."""

    def test_missing_directory_is_refused(self):
        with self.assertRaises(SnapshotError) as caught:
            snapshot.prepare(self.root / "neexistuje", self.published)
        self.assertEqual(caught.exception.reason, "path")

    def test_symlinked_directory_is_refused(self):
        link = self.root / "odkaz"
        link.symlink_to(self.work)
        with self.assertRaises(SnapshotError) as caught:
            snapshot.prepare(link, self.published)
        self.assertIn("symlink", str(caught.exception))

    def test_work_inside_published_is_refused(self):
        inside = self.published / "work"
        inside.mkdir()
        with self.assertRaises(SnapshotError) as caught:
            snapshot.prepare(inside, self.published)
        self.assertIn("nesmí ležet", str(caught.exception))

    def test_network_storage_is_refused(self):
        mounts = self.root / "mounts"
        mounts.write_text(f"21 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
                          f"22 21 0:40 / {self.published} rw - cifs //server/share rw\n", encoding="utf-8")
        with self.assertRaises(SnapshotError) as caught:
            snapshot.prepare(self.work, self.published, mounts=mounts)
        self.assertIn("síťovém", str(caught.exception))

    def test_unverifiable_storage_is_refused(self):
        with self.assertRaises(SnapshotError):
            snapshot.prepare(self.work, self.published, mounts=self.root / "nic")

    def test_different_filesystems_are_refused(self):
        with mock.patch.object(storage, "same_filesystem", return_value=False):
            with self.assertRaises(SnapshotError) as caught:
                snapshot.prepare(self.work, self.published)
        self.assertIn("stejném souborovém systému", str(caught.exception))

    def test_prepared_roots_are_returned(self):
        work, published = snapshot.prepare(self.work, self.published)
        self.assertEqual((work, published), (self.work, self.published))


class CreateTest(SnapshotCase):
    """Vytvoření, ověření a zveřejnění generace."""

    def test_generation_contains_database_and_record(self):
        self.fill()
        generation = self.create()
        self.assertEqual(self.names(), [generation.name])
        self.assertEqual(sorted(entry.name for entry in generation.path.iterdir()),
                         [SNAPSHOT_RECORD, SNAPSHOT_DB])
        self.assertEqual(generation.name, snapshot.generation_name(FIRST))
        record = generation.record
        self.assertEqual((record.snapshot_utc, record.schema_version, record.role, record.host_id),
                         (FIRST, history.SCHEMA_VERSION, "test", HOST))
        self.assertEqual(record.rows["sample"], 60)
        self.assertGreater(record.rows["agg_5m"], 0)
        self.assertEqual(record.sha256, snapshot.sha256_file(generation.database))
        self.assertEqual(record.bytes, generation.database.stat().st_size)

    def test_work_directory_is_empty_after_publication(self):
        self.fill()
        self.create()
        self.assertEqual(list(self.work.iterdir()), [])

    def test_live_database_is_untouched(self):
        self.fill()
        before = dict(self.database.connection.execute("SELECT key, value FROM meta"))
        digest = snapshot.sha256_file(self.db)
        self.create()
        after = dict(self.database.connection.execute("SELECT key, value FROM meta"))
        self.assertEqual(before, after)
        self.assertNotIn("snapshot_utc", after)  # meta jen v kopii
        self.assertEqual(snapshot.sha256_file(self.db), digest)

    def test_snapshot_is_standalone_without_wal(self):
        self.fill()
        generation = self.create()
        for suffix in history.COMPANION_SUFFIXES:
            self.assertFalse(os.path.lexists(f"{generation.database}{suffix}"))
        with snapshot._standalone(generation.database) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(conn.execute("SELECT count(*) FROM sample").fetchone()[0], 60)

    def test_untrusted_time_is_refused(self):
        self.fill()
        with self.assertRaises(SnapshotError) as caught:
            self.create(moment=WRONG)
        self.assertEqual(caught.exception.reason, "time")
        self.assertEqual(self.names(), [])

    def test_existing_generation_is_never_overwritten(self):
        self.fill()
        first = self.create()
        digest = snapshot.sha256_file(first.database)
        with self.assertRaises(SnapshotError) as caught:
            self.create(moment=FIRST)  # stejná sekunda = stejné jméno generace
        self.assertEqual(caught.exception.reason, "exists")
        self.assertEqual(self.names(), [first.name])
        self.assertEqual(snapshot.sha256_file(first.database), digest)

    def test_second_generation_keeps_the_first(self):
        self.fill()
        first = self.create(FIRST)
        second = self.create(SECOND)
        self.assertEqual(self.names(), [first.name, second.name])
        self.assertTrue(snapshot.load_generation(first.path).record.snapshot_utc == FIRST)

    def test_failed_verification_publishes_nothing(self):
        self.fill()
        with mock.patch.object(snapshot, "verify_database",
                               side_effect=SnapshotError("verify", "neprošlo integrity_check")):
            with self.assertRaises(SnapshotError):
                self.create()
        self.assertEqual(self.names(), [])
        self.assertEqual(list(self.work.iterdir()), [])  # rozpracovaná generace uklizena

    def test_failed_record_publishes_nothing(self):
        self.fill()
        with mock.patch.object(snapshot, "check_record", side_effect=SnapshotError("record", "nesouhlasí")):
            with self.assertRaises(SnapshotError):
                self.create()
        self.assertEqual(self.names(), [])

    def test_interrupted_copy_cleans_up(self):
        self.fill(200)

        def interrupt(status, remaining, total):
            raise SnapshotError("stopped", "přerušeno")

        with self.assertRaises(SnapshotError) as caught:
            self.create(progress=interrupt, pages=1)
        self.assertEqual(caught.exception.reason, "stopped")
        self.assertEqual(self.names(), [])
        self.assertEqual(list(self.work.iterdir()), [])


class VerifyTest(SnapshotCase):
    """Kontroly před zveřejněním."""

    def copy(self, moment=FIRST):
        """Hotová kopie v rozpracovaném adresáři (bez zveřejnění)."""
        work_dir = self.work / f".{snapshot.generation_name(moment)}.partial"
        work_dir.mkdir()
        snapshot.copy_database(self.database.connection, work_dir / SNAPSHOT_DB, moment)
        return work_dir / SNAPSHOT_DB

    def test_verification_returns_rows_size_and_digest(self):
        self.fill()
        path = self.copy()
        checked = snapshot.verify_database(path, role="test", host_id=HOST, snapshot_utc=FIRST)
        self.assertEqual(checked["integrity"], "ok")
        self.assertEqual(checked["journal_mode"], "delete")
        self.assertEqual(checked["snapshot_utc"], FIRST)
        self.assertEqual(checked["rows"]["sample"], 60)
        self.assertEqual(checked["bytes"], path.stat().st_size)
        self.assertEqual(checked["sha256"], snapshot.sha256_file(path))

    def test_wrong_snapshot_time_is_refused(self):
        self.fill()
        path = self.copy()
        with self.assertRaises(SnapshotError) as caught:
            snapshot.verify_database(path, snapshot_utc=FIRST + 1)
        self.assertIn("snapshot_utc", str(caught.exception))

    def test_foreign_role_is_refused(self):
        self.fill()
        path = self.copy()
        with self.assertRaises(SnapshotError) as caught:
            snapshot.verify_database(path, role="development", host_id=HOST)
        self.assertIn("ověřením identity", str(caught.exception))

    def test_damaged_copy_is_refused(self):
        self.fill()
        path = self.copy()
        with open(path, "r+b") as file:
            file.seek(4096)  # přepsání stránek 2-4: struktura b-tree i data
            file.write(b"\xa5" * 4096 * 3)
        with self.assertRaises(SnapshotError) as caught:
            snapshot.verify_database(path, role="test", host_id=HOST, snapshot_utc=FIRST)
        self.assertEqual(caught.exception.reason, "verify")

    def test_inconsistency_reported_by_integrity_check_is_refused(self):
        # Poškozený počet volných stránek v hlavičce: identita i schéma projdou
        # a jedinou obranou je návratová hodnota integrity_check (regrese: bez
        # její kontroly by se takový snímek zveřejnil).
        self.fill(500)
        path = self.copy()
        with open(path, "r+b") as file:
            file.seek(36)  # hlavička SQLite: počet stránek ve volném seznamu
            file.write(bytes((0, 0, 0, 7)))
        with history.open_reader(path, "test", HOST):
            pass  # identita a schéma jsou v pořádku
        with self.assertRaises(SnapshotError) as caught:
            snapshot.verify_database(path, role="test", host_id=HOST, snapshot_utc=FIRST)
        self.assertIn("integrity_check", str(caught.exception))
        self.assertIn("Freelist", str(caught.exception))

    def test_malformed_image_is_refused(self):
        # Poškozená stránka s daty: SQLite chybu vyhodí už při dotazu.
        self.fill(500)
        path = self.copy()
        size = path.stat().st_size
        with open(path, "r+b") as file:
            file.seek(size - 4096)
            file.write(b"\xa5" * 4096)
        with self.assertRaises(SnapshotError) as caught:
            snapshot.verify_database(path, role="test", host_id=HOST, snapshot_utc=FIRST)
        self.assertEqual(caught.exception.reason, "verify")

    def test_leftover_companion_is_refused(self):
        self.fill()
        path = self.copy()
        Path(f"{path}-wal").write_bytes(b"x")
        with self.assertRaises(SnapshotError) as caught:
            snapshot.verify_database(path, snapshot_utc=FIRST)
        self.assertIn("není samostatná", str(caught.exception))

    def test_record_is_compared_with_the_real_file(self):
        self.fill()
        generation = self.create()
        record = generation.record
        work_dir = self.work / ".tmp.partial"
        work_dir.mkdir()
        (work_dir / SNAPSHOT_DB).write_bytes("jiný obsah".encode())
        snapshot.write_record(work_dir, record)
        with self.assertRaises(SnapshotError) as caught:
            snapshot.check_record(work_dir, record)
        self.assertEqual(caught.exception.reason, "record")


class PublishTest(SnapshotCase):
    """Zveřejnění je jediné přejmenování a nic nepřepíše."""

    def partial(self, moment=SECOND, files=True):
        work_dir = self.work / f".{snapshot.generation_name(moment)}.partial"
        work_dir.mkdir()
        if files:
            (work_dir / SNAPSHOT_DB).write_bytes(b"db")
            (work_dir / SNAPSHOT_RECORD).write_text("{}", encoding="utf-8")
        return work_dir

    def test_publication_moves_the_whole_generation_at_once(self):
        work_dir = self.partial()
        name = snapshot.generation_name(SECOND)
        target = snapshot.publish(work_dir, self.published, name)
        self.assertEqual(sorted(entry.name for entry in target.iterdir()), [SNAPSHOT_RECORD, SNAPSHOT_DB])
        self.assertFalse(work_dir.exists())

    def test_publication_refuses_existing_generation(self):
        self.fill()
        first = self.create(SECOND)
        work_dir = self.partial(SECOND)
        with self.assertRaises(SnapshotError) as caught:
            snapshot.publish(work_dir, self.published, first.name)
        self.assertEqual(caught.exception.reason, "exists")
        self.assertEqual(snapshot.load_generation(first.path).record.snapshot_utc, SECOND)

    def test_publication_refuses_symlink_target(self):
        name = snapshot.generation_name(SECOND)
        (self.published / name).symlink_to(self.work)
        work_dir = self.partial(SECOND)
        with self.assertRaises(SnapshotError) as caught:
            snapshot.publish(work_dir, self.published, name)
        self.assertEqual(caught.exception.reason, "exists")

    def test_empty_target_directory_is_never_replaced(self):
        # os.rename() prázdný cílový adresář atomicky nahradí; publikace nesmí.
        name = snapshot.generation_name(SECOND)
        (self.published / name).mkdir()
        work_dir = self.partial(SECOND)
        with self.assertRaises(SnapshotError) as caught:
            snapshot.publish(work_dir, self.published, name)
        self.assertEqual(caught.exception.reason, "exists")
        self.assertEqual(list((self.published / name).iterdir()), [])  # cíl zůstal prázdný
        self.assertTrue(work_dir.is_dir())  # rozpracovaná generace se nepřesunula

    def test_target_that_is_a_regular_file_is_refused(self):
        name = snapshot.generation_name(SECOND)
        (self.published / name).write_text("cizí soubor", encoding="utf-8")
        work_dir = self.partial(SECOND)
        with self.assertRaises(SnapshotError) as caught:
            snapshot.publish(work_dir, self.published, name)
        self.assertEqual(caught.exception.reason, "exists")
        self.assertEqual((self.published / name).read_text(encoding="utf-8"), "cizí soubor")

    def test_target_created_between_check_and_rename_is_refused(self):
        # Souběh: cíl vznikne až po předběžné kontrole. Kontrola existence sama
        # proti tomu nechrání, atomické přejmenování bez přepsání ano.
        self.fill()
        name = snapshot.generation_name(SECOND)
        work_dir = self.partial(SECOND)
        real_fsync = storage.fsync_directory

        def create_target_then_sync(path):
            if Path(path) == work_dir:
                (self.published / name).mkdir(exist_ok=True)  # cizí proces mezitím
            return real_fsync(path)

        with mock.patch.object(storage, "fsync_directory", create_target_then_sync):
            with self.assertRaises(SnapshotError) as caught:
                snapshot.publish(work_dir, self.published, name)
        self.assertEqual(caught.exception.reason, "exists")
        self.assertEqual(list((self.published / name).iterdir()), [])
        self.assertTrue((work_dir / SNAPSHOT_DB).is_file())

    def test_two_attempts_with_the_same_identity(self):
        # Souběžné pokusy o stejnou generaci: právě jeden uspěje, druhý se odmítne.
        name = snapshot.generation_name(SECOND)
        first, second = self.partial(SECOND), self.work / f".{name}.second.partial"
        second.mkdir()
        (second / SNAPSHOT_DB).write_bytes(b"druhy")
        (second / SNAPSHOT_RECORD).write_text("{}", encoding="utf-8")
        results, start = [], threading.Barrier(2)

        def attempt(work_dir):
            start.wait(WAIT)
            try:
                snapshot.publish(work_dir, self.published, name)
                results.append(("ok", work_dir.name))
            except SnapshotError as error:
                results.append((error.reason, work_dir.name))

        threads = [threading.Thread(target=attempt, args=(path,)) for path in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT)
        self.assertEqual(sorted(reason for reason, _ in results), ["exists", "ok"])
        published = sorted(entry.name for entry in (self.published / name).iterdir())
        self.assertEqual(published, [SNAPSHOT_RECORD, SNAPSHOT_DB])  # obsah právě jedné generace

    def test_publication_never_falls_back_to_plain_rename(self):
        # Bez atomické ochrany se nic nezveřejní (žádný tichý os.rename).
        work_dir = self.partial(SECOND)
        name = snapshot.generation_name(SECOND)

        def unsupported(source, target):
            raise OSError(errno.ENOSYS, "renameat2 není k dispozici")

        with mock.patch.object(storage, "rename_noreplace", unsupported):
            with self.assertRaises(SnapshotError) as caught:
                snapshot.publish(work_dir, self.published, name)
        self.assertEqual(caught.exception.reason, "publish")
        self.assertIn("není k dispozici", str(caught.exception))
        self.assertEqual(snapshot.generations(self.published), [])

    def test_missing_support_refuses_before_building(self):
        with mock.patch.object(storage, "supports_rename_noreplace", lambda directory: False):
            with self.assertRaises(SnapshotError) as caught:
                snapshot.prepare(self.work, self.published)
        self.assertEqual(caught.exception.reason, "path")
        self.assertIn("RENAME_NOREPLACE", str(caught.exception))

    def test_failed_publication_keeps_the_previous_generation(self):
        self.fill()
        first = self.create(FIRST)
        digest = snapshot.sha256_file(first.database)
        name = snapshot.generation_name(SECOND)
        (self.published / name).mkdir()  # cíl obsazený: publikace selže
        work_dir = self.partial(SECOND)
        with self.assertRaises(SnapshotError):
            snapshot.publish(work_dir, self.published, name)
        self.assertEqual(snapshot.load_generation(first.path).record.snapshot_utc, FIRST)
        self.assertEqual(snapshot.sha256_file(first.database), digest)
        self.assertEqual(snapshot.latest_valid(self.published).name, first.name)

    def test_cleanup_removes_only_own_work_directories(self):
        own = self.partial(SECOND)
        foreign = self.work / "cizi-adresar"
        foreign.mkdir()
        (foreign / "data.txt").write_text("x", encoding="utf-8")
        extra = self.partial(FIRST)
        (extra / "cizi.txt").write_text("x", encoding="utf-8")  # cizí soubor: nemazat
        removed = snapshot.cleanup_work(self.work)
        self.assertEqual(removed, [own.name])
        self.assertTrue(foreign.is_dir() and (foreign / "data.txt").is_file())
        self.assertTrue(extra.is_dir())


class DurabilityTest(SnapshotCase):
    """Pořadí fsync a jediné přejmenování při zveřejnění."""

    def test_sync_order_is_file_then_directory_then_rename(self):
        self.fill()
        calls = []
        real_rename = storage.rename_noreplace

        def rename(src, dst):
            calls.append(("rename", Path(src).name, Path(dst).name))
            return real_rename(src, dst)

        with mock.patch.object(storage, "supports_rename_noreplace", lambda directory: True), \
                mock.patch.object(storage, "fsync_file", lambda path: calls.append(("file", Path(path).name))), \
                mock.patch.object(storage, "fsync_directory", lambda path: calls.append(("dir", Path(path).name))), \
                mock.patch.object(storage, "rename_noreplace", rename):
            generation = self.create()
        kinds = [call[0] for call in calls]
        self.assertEqual(kinds, ["file", "file", "dir", "rename", "dir", "dir"], calls)
        self.assertEqual(calls[0][1], SNAPSHOT_DB)  # nejdřív obsah databáze
        self.assertEqual(calls[1][1], SNAPSHOT_RECORD)  # pak záznam
        self.assertTrue(calls[2][1].endswith(".partial"))  # pak položky rozpracované generace
        self.assertEqual(calls[3][2], generation.name)  # teprve pak přejmenování
        self.assertEqual({calls[4][1], calls[5][1]}, {self.published.name, self.work.name})

    def test_publication_is_a_single_atomic_rename(self):
        self.fill()
        renames = []
        real_rename = storage.rename_noreplace
        plain = []
        with mock.patch.object(storage, "supports_rename_noreplace", lambda directory: True), \
                mock.patch.object(storage, "rename_noreplace",
                                  lambda src, dst: (renames.append((src, dst)), real_rename(src, dst))[1]), \
                mock.patch.object(os, "rename", lambda src, dst: plain.append((src, dst))):
            self.create()
        self.assertEqual(len(renames), 1)  # žádná dvojice přejmenovaných souborů
        self.assertEqual(plain, [])  # nikdy nechráněné os.rename()


class SharedStorageRulesTest(SnapshotCase):
    """Zapisovač i snímky používají stejnou jedinou implementaci kontrol úložiště."""

    def table(self, kind):
        mounts = self.root / "mounts"
        mounts.write_text(f"21 1 8:1 / / rw - ext4 /dev/sda1 rw\n22 21 0:40 / {self.root} rw - {kind} zdroj rw\n",
                          encoding="utf-8")
        return mounts

    def test_both_uses_refuse_the_same_storage(self):
        from nexusstat import history_link
        for kind, expected in (("cifs", "síťovém"), ("nfs4", "síťovém"), ("fuse.sshfs", "síťovém"),
                               ("autofs", "autofs"), ("tmpfs", "trvalé úložiště"), ("overlay", "trvalé úložiště")):
            with self.subTest(kind=kind):
                mounts = self.table(kind)
                with self.assertRaises(storage.StorageError) as direct:
                    storage.check_local_path(self.db, mounts)
                self.assertIn(expected, str(direct.exception))
                # zapisovač: stejná pravidla, jen jako HistoryError("path")
                with self.assertRaises(history.HistoryError) as writer:
                    history_link.check_local_path(self.db, mounts)
                self.assertEqual(writer.exception.reason, "path")
                self.assertIn(expected, str(writer.exception))
                # snímky: stejná pravidla jako SnapshotError("path")
                with self.assertRaises(SnapshotError) as snap:
                    snapshot.prepare(self.work, self.published, mounts=mounts)
                self.assertEqual(snap.exception.reason, "path")

    def test_both_uses_refuse_unverifiable_table(self):
        from nexusstat import history_link
        missing = self.root / "chybi-mounts"
        with self.assertRaises(storage.StorageError):
            storage.check_local_path(self.db, missing)
        with self.assertRaises(history.HistoryError):
            history_link.check_local_path(self.db, missing)
        with self.assertRaises(SnapshotError):
            snapshot.prepare(self.work, self.published, mounts=missing)

    def test_both_uses_accept_local_storage(self):
        from nexusstat import history_link
        mounts = self.table("ext4")
        self.assertEqual(storage.check_local_path(self.db, mounts), "ext4")
        self.assertEqual(history_link.check_local_path(self.db, mounts), "ext4")
        snapshot.prepare(self.work, self.published, mounts=mounts)

    def test_symlink_and_nested_mounts_behave_the_same(self):
        from nexusstat import history_link
        mounts = self.root / "mounts"
        mounts.write_text(f"21 1 8:1 / / rw - ext4 /dev/sda1 rw\n22 21 0:40 / {self.root}/nas rw - cifs //server/share rw\n"
                          f"23 22 8:2 / {self.root}/nas/lokalni rw - ext4 /dev/sdb1 rw\n", encoding="utf-8")
        (self.root / "nas" / "lokalni").mkdir(parents=True)
        link = self.root / "odkaz"
        link.symlink_to(self.root / "nas")
        for path in (self.root / "nas" / "db.sqlite3", link / "db.sqlite3"):
            with self.subTest(path=path.name):
                with self.assertRaises(storage.StorageError):
                    storage.check_local_path(path, mounts)
                with self.assertRaises(history.HistoryError):
                    history_link.check_local_path(path, mounts)
        # Lokální systém připojený dovnitř síťového se odmítne stejně: cesta k němu vede přes
        # síťový mount (zpřísnění opravy viditelného mountu, dřív vyhrál delší bod připojení).
        inner = self.root / "nas" / "lokalni" / "db.sqlite3"
        with self.assertRaises(storage.StorageError):
            storage.check_local_path(inner, mounts)
        with self.assertRaises(history.HistoryError):
            history_link.check_local_path(inner, mounts)


class RenameNoReplaceTest(SnapshotCase):
    """Atomické přejmenování bez přepsání (storage.rename_noreplace)."""

    def test_free_target_succeeds(self):
        source, target = self.work / "a", self.work / "b"
        source.mkdir()
        (source / "x").write_bytes(b"obsah")
        storage.rename_noreplace(source, target)
        self.assertEqual([entry.name for entry in target.iterdir()], ["x"])
        self.assertFalse(source.exists())

    def test_existing_targets_are_refused(self):
        source = self.work / "a"
        source.mkdir()
        cases = {}
        empty, full, file, link = (self.work / name for name in ("empty", "full", "file", "link"))
        empty.mkdir()
        full.mkdir()
        (full / "x").write_bytes(b"x")
        file.write_bytes(b"x")
        link.symlink_to(empty)
        for target in (empty, full, file, link):
            with self.subTest(target=target.name):
                with self.assertRaises(OSError) as caught:
                    storage.rename_noreplace(source, target)
                cases[target.name] = caught.exception.errno
                self.assertEqual(caught.exception.errno, errno.EEXIST)
        self.assertTrue(source.is_dir())  # zdroj zůstal na místě
        self.assertEqual(list(empty.iterdir()), [])

    def test_support_is_verified_not_assumed(self):
        self.assertTrue(storage.supports_rename_noreplace(self.work))
        with mock.patch.object(storage, "_renameat2", return_value=None):
            self.assertFalse(storage.supports_rename_noreplace(self.work))
        self.assertEqual(sorted(entry.name for entry in self.work.iterdir()), [])  # nic po sobě


class GenerationSelectionTest(SnapshotCase):
    """Výběr poslední prokazatelně platné generace."""

    def test_latest_valid_picks_the_newest(self):
        self.fill()
        self.create(FIRST)
        second = self.create(SECOND)
        self.assertEqual(snapshot.latest_valid(self.published).name, second.name)

    def test_newer_invalid_generation_is_skipped(self):
        self.fill()
        first = self.create(FIRST)
        second = self.create(SECOND)
        (second.path / SNAPSHOT_RECORD).write_text("{ rozbitý json", encoding="utf-8")
        self.assertEqual(snapshot.latest_valid(self.published).name, first.name)

    def test_generation_with_wrong_digest_is_invalid(self):
        self.fill()
        generation = self.create()
        with open(generation.database, "r+b") as file:
            file.seek(100)
            file.write(b"\x00" * 16)
        with self.assertRaises(SnapshotError) as caught:
            snapshot.load_generation(generation.path)
        self.assertIn("SHA-256", str(caught.exception))
        self.assertIsNone(snapshot.latest_valid(self.published))

    def test_generation_with_wrong_size_is_invalid(self):
        self.fill()
        generation = self.create()
        with open(generation.database, "ab") as file:
            file.write(b"\x00" * 4096)
        with self.assertRaises(SnapshotError) as caught:
            snapshot.load_generation(generation.path)
        self.assertIn("B, záznam", str(caught.exception))

    def test_missing_database_is_invalid(self):
        self.fill()
        generation = self.create()
        generation.database.unlink()
        with self.assertRaises(SnapshotError) as caught:
            snapshot.load_generation(generation.path)
        self.assertIn(SNAPSHOT_DB, str(caught.exception))

    def test_file_that_is_not_sqlite_is_invalid(self):
        self.fill()
        generation = self.create()
        record = SnapshotRecord.from_json((generation.path / SNAPSHOT_RECORD).read_text(encoding="utf-8"))
        generation.database.write_bytes("to není databáze".encode())
        changed = SnapshotRecord(record.generation, record.snapshot_utc, record.schema_version, record.role,
                                 record.host_id, record.database, generation.database.stat().st_size,
                                 snapshot.sha256_file(generation.database), record.rows,
                                 record.sqlite_version, record.app_version)
        (generation.path / SNAPSHOT_RECORD).write_text(changed.to_json(), encoding="utf-8")
        with self.assertRaises(SnapshotError) as caught:
            snapshot.load_generation(generation.path)
        self.assertIn("není databáze SQLite", str(caught.exception))

    def test_no_generations_at_all(self):
        self.assertEqual(snapshot.generations(self.published), [])
        self.assertIsNone(snapshot.latest_valid(self.published))


class RestoreTest(SnapshotCase):
    """Test obnovitelnosti: jen čtení, nikdy živá databáze."""

    def test_valid_generation_passes_all_checks(self):
        self.fill()
        generation = self.create()
        report = snapshot.check_restore(generation.path, role="test", host_id=HOST)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual((report.integrity, report.foreign_keys, report.journal_mode), ("ok", 0, "delete"))
        self.assertEqual(report.snapshot_utc, FIRST)
        self.assertEqual(report.rows["sample"], 60)
        self.assertGreater(report.queries["samples"], 0)
        self.assertGreater(report.queries["aggregates_5m"], 0)
        self.assertGreaterEqual(report.queries["state_episodes"], 1)
        self.assertEqual(report.problems, ())

    def test_restore_check_does_not_open_the_live_database(self):
        self.fill()
        generation = self.create()
        digest = snapshot.sha256_file(self.db)
        with mock.patch.object(history, "open_writer", side_effect=AssertionError("živá databáze se neotevírá")):
            report = snapshot.check_restore(generation.path, role="test", host_id=HOST)
        self.assertTrue(report.ok)
        self.assertEqual(snapshot.sha256_file(self.db), digest)
        for suffix in history.COMPANION_SUFFIXES:
            self.assertFalse(os.path.lexists(f"{generation.database}{suffix}"))

    def test_damaged_json_is_reported(self):
        self.fill()
        generation = self.create()
        (generation.path / SNAPSHOT_RECORD).write_text("{ rozbitý", encoding="utf-8")
        report = snapshot.check_restore(generation.path)
        self.assertFalse(report.ok)
        self.assertTrue(report.problems)

    def test_damaged_database_is_reported(self):
        self.fill()
        generation = self.create()
        with open(generation.database, "r+b") as file:
            file.seek(4096)
            file.write(b"\xa5" * 4096 * 3)
        report = snapshot.check_restore(generation.path, role="test", host_id=HOST)
        self.assertFalse(report.ok)
        self.assertTrue(any("SHA-256" in problem or "integrity" in problem for problem in report.problems))

    def test_missing_file_is_reported(self):
        self.fill()
        generation = self.create()
        (generation.path / SNAPSHOT_RECORD).unlink()
        report = snapshot.check_restore(generation.path)
        self.assertFalse(report.ok)
        self.assertIn(SNAPSHOT_RECORD, report.problems[0])

    def test_row_counts_must_match_the_record(self):
        self.fill()
        generation = self.create()
        record = SnapshotRecord.from_json((generation.path / SNAPSHOT_RECORD).read_text(encoding="utf-8"))
        rows = dict(record.rows)
        rows["sample"] += 1
        changed = SnapshotRecord(record.generation, record.snapshot_utc, record.schema_version, record.role,
                                 record.host_id, record.database, record.bytes, record.sha256, rows,
                                 record.sqlite_version, record.app_version)
        (generation.path / SNAPSHOT_RECORD).write_text(changed.to_json(), encoding="utf-8")
        report = snapshot.check_restore(generation.path, role="test", host_id=HOST)
        self.assertFalse(report.ok)
        self.assertTrue(any("počty řádků" in problem for problem in report.problems))

    def test_older_generation_is_still_restorable_after_newer_breaks(self):
        self.fill()
        first = self.create(FIRST)
        second = self.create(SECOND)
        second.database.write_bytes(b"rozbito-not-sqlite")
        self.assertFalse(snapshot.check_restore(second.path, role="test", host_id=HOST).ok)
        self.assertTrue(snapshot.check_restore(first.path, role="test", host_id=HOST).ok)
        self.assertEqual(snapshot.latest_valid(self.published).name, first.name)


class CrashTest(SnapshotCase):
    """Pády v oddělených procesech: nikdy se nezveřejní nekompletní dvojice."""

    def previous(self):
        """Předchozí platná generace, která musí každý pád přežít."""
        self.fill()
        generation = self.create(FIRST)
        return generation

    def assertPreviousUsable(self, generation):
        loaded = snapshot.load_generation(generation.path)
        self.assertEqual(loaded.record.snapshot_utc, FIRST)
        self.assertTrue(snapshot.check_restore(generation.path, role="test", host_id=HOST).ok)

    def run_stage(self, stage):
        previous = self.previous()
        result = self.child(stage)
        self.assertEqual(result.returncode, 81, f"{stage}: {result.stderr[-800:]}")
        self.assertPreviousUsable(previous)
        return previous

    def test_crash_before_backup(self):
        self.run_stage("before_backup")
        self.assertEqual(self.names(), [snapshot.generation_name(FIRST)])

    def test_crash_during_copy(self):
        self.run_stage("during_copy")
        self.assertEqual(self.names(), [snapshot.generation_name(FIRST)])
        leftovers = [entry.name for entry in self.work.iterdir()]
        self.assertTrue(all(name.endswith(".partial") for name in leftovers), leftovers)

    def test_crash_after_database(self):
        self.run_stage("after_database")
        self.assertEqual(self.names(), [snapshot.generation_name(FIRST)])

    def test_crash_after_verification(self):
        self.run_stage("after_verify")
        self.assertEqual(self.names(), [snapshot.generation_name(FIRST)])

    def test_crash_after_record(self):
        self.run_stage("after_record")
        self.assertEqual(self.names(), [snapshot.generation_name(FIRST)])

    def test_crash_before_publication(self):
        self.run_stage("before_publish")
        self.assertEqual(self.names(), [snapshot.generation_name(FIRST)])
        self.assertIsNotNone(snapshot.latest_valid(self.published))

    def test_crash_right_after_publication(self):
        self.run_stage("after_publish")
        # Generace je zveřejněná a kompletní: přejmenování adresáře je atomické.
        self.assertEqual(self.names(), [snapshot.generation_name(FIRST), snapshot.generation_name(SECOND)])
        published = snapshot.load_generation(self.published / snapshot.generation_name(SECOND))
        self.assertEqual(published.record.snapshot_utc, SECOND)
        self.assertTrue(snapshot.check_restore(published.path, role="test", host_id=HOST).ok)

    def test_crash_during_later_verification(self):
        self.run_stage("during_recheck")
        self.assertEqual(self.names(), [snapshot.generation_name(FIRST), snapshot.generation_name(SECOND)])
        self.assertTrue(snapshot.check_restore(self.published / snapshot.generation_name(SECOND),
                                               role="test", host_id=HOST).ok)

    def test_next_attempt_cleans_leftovers_and_succeeds(self):
        self.run_stage("during_copy")
        self.database = history.open_writer(self.db, "test", HOST)
        generation = self.create(SECOND + 60)
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertTrue(snapshot.check_restore(generation.path, role="test", host_id=HOST).ok)
        self.assertEqual(len(self.names()), 2)


class RecordTest(unittest.TestCase):
    """JSON záznam generace."""

    def record(self, **changes):
        fields = dict(generation="20261022T140000Z", snapshot_utc=BASE, schema_version=1, role="test",
                      host_id=HOST, database=SNAPSHOT_DB, bytes=65536, sha256="a" * 64,
                      rows={"sample": 10}, sqlite_version="3.45.1", app_version="test")
        fields.update(changes)
        return SnapshotRecord(**fields)

    def test_round_trip(self):
        record = self.record()
        self.assertEqual(SnapshotRecord.from_json(record.to_json()), record)

    def test_json_is_sorted_and_readable(self):
        data = json.loads(self.record().to_json())
        self.assertEqual(data["database"], {"name": SNAPSHOT_DB, "bytes": 65536, "sha256": "a" * 64})
        self.assertEqual(data["record_version"], snapshot.RECORD_VERSION)
        self.assertEqual(data["rows"], {"sample": 10})

    def test_broken_json_is_refused(self):
        for text in ("", "{}", "{ rozbité", json.dumps({"generation": "x"})):
            with self.subTest(text=text[:20]):
                with self.assertRaises(SnapshotError) as caught:
                    SnapshotRecord.from_json(text)
                self.assertEqual(caught.exception.reason, "record")

    def test_generation_name_from_time(self):
        self.assertEqual(snapshot.generation_name(1_800_003_600), "20270115T090000Z")
        for bad in (WRONG, True, 1.5, None):
            with self.subTest(bad=bad):
                with self.assertRaises(SnapshotError):
                    snapshot.generation_name(bad)
