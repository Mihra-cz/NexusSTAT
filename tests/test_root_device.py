"""Testy read-only identifikace zařízení ROOT (M5-S3).

Sysfs je syntetický strom v dočasném adresáři ve tvaru jádra (odkazy
/sys/dev/block, uevent, device/subsystem); skutečné disky, /dev ani síť se
nepoužívají.
"""

import ast
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexusstat import mounts, root_device
from nexusstat.core import State
from nexusstat.mounts import parse_mountinfo
from nexusstat.root_device import identify_root_device, root_device as find_root_device

NVME_CTRL = "pci0000:00/0000:00:1c.0/0000:01:00.0/nvme/nvme0"
SCSI_DEVICE = "pci0000:00/0000:00:17.0/ata2/host1/target1:0:0/1:0:0:0"
MMC_CARD = "platform/mmc0/mmc_host/mmc0/mmc0:aaaa"


def mountinfo(device, fstype=b"ext4", source=b"/dev/nvme0n1p2"):
    return b"21 1 " + device + b" / / rw,noatime shared:1 - " + fstype + b" " + source + b" rw\n"


class FakeSys:
    """Syntetický /sys: blokové zařízení = adresář s uevent a odkaz /sys/dev/block/M:m."""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / "dev" / "block").mkdir(parents=True)

    def parent(self, path, subsystem):
        """Nadřazené zařízení disku (řadič NVMe, SCSI zařízení, karta MMC) s odkazem subsystem."""
        directory = self.root / "devices" / path
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "subsystem").symlink_to(os.path.relpath(self.root / "bus" / subsystem, directory))
        return directory

    def block(self, path, device, devtype, parent=None, link=True, uevent=None):
        directory = self.root / "devices" / path
        directory.mkdir(parents=True, exist_ok=True)
        major, minor = device.split(":")
        (directory / "uevent").write_text(uevent if uevent is not None else
                                          f"MAJOR={major}\nMINOR={minor}\nDEVNAME={directory.name}\n"
                                          f"DEVTYPE={devtype}\nDISKSEQ=1\n")
        if parent is not None:
            (directory / "device").symlink_to(os.path.relpath(parent, directory))
        if link:
            links = self.root / "dev" / "block"
            (links / device).symlink_to(os.path.relpath(directory, links))
        return directory


def nvme(sys, multipath=False):
    """NVMe s oddílem p2: řadič nvme0, nebo hlava nativního multipath pod nvme-subsys0."""
    if multipath:
        controller = sys.parent("virtual/nvme-subsystem/nvme-subsys0", "../class/nvme-subsystem")
        disk = "virtual/nvme-subsystem/nvme-subsys0/nvme0n1"
    else:
        controller = sys.parent(NVME_CTRL, "../class/nvme")
        disk = f"{NVME_CTRL}/nvme0n1"
    sys.block(disk, "259:0", "disk", parent=controller)
    sys.block(f"{disk}/nvme0n1p2", "259:2", "partition")


def sata(sys, name="sda"):
    disk = sys.block(f"{SCSI_DEVICE}/block/{name}", "8:0", "disk", parent=sys.parent(SCSI_DEVICE, "scsi"))
    sys.block(f"{SCSI_DEVICE}/block/{name}/{name}5", "8:5", "partition")
    return disk


def stacked(sys, name, device):
    sys.block(f"virtual/block/{name}", device, "disk")


class RootDeviceTest(unittest.TestCase):
    def identify(self, device, build, mountinfo_data=None):
        with tempfile.TemporaryDirectory() as directory:
            sys = FakeSys(Path(directory) / "sys")
            build(sys)
            table_path = Path(directory) / "mountinfo"
            table_path.write_bytes(mountinfo(device) if mountinfo_data is None else mountinfo_data)
            return identify_root_device(table_path, sys.root)

    def test_recognised_root_devices(self):
        cases = [
            ("NVMe oddíl", b"259:2", nvme, ("NVMe (oddíl disku)", "nvme", "nvme0n1", "partition")),
            ("NVMe multipath", b"259:2", lambda sys: nvme(sys, multipath=True),
             ("NVMe (oddíl disku)", "nvme", "nvme0n1", "partition")),
            ("NVMe celý disk", b"259:0", nvme, ("NVMe (celý disk)", "nvme", "nvme0n1", "disk")),
            ("SATA oddíl", b"8:5", sata, ("zařízení scsi (oddíl disku)", "scsi", "sda", "partition")),
            ("SD karta", b"179:2", lambda sys: (
                sys.block(f"{MMC_CARD}/block/mmcblk0", "179:0", "disk", parent=sys.parent(MMC_CARD, "mmc")),
                sys.block(f"{MMC_CARD}/block/mmcblk0/mmcblk0p2", "179:2", "partition")),
             ("zařízení mmc (oddíl disku)", "mmc", "mmcblk0", "partition")),
            # Jméno nvme… ve zdroji mountu i v jádře nic nedokládá: rozhoduje podsystém (zde SCSI).
            ("NVMe jen jménem", b"8:5", lambda sys: sata(sys, name="nvme0n1"),
             ("zařízení scsi (oddíl disku)", "scsi", "nvme0n1", "partition")),
        ]
        for name, device, build, (text, bus, disk, layout) in cases:
            with self.subTest(name):
                state, summary, details = self.identify(device, build)
                self.assertEqual((state, summary), (State.INFO, f"ROOT na {text}"))
                self.assertEqual(details, {"bus": bus, "disk": disk, "layout": layout})

    def test_unverifiable_root_device_is_unknown(self):
        def broken_uevent(sys):
            nvme(sys)
            (sys.root / "devices" / NVME_CTRL / "nvme0n1" / "nvme0n1p2" / "uevent").write_text(
                "MAJOR=259\nMINOR=7\nDEVNAME=nvme0n1p2\nDEVTYPE=partition\n")  # jiné číslo než mount

        def partition_without_disk(sys):
            sys.block(f"{SCSI_DEVICE}/block/sda/sda5", "8:5", "partition")
            (sys.root / "devices" / SCSI_DEVICE / "block" / "sda" / "uevent").write_text("DEVNAME=sda\n")

        def outside_devices(sys):
            (sys.root / "dev" / "block" / "8:5").symlink_to("../../../etc")

        cases = [
            ("chybí odkaz v sysfs", b"8:5", lambda sys: sata(sys) and (sys.root / "dev/block/8:5").unlink(),
             "no_sysfs"),
            ("chybí uevent", b"8:5", lambda sys: sys.block("x/block/sda/sda5", "8:5", "partition") and
             (sys.root / "devices/x/block/sda/sda5/uevent").unlink(), "no_sysfs"),
            ("jiné číslo v uevent", b"259:2", broken_uevent, "inconsistent"),
            ("oddíl bez disku", b"8:5", partition_without_disk, "inconsistent"),
            ("disk bez nadřazeného zařízení", b"8:5", lambda sys: (
                sys.block(f"{SCSI_DEVICE}/block/sda", "8:0", "disk"),
                sys.block(f"{SCSI_DEVICE}/block/sda/sda5", "8:5", "partition")), "no_parent_device"),
            ("odkaz mimo /sys/devices", b"8:5", outside_devices, "unexpected_link"),
            ("device-mapper (LVM, LUKS)", b"253:0", lambda sys: stacked(sys, "dm-0", "253:0"), "stacked"),
            ("RAID md", b"9:127", lambda sys: stacked(sys, "md127", "9:127"), "stacked"),
            ("loop", b"7:0", lambda sys: stacked(sys, "loop0", "7:0"), "stacked"),
            ("anonymní číslo (btrfs, overlay, síť)", b"0:31", nvme, "no_block_device"),
        ]
        for name, device, build, cause in cases:
            with self.subTest(name):
                state, _, details = self.identify(device, build)
                self.assertEqual((state, details), (State.UNKNOWN, {"cause": cause}))

    def test_unverifiable_mount_table_is_unknown(self):
        root = mountinfo(b"259:2")
        cases = [
            ("neúplná tabulka", root + b"33 21 0:35 / /mnt/backup rw - cifs //192.0.2.20/backup\n", "incomplete"),
            ("neplatné major:minor", mountinfo(b"259-2"), "incomplete"),  # řádek kořene je nečitelný
            ("dva kořeny", root + mountinfo(b"8:5").replace(b"21 1", b"22 1"), "ambiguous"),
            ("příliš dlouhá tabulka", b"x" * (mounts.MAX_MOUNTINFO_BYTES + 1), "invalid"),
        ]
        for name, data, cause in cases:
            with self.subTest(name):
                self.assertEqual(self.identify(b"259:2", nvme, data)[2], {"cause": cause})
        self.assertEqual(identify_root_device("/nonexistent/mountinfo", "/nonexistent")[2], {"cause": "unreadable"})
        self.assertEqual(find_root_device(parse_mountinfo(root), "/nonexistent")[2], {"cause": "no_sysfs"})

    def test_reads_only_sysfs_links_and_uevent(self):
        # Žádné /dev, stat, procházení adresářů, příkazy ani síť; jen odkazy a uevent.
        forbidden = ("os.stat", "os.lstat", "os.statvfs", "os.listdir", "os.scandir", "os.walk", "os.path.realpath",
                     "os.path.exists", "os.path.isdir", "pathlib.Path.resolve", "pathlib.Path.stat",
                     "pathlib.Path.iterdir", "os.open", "socket.socket", "subprocess.run", "subprocess.Popen",
                     "os.system")
        with tempfile.TemporaryDirectory() as directory:
            sys = FakeSys(Path(directory) / "sys")
            nvme(sys)
            table_path = Path(directory) / "mountinfo"
            table_path.write_bytes(mountinfo(b"259:2"))
            opened, links = [], []
            real_open, real_readlink = open, os.readlink

            def recording_open(path, mode="r", *args, **kwargs):
                opened.append((os.path.relpath(path, directory), mode))
                return real_open(path, mode, *args, **kwargs)

            def recording_readlink(path, *args, **kwargs):
                links.append(os.path.relpath(path, directory))
                return real_readlink(path, *args, **kwargs)

            with contextlib.ExitStack() as stack:
                for name in forbidden:
                    stack.enter_context(mock.patch(name, side_effect=AssertionError(f"zakázaný přístup: {name}")))
                stack.enter_context(mock.patch("builtins.open", recording_open))
                stack.enter_context(mock.patch("os.readlink", recording_readlink))
                state, _, _ = identify_root_device(table_path, sys.root)
        self.assertIs(state, State.INFO)
        disk = f"sys/devices/{NVME_CTRL}/nvme0n1"
        self.assertEqual(opened, [("mountinfo", "rb"), (f"{disk}/nvme0n1p2/uevent", "rb"), (f"{disk}/uevent", "rb")])
        self.assertEqual(links, ["sys/dev/block/259:2", f"{disk}/device/subsystem"])

    def test_on_demand_only_and_no_forbidden_calls(self):
        source = Path(root_device.__file__)
        tree = ast.parse(source.read_text(encoding="utf-8"))
        called = {node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                  for node in ast.walk(tree) if isinstance(node, ast.Call)}
        self.assertFalse(called & {"stat", "lstat", "statvfs", "listdir", "scandir", "realpath", "resolve", "exists",
                                   "is_dir", "iterdir", "walk", "glob", "socket", "run", "Popen", "system"})
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        self.assertFalse({"subprocess", "socket", "glob", "shutil"} & imported)
        # Smyčka, kreslení, RGB ani historie identifikaci nevolají. Na požádání ji volá jen živý
        # náhled při vytvoření LIVE a simulate --live jednou za běh (M5-S4); oled_text bere jen klíč.
        callers = []
        for path in source.parent.glob("*.py"):
            if path == source:
                continue
            nodes = list(ast.walk(ast.parse(path.read_text(encoding="utf-8"))))
            names = {alias.name for node in nodes if isinstance(node, ast.ImportFrom) for alias in node.names}
            names |= {node.id for node in nodes if isinstance(node, ast.Name)}
            if "identify_root_device" in names:
                callers.append(path.name)
        self.assertEqual(sorted(callers), ["preview.py", "simulate.py"])

    def test_manual_listing(self):
        output = io.StringIO()
        result = (State.INFO, "ROOT na NVMe (oddíl disku)", {"bus": "nvme", "disk": "nvme0n1", "layout": "partition"})
        with mock.patch.object(root_device, "identify_root_device", return_value=result), \
                contextlib.redirect_stdout(output):
            root_device.main()
        self.assertEqual(output.getvalue(), "INFO: ROOT na NVMe (oddíl disku)\n  bus = nvme\n  disk = nvme0n1\n"
                                            "  layout = partition\n")


if __name__ == "__main__":
    unittest.main()
