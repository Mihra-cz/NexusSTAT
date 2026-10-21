"""Testy vlákna zapisovače historie (M3.3a).

Vše nad dočasnými databázemi v tempfile.TemporaryDirectory, se simulovanými
hodinami (monotónní i UTC) a řízenou synchronizací vláken. Žádné skutečné
minutové čekání: čas se posouvá ručně a vlákno se probouzí přes wake(); na
vlákno se čeká s limitem WAIT, který se při správné funkci nevyčerpá.
Chyby SQLite se vyvolávají ve vlákně zapisovače obalem spojení nebo skutečně
(zámek jiného spojení, max_page_count, práva souboru).
"""

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

from nexusstat import history
from nexusstat.core import CheckResult, Measurement, State
from nexusstat import history_worker
from nexusstat.history_worker import DEGRADED, FAILED, RUNNING, STOPPED, HistoryWorker, classify
from nexusstat.network_monitor import INTERFACE, LINK, LINK_ID
from nexusstat.system_monitor import CELSIUS, CPU_TEMP_ID, CPU_USAGE_ID, PERCENT

REPO = Path(__file__).resolve().parent.parent
HOST = "0123456789abcdef"
CREATED = 1_800_001_234
BASE = 1_800_003_600  # synchronizovaný čas začátku testů (po vytvoření databáze)
WRONG = 1_740_000_000  # chybný čas systému před synchronizací (2025, před MIN_TRUSTED_UTC)
WAIT = 5.0  # skutečný limit čekání na vlákno; při správné funkci se nevyčerpá


def sqlite_error(cls, name, message="simulace"):
    error = cls(f"{message} ({name})")
    error.sqlite_errorname = name
    return error


class Clock:
    """Simulované hodiny: monotónní čas a systémové UTC = monotónní + posun."""

    def __init__(self, mono=1000.0, utc=BASE):
        self._lock = threading.Lock()
        self.mono = mono
        self.offset = utc - mono

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


class Faults:
    """Řízené chyby SQLite ve vlákně zapisovače: pravidla podle textu příkazu."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rules = []  # [match(sql) -> bool, akce, zbývající počet nebo None]
        self.closed_in = []  # vlákna, ve kterých se spojení zavřelo

    def add(self, match, action, times=None):
        with self.lock:
            self.rules.append([match, action, times])

    def clear(self):
        with self.lock:
            self.rules.clear()

    def pick(self, sql):
        with self.lock:
            for rule in self.rules:
                if rule[0](sql) and rule[2] != 0:
                    if rule[2] is not None:
                        rule[2] -= 1
                    return rule[1]
        return None


class FaultyConnection:
    """Obal spojení zapisovače; akce dostane (spojení, sql, provést) a rozhodne, co se stane."""

    def __init__(self, connection, faults):
        self._connection = connection
        self._faults = faults

    def execute(self, sql, *args):
        action = self._faults.pick(sql)
        run = lambda: self._connection.execute(sql, *args)
        return run() if action is None else action(self._connection, sql, run)

    def executemany(self, sql, *args):
        action = self._faults.pick(sql)
        run = lambda: self._connection.executemany(sql, *args)
        return run() if action is None else action(self._connection, sql, run)

    def close(self):
        self._faults.closed_in.append(threading.get_ident())
        return self._connection.close()

    def __getattr__(self, name):
        return getattr(self._connection, name)


def fail(error):
    def action(connection, sql, run):
        raise error
    return action


def after(error):
    """Příkaz proběhne, volající přesto dostane chybu (nejistý COMMIT)."""
    def action(connection, sql, run):
        run()
        raise error
    return action


def is_commit(sql):
    return sql == "COMMIT"


def batch_commit(faults_flag):
    """COMMIT jen u dávky (ne start_run): pozná se podle příznaku nastaveného před zápisem."""
    return lambda sql: sql == "COMMIT" and faults_flag.is_set()


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.folder = Path(self._temp.name)
        self.db = self.folder / "history.sqlite3"
        history.create_database(self.db, "test", HOST, now_utc=CREATED)
        self.clock = Clock()
        self.faults = Faults()
        self.open_threads = []
        self.open_error = None
        self.on_open = None
        self.workers = []
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        for worker in self.workers:
            worker.stop(timeout=WAIT)
        for path in (self.db, *history.companion_paths(self.db)):
            if path.exists():
                os.chmod(path, 0o644)
        self._temp.cleanup()

    def opener(self):
        self.open_threads.append(threading.get_ident())
        if self.open_error is not None:
            raise self.open_error
        database = history.open_writer(self.db, "test", HOST)
        if self.on_open is not None:
            self.on_open(database.connection)
        database.connection = FaultyConnection(database.connection, self.faults)
        return database

    def make(self, start=True, synced=True, **options):
        options.setdefault("interval_s", 10)
        worker = HistoryWorker(self.db, "test", HOST, app_version="test", monotonic=self.clock.monotonic,
                               utc_now=self.clock.utc, opener=options.pop("opener", self.opener), **options)
        self.workers.append(worker)
        if start:
            worker.start(time_synced=synced)
            self.assertTrue(worker.wait_until(lambda s: s.state != "STARTING", WAIT), worker.status())
        return worker

    def results(self, temp_state=State.INFO, temp=50.0, usage=5.0, link=None, simulated=False):
        observed, expires = self.clock.utc(), self.clock.monotonic() + 30
        out = [CheckResult(CPU_TEMP_ID, temp_state, f"CPU {temp}", observed, expires, simulated=simulated,
                           measurement=None if temp_state is State.UNKNOWN else Measurement(temp, CELSIUS)),
               CheckResult(CPU_USAGE_ID, State.INFO, f"CPU {usage} %", observed, expires, simulated=simulated,
                           measurement=Measurement(usage, PERCENT))]
        if link is not None:
            out.append(CheckResult(LINK_ID, State.INFO, f"eth0 {link}", observed, expires, simulated=simulated,
                                   details={INTERFACE: "eth0", LINK: link}))
        return out

    def submit(self, worker, synced=True, **result_options):
        return worker.submit(self.results(**result_options), self.clock.monotonic(), synced)

    def advance(self, worker, seconds):
        self.clock.advance(seconds)
        worker.wake()

    def settle(self, worker, predicate, message=""):
        self.assertTrue(worker.wait_until(predicate, WAIT), f"{message} {worker.status()}")

    def commits(self, worker, count):
        self.settle(worker, lambda s: s.commits >= count, f"čekám na {count} dávek")

    def assertNoCommitYet(self, worker, commits):
        worker.wake()
        time.sleep(0.05)  # dát vláknu příležitost (kdyby chybně zapsalo, je to vidět)
        self.assertEqual(worker.status().commits, commits)

    def query(self, sql, params=()):
        with history.open_reader(self.db) as reader:
            return reader.connection.execute(sql, params).fetchall()

    def samples(self, check_id=CPU_TEMP_ID):
        return self.query("SELECT s.ts, s.value FROM sample s JOIN metric m ON m.id = s.metric_id "
                          "WHERE m.check_id = ? ORDER BY s.ts", (check_id,))

    def changes(self, check_id=CPU_TEMP_ID):
        return self.query("SELECT ts, ts_quality, from_state, to_state FROM check_change WHERE check_id = ? ORDER BY id",
                          (check_id,))

    def events(self, kind):
        return [(ts, quality, severity, json.loads(data) if data else None) for ts, quality, severity, data in
                self.query("SELECT ts, ts_quality, severity, data FROM event WHERE kind = ? ORDER BY id", (kind,))]

    def hold(self, worker):
        """Zapisovač drží jednu dávku v COMMIT: fronty se plní deterministicky."""
        release, entered = self.block_commits()
        worker.submit_event(history.Event(BASE, "synced", "test.hold", "INFO", "drží"))
        self.assertTrue(entered.wait(WAIT))
        return release

    def block_commits(self):
        """COMMIT dávek visí, dokud test neuvolní událost (zaseknuté I/O)."""
        release, entered = threading.Event(), threading.Event()
        self.releases.append(release)

        def action(connection, sql, run):
            entered.set()
            release.wait(30)
            return run()

        self.faults.add(is_commit, action, times=1)
        return release, entered


class OrderTest(WorkerTest):
    """Pořadí a atomicita s řízenými bariérami (žádné skutečné čekání na čas)."""

    def gate_commits(self):
        """Každý COMMIT dávky počká na povolení testu; vrací (vstoupil, povol)."""
        entered, allow = threading.Semaphore(0), threading.Semaphore(0)
        self.releases.append(_Releaser(allow))

        def action(connection, sql, run):
            entered.release()
            allow.acquire(timeout=30)
            return run()

        self.faults.add(is_commit, action)
        return entered, allow

    def test_newer_data_never_overtakes_unconfirmed_batch(self):
        worker = self.make()
        entered, allow = self.gate_commits()
        self.submit(worker, temp_state=State.WARNING, temp=75.0)  # dávka 1 visí v COMMIT
        self.assertTrue(entered.acquire(timeout=WAIT))
        self.assertTrue(worker._cond.acquire(timeout=0.5))  # zámek front se během I/O nedrží
        worker._cond.release()
        self.assertEqual(worker.status().in_flight, 3)  # 1 měření + 2 výchozí změny
        for state in (State.CRITICAL, State.INFO):  # nová data během COMMIT
            self.advance(worker, 10)
            self.submit(worker, temp_state=state, temp=95.0 if state is State.CRITICAL else 40.0)
        self.assertEqual(worker.status().pending_changes, 2)
        allow.release()  # dávka 1 potvrzena
        self.assertTrue(entered.acquire(timeout=WAIT))  # dávka 2 (novější data) až po ní
        self.assertEqual(self.query("SELECT last_batch FROM run")[0][0], 1)
        self.assertEqual([row[3] for row in self.changes()], ["WARNING"])
        allow.release()
        self.commits(worker, 2)
        self.assertEqual([row[3] for row in self.changes()], ["WARNING", "CRITICAL", "INFO"])
        self.assertEqual([ts for ts, _ in self.samples()], [BASE, BASE + 10, BASE + 20])

    def test_sealed_batch_is_retried_unchanged_before_newer_data(self):
        worker = self.make()
        self.faults.add(lambda sql: sql == "BEGIN IMMEDIATE",
                        fail(sqlite_error(sqlite3.OperationalError, "SQLITE_BUSY", "database is locked")), times=1)
        self.submit(worker, temp_state=State.ERROR, temp=85.0)
        self.settle(worker, lambda s: s.error_kind == "busy")
        sealed = worker._batch
        self.advance(worker, 1)
        self.submit(worker, temp_state=State.CRITICAL, temp=95.0)  # novější data během odstupu
        self.assertIs(worker._batch, sealed)
        self.assertEqual(len(sealed.changes), 2)  # zapečetěná dávka nezískala nové položky
        self.advance(worker, 5)
        self.commits(worker, 2)
        rows = self.query("SELECT id, ts, to_state FROM check_change WHERE check_id = ? ORDER BY id", (CPU_TEMP_ID,))
        self.assertEqual([row[2] for row in rows], ["ERROR", "CRITICAL"])
        self.assertEqual(self.query("SELECT last_batch FROM run")[0][0], 2)

    def test_uncertain_commit_counts_everything_once(self):
        worker = self.make(interval_s=600)  # kapacita 6 měření
        release, entered = self.block_commits()
        worker.submit_event(history.Event(BASE, "synced", "test.hold", "INFO", "zapisovač drží první dávku"))
        self.assertTrue(entered.wait(WAIT))
        for k in range(8):  # mezitím přetečení: deterministicky 2 ztracená měření
            self.submit(worker, temp_state=State.CRITICAL if k % 2 else State.INFO, temp=90.0 if k % 2 else 40.0)
            self.clock.advance(600)
        self.faults.add(is_commit, after(sqlite_error(sqlite3.OperationalError, "SQLITE_IOERR_FSYNC", "disk I/O error")),
                        times=1)
        release.set()
        self.settle(worker, lambda s: s.error_kind == "io")  # druhá dávka zapsaná, volající to neví
        self.advance(worker, 60)
        self.assertTrue(worker.flush(WAIT))
        status = worker.status()
        self.assertEqual(status.losses["queue_full"], 2)
        self.assertEqual(len(self.events("history.gap")), 1)  # ztráta zapsaná právě jednou
        self.assertEqual(self.events("history.gap")[0][3]["count"], 2)
        self.assertEqual(status.unrecorded_losses, {})
        self.assertEqual((len(self.changes()), len(self.samples())), (8, 6))  # nic dvakrát ani ztracené
        self.assertEqual((status.confirmed_measurements, status.confirmed_changes), (6, 9))

    def test_connection_closes_in_writer_thread(self):
        worker = self.make()
        self.faults.add(is_commit, fail(sqlite_error(sqlite3.OperationalError, "SQLITE_IOERR", "disk I/O error")), times=1)
        self.submit(worker, temp_state=State.WARNING, temp=75.0)
        self.settle(worker, lambda s: s.error_kind == "io")
        self.advance(worker, 60)
        self.commits(worker, 1)
        worker.stop()
        self.assertEqual(set(self.faults.closed_in), {worker._thread.ident})
        self.assertEqual(len(self.faults.closed_in), 2)  # po chybě I/O a při zastavení


class ContractTest(WorkerTest):
    def test_add_change_path_equals_recorder_tracking(self):
        """Změny spočítané při submit() a vložené přes add_change() = změny zapisovače M3.1b."""
        sequence = [(State.INFO, "UP"), (State.CRITICAL, "UP"), (State.CRITICAL, "DOWN"), (State.UNKNOWN, "DOWN"),
                    (State.INFO, "UP"), (State.INFO, "UP")]
        worker = self.make()
        cycles = []
        for state, link in sequence:
            cycle = self.results(temp_state=state, temp=90.0 if state is State.CRITICAL else 40.0, link=link)
            cycles.append(cycle)
            worker.submit(cycle, self.clock.monotonic(), True)
            self.advance(worker, 10)
        worker.stop()
        columns = "ts, ts_quality, check_id, from_state, to_state, value, unit, details, note"
        via_worker = self.query(f"SELECT {columns} FROM check_change ORDER BY id")
        other = self.folder / "direct.sqlite3"
        history.create_database(other, "test", HOST, now_utc=CREATED)
        with history.open_writer(other, "test", HOST) as database:
            recorder = history.HistoryRecorder(database)
            recorder.start_run("test", 10, datetime.fromtimestamp(BASE, timezone.utc))
            for cycle in cycles:
                batch = recorder.begin_batch()
                recorder.add_results(batch, cycle, "synced")  # cesta M3.1b: změny počítá zapisovač
                recorder.write(batch)
            direct = database.connection.execute(f"SELECT {columns} FROM check_change ORDER BY id").fetchall()
        self.assertEqual(via_worker, direct)
        self.assertEqual(len(via_worker), 3 + 3 + 2)  # výchozí řádky 3 kontrol, 3 změny teploty, 2 linky


class _Releaser:
    """Uvolní semafor v tearDown, aby žádné vlákno nezůstalo viset."""

    def __init__(self, semaphore):
        self.semaphore = semaphore

    def set(self):
        for _ in range(100):
            self.semaphore.release()


class IncompleteHistoryTest(WorkerTest):
    """Ztráta změn ve frontě: aktuální stav pravdivý, neúplná historie přiznaná, nic vymyšlené."""

    def episodes(self, state):
        return self.query(_doc_block("state-episodes"), {"check_id": CPU_TEMP_ID, "state": state,
                                                         "start": 0, "end": BASE + 100_000})

    def test_dropped_middle_transitions_never_extend_an_episode(self):
        worker = self.make(interval_s=600, flapping_checks=1)  # kapacita 6 změn
        self.submit(worker, temp_state=State.CRITICAL, temp=95.0)  # CRITICAL od BASE, zapsáno
        self.assertTrue(worker.flush(WAIT))
        release = self.hold(worker)
        sequence = [State.INFO, State.CRITICAL, State.INFO, State.WARNING, State.INFO, State.WARNING, State.INFO,
                    State.WARNING, State.INFO]
        for k, state in enumerate(sequence, start=1):
            self.clock.advance(600)
            self.submit(worker, temp_state=state, temp=40.0 + k)
        dropped = worker.status().losses["changes_dropped"]
        self.assertEqual(dropped, 3)  # 9 změn, kapacita 6: zahozeny 3 nejstarší
        release.set()
        self.assertTrue(worker.flush(WAIT))
        rows = self.changes()
        self.assertEqual(rows[-1][3], "INFO")  # aktuální stav je pravdivý
        # CRITICAL od BASE skončil v BASE + 600 (zahozená změna): délka se nesmí natáhnout až k další změně.
        first = self.episodes("CRITICAL")[0]
        self.assertEqual(first[0], BASE)
        self.assertIsNone(first[1])  # konec neznámý, ne vymyšlený
        self.assertEqual([row[2:] for row in rows[1:3]], [("INFO", "WARNING"), ("WARNING", "INFO")])  # od 4. změny
        lost = self.query(_doc_block("changes-dropped"), {"check_id": CPU_TEMP_ID})
        self.assertEqual([row[1:] for row in lost], [(3, BASE + 600, BASE + 1800)])  # přesné hranice ztráty
        self.assertEqual(self.episodes("INFO")[0], (None, BASE + 2400, None, 0))  # INFO před 4. změnou: začátek neznámý

    def test_cycle_of_dropped_changes_is_reported_per_check(self):
        worker = self.make(interval_s=600, flapping_checks=1)
        self.submit(worker, temp_state=State.CRITICAL, temp=95.0)
        self.assertTrue(worker.flush(WAIT))
        release = self.hold(worker)
        for state in [State.INFO, State.CRITICAL] * 4:  # zahozen celý cyklus: řádky navenek spojité
            self.clock.advance(600)
            self.submit(worker, temp_state=state, temp=50.0)
        release.set()
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual(self.changes()[1][2:], ("CRITICAL", "INFO"))  # navazuje na zapsané CRITICAL
        event = self.events("history.changes_dropped")[0][3]
        self.assertIn(CPU_TEMP_ID, event["checks"])  # nespojitost, kterou řádky samy neukážou
        self.assertEqual(sum(item["count"] for item in event["checks"].values()), event["count"])
        self.assertTrue(self.query(_doc_block("changes-dropped"), {"check_id": CPU_TEMP_ID}))
        self.assertEqual(self.query(_doc_block("changes-dropped"), {"check_id": CPU_USAGE_ID}), [])

    def test_episode_overlap_rules(self):
        # Jen tabulka event: pravidla zásahu ztráty do epizody včetně mezí a úseku bez UTC.
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE event (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, data TEXT)")
        for data in ({"checks": {"a": {"count": 3, "from": 100, "to": 200}, "b": {"count": 5, "from": 150, "to": 150}}},
                     {"checks": {"a": {"count": 7, "mono_from": 1.0, "mono_to": 2.0}}}):
            conn.execute("INSERT INTO event (ts, kind, data) VALUES (1, 'history.changes_dropped', ?)", (json.dumps(data),))
        conn.execute("INSERT INTO event (ts, kind, data) VALUES (1, 'history.gap', ?)",
                     (json.dumps({"checks": {"a": {"count": 9, "from": 0, "to": 999}}}),))
        sql = _doc_block("episode-dropped-changes")
        for start, end, expected in ((0, 99, [7]), (0, 100, [3, 7]), (200, 300, [3, 7]), (201, None, [7]),
                                     (None, 99, [7]), (None, 100, [3, 7]), (None, None, [3, 7])):
            with self.subTest(start=start, end=end):
                rows = conn.execute(sql, {"check_id": "a", "start_ts": start, "end_ts": end}).fetchall()
                self.assertEqual([row[1] for row in rows], expected)
        self.assertEqual(conn.execute(sql, {"check_id": "b", "start_ts": 151, "end_ts": None}).fetchall(), [])
        conn.close()

    def test_lost_full_cycle_never_reported_as_continuous(self):
        worker = self.make(interval_s=1800, flapping_checks=1)  # kapacita 2 změny
        self.submit(worker, temp_state=State.CRITICAL, temp=95.0)  # CRITICAL od BASE, zapsáno
        self.assertTrue(worker.flush(WAIT))
        release = self.hold(worker)
        # CRITICAL → INFO (BASE+1800) → CRITICAL (BASE+3600) se zahodí, zůstanou dvě nejnovější změny.
        for state in (State.INFO, State.CRITICAL, State.WARNING, State.INFO):
            self.clock.advance(1800)
            self.submit(worker, temp_state=state, temp=60.0)
        self.assertEqual(worker.status().losses["changes_dropped"], 2)
        release.set()
        self.assertTrue(worker.flush(WAIT))
        rows = self.changes()
        # Žádný domyšlený přechod ani čas: jen skutečně zapsané změny, CRITICAL na obou stranách mezery.
        self.assertEqual(rows, [(BASE, "synced", None, "CRITICAL"), (BASE + 5400, "synced", "CRITICAL", "WARNING"),
                                (BASE + 7200, "synced", "WARNING", "INFO")])
        # Návaznost from_state sama vypadá jako nepřerušená epizoda: to důkaz úplnosti není.
        alone = self.episodes("CRITICAL")
        self.assertEqual(alone, [(BASE, BASE + 5400, 5400, 1)])
        lost = self.query(_doc_block("changes-dropped"), {"check_id": CPU_TEMP_ID})
        self.assertEqual([row[1:] for row in lost], [(2, BASE + 1800, BASE + 3600)])  # kontrola a úsek ztráty
        # Kombinované čtení podle kontraktu 7.1: epizoda zasažená ztrátou má neznámý konec i délku.
        checked = []
        for state in ("CRITICAL", "WARNING", "INFO"):
            for start, end, duration, at_begin in self.episodes(state):
                hits = self.query(_doc_block("episode-dropped-changes"),
                                  {"check_id": CPU_TEMP_ID, "start_ts": start, "end_ts": end})
                checked.append((state, start, None if hits else end, None if hits else duration,
                                [row[1:] for row in hits]))
        self.assertEqual(checked, [("CRITICAL", BASE, None, None, [(2, BASE + 1800, BASE + 3600)]),
                                   ("WARNING", BASE + 5400, BASE + 7200, 1800, []),
                                   ("INFO", BASE + 7200, None, None, [])])
        self.assertEqual(self.query(_doc_block("episode-dropped-changes"),
                                    {"check_id": CPU_USAGE_ID, "start_ts": None, "end_ts": None}), [])


class MemoryTest(WorkerTest):
    def test_measurement_queue_has_hard_value_limit(self):
        worker = self.make(interval_s=1)  # nejkratší interval: 3 600 měření
        self.open_error = None
        self.assertEqual((worker.measurement_capacity, worker.change_capacity), (3600, 72_000))
        observed, expires = self.clock.utc(), self.clock.monotonic() + 3
        wide = [CheckResult(f"test.v{n}", State.INFO, "x", observed, expires, simulated=False) for n in range(200)]
        worker.submit(wide, self.clock.monotonic(), True)  # 200 výsledků v měření
        self.assertTrue(worker.flush(WAIT))
        self.faults.add(lambda sql: sql == "BEGIN IMMEDIATE",
                        fail(sqlite_error(sqlite3.OperationalError, "SQLITE_BUSY", "database is locked")))
        for _ in range(1000):
            self.clock.advance(1)
            worker.submit(wide, self.clock.monotonic(), True)
        status = worker.status()
        self.assertLessEqual(status.pending_values, history_worker.MAX_QUEUE_VALUES)
        self.assertGreater(status.losses["queue_full"], 0)

    def test_every_allowed_interval_has_bounded_queues(self):
        for interval in (0, -1, 3601, 0.5, 1.0, True, None):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self.make(start=False, interval_s=interval)
        for interval, measurements, changes in ((1, 3600, 72_000), (10, 360, 7200), (3600, 1, 20)):
            worker = self.make(start=False, interval_s=interval)
            self.assertEqual((worker.measurement_capacity, worker.change_capacity, worker.event_capacity),
                             (measurements, changes, 500))
            self.assertLessEqual(worker.change_capacity, history_worker.MAX_QUEUE_VALUES)

    def test_oversized_event_is_counted_not_queued(self):
        worker = self.make()
        limit = history_worker.MAX_EVENT_BYTES
        exact = history.Event(BASE, "synced", "test.big", "INFO", "x" * (limit - len("test.big")))
        self.assertTrue(worker.submit_event(exact))  # přesně na limitu se přijme
        self.assertTrue(worker.flush(WAIT))
        release = self.hold(worker)
        began = time.perf_counter()
        huge = history.Event(BASE + 5, "synced", "test.big", "INFO", "zpráva", data={"blob": "é" * (limit // 2)})
        self.assertFalse(worker.submit_event(huge))  # znaků pod limitem, bajtů UTF-8 nad ním
        self.assertLess(time.perf_counter() - began, 1.0)
        status = worker.status()
        self.assertEqual((status.pending_events, status.losses["event_too_large"]), (0, 1))
        self.assertTrue(self.submit(worker, temp_state=State.WARNING, temp=75.0))  # sledování jede dál
        release.set()
        self.assertTrue(worker.flush(WAIT))
        events = self.events("history.events_dropped")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][2:], ("ERROR", {"reason": "event_too_large", "count": 1, "from": BASE + 5,
                                                   "to": BASE + 5}))
        self.assertEqual(self.query("SELECT count(*) FROM event WHERE kind = 'test.big'")[0][0], 1)


class OwnershipTest(WorkerTest):
    def test_connection_lives_only_in_worker_thread(self):
        worker = self.make()
        self.assertEqual(worker.status().state, RUNNING)
        self.assertEqual(self.open_threads, [worker._thread.ident])
        self.assertNotEqual(worker._thread.ident, threading.get_ident())
        with self.assertRaises(sqlite3.ProgrammingError):  # check_same_thread: jiné vlákno spojení nepoužije
            worker._database.connection._connection.execute("SELECT 1")
        report = worker.stop()
        self.assertEqual((report.completed, report.clean_stop, report.status.state), (True, True, STOPPED))
        with history.open_writer(self.db, "test", HOST):  # zámek uvolněn
            pass

    def test_second_worker_never_writes(self):
        first = self.make()
        second = self.make(start=False)
        second.start(time_synced=True)
        self.settle(second, lambda s: s.state == DEGRADED and s.error_kind == "locked")
        self.submit(second)
        self.submit(first, temp_state=State.WARNING, temp=75.0)
        self.commits(first, 1)
        self.assertEqual(self.query("SELECT count(*) FROM run")[0][0], 1)  # druhý zapisovač nezahájil běh
        self.assertEqual(second.status().commits, 0)
        self.assertFalse(second.stop(timeout=WAIT).clean_stop)

    def test_status_is_snapshot_without_sqlite(self):
        worker = self.make(start=False)
        before = worker.status()
        self.assertEqual((before.state, before.run_id, before.commits), ("STARTING", None, 0))
        worker.start(time_synced=True)
        self.settle(worker, lambda s: s.state == RUNNING)
        status = worker.status()
        self.assertIsInstance(status.run_id, int)
        with self.assertRaises(FrozenInstanceError):
            status.state = FAILED
        with self.assertRaises(TypeError):
            status.losses["x"] = 1
        self.assertEqual(status.time_base, "synced")

    def test_no_automatic_aggregation_or_retention(self):
        worker = self.make()
        for _ in range(20):
            self.submit(worker)
            self.advance(worker, 10)
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual(self.query("SELECT count(*) FROM agg_5m")[0][0], 0)
        self.assertEqual(self.query("SELECT done_until FROM aggregation_state WHERE level = '5m'")[0][0], 1_800_000_000)


class SubmitTest(WorkerTest):
    def test_submit_never_waits_for_stuck_io(self):
        worker = self.make()
        release, entered = self.block_commits()
        self.submit(worker, temp_state=State.CRITICAL, temp=91.0)  # změna: zápis hned, COMMIT visí
        self.assertTrue(entered.wait(WAIT))
        began = time.perf_counter()
        for _ in range(200):
            self.advance(worker, 10)
            self.submit(worker, temp_state=State.CRITICAL, temp=91.0)
        self.assertLess(time.perf_counter() - began, 1.0)  # 200 předání bez čekání na SQLite
        self.assertGreater(worker.status().in_flight, 0)
        release.set()
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual(len(self.samples()), 201)

    def test_submitted_snapshot_cannot_change(self):
        worker = self.make()
        results = self.results(temp=60.0)
        worker.submit(results, self.clock.monotonic(), True)
        results.clear()  # volající změní svůj seznam
        with self.assertRaises(FrozenInstanceError):
            worker._measurements[0].results[0].state = State.OK
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual(self.samples(), [(BASE, 60.0)])

    def test_submit_validates_input(self):
        worker = self.make()
        with self.assertRaises(TypeError):
            worker.submit(["x"], 1.0, True)
        with self.assertRaises(ValueError):
            worker.submit([], float("nan"), True)
        with self.assertRaises(ValueError):
            worker.submit([], 1.0, 1)
        with self.assertRaises(TypeError):
            worker.submit_event("událost")


class BatchTest(WorkerTest):
    def test_measurements_wait_for_batch_interval(self):
        worker = self.make()
        self.submit(worker)  # výchozí stavy: změny se zapíšou hned
        self.commits(worker, 1)
        for _ in range(5):
            self.advance(worker, 10)
            self.submit(worker)  # beze změny stavu: jen vzorky
        self.assertNoCommitYet(worker, 1)  # nejstarší čekající měření je staré 40 s
        self.advance(worker, 19)
        self.assertNoCommitYet(worker, 1)  # 59 s
        self.advance(worker, 1)  # 60 s: dávka
        self.commits(worker, 2)
        self.assertEqual(len(self.samples()), 6)
        self.assertEqual(self.query("SELECT last_batch FROM run")[0][0], 2)  # pět měření v jedné dávce

    def test_changes_and_events_wake_writer_immediately(self):
        worker = self.make()
        self.submit(worker)
        self.commits(worker, 1)
        self.submit(worker, temp_state=State.CRITICAL, temp=95.0)  # bez posunu času
        self.commits(worker, 2)
        worker.submit_event(history.Event(BASE, "synced", "test.important", "WARNING", "důležité"))
        self.commits(worker, 3)
        self.assertEqual([row[3] for row in self.changes()], ["INFO", "CRITICAL"])
        self.assertEqual(len(self.events("test.important")), 1)

    def test_order_and_every_transition_with_flapping(self):
        worker = self.make()
        release, entered = self.block_commits()
        states = [State.CRITICAL if k % 2 else State.INFO for k in range(61)]
        for state in states:  # zápis visí: vše se nashromáždí a zapíše pak najednou
            self.submit(worker, temp_state=state, temp=90.0 if state is State.CRITICAL else 40.0)
            self.advance(worker, 10)
        release.set()
        self.assertTrue(worker.flush(WAIT))
        rows = self.changes()
        self.assertEqual(len(rows), 61)  # výchozí řádek + 60 přechodů, nic sloučeno
        self.assertEqual([row[3] for row in rows], [state.value for state in states])
        self.assertEqual([row[0] for row in rows], [BASE + 10 * k for k in range(61)])
        episodes = self.query(_doc_block("state-episodes"), {"check_id": CPU_TEMP_ID, "state": "CRITICAL",
                                                             "start": 0, "end": BASE + 10_000})
        self.assertEqual(len(episodes), 30)
        self.assertEqual([ts for ts, _ in self.samples()], [BASE + 10 * k for k in range(61)])


class OverflowTest(WorkerTest):
    """Databáze je nedostupná už při otevření, takže fronty se plní deterministicky (žádná dávka)."""

    def down(self, **options):
        self.open_error = history.HistoryError("locked", "zapisuje jiný proces")
        worker = self.make(**options)
        self.assertEqual((worker.status().state, worker.status().error_kind), (DEGRADED, "locked"))
        return worker

    def recover(self, worker):
        self.open_error = None
        self.advance(worker, 600)
        self.assertTrue(worker.flush(WAIT))

    def test_measurement_overflow_keeps_changes_and_records_gap(self):
        worker = self.down(interval_s=600)  # kapacita 6 měření
        self.assertEqual(worker.measurement_capacity, 6)
        for k in range(10):
            state = State.CRITICAL if k == 1 else State.INFO  # CRITICAL v měření, které se zahodí
            self.submit(worker, temp_state=state, temp=90.0 if k == 1 else 40.0)
            self.advance(worker, 600)
        status = worker.status()
        self.assertEqual((status.losses["queue_full"], status.pending_measurements, status.unrecorded_losses["queue_full"]),
                         (4, 6, 4))
        self.recover(worker)
        self.assertEqual([ts for ts, _ in self.samples()], [BASE + 600 * k for k in range(4, 10)])
        self.assertEqual([row[3] for row in self.changes()], ["INFO", "CRITICAL", "INFO"])  # přechod přežil
        gaps = self.events("history.gap")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][3]["reason"], "queue_full")
        self.assertEqual((gaps[0][3]["count"], gaps[0][3]["values"], gaps[0][3]["from"], gaps[0][3]["to"]),
                         (4, 8, BASE, BASE + 1800))
        self.assertEqual(worker.status().unrecorded_losses, {})

    def test_change_overflow_never_shows_false_ok(self):
        worker = self.down(interval_s=600, flapping_checks=1)  # kapacita 6 změn
        states = [State.INFO, State.CRITICAL] * 5
        for state in states:
            self.submit(worker, temp_state=state, temp=90.0 if state is State.CRITICAL else 40.0)
            self.advance(worker, 600)
        self.assertEqual(worker.status().losses["changes_dropped"], 11 - 6)  # 10 změn teploty + výchozí využití
        self.recover(worker)
        rows = self.changes()
        self.assertEqual(rows[-1][3], "CRITICAL")  # poslední stav odpovídá skutečnosti
        self.assertIsNotNone(rows[0][2])  # první zachovaná změna nese předchozí stav (žádné vymyšlené OK)
        dropped = self.events("history.changes_dropped")
        self.assertEqual((len(dropped), dropped[0][2]), (1, "ERROR"))
        self.assertEqual(dropped[0][3]["count"], 5)

    def test_event_overflow_is_counted(self):
        worker = self.down(event_capacity=3)
        for k in range(5):
            worker.submit_event(history.Event(BASE + k, "synced", "test.e", "INFO", f"e{k}"))
        self.assertEqual(worker.status().losses["events_dropped"], 2)
        self.recover(worker)
        self.assertEqual([data for *_, data in self.events("test.e")], [None, None, None])
        self.assertEqual(self.events("history.events_dropped")[0][3]["count"], 2)

    def test_loss_records_do_not_flood_the_queue(self):
        worker = self.down(interval_s=600)
        for _ in range(200):
            self.submit(worker)
            self.advance(worker, 600)
        status = worker.status()
        self.assertEqual((status.pending_events, status.losses["queue_full"]), (0, 194))  # počitadlo, ne položky
        self.recover(worker)
        self.assertEqual(len(self.events("history.gap")), 1)
        self.assertEqual(self.events("history.gap")[0][2], "ERROR")  # mezera delší než hodina
        self.assertEqual(len(self.samples()) + self.events("history.gap")[0][3]["count"], 200)

    def test_change_capacity_for_flapping_checks(self):
        # Hodinový výpadek zápisu při trvalém kmitání 1, 5, 10, 20 a 21 kontrol po 10 s.
        for checks, expected_loss in ((1, 0), (5, 0), (10, 0), (20, 0), (21, 360)):
            with self.subTest(checks=checks):
                self.open_error = history.HistoryError("locked", "zapisuje jiný proces")  # databáze nedostupná
                worker = self.make(synced=True)
                self.assertEqual(worker.change_capacity, 7200)
                for k in range(360):
                    state = State.CRITICAL if k % 2 else State.INFO
                    observed, expires = self.clock.utc(), self.clock.monotonic() + 30
                    worker.submit([CheckResult(f"test.flap{n}", state, "x", observed, expires, simulated=False)
                                   for n in range(checks)], self.clock.monotonic(), True)
                    self.clock.advance(10)
                status = worker.status()
                self.assertEqual(status.losses.get("changes_dropped", 0), expected_loss)
                self.assertEqual(status.pending_changes, min(7200, 360 * checks))
                worker.stop(timeout=WAIT)
                self.open_error = None


class FailureTest(WorkerTest):
    def test_busy_retries_with_backoff_without_duplicates(self):
        self.on_open = lambda connection: connection.execute("PRAGMA busy_timeout = 0")  # bez skutečného čekání
        worker = self.make()
        other = sqlite3.connect(self.db, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")  # zámek zápisu drží ruční sqlite3
            self.submit(worker, temp_state=State.WARNING, temp=75.0)
            self.settle(worker, lambda s: s.state == DEGRADED and s.error_kind == "busy")
            self.assertEqual(worker.status().retry_in, 5)
            self.advance(worker, 5)  # druhý pokus selže: odstup se zdvojnásobí
            self.settle(worker, lambda s: s.consecutive_failures == 2)
            self.assertEqual(worker.status().retry_in, 10)
            other.execute("ROLLBACK")
        finally:
            other.close()
        self.advance(worker, 10)
        self.commits(worker, 1)
        self.assertEqual(worker.status().state, RUNNING)
        self.assertEqual(len(self.samples()), 1)
        self.assertEqual(len(self.changes()), 1)

    def test_real_disk_full_keeps_data_and_recovers(self):
        limit = threading.Event()

        def guard(connection, sql, run):  # max_page_count se mění ve vlákně zapisovače
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            connection.execute(f"PRAGMA max_page_count = {pages if limit.is_set() else 1_000_000}")
            return run()

        self.faults.add(lambda sql: sql == "BEGIN IMMEDIATE", guard)
        worker = self.make()  # 10 s: fronta pojme 360 měření
        release, entered = self.block_commits()
        worker.submit_event(history.Event(BASE, "synced", "test.start", "INFO", "první dávka visí"))
        self.assertTrue(entered.wait(WAIT))
        for k in range(300):  # mezitím se nashromáždí velká dávka: potřebuje nové stránky
            self.submit(worker, temp=40.0 + k % 7, usage=float(k % 5), temp_state=State.WARNING if k % 2 else State.INFO)
            self.clock.advance(10)
        limit.set()  # skutečný SQLITE_FULL: soubor nesmí přibrat stránku
        release.set()
        self.settle(worker, lambda s: s.error_kind == "full")
        self.assertEqual(worker.status().retry_in, 60)
        self.assertEqual(len(self.samples()), 0)
        limit.clear()
        self.advance(worker, 60)
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual((len(self.samples()), len(self.changes())), (300, 300))
        self.assertEqual(worker.status().losses, {})

    def test_io_error_reopens_connection_in_same_run(self):
        worker = self.make()
        run_id = worker.status().run_id
        self.faults.add(is_commit, fail(sqlite_error(sqlite3.OperationalError, "SQLITE_IOERR_WRITE", "disk I/O error")),
                        times=1)
        self.submit(worker, temp_state=State.ERROR, temp=85.0)
        self.settle(worker, lambda s: s.error_kind == "io" and s.state == DEGRADED)
        self.assertIsNone(worker._database)  # staré spojení zavřené dřív, než vznikne nové
        self.advance(worker, 60)
        self.commits(worker, 1)
        self.assertEqual(len(self.open_threads), 2)
        self.assertEqual(worker.status().run_id, run_id)
        self.assertEqual((self.query("SELECT count(*) FROM run")[0][0], len(self.changes()), len(self.samples())), (1, 1, 1))

    def test_stop_after_uncertain_commit_reports_data_in_doubt(self):
        worker = self.make()
        self.faults.add(is_commit, after(sqlite_error(sqlite3.OperationalError, "SQLITE_IOERR_FSYNC", "disk I/O error")))
        self.open_error = None
        self.submit(worker, temp_state=State.CRITICAL, temp=95.0)
        self.settle(worker, lambda s: s.error_kind == "io")
        self.open_error = history.HistoryError("locked", "nelze znovu otevřít")  # stav nejde ověřit
        report = worker.stop(timeout=WAIT)
        self.assertEqual((report.clean_stop, report.unsaved_measurements, report.in_doubt), (False, 0, 3))
        self.assertEqual(len(self.changes()), 1)  # commit ve skutečnosti proběhl: proto „nejisté“, ne „nezapsané“

    def test_uncertain_commit_is_written_once(self):
        worker = self.make()
        self.faults.add(is_commit, after(sqlite_error(sqlite3.OperationalError, "SQLITE_IOERR_FSYNC", "disk I/O error")),
                        times=1)
        self.submit(worker, temp_state=State.CRITICAL, temp=95.0)
        self.settle(worker, lambda s: s.error_kind == "io")
        self.advance(worker, 60)
        self.commits(worker, 1)
        self.assertEqual((len(self.changes()), len(self.samples()), worker.status().commits), (1, 1, 1))
        self.assertEqual(self.query("SELECT last_batch FROM run")[0][0], 1)

    @unittest.skipIf(os.geteuid() == 0, "root práva souboru obchází")
    def test_lost_write_permission_is_transient(self):
        worker = self.make()
        self.faults.add(is_commit, fail(sqlite_error(sqlite3.OperationalError, "SQLITE_IOERR", "disk I/O error")), times=1)
        os.chmod(self.db, stat.S_IRUSR | stat.S_IRGRP)  # zápis znemožněn
        self.submit(worker, temp_state=State.WARNING, temp=75.0)
        self.settle(worker, lambda s: s.error_kind == "io")
        self.advance(worker, 60)  # znovuotevření projde, zápis selže SQLITE_READONLY
        self.settle(worker, lambda s: s.consecutive_failures == 2 and "readonly" in (s.last_error or ""))
        self.assertEqual(worker.status().state, DEGRADED)
        for path in (self.db, *history.companion_paths(self.db)):  # správce vrátí práva i -wal a -shm,
            if path.exists():  # které SQLite mezitím vytvořilo s právy hlavního souboru
                os.chmod(path, 0o644)
        self.advance(worker, 120)
        self.commits(worker, 1)
        self.assertEqual(len(self.changes()), 1)

    def test_unexpected_integrity_error_fails_without_retry_loop(self):
        worker = self.make()
        self.faults.add(lambda sql: sql == history.INSERT_CHANGE_SQL,
                        fail(sqlite_error(sqlite3.IntegrityError, "SQLITE_CONSTRAINT_CHECK", "CHECK constraint failed")))
        self.submit(worker, temp_state=State.CRITICAL, temp=95.0)
        self.settle(worker, lambda s: s.state == FAILED and s.error_kind == "integrity")
        self.advance(worker, 3600)
        self.assertTrue(self.submit(worker))  # monitoring dál předává bez blokování
        self.assertEqual((worker.status().commits, worker.status().consecutive_failures), (0, 0))
        self.assertEqual(self.changes(), [])
        report = worker.stop(timeout=WAIT)
        self.assertEqual((report.completed, report.clean_stop, report.unsaved_measurements), (True, False, 2))

    def test_corruption_is_fatal_and_file_is_never_replaced(self):
        worker = self.make()
        self.faults.add(lambda sql: sql == "BEGIN IMMEDIATE",
                        fail(sqlite_error(sqlite3.DatabaseError, "SQLITE_CORRUPT", "database disk image is malformed")))
        self.submit(worker, temp_state=State.CRITICAL, temp=95.0)
        self.settle(worker, lambda s: s.state == FAILED and s.error_kind == "corrupt")
        worker.stop(timeout=WAIT)
        broken = self.folder / "broken.sqlite3"
        history.create_database(broken, "test", HOST, now_utc=CREATED)
        with open(broken, "r+b") as handle:  # poškozená kopie (hlavička zůstává NexusSTAT)
            handle.seek(4096)
            handle.write(b"\xff" * 4096)
        auxiliary = (".lock", "-wal", "-shm")  # pomocné soubory SQLite a zámku (M3.1a), ne nová databáze
        listing = lambda: sorted(name for name in os.listdir(self.folder) if not name.endswith(auxiliary))
        before = (listing(), hashlib.sha256(broken.read_bytes()).hexdigest())
        second = self.make(opener=lambda: history.open_writer(broken, "test", HOST))
        self.settle(second, lambda s: s.state == FAILED)
        self.assertIn(second.status().error_kind, ("corrupt", "identity"))
        second.stop(timeout=WAIT)
        self.assertEqual((listing(), hashlib.sha256(broken.read_bytes()).hexdigest()), before)  # nic nového ani přepsaného

    def test_missing_database_is_never_created(self):
        missing = self.folder / "missing.sqlite3"
        worker = self.make(opener=lambda: history.open_writer(missing, "test", HOST))
        self.settle(worker, lambda s: s.state == DEGRADED and s.error_kind == "path")
        self.advance(worker, 60)
        self.settle(worker, lambda s: s.consecutive_failures >= 2)
        self.assertFalse(missing.exists())

    def test_classify_distinguishes_trust_from_write_errors(self):
        cases = {("write", "SQLITE_BUSY"): "busy", ("write", "SQLITE_LOCKED_SHAREDCACHE"): "busy",
                 ("write", "SQLITE_FULL"): "full", ("write", "SQLITE_IOERR_WRITE"): "io",
                 ("write", "SQLITE_READONLY"): "io", ("write", None): "io", ("write", "SQLITE_CORRUPT"): "corrupt",
                 ("write", "SQLITE_NOTADB"): "corrupt", ("write", "SQLITE_CONSTRAINT_FOREIGNKEY"): "integrity",
                 ("corrupt", None): "corrupt", ("corrupt", "SQLITE_IOERR_READ"): "io", ("locked", None): "locked",
                 ("path", None): "path", ("foreign", None): "identity", ("host", None): "identity", ("batch", None): "integrity"}
        for (reason, name), kind in cases.items():
            with self.subTest(reason=reason, name=name):
                self.assertEqual(classify(history.HistoryError(reason, "x", name)), kind)

    def test_stuck_io_stop_is_honest(self):
        worker = self.make()
        release, entered = self.block_commits()
        self.submit(worker, temp_state=State.CRITICAL, temp=95.0)
        self.assertTrue(entered.wait(WAIT))
        self.submit(worker)
        began = time.perf_counter()
        report = worker.stop(timeout=0.2)
        self.assertLess(time.perf_counter() - began, 2.0)
        self.assertFalse(report.completed)  # vlákno visí v I/O; nepředstírá se, že skončilo
        self.assertFalse(report.clean_stop)
        self.assertEqual(report.unsaved_measurements, 1)  # ve frontě: jistě nezapsané
        self.assertEqual(report.in_doubt, 3)  # dávka v COMMIT (1 měření, 2 změny): výsledek neznámý
        self.assertEqual(report.status.confirmed_measurements, 0)
        release.set()
        worker._thread.join(WAIT)
        self.assertFalse(worker._thread.is_alive())


class TimeTest(WorkerTest):
    def test_unsynced_data_waits_and_gets_derived_time(self):
        self.clock.set_utc(WRONG)  # Raspberry Pi bez RTC: čas posledního vypnutí
        worker = self.make(synced=False)
        start_mono = self.clock.monotonic()
        for _ in range(3):
            self.submit(worker, synced=False, temp_state=State.WARNING, temp=75.0)
            self.advance(worker, 10)
        self.submit(worker, synced=None)  # neznámý stav = nedůvěryhodný
        self.advance(worker, 10)
        status = worker.status()
        self.assertEqual((status.time_base, status.held_unsynced, status.commits), ("unsynced", 4 + 3, 0))
        self.assertEqual(self.samples(), [])
        self.clock.set_utc(BASE + 40)  # NTP posune čas na skutečný
        self.submit(worker, synced=True)
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual([ts for ts, _ in self.samples()], [BASE + 10 * k for k in range(5)])
        self.assertEqual([row[:2] for row in self.changes()],
                         [(BASE, "derived"), (BASE + 30, "derived")])  # WARNING a pak INFO, odvozený čas
        synced = self.events("clock.synced")
        self.assertEqual(synced[0][3]["held_measurements"], 4)
        self.assertEqual((synced[0][3]["from"], synced[0][3]["to"]), (BASE, BASE + 30))
        self.assertEqual(synced[0][3]["mono_from"], start_mono)
        self.assertEqual(self.query("SELECT count(*) FROM sample WHERE ts < ?", (history.MIN_TRUSTED_UTC,))[0][0], 0)

    def test_stop_without_sync_drops_samples_and_keeps_changes(self):
        self.clock.set_utc(WRONG)
        worker = self.make(synced=False)
        mono = []
        for _ in range(3):
            mono.append(self.clock.monotonic())
            self.submit(worker, synced=False, temp_state=State.CRITICAL, temp=95.0)
            self.advance(worker, 10)
        report = worker.stop(timeout=WAIT)
        self.assertEqual((report.completed, report.clean_stop), (True, True))
        self.assertEqual(self.query("SELECT count(*) FROM sample")[0][0], 0)  # žádný vzorek s vymyšleným časem
        self.assertEqual([row[1] for row in self.changes()], ["unsynced"])
        gaps = self.events("history.gap")
        self.assertEqual((len(gaps), gaps[0][1]), (1, "unsynced"))
        data = gaps[0][3]
        self.assertEqual((data["reason"], data["count"], data["mono_from"], data["mono_to"]), ("time_unsynced", 3, mono[0], mono[-1]))
        self.assertNotIn("from", data)  # rozsah UTC se nevymýšlí
        self.assertEqual(report.status.losses["time_unsynced"], 3)

    def test_unsynced_overflow_waits_for_derived_range(self):
        self.clock.set_utc(WRONG)
        worker = self.make(synced=False, interval_s=600)
        first = self.clock.monotonic()
        for _ in range(10):
            self.submit(worker, synced=False)
            self.advance(worker, 600)
        status = worker.status()
        self.assertEqual((status.unrecorded_losses["queue_full"], status.held_unsynced), (4, 6 + 2))  # 6 měření, 2 změny
        self.assertTrue(worker.flush(WAIT))  # ztráta jen s monotónním rozsahem čeká na synchronizaci
        self.assertEqual(self.events("history.gap"), [])
        self.clock.set_utc(BASE + 6000)  # synchronizace: UTC − monotonic je teď známé
        self.submit(worker, synced=True)  # plná fronta: zahodí se ještě jedno čekající měření
        self.assertTrue(worker.flush(WAIT))
        gap = self.events("history.gap")[0]
        self.assertEqual((gap[1], gap[3]["count"], gap[3]["from"], gap[3]["to"]), ("derived", 5, BASE, BASE + 2400))
        self.assertEqual((gap[3]["mono_from"], gap[3]["mono_to"]), (first, first + 2400))

    def test_unsynced_overflow_at_stop_has_monotonic_range_only(self):
        self.clock.set_utc(WRONG)
        worker = self.make(synced=False, interval_s=600)
        for _ in range(10):
            self.submit(worker, synced=False)
            self.advance(worker, 600)
        worker.stop()
        gaps = {gap[3]["reason"]: gap for gap in self.events("history.gap")}
        self.assertEqual((gaps["queue_full"][1], gaps["queue_full"][3]["count"]), ("unsynced", 4))
        self.assertNotIn("from", gaps["queue_full"][3])  # rozsah UTC se nevymýšlí
        self.assertEqual(gaps["time_unsynced"][3]["count"], 6)

    def test_plausible_time_without_proof_is_never_trusted(self):
        # Čas vypadá správně (2027), ale synchronizace není doložená: nic se nepovažuje za synced.
        worker = self.make(synced=None, interval_s=600)
        for synced in (None, False, None, False, None, False, None, False, None, False):
            self.submit(worker, synced=synced)
            self.advance(worker, 600)
        self.clock.set_utc(WRONG)
        self.submit(worker, synced=True)  # „synchronizováno“, ale čas před 2026: nevěrohodné
        status = worker.status()
        self.assertEqual((status.time_base, status.commits), ("unsynced", 0))
        worker.stop()
        self.assertEqual(self.query("SELECT count(*) FROM sample")[0][0], 0)
        gaps = {gap[3]["reason"]: gap for gap in self.events("history.gap")}
        self.assertEqual(gaps["queue_full"][1], "unsynced")
        self.assertNotIn("from", gaps["queue_full"][3])  # věrohodný, ale nedoložený čas není rozsah UTC
        self.assertEqual({row[1] for row in self.changes()}, {"unsynced"})

    def test_sync_loss_after_first_sync_is_only_an_event(self):
        worker = self.make()
        self.submit(worker)
        self.advance(worker, 10)
        self.submit(worker, synced=False)
        self.assertEqual(worker.status().time_base, "sync_lost")
        self.advance(worker, 10)
        self.submit(worker, synced=True)
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual(len(self.samples()), 3)  # data se dál zapisují
        self.assertEqual((len(self.events("clock.sync_lost")), len(self.events("clock.sync_restored"))), (1, 1))
        self.assertEqual(worker.status().time_base, "synced")


class ShutdownTest(WorkerTest):
    def test_clean_stop_drains_queue_and_releases_lock(self):
        worker = self.make()
        for _ in range(3):
            self.submit(worker)
            self.advance(worker, 10)
        report = worker.stop()
        self.assertEqual((report.completed, report.clean_stop, report.unsaved_measurements), (True, True, 0))
        self.assertEqual(len(self.samples()), 3)
        self.assertEqual(self.query("SELECT clean_stop FROM run")[0][0], 1)
        self.assertFalse(self.submit(worker))  # po zastavení se nic nepřijímá, jen započte
        self.assertEqual(worker.status().losses["after_stop"], 1)
        with history.open_writer(self.db, "test", HOST):
            pass

    def test_stop_with_unavailable_database_reports_unsaved(self):
        worker = self.make()
        self.faults.add(lambda sql: sql == "BEGIN IMMEDIATE",
                        fail(sqlite_error(sqlite3.OperationalError, "SQLITE_BUSY", "database is locked")))
        self.submit(worker, temp_state=State.ERROR, temp=85.0)
        self.settle(worker, lambda s: s.error_kind == "busy")
        report = worker.stop(timeout=WAIT)
        self.assertEqual((report.completed, report.clean_stop), (True, False))
        self.assertEqual((report.unsaved_measurements, report.unsaved_changes, report.in_doubt), (1, 2, 0))  # před COMMIT
        self.assertIsNone(self.query("SELECT stopped_utc FROM run")[0][0])  # běh nečistý, příští start to označí

    def test_restart_keeps_committed_data_once(self):
        first = self.make()
        for _ in range(3):
            self.submit(first)
            self.advance(first, 10)
        first.stop()
        second = self.make()
        for _ in range(2):
            self.submit(second, temp=60.0)
            self.advance(second, 10)
        second.stop()
        self.assertEqual(len(self.samples()), 5)
        self.assertEqual(self.query("SELECT count(*), sum(clean_stop) FROM run")[0], (2, 2))

    def test_process_crash_leaves_unknown_gap(self):
        code = subprocess.run([sys.executable, "-c", CHILD, str(self.db)], env=dict(os.environ, PYTHONPATH=str(REPO / "src")),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(code.returncode, 81, code.stderr)
        worker = self.make()
        worker.stop()
        self.assertEqual(len(self.samples()), 1)  # potvrzená dávka přežila pád
        gap = self.events("history.unknown_gap")
        self.assertEqual(len(gap), 1)  # nezapsaná data procesu zůstávají neznámou mezerou
        self.assertEqual(self.query("SELECT clean_stop FROM run ORDER BY id")[0][0], 0)

    def test_flush_is_truthful(self):
        worker = self.make()
        self.faults.add(lambda sql: sql == "BEGIN IMMEDIATE",
                        fail(sqlite_error(sqlite3.OperationalError, "SQLITE_BUSY", "database is locked")))
        self.submit(worker)
        self.assertFalse(worker.flush(0.2))  # nic se nepotvrdilo
        self.faults.clear()
        self.advance(worker, 5)
        self.assertTrue(worker.flush(WAIT))


CHILD = r"""
import os, sys
from datetime import datetime, timezone
from nexusstat import history
from nexusstat.core import CheckResult, Measurement, State
from nexusstat.history_worker import HistoryWorker
db = sys.argv[1]
now = [1000.0]
utc = lambda: datetime.fromtimestamp(1_800_003_600 + now[0] - 1000.0, timezone.utc)
worker = HistoryWorker(db, "test", "0123456789abcdef", app_version="child", interval_s=10, monotonic=lambda: now[0], utc_now=utc)
worker.start(time_synced=True)
result = lambda: [CheckResult("system.cpu_temp", State.INFO, "x", utc(), now[0] + 30, simulated=False,
                              measurement=Measurement(50.0, "°C"))]
worker.submit(result(), now[0], True)
assert worker.flush(5)
now[0] += 10
worker.submit(result(), now[0], True)  # nezapsané měření
os._exit(81)  # pád procesu bez stop()
"""


def _doc_block(name):
    import re
    text = (REPO / "docs" / "HISTORY.md").read_text(encoding="utf-8")
    return re.search(rf"<!-- sql:{re.escape(name)} -->\s*```sql\n(.*?)```", text, re.S).group(1)


if __name__ == "__main__":
    unittest.main()
