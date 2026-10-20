"""Základ SQLite historie (M3.1a): vytvoření, bezpečné otevření a ověření databáze.

Implementuje jen infrastrukturu schématu v1 z docs/HISTORY.md (sekce 3, 5.7, 5.8):
- vytvoření nové testovací nebo vývojové databáze na výslovně zadané cestě,
- otevření pro zápis s ověřením identity a zámkem jediného zapisovače,
- otevření jen pro čtení, které nic nezapíše ani neprovede checkpoint.

Zatím se sem nic neukládá: zápis vzorků a změn stavu je M3.1b, agregace,
retence a snímky přijdou později. Modul nemá žádnou výchozí cestu, nikdy
nevytváří adresář a nikdy nepřepíše ani nesmaže existující soubor; maže jen
vlastní dočasné soubory z nepovedeného vytvoření. Produkční databáze se v M3.1a
nevytváří ani neotevírá. Nečte docs/HISTORY.md: schéma je v SCHEMA_SQL a jeho
shodu s dokumentací hlídá tests/test_history.py.
"""

import fcntl
import functools
import hashlib
import os
import re
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

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
    selhalo), "settings" (nastavení spojení se nepodařilo) a "alias" (druhé
    jméno databáze po přerušeném vytvoření má vlastní -wal, -shm nebo -journal).
    """

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


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
            raise HistoryError("path", f"databázi {path} nelze otevřít pro zápis: {error}") from None
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
            raise HistoryError("corrupt", f"databázi {path} nelze použít: {error}") from None
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
