"""Read-only identifikace blokového zařízení kořenového souborového systému (M5-S3).

Použití (ruční výpis tohoto počítače, nic se nezapisuje):
  PYTHONPATH=src python3 -m nexusstat.root_device

Funkce se volá jen na požádání, měřicí smyčka ji nespouští: zařízení ROOT se
mění velmi zřídka. Postup je jeden řetězec údajů jádra, žádný odhad:

1. `major:minor` viditelného mountu `/` z `/proc/self/mountinfo` (mounts.py),
2. odkaz `/sys/dev/block/<major:minor>` na zařízení v `/sys/devices/…`,
3. `uevent` zařízení (číslo, název, oddíl nebo disk) a u oddílu i jeho disku,
4. podsystém nadřazeného zařízení disku (`<disk>/device/subsystem`).

Doložené NVMe je jen disk, jehož nadřazené zařízení patří do podsystému jádra
`nvme` (řadič) nebo `nvme-subsystem` (nativní multipath). Název `/dev/nvme…`
ve zdroji mountu ani existence NVMe v počítači důkazem nejsou. Vrstvené
zařízení (device-mapper, RAID, loop), anonymní číslo zařízení (btrfs, overlay,
síť), chybějící nebo nekonzistentní údaje dají UNKNOWN s důvodem.

Čtou se jen odkazy a malé textové soubory sysfs (`readlink`, `uevent`).
Žádné `/dev/…` ani jiné blokové zařízení se neotevírá, nespouští se žádný
příkaz (smartctl, nvme-cli, lsblk, udevadm), síť se nepoužívá a souborové
systémy disků se neprocházejí. Výsledek nese jen jméno disku v jádře
(např. `nvme0n1`), sběrnici a rozložení; sériová čísla, identifikátory
ani cesty sysfs ne.
"""

import os
import re
from pathlib import PurePosixPath

from .core import State
from .mounts import MOUNTINFO_PATH, MountInfoError, read_mountinfo

SYS_DIR = "/sys"
NVME = "nvme"
# Podsystémy nadřazeného zařízení NVMe disku: řadič, nebo subsystém u nativního multipath.
NVME_SUBSYSTEMS = frozenset({"nvme", "nvme-subsystem"})
# Malé atributy sysfs; větší obsah je neočekávaný.
MAX_ATTRIBUTE_BYTES = 4096
# Jméno disku v jádře a podsystému: krátký text bez lomítek (do details i na displej).
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,31}")
LAYOUT_TEXT = {"partition": "oddíl disku", "disk": "celý disk"}
BUS, DISK, LAYOUT, CAUSE = "bus", "disk", "layout", "cause"


class Unverifiable(Exception):
    """Zařízení ROOT nejde doložit; první argument je důvod (cause), druhý text."""


def read_uevent(directory):
    """Klíče KEY=VALUE z `<directory>/uevent` (jen čtení malého souboru sysfs)."""
    try:
        with open(directory / "uevent", "rb") as file:
            data = file.read(MAX_ATTRIBUTE_BYTES + 1)
    except OSError:
        raise Unverifiable("no_sysfs", "údaje zařízení v sysfs chybí") from None
    if len(data) > MAX_ATTRIBUTE_BYTES or not data.isascii():
        raise Unverifiable("inconsistent", "neplatné údaje zařízení v sysfs")
    return dict(line.split("=", 1) for line in data.decode("ascii").splitlines() if "=" in line)


def device_directory(sys_dir, device):
    """Adresář zařízení v `/sys/devices` z odkazu `/sys/dev/block/<major:minor>`, jen lexikálně."""
    links = PurePosixPath(sys_dir) / "dev" / "block"
    try:
        target = os.readlink(links / device)
    except OSError:
        raise Unverifiable("no_sysfs", "zařízení chybí v /sys/dev/block") from None
    # Odkaz jádra je relativní (../../devices/…); normpath ho složí bez přístupu k disku.
    directory = PurePosixPath(os.path.normpath(links / target))
    devices = PurePosixPath(sys_dir) / "devices"
    if target.startswith("/") or devices not in directory.parents:
        raise Unverifiable("unexpected_link", "neočekávaný odkaz v /sys/dev/block")
    if directory.relative_to(devices).parts[:2] == ("virtual", "block"):
        # device-mapper, RAID (md), loop, zram…: vrstvy se zatím nerozplétají.
        raise Unverifiable("stacked", "vrstvené nebo virtuální blokové zařízení")
    return directory


def checked_uevent(directory, device=None):
    """uevent zařízení s ověřeným názvem a typem; u `device` i shodu čísla zařízení."""
    event = read_uevent(directory)
    name, kind = event.get("DEVNAME", ""), event.get("DEVTYPE")
    number = f"{event.get('MAJOR')}:{event.get('MINOR')}"
    if not NAME.fullmatch(name) or name != directory.name or kind not in LAYOUT_TEXT or (
            device is not None and number != device):
        raise Unverifiable("inconsistent", "údaje zařízení v sysfs si odporují")
    return name, kind


def root_device(table, sys_dir=SYS_DIR):
    """(stav, text, details) blokového zařízení pod viditelným mountem `/` (M5-S3).

    INFO s `bus` (`nvme` jen u doloženého NVMe, jinak podsystém jádra, např.
    `scsi` nebo `mmc`), `disk` (jméno disku v jádře) a `layout` (`partition`
    nebo `disk`); jinak UNKNOWN s důvodem v `cause`, nikdy odhad.
    """
    if not table.complete:
        return State.UNKNOWN, "tabulka připojení je neúplná", {CAUSE: "incomplete"}
    mount = table.covering("/")
    if mount is None:
        return State.UNKNOWN, "kořenový mount nejde jednoznačně určit", {CAUSE: "ambiguous"}
    if mount.device.split(":")[0] == "0":
        # Anonymní číslo: btrfs, overlay, tmpfs, síťový kořen; blokové zařízení nedokládá.
        return State.UNKNOWN, "kořen nemá vlastní blokové zařízení", {CAUSE: "no_block_device"}
    try:
        directory = device_directory(sys_dir, mount.device)
        name, layout = checked_uevent(directory, mount.device)
        disk = directory
        if layout == "partition":
            disk = directory.parent  # oddíl je v sysfs podadresářem svého disku
            name, kind = checked_uevent(disk)
            if kind != "disk":
                raise Unverifiable("inconsistent", "oddíl není pod diskem")
        try:
            subsystem = PurePosixPath(os.readlink(disk / "device" / "subsystem")).name
        except OSError:
            raise Unverifiable("no_parent_device", "disk nemá doložené nadřazené zařízení") from None
        if not NAME.fullmatch(subsystem):
            raise Unverifiable("inconsistent", "neplatný podsystém zařízení")
    except Unverifiable as reason:
        cause, text = reason.args
        return State.UNKNOWN, text, {CAUSE: cause}
    bus = NVME if subsystem in NVME_SUBSYSTEMS else subsystem
    where = "NVMe" if bus == NVME else f"zařízení {bus}"
    return State.INFO, f"ROOT na {where} ({LAYOUT_TEXT[layout]})", {BUS: bus, DISK: name, LAYOUT: layout}


def identify_root_device(mountinfo_path=MOUNTINFO_PATH, sys_dir=SYS_DIR):
    """Zařízení ROOT na požádání: přečte tabulku připojení a sysfs, vrátí (stav, text, details)."""
    try:
        table = read_mountinfo(mountinfo_path)
    except OSError:
        return State.UNKNOWN, "tabulka připojení nedostupná", {CAUSE: "unreadable"}
    except MountInfoError:
        return State.UNKNOWN, "neplatná tabulka připojení", {CAUSE: "invalid"}
    return root_device(table, sys_dir)


def main():
    """Ruční výpis pro tento počítač: stav, text a strojové údaje (bez cest a identifikátorů)."""
    state, text, details = identify_root_device()
    print(f"{state.value}: {text}")
    for key, value in details.items():
        print(f"  {key} = {value}")


if __name__ == "__main__":
    main()
