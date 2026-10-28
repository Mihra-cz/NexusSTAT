"""Kontroly úložiště pro databázi historie a její snímky (jen čtení a stat).

Rozhoduje skutečné místo souboru, ne zápis cesty: cesta se rozvíjí (symlinky,
`.`, `..`), takže symlink v poslední části ani v nadřazeném adresáři
nepropašuje data na jiné úložiště. Úložiště určuje viditelný mount z tabulky
jádra `/proc/self/mountinfo` (mounts.py), ne nejdelší bod připojení: mount
skrytý pozdějším překryvem nadřazeného bodu se neuplatní. Povoleno je jen
doloženě lokální úložiště (druh `local` z mounts.py); odmítá se síťové (NAS,
CIFS, NFS, 9p, sshfs…), `autofs`, virtuální (tmpfs, ramfs, overlay, proc…),
neznámý typ i stav, kdy úložiště **ověřit nelze** (neúplná nebo nejednoznačná
tabulka): raději se nezapíše nic, než aby historie skončila na NAS nebo v paměti
(docs/HISTORY.md 11.5). Název typu souborového systému ale není důkaz
fyzického umístění (např. ext4 na síťovém blokovém zařízení).

Modul je záměrně bez závislosti na ostatních modulech historie, aby ho mohl
použít jak zapisovač, tak snímky.
"""

import ctypes
import ctypes.util
import errno
import functools
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath

# Klasifikace typů je společná s inventurou úložišť (mounts.py); jména zůstávají
# dostupná i odsud, aby se volajícím nic nezměnilo.
from .mounts import (  # noqa: F401
    LOCAL,
    MOUNTINFO_PATH,
    NETWORK_FILESYSTEMS,
    UNVERIFIABLE_FILESYSTEMS,
    MountInfoError,
    is_network_filesystem,
    kind_of,
    read_mountinfo,
)

# Tabulka připojení pro kontrolu cest; od opravy viditelného mountu formát
# /proc/self/mountinfo (dřív /proc/self/mounts). Testy podstrkují vlastní soubor.
MOUNTS_PATH = MOUNTINFO_PATH
# Druhy úložiště, na které se smí zapisovat živá historie a její snímky: jen
# lokální souborový systém. Virtuální druh je legitimní pro inventuru, ale pro
# SQLite ne: tmpfs a ramfs nepřežijí restart, u overlay není podkladové úložiště
# doložené a proc, sysfs a další pseudo-FS nejsou úložiště. Neznámý typ se nikdy
# nepovažuje za lokální.
ALLOWED_KINDS = frozenset({LOCAL})
# Nejvýš tolik symlinků při rozvíjení jedné cesty, jako dovolí jádro (ELOOP).
MAX_SYMLINKS = 40

# renameat2(RENAME_NOREPLACE): atomické přejmenování, které **nikdy** nepřepíše
# existující cílovou cestu. Obyčejné os.rename() prázdný cílový adresář
# atomicky nahradí a kontrola existence před ním není ochrana proti souběhu.
RENAME_NOREPLACE = 1
AT_FDCWD = -100
# Chyby, které znamenají „jádro nebo souborový systém příznak nezná“: volající
# musí operaci odmítnout, nikdy tiše použít nechráněné os.rename().
UNSUPPORTED_ERRNO = frozenset({errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP})


class StorageError(Exception):
    """Cesta se pro data historie nesmí použít; nic se přitom nezměnilo."""


def resolve_path(path):
    """Cesta po rozvinutí symlinků a odstranění `.` a `..` (`os.path.realpath`).

    Neexistující poslední část se rozvine podle existující předpony.
    """
    return PurePosixPath(os.path.realpath(path))


def _mount_table(mounts):
    """Úplná tabulka připojení, nebo None: nečitelná, příliš dlouhá i neúplná tabulka nic nevylučuje."""
    try:
        table = read_mountinfo(mounts or MOUNTS_PATH)
    except (OSError, MountInfoError):
        return None
    return table if table.complete else None


def filesystem_type(path, mounts=None):
    """Typ souborového systému viditelného mountu, na kterém cesta skutečně leží.

    Cesta se rozvíjí po částech jako `os.path.realpath` (symlinky, `.`, `..`;
    neexistující zbytek lexikálně), ale každá část se nejdřív lexikálně najde
    v tabulce připojení (mounts.MountTable.covering). Na úložiště, které se
    nesmí použít (síťové, autofs, virtuální, neznámé), se tak kontrola nedotkne
    ani jedním lstat: vrátí rovnou jeho typ. Cesta, která takovým mountem jen
    prochází, se tím odmítne také. Jinak by už samotné rozvíjení cesty
    sáhlo na NAS (a mohlo se zaseknout) nebo spustilo autofs.
    None = typ nelze zjistit (nečitelná, neúplná nebo nejednoznačná tabulka,
    cesta bez viditelného mountu, smyčka symlinků); volající pak zápis odmítne.
    Tabulka se čte až tady, protože se za běhu mění.
    """
    table = _mount_table(mounts)
    if table is None:
        return None
    text = os.fspath(path)
    if not text.startswith("/"):
        text = os.path.join(os.getcwd(), text)
    pending = list(PurePosixPath("/" + text.lstrip("/")).parts[1:])
    current, links = PurePosixPath("/"), 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            current = current.parent  # current je už rozvinutý, takže stačí lexikálně
            continue
        candidate = current / part
        mount = table.covering(candidate)
        if mount is None or mount.kind not in ALLOWED_KINDS:
            return None if mount is None else mount.fstype
        try:
            is_link = stat.S_ISLNK(os.lstat(candidate).st_mode)
        except OSError:
            is_link = False  # neexistující zbytek cesty se rozvine lexikálně
        if not is_link:
            current = candidate
            continue
        links += 1
        if links > MAX_SYMLINKS:
            return None
        try:
            target = os.readlink(candidate)
        except OSError:
            return None
        if target.startswith("/"):
            current, target = PurePosixPath("/"), target.lstrip("/")
        # Cíl symlinku se zpracuje stejným postupem, takže se ověří každá jeho část.
        pending[:0] = PurePosixPath(target).parts
    mount = table.covering(current)
    return None if mount is None else mount.fstype


def check_local_path(path, mounts=None):
    """Povolí jen cestu na doloženě lokálním úložišti (druh `local`); vrací jeho typ.

    Odmítne síťové úložiště, `autofs` (cíl se připojí až při přístupu),
    virtuální souborový systém (tmpfs, ramfs, overlay, proc…), neznámý typ
    a stejně tak stav, kdy úložiště ověřit nelze
    (nečitelná, neúplná nebo nejednoznačná tabulka připojení, cesta bez
    viditelného mountu). Kontroluje se při každém použití, protože připojení se
    za běhu mění. Typ je to, co hlásí jádro; o fyzickém umístění zařízení
    (např. síťový blokový disk) nic nedokládá.
    """
    kind = filesystem_type(path, mounts)
    if kind is None:
        raise StorageError(f"úložiště cesty {path} nelze ověřit (tabulka připojení); nic se nezapisuje")
    if is_network_filesystem(kind):
        raise StorageError(f"{path} leží na síťovém souborovém systému {kind}; data historie se tam nezapisují")
    if kind in UNVERIFIABLE_FILESYSTEMS:
        raise StorageError(f"{path} leží pod {kind}; cílové úložiště se připojí až při přístupu a nelze ho ověřit")
    if kind_of(kind) not in ALLOWED_KINDS:
        raise StorageError(f"{path} leží na souborovém systému {kind}, který není doloženě lokální trvalé "
                           "úložiště; nic se nezapisuje")
    return kind


def check_directory(path, purpose, mounts=None):
    """Existující skutečný adresář na lokálním úložišti; vrací jeho st_dev.

    Adresář se nikdy nevytváří (to je věc správce) a symlink se odmítá: u
    publikace generace musí být jisté, kam přejmenování povede. Úložiště se
    ověří jako první, aby se na síťové úložiště ani autofs nesáhlo ani kvůli
    zjištění, zda adresář existuje.
    """
    path = Path(path)
    check_local_path(path, mounts)
    if path.is_symlink():
        raise StorageError(f"{purpose} {path} je symlink; cesta musí být skutečný adresář")
    if not path.is_dir():
        raise StorageError(f"{purpose} {path} neexistuje nebo není adresář; vytvořit ho musí správce")
    return os.stat(path).st_dev


def same_filesystem(first, second):
    """True, když oba adresáře leží na stejném souborovém systému (stejné st_dev).

    Atomické přejmenování (publikace generace) jinak selže na EXDEV, takže se
    rozdíl musí poznat dřív, než se začne stavět snímek.
    """
    return os.stat(first).st_dev == os.stat(second).st_dev


@functools.lru_cache(maxsize=1)
def _renameat2():
    """Funkce renameat2 z libc, nebo None, když ji použít nejde."""
    try:
        library = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        function = library.renameat2
    except (OSError, AttributeError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    return function


def rename_noreplace(source, target):
    """Atomicky přejmenuje `source` na `target`, **nikdy** nepřepíše existující cíl.

    Na rozdíl od `os.rename()` odmítne i prázdný cílový adresář, obyčejný soubor
    i symlink, a nemá okno mezi kontrolou a přejmenováním. OSError s EEXIST =
    cíl existuje; OSError s ENOSYS, EINVAL nebo EOPNOTSUPP = jádro nebo
    souborový systém příznak nezná a volající musí operaci odmítnout.
    """
    function = _renameat2()
    if function is None:
        raise OSError(errno.ENOSYS, "renameat2 není v této libc dostupné")
    ctypes.set_errno(0)
    if function(AT_FDCWD, os.fsencode(str(source)), AT_FDCWD, os.fsencode(str(target)),
                RENAME_NOREPLACE) != 0:
        code = ctypes.get_errno()
        raise OSError(code, f"renameat2({source} → {target}): {os.strerror(code)}")
    return Path(target)


def supports_rename_noreplace(directory):
    """Zkusí RENAME_NOREPLACE v daném adresáři; True jen při doloženém chování.

    Ověří obojí: přejmenování na volný cíl projde a na existující cíl selže
    s EEXIST. Po sobě nic nenechá. Zkouší se v rozpracovaném adresáři, který
    leží na stejném souborovém systému jako zveřejňovaný, takže se do
    zálohovaného adresáře nic dočasného nepíše.
    """
    try:
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            source, free, taken = (Path(folder) / name for name in ("source", "free", "taken"))
            source.mkdir()
            taken.mkdir()
            rename_noreplace(source, free)  # volný cíl: musí projít
            try:
                rename_noreplace(free, taken)  # existující prázdný adresář: musí selhat
            except OSError as error:
                return error.errno == errno.EEXIST
            return False  # cíl se přepsal: ochrana nefunguje
    except OSError:
        return False


def fsync_file(path):
    """Zapíše obsah souboru na disk."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory(path):
    """Zapíše na disk položky adresáře (Linux: adresář jde otevřít jen pro čtení)."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
