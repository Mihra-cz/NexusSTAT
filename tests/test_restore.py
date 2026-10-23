"""Testy rozpoznání obnovené databáze, příkazů info/adopt a návrhu retence generací (M3.4b-2).

Vše nad dočasnými databázemi v `tempfile.TemporaryDirectory` s pevnými časy;
žádná cesta Nexusu, žádná produkční role, žádné skutečné čekání. Pády při
převzetí se simulují v oddělených procesech (`os._exit`), aby zůstal skutečný
stav na disku. Generace se nikdy nemažou.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from nexusstat import history, history_cli, history_worker, snapshot, storage
from nexusstat.core import CheckResult, Measurement, State
from nexusstat.history import HistoryError, HistoryRecorder
from nexusstat.network_monitor import INTERFACE, LINK, LINK_ID
from nexusstat.system_monitor import CELSIUS, CPU_TEMP_ID

REPO = Path(__file__).resolve().parent.parent
HOST = "0123456789abcdef"
OTHER_HOST = "fedcba9876543210"
CREATED = 1_800_000_000
BASE = 1_800_003_600  # čas prvního běhu (po vytvoření databáze)
SNAP = BASE + 600  # čas snímku
LATER = BASE + 7200  # start po obnově
WRONG = 1_700_000_000  # čas před MIN_TRUSTED_UTC
DAY = 86400

# Dítě pro simulaci pádu při převzetí: v zadané fázi proces zabije.
CHILD = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from nexusstat import history, storage

source, target, stage, moment = sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])


def die(*args, **kwargs):
    os._exit(81)


if stage == "before_copy":
    history.shutil.copyfile = die
elif stage == "during_write":
    history._adopt_write = die
elif stage == "after_publish":
    real = storage.rename_noreplace
    final = os.path.basename(target)

    def rename_then_die(src, dst):
        result = real(src, dst)
        if os.path.basename(str(dst)) == final:
            os._exit(81)  # jen zveřejnění, ne zkouška podpory RENAME_NOREPLACE
        return result

    storage.rename_noreplace = rename_then_die

history.adopt_database(source, target, host_id="fedcba9876543210", now_utc=moment)
print("bez pádu")
"""


# Dítě, které nechá horký rollback žurnál: malá cache vylije stránky do souboru
# ještě před COMMIT a proces pak spadne.
HOT_JOURNAL = r"""
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA journal_mode = delete")
conn.execute("PRAGMA cache_size = 2")
conn.execute("BEGIN IMMEDIATE")
for step in range(20000):
    conn.execute("INSERT INTO event (ts, ts_quality, kind, severity, message) "
                 "VALUES (?, 'synced', 'nepotvrzeno', 'ERROR', 'nikdy necommitnuto')", (1800000000 + step,))
os._exit(9)
"""


def at(moment):
    return datetime.fromtimestamp(moment, timezone.utc)


def sample(moment=BASE, value=50.0):
    return [CheckResult(CPU_TEMP_ID, State.INFO, f"CPU {value}", at(moment), moment + 30.0,
                        simulated=False, measurement=Measurement(value, CELSIUS))]


class RestoreCase(unittest.TestCase):
    """Dočasná databáze, snímek a jeho obnova jako živá databáze."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.db = self.root / "history.sqlite3"
        self.work, self.published = self.root / "snapshot-work", self.root / "snapshot"
        self.work.mkdir()
        self.published.mkdir()
        self.lines = []

    def tearDown(self):
        self._temp.cleanup()

    def create(self, path=None, role="test", host=HOST):
        path = self.db if path is None else path
        history.create_database(path, role, host, now_utc=CREATED)
        return path

    def filled(self, *, role="test", host=HOST, samples=(BASE,)):
        """Databáze s jedním dokončeným během a vzorky; vrací cestu."""
        self.create(role=role, host=host)
        with history.open_writer(self.db, role, host) as database:
            recorder = HistoryRecorder(database)
            recorder.start_run("test", 10, at(BASE - 60), "boot-1")
            for moment in samples:
                batch = recorder.begin_batch()
                recorder.add_results(batch, sample(moment), "synced")
                recorder.write(batch)
            recorder.finish_run(at(BASE + 30))
        return self.db

    def generation(self, moment=SNAP, *, role="test", host=HOST, finish=True):
        """Vytvoří databázi, zapíše vzorek a vrátí Generation jejího snímku."""
        if not self.db.exists():
            self.create(role=role, host=host)
        with history.open_writer(self.db, role, host) as database:
            recorder = HistoryRecorder(database)
            recorder.start_run("test", 10, at(BASE - 60), "boot-1")
            batch = recorder.begin_batch()
            recorder.add_results(batch, sample(BASE), "synced")
            recorder.write(batch)
            generation = snapshot.create(database.connection, self.work, self.published, moment,
                                         app_version="test", role=role, host_id=host)
            if finish:
                recorder.finish_run(at(moment + 10))
        return generation

    def restored(self, generation=None, name="restored.sqlite3"):
        """Kopie snímku nasazená jako živá databáze (postup správce z 9.6)."""
        generation = self.generation() if generation is None else generation
        target = self.root / name
        shutil.copyfile(generation.path / snapshot.SNAPSHOT_DB, target)
        return target

    def foreign_wal(self):
        """WAL jiné databáze téhož schématu s rozpoznatelným řádkem."""
        other = self.root / "cizi.sqlite3"
        history.create_database(other, "test", HOST, now_utc=CREATED)
        raw = sqlite3.connect(other, isolation_level=None)
        raw.execute("BEGIN IMMEDIATE")
        raw.execute("INSERT INTO event (ts, ts_quality, kind, severity, message) "
                    "VALUES (?, 'synced', 'cizi.wal', 'ERROR', 'data z jine databaze')", (BASE,))
        raw.execute("COMMIT")
        data = Path(f"{other}-wal").read_bytes()
        raw.close()
        return data

    def events(self, path, kind=None):
        where, params = ("WHERE kind = ?", (kind,)) if kind else ("", ())
        with history.open_reader(path) as reader:
            return reader.connection.execute(
                f"SELECT kind, severity, ts, run_id, data FROM event {where} ORDER BY id", params).fetchall()

    def meta(self, path):
        with history.open_reader(path) as reader:
            return dict(reader.connection.execute("SELECT key, value FROM meta"))

    def start(self, path, *, role="test", host=HOST, moment=LATER, quality="synced", app="test"):
        """Otevře databázi zapisovačem a zahájí běh; vrací (restored_utc, run_id)."""
        with history.open_writer(path, role, host) as database:
            recorder = HistoryRecorder(database)
            marker = database.restored_utc
            run_id = recorder.start_run(app, 10, at(moment), "boot-2", quality)
            recorder.finish_run(at(moment + 10))
            return marker, run_id, recorder.restored

    def assertRefused(self, reason, function, *args, **kwargs):
        with self.assertRaises(HistoryError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.reason, reason, str(caught.exception))
        return caught.exception


class RestoredDatabaseTest(RestoreCase):
    """Rozpoznání obnovené databáze podle meta.snapshot_utc (HISTORY.md 9.6)."""

    def test_normal_start_writes_no_restore_event(self):
        self.filled()
        marker, _, recorded = self.start(self.db)
        self.assertIsNone(marker)
        self.assertIsNone(recorded)
        self.assertEqual(self.events(self.db, "history.restored"), [])
        self.assertNotIn(history.RESTORED_SNAPSHOT_KEY, self.meta(self.db))

    def test_restored_snapshot_is_recognised_before_the_first_write(self):
        path = self.restored()
        with history.open_writer(path, "test", HOST) as database:
            # Rozpoznáno už při otevření, tedy před prvním zápisem do databáze.
            self.assertEqual(database.restored_utc, SNAP)
            self.assertEqual(self.events(path, "history.restored"), [])

    def test_restore_is_recorded_exactly_once(self):
        path = self.restored()
        marker, first_run, recorded = self.start(path)
        self.assertEqual((marker, recorded), (SNAP, SNAP))
        events = self.events(path, "history.restored")
        self.assertEqual(len(events), 1)
        kind, severity, ts, run_id, data = events[0]
        self.assertEqual((severity, ts, run_id), ("WARNING", LATER, first_run))
        self.assertIn(f'"snapshot_utc":{SNAP}', data)
        self.assertIn(f'"generation":"{snapshot.generation_name(SNAP)}"', data)
        # Hranice mezery nese schválený marker, ne událost o původu: táž ztráta
        # se tím nepočítá dvakrát.
        self.assertNotIn('"until"', data)
        gaps = self.events(path, "history.unknown_gap")
        self.assertEqual(len(gaps), 1)
        self.assertIn('"reason":"restored"', gaps[0][4])
        self.assertIn(f'"until":{LATER}', gaps[0][4])
        # Evidence v meta, značka odstraněná: opakovaný start událost nezdvojí.
        stored = self.meta(path)
        self.assertEqual(stored[history.RESTORED_SNAPSHOT_KEY], str(SNAP))
        self.assertEqual(stored[history.RESTORED_RECORDED_KEY], str(LATER))
        self.assertEqual(stored[history.RESTORED_GENERATION_KEY], snapshot.generation_name(SNAP))
        self.assertNotIn(history.SNAPSHOT_KEY, stored)
        second_marker, second_run, second_recorded = self.start(path, moment=LATER + DAY)
        self.assertIsNone(second_marker)
        self.assertIsNone(second_recorded)
        self.assertNotEqual(second_run, first_run)
        self.assertEqual(len(self.events(path, "history.restored")), 1)

    def test_restore_replaces_the_unclean_stop_note(self):
        # Běh otevřený ve snímku se uzavře jako nečistý, ale mezeru popisuje
        # jedna událost o obnově místo běžné dvojice.
        generation = self.generation(finish=False)
        path = self.restored(generation)
        self.start(path)
        # Místo `process.unclean_stop` je událost o obnově; hranice neznámého
        # úseku hlásí schválený `history.unknown_gap` (7.1), právě jednou.
        self.assertEqual(self.events(path, "process.unclean_stop"), [])
        gaps = self.events(path, "history.unknown_gap")
        self.assertEqual(len(gaps), 1)
        self.assertIn('"reason":"restored"', gaps[0][4])
        data = self.events(path, "history.restored")[0][4]
        self.assertIn('"runs":[1]', data)
        with history.open_reader(path) as reader:
            closed = reader.connection.execute("SELECT clean_stop, stopped_utc FROM run WHERE id = 1").fetchone()
        self.assertEqual(closed[0], 0)  # nečistě uzavřený běh ze snímku
        self.assertIsNotNone(closed[1])

    def test_unclean_stop_is_still_reported_without_a_restore(self):
        # Regrese: bez obnovy se nečisté ukončení hlásí jako dosud.
        self.create()
        database = history.open_writer(self.db, "test", HOST)
        recorder = HistoryRecorder(database)
        recorder.start_run("test", 10, at(BASE), "boot-1")
        database.close()  # bez finish_run: běh zůstal otevřený
        self.start(self.db)
        self.assertEqual(len(self.events(self.db, "process.unclean_stop")), 1)
        self.assertEqual(len(self.events(self.db, "history.unknown_gap")), 1)
        self.assertEqual(self.events(self.db, "history.restored"), [])

    def test_foreign_machine_is_refused_without_changing_the_file(self):
        path = self.restored()
        before = path.read_bytes()
        self.assertRefused("host", history.open_writer, path, "test", OTHER_HOST)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.meta(path)[history.SNAPSHOT_KEY], str(SNAP))  # značka zůstala

    def test_unreadable_marker_is_an_unclear_state(self):
        path = self.restored()
        raw = sqlite3.connect(path)
        raw.execute("UPDATE meta SET value = 'pondělí' WHERE key = ?", (history.SNAPSHOT_KEY,))
        raw.commit()
        raw.close()
        error = self.assertRefused("restored", history.open_writer, path, "test", HOST)
        self.assertIn("rozhodne správce", str(error))
        # Zapisovač to bere jako nevratnou chybu identity: zásah správce.
        self.assertEqual(history_worker.classify(error), "identity")

    def test_marker_older_than_the_database_is_refused(self):
        path = self.restored()
        raw = sqlite3.connect(path)
        raw.execute("UPDATE meta SET value = ? WHERE key = ?", (str(CREATED - 10), history.SNAPSHOT_KEY))
        raw.commit()
        raw.close()
        self.assertRefused("restored", history.open_writer, path, "test", HOST)

    def test_marker_before_min_trusted_utc_is_refused(self):
        path = self.restored()
        raw = sqlite3.connect(path)
        raw.executemany("UPDATE meta SET value = ? WHERE key = ?",
                        [(str(WRONG), history.SNAPSHOT_KEY), (str(WRONG - 10), history.CREATED_KEY)])
        raw.commit()
        raw.close()
        self.assertRefused("restored", history.open_writer, path, "test", HOST)

    def test_restore_waits_for_a_trusted_time(self):
        path = self.restored()
        before = self.events(path)  # ve snímku je process.start původního běhu
        with history.open_writer(path, "test", HOST) as database:
            recorder = HistoryRecorder(database)
            error = self.assertRefused("restored_wait", recorder.start_run, "test", 10,
                                       at(LATER), "boot-2", "unsynced")
            self.assertIn("čeká na doložený čas", str(error))
            self.assertEqual(history_worker.classify(error), "time")  # přechodné, ne zásah správce
        # Nic se nezapsalo: značka, běhy i události zůstaly jako ve snímku.
        self.assertEqual(self.meta(path)[history.SNAPSHOT_KEY], str(SNAP))
        self.assertEqual(self.events(path, "history.restored"), [])
        self.assertEqual(self.events(path), before)
        with history.open_reader(path) as reader:
            self.assertEqual(reader.connection.execute("SELECT count(*) FROM run").fetchone()[0], 1)

    def test_foreign_companions_stop_the_open(self):
        """Cizí WAL vedle obnoveného snímku nesmí jít přehrát do historie.

        Reprodukce nálezu review: `PRAGMA journal_mode = wal` při konfiguraci
        spojení přehrálo cizí WAL do obnovené databáze (stránky jiné databáze
        včetně jejích řádků). Databáze v režimu `delete` vlastní pomocné soubory
        mít nemůže, takže jejich původ nelze určit a zápis se odmítá.
        """
        path = self.restored()
        before = path.read_bytes()
        foreign = self.foreign_wal()
        Path(f"{path}-wal").write_bytes(foreign)
        error = self.assertRefused("companions", history.open_writer, path, "test", HOST)
        self.assertIn("původ nelze určit", str(error))
        self.assertIn("restored.sqlite3-wal", str(error))
        self.assertEqual(path.read_bytes(), before)  # soubor zůstal nedotčený
        self.assertEqual(history.file_journal_mode(path), "delete")
        self.assertEqual(history_worker.classify(error), "identity")  # zásah správce
        # Po odklizení pomocných souborů (9.6 krok 2) obnova proběhne normálně.
        Path(f"{path}-wal").unlink()
        marker, _, recorded = self.start(path)
        self.assertEqual((marker, recorded), (SNAP, SNAP))
        with history.open_reader(path) as reader:
            rows = reader.connection.execute(
                "SELECT count(*) FROM event WHERE kind LIKE 'cizi%'").fetchone()[0]
        self.assertEqual(rows, 0)  # žádná data z cizí databáze

    def test_shm_alone_also_stops_the_open(self):
        path = self.restored()
        Path(f"{path}-shm").write_bytes(b"")
        self.assertRefused("companions", history.open_writer, path, "test", HOST)

    def test_own_wal_of_a_wal_database_is_recovered_as_usual(self):
        # Běžný pád běžící databáze: vlastní -wal patří k databázi ve WAL
        # a zotavení musí fungovat dál (žádné odmítnutí).
        self.create()
        database = history.open_writer(self.db, "test", HOST)
        recorder = HistoryRecorder(database)
        recorder.start_run("test", 10, at(BASE), "boot-1")
        batch = recorder.begin_batch()
        recorder.add_results(batch, sample(BASE), "synced")
        recorder.write(batch)
        self.assertTrue(Path(f"{self.db}-wal").exists())
        database.connection.close()  # pád bez čistého zavření: -wal zůstal
        os.close(database._lock)
        database.connection = None
        self.assertEqual(history.file_journal_mode(self.db), "wal")
        marker, run_id, recorded = self.start(self.db)  # otevření projde
        self.assertIsNone(marker)
        self.assertIsNone(recorded)
        self.assertEqual(len(self.events(self.db, "process.unclean_stop")), 1)
        with history.open_reader(self.db) as reader:
            self.assertEqual(reader.connection.execute("SELECT count(*) FROM sample").fetchone()[0], 1)

    def test_restart_after_a_partially_opened_restored_database(self):
        # První otevření obnovené databáze ji převede do WAL a vytvoří vlastní
        # -wal; restart po pádu před evidencí musí projít a obnovu zaevidovat.
        path = self.restored()
        database = history.open_writer(path, "test", HOST)
        self.assertEqual(database.restored_utc, SNAP)
        self.assertEqual(history.file_journal_mode(path), "wal")  # vlastní režim po otevření
        database.connection.close()  # pád před zápisem evidence
        os.close(database._lock)
        database.connection = None
        marker, _, recorded = self.start(path)
        self.assertEqual((marker, recorded), (SNAP, SNAP))
        self.assertEqual(len(self.events(path, "history.restored")), 1)

    def test_new_wal_after_the_restore_is_normal(self):
        path = self.restored()
        self.start(path)  # obnova zaevidovaná, databáze je ve WAL
        self.assertEqual(history.file_journal_mode(path), "wal")
        database = history.open_writer(path, "test", HOST)  # druhý start projde
        try:
            self.assertIsNone(database.restored_utc)
        finally:
            database.close()

    def test_restored_run_identity_and_batch_guard_start_over(self):
        path = self.restored()
        with history.open_writer(path, "test", HOST) as database:
            recorder = HistoryRecorder(database)
            run_id = recorder.start_run("test", 10, at(LATER), "boot-2")
            self.assertEqual(recorder.last_batch, 0)  # evidence dávek nového běhu začíná od nuly
            self.assertGreater(run_id, 1)
            # Horní mez ze snímku chrání před zápisem do již uložené historie.
            self.assertEqual(recorder.high_water, BASE)
            batch = recorder.begin_batch()
            recorder.add_results(batch, sample(BASE), "synced")  # starý čas: odmítnuto
            recorder.write(batch)
            self.assertTrue(recorder.rejected)
            batch = recorder.begin_batch()
            recorder.add_results(batch, sample(LATER), "synced")  # nový čas: zapsáno
            outcome = recorder.write(batch)
            self.assertEqual(outcome.samples, 1)
            self.assertEqual(recorder.last_batch, 2)
            recorder.finish_run(at(LATER + 10))

    def test_restored_database_keeps_the_snapshot_data(self):
        path = self.restored()
        self.start(path)
        with history.open_reader(path) as reader:
            rows = reader.connection.execute("SELECT count(*) FROM sample").fetchone()[0]
        self.assertEqual(rows, 1)  # vzorek ze snímku zůstal


class InfoTest(RestoreCase):
    """Příkaz info: jen čte, nic nemění (HISTORY.md 9.6)."""

    def info(self, path=None, **options):
        return history.database_info(self.db if path is None else path, host_id=HOST, **options)

    def digest(self, path):
        return Path(path).read_bytes()

    def hot_journal(self, path):
        """Nechá vedle databáze horký `-journal` (pád zapisovače v režimu delete)."""
        script = self.root / "spill.py"
        script.write_text(HOT_JOURNAL, encoding="utf-8")
        done = subprocess.run([sys.executable, str(script), str(path)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 9, done.stderr)
        self.assertTrue(Path(f"{path}-journal").exists(), "horký žurnál nevznikl")

    def test_live_database_is_described(self):
        self.filled()
        info = self.info()
        self.assertTrue(info.exists)
        self.assertEqual((info.role, info.schema_version, info.schema_matches), ("test", 1, True))
        self.assertEqual(info.created_utc, CREATED)
        self.assertIsNone(info.snapshot_utc)
        self.assertEqual(info.journal_mode, "wal")
        self.assertEqual(info.integrity, "ok")
        self.assertEqual(info.foreign_keys, 0)
        self.assertEqual(info.counts["sample"], 1)
        self.assertEqual(info.counts["run"], 1)
        self.assertEqual(info.last_run["clean_stop"], 1)
        self.assertEqual(info.last_run["app_version"], "test")
        self.assertTrue(info.same_machine)
        self.assertTrue(info.openable)
        self.assertFalse(info.needs_adopt)
        self.assertEqual(info.problems, ())

    def test_host_id_is_shown_only_as_a_fingerprint(self):
        self.filled()
        info = self.info()
        self.assertEqual(info.fingerprint, history.host_fingerprint(HOST))
        self.assertNotEqual(info.fingerprint, HOST)
        self.assertEqual(len(info.fingerprint), history.FINGERPRINT_CHARS)
        text = "\n".join(history_cli.info_lines(info))
        self.assertNotIn(HOST, text)  # uložené host_id se nikdy netiskne
        self.assertIn(info.fingerprint, text)

    def test_info_never_changes_the_database_file(self):
        """Pravdivý kontrakt: soubor databáze se nemění, checkpoint neproběhne.

        U databáze ve **WAL** smí SQLite vedle souboru vytvořit prázdné `-wal`
        a `-shm` (index WAL je nutný i pro čtení); data v nich nejsou.
        """
        self.filled()
        before = self.digest(self.db)
        info = self.info()
        self.assertEqual(self.digest(self.db), before)  # ani bajt
        self.assertEqual(info.journal_mode, "wal")
        wal = Path(f"{self.db}-wal")
        if wal.exists():
            self.assertEqual(wal.stat().st_size, 0)  # žádné rámce: nic se nezapsalo
        for companion in history.companion_paths(self.db):
            if companion.exists():
                # -shm je index WAL ve sdílené paměti (má pevnou hlavičku), ne data.
                self.assertTrue(companion.name.endswith(("-wal", "-shm")), companion.name)
        self.assertEqual(self.info().counts["sample"], 1)  # obsah beze změny

    def test_info_creates_nothing_for_a_standalone_database(self):
        # Obnovený snímek je v režimu delete: při čtení nesmí vzniknout žádný
        # pomocný soubor, jinak by příští otevření zapisovačem (správně)
        # odmítlo databázi s pomocnými soubory neurčitého původu.
        path = self.restored()
        before = self.digest(path)
        listing = sorted(entry.name for entry in self.root.iterdir())
        info = history.database_info(path, host_id=HOST)
        self.assertEqual(info.journal_mode, "delete")
        self.assertEqual(self.digest(path), before)
        self.assertEqual(sorted(entry.name for entry in self.root.iterdir()), listing)
        self.assertEqual([entry.name for entry in history.companion_paths(path) if entry.exists()], [])
        # A obnova po takovém čtení normálně proběhne.
        marker, _, recorded = self.start(path)
        self.assertEqual((marker, recorded), (SNAP, SNAP))

    def test_committed_data_in_wal_is_counted(self):
        # Se zapisovačem drženým otevřeným zůstává -wal: info musí vidět i jeho data.
        self.create()
        database = history.open_writer(self.db, "test", HOST)
        try:
            recorder = HistoryRecorder(database)
            recorder.start_run("test", 10, at(BASE), "boot-1")
            batch = recorder.begin_batch()
            recorder.add_results(batch, sample(BASE), "synced")
            recorder.write(batch)
            self.assertTrue(Path(f"{self.db}-wal").exists())
            info = self.info()
        finally:
            database.close()
        self.assertEqual(info.counts["sample"], 1)
        self.assertIn(f"{self.db.name}-wal", info.companions)

    def test_missing_foreign_and_corrupt_files_are_reported(self):
        missing = self.info(self.root / "nic.sqlite3")
        self.assertFalse(missing.exists)
        self.assertIn("neexistuje", missing.problems[0])
        self.assertFalse(missing.openable)
        foreign = self.root / "cizi.sqlite3"
        foreign.write_bytes(b"to nejsou data SQLite" + b"\x00" * 200)
        report = self.info(foreign)
        self.assertFalse(report.openable)
        self.assertIn("není databáze SQLite", report.problems[0])
        other = self.root / "jina.sqlite3"
        raw = sqlite3.connect(other)  # platná SQLite, ale ne NexusSTAT
        raw.execute("CREATE TABLE cizi (x INTEGER)")
        raw.commit()
        raw.close()
        self.assertIn("ne NexusSTAT", self.info(other).problems[0])

    def test_corrupt_database_is_reported_without_crashing(self):
        self.filled()
        broken = self.root / "poskozena.sqlite3"
        data = bytearray(self.db.read_bytes())
        data[4096:4196] = b"\xff" * 100  # rozbitá stránka za hlavičkou
        broken.write_bytes(bytes(data))
        info = self.info(broken)
        self.assertFalse(info.openable)
        self.assertTrue(info.problems)

    def test_newer_schema_is_refused_with_a_clear_reason(self):
        self.filled()
        newer = self.root / "novejsi.sqlite3"
        shutil.copyfile(self.db, newer)
        raw = sqlite3.connect(newer)
        raw.execute("PRAGMA user_version = 2")
        raw.close()
        info = self.info(newer)
        self.assertFalse(info.openable)
        self.assertIn("novější schéma", info.problems[0])

    def test_restored_marker_and_foreign_host_are_visible(self):
        path = self.restored()
        info = history.database_info(path, host_id=OTHER_HOST)
        self.assertEqual(info.snapshot_utc, SNAP)
        self.assertFalse(info.same_machine)
        self.assertFalse(info.openable)
        self.assertTrue(info.needs_adopt)
        self.assertTrue(any("ruční převzetí" in problem for problem in info.problems))
        self.assertTrue(any(str(SNAP) in problem for problem in info.problems))

    def test_skipped_integrity_is_never_presented_as_verified(self):
        self.filled()
        info = self.info(integrity=False)
        self.assertIsNone(info.integrity)
        self.assertIsNone(info.foreign_keys)
        self.assertFalse(info.integrity_checked)
        self.assertTrue(any("integrita nebyla ověřena" in problem for problem in info.problems))
        text = "\n".join(history_cli.info_lines(info))
        self.assertIn("integrity_check: neprověřeno", text)
        self.assertIn("integrita neprověřena", text)  # i v závěru o otevření
        checked = self.info()
        self.assertTrue(checked.integrity_checked)
        self.assertEqual(checked.integrity, "ok")

    def test_hot_rollback_journal_is_not_reported_as_intact(self):
        """Nález review: `immutable=1` čte databázi s čekajícím rollbackem a hlásí ok.

        Absence `-wal` nedokazuje, že databáze není živá, takže `info` čte
        běžným spojením jen pro čtení: to čekající rollback přizná.
        """
        self.filled()
        standalone = self.root / "delete.sqlite3"
        shutil.copyfile(self.db, standalone)
        raw = sqlite3.connect(standalone, isolation_level=None)
        raw.execute("PRAGMA journal_mode = delete")
        raw.close()
        self.hot_journal(standalone)
        info = history.database_info(standalone, host_id=HOST)
        self.assertFalse(info.openable)
        self.assertTrue(info.problems)
        self.assertTrue(any("rollback žurnál" in problem for problem in info.problems), info.problems)
        self.assertNotEqual(info.integrity, "ok")  # nic se nevydává za v pořádku
        # Výslovné tvrzení správce o neměnné kopii je jiná věc a smí se použít.
        asserted = history.database_info(standalone, host_id=HOST, immutable=True)
        self.assertEqual(asserted.integrity, "ok")

    def test_immutable_is_refused_next_to_a_wal(self):
        self.create()
        database = history.open_writer(self.db, "test", HOST)
        try:
            recorder = HistoryRecorder(database)
            recorder.start_run("test", 10, at(BASE), "boot-1")
            info = history.database_info(self.db, host_id=HOST, immutable=True)
        finally:
            database.close()
        self.assertFalse(info.openable)
        self.assertTrue(any("immutable=1 nelze použít" in problem for problem in info.problems))


class AdoptTest(RestoreCase):
    """Ruční převzetí: jen offline, jen nový soubor, vstup beze změny (9.6 krok 4)."""

    def adopt(self, source=None, target=None, **options):
        options.setdefault("host_id", OTHER_HOST)
        options.setdefault("now_utc", LATER)
        return history.adopt_database(self.restored() if source is None else source,
                                      self.root / "prevzata.sqlite3" if target is None else target,
                                      **options)

    def test_adopt_creates_a_new_database_and_keeps_the_source(self):
        source = self.restored()
        before = source.read_bytes()
        target = self.root / "prevzata.sqlite3"
        report = self.adopt(source, target)
        self.assertEqual(source.read_bytes(), before)  # vstup beze změny
        self.assertTrue(report.verified)
        self.assertEqual(report.role, "test")
        self.assertEqual(report.from_fingerprint, history.host_fingerprint(HOST))
        self.assertEqual(report.to_fingerprint, history.host_fingerprint(OTHER_HOST))
        self.assertEqual(report.snapshot_utc, SNAP)
        self.assertEqual(report.events, 1)
        stored = self.meta(target)
        self.assertEqual(stored[history.HOST_KEY], OTHER_HOST)
        self.assertEqual(stored[history.ADOPTED_HOST_KEY], HOST)  # původní identita zůstala
        self.assertEqual(stored[history.ADOPTED_UTC_KEY], str(LATER))
        self.assertEqual(stored[history.ADOPTED_SOURCE_KEY], source.name)
        self.assertEqual(stored[history.SNAPSHOT_KEY], str(SNAP))  # původ obnovy zůstal
        kind, severity, ts, run_id, data = self.events(target, "history.adopted")[0]
        self.assertEqual((severity, ts, run_id), ("WARNING", LATER, None))  # mimo běh procesu
        self.assertIn(f'"from_host_id":"{HOST}"', data)

    def test_adopted_database_leaves_no_companion_files(self):
        target = self.root / "prevzata.sqlite3"
        self.adopt(target=target)
        self.assertEqual([path.name for path in history.companion_paths(target) if path.exists()], [])
        self.assertEqual(history.file_journal_mode(target), "delete")  # samostatný soubor

    def test_adopted_database_records_the_restore_on_first_start(self):
        target = self.root / "prevzata.sqlite3"
        self.adopt(target=target)
        marker, run_id, recorded = self.start(target, host=OTHER_HOST, moment=LATER + 60)
        self.assertEqual((marker, recorded), (SNAP, SNAP))
        self.assertEqual(len(self.events(target, "history.restored")), 1)
        self.assertEqual(len(self.events(target, "history.adopted")), 1)  # převzetí se nezdvojí

    def test_existing_target_is_never_overwritten(self):
        source, target = self.restored(), self.root / "prevzata.sqlite3"
        self.adopt(source, target)
        before = target.read_bytes()
        self.assertRefused("exists", self.adopt, source, target)
        self.assertEqual(target.read_bytes(), before)

    def test_production_role_is_refused(self):
        # Produkční databázi tento kód nevytvoří, proto se role podvrhne přímo
        # v meta: převzetí ji musí odmítnout, ne ji "opravit".
        source = self.restored()
        raw = sqlite3.connect(source)
        raw.execute("UPDATE meta SET value = 'production' WHERE key = ?", (history.ROLE_KEY,))
        raw.commit()
        raw.close()
        before = source.read_bytes()
        error = self.assertRefused("role", self.adopt, source)
        self.assertIn("production", str(error))
        self.assertEqual(source.read_bytes(), before)

    def test_live_writer_blocks_the_adoption(self):
        source = self.restored()
        database = history.open_writer(source, "test", HOST)
        try:
            self.assertRefused("locked", self.adopt, source)
        finally:
            database.close()

    def test_companion_files_block_the_adoption(self):
        source = self.restored()
        Path(f"{source}-wal").write_bytes(b"")
        error = self.assertRefused("exists", self.adopt, source)
        self.assertIn("samostatný soubor", str(error))

    def test_untrusted_clock_blocks_the_adoption(self):
        self.assertRefused("time", self.adopt, now_utc=WRONG)

    def test_same_machine_needs_no_adoption(self):
        self.assertRefused("host", self.adopt, host_id=HOST)

    def test_corrupt_source_is_refused(self):
        source = self.restored()
        data = bytearray(source.read_bytes())
        data[4096:4196] = b"\xff" * 100
        source.write_bytes(bytes(data))
        with self.assertRaises(HistoryError) as caught:
            self.adopt(source)
        self.assertIn(caught.exception.reason, ("corrupt", "integrity"))
        self.assertFalse((self.root / "prevzata.sqlite3").exists())

    def test_missing_target_directory_is_refused(self):
        self.assertRefused("path", self.adopt, target=self.root / "neni" / "x.sqlite3")

    def test_refused_adoption_leaves_no_leftovers(self):
        source = self.restored()
        self.assertRefused("time", self.adopt, source, now_utc=WRONG)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         sorted(["history.sqlite3", "history.lock", "restored.sqlite3",
                                 "snapshot", "snapshot-work"]))


class AdoptCrashTest(RestoreCase):
    """Pád procesu v jednotlivých fázích převzetí (oddělené procesy)."""

    def child(self, source, target, stage, moment=LATER):
        script = self.root / "child.py"
        script.write_text(CHILD, encoding="utf-8")
        done = subprocess.run([sys.executable, str(script), str(REPO / "src"), str(source), str(target),
                               stage, str(moment)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 81, f"dítě neskončilo pádem: {done.stdout} {done.stderr}")

    def leftovers(self, target):
        return sorted(path.name for path in self.root.iterdir()
                      if path.name.startswith(f".{target.name}."))

    def test_crash_before_copy_leaves_no_target(self):
        source = self.restored()
        before = source.read_bytes()
        target = self.root / "prevzata.sqlite3"
        self.child(source, target, "before_copy")
        self.assertFalse(target.exists())
        self.assertEqual(source.read_bytes(), before)

    def test_crash_during_modification_leaves_no_target(self):
        source = self.restored()
        before = source.read_bytes()
        target = self.root / "prevzata.sqlite3"
        self.child(source, target, "during_write")
        self.assertFalse(target.exists())
        self.assertEqual(source.read_bytes(), before)
        # Nedokončený dočasný soubor je rozpoznatelný a převzetí lze zopakovat.
        for name in self.leftovers(target):
            self.assertTrue(name.endswith(history.ADOPT_SUFFIX), name)
        report = history.adopt_database(source, target, host_id=OTHER_HOST, now_utc=LATER + 60)
        self.assertTrue(report.verified)

    def test_crash_right_after_publication_keeps_a_valid_database(self):
        source = self.restored()
        target = self.root / "prevzata.sqlite3"
        self.child(source, target, "after_publish")
        self.assertTrue(target.exists())
        info = history.database_info(target, host_id=OTHER_HOST)
        self.assertTrue(info.openable, info.problems)
        self.assertEqual(info.adopted[history.ADOPTED_HOST_KEY], HOST)
        self.assertEqual(info.snapshot_utc, SNAP)  # obnovu zaeviduje první start
        self.assertEqual(len(self.events(target, "history.adopted")), 1)


class RetentionPlanTest(RestoreCase):
    """Návrh retence generací: jen čtení a dry-run, nikdy mazání (9.7)."""

    def make(self, *moments, broken=()):
        """Vytvoří generace v zadaných časech; `broken` jmenuje poškozené."""
        self.generation(moments[0])
        for moment in moments[1:]:
            with history.open_writer(self.db, "test", HOST) as database:
                snapshot.create(database.connection, self.work, self.published, moment,
                                app_version="test", role="test", host_id=HOST)
        for moment in broken:
            name = snapshot.generation_name(moment)
            (self.published / name / snapshot.SNAPSHOT_RECORD).write_text("{", encoding="utf-8")
        return [snapshot.generation_name(moment) for moment in moments]

    def plan(self, **options):
        options.setdefault("now_utc", SNAP + 30 * DAY)
        return snapshot.retention_candidates(self.published, **options)

    def test_empty_directory_has_no_candidates(self):
        plan = self.plan()
        self.assertEqual((plan.generations, plan.candidates, plan.deletable), ((), (), ()))
        self.assertIsNone(plan.newest_valid)

    def test_young_generations_are_protected(self):
        names = self.make(SNAP, SNAP + 3600)
        plan = self.plan(now_utc=SNAP + 3600 + 60, keep=1)
        self.assertEqual(plan.candidates, ())
        for name in names:
            self.assertIn(name, plan.protected)

    def test_newest_valid_is_always_protected(self):
        names = self.make(SNAP, SNAP + DAY, SNAP + 2 * DAY)
        plan = self.plan(keep=0)  # i při nulovém počtu zůstane poslední platná
        self.assertEqual(plan.newest_valid, names[-1])
        self.assertEqual(plan.protected[names[-1]], "poslední platná generace")
        self.assertEqual(plan.candidates, (names[0], names[1]))

    def test_known_restorable_generation_is_protected(self):
        names = self.make(SNAP, SNAP + DAY, SNAP + 2 * DAY)
        plan = self.plan(keep=0, restorable=names[0])
        self.assertEqual(plan.protected[names[0]], "poslední známá obnovitelná generace")
        self.assertEqual(plan.candidates, (names[1],))

    def test_invalid_generation_is_never_a_candidate(self):
        names = self.make(SNAP, SNAP + DAY, SNAP + 2 * DAY, broken=(SNAP + DAY,))
        plan = self.plan(keep=0)
        broken = next(state for state in plan.generations if state.name == names[1])
        self.assertFalse(broken.valid)
        self.assertIsNotNone(broken.problem)
        self.assertIn("rozhodne správce", plan.protected[names[1]])
        self.assertNotIn(names[1], plan.candidates)
        self.assertEqual(plan.candidates, (names[0],))

    def test_surplus_beyond_keep_becomes_a_candidate(self):
        names = self.make(*(SNAP + step * DAY for step in range(5)))
        plan = self.plan(keep=2, min_keep_seconds=DAY)
        self.assertEqual(plan.candidates, tuple(names[:3]))
        self.assertEqual(set(plan.protected), set(names[3:]))

    def test_nothing_is_deletable_and_nothing_changes_on_disk(self):
        self.make(*(SNAP + step * DAY for step in range(4)))
        listing = sorted(str(path) for path in self.published.rglob("*"))
        digests = {path: path.read_bytes() for path in self.published.rglob("*") if path.is_file()}
        plan = self.plan(keep=1, min_keep_seconds=0, backup_verified=True)
        self.assertTrue(plan.candidates)  # kandidáti ano
        self.assertEqual(plan.deletable, ())  # mazat se ale nesmí nic
        self.assertTrue(any(blocker.startswith("not_implemented") for blocker in plan.blockers))
        self.assertEqual(sorted(str(path) for path in self.published.rglob("*")), listing)
        for path, data in digests.items():
            self.assertEqual(path.read_bytes(), data)

    def test_unverified_backup_blocks_the_plan(self):
        self.make(SNAP, SNAP + DAY)
        plan = self.plan(keep=0, min_keep_seconds=0)
        self.assertTrue(any(blocker.startswith("backup_unverified") for blocker in plan.blockers))
        self.assertEqual(plan.deletable, ())

    def test_unverifiable_path_blocks_everything(self):
        link = self.root / "odkaz"
        link.symlink_to(self.published)
        plan = snapshot.retention_candidates(link, now_utc=SNAP)
        self.assertEqual((plan.generations, plan.candidates), ((), ()))
        self.assertTrue(plan.blockers[0].startswith("path:"))


class CommandLineTest(RestoreCase):
    """Příkazová řádka: návratové kódy a výstup (python3 -m nexusstat.history)."""

    def run_cli(self, *argv, now_utc=LATER):
        self.lines = []
        code = history_cli.main(list(argv), output=self.lines.append, now_utc=now_utc)
        return code, "\n".join(self.lines)

    def test_info_of_a_usable_database_returns_zero(self):
        self.filled()
        with mock.patch.object(history, "local_host_id", return_value=HOST):
            code, text = self.run_cli("info", str(self.db))
        self.assertEqual(code, 0)
        self.assertIn("vhodná k otevření na tomto stroji: ano", text)
        self.assertIn("journal_mode: wal", text)

    def test_info_of_a_foreign_database_returns_one(self):
        path = self.restored()
        with mock.patch.object(history, "local_host_id", return_value=OTHER_HOST):
            code, text = self.run_cli("info", str(path))
        self.assertEqual(code, 1)
        self.assertIn("vyžaduje ruční převzetí (adopt): ano", text)
        self.assertNotIn(HOST, text)

    def test_info_of_missing_file_returns_one(self):
        code, text = self.run_cli("info", str(self.root / "nic.sqlite3"))
        self.assertEqual(code, 1)
        self.assertIn("neexistuje", text)

    def test_adopt_prints_the_report(self):
        path = self.restored()
        target = self.root / "prevzata.sqlite3"
        with mock.patch.object(history, "local_host_id", return_value=OTHER_HOST):
            code, text = self.run_cli("adopt", str(path), "--output", str(target))
        self.assertEqual(code, 0)
        self.assertIn("nezávislé ověření nové databáze: ano", text)
        self.assertIn("vstupní soubor zůstal beze změny", text)
        self.assertTrue(target.exists())

    def test_adopt_refusal_is_a_message_not_a_traceback(self):
        path = self.restored()
        with mock.patch.object(history, "local_host_id", return_value=HOST):
            code, text = self.run_cli("adopt", str(path), "--output", str(self.root / "x.sqlite3"))
        self.assertEqual(code, 1)
        self.assertIn("Odmítnuto (host)", text)
        self.assertFalse((self.root / "x.sqlite3").exists())

    def test_generations_prints_the_dry_run_plan(self):
        self.generation(SNAP)
        code, text = self.run_cli("generations", str(self.published), now_utc=SNAP + 30 * DAY)
        self.assertEqual(code, 0)
        self.assertIn("skutečně ke smazání nyní: nic", text)
        self.assertIn("blokuje: not_implemented", text)
        self.assertIn(snapshot.generation_name(SNAP), text)

    def test_generations_of_a_bad_path_returns_one(self):
        code, text = self.run_cli("generations", str(self.root / "neni"))
        self.assertEqual(code, 1)
        self.assertIn("path:", text)


class FailingInserts:
    """Obal spojení, kde vkládání událostí selže: transakce se vrátí celá."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, *args):
        return self._connection.execute(sql, *args)

    def executemany(self, sql, *args):
        raise sqlite3.OperationalError("disk I/O error")

    def __getattr__(self, name):
        return getattr(self._connection, name)


def doc_block(name):
    """SQL blok z docs/HISTORY.md (čtecí kontrakt, stejně jako v test_history)."""
    import re
    text = (REPO / "docs" / "HISTORY.md").read_text(encoding="utf-8")
    return re.search(rf"<!-- sql:{re.escape(name)} -->\s*```sql\n(.*?)```", text, re.S).group(1)


class RestoreGapTest(RestoreCase):
    """Mezera v historii po obnově: čtecí kontrakty nesmí domýšlet (review bod 4)."""

    def setUp(self):
        super().setUp()
        self.t1 = BASE + 120  # čas snímku
        self.t2 = BASE + 2 * DAY  # obnova o dva dny později

    def critical(self, moment, state=State.CRITICAL, link="down"):
        observed, expires = at(moment), moment + 30.0
        return [CheckResult(CPU_TEMP_ID, state, f"CPU {state.value}", observed, expires, simulated=False,
                            measurement=Measurement(95.0 if state is State.CRITICAL else 50.0, CELSIUS)),
                CheckResult(LINK_ID, State.INFO, f"eth0 {link}", observed, expires, simulated=False,
                            details={INTERFACE: "eth0", LINK: link})]

    def scenario(self):
        """Potvrzené CRITICAL před snímkem, pokračující provoz, obnova za dva dny."""
        self.create()
        with history.open_writer(self.db, "test", HOST) as database:
            recorder = HistoryRecorder(database)
            recorder.start_run("test", 10, at(BASE - 60), "boot-1")
            for moment, state in ((BASE, State.INFO), (BASE + 60, State.CRITICAL)):
                batch = recorder.begin_batch()
                recorder.add_results(batch, self.critical(moment, state), "synced")
                recorder.write(batch)
            generation = snapshot.create(database.connection, self.work, self.published, self.t1,
                                         app_version="test", role="test", host_id=HOST)
            # Původní server měří dál: tato data ve snímku nejsou.
            for moment, state, link in ((BASE + 180, State.CRITICAL, "up"), (BASE + 240, State.INFO, "up")):
                batch = recorder.begin_batch()
                recorder.add_results(batch, self.critical(moment, state, link), "synced")
                recorder.write(batch)
            recorder.finish_run(at(BASE + 300))
        snapshot_rows = self.rows(generation.path / snapshot.SNAPSHOT_DB)
        path = self.restored(generation, name="obnovena.sqlite3")
        # Obnova v T2 a nové měření, které je znovu CRITICAL.
        with history.open_writer(path, "test", HOST) as database:
            recorder = HistoryRecorder(database)
            recorder.start_run("test", 10, at(self.t2), "boot-2")
            for step, state in enumerate((State.CRITICAL, State.CRITICAL)):
                batch = recorder.begin_batch()
                recorder.add_results(batch, self.critical(self.t2 + 10 + step * 10, state), "synced")
                recorder.write(batch)
            recorder.finish_run(at(self.t2 + 60))
        return path, snapshot_rows

    def rows(self, path):
        with history.open_reader(path) as reader:
            changes = reader.connection.execute(
                "SELECT ts, ts_quality, run_id, check_id, from_state, to_state, details FROM check_change "
                "ORDER BY id").fetchall()
            samples = reader.connection.execute(
                "SELECT metric_id, ts, value FROM sample ORDER BY ts, metric_id").fetchall()
        return changes, samples

    def episodes(self, path, state=State.CRITICAL.value, check_id=CPU_TEMP_ID, start=0, end=BASE + 10 * DAY):
        with history.open_reader(path) as reader:
            return reader.connection.execute(doc_block("state-episodes"),
                                             {"check_id": check_id, "state": state,
                                              "start": start, "end": end}).fetchall()

    def details_episodes(self, path, key=LINK, value="down", start=0, end=BASE + 10 * DAY):
        with history.open_reader(path) as reader:
            return reader.connection.execute(doc_block("details-episodes"),
                                             {"check_id": LINK_ID, "key": key, "value": value,
                                              "start": start, "end": end}).fetchall()

    def test_state_episodes_do_not_bridge_the_unknown_period(self):
        path, _ = self.scenario()
        episodes = self.episodes(path)
        # Epizoda CRITICAL ze snímku: konec ani délka se nedomýšlí (konec běhu).
        self.assertEqual(len(episodes), 2, episodes)
        first, second = episodes
        self.assertEqual(first[0], BASE + 60)  # start_ts přechodu do CRITICAL
        self.assertIsNone(first[1])  # end_ts neznámý
        self.assertIsNone(first[2])  # duration_s se nevymýšlí
        # Nové CRITICAL po obnově je samostatná epizoda označená jako „trvá už při startu běhu“.
        self.assertEqual(second[0], self.t2 + 10)
        self.assertEqual(second[3], 1)  # start_at_run_begin: skutečný začátek neznámý
        self.assertIsNone(second[1])
        # Žádná epizoda nepřeklenuje neznámý úsek mezi snímkem a obnovou.
        for start, end, duration, _ in episodes:
            self.assertFalse(start is not None and end is not None and start <= self.t1 <= end)
            self.assertFalse(duration is not None and duration >= DAY)

    def test_details_episodes_also_admit_the_unknown_period(self):
        path, _ = self.scenario()
        episodes = self.details_episodes(path)
        self.assertTrue(episodes)
        for start, end, duration, _ in episodes:
            self.assertFalse(duration is not None and duration >= DAY)
            self.assertFalse(start is not None and end is not None and start <= self.t1 <= end)

    def test_unknown_gap_marks_the_boundary_exactly_once(self):
        path, _ = self.scenario()
        gaps = self.events(path, "history.unknown_gap")
        self.assertEqual(len(gaps), 1)
        data = json.loads(gaps[0][4])
        self.assertEqual(data["reason"], "restored")
        self.assertEqual(data["after"], BASE + 60)  # nejnovější vzorek ve snímku
        self.assertEqual(data["until"], self.t2)
        self.assertEqual(data["snapshot_utc"], self.t1)
        restored = self.events(path, "history.restored")
        self.assertEqual(len(restored), 1)
        self.assertEqual(self.events(path, "process.unclean_stop"), [])
        # Táž ztráta se nepočítá dvakrát: hranice jsou jen v jedné události.
        self.assertNotIn("until", json.loads(restored[0][4]))

    def test_rows_before_the_snapshot_stay_untouched(self):
        path, snapshot_rows = self.scenario()
        changes, samples = self.rows(path)
        original_changes, original_samples = snapshot_rows
        self.assertEqual(changes[:len(original_changes)], original_changes)
        self.assertEqual(samples[:len(original_samples)], original_samples)
        self.assertTrue(original_samples)

    def test_aggregation_creates_no_rows_inside_the_gap(self):
        path, _ = self.scenario()
        with history.open_writer(path, "test", HOST) as database:
            recorder = HistoryRecorder(database)
            recorder.start_run("test", 10, at(self.t2 + 3600), "boot-3")
            for _ in range(60):  # dohánění agregace přes celý neznámý úsek
                result = recorder.aggregate(at(self.t2 + 7200), "synced", history.QueueState.empty())
                if not result.more:
                    break
            recorder.finish_run(at(self.t2 + 7300))
        with history.open_reader(path) as reader:
            inside = reader.connection.execute(
                "SELECT count(*) FROM agg_5m WHERE bucket >= ? AND bucket < ?",
                (BASE + 300, self.t2)).fetchone()[0]
            hourly = reader.connection.execute(
                "SELECT count(*) FROM agg_1h WHERE bucket >= ? AND bucket < ?",
                (BASE + 3600, self.t2)).fetchone()[0]
            samples = reader.connection.execute(
                "SELECT count(*) FROM sample WHERE ts > ? AND ts < ?", (BASE + 60, self.t2)).fetchone()[0]
        self.assertEqual((inside, hourly, samples), (0, 0, 0))  # žádná vymyšlená měření

    def test_crash_before_commit_keeps_the_evidence_possible(self):
        path, _ = self.scenario()
        # Druhá obnova téhož souboru: pád před potvrzením nechá značku na místě.
        second = self.root / "obnovena2.sqlite3"
        shutil.copyfile(self.published / snapshot.generation_name(self.t1) / snapshot.SNAPSHOT_DB, second)
        database = history.open_writer(second, "test", HOST)
        recorder = HistoryRecorder(database)
        real = database.connection
        database.connection = FailingInserts(real)  # pád před COMMIT
        try:
            with self.assertRaises(HistoryError):
                recorder.start_run("test", 10, at(self.t2), "boot-2")
        finally:
            database.connection = real
            database.close()
        self.assertEqual(self.meta(second)[history.SNAPSHOT_KEY], str(self.t1))  # značka zůstala
        self.assertEqual(self.events(second, "history.restored"), [])
        marker, _, recorded = self.start(second, moment=self.t2 + 10)
        self.assertEqual((marker, recorded), (self.t1, self.t1))
        self.assertEqual(len(self.events(second, "history.restored")), 1)
        self.assertEqual(len(self.events(second, "history.unknown_gap")), 1)

    def test_repeated_restart_does_not_duplicate_the_gap(self):
        path, _ = self.scenario()
        self.start(path, moment=self.t2 + 600)
        self.start(path, moment=self.t2 + 1200)
        self.assertEqual(len(self.events(path, "history.restored")), 1)
        self.assertEqual(len(self.events(path, "history.unknown_gap")), 1)


class InfoConcurrencyTest(RestoreCase):
    """info nad potenciálně živou databází (review bod 1)."""

    def test_live_delete_mode_database_without_wal(self):
        # Aktivní databáze bez WAL: absence -wal nic nedokazuje, čtení proto
        # zamyká a vidí jen potvrzená data.
        self.filled()
        standalone = self.root / "zive.sqlite3"
        shutil.copyfile(self.db, standalone)
        raw = sqlite3.connect(standalone, isolation_level=None)
        raw.execute("PRAGMA journal_mode = delete")
        try:
            raw.execute("BEGIN IMMEDIATE")
            raw.execute("INSERT INTO event (ts, ts_quality, kind, severity, message) "
                        "VALUES (?, 'synced', 'test.nepotvrzeno', 'INFO', 'jen v transakci')", (BASE,))
            with mock.patch.object(history, "READER_BUSY_TIMEOUT_MS", 100):
                info = history.database_info(standalone, host_id=HOST)
            # Otevřená transakce jiného procesu: buď se přizná zámek, nebo se
            # přečtou jen potvrzená data. Nikdy nepotvrzený řádek.
            if info.counts:
                self.assertEqual(info.counts["event"], 2)
            else:
                self.assertTrue(any("lock" in problem or "zamč" in problem for problem in info.problems),
                                info.problems)
            raw.execute("COMMIT")
        finally:
            raw.close()
        after = history.database_info(standalone, host_id=HOST)
        self.assertEqual(after.counts["event"], 3)  # po potvrzení je řádek vidět

    def test_change_by_another_process_is_visible_on_the_next_read(self):
        self.filled()
        first = history.database_info(self.db, host_id=HOST)
        with history.open_writer(self.db, "test", HOST) as database:
            recorder = HistoryRecorder(database)
            recorder.start_run("test", 10, at(BASE + 60), "boot-2")
            batch = recorder.begin_batch()
            recorder.add_results(batch, sample(BASE + 60, 51.0), "synced")
            recorder.write(batch)
            recorder.finish_run(at(BASE + 70))
        second = history.database_info(self.db, host_id=HOST)
        self.assertEqual(first.counts["sample"], 1)
        self.assertEqual(second.counts["sample"], 2)  # žádný zastaralý obraz
        self.assertEqual(second.runs, 2)

    def test_info_of_an_active_database_with_wal(self):
        self.create()
        database = history.open_writer(self.db, "test", HOST)
        try:
            recorder = HistoryRecorder(database)
            recorder.start_run("test", 10, at(BASE), "boot-1")
            batch = recorder.begin_batch()
            recorder.add_results(batch, sample(BASE), "synced")
            recorder.write(batch)
            info = history.database_info(self.db, host_id=HOST)
        finally:
            database.close()
        self.assertEqual(info.counts["sample"], 1)  # potvrzená data z WAL
        self.assertEqual(info.journal_mode, "wal")
        self.assertTrue(any(name.endswith("-wal") for name in info.companions))
        self.assertEqual(info.integrity, "ok")


class AdoptConcurrencyTest(RestoreCase):
    """Souběh s cizím zapisovačem při převzetí (review bod 5)."""

    def test_source_changed_during_the_copy_is_refused(self):
        source = self.restored()
        target = self.root / "prevzata.sqlite3"
        real = shutil.copyfile

        def copy_then_write(src, dst, **options):
            result = real(src, dst, **options)
            raw = sqlite3.connect(source, isolation_level=None)  # cizí zapisovač mimo NexusSTAT
            raw.execute("BEGIN IMMEDIATE")
            raw.execute("INSERT INTO event (ts, ts_quality, kind, severity, message) "
                        "VALUES (?, 'synced', 'cizi.zapis', 'INFO', 'zápis během kopie')", (BASE,))
            raw.execute("COMMIT")
            raw.close()
            return result

        with mock.patch.object(history.shutil, "copyfile", copy_then_write):
            error = self.assertRefused("busy", history.adopt_database, source, target,
                                       host_id=OTHER_HOST, now_utc=LATER)
        self.assertIn("během kopírování", str(error))
        self.assertFalse(target.exists())
        self.assertEqual([name for name in self.root.iterdir() if name.name.startswith(".prevzata")], [])

    def test_external_sqlite_writer_in_wal_mode_is_detected(self):
        # Cizí program otevřený nad databází ve WAL po sobě nechá -wal a -shm:
        # právě to převzetí odmítne, i když zámek NexusSTAT nedrží nikdo.
        source = self.restored()
        raw = sqlite3.connect(source, isolation_level=None)
        try:
            raw.execute("PRAGMA journal_mode = wal")
            raw.execute("BEGIN IMMEDIATE")
            raw.execute("INSERT INTO event (ts, ts_quality, kind, severity, message) "
                        "VALUES (?, 'synced', 'cizi.zapisovac', 'INFO', 'cizí proces')", (BASE,))
            raw.execute("COMMIT")
            self.assertTrue(Path(f"{source}-wal").exists())
            error = self.assertRefused("exists", history.adopt_database, source,
                                       self.root / "prevzata.sqlite3", host_id=OTHER_HOST, now_utc=LATER)
            self.assertIn("samostatný soubor", str(error))
        finally:
            raw.close()
