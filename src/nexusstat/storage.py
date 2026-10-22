"""Kontroly úložiště pro databázi historie a její snímky (jen čtení a stat).

Rozhoduje skutečné místo souboru, ne zápis cesty: cesty se rozvíjejí
(`os.path.realpath`), takže symlink v poslední části ani v nadřazeném adresáři
nepropašuje data na jiné úložiště. Odmítá se síťové úložiště (NAS, CIFS, NFS,
9p, sshfs…), `autofs` i stav, kdy úložiště **ověřit nelze**: raději se nezapíše
nic, než aby historie skončila na NAS (docs/HISTORY.md 11.5).

Modul je záměrně bez závislosti na ostatních modulech historie, aby ho mohl
použít jak zapisovač, tak snímky.
"""

import ctypes
import ctypes.util
import errno
import functools
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

MOUNTS_PATH = "/proc/self/mounts"

# renameat2(RENAME_NOREPLACE): atomické přejmenování, které **nikdy** nepřepíše
# existující cílovou cestu. Obyčejné os.rename() prázdný cílový adresář
# atomicky nahradí a kontrola existence před ním není ochrana proti souběhu.
RENAME_NOREPLACE = 1
AT_FDCWD = -100
# Chyby, které znamenají „jádro nebo souborový systém příznak nezná“: volající
# musí operaci odmítnout, nikdy tiše použít nechráněné os.rename().
UNSUPPORTED_ERRNO = frozenset({errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP})

# Síťová úložiště, na která databáze historie ani její snímky nepatří: SQLite
# tam nemá spolehlivé zámky ani fsync a připojení může zmizet uprostřed zápisu.
NETWORK_FILESYSTEMS = frozenset({
    "cifs", "smb2", "smb3", "smbfs", "nfs", "nfs3", "nfs4", "afs", "ceph", "glusterfs",
    "9p", "ncpfs", "lustre", "beegfs", "orangefs", "gfs2", "ocfs2",
    "sshfs", "davfs", "webdav", "s3fs", "rclone", "curlftpfs", "smbnetfs",
})

# Typy připojení, u kterých cílové úložiště dopředu ověřit nejde: autofs připojí
# cíl až při prvním přístupu a bývá to NFS nebo CIFS.
UNVERIFIABLE_FILESYSTEMS = frozenset({"autofs"})


class StorageError(Exception):
    """Cesta se pro data historie nesmí použít; nic se přitom nezměnilo."""


def _unescape(field):
    # /proc/self/mounts kóduje mezeru, tabulátor, nový řádek a zpětné lomítko osmičkově.
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)


def resolve_path(path):
    """Cesta po rozvinutí symlinků a odstranění `.` a `..` (`os.path.realpath`).

    Neexistující poslední část se rozvine podle existující předpony.
    """
    return PurePosixPath(os.path.realpath(path))


def filesystem_type(path, mounts=None):
    """Typ souborového systému, na kterém cesta skutečně leží.

    Rozhoduje nejdelší bod připojení, který je předponou rozvinuté cesty; při
    stejně dlouhém bodu platí poslední záznam, protože pozdější připojení
    překryje starší. None = typ nelze zjistit (nečitelná tabulka připojení,
    žádný odpovídající bod); volající pak zápis odmítne. Tabulka se čte až tady,
    protože se za běhu mění.
    """
    try:
        lines = Path(mounts or MOUNTS_PATH).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    target = resolve_path(path)
    found = None
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue  # neúplný řádek: z takové tabulky se nic nedovozuje
        point = PurePosixPath(_unescape(fields[1]))
        # Porovnávají se části cesty, ne text: /srv/nas-zaloha neleží pod /srv/nas.
        if target != point and point not in target.parents:
            continue
        if found is None or len(point.parts) >= len(found[0].parts):
            found = (point, _unescape(fields[2]))
    return None if found is None else found[1]


def is_network_filesystem(kind):
    name = kind.split(".", 1)[1] if kind.startswith("fuse.") else kind
    return name in NETWORK_FILESYSTEMS


def check_local_path(path, mounts=None):
    """Povolí jen cestu na doloženě lokálním úložišti; vrací jeho typ.

    Odmítne síťové úložiště, `autofs` (cíl se připojí až při přístupu) a stejně
    tak stav, kdy úložiště ověřit nelze (nečitelná nebo neúplná tabulka
    připojení, cesta mimo všechny body připojení). Kontroluje se při každém
    použití, protože připojení se za běhu mění.
    """
    kind = filesystem_type(path, mounts)
    if kind is None:
        raise StorageError(f"úložiště cesty {path} nelze ověřit (tabulka připojení); nic se nezapisuje")
    if is_network_filesystem(kind):
        raise StorageError(f"{path} leží na síťovém souborovém systému {kind}; data historie se tam nezapisují")
    if kind in UNVERIFIABLE_FILESYSTEMS:
        raise StorageError(f"{path} leží pod {kind}; cílové úložiště se připojí až při přístupu a nelze ho ověřit")
    return kind


def check_directory(path, purpose, mounts=None):
    """Existující skutečný adresář na lokálním úložišti; vrací jeho st_dev.

    Adresář se nikdy nevytváří (to je věc správce) a symlink se odmítá: u
    publikace generace musí být jisté, kam přejmenování povede.
    """
    path = Path(path)
    if path.is_symlink():
        raise StorageError(f"{purpose} {path} je symlink; cesta musí být skutečný adresář")
    if not path.is_dir():
        raise StorageError(f"{purpose} {path} neexistuje nebo není adresář; vytvořit ho musí správce")
    check_local_path(path, mounts)
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
