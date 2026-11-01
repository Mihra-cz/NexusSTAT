"""Testy čtení historie pro grafy lokálního webu (M6-2).

Databáze je malá syntetická SQLite v dočasném adresáři (mimo repozitář),
vytvořená a naplněná přes history.create_database a open_writer. Čtení jde
jen přes history.open_reader. Čas je pevný, nic se nečeká.
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from nexusstat import history, history_charts
from nexusstat.history_charts import RANGES, ChartError, HistoryCharts, read_charts

HOST = "0123456789abcdef"
END = 1_800_000_000 - 1_800_000_000 % 7200  # konec rozsahů, zarovnaný na všechny kroky
CPU, TEMP, RX, DISK = 1, 2, 3, 4


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def build(directory, samples=(), aggregates=(), done_5m=END - 3600):
    """Syntetická databáze: řady CPU, teplota, RX na eth0 a disk; vrací cestu."""
    path = Path(directory) / "history.sqlite3"
    history.create_database(path, "test", HOST, now_utc=END - 40 * 86_400)
    database = history.open_writer(path, "test", HOST)
    try:
        connection = database.connection
        connection.executemany("INSERT INTO metric (id, check_id, subject, unit, created_utc) VALUES (?, ?, ?, ?, 0)",
                               [(CPU, "system.cpu_usage", "", "%"), (TEMP, "system.cpu_temp", "", "°C"),
                                (RX, "network.rx_rate", "eth0", "B/s"), (DISK, "disk.root", "", "%")])
        connection.executemany("INSERT INTO sample (metric_id, ts, value) VALUES (?, ?, ?)", samples)
        connection.executemany("INSERT INTO agg_5m VALUES (?, ?, ?, ?, ?, ?, ?)", aggregates)
        connection.execute("UPDATE aggregation_state SET done_until = ? WHERE level = '5m'", (done_5m,))
    finally:
        database.close()
    return path


def charts(path, name, **options):
    source = HistoryCharts(path, "test", 10, utc_clock=lambda: END - 1, host_id_factory=lambda: HOST, **options)
    return source, source.charts(name)


def series(result, metric_check):
    [found] = [item for item in result.series if item.check_id == metric_check]
    return found


class ReadTest(unittest.TestCase):
    def test_short_ranges_bucket_raw_samples_and_keep_unknown_and_gaps(self):
        start = END - 3600
        samples = [(CPU, start + 0, 10.0), (CPU, start + 10, None),  # UNKNOWN
                   (CPU, start + 20, 30.0), (CPU, start + 300, 50.0)]  # mezi 30 a 300 s nic
        with tempfile.TemporaryDirectory() as directory:
            source, result = charts(build(directory, samples), "1h")
            cpu = series(result, "system.cpu_usage")
            self.assertEqual([(b.start - start, b.n_samples, b.n_valid, b.average) for b in cpu.buckets],
                             [(0, 1, 1, 10.0), (10, 1, 0, None), (20, 1, 1, 30.0), (300, 1, 1, 50.0)])
            self.assertEqual(result.queries, 2)  # řady + vzorky jedním dotazem pro všechny řady
            # 24 h: okna po 5 min už spočítá SQL; průměr jen z platných hodnot, UNKNOWN není nula.
            _, day = charts(Path(directory) / "history.sqlite3", "24h")
            [window] = series(day, "system.cpu_usage").buckets[:1]
            self.assertEqual((window.n_samples, window.n_valid, window.minimum, window.maximum, window.average),
                             (3, 2, 10.0, 30.0, 20.0))
            self.assertLessEqual(max(len(item.buckets) for item in day.series), 86_400 // 300 + 1)

    def test_long_ranges_combine_aggregates_and_recent_samples(self):
        done = END - 3600
        aggregates = [(CPU, done - 1800, 30, 30, 5.0, 15.0, 300.0),  # průměr 10 z 30 platných
                      (CPU, done - 1500, 30, 10, 40.0, 60.0, 500.0),  # průměr 50 jen z 10 platných
                      (CPU, END - 7 * 86_400, 30, 0, None, None, None)]  # první okno rozsahu jen s UNKNOWN
        samples = [(CPU, done + 10, 70.0), (CPU, done + 20, None), (CPU, done - 10, 999.0)]  # poslední už agregováno
        with tempfile.TemporaryDirectory() as directory:
            source, week = charts(build(directory, samples, aggregates, done_5m=done), "7d")
            buckets = {b.start: b for b in series(week, "system.cpu_usage").buckets}
            merged = buckets[done - 1800]
            # 800 / 40 platných = 20, ne průměr průměrů (30); vzorek před done_until se nezapočte.
            self.assertEqual((merged.n_samples, merged.n_valid, merged.average, merged.maximum), (60, 40, 20.0, 60.0))
            tail = buckets[done]
            self.assertEqual((tail.n_samples, tail.n_valid, tail.average), (2, 1, 70.0))  # neagregovaná okna ze sample
            self.assertIsNone(buckets[END - 7 * 86_400].average)
            self.assertEqual(week.queries, 4)  # řady, done_until, agregace, nejnovější vzorky
            _, month = charts(Path(directory) / "history.sqlite3", "30d")
            self.assertLessEqual(max(len(item.buckets) for item in month.series), 30 * 86_400 // 7200 + 1)
            for name, chart_range in RANGES.items():
                self.assertLessEqual(chart_range.seconds // chart_range.step, 400, name)  # stovky bodů na řadu

    def test_unavailable_history_is_an_error_not_a_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sqlite3"
            self.assertEqual(charts(missing, "1h")[1].reason, "path")
            self.assertEqual(list(Path(directory).iterdir()), [])  # nic nevzniklo
            foreign = Path(directory) / "foreign.sqlite3"
            foreign.write_bytes(b"not a database" * 10)
            self.assertEqual(charts(foreign, "1h")[1].reason, "foreign")
            path = build(directory)
            source = HistoryCharts(path, "test", 10, host_id_factory=lambda: "fedcba9876543210")
            self.assertEqual(source.charts("1h").reason, "host")  # cizí databáze se nečte
            database = history.open_reader(path, role="test", host_id=HOST)
            try:  # dotaz po limitu SQLite přeruší (kontrola po každém kroku)
                with mock.patch.object(history_charts, "PROGRESS_STEPS", 1), self.assertRaises(ChartError) as caught:
                    read_charts(database.connection, RANGES["1h"], END, deadline=0)
                self.assertFalse(database.connection.in_transaction)  # čtecí transakce skončila
            finally:
                database.close()
            self.assertEqual(caught.exception.reason, "timeout")
            with self.assertRaises(ValueError):
                source.charts("1y")

    def test_results_and_errors_are_cached_and_connections_closed(self):
        opened = []

        def opener(path, role, host_id):
            database = history.open_reader(path, role=role, host_id=host_id)
            opened.append(database)
            return database

        with tempfile.TemporaryDirectory() as directory:
            path = build(directory, [(CPU, END - 100, 1.0)])
            clock = Clock()
            source = HistoryCharts(path, "test", 10, clock=clock, utc_clock=lambda: END - 1,
                                   host_id_factory=lambda: HOST, opener=opener)
            first = source.charts("1h")
            clock.now += RANGES["1h"].cache_seconds - 1
            self.assertIs(source.charts("1h"), first)  # obnova stránky nečte disk znovu
            source.charts("24h")
            clock.now += 1
            source.charts("1h")
            self.assertEqual(source.reads, 3)
            self.assertTrue(opened and all(database.connection is None for database in opened))  # vše zavřené
            missing = HistoryCharts(Path(directory) / "missing.sqlite3", "test", 10, clock=clock,
                                    host_id_factory=lambda: HOST)
            missing.charts("1h")
            clock.now += history_charts.ERROR_CACHE_SECONDS - 1
            missing.charts("1h")
            self.assertEqual(missing.reads, 1)  # nedostupnou databázi nezkouší při každé obnově

    def test_reading_while_the_writer_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = build(directory)
            stop, errors, written, ready = threading.Event(), [], [0], threading.Event()

            def write():
                # Zapisovač ve vlastním vlákně a s vlastním spojením jako history_worker.
                writer = history.open_writer(path, "test", HOST)
                ready.set()
                try:
                    ts = END - 3000
                    while not stop.is_set():
                        writer.connection.execute("BEGIN")
                        writer.connection.execute("INSERT INTO sample VALUES (?, ?, ?)", (CPU, ts, 1.0))
                        writer.connection.execute("COMMIT")
                        ts += 10
                        written[0] += 1
                except Exception as error:  # noqa: BLE001
                    errors.append(error)
                finally:
                    writer.close()

            thread = threading.Thread(target=write)
            thread.start()
            ready.wait(10)
            counts = []
            try:
                for _ in range(30):
                    database = history.open_reader(path, role="test", host_id=HOST)
                    try:
                        result = read_charts(database.connection, RANGES["1h"], END - 1, deadline=float("inf"))
                    finally:
                        database.close()
                    counts.append(sum(b.n_samples for b in series(result, "system.cpu_usage").buckets))
            finally:
                stop.set()
                thread.join()
            self.assertEqual(errors, [])
            self.assertGreater(written[0], 0)
            self.assertEqual(counts, sorted(counts))  # čtenář vidí vždy úplné potvrzené transakce

    def test_reader_is_read_only_by_construction(self):
        # Jen SELECT nad pevnými tabulkami; spojení dodá jedině open_reader (mode=ro, query_only, bez immutable).
        sql = " ".join(value.upper() for name, value in vars(history_charts).items() if name.endswith("_SQL"))
        self.assertIn("SELECT", sql)
        for word in ("INSERT", "UPDATE ", "DELETE", "CREATE", "DROP", "IMMUTABLE", "CHECKPOINT", "VACUUM", "PRAGMA"):
            self.assertNotIn(word, sql)
        self.assertIs(HistoryCharts.__init__.__defaults__[-1], history.open_reader)
        with tempfile.TemporaryDirectory() as directory:
            path = build(directory, [(CPU, END - 100, 1.0)])
            statements = []

            def tracing_opener(path, role, host_id):
                database = history.open_reader(path, role=role, host_id=host_id)
                database.connection.set_trace_callback(statements.append)
                return database

            HistoryCharts(path, "test", 10, utc_clock=lambda: END - 1, host_id_factory=lambda: HOST,
                          opener=tracing_opener).charts("7d")
        self.assertTrue(statements)
        self.assertEqual({statement.split()[0] for statement in statements}, {"BEGIN", "SELECT", "ROLLBACK"})


if __name__ == "__main__":
    unittest.main()
