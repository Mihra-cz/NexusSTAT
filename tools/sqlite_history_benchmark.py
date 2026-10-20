#!/usr/bin/env python3
"""Benchmark a ověření návrhu SQLite historie NexusSTAT (milník M2).

Jen syntetická data v novém dočasném adresáři, který se na konci smaže
(--keep ho ponechá). Nepoužívá kód aplikace, síť ani cesty Nexusu. Výsledky
platí pro počítač, na kterém běží; nejsou to měření Raspberry Pi 5.

Použití:
  python3 tools/sqlite_history_benchmark.py            # plný běh, řádově minuty
  python3 tools/sqlite_history_benchmark.py --quick    # zmenšená data, kontrola skriptu
  python3 tools/sqlite_history_benchmark.py --only selfcheck
  TMPDIR=/jiny/docasny/adresar python3 tools/sqlite_history_benchmark.py

Postup:
1. Spustí SQL z docs/HISTORY.md (bloky <!-- sql:... -->) a ověří slíbené
   chování: nula vs. UNKNOWN vs. mezera, duplicita a konflikt, ochrana dávky,
   agregace bez přepisu, hodinová agregace, retence, restart, omezení typů.
2. Ověří model pravidel času a identity (HISTORY.md 4.1–4.3), stav snímku
   (9.3), regresní scénáře R4 (7.1) a předpoklad jediného zapisovače (5.8).
3. Porovná tři uspořádání tabulky surových vzorků.
4. Porovná 1min, 5min a hodinové agregace a změří check_change při kmitání.
5. Dlouhodobý cyklus zápis → agregace → retence → snímek nad celou navrženou
   databází, souběžné čtení, dlouhá čtecí transakce, zkouška obnovy.
"""

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "HISTORY.md"

START = 1767225600  # 2026-01-01 00:00:00 UTC, pevný začátek syntetických dat
INTERVAL = 10  # s, jako výchozí [collector] interval_seconds
HOUR = 3600
DAY = 86400
MB = 1_000_000
APPLICATION_ID = 1314411348  # 'NXST'
GRACE = 120  # s rezervy po konci okna před agregací (HISTORY.md 6)
SNAPSHOT_AT = 2 * HOUR + 30 * 60  # snímek denně 02:30 UTC (HISTORY.md 9.2)

# Výpadek procesu každý den 03:00–03:05 UTC: žádné řádky (mezera), po něm
# první vzorek CPU a sítě UNKNOWN jako po skutečném startu.
GAP_START, GAP_LENGTH = 3 * HOUR, 300
UNKNOWN_SHARE = 0.003  # podíl náhodných UNKNOWN (NULL)

# 20 plánovaných číselných řad (předpoklad pro odhad v docs/HISTORY.md):
# 6 dnešních a 14 budoucích. Druh určuje tvar hodnot, protože SQLite ukládá
# celé číslo menší než desetinné a velikost na tom závisí.
SERIES = [
    ("system.cpu_usage", "pct"), ("system.cpu_temp", "temp"), ("system.memory", "pct_slow"),
    ("disk.root", "pct_slow"), ("network.rx_rate", "rate"), ("network.tx_rate", "rate"),
    ("fan.cpu_rpm", "rpm"), ("fan.cpu_level", "level"), ("storage.nvme_temp", "int_temp"),
    ("storage.a_used", "pct_slow"), ("storage.b_used", "pct_slow"), ("storage.c_used", "pct_slow"),
    ("storage.d_used", "pct_slow"), ("storage.a_free", "bytes"), ("storage.b_free", "bytes"),
    ("nas.latency", "ms"), ("backup.age", "age"), ("system.load1", "load"),
    ("system.cpu_freq", "mhz"), ("system.throttled", "flag"),
]
FIRST_SAMPLE_UNKNOWN = {0, 4, 5}  # CPU, RX, TX potřebují dva vzorky

METRIC_DDL = """CREATE TABLE metric (
    id INTEGER PRIMARY KEY, check_id TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL, created_utc INTEGER NOT NULL, UNIQUE (check_id, subject)) STRICT"""

RAW_VARIANTS = {
    "A": ("ROWID + unikátní index (metric_id, ts)", [
        """CREATE TABLE sample (id INTEGER PRIMARY KEY, metric_id INTEGER NOT NULL REFERENCES metric (id),
           ts INTEGER NOT NULL, value REAL) STRICT""",
        "CREATE UNIQUE INDEX sample_metric_ts ON sample (metric_id, ts)",
    ]),
    "B": ("WITHOUT ROWID, klíč (metric_id, ts)", [
        """CREATE TABLE sample (metric_id INTEGER NOT NULL REFERENCES metric (id), ts INTEGER NOT NULL,
           value REAL, PRIMARY KEY (metric_id, ts)) STRICT, WITHOUT ROWID""",
    ]),
    "C": ("WITHOUT ROWID, klíč (ts, metric_id)", [
        """CREATE TABLE sample (metric_id INTEGER NOT NULL REFERENCES metric (id), ts INTEGER NOT NULL,
           value REAL, PRIMARY KEY (ts, metric_id)) STRICT, WITHOUT ROWID""",
    ]),
}

AGG_DDL = """CREATE TABLE agg (metric_id INTEGER NOT NULL REFERENCES metric (id), bucket INTEGER NOT NULL,
    n_samples INTEGER NOT NULL, n_valid INTEGER NOT NULL, min_value REAL, max_value REAL, sum_value REAL,
    PRIMARY KEY (metric_id, bucket)) STRICT, WITHOUT ROWID"""

# Stejné dotazy pro všechny varianty, aby srovnání bylo férové.
INSERT = "INSERT INTO sample (metric_id, ts, value) VALUES (?, ?, ?) ON CONFLICT DO NOTHING"
READ_ONE = "SELECT ts, value FROM sample WHERE metric_id = ? AND ts >= ? AND ts < ? ORDER BY ts"
READ_ALL = "SELECT metric_id, ts, value FROM sample WHERE metric_id IN (SELECT id FROM metric) AND ts >= ? AND ts < ?"
RETENTION = "DELETE FROM sample WHERE metric_id IN (SELECT id FROM metric) AND ts < ?"
READ_AGG = "SELECT bucket, n_valid, min_value, max_value, sum_value / n_valid FROM agg WHERE metric_id = ? AND bucket >= ? AND bucket < ? ORDER BY bucket"

# Bloky SQL z dokumentace, které používá dlouhodobý cyklus.
STEADY_SQL = ("insert-sample", "batch-guard", "aggregate-5m", "progress-5m", "rollup-1h", "progress-1h",
              "oldest-sample", "retention-sample", "oldest-5m", "retention-5m", "read-agg")
CHANGE_SQL = """INSERT INTO check_change (ts, ts_quality, run_id, check_id, from_state, to_state, details, note)
                VALUES (:ts, 'synced', :run_id, 'network.link', 'INFO', 'INFO', :details, 'Síť eth0')"""
COUNTED_TABLES = ("run", "metric", "sample", "agg_5m", "agg_1h", "check_change", "event")


def doc_sql(name):
    """SQL blok <!-- sql:name --> z docs/HISTORY.md."""
    text = DOC.read_text(encoding="utf-8")
    match = re.search(rf"<!-- sql:{re.escape(name)} -->\s*```sql\n(.*?)```", text, re.S)
    if match is None:
        raise SystemExit(f"V {DOC} chybí blok <!-- sql:{name} -->")
    return match.group(1)


def io_counters():
    """Čítače /proc/self/io, nebo None mimo Linux.

    wchar = bajty předané do write() (WAL, checkpoint do databáze, snímek):
    logický objem SQLite. write_bytes = odhad jádra, kolik z toho šlo na
    úložiště (při zašpinění stránek, včetně mapované -shm). Ani jedno není
    skutečné opotřebení SSD (to přidá souborový systém a řadič disku).
    """
    try:
        values = {}
        for line in Path("/proc/self/io").read_text().splitlines():
            key, _, value = line.partition(":")
            values[key] = int(value)
        return values
    except OSError:
        return None


def io_written():
    counters = io_counters()
    return None if counters is None else counters["wchar"]


def connect(path, synchronous="FULL", secure_delete="FAST"):
    # isolation_level=None: transakce řídí skript sám (BEGIN IMMEDIATE / COMMIT).
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA synchronous = {synchronous}")
    conn.execute(f"PRAGMA secure_delete = {secure_delete}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_size_limit = 8388608")
    return conn


def size_mb(conn):
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    return pages * conn.execute("PRAGMA page_size").fetchone()[0] / MB


def object_sizes(conn):
    """Velikost tabulek a indexů v MB (virtuální tabulka dbstat)."""
    rows = conn.execute("SELECT name, sum(pgsize) FROM dbstat GROUP BY name").fetchall()
    return {name: size / MB for name, size in rows if name.startswith(("sample", "agg"))}


def median_ms(function, repeat=5):
    times = []
    for _ in range(repeat):
        began = time.perf_counter()
        function()
        times.append((time.perf_counter() - began) * 1000)
    return statistics.median(times)


def p95(values):
    return statistics.quantiles(values, n=20)[-1] if len(values) >= 2 else float("nan")


def plan(conn, sql, params):
    return "; ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def file_size(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fsync_path(path):
    # Funguje pro soubor i adresář (Linux): potvrdí obsah, resp. přejmenování.
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Synthetic:
    """Deterministická syntetická měření: stejný seed = stejná data pro všechny varianty."""

    def __init__(self, seed=20261020):
        self.rng = random.Random(seed)
        self.walk = [0.5] * len(SERIES)

    def value(self, index, kind, ts):
        rng = self.rng
        walk = self.walk[index] = min(1.0, max(0.0, self.walk[index] + rng.uniform(-0.01, 0.01)))
        if kind == "pct":
            return rng.random() * 100  # vytížení CPU skáče, plné desetinné číslo
        if kind == "pct_slow":
            return 20 + walk * 60 + rng.random() * 0.001  # obsazenost: pomalá změna
        if kind == "temp":
            return round(40 + walk * 30 + rng.uniform(-0.5, 0.5), 3)  # tisíciny °C jako /sys
        if kind == "rate":
            return 0.0 if rng.random() < 0.05 else rng.expovariate(1 / 50_000)
        if kind == "rpm":
            return float(2000 + int(walk * 3000))  # hwmon dává celé otáčky
        if kind == "level":
            return float(int(walk * 4.99))
        if kind == "int_temp":
            return float(35 + int(walk * 25))
        if kind == "bytes":
            return float(400_000_000_000 - int(walk * 100_000_000_000) // 4096 * 4096)
        if kind == "ms":
            return round(rng.uniform(0.2, 3.0), 3)
        if kind == "age":
            return float(ts % DAY)
        if kind == "load":
            return round(walk * 4, 2)
        if kind == "mhz":
            return 2400.0 if rng.random() < 0.7 else 1500.0
        return 0.0  # flag

    def cycles(self, start, end):
        """(ts, řádky) pro každé měření v [start, end); během výpadku nic."""
        after_gap = start == START
        for ts in range(start, end, INTERVAL):
            if GAP_START <= ts % DAY < GAP_START + GAP_LENGTH:
                after_gap = True
                continue
            rows = []
            for index, (_, kind) in enumerate(SERIES):
                unknown = (after_gap and index in FIRST_SAMPLE_UNKNOWN) or self.rng.random() < UNKNOWN_SHARE
                rows.append((index + 1, ts, None if unknown else self.value(index, kind, ts)))
            after_gap = False
            yield ts, rows


def synthetic_agg_rows(synth, bucket, bucket_seconds):
    """Syntetické agregace jednoho okna pro všechny řady (n_samples plné, občas o jeden méně platný)."""
    per_bucket = bucket_seconds // INTERVAL
    rows = []
    for index, (_, kind) in enumerate(SERIES):
        n_valid = per_bucket - (1 if synth.rng.random() < 0.1 else 0)
        first, second = synth.value(index, kind, bucket), synth.value(index, kind, bucket)
        rows.append((index + 1, bucket, per_bucket, n_valid, min(first, second), max(first, second),
                     (first + second) / 2 * n_valid))
    return rows


def create_metrics(conn, create=True):
    if create:
        conn.execute(METRIC_DDL)
    conn.executemany("INSERT INTO metric (id, check_id, unit, created_utc) VALUES (?, ?, 'x', ?)",
                     [(index + 1, name, START) for index, (name, _) in enumerate(SERIES)])


def write_batch(conn, rows):
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(INSERT, rows)
    conn.execute("COMMIT")


def ingest(conn, synth, start, end, batch_seconds, latencies=None):
    """Zapíše měření z [start, end) po dávkách `batch_seconds`; vrací počet řádků."""
    rows, count, batch_end = [], 0, start + batch_seconds
    for ts, cycle in synth.cycles(start, end):
        if ts >= batch_end and rows:
            began = time.perf_counter()
            write_batch(conn, rows)
            if latencies is not None:
                latencies.append((time.perf_counter() - began) * 1000)
            count += len(rows)
            rows = []
            batch_end = ts - (ts - start) % batch_seconds + batch_seconds
        rows += cycle
    if rows:
        write_batch(conn, rows)
        count += len(rows)
    return count


def snapshot(conn, path):
    """Konzistentní kopie přes Backup API jako samostatný soubor bez WAL."""
    began = time.perf_counter()
    target = sqlite3.connect(path)
    conn.backup(target)
    target.execute("PRAGMA journal_mode = DELETE")
    backup_s = time.perf_counter() - began
    began = time.perf_counter()
    result = target.execute("PRAGMA integrity_check").fetchone()[0]
    target.close()
    return backup_s, time.perf_counter() - began, result


def changed_pages(first, second, page_size=4096):
    changed = total = 0
    with open(first, "rb") as a, open(second, "rb") as b:
        while True:
            page_a, page_b = a.read(page_size), b.read(page_size)
            if not page_a and not page_b:
                return changed, total
            total += 1
            changed += page_a != page_b


def retention_day(conn, day_start):
    """Smaže jeden den po hodinových dávkách; vrací (s, MB zapsáno)."""
    written, began = io_written(), time.perf_counter()
    for hour in range(1, 25):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(RETENTION, (day_start + hour * HOUR,))
        conn.execute("COMMIT")
    elapsed = time.perf_counter() - began
    return elapsed, (io_written() - written) / MB if written is not None else None


# --- 1. Ověření SQL z dokumentace ----------------------------------------------------

def selfcheck(directory):
    """Spustí bloky SQL z docs/HISTORY.md nad malou databází a ověří slíbené chování."""
    conn = sqlite3.connect(directory / "selfcheck.sqlite3", isolation_level=None)
    conn.executescript(doc_sql("connection"))
    conn.executescript(doc_sql("ddl"))
    sql = {name: doc_sql(name) for name in STEADY_SQL + ("sample-conflict", "gaps", "close-unclean-runs", "high-water")}
    checks = []

    def check(condition, text):
        checks.append(text)
        if not condition:
            raise SystemExit(f"Kontrola návrhu selhala: {text}")

    def fails(statement, params, text):
        try:
            conn.execute(statement, params)
        except sqlite3.IntegrityError:
            check(True, text)
        else:
            check(False, text)

    one = lambda statement, params=(): conn.execute(statement, params).fetchone()
    check(one("PRAGMA application_id")[0] == APPLICATION_ID, "application_id NXST")
    check(one("PRAGMA user_version")[0] == 1, "user_version 1")
    check(one("PRAGMA journal_mode")[0] == "wal", "režim WAL")
    check(one("PRAGMA foreign_keys")[0] == 1, "cizí klíče zapnuté")

    t = START  # 00:00 UTC, násobek 300 i 3600
    conn.execute("INSERT INTO metric (id, check_id, subject, unit, created_utc) VALUES (1, 'network.rx_rate', 'eth0', 'B/s', ?)", (t,))
    conn.execute("INSERT INTO run (id, started_utc, app_version, interval_s) VALUES (1, ?, 'selfcheck', 10)", (t,))
    conn.executemany("INSERT INTO aggregation_state (level, done_until) VALUES (?, ?)", [("5m", t), ("1h", t)])

    def batch(number, rows):
        """Dávka podle HISTORY.md 5.1: ochrana pořadím dávky, pak vzorky, jedna transakce."""
        conn.execute("BEGIN IMMEDIATE")
        guard = {"run_id": 1, "batch": number, "max_ts": max(ts for ts, _ in rows)}
        if conn.execute(sql["batch-guard"], guard).rowcount != 1:
            conn.execute("ROLLBACK")
            return False
        conn.executemany(sql["insert-sample"], [{"metric_id": 1, "ts": ts, "value": value} for ts, value in rows])
        conn.execute("COMMIT")
        return True

    # Okno 1 [t, t+300): skutečná nula, UNKNOWN, 30 B/s; zbylé vzorky chybí (mezera).
    # Okno 2 [t+300, t+600): jen UNKNOWN. Okno 3 bez vzorků. Okno 4: jedna hodnota.
    first = [(t, 0.0), (t + 10, None), (t + 20, 30.0)]
    check(batch(1, first), "dávka se zapíše v jedné transakci s posunem run.last_batch")
    check(not batch(1, first), "opakovaná dávka (po nejistém commitu) se celá odmítne, nic se nezdvojí")
    check(batch(2, [(t + 300, None), (t + 900, 5.0)]), "další dávka v pořadí se zapíše")
    check(not batch(4, [(t + 1000, 1.0)]), "dávka mimo pořadí se odmítne")
    check(one("SELECT last_batch, last_write_utc FROM run WHERE id = 1") == (2, t + 900),
          "run nese poslední dávku a nejnovější zapsaný čas")
    check(one("SELECT count(*) FROM sample") == (5,), "v sample je každý vzorek jednou")

    # Duplicita a konflikt: INSERT nic nepřepíše, zapisovač z rowcount a dotazu pozná, co nastalo.
    for ts, value, same, text in ((t + 20, 30.0, 1, "stejná hodnota = duplicita"),
                                  (t + 10, None, 1, "stejné UNKNOWN = duplicita"),
                                  (t + 20, 99.0, 0, "jiná hodnota = konflikt k ohlášení")):
        inserted = conn.execute(sql["insert-sample"], {"metric_id": 1, "ts": ts, "value": value}).rowcount
        found = one(sql["sample-conflict"], {"metric_id": 1, "ts": ts, "value": value})
        check(inserted == 0 and found[0] == same, f"nevložený vzorek: {text}")
    check(one("SELECT value FROM sample WHERE ts = ?", (t + 20,)) == (30.0,), "první uložená hodnota platí, konflikt ji nepřepíše")

    def aggregate(start, end, commit=True):
        """Agregace podle HISTORY.md 6: posun done_until s kontrolou, pak okna, jedna transakce."""
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(sql["progress-5m"], {"start": start, "end": end}).rowcount != 1:
            conn.execute("ROLLBACK")
            return False
        conn.execute(sql["aggregate-5m"], {"start": start, "end": end})
        conn.execute("COMMIT" if commit else "ROLLBACK")
        return commit

    done = lambda level: one("SELECT done_until FROM aggregation_state WHERE level = ?", (level,))[0]
    aggregate(t, t + HOUR, commit=False)  # pád před commitem
    check(one("SELECT count(*) FROM agg_5m") == (0,) and done("5m") == t,
          "agregace přerušená před commitem nezanechá nic a interval zůstane nezpracovaný")
    check(aggregate(t, t + HOUR), "po pádu se stejný interval zpracuje znovu celý")
    rows = conn.execute("SELECT bucket, n_samples, n_valid, min_value, max_value, sum_value FROM agg_5m ORDER BY bucket").fetchall()
    check(rows[0] == (t, 3, 2, 0.0, 30.0, 30.0), "nula je platná hodnota, UNKNOWN ani mezera se nepočítají")
    check(rows[1] == (t + 300, 1, 0, None, None, None), "okno jen s UNKNOWN: n_valid 0 a prázdné hodnoty")
    check([row[0] for row in rows] == [t, t + 300, t + 900], "okno bez vzorků nemá řádek (mezera zůstane)")
    average = conn.execute(sql["read-agg"], {"metric_id": 1, "start": t, "end": t + HOUR}).fetchall()
    check([row[5] for row in average] == [15.0, None, 5.0], "průměr = součet / platné, bez nul za chybějící")
    check(not aggregate(t, t + HOUR), "hotový interval se znovu nezpracuje (kontrola done_until)")
    fails(sql["aggregate-5m"], {"start": t, "end": t + HOUR},
          "existující okno se nepřepíše: opětovné vložení selže jako konflikt")
    check(conn.execute("SELECT bucket, n_samples, n_valid, min_value, max_value, sum_value FROM agg_5m ORDER BY bucket").fetchall() == rows,
          "po odmítnutém opakování zůstávají agregace beze změny")

    def rollup(start, end):
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(sql["progress-1h"], {"start": start, "end": end}).rowcount != 1:
            conn.execute("ROLLBACK")
            return False
        conn.execute(sql["rollup-1h"], {"start": start, "end": end})
        conn.execute("COMMIT")
        return True

    check(not rollup(t, t + 2 * HOUR), "hodina bez hotových 5min oken se nezpracuje")
    check(rollup(t, t + HOUR), "hodina s hotovými 5min okny se zpracuje")
    hourly = one("SELECT n_samples, n_valid, min_value, max_value, sum_value FROM agg_1h WHERE bucket = ?", (t,))
    direct = one("SELECT count(*), count(value), min(value), max(value), sum(value) FROM sample WHERE ts >= ? AND ts < ?", (t, t + HOUR))
    check(hourly == direct, "hodinová agregace z 5min oken = přímý výpočet ze surových dat")

    gaps = conn.execute(sql["gaps"], {"metric_id": 1, "start": t, "end": t + HOUR, "max_step": 2 * INTERVAL}).fetchall()
    check(gaps == [(t + 20, t + 300), (t + 300, t + 900)], "dotaz na mezery najde chybějící vzorky")

    conn.execute(sql["retention-sample"], {"cutoff": t + 300})
    check(one("SELECT min(ts), count(*) FROM sample") == (t + 300, 2), "retence maže jen vzorky před hranicí")
    check(one(sql["oldest-sample"]) == (t + 300,), "nejstarší vzorek pro další dávku retence")
    conn.execute(sql["retention-5m"], {"cutoff": t + 300})
    check(one(sql["oldest-5m"]) == (t + 300,), "retence 5min agregací maže jen okna před hranicí")

    check(conn.execute(sql["close-unclean-runs"]).rowcount == 1
          and one("SELECT stopped_utc, clean_stop FROM run WHERE id = 1") == (t + 900, 0),
          "restart: neukončený běh se uzavře jako nečistý časem posledního zápisu")
    check(one(sql["high-water"]) == (t + 900,), "restart: horní mez pro nový běh = nejnovější zapsaný vzorek")

    fails(sql["insert-sample"], {"metric_id": 99, "ts": t, "value": 1.0}, "vzorek neznámé řady odmítne cizí klíč")
    # STRICT převede bezeztrátově převoditelný text ('12'), jiný text odmítne.
    fails(sql["insert-sample"], {"metric_id": 1, "ts": t + 1000, "value": "12 °C"}, "STRICT: text místo čísla se odmítne")
    fails("INSERT INTO agg_5m VALUES (1, ?, 1, 2, 1.0, 1.0, 1.0)", (t + 1200,), "n_valid větší než n_samples se odmítne")
    fails("INSERT INTO agg_5m VALUES (1, ?, 1, 1, 1.0, 1.0, 1.0)", (t + 1201,), "okno mimo násobek 300 s se odmítne")
    fails("INSERT INTO agg_5m VALUES (1, ?, 1, 0, NULL, NULL, 0.0)", (t + 1500,), "okno bez platných hodnot nesmí mít součet 0")
    conn.execute("INSERT INTO check_change (ts, ts_quality, run_id, check_id, to_state, note) "
                 "VALUES (?, 'synced', 1, 'service.animedb', 'N/A', 'AnimeDB není nasazena')", (t,))
    check(one("SELECT to_state FROM check_change")[0] == "N/A", "N/A je stav v check_change, ne vzorek")
    fails("INSERT INTO check_change (ts, ts_quality, run_id, check_id, to_state) VALUES (?, 'synced', 1, 'x', 'SIM')", (t,),
          "neznámý stav se odmítne")
    fails("INSERT INTO check_change (ts, ts_quality, run_id, check_id, to_state, details) VALUES (?, 'synced', 1, 'x', 'INFO', '{bad')", (t,),
          "details musí být JSON")
    fails("INSERT INTO check_change (ts, run_id, check_id, to_state) VALUES (?, 1, 'x', 'INFO')", (t,),
          "kvalita času je povinná a nemá výchozí hodnotu")
    fails("INSERT INTO event (ts, ts_quality, kind, severity, message) VALUES (?, 'guess', 'x', 'INFO', 'x')", (t,),
          "neznámá kvalita času se odmítne")
    conn.execute("INSERT INTO event (ts, ts_quality, kind, severity, message) "
                 "VALUES (?, 'unsynced', 'process.stop', 'WARNING', 'ukončeno před synchronizací času')", (t,))
    check(one("SELECT ts_quality FROM event")[0] == "unsynced", "událost bez synchronizovaného času nese kvalitu unsynced")
    check(one("PRAGMA integrity_check")[0] == "ok", "integrity_check")
    conn.close()
    return checks


def selfcheck_single_writer(directory):
    """Předpoklad ochrany před chybou WAL-reset (docs/HISTORY.md 5.8) na této verzi SQLite.

    Chyba vyžaduje dvě spojení, která současně zapisují nebo dělají checkpoint.
    Návrh dovoluje zápis a checkpoint jen spojení zapisovače; ověří se, že
    spojení jen pro čtení nezapíše a checkpoint neprovede, ani když se zavírá
    jako poslední. Chybu samotnou to netestuje a o opravě nic neříká.
    """
    path = directory / "single-writer.sqlite3"
    wal = Path(f"{path}-wal")
    checks = []

    def check(condition, text):
        checks.append(text)
        if not condition:
            raise SystemExit(f"Kontrola jednoho zapisovače selhala: {text}")

    writer = sqlite3.connect(path, isolation_level=None)
    writer.executescript(doc_sql("connection"))
    writer.execute("PRAGMA wal_autocheckpoint = 0")  # WAL zůstane plný, aby šel checkpoint poznat
    writer.execute("CREATE TABLE t (x INTEGER) STRICT")
    writer.execute("BEGIN")
    writer.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(1000)])
    writer.execute("COMMIT")
    before = (sha256(path), file_size(wal))
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    if hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"):  # Python ≥ 3.12
        reader.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    check(reader.execute("SELECT count(*) FROM t").fetchone()[0] == 1000, "čtenář vidí potvrzená data z WAL")
    for statement in ("PRAGMA wal_checkpoint(PASSIVE)", "PRAGMA wal_checkpoint(TRUNCATE)", "INSERT INTO t VALUES (1)"):
        try:
            reader.execute(statement).fetchall()
        except sqlite3.Error:
            pass
    check((sha256(path), file_size(wal)) == before, "čtenář (mode=ro) nezapíše ani neprovede checkpoint")
    writer.close()
    check((sha256(path), file_size(wal)) == before, "zapisovač zavřený při otevřeném čtenáři checkpoint neprovede")
    reader.close()
    check((sha256(path), file_size(wal)) == before and wal.exists(),
          "poslední zavírané spojení jen pro čtení checkpoint neprovede, WAL zůstane")
    writer = sqlite3.connect(path)
    check(writer.execute("SELECT count(*) FROM t").fetchone()[0] == 1000, "data jsou celá po znovuotevření zapisovačem")
    writer.close()
    check(not wal.exists() and sha256(path) != before[0], "checkpoint provedl až zapisovač při zavření jako poslední spojení")
    return checks


# --- 2. Model pravidel času a identity ------------------------------------------------

class TimeRules:
    """Model pravidel z docs/HISTORY.md 4.1–4.3 pro simulaci scénářů. Není to kód aplikace.

    Měření přichází s monotónním časem, systémovým UTC (float s) a údajem, zda
    je čas synchronizovaný. Výsledkem jsou přijaté vzorky (řada, ts, kvalita),
    počty odmítnutí podle důvodu a události. Nic se nepřepisuje ani neposouvá.
    """

    JUMP_SECONDS = 2

    def __init__(self, high_water=None, capacity=None):
        self.high_water = high_water  # max(run.last_write_utc) předchozích běhů
        self.capacity = capacity  # kapacita fronty v měřeních (None = neomezená)
        self.last = {}  # řada -> (poslední přijatý ts, monotonní čas jeho měření)
        self.offset = None  # UTC − monotonic od první synchronizace v tomto bootu
        self.held = []  # (řada, monotonic) před první synchronizací
        self.lost = None  # zahozená měření před synchronizací: jen monotonní rozsah, UTC až po synchronizaci
        self.sync_lost = False
        self.accepted = []
        self.rejected = Counter()
        self.duplicates = 0  # stejné měření podruhé: nic se neztrácí, nepočítá se jako ztráta
        self.events = []

    def measure(self, metric, mono, utc, synced):
        if self.offset is None:
            if not synced:
                self.held.append((metric, mono))  # čas zatím nevíme, data čekají ve frontě
                if self.capacity is not None and len(self.held) > self.capacity:
                    self._lose(self.held.pop(0)[1])
                return
            self.offset = utc - mono
            if self.lost:
                # Rozsah ztráty se dopočte stejně jako čas čekajících měření, nikdy dřív.
                self.lost.update(utc_from=math.floor(self.lost["mono_from"] + self.offset),
                                 utc_to=math.floor(self.lost["mono_to"] + self.offset), quality="derived")
            for held_metric, held_mono in self.held:
                self._accept(held_metric, held_mono + self.offset, "derived", held_mono)
            if self.held:
                self.events.append(("clock.synced", len(self.held)))
            self.held = []
        else:
            shift = (utc - mono) - self.offset
            if abs(shift) > self.JUMP_SECONDS:
                self.events.append(("clock.jump", round(shift)))
            self.offset = utc - mono
            if not synced and not self.sync_lost:
                self.events.append(("clock.sync_lost", 0))
            elif synced and self.sync_lost:
                self.events.append(("clock.sync_restored", 0))
            self.sync_lost = not synced
        self._accept(metric, utc, "synced", mono)

    def _accept(self, metric, utc, quality, mono):
        ts = math.floor(utc)
        last_ts, last_mono = self.last.get(metric, (None, None))
        if last_ts == ts and last_mono == mono:
            self.duplicates += 1  # totéž měření odeslané znovu
        elif self.high_water is not None and ts <= self.high_water:
            self.rejected["before_high_water"] += 1
        elif last_ts is not None and ts == last_ts:
            self.rejected["same_second"] += 1  # jiné měření ve stejné sekundě
        elif last_ts is not None and ts < last_ts:
            self.rejected["clock_backward"] += 1
        else:
            self.last[metric] = (ts, mono)
            self.accepted.append((metric, ts, quality))

    def _lose(self, mono):
        if self.lost is None:
            self.lost = {"count": 0, "mono_from": mono, "utc_from": None, "utc_to": None, "quality": None}
        self.lost["count"] += 1
        self.lost["mono_to"] = mono

    def shutdown(self):
        """Měření bez důvěryhodného času se při ukončení nezapíšou, jen započítají.

        Záznam o ztrátě dostane kvalitu unsynced a jen monotonní rozsah: UTC
        rozsah se nevymýšlí.
        """
        self.rejected["time_unsynced"] += len(self.held)
        self.held = []
        if self.lost and self.lost["quality"] is None:
            self.lost["quality"] = "unsynced"


class ChangeRules:
    """Model pravidla R4 (docs/HISTORY.md 7.1) pro zapisovač. Není to kód aplikace.

    Řádek check_change vzniká jen při změně diagnostického stavu nebo provozně
    významných údajů (details), opakované měření beze změny nevytvoří nic a nic
    se neslučuje. První výsledek kontroly v běhu je výchozí řádek s from_state NULL.
    """

    def __init__(self):
        self.last = {}
        self.pending = []
        self.measurements = 0

    def submit(self, utc, check_id, state, details=None, value=None, unit=None):
        self.measurements += 1
        key = (state, json.dumps(details, sort_keys=True) if details else None)
        previous = self.last.get(check_id)
        if previous == key:
            return
        self.last[check_id] = key
        self.pending.append({"ts": math.floor(utc), "check_id": check_id, "from_state": previous[0] if previous else None,
                             "to_state": state, "value": value, "unit": unit, "details": key[1], "note": None})


def selfcheck_r4(directory):
    """Regresní scénáře R4 nad DDL a SQL z dokumentace (docs/HISTORY.md 7.1)."""
    checks = []

    def check(condition, text):
        checks.append(text)
        if not condition:
            raise SystemExit(f"Kontrola R4 selhala: {text}")

    conn = sqlite3.connect(directory / "r4.sqlite3", isolation_level=None)
    conn.executescript(doc_sql("connection"))
    conn.executescript(doc_sql("ddl"))
    sql = {name: doc_sql(name) for name in ("batch-guard", "insert-change", "state-episodes", "details-episodes",
                                            "transition-rate", "transitions-window")}
    rows_of = lambda check_id: conn.execute(
        "SELECT ts, from_state, to_state, details FROM check_change WHERE check_id = ? ORDER BY ts, id", (check_id,)).fetchall()
    base = 1_800_000_000  # násobek 600 i 3600
    conn.execute("INSERT INTO run (id, started_utc, app_version, interval_s) VALUES (1, ?, 'r4', 5)", (base,))
    batch = {"done": 0}

    def write(rows, number=None, fail=False):
        """Dávka podle 5.2: batch-guard, změny, COMMIT; fail = transakce selže před commitem."""
        number = batch["done"] + 1 if number is None else number
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(sql["batch-guard"], {"run_id": 1, "batch": number, "max_ts": None}).rowcount != 1:
            conn.execute("ROLLBACK")
            return False
        conn.executemany(sql["insert-change"], [dict(row, run_id=1, ts_quality="synced") for row in rows])
        if fail:
            conn.execute("ROLLBACK")
            return False
        conn.execute("COMMIT")
        batch["done"] = number
        return True

    rules = ChangeRules()
    # A: 30 min, měření po 5 s, stav se mění každých 10 s: 360 měření, 180 změn.
    rules.submit(base, "system.cpu_temp", "INFO", value=40.0, unit="°C")
    for k in range(1, 361):
        state = "CRITICAL" if ((k + 1) // 2) % 2 else "INFO"
        rules.submit(base + 5 * k, "system.cpu_temp", state, value=90.0 if state == "CRITICAL" else 40.0, unit="°C")
    # B: WARNING → ERROR → CRITICAL → WARNING → INFO (s opakovaným měřením beze změny).
    for offset, state in ((2000, "INFO"), (2010, "WARNING"), (2020, "ERROR"), (2030, "CRITICAL"), (2040, "WARNING"),
                          (2045, "WARNING"), (2050, "INFO")):
        rules.submit(base + offset, "disk.root", state)
    # C: přechod do UNKNOWN (měření selhalo) a obnovení měření.
    for offset, state in ((3000, "INFO"), (3010, "UNKNOWN"), (3020, "UNKNOWN"), (3030, "INFO")):
        rules.submit(base + offset, "system.cpu_usage", state)
    # E: dva různé přechody během jedné sekundy (měření zpožděné o 9 s a další podle plánu).
    for utc, state in ((base + 4000.0, "OK"), (base + 4010.2, "CRITICAL"), (base + 4010.9, "OK")):
        rules.submit(utc, "service.nas", state)
    # G: linka UP/DOWN se stavem INFO; měření po 5 s, změna údaje každých 10 s.
    for k in range(0, 41):
        link = "DOWN" if ((k + 1) // 2) % 2 else "UP"
        rules.submit(base + 5000 + 5 * k, "network.link", "INFO", details={"interface": "eth0", "link": link})

    pending = rules.pending
    chunks = [pending[i:i + 10] for i in range(0, len(pending), 10)]
    check(not write(chunks[0], fail=True) and conn.execute("SELECT count(*), (SELECT last_batch FROM run) FROM check_change").fetchone() == (0, 0),
          "selhaná transakce nezanechá žádný přechod ani posun last_batch")
    check(all(write(chunk) for chunk in chunks), "opakovaný zápis po selhání a všechny další dávky projdou")
    check(not write(chunks[-1], number=batch["done"]), "opakování už zapsané dávky (nejistý commit) se celé odmítne")
    total = conn.execute("SELECT count(*) FROM check_change").fetchone()[0]
    duplicates = conn.execute("""SELECT count(*) FROM (SELECT 1 FROM check_change
                                 GROUP BY check_id, ts, from_state, to_state, details HAVING count(*) > 1)""").fetchone()[0]
    check(total == len(pending) == 214 and duplicates == 0, "214 řádků = přesně všechny změny, žádný duplikát po selhání ani opakování")

    a_rows = rows_of("system.cpu_temp")
    entries = [base + 5 * k for k in range(1, 361, 4)]
    exits = [base + 5 * k for k in range(3, 361, 4)]
    check(rules.measurements == 361 + 7 + 4 + 3 + 41 and len(a_rows) == 181 and a_rows[0][1] is None,
          "A: 360 měření, 180 změn → 181 řádků (výchozí + 180 přechodů); 180 opakovaných měření beze změny nevytvořilo nic")
    check([row[0] for row in a_rows if row[1:3] == ("INFO", "CRITICAL")] == entries,
          "A: 90 přesných časů vzniku CRITICAL, každý vlastní řádek")
    check([row[0] for row in a_rows if row[1:3] == ("CRITICAL", "INFO")] == exits,
          "A: 90 přesných časů odeznění CRITICAL, každý vlastní řádek")
    episodes = conn.execute(sql["state-episodes"], {"check_id": "system.cpu_temp", "state": "CRITICAL",
                                                    "start": base, "end": base + HOUR}).fetchall()
    check([(start, end, duration, begin) for start, end, duration, begin in episodes]
          == [(start, end, 10, 0) for start, end in zip(entries, exits)],
          "A: state-episodes vrátí všech 90 epizod CRITICAL se začátkem, koncem a délkou 10 s")
    rate = conn.execute(sql["transition-rate"], {"check_id": "system.cpu_temp", "bucket_s": 600,
                                                 "start": base, "end": base + HOUR}).fetchall()
    check(rate == [(base, 60, 60), (base + 600, 60, 60), (base + 1200, 60, 60)],
          "A: četnost přechodů po 10 min: 60 + 60 + 60 = 180, výchozí řádek se nepočítá")
    window = conn.execute(sql["transitions-window"], {"check_id": "system.cpu_temp", "window_s": 600,
                                                      "start": base, "end": base + HOUR}).fetchall()
    check(len(window) == 180 and next(ts for ts, recent in window if recent >= 6) == base + 55,
          "A: z historie jde spočítat počet přechodů za 10 min (příklad prahu 6 dosažen v 6. přechodu)")
    conn.executemany("INSERT INTO event (ts, ts_quality, run_id, kind, severity, check_id, message) "
                     "VALUES (?, 'synced', 1, ?, 'WARNING', 'system.cpu_temp', 'kmitání')",
                     [(base + 55, "flapping.start"), (base + 2995, "flapping.end")])
    check(conn.execute(sql["transition-rate"], {"check_id": "system.cpu_temp", "bucket_s": 600,
                                                "start": base, "end": base + HOUR}).fetchall() == rate,
          "události FLAPPING START/END jsou v event a do přechodů se nezapočítají")

    check(rows_of("disk.root") == [(base + 2000, None, "INFO", None), (base + 2010, "INFO", "WARNING", None),
                                    (base + 2020, "WARNING", "ERROR", None), (base + 2030, "ERROR", "CRITICAL", None),
                                    (base + 2040, "CRITICAL", "WARNING", None), (base + 2050, "WARNING", "INFO", None)],
          "B: WARNING → ERROR → CRITICAL → WARNING → INFO, každý přechod i úplné odeznění vlastním řádkem")
    episodes = lambda check_id, state: conn.execute(sql["state-episodes"], {"check_id": check_id, "state": state,
                                                                            "start": base, "end": base + HOUR * 2}).fetchall()
    check(episodes("disk.root", "CRITICAL") == [(base + 2030, base + 2040, 10, 0)]
          and episodes("disk.root", "WARNING") == [(base + 2010, base + 2020, 10, 0), (base + 2040, base + 2050, 10, 0)],
          "B: epizody CRITICAL (10 s) a dvě epizody WARNING s délkami")
    check(rows_of("system.cpu_usage") == [(base + 3000, None, "INFO", None), (base + 3010, "INFO", "UNKNOWN", None),
                                           (base + 3030, "UNKNOWN", "INFO", None)]
          and episodes("system.cpu_usage", "UNKNOWN") == [(base + 3010, base + 3030, 20, 0)],
          "C: přechod do UNKNOWN a obnovení měření evidovány, epizoda UNKNOWN 20 s")
    check(rows_of("service.nas") == [(base + 4000, None, "OK", None), (base + 4010, "OK", "CRITICAL", None),
                                      (base + 4010, "CRITICAL", "OK", None)]
          and episodes("service.nas", "CRITICAL") == [(base + 4010, base + 4010, 0, 0)],
          "E: dva přechody v jedné sekundě uloženy oba, pořadí podle id, délka epizody 0 s (rozlišení 1 s)")
    link_rows = rows_of("network.link")
    downs = conn.execute(sql["details-episodes"], {"check_id": "network.link", "key": "link", "value": "DOWN",
                                                   "start": base, "end": base + HOUR * 2}).fetchall()
    check(len(link_rows) == 21 and all(row[1:3] == ("INFO", "INFO") for row in link_rows[1:])
          and [(start, end, duration) for start, end, duration, _ in downs]
          == [(base + 5000 + 5 * k, base + 5010 + 5 * k, 10) for k in range(1, 41, 4)],
          "G: 20 změn linky UP/DOWN při stavu INFO uloženo a 10 epizod DOWN má začátek, konec i délku")
    check(conn.execute(sql["transition-rate"], {"check_id": "network.link", "bucket_s": 3600,
                                                "start": base, "end": base + HOUR * 2}).fetchall() == [(base + 3600, 20, 0)],
          "G: četnost počítá i změny linky (20 přechodů, 0 změn diagnostického stavu)")
    check(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "integrity_check")
    conn.close()
    return checks


def bench_changes(directory, hours):
    """Velikost a zápis check_change při trvalém kmitání (R4): přechod při každém měření po 10 s.

    Každý přechod je samostatná dávka (změny se zapisují do 10 s, 5.2) s batch-guard
    a synchronous=FULL. Výsledek platí jen pro tyto předpoklady (texty, hodnoty, 10 s).
    """
    results = {}
    for name, check_id in (("stav INFO/CRITICAL s hodnotou", "system.cpu_temp"), ("linka UP/DOWN se stavem INFO", "network.link")):
        path = directory / f"changes_{len(results)}.sqlite3"
        conn = sqlite3.connect(path, isolation_level=None)
        conn.executescript(doc_sql("connection"))
        conn.executescript(doc_sql("ddl"))
        conn.execute("INSERT INTO run (id, started_utc, app_version, interval_s) VALUES (1, ?, 'changes', 10)", (START,))
        guard, insert = doc_sql("batch-guard"), doc_sql("insert-change")
        count = hours * 360
        written, began = io_written(), time.perf_counter()
        for k in range(count):
            if check_id == "network.link":
                link = "DOWN" if k % 2 else "UP"
                row = {"from_state": "INFO", "to_state": "INFO", "value": None, "unit": None, "note": f"Síť eth0 {link}",
                       "details": json.dumps({"interface": "eth0", "link": link}, sort_keys=True)}
            else:
                value = 85.0 + (k % 13) * 0.137 if k % 2 == 0 else 61.0 + (k % 11) * 0.211
                row = {"from_state": "INFO" if k % 2 == 0 else "CRITICAL", "to_state": "CRITICAL" if k % 2 == 0 else "INFO",
                       "value": value, "unit": "°C", "note": f"Teplota CPU {value:.0f} °C", "details": None}
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(guard, {"run_id": 1, "batch": k + 1, "max_ts": None})
            conn.execute(insert, dict(row, ts=START + 10 * k, run_id=1, check_id=check_id, ts_quality="synced"))
            conn.execute("COMMIT")
        elapsed = time.perf_counter() - began
        wchar = (io_written() - written) / MB if written is not None else None
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size = sum(pgsize for name_, pgsize in conn.execute(
            "SELECT name, sum(pgsize) FROM dbstat WHERE name IN ('check_change', 'check_change_ts') GROUP BY name"))
        conn.close()
        per_day = DAY / (hours * HOUR)
        results[name] = {"rows": count, "bytes_per_row": size / count, "mb_per_day": size / MB * per_day,
                         "gb_2y": size * per_day * 730 / 1e9, "wchar_mb_per_day": wchar * per_day if wchar is not None else None,
                         "commit_ms": elapsed / count * 1000}
        path.unlink()
    return results


def snapshot_state(marker, now):
    """Stav kontroly history.snapshot podle stáří posledního úspěšného snímku (HISTORY.md 9.2)."""
    if marker is None:
        return "ERROR"
    age = now - marker["created_utc"]
    if age <= 26 * HOUR:
        return "INFO"
    return "WARNING" if age <= 50 * HOUR else "ERROR"


def selfcheck_rules():
    checks = []

    def check(condition, text):
        checks.append(text)
        if not condition:
            raise SystemExit(f"Kontrola pravidel selhala: {text}")

    base = 1_800_000_000
    # Dvě měření stejné řady v jedné sekundě (měření zpožděné o 9,6 s, další podle plánu).
    rules = TimeRules()
    rules.measure("cpu", 100.0, base + 9.6, True)
    rules.measure("cpu", 100.35, base + 9.95, True)
    check(rules.accepted == [("cpu", base + 9, "synced")] and rules.rejected == {"same_second": 1},
          "dvě měření v jedné sekundě: první platí, druhé se nezapíše a započítá jako same_second")
    rules.measure("cpu", 100.0, base + 9.6, True)
    check(rules.duplicates == 1 and rules.rejected == {"same_second": 1} and len(rules.accepted) == 1,
          "totéž měření odeslané znovu (stejný monotónní čas) je duplicita, ne ztráta ani kolize")

    # Restart: horní mez z run.last_write_utc předchozího běhu.
    rules = TimeRules(high_water=base)
    rules.measure("cpu", 5.0, base + 0.4, True)
    rules.measure("cpu", 15.0, base + 10.4, True)
    check(rules.accepted == [("cpu", base + 10, "synced")] and rules.rejected == {"before_high_water": 1},
          "restart: vzorek v sekundě horní meze se odmítne, novější se zapíše")

    # Posun času zpět o hodinu (3605 s, aby žádné měření nepadlo přesně na sekundu
    # posledního vzorku: to by byl případ same_second) za synchronizovaného běhu.
    rules = TimeRules()
    for k in range(11):
        rules.measure("cpu", 10.0 * k, base + 10 * k, True)  # do base + 100
    for k in range(11, 11 + 380):
        rules.measure("cpu", 10.0 * k, base + 10 * k - 3605, True)
    check(("clock.jump", -3605) in rules.events and rules.rejected == {"clock_backward": 360},
          "posun zpět o 1 h: událost clock.jump, 360 měření se nezapíše, nic se nepřepíše")
    check(rules.accepted[11] == ("cpu", base + 105, "synced") and len(rules.accepted) == 11 + 20,
          "po posunu zpět se zapisuje až od času po posledním zapsaném vzorku")

    # Start bez synchronizace (RTC bez baterie): čas je o 10 000 s pozadu, NTP ho pak posune.
    previous_high_water = base - 11_000
    rules = TimeRules(high_water=previous_high_water)
    for k in range(3):
        rules.measure("cpu", 5.0 + 10 * k, base - 10_000 + 5 + 10 * k, False)
    check(not rules.accepted and len(rules.held) == 3,
          "před první synchronizací se nic nezapíše, měření čekají s monotónním časem")
    check(all(previous_high_water < base - 10_000 + 5 + 10 * k for k in range(3)),
          "chybný čas před synchronizací by prošel horní mezí: zápis bez čekání by ho uložil do mezery")
    rules.measure("cpu", 35.0, base + 35, True)
    check(rules.accepted == [("cpu", base + 5, "derived"), ("cpu", base + 15, "derived"),
                             ("cpu", base + 25, "derived"), ("cpu", base + 35, "synced")]
          and ("clock.synced", 3) in rules.events,
          "po synchronizaci dostanou čekající měření čas z monotónních hodin (kvalita derived)")

    # Ztráta synchronizace a její obnova s krokem +5 s.
    rules.measure("cpu", 45.0, base + 45, False)
    rules.measure("cpu", 55.0, base + 60, True)
    check(("clock.sync_lost", 0) in rules.events and ("clock.sync_restored", 0) in rules.events
          and ("clock.jump", 5) in rules.events and rules.accepted[-2:] == [("cpu", base + 45, "synced"), ("cpu", base + 60, "synced")],
          "ztráta synchronizace jen událost; obnova s krokem +5 s = clock.jump a mezera, nic se nedoplňuje")

    # Ukončení bez synchronizace.
    rules = TimeRules()
    for k in range(4):
        rules.measure("cpu", 10.0 * k, base + 10 * k, False)
    rules.shutdown()
    check(not rules.accepted and rules.rejected == {"time_unsynced": 4},
          "ukončení bez synchronizace: měření se nezapíšou a ztráta se započítá")

    # Fronta se zaplní dřív, než se čas synchronizuje (kapacita 3 měření).
    rules = TimeRules(capacity=3)
    for k in range(5):
        rules.measure("cpu", 10.0 * k, base - 10_000 + 10 * k, False)
    rules.shutdown()
    check(rules.lost == {"count": 2, "mono_from": 0.0, "mono_to": 10.0, "utc_from": None, "utc_to": None, "quality": "unsynced"}
          and rules.rejected == {"time_unsynced": 3} and not rules.accepted,
          "přetečení fronty bez synchronizace: ztráta jen s monotónním rozsahem, žádný vymyšlený čas UTC")
    rules = TimeRules(capacity=3)
    for k in range(5):
        rules.measure("cpu", 10.0 * k, base - 10_000 + 10 * k, False)
    rules.measure("cpu", 50.0, base + 50, True)
    check(rules.lost["quality"] == "derived" and (rules.lost["utc_from"], rules.lost["utc_to"]) == (base, base + 10)
          and [ts for _, ts, _ in rules.accepted] == [base + 20, base + 30, base + 40, base + 50],
          "přetečení fronty a pak synchronizace: rozsah ztráty i čekající vzorky dostanou odvozený čas")

    marker = {"created_utc": base}
    states = [snapshot_state(None, base)] + [snapshot_state(marker, base + hours * HOUR) for hours in (1, 26, 27, 50, 51)]
    check(states == ["ERROR", "INFO", "INFO", "WARNING", "WARNING", "ERROR"],
          "stav snímku: chybí ERROR, do 26 h INFO, do 50 h WARNING, starší ERROR")
    return checks


# --- 3. Surové vzorky ----------------------------------------------------------------

def bench_raw(directory, key, days, volume_hours):
    title, ddl = RAW_VARIANTS[key]
    path = directory / f"raw_{key}.sqlite3"
    snap1, snap2 = directory / f"raw_{key}_s1.sqlite3", directory / f"raw_{key}_s2.sqlite3"
    synth = Synthetic()
    result = {"variant": f"{key} {title}"}

    # Naplnění 30 dní v pořadí jako v provozu (čas, uvnitř řady), dávky po 60 s.
    # synchronous=OFF jen kvůli délce běhu; na velikost nemá vliv.
    conn = connect(path, synchronous="OFF")
    create_metrics(conn)
    for statement in ddl:
        conn.execute(statement)
    began = time.perf_counter()
    rows = ingest(conn, synth, START, START + days * DAY, 60)
    elapsed = time.perf_counter() - began
    result["rows"] = rows
    result["fill_rows_per_s"] = rows / elapsed
    result["size_mb"] = size_mb(conn)
    result["objects_mb"] = object_sizes(conn)
    result["bytes_per_row"] = result["size_mb"] * MB / rows

    end = START + days * DAY
    metric = 2  # teplota CPU
    reads = {}
    for label, span in (("1 h", HOUR), ("24 h", DAY), ("48 h", 2 * DAY), ("7 d", 7 * DAY), ("30 d", days * DAY)):
        params = (metric, end - span, end)
        reads[label] = median_ms(lambda: conn.execute(READ_ONE, params).fetchall())
    reads["všech 20 řad 1 h"] = median_ms(lambda: conn.execute(READ_ALL, (end - HOUR, end)).fetchall())
    result["read_ms"] = reads
    aggregate = doc_sql("aggregate-5m").replace("agg_5m", "agg")
    conn.execute(AGG_DDL)

    def aggregate_once():
        # Návrh existující okno nepřepisuje, proto se každé měření vrátí ROLLBACKem.
        conn.execute("BEGIN")
        conn.execute(aggregate, {"start": end - HOUR, "end": end})
        conn.execute("ROLLBACK")

    result["aggregate_hour_ms"] = median_ms(aggregate_once)
    result["plans"] = {
        "graf jedné řady": plan(conn, READ_ONE, (metric, end - DAY, end)),
        "všechny řady": plan(conn, READ_ALL, (end - HOUR, end)),
        "retence": plan(conn, RETENTION, (START,)),
    }
    result["quick_check_s"] = median_ms(lambda: conn.execute("PRAGMA quick_check").fetchall(), 1) / 1000
    result["integrity_check_s"] = median_ms(lambda: conn.execute("PRAGMA integrity_check").fetchall(), 1) / 1000
    conn.execute("DROP TABLE agg")
    conn.close()

    # Objem zápisu a latence commitu podle dávky (synchronous=FULL podle návrhu).
    cursor = end
    volume = {}
    for batch, sync in ((10, "FULL"), (60, "FULL"), (300, "FULL"), (60, "NORMAL")):
        conn = connect(path, synchronous=sync)
        latencies = []
        written = io_written()
        ingest(conn, synth, cursor, cursor + volume_hours * HOUR, batch, latencies)
        conn.close()
        cursor += volume_hours * HOUR
        per_day = DAY / (volume_hours * HOUR)
        volume[f"dávka {batch} s, {sync}"] = {
            "mb_per_day": (io_written() - written) / MB * per_day if written is not None else None,
            "commit_ms_median": statistics.median(latencies),
            "commit_ms_p95": p95(latencies),
        }
    result["volume"] = volume

    # Retence: nejstarší den s secure_delete=ON (výchozí sestavení Ubuntu), další s FAST.
    retention = {}
    for day, mode in ((0, "ON"), (1, "FAST")):
        conn = connect(path, secure_delete=mode)
        retention[f"secure_delete={mode}"] = retention_day(conn, START + day * DAY)
        conn.close()
    result["retention"] = retention

    # Denní změna snímku: kolik stránek kopie se změní za den provozu (odhad pro Restic).
    conn = connect(path)
    result["snapshot"] = snapshot(conn, snap1)
    ingest(conn, synth, cursor, cursor + DAY, 60)
    retention_day(conn, START + 2 * DAY)
    snapshot(conn, snap2)
    changed, total = changed_pages(snap1, snap2)
    result["snapshot_changed"] = (changed, total)
    result["freelist_pages"] = conn.execute("PRAGMA freelist_count").fetchone()[0]
    conn.close()
    for file in (path, snap1, snap2):
        file.unlink()
    return result


# --- 4. Agregace ---------------------------------------------------------------------

def bench_agg(directory, bucket_seconds, days, commit_seconds_list):
    path = directory / f"agg_{bucket_seconds}.sqlite3"
    synth = Synthetic(seed=bucket_seconds)

    def write(conn, start, end, commit_seconds, latencies=None):
        rows, commit_end = [], start + commit_seconds
        for bucket in range(start, end, bucket_seconds):
            if bucket >= commit_end and rows:
                began = time.perf_counter()
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany("INSERT INTO agg VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING", rows)
                conn.execute("COMMIT")
                if latencies is not None:
                    latencies.append((time.perf_counter() - began) * 1000)
                rows, commit_end = [], commit_end + commit_seconds
            rows += synthetic_agg_rows(synth, bucket, bucket_seconds)
        if rows:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("INSERT INTO agg VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING", rows)
            conn.execute("COMMIT")

    conn = connect(path, synchronous="OFF")
    create_metrics(conn)
    conn.execute(AGG_DDL)
    began = time.perf_counter()
    write(conn, START, START + days * DAY, DAY)
    result = {"bucket_s": bucket_seconds, "fill_s": time.perf_counter() - began, "size_mb": size_mb(conn)}
    result["rows"] = conn.execute("SELECT count(*) FROM agg").fetchone()[0]
    end = START + days * DAY
    result["read_ms"] = {
        label: median_ms(lambda: conn.execute(READ_AGG, (2, end - span, end)).fetchall())
        for label, span in (("7 d", 7 * DAY), ("30 d", 30 * DAY), (f"{days} d", days * DAY))
    }
    conn.close()

    volume = {}
    cursor = end
    for commit_seconds in commit_seconds_list:
        conn = connect(path)
        written = io_written()
        write(conn, cursor, cursor + DAY, commit_seconds)
        conn.close()
        cursor += DAY
        volume[f"commit každých {commit_seconds} s"] = (io_written() - written) / MB if written is not None else None
    result["volume_mb_per_day"] = volume
    path.unlink()
    return result


# --- 5. Dlouhodobý cyklus nad navrženou databází -------------------------------------

def publish_snapshot(conn, history_dir, snapshot_dir, created, corrupt=False):
    """Snímek podle HISTORY.md 9.2; vrací záznam o snímku, nebo None při selhání.

    Dočasný soubor vzniká v history/ (mimo Restic) a do snapshot/ se dostane
    jen hotový a ověřený soubor atomickým přejmenováním. `corrupt` poškodí
    kopii před kontrolou (simulace selhání).
    """
    tmp, marker_tmp = history_dir / "snapshot.tmp", history_dir / "snapshot.json.tmp"
    for stale in (tmp, Path(f"{tmp}-journal"), marker_tmp):
        stale.unlink(missing_ok=True)  # zbytek po předchozím přerušeném pokusu
    began = time.perf_counter()
    target = sqlite3.connect(tmp, isolation_level=None)
    conn.backup(target)
    target.execute("PRAGMA journal_mode = DELETE")
    target.execute("INSERT INTO meta (key, value) VALUES ('snapshot_utc', ?)", (str(created),))
    counts = {table: target.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in COUNTED_TABLES}
    root = target.execute("SELECT rootpage FROM sqlite_schema WHERE name = 'sample'").fetchone()[0]
    page_size = target.execute("PRAGMA page_size").fetchone()[0]
    target.close()
    backup_s = time.perf_counter() - began
    if corrupt:
        with open(tmp, "r+b") as file:
            file.seek((root - 1) * page_size)
            file.write(b"\xff" * page_size)

    began = time.perf_counter()
    try:
        check = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        ok = (check.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
              and not check.execute("PRAGMA foreign_key_check").fetchall()
              and check.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
              and check.execute("PRAGMA user_version").fetchone()[0] == 1)
        check.close()
    except sqlite3.DatabaseError:
        ok = False
    check_s = time.perf_counter() - began
    if not ok:
        tmp.unlink()
        return None

    began = time.perf_counter()
    fsync_path(tmp)
    final = snapshot_dir / "history-snapshot.sqlite3"
    os.replace(tmp, final)
    marker = {"created_utc": created, "schema_version": 1, "sha256": sha256(final), "size": final.stat().st_size,
              "counts": counts, "integrity": "ok"}
    # Záznam až po snímku: při pádu mezi nimi záznam ukazuje starší snímek (bezpečná strana).
    marker_tmp.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
    fsync_path(marker_tmp)
    os.replace(marker_tmp, snapshot_dir / "history-snapshot.json")
    fsync_path(snapshot_dir)
    return {"marker": marker, "backup_s": backup_s, "check_s": check_s, "publish_s": time.perf_counter() - began}


def verify_restore(snapshot_dir, restore_dir):
    """Zkouška obnovy podle HISTORY.md 9.4 nad kopií snímku (místo restic restore)."""
    marker = json.loads((snapshot_dir / "history-snapshot.json").read_text(encoding="utf-8"))
    copy = restore_dir / "history.sqlite3"
    shutil.copyfile(snapshot_dir / "history-snapshot.sqlite3", copy)
    checks = [("SHA-256 souboru odpovídá záznamu snímku", sha256(copy) == marker["sha256"])]
    conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    one = lambda statement, params=(): conn.execute(statement, params).fetchone()
    checks += [
        ("integrity_check ok", conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]),
        ("foreign_key_check bez řádků", not conn.execute("PRAGMA foreign_key_check").fetchall()),
        ("application_id a user_version", one("PRAGMA application_id")[0] == APPLICATION_ID and one("PRAGMA user_version")[0] == 1),
        ("samostatný soubor bez WAL", one("PRAGMA journal_mode")[0] == "delete"),
        ("meta.snapshot_utc = čas v záznamu", one("SELECT value FROM meta WHERE key = 'snapshot_utc'")[0] == str(marker["created_utc"])),
        ("počty řádků všech tabulek = záznam",
         all(one(f"SELECT count(*) FROM {table}")[0] == count for table, count in marker["counts"].items())),
    ]
    newest = one("SELECT max(ts) FROM sample")[0]
    reads = (conn.execute(READ_ONE, (2, newest - DAY, newest + 1)).fetchall(),
             conn.execute(doc_sql("read-agg"), {"metric_id": 2, "start": newest - 7 * DAY, "end": newest}).fetchall(),
             conn.execute(doc_sql("gaps"), {"metric_id": 2, "start": newest - DAY, "end": newest + 1, "max_step": 2 * INTERVAL}).fetchall())
    checks.append(("čtecí dotazy webu vrací data (graf 24 h, agregace 7 d, mezery)", bool(reads[0]) and bool(reads[1]) and bool(reads[2])))
    conn.close()
    checks.append(("čtení jen pro čtení nevytvořilo -wal ani -shm",
                   not Path(f"{copy}-wal").exists() and not Path(f"{copy}-shm").exists()))
    return checks


class Reader(threading.Thread):
    """Čtenář jako budoucí web: krátké dotazy, každý ve vlastní transakci, vlastní spojení jen pro čtení."""

    def __init__(self, path, clock):
        super().__init__(daemon=True)
        self.path, self.clock = path, clock
        self.stop = threading.Event()
        self.latencies = []

    def run(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        read_agg = doc_sql("read-agg")
        while not self.stop.is_set():
            now = self.clock[0]
            for statement, params in ((READ_ONE, (2, now - DAY, now)),
                                      (read_agg, {"metric_id": 2, "start": now - 7 * DAY, "end": now}),
                                      (READ_ALL, (now - HOUR, now))):
                began = time.perf_counter()
                conn.execute(statement, params).fetchall()
                self.latencies.append((time.perf_counter() - began) * 1000)
            time.sleep(0.05)  # web s několika obnoveními za sekundu, víc než skutečný provoz
        conn.close()


class SteadyWriter:
    """Cyklus zapisovače podle HISTORY.md 5–9 nad dočasnou databází se simulovaným časem.

    Minuta = jedna dávka (dávka 60 s), po ní agregace uzavřených oken, na celou
    hodinu retence, jednou denně snímek. Je to měřené pořadí operací s SQL
    z dokumentace, ne kód aplikace (M3).
    """

    def __init__(self, conn, path, history_dir, snapshot_dir, synth, cursor, raw_keep, agg_keep):
        self.conn, self.path = conn, path
        self.history_dir, self.snapshot_dir = history_dir, snapshot_dir
        self.synth, self.cursor = synth, cursor
        self.raw_keep, self.agg_keep = raw_keep, agg_keep
        self.sql = {name: doc_sql(name) for name in STEADY_SQL}
        self.clock = [cursor]
        self.batch = 0
        self.conflicts = 0
        self.reset_day()

    def reset_day(self):
        self.latencies, self.wal_max, self.snapshots = [], 0, []
        self.transactions = Counter()
        self.retention_cutoff = None

    def done(self, level):
        return self.conn.execute("SELECT done_until FROM aggregation_state WHERE level = ?", (level,)).fetchone()[0]

    def guarded(self, guard, guard_params, statement, params):
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(self.sql[guard], guard_params).rowcount != 1:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"ochrana {guard} odmítla transakci {guard_params}")
        conn.execute(self.sql[statement], params)
        conn.execute("COMMIT")

    def minute(self, snapshots=True):
        conn, start = self.conn, self.cursor
        end = start + 60
        rows = [{"metric_id": metric, "ts": ts, "value": value}
                for _, cycle in self.synth.cycles(start, end) for metric, ts, value in cycle]
        changes = [{"ts": start, "run_id": 1, "details": json.dumps({"interface": "eth0", "link": "UP"})}] if start % 1800 == 0 else []
        self.cursor = self.clock[0] = end
        if rows or changes:
            self.batch += 1
            began = time.perf_counter()
            conn.execute("BEGIN IMMEDIATE")
            guard = {"run_id": 1, "batch": self.batch, "max_ts": max(row["ts"] for row in rows) if rows else None}
            if conn.execute(self.sql["batch-guard"], guard).rowcount != 1:
                raise RuntimeError("ochrana dávky odmítla novou dávku")
            if rows:
                self.conflicts += len(rows) - conn.executemany(self.sql["insert-sample"], rows).rowcount
            conn.executemany(CHANGE_SQL, changes)
            conn.execute("COMMIT")
            self.latencies.append((time.perf_counter() - began) * 1000)
            self.transactions["dávka"] += 1
            self.wal_max = max(self.wal_max, file_size(Path(f"{self.path}-wal")))
        self.aggregate(end)
        if end % HOUR == 0:
            self.retention(end)
        if snapshots and end % DAY == SNAPSHOT_AT:
            written = io_written()
            info = publish_snapshot(conn, self.history_dir, self.snapshot_dir, end)
            info["mb"] = (io_written() - written) / MB if written is not None else None
            self.snapshots.append(info)

    def aggregate(self, now):
        # Uzavřené okno: konec nejpozději now − GRACE; fronta je v simulaci vždy zapsaná.
        limit = (now - GRACE) // 300 * 300
        done = self.done("5m")
        while done < limit:
            end = min(done + HOUR, limit)
            self.guarded("progress-5m", {"start": done, "end": end}, "aggregate-5m", {"start": done, "end": end})
            self.transactions["agregace 5 min"] += 1
            done = end
        limit, done = done // HOUR * HOUR, self.done("1h")
        while done < limit:
            end = min(done + DAY, limit)
            self.guarded("progress-1h", {"start": done, "end": end}, "rollup-1h", {"start": done, "end": end})
            self.transactions["agregace 1 h"] += 1
            done = end

    def retention(self, now):
        # Surový vzorek jen když je zároveň starší než raw_keep a jeho okno je agregované.
        conn = self.conn
        self.retention_cutoff = cutoff = min(now - self.raw_keep, self.done("5m"))
        for _ in range(24):
            oldest = conn.execute(self.sql["oldest-sample"]).fetchone()
            if oldest is None or oldest[0] >= cutoff:
                break
            slice_end = min(cutoff, oldest[0] - oldest[0] % HOUR + HOUR)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(self.sql["retention-sample"], {"cutoff": slice_end})
            conn.execute("COMMIT")
            self.transactions["retence sample"] += 1
        cutoff = min((now - self.agg_keep) // HOUR * HOUR, self.done("1h"))
        for _ in range(24):
            oldest = conn.execute(self.sql["oldest-5m"]).fetchone()[0]
            if oldest is None or oldest >= cutoff:
                break
            slice_end = min(cutoff, oldest - oldest % HOUR + HOUR)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(self.sql["retention-5m"], {"cutoff": slice_end})
            conn.execute("COMMIT")
            self.transactions["retence 5 min"] += 1


def verify_steady(conn, writer):
    """Invarianty po cyklu: retence, shoda agregací se surovými daty, žádné dvojí započítání."""
    one = lambda statement, params=(): conn.execute(statement, params).fetchone()
    checks = []
    oldest = one("SELECT min(ts) FROM sample")[0]
    checks.append(("nejstarší surový vzorek není před hranicí retence", oldest >= writer.retention_cutoff))
    checks.append(("surová data drží aspoň dobu retence",
                   oldest <= writer.cursor - writer.raw_keep + HOUR))
    low, high = -(-oldest // 300) * 300, writer.done("5m")
    recomputed = """SELECT metric_id, ts - ts % 300 AS bucket, count(*) AS n, count(value) AS v,
                           min(value) AS lo, max(value) AS hi, sum(value) AS total
                    FROM sample WHERE ts >= :low AND ts < :high GROUP BY metric_id, ts - ts % 300"""
    params = {"low": low, "high": high}
    windows = one(f"SELECT count(*) FROM ({recomputed})", params)[0]
    stored = one("SELECT count(*) FROM agg_5m WHERE bucket >= :low AND bucket < :high", params)[0]
    matching = one(f"""SELECT count(*) FROM ({recomputed}) r JOIN agg_5m a USING (metric_id, bucket)
                       WHERE a.n_samples = r.n AND a.n_valid = r.v AND a.min_value IS r.lo AND a.max_value IS r.hi
                         AND (a.sum_value IS r.total OR abs(a.sum_value - r.total) <= 1e-9 * abs(r.total))""", params)[0]
    checks.append((f"5min agregace = přepočet ze surových dat ({windows:,} oken)", windows == stored == matching > 0))
    counted = one("SELECT sum(n_samples) FROM agg_5m WHERE bucket >= :low AND bucket < :high", params)[0]
    raw = one("SELECT count(*) FROM sample WHERE ts >= :low AND ts < :high", params)[0]
    checks.append(("žádné dvojí započítání: součet n_samples = počet surových vzorků", counted == raw))
    low_h = -(-one("SELECT min(bucket) FROM agg_5m")[0] // HOUR) * HOUR
    high_h = writer.done("1h")
    hours = one("""SELECT count(*) FROM (SELECT metric_id, bucket - bucket % 3600 AS b, sum(n_samples) AS n, sum(n_valid) AS v,
                                         min(min_value) AS lo, max(max_value) AS hi, sum(sum_value) AS total
                                  FROM agg_5m WHERE bucket >= :low AND bucket < :high GROUP BY metric_id, bucket - bucket % 3600) r
                   JOIN agg_1h a ON a.metric_id = r.metric_id AND a.bucket = r.b
                   WHERE a.n_samples = r.n AND a.n_valid = r.v AND a.min_value IS r.lo AND a.max_value IS r.hi
                     AND (a.sum_value IS r.total OR abs(a.sum_value - r.total) <= 1e-9 * abs(r.total))""",
                {"low": low_h, "high": high_h})[0]
    stored_h = one("SELECT count(*) FROM agg_1h WHERE bucket >= ? AND bucket < ?", (low_h, high_h))[0]
    checks.append((f"hodinové agregace = součet 5min oken ({stored_h:,} hodin)", hours == stored_h > 0))
    checks.append(("neočekávané konflikty vzorků", writer.conflicts == 0))
    checks.append(("integrity_check ok", conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]))
    checks.append(("foreign_key_check bez řádků", not conn.execute("PRAGMA foreign_key_check").fetchall()))
    return checks


def bench_steady(directory, raw_days, agg_days, run_days, reader_day, hold_minutes):
    """Plná navržená databáze po roce provozu, pak `run_days` dní cyklu zápis → agregace → retence → snímek."""
    base = directory / "steady"
    history_dir, snapshot_dir, restore_dir = base / "history", base / "snapshot", base / "restore"
    for folder in (history_dir, snapshot_dir, restore_dir):
        folder.mkdir(parents=True)
    path = history_dir / "history.sqlite3"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(doc_sql("connection"))
    conn.executescript(doc_sql("ddl"))
    sql = {name: doc_sql(name) for name in STEADY_SQL}
    began = time.perf_counter()

    # Naplnění (synchronous=OFF jen kvůli délce běhu): syntetické 5min agregace
    # za starší část roku, surová data za posledních raw_days a jejich agregace
    # navrženými dotazy, hodinové agregace z 5min oken.
    conn.execute("PRAGMA synchronous = OFF")
    create_metrics(conn, create=False)
    raw_start = START + (agg_days - raw_days) * DAY
    now = raw_start + raw_days * DAY
    conn.execute("INSERT INTO run (id, started_utc, app_version, interval_s) VALUES (1, ?, 'steady', 10)", (raw_start,))
    conn.executemany("INSERT INTO aggregation_state (level, done_until) VALUES (?, ?)", [("5m", raw_start), ("1h", START)])
    synth = Synthetic()
    conn.execute("BEGIN")
    for bucket in range(START, raw_start, 300):
        conn.executemany("INSERT INTO agg_5m VALUES (?, ?, ?, ?, ?, ?, ?)", synthetic_agg_rows(synth, bucket, 300))
    conn.execute("COMMIT")
    ingest(conn, synth, raw_start, now, 60)
    writer = SteadyWriter(conn, path, history_dir, snapshot_dir, synth, now, raw_days * DAY, agg_days * DAY)
    limit = (now - GRACE) // 300 * 300
    for start in range(raw_start, limit, DAY):
        end = min(start + DAY, limit)
        writer.guarded("progress-5m", {"start": start, "end": end}, "aggregate-5m", {"start": start, "end": end})
    writer.aggregate(now)
    conn.execute("UPDATE run SET last_write_utc = (SELECT max(ts) FROM sample) WHERE id = 1")
    conn.execute("PRAGMA synchronous = FULL")
    initial_mb = size_mb(conn)
    result = {"prefill_s": time.perf_counter() - began, "initial_mb": initial_mb, "days": []}

    for day in range(1, run_days + 1):
        writer.reset_day()
        reader = Reader(path, writer.clock) if day == reader_day else None
        if reader:
            reader.start()
        counters = io_counters()
        began = time.perf_counter()
        for _ in range(DAY // 60):
            writer.minute()
        elapsed = time.perf_counter() - began
        if reader:
            reader.stop.set()
            reader.join()
        after = io_counters()
        result["days"].append({
            "day": day, "reader": reader is not None, "elapsed_s": elapsed,
            "size_mb": file_size(path) / MB, "wal_max_mb": writer.wal_max / MB,
            "freelist": conn.execute("PRAGMA freelist_count").fetchone()[0],
            "wchar_mb": (after["wchar"] - counters["wchar"]) / MB if counters else None,
            "write_bytes_mb": (after["write_bytes"] - counters["write_bytes"]) / MB if counters else None,
            "commit_median_ms": statistics.median(writer.latencies), "commit_p95_ms": p95(writer.latencies),
            "transactions": dict(writer.transactions), "snapshots": writer.snapshots,
            "reader_p95_ms": p95(reader.latencies) if reader else None,
            "reader_queries": len(reader.latencies) if reader else 0,
        })

    # Dlouhá čtecí transakce: čtenář drží jeden snímek dat, checkpoint nemůže WAL vrátit na začátek.
    writer.reset_day()
    holder = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)  # jen čtenář (5.8)
    holder.execute("BEGIN")
    holder.execute("SELECT count(*) FROM metric").fetchone()
    for _ in range(hold_minutes):
        writer.minute(snapshots=False)
    wal_held = writer.wal_max
    holder.execute("COMMIT")
    holder.close()
    for _ in range(30):
        writer.minute(snapshots=False)
    result["long_reader"] = {"minutes": hold_minutes, "wal_max_mb": wal_held / MB,
                             "wal_after_mb": file_size(Path(f"{path}-wal")) / MB}

    result["checks"] = verify_steady(conn, writer)

    # Selhání snímku: poškozená kopie se nezveřejní, starý snímek i záznam zůstanou.
    before = (sha256(snapshot_dir / "history-snapshot.sqlite3"), (snapshot_dir / "history-snapshot.json").read_text())
    failed = publish_snapshot(conn, history_dir, snapshot_dir, writer.cursor, corrupt=True)
    after = (sha256(snapshot_dir / "history-snapshot.sqlite3"), (snapshot_dir / "history-snapshot.json").read_text())
    result["checks"].append(("poškozený snímek se nezveřejní, předchozí snímek a záznam zůstanou",
                             failed is None and before == after and not (history_dir / "snapshot.tmp").exists()))
    result["checks"].append(("v adresáři snapshot/ jsou jen hotové soubory",
                             sorted(item.name for item in snapshot_dir.iterdir()) == ["history-snapshot.json", "history-snapshot.sqlite3"]))
    result["restore"] = verify_restore(snapshot_dir, restore_dir)
    result["rows"] = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("sample", "agg_5m", "agg_1h")}
    result["final_mb"] = size_mb(conn)
    conn.close()
    shutil.rmtree(base)
    return result


# --- Výstup --------------------------------------------------------------------------

def mb(value, suffix=" MB"):
    return "nezjištěno" if value is None else f"{value:.1f}{suffix}"


def report_raw(result):
    print(f"\n{result['variant']}")
    print(f"  řádků {result['rows']:,}, velikost {result['size_mb']:.1f} MB ({result['bytes_per_row']:.1f} B/řádek), "
          f"plnění {result['fill_rows_per_s']:,.0f} řádků/s")
    print("  objekty: " + ", ".join(f"{name} {size:.1f} MB" for name, size in result["objects_mb"].items()))
    print("  čtení (medián, teplá cache): " + ", ".join(f"{k} {v:.1f} ms" for k, v in result["read_ms"].items()))
    print(f"  agregace 1 h všech řad do 5min oken: {result['aggregate_hour_ms']:.1f} ms")
    print(f"  quick_check {result['quick_check_s']:.2f} s, integrity_check {result['integrity_check_s']:.2f} s")
    for label, values in result["volume"].items():
        print(f"  {label}: zápis {mb(values['mb_per_day'], ' MB/den')}, commit medián {values['commit_ms_median']:.2f} ms, "
              f"p95 {values['commit_ms_p95']:.2f} ms")
    for label, (seconds, written) in result["retention"].items():
        print(f"  retence 1 dne ({label}): {seconds:.2f} s, zápis {mb(written)}")
    backup_s, check_s, check_result = result["snapshot"]
    changed, total = result["snapshot_changed"]
    print(f"  snímek Backup API {backup_s:.2f} s, integrity_check snímku {check_s:.2f} s ({check_result})")
    print(f"  změna snímku za den: {changed:,} z {total:,} stránek ({100 * changed / total:.1f} %), "
          f"volné stránky po retenci {result['freelist_pages']:,}")
    for label, text in result["plans"].items():
        print(f"  plán {label}: {text}")


def report_agg(result, days):
    name = {60: "1min", 300: "5min", 3600: "hodinové"}[result["bucket_s"]]
    print(f"\nAgregace {name}, {days} dní: řádků {result['rows']:,}, velikost {result['size_mb']:.1f} MB, "
          f"plnění {result['fill_s']:.1f} s")
    print("  čtení jedné řady: " + ", ".join(f"{k} {v:.1f} ms" for k, v in result["read_ms"].items()))
    for label, value in result["volume_mb_per_day"].items():
        print(f"  přírůstek 1 dne, {label}: zápis {mb(value)}")


def report_changes(results, hours):
    print(f"\nZměny stavu při trvalém kmitání (přechod při každém měření po 10 s, {hours} h, dávka na přechod, FULL)")
    for name, value in results.items():
        print(f"  {name}: {value['rows']:,} řádků, {value['bytes_per_row']:.0f} B/řádek včetně indexu ts, "
              f"{value['mb_per_day']:.2f} MB/den, za 2 roky {value['gb_2y']:.2f} GB; zápis {mb(value['wchar_mb_per_day'], ' MB/den')}, "
              f"commit {value['commit_ms']:.2f} ms")


def report_checks(title, checks):
    print(f"\n{title}: OK ({len(checks)} kontrol)")
    for text in checks:
        print(f"  - {text}")


def report_steady(result, raw_days, agg_days):
    print(f"\nDlouhodobý cyklus (surová data {raw_days} d, 5min agregace {agg_days} d, dávka 60 s, FULL, secure_delete=FAST)")
    print(f"  naplnění {result['prefill_s']:.0f} s, výchozí velikost {result['initial_mb']:.1f} MB")
    for day in result["days"]:
        snapshot = day["snapshots"][0] if day["snapshots"] else None
        snap_text = (f", snímek {snapshot['backup_s']:.2f} + kontrola {snapshot['check_s']:.2f} + zveřejnění "
                     f"{snapshot['publish_s']:.2f} s, zápis snímku {mb(snapshot['mb'])}") if snapshot else ""
        reader = (f", čtenář {day['reader_queries']:,} dotazů, p95 {day['reader_p95_ms']:.1f} ms" if day["reader"] else "")
        print(f"  den {day['day']}: soubor {day['size_mb']:.1f} MB, WAL max {day['wal_max_mb']:.1f} MB, volné stránky "
              f"{day['freelist']:,}, wchar {mb(day['wchar_mb'])}, write_bytes {mb(day['write_bytes_mb'])}, commit "
              f"{day['commit_median_ms']:.2f}/{day['commit_p95_ms']:.2f} ms{reader}{snap_text}")
        print("         transakce: " + ", ".join(f"{name} {count}" for name, count in day["transactions"].items())
              + f"; běh {day['elapsed_s']:.0f} s")
    held = result["long_reader"]
    print(f"  dlouhá čtecí transakce {held['minutes']} min: WAL až {held['wal_max_mb']:.1f} MB, "
          f"30 min po uvolnění {held['wal_after_mb']:.1f} MB")
    print("  řádků na konci: " + ", ".join(f"{table} {count:,}" for table, count in result["rows"].items())
          + f"; velikost po checkpointu {result['final_mb']:.1f} MB")
    for text, ok in result["checks"] + result["restore"]:
        if not ok:
            raise SystemExit(f"Kontrola dlouhodobého cyklu selhala: {text}")
    report_checks("Invarianty cyklu, selhání snímku a zkouška obnovy", [text for text, _ in result["checks"] + result["restore"]])


def main(argv=None):
    parser = argparse.ArgumentParser(description="Benchmark návrhu SQLite historie NexusSTAT (jen dočasná data)")
    parser.add_argument("--quick", action="store_true", help="zmenšená data pro kontrolu skriptu")
    parser.add_argument("--dir", type=Path, help="nadřazený dočasný adresář (výchozí TMPDIR nebo /tmp)")
    parser.add_argument("--keep", action="store_true", help="dočasný adresář na konci nesmazat")
    parser.add_argument("--only", choices=["selfcheck", "raw", "agg", "changes", "steady"], help="jen jedna část")
    args = parser.parse_args(argv)
    raw_days, agg_days, volume_hours = (4, 20, 1) if args.quick else (30, 365, 6)
    # Dlouhodobý cyklus: (surová data, agregace, dny cyklu, den se čtenářem, minuty dlouhé transakce).
    steady = (2, 10, 2, 2, 60) if args.quick else (30, 365, 7, 6, 360)

    directory = Path(tempfile.mkdtemp(prefix="nexusstat-m2-bench-", dir=args.dir))
    print(f"SQLite {sqlite3.sqlite_version}, Python {sys.version.split()[0]}, dočasný adresář {directory}")
    print(f"Syntetická data: {len(SERIES)} řad po {INTERVAL} s, denní výpadek {GAP_LENGTH} s, "
          f"UNKNOWN {UNKNOWN_SHARE:.1%}; surová data {raw_days} dní, agregace {agg_days} dní")
    try:
        if args.only in (None, "selfcheck"):
            report_checks(f"Kontrola SQL z {DOC.name}", selfcheck(directory))
            report_checks("Model pravidel času, identity a stavu snímku", selfcheck_rules())
            report_checks("Regresní scénáře R4 (každý přechod samostatně)", selfcheck_r4(directory))
            report_checks(f"Jeden zapisovač (SQLite {sqlite3.sqlite_version})", selfcheck_single_writer(directory))
        if args.only in (None, "raw"):
            for key in RAW_VARIANTS:
                report_raw(bench_raw(directory, key, raw_days, volume_hours))
        if args.only in (None, "agg"):
            report_agg(bench_agg(directory, 60, agg_days, (60, 300)), agg_days)
            report_agg(bench_agg(directory, 300, agg_days, (300,)), agg_days)
            report_agg(bench_agg(directory, 3600, agg_days, (3600,)), agg_days)
        if args.only in (None, "changes"):
            report_changes(bench_changes(directory, 2 if args.quick else 24), 2 if args.quick else 24)
        if args.only in (None, "steady"):
            report_steady(bench_steady(directory, *steady), steady[0], steady[1])
    finally:
        if args.keep:
            print(f"\nDočasný adresář ponechán: {directory}")
        else:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    main()
