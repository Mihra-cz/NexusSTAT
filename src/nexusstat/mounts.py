"""Read-only inventura připojených úložišť z /proc/self/mountinfo (M5-S1).

Použití (ruční výpis tohoto počítače, nic se nezapisuje):
  PYTHONPATH=src python3 -m nexusstat.mounts

Čte se jen tabulka jádra `/proc/self/mountinfo`. Žádný bod připojení se
neotevírá ani neprochází (žádné stat, statvfs, realpath, resolve ani listdir),
takže se zaseknutý síťový mount nemůže projevit a autofs nic nepřipojí. Síť se
nepoužívá. Záznam v tabulce dokládá jen to, že jádro mount eviduje, **ne že je
úložiště dostupné, čitelné nebo zapisovatelné**.

Každý mount se zařadí podle typu souborového systému:

* `local`: souborový systém na blokovém zařízení nebo obrazu (ext4, btrfs, vfat…),
* `network`: síťové úložiště (CIFS, NFS, sshfs…; tentýž seznam používá storage.py),
* `virtual`: systémový nebo paměťový souborový systém (proc, sysfs, tmpfs, overlay…),
* `autofs`: bod automatického připojení; skutečný cíl se připojí až při přístupu,
* `unknown`: jiný typ; nikdy se nepovažuje za lokální.

Modul zatím není napojený na měřicí smyčku ani OLED.
"""

import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

MOUNTINFO_PATH = "/proc/self/mountinfo"
# Běžná tabulka má jednotky až desítky KiB (i s Dockerem a snapy); větší vstup
# je neočekávaný a dál se nečte.
MAX_MOUNTINFO_BYTES = 4 * 1024 * 1024

# Síťová úložiště, na která databáze historie ani její snímky nepatří (storage.py):
# SQLite tam nemá spolehlivé zámky ani fsync a připojení může zmizet uprostřed zápisu.
NETWORK_FILESYSTEMS = frozenset({
    "cifs", "smb2", "smb3", "smbfs", "nfs", "nfs3", "nfs4", "afs", "ceph", "glusterfs",
    "9p", "ncpfs", "lustre", "beegfs", "orangefs", "gfs2", "ocfs2",
    "sshfs", "davfs", "webdav", "s3fs", "rclone", "curlftpfs", "smbnetfs",
})

# Typy připojení, u kterých cílové úložiště dopředu ověřit nejde: autofs připojí
# cíl až při prvním přístupu a bývá to NFS nebo CIFS.
UNVERIFIABLE_FILESYSTEMS = frozenset({"autofs"})

LOCAL = "local"
NETWORK = "network"
VIRTUAL = "virtual"
AUTOFS = "autofs"
UNKNOWN = "unknown"
KINDS = (LOCAL, NETWORK, VIRTUAL, AUTOFS, UNKNOWN)

# Souborové systémy s vlastním úložištěm na tomto počítači (blokové zařízení,
# obraz na loop zařízení nebo ZFS pool). fuseblk je FUSE nad blokovým zařízením.
LOCAL_FILESYSTEMS = frozenset({
    "ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "jfs", "reiserfs", "bcachefs", "nilfs2", "zfs",
    "vfat", "msdos", "exfat", "ntfs", "ntfs3", "fuseblk", "hfs", "hfsplus", "iso9660", "udf",
    "squashfs", "erofs", "ubifs",
})
# Systémové a paměťové souborové systémy bez vlastního trvalého úložiště.
# overlay (např. Docker) jen skládá adresáře jiného souborového systému.
VIRTUAL_FILESYSTEMS = frozenset({
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "ramfs", "cgroup", "cgroup2", "securityfs",
    "debugfs", "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs", "pstore", "bpf", "efivarfs",
    "binfmt_misc", "nsfs", "rpc_pipefs", "nfsd", "selinuxfs", "overlay",
})

# Jádro v cestách a zdroji kóduje mezeru, tabulátor, nový řádek a zpětné lomítko
# osmičkově (\040, \011, \012, \134). Jiné zpětné lomítko je chybný vstup.
ESCAPE = re.compile(rb"\\([0-3][0-7]{2})")
DEVICE = re.compile(rb"[0-9]+:[0-9]+")
# Volitelná pole (shared:N, master:N, propagate_from:N, unbindable) se nečtou:
# podle proc_pid_mountinfo(5) má parser neznámá pole ignorovat.
SEPARATOR = b"-"
FIELDS_BEFORE_OPTIONAL = 6
FIELDS_AFTER_SEPARATOR = 3


class MountInfoError(ValueError):
    """Tabulku připojení nejde věrohodně přečíst."""


def is_network_filesystem(kind):
    name = kind.split(".", 1)[1] if kind.startswith("fuse.") else kind
    return name in NETWORK_FILESYSTEMS


def kind_of(fstype):
    """Druh úložiště podle typu souborového systému (fuse.sshfs = sshfs)."""
    if fstype in UNVERIFIABLE_FILESYSTEMS:
        return AUTOFS
    if is_network_filesystem(fstype):
        return NETWORK
    if fstype in VIRTUAL_FILESYSTEMS:
        return VIRTUAL
    if fstype in LOCAL_FILESYSTEMS:
        return LOCAL
    return UNKNOWN


@dataclass(frozen=True)
class Mount:
    """Jeden řádek mountinfo. `source` může obsahovat adresu NAS: jen pro paměť, nevypisovat."""

    mount_id: int
    parent_id: int
    device: str  # major:minor
    root: str  # kořen připojení uvnitř souborového systému (bind mount, podsvazek btrfs)
    mount_point: str
    read_only: bool
    fstype: str
    source: str

    @property
    def kind(self):
        return kind_of(self.fstype)


def unescape(field):
    """Pole mountinfo jako text: osmičkové sekvence na bajty, pak jako název souboru."""
    if b"\\" in ESCAPE.sub(b"", field):
        raise MountInfoError("neplatná escape sekvence")
    # Bajty mimo UTF-8 se zachovají přes surrogateescape jako u os.listdir().
    return os.fsdecode(ESCAPE.sub(lambda match: bytes([int(match[1], 8)]), field))


def parse_line(line):
    """Mount z jednoho řádku (bez znaku konce řádku); MountInfoError u chybného tvaru."""
    fields = line.split(b" ")
    if len(fields) < FIELDS_BEFORE_OPTIONAL + 1 + FIELDS_AFTER_SEPARATOR or b"" in fields or b"\0" in line:
        raise MountInfoError("neúplný řádek")
    try:
        separator = fields.index(SEPARATOR, FIELDS_BEFORE_OPTIONAL)
    except ValueError:
        raise MountInfoError("chybí oddělovač") from None
    if len(fields) - separator - 1 != FIELDS_AFTER_SEPARATOR:
        raise MountInfoError("neočekávaný počet polí za oddělovačem")
    mount_id, parent_id, device, root, point, options = fields[:FIELDS_BEFORE_OPTIONAL]
    if not (mount_id.isdigit() and parent_id.isdigit() and DEVICE.fullmatch(device)):
        raise MountInfoError("neplatné číslo připojení nebo zařízení")
    mount_point = unescape(point)
    if not mount_point.startswith("/"):
        raise MountInfoError("bod připojení není absolutní cesta")
    fstype, source, _super_options = fields[separator + 1:]
    return Mount(int(mount_id), int(parent_id), device.decode("ascii"), unescape(root), mount_point,
                 options.split(b",")[0] == b"ro", unescape(fstype), unescape(source))


def lexical_path(path):
    """Absolutní cesta pro porovnání s body připojení, bez přístupu k ní.

    Nerozvíjí se (symlinky, `..`): to by znamenalo sáhnout na úložiště. Cesta
    s `..` je proto nejednoznačná a odmítne se.
    """
    text = os.fspath(path)
    if not text.startswith("/"):
        raise ValueError(f"cesta musí být absolutní a bez '..', ne {text!r}")
    # PurePosixPath ponechá úvodní "//" jako zvláštní kořen (POSIX); Linux ho
    # bere jako "/", stejně jako jádro v tabulce připojení.
    target = PurePosixPath("/" + text.lstrip("/"))
    if ".." in target.parts:
        raise ValueError(f"cesta musí být absolutní a bez '..', ne {text!r}")
    return target


@dataclass(frozen=True)
class MountTable:
    """Přečtená tabulka připojení; `invalid_lines` jsou čísla řádků (od 1), které nešlo přečíst."""

    mounts: tuple
    invalid_lines: tuple = ()

    @property
    def complete(self):
        """True, když se přečetl každý řádek; neúplná tabulka nic nevylučuje."""
        return not self.invalid_lines

    def by_kind(self):
        """Mounty podle druhu úložiště v pořadí tabulky; každý druh je uvedený, i prázdný."""
        return {kind: tuple(mount for mount in self.mounts if mount.kind == kind) for kind in KINDS}

    def stack(self, path):
        """Viditelné mounty na nejhlubším bodu připojení cesty, odspodu nahoru; [] = nelze určit.

        Cesta se lexikálně prochází od kořene jako v jádře (proc_pid_mountinfo(5)):
        na každém bodu připojení platí jen mount, jehož rodič je právě viditelný
        mount, a nad ním mounty, jejichž rodičem je ten předchozí (např. CIFS nad
        autofs). Mount pod skrytým nadřazeným mountem se tak neuplatní: když je
        přes /mnt připojený CIFS, starší /mnt/data pod ním není vidět. Porovnávají
        se části cesty, ne text (/mnt/nas2 neleží pod /mnt/nas). Neúplná tabulka,
        chybějící kořen i nejednoznačný zásobník dají [], protože by nic nevylučovaly.
        """
        target = lexical_path(path)
        if not self.complete:
            return []
        chain = self._chain_at(PurePosixPath("/"), None)
        point = PurePosixPath("/")
        for part in target.parts[1:]:
            if not chain:
                return []
            point = point / part
            above = self._chain_at(point, chain[-1].mount_id)
            if above is None:
                return []
            if above:
                chain = above
        return chain or []

    def covering(self, path):
        """Mount, který cestu skutečně obsluhuje (horní na nejhlubším bodu), nebo None."""
        stack = self.stack(path)
        return stack[-1] if stack else None


    def _chain_at(self, point, parent_id):
        """Mounty na bodu `point` nad mountem `parent_id`, odspodu nahoru; None = nejednoznačné.

        U kořene (parent_id None) je spodní ten, jehož rodič v tabulce na "/" není
        nebo je jím sám (kořen jmenného prostoru, proc_pid_mountinfo(5)).
        """
        here = [mount for mount in self.mounts if PurePosixPath(mount.mount_point) == point]
        if parent_id is None:
            ids = {mount.mount_id for mount in here}
            above = [mount for mount in here if mount.parent_id == mount.mount_id or mount.parent_id not in ids]
        else:
            above = [mount for mount in here if mount.parent_id == parent_id]
        chain = []
        while above:
            if len(above) != 1:
                return None
            chain.append(above[0])
            above = [mount for mount in here if mount.parent_id == chain[-1].mount_id and mount not in chain]
        return chain


def parse_mountinfo(data):
    """MountTable z obsahu mountinfo (bytes). Chybné řádky se přeskočí a zapíšou do invalid_lines.

    Za chybný se považuje i řádek s číslem připojení, které už v tabulce bylo:
    z takové tabulky nejde jednoznačně určit pořadí připojení.
    """
    if not isinstance(data, bytes):
        raise TypeError("mountinfo se čte jako bytes")
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()  # poslední řádek končí znakem nového řádku
    mounts, invalid, seen = [], [], set()
    for number, line in enumerate(lines, start=1):
        try:
            mount = parse_line(line)
        except MountInfoError:
            invalid.append(number)
            continue
        if mount.mount_id in seen:
            invalid.append(number)
            continue
        seen.add(mount.mount_id)
        mounts.append(mount)
    return MountTable(tuple(mounts), tuple(invalid))


def read_mountinfo(path=MOUNTINFO_PATH):
    """MountTable z tabulky jádra. Čte jen tento soubor; OSError a MountInfoError propustí volajícímu."""
    with open(path, "rb") as file:
        data = file.read(MAX_MOUNTINFO_BYTES + 1)
    if len(data) > MAX_MOUNTINFO_BYTES:
        raise MountInfoError("neočekávaně dlouhá tabulka připojení")
    return parse_mountinfo(data)


def main():
    """Výpis druhů úložišť tohoto počítače. Zdroj se nevypisuje (může obsahovat adresu NAS)."""
    table = read_mountinfo()
    for kind, mounts in table.by_kind().items():
        print(f"{kind}: {len(mounts)}")
        for mount in mounts:
            mode = "ro" if mount.read_only else "rw"
            print(f"  {mount.fstype:<12} {mode}  {mount.mount_point!r}")
    if not table.complete:
        print(f"Nečitelné řádky: {', '.join(map(str, table.invalid_lines))}")


if __name__ == "__main__":
    main()
