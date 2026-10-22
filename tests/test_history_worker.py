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
import math
import os
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
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from nexusstat import history
from nexusstat.core import CheckResult, Measurement, State
from nexusstat import history_worker
from nexusstat.history_worker import (
    AGGREGATION_INTERVAL_SECONDS,
    DEGRADED,
    FAILED,
    MAX_RESULT_BYTES,
    RETENTION_INTERVAL_SECONDS,
    RUNNING,
    STOPPED,
    HistoryWorker,
    classify,
    result_bytes,
    trim_result,
)
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

    def stable(self, worker):
        """Doložená doba souvislé synchronizace ze snímku stavu (None = nedoložená)."""
        return worker.status().maintenance.stable_s

    def observe(self, worker, monotonic=None, synced=True):
        """Jedno pozorování zdroje synchronizace bez měření (jen evidence stability)."""
        monotonic = self.clock.monotonic() if monotonic is None else monotonic
        with worker._cond:
            worker._track_stability(monotonic, monotonic + self.clock.offset, synced)

    def run_for(self, worker, seconds, step=300):
        """Posune čas po krocích s průběžným potvrzováním synchronizace (jako skutečný provoz)."""
        end = self.clock.monotonic() + seconds
        while self.clock.monotonic() < end:
            self.clock.advance(min(step, end - self.clock.monotonic()))
            self.observe(worker)
        worker.wake()

    def query_connection(self):
        """Spojení jen pro čtení pro kontroly, které potřebují dotaz (zavře se s testem)."""
        reader = history.open_reader(self.db)
        self.addCleanup(reader.close)
        return reader.connection

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


class ResultSizeTest(WorkerTest):
    """Velikost jednoho výsledku: text se zkrátí, identita a hodnota zůstanou."""

    def result(self, summary="x", details=None, check_id=CPU_TEMP_ID, state=State.INFO, value=50.0):
        return CheckResult(check_id, state, summary, self.clock.utc(), self.clock.monotonic() + 30,
                           simulated=False, details=details or {},
                           measurement=None if value is None else Measurement(value, CELSIUS))

    def test_size_counts_id_summary_and_details(self):
        plain = self.result(summary="CPU 50 °C")  # ° je v UTF-8 dvoubajtový
        self.assertEqual(result_bytes(plain), len(CPU_TEMP_ID.encode()) + len("CPU 50 °C".encode()))
        with_details = self.result(summary="x", details={INTERFACE: "eth0"})
        self.assertEqual(result_bytes(with_details) - result_bytes(self.result(summary="x")),
                         len(INTERFACE) + len("eth0") + history_worker.DETAILS_OVERHEAD)

    def test_result_within_limit_is_untouched(self):
        exact = self.result(summary="x" * (MAX_RESULT_BYTES - len(CPU_TEMP_ID)))
        self.assertEqual(result_bytes(exact), MAX_RESULT_BYTES)
        trimmed, cut = trim_result(exact)
        self.assertFalse(cut)
        self.assertIs(trimmed, exact)

    def test_one_byte_over_limit_is_trimmed(self):
        over = self.result(summary="x" * (MAX_RESULT_BYTES - len(CPU_TEMP_ID) + 1))
        trimmed, cut = trim_result(over)
        self.assertTrue(cut)
        self.assertLessEqual(result_bytes(trimmed), MAX_RESULT_BYTES)
        self.assertTrue(trimmed.summary.endswith(history_worker.TRUNCATION_MARK))

    def test_identity_state_and_value_survive(self):
        huge = self.result(summary="á" * 10_000, state=State.WARNING, details={INTERFACE: "eth0"})
        trimmed, _ = trim_result(huge)
        self.assertEqual(trimmed.id, huge.id)
        self.assertIs(trimmed.state, State.WARNING)
        self.assertEqual(trimmed.measurement, huge.measurement)
        self.assertEqual((trimmed.observed_utc, trimmed.expires_monotonic),
                         (huge.observed_utc, huge.expires_monotonic))
        self.assertFalse(trimmed.simulated)
        trimmed.summary.encode().decode()  # řez uprostřed znaku UTF-8 nenechá rozbitý text

    def test_series_subject_has_priority_in_details(self):
        # "aaa" se do limitu vejde samo, ale po zachování rozhraní už ne: objekt
        # řady (rozhraní u RX/TX) má přednost, jinak by vzorek řadu nedostal.
        huge = self.result(check_id=history.RX_RATE_ID, summary="x" * 1000, value=None,
                           details={INTERFACE: "eth0", "aaa": "y" * 480, "zzz": "z" * 1000})
        trimmed, cut = trim_result(huge)
        self.assertTrue(cut)
        self.assertEqual(dict(trimmed.details), {INTERFACE: "eth0"})
        self.assertLessEqual(result_bytes(trimmed), MAX_RESULT_BYTES)

    def test_absurd_id_passes_without_text(self):
        huge = self.result(check_id="t" * (MAX_RESULT_BYTES + 10), summary="x" * 100, value=None,
                           details={"a": "b"})
        trimmed, cut = trim_result(huge)
        self.assertTrue(cut)
        self.assertEqual((trimmed.summary, dict(trimmed.details)), ("", {}))
        self.assertEqual(trimmed.id, huge.id)  # identita kontroly se nikdy nezahodí

    def test_queue_holds_only_trimmed_results(self):
        worker = self.make()
        release = self.hold(worker)
        for index in range(10):
            self.clock.advance(1)
            self.assertTrue(worker.submit([self.result(summary="x" * 100_000)], self.clock.monotonic(), True))
        status = worker.status()
        self.assertEqual(status.pending_measurements, 10)
        self.assertEqual(status.losses["result_truncated"], 10)
        queued = [item.results[0] for item in worker._measurements]
        self.assertTrue(all(result_bytes(result) <= MAX_RESULT_BYTES for result in queued))
        release.set()
        self.assertTrue(worker.flush(WAIT))
        notes = self.query("SELECT note FROM check_change WHERE check_id = ?", (CPU_TEMP_ID,))
        self.assertTrue(all(len(note[0].encode()) <= MAX_RESULT_BYTES for note in notes))

    def test_truncation_is_recorded_per_check(self):
        worker = self.make()
        self.assertTrue(worker.submit([self.result(summary="x" * 100_000),
                                       self.result(check_id=CPU_USAGE_ID, summary="y" * 100_000, value=5.0)],
                                      self.clock.monotonic(), True))
        self.assertTrue(worker.flush(WAIT))
        events = self.events("history.result_truncated")
        self.assertEqual(len(events), 1)
        ts, quality, severity, data = events[0]
        self.assertEqual((quality, severity), ("synced", "WARNING"))  # zkrácení textu není ztráta měření
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["limit_bytes"], MAX_RESULT_BYTES)
        self.assertEqual({check: item["count"] for check, item in data["checks"].items()},
                         {CPU_TEMP_ID: 1, CPU_USAGE_ID: 1})
        self.assertEqual(self.samples(CPU_TEMP_ID), [(BASE, 50.0)])  # hodnota se zapsala normálně

    def test_important_state_survives_truncation_with_evidence(self):
        # Velký výsledek nesmí umlčet důležitý diagnostický stav: stav i hodnota
        # se zapíšou a zkrácení textu má vlastní doklad.
        worker = self.make()
        self.assertTrue(worker.submit([self.result(summary="x" * 50_000, state=State.CRITICAL, value=99.0)],
                                      self.clock.monotonic(), True))
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual([row[3] for row in self.changes()], ["CRITICAL"])
        self.assertEqual(self.samples(CPU_TEMP_ID), [(BASE, 99.0)])
        self.assertEqual(self.events("history.result_truncated")[0][3]["count"], 1)

    def test_truncation_before_synchronization_has_no_utc_range(self):
        worker = self.make(synced=False)
        self.clock.set_utc(WRONG)
        self.assertTrue(worker.submit([self.result(summary="x" * 100_000)], self.clock.monotonic(), False))
        status = worker.status()
        self.assertEqual(status.losses["result_truncated"], 1)
        self.assertEqual(status.time_base, "unsynced")

    def test_oversized_result_does_not_stop_monitoring(self):
        worker = self.make()
        self.assertTrue(worker.submit([self.result(summary="x" * 500_000, details={"big": "y" * 500_000})],
                                      self.clock.monotonic(), True))
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual(worker.status().state, RUNNING)
        self.clock.advance(10)
        self.assertTrue(self.submit(worker, temp_state=State.WARNING, temp=75.0))
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual([row[3] for row in self.changes()], ["INFO", "WARNING"])


class QueueStateTest(WorkerTest):
    """Kontrakt fronty pro uzavírání oken: EMPTY jen tehdy, když to lze doložit."""

    def state(self, worker):
        with worker._cond:
            queue, quality = worker._queue_state()
        return queue, quality

    def test_empty_queue_with_synchronized_clock(self):
        worker = self.make(start=False)
        worker.start(time_synced=True)
        self.settle(worker, lambda s: s.state != "STARTING")
        queue, quality = self.state(worker)
        self.assertEqual((queue.known, queue.oldest_utc, quality), (True, None, "synced"))

    def test_waiting_measurement_reports_its_time(self):
        worker = self.make()
        self.submit(worker)
        queue, quality = self.state(worker)
        self.assertEqual((queue.known, queue.oldest_utc), (True, BASE))
        self.assertEqual(quality, "synced")

    def test_oldest_time_decides_even_after_the_clock_went_back(self):
        worker = self.make(start=False)
        worker.start(time_synced=True)
        self.submit(worker)
        self.clock.advance(10)
        self.clock.set_utc(BASE - 50)  # čas šel zpět: první měření je novější
        self.submit(worker)
        queue, _ = self.state(worker)
        self.assertEqual(queue.oldest_utc, BASE - 50)

    def test_unsynchronized_measurement_is_unknown(self):
        self.clock.set_utc(WRONG)
        worker = self.make(start=False, synced=False)
        worker.start(time_synced=False)
        self.submit(worker, synced=False)
        queue, quality = self.state(worker)
        self.assertEqual((queue.known, quality), (False, "unsynced"))

    def test_measurement_held_before_the_first_synchronization_is_unknown(self):
        # Hodiny ukazovaly věrohodný čas, ale zdroj ho nedokládal: takové měření
        # dostane až odvozený čas po první synchronizaci, takže dokud není
        # zapsané, okno uzavřít nelze — i když jeho čas vypadá použitelně.
        worker = self.make(start=False, synced=False)  # bez vlákna: nic se nezapisuje
        worker.start(time_synced=False)
        self.submit(worker, synced=False)  # čas BASE, ale nedoložený
        self.clock.advance(10)
        self.submit(worker, synced=True)  # první doložená synchronizace
        held = [item for item in worker._measurements if item.quality == "unsynced"]
        self.assertEqual(len(held), 1)
        self.assertGreaterEqual(math.floor(held[0].results[0].observed_utc.timestamp()), history.MIN_TRUSTED_UTC)
        queue, quality = self.state(worker)
        self.assertEqual(quality, "synced")
        self.assertFalse(queue.known)

    def test_measurement_without_results_is_unknown(self):
        worker = self.make()
        worker.submit([], self.clock.monotonic(), True)
        queue, _ = self.state(worker)
        self.assertFalse(queue.known)

    def test_selected_or_sealed_batch_is_unknown(self):
        worker = self.make()
        release = self.hold(worker)  # dávka drží COMMIT: její vzorky nejsou zapsané
        queue, _ = self.state(worker)
        self.assertFalse(queue.known)
        release.set()

    def test_quality_follows_the_source_of_synchronization(self):
        worker = self.make()
        self.submit(worker, synced=False)  # ztráta synchronizace: čas „teď“ není jistý
        _, quality = self.state(worker)
        self.assertEqual(quality, "unsynced")


class MaintenanceTest(WorkerTest):
    """Automatická agregace a retence spouštěná vláknem zapisovače (M3.3b-2)."""

    GRACE = history.AGGREGATION_GRACE_SECONDS

    def fill(self, worker, count, step=10):
        """Předá a potvrdí `count` měření v odstupu `step` sekund."""
        for _ in range(count):
            self.submit(worker)
            self.clock.advance(step)
        self.assertTrue(worker.flush(WAIT), worker.status())

    def aggregated(self, worker, done_5m):
        self.assertTrue(worker.wait_until(
            lambda s: s.maintenance.done_5m is not None and s.maintenance.done_5m >= done_5m, WAIT),
            worker.status().maintenance)

    def test_no_maintenance_right_after_opening(self):
        worker = self.make()
        self.advance(worker, AGGREGATION_INTERVAL_SECONDS - 1)
        time.sleep(0.05)
        maintenance = worker.status().maintenance
        self.assertIsNone(maintenance.aggregation_monotonic)  # start sám nic neagreguje
        self.assertIsNone(maintenance.retention_monotonic)

    def test_aggregation_after_the_interval_without_any_data(self):
        worker = self.make()
        self.advance(worker, AGGREGATION_INTERVAL_SECONDS)
        self.settle(worker, lambda s: s.maintenance.aggregation_monotonic is not None, "čekám na agregaci")
        self.assertEqual(worker.status().maintenance.windows_5m, 0)  # není co uzavřít

    def test_aggregation_runs_after_a_confirmed_batch(self):
        worker = self.make()
        self.fill(worker, 31)  # BASE .. BASE+300
        self.advance(worker, self.GRACE + 1)
        self.aggregated(worker, BASE + 300)
        # Dvě řady měření (teplota a vytížení CPU) dávají dva řádky na okno.
        rows = self.query("SELECT bucket, n_samples, n_valid FROM agg_5m ORDER BY bucket")
        self.assertEqual(rows, [(BASE, 30, 30), (BASE, 30, 30)])
        maintenance = worker.status().maintenance
        self.assertEqual((maintenance.windows_5m, maintenance.blocked, maintenance.conflict), (2, None, None))
        self.assertIsNotNone(maintenance.aggregation_utc)

    def test_five_minute_and_hourly_windows(self):
        worker = self.make()
        self.fill(worker, 61, step=60)  # celá hodina vzorků
        self.advance(worker, self.GRACE + 1)
        self.settle(worker, lambda s: s.maintenance.done_1h is not None and s.maintenance.done_1h >= BASE + 3600,
                    "čekám na hodinovou agregaci")
        self.assertEqual(len(self.query("SELECT DISTINCT bucket FROM agg_5m")), 12)
        self.assertEqual(len(self.query("SELECT bucket FROM agg_5m")), 24)  # 12 oken × 2 řady
        self.assertEqual(self.query("SELECT bucket, n_samples FROM agg_1h"), [(BASE, 60), (BASE, 60)])

    def test_unconfirmed_batch_blocks_maintenance(self):
        worker = self.make()
        release = self.hold(worker)  # dávka visí v COMMIT
        self.advance(worker, AGGREGATION_INTERVAL_SECONDS * 2)
        time.sleep(0.05)
        self.assertIsNone(worker.status().maintenance.aggregation_monotonic)
        release.set()
        self.settle(worker, lambda s: s.maintenance.aggregation_monotonic is not None, "agregace po potvrzení")

    def test_sealed_batch_waiting_for_retry_blocks_maintenance(self):
        # Dávka po chybě čeká na odstup: vlákno je volné, ale údržba se nespustí,
        # dokud není výsledek zápisu znám (ani agregace, ani retence).
        worker = self.make()
        self.faults.add(lambda sql: sql == "BEGIN IMMEDIATE",
                        fail(sqlite_error(sqlite3.OperationalError, "SQLITE_BUSY", "database is locked")))
        self.submit(worker)
        self.settle(worker, lambda s: s.error_kind == "busy", "čekám na chybu zápisu")
        self.assertIsNotNone(worker._batch)  # zapečetěná dávka čeká
        with worker._cond:
            worker._aggregate_at = worker._retain_at = 0.0  # údržba by byla na řadě
        worker.wake()
        time.sleep(0.05)
        maintenance = worker.status().maintenance
        self.assertIsNone(maintenance.aggregation_monotonic)
        self.assertIsNone(maintenance.retention_monotonic)
        self.faults.clear()
        self.advance(worker, 10)
        self.commits(worker, 1)
        self.settle(worker, lambda s: s.maintenance.aggregation_monotonic is not None, "agregace po potvrzení")

    def test_conflict_stops_aggregation_and_keeps_writing(self):
        worker = self.make()
        error = history.HistoryError("conflict", "okno 1800003600 už existuje; doklad zapsán")
        with mock.patch.object(history.HistoryRecorder, "aggregate", side_effect=error) as aggregate:
            self.fill(worker, 2)
            self.advance(worker, self.GRACE + 1)
            self.settle(worker, lambda s: s.maintenance.conflict is not None, "čekám na konflikt")
            calls = aggregate.call_count
            self.advance(worker, AGGREGATION_INTERVAL_SECONDS * 3)
            time.sleep(0.05)
            self.assertEqual(aggregate.call_count, calls)  # po konfliktu se už neagreguje
        maintenance = worker.status().maintenance
        self.assertEqual(maintenance.blocked, "conflict")
        self.assertIn("doklad", maintenance.conflict)
        self.assertEqual(worker.status().state, RUNNING)  # měření a zápis pokračují
        self.submit(worker, temp_state=State.WARNING, temp=75.0)
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual([row[3] for row in self.changes()], ["INFO", "WARNING"])

    def test_maintenance_failure_does_not_stop_writing(self):
        worker = self.make()
        busy = history.HistoryError("write", "agregaci nelze zapsat: database is locked", "SQLITE_BUSY")
        with mock.patch.object(history.HistoryRecorder, "aggregate", side_effect=busy):
            self.fill(worker, 2)
            self.advance(worker, self.GRACE + 1)
            self.settle(worker, lambda s: s.maintenance.failures > 0, "čekám na selhání údržby")
        status = worker.status()
        self.assertEqual(status.state, RUNNING)  # zápis měření není v chybě
        self.assertIsNone(status.error_kind)
        self.assertIn("busy", status.maintenance.last_error)
        self.submit(worker, temp_state=State.CRITICAL, temp=95.0)
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual([row[3] for row in self.changes()], ["INFO", "CRITICAL"])
        self.advance(worker, 600)  # po odstupu se údržba zkusí znovu a uspěje
        self.settle(worker, lambda s: s.maintenance.failures == 0, "čekám na obnovu údržby")

    def test_fatal_maintenance_error_stops_the_writer(self):
        worker = self.make()
        corrupt = history.HistoryError("write", "databáze je poškozená", "SQLITE_CORRUPT")
        with mock.patch.object(history.HistoryRecorder, "aggregate", side_effect=corrupt):
            self.fill(worker, 2)
            self.advance(worker, self.GRACE + 1)
            self.settle(worker, lambda s: s.state == FAILED, "čekám na zastavení")
        self.assertEqual(worker.status().error_kind, "corrupt")

    def test_new_measurement_is_written_during_catch_up(self):
        worker = self.make()
        self.fill(worker, 121, step=60)  # dvě hodiny vzorků k dohnání
        self.advance(worker, self.GRACE + 1)
        self.submit(worker, temp_state=State.CRITICAL, temp=95.0)  # nová data během dohánění
        self.assertTrue(worker.flush(WAIT), worker.status())
        self.assertIn("CRITICAL", [row[3] for row in self.changes()])
        self.settle(worker, lambda s: s.maintenance.done_1h is not None and s.maintenance.done_1h >= BASE + 3600,
                    "dohánění pokračuje")

    def seed_days(self, days=3, step=300):
        """Předchozí běh zapsal vzorky za několik dní; spojení se pak zavře.

        Vzorky končí před BASE, takže nová měření zapisovače projdou horní mezí.
        Databáze musí vzniknout ještě dřív: `aggregation_state` začíná hodinou
        vzniku a starší vzorky by pravidla přijetí odmítla jako pozdní.
        """
        for path in (self.db, *history.companion_paths(self.db)):
            path.unlink(missing_ok=True)
        history.create_database(self.db, "test", HOST, now_utc=BASE - (days + 1) * history.DAY_SECONDS)
        database = history.open_writer(self.db, "test", HOST)
        recorder = history.HistoryRecorder(database)
        start = BASE - days * history.DAY_SECONDS
        recorder.start_run("seed", 300, datetime.fromtimestamp(start - step, timezone.utc))
        batch = recorder.begin_batch()
        count = days * history.DAY_SECONDS // step
        for index in range(count):
            moment = start + index * step
            observed = datetime.fromtimestamp(moment, timezone.utc)
            recorder.add_results(batch, [CheckResult(CPU_TEMP_ID, State.INFO, "x", observed,
                                                     1000.0 + index * step, simulated=False,
                                                     measurement=Measurement(50.0, CELSIUS))])
            if len(batch.samples) >= 500:
                recorder.write(batch)
                batch = recorder.begin_batch()
        recorder.write(batch)
        recorder.finish_run(datetime.fromtimestamp(BASE - step, timezone.utc))
        database.close()
        return count

    def test_catch_up_over_several_days_is_progressive(self):
        # Jedno volání uzavře nejvýš hodinu oken, takže se tři dny dohánějí
        # postupně; mezi voláními se smyčka vrací k zápisu a k požadavku na stop.
        self.seed_days(3)
        worker = self.make()
        self.advance(worker, AGGREGATION_INTERVAL_SECONDS)
        self.assertTrue(worker.wait_until(
            lambda s: s.maintenance.done_5m is not None and s.maintenance.done_5m >= BASE - 300, 20),
            worker.status().maintenance)
        maintenance = worker.status().maintenance
        self.assertGreater(maintenance.windows_5m, 12)  # víc než jedna hodina = víc volání
        self.assertGreaterEqual(maintenance.done_1h, BASE - history.HOUR_SECONDS)
        self.assertFalse(maintenance.more)
        self.assertEqual(maintenance.failures, 0)

    def test_wal_size_is_recorded(self):
        worker = self.make()
        self.fill(worker, 2)
        self.settle(worker, lambda s: s.maintenance.wal_bytes is not None, "čekám na velikost WAL")
        self.assertGreater(worker.status().maintenance.wal_bytes, 0)

    def test_stop_during_maintenance_is_clean(self):
        worker = self.make()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = history.HistoryRecorder.aggregate

        def slow(recorder, *args, **kwargs):
            entered.set()
            release.wait(1.0)
            return original(recorder, *args, **kwargs)

        with mock.patch.object(history.HistoryRecorder, "aggregate", slow):
            self.fill(worker, 2)
            self.advance(worker, self.GRACE + 1)
            self.assertTrue(entered.wait(WAIT))
            release.set()
            report = worker.stop(timeout=WAIT)
        self.assertTrue(report.completed and report.clean_stop, report)
        self.assertEqual(self.query("SELECT clean_stop FROM run"), [(1,)])


class RetentionAutomationTest(WorkerTest):
    """Automatická retence: maže jen s doloženou stabilitou hodin a pod agregací."""

    MONTH = 31 * history.DAY_SECONDS

    def prepare(self, worker, count=31, step=10):
        """Zapíše vzorky a nechá je zagregovat, aby je retence vůbec mohla smazat."""
        for _ in range(count):
            self.submit(worker)
            self.clock.advance(step)
        self.assertTrue(worker.flush(WAIT))
        self.advance(worker, history.AGGREGATION_GRACE_SECONDS + 1)
        self.assertTrue(worker.wait_until(
            lambda s: s.maintenance.done_5m is not None and s.maintenance.done_5m >= BASE + 300, WAIT),
            worker.status().maintenance)

    def test_retention_deletes_after_proven_stability(self):
        worker = self.make()
        self.prepare(worker)
        before = self.query("SELECT count(*) FROM sample")[0][0]
        self.run_for(worker, self.MONTH)  # měsíc provozu s průběžným potvrzováním
        self.settle(worker, lambda s: s.maintenance.retention_monotonic is not None, "čekám na retenci")
        maintenance = worker.status().maintenance
        self.assertIsNone(maintenance.retention_blocked)
        self.assertGreater(maintenance.deleted.get("sample", 0), 0)
        self.assertLess(self.query("SELECT count(*) FROM sample")[0][0], before)
        self.assertIsNotNone(history.retained_since(self.query_connection())["sample"])

    def test_retention_is_blocked_just_before_twenty_four_hours(self):
        worker = self.make()
        self.prepare(worker)
        self.run_for(worker, history.RETENTION_STABLE_CLOCK_SECONDS - 600 - 431)
        self.settle(worker, lambda s: s.maintenance.retention_monotonic is not None, "čekám na pokus o retenci")
        maintenance = worker.status().maintenance
        self.assertEqual(maintenance.retention_blocked, "clock_unstable")
        self.assertEqual(maintenance.deleted.get("sample", 0), 0)

    def test_lost_synchronization_blocks_retention(self):
        worker = self.make()
        self.prepare(worker)
        self.run_for(worker, self.MONTH)
        self.submit(worker, synced=False)  # ztráta synchronizace ruší doloženou stabilitu
        worker.wake()
        self.settle(worker, lambda s: s.maintenance.retention_monotonic is not None, "čekám na pokus o retenci")
        self.assertEqual(worker.status().maintenance.retention_blocked, "time_unsynced")
        self.assertEqual(worker.status().maintenance.deleted.get("sample", 0), 0)

    def test_retention_never_passes_the_aggregation_boundary(self):
        worker = self.make()
        for _ in range(5):  # vzorky, které se nikdy neagregovaly
            self.submit(worker)
            self.clock.advance(10)
        self.assertTrue(worker.flush(WAIT))
        before = self.query("SELECT count(*) FROM sample")[0][0]
        self.run_for(worker, self.MONTH)
        self.settle(worker, lambda s: s.maintenance.retention_monotonic is not None, "čekám na retenci")
        self.assertIsNone(worker.status().maintenance.retention_blocked)
        self.assertEqual(self.query("SELECT count(*) FROM sample")[0][0], before)  # nic bez agregace

    def test_aggregation_conflict_does_not_open_the_way_to_retention(self):
        # Po konfliktu se hranice agregace nehýbe, takže retence nemůže smazat
        # nic nového: surové vzorky zůstanou nad potvrzeným done_until.
        worker = self.make()
        for _ in range(31):
            self.submit(worker)
            self.clock.advance(10)
        self.assertTrue(worker.flush(WAIT))
        before = self.query("SELECT count(*) FROM sample")[0][0]
        error = history.HistoryError("conflict", "okno už existuje; doklad zapsán")
        with mock.patch.object(history.HistoryRecorder, "aggregate", side_effect=error):
            self.advance(worker, history.AGGREGATION_GRACE_SECONDS + 1)
            self.settle(worker, lambda s: s.maintenance.conflict is not None, "čekám na konflikt")
            self.run_for(worker, self.MONTH)
            self.settle(worker, lambda s: s.maintenance.retention_monotonic is not None, "čekám na retenci")
        maintenance = worker.status().maintenance
        self.assertIsNone(maintenance.retention_blocked)  # čas je v pořádku
        self.assertEqual(maintenance.deleted.get("sample", 0), 0)  # přesto se nic nesmazalo
        self.assertEqual(self.query("SELECT count(*) FROM sample")[0][0], before)

    def test_retention_runs_at_most_once_an_hour(self):
        worker = self.make()
        self.prepare(worker)
        self.run_for(worker, self.MONTH)
        self.settle(worker, lambda s: s.maintenance.retention_monotonic is not None, "první retence")
        first = worker.status().maintenance.retention_monotonic
        self.run_for(worker, RETENTION_INTERVAL_SECONDS - 300, step=300)
        time.sleep(0.05)
        self.assertEqual(worker.status().maintenance.retention_monotonic, first)
        self.advance(worker, 300)
        self.settle(worker, lambda s: s.maintenance.retention_monotonic != first, "druhá retence po hodině")

    def test_stop_during_retention_is_clean(self):
        worker = self.make()
        self.prepare(worker)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = history.HistoryRecorder.retain

        def slow(recorder, *args, **kwargs):
            entered.set()
            release.wait(1.0)
            return original(recorder, *args, **kwargs)

        with mock.patch.object(history.HistoryRecorder, "retain", slow):
            self.run_for(worker, self.MONTH)
            self.assertTrue(entered.wait(WAIT))
            release.set()
            report = worker.stop(timeout=WAIT)
        self.assertTrue(report.completed and report.clean_stop, report)


class ClockJumpEvidenceTest(WorkerTest):
    """Doklad skoku hodin: zápis, dohledatelnost a vliv na retenci."""

    def jump(self, worker, shift=3600):
        """Vyvolá skok systémového času mezi dvěma měřeními."""
        self.submit(worker, synced=True)
        self.clock.advance(10)
        self.clock.set_utc(self.clock.utc().timestamp() + shift)
        self.submit(worker, temp_state=State.WARNING, temp=75.0, synced=True)

    def test_jump_event_reaches_the_database(self):
        worker = self.make()
        self.jump(worker)
        self.assertTrue(worker.flush(WAIT))
        events = self.events("clock.jump")
        self.assertEqual(len(events), 1)
        self.assertGreater(events[0][3]["shift_s"], 3000)
        self.assertEqual(events[0][2], "WARNING")
        self.assertEqual(self.stable(worker), 0)  # a zároveň ruší doloženou stabilitu

    def test_jump_event_survives_a_transient_error(self):
        # Přechodná chyba SQLite událost neztratí tiše: zůstane ve frontě dávky.
        worker = self.make()
        self.faults.add(lambda sql: sql == "BEGIN IMMEDIATE",
                        fail(sqlite_error(sqlite3.OperationalError, "SQLITE_BUSY", "database is locked")), times=1)
        self.jump(worker)
        self.settle(worker, lambda s: s.error_kind == "busy", "čekám na chybu zápisu")
        self.assertEqual(self.events("clock.jump"), [])  # ještě nezapsaná
        self.advance(worker, 10)
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual(len(self.events("clock.jump")), 1)  # po obnovení dohledatelná

    def test_recorded_jump_blocks_retention_for_twenty_four_hours(self):
        # Doklad v databázi platí i proti doložené stabilitě v paměti.
        worker = self.make()
        self.run_for(worker, history.RETENTION_STABLE_CLOCK_SECONDS)
        state = worker._clock_state()
        self.assertEqual(state.stable_s, history.RETENTION_STABLE_CLOCK_SECONDS)
        now = math.floor(self.clock.utc().timestamp())
        worker.submit_event(history.Event(now - 3600, "synced", "clock.jump", "WARNING", "skok o +5 s",
                                          data={"shift_s": 5.0}))
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual(history._retention_guard(self.query_connection(), now, state), "clock_jump_recent")

    def test_unrecorded_event_loss_blocks_retention(self):
        # Ztracená událost mohla být doklad skoku: dokud o ztrátě není záznam
        # v databázi, retence se nepovažuje za bezpečnou, i když je stabilita
        # doložená. Po zapsání ztráty je mezera viditelná a skok v tomto běhu
        # hlídá i vlastní detekce posunu času.
        worker = self.make(event_capacity=1)
        self.run_for(worker, history.RETENTION_STABLE_CLOCK_SECONDS)
        self.assertTrue(worker._clock_state().synced)
        release = self.hold(worker)  # zapisovač drží dávku: fronta událostí přeteče
        for index in range(3):
            worker.submit_event(history.Event(BASE + index, "synced", "test.e", "INFO", "x"))
        status = worker.status()
        self.assertGreater(status.losses.get("events_dropped", 0), 0)
        self.assertGreater(status.unrecorded_losses.get("events_dropped", 0), 0)
        self.assertFalse(worker._clock_state().synced)  # doklad skoku mohl zmizet
        release.set()
        self.assertTrue(worker.flush(WAIT))
        self.assertEqual(len(self.events("history.events_dropped")), 1)  # ztráta je doložená
        self.assertTrue(worker._clock_state().synced)

    def test_oversized_event_blocks_retention_until_recorded(self):
        worker = self.make()
        self.run_for(worker, history.RETENTION_STABLE_CLOCK_SECONDS)
        huge = history.Event(BASE, "synced", "test.big", "INFO", "x" * (history_worker.MAX_EVENT_BYTES + 1))
        release = self.hold(worker)
        self.assertFalse(worker.submit_event(huge))
        self.assertEqual(worker.status().losses.get("event_too_large"), 1)
        self.assertFalse(worker._clock_state().synced)
        release.set()
        self.assertTrue(worker.flush(WAIT))
        self.assertTrue(worker._clock_state().synced)  # po zapsání dokladu ztráty


class StarvationTest(WorkerTest):
    """Zápis má přednost: údržba nesmí vyhladovět měření ani se zacyklit."""

    def test_measurements_are_not_dropped_during_catch_up(self):
        # Tři dny oken k dohnání a měření každou sekundu: nic se nesmí zahodit.
        MaintenanceTest.seed_days(self, days=3)
        worker = self.make(interval_s=1)
        self.advance(worker, AGGREGATION_INTERVAL_SECONDS)
        for index in range(60):  # kmitání dvou kontrol, nová data každou sekundu
            self.submit(worker, temp_state=State.CRITICAL if index % 2 else State.INFO,
                        temp=95.0 if index % 2 else 40.0)
            self.clock.advance(1)
            worker.wake()
        self.assertTrue(worker.flush(WAIT), worker.status())
        status = worker.status()
        self.assertEqual(status.losses.get("queue_full", 0), 0)
        self.assertEqual(status.confirmed_measurements, 60)
        self.assertGreaterEqual(len(self.changes()), 60)  # všechny přechody zapsané
        self.assertTrue(worker.wait_until(
            lambda s: s.maintenance.done_5m is not None and s.maintenance.done_5m >= BASE - 300, 20),
            worker.status().maintenance)

    def test_new_event_during_catch_up_is_written(self):
        MaintenanceTest.seed_days(self, days=2)
        worker = self.make()
        self.advance(worker, AGGREGATION_INTERVAL_SECONDS)
        worker.submit_event(history.Event(BASE, "synced", "test.urgent", "WARNING", "během dohánění"))
        self.settle(worker, lambda s: s.confirmed_events >= 1, "událost během dohánění")
        self.assertEqual(len(self.events("test.urgent")), 1)

    def test_stop_during_catch_up_is_clean(self):
        MaintenanceTest.seed_days(self, days=2)
        worker = self.make()
        self.advance(worker, AGGREGATION_INTERVAL_SECONDS)
        report = worker.stop(timeout=WAIT)
        self.assertTrue(report.completed and report.clean_stop, report)

    def test_catch_up_without_progress_does_not_spin(self):
        # Bez posunu hranice se další pokus odloží na interval (žádná smyčka).
        worker = self.make()
        self.fill = MaintenanceTest.fill.__get__(self)
        self.fill(worker, 2)
        stuck = history.AggregationResult(done_5m=BASE, done_1h=BASE, windows_5m=0, windows_1h=0, more=True)
        with mock.patch.object(history.HistoryRecorder, "aggregate", return_value=stuck) as aggregate:
            self.advance(worker, AGGREGATION_INTERVAL_SECONDS)
            self.settle(worker, lambda s: s.maintenance.more, "čekám na more")
            calls = aggregate.call_count
            time.sleep(0.1)
            self.assertLessEqual(aggregate.call_count - calls, 1)


class WalTest(WorkerTest):
    """Velikost WAL pochází ze skutečného souboru správné databáze."""

    def test_no_automatic_truncate_or_vacuum(self):
        # Údržba velikost souboru nikdy neupravuje: žádný VACUUM ani vynucený
        # checkpoint (ani v podobě wal_checkpoint(TRUNCATE), HISTORY.md 5.7, 7).
        # "result_truncated" je zkrácení textu výsledku, ne operace nad souborem.
        for name in ("history.py", "history_worker.py", "history_link.py", "run.py"):
            source = (REPO / "src" / "nexusstat" / name).read_text(encoding="utf-8").lower()
            for forbidden in ("vacuum", "wal_checkpoint", "checkpoint(", "truncate)"):
                with self.subTest(module=name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)

    def test_size_is_read_from_the_real_file(self):
        worker = self.make()
        MaintenanceTest.fill.__get__(self)(worker, 2)
        self.settle(worker, lambda s: s.maintenance.wal_bytes is not None, "čekám na velikost WAL")
        real = (self.folder / "history.sqlite3-wal").stat().st_size
        self.assertEqual(worker.status().maintenance.wal_bytes, real)

    def test_symlinked_database_reports_the_real_wal(self):
        link = self.folder / "odkaz.sqlite3"
        link.symlink_to(self.db)
        worker = self.make(start=False)
        worker._path = link  # cesta z konfigurace může být symlink
        self.assertEqual(worker._wal_bytes(), None if not (self.folder / "history.sqlite3-wal").exists()
                         else (self.folder / "history.sqlite3-wal").stat().st_size)
        worker.start(time_synced=True)
        self.settle(worker, lambda s: s.state != "STARTING")
        MaintenanceTest.fill.__get__(self)(worker, 2)
        self.settle(worker, lambda s: s.maintenance.wal_bytes is not None, "čekám na velikost WAL")
        self.assertEqual(worker.status().maintenance.wal_bytes,
                         (self.folder / "history.sqlite3-wal").stat().st_size)

    def test_missing_wal_is_not_an_error(self):
        worker = self.make(start=False)
        worker._path = self.folder / "neexistuje.sqlite3"
        self.assertIsNone(worker._wal_bytes())

    def test_unreadable_wal_is_unknown_not_zero(self):
        worker = self.make(start=False)
        with mock.patch.object(os.path, "getsize", side_effect=PermissionError("nelze")):
            self.assertIsNone(worker._wal_bytes())


class StabilityTest(WorkerTest):
    """Evidence souvislé synchronizace hodin pro retenci (ClockState, M3.3b-2).

    Nic se nepředpokládá: stabilitu dokládají jen pozorované odpovědi zdroje
    a monotónní čas. Systémový čas se nikde nenastavuje.
    """

    def test_start_with_synchronized_clock_starts_from_zero(self):
        worker = self.make(synced=True)
        self.assertEqual(self.stable(worker), 0)  # po startu se 24 h nemaže
        self.run_for(worker, 3600)  # hodina provozu s průběžným potvrzováním
        self.assertEqual(self.stable(worker), 3600)

    def test_start_without_synchronization_has_no_stability(self):
        self.clock.set_utc(WRONG)
        worker = self.make(synced=False)
        self.assertIsNone(self.stable(worker))
        self.assertFalse(worker._clock_state().synced)

    def test_first_synchronization_starts_the_interval(self):
        self.clock.set_utc(WRONG)
        worker = self.make(synced=False)
        self.submit(worker, synced=False)
        self.assertIsNone(self.stable(worker))
        self.clock.advance(10)
        self.clock.set_utc(BASE + 10)
        self.submit(worker, synced=True)  # první doložená synchronizace
        self.assertEqual(self.stable(worker), 0)
        self.run_for(worker, 60, step=10)
        self.assertEqual(self.stable(worker), 60)

    def test_loss_and_recovery_restart_the_interval(self):
        worker = self.make(synced=True)
        self.run_for(worker, 7200)
        self.assertEqual(self.stable(worker), 7200)
        self.submit(worker, synced=False)  # ztráta synchronizace
        self.assertIsNone(self.stable(worker))
        self.clock.advance(10)
        self.submit(worker, synced=True)  # obnova: interval začíná znovu
        self.assertEqual(self.stable(worker), 0)

    def test_unknown_state_cancels_stability(self):
        worker = self.make(synced=True)
        self.run_for(worker, 86400)
        self.assertEqual(self.stable(worker), 86400)
        self.submit(worker, synced=None)  # neznámý stav nic nedokládá
        self.assertIsNone(self.stable(worker))
        self.assertFalse(worker._clock_state().synced)

    def test_short_outage_restarts_the_interval(self):
        worker = self.make(synced=True)
        self.run_for(worker, 86400)
        self.assertEqual(self.stable(worker), 86400)
        self.clock.advance(10)
        self.submit(worker, synced=None)  # krátký výpadek NTP
        self.clock.advance(10)
        self.submit(worker, synced=True)
        self.assertEqual(self.stable(worker), 0)  # 24 h se počítá znovu

    def test_restart_of_the_process_does_not_inherit_stability(self):
        worker = self.make(synced=True)
        self.run_for(worker, 86400 * 2)
        self.submit(worker, synced=True)
        self.assertTrue(worker.flush(WAIT))
        worker.stop(timeout=WAIT)
        second = self.make(synced=True, boot_id="jiny-boot-id")
        self.assertEqual(self.stable(second), 0)  # jiný proces i boot: od nuly
        self.assertEqual(second.status().maintenance.stable_s, 0)

    def test_forward_jump_restarts_the_interval(self):
        worker = self.make(synced=True)
        self.run_for(worker, 86400)
        self.submit(worker, synced=True)
        self.clock.advance(10)
        self.clock.set_utc(BASE + 86400 + 10 + 3600)  # skok UTC dopředu o hodinu
        self.submit(worker, synced=True)
        self.assertEqual(self.stable(worker), 0)

    def test_backward_jump_restarts_the_interval(self):
        worker = self.make(synced=True)
        self.run_for(worker, 86400)
        self.submit(worker, synced=True)
        self.clock.advance(10)
        self.clock.set_utc(BASE + 86400)  # skok UTC dozadu o 10 s
        self.submit(worker, synced=True)
        self.assertEqual(self.stable(worker), 0)

    def test_jump_threshold_is_the_documented_two_seconds(self):
        # Práh je stejný jako u události clock.jump (HISTORY.md 4.3): běžné
        # doladění NTP je řádově milisekundy, 2 s už je skok.
        for drift, restarts in ((history.CLOCK_JUMP_SECONDS, False), (history.CLOCK_JUMP_SECONDS + 0.5, True)):
            with self.subTest(drift=drift):
                self.setUp()
                worker = self.make(synced=True)
                self.run_for(worker, 3600)
                self.submit(worker, synced=True)
                self.clock.advance(10)
                self.clock.set_utc(BASE + 3610 + drift)
                self.submit(worker, synced=True)
                self.assertEqual(self.stable(worker) == 0, restarts)

    def test_retention_boundary_is_exactly_twenty_four_hours(self):
        worker = self.make(synced=True)
        self.run_for(worker, history.RETENTION_STABLE_CLOCK_SECONDS - 60)
        state = worker._clock_state()
        self.assertEqual(state.stable_s, history.RETENTION_STABLE_CLOCK_SECONDS - 60)
        self.assertEqual(history._retention_guard(self.query_connection(), BASE, state), "clock_unstable")
        self.run_for(worker, 60, step=60)
        state = worker._clock_state()
        self.assertEqual(state.stable_s, history.RETENTION_STABLE_CLOCK_SECONDS)
        self.assertIsNone(history._retention_guard(self.query_connection(), BASE + 86400, state))

    # --- průběžnost pozorování (M3.3b-2, review) ---------------------------------------

    def test_single_confirmation_without_further_measurements_is_not_proof(self):
        # Měřicí smyčka může být mrtvá: samotný uplynulý monotónní čas nic nedokládá.
        worker = self.make(synced=True)
        self.clock.advance(history.RETENTION_STABLE_CLOCK_SECONDS)
        state = worker._clock_state()
        self.assertFalse(state.synced, f"stabilita bez pozorování: {state}")
        self.assertIsNone(worker.status().maintenance.stable_s)

    def test_outage_of_six_hours_breaks_the_interval(self):
        worker = self.make(synced=True)
        self.submit(worker, synced=True)
        self.clock.advance(6 * 3600)  # šest hodin bez měření
        self.submit(worker, synced=True)
        self.assertEqual(self.stable(worker), 0)

    def test_outage_of_twenty_five_hours_breaks_the_interval(self):
        worker = self.make(synced=True)
        self.submit(worker, synced=True)
        self.clock.advance(25 * 3600)
        self.submit(worker, synced=True)
        state = worker._clock_state()
        self.assertEqual(state.stable_s, 0)  # dvě vzdálená pozorování nejsou 25 h stability

    def test_regular_confirmations_prove_the_span(self):
        worker = self.make(synced=True)
        start = self.clock.monotonic()
        step = 60
        for index in range(1, history.RETENTION_STABLE_CLOCK_SECONDS // step + 1):
            self.observe(worker, start + index * step)
        self.clock.advance(history.RETENTION_STABLE_CLOCK_SECONDS)
        state = worker._clock_state()
        self.assertTrue(state.synced)
        self.assertGreaterEqual(state.stable_s, history.RETENTION_STABLE_CLOCK_SECONDS)

    def test_exact_boundary_with_regular_confirmations(self):
        worker = self.make(synced=True)
        start = self.clock.monotonic()
        step = 60
        for index in range(1, history.RETENTION_STABLE_CLOCK_SECONDS // step):
            self.observe(worker, start + index * step)
        self.clock.advance(history.RETENTION_STABLE_CLOCK_SECONDS - step)
        self.assertEqual(worker._clock_state().stable_s, history.RETENTION_STABLE_CLOCK_SECONDS - step)
        self.observe(worker, start + history.RETENTION_STABLE_CLOCK_SECONDS)
        self.clock.advance(step)
        self.assertEqual(worker._clock_state().stable_s, history.RETENTION_STABLE_CLOCK_SECONDS)

    def test_unobserved_time_is_never_counted(self):
        # Čas, kdy se nikdo neptal, se do doloženého rozsahu nepřičítá — ani když
        # je poslední potvrzení ještě čerstvé.
        worker = self.make(synced=True)
        self.run_for(worker, 3600)
        self.assertEqual(self.stable(worker), 3600)
        self.clock.advance(120)  # čerstvé (pod sync_fresh_seconds), ale bez pozorování
        self.assertEqual(self.stable(worker), 3600)

    def test_boundary_is_not_reached_by_unobserved_time(self):
        worker = self.make(synced=True)
        self.run_for(worker, history.RETENTION_STABLE_CLOCK_SECONDS - 120)
        self.clock.advance(120)
        state = worker._clock_state()
        self.assertEqual(state.stable_s, history.RETENTION_STABLE_CLOCK_SECONDS - 120)
        self.assertEqual(history._retention_guard(self.query_connection(), BASE + 86400, state), "clock_unstable")

    def test_stale_evidence_blocks_retention_even_after_a_long_series(self):
        worker = self.make(synced=True)
        start = self.clock.monotonic()
        for index in range(1, history.RETENTION_STABLE_CLOCK_SECONDS // 60 + 1):
            self.observe(worker, start + index * 60)
        self.clock.advance(history.RETENTION_STABLE_CLOCK_SECONDS)
        self.assertTrue(worker._clock_state().synced)
        self.clock.advance(2 * 3600)  # měření přestala přicházet
        self.assertFalse(worker._clock_state().synced)

    def test_resume_from_suspend_breaks_the_interval(self):
        # Po uspání notebooku monotónní čas stojí, UTC jde dál: to je skok.
        worker = self.make(synced=True)
        self.submit(worker, synced=True)
        self.clock.advance(1)
        self.clock.set_utc(BASE + 7200)  # dvě hodiny uspání
        self.submit(worker, synced=True)
        self.assertEqual(self.stable(worker), 0)

    def test_measurement_without_results_extends_the_interval(self):
        # Měření bez výsledků je stále pozorování zdroje: jen nejde porovnat čas.
        worker = self.make(synced=True)
        self.clock.advance(60)
        worker.submit([], self.clock.monotonic(), True)
        self.assertEqual(self.stable(worker), 60)


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
