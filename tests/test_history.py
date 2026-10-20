"""Testy základu SQLite historie (M3.1a): vytvoření, otevření, ověření a zámek.

Vše jen v tempfile.TemporaryDirectory. Simulace chyb upravují výhradně kopie
databáze vytvořené testem; žádná produkční cesta, žádné skutečné čekání.
"""

import fcntl
import hashlib
import inspect
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexusstat import history
from nexusstat.history import HistoryError

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
