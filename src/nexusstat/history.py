"""SQLite historie NexusSTAT: databáze schématu v1 a synchronní zápis (M3.1a, M3.1b).

Podle docs/HISTORY.md (sekce 3–7.1):
- vytvoření nové testovací nebo vývojové databáze na výslovně zadané cestě,
- otevření pro zápis s ověřením identity a zámkem jediného zapisovače,
- otevření jen pro čtení, které nic nezapíše ani neprovede checkpoint,
- HistoryRecorder: běh, transakční dávky vzorků, změn stavu a událostí,
  agregace uzavřených oken do agg_5m a agg_1h (M3.2a) a retence s plánem
  bez zápisu (M3.2b).

Zápis, agregace i retence jsou synchronní a zatím je nic nevolá: vlákno,
fronty a napojení na run.py jsou M3.3, snímky přijdou později. Modul nemá
žádnou výchozí cestu, nikdy nevytváří adresář a nikdy nepřepíše ani nesmaže
existující soubor; maže jen vlastní dočasné soubory z nepovedeného vytvoření.
Produkční databáze se zatím nevytváří ani neotevírá. Nečte docs/HISTORY.md:
schéma a SQL jsou v konstantách a jejich shodu s dokumentací hlídá
tests/test_history.py.
"""

import fcntl
import functools
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import CheckResult, State, is_finite_number
from .network_monitor import BYTES_PER_SECOND, INTERFACE, RX_RATE_ID, TX_RATE_ID
from .system_monitor import CELSIUS, CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID, PERCENT

APPLICATION_ID = 0x4E585354  # 'NXST' v hlavičce souboru: soubor patří NexusSTAT
SCHEMA_VERSION = 1  # PRAGMA user_version

# STRICT tabulky (3.37) a vestavěné JSON funkce pro CHECK json_valid (3.38).
MIN_SQLITE_VERSION = (3, 38, 0)

# Produkční role existuje v návrhu, ale M3.1a pracuje jen s bezcennými databázemi.
ROLES = ("production", "development", "test")
ALLOWED_ROLES = ("development", "test")

# Otisk stroje v meta.host_id: prvních 16 znaků SHA-256 z /etc/machine-id.
HOST_ID_PATTERN = re.compile(r"[0-9a-f]{16}")
MACHINE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
DEFAULT_MACHINE_ID_PATH = "/etc/machine-id"

# Hlavička SQLite (prvních 100 bajtů): magický řetězec, user_version, application_id.
SQLITE_MAGIC = b"SQLite format 3\x00"
HEADER_SIZE = 100

PAGE_SIZE = 4096

# Nastavení každého zapisujícího spojení (docs/HISTORY.md 5.7): (pragma, hodnota,
# hodnota při zpětném čtení). Neplatí pro soubor, nastavují se při každém otevření.
CONNECTION_SETTINGS = (
    ("synchronous", "FULL", 2),
    ("foreign_keys", "ON", 1),
    ("busy_timeout", "5000", 5000),
    ("journal_size_limit", "8388608", 8388608),
    ("secure_delete", "FAST", 2),
)
# Vlastnost souboru: zapíše se do hlavičky a platí pro všechna další spojení.
JOURNAL_MODE = "wal"
READER_BUSY_TIMEOUT_MS = 5000

# Pomocné soubory SQLite vedle databáze. Cizí -wal nebo -journal by se mohl
# použít na nový soubor se stejným jménem, proto vytvoření skončí, když existují.
COMPANION_SUFFIXES = ("-wal", "-shm", "-journal")

# Schéma v1: doslovná kopie bloku <!-- sql:ddl --> z docs/HISTORY.md 3.2.
SCHEMA_SQL = """\
-- Identita souboru a verze schématu (hlavička databáze).
PRAGMA application_id = 1314411348;  -- 0x4E585354 = 'NXST'
PRAGMA user_version = 1;

-- Vlastnosti souboru: 'role' (production | development | test), 'host_id', 'created_utc';
-- jen ve snímku navíc 'snapshot_utc' (9.2).
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT, WITHOUT ROWID;

-- Provedené migrace; nemaže se.
CREATE TABLE schema_migration (
    version     INTEGER PRIMARY KEY,
    applied_utc INTEGER NOT NULL,
    description TEXT NOT NULL
) STRICT;

-- Jedno spuštění procesu NexusSTAT.
CREATE TABLE run (
    id             INTEGER PRIMARY KEY,
    started_utc    INTEGER NOT NULL,
    stopped_utc    INTEGER,            -- NULL jen u právě běžícího procesu
    clean_stop     INTEGER CHECK (clean_stop IN (0, 1)),  -- 0 = nečisté ukončení zjištěné při dalším startu
    last_write_utc INTEGER,            -- nejnovější zapsaný vzorek běhu (horní mez pro další běh)
    last_batch     INTEGER NOT NULL DEFAULT 0 CHECK (last_batch >= 0),  -- poslední zapsaná dávka (5.2)
    app_version    TEXT NOT NULL,
    interval_s     INTEGER NOT NULL CHECK (interval_s BETWEEN 1 AND 3600),
    boot_id        TEXT                -- /proc/sys/kernel/random/boot_id: restart procesu vs. systému
) STRICT;

-- Definice číselné řady: kontrola + konkrétní objekt (rozhraní, mount, ventilátor).
CREATE TABLE metric (
    id          INTEGER PRIMARY KEY,
    check_id    TEXT NOT NULL,              -- CheckResult.id, např. 'network.rx_rate'
    subject     TEXT NOT NULL DEFAULT '',   -- '' nebo např. 'eth0', '/'
    unit        TEXT NOT NULL CHECK (unit <> ''),  -- Measurement.unit: '%', '°C', 'B/s', 'RPM'
    created_utc INTEGER NOT NULL,
    UNIQUE (check_id, subject)
) STRICT;

-- Surové vzorky. Řádek = měření proběhlo; value NULL = výsledek UNKNOWN.
-- Chybějící řádek = měření neproběhlo nebo se nezapsalo (mezera).
-- Klíč začíná časem: nové řádky přibývají na konec, retence maže od začátku.
CREATE TABLE sample (
    metric_id INTEGER NOT NULL REFERENCES metric (id),
    ts        INTEGER NOT NULL,         -- čas měření, celé Unixové sekundy UTC (4.3)
    value     REAL,
    PRIMARY KEY (ts, metric_id)
) STRICT, WITHOUT ROWID;

-- 5min agregace ze sample. Řádek jen pro okno s aspoň jedním řádkem sample.
CREATE TABLE agg_5m (
    metric_id INTEGER NOT NULL REFERENCES metric (id),
    bucket    INTEGER NOT NULL CHECK (bucket % 300 = 0),   -- začátek okna, UTC
    n_samples INTEGER NOT NULL CHECK (n_samples > 0),      -- řádky sample v okně včetně UNKNOWN
    n_valid   INTEGER NOT NULL CHECK (n_valid BETWEEN 0 AND n_samples),  -- s hodnotou
    min_value REAL,
    max_value REAL,
    sum_value REAL,                                         -- průměr = sum_value / n_valid
    CHECK ((n_valid = 0) = (sum_value IS NULL)),
    CHECK ((sum_value IS NULL) = (min_value IS NULL) AND (sum_value IS NULL) = (max_value IS NULL)),
    PRIMARY KEY (metric_id, bucket)
) STRICT, WITHOUT ROWID;

-- Hodinové agregace z agg_5m (stejný význam sloupců).
CREATE TABLE agg_1h (
    metric_id INTEGER NOT NULL REFERENCES metric (id),
    bucket    INTEGER NOT NULL CHECK (bucket % 3600 = 0),
    n_samples INTEGER NOT NULL CHECK (n_samples > 0),
    n_valid   INTEGER NOT NULL CHECK (n_valid BETWEEN 0 AND n_samples),
    min_value REAL,
    max_value REAL,
    sum_value REAL,
    CHECK ((n_valid = 0) = (sum_value IS NULL)),
    CHECK ((sum_value IS NULL) = (min_value IS NULL) AND (sum_value IS NULL) = (max_value IS NULL)),
    PRIMARY KEY (metric_id, bucket)
) STRICT, WITHOUT ROWID;

-- Agregace jsou hotové pro všechna okna, která končí nejpozději v done_until.
CREATE TABLE aggregation_state (
    level      TEXT PRIMARY KEY CHECK (level IN ('5m', '1h')),
    done_until INTEGER NOT NULL
) STRICT, WITHOUT ROWID;

-- Každá skutečná změna diagnostického stavu nebo provozně významného údaje kontroly (7.1):
-- alarmy, UNKNOWN, N/A, linka UP/DOWN, IPv4, služby. Nic se neslučuje.
CREATE TABLE check_change (
    id         INTEGER PRIMARY KEY,    -- pořadí vložení = pořadí přechodů i ve stejné sekundě
    ts         INTEGER NOT NULL,
    ts_quality TEXT NOT NULL CHECK (ts_quality IN ('synced', 'derived', 'unsynced')),  -- 4.3, bez výchozí hodnoty
    run_id     INTEGER NOT NULL REFERENCES run (id),
    check_id   TEXT NOT NULL,
    from_state TEXT CHECK (from_state IN ('OK', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'UNKNOWN', 'N/A')),
                                       -- NULL = výchozí řádek kontroly v tomto běhu (není přechod)
    to_state   TEXT NOT NULL CHECK (to_state IN ('OK', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'UNKNOWN', 'N/A')),
                                       -- from_state = to_state: změnil se jen provozně významný údaj v details
    value      REAL,                   -- naměřená hodnota v okamžiku změny, je-li
    unit       TEXT,
    details    TEXT CHECK (details IS NULL OR json_valid(details)),  -- CheckResult.details jako JSON
    note       TEXT                    -- summary jen pro člověka, nikdy pro logiku
) STRICT;
CREATE INDEX check_change_ts ON check_change (ts);

-- Systémové události.
CREATE TABLE event (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,
    ts_quality TEXT NOT NULL CHECK (ts_quality IN ('synced', 'derived', 'unsynced')),
    run_id     INTEGER REFERENCES run (id),   -- NULL = mimo běh procesu (např. ruční obnova)
    kind       TEXT NOT NULL,                 -- 'process.start', 'history.gap', ...
    severity   TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    check_id   TEXT,
    message    TEXT NOT NULL,                 -- česky pro člověka
    data       TEXT CHECK (data IS NULL OR json_valid(data))
) STRICT;
CREATE INDEX event_ts ON event (ts);
"""

# Tabulky a indexy, které schéma v1 vytváří (pro srozumitelnou chybu i testy).
SCHEMA_TABLES = ("meta", "schema_migration", "run", "metric", "sample", "agg_5m", "agg_1h",
                 "aggregation_state", "check_change", "event")
SCHEMA_INDEXES = ("check_change_ts", "event_ts")


class HistoryError(Exception):
    """Databázi historie nelze vytvořit nebo otevřít; nic se přitom nezměnilo.

    `reason` je krátký kód pro program: "sqlite" (nevhodná verze), "path"
    (cesta nebo adresář), "exists" (soubor by se přepsal), "foreign" (není
    databáze NexusSTAT), "schema" (jiná nebo novější verze schématu), "role",
    "host", "locked" (zapisuje jiný proces), "corrupt", "create" (vytvoření
    selhalo), "settings" (nastavení spojení se nepodařilo), "alias" (druhé
    jméno databáze po přerušeném vytvoření má vlastní -wal, -shm nebo -journal),
    "run" (běh nezahájený, už zahájený nebo ukončený), "batch" (dávka mimo
    pořadí nebo nezapsaná předchozí), "write" (transakce zápisu selhala a byla
    vrácena) a "conflict" (hotová agregace už existuje; nic se nepřepsalo a
    agregace je do konce běhu zastavená).

    `sqlite_error` je jméno chyby SQLite (např. "SQLITE_BUSY", "SQLITE_FULL"),
    pokud ji způsobila knihovna; vlákno zapisovače podle něj rozliší přechodnou
    chybu od ztráty důvěryhodnosti databáze (docs/HISTORY.md 5.3).
    """

    def __init__(self, reason, message, sqlite_error=None):
        super().__init__(message)
        self.reason = reason
        self.sqlite_error = sqlite_error


def _sqlite_name(error):
    return getattr(error, "sqlite_errorname", None)


def check_sqlite(version=sqlite3.sqlite_version_info):
    """Ověří, že knihovna SQLite umí schéma v1 (STRICT a json_valid)."""
    if tuple(version) < MIN_SQLITE_VERSION:
        needed = ".".join(map(str, MIN_SQLITE_VERSION))
        found = ".".join(map(str, version))
        raise HistoryError("sqlite", f"SQLite {found} je starší než potřebná {needed}")


def local_host_id(machine_id_path=DEFAULT_MACHINE_ID_PATH):
    """Otisk tohoto stroje pro meta.host_id; samotné machine-id se neukládá."""
    try:
        machine_id = Path(machine_id_path).read_text(encoding="ascii").strip()
    except (OSError, ValueError) as error:
        raise HistoryError("host", f"nelze přečíst {machine_id_path}: {error}") from None
    if not MACHINE_ID_PATTERN.fullmatch(machine_id):
        raise HistoryError("host", f"{machine_id_path} neobsahuje platné machine-id")
    return hashlib.sha256(machine_id.encode("ascii")).hexdigest()[:16]


def lock_path(path):
    """Soubor zámku jediného zapisovače: history.sqlite3 → history.lock."""
    path = Path(path)
    lock = path.with_suffix(".lock")
    if lock == path:
        raise HistoryError("path", f"databáze nesmí mít příponu .lock: {path}")
    return lock


def companion_paths(path):
    return [Path(f"{path}{suffix}") for suffix in COMPANION_SUFFIXES]


def _publication_name(path):
    # Dočasné jméno při vytváření: .history.sqlite3.<náhodné>.creating
    return f".{Path(path).name}.", ".creating"


def _fsync(path):
    """Zapíše obsah souboru nebo položky adresáře na disk (Linux: i pro adresář)."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _check_role(role):
    if role not in ROLES:
        raise HistoryError("role", f"neznámá role databáze {role!r}")
    if role not in ALLOWED_ROLES:
        raise HistoryError("role", f"role {role!r} se v tomto kroku nepoužívá (jen test a development)")


def _check_host_id(host_id):
    if not isinstance(host_id, str) or not HOST_ID_PATTERN.fullmatch(host_id):
        raise HistoryError("host", f"neplatný otisk stroje {host_id!r} (16 malých šestnáctkových znaků)")


@functools.lru_cache(maxsize=1)
def expected_schema():
    """Otisk schématu v1: řádky sqlite_schema z databáze vytvořené v paměti."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA_SQL)
        return _schema_rows(conn)
    finally:
        conn.close()


def _schema_rows(conn):
    # Vnitřní objekty SQLite (sqlite_autoindex_*, sqlite_stat1 po PRAGMA optimize)
    # nejsou součástí návrhu a mohou přibýt, proto se neporovnávají.
    return tuple(conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY type, name"))


def _check_header(path):
    """Identita z hlavičky souboru bez SQLite, takže cizí soubor zůstane nedotčený.

    SQLite by i při otevření jen pro čtení vytvořilo vedle databáze ve WAL
    soubory -wal a -shm; cizí soubor se proto odmítne dřív, než ho otevře.
    """
    try:
        with open(path, "rb") as file:
            header = file.read(HEADER_SIZE)
    except FileNotFoundError:
        raise HistoryError("path", f"databáze {path} neexistuje") from None
    except OSError as error:
        raise HistoryError("path", f"databázi {path} nelze číst: {error}") from None
    if len(header) < HEADER_SIZE or not header.startswith(SQLITE_MAGIC):
        raise HistoryError("foreign", f"{path} není databáze SQLite")
    if int.from_bytes(header[68:72], "big") != APPLICATION_ID:
        raise HistoryError("foreign", f"{path} je databáze SQLite, ale ne NexusSTAT")
    _check_version(int.from_bytes(header[60:64], "big"), path)


def _check_version(version, path):
    if version > SCHEMA_VERSION:
        raise HistoryError("schema", f"{path} má novější schéma {version}, tento kód zná {SCHEMA_VERSION}; "
                                     "automatická migrace neexistuje")
    if version != SCHEMA_VERSION:
        raise HistoryError("schema", f"{path} má nepodporované schéma {version}")


def _verify(conn, path, role=None, host_id=None):
    """Úplné ověření na otevřeném spojení (hlavička, schéma, meta)."""
    if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise HistoryError("foreign", f"{path} není databáze NexusSTAT")
    _check_version(conn.execute("PRAGMA user_version").fetchone()[0], path)
    if _schema_rows(conn) != expected_schema():
        raise HistoryError("schema", f"schéma {path} se liší od schématu v{SCHEMA_VERSION}")
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    if not {"role", "host_id", "created_utc"} <= meta.keys():
        raise HistoryError("schema", f"{path} nemá úplná metadata (role, host_id, created_utc)")
    if role is not None and meta["role"] != role:
        raise HistoryError("role", f"{path} má roli {meta['role']!r}, očekávaná {role!r}")
    if host_id is not None and meta["host_id"] != host_id:
        raise HistoryError("host", f"{path} patří jinému stroji (host_id {meta['host_id']})")


def _uri(path, mode):
    # URI s mode=rw nebo mode=ro: SQLite soubor nikdy nevytvoří. as_uri() kóduje
    # zvláštní znaky v cestě.
    return f"{Path(path).resolve().as_uri()}?mode={mode}"


def create_database(path, role, host_id, now_utc=None):
    """Vytvoří novou databázi schématu v1 na výslovně zadané cestě.

    Adresář musí existovat (nevytváří se). Soubor ani jeho -wal, -shm a
    -journal nesmí existovat. Databáze vznikne v dočasném souboru vedle cíle
    a zveřejní se pevným odkazem os.link(), který existující soubor nikdy
    nepřepíše. Při chybě se smažou jen vlastní dočasné soubory.
    `now_utc` (celé Unixové sekundy) slouží testům; jinak skutečný čas.
    """
    check_sqlite()
    _check_role(role)
    _check_host_id(host_id)
    path = Path(path)
    created = int(time.time()) if now_utc is None else now_utc
    if isinstance(created, bool) or not isinstance(created, int) or created <= 0:
        raise HistoryError("create", f"neplatný čas vytvoření {now_utc!r}")
    if not path.parent.is_dir():
        raise HistoryError("path", f"adresář {path.parent} neexistuje; vytvořit ho musí správce")
    for existing in [path, *companion_paths(path)]:
        if os.path.lexists(existing):
            raise HistoryError("exists", f"{existing} už existuje a nepřepíše se")

    prefix, suffix = _publication_name(path)
    descriptor, temporary = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        _build(temporary, role, host_id, created)
        # Zveřejní se jen samostatně použitelný soubor: po zavření spojení nesmí
        # zůstat -wal ani -shm (jejich obsah by v cíli chyběl) a soubor musí projít
        # kontrolou bez pomocných souborů.
        if any(os.path.lexists(companion) for companion in companion_paths(temporary)):
            raise HistoryError("create", f"po vytvoření zůstaly pomocné soubory {temporary}; nezveřejněno")
        _check_standalone(temporary, role, host_id)
        _fsync(temporary)
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise HistoryError("exists", f"{path} mezitím vznikl a nepřepíše se") from None
        except OSError as error:
            raise HistoryError("create", f"databázi {path} nelze zveřejnit: {error}") from None
        # Nové jméno v adresáři přežije výpadek napájení až po synchronizaci adresáře.
        _fsync(path.parent)
    finally:
        # Jen vlastní dočasné soubory. Pád přesně mezi os.link a tímto úklidem nechá
        # druhé jméno téhož souboru; odstraní ho až open_writer (_remove_publication_alias).
        for own in [temporary, *companion_paths(temporary)]:
            own.unlink(missing_ok=True)


def _build(path, role, host_id, created):
    """Schéma a výchozí data v jedné transakci, pak trvalý režim WAL."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute(f"PRAGMA page_size = {PAGE_SIZE}")  # před první tabulkou
        conn.execute("PRAGMA synchronous = FULL")  # nezávisle na výchozím nastavení sestavení
        conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL)
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                         [("role", role), ("host_id", host_id), ("created_utc", str(created))])
        conn.execute("INSERT INTO schema_migration (version, applied_utc, description) VALUES (?, ?, ?)",
                     (SCHEMA_VERSION, created, "schéma v1 (M2)"))
        hour = created - created % 3600
        conn.executemany("INSERT INTO aggregation_state (level, done_until) VALUES (?, ?)",
                         [("5m", hour), ("1h", hour)])
        conn.execute("COMMIT")
        if conn.execute(f"PRAGMA journal_mode = {JOURNAL_MODE}").fetchone()[0] != JOURNAL_MODE:
            raise HistoryError("create", f"databázi {path} nelze přepnout do režimu WAL")
        _verify(conn, path, role, host_id)
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise HistoryError("create", f"nově vytvořená databáze {path} neprošla integrity_check")
    except sqlite3.Error as error:
        raise HistoryError("create", f"databázi nelze vytvořit: {error}") from None
    finally:
        # Zavření posledního spojení provede checkpoint a smaže -wal a -shm.
        conn.close()


def _check_standalone(path, role, host_id):
    """Hlavní soubor sám (bez -wal a -shm) je úplná a platná databáze.

    immutable=1: SQLite soubor jen čte, nezamyká a nevytváří pomocné soubory.
    """
    try:
        conn = sqlite3.connect(f"{_uri(path, 'ro')}&immutable=1", uri=True)
    except sqlite3.Error as error:
        raise HistoryError("create", f"vytvořený soubor {path} nelze otevřít: {error}") from None
    try:
        _verify(conn, path, role, host_id)
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise HistoryError("create", f"vytvořený soubor {path} neprošel integrity_check")
    except sqlite3.Error as error:
        raise HistoryError("create", f"vytvořený soubor {path} není samostatně použitelný: {error}") from None
    finally:
        conn.close()


def _remove_publication_alias(path):
    """Odstraní druhé jméno databáze, které nechal pád mezi os.link a úklidem.

    Smaže se jen položka s dočasným jménem, která je prokazatelně tentýž soubor
    (stejné zařízení a inode) a nemá vlastní pomocné soubory; data se tím
    nemění. Pokud vlastní -wal, -shm nebo -journal má, někdo databázi otevřel
    pod druhým jménem a rozhodnout musí správce. Dočasné soubory s jiným inode
    (pád před zveřejněním, nebo právě probíhající vytváření jiného procesu)
    se nemažou.
    """
    target = os.stat(path)
    prefix, suffix = _publication_name(path)
    for name in os.listdir(path.parent):
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        alias = path.parent / name
        try:
            info = os.lstat(alias)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (target.st_dev, target.st_ino):
            continue
        if any(os.path.lexists(companion) for companion in companion_paths(alias)):
            raise HistoryError("alias", f"{alias} je druhé jméno databáze {path} s vlastními pomocnými "
                                        "soubory; nutný zásah správce")
        alias.unlink()


def _acquire_lock(path):
    """Výhradní zámek zapisovače; druhý zapisovač (i v jiném procesu) dostane chybu hned."""
    lock = lock_path(path)
    try:
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as error:
        raise HistoryError("path", f"zámek {lock} nelze otevřít: {error}") from None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise HistoryError("locked", f"databázi {path} už zapisuje jiný proces") from None
    except OSError as error:
        os.close(descriptor)
        raise HistoryError("path", f"zámek {lock} nelze získat: {error}") from None
    return descriptor


def open_writer(path, role, host_id, quick_check=True):
    """Otevře existující databázi pro zápis jako jediný zapisovač.

    Pořadí chrání cizí soubory i data: hlavička bez SQLite, zámek, odstranění
    druhého jména po přerušeném vytvoření, úplné ověření na spojení jen pro
    čtení, otevření pro zápis bez možnosti vytvořit soubor (do konce kontrol
    bez checkpointu při zavření), teprve pak nastavení spojení. Odmítnutí
    tak hlavní soubor nikdy nezmění. Platný -wal po nečistém ukončení se
    nemaže: SQLite ho při otevření obnoví.

    Odpovědnosti kontrol: quick_check (rychlá kontrola struktury) při každém
    otevření; cizí klíče hlídá při zápisu PRAGMA foreign_keys = ON. Úplná
    integrity_check a foreign_key_check patří k ověření denního snímku a testu
    obnovy (docs/HISTORY.md 9.2, 9.5), ne k otevření.
    """
    check_sqlite()
    _check_role(role)
    _check_host_id(host_id)
    path = Path(path)
    _check_header(path)
    lock = _acquire_lock(path)
    try:
        _remove_publication_alias(path)
        # Identita nejdřív na spojení jen pro čtení: odmítnutý soubor tak nikdy
        # neotevře zapisující spojení, jehož zavření by provedlo checkpoint.
        _verify_read_only(path, role, host_id)
        try:
            conn = sqlite3.connect(_uri(path, "rw"), uri=True, isolation_level=None)
        except sqlite3.Error as error:
            raise HistoryError("path", f"databázi {path} nelze otevřít pro zápis: {error}",
                               _sqlite_name(error)) from None
        try:
            # Do úspěšného ověření ani zavření nezapíše checkpoint (soubor se nezmění).
            _set_no_checkpoint_on_close(conn, True)
            _verify(conn, path, role, host_id)  # znovu: soubor se mohl mezitím vyměnit
            _configure_writer(conn, path)
            if quick_check and conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise HistoryError("corrupt", f"databáze {path} neprošla quick_check")
            _set_no_checkpoint_on_close(conn, False)
        except sqlite3.DatabaseError as error:
            conn.close()
            raise HistoryError("corrupt", f"databázi {path} nelze použít: {error}", _sqlite_name(error)) from None
        except BaseException:
            conn.close()
            raise
    except BaseException:
        os.close(lock)
        raise
    return HistoryDatabase(path, conn, readonly=False, lock=lock)


def _set_no_checkpoint_on_close(conn, enabled):
    # Python ≥ 3.12; na starším zůstává ochrana jen v pořadí kontrol (ověření jen pro čtení).
    if hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"):
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, enabled)


def _connect_reader(path):
    """Spojení, které nemůže zapisovat ani provést checkpoint (ani při zavření)."""
    try:
        conn = sqlite3.connect(_uri(path, "ro"), uri=True, isolation_level=None)
    except sqlite3.Error as error:
        raise HistoryError("path", f"databázi {path} nelze otevřít pro čtení: {error}") from None
    try:
        _set_no_checkpoint_on_close(conn, True)
        conn.execute("PRAGMA query_only = ON")
        conn.execute(f"PRAGMA busy_timeout = {READER_BUSY_TIMEOUT_MS}")
    except BaseException:
        conn.close()
        raise
    return conn


def _verify_read_only(path, role, host_id):
    conn = _connect_reader(path)
    try:
        _verify(conn, path, role, host_id)
    except sqlite3.DatabaseError as error:
        raise HistoryError("corrupt", f"databázi {path} nelze ověřit: {error}") from None
    finally:
        conn.close()


def _configure_writer(conn, path):
    for name, value, expected in CONNECTION_SETTINGS:
        conn.execute(f"PRAGMA {name} = {value}")
        actual = conn.execute(f"PRAGMA {name}").fetchone()[0]
        if actual != expected:
            raise HistoryError("settings", f"PRAGMA {name} má na {path} hodnotu {actual!r}, očekáváno {expected!r}")
    if conn.execute(f"PRAGMA journal_mode = {JOURNAL_MODE}").fetchone()[0] != JOURNAL_MODE:
        raise HistoryError("settings", f"databáze {path} není v režimu WAL")


def open_reader(path, role=None, host_id=None):
    """Otevře databázi jen pro čtení (budoucí web a výpisy), bez zámku zapisovače.

    Spojení nezapisuje (mode=ro, query_only) a neprovede checkpoint ani při
    zavření jako poslední spojení (docs/HISTORY.md 5.8). SQLite přitom smí
    vytvořit prázdné -wal a -shm, pokud ještě neexistují; samotný soubor
    databáze se nemění. Role a otisk stroje se ověří, jen když jsou zadané.
    """
    check_sqlite()
    if role is not None and role not in ROLES:
        raise HistoryError("role", f"neznámá role databáze {role!r}")
    if host_id is not None:
        _check_host_id(host_id)
    path = Path(path)
    _check_header(path)
    conn = _connect_reader(path)
    try:
        _verify(conn, path, role, host_id)
    except sqlite3.DatabaseError as error:
        conn.close()
        raise HistoryError("corrupt", f"databázi {path} nelze číst: {error}") from None
    except BaseException:
        conn.close()
        raise
    return HistoryDatabase(path, conn, readonly=True)


class HistoryDatabase:
    """Otevřená databáze historie: spojení, druh přístupu a bezpečné zavření."""

    def __init__(self, path, connection, readonly, lock=None):
        self.path = Path(path)
        self.connection = connection
        self.readonly = readonly
        self._lock = lock

    def close(self):
        """Zavře spojení (zapisovač po PRAGMA optimize) a uvolní zámek; opakované volání nic nedělá."""
        if self.connection is None:
            return
        try:
            if not self.readonly:
                self.connection.execute("PRAGMA optimize")
        finally:
            self.connection.close()
            self.connection = None
            if self._lock is not None:
                os.close(self._lock)
                self._lock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


# --- M3.1b: zápis vzorků, změn stavu a událostí ------------------------------------

# SQL z docs/HISTORY.md: doslovné kopie bloků insert-sample, sample-conflict,
# batch-guard, run-last-write, close-unclean-runs, high-water, insert-change,
# progress-5m, aggregate-5m, progress-1h, rollup-1h a bloky retence (oldest-*,
# retention-*, retention-check-*); shodu hlídá test.
INSERT_SAMPLE_SQL = """\
INSERT INTO sample (metric_id, ts, value) VALUES (:metric_id, :ts, :value)
ON CONFLICT DO NOTHING;
"""
SAMPLE_CONFLICT_SQL = """\
-- Pro vzorek, který INSERT nevložil: same_value 1 = duplicita, 0 = konflikt (ohlásit).
SELECT value IS :value AS same_value, value AS stored_value
FROM sample
WHERE ts = :ts AND metric_id = :metric_id;
"""
BATCH_GUARD_SQL = """\
-- První příkaz transakce dávky. 0 změněných řádků = dávka už je zapsaná (opakování
-- po nejistém commitu) nebo porušené pořadí: ROLLBACK, z dávky se nic nezapíše.
UPDATE run SET last_batch = :batch
WHERE id = :run_id AND last_batch = :batch - 1;
"""
RUN_LAST_WRITE_SQL = """\
-- Poslední příkaz dávky před COMMIT: :max_ts = nejnovější vzorek, který dávka skutečně
-- vložila (rowcount 1). Duplicita, konflikt ani odmítnutý vzorek horní mez neposunou;
-- NULL = beze změny.
UPDATE run SET last_write_utc = coalesce(max(last_write_utc, :max_ts), last_write_utc, :max_ts)
WHERE id = :run_id;
"""
CLOSE_UNCLEAN_RUNS_SQL = """\
UPDATE run SET stopped_utc = coalesce(last_write_utc, started_utc), clean_stop = 0
WHERE stopped_utc IS NULL;
"""
HIGH_WATER_SQL = """\
SELECT max(last_write_utc) FROM run;
"""
INSERT_CHANGE_SQL = """\
INSERT INTO check_change (ts, ts_quality, run_id, check_id, from_state, to_state, value, unit, details, note)
VALUES (:ts, :ts_quality, :run_id, :check_id, :from_state, :to_state, :value, :unit, :details, :note);
"""
PROGRESS_5M_SQL = """\
-- První příkaz transakce agregace; 0 změněných řádků = interval už je hotový: ROLLBACK.
UPDATE aggregation_state SET done_until = :end
WHERE level = '5m' AND done_until = :start;
"""
AGGREGATE_5M_SQL = """\
-- :start a :end jsou násobky 300; okna končící nejpozději v :end. Bez ON CONFLICT:
-- existující okno je rozpor, transakce selže a nic se nepřepíše.
INSERT INTO agg_5m (metric_id, bucket, n_samples, n_valid, min_value, max_value, sum_value)
SELECT metric_id, ts - ts % 300, count(*), count(value), min(value), max(value), sum(value)
FROM sample
WHERE metric_id IN (SELECT id FROM metric) AND ts >= :start AND ts < :end
GROUP BY metric_id, ts - ts % 300;
"""
PROGRESS_1H_SQL = """\
UPDATE aggregation_state SET done_until = :end
WHERE level = '1h' AND done_until = :start
  AND :end <= (SELECT done_until FROM aggregation_state WHERE level = '5m');
"""
ROLLUP_1H_SQL = """\
-- :start a :end jsou násobky 3600.
INSERT INTO agg_1h (metric_id, bucket, n_samples, n_valid, min_value, max_value, sum_value)
SELECT metric_id, bucket - bucket % 3600, sum(n_samples), sum(n_valid),
       min(min_value), max(max_value), sum(sum_value)
FROM agg_5m
WHERE metric_id IN (SELECT id FROM metric) AND bucket >= :start AND bucket < :end
GROUP BY metric_id, bucket - bucket % 3600;
"""
INSERT_EVENT_SQL = """\
INSERT INTO event (ts, ts_quality, run_id, kind, severity, check_id, message, data)
VALUES (:ts, :ts_quality, :run_id, :kind, :severity, :check_id, :message, :data);
"""
INSERT_METRIC_SQL = ("INSERT INTO metric (check_id, subject, unit, created_utc) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (check_id, subject) DO NOTHING")
# Kvalita času startu běhu (run sám kvalitu nemá, nese ji událost process.start).
START_QUALITY_SQL = "SELECT ts_quality FROM event WHERE run_id = ? AND kind = 'process.start' ORDER BY id LIMIT 1"
DONE_UNTIL_SQL = "SELECT done_until FROM aggregation_state WHERE level = ?"
# Úroveň agregace -> (posun done_until, vložení oken, tabulka).
AGGREGATION_SQL = {"5m": (PROGRESS_5M_SQL, AGGREGATE_5M_SQL, "agg_5m"), "1h": (PROGRESS_1H_SQL, ROLLUP_1H_SQL, "agg_1h")}

OLDEST_SAMPLE_SQL = """\
SELECT ts FROM sample ORDER BY ts LIMIT 1;
"""
RETENTION_SAMPLE_SQL = """\
DELETE FROM sample
WHERE metric_id IN (SELECT id FROM metric) AND ts < :cutoff;
"""
OLDEST_5M_SQL = """\
SELECT min((SELECT min(bucket) FROM agg_5m WHERE agg_5m.metric_id = metric.id)) FROM metric;
"""
RETENTION_5M_SQL = """\
DELETE FROM agg_5m
WHERE metric_id IN (SELECT id FROM metric) AND bucket < :cutoff;
"""
OLDEST_1H_SQL = """\
SELECT min((SELECT min(bucket) FROM agg_1h WHERE agg_1h.metric_id = metric.id)) FROM metric;
"""
RETENTION_1H_SQL = """\
DELETE FROM agg_1h
WHERE metric_id IN (SELECT id FROM metric) AND bucket < :cutoff;
"""
OLDEST_CHANGE_SQL = """\
SELECT ts FROM check_change ORDER BY ts LIMIT 1;
"""
RETENTION_CHANGE_SQL = """\
DELETE FROM check_change
WHERE ts < :cutoff;
"""
OLDEST_EVENT_SQL = """\
SELECT ts FROM event ORDER BY ts LIMIT 1;
"""
RETENTION_EVENT_SQL = """\
DELETE FROM event
WHERE ts < :cutoff;
"""
RETENTION_CHECK_SAMPLE_SQL = """\
-- Invariant před mazáním vzorků: okna se vzorky před :cutoff bez 5min agregace, která
-- zahrnuje aspoň tolik vzorků. Výsledek 0 = vzorky jsou v agregacích, smí se mazat.
SELECT count(*) FROM (
    SELECT metric_id, ts - ts % 300 AS bucket, count(*) AS n
    FROM sample
    WHERE metric_id IN (SELECT id FROM metric) AND ts < :cutoff
    GROUP BY metric_id, ts - ts % 300
) raw
LEFT JOIN agg_5m a ON a.metric_id = raw.metric_id AND a.bucket = raw.bucket
WHERE a.n_samples IS NULL OR a.n_samples < raw.n;
"""
RETENTION_CHECK_5M_SQL = """\
-- Invariant před mazáním 5min agregací: hodiny s okny před :cutoff bez hodinové agregace,
-- která zahrnuje aspoň tolik vzorků. Výsledek 0 = okna jsou v agg_1h, smí se mazat.
SELECT count(*) FROM (
    SELECT metric_id, bucket - bucket % 3600 AS hour, sum(n_samples) AS n
    FROM agg_5m
    WHERE metric_id IN (SELECT id FROM metric) AND bucket < :cutoff
    GROUP BY metric_id, bucket - bucket % 3600
) five
LEFT JOIN agg_1h h ON h.metric_id = five.metric_id AND h.bucket = five.hour
WHERE h.n_samples IS NULL OR h.n_samples < five.n;
"""
# Tabulka -> (nejstarší řádek, mazání, invariant agregace nebo None). Počet ke kontrole
# rowcount má stejnou podmínku WHERE jako mazání (odvozený přímo z jeho textu).
RETENTION_SQL = {
    "sample": (OLDEST_SAMPLE_SQL, RETENTION_SAMPLE_SQL, RETENTION_CHECK_SAMPLE_SQL),
    "agg_5m": (OLDEST_5M_SQL, RETENTION_5M_SQL, RETENTION_CHECK_5M_SQL),
    "agg_1h": (OLDEST_1H_SQL, RETENTION_1H_SQL, None),
    "check_change": (OLDEST_CHANGE_SQL, RETENTION_CHANGE_SQL, None),
    "event": (OLDEST_EVENT_SQL, RETENTION_EVENT_SQL, None),
}
RETENTION_COUNT_SQL = {table: "SELECT count(*) FROM" + sql[len("DELETE FROM"):] for table, (_, sql, _) in RETENTION_SQL.items()}
RETENTION_TABLES = tuple(RETENTION_SQL)
# Začátek zachované historie (trvale v meta, nikdy neklesá): řádky před ním retence smazala,
# od něj nesmazala nic. Není to záruka souvislé řady: mezery v zápisu zůstávají mezerami.
RETAINED_SINCE_KEY = "retained_since.{table}"
# Skok v posledních 24 h (i událost „z budoucnosti“ po skoku zpět); po přesně 24 h pauza končí.
CLOCK_JUMP_SINCE_SQL = "SELECT count(*) FROM event WHERE ts > ? AND kind = 'clock.jump'"
NEWEST_DATA_SQL = ("SELECT max(coalesce((SELECT max(last_write_utc) FROM run), 0), "
                   "coalesce((SELECT max(ts) FROM check_change), 0), coalesce((SELECT max(ts) FROM event), 0))")

TIME_QUALITIES = ("synced", "derived", "unsynced")
SEVERITIES = ("INFO", "WARNING", "ERROR", "CRITICAL")
# Čas před 2026-01-01 UTC se nikdy nepovažuje za synchronizovaný (HISTORY.md 4.3).
MIN_TRUSTED_UTC = 1767225600
# Změna posunu UTC − monotonic mezi měřeními nad touto mez = skok času (HISTORY.md 4.3).
CLOCK_JUMP_SECONDS = 2
# Souhrn odmítnutých vzorků history.rejected nejvýš jednou za hodinu (HISTORY.md 5.4).
LOSS_SUMMARY_SECONDS = 3600
# Agregace (HISTORY.md 6.1): 5min okno, hodina a rezerva po konci okna před uzavřením.
WINDOW_SECONDS = 300
HOUR_SECONDS = 3600
AGGREGATION_GRACE_SECONDS = 120
# Retence (HISTORY.md 7): schválené nejkratší doby uchování; parametry API je nemění.
DAY_SECONDS = 86400
SAMPLE_KEEP_DAYS = 30
AGG_5M_KEEP_DAYS = 365
AGG_1H_KEEP_YEARS = 10
CHANGE_KEEP_DAYS = 730  # check_change a event (R4)
RETENTION_MAX_TRANSACTIONS = 24  # nejvýš den dat (po hodině) za jeden běh údržby
RETENTION_STEP_SECONDS = 3600  # jedna transakce smaže nejvýš hodinu historie
RETENTION_STABLE_CLOCK_SECONDS = 86400  # po skoku hodin (nebo bez doložení) se nemaže 24 h


@dataclass(frozen=True)
class SeriesSpec:
    """Definice číselné řady: jednotka a klíč v details, který určuje objekt řady."""

    unit: str
    subject_key: str | None = None


# Pevný katalog číselných řad (HISTORY.md 4.1). Jiné id nikdy nevytvoří řadu;
# RX a TX mají objekt = sledované rozhraní, takže se různá rozhraní nemíchají.
SERIES = {
    CPU_USAGE_ID: SeriesSpec(PERCENT),
    CPU_TEMP_ID: SeriesSpec(CELSIUS),
    MEMORY_ID: SeriesSpec(PERCENT),
    DISK_ROOT_ID: SeriesSpec(PERCENT),
    RX_RATE_ID: SeriesSpec(BYTES_PER_SECOND, INTERFACE),
    TX_RATE_ID: SeriesSpec(BYTES_PER_SECOND, INTERFACE),
}


def _utc_seconds(value):
    """Celé Unixové sekundy z datetime v UTC (dolů na celou sekundu, HISTORY.md 3.4)."""
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"čas musí být datetime v UTC, ne {value!r}")
    return math.floor(value.timestamp())


def _check_quality(ts_quality):
    if ts_quality not in TIME_QUALITIES:
        raise ValueError(f"neznámá kvalita času {ts_quality!r}")


def _trusted_quality(ts, ts_quality):
    """Čas před MIN_TRUSTED_UTC je vždy unsynced, ať volající tvrdí cokoli (HISTORY.md 4.3)."""
    return ts_quality if ts >= MIN_TRUSTED_UTC else "unsynced"


def _json(data):
    # Seřazené klíče: stejné údaje dávají vždy stejný text (porovnání změn).
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class Event:
    """Systémová událost pro tabulku event. Kvalita času je povinná (bez výchozí hodnoty)."""

    ts: int
    ts_quality: str
    kind: str
    severity: str
    message: str
    check_id: str | None = None
    data: Mapping | None = None

    def __post_init__(self):
        if isinstance(self.ts, bool) or not isinstance(self.ts, int) or self.ts <= 0:
            raise ValueError(f"čas události musí být kladné celé číslo, ne {self.ts!r}")
        _check_quality(self.ts_quality)
        if _trusted_quality(self.ts, self.ts_quality) != self.ts_quality:
            raise ValueError(f"čas {self.ts} je před MIN_TRUSTED_UTC; kvalita smí být jen unsynced")
        if self.severity not in SEVERITIES:
            raise ValueError(f"neznámá závažnost události {self.severity!r}")
        for name in ("kind", "message"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"událost musí mít neprázdné {name}")
        if self.check_id is not None and (not isinstance(self.check_id, str) or not self.check_id):
            raise ValueError("check_id události musí být neprázdný text")
        if self.data is not None:
            _json(dict(self.data))  # odmítne údaje, které nejdou uložit jako JSON

    def row(self, run_id):
        return {"ts": self.ts, "ts_quality": self.ts_quality, "run_id": run_id, "kind": self.kind,
                "severity": self.severity, "check_id": self.check_id, "message": self.message,
                "data": None if self.data is None else _json(dict(self.data))}


@dataclass(frozen=True)
class Change:
    """Jeden řádek check_change: skutečná změna stavu nebo provozně významných údajů."""

    ts: int
    ts_quality: str
    check_id: str
    from_state: str | None
    to_state: str
    value: float | None
    unit: str | None
    details: str | None
    note: str | None

    def row(self, run_id):
        return {"ts": self.ts, "ts_quality": self.ts_quality, "run_id": run_id, "check_id": self.check_id,
                "from_state": self.from_state, "to_state": self.to_state, "value": self.value,
                "unit": self.unit, "details": self.details, "note": self.note}


@dataclass(frozen=True)
class PendingSample:
    """Přijatý vzorek čekající v dávce na zápis; value None = UNKNOWN."""

    check_id: str
    subject: str
    unit: str
    ts: int
    ts_quality: str
    value: float | None


@dataclass(frozen=True)
class SampleConflict:
    """V databázi už je pro (řada, čas) jiná hodnota; uložená zůstává."""

    check_id: str
    subject: str
    ts: int
    stored: float | None
    rejected: float | None


@dataclass
class Batch:
    """Jedna transakční dávka; číslo je pevné od vytvoření (ochrana run.last_batch).

    Prvním pokusem o zápis se dávka zapečetí: opakuje se jen beze změny, jinak
    by opakování po nejistém commitu mohlo tiše zahodit později přidané položky.
    """

    number: int
    samples: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    events: list = field(default_factory=list)
    sealed_content: tuple | None = field(default=None, repr=False)  # obsah při zapečetění

    @property
    def sealed(self):
        return self.sealed_content is not None

    def _content(self):
        return (tuple(self.samples), tuple(self.changes), tuple(self.events))

    def seal(self):
        self.sealed_content = self._content()

    def changed_since_seal(self):
        return self.sealed and self.sealed_content != self._content()


@dataclass(frozen=True)
class BatchResult:
    number: int
    already_written: bool  # dávka byla zapsaná dřív (opakování po nejistém commitu); nic nového
    samples: int = 0  # nově uložené vzorky
    duplicates: int = 0  # vzorky už uložené se stejnou hodnotou
    conflicts: tuple = ()  # SampleConflict: jiná uložená hodnota, nic se nepřepsalo
    changes: int = 0
    events: int = 0


@dataclass(frozen=True)
class QueueState:
    """Kontrakt s frontou měření M3.3 pro uzavírání oken (HISTORY.md 6.1, podmínka 2).

    Zapisovač frontu nemá a nic nepředpokládá: volající stav vždy výslovně uvede.
    empty() = žádné nezapsané měření; waiting_since(ts) = nejstarší čekající
    měření má čas ts (celé Unixové sekundy UTC); unknown() = stav neznámý nebo
    měření čekají na synchronizaci hodin (čas nemají), pak se neuzavře nic.
    """

    known: bool
    oldest_utc: int | None = None

    def __post_init__(self):
        if not isinstance(self.known, bool):
            raise TypeError("known musí být bool")
        if self.oldest_utc is None:
            return
        if not self.known:
            raise ValueError("neznámý stav fronty nemá čas nejstaršího měření")
        if isinstance(self.oldest_utc, bool) or not isinstance(self.oldest_utc, int) or self.oldest_utc < MIN_TRUSTED_UTC:
            raise ValueError("čekající měření bez důvěryhodného času se vyjádří jako QueueState.unknown()")

    @classmethod
    def empty(cls):
        return cls(True)

    @classmethod
    def waiting_since(cls, oldest_utc):
        if oldest_utc is None:
            raise ValueError("prázdná fronta je QueueState.empty()")
        return cls(True, oldest_utc)

    @classmethod
    def unknown(cls):
        return cls(False)


@dataclass(frozen=True)
class AggregationResult:
    done_5m: int  # potvrzená hranice: 5min okna končící nejpozději zde jsou hotová
    done_1h: int
    windows_5m: int = 0  # nově vložené řádky agg_5m
    windows_1h: int = 0  # nově vložené řádky agg_1h
    blocked: str | None = None  # "time_unsynced" nebo "queue_unknown": nic se nezpracovalo
    more: bool = False  # zbývají uzavřená okna na další volání (postupné dohánění)


@dataclass(frozen=True)
class ClockState:
    """Kontrakt s M3.3 o čase pro nevratné mazání (HISTORY.md 7); zapisovač ho sám nezjišťuje.

    synced = zdroj synchronizace potvrzuje správný UTC čas právě teď. stable_s =
    doložená doba (sekundy monotónního času) souvislé synchronizace bez zjištěného
    skoku hodin; None = neznámo. Retence maže jen při synced a stable_s ≥ 24 h.
    Testy ho vytvářejí ručně: to je simulace, ne důkaz skutečné synchronizace.
    """

    synced: bool
    stable_s: int | None

    def __post_init__(self):
        if not isinstance(self.synced, bool):
            raise TypeError("synced musí být bool")
        if self.stable_s is not None:
            if isinstance(self.stable_s, bool) or not isinstance(self.stable_s, int) or self.stable_s < 0:
                raise ValueError("stable_s musí být nezáporné celé číslo nebo None")
            if not self.synced:
                raise ValueError("nesynchronizovaný čas nemá dobu stability")

    @classmethod
    def unknown(cls):
        return cls(False, None)

    @classmethod
    def synced_for(cls, stable_s):
        if stable_s is None:
            raise ValueError("neznámá doba stability je ClockState.unknown()")
        return cls(True, stable_s)


@dataclass(frozen=True)
class RetentionTable:
    """Plán retence jedné tabulky (výpočet bez zápisu)."""

    table: str
    time_cutoff: int  # now − schválená doba uchování (zarovnáno podle HISTORY.md 7)
    aggregation_until: int | None  # done_until potřebné úrovně (sample: 5m, agg_5m: 1h), jinak None
    cutoff: int  # mazalo by se jen ts/bucket < cutoff
    limited_by_aggregation: bool  # hranici drží nedokončená agregace, ne čas
    oldest: int | None  # nejstarší řádek tabulky
    candidates: int  # řádky pod hranicí
    invariant_ok: bool | None  # další krok má data v agregacích (None = bez kandidátů nebo bez invariantu)


@dataclass(frozen=True)
class RetentionPlan:
    now: int
    blocked: str | None  # time_unsynced, clock_unstable, clock_jump_recent, clock_behind_data
    done_5m: int
    done_1h: int
    tables: tuple  # RetentionTable v pořadí RETENTION_TABLES

    def table(self, name):
        return next(entry for entry in self.tables if entry.table == name)


@dataclass(frozen=True)
class RetentionResult:
    blocked: str | None  # proč se nesmazalo nic (čas); jinak None
    transactions: int  # potvrzené mazací transakce v tomto běhu
    deleted: Mapping  # tabulka -> skutečně smazané řádky (ověřený rowcount)
    stopped: Mapping  # tabulka -> důvod zastavení (aggregation_mismatch, aggregation_state_invalid)
    retained_since: Mapping  # tabulka -> trvalá hranice v meta po běhu (None = nic nesmazáno)
    verified_after_uncertain_commit: int = 0  # kroky potvrzené kontrolou stavu po chybě COMMIT
    more: bool = False  # limit transakcí došel dřív, než byla práce hotová


@dataclass(frozen=True)
class RunInfo:
    id: int
    started_utc: int
    stopped_utc: int | None
    clean_stop: int | None
    last_write_utc: int | None
    last_batch: int


def run_info(connection, run_id):
    """Evidence běhu z databáze (i přes spojení jen pro čtení), nebo None."""
    row = connection.execute("SELECT id, started_utc, stopped_utc, clean_stop, last_write_utc, last_batch "
                             "FROM run WHERE id = ?", (run_id,)).fetchone()
    return None if row is None else RunInfo(*row)


def high_water(connection):
    """Horní časová mez: nejnovější zapsaný vzorek všech běhů (HISTORY.md 4.2), nebo None."""
    return connection.execute(HIGH_WATER_SQL).fetchone()[0]


def _years_before(ts, years):
    """Kalendářní roky zpět v UTC; 29. února → 28. února (uchová se aspoň celých `years` let)."""
    moment = datetime.fromtimestamp(ts, timezone.utc)
    try:
        earlier = moment.replace(year=moment.year - years)
    except ValueError:
        earlier = moment.replace(year=moment.year - years, day=28)
    return math.floor(earlier.timestamp())


def _retention_bounds(table, now, done_5m, done_1h):
    """(časová hranice, hranice z agregace nebo None) podle HISTORY.md 7."""
    if table == "sample":
        return now - SAMPLE_KEEP_DAYS * DAY_SECONDS, done_5m
    if table == "agg_5m":
        return (now - AGG_5M_KEEP_DAYS * DAY_SECONDS) // HOUR_SECONDS * HOUR_SECONDS, done_1h
    if table == "agg_1h":
        # Celé hodiny: bucket před hranicí je celý starší než 10 let.
        return _years_before(now, AGG_1H_KEEP_YEARS) // HOUR_SECONDS * HOUR_SECONDS, None
    return now - CHANGE_KEEP_DAYS * DAY_SECONDS, None


def _retention_guard(conn, now, clock):
    """Důvod, proč se nesmí mazat (čas), nebo None. Při jakékoli nejistotě se nemaže."""
    if now < MIN_TRUSTED_UTC or not clock.synced:
        return "time_unsynced"
    if clock.stable_s is None or clock.stable_s < RETENTION_STABLE_CLOCK_SECONDS:
        return "clock_unstable"
    if conn.execute(CLOCK_JUMP_SINCE_SQL, (now - RETENTION_STABLE_CLOCK_SECONDS,)).fetchone()[0]:
        return "clock_jump_recent"  # i skok zjištěný zapisovačem v předchozím běhu
    if conn.execute(NEWEST_DATA_SQL).fetchone()[0] > now:
        return "clock_behind_data"  # zapsaná data jsou novější než „teď“: čas není jistý
    return None


def _oldest(conn, sql):
    # oldest-sample/-change/-event (ORDER BY … LIMIT 1) nad prázdnou tabulkou nevrátí žádný řádek.
    row = conn.execute(sql).fetchone()
    return None if row is None else row[0]


def _check_clock_state(now_utc, clock):
    if not isinstance(clock, ClockState):
        raise TypeError("stav hodin musí být ClockState (synced_for nebo unknown)")
    return _utc_seconds(now_utc)


def retained_since(connection):
    """Trvalé hranice retence z meta: tabulka -> začátek zachované historie, nebo None (nic nesmazáno)."""
    rows = dict(connection.execute("SELECT key, value FROM meta WHERE key LIKE 'retained_since.%'"))
    return {table: None if RETAINED_SINCE_KEY.format(table=table) not in rows
            else int(rows[RETAINED_SINCE_KEY.format(table=table)]) for table in RETENTION_TABLES}


def retention_plan(connection, now_utc, clock):
    """Plán retence bez zápisu (dry-run); funguje i nad spojením jen pro čtení.

    Vše se čte v jedné čtecí transakci ukončené ROLLBACK. Plán je jen informace a
    horní mez pro retain(): provedení každou podmínku ověří znovu ve své transakci.
    """
    now = _check_clock_state(now_utc, clock)
    started = not connection.in_transaction
    if started:
        connection.execute("BEGIN")
    try:
        blocked = _retention_guard(connection, now, clock)
        done_5m = connection.execute(DONE_UNTIL_SQL, ("5m",)).fetchone()[0]
        done_1h = connection.execute(DONE_UNTIL_SQL, ("1h",)).fetchone()[0]
        tables = []
        for table in RETENTION_TABLES:
            oldest_sql, _, check_sql = RETENTION_SQL[table]
            time_cutoff, bound = _retention_bounds(table, now, done_5m, done_1h)
            cutoff = time_cutoff if bound is None else min(time_cutoff, bound)
            oldest = _oldest(connection, oldest_sql)
            candidates = connection.execute(RETENTION_COUNT_SQL[table], {"cutoff": cutoff}).fetchone()[0]
            invariant = None
            if candidates and check_sql is not None:
                step_end = min(cutoff, oldest // RETENTION_STEP_SECONDS * RETENTION_STEP_SECONDS + RETENTION_STEP_SECONDS)
                invariant = connection.execute(check_sql, {"cutoff": step_end}).fetchone()[0] == 0
            tables.append(RetentionTable(table, time_cutoff, bound, cutoff, bound is not None and bound < time_cutoff,
                                         oldest, candidates, invariant))
    finally:
        if started:
            connection.execute("ROLLBACK")  # plán nikdy nic nezapíše
    return RetentionPlan(now, blocked, done_5m, done_1h, tuple(tables))


class ChangeTracker:
    """Pravidlo R4 (HISTORY.md 7.1): změna = jiný stav nebo jiné provozně významné údaje.

    Pamatuje si poslední (stav, details) každé kontroly v běhu. První výsledek
    kontroly je výchozí řádek s from_state None. Hodnota ani text summary se
    neporovnávají, takže opakované měření se stejným stavem a údaji nic
    nevytvoří. Nic se neslučuje: každá změna je jeden řádek, v pořadí volání.
    """

    def __init__(self):
        self._last = {}

    def observe(self, result, ts, ts_quality):
        details = _json(dict(result.details)) if result.details else None
        key = (result.state.value, details)
        previous = self._last.get(result.id)
        if previous == key:
            return None
        self._last[result.id] = key
        measurement = result.measurement
        return Change(ts, ts_quality, result.id, None if previous is None else previous[0], result.state.value,
                      None if measurement is None else float(measurement.value),
                      None if measurement is None else measurement.unit, details, result.summary)


class SampleGate:
    """Pravidla přijetí číselného vzorku v jednom běhu (HISTORY.md 4.1).

    accept() vrací None (přijmout), "duplicate" (totéž měření znovu, není to
    ztráta) nebo důvod odmítnutí: before_high_water, same_second,
    clock_backward, late. Totožnost měření je expires_monotonic výsledku,
    stejně jako v AlarmEvaluator. Nic se neposouvá ani nepřepisuje.
    """

    def __init__(self, high_water, done_until):
        self.high_water = high_water  # max(run.last_write_utc) předchozích běhů
        self.done_until = done_until  # aggregation_state '5m': starší okna jsou agregovaná
        self._last = {}  # řada -> (ts, totožnost měření) posledního přijatého vzorku

    def accept(self, series, ts, cycle):
        last = self._last.get(series)
        if last == (ts, cycle):
            return "duplicate"
        if self.high_water is not None and ts <= self.high_water:
            return "before_high_water"
        if last is not None and ts == last[0]:
            return "same_second"
        if last is not None and ts < last[0]:
            return "clock_backward"
        if ts < self.done_until:
            return "late"
        self._last[series] = (ts, cycle)
        return None


class HistoryRecorder:
    """Jediný zapisovač historie pro jeden běh procesu (synchronně, bez vlákna).

    Použití: start_run(), pak opakovaně begin_batch() → add_results() /
    add_event() → write(), nakonec finish_run(). Každá dávka je jedna
    transakce s ochranou run.last_batch; při chybě se vrátí celá a stejnou
    dávku lze zapsat znovu. Simulovaná data, neznámé řady, jiné jednotky a čas,
    kterému nelze věřit, se nezapíšou a započítají v `rejected`.
    """

    def __init__(self, database):
        if not isinstance(database, HistoryDatabase) or database.readonly or database.connection is None:
            raise HistoryError("path", "zapisovat lze jen do databáze otevřené přes open_writer()")
        self.database = database
        self.run_id = None
        self.high_water = None
        self.last_batch = 0
        self.rejected = Counter()  # odmítnuté vzorky podle důvodu (skutečná ztráta, HISTORY.md 5.4)
        self.duplicates = 0  # totéž měření znovu nebo už uložený vzorek; není ztráta
        self.conflicts = 0
        self._finished = False
        self._open_batch = None
        self._metric_ids = {}  # (check_id, subject) -> id; jen potvrzené commitem
        self._noted = set()
        self._offset = None
        self._tracker = ChangeTracker()
        self._gate = None
        self._loss = {}  # důvod -> [počet, první ts, poslední ts]: ztráty ještě nezapsané v souhrnu
        self._loss_reported = None  # monotónní čas posledního souhrnu
        self._monotonic = None  # nejnovější monotónní čas měření (expires_monotonic)
        self._latest = None  # (ts, kvalita) nejnovějšího výsledku: čas souhrnu ztrát
        self._seen = {}  # (check_id, simulated) -> poslední zpracovaný výsledek: opakované odeslání
        self._committing = None  # (číslo dávky, výsledek pokusu) posledního pokusu, který došel ke COMMIT
        self._aggregation_stopped = None  # popis konfliktu agregace; do konce běhu se neagreguje

    def start_run(self, app_version, interval_s, now_utc, boot_id=None, ts_quality="synced"):
        """Zahájí běh: uzavře nečisté běhy, zjistí horní mez, zapíše process.start. Vrací run_id."""
        if self.run_id is not None:
            raise HistoryError("run", f"běh {self.run_id} už je zahájený")
        if not isinstance(app_version, str) or not app_version:
            raise ValueError("app_version musí být neprázdný text")
        if isinstance(interval_s, bool) or not isinstance(interval_s, int) or not 1 <= interval_s <= 3600:
            raise ValueError(f"interval_s musí být celé číslo 1–3600, ne {interval_s!r}")
        if boot_id is not None and (not isinstance(boot_id, str) or not boot_id):
            raise ValueError("boot_id musí být neprázdný text nebo None")
        _check_quality(ts_quality)
        started = _utc_seconds(now_utc)
        ts_quality = _trusted_quality(started, ts_quality)
        conn = self.database.connection
        try:
            conn.execute("BEGIN IMMEDIATE")
            unclean = conn.execute("SELECT id, started_utc, last_write_utc FROM run "
                                   "WHERE stopped_utc IS NULL ORDER BY id").fetchall()
            conn.execute(CLOSE_UNCLEAN_RUNS_SQL)
            high = conn.execute(HIGH_WATER_SQL).fetchone()[0]
            done_until = conn.execute("SELECT done_until FROM aggregation_state WHERE level = '5m'").fetchone()[0]
            run_id = conn.execute("INSERT INTO run (started_utc, app_version, interval_s, boot_id) VALUES (?, ?, ?, ?)",
                                  (started, app_version, interval_s, boot_id)).lastrowid
            events = []
            for old_id, old_started, last_write in unclean:
                after = last_write
                if after is None:
                    # Bez vzorku je hranicí jen start běhu, a to jen s důvěryhodným časem (process.start).
                    row = conn.execute(START_QUALITY_SQL, (old_id,)).fetchone()
                    after = old_started if row is not None and row[0] != "unsynced" else None
                events.append(Event(started, ts_quality, "process.unclean_stop", "WARNING",
                                    f"běh {old_id} neskončil čistě", data={"run_id": old_id, "last_write_utc": last_write}))
                events.append(Event(started, ts_quality, "history.unknown_gap", "WARNING",
                                    f"neznámá mezera po nečistém ukončení běhu {old_id}",
                                    # Nesynchronizovaný čas startu není hranice mezery (HISTORY.md 4.3).
                                    data={"run_id": old_id, "after": after,
                                          "until": None if ts_quality == "unsynced" else started}))
            events.append(Event(started, ts_quality, "process.start", "INFO", f"start NexusSTAT {app_version}",
                                data={"app_version": app_version, "interval_s": interval_s, "boot_id": boot_id,
                                      "schema_version": SCHEMA_VERSION, "high_water": high,
                                      "unclean_runs": [row[0] for row in unclean]}))
            conn.executemany(INSERT_EVENT_SQL, [event.row(run_id) for event in events])
            conn.execute("COMMIT")
        except sqlite3.Error as error:
            _rollback(conn)
            raise HistoryError("write", f"běh nelze zahájit: {error}", _sqlite_name(error)) from None
        except BaseException:
            _rollback(conn)  # i Ctrl+C nebo chyba programu: nic rozepsaného, zámek zápisu uvolněn
            raise
        self.run_id, self.high_water = run_id, high
        self._gate = SampleGate(high, done_until)
        return run_id

    def begin_batch(self):
        """Nová dávka s číslem last_batch + 1. Předchozí musí být zapsaná (jinak by se ztratila)."""
        self._require_run()
        if self._open_batch is not None:
            raise HistoryError("batch", f"dávka {self._open_batch.number} ještě není zapsaná")
        self._open_batch = Batch(self.last_batch + 1)
        return self._open_batch

    def add_results(self, batch, results, ts_quality="synced", track_changes=True):
        """Přidá do dávky výsledky jednoho měření: změny stavu (R4) a přijaté vzorky.

        `ts_quality` dodává volající (stav synchronizace hodin se tu nezjišťuje):
        synced a derived dovolí vzorky, unsynced ne. Čas před MIN_TRUSTED_UTC
        se bere jako unsynced. Změny a události se ukládají s kvalitou vždy.
        `track_changes=False`: změny počítá volající už při předání (vlákno
        zapisovače, docs/HISTORY.md 5.2) a přidá je přes add_change().
        """
        self._require_run()
        self._check_open(batch)
        _check_quality(ts_quality)
        clock_checked = False
        for result in results:
            if not isinstance(result, CheckResult):
                raise TypeError(f"očekáván CheckResult, ne {type(result).__name__}")
            key = (result.id, result.simulated)
            if self._seen.get(key) == result:
                self.duplicates += 1  # totéž měření odeslané znovu: nic nového a žádná další ztráta
                continue
            self._seen[key] = result
            if result.simulated:
                self._lose("simulated", None, "unsynced")  # čas simulace se nikam nepíše
                self._note_once(batch, ("simulated",), Event(
                    max(_utc_seconds(result.observed_utc), 1), "unsynced", "history.simulated_rejected", "WARNING",
                    "simulovaná data se do historie nezapisují", check_id=result.id))
                continue
            if self._monotonic is None or result.expires_monotonic > self._monotonic:
                self._monotonic = result.expires_monotonic
            ts = _utc_seconds(result.observed_utc)
            quality = _trusted_quality(ts, ts_quality)
            self._latest = (ts, quality)
            if not clock_checked:
                self._check_clock(batch, result, ts, quality)
                clock_checked = True
            if track_changes:
                change = self._tracker.observe(result, ts, quality)
                if change is not None:
                    batch.changes.append(change)
            self._add_sample(batch, result, ts, quality)

    def add_change(self, batch, change):
        """Přidá změnu stavu spočítanou mimo zapisovač (ChangeTracker volajícího)."""
        self._require_run()
        self._check_open(batch)
        if not isinstance(change, Change):
            raise TypeError(f"očekávána Change, ne {type(change).__name__}")
        _check_quality(change.ts_quality)
        if isinstance(change.ts, bool) or not isinstance(change.ts, int) or change.ts <= 0:
            raise ValueError(f"čas změny musí být kladné celé číslo, ne {change.ts!r}")
        states = {state.value for state in State}
        if change.to_state not in states or (change.from_state is not None and change.from_state not in states):
            raise ValueError(f"neznámý stav změny {change.from_state!r} → {change.to_state!r}")
        quality = _trusted_quality(change.ts, change.ts_quality)  # čas před 2026 nikdy jako synced
        batch.changes.append(change if quality == change.ts_quality else replace(change, ts_quality=quality))

    @property
    def commit_in_doubt(self):
        """Poslední neúspěšný pokus došel až ke COMMIT: dávka mohla i nemusela být zapsaná."""
        return self._committing is not None

    def rebind(self, database):
        """Pokračuje stejný běh na nově otevřeném spojení téhož souboru (po chybě I/O).

        Staré spojení musí volající zavřít dřív (nikdy dvě zapisující spojení najednou).
        """
        if not isinstance(database, HistoryDatabase) or database.readonly or database.connection is None:
            raise HistoryError("path", "zapisovat lze jen do databáze otevřené přes open_writer()")
        if database.path.resolve() != self.database.path.resolve():
            raise HistoryError("path", f"{database.path} není soubor tohoto běhu ({self.database.path})")
        self.database = database

    def add_event(self, batch, event):
        self._require_run()
        self._check_open(batch)
        if not isinstance(event, Event):
            raise TypeError(f"očekávána Event, ne {type(event).__name__}")
        batch.events.append(event)

    def write(self, batch):
        """Zapíše dávku jednou transakcí; vrací BatchResult.

        Opakovaná dávka (po nejistém commitu) se pozná podle run.last_batch a
        nic se nezdvojí. Dávka mimo pořadí a chyba SQLite vyvolají HistoryError;
        po chybě v databázi nezůstane nic z dávky a stejnou dávku lze poslat znovu.
        První pokus dávku zapečetí a přidá k ní souhrn ztrát history.rejected
        (první hned, další nejvýš jednou za hodinu monotónního času).
        """
        self._require_run()
        if not batch.sealed:
            if batch is not self._open_batch:
                # Cizí dávka se stejným číslem by otevřenou dávku předběhla a ta by se
                # pak tiše zahodila jako „už zapsaná“.
                raise HistoryError("batch", f"dávka {batch.number} nevznikla v begin_batch()")
            self._attach_loss_summary(batch)
            batch.seal()
        elif batch.changed_since_seal():
            raise HistoryError("batch", f"dávka {batch.number} se po odeslání změnila; opakuje se jen beze změny")
        conn = self.database.connection
        tentative, rejected, conflicts, extra_events = {}, [], [], []
        inserted = duplicates = 0
        newest = None  # nejnovější skutečně vložený vzorek: jediný zdroj horní meze
        try:
            conn.execute("BEGIN IMMEDIATE")
            guard = {"run_id": self.run_id, "batch": batch.number}
            if conn.execute(BATCH_GUARD_SQL, guard).rowcount != 1:
                conn.execute("ROLLBACK")
                return self._not_applied(conn, batch)
            # Potvrzená hranice agregace v téže transakci: vzorek do hotového okna se nevloží
            # nikdy, ani když obešel SampleGate (hotová agregace = surová data, HISTORY.md 6.4).
            aggregated = conn.execute(DONE_UNTIL_SQL, ("5m",)).fetchone()[0]
            for sample in batch.samples:
                if sample.ts < aggregated:
                    rejected.append(("late", sample))
                    continue
                metric_id = self._metric_id(conn, sample, tentative, extra_events)
                if metric_id is None:
                    rejected.append(("unit_mismatch", sample))
                    continue
                params = {"metric_id": metric_id, "ts": sample.ts, "value": sample.value}
                if conn.execute(INSERT_SAMPLE_SQL, params).rowcount == 1:
                    inserted += 1
                    newest = sample.ts if newest is None else max(newest, sample.ts)
                    continue
                same, stored = conn.execute(SAMPLE_CONFLICT_SQL, params).fetchone()
                if same:
                    duplicates += 1
                    continue
                conflict = SampleConflict(sample.check_id, sample.subject, sample.ts, stored, sample.value)
                conflicts.append(conflict)
                extra_events.append(Event(sample.ts, sample.ts_quality, "history.sample_conflict", "ERROR",
                                          "jiná hodnota vzorku je už uložená; uložená zůstává",
                                          check_id=sample.check_id,
                                          data={"subject": sample.subject, "ts": sample.ts,
                                                "stored": stored, "rejected": sample.value}))
            conn.executemany(INSERT_CHANGE_SQL, [change.row(self.run_id) for change in batch.changes])
            events = batch.events + extra_events
            conn.executemany(INSERT_EVENT_SQL, [event.row(self.run_id) for event in events])
            conn.execute(RUN_LAST_WRITE_SQL, {"run_id": self.run_id, "max_ts": newest})
            # Výsledek pokusu si zapisovač pamatuje před COMMIT: kdyby commit proběhl a volající
            # dostal chybu, promítne se do počitadel až při opakování (_not_applied), právě jednou.
            self._committing = (batch.number, (tentative, duplicates, conflicts, rejected))
            conn.execute("COMMIT")
        except sqlite3.Error as error:
            _rollback(conn)
            raise HistoryError("write", f"dávku {batch.number} nelze zapsat, vrácena: {error}", _sqlite_name(error)) from None
        except BaseException:
            _rollback(conn)  # i Ctrl+C nebo chyba programu: nic rozepsaného, zámek zápisu uvolněn
            raise
        self._committing = None
        self._settle(batch, (tentative, duplicates, conflicts, rejected))
        return BatchResult(batch.number, False, inserted, duplicates, tuple(conflicts), len(batch.changes), len(events))

    def aggregate(self, now_utc, ts_quality, queue):
        """Agreguje uzavřená okna (HISTORY.md 6); vrací AggregationResult.

        Okno [b, b + 300) je uzavřené, když (1) b + 300 ≤ now − 120 s, (2) ve
        frontě (`queue`, kontrakt s M3.3) ani v otevřené dávce zapisovače není
        vzorek s časem před b + 300, (3) čas je synchronizovaný a (4) okno
        nepředbíhá nejnovější zapsaný vzorek. Jedno volání provede nejvýš jednu
        transakci 5min oken (nejvýš do konce hodiny) a jednu hodinovou, takže
        dohánění je postupné. Hotové okno se nikdy nepřepíše: konflikt zapíše
        history.aggregation_conflict, vyvolá HistoryError("conflict") a agregaci
        do konce běhu zastaví.
        """
        self._require_run()
        if not isinstance(queue, QueueState):
            raise TypeError("stav fronty musí být QueueState (empty, waiting_since nebo unknown)")
        _check_quality(ts_quality)
        now = _utc_seconds(now_utc)
        if self._aggregation_stopped is not None:
            raise HistoryError("conflict", self._aggregation_stopped)
        if _trusted_quality(now, ts_quality) != "synced":
            return self._aggregation_result(None, "time_unsynced")
        if not queue.known:
            return self._aggregation_result(None, "queue_unknown")
        # (1) rezerva po konci okna, (2) nejstarší nezapsaný vzorek ve frontě a v otevřené dávce.
        limit = (now - AGGREGATION_GRACE_SECONDS) // WINDOW_SECONDS * WINDOW_SECONDS
        pending = [queue.oldest_utc]
        if self._open_batch is not None:
            pending += [sample.ts for sample in self._open_batch.samples]
        for oldest in pending:
            if oldest is not None:
                limit = min(limit, oldest // WINDOW_SECONDS * WINDOW_SECONDS)

        def plan_5m(conn):
            start = conn.execute(DONE_UNTIL_SQL, ("5m",)).fetchone()[0]
            newest = conn.execute(HIGH_WATER_SQL).fetchone()[0]
            if newest is None:
                return None  # žádný zapsaný vzorek: není co uzavřít
            # (4) okno s nejnovějším zapsaným vzorkem a pozdější zůstávají otevřené (skok času dopředu).
            end = min(limit, newest // WINDOW_SECONDS * WINDOW_SECONDS, (start // HOUR_SECONDS + 1) * HOUR_SECONDS)
            return (start, end) if end > start else None

        def plan_1h(conn):
            start = conn.execute(DONE_UNTIL_SQL, ("1h",)).fetchone()[0]
            ready = conn.execute(DONE_UNTIL_SQL, ("5m",)).fetchone()[0]
            return (start, start + HOUR_SECONDS) if start + HOUR_SECONDS <= ready else None

        done, windows_5m = self._aggregation_step("5m", plan_5m, now)
        if done is not None:
            self._gate.done_until = max(self._gate.done_until, done)  # až po COMMIT
        _, windows_1h = self._aggregation_step("1h", plan_1h, now)
        return self._aggregation_result(limit, None, windows_5m, windows_1h)

    def finish_run(self, now_utc, ts_quality="synced"):
        """Čisté ukončení běhu (stopped_utc, clean_stop = 1, process.stop); opakované volání nic nezdvojí."""
        if self.run_id is None:
            raise HistoryError("run", "běh není zahájený")
        if self._open_batch is not None:
            raise HistoryError("batch", f"dávka {self._open_batch.number} ještě není zapsaná")
        _check_quality(ts_quality)
        stopped = _utc_seconds(now_utc)
        ts_quality = _trusted_quality(stopped, ts_quality)
        conn = self.database.connection
        try:
            conn.execute("BEGIN IMMEDIATE")
            closed = conn.execute("UPDATE run SET stopped_utc = ?, clean_stop = 1 WHERE id = ? AND stopped_utc IS NULL",
                                  (stopped, self.run_id)).rowcount
            if closed:
                # Ztráty od posledního souhrnu se zapíšou i při ukončení, bez ohledu na hodinový odstup.
                events = [] if not self._loss else [self._loss_summary(stopped, ts_quality)]
                events.append(Event(stopped, ts_quality, "process.stop", "INFO", "čisté ukončení NexusSTAT"))
                conn.executemany(INSERT_EVENT_SQL, [event.row(self.run_id) for event in events])
            conn.execute("COMMIT")
        except sqlite3.Error as error:
            _rollback(conn)
            raise HistoryError("write", f"běh nelze ukončit: {error}", _sqlite_name(error)) from None
        except BaseException:
            _rollback(conn)
            raise
        if closed:
            self._loss = {}
        self._finished = True
        return bool(closed)

    def _aggregation_step(self, level, plan, now):
        """BEGIN IMMEDIATE → progress → INSERT oken → COMMIT; vrací (nové done_until, vložené řádky)."""
        progress, insert, table = AGGREGATION_SQL[level]
        conn = self.database.connection
        span = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            span = plan(conn)
            if span is None:
                conn.execute("ROLLBACK")
                return None, 0
            params = {"start": span[0], "end": span[1]}
            if conn.execute(progress, params).rowcount != 1:
                conn.execute("ROLLBACK")  # interval už je hotový (opakování): nic se nezapíše
                return None, 0
            rows = conn.execute(insert, params).rowcount
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as error:
            _rollback(conn)
            self._aggregation_conflict(level, table, span, now, error)
        except sqlite3.Error as error:
            _rollback(conn)
            raise HistoryError("write", f"agregaci {level} {span} nelze zapsat, vrácena: {error}") from None
        except BaseException:
            _rollback(conn)  # i Ctrl+C nebo chyba programu: nic rozepsaného, zámek zápisu uvolněn
            raise
        return span[1], rows

    def _aggregation_conflict(self, level, table, span, now, error):
        """Doloží a ohlásí okno, které už existuje; nic se nepřepíše a agregace se zastaví."""
        conn = self.database.connection
        start, end = span
        try:
            existing, first, last = conn.execute(f"SELECT count(*), min(bucket), max(bucket) FROM {table} "
                                                 "WHERE bucket >= ? AND bucket < ?", (start, end)).fetchone()
        except sqlite3.Error as failure:
            # Konflikt nelze doložit: obecná chyba zápisu, agregace se nezastavuje.
            raise HistoryError("write", f"agregaci {level} {span} nelze zapsat ({error}) "
                                        f"a existující okna nelze ověřit: {failure}") from None
        if not existing:
            raise HistoryError("write", f"agregaci {level} {span} nelze zapsat, vrácena: {error}") from None
        message = (f"agregace {level} [{start}, {end}): {existing} oken už existuje; "
                   "nic se nepřepsalo, agregace zastavena do zásahu správce")
        event = Event(now, "synced", "history.aggregation_conflict", "ERROR", message,
                      data={"level": level, "start": start, "end": end, "existing": existing,
                            "first": first, "last": last})
        # Konflikt je doložený existujícími okny, agregace se zastaví v každém případě. Zpráva
        # tvrdí zapsaný doklad jen po potvrzeném COMMIT; jinak „nepodařilo potvrdit“ (i nejistý commit).
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(INSERT_EVENT_SQL, event.row(self.run_id))
            conn.execute("COMMIT")
            note = "doklad history.aggregation_conflict zapsán"
        except sqlite3.Error as failure:
            _rollback(conn)
            note = f"doklad history.aggregation_conflict se nepodařilo potvrdit: {failure}"
        except BaseException:
            _rollback(conn)
            self._aggregation_stopped = f"{message}; doklad history.aggregation_conflict se nepodařilo potvrdit (přerušeno)"
            raise
        self._aggregation_stopped = f"{message}; {note}"
        raise HistoryError("conflict", self._aggregation_stopped)

    def _aggregation_result(self, limit, blocked, windows_5m=0, windows_1h=0):
        conn = self.database.connection
        done_5m = conn.execute(DONE_UNTIL_SQL, ("5m",)).fetchone()[0]
        done_1h = conn.execute(DONE_UNTIL_SQL, ("1h",)).fetchone()[0]
        more = False
        if blocked is None:
            newest = conn.execute(HIGH_WATER_SQL).fetchone()[0]
            reachable = done_5m if newest is None else min(limit, newest // WINDOW_SECONDS * WINDOW_SECONDS)
            more = reachable > done_5m or done_1h + HOUR_SECONDS <= done_5m
        return AggregationResult(done_5m, done_1h, windows_5m, windows_1h, blocked, more)

    def retention_plan(self, now_utc, clock):
        """Plán retence nad spojením zapisovače, bez zápisu (viz retention_plan())."""
        self._require_run()
        return retention_plan(self.database.connection, now_utc, clock)

    def retain(self, now_utc, clock, plan=None, max_transactions=RETENTION_MAX_TRANSACTIONS):
        """Jeden běh retence (HISTORY.md 7); vrací RetentionResult.

        Každý krok je jedna transakce, která smaže nejvýš hodinu historie jedné tabulky:
        BEGIN IMMEDIATE → znovu čas, done_until a invariant agregací → počet → DELETE →
        kontrola rowcount → hranice v meta → COMMIT. Tabulky se střídají; nejvýš
        `max_transactions` (≤ 24) mazacích transakcí za běh. `plan` je jen horní mez.
        Při nejistém COMMIT se nejdřív ověří stav databáze. Nic jiného se nemění.
        """
        self._require_run()
        now = _check_clock_state(now_utc, clock)
        if isinstance(max_transactions, bool) or not isinstance(max_transactions, int) \
                or not 1 <= max_transactions <= RETENTION_MAX_TRANSACTIONS:
            raise ValueError(f"max_transactions musí být 1–{RETENTION_MAX_TRANSACTIONS}")
        caps = {}
        if plan is not None:
            if not isinstance(plan, RetentionPlan):
                raise TypeError("plan musí být RetentionPlan z retention_plan()")
            caps = {entry.table: entry.cutoff for entry in plan.tables}
        conn = self.database.connection
        deleted, stopped = Counter(), {}
        transactions = verified = 0
        blocked = None  # čas se ověřuje jen uvnitř každé mazací transakce (autoritativně)
        active = list(RETENTION_TABLES)
        while active and transactions < max_transactions:
            for table in list(active):
                if transactions >= max_transactions:
                    break
                status, value, confirmed = self._retention_step(table, now, clock, caps.get(table))
                if status == "deleted":
                    transactions += 1
                    deleted[table] += value
                    verified += confirmed
                elif status == "done":
                    active.remove(table)
                elif status == "stopped":
                    stopped[table] = value
                    active.remove(table)
                else:  # čas není bezpečný: v tomto běhu se nemaže nic dalšího
                    blocked, active = value, []
                    break
        # Limit běhu došel: zbývá práce jen tam, kde ještě je řádek pod hranicí (jen čtení).
        more = any(self._retention_pending(table, now, caps.get(table)) for table in active)
        return RetentionResult(blocked, transactions, {table: deleted[table] for table in RETENTION_TABLES},
                               stopped, retained_since(conn), verified, more)

    def _retention_pending(self, table, now, cap):
        conn = self.database.connection
        done_5m = conn.execute(DONE_UNTIL_SQL, ("5m",)).fetchone()[0]
        done_1h = conn.execute(DONE_UNTIL_SQL, ("1h",)).fetchone()[0]
        time_cutoff, bound = _retention_bounds(table, now, done_5m, done_1h)
        cutoff = min(value for value in (time_cutoff, bound, cap) if value is not None)
        oldest = _oldest(conn, RETENTION_SQL[table][0])
        return oldest is not None and oldest < cutoff

    def _retention_step(self, table, now, clock, cap):
        """Jedna mazací transakce; vrací (stav, hodnota, potvrzeno až kontrolou po COMMIT)."""
        oldest_sql, delete_sql, check_sql = RETENTION_SQL[table]
        conn = self.database.connection
        end = expected = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            reason = _retention_guard(conn, now, clock)
            if reason is not None:
                conn.execute("ROLLBACK")
                return "blocked", reason, 0
            done_5m = conn.execute(DONE_UNTIL_SQL, ("5m",)).fetchone()[0]
            done_1h = conn.execute(DONE_UNTIL_SQL, ("1h",)).fetchone()[0]
            if check_sql is not None and done_1h > done_5m:
                conn.execute("ROLLBACK")  # porušená evidence agregací: nemazat nic, co na ní závisí
                return "stopped", "aggregation_state_invalid", 0
            time_cutoff, bound = _retention_bounds(table, now, done_5m, done_1h)
            cutoff = min(value for value in (time_cutoff, bound, cap) if value is not None)
            oldest = _oldest(conn, oldest_sql)
            if oldest is None or oldest >= cutoff:
                conn.execute("ROLLBACK")
                return "done", None, 0
            end = min(cutoff, oldest // RETENTION_STEP_SECONDS * RETENTION_STEP_SECONDS + RETENTION_STEP_SECONDS)
            params = {"cutoff": end}
            if check_sql is not None and conn.execute(check_sql, params).fetchone()[0]:
                conn.execute("ROLLBACK")  # data pod hranicí nejsou celá v agregacích: nemazat
                return "stopped", "aggregation_mismatch", 0
            expected = conn.execute(RETENTION_COUNT_SQL[table], params).fetchone()[0]
            removed = conn.execute(delete_sql, params).rowcount
            if removed != expected:
                raise sqlite3.DatabaseError(f"smazáno {removed} řádků místo {expected}")
            self._mark_retained(conn, table, end)
            conn.execute("COMMIT")
        except sqlite3.Error as error:
            _rollback(conn)
            if expected is not None and self._retention_committed(table, end):
                return "deleted", expected, 1  # COMMIT proběhl, jen volající dostal chybu
            raise HistoryError("write", f"retenci {table} pod {end} nelze provést, vrácena: {error}") from None
        except BaseException:
            _rollback(conn)  # i Ctrl+C nebo chyba programu: nic smazaného, zámek zápisu uvolněn
            raise
        return "deleted", removed, 0

    def _mark_retained(self, conn, table, end):
        key = RETAINED_SINCE_KEY.format(table=table)
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, str(end)))
        elif int(row[0]) < end:
            conn.execute("UPDATE meta SET value = ? WHERE key = ?", (str(end), key))

    def _retention_committed(self, table, end):
        """Po chybě COMMIT: je krok v databázi? (pod hranicí nic nezbylo a hranice je zapsaná)."""
        conn = self.database.connection
        try:
            remaining = conn.execute(RETENTION_COUNT_SQL[table], {"cutoff": end}).fetchone()[0]
            mark = retained_since(conn)[table]
        except sqlite3.Error:
            return False  # stav nelze ověřit: hlásí se jako neprovedené
        return remaining == 0 and mark is not None and mark >= end

    def _check_open(self, batch):
        if not isinstance(batch, Batch):
            raise TypeError(f"očekávána Batch, ne {type(batch).__name__}")
        if batch.sealed:
            raise HistoryError("batch", f"dávka {batch.number} už byla odeslána k zápisu; opakuje se jen beze změny")

    def _lose(self, reason, ts, quality):
        """Započte odmítnutý vzorek; rozsah UTC jen z důvěryhodného času (nic se neodhaduje)."""
        self.rejected[reason] += 1
        entry = self._loss.setdefault(reason, [0, None, None])
        entry[0] += 1
        if ts is not None and quality != "unsynced":
            entry[1] = ts if entry[1] is None else min(entry[1], ts)
            entry[2] = ts if entry[2] is None else max(entry[2], ts)

    def _loss_summary(self, ts, ts_quality):
        reasons = {}
        for reason, (count, first, last) in sorted(self._loss.items()):
            reasons[reason] = {"count": count} if first is None else {"count": count, "from": first, "to": last}
        total = sum(entry["count"] for entry in reasons.values())
        return Event(ts, ts_quality, "history.rejected", "WARNING", f"odmítnuté vzorky: {total}",
                     data={"total": total, "reasons": reasons})

    def _attach_loss_summary(self, batch):
        # Souhrn jede se zapečetěnou dávkou: při chybě zůstane v ní a zapíše se jejím opakováním.
        if not self._loss or self._latest is None:
            return
        if self._loss_reported is not None and self._monotonic - self._loss_reported < LOSS_SUMMARY_SECONDS:
            return
        batch.events.append(self._loss_summary(*self._latest))
        self._loss = {}
        self._loss_reported = self._monotonic

    def _require_run(self):
        if self.run_id is None:
            raise HistoryError("run", "běh není zahájený (start_run)")
        if self._finished:
            raise HistoryError("run", f"běh {self.run_id} je ukončený")

    def _not_applied(self, conn, batch):
        stored = run_info(conn, self.run_id).last_batch
        if stored >= batch.number:
            # Dávka už je v databázi (např. commit proběhl, ale volající dostal chybu).
            # Jediný zapisovač: potvrzený je poslední pokus, který došel ke COMMIT.
            committed, self._committing = self._committing, None
            if committed is not None and committed[0] == batch.number:
                self._settle(batch, committed[1])
            if batch is self._open_batch:
                self._open_batch = None
            self.last_batch = max(self.last_batch, stored)
            return BatchResult(batch.number, True)
        raise HistoryError("batch", f"dávka {batch.number} je mimo pořadí (poslední zapsaná {stored})")

    def _settle(self, batch, outcome):
        """Promítne potvrzenou dávku do paměti zapisovače (cache řad, počitadla, ztráty)."""
        tentative, duplicates, conflicts, rejected = outcome
        self._metric_ids.update(tentative)
        self.duplicates += duplicates
        self.conflicts += len(conflicts)
        for reason, sample in rejected:
            self._lose(reason, sample.ts, sample.ts_quality)
        self.last_batch = max(self.last_batch, batch.number)
        if batch is self._open_batch:
            self._open_batch = None

    def _metric_id(self, conn, sample, tentative, events):
        key = (sample.check_id, sample.subject)
        if key in self._metric_ids or key in tentative:
            return self._metric_ids.get(key, tentative.get(key))
        conn.execute(INSERT_METRIC_SQL, (sample.check_id, sample.subject, sample.unit, sample.ts))
        metric_id, unit = conn.execute("SELECT id, unit FROM metric WHERE check_id = ? AND subject = ?", key).fetchone()
        if unit != sample.unit:
            # Řada už existuje s jinou jednotkou (jiná verze katalogu): nic se nemíchá.
            events.append(Event(sample.ts, sample.ts_quality, "history.unit_mismatch", "WARNING",
                                f"řada má v databázi jednotku {unit}, katalog {sample.unit}; vzorek nezapsán",
                                check_id=sample.check_id, data={"subject": sample.subject}))
            metric_id = None
        tentative[key] = metric_id
        return metric_id

    def _add_sample(self, batch, result, ts, quality):
        spec = SERIES.get(result.id)
        if spec is None:
            if result.measurement is not None:
                self._lose("unknown_series", ts, quality)  # číslo bez řady v katalogu se nezapíše
            return
        if result.state is State.NA:
            return  # záměrně neaktivní: žádný vzorek
        if quality == "unsynced":
            self._lose("time_unsynced", ts, quality)
            return
        subject = ""
        if spec.subject_key is not None:
            subject = result.details.get(spec.subject_key)
            if not isinstance(subject, str) or not subject:
                self._lose("no_subject", ts, quality)  # např. síť bez rozhraní: nelze určit řadu
                return
        if result.state is State.UNKNOWN:
            value = None
        else:
            measurement = result.measurement
            if measurement is None:
                self._lose("missing_value", ts, quality)
                return
            if measurement.unit != spec.unit:
                self._lose("unit_mismatch", ts, quality)
                self._note_once(batch, ("unit", result.id), Event(
                    ts, quality, "history.unit_mismatch", "WARNING",
                    f"jednotka {measurement.unit}, katalog {spec.unit}; vzorek nezapsán", check_id=result.id))
                return
            if not is_finite_number(measurement.value):
                self._lose("invalid_value", ts, quality)
                return
            value = float(measurement.value)
        reason = self._gate.accept((result.id, subject), ts, result.expires_monotonic)
        if reason == "duplicate":
            self.duplicates += 1
        elif reason is not None:
            self._lose(reason, ts, quality)
        else:
            batch.samples.append(PendingSample(result.id, subject, spec.unit, ts, quality, value))

    def _check_clock(self, batch, result, ts, quality):
        # Posun UTC − monotonic (expires_monotonic = monotónní čas měření + stálá platnost).
        offset = result.observed_utc.timestamp() - result.expires_monotonic
        if self._offset is not None and abs(offset - self._offset) > CLOCK_JUMP_SECONDS:
            shift = offset - self._offset
            batch.events.append(Event(ts, quality, "clock.jump", "WARNING", f"skok systémového času o {shift:+.0f} s",
                                      data={"shift_s": round(shift, 3)}))
        self._offset = offset

    def _note_once(self, batch, key, event):
        if key not in self._noted:
            self._noted.add(key)
            batch.events.append(event)


def _rollback(conn):
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
