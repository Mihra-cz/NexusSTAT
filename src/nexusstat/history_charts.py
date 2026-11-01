"""Čtení historie SQLite pro grafy lokálního webu (M6-2), jen pro čtení.

Databázi otevírá jedině `history.open_reader()`: spojení `mode=ro`,
`query_only`, bez checkpointu i při zavření a nikdy `immutable=1`. Neexistující
soubor se nevytvoří, schéma ani data se nemění. Celé čtení je jedna krátká
čtecí transakce s časovým limitem a s omezeným počtem vrácených řádků; spojení
se hned zavře.

Pravidla čtení (docs/HISTORY.md, sekce 6 a 3.3):

* 1 h a 24 h ze surových vzorků `sample`, 7 a 30 dní z `agg_5m` doplněné
  surovými vzorky za `aggregation_state.done_until` (ještě neagregovaná
  poslední okna). Delší rozsahy (`agg_1h`) web zatím nenabízí.
* Shlukuje už SQL do stovek bodů na řadu: počet řádků (`n_samples`, včetně
  UNKNOWN), počet platných (`n_valid`), minimum, maximum a součet. Průměr je
  součet / počet platných, nikdy průměr průměrů a nikdy s UNKNOWN jako nulou.
* Okno jen s UNKNOWN zůstane neznámé, okno bez řádku je mezera.

Výsledek se drží v paměti (`cache_seconds` podle rozsahu), takže obnova
stránky ani víc záložek nečtou disk znovu a hlavní dashboard SQLite nečte vůbec.
Chyba čtení se ukáže jen na stránce historie, měření ani dashboard nezastaví.
"""

import sqlite3
import threading
import time
from dataclasses import dataclass

from . import history
from .network_monitor import RX_RATE_ID, TX_RATE_ID
from .system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID


@dataclass(frozen=True)
class Range:
    """Zobrazený rozsah: délka, zdroj podle HISTORY.md, krok bodu a doba platnosti výsledku."""

    name: str
    label: str
    seconds: int
    source: str  # "sample" nebo "agg_5m"
    step: int  # sekundy jednoho bodu grafu (násobek 300 u agregací)
    cache_seconds: int


RANGES = {
    "1h": Range("1h", "1 hodina", 3600, "sample", 10, 30),
    "24h": Range("24h", "24 hodin", 86_400, "sample", 300, 120),
    "7d": Range("7d", "7 dní", 7 * 86_400, "agg_5m", 1800, 600),
    "30d": Range("30d", "30 dní", 30 * 86_400, "agg_5m", 7200, 1800),
}
DEFAULT_RANGE = "24h"

# Řady grafů v pořadí stránky. RX a TX mají řadu pro každé rozhraní (subject).
CHART_CHECKS = (CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID, RX_RATE_ID, TX_RATE_ID, DISK_ROOT_ID)
# Nejdelší čtení jedné stránky; delší dotaz SQLite přeruší (progress handler).
QUERY_SECONDS = 2.0
PROGRESS_STEPS = 1000  # po kolika krocích virtuálního stroje SQLite se kontroluje limit
ERROR_CACHE_SECONDS = 30  # nedostupnou databázi nezkoušet při každé obnově

METRICS_SQL = "SELECT id, check_id, subject, unit FROM metric WHERE check_id IN ({}) ORDER BY id"
DONE_5M_SQL = "SELECT done_until FROM aggregation_state WHERE level = '5m'"
# Názvy tabulek a sloupců jsou pevné v kódu; od klienta je jen název rozsahu z RANGES.
SAMPLE_SQL = """
SELECT metric_id, ts - ts % ? AS bucket, count(*), count(value), min(value), max(value), sum(value)
FROM sample
WHERE ts >= ? AND ts < ? AND metric_id IN ({})
GROUP BY metric_id, bucket
ORDER BY metric_id, bucket
LIMIT ?
"""
AGG_5M_SQL = """
SELECT metric_id, bucket - bucket % ? AS chart_bucket, sum(n_samples), sum(n_valid), min(min_value), max(max_value),
       sum(sum_value)
FROM agg_5m
WHERE metric_id IN ({}) AND bucket >= ? AND bucket < ?
GROUP BY metric_id, chart_bucket
ORDER BY metric_id, chart_bucket
LIMIT ?
"""


class ChartError(Exception):
    """Historii nejde přečíst; `reason` je krátký kód pro stránku (bez cest a adres)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Bucket:
    """Jeden bod grafu: začátek okna (UTC s), řádky včetně UNKNOWN, platné, min, max, součet platných."""

    start: int
    n_samples: int
    n_valid: int
    minimum: float | None
    maximum: float | None
    total: float | None

    @property
    def average(self):
        return self.total / self.n_valid if self.n_valid else None


@dataclass(frozen=True)
class Series:
    check_id: str
    subject: str
    unit: str
    buckets: tuple


@dataclass(frozen=True)
class Charts:
    """Přečtené řady jednoho rozsahu: [start, end) a čas čtení v UTC sekundách, počet SQL dotazů."""

    period: Range
    start: int
    end: int
    series: tuple
    queries: int
    read_utc: int


def merge(first, second):
    """Spojí dva disjunktní úseky téhož okna (agregace + surové vzorky za done_until)."""
    def pick(function, a, b):
        return b if a is None else a if b is None else function(a, b)

    return Bucket(first.start, first.n_samples + second.n_samples, first.n_valid + second.n_valid,
                  pick(min, first.minimum, second.minimum), pick(max, first.maximum, second.maximum),
                  pick(lambda x, y: x + y, first.total, second.total))


def read_charts(connection, chart_range, end_utc, deadline):
    """Řady pro `chart_range` končící v `end_utc` z otevřeného spojení jen pro čtení; ChartError při chybě.

    `deadline` je čas `time.monotonic()`, po kterém SQLite dotaz přeruší.
    """
    step = chart_range.step
    end = end_utc - end_utc % step + step  # včetně rozpracovaného posledního okna
    start = end - chart_range.seconds
    queries = 0

    def run(sql, parameters):
        nonlocal queries
        queries += 1
        return connection.execute(sql, parameters).fetchall()

    try:
        # Jedna krátká čtecí transakce: všechny řady ze stejného stavu databáze. Limit
        # hlídá jen dotazy; ROLLBACK proběhne vždy bez něj, takže chybu dotazu nezakryje.
        connection.execute("BEGIN")
        connection.set_progress_handler(lambda: time.monotonic() > deadline, PROGRESS_STEPS)
        try:
            metrics = run(METRICS_SQL.format(",".join("?" * len(CHART_CHECKS))), CHART_CHECKS)
            ids = [row[0] for row in metrics]
            buckets = {}
            if ids:
                marks = ",".join("?" * len(ids))
                limit = len(ids) * (chart_range.seconds // step + 2)
                split = start
                if chart_range.source == "agg_5m":
                    done = run(DONE_5M_SQL, ())
                    split = min(max(done[0][0] if done else start, start), end)
                    for row in run(AGG_5M_SQL.format(marks), (step, *ids, start, split, limit)):
                        buckets[(row[0], row[1])] = Bucket(*row[1:])
                for row in run(SAMPLE_SQL.format(marks), (step, split, end, *ids, limit)):
                    key, part = (row[0], row[1]), Bucket(*row[1:])
                    buckets[key] = merge(buckets[key], part) if key in buckets else part
        finally:
            connection.set_progress_handler(None, 0)
            connection.execute("ROLLBACK")
    except sqlite3.Error as error:
        raise ChartError("timeout" if "interrupt" in str(error) else "sqlite") from None
    order = {check_id: index for index, check_id in enumerate(CHART_CHECKS)}
    series = [Series(check_id, subject, unit,
                     tuple(sorted((b for (metric, _), b in buckets.items() if metric == metric_id),
                                  key=lambda b: b.start)))
              for metric_id, check_id, subject, unit in metrics]
    series.sort(key=lambda item: (order[item.check_id], item.subject))
    return Charts(chart_range, start, end, tuple(series), queries, end_utc)


class HistoryCharts:
    """Grafy historie pro web: otevře databázi jen pro čtení, přečte a hned zavře; výsledek drží v paměti.

    `host_id` se zjistí až při prvním čtení (tentýž otisk jako zapisovač). Souběžné
    požadavky na tentýž rozsah počkají na jedno čtení, nikdy nečtou dvakrát.
    """

    def __init__(self, path, role, interval_seconds, clock=time.monotonic, utc_clock=time.time,
                 host_id_factory=history.local_host_id, opener=history.open_reader):
        self.path = path
        self.role = role
        self.interval_seconds = interval_seconds  # zápis do historie: rozestup surových vzorků
        self.clock = clock
        self.utc_clock = utc_clock
        self.host_id_factory = host_id_factory
        self.opener = opener
        self.reads = 0  # počet skutečných čtení databáze (pro diagnostiku a testy)
        self._host_id = None  # otisk stroje se čte jednou, ne při každém čtení
        self._lock = threading.Lock()
        self._cache = {}

    def charts(self, name):
        """Charts nebo ChartError pro rozsah `name` (klíč RANGES); jiný klíč je ValueError."""
        chart_range = RANGES[name] if name in RANGES else None
        if chart_range is None:
            raise ValueError(f"neznámý rozsah {name!r}")
        with self._lock:
            now = self.clock()
            cached = self._cache.get(name)
            if cached is not None and now < cached[0]:
                return cached[1]
            try:
                result = self._read(chart_range)
                keep = chart_range.cache_seconds
            except ChartError as error:
                result, keep = error, ERROR_CACHE_SECONDS
            self._cache[name] = (now + keep, result)
            return result

    def _read(self, chart_range):
        self.reads += 1
        try:
            if self._host_id is None:
                self._host_id = self.host_id_factory()
            database = self.opener(self.path, role=self.role, host_id=self._host_id)
        except history.HistoryError as error:
            raise ChartError(error.reason) from None
        try:
            return read_charts(database.connection, chart_range, int(self.utc_clock()),
                               time.monotonic() + QUERY_SECONDS)
        finally:
            database.close()
