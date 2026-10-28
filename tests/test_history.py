"""Testy SQLite historie: vytvoření, otevření, ověření a zámek (M3.1a), zápis (M3.1b), agregace (M3.2a) a retence (M3.2b).

Vše jen v tempfile.TemporaryDirectory. Simulace chyb upravují výhradně kopie
databáze vytvořené testem; žádná produkční cesta, žádné skutečné čekání ani
změna systémového času (čas měření je parametr výsledků).
"""

import fcntl
import hashlib
import importlib.util
import inspect
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from nexusstat import history
from nexusstat.core import CheckResult, Measurement, State
from nexusstat.fan_monitor import CPU_FAN_ID, RPM
from nexusstat.history import ClockState, Event, HistoryError, HistoryRecorder, PendingSample, QueueState
from nexusstat.network_monitor import BYTES_PER_SECOND, INTERFACE, LINK, LINK_ID, RX_RATE_ID, TX_RATE_ID
from nexusstat.system_monitor import CELSIUS, CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID, PERCENT

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "HISTORY.md"
HOST = "0123456789abcdef"
OTHER_HOST = "fedcba9876543210"
CREATED = 1_800_001_234  # pevný čas vytvoření, žádné hodiny


def doc_block(name):
    match = re.search(rf"<!-- sql:{re.escape(name)} -->\s*```sql\n(.*?)```", DOC.read_text(encoding="utf-8"), re.S)
    return match.group(1)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def listing(folder):
    return sorted(item.name for item in Path(folder).iterdir())


def raw(path):
    """Přímé spojení sqlite3 jen pro přípravu kopií v testech, mimo history.py."""
    return sqlite3.connect(path, isolation_level=None)


def change(path, statement):
    """Úprava kopie v testu se zavřením spojení (změna se dostane do hlavního souboru)."""
    conn = raw(path)
    try:
        conn.execute(statement)
    finally:
        conn.close()


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.folder = self.root / "db"
        self.folder.mkdir()
        self.db = self.folder / "history.sqlite3"

    def tearDown(self):
        self._temp.cleanup()

    def create(self, path=None, role="test", host=HOST):
        history.create_database(path or self.db, role, host, now_utc=CREATED)
        return path or self.db

    def copy(self, name="copy.sqlite3"):
        """Kopie čistě zavřené databáze (bez -wal) pro úpravy v testu."""
        target = self.folder / name
        shutil.copyfile(self.db, target)
        return target

    def assertRefused(self, reason, function, *args, **kwargs):
        with self.assertRaises(HistoryError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.reason, reason, str(caught.exception))
        return caught.exception


class SchemaContractTest(TempDirTest):
    """Shoda implementace s návrhem M2 v docs/HISTORY.md."""

    def test_schema_sql_is_exact_copy_of_documented_ddl(self):
        self.assertEqual(history.SCHEMA_SQL, doc_block("ddl"))

    def test_connection_settings_match_documented_block(self):
        documented = dict(re.findall(r"^PRAGMA (\w+) = (\w+);", doc_block("connection"), re.M))
        implemented = {name: value for name, value, _ in history.CONNECTION_SETTINGS}
        implemented["journal_mode"] = history.JOURNAL_MODE.upper()
        self.assertEqual(implemented, documented)

    def test_created_database_matches_schema_built_from_documentation(self):
        self.create()
        reference = sqlite3.connect(":memory:")
        reference.executescript(doc_block("ddl"))
        created = raw(self.db)
        query = "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        self.assertEqual(created.execute(query).fetchall(), reference.execute(query).fetchall())
        for pragma in ("application_id", "user_version"):
            self.assertEqual(created.execute(f"PRAGMA {pragma}").fetchone(), reference.execute(f"PRAGMA {pragma}").fetchone())
        created.close()
        reference.close()

    def test_tables_are_strict_and_without_rowid_as_designed(self):
        self.create()
        conn = raw(self.db)
        tables = {name: (wr, strict) for name, wr, strict in conn.execute(
            "SELECT name, wr, strict FROM pragma_table_list WHERE schema = 'main' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(sorted(tables), sorted(history.SCHEMA_TABLES))
        self.assertTrue(all(strict == 1 for _, strict in tables.values()))
        self.assertEqual({name for name, (wr, _) in tables.items() if wr},
                         {"meta", "sample", "agg_5m", "agg_1h", "aggregation_state"})
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'index' AND sql IS NOT NULL")}
        self.assertEqual(indexes, set(history.SCHEMA_INDEXES))
        self.assertEqual(conn.execute("PRAGMA table_info(sample)").fetchall()[1][1:3], ("ts", "INTEGER"))
        conn.close()

    def test_module_works_without_documentation(self):
        # Provoz nesmí číst Markdown: čtení docs/HISTORY.md by selhalo.
        original = Path.read_text

        def guarded(path, *args, **kwargs):
            if path.name == "HISTORY.md":
                raise AssertionError("history.py nesmí číst dokumentaci")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", guarded):
            self.create()
            with history.open_writer(self.db, "test", HOST):
                pass
            with history.open_reader(self.db):
                pass


class CreateTest(TempDirTest):
    def test_new_database_has_metadata_and_only_its_file(self):
        self.create()
        self.assertEqual(listing(self.folder), ["history.sqlite3"])
        self.assertEqual(listing(self.root), ["db"])
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
        conn = raw(self.db)
        self.assertEqual(dict(conn.execute("SELECT key, value FROM meta")),
                         {"role": "test", "host_id": HOST, "created_utc": str(CREATED)})
        self.assertEqual(conn.execute("SELECT version, applied_utc FROM schema_migration").fetchall(), [(1, CREATED)])
        hour = CREATED - CREATED % 3600
        self.assertEqual(conn.execute("SELECT level, done_until FROM aggregation_state ORDER BY level").fetchall(),
                         [("1h", hour), ("5m", hour)])
        for table in ("run", "metric", "sample", "agg_5m", "agg_1h", "check_change", "event"):
            self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone(), (0,), table)
        self.assertEqual(conn.execute("PRAGMA application_id").fetchone(), (history.APPLICATION_ID,))
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone(), (1,))
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone(), ("wal",))
        self.assertEqual(conn.execute("PRAGMA page_size").fetchone(), (4096,))
        self.assertEqual(conn.execute("PRAGMA auto_vacuum").fetchone(), (0,))
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        conn.close()

    def test_development_role_can_be_created(self):
        self.create(role="development")
        with history.open_writer(self.db, "development", HOST) as database:
            self.assertFalse(database.readonly)

    def test_missing_directory_is_not_created(self):
        target = self.root / "missing" / "history.sqlite3"
        self.assertRefused("path", history.create_database, target, "test", HOST, now_utc=CREATED)
        self.assertFalse(target.parent.exists())
        self.assertEqual(listing(self.root), ["db"])

    def test_existing_file_is_never_overwritten(self):
        self.db.write_bytes(b"cizi data")
        self.assertRefused("exists", self.create)
        self.assertEqual(self.db.read_bytes(), b"cizi data")
        self.assertEqual(listing(self.folder), ["history.sqlite3"])

    def test_existing_database_keeps_its_data(self):
        self.create()
        with history.open_writer(self.db, "test", HOST) as database:
            database.connection.execute(
                "INSERT INTO run (id, started_utc, app_version, interval_s) VALUES (1, ?, 'test', 10)", (CREATED,))
        before = digest(self.db)
        self.assertRefused("exists", self.create)
        self.assertEqual(digest(self.db), before)
        with history.open_reader(self.db) as database:
            self.assertEqual(database.connection.execute("SELECT count(*) FROM run").fetchone(), (1,))

    def test_stale_companion_files_block_creation(self):
        for suffix in history.COMPANION_SUFFIXES:
            with self.subTest(suffix=suffix):
                stale = Path(f"{self.db}{suffix}")
                stale.write_bytes(b"stary")
                self.assertRefused("exists", self.create)
                self.assertFalse(self.db.exists())
                self.assertEqual(stale.read_bytes(), b"stary")
                stale.unlink()

    def test_symlink_in_place_of_database_is_not_followed(self):
        target = self.root / "jinde.sqlite3"
        self.db.symlink_to(target)
        self.assertRefused("exists", self.create)
        self.assertFalse(target.exists())

    def test_production_and_unknown_roles_are_refused(self):
        for role in ("production", "admin", None):
            with self.subTest(role=role):
                self.assertRefused("role", history.create_database, self.db, role, HOST, now_utc=CREATED)
        self.assertEqual(listing(self.folder), [])

    def test_invalid_host_id_is_refused(self):
        for host in ("ABCDEF0123456789", "0123", 123, None, "0123456789abcdeg"):
            with self.subTest(host=host):
                self.assertRefused("host", history.create_database, self.db, "test", host, now_utc=CREATED)
        self.assertEqual(listing(self.folder), [])

    def test_invalid_creation_time_is_refused(self):
        for value in (0, -5, True, 1.5, "1800000000"):
            with self.subTest(value=value):
                self.assertRefused("create", history.create_database, self.db, "test", HOST, now_utc=value)
        self.assertEqual(listing(self.folder), [])

    def test_failed_schema_removes_only_own_temporary_files(self):
        (self.folder / "jiny.txt").write_text("nedotknutelne")
        (self.folder / "history.sqlite3.bak").write_text("zaloha")
        with mock.patch.object(history, "SCHEMA_SQL", history.SCHEMA_SQL + "\nCREATE TABLE rozbita ("):
            self.assertRefused("create", self.create)
        self.assertEqual(listing(self.folder), ["history.sqlite3.bak", "jiny.txt"])
        self.assertEqual((self.folder / "jiny.txt").read_text(), "nedotknutelne")

    def test_failed_publication_removes_only_own_temporary_files(self):
        (self.folder / "jiny.txt").write_text("nedotknutelne")
        with mock.patch.object(history.os, "link", side_effect=PermissionError("hard link nepodporován")):
            self.assertRefused("create", self.create)
        self.assertEqual(listing(self.folder), ["jiny.txt"])

    def test_file_appearing_during_creation_is_not_overwritten(self):
        original_link = os.link

        def racing_link(source, target):
            Path(target).write_bytes(b"predbehl me")  # jiný proces byl rychlejší
            return original_link(source, target)

        with mock.patch.object(history.os, "link", racing_link):
            self.assertRefused("exists", self.create)
        self.assertEqual(self.db.read_bytes(), b"predbehl me")
        self.assertEqual(listing(self.folder), ["history.sqlite3"])


class OpenWriterTest(TempDirTest):
    def test_reopen_after_close_keeps_data(self):
        self.create()
        database = history.open_writer(self.db, "test", HOST)
        database.connection.execute(
            "INSERT INTO metric (id, check_id, unit, created_utc) VALUES (1, 'system.cpu_temp', '°C', ?)", (CREATED,))
        database.close()
        database.close()  # opakované zavření nic nedělá
        self.assertIsNone(database.connection)
        with history.open_writer(self.db, "test", HOST) as database:
            self.assertEqual(database.connection.execute("SELECT check_id FROM metric").fetchall(), [("system.cpu_temp",)])
        self.assertEqual(listing(self.folder), ["history.lock", "history.sqlite3"])
        self.assertEqual(listing(self.root), ["db"])

    def test_writer_connection_settings(self):
        self.create()
        with history.open_writer(self.db, "test", HOST) as database:
            conn = database.connection
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone(), ("wal",))
            for name, _, expected in history.CONNECTION_SETTINGS:
                with self.subTest(pragma=name):
                    self.assertEqual(conn.execute(f"PRAGMA {name}").fetchone()[0], expected)
            with self.assertRaises(sqlite3.IntegrityError):  # cizí klíče platí
                conn.execute("INSERT INTO sample (metric_id, ts, value) VALUES (99, 1, 1.0)")

    def test_missing_file_is_refused_and_not_created(self):
        self.assertRefused("path", history.open_writer, self.db, "test", HOST)
        self.assertRefused("path", history.open_reader, self.db)
        self.assertEqual(listing(self.folder), [])

    def test_foreign_sqlite_database_is_refused_without_side_effects(self):
        foreign = self.folder / "cizi.sqlite3"
        conn = raw(foreign)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.close()
        before = (digest(foreign), listing(self.folder))
        self.assertRefused("foreign", history.open_writer, foreign, "test", HOST)
        self.assertRefused("foreign", history.open_reader, foreign)
        self.assertEqual((digest(foreign), listing(self.folder)), before)  # žádný -wal, -shm ani zámek

    def test_non_sqlite_and_empty_files_are_refused(self):
        for name, content in (("text.sqlite3", b"SQLite neni" * 20), ("empty.sqlite3", b""),
                              ("short.sqlite3", b"SQLite format 3\x00")):
            with self.subTest(name=name):
                path = self.folder / name
                path.write_bytes(content)
                self.assertRefused("foreign", history.open_writer, path, "test", HOST)
                self.assertRefused("foreign", history.open_reader, path)
                self.assertEqual(path.read_bytes(), content)
        self.assertEqual(listing(self.folder), ["empty.sqlite3", "short.sqlite3", "text.sqlite3"])

    def test_directory_instead_of_file_is_refused(self):
        self.db.mkdir()
        self.assertRefused("path", history.open_writer, self.db, "test", HOST)

    def test_newer_schema_is_refused(self):
        self.create()
        newer = self.copy()
        change(newer, "PRAGMA user_version = 2")
        error = self.assertRefused("schema", history.open_writer, newer, "test", HOST)
        self.assertIn("novější", str(error))
        self.assertRefused("schema", history.open_reader, newer)
        with history.open_writer(self.db, "test", HOST):  # originál beze změny
            pass

    def test_unsupported_older_schema_is_refused(self):
        self.create()
        older = self.copy()
        change(older, "PRAGMA user_version = 0")
        self.assertRefused("schema", history.open_writer, older, "test", HOST)

    def test_modified_schema_is_refused(self):
        self.create()
        for name, statement in (("column.sqlite3", "ALTER TABLE run ADD COLUMN navic TEXT"),
                                ("table.sqlite3", "CREATE TABLE navic (x INTEGER)"),
                                ("index.sqlite3", "DROP INDEX event_ts")):
            with self.subTest(change=statement):
                modified = self.copy(name)
                change(modified, statement)
                self.assertRefused("schema", history.open_writer, modified, "test", HOST)
                self.assertRefused("schema", history.open_reader, modified)

    def test_missing_metadata_is_refused(self):
        self.create()
        broken = self.copy()
        change(broken, "DELETE FROM meta WHERE key = 'host_id'")
        self.assertRefused("schema", history.open_writer, broken, "test", HOST)

    def test_wrong_role_and_host_are_refused_without_changes(self):
        self.create()
        before = digest(self.db)
        self.assertRefused("role", history.open_writer, self.db, "development", HOST)
        self.assertRefused("role", history.open_writer, self.db, "production", HOST)
        self.assertRefused("host", history.open_writer, self.db, "test", OTHER_HOST)
        self.assertRefused("host", history.open_writer, self.db, "test", "neplatny")
        self.assertEqual(digest(self.db), before)
        self.assertRefused("role", history.open_reader, self.db, role="development")
        self.assertRefused("host", history.open_reader, self.db, host_id=OTHER_HOST)
        with history.open_reader(self.db, role="test", host_id=HOST):
            pass

    def test_corrupted_copy_is_refused_and_lock_released(self):
        self.create()
        broken = self.copy()
        conn = raw(broken)
        root = conn.execute("SELECT rootpage FROM sqlite_schema WHERE name = 'metric'").fetchone()[0]
        conn.close()
        with open(broken, "r+b") as file:
            file.seek((root - 1) * 4096)
            file.write(b"\xff" * 4096)
        self.assertRefused("corrupt", history.open_writer, broken, "test", HOST)
        # Zámek po neúspěchu nezůstal držený.
        descriptor = os.open(history.lock_path(broken), os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(descriptor)

    def test_second_writer_in_same_process_is_refused(self):
        self.create()
        first = history.open_writer(self.db, "test", HOST)
        self.assertRefused("locked", history.open_writer, self.db, "test", HOST)
        first.close()
        with history.open_writer(self.db, "test", HOST):
            pass

    def test_second_writer_in_other_process_is_refused(self):
        self.create()
        code = ("import sys\nfrom nexusstat import history\ntry:\n"
                "    history.open_writer(sys.argv[1], 'test', sys.argv[2]).close()\n"
                "except history.HistoryError as error:\n    print(error.reason)\nelse:\n    print('opened')\n")
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        run = lambda: subprocess.run([sys.executable, "-c", code, str(self.db), HOST], env=env,
                                     capture_output=True, text=True, timeout=60).stdout.strip()
        with history.open_writer(self.db, "test", HOST):
            self.assertEqual(run(), "locked")
        self.assertEqual(run(), "opened")


class ReaderTest(TempDirTest):
    def test_reader_does_not_take_writer_lock(self):
        self.create()
        with history.open_reader(self.db) as reader:
            self.assertTrue(reader.readonly)
            with history.open_writer(self.db, "test", HOST):
                pass

    def test_reader_cannot_write_or_checkpoint(self):
        self.create()
        writer = history.open_writer(self.db, "test", HOST)
        writer.connection.execute("PRAGMA wal_autocheckpoint = 0")  # změny zůstanou ve WAL
        writer.connection.execute(
            "INSERT INTO run (id, started_utc, app_version, interval_s) VALUES (1, ?, 'test', 10)", (CREATED,))
        wal = Path(f"{self.db}-wal")
        before = (digest(self.db), wal.stat().st_size)
        reader = history.open_reader(self.db)
        conn = reader.connection
        self.assertEqual(conn.execute("SELECT count(*) FROM run").fetchone(), (1,))
        self.assertEqual(conn.execute("PRAGMA query_only").fetchone(), (1,))
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO event (ts, ts_quality, kind, severity, message) "
                         "VALUES (1, 'synced', 'x', 'INFO', 'x')")
        for statement in ("PRAGMA wal_checkpoint(PASSIVE)", "PRAGMA wal_checkpoint(TRUNCATE)"):
            try:
                conn.execute(statement).fetchall()
            except sqlite3.Error:
                pass
        self.assertEqual((digest(self.db), wal.stat().st_size), before)
        writer.close()  # zapisovač končí dřív než čtenář: checkpoint neprovede
        self.assertEqual((digest(self.db), wal.stat().st_size), before)
        reader.close()  # poslední spojení jen pro čtení: checkpoint neprovede ani teď
        self.assertEqual((digest(self.db), wal.stat().st_size), before)
        with history.open_writer(self.db, "test", HOST) as database:
            self.assertEqual(database.connection.execute("SELECT count(*) FROM run").fetchone(), (1,))
        self.assertFalse(wal.exists())  # checkpoint udělal až zapisovač při zavření

    def test_reader_opens_copy_on_other_machine_without_role(self):
        self.create()
        with history.open_reader(self.copy()) as reader:  # např. test obnovy: role a stroj se neověřují
            self.assertEqual(reader.connection.execute("SELECT value FROM meta WHERE key = 'role'").fetchone(), ("test",))


CHILD = r"""
import os, sqlite3, sys
from nexusstat import history
point, db, host = sys.argv[1], sys.argv[2], sys.argv[3]
if point == "commit":
    class Crashing(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "COMMIT":
                os._exit(70)  # pád uprostřed inicializační transakce
            return super().execute(sql, *args)
    real_connect = sqlite3.connect
    sqlite3.connect = lambda *a, **k: real_connect(*a, factory=Crashing, **k)
    history.create_database(db, "test", host, now_utc=1800001234)
elif point == "link":
    real_link = os.link
    def link(source, target):
        real_link(source, target)
        os._exit(71)  # pád po zveřejnění, před úklidem dočasného jména
    os.link = link
    history.create_database(db, "test", host, now_utc=1800001234)
elif point == "newer-wal":
    conn = sqlite3.connect(db, isolation_level=None)  # budoucí verze, mimo history.py
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("PRAGMA user_version = 7")
    os._exit(73)  # pád: novější schéma je jen ve -wal
elif point == "writer":
    database = history.open_writer(db, "test", host)
    database.connection.execute("PRAGMA wal_autocheckpoint = 0")
    database.connection.execute(
        "INSERT INTO run (id, started_utc, app_version, interval_s) VALUES (1, 1800001234, 'x', 10)")
    os._exit(72)  # nečisté ukončení: data jen ve -wal
elif point == "aggregate":
    from datetime import datetime, timezone
    class Crashing(sqlite3.Connection):
        armed = False
        def execute(self, sql, *args):
            if sql == history.AGGREGATE_5M_SQL:
                Crashing.armed = True
            elif sql == "COMMIT" and Crashing.armed:
                os._exit(74)  # pád po vložení 5min oken, před COMMIT
            return super().execute(sql, *args)
    real_connect = sqlite3.connect
    sqlite3.connect = lambda *a, **k: real_connect(*a, factory=Crashing, **k)
    now = datetime.fromtimestamp(int(sys.argv[4]), timezone.utc)
    recorder = history.HistoryRecorder(history.open_writer(db, "test", host))
    recorder.start_run("test", 10, now)
    recorder.aggregate(now, "synced", history.QueueState.empty())
    os._exit(75)  # sem se nesmí dostat
elif point == "retain":
    from datetime import datetime, timezone
    deletes = {sql for _, sql, _ in history.RETENTION_SQL.values()}
    class Crashing(sqlite3.Connection):
        armed = False
        def execute(self, sql, *args):
            if sql in deletes:
                Crashing.armed = True
            elif sql == "COMMIT" and Crashing.armed:
                os._exit(76)  # pád po DELETE, před COMMIT
            return super().execute(sql, *args)
    real_connect = sqlite3.connect
    sqlite3.connect = lambda *a, **k: real_connect(*a, factory=Crashing, **k)
    now = datetime.fromtimestamp(int(sys.argv[4]), timezone.utc)
    recorder = history.HistoryRecorder(history.open_writer(db, "test", host))
    recorder.start_run("test", 10, now)
    recorder.retain(now, history.ClockState.synced_for(86400))
    os._exit(77)  # sem se nesmí dostat
"""


def lock_is_free(path):
    descriptor = os.open(history.lock_path(path), os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(descriptor)


def immutable_count(path, table):
    """Počet řádků jen z hlavního souboru (bez -wal), beze změny čehokoli."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


class CrashSafetyTest(TempDirTest):
    """Přerušení simulovaná odděleným procesem (os._exit) v dočasném adresáři."""

    def crash(self, point):
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        return subprocess.run([sys.executable, "-c", CHILD, point, str(self.db), HOST], env=env,
                              capture_output=True, text=True, timeout=60).returncode

    def test_crash_during_initialisation_publishes_nothing(self):
        self.assertEqual(self.crash("commit"), 70)
        self.assertFalse(self.db.exists())
        leftovers = listing(self.folder)
        self.assertTrue(leftovers and all(name.startswith(".history.sqlite3.") for name in leftovers))
        self.create()  # nové vytvoření projde, cizí (nedokončené) zbytky nechá být
        with history.open_writer(self.db, "test", HOST):
            pass
        self.assertEqual(listing(self.folder), sorted(leftovers + ["history.lock", "history.sqlite3"]))

    def test_crash_after_publication_leaves_alias_that_writer_removes(self):
        self.assertEqual(self.crash("link"), 71)
        alias = [name for name in listing(self.folder) if name.endswith(".creating")]
        self.assertEqual(len(alias), 1)
        self.assertEqual(os.stat(self.folder / alias[0]).st_ino, os.stat(self.db).st_ino)
        with history.open_reader(self.db):  # čtenář nic nemaže
            pass
        self.assertIn(alias[0], listing(self.folder))
        with history.open_writer(self.db, "test", HOST) as database:
            self.assertEqual(database.connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        self.assertEqual(listing(self.folder), ["history.lock", "history.sqlite3"])

    def test_alias_with_own_companion_files_is_refused(self):
        self.create()
        alias = self.folder / ".history.sqlite3.ruka.creating"
        os.link(self.db, alias)
        Path(f"{alias}-wal").write_bytes(b"cizi wal")
        self.assertRefused("alias", history.open_writer, self.db, "test", HOST)
        self.assertTrue(alias.exists() and Path(f"{alias}-wal").read_bytes() == b"cizi wal")
        self.assertTrue(lock_is_free(self.db))

    def test_refused_newer_schema_in_wal_is_not_checkpointed(self):
        self.create()
        self.assertEqual(self.crash("newer-wal"), 73)
        wal = Path(f"{self.db}-wal")
        before = (digest(self.db), wal.read_bytes())
        self.assertRefused("schema", history.open_writer, self.db, "test", HOST)
        self.assertRefused("schema", history.open_reader, self.db)
        self.assertEqual((digest(self.db), wal.read_bytes()), before)  # odmítnutí nic nezapsalo ani nezapsalo checkpointem
        self.assertTrue(lock_is_free(self.db))

    def test_unclean_writer_shutdown_keeps_committed_wal(self):
        self.create()
        self.assertEqual(self.crash("writer"), 72)
        wal = Path(f"{self.db}-wal")
        self.assertTrue(wal.exists() and wal.stat().st_size > 0)
        self.assertEqual(immutable_count(self.db, "run"), 0)  # data jsou jen ve -wal
        self.assertTrue(lock_is_free(self.db))  # zámek zanikl s procesem
        with history.open_reader(self.db) as reader:
            self.assertEqual(reader.connection.execute("SELECT count(*) FROM run").fetchone(), (1,))
        with history.open_writer(self.db, "test", HOST) as database:
            self.assertEqual(database.connection.execute("SELECT count(*) FROM run").fetchone(), (1,))
        self.assertFalse(wal.exists())
        self.assertEqual(immutable_count(self.db, "run"), 1)  # po čistém zavření v hlavním souboru


class DurabilityTest(TempDirTest):
    def test_file_is_synced_before_and_directory_after_publication(self):
        calls = []
        real_link = os.link
        with mock.patch.object(history, "_fsync", side_effect=lambda path: calls.append(("fsync", Path(path)))), \
                mock.patch.object(history.os, "link", side_effect=lambda s, t: (calls.append(("link", Path(t))), real_link(s, t))):
            self.create()
        self.assertEqual([kind for kind, _ in calls], ["fsync", "link", "fsync"])
        self.assertTrue(calls[0][1].name.endswith(".creating"))  # dočasný soubor před zveřejněním
        self.assertEqual(calls[1][1], self.db)
        self.assertEqual(calls[2][1], self.folder)  # adresář s novým jménem

    def test_published_file_is_usable_alone(self):
        self.create()
        alone = self.root / "alone"
        alone.mkdir()
        shutil.copyfile(self.db, alone / "history.sqlite3")  # jen hlavní soubor, žádné -wal ani -shm
        self.assertEqual(immutable_count(alone / "history.sqlite3", "meta"), 3)
        with history.open_writer(alone / "history.sqlite3", "test", HOST) as database:
            self.assertEqual(database.connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))

    def test_publication_refused_when_companion_files_remain(self):
        real_build = history._build

        def build_leaving_wal(path, *args):
            real_build(path, *args)
            Path(f"{path}-wal").write_bytes(b"zbytek")  # jako by zavření nesmazalo -wal

        with mock.patch.object(history, "_build", build_leaving_wal):
            self.assertRefused("create", self.create)
        self.assertEqual(listing(self.folder), [])


class VerificationTest(TempDirTest):
    def test_lock_is_released_after_identity_failure(self):
        self.create()
        self.assertRefused("role", history.open_writer, self.db, "development", HOST)
        self.assertRefused("host", history.open_writer, self.db, "test", OTHER_HOST)
        self.assertTrue(lock_is_free(self.db))

    def test_newer_schema_refusal_leaves_file_untouched(self):
        self.create()
        newer = self.copy()
        change(newer, "PRAGMA user_version = 7")
        before = (digest(newer), listing(self.folder))
        self.assertRefused("schema", history.open_writer, newer, "test", HOST)
        self.assertRefused("schema", history.open_reader, newer)
        self.assertEqual((digest(newer), listing(self.folder)), before)  # žádná migrace ani pomocné soubory

    def test_statistics_from_optimize_keep_schema_valid(self):
        self.create()
        with history.open_writer(self.db, "test", HOST) as database:
            database.connection.execute("ANALYZE")  # sqlite_stat1 není součást návrhu
        with history.open_writer(self.db, "test", HOST):
            pass

    def test_foreign_key_check_belongs_to_snapshot_verification(self):
        # Otevření dělá jen quick_check; porušení cizích klíčů (možné jen zásahem
        # mimo zapisovač, který má foreign_keys = ON) najde kontrola snímku (9.2).
        self.create()
        tampered = self.copy()
        change(tampered, "INSERT INTO sample (metric_id, ts, value) VALUES (99, 1, 1.0)")
        with history.open_writer(tampered, "test", HOST) as database:
            self.assertEqual(len(database.connection.execute("PRAGMA foreign_key_check").fetchall()), 1)


# --- M3.1b: zápis ------------------------------------------------------------------

BASE = 1_800_003_600  # po vytvoření databáze (done_until '5m' = 1_800_000_000), násobek 600 i 3600
VALIDITY = 30.0


def at(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def result(check_id, state, value=None, unit=None, ts=BASE, cycle=None, details=None, simulated=False, summary=None):
    """Výsledek kontroly jako od kolektoru; `cycle` = monotónní čas měření (totožnost měření)."""
    cycle = ts if cycle is None else cycle
    return CheckResult(check_id, state, summary or f"{check_id} {state.value}", at(ts), float(cycle) + VALIDITY,
                       simulated=simulated, measurement=None if value is None else Measurement(value, unit),
                       details=details or {})


def benchmark():
    """Modely pravidel M2 z tools/sqlite_history_benchmark.py (ChangeRules, TimeRules)."""
    spec = importlib.util.spec_from_file_location("m2_benchmark", REPO / "tools" / "sqlite_history_benchmark.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FlakyCommit:
    """Spojení, kterému COMMIT proběhne, ale volající dostane chybu (nejistý commit)."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, *args):
        cursor = self._connection.execute(sql, *args)
        if sql == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error (simulace po úspěšném commitu)")
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)


class Passthrough:
    """Obal spojení pro simulace chyb v testech; sám nic nemění, podtřídy přepíší execute()."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, *args):
        return self._connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class RecorderTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.create()
        self.database = history.open_writer(self.db, "test", HOST)
        self.recorder = HistoryRecorder(self.database)
        self.run_id = self.recorder.start_run("test", 10, at(BASE - 60))

    def tearDown(self):
        self.database.close()
        super().tearDown()

    def write(self, *cycles, quality="synced", events=()):
        batch = self.recorder.begin_batch()
        for cycle in cycles:
            self.recorder.add_results(batch, cycle, quality)
        for event in events:
            self.recorder.add_event(batch, event)
        return self.recorder.write(batch)

    def query(self, sql, params=()):
        """Čtení přes samostatné spojení jen pro čtení (jako budoucí web)."""
        with history.open_reader(self.db) as reader:
            return reader.connection.execute(sql, params).fetchall()

    def samples(self, check_id=None):
        where, params = ("WHERE m.check_id = ?", (check_id,)) if check_id else ("", ())
        return self.query(f"SELECT m.check_id, m.subject, s.ts, s.value FROM sample s JOIN metric m ON m.id = s.metric_id "
                          f"{where} ORDER BY s.ts, m.check_id, m.subject", params)

    def changes(self, check_id):
        return self.query("SELECT ts, from_state, to_state, details FROM check_change WHERE check_id = ? ORDER BY id",
                          (check_id,))

    def events(self, kind):
        return self.query("SELECT ts, severity, check_id, data FROM event WHERE kind = ? ORDER BY id", (kind,))


class WriterContractTest(RecorderTest):
    def test_sql_matches_documented_blocks(self):
        for name, value in (("insert-sample", history.INSERT_SAMPLE_SQL), ("sample-conflict", history.SAMPLE_CONFLICT_SQL),
                            ("batch-guard", history.BATCH_GUARD_SQL), ("run-last-write", history.RUN_LAST_WRITE_SQL),
                            ("close-unclean-runs", history.CLOSE_UNCLEAN_RUNS_SQL),
                            ("high-water", history.HIGH_WATER_SQL), ("insert-change", history.INSERT_CHANGE_SQL),
                            ("progress-5m", history.PROGRESS_5M_SQL), ("aggregate-5m", history.AGGREGATE_5M_SQL),
                            ("progress-1h", history.PROGRESS_1H_SQL), ("rollup-1h", history.ROLLUP_1H_SQL),
                            ("oldest-sample", history.OLDEST_SAMPLE_SQL), ("retention-sample", history.RETENTION_SAMPLE_SQL),
                            ("oldest-5m", history.OLDEST_5M_SQL), ("retention-5m", history.RETENTION_5M_SQL),
                            ("oldest-1h", history.OLDEST_1H_SQL), ("retention-1h", history.RETENTION_1H_SQL),
                            ("oldest-change", history.OLDEST_CHANGE_SQL), ("retention-change", history.RETENTION_CHANGE_SQL),
                            ("oldest-event", history.OLDEST_EVENT_SQL), ("retention-event", history.RETENTION_EVENT_SQL),
                            ("retention-check-sample", history.RETENTION_CHECK_SAMPLE_SQL),
                            ("retention-check-5m", history.RETENTION_CHECK_5M_SQL)):
            with self.subTest(block=name):
                self.assertEqual(value, doc_block(name))

    def test_catalog_has_only_collector_series(self):
        self.assertEqual({check_id: (spec.unit, spec.subject_key) for check_id, spec in history.SERIES.items()}, {
            CPU_USAGE_ID: (PERCENT, None), CPU_TEMP_ID: (CELSIUS, None), MEMORY_ID: (PERCENT, None),
            DISK_ROOT_ID: (PERCENT, None), RX_RATE_ID: (BYTES_PER_SECOND, INTERFACE),
            TX_RATE_ID: (BYTES_PER_SECOND, INTERFACE),
            # Ventilátor CPU (M4-1): jen naměřené otáčky; stav z požadavku řízení
            # žádnou číselnou řadu nemá.
            CPU_FAN_ID: (RPM, None)})

    def test_start_run_records_run_and_event(self):
        info = history.run_info(self.database.connection, self.run_id)
        self.assertEqual((info.started_utc, info.stopped_utc, info.last_batch, info.last_write_utc), (BASE - 60, None, 0, None))
        self.assertEqual(len(self.events("process.start")), 1)

    def test_recorder_needs_writer_and_started_run(self):
        with history.open_reader(self.db) as reader:
            with self.assertRaises(HistoryError):
                HistoryRecorder(reader)  # čtecí spojení nikdy nezapisuje
        with self.assertRaises(HistoryError):
            HistoryRecorder(None)
        fresh = HistoryRecorder(self.database)
        with self.assertRaises(HistoryError):
            fresh.begin_batch()  # bez start_run
        with self.assertRaises(HistoryError):
            self.recorder.start_run("test", 10, at(BASE))  # druhý start téhož zapisovače
        self.assertRefused("locked", history.open_writer, self.db, "test", HOST)  # jediný zapisovač

    def test_foreign_database_never_gets_a_recorder(self):
        other = self.folder / "other.sqlite3"
        history.create_database(other, "test", OTHER_HOST, now_utc=CREATED)
        self.assertRefused("host", history.open_writer, other, "test", HOST)  # databáze jiného stroje
        self.assertRefused("role", history.open_writer, other, "development", OTHER_HOST)
        foreign = self.folder / "foreign.sqlite3"
        change(foreign, "CREATE TABLE sample (ts INTEGER)")  # cizí SQLite soubor se stejným názvem tabulky
        self.assertRefused("foreign", history.open_writer, foreign, "test", HOST)
        with history.open_reader(other) as reader:  # odmítnutí nic nezapsalo
            self.assertEqual(reader.connection.execute("SELECT count(*) FROM run").fetchall(), [(0,)])


class SampleContractTest(RecorderTest):
    def test_zero_value_unknown_and_missing_measurement(self):
        self.write([result(CPU_USAGE_ID, State.INFO, 0.0, PERCENT), result(CPU_TEMP_ID, State.UNKNOWN),
                    result(MEMORY_ID, State.INFO, 24.5, PERCENT)])  # disk.root chybí
        self.assertEqual(self.samples(), [(CPU_TEMP_ID, "", BASE, None), (CPU_USAGE_ID, "", BASE, 0.0),
                                          (MEMORY_ID, "", BASE, 24.5)])
        self.assertEqual(self.query("SELECT typeof(value) FROM sample s JOIN metric m ON m.id = s.metric_id "
                                    "WHERE m.check_id = ?", (CPU_USAGE_ID,)), [("real",)])
        self.assertEqual(self.query("SELECT check_id FROM metric WHERE check_id = ?", (DISK_ROOT_ID,)), [])

    def test_na_creates_change_but_no_sample(self):
        self.write([result(CPU_TEMP_ID, State.NA), result("service.animedb", State.NA)])
        self.assertEqual(self.samples(), [])
        self.assertEqual(self.changes("service.animedb"), [(BASE, None, "N/A", None)])

    def test_animedb_health_stores_changes_only(self):
        # SERVICES-1: healthcheck AnimeDB nemá číselnou řadu. Do historie jdou jen
        # skutečné změny stavu a kategorických details; trvající výpadek řádky nepřidává.
        down = {"health": "down", "cause": "refused"}
        up = {"health": "ok"}
        sequence = [(State.OK, up), (State.UNKNOWN, down), (State.UNKNOWN, down), (State.ERROR, down),
                    (State.ERROR, down), (State.ERROR, up), (State.OK, up)]
        self.write(*[[result("service.animedb", state, ts=BASE + 10 * index, details=details)]
                     for index, (state, details) in enumerate(sequence)])
        self.assertEqual(self.samples(), [])
        self.assertEqual([(from_state, to_state) for _, from_state, to_state, _ in self.changes("service.animedb")],
                         [(None, "OK"), ("OK", "UNKNOWN"), ("UNKNOWN", "ERROR"), ("ERROR", "ERROR"), ("ERROR", "OK")])
        self.assertEqual(sum(self.recorder.rejected.values()), 0)

    def test_nas_reachability_stores_changes_only(self):
        # SERVICES-2: kontrola NAS je stejný druh výsledku jako healthcheck AnimeDB:
        # bez číselné řady, jen změny stavu a kategorických details, schéma beze změny.
        down = {"health": "down", "cause": "refused"}
        timeout = {"health": "down", "cause": "timeout"}
        up = {"health": "ok"}
        sequence = [(State.OK, up), (State.UNKNOWN, down), (State.UNKNOWN, timeout), (State.ERROR, down),
                    (State.ERROR, down), (State.ERROR, up), (State.OK, up), (State.OK, up)]
        self.write(*[[result("service.nas", state, ts=BASE + 10 * index, details=details)]
                     for index, (state, details) in enumerate(sequence)])
        self.assertEqual(self.samples(), [])
        self.assertEqual([(from_state, to_state) for _, from_state, to_state, _ in self.changes("service.nas")],
                         [(None, "OK"), ("OK", "UNKNOWN"), ("UNKNOWN", "UNKNOWN"), ("UNKNOWN", "ERROR"),
                          ("ERROR", "ERROR"), ("ERROR", "OK")])
        self.assertEqual(sum(self.recorder.rejected.values()), 0)

    def test_simulated_results_are_never_stored(self):
        outcome = self.write([result(CPU_TEMP_ID, State.WARNING, 80.0, CELSIUS, simulated=True),
                              result(LINK_ID, State.INFO, details={INTERFACE: "eth0", LINK: "UP"}, simulated=True)])
        self.assertEqual((self.samples(), self.changes(CPU_TEMP_ID), self.changes(LINK_ID)), ([], [], []))
        self.assertEqual(self.recorder.rejected["simulated"], 2)
        self.assertEqual(len(self.events("history.simulated_rejected")), 1)
        self.assertEqual(outcome.samples, 0)

    def test_unknown_series_and_wrong_unit_create_no_series(self):
        self.write([result("custom.metric", State.INFO, 5.0, "x"), result(CPU_TEMP_ID, State.INFO, 320.0, "K")])
        self.write([result(CPU_TEMP_ID, State.INFO, 321.0, "K", ts=BASE + 10)])
        self.assertEqual(self.query("SELECT check_id FROM metric"), [])
        self.assertEqual((self.recorder.rejected["unknown_series"], self.recorder.rejected["unit_mismatch"]), (1, 2))
        self.assertEqual(len(self.events("history.unit_mismatch")), 1)  # jen jednou za běh
        self.assertEqual(self.changes("custom.metric"), [(BASE, None, "INFO", None)])  # stav se eviduje i tak

    def test_stored_series_with_other_unit_is_not_mixed(self):
        # Řada z jiné verze katalogu (jiná jednotka v databázi): vzorek se nezapíše ani nepřevede.
        self.database.connection.execute("INSERT INTO metric (check_id, subject, unit, created_utc) VALUES (?, '', 'K', ?)",
                                         (CPU_TEMP_ID, CREATED))
        outcome = self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        self.assertEqual((outcome.samples, self.samples(), self.recorder.rejected["unit_mismatch"]), (0, [], 1))
        self.assertEqual(len(self.events("history.unit_mismatch")), 1)
        self.assertEqual(self.query("SELECT unit FROM metric"), [("K",)])

    def test_invalid_number_is_refused(self):
        broken = result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)
        object.__setattr__(broken.measurement, "value", float("nan"))  # poškozený objekt mimo kontrolu core
        self.write([broken])
        self.assertEqual((self.samples(), self.recorder.rejected["invalid_value"]), ([], 1))

    def test_network_rates_keep_interface_identity(self):
        self.write([result(RX_RATE_ID, State.INFO, 1200.0, BYTES_PER_SECOND, details={INTERFACE: "eth0"}),
                    result(TX_RATE_ID, State.UNKNOWN, details={INTERFACE: "eth0"})])
        self.write([result(RX_RATE_ID, State.INFO, 300.0, BYTES_PER_SECOND, ts=BASE + 10, details={INTERFACE: "wlan0"}),
                    result(RX_RATE_ID, State.UNKNOWN, ts=BASE + 10, cycle=BASE + 10.5)])  # bez rozhraní
        self.assertEqual(self.query("SELECT check_id, subject, unit FROM metric ORDER BY id"),
                         [(RX_RATE_ID, "eth0", "B/s"), (TX_RATE_ID, "eth0", "B/s"), (RX_RATE_ID, "wlan0", "B/s")])
        self.assertEqual(self.samples(RX_RATE_ID), [(RX_RATE_ID, "eth0", BASE, 1200.0), (RX_RATE_ID, "wlan0", BASE + 10, 300.0)])
        self.assertEqual(self.samples(TX_RATE_ID), [(TX_RATE_ID, "eth0", BASE, None)])
        self.assertEqual(self.recorder.rejected["no_subject"], 1)

    def test_samples_before_aggregation_watermark_are_late(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=1_799_990_000)])
        self.assertEqual((self.samples(), self.recorder.rejected["late"]), ([], 1))


class ChangeContractTest(RecorderTest):
    def test_each_state_transition_is_one_row_with_baseline(self):
        sequence = ["INFO", "WARNING", "ERROR", "CRITICAL", "INFO", "UNKNOWN", "INFO", "INFO"]
        for k, state in enumerate(sequence):
            value = None if state == "UNKNOWN" else 50.0 + k
            self.write([result(CPU_TEMP_ID, State(state), value, None if value is None else CELSIUS, ts=BASE + 10 * k)])
        self.assertEqual([row[1:3] for row in self.changes(CPU_TEMP_ID)],
                         [(None, "INFO"), ("INFO", "WARNING"), ("WARNING", "ERROR"), ("ERROR", "CRITICAL"),
                          ("CRITICAL", "INFO"), ("INFO", "UNKNOWN"), ("UNKNOWN", "INFO")])  # poslední INFO beze změny
        self.assertEqual([row[0] for row in self.changes(CPU_TEMP_ID)], [BASE + 10 * k for k in range(7)])

    def test_link_changes_are_stored_while_state_stays_info(self):
        for k, link in enumerate(["UP", "DOWN", "DOWN", "UP"]):
            self.write([result(LINK_ID, State.INFO, ts=BASE + 10 * k, details={INTERFACE: "eth0", LINK: link})])
        rows = self.changes(LINK_ID)
        self.assertEqual([(row[0], row[1], row[2], json.loads(row[3])[LINK]) for row in rows],
                         [(BASE, None, "INFO", "UP"), (BASE + 10, "INFO", "INFO", "DOWN"), (BASE + 30, "INFO", "INFO", "UP")])

    def test_repeated_identical_state_creates_no_rows(self):
        for k in range(5):  # hodnota i text se mění, stav ne
            self.write([result(CPU_TEMP_ID, State.INFO, 50.0 + k, CELSIUS, ts=BASE + 10 * k, summary=f"{50 + k} °C")])
        self.assertEqual(len(self.changes(CPU_TEMP_ID)), 1)
        self.assertEqual(len(self.samples(CPU_TEMP_ID)), 5)

    def test_same_details_in_other_key_order_are_no_change(self):
        self.write([result(LINK_ID, State.INFO, details={INTERFACE: "eth0", LINK: "UP"})])
        self.write([result(LINK_ID, State.INFO, ts=BASE + 10, details={LINK: "UP", INTERFACE: "eth0"})])
        self.assertEqual(len(self.changes(LINK_ID)), 1)

    def test_info_critical_flapping_keeps_every_transition(self):
        # Scénář A ze selfcheck_r4 (benchmark M2): 30 min, měření po 5 s, změna každých 10 s.
        cycles = [[result(CPU_TEMP_ID, State.INFO, 40.0, CELSIUS, ts=BASE)]]
        for k in range(1, 361):
            state = State.CRITICAL if ((k + 1) // 2) % 2 else State.INFO
            cycles.append([result(CPU_TEMP_ID, state, 90.0 if state is State.CRITICAL else 40.0, CELSIUS, ts=BASE + 5 * k)])
        for start in range(0, len(cycles), 12):  # dávka po minutě
            self.write(*cycles[start:start + 12])
        rows = self.changes(CPU_TEMP_ID)
        entries = [BASE + 5 * k for k in range(1, 361, 4)]
        exits = [BASE + 5 * k for k in range(3, 361, 4)]
        self.assertEqual(len(rows), 181)
        self.assertEqual([row[0] for row in rows if row[1:3] == ("INFO", "CRITICAL")], entries)
        self.assertEqual([row[0] for row in rows if row[1:3] == ("CRITICAL", "INFO")], exits)
        episodes = self.query(doc_block("state-episodes"), {"check_id": CPU_TEMP_ID, "state": "CRITICAL",
                                                             "start": BASE, "end": BASE + 3600})
        self.assertEqual(episodes, [(start, end, 10, 0) for start, end in zip(entries, exits)])
        self.assertEqual(self.query(doc_block("transition-rate"), {"check_id": CPU_TEMP_ID, "bucket_s": 600,
                                                                    "start": BASE, "end": BASE + 3600}),
                         [(BASE, 60, 60), (BASE + 600, 60, 60), (BASE + 1200, 60, 60)])
        # Shoda s modelem ChangeRules z benchmarku M2.
        model = benchmark().ChangeRules()
        model.submit(BASE, CPU_TEMP_ID, "INFO", value=40.0, unit=CELSIUS)
        for k in range(1, 361):
            state = "CRITICAL" if ((k + 1) // 2) % 2 else "INFO"
            model.submit(BASE + 5 * k, CPU_TEMP_ID, state, value=90.0 if state == "CRITICAL" else 40.0, unit=CELSIUS)
        stored = self.query("SELECT ts, from_state, to_state, value, unit FROM check_change WHERE check_id = ? ORDER BY id",
                            (CPU_TEMP_ID,))
        self.assertEqual(stored, [(row["ts"], row["from_state"], row["to_state"], row["value"], row["unit"])
                                  for row in model.pending])

    def test_two_changes_in_one_second_keep_order(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE)])
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.CRITICAL, 90.0, CELSIUS, ts=BASE + 10.2, cycle=BASE + 10.2)])
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE + 10.9, cycle=BASE + 10.9)])
        self.recorder.write(batch)
        self.assertEqual([row[:3] for row in self.changes(CPU_TEMP_ID)],
                         [(BASE, None, "INFO"), (BASE + 10, "INFO", "CRITICAL"), (BASE + 10, "CRITICAL", "INFO")])
        self.assertEqual(self.query(doc_block("state-episodes"), {"check_id": CPU_TEMP_ID, "state": "CRITICAL",
                                                                   "start": BASE, "end": BASE + 3600}),
                         [(BASE + 10, BASE + 10, 0, 0)])
        self.assertEqual(self.samples(CPU_TEMP_ID), [(CPU_TEMP_ID, "", BASE, 50.0), (CPU_TEMP_ID, "", BASE + 10, 90.0)])
        self.assertEqual(self.recorder.rejected["same_second"], 1)  # druhé měření v sekundě = ztráta vzorku

    def test_events_are_stored_and_validated(self):
        self.write(events=[Event(BASE, "synced", "flapping.start", "WARNING", "kmitání", CPU_TEMP_ID, {"recent": 6})])
        self.assertEqual(self.events("flapping.start"), [(BASE, "WARNING", CPU_TEMP_ID, '{"recent":6}')])
        self.assertEqual(self.changes(CPU_TEMP_ID), [])  # událost FLAPPING není přechod
        for arguments in ((BASE, "synced", "x", "FATAL", "zpráva"), (BASE, "guess", "x", "INFO", "zpráva"),
                          (0, "synced", "x", "INFO", "zpráva"), (BASE, "synced", "", "INFO", "zpráva")):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                Event(*arguments)
        with self.assertRaises(TypeError):
            Event(BASE, "synced", "x", "INFO", "zpráva", data={"set": {1, 2}})


class BatchSafetyTest(RecorderTest):
    def test_same_measurement_twice_is_duplicate_not_loss(self):
        cycle = [result(CPU_TEMP_ID, State.WARNING, 72.0, CELSIUS)]
        self.write(cycle, cycle)
        self.assertEqual(len(self.samples(CPU_TEMP_ID)), 1)
        self.assertEqual(len(self.changes(CPU_TEMP_ID)), 1)
        self.assertEqual((self.recorder.duplicates, sum(self.recorder.rejected.values())), (1, 0))

    def test_replayed_batch_is_not_written_twice(self):
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        first = self.recorder.write(batch)
        again = self.recorder.write(batch)
        self.assertEqual((first.already_written, first.samples, again.already_written, again.samples), (False, 1, True, 0))
        self.assertEqual((len(self.samples()), len(self.changes(CPU_TEMP_ID))), (1, 1))

    def test_uncertain_commit_then_retry_writes_once(self):
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.CRITICAL, 91.0, CELSIUS)])
        real = self.database.connection
        self.database.connection = FlakyCommit(real)
        try:
            self.assertRefused("write", self.recorder.write, batch)
        finally:
            self.database.connection = real
        retry = self.recorder.write(batch)
        self.assertTrue(retry.already_written)
        self.assertEqual((len(self.samples()), len(self.changes(CPU_TEMP_ID))), (1, 1))
        self.assertEqual(history.run_info(real, self.run_id).last_batch, 1)
        self.recorder.begin_batch()  # dávka uzavřená, další jde vytvořit

    def test_skipped_batch_number_is_refused(self):
        # Ručně vytvořenou dávku (i s vynechaným číslem) zapisovač odmítne ještě před transakcí;
        # vynechané číslo u řádné dávky hlídá batch-guard (test_guard_refuses_open_batch_out_of_order).
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        skipped = history.Batch(self.recorder.last_batch + 2)
        skipped.samples.append(PendingSample(CPU_TEMP_ID, "", CELSIUS, BASE + 20, "synced", 51.0))
        self.assertRefused("batch", self.recorder.write, skipped)
        self.assertFalse(skipped.sealed)
        self.assertEqual(len(self.samples()), 1)
        self.assertEqual(history.run_info(self.database.connection, self.run_id).last_batch, 1)
        # Odmítnutá dávka mimo pořadí nespotřebuje souhrn ztrát; nese ho až řádná dávka.
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 52.0, CELSIUS, cycle=BASE + 0.5)])  # same_second
        self.assertRefused("batch", self.recorder.write, history.Batch(batch.number + 1))
        self.recorder.write(batch)
        self.assertEqual(len(self.events("history.rejected")), 1)

    def test_conflicting_sample_is_reported_and_not_overwritten(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        batch = self.recorder.begin_batch()  # obejde pravidla přijetí jako chyba programu nebo obnovy
        batch.samples += [PendingSample(CPU_TEMP_ID, "", CELSIUS, BASE, "synced", 50.0),
                          PendingSample(CPU_TEMP_ID, "", CELSIUS, BASE, "synced", 99.0)]
        outcome = self.recorder.write(batch)
        self.assertEqual((outcome.samples, outcome.duplicates, len(outcome.conflicts)), (0, 1, 1))
        self.assertEqual(outcome.conflicts[0].stored, 50.0)
        self.assertEqual(self.samples(CPU_TEMP_ID), [(CPU_TEMP_ID, "", BASE, 50.0)])
        conflict = self.events("history.sample_conflict")
        self.assertEqual(len(conflict), 1)
        self.assertEqual((conflict[0][1], json.loads(conflict[0][3])["rejected"]), ("ERROR", 99.0))

    def test_failure_mid_batch_rolls_back_everything(self):
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.CRITICAL, 90.0, CELSIUS)])
        self.recorder.add_event(batch, Event(BASE, "synced", "test.event", "INFO", "událost"))
        with mock.patch.object(history, "INSERT_EVENT_SQL", "INSERT INTO neexistuje VALUES (1)"):
            self.assertRefused("write", self.recorder.write, batch)  # selže až po vzorcích a změnách
        self.assertEqual((self.samples(), self.changes(CPU_TEMP_ID), self.events("test.event")), ([], [], []))
        self.assertEqual(history.run_info(self.database.connection, self.run_id).last_batch, 0)
        self.assertEqual(self.recorder.last_batch, 0)
        outcome = self.recorder.write(batch)  # stejná dávka znovu
        self.assertEqual((outcome.already_written, outcome.samples, outcome.changes), (False, 1, 1))

    def test_sent_batch_is_sealed(self):
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        with mock.patch.object(history, "INSERT_EVENT_SQL", "INSERT INTO neexistuje VALUES (1)"):
            self.assertRefused("write", self.recorder.write, batch)
        # Po neúspěchu se dávka jen opakuje beze změny; nové výsledky patří do další dávky.
        self.assertRefused("batch", self.recorder.add_results, batch, [result(CPU_USAGE_ID, State.INFO, 5.0, PERCENT)])
        self.assertRefused("batch", self.recorder.add_event, batch, Event(BASE, "synced", "x", "INFO", "zpráva"))
        self.recorder.write(batch)
        self.assertRefused("batch", self.recorder.add_results, batch, [])

    def test_foreign_batch_cannot_displace_open_batch(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.CRITICAL, 95.0, CELSIUS, ts=BASE + 10)])
        # Ručně vytvořená dávka se stejným číslem by jinak otevřenou dávku tiše „nahradila“.
        self.assertRefused("batch", self.recorder.write, history.Batch(batch.number))
        outcome = self.recorder.write(batch)
        self.assertEqual((outcome.already_written, outcome.changes), (False, 1))
        self.assertEqual([row[2] for row in self.changes(CPU_TEMP_ID)], ["INFO", "CRITICAL"])

    def test_guard_refuses_open_batch_out_of_order(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.WARNING, 75.0, CELSIUS, ts=BASE + 10)])
        conn = self.database.connection
        conn.execute("UPDATE run SET last_batch = 0 WHERE id = ?", (self.run_id,))  # cizí zásah do evidence
        self.assertRefused("batch", self.recorder.write, batch)
        self.assertEqual((len(self.samples()), len(self.changes(CPU_TEMP_ID))), (1, 1))
        conn.execute("UPDATE run SET last_batch = 1 WHERE id = ?", (self.run_id,))
        self.assertEqual(self.recorder.write(batch).samples, 1)  # zapečetěná dávka beze změny projde

    def test_sealed_batch_changed_directly_is_refused(self):
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        real = self.database.connection
        self.database.connection = FlakyCommit(real)
        try:
            self.assertRefused("write", self.recorder.write, batch)  # commit proběhl, volající to neví
        finally:
            self.database.connection = real
        late = PendingSample(CPU_USAGE_ID, "", PERCENT, BASE + 10, "synced", 7.0)
        batch.samples.append(late)  # obejde add_results: opakování by ho tiše zahodilo jako already_written
        self.assertRefused("batch", self.recorder.write, batch)
        batch.samples.remove(late)
        self.assertTrue(self.recorder.write(batch).already_written)

    def test_uncertain_commit_keeps_counters_of_committed_attempt(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        conn = self.database.connection
        conn.execute("INSERT INTO sample (metric_id, ts, value) SELECT id, ?, 1.0 FROM metric", (BASE + 10,))
        conn.execute("INSERT INTO metric (check_id, subject, unit, created_utc) VALUES (?, '', 'K', ?)", (MEMORY_ID, CREATED))
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 2.0, CELSIUS, ts=BASE + 10),
                                          result(MEMORY_ID, State.INFO, 20.0, PERCENT, ts=BASE + 10)])
        self.database.connection = FlakyCommit(conn)
        try:
            self.assertRefused("write", self.recorder.write, batch)
        finally:
            self.database.connection = conn
        self.assertTrue(self.recorder.write(batch).already_written)
        self.assertEqual((self.recorder.conflicts, self.recorder.rejected["unit_mismatch"]), (1, 1))
        self.assertEqual(len(self.events("history.sample_conflict")), 1)
        self.recorder.finish_run(at(BASE + 20))  # ztráta z potvrzeného pokusu se dostane do souhrnu
        self.assertEqual(json.loads(self.events("history.rejected")[-1][3])["reasons"],
                         {"unit_mismatch": {"count": 1, "from": BASE + 10, "to": BASE + 10}})

    def test_unexpected_exception_rolls_back_transaction(self):
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.CRITICAL, 90.0, CELSIUS)])
        with mock.patch.object(history.Change, "row", side_effect=KeyboardInterrupt):  # např. Ctrl+C uprostřed
            with self.assertRaises(KeyboardInterrupt):
                self.recorder.write(batch)
        self.assertFalse(self.database.connection.in_transaction)  # zámek zápisu uvolněn, nic rozepsaného
        self.assertEqual((self.samples(), self.changes(CPU_TEMP_ID)), ([], []))
        self.assertEqual(self.recorder.write(batch).samples, 1)

    def test_unexpected_exception_rolls_back_start_and_finish(self):
        self.database.close()
        self.database = history.open_writer(self.db, "test", HOST)
        recorder = HistoryRecorder(self.database)
        with mock.patch.object(history.Event, "row", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                recorder.start_run("test", 10, at(BASE + 100))
        self.assertFalse(self.database.connection.in_transaction)
        self.assertEqual(self.query("SELECT count(*), count(stopped_utc) FROM run"), [(1, 0)])  # nic nového ani uzavřeného
        run_id = recorder.start_run("test", 10, at(BASE + 100))
        with mock.patch.object(history.Event, "row", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                recorder.finish_run(at(BASE + 200))
        self.assertFalse(self.database.connection.in_transaction)
        self.assertIsNone(history.run_info(self.database.connection, run_id).stopped_utc)
        self.assertTrue(recorder.finish_run(at(BASE + 200)))

    def test_pending_batch_blocks_new_batch_and_finish(self):
        self.recorder.begin_batch()
        self.assertRefused("batch", self.recorder.begin_batch)
        self.assertRefused("batch", self.recorder.finish_run, at(BASE))


class LossSummaryTest(RecorderTest):
    def summaries(self):
        return [(ts, json.loads(data)) for ts, _, _, data in self.events("history.rejected")]

    def test_first_loss_is_summarized_then_at_most_hourly(self):
        cycle = [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE), result(CPU_USAGE_ID, State.INFO, 5.0, PERCENT,
                                                                                   ts=BASE)]
        self.write(cycle, [result(CPU_TEMP_ID, State.INFO, 51.0, CELSIUS, ts=BASE, cycle=BASE + 0.4)])  # same_second
        self.write([result(CPU_TEMP_ID, State.INFO, 52.0, CELSIUS, ts=BASE + 10)], quality="unsynced")
        self.assertEqual(self.summaries(), [(BASE, {"total": 1, "reasons": {
            "same_second": {"count": 1, "from": BASE, "to": BASE}}})])
        self.write([result(CPU_TEMP_ID, State.INFO, 53.0, CELSIUS, ts=BASE + 3610)])  # hodina od souhrnu
        self.assertEqual(self.summaries()[1:], [(BASE + 3610, {"total": 1, "reasons": {"time_unsynced": {"count": 1}}})])
        self.assertEqual(dict(self.recorder.rejected), {"same_second": 1, "time_unsynced": 1})

    def test_remaining_loss_is_written_on_clean_stop(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE)])
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE + 10, simulated=True)])
        self.write([result(CPU_TEMP_ID, State.INFO, 49.0, CELSIUS, ts=BASE - 5, cycle=BASE + 20)])  # clock_backward
        self.assertEqual(len(self.summaries()), 1)  # druhá ztráta čeká na hodinový odstup
        self.recorder.finish_run(at(BASE + 30))
        self.assertEqual(self.summaries()[1:], [(BASE + 30, {"total": 1, "reasons": {
            "clock_backward": {"count": 1, "from": BASE - 5, "to": BASE - 5}}})])
        self.assertEqual(self.summaries()[0][1], {"total": 1, "reasons": {"simulated": {"count": 1}}})

    def test_resubmitted_measurement_is_not_counted_twice(self):
        unsynced = [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS), result("custom.metric", State.INFO, 1.0, "x")]
        simulated = [result(CPU_USAGE_ID, State.INFO, 5.0, PERCENT, ts=BASE + 10, simulated=True)]
        self.write(unsynced, unsynced, quality="unsynced")  # totéž měření odeslané dvakrát
        self.write(simulated, simulated)
        self.assertEqual(dict(self.recorder.rejected), {"time_unsynced": 1, "unknown_series": 1, "simulated": 1})
        self.assertEqual(self.recorder.duplicates, 3)
        self.assertEqual(len(self.changes(CPU_TEMP_ID)), 1)

    def test_rollback_keeps_summary_for_retry(self):
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)], "unsynced")
        with mock.patch.object(history, "INSERT_EVENT_SQL", "INSERT INTO neexistuje VALUES (1)"):
            self.assertRefused("write", self.recorder.write, batch)
        self.assertEqual(self.summaries(), [])
        self.recorder.write(batch)
        self.assertEqual(self.summaries(), [(BASE, {"total": 1, "reasons": {"time_unsynced": {"count": 1}}})])
        self.recorder.finish_run(at(BASE + 10))
        self.assertEqual(len(self.summaries()), 1)  # nic se nezapočetlo dvakrát

    def test_simulated_result_does_not_move_summary_clock(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS),
                    result(CPU_TEMP_ID, State.INFO, 51.0, CELSIUS, cycle=BASE + 0.4)])  # same_second: první souhrn
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE + 10, cycle=BASE + 10_000, simulated=True)])
        self.assertEqual(len(self.summaries()), 1)  # monotónní čas simulace hodinový odstup neurychlí

    def test_summary_survives_failed_write_exactly_once(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=1_799_990_000)])  # late
        self.assertEqual(len(self.summaries()), 1)
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=1_799_990_010)], quality="derived")
        batch = self.recorder.begin_batch()
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=1_799_990_020,
                                                 cycle=BASE + 3600)])
        real = self.database.connection
        self.database.connection = FlakyCommit(real)
        try:
            self.assertRefused("write", self.recorder.write, batch)
        finally:
            self.database.connection = real
        self.assertTrue(self.recorder.write(batch).already_written)
        self.assertEqual([data["reasons"]["late"]["count"] for _, data in self.summaries()], [1, 2])
        self.recorder.finish_run(at(BASE + 3700))
        self.assertEqual(len(self.summaries()), 2)  # nic dalšího k souhrnu, nic se nezdvojilo


class TimeAndRestartTest(RecorderTest):
    def test_restart_after_unclean_shutdown(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE)])
        self.database.close()  # konec bez finish_run, jako pád procesu
        self.database = history.open_writer(self.db, "test", HOST)
        recorder = HistoryRecorder(self.database)
        second = recorder.start_run("test", 10, at(BASE + 100))
        first = history.run_info(self.database.connection, self.run_id)
        self.assertEqual((first.stopped_utc, first.clean_stop, first.last_batch), (BASE, 0, 1))
        self.assertEqual(recorder.high_water, BASE)
        self.assertEqual(json.loads(self.events("process.unclean_stop")[0][3])["run_id"], self.run_id)
        gap = json.loads(self.events("history.unknown_gap")[0][3])
        self.assertEqual((gap["after"], gap["until"]), (BASE, BASE + 100))
        batch = recorder.begin_batch()
        recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 51.0, CELSIUS, ts=BASE, cycle=900.0)])
        recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 52.0, CELSIUS, ts=BASE + 110, cycle=910.0)])
        recorder.write(batch)
        self.assertEqual(recorder.rejected["before_high_water"], 1)
        self.assertEqual(self.samples(CPU_TEMP_ID), [(CPU_TEMP_ID, "", BASE, 50.0), (CPU_TEMP_ID, "", BASE + 110, 52.0)])
        self.assertEqual(self.query("SELECT run_id, from_state FROM check_change ORDER BY id"),
                         [(self.run_id, None), (second, None)])  # nový běh = nový výchozí řádek
        self.assertTrue(recorder.finish_run(at(BASE + 120)))
        self.assertFalse(recorder.finish_run(at(BASE + 130)))  # opakované ukončení nic nezdvojí
        info = history.run_info(self.database.connection, second)
        self.assertEqual((info.stopped_utc, info.clean_stop), (BASE + 120, 1))
        self.assertEqual(len(self.events("process.stop")), 1)
        self.assertRefused("run", recorder.begin_batch)

    def test_run_without_samples_leaves_gap_from_its_start(self):
        self.database.close()
        self.database = history.open_writer(self.db, "test", HOST)
        HistoryRecorder(self.database).start_run("test", 10, at(BASE + 50))
        gap = json.loads(self.events("history.unknown_gap")[0][3])
        self.assertEqual((gap["after"], gap["until"]), (BASE - 60, BASE + 50))

    def test_gap_after_unsynced_start_has_no_utc_start(self):
        self.database.close()  # setUp: běh bez vzorků se synchronizovaným startem
        self.database = history.open_writer(self.db, "test", HOST)
        HistoryRecorder(self.database).start_run("test", 10, at(BASE + 50), ts_quality="unsynced")
        self.database.close()  # i druhý běh bez vzorků, start s nedůvěryhodným časem
        self.database = history.open_writer(self.db, "test", HOST)
        HistoryRecorder(self.database).start_run("test", 10, at(BASE + 500))
        gaps = [json.loads(row[3]) for row in self.events("history.unknown_gap")]
        self.assertEqual([(gap["after"], gap["until"]) for gap in gaps], [(BASE - 60, None), (None, BASE + 500)])

    def test_time_before_trusted_bound_is_never_marked_synced(self):
        self.database.close()
        self.database = history.open_writer(self.db, "test", HOST)
        recorder = HistoryRecorder(self.database)
        recorder.start_run("test", 10, at(1_700_000_000))  # hodiny RPi bez RTC, volající tvrdí synced
        recorder.finish_run(at(1_700_000_100), ts_quality="derived")
        self.assertEqual(self.query("SELECT kind, ts_quality FROM event WHERE ts < ? ORDER BY id", (history.MIN_TRUSTED_UTC,)),
                         [("process.unclean_stop", "unsynced"), ("history.unknown_gap", "unsynced"),
                          ("process.start", "unsynced"), ("process.stop", "unsynced")])
        for quality in ("synced", "derived"):
            with self.subTest(quality=quality), self.assertRaises(ValueError):
                Event(1_700_000_000, quality, "x", "INFO", "zpráva")
        Event(1_700_000_000, "unsynced", "x", "INFO", "zpráva")

    def test_unsynced_start_does_not_invent_gap_end(self):
        self.database.close()
        self.database = history.open_writer(self.db, "test", HOST)
        HistoryRecorder(self.database).start_run("test", 10, at(BASE + 50), ts_quality="unsynced")
        rows = self.query("SELECT ts_quality, data FROM event WHERE kind = 'history.unknown_gap'")
        self.assertEqual([(quality, json.loads(data)["after"], json.loads(data)["until"]) for quality, data in rows],
                         [("unsynced", BASE - 60, None)])

    def test_clock_step_back_rejects_older_samples_without_overwrite(self):
        for k in range(11):
            self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE + 10 * k, cycle=1000 + 10 * k)])
        for k in range(11, 21):  # hodiny o 3605 s zpět, monotónní čas běží dál
            self.write([result(CPU_TEMP_ID, State.INFO, 60.0, CELSIUS, ts=BASE + 10 * k - 3605, cycle=1000 + 10 * k)])
        self.assertEqual(self.recorder.rejected["clock_backward"], 10)
        self.assertEqual([row[3] for row in self.samples(CPU_TEMP_ID)], [50.0] * 11)
        jumps = self.events("clock.jump")
        self.assertEqual((len(jumps), json.loads(jumps[0][3])["shift_s"]), (1, -3605.0))

    def test_time_quality_controls_samples_not_changes(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE)], quality="derived")
        self.write([result(CPU_TEMP_ID, State.WARNING, 75.0, CELSIUS, ts=BASE + 10)], quality="unsynced")
        self.write([result(CPU_TEMP_ID, State.ERROR, 85.0, CELSIUS, ts=1_700_000_000, cycle=BASE + 20)])  # před 2026: nedůvěryhodné
        self.assertEqual(self.samples(CPU_TEMP_ID), [(CPU_TEMP_ID, "", BASE, 50.0)])
        self.assertEqual(self.recorder.rejected["time_unsynced"], 2)
        self.assertEqual(self.query("SELECT to_state, ts_quality FROM check_change ORDER BY id"),
                         [("INFO", "derived"), ("WARNING", "unsynced"), ("ERROR", "unsynced")])
        batch = self.recorder.begin_batch()
        with self.assertRaises(ValueError):
            self.recorder.add_results(batch, [], "guess")

    def test_gate_matches_benchmark_time_rules(self):
        # Shoda s modelem TimeRules z benchmarku M2, rozhodnutí po rozhodnutí (synchronizovaný čas).
        module = benchmark()
        base = 1_800_000_000

        def decide(rules, gate, mono, utc):
            before = (dict(rules.rejected), rules.duplicates, len(rules.accepted))
            rules.measure("cpu", mono, utc, True)
            if rules.duplicates != before[1]:
                expected = "duplicate"
            elif len(rules.accepted) != before[2]:
                expected = None
            else:
                expected = next(reason for reason, count in rules.rejected.items() if count != before[0].get(reason, 0))
            return expected, gate.accept("cpu", math.floor(utc), mono)

        steps = [(10.0 * k, base + 10 * k) for k in range(11)]
        steps += [(10.0 * k, base + 10 * k - 3605) for k in range(11, 391)]  # hodiny zpět, monotonní dál
        steps += [(3910.35, base + 3900 - 3605 + 0.35),  # jiné měření v téže sekundě
                  (3900.0, base + 3900 - 3605)]  # totéž měření znovu
        rules, gate = module.TimeRules(), history.SampleGate(None, 0)
        pairs = [decide(rules, gate, mono, utc) for mono, utc in steps]
        rules, gate = module.TimeRules(high_water=base + 50), history.SampleGate(base + 50, 0)
        pairs += [decide(rules, gate, 10.0 * k, base + 10 * k) for k in range(11)]  # po restartu
        self.assertEqual([actual for _, actual in pairs], [expected for expected, _ in pairs])
        seen = {expected for expected, _ in pairs}
        self.assertEqual(seen, {None, "duplicate", "same_second", "clock_backward", "before_high_water"})


class HighWaterTest(TempDirTest):
    """Horní mez = nejnovější vzorek, který běh skutečně vložil (run.last_write_utc), i po restartu."""

    def cycle(self, recorder, results, quality="synced"):
        batch = recorder.begin_batch()
        recorder.add_results(batch, results, quality)
        return recorder.write(batch)

    def all_valid(self, recorder, conn):
        self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS), result(CPU_USAGE_ID, State.INFO, 0.0, PERCENT)])
        self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 51.0, CELSIUS, ts=BASE + 10)])

    def all_rejected(self, recorder, conn):
        self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE)], quality="unsynced")
        self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 320.0, "K", ts=BASE + 10)])
        self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE + 20, simulated=True)])

    def mixed(self, recorder, conn):
        batch = recorder.begin_batch()
        recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE + 10)])
        recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 52.0, CELSIUS, ts=BASE + 20)], "unsynced")
        recorder.add_results(batch, [result(CPU_USAGE_ID, State.INFO, 5.0, "x", ts=BASE + 30)])
        recorder.write(batch)

    def conflict(self, recorder, conn):
        self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        # Cizí zápis mimo zapisovač (obnova, ruční zásah): pro BASE + 50 už je jiná hodnota.
        conn.execute("INSERT INTO sample (metric_id, ts, value) SELECT id, ?, 1.0 FROM metric", (BASE + 50,))
        outcome = self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 2.0, CELSIUS, ts=BASE + 50)])
        self.assertEqual(len(outcome.conflicts), 1)

    def same_second(self, recorder, conn):
        self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 51.0, CELSIUS, cycle=BASE + 0.6)])
        self.assertEqual(recorder.rejected["same_second"], 1)

    def unit_mismatch(self, recorder, conn):
        # Řada z jiné verze katalogu (jednotka K) + výsledek s jinou jednotkou než katalog.
        conn.execute("INSERT INTO metric (check_id, subject, unit, created_utc) VALUES (?, '', 'K', ?)", (CPU_TEMP_ID, CREATED))
        self.cycle(recorder, [result(CPU_USAGE_ID, State.INFO, 5.0, PERCENT)])
        self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE + 50)])
        self.cycle(recorder, [result(CPU_USAGE_ID, State.INFO, 6.0, "x", ts=BASE + 60)])
        self.assertEqual(recorder.rejected["unit_mismatch"], 2)

    def untrusted_utc(self, recorder, conn):
        self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 51.0, CELSIUS, ts=BASE + 10)], quality="unsynced")
        self.cycle(recorder, [result(CPU_TEMP_ID, State.INFO, 52.0, CELSIUS, ts=1_700_000_000, cycle=BASE + 20)])
        self.assertEqual(recorder.rejected["time_unsynced"], 2)

    def replayed(self, recorder, conn):
        batch = recorder.begin_batch()
        recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS)])
        recorder.write(batch)
        self.assertTrue(recorder.write(batch).already_written)
        batch = recorder.begin_batch()
        recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 51.0, CELSIUS, ts=BASE + 10)])
        real = recorder.database.connection
        recorder.database.connection = FlakyCommit(real)
        try:
            self.assertRefused("write", recorder.write, batch)
        finally:
            recorder.database.connection = real
        self.assertTrue(recorder.write(batch).already_written)

    def test_high_water_is_newest_inserted_sample_even_after_restart(self):
        cases = {"all_valid": BASE + 10, "all_rejected": None, "mixed": BASE + 10, "conflict": BASE,
                 "same_second": BASE, "unit_mismatch": BASE, "untrusted_utc": BASE, "replayed": BASE + 10}
        for name, expected in cases.items():
            with self.subTest(case=name):
                path = self.create(self.folder / f"{name}.sqlite3")
                with history.open_writer(path, "test", HOST) as database:
                    recorder = HistoryRecorder(database)
                    run_id = recorder.start_run("test", 10, at(BASE - 60))
                    getattr(self, name)(recorder, database.connection)
                    self.assertEqual(history.run_info(database.connection, run_id).last_write_utc, expected)
                # Restart po nečistém ukončení: mez nového běhu a hranice mezery z téhož údaje.
                with history.open_writer(path, "test", HOST) as database:
                    recorder = HistoryRecorder(database)
                    recorder.start_run("test", 10, at(BASE + 1000))
                    self.assertEqual(recorder.high_water, expected)
                    stopped = history.run_info(database.connection, run_id).stopped_utc
                    self.assertEqual(stopped, BASE - 60 if expected is None else expected)
                    probe = BASE - 30 if expected is None else expected
                    batch = recorder.begin_batch()
                    recorder.add_results(batch, [result(MEMORY_ID, State.INFO, 20.0, PERCENT, ts=probe, cycle=10.0)])
                    recorder.add_results(batch, [result(MEMORY_ID, State.INFO, 21.0, PERCENT, ts=probe + 1, cycle=11.0)])
                    self.assertEqual(recorder.write(batch).samples, 1 if expected is not None else 2)
                    self.assertEqual(recorder.rejected["before_high_water"], 0 if expected is None else 1)


# --- M3.2a: agregace ----------------------------------------------------------------

AGG_ROWS = ("SELECT m.check_id, m.subject, a.bucket, a.n_samples, a.n_valid, a.min_value, a.max_value, a.sum_value "
            "FROM {table} a JOIN metric m ON m.id = a.metric_id ORDER BY m.check_id, m.subject, a.bucket")


def direct_rows(written, size, start, end):
    """Přímý výpočet agregací ze zapsaných hodnot v Pythonu, nezávisle na SQL."""
    rows = []
    for (check_id, subject), points in sorted(written.items()):
        windows = {}
        for ts, value in points:
            if start <= ts < end:
                windows.setdefault(ts - ts % size, []).append(value)
        for bucket, values in sorted(windows.items()):
            valid = [value for value in values if value is not None]
            rows.append((check_id, subject, bucket, len(values), len(valid), min(valid) if valid else None,
                         max(valid) if valid else None, sum(valid) if valid else None))
    return rows


class AggregationTest(RecorderTest):
    """Agregace uzavřených oken (HISTORY.md 6) nad dočasnou databází a se simulovaným časem.

    Databáze z RecorderTest má done_until obou úrovní BASE − 3600 (hodina vytvoření).
    Hodnoty jsou násobky 0,25, takže součty jsou v binární aritmetice přesné.
    """

    TEMP = (CPU_TEMP_ID, None, lambda k: 40.0 + (k * 7) % 13 * 0.25)
    USAGE = (CPU_USAGE_ID, None, lambda k: None if k % 11 == 3 else float(k % 5))  # nuly i UNKNOWN

    def fill(self, start, end, step=10, series=(TEMP, USAGE), per_batch=30):
        """Měření každých `step` s v [start, end); vrací {(check_id, subject): [(ts, hodnota)]}."""
        written, cycles = {}, []
        for k, ts in enumerate(range(start, end, step)):
            cycle = []
            for check_id, details, value_of in series:
                value = value_of(k)
                unit = history.SERIES[check_id].unit
                cycle.append(result(check_id, State.UNKNOWN if value is None else State.INFO, value,
                                    None if value is None else unit, ts=ts, details=details))
                written.setdefault((check_id, (details or {}).get(INTERFACE, "")), []).append((ts, value))
            cycles.append(cycle)
        for first in range(0, len(cycles), per_batch):
            self.write(*cycles[first:first + per_batch])
        return written

    def aggregate(self, now, quality="synced", queue=None):
        return self.recorder.aggregate(at(now), quality, QueueState.empty() if queue is None else queue)

    def catch_up(self, now, queue=None):
        """Volá agregaci, dokud zbývají uzavřená okna (jedno volání zpracuje nejvýš hodinu)."""
        for _ in range(200):
            outcome = self.aggregate(now, queue=queue)
            if not outcome.more:
                return outcome
        self.fail("agregace nedohnala ani po 200 voláních")

    def stored(self, table="agg_5m"):
        return self.query(AGG_ROWS.format(table=table))

    def done(self):
        return dict(self.query("SELECT level, done_until FROM aggregation_state"))

    def reopen(self, now):
        self.database.close()  # bez finish_run: jako pád procesu
        self.database = history.open_writer(self.db, "test", HOST)
        self.recorder = HistoryRecorder(self.database)
        self.recorder.start_run("test", 10, at(now))


class AggregationValueTest(AggregationTest):
    def test_one_full_window_matches_direct_computation(self):
        written = self.fill(BASE, BASE + 310)  # 30 měření v okně + jedno v dalším
        outcome = self.catch_up(BASE + 300 + 120)
        self.assertEqual((outcome.done_5m, outcome.done_1h), (BASE + 300, BASE))
        self.assertEqual(self.stored(), direct_rows(written, 300, BASE, BASE + 300))
        self.assertEqual([row[3:5] for row in self.stored()], [(30, 30), (30, 27)])  # 3 × UNKNOWN
        average = self.query(doc_block("read-agg"), {"metric_id": 1, "start": BASE, "end": BASE + 3600})
        expected = direct_rows(written, 300, BASE, BASE + 300)[0]
        self.assertEqual([row[5] for row in average], [expected[7] / expected[4]])  # průměr = součet / platné

    def test_zero_unknown_gap_and_partial_windows(self):
        points = [(BASE + 10 * k, None if k % 3 == 0 else 0.0) for k in range(30)]  # skutečné nuly a UNKNOWN
        points += [(BASE + 300 + 30 * k, None) for k in range(10)]  # okno jen s UNKNOWN; okno BASE + 600 prázdné
        points += list(zip(range(BASE + 900, BASE + 1200, 60), (2.5, 7.5, 0.0, 5.0, None)))  # neúplné okno
        points += [(BASE + 1200, 1.0)]
        cycles = [[result(CPU_USAGE_ID, State.UNKNOWN if value is None else State.INFO, value,
                          None if value is None else PERCENT, ts=ts)] for ts, value in points]
        self.write(*cycles)
        self.catch_up(BASE + 1200 + 120)
        self.assertEqual(self.stored(), [(CPU_USAGE_ID, "", BASE, 30, 20, 0.0, 0.0, 0.0),
                                         (CPU_USAGE_ID, "", BASE + 300, 10, 0, None, None, None),
                                         (CPU_USAGE_ID, "", BASE + 900, 5, 4, 0.0, 7.5, 15.0)])
        self.assertEqual(self.stored(), direct_rows({(CPU_USAGE_ID, ""): points}, 300, BASE, BASE + 1200))
        average = self.query(doc_block("read-agg"), {"metric_id": 1, "start": BASE, "end": BASE + 3600})
        self.assertEqual([(row[0], row[5]) for row in average], [(BASE, 0.0), (BASE + 300, None), (BASE + 900, 3.75)])

    def test_hour_is_exact_rollup_of_twelve_windows(self):
        # Okno j má j hodnot UNKNOWN: různý počet platných vzorků, průměr průměrů by byl špatně.
        temp = (CPU_TEMP_ID, None, lambda k: None if k % 30 < k // 30 else 20.0 + (k % 7) * 0.25)
        written = self.fill(BASE, BASE + 3610, series=(temp,), per_batch=60)
        outcome = self.catch_up(BASE + 3600 + 120)
        self.assertEqual((outcome.done_5m, outcome.done_1h), (BASE + 3600, BASE + 3600))
        windows = self.stored()
        self.assertEqual(len(windows), 12)
        self.assertEqual(windows, direct_rows(written, 300, BASE, BASE + 3600))
        hourly = self.stored("agg_1h")
        self.assertEqual(hourly, direct_rows(written, 3600, BASE, BASE + 3600))
        self.assertEqual(hourly[0][3:5], (360, 360 - sum(range(12))))
        exact = hourly[0][7] / hourly[0][4]
        mean_of_means = sum(row[7] / row[4] for row in windows) / len(windows)
        self.assertGreater(abs(exact - mean_of_means), 1e-6)  # test by chybu průměrování opravdu poznal

    def test_several_series_and_interfaces_match_m2_benchmark(self):
        series = (self.TEMP, self.USAGE,
                  (RX_RATE_ID, {INTERFACE: "eth0"}, lambda k: float(k % 4) * 256.0),
                  (RX_RATE_ID, {INTERFACE: "wlan0"}, lambda k: None if k % 9 == 0 else 1024.0 + k % 3))
        written = self.fill(BASE, BASE + 7210, step=20, series=series, per_batch=90)
        now = BASE + 7200 + 120
        self.database.close()
        reference = self.copy("m2.sqlite3")  # stejná surová data pro smyčku zapisovače z benchmarku M2
        self.database = history.open_writer(self.db, "test", HOST)
        self.recorder = HistoryRecorder(self.database)
        self.recorder.start_run("test", 10, at(now))
        self.catch_up(now)
        module = benchmark()
        conn = sqlite3.connect(reference, isolation_level=None)
        try:
            writer = object.__new__(module.SteadyWriter)
            writer.conn, writer.transactions = conn, Counter()
            writer.sql = {name: module.doc_sql(name) for name in module.STEADY_SQL}
            writer.aggregate(now)  # M2: hodina 5min oken na transakci, rezerva GRACE
            m2 = {table: conn.execute(AGG_ROWS.format(table=table)).fetchall() for table in ("agg_5m", "agg_1h")}
        finally:
            conn.close()
        self.assertEqual(module.GRACE, history.AGGREGATION_GRACE_SECONDS)
        self.assertEqual(self.stored(), m2["agg_5m"])
        self.assertEqual(self.stored("agg_1h"), m2["agg_1h"])
        self.assertEqual(self.stored(), direct_rows(written, 300, BASE, BASE + 7200))
        self.assertEqual(self.stored("agg_1h"), direct_rows(written, 3600, BASE, BASE + 7200))
        counted = self.query("SELECT sum(n_samples) FROM agg_5m")[0][0]
        self.assertEqual(counted, self.query("SELECT count(*) FROM sample WHERE ts < ?", (BASE + 7200,))[0][0])


class AggregationClosingTest(AggregationTest):
    def test_window_closes_exactly_after_grace(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE + 299)])
        self.write([result(CPU_TEMP_ID, State.INFO, 60.0, CELSIUS, ts=BASE + 300)])
        self.assertEqual(self.catch_up(BASE + 300 + 119).done_5m, BASE)  # rezerva 119 s nestačí
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.catch_up(BASE + 300 + 120).done_5m, BASE + 300)  # přesně 120 s
        self.assertEqual(self.stored(), [(CPU_TEMP_ID, "", BASE, 1, 1, 50.0, 50.0, 50.0)])  # BASE + 300 je další okno

    def test_queue_state_is_explicit_contract(self):
        self.fill(BASE, BASE + 610)
        with self.assertRaises(TypeError):
            self.recorder.aggregate(at(BASE + 720), "synced", None)  # chybějící fronta není prázdná fronta
        with self.assertRaises(TypeError):
            QueueState("ano")
        for build in (lambda: QueueState.waiting_since(None), lambda: QueueState.waiting_since(1_700_000_000),
                      lambda: QueueState.waiting_since(True), lambda: QueueState(False, BASE)):
            with self.subTest(build=build), self.assertRaises(ValueError):
                build()
        unknown = self.aggregate(BASE + 720, queue=QueueState.unknown())
        self.assertEqual((unknown.blocked, unknown.done_5m, unknown.windows_5m, unknown.more),
                         ("queue_unknown", BASE - 3600, 0, False))
        self.assertEqual(self.catch_up(BASE + 720, queue=QueueState.waiting_since(BASE + 299)).done_5m, BASE)
        self.assertEqual(self.catch_up(BASE + 720, queue=QueueState.waiting_since(BASE + 300)).done_5m, BASE + 300)
        self.assertEqual(self.catch_up(BASE + 720).done_5m, BASE + 600)

    def test_open_batch_keeps_its_window_open(self):
        self.fill(BASE, BASE + 610)
        batch = self.recorder.begin_batch()  # přijatý, ale ještě nezapsaný vzorek nové řady
        self.recorder.add_results(batch, [result(MEMORY_ID, State.INFO, 30.0, PERCENT, ts=BASE + 450)])
        self.assertEqual(self.catch_up(BASE + 720).done_5m, BASE + 300)
        self.recorder.write(batch)
        self.assertEqual(self.catch_up(BASE + 720).done_5m, BASE + 600)
        self.assertIn((MEMORY_ID, "", BASE + 300, 1, 1, 30.0, 30.0, 30.0), self.stored())
        self.assertEqual(self.recorder.rejected["late"], 0)

    def test_untrusted_time_closes_nothing(self):
        self.fill(BASE, BASE + 610)
        for now, quality in ((BASE + 720, "unsynced"), (BASE + 720, "derived"), (1_700_000_000, "synced")):
            with self.subTest(now=now, quality=quality):
                outcome = self.aggregate(now, quality)
                self.assertEqual((outcome.blocked, outcome.done_5m, outcome.done_1h), ("time_unsynced", BASE - 3600, BASE - 3600))
        self.assertEqual((self.stored(), self.stored("agg_1h")), ([], []))
        with self.assertRaises(ValueError):
            self.aggregate(BASE + 720, "guess")

    def test_clock_step_back_never_moves_boundary_back(self):
        self.fill(BASE, BASE + 610)
        self.catch_up(BASE + 720)
        rows = self.stored()
        outcome = self.aggregate(BASE - 7200)  # hodiny o 2 h zpět, pořád po 2026-01-01
        self.assertEqual((outcome.done_5m, outcome.windows_5m, outcome.blocked), (BASE + 600, 0, None))
        self.assertEqual(self.stored(), rows)

    def test_forward_clock_jump_without_new_samples_closes_no_future_window(self):
        written = self.fill(BASE, BASE + 310)
        for _ in range(5):  # hodiny o den dopředu (synced), kolektor zatím bez nového vzorku
            outcome = self.aggregate(BASE + 86_400)
        self.assertEqual((outcome.done_5m, outcome.more), (BASE + 300, False))
        for key, points in self.fill(BASE + 320, BASE + 610).items():  # po návratu hodin měření pokračuje
            written[key] += points
        self.assertEqual(self.recorder.rejected["late"], 0)
        self.catch_up(BASE + 720)
        self.assertEqual(self.stored()[-1][2:5], (BASE + 300, 29, 26))
        self.assertEqual(self.stored(), direct_rows(written, 300, BASE, BASE + 600))

    def test_catch_up_is_bounded_per_call(self):
        self.fill(BASE, BASE + 5 * 3600 + 60, step=60, per_batch=120)
        steps = []
        while True:
            before = self.done()["5m"]
            outcome = self.aggregate(BASE + 5 * 3600 + 120)
            steps.append(outcome.done_5m - before)
            if not outcome.more:
                break
        self.assertEqual(steps, [3600] * 6)  # jedno volání = nejvýš hodina 5min oken, zarovnaná na hodinu
        self.assertEqual(self.done(), {"5m": BASE + 5 * 3600, "1h": BASE + 5 * 3600})
        self.assertEqual(len(self.stored("agg_1h")), 2 * 5)

    def test_aggregation_needs_running_recorder(self):
        fresh = HistoryRecorder(self.database)
        self.assertRefused("run", fresh.aggregate, at(BASE), "synced", QueueState.empty())
        self.recorder.finish_run(at(BASE))
        self.assertRefused("run", self.aggregate, BASE + 720)


class AggregationSafetyTest(AggregationTest):
    def test_repeated_aggregation_changes_nothing(self):
        self.fill(BASE, BASE + 610)
        first = self.catch_up(BASE + 720)
        rows, hourly = self.stored(), self.stored("agg_1h")
        again = self.aggregate(BASE + 720)
        self.assertEqual((again.windows_5m, again.windows_1h, again.done_5m, again.done_1h),
                         (0, 0, first.done_5m, first.done_1h))
        # Krok nad hotovým intervalem odmítne progress-5m: nic se nezapíše ani nezdvojí.
        self.assertEqual(self.recorder._aggregation_step("5m", lambda conn: (BASE, BASE + 300), BASE + 720), (None, 0))
        self.assertEqual((self.stored(), self.stored("agg_1h")), (rows, hourly))

    def test_hour_waits_for_all_its_windows(self):
        self.fill(BASE, BASE + 1810, step=30)
        self.assertEqual(self.catch_up(BASE + 1800 + 120).done_5m, BASE + 1800)  # půl hodiny oken
        self.assertEqual((self.done()["1h"], self.stored("agg_1h")), (BASE, []))
        # I bez kontroly v kódu odmítne neúplnou hodinu samo SQL progress-1h (druhá vrstva).
        step = self.recorder._aggregation_step("1h", lambda conn: (BASE, BASE + 3600), BASE + 1920)
        self.assertEqual((step, self.done()["1h"], self.stored("agg_1h")), ((None, 0), BASE, []))

    def test_existing_window_is_conflict_and_never_overwritten(self):
        self.fill(BASE, BASE + 610)
        self.aggregate(BASE + 200)  # jen prázdná hodina před BASE
        conn = self.database.connection
        metric = conn.execute("SELECT id FROM metric WHERE check_id = ?", (CPU_TEMP_ID,)).fetchone()[0]
        conn.execute("INSERT INTO agg_5m VALUES (?, ?, 1, 1, 99.0, 99.0, 99.0)", (metric, BASE + 300))  # cizí zásah
        self.assertRefused("conflict", self.aggregate, BASE + 720)
        self.assertEqual(self.stored(), [(CPU_TEMP_ID, "", BASE + 300, 1, 1, 99.0, 99.0, 99.0)])  # nic přepsáno ani přidáno
        self.assertEqual(self.done()["5m"], BASE)
        self.assertFalse(conn.in_transaction)
        conflicts = self.events("history.aggregation_conflict")
        self.assertEqual([(row[1], json.loads(row[3])) for row in conflicts],
                         [("ERROR", {"level": "5m", "start": BASE, "end": BASE + 600, "existing": 1,
                                     "first": BASE + 300, "last": BASE + 300})])
        self.assertRefused("conflict", self.aggregate, BASE + 720)  # agregace stojí, doklad se neopakuje
        self.assertEqual(len(self.events("history.aggregation_conflict")), 1)
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE + 700)])  # měření se zapisují dál
        self.reopen(BASE + 800)
        self.assertRefused("conflict", self.aggregate, BASE + 920)  # nový běh konflikt doloží znovu
        self.assertEqual(len(self.events("history.aggregation_conflict")), 2)

    def test_existing_hour_is_conflict_after_finished_windows(self):
        self.fill(BASE, BASE + 3660, step=60, per_batch=61)
        conn = self.database.connection
        metric = conn.execute("SELECT id FROM metric WHERE check_id = ?", (CPU_TEMP_ID,)).fetchone()[0]
        conn.execute("INSERT INTO agg_1h VALUES (?, ?, 1, 1, 99.0, 99.0, 99.0)", (metric, BASE))
        self.assertRefused("conflict", self.catch_up, BASE + 3600 + 120)
        self.assertEqual(self.done(), {"5m": BASE + 3600, "1h": BASE})  # 5min okna hotová, hodina ne
        self.assertEqual(self.stored("agg_1h"), [(CPU_TEMP_ID, "", BASE, 1, 1, 99.0, 99.0, 99.0)])
        self.assertEqual(json.loads(self.events("history.aggregation_conflict")[0][3])["level"], "1h")

    def test_crash_before_commit_in_child_process_leaves_nothing(self):
        written = self.fill(BASE, BASE + 310)
        self.aggregate(BASE + 200)  # prázdná hodina před BASE je hotová
        self.database.close()
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        code = subprocess.run([sys.executable, "-c", CHILD, "aggregate", str(self.db), HOST, str(BASE + 420)],
                              env=env, capture_output=True, text=True, timeout=60).returncode
        self.assertEqual(code, 74)
        self.database = history.open_writer(self.db, "test", HOST)
        self.recorder = HistoryRecorder(self.database)
        self.recorder.start_run("test", 10, at(BASE + 430))
        self.assertEqual((self.done()["5m"], self.stored()), (BASE, []))  # vložená okna zmizela s transakcí
        self.assertEqual(self.recorder._gate.done_until, BASE)
        self.assertEqual(self.catch_up(BASE + 430).done_5m, BASE + 300)
        self.assertEqual(self.stored(), direct_rows(written, 300, BASE, BASE + 300))

    def test_failure_mid_insert_rolls_back_everything(self):
        written = self.fill(BASE, BASE + 610)
        self.aggregate(BASE + 200)
        conn = self.database.connection
        calls = []

        def fail_second():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("simulované selhání uprostřed vkládání")
            return 0

        conn.create_function("fail_second", 0, fail_second)
        conn.execute("CREATE TEMP TRIGGER fail_mid BEFORE INSERT ON main.agg_5m BEGIN SELECT fail_second(); END")
        try:
            self.assertRefused("write", self.aggregate, BASE + 720)
        finally:
            conn.execute("DROP TRIGGER temp.fail_mid")
        self.assertEqual(len(calls), 2)  # první okno už bylo vložené, druhé selhalo
        self.assertEqual((self.stored(), self.done()["5m"], self.recorder._gate.done_until), ([], BASE, BASE))
        self.assertFalse(conn.in_transaction)
        self.assertEqual(self.catch_up(BASE + 720).done_5m, BASE + 600)
        self.assertEqual(self.stored(), direct_rows(written, 300, BASE, BASE + 600))

    def test_interrupt_mid_aggregation_rolls_back(self):
        self.fill(BASE, BASE + 610)
        self.aggregate(BASE + 200)
        real = self.database.connection

        class Interrupting(Passthrough):
            def execute(self, sql, *args):
                if sql == history.AGGREGATE_5M_SQL:
                    raise KeyboardInterrupt  # např. Ctrl+C po posunu done_until, před vložením oken
                return super().execute(sql, *args)

        self.database.connection = Interrupting(real)
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.aggregate(BASE + 720)
        finally:
            self.database.connection = real
        self.assertFalse(real.in_transaction)  # zámek zápisu uvolněn, posun done_until vrácen
        self.assertEqual((self.done()["5m"], self.recorder._gate.done_until, self.stored()), (BASE, BASE, []))
        self.assertEqual(self.catch_up(BASE + 720).done_5m, BASE + 600)

    def test_restart_continues_without_gaps_or_double_counting(self):
        written = self.fill(BASE, BASE + 7210, step=30, per_batch=60)
        self.assertEqual(self.catch_up(BASE + 4200 + 120).done_5m, BASE + 4200)
        self.reopen(BASE + 7300)
        self.assertEqual(self.recorder._gate.done_until, BASE + 4200)  # hranice z databáze, ne z paměti
        final = self.catch_up(BASE + 7200 + 120)
        self.assertEqual((final.done_5m, final.done_1h), (BASE + 7200, BASE + 7200))
        self.assertEqual(self.stored(), direct_rows(written, 300, BASE, BASE + 7200))
        self.assertEqual(self.stored("agg_1h"), direct_rows(written, 3600, BASE, BASE + 7200))
        counted = self.query("SELECT sum(n_samples) FROM agg_5m")[0][0]
        self.assertEqual(counted, self.query("SELECT count(*) FROM sample WHERE ts < ?", (BASE + 7200,))[0][0])


class AggregationDataBoundTest(AggregationTest):
    """Podmínka 4 (HISTORY.md 6.1): uzavření nepředběhne nejnovější zapsaný vzorek."""

    def drive(self, now, limit=500):
        """Volá agregaci se stejným časem, dokud hlásí `more`; ověří, že každé `more` znamená postup."""
        calls = []
        for _ in range(limit):
            before = self.done()
            outcome = self.aggregate(now)
            after = (outcome.done_5m, outcome.done_1h)
            self.assertGreaterEqual(after, (before["5m"], before["1h"]))  # hranice nikdy neklesne
            self.assertLessEqual(outcome.done_5m - before["5m"], 3600)  # nejvýš hodina oken za volání
            calls.append((outcome.more, after != (before["5m"], before["1h"])))
            if not outcome.more:
                break
        else:
            self.fail(f"agregace hlásí more i po {limit} voláních")
        self.assertTrue(all(progressed for more, progressed in calls[:-1]))  # more vždy s postupem
        return outcome, len(calls)

    def merged(self, *parts):
        written = {}
        for part in parts:
            for key, points in part.items():
                written.setdefault(key, []).extend(points)
        return written

    def test_last_sample_inside_window_keeps_that_window_open(self):
        written = self.fill(BASE, BASE + 160)  # poslední vzorek BASE + 150, uprostřed okna
        for now in (BASE + 420, BASE + 3600, BASE + 86_400):
            outcome, _ = self.drive(now)
            self.assertEqual((outcome.done_5m, outcome.more, self.stored()), (BASE, False, []))
        written = self.merged(written, self.fill(BASE + 300, BASE + 310))  # další vzorek okno uzavře
        self.assertEqual(self.drive(BASE + 86_400)[0].done_5m, BASE + 300)
        self.assertEqual(self.stored(), direct_rows(written, 300, BASE, BASE + 300))

    def test_last_sample_on_window_boundary_closes_previous_window(self):
        written = self.fill(BASE, BASE + 310)  # poslední vzorek přesně BASE + 300
        outcome, _ = self.drive(BASE + 86_400)
        self.assertEqual((outcome.done_5m, outcome.more), (BASE + 300, False))
        self.assertEqual(self.stored(), direct_rows(written, 300, BASE, BASE + 300))  # okno BASE + 300 zůstává otevřené

    def test_stopped_measurement_never_loops_and_keeps_last_window_open(self):
        self.fill(BASE, BASE + 610)
        reports = []
        for step in range(200):  # dva dny bez měření, volání každých 15 min
            outcome = self.aggregate(BASE + 720 + 900 * step)
            reports.append(outcome.more)
            self.assertLessEqual(outcome.done_5m, BASE + 600)
        self.assertEqual(self.done(), {"5m": BASE + 600, "1h": BASE})
        self.assertEqual(reports[2:], [False] * 198)  # po dohnání už nic nehlásí

    def test_measurement_resuming_after_an_hour_closes_the_gap(self):
        first = self.fill(BASE, BASE + 610)
        self.drive(BASE + 3000)
        second = self.fill(BASE + 4210, BASE + 4510)  # po více než hodinové mezeře
        outcome, _ = self.drive(BASE + 4500 + 120)
        written = self.merged(first, second)
        self.assertEqual((outcome.done_5m, outcome.done_1h), (BASE + 4500, BASE + 3600))
        self.assertEqual(self.stored(), direct_rows(written, 300, BASE, BASE + 4500))  # mezera bez řádků
        self.assertEqual(self.stored("agg_1h"), direct_rows(written, 3600, BASE, BASE + 3600))

    def test_measurement_resuming_after_days_is_caught_up_gradually(self):
        first = self.fill(BASE, BASE + 610)
        self.drive(BASE + 720)
        resume = BASE + 3 * 86_400
        second = self.fill(resume, resume + 310)
        outcome, calls = self.drive(resume + 300 + 120)
        written = self.merged(first, second)
        self.assertEqual((outcome.done_5m, outcome.done_1h), (resume + 300, resume))
        self.assertEqual(calls, 73)  # zbytek první hodiny + 71 celých hodin + poslední okno, po hodině na volání
        self.assertEqual(self.stored(), direct_rows(written, 300, BASE, resume + 300))
        self.assertEqual(self.stored("agg_1h"), direct_rows(written, 3600, BASE, resume))

    def test_restart_during_outage_keeps_bound_of_previous_run(self):
        first = self.fill(BASE, BASE + 610)
        self.reopen(BASE + 7200)  # pád a restart během výpadku měření
        outcome, _ = self.drive(BASE + 7200)
        self.assertEqual((outcome.done_5m, outcome.more), (BASE + 600, False))  # mez z last_write předchozího běhu
        second = self.fill(BASE + 7210, BASE + 7510)
        outcome, _ = self.drive(BASE + 7500 + 120)
        self.assertEqual(outcome.done_5m, BASE + 7500)
        self.assertEqual(self.stored(), direct_rows(self.merged(first, second), 300, BASE, BASE + 7500))

    def test_forward_jump_with_samples_then_clock_returns(self):
        first = self.fill(BASE, BASE + 310)
        future = BASE + 86_400
        jumped = self.fill(future, future + 610)  # hodiny o den dopředu, měření pokračuje (synced)
        self.drive(future + 720)
        self.assertEqual(self.done()["5m"], future + 600)
        rows = self.stored()
        self.fill(BASE + 320, BASE + 610)  # hodiny zpět na správný čas
        self.write([result(MEMORY_ID, State.INFO, 30.0, PERCENT, ts=BASE + 330)])  # nová řada
        self.assertEqual(self.recorder.rejected["clock_backward"], 2 * 29)
        self.assertEqual(self.recorder.rejected["late"], 1)  # do hotového okna se nic nevloží
        self.assertEqual(self.drive(BASE + 720)[0].done_5m, future + 600)  # hranice neklesla
        self.assertEqual(self.stored(), rows)
        self.assertEqual(rows, direct_rows(self.merged(first, jumped), 300, BASE, future + 600))

    def test_queue_and_data_bound_together(self):
        self.fill(BASE, BASE + 1210)
        self.assertEqual(self.drive_queue(QueueState.waiting_since(BASE + 450)), BASE + 300)  # starší čekající vzorek
        self.assertEqual(self.drive_queue(QueueState.waiting_since(BASE + 600)), BASE + 600)  # přesně na hranici
        unknown = self.aggregate(BASE + 86_400, queue=QueueState.unknown())
        self.assertEqual((unknown.blocked, unknown.done_5m, unknown.more), ("queue_unknown", BASE + 600, False))
        self.assertEqual(self.drive_queue(QueueState.empty()), BASE + 1200)  # prázdná fronta: mez dají data

    def drive_queue(self, queue):
        for _ in range(50):
            outcome = self.aggregate(BASE + 86_400, queue=queue)
            if not outcome.more:
                return outcome.done_5m
        self.fail("agregace s frontou nedoběhla")

    def test_no_new_data_still_finishes_older_hour(self):
        written = self.fill(BASE, BASE + 3610, step=60, per_batch=61)
        real = self.database.connection

        class StopRollup(Passthrough):
            def execute(self, sql, *args):
                if sql == history.ROLLUP_1H_SQL and args[0]["start"] == BASE:
                    raise KeyboardInterrupt  # přerušení hodinového kroku po potvrzených 5min oknech
                return super().execute(sql, *args)

        self.database.connection = StopRollup(real)
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.drive(BASE + 3720)
        finally:
            self.database.connection = real
        self.assertEqual(self.done(), {"5m": BASE + 3600, "1h": BASE})
        outcome = self.aggregate(BASE + 3720)  # žádná nová data, hotová 5min okna se dosčítají
        self.assertEqual((outcome.done_1h, outcome.windows_1h, outcome.more), (BASE + 3600, 2, False))
        self.assertEqual(self.stored("agg_1h"), direct_rows(written, 3600, BASE, BASE + 3600))


class AggregationConflictTest(AggregationTest):
    def conflict_row(self, table, bucket):
        conn = self.database.connection
        metric = conn.execute("SELECT id FROM metric WHERE check_id = ?", (CPU_TEMP_ID,)).fetchone()[0]
        conn.execute(f"INSERT INTO {table} VALUES (?, ?, 1, 1, 99.0, 99.0, 99.0)", (metric, bucket))

    def test_failed_conflict_record_is_not_claimed(self):
        self.fill(BASE, BASE + 610)
        self.aggregate(BASE + 200)
        self.conflict_row("agg_5m", BASE + 300)
        with mock.patch.object(history, "INSERT_EVENT_SQL", "INSERT INTO neexistuje VALUES (1)"):
            error = self.assertRefused("conflict", self.aggregate, BASE + 720)
        self.assertIn("nepodařilo potvrdit", str(error))  # žádné tvrzení, že doklad existuje
        self.assertEqual(self.events("history.aggregation_conflict"), [])
        self.assertFalse(self.database.connection.in_transaction)
        self.assertEqual((self.done()["5m"], self.recorder._gate.done_until), (BASE, BASE))
        self.assertEqual(self.stored(), [(CPU_TEMP_ID, "", BASE + 300, 1, 1, 99.0, 99.0, 99.0)])
        later = self.assertRefused("conflict", self.aggregate, BASE + 720)  # zastaveno i bez dokladu
        self.assertIn("nepodařilo potvrdit", str(later))
        self.assertEqual(self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE + 700)]).samples, 1)

    def test_recorded_conflict_keeps_gate_and_writing(self):
        self.fill(BASE, BASE + 610)
        self.aggregate(BASE + 200)
        self.conflict_row("agg_5m", BASE + 300)
        error = self.assertRefused("conflict", self.aggregate, BASE + 720)
        self.assertIn("doklad history.aggregation_conflict zapsán", str(error))
        self.assertEqual(self.recorder._gate.done_until, BASE)  # rollback: brána beze změny
        self.assertEqual(self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE + 700)]).samples, 1)
        self.reopen(BASE + 800)
        self.assertEqual(self.recorder._gate.done_until, BASE)  # po restartu skutečná potvrzená hranice

    def test_conflict_check_failure_is_write_error_not_conflict(self):
        self.fill(BASE, BASE + 610)
        self.aggregate(BASE + 200)
        self.conflict_row("agg_5m", BASE + 300)
        real = self.database.connection

        class BrokenLookup(Passthrough):
            def execute(self, sql, *args):
                if sql.startswith("SELECT count(*), min(bucket)"):
                    raise sqlite3.OperationalError("disk I/O error (simulace)")
                return super().execute(sql, *args)

        self.database.connection = BrokenLookup(real)
        try:
            self.assertRefused("write", self.aggregate, BASE + 720)  # konflikt nedoložen: nezastavuje se
        finally:
            self.database.connection = real
        self.assertEqual(self.events("history.aggregation_conflict"), [])
        error = self.assertRefused("conflict", self.aggregate, BASE + 720)  # další volání konflikt ověří a doloží
        self.assertIn("doklad history.aggregation_conflict zapsán", str(error))
        self.assertEqual(len(self.events("history.aggregation_conflict")), 1)


class AggregationGateTest(AggregationTest):
    def test_gate_follows_confirmed_boundary_in_same_run(self):
        self.fill(BASE, BASE + 610)
        self.assertEqual(self.recorder._gate.done_until, BASE - 3600)
        self.catch_up(BASE + 720)
        self.assertEqual(self.recorder._gate.done_until, BASE + 600)
        rows = self.stored()
        # Opožděné měření (nová řada) s časem v hotovém okně: late, agregace ani surová data beze změny.
        self.write([result(MEMORY_ID, State.INFO, 30.0, PERCENT, ts=BASE + 550, cycle=BASE + 700.5)])
        self.assertEqual((self.recorder.rejected["late"], self.samples(MEMORY_ID)), (1, []))
        self.assertEqual(self.stored(), rows)
        self.reopen(BASE + 800)
        self.assertEqual(self.recorder._gate.done_until, BASE + 600)

    def test_open_batch_cannot_be_bypassed(self):
        self.fill(BASE, BASE + 910)
        batch = self.recorder.begin_batch()
        batch.samples.append(PendingSample(MEMORY_ID, "", PERCENT, BASE + 350, "synced", 30.0))  # mimo add_results
        self.assertEqual(self.catch_up(BASE + 1020).done_5m, BASE + 300)
        real = self.database.connection
        self.database.connection = FlakyCommit(real)
        try:
            self.assertRefused("write", self.recorder.write, batch)  # nejistý commit, dávka zůstává otevřená
        finally:
            self.database.connection = real
        self.assertEqual(self.catch_up(BASE + 1020).done_5m, BASE + 300)  # zapečetěná dávka dál blokuje
        self.assertTrue(self.recorder.write(batch).already_written)
        self.assertEqual(self.catch_up(BASE + 1020).done_5m, BASE + 900)
        self.assertIn((MEMORY_ID, "", BASE + 300, 1, 1, 30.0, 30.0, 30.0), self.stored())

    def test_late_sample_bypassing_gate_is_never_written(self):
        self.fill(BASE, BASE + 610)
        self.catch_up(BASE + 720)
        rows = self.stored()
        batch = self.recorder.begin_batch()
        batch.samples.append(PendingSample(CPU_TEMP_ID, "", CELSIUS, BASE + 299, "synced", 1.0))  # obejde SampleGate
        self.assertEqual(self.recorder.write(batch).samples, 0)
        self.assertEqual(self.recorder.rejected["late"], 1)
        self.assertEqual(self.stored(), rows)
        self.assertEqual(self.query("SELECT count(*) FROM sample WHERE ts >= ? AND ts < ?", (BASE, BASE + 300)), [(60,)])


# --- M3.2b: retence -----------------------------------------------------------------

DAY = 86_400
STABLE = ClockState.synced_for(DAY)  # simulace kontraktu M3.3, ne důkaz synchronizace
TABLES = ("sample", "agg_5m", "agg_1h", "check_change", "event")


class RetentionTest(AggregationTest):
    """Retence nad dočasnou databází se simulovaným časem (HISTORY.md 7)."""

    def retain(self, now, clock=STABLE, **options):
        return self.recorder.retain(at(now), clock, **options)

    def plan(self, now, clock=STABLE):
        return self.recorder.retention_plan(at(now), clock)

    def drain(self, now, clock=STABLE):
        """Běhy retence, dokud zbývá práce; vrací součty smazaných řádků a počet běhů."""
        total, runs = Counter(), 0
        while True:
            outcome = self.retain(now, clock)
            runs += 1
            total.update(outcome.deleted)
            if not outcome.more:
                return total, runs, outcome
            self.assertLess(runs, 100)

    def counts(self):
        return {table: self.query(f"SELECT count(*) FROM {table}")[0][0] for table in TABLES}

    def metric(self, check_id, subject=""):
        conn = self.database.connection
        conn.execute("INSERT INTO metric (check_id, subject, unit, created_utc) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (check_id, subject) DO NOTHING", (check_id, subject, history.SERIES[check_id].unit, CREATED))
        return conn.execute("SELECT id FROM metric WHERE check_id = ? AND subject = ?", (check_id, subject)).fetchone()[0]

    def seed_samples(self, start, end, step=600, checks=(CPU_TEMP_ID, CPU_USAGE_ID)):
        """Syntetické vzorky přímo v SQL (rychlá dlouhá historie) a horní mez běhu podle nich."""
        conn = self.database.connection
        ids = [self.metric(check_id) for check_id in checks]
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("INSERT INTO sample (metric_id, ts, value) VALUES (?, ?, ?)",
                         [(metric, ts, float(ts // step % 7)) for ts in range(start, end, step) for metric in ids])
        conn.execute("UPDATE run SET last_write_utc = max(coalesce(last_write_utc, 0), ?) WHERE id = ?",
                     (end - step, self.run_id))
        conn.execute("COMMIT")

    def aggregate_until(self, end_5m, end_1h=None, now=None):
        """Fixture: schválené kroky agregace (progress + INSERT) přes celý rozsah najednou."""
        done = lambda level: lambda conn: (conn.execute(history.DONE_UNTIL_SQL, (level,)).fetchone()[0],
                                           end_5m if level == "5m" else end_1h)
        now = now or end_5m + 600
        self.recorder._aggregation_step("5m", done("5m"), now)
        if end_1h is not None:
            self.recorder._aggregation_step("1h", done("1h"), now)

    def raw_in(self, start, end):
        return self.query("SELECT count(*) FROM sample WHERE ts >= ? AND ts < ?", (start, end))[0][0]


class RetentionPlanTest(RetentionTest):
    def test_plan_reports_bounds_and_writes_nothing(self):
        self.seed_samples(BASE, BASE + 2 * DAY)
        self.aggregate_until(BASE + DAY, BASE + DAY)
        now = BASE + 32 * DAY
        conn = self.database.connection
        before, changes = self.counts(), conn.total_changes
        plan = self.plan(now)
        with history.open_reader(self.db) as reader:  # i nad spojením jen pro čtení
            self.assertEqual(history.retention_plan(reader.connection, at(now), STABLE), plan)
        self.assertEqual((self.counts(), conn.total_changes), (before, changes))  # dry-run nic nezměnil
        self.assertEqual((plan.blocked, plan.done_5m, plan.done_1h), (None, BASE + DAY, BASE + DAY))
        sample = plan.table("sample")
        self.assertEqual((sample.time_cutoff, sample.aggregation_until, sample.cutoff, sample.limited_by_aggregation),
                         (now - 30 * DAY, BASE + DAY, BASE + DAY, True))
        self.assertEqual((sample.oldest, sample.candidates, sample.invariant_ok), (BASE, 2 * 144, True))
        five = plan.table("agg_5m")
        self.assertEqual((five.time_cutoff, five.aggregation_until, five.candidates),
                         ((now - 365 * DAY) // 3600 * 3600, BASE + DAY, 0))
        self.assertEqual(plan.table("agg_1h").time_cutoff, history._years_before(now, 10) // 3600 * 3600)
        self.assertEqual(datetime.fromtimestamp(plan.table("agg_1h").time_cutoff, timezone.utc).year,
                         datetime.fromtimestamp(now, timezone.utc).year - 10)
        for table in ("check_change", "event"):
            self.assertEqual(plan.table(table).time_cutoff, now - 730 * DAY)
        outcome = self.retain(now)
        self.assertEqual(outcome.deleted["sample"], sample.candidates)  # skutečný počet až po provedení
        self.assertEqual(outcome.retained_since["sample"], BASE + DAY)

    def test_years_before_handles_leap_day(self):
        leap = math.floor(datetime(2036, 2, 29, 12, tzinfo=timezone.utc).timestamp())
        self.assertEqual(history._years_before(leap, 10), math.floor(datetime(2026, 2, 28, 12, tzinfo=timezone.utc).timestamp()))

    def test_empty_database(self):
        plan = self.plan(BASE + 800 * DAY)
        self.assertEqual([(entry.oldest, entry.candidates) for entry in plan.tables][:3], [(None, 0)] * 3)
        outcome = self.retain(BASE + 800 * DAY)
        self.assertEqual((outcome.transactions, outcome.more, outcome.blocked), (1, False, None))  # jen staré process.start
        self.assertEqual(outcome.deleted["event"], 1)
        again = self.retain(BASE + 800 * DAY)
        self.assertEqual((again.transactions, sum(again.deleted.values()), again.more), (0, 0, False))

    def test_parameters_cannot_shorten_retention(self):
        self.seed_samples(BASE, BASE + 2 * DAY)
        self.aggregate_until(BASE + 2 * DAY - 600, BASE + DAY)
        now = BASE + 31 * DAY
        for bad in (0, 25, True, 1.5):
            with self.subTest(max_transactions=bad), self.assertRaises(ValueError):
                self.retain(now, max_transactions=bad)
        with self.assertRaises(TypeError):
            self.retain(now, plan={"sample": now})
        forged = self.plan(now)
        forged = history.RetentionPlan(forged.now, None, forged.done_5m, forged.done_1h, tuple(
            history.RetentionTable(entry.table, now, None, now, False, entry.oldest, entry.candidates, True)
            for entry in forged.tables))  # podvržený plán „smaž vše do teď“
        self.retain(now, plan=forged)
        self.assertEqual(self.query("SELECT min(ts) FROM sample")[0][0], now - 30 * DAY)  # jen schválených 30 dní
        self.assertEqual(self.raw_in(now - 30 * DAY, now), self.raw_in(BASE + DAY, BASE + 2 * DAY))

    def test_older_plan_caps_execution(self):
        self.seed_samples(BASE, BASE + 3 * DAY)
        self.aggregate_until(BASE + 3 * DAY - 600, BASE + 2 * DAY)
        plan = self.plan(BASE + 31 * DAY)  # schválený plán: hranice BASE + 1 den
        for _ in range(3):  # pozdější běhy by smazaly víc (BASE + 2 dny), plán je omezí
            outcome = self.retain(BASE + 32 * DAY, plan=plan)
        self.assertEqual((outcome.more, self.query("SELECT min(ts) FROM sample")[0][0]), (False, BASE + DAY))


class RetentionSafetyTest(RetentionTest):
    def test_unaggregated_samples_are_never_deleted(self):
        written = self.fill(BASE, BASE + 3 * 3600 + 10, step=60, per_batch=120)
        self.drive_catch_up(BASE + 2 * 3600 + 120)  # agregace jen do BASE + 2 h
        aggregated = sum(n for (_, _, bucket, n, *_rest) in self.stored() if bucket < BASE + 2 * 3600)
        now = BASE + 400 * DAY  # všechno je starší než 30 dní
        total, _, outcome = self.drain(now)
        self.assertEqual(outcome.retained_since["sample"], BASE + 2 * 3600)
        self.assertEqual(self.query("SELECT min(ts), count(*) FROM sample")[0],
                         (BASE + 2 * 3600, self.raw_in(BASE + 2 * 3600, BASE + 4 * 3600)))
        self.assertEqual(total["sample"], aggregated)  # smazané = přesně to, co je v agregacích
        self.assertEqual(sum(len([p for p in points if p[0] < BASE + 2 * 3600]) for points in written.values()), aggregated)

    def drive_catch_up(self, now):
        for _ in range(50):
            if not self.aggregate(now).more:
                return
        self.fail("agregace nedoběhla")

    def test_lagging_aggregation_keeps_source_data(self):
        self.seed_samples(BASE, BASE + 10 * DAY)
        self.aggregate_until(BASE + 2 * DAY, BASE + 2 * DAY)  # agregace stojí několik dní
        now = BASE + 45 * DAY
        sample = self.plan(now).table("sample")
        self.assertEqual((sample.cutoff, sample.limited_by_aggregation), (BASE + 2 * DAY, True))
        self.drain(now)
        self.assertEqual(self.query("SELECT min(ts) FROM sample")[0][0], BASE + 2 * DAY)
        self.aggregate_until(BASE + 10 * DAY - 600, BASE + 9 * DAY)  # agregace dohnala
        self.drain(now)
        self.assertEqual(self.query("SELECT min(ts) FROM sample")[0][0], BASE + 10 * DAY - 600)  # now − 30 d je později

    def test_aggregated_rows_beyond_done_until_prove_nothing(self):
        self.seed_samples(BASE, BASE + 2 * DAY)
        self.aggregate_until(BASE + DAY)
        conn = self.database.connection  # cizí agregovaný řádek za done_until (částečný, nepotvrzený interval)
        conn.execute("INSERT INTO agg_5m SELECT metric_id, ts - ts % 300, count(*), count(value), min(value), max(value), "
                     "sum(value) FROM sample WHERE ts >= ? AND ts < ? GROUP BY metric_id, ts - ts % 300", (BASE + DAY, BASE + DAY + 3600))
        self.drain(BASE + 60 * DAY)
        self.assertEqual(self.query("SELECT min(ts) FROM sample")[0][0], BASE + DAY)  # autoritativní je done_until

    def test_failed_aggregation_does_not_open_retention(self):
        self.seed_samples(BASE, BASE + 3600 + 600)
        self.aggregate(BASE + 200)  # prázdná hodina před daty
        conn = self.database.connection
        conn.create_function("boom", 0, lambda: 1 / 0)
        conn.execute("CREATE TEMP TRIGGER boom_agg BEFORE INSERT ON main.agg_5m BEGIN SELECT boom(); END")
        try:
            self.assertRefused("write", self.aggregate, BASE + 3600 + 900)  # krok agregace selže a vrátí se
        finally:
            conn.execute("DROP TRIGGER temp.boom_agg")
        self.assertEqual(self.done()["5m"], BASE)
        self.drain(BASE + 60 * DAY)
        self.assertEqual(self.query("SELECT min(ts) FROM sample")[0][0], BASE)  # nic smazáno

    def test_five_minute_rows_wait_for_hourly_rollup(self):
        self.seed_samples(BASE, BASE + 3 * 3600)
        self.aggregate_until(BASE + 3 * 3600 - 600, BASE + 3600)  # 5 min hotové, hodina jen první
        now = BASE + 400 * DAY
        five = self.plan(now).table("agg_5m")
        self.assertEqual((five.cutoff, five.limited_by_aggregation), (BASE + 3600, True))
        self.drain(now)
        self.assertEqual(self.query("SELECT min(bucket) FROM agg_5m")[0][0], BASE + 3600)
        self.assertEqual(self.query("SELECT count(*) FROM agg_1h WHERE bucket = ?", (BASE,))[0][0], 2)  # hodina zůstala

    def test_last_open_window_stays_protected(self):
        written = self.fill(BASE, BASE + 160)  # poslední okno s posledním vzorkem se neuzavře (podmínka 4)
        self.drive_catch_up(BASE + 86_400)
        self.drain(BASE + 400 * DAY)
        self.assertEqual(self.query("SELECT count(*) FROM sample")[0][0], sum(len(points) for points in written.values()))

    def test_exact_cutoffs_follow_sql_rule(self):
        cpu = self.metric(CPU_TEMP_ID)
        now = BASE + 3 * 365 * DAY + 3600 * 5 + 1234
        sample_cut = now - 30 * DAY
        five_cut = (now - 365 * DAY) // 3600 * 3600
        hour_cut = history._years_before(now, 10) // 3600 * 3600
        conn = self.database.connection
        conn.executemany("INSERT INTO sample VALUES (?, ?, 1.0)", [(cpu, sample_cut - 1), (cpu, sample_cut)])
        conn.executemany("INSERT INTO agg_5m VALUES (?, ?, 1, 1, 1.0, 1.0, 1.0)", [(cpu, five_cut - 300), (cpu, five_cut)])
        conn.executemany("INSERT INTO agg_1h VALUES (?, ?, 1, 1, 1.0, 1.0, 1.0)",
                         [(cpu, five_cut - 3600), (cpu, five_cut), (cpu, hour_cut - 3600), (cpu, hour_cut)])
        conn.execute("UPDATE aggregation_state SET done_until = ?", (sample_cut + 300 - sample_cut % 300,))
        conn.execute("INSERT INTO agg_5m VALUES (?, ?, 2, 2, 1.0, 1.0, 2.0)", (cpu, sample_cut - sample_cut % 300))
        conn.execute("INSERT INTO event (ts, ts_quality, kind, severity, message) VALUES (?, 'synced', 'x', 'INFO', 'a'), "
                     "(?, 'synced', 'x', 'INFO', 'b')", (now - 730 * DAY - 1, now - 730 * DAY))
        conn.execute("UPDATE run SET last_write_utc = ?", (sample_cut,))
        self.drain(now)
        self.assertEqual(self.query("SELECT ts FROM sample ORDER BY ts"), [(sample_cut,)])  # přesně na hranici zůstává
        self.assertEqual(self.query("SELECT min(bucket) FROM agg_5m")[0][0], five_cut)
        self.assertEqual(self.query("SELECT min(bucket) FROM agg_1h")[0][0], hour_cut)
        self.assertEqual(self.query("SELECT message FROM event WHERE kind = 'x'"), [("b",)])

    def test_invariant_mismatch_stops_only_that_table(self):
        self.seed_samples(BASE, BASE + 2 * DAY)
        self.aggregate_until(BASE + DAY, BASE + DAY)
        conn = self.database.connection
        conn.execute("UPDATE aggregation_state SET done_until = ? WHERE level = '5m'", (BASE + 2 * DAY,))  # bez agregace
        conn.execute("INSERT INTO event (ts, ts_quality, kind, severity, message) VALUES (?, 'synced', 'x', 'INFO', 'staré')",
                     (BASE,))
        conn.execute("DELETE FROM agg_5m WHERE bucket = ?", (BASE,))  # chybí agregace pro nejstarší vzorky
        now = BASE + 800 * DAY
        self.assertFalse(self.plan(now).table("sample").invariant_ok)
        before = self.query("SELECT count(*) FROM sample")[0][0]
        outcome = self.retain(now)
        self.assertEqual(outcome.stopped, {"sample": "aggregation_mismatch"})
        self.assertEqual(self.query("SELECT count(*) FROM sample")[0][0], before)  # vzorky bez agregace zůstaly
        self.assertGreater(outcome.deleted["event"], 0)  # ostatní tabulky pokračují
        conn.execute("UPDATE aggregation_state SET done_until = ? WHERE level = '1h'", (BASE + 3 * DAY,))  # 1h > 5m
        second = self.retain(now)
        self.assertEqual(second.stopped, {"sample": "aggregation_state_invalid", "agg_5m": "aggregation_state_invalid"})

    def test_repeated_retention_is_idempotent_and_keeps_aggregation_state(self):
        self.seed_samples(BASE, BASE + 3 * DAY)
        self.aggregate_until(BASE + 2 * DAY, BASE + 2 * DAY)
        state = self.done()
        self.drain(BASE + 40 * DAY)
        snapshot = self.counts()
        again = self.retain(BASE + 40 * DAY)
        self.assertEqual((again.transactions, sum(again.deleted.values())), (0, 0))
        self.assertEqual((self.counts(), self.done()), (snapshot, state))  # retence aggregation_state nemění

    def test_aggregation_and_gate_work_after_retention(self):
        self.fill(BASE, BASE + 3 * 3600 + 10, step=60, per_batch=200)
        self.drive_catch_up(BASE + 3 * 3600 + 120)
        self.drain(BASE + 3 * 3600 + 31 * DAY)
        self.assertEqual(self.query("SELECT count(*) FROM sample WHERE ts < ?", (BASE + 3 * 3600,))[0][0], 0)
        self.write([result(MEMORY_ID, State.INFO, 1.0, PERCENT, ts=BASE + 100)])  # do hotového okna: late
        self.assertEqual(self.recorder.rejected["late"], 1)
        self.fill(BASE + 3 * 3600 + 60, BASE + 3 * 3600 + 700, step=60)
        self.assertEqual(self.aggregate(BASE + 3 * 3600 + 900).done_5m, BASE + 3 * 3600 + 600)


class RetentionLimitTest(RetentionTest):
    def test_one_hour_per_transaction_and_24_per_run(self):
        self.seed_samples(BASE, BASE + 40 * 3600, step=300)
        self.aggregate_until(BASE + 40 * 3600 - 300, BASE + 39 * 3600)
        now = BASE + 40 * 3600 + 31 * DAY
        first = self.retain(now, max_transactions=1)
        self.assertEqual((first.transactions, first.retained_since["sample"], first.more), (1, BASE + 3600, True))
        self.assertEqual(first.deleted["sample"], 2 * 12)  # jedna hodina historie
        full = self.retain(now)
        self.assertEqual((full.transactions, full.more), (24, True))  # limit běhu
        self.assertEqual(full.retained_since["sample"], BASE + 25 * 3600)
        rest = self.retain(now)
        self.assertEqual((rest.more, rest.retained_since["sample"]), (False, BASE + 40 * 3600 - 300))

    def test_more_is_false_when_limit_exactly_finishes_work(self):
        self.seed_samples(BASE, BASE + 24 * 3600 + 600, step=600)
        self.aggregate_until(BASE + 24 * 3600)
        outcome = self.retain(BASE + 24 * 3600 + 31 * DAY)
        self.assertEqual((outcome.transactions, outcome.more, outcome.retained_since["sample"]), (24, False, BASE + 24 * 3600))

    def test_tables_take_turns_within_run_limit(self):
        self.seed_samples(BASE, BASE + 30 * 3600, step=600)
        self.aggregate_until(BASE + 30 * 3600 - 600)
        conn = self.database.connection
        conn.executemany("INSERT INTO event (ts, ts_quality, kind, severity, message) VALUES (?, 'synced', 'x', 'INFO', 'e')",
                         [(BASE + 3600 * h,) for h in range(30)])
        outcome = self.retain(BASE + 800 * DAY)
        self.assertEqual(outcome.transactions, 24)
        self.assertGreater(outcome.deleted["event"], 0)  # dluh vzorků nezablokuje ostatní tabulky
        self.assertGreater(outcome.deleted["sample"], 0)

    def test_many_rapid_transitions_are_deleted_hour_by_hour(self):
        conn = self.database.connection
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("INSERT INTO check_change (ts, ts_quality, run_id, check_id, from_state, to_state) "
                         "VALUES (?, 'synced', ?, ?, ?, ?)",
                         [(BASE + second, self.run_id, f"check.{n}", "INFO" if second % 2 else "CRITICAL",
                           "CRITICAL" if second % 2 else "INFO") for second in range(2 * 3600) for n in range(5)])
        conn.execute("COMMIT")
        now = BASE + 730 * DAY + 3 * 3600
        first = self.retain(now, max_transactions=1)
        self.assertEqual((first.deleted["check_change"], first.retained_since["check_change"]), (5 * 3600, BASE + 3600))
        self.assertEqual(self.query("SELECT count(*), min(ts) FROM check_change")[0], (5 * 3600, BASE + 3600))
        self.drain(now)
        self.assertEqual(self.query("SELECT count(*) FROM check_change")[0][0], 0)

    def test_long_history_is_trimmed_to_bounds(self):
        cpu = self.metric(CPU_TEMP_ID)
        now = BASE + 11 * 365 * DAY
        conn = self.database.connection
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("INSERT INTO agg_1h VALUES (?, ?, 12, 12, 1.0, 1.0, 12.0)",
                         [(cpu, BASE + h * 3600) for h in range(0, 11 * 365 * 24, 24)])  # 11 let, jeden řádek denně
        conn.executemany("INSERT INTO event (ts, ts_quality, kind, severity, message) VALUES (?, 'synced', 'x', 'INFO', 'e')",
                         [(now - day * DAY,) for day in range(0, 800, 7)])
        conn.execute("COMMIT")
        for _ in range(30):
            if not self.retain(now).more:
                break
        hour_cut = history._years_before(now, 10) // 3600 * 3600
        self.assertGreaterEqual(self.query("SELECT min(bucket) FROM agg_1h")[0][0], hour_cut)
        self.assertEqual(self.query("SELECT count(*) FROM agg_1h WHERE bucket < ?", (hour_cut,))[0][0], 0)
        self.assertEqual(self.query("SELECT count(*) FROM event WHERE kind = 'x' AND ts < ?", (now - 730 * DAY,))[0][0], 0)
        self.assertEqual(self.query("SELECT count(*) FROM event WHERE kind = 'x'")[0][0], len(range(0, 731, 7)))


class RetentionClockTest(RetentionTest):
    def setUp(self):
        super().setUp()
        self.seed_samples(BASE, BASE + 2 * DAY)
        self.aggregate_until(BASE + 2 * DAY - 600, BASE + DAY)
        self.now = BASE + 40 * DAY

    def assertBlocked(self, reason, now=None, clock=STABLE):
        before = self.counts()
        self.assertEqual(self.plan(now or self.now, clock).blocked, reason)
        outcome = self.retain(now or self.now, clock)
        self.assertEqual((outcome.blocked, outcome.transactions, self.counts()), (reason, 0, before))

    def test_clock_state_contract(self):
        with self.assertRaises(TypeError):
            self.recorder.retain(at(self.now), None)  # chybějící informace není „čas je v pořádku“
        with self.assertRaises(TypeError):
            ClockState("ano", None)
        for build in (lambda: ClockState.synced_for(None), lambda: ClockState(False, 100), lambda: ClockState(True, -1),
                      lambda: ClockState(True, True)):
            with self.subTest(build=build), self.assertRaises(ValueError):
                build()

    def test_untrusted_or_unstable_time_deletes_nothing(self):
        self.assertBlocked("time_unsynced", clock=ClockState.unknown())
        self.assertBlocked("clock_unstable", clock=ClockState(True, None))  # synchronizovaný, stabilita nedoložena
        self.assertBlocked("clock_unstable", clock=ClockState.synced_for(DAY - 1))
        self.assertBlocked("time_unsynced", now=1_700_000_000)  # čas před 2026

    def test_recent_clock_jump_pauses_for_a_day(self):
        self.write(events=[Event(self.now - 3600, "synced", "clock.jump", "WARNING", "skok")])
        self.assertBlocked("clock_jump_recent")
        self.assertBlocked("clock_jump_recent", now=self.now - 3600 + DAY - 1)
        self.assertIsNone(self.retain(self.now - 3600 + DAY).blocked)  # po 24 h znovu

    def test_data_newer_than_now_blocks(self):
        self.write(events=[Event(self.now + 60, "synced", "test.future", "INFO", "z budoucnosti")])
        self.assertBlocked("clock_behind_data")

    def test_execution_rechecks_time_inside_transaction(self):
        plan = self.plan(self.now)
        self.assertIsNone(plan.blocked)
        self.write(events=[Event(self.now - 10, "synced", "clock.jump", "WARNING", "skok po plánu")])
        before = self.counts()
        outcome = self.retain(self.now, plan=plan)  # plán bez blokace se neuplatní slepě
        self.assertEqual((outcome.blocked, self.counts()), ("clock_jump_recent", before))


class RetentionHistoryTest(RetentionTest):
    """R4 po retenci: přechody po hranici zůstávají přesné, začátek před hranicí se nedomýšlí."""

    def episodes(self, check_id, state):
        return self.query(doc_block("state-episodes"), {"check_id": check_id, "state": state,
                                                         "start": 0, "end": BASE + 2000 * DAY})

    def test_critical_episodes_across_cutoff(self):
        cycles = [[result(CPU_TEMP_ID, State.INFO, 40.0, CELSIUS, ts=BASE)]]
        for k in range(1, 361):  # scénář A: změna každých 10 s
            state = State.CRITICAL if ((k + 1) // 2) % 2 else State.INFO
            cycles.append([result(CPU_TEMP_ID, state, 90.0 if state is State.CRITICAL else 40.0, CELSIUS, ts=BASE + 5 * k)])
        for first in range(0, len(cycles), 60):
            self.write(*cycles[first:first + 60])
        entries = [BASE + 5 + 20 * j for j in range(90)]
        self.assertEqual(self.episodes(CPU_TEMP_ID, "CRITICAL"), [(e, e + 10, 10, 0) for e in entries])
        self.retain(BASE + 730 * DAY + 885)  # hranice přesně na vzniku 45. epizody: zůstává
        self.assertEqual(self.episodes(CPU_TEMP_ID, "CRITICAL"), [(e, e + 10, 10, 0) for e in entries[44:]])
        self.retain(BASE + 730 * DAY + 890)  # hranice uprostřed epizody: začátek smazán
        self.assertEqual(self.episodes(CPU_TEMP_ID, "CRITICAL"),
                         [(None, entries[44] + 10, None, 0)] + [(e, e + 10, 10, 0) for e in entries[45:]])
        self.assertEqual(history.retained_since(self.database.connection)["check_change"], BASE + 890)

    def test_unknown_episode_across_cutoff(self):
        for ts, state in ((BASE, State.INFO), (BASE + 20, State.UNKNOWN), (BASE + 40, State.UNKNOWN), (BASE + 60, State.INFO)):
            value = None if state is State.UNKNOWN else 50.0
            self.write([result(CPU_TEMP_ID, state, value, None if value is None else CELSIUS, ts=ts)])
        self.assertEqual(self.episodes(CPU_TEMP_ID, "UNKNOWN"), [(BASE + 20, BASE + 60, 40, 0)])
        self.retain(BASE + 730 * DAY + 30)
        self.assertEqual(self.episodes(CPU_TEMP_ID, "UNKNOWN"), [(None, BASE + 60, None, 0)])  # obnova zůstala, vznik ne
        self.assertEqual(self.episodes(CPU_TEMP_ID, "INFO"), [(BASE + 60, None, None, 0)])

    def test_link_episodes_do_not_invent_start(self):
        sequence = [(0, State.INFO, "UP"), (10, State.INFO, "DOWN"), (20, State.INFO, "UP"), (30, State.WARNING, "UP"),
                    (40, State.INFO, "UP"), (50, State.INFO, "DOWN"), (60, State.INFO, "UP")]
        for offset, state, link in sequence:
            self.write([result(LINK_ID, state, ts=BASE + offset, details={INTERFACE: "eth0", LINK: link})])
        query = lambda value: self.query(doc_block("details-episodes"), {"check_id": LINK_ID, "key": "link", "value": value,
                                                                         "start": 0, "end": BASE + 2000 * DAY})
        self.assertEqual(query("UP"), [(BASE, BASE + 10, 10, 1), (BASE + 20, BASE + 50, 30, 0), (BASE + 60, None, None, 0)])
        self.retain(BASE + 730 * DAY + 25)  # první zachovaný řádek (WARNING) linku nemění
        self.assertEqual(query("UP"), [(None, BASE + 50, None, 0), (BASE + 60, None, None, 0)])  # začátek neznámý
        self.assertEqual(query("DOWN"), [(BASE + 50, BASE + 60, 10, 0)])


class RetentionFailureTest(RetentionTest):
    def setUp(self):
        super().setUp()
        self.seed_samples(BASE, BASE + 3 * 3600, step=300)
        self.aggregate_until(BASE + 3 * 3600 - 300, BASE + 2 * 3600)
        self.now = BASE + 3 * 3600 + 31 * DAY

    def unchanged(self, before, meta=None):
        self.assertEqual(self.counts(), before)
        self.assertEqual(history.retained_since(self.database.connection), meta or dict.fromkeys(TABLES))
        self.assertFalse(self.database.connection.in_transaction)

    def intercept(self, raiser):
        real = self.database.connection

        class Failing(Passthrough):
            def execute(self, sql, *args):
                if sql == history.RETENTION_SAMPLE_SQL:
                    raise raiser()
                return super().execute(sql, *args)

        self.database.connection = Failing(real)
        return real

    def test_crash_before_commit_in_child_process(self):
        before, state = self.counts(), self.done()
        self.database.close()
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        code = subprocess.run([sys.executable, "-c", CHILD, "retain", str(self.db), HOST, str(self.now)],
                              env=env, capture_output=True, text=True, timeout=60).returncode
        self.assertEqual(code, 76)
        self.reopen(self.now + 60)
        after = self.counts()
        self.assertEqual({table: after[table] for table in ("sample", "agg_5m", "agg_1h", "check_change")},
                         {table: before[table] for table in ("sample", "agg_5m", "agg_1h", "check_change")})
        self.assertEqual((history.retained_since(self.database.connection), self.done()), (dict.fromkeys(TABLES), state))
        self.assertGreater(self.retain(self.now + 60).deleted["sample"], 0)

    def test_exception_mid_delete_rolls_back(self):
        conn = self.database.connection
        calls = []

        def fail_second():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("selhání uprostřed DELETE")
            return 0

        conn.create_function("fail_second", 0, fail_second)
        conn.execute("CREATE TEMP TRIGGER fail_mid BEFORE DELETE ON main.sample BEGIN SELECT fail_second(); END")
        before = self.counts()
        try:
            self.assertRefused("write", self.retain, self.now)
        finally:
            conn.execute("DROP TRIGGER temp.fail_mid")
        self.assertEqual(len(calls), 2)  # první řádek už byl smazaný, druhý selhal
        self.unchanged(before)
        self.assertEqual(self.retain(self.now, max_transactions=1).deleted["sample"], 2 * 12)

    def test_ctrl_c_and_disk_full_roll_back(self):
        before = self.counts()
        for raiser, expected in ((KeyboardInterrupt, KeyboardInterrupt),
                                 (lambda: sqlite3.OperationalError("database or disk is full"), HistoryError)):
            with self.subTest(raiser=raiser):
                real = self.intercept(raiser)
                try:
                    with self.assertRaises(expected):
                        self.retain(self.now)
                finally:
                    self.database.connection = real
                self.unchanged(before)

    def test_incomplete_delete_is_never_reported_as_success(self):
        conn = self.database.connection
        conn.execute("CREATE TEMP TRIGGER keep_one BEFORE DELETE ON main.sample WHEN OLD.ts = ? "
                     "BEGIN SELECT RAISE(IGNORE); END".replace("?", str(BASE + 600)))  # řádek „nesmazatelný“
        before = self.counts()
        try:
            self.assertRefused("write", self.retain, self.now, max_transactions=1)  # rowcount ≠ spočítané kandidáty
        finally:
            conn.execute("DROP TRIGGER temp.keep_one")
        self.unchanged(before)  # nic smazáno, hranice nezapsána

    def test_sqlite_busy_deletes_nothing(self):
        conn = self.database.connection
        conn.execute("PRAGMA busy_timeout = 0")  # bez skutečného čekání v testu
        other = raw(self.db)
        try:
            other.execute("BEGIN IMMEDIATE")  # zámek zápisu drží jiné spojení (ruční sqlite3)
            before = self.counts()
            error = self.assertRefused("write", self.retain, self.now)
            self.assertIn("locked", str(error))
            other.execute("ROLLBACK")
        finally:
            other.close()
            conn.execute("PRAGMA busy_timeout = 5000")
        self.unchanged(before)
        self.assertGreater(self.retain(self.now).deleted["sample"], 0)

    def test_uncertain_commit_is_verified_not_repeated(self):
        real = self.database.connection
        self.database.connection = FlakyCommit(real)  # COMMIT proběhne, volající dostane chybu
        try:
            outcome = self.retain(self.now, max_transactions=1)
        finally:
            self.database.connection = real
        self.assertEqual((outcome.transactions, outcome.verified_after_uncertain_commit, outcome.deleted["sample"]),
                         (1, 1, 2 * 12))
        self.assertEqual(self.query("SELECT min(ts) FROM sample")[0][0], BASE + 3600)

        class LostCommit(Passthrough):
            def execute(self, sql, *args):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("disk I/O error (COMMIT neproběhl)")
                return super().execute(sql, *args)

        before = self.counts()
        self.database.connection = LostCommit(real)
        try:
            self.assertRefused("write", self.retain, self.now, max_transactions=1)  # stav ověřen: nic nesmazáno
        finally:
            self.database.connection = real
        self.assertEqual(self.counts(), before)
        self.assertFalse(real.in_transaction)

    def test_restart_between_steps_continues(self):
        self.retain(self.now, max_transactions=1)
        self.reopen(self.now + 60)
        self.drain(self.now + 60)
        self.assertEqual(self.query("SELECT min(ts) FROM sample")[0][0], BASE + 3 * 3600 - 300)
        self.assertEqual(history.retained_since(self.database.connection)["sample"], BASE + 3 * 3600 - 300)

    def test_reader_keeps_its_snapshot_during_retention(self):
        with history.open_reader(self.db) as reader:
            reader.connection.execute("BEGIN")
            seen = reader.connection.execute("SELECT count(*) FROM sample").fetchone()[0]
            outcome = self.retain(self.now)
            self.assertGreater(outcome.deleted["sample"], 0)
            self.assertEqual(reader.connection.execute("SELECT count(*) FROM sample").fetchone()[0], seen)
            reader.connection.execute("COMMIT")
            self.assertEqual(reader.connection.execute("SELECT count(*) FROM sample").fetchone()[0],
                             seen - outcome.deleted["sample"])


class RetentionMarkTest(RetentionTest):
    """retained_since.*: začátek zachované historie po retenci, ne záruka souvislé řady."""

    def old_rows_everywhere(self, now):
        cpu = self.metric(CPU_TEMP_ID)
        conn = self.database.connection
        self.seed_samples(BASE, BASE + 6 * 3600, step=600)
        self.aggregate_until(BASE + 6 * 3600 - 600, BASE + 5 * 3600)
        conn.execute("BEGIN IMMEDIATE")
        hour_cut = history._years_before(now, 10) // 3600 * 3600
        conn.executemany("INSERT INTO agg_1h VALUES (?, ?, 1, 1, 1.0, 1.0, 1.0)", [(cpu, hour_cut - 3600 * h) for h in range(1, 4)])
        conn.executemany("INSERT INTO check_change (ts, ts_quality, run_id, check_id, from_state, to_state) "
                         "VALUES (?, 'synced', ?, 'x.check', 'INFO', 'WARNING')",
                         [(now - 730 * DAY - 600 * k, self.run_id) for k in range(1, 9)])
        conn.executemany("INSERT INTO event (ts, ts_quality, kind, severity, message) VALUES (?, 'synced', 'x', 'INFO', 'e')",
                         [(now - 730 * DAY - 900 * k,) for k in range(1, 9)])
        conn.execute("COMMIT")

    def assertMarksConsistent(self):
        marks = history.retained_since(self.database.connection)
        column = {"sample": "ts", "agg_5m": "bucket", "agg_1h": "bucket", "check_change": "ts", "event": "ts"}
        for table, mark in marks.items():
            if mark is not None:  # pod hranicí nezbyl žádný řádek: hranice nepřeskočila nesmazaná data
                self.assertEqual(self.query(f"SELECT count(*) FROM {table} WHERE {column[table]} < ?", (mark,))[0][0], 0, table)
        return marks

    def test_marks_follow_each_step_per_table_without_skipping(self):
        now = BASE + 3 * 365 * DAY
        self.old_rows_everywhere(now)
        cutoffs = {entry.table: entry.cutoff for entry in self.plan(now).tables}
        previous = dict.fromkeys(TABLES)
        while True:
            oldest = {entry.table: entry.oldest for entry in self.plan(now).tables}
            outcome = self.retain(now, max_transactions=1)
            marks = self.assertMarksConsistent()
            changed = [table for table in TABLES if marks[table] != previous[table]]
            self.assertLessEqual(len(changed), 1)  # jedna transakce = jedna tabulka
            for table in changed:
                self.assertLessEqual(marks[table] - oldest[table] // 3600 * 3600, 3600, table)  # nejvýš hodina dat
                self.assertGreater(marks[table], previous[table] or 0)  # nikdy neklesá
                self.assertLessEqual(marks[table], cutoffs[table])  # nikdy za vlastní hranicí tabulky
            previous = marks
            if not outcome.more:
                break
        self.assertTrue(all(previous[table] is not None for table in TABLES))  # každá tabulka má vlastní hranici
        self.assertEqual({table: self.plan(now).table(table).candidates for table in TABLES}, dict.fromkeys(TABLES, 0))

    def test_mark_is_start_of_retained_history_not_continuity(self):
        self.seed_samples(BASE, BASE + 3 * 3600, step=600)
        self.seed_samples(BASE + 5 * 3600, BASE + 8 * 3600, step=600)  # mezera 2 h v zápisu
        self.aggregate_until(BASE + 8 * 3600 - 600, BASE + 7 * 3600)
        self.drain(BASE + 2 * 3600 + 30 * DAY)
        mark = history.retained_since(self.database.connection)["sample"]
        self.assertEqual(mark, BASE + 2 * 3600)
        metric = self.query("SELECT id FROM metric WHERE check_id = ?", (CPU_TEMP_ID,))[0][0]
        gaps = self.query(doc_block("gaps"), {"metric_id": metric, "start": mark, "end": BASE + 8 * 3600, "max_step": 1200})
        self.assertEqual(gaps, [(BASE + 3 * 3600 - 600, BASE + 5 * 3600)])  # mezera po hranici zůstává mezerou

    def test_mark_never_decreases(self):
        self.seed_samples(BASE, BASE + 3 * 3600, step=600)
        self.aggregate_until(BASE + 3 * 3600 - 600, BASE + 2 * 3600)
        now = BASE + 2 * 3600 + 30 * DAY
        self.drain(now)
        mark = history.retained_since(self.database.connection)["sample"]
        older_plan = self.plan(now - 3600)
        for outcome in (self.retain(now - 5 * DAY), self.retain(now, plan=older_plan)):  # dřívější čas, starší plán
            self.assertEqual((outcome.transactions, outcome.retained_since["sample"]), (0, mark))


class RetentionEpisodeTest(RetentionTest):
    """Epizody po retenci skutečným SQL z dokumentace: žádný vymyšlený začátek, konec ani délka."""

    def episodes(self, check_id, state):
        return self.query(doc_block("state-episodes"), {"check_id": check_id, "state": state, "start": 0, "end": BASE + 2000 * DAY})

    def links(self, value):
        return self.query(doc_block("details-episodes"), {"check_id": LINK_ID, "key": "link", "value": value,
                                                          "start": 0, "end": BASE + 2000 * DAY})

    def unknown(self, check_id):
        mark = history.retained_since(self.database.connection)["check_change"]
        return self.query(doc_block("retention-unknown"), {"check_id": check_id, "retained": mark, "end": BASE + 2000 * DAY})

    def assertCompatible(self, before, after):
        """Po retenci jen podmnožina úplné historie: známý začátek = stejná epizoda, neznámý = stejný konec."""
        for start, end, duration, flag in after:
            if start is None:
                self.assertIsNone(duration)
                self.assertIn(end, [row[1] for row in before])
            else:
                self.assertIn((start, end, duration, flag), before)

    def write_states(self, check_id, sequence):
        for ts, state in sequence:
            value = None if state is State.UNKNOWN else 50.0
            self.write([result(check_id, state, value, None if value is None else CELSIUS, ts=ts)])

    def test_episode_started_before_cutoff_without_further_transition(self):
        self.write_states(CPU_TEMP_ID, [(BASE, State.INFO), (BASE + 10, State.CRITICAL)])
        self.write([result(LINK_ID, State.INFO, ts=BASE, details={INTERFACE: "eth0", LINK: "UP"})])
        for k in range(1, 4):  # po hranici se mění jen linka, stav INFO trvá
            self.write([result(LINK_ID, State.INFO, ts=BASE + 100 * k, details={INTERFACE: "eth0", LINK: ("DOWN", "UP")[k % 2]})])
        self.retain(BASE + 730 * DAY + 50)
        self.assertEqual(self.episodes(CPU_TEMP_ID, "CRITICAL"), [])  # stav po hranici neznáme...
        self.assertEqual(self.unknown(CPU_TEMP_ID), [(self.run_id, BASE + 50, None, None)])  # ...a výsledek to přizná
        self.assertEqual(self.episodes(LINK_ID, "INFO"), [(None, None, None, 0)])  # trvá, začátek i konec neznámé
        self.assertEqual(self.unknown(LINK_ID), [(self.run_id, BASE + 50, BASE + 200, "INFO")])  # stav znám, údaje ne
        # (BASE + 100 hlásí UP jako výchozí řádek: beze změny žádný řádek, první zachovaný je BASE + 200)

    def test_episode_starting_exactly_on_cutoff_keeps_its_start(self):
        self.write_states(CPU_TEMP_ID, [(BASE, State.INFO), (BASE + 40, State.CRITICAL), (BASE + 70, State.INFO)])
        before = self.episodes(CPU_TEMP_ID, "CRITICAL")
        self.retain(BASE + 730 * DAY + 40)
        self.assertEqual(self.episodes(CPU_TEMP_ID, "CRITICAL"), before)  # (BASE + 40, BASE + 70, 30, 0)
        self.assertEqual(self.episodes(CPU_TEMP_ID, "INFO"), [(None, BASE + 40, None, 0), (BASE + 70, None, None, 0)])

    def test_same_second_transitions_on_the_cutoff(self):
        self.write([result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE)])
        batch = self.recorder.begin_batch()  # dva přechody v jedné sekundě BASE + 20
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.CRITICAL, 90.0, CELSIUS, ts=BASE + 20.1, cycle=BASE + 20.1)])
        self.recorder.add_results(batch, [result(CPU_TEMP_ID, State.INFO, 50.0, CELSIUS, ts=BASE + 20.6, cycle=BASE + 20.6)])
        self.recorder.write(batch)
        self.write_states(CPU_TEMP_ID, [(BASE + 50, State.WARNING)])
        full = self.episodes(CPU_TEMP_ID, "CRITICAL")
        self.assertEqual(full, [(BASE + 20, BASE + 20, 0, 0)])
        self.retain(BASE + 730 * DAY + 20)  # hranice přesně na sekundě obou přechodů: zůstanou oba
        self.assertEqual(self.episodes(CPU_TEMP_ID, "CRITICAL"), full)
        self.assertEqual(self.episodes(CPU_TEMP_ID, "INFO"), [(None, BASE + 20, None, 0), (BASE + 20, BASE + 50, 30, 0)])
        self.retain(BASE + 730 * DAY + 21)  # o sekundu dál: zmizí oba, nikdy jen jeden
        self.assertEqual(self.episodes(CPU_TEMP_ID, "CRITICAL"), [])
        self.assertEqual(self.episodes(CPU_TEMP_ID, "INFO"), [(None, BASE + 50, None, 0)])

    def test_unknown_before_first_retained_row(self):
        self.write_states(CPU_TEMP_ID, [(BASE, State.INFO), (BASE + 20, State.UNKNOWN), (BASE + 60, State.INFO),
                                        (BASE + 90, State.CRITICAL), (BASE + 95, State.INFO)])
        before = {state: self.episodes(CPU_TEMP_ID, state) for state in ("UNKNOWN", "INFO", "CRITICAL")}
        self.retain(BASE + 730 * DAY + 30)
        after = {state: self.episodes(CPU_TEMP_ID, state) for state in ("UNKNOWN", "INFO", "CRITICAL")}
        self.assertEqual(after["UNKNOWN"], [(None, BASE + 60, None, 0)])
        self.assertEqual(after["CRITICAL"], before["CRITICAL"])
        for state in after:
            self.assertCompatible(before[state], after[state])

    def test_link_up_down_start_and_end(self):
        sequence = [(0, "UP"), (10, "DOWN"), (20, "UP"), (40, "DOWN"), (70, "UP")]
        for offset, link in sequence:
            self.write([result(LINK_ID, State.INFO, ts=BASE + offset, details={INTERFACE: "eth0", LINK: link})])
        before = {value: self.links(value) for value in ("UP", "DOWN")}
        self.retain(BASE + 730 * DAY + 15)  # uprostřed DOWN: hodnota před prvním řádkem (BASE + 20) není známá
        self.assertEqual(self.links("DOWN"), [(BASE + 40, BASE + 70, 30, 0)])  # starší DOWN se nedomýšlí...
        self.assertEqual(self.links("UP"), [(None, BASE + 40, None, 0), (BASE + 70, None, None, 0)])
        self.assertEqual(self.unknown(LINK_ID), [(self.run_id, BASE + 15, BASE + 20, "INFO")])  # ...výsledek to přizná
        for value in before:
            self.assertCompatible(before[value], self.links(value))
        self.retain(BASE + 730 * DAY + 45)  # zůstane jediný řádek: UP trvá, začátek ani konec neznámé
        self.assertEqual((self.links("UP"), self.links("DOWN")), ([(None, None, None, 0)], []))
        self.assertEqual(self.unknown(LINK_ID), [(self.run_id, BASE + 45, BASE + 70, "INFO")])


class RetentionBoundaryTest(RetentionTest):
    def test_ten_years_handles_leap_days_conservatively(self):
        for moment in (datetime(2036, 2, 29, 12, 34, 56, tzinfo=timezone.utc), datetime(2036, 3, 1, tzinfo=timezone.utc),
                       datetime(2037, 2, 28, 23, 59, 59, tzinfo=timezone.utc), datetime(2040, 2, 29, 0, 30, tzinfo=timezone.utc),
                       datetime(2036, 12, 31, 23, 59, 59, tzinfo=timezone.utc)):
            with self.subTest(now=moment.isoformat()):
                now = math.floor(moment.timestamp())
                cutoff = self.plan(now).table("agg_1h").time_cutoff
                self.assertEqual(cutoff % 3600, 0)  # celé hodinové okno
                if moment.month == 2 and moment.day == 29:
                    later = moment.replace(year=moment.year - 10, month=3, day=1)  # „10 let“ může znamenat i 1. března
                    earlier = moment.replace(year=moment.year - 10, day=28)
                    self.assertLessEqual(cutoff, earlier.timestamp())
                    self.assertLess(cutoff, later.timestamp())
                else:
                    self.assertLessEqual(cutoff, moment.replace(year=moment.year - 10).timestamp())
                self.assertLessEqual(cutoff, now - 3650 * DAY - 2 * DAY)  # 10 kalendářních let má aspoň 2 přestupné dny

    def test_data_newer_than_now_in_any_table_blocks(self):
        self.seed_samples(BASE, BASE + 2 * DAY)
        self.aggregate_until(BASE + 2 * DAY - 600, BASE + DAY)
        now = BASE + 40 * DAY
        conn = self.database.connection
        high = conn.execute("SELECT last_write_utc FROM run WHERE id = ?", (self.run_id,)).fetchone()[0]
        sources = (("run", "UPDATE run SET last_write_utc = ? WHERE id = %d" % self.run_id,
                    "UPDATE run SET last_write_utc = %d WHERE id = %d" % (high, self.run_id)),
                   ("check_change", "INSERT INTO check_change (ts, ts_quality, run_id, check_id, to_state) "
                    "VALUES (?, 'synced', %d, 'x', 'INFO')" % self.run_id, "DELETE FROM check_change WHERE check_id = 'x'"),
                   ("event", "INSERT INTO event (ts, ts_quality, kind, severity, message) VALUES (?, 'synced', 'x', 'INFO', 'z')",
                    "DELETE FROM event WHERE kind = 'x'"))
        for source, apply, revert in sources:
            with self.subTest(source=source):
                conn.execute(apply, (now + 1,))  # záznam z budoucnosti
                before = self.counts()
                self.assertEqual((self.retain(now).blocked, self.counts()), ("clock_behind_data", before))
                conn.execute(revert)
        self.assertIsNone(self.retain(now).blocked)

    def test_dry_run_on_read_only_open_changes_no_user_data(self):
        self.seed_samples(BASE, BASE + 2 * DAY)
        self.aggregate_until(BASE + DAY, BASE + DAY)
        self.database.close()  # bez zapisovače a po checkpointu
        main = digest(self.db)
        with history.open_reader(self.db) as reader:
            plan = history.retention_plan(reader.connection, at(BASE + 40 * DAY), STABLE)
        self.assertGreater(plan.table("sample").candidates, 0)
        self.assertEqual(digest(self.db), main)  # hlavní soubor beze změny; -wal/-shm jsou pomocné soubory SQLite
        self.database = history.open_writer(self.db, "test", HOST)


class HelpersTest(unittest.TestCase):
    def test_check_sqlite_versions(self):
        history.check_sqlite()  # knihovna tohoto počítače
        history.check_sqlite((3, 38, 0))
        for version in ((3, 37, 2), (3, 7, 17)):
            with self.subTest(version=version), self.assertRaises(HistoryError) as caught:
                history.check_sqlite(version)
            self.assertEqual(caught.exception.reason, "sqlite")

    def test_local_host_id_hashes_machine_id(self):
        with tempfile.TemporaryDirectory() as folder:
            machine_id = Path(folder) / "machine-id"
            machine_id.write_text("0123456789abcdef0123456789abcdef\n")
            expected = hashlib.sha256(b"0123456789abcdef0123456789abcdef").hexdigest()[:16]
            self.assertEqual(history.local_host_id(machine_id), expected)
            machine_id.write_text("neplatne")
            with self.assertRaises(HistoryError):
                history.local_host_id(machine_id)
            with self.assertRaises(HistoryError):
                history.local_host_id(Path(folder) / "chybi")

    def test_lock_path(self):
        self.assertEqual(history.lock_path("/x/history.sqlite3"), Path("/x/history.lock"))
        with self.assertRaises(HistoryError):
            history.lock_path("/x/history.lock")

    def test_api_has_no_implicit_paths(self):
        for function in (history.create_database, history.open_writer, history.open_reader):
            parameters = inspect.signature(function).parameters
            self.assertIs(parameters["path"].default, inspect.Parameter.empty, function.__name__)
        for function in (history.create_database, history.open_writer):
            parameters = inspect.signature(function).parameters
            self.assertIs(parameters["role"].default, inspect.Parameter.empty)
            self.assertIs(parameters["host_id"].default, inspect.Parameter.empty)
        source = Path(history.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/srv/nexus", source)
        self.assertNotIn("makedirs", source)
        self.assertNotIn("mkdir", source)


if __name__ == "__main__":
    unittest.main()
