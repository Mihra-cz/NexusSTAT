"""Neměnné generace konzistentních snímků SQLite historie (M3.4a).

Snímek vzniká výhradně přes `sqlite3.Connection.backup()` ze spojení vlákna
zapisovače (jediný zapisovač, docs/HISTORY.md 5.8); kopírování živého souboru
by konzistentní nebylo. Cílem je nové spojení na nový soubor v **rozpracovaném
adresáři mimo zálohovaný adresář**, takže Restic nikdy nevidí nehotovou práci.

Generace je neměnná dvojice (databáze + její JSON záznam) v jednom adresáři.
Zveřejní se **jediným přejmenováním adresáře** na témže souborovém systému:
dvě samostatná přejmenování souborů nejsou atomické zveřejnění dvojice a pád
mezi nimi by nechal nový snímek se starým záznamem (oprava návrhu 9.2).
Starší generace se nikdy nepřepisují; nedokončená generace se nikdy nevydává
za platnou a po pádu se vybírá poslední prokazatelně platná generace.

Automatické mazání starých generací, plánování, kontrola `history.snapshot`
a napojení na Restic nejsou součástí tohoto kroku (M3.4b, M7).
"""

import errno
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from . import history, storage

SNAPSHOT_DB = "history-snapshot.sqlite3"
SNAPSHOT_RECORD = "history-snapshot.json"
RECORD_VERSION = 1

# Jméno generace je čas snímku v UTC; pořadí jmen je pořadí generací.
GENERATION_FORMAT = "%Y%m%dT%H%M%SZ"
GENERATION_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z")
WORK_PREFIX = "."
WORK_SUFFIX = ".partial"

# Backup API po stránkách (4 MiB na krok při stránce 4 KiB). Měření na notebooku
# (HISTORY.md 10.9) ukázalo stejnou celkovou dobu jako jednorázové `pages=-1`
# (22 MB snímek: 103 ms proti 105 ms), ale mezi kroky se volá `progress`, takže
# kopii lze při ukončení přerušit na hranici stránek (nejdelší krok 61 ms).
# Konzistenci to neohrozí: jediný zapisovač je právě uvnitř kopie, takže se
# zdroj během ní nemění a Backup API kopii nerestartuje.
SNAPSHOT_PAGES = 1024
SHA_CHUNK = 1 << 20

# Reprezentativní čtecí dotazy pro test obnovitelnosti (ne kontrakt čtení
# epizod podle 7.1, jen doklad, že obnovený soubor jde skutečně číst).
RESTORE_QUERIES = {
    "samples": "SELECT m.check_id, s.ts, s.value FROM sample s JOIN metric m ON m.id = s.metric_id "
               "ORDER BY s.ts DESC LIMIT 20",
    "aggregates_5m": "SELECT bucket, n_samples, n_valid, min_value, max_value FROM agg_5m "
                     "ORDER BY bucket DESC LIMIT 20",
    "aggregates_1h": "SELECT bucket, n_samples, sum_value FROM agg_1h ORDER BY bucket DESC LIMIT 20",
    "state_episodes": "SELECT check_id, ts, from_state, to_state FROM check_change "
                      "ORDER BY check_id, ts DESC LIMIT 50",
    "events": "SELECT kind, severity, ts FROM event ORDER BY ts DESC LIMIT 20",
}


class SnapshotError(Exception):
    """Snímek nelze vytvořit, ověřit nebo zveřejnit; platné generace se nemění.

    `reason` je krátký kód pro program: "path" (adresář nebo úložiště), "time"
    (nedůvěryhodný čas), "exists" (generace už existuje), "copy" (Backup API),
    "verify" (kontrola kopie), "record" (JSON záznam), "publish" (zveřejnění).
    """

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SnapshotRecord:
    """Záznam generace (JSON vedle snímku): identita, čas, kontrolní součet, obsah."""

    generation: str
    snapshot_utc: int
    schema_version: int
    role: str
    host_id: str
    database: str
    bytes: int
    sha256: str
    rows: MappingProxyType
    sqlite_version: str
    app_version: str
    record_version: int = RECORD_VERSION

    def to_json(self):
        data = {
            "record_version": self.record_version, "generation": self.generation,
            "snapshot_utc": self.snapshot_utc, "schema_version": self.schema_version,
            "role": self.role, "host_id": self.host_id,
            "database": {"name": self.database, "bytes": self.bytes, "sha256": self.sha256},
            "rows": dict(self.rows), "sqlite_version": self.sqlite_version, "app_version": self.app_version,
        }
        return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text):
        try:
            data = json.loads(text)
            database = data["database"]
            return cls(data["generation"], int(data["snapshot_utc"]), int(data["schema_version"]),
                       data["role"], data["host_id"], database["name"], int(database["bytes"]),
                       database["sha256"], MappingProxyType(dict(data["rows"])), data["sqlite_version"],
                       data["app_version"], int(data["record_version"]))
        except (ValueError, TypeError, KeyError) as error:
            raise SnapshotError("record", f"záznam snímku nelze přečíst: {error}") from None


@dataclass(frozen=True)
class Generation:
    """Zveřejněná generace: adresář, jméno a její záznam."""

    name: str
    path: Path
    record: SnapshotRecord

    @property
    def database(self):
        return self.path / self.record.database


@dataclass(frozen=True)
class RestoreReport:
    """Výsledek testu obnovitelnosti jedné generace (jen čtení)."""

    generation: str
    ok: bool
    snapshot_utc: int | None = None
    bytes: int | None = None
    sha256: str | None = None
    rows: MappingProxyType = MappingProxyType({})
    integrity: str | None = None
    foreign_keys: int | None = None
    journal_mode: str | None = None
    queries: MappingProxyType = MappingProxyType({})
    problems: tuple = ()


def generation_name(snapshot_utc):
    """Jméno generace z času snímku (UTC); čas musí být důvěryhodný."""
    if isinstance(snapshot_utc, bool) or not isinstance(snapshot_utc, int):
        raise SnapshotError("time", f"čas snímku musí být celé číslo sekund, ne {snapshot_utc!r}")
    if snapshot_utc < history.MIN_TRUSTED_UTC:
        raise SnapshotError("time", f"čas {snapshot_utc} není důvěryhodný; snímek se nevytváří")
    return datetime.fromtimestamp(snapshot_utc, timezone.utc).strftime(GENERATION_FORMAT)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(SHA_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _standalone(path):
    """Spojení jen pro čtení nad samostatným souborem (nevytvoří -wal ani -shm)."""
    uri = f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1"
    try:
        return sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as error:
        raise SnapshotError("verify", f"snímek {path} nelze otevřít pro čtení: {error}") from None


def prepare(work_root, published_root, mounts=None):
    """Ověří adresáře před stavbou snímku; vrací (rozpracovaný, zveřejněný) jako Path.

    Oba musí existovat, být skutečné adresáře (ne symlink) na doloženě lokálním
    úložišti a na **stejném** souborovém systému: publikace generace je jedno
    přejmenování, které mezi systémy skončí na EXDEV. Rozpracovaný adresář musí
    ležet mimo zveřejňovaný, aby Restic nikdy neviděl nehotovou generaci.
    """
    work, published = Path(work_root), Path(published_root)
    try:
        storage.check_directory(work, "rozpracovaný adresář snímků", mounts)
        storage.check_directory(published, "adresář generací snímků", mounts)
    except storage.StorageError as error:
        raise SnapshotError("path", str(error)) from None
    work_real, published_real = storage.resolve_path(work), storage.resolve_path(published)
    if work_real == published_real or published_real in work_real.parents:
        raise SnapshotError("path", f"rozpracovaný adresář {work} nesmí ležet v zveřejňovaném {published}")
    if not storage.same_filesystem(work, published):
        raise SnapshotError("path", f"{work} a {published} nejsou na stejném souborovém systému; "
                                    "generaci nelze zveřejnit jedním přejmenováním")
    # Zveřejnění musí být atomické **a** nesmí nikdy přepsat existující cíl.
    # Bez RENAME_NOREPLACE by prázdný cílový adresář vzniklý mezi kontrolou a
    # přejmenováním zmizel pod rukama, proto se podpora ověří dřív, než se
    # začne stavět snímek, a bez ní se nic nezveřejňuje.
    if not storage.supports_rename_noreplace(work):
        raise SnapshotError("path", f"{work} neumí atomické přejmenování bez přepsání cíle "
                                    "(renameat2 RENAME_NOREPLACE); generace se nezveřejňuje")
    return work, published


def cleanup_work(work_root):
    """Odstraní **vlastní** rozpracované generace; cizí obsah nikdy nemaže.

    Maže jen adresáře jména `.<generace>.partial` a v nich jen soubory snímku.
    Zůstane-li v adresáři něco jiného, nechá se být a rozhodne správce.
    """
    removed = []
    for entry in sorted(Path(work_root).iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if not (entry.name.startswith(WORK_PREFIX) and entry.name.endswith(WORK_SUFFIX)):
            continue
        if not GENERATION_NAME.fullmatch(entry.name[len(WORK_PREFIX):-len(WORK_SUFFIX)]):
            continue
        own = {SNAPSHOT_DB, SNAPSHOT_RECORD, *(f"{SNAPSHOT_DB}{suffix}" for suffix in history.COMPANION_SUFFIXES)}
        if any(child.name not in own for child in entry.iterdir()):
            continue  # cizí soubor: adresář se nemaže
        for child in entry.iterdir():
            child.unlink()
        entry.rmdir()
        removed.append(entry.name)
    return removed


def copy_database(connection, target_path, snapshot_utc, pages=SNAPSHOT_PAGES, progress=None):
    """Konzistentní kopie přes Backup API do nového souboru; vrací cestu.

    Zdrojem je spojení vlákna zapisovače, cílem nové spojení výhradně na nový
    soubor (`check_same_thread` zůstává zapnuté, takže kopii nemůže obsluhovat
    jiné vlákno). Živá databáze se nemění a druhý zapisovatel nevzniká.
    Kopie se převede na samostatný soubor (`journal_mode = DELETE`) a dostane
    `meta.snapshot_utc`; do živého originálu se nikdy nic nezapisuje.
    """
    target_path = Path(target_path)
    try:
        target = sqlite3.connect(target_path, isolation_level=None)
    except sqlite3.Error as error:
        raise SnapshotError("copy", f"cílový soubor {target_path} nelze otevřít: {error}") from None
    try:
        connection.backup(target, pages=pages, progress=progress)
        if target.execute("PRAGMA journal_mode = delete").fetchone()[0] != "delete":
            raise SnapshotError("copy", "kopii nelze převést na samostatný soubor (journal_mode)")
        target.execute("INSERT INTO meta (key, value) VALUES ('snapshot_utc', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(snapshot_utc),))
    except sqlite3.Error as error:
        raise SnapshotError("copy", f"snímek nelze vytvořit: {error}") from None
    finally:
        target.close()
    return target_path


def verify_database(path, *, role=None, host_id=None, snapshot_utc=None):
    """Úplná kontrola samostatného souboru snímku; vrací údaje pro záznam.

    Ověří: žádné pomocné soubory, identitu a schéma veřejným ověřením historie
    (`application_id`, `user_version`, otisk schématu, role a stroj),
    `integrity_check`, `foreign_key_check`, `journal_mode`, `meta.snapshot_utc`,
    přítomnost očekávaných tabulek, počty řádků, velikost a SHA-256.
    """
    path = Path(path)
    companions = [Path(f"{path}{suffix}") for suffix in history.COMPANION_SUFFIXES]
    for companion in companions:
        if os.path.lexists(companion):
            raise SnapshotError("verify", f"vedle snímku zůstal {companion.name}; kopie není samostatná")
    try:
        with history.open_reader(path, role, host_id):
            pass  # identita, verze schématu a meta: stejné kontroly jako u živé databáze
    except history.HistoryError as error:
        raise SnapshotError("verify", f"snímek neprošel ověřením identity: {error}") from None
    for companion in companions:
        if os.path.lexists(companion):
            raise SnapshotError("verify", f"ověření vytvořilo {companion.name}; snímek není samostatný")
    conn = _standalone(path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SnapshotError("verify", f"snímek neprošel integrity_check: {integrity}")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SnapshotError("verify", f"snímek má {len(violations)} porušení cizích klíčů")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode != "delete":
            raise SnapshotError("verify", f"snímek má journal_mode {mode}, očekáváno delete")
        stored = dict(conn.execute("SELECT key, value FROM meta"))
        if "snapshot_utc" not in stored:
            raise SnapshotError("verify", "snímek nemá meta.snapshot_utc")
        moment = int(stored["snapshot_utc"])
        if snapshot_utc is not None and moment != snapshot_utc:
            raise SnapshotError("verify", f"meta.snapshot_utc je {moment}, očekáváno {snapshot_utc}")
        rows = {}
        for table in history.SCHEMA_TABLES:
            rows[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    except sqlite3.Error as error:
        raise SnapshotError("verify", f"snímek nelze ověřit: {error}") from None
    finally:
        conn.close()
    return {"snapshot_utc": moment, "rows": rows, "journal_mode": mode, "integrity": integrity,
            "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def write_record(work_dir, record):
    """Zapíše JSON záznam generace a synchronizuje ho na disk."""
    path = Path(work_dir) / SNAPSHOT_RECORD
    try:
        path.write_text(record.to_json(), encoding="utf-8")
        storage.fsync_file(path)
    except OSError as error:
        raise SnapshotError("record", f"záznam {path} nelze zapsat: {error}") from None
    return path


def check_record(work_dir, record):
    """Porovná záznam se skutečným souborem snímku těsně před zveřejněním."""
    database = Path(work_dir) / record.database
    actual_bytes, actual_sha = database.stat().st_size, sha256_file(database)
    if (actual_bytes, actual_sha) != (record.bytes, record.sha256):
        raise SnapshotError("record", f"záznam neodpovídá souboru snímku ({actual_bytes} B / {actual_sha[:12]}… "
                                      f"proti {record.bytes} B / {record.sha256[:12]}…)")
    stored = SnapshotRecord.from_json((Path(work_dir) / SNAPSHOT_RECORD).read_text(encoding="utf-8"))
    if stored != record:
        raise SnapshotError("record", "zapsaný záznam se liší od ověřeného")


def publish(work_dir, published_root, name):
    """Zveřejní hotovou generaci jediným atomickým přejmenováním; vrací cílovou cestu.

    Používá `renameat2(RENAME_NOREPLACE)`, takže **žádný** existující cílový
    objekt se nepřepíše: ani platná generace, ani prázdný adresář, ani soubor
    nebo symlink, a to i když cíl vznikne až po předběžné kontrole (kontrola
    existence sama proti souběhu nechrání). Není-li příznak k dispozici, nic se
    nezveřejní; na nechráněné `os.rename()` se nikdy nepřechází. Po přejmenování
    se synchronizují oba nadřazené adresáře, aby publikace přežila výpadek napájení.
    """
    work_dir, published_root = Path(work_dir), Path(published_root)
    target = published_root / name
    storage.fsync_directory(work_dir)  # obě položky generace jsou na disku
    try:
        storage.rename_noreplace(work_dir, target)
    except OSError as error:
        if error.errno == errno.EEXIST:
            raise SnapshotError("exists", f"generace {name} už existuje; nic se nepřepisuje") from None
        if error.errno in storage.UNSUPPORTED_ERRNO:
            raise SnapshotError("publish", f"atomické zveřejnění bez přepsání cíle není k dispozici "
                                           f"({error}); generace {name} se nezveřejňuje") from None
        raise SnapshotError("publish", f"generaci {name} nelze zveřejnit: {error}") from None
    storage.fsync_directory(published_root)
    storage.fsync_directory(work_dir.parent)
    return target


def create(connection, work_root, published_root, snapshot_utc, *, app_version, role=None, host_id=None,
           pages=SNAPSHOT_PAGES, progress=None, mounts=None):
    """Vytvoří, ověří a zveřejní jednu generaci snímku; vrací Generation.

    Při jakékoli chybě se zveřejní nic a předchozí generace zůstanou beze změny;
    rozpracovaná generace se uklidí. Nepřepisuje živou databázi ani jiné generace.
    """
    work, published = prepare(work_root, published_root, mounts)
    name = generation_name(snapshot_utc)
    if os.path.lexists(published / name):
        raise SnapshotError("exists", f"generace {name} už existuje; nic se nepřepisuje")
    cleanup_work(work)
    work_dir = work / f"{WORK_PREFIX}{name}{WORK_SUFFIX}"
    if os.path.lexists(work_dir):
        raise SnapshotError("exists", f"rozpracovaná generace {work_dir.name} už existuje")
    work_dir.mkdir()
    try:
        database = copy_database(connection, work_dir / SNAPSHOT_DB, snapshot_utc, pages, progress)
        checked = verify_database(database, role=role, host_id=host_id, snapshot_utc=snapshot_utc)
        record = SnapshotRecord(name, snapshot_utc, history.SCHEMA_VERSION, role or "", host_id or "",
                                SNAPSHOT_DB, checked["bytes"], checked["sha256"],
                                MappingProxyType(dict(checked["rows"])), sqlite3.sqlite_version, app_version)
        storage.fsync_file(database)
        write_record(work_dir, record)
        check_record(work_dir, record)
    except BaseException:
        cleanup_work(work)  # jen vlastní rozpracovaná generace; zveřejněné se nedotkne
        raise
    publish(work_dir, published, name)
    return Generation(name, published / name, record)


def generations(published_root):
    """Jména zveřejněných generací vzestupně (bez ověřování obsahu)."""
    root = Path(published_root)
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir()
                  if entry.is_dir() and not entry.is_symlink() and GENERATION_NAME.fullmatch(entry.name))


def load_generation(path):
    """Načte generaci z adresáře a zkontroluje, že záznam odpovídá souboru.

    Kontroluje se jméno, hlavička SQLite, velikost a SHA-256; úplnou integritu
    ověřuje až test obnovitelnosti (`check_restore`).
    """
    path = Path(path)
    name = path.name
    if not GENERATION_NAME.fullmatch(name):
        raise SnapshotError("record", f"{name} není jméno generace")
    record_path, database = path / SNAPSHOT_RECORD, path / SNAPSHOT_DB
    for required in (record_path, database):
        if not required.is_file():
            raise SnapshotError("record", f"generace {name} nemá {required.name}")
    try:
        record = SnapshotRecord.from_json(record_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SnapshotError("record", f"záznam generace {name} nelze přečíst: {error}") from None
    if record.generation != name:
        raise SnapshotError("record", f"záznam generace {name} hlásí jméno {record.generation}")
    if record.database != SNAPSHOT_DB:
        raise SnapshotError("record", f"záznam generace {name} hlásí jiný soubor {record.database}")
    with open(database, "rb") as file:
        if file.read(len(history.SQLITE_MAGIC)) != history.SQLITE_MAGIC:
            raise SnapshotError("record", f"snímek generace {name} není databáze SQLite")
    actual = database.stat().st_size
    if actual != record.bytes:
        raise SnapshotError("record", f"snímek generace {name} má {actual} B, záznam {record.bytes} B")
    digest = sha256_file(database)
    if digest != record.sha256:
        raise SnapshotError("record", f"snímek generace {name} má jiný SHA-256 než záznam")
    return Generation(name, path, record)


def latest_valid(published_root):
    """Poslední prokazatelně platná generace, nebo None; novější vadné se přeskočí."""
    root = Path(published_root)
    for name in reversed(generations(root)):
        try:
            return load_generation(root / name)
        except SnapshotError:
            continue
    return None


def check_restore(generation_path, *, role=None, host_id=None):
    """Test obnovitelnosti jedné generace: jen čtení, nikdy živá databáze.

    Ověří záznam, SHA-256, identitu a schéma, `integrity_check`,
    `foreign_key_check`, `journal_mode`, `meta.snapshot_utc`, počty řádků proti
    záznamu a spustí reprezentativní čtecí dotazy. Nic neobnovuje ani nemění.
    """
    path = Path(generation_path)
    try:
        generation = load_generation(path)
    except SnapshotError as error:
        return RestoreReport(path.name, False, problems=(str(error),))
    record = generation.record
    try:
        checked = verify_database(generation.database, role=role, host_id=host_id,
                                  snapshot_utc=record.snapshot_utc)
    except SnapshotError as error:
        return RestoreReport(record.generation, False, snapshot_utc=record.snapshot_utc,
                             bytes=record.bytes, sha256=record.sha256, problems=(str(error),))
    problems = []
    if dict(checked["rows"]) != dict(record.rows):
        problems.append(f"počty řádků se liší od záznamu: {checked['rows']} proti {dict(record.rows)}")
    if record.schema_version != history.SCHEMA_VERSION:
        problems.append(f"záznam hlásí schéma v{record.schema_version}, tento kód zná v{history.SCHEMA_VERSION}")
    counts = {}
    conn = _standalone(generation.database)
    try:
        for name, sql in RESTORE_QUERIES.items():
            counts[name] = len(conn.execute(sql).fetchall())
    except sqlite3.Error as error:
        problems.append(f"čtecí dotaz {name} selhal: {error}")
    finally:
        conn.close()
    for companion in history.COMPANION_SUFFIXES:
        if os.path.lexists(f"{generation.database}{companion}"):
            problems.append(f"čtení vytvořilo {companion}; snímek není samostatný")
    return RestoreReport(record.generation, not problems, record.snapshot_utc, record.bytes, record.sha256,
                         MappingProxyType(dict(checked["rows"])), checked["integrity"], 0,
                         checked["journal_mode"], MappingProxyType(counts), tuple(problems))
