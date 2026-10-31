"""Testy read-only inventury úložišť z /proc/self/mountinfo (M5-S1) a režimu / (M5-S2).

Tabulky jsou syntetické ve formátu proc(5); cesty jsou obecné a adresy NAS jen
z dokumentačního rozsahu RFC 5737. Žádný test nečte skutečné body připojení,
nevolá stat ani statvfs a nepoužívá síť. Čas je simulovaný.
"""

import ast
import contextlib
import io
import os
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from nexusstat import mounts, rgb
from nexusstat.core import State, active_problems, effective
from nexusstat.mounts import (AUTOFS, LOCAL, NETWORK, ROOT_MODE_ID, UNKNOWN, VIRTUAL, Mount, MountInfoError,
                              MountTable, RootMountMonitor, kind_of, parse_mountinfo, read_mountinfo, root_mode)
from nexusstat.oled import ALERT_SYMBOL, UNKNOWN_SYMBOL, home_symbol
from nexusstat.oled_text import check_name, status_texts
from nexusstat.run import Scheduled
from nexusstat.storage import NETWORK_FILESYSTEMS

SRC = Path(mounts.__file__).resolve().parent

# Tabulka podobná malému serveru: kořen na NVMe, boot oddíl, systémové mounty,
# Docker overlay, autofs s CIFS nad sebou, NFS a sshfs. Pořadí odpovídá jádru.
TABLE = b"""\
21 1 259:2 / / rw,noatime shared:1 - ext4 /dev/nvme0n1p2 rw
22 21 0:20 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw
23 21 0:21 / /sys rw,nosuid,nodev,noexec,relatime shared:2 - sysfs sysfs rw
24 21 0:5 / /dev rw,nosuid,relatime shared:8 - devtmpfs udev rw,size=4012345k,mode=755
25 21 259:1 / /boot/firmware rw,relatime shared:10 - vfat /dev/nvme0n1p1 rw,fmask=0022
26 21 0:25 / /run rw,nosuid,nodev,noexec,relatime shared:5 - tmpfs tmpfs rw,size=812345k,mode=755
27 21 0:30 / /var/lib/docker/overlay2/abc/merged rw,relatime - overlay overlay rw,lowerdir=/x,upperdir=/y
28 21 0:31 / /mnt/share rw,relatime shared:20 - autofs systemd-1 rw,fd=50,pgrp=1,timeout=300
29 28 0:32 / /mnt/share rw,relatime shared:21 - cifs //192.0.2.20/share rw,vers=3.1.1,addr=192.0.2.20
30 21 0:33 / /mnt/nfs rw,relatime shared:22 - nfs4 192.0.2.21:/export rw,vers=4.2
31 21 0:34 / /mnt/remote ro,nosuid,nodev,relatime shared:23 - fuse.sshfs user@192.0.2.22:/data rw,user_id=0
32 21 7:0 / /snap/core22/1 ro,nodev,relatime shared:24 - squashfs /dev/loop0 ro,errors=continue
"""


def table(data=TABLE):
    return parse_mountinfo(data)


def line(point=b"/mnt/x", fstype=b"ext4", source=b"/dev/sda1", optional=b"shared:1", mount_id=b"40", parent=b"21",
         device=b"8:1", root=b"/", options=b"rw,relatime", super_options=b"rw"):
    optional_part = (optional + b" ") if optional else b""
    return (mount_id + b" " + parent + b" " + device + b" " + root + b" " + point + b" " + options + b" "
            + optional_part + b"- " + fstype + b" " + source + b" " + super_options)


class ParseTest(unittest.TestCase):
    def test_fields_of_a_real_looking_table(self):
        parsed = table()
        self.assertTrue(parsed.complete)
        self.assertEqual(len(parsed.mounts), 12)
        root = parsed.mounts[0]
        self.assertEqual(root, Mount(21, 1, "259:2", "/", "/", False, "ext4", "/dev/nvme0n1p2"))
        self.assertEqual(root.kind, LOCAL)
        cifs = parsed.mounts[8]
        self.assertEqual((cifs.mount_id, cifs.parent_id, cifs.mount_point, cifs.fstype, cifs.kind),
                         (29, 28, "/mnt/share", "cifs", NETWORK))
        self.assertTrue(parsed.mounts[10].read_only)  # podle voleb připojení (ro), ne superbloku (rw)
        self.assertFalse(parsed.mounts[9].read_only)

    def test_inventory_by_kind(self):
        kinds = {kind: [m.mount_point for m in found] for kind, found in table().by_kind().items()}
        self.assertEqual(kinds, {
            LOCAL: ["/", "/boot/firmware", "/snap/core22/1"],
            NETWORK: ["/mnt/share", "/mnt/nfs", "/mnt/remote"],
            VIRTUAL: ["/proc", "/sys", "/dev", "/run", "/var/lib/docker/overlay2/abc/merged"],
            AUTOFS: ["/mnt/share"],
            UNKNOWN: [],
        })
        self.assertEqual(MountTable(()).by_kind(), {kind: () for kind in mounts.KINDS})

    def test_classification(self):
        cases = {
            "ext4": LOCAL, "btrfs": LOCAL, "xfs": LOCAL, "vfat": LOCAL, "exfat": LOCAL, "fuseblk": LOCAL,
            "zfs": LOCAL, "squashfs": LOCAL,
            "cifs": NETWORK, "smb3": NETWORK, "nfs": NETWORK, "nfs4": NETWORK, "9p": NETWORK, "sshfs": NETWORK,
            "fuse.sshfs": NETWORK, "fuse.rclone": NETWORK,
            "proc": VIRTUAL, "sysfs": VIRTUAL, "tmpfs": VIRTUAL, "cgroup2": VIRTUAL, "overlay": VIRTUAL,
            "nsfs": VIRTUAL, "nfsd": VIRTUAL, "rpc_pipefs": VIRTUAL,
            "autofs": AUTOFS,
            # Neznámý typ se nikdy nepovažuje za lokální úložiště.
            "fuse": UNKNOWN, "fuse.gvfsd-fuse": UNKNOWN, "fuse.portal": UNKNOWN, "foo": UNKNOWN, "": UNKNOWN,
        }
        for fstype, expected in cases.items():
            with self.subTest(fstype=fstype):
                self.assertEqual(kind_of(fstype), expected)
        # Stejný seznam síťových typů jako kontrola cest historie (storage.py).
        self.assertEqual({kind_of(fstype) for fstype in NETWORK_FILESYSTEMS}, {NETWORK})

    def test_escaped_characters(self):
        data = b"\n".join((
            line(mount_id=b"21", parent=b"1", point=b"/"),
            line(point=b"/mnt/my\\040disk", source=b"/dev/disk\\040one"),
            line(point=b"/mnt/tab\\011x", mount_id=b"41"),
            line(point=b"/mnt/new\\012line", mount_id=b"42"),
            line(point=b"/mnt/back\\134slash", mount_id=b"43"),
            line(point=b"/mnt/\\303\\251", mount_id=b"44"),  # é v UTF-8 zapsané osmičkově
            line(point=b"/mnt/\xc3\xa9t\xc3\xa9", mount_id=b"45"),  # UTF-8 bez escapování
            line(point=b"/mnt/raw\xff", mount_id=b"46"),  # bajt mimo UTF-8
        )) + b"\n"
        parsed = table(data)
        self.assertTrue(parsed.complete)
        points = [m.mount_point for m in parsed.mounts[1:]]
        self.assertEqual(points[:6], ["/mnt/my disk", "/mnt/tab\tx", "/mnt/new\nline", "/mnt/back\\slash",
                                      "/mnt/é", "/mnt/été"])
        self.assertEqual(parsed.mounts[1].source, "/dev/disk one")
        self.assertEqual(os.fsencode(points[6]), b"/mnt/raw\xff")  # zachováno přes surrogateescape
        self.assertEqual(parsed.covering("/mnt/my disk/file").mount_point, "/mnt/my disk")

    def test_optional_fields_and_bind_root(self):
        data = b"\n".join((
            line(optional=b""),
            line(optional=b"shared:1 master:2 propagate_from:3 unbindable", mount_id=b"41"),
            line(root=b"/srv/data", point=b"/home/data", mount_id=b"42"),  # bind mount podadresáře
            line(root=b"/@home", fstype=b"btrfs", point=b"/home", mount_id=b"43"),  # podsvazek btrfs
            line(root=b"net:[4026531992]", fstype=b"nsfs", point=b"/run/netns/x", mount_id=b"44"),
        ))
        parsed = table(data)
        self.assertTrue(parsed.complete)
        self.assertEqual([m.root for m in parsed.mounts], ["/", "/", "/srv/data", "/@home", "net:[4026531992]"])
        self.assertEqual(parsed.mounts[4].kind, VIRTUAL)

    def test_invalid_lines_are_skipped_and_reported(self):
        good = line()
        bad = (
            b"",  # prázdný řádek uprostřed
            b"40 21 8:1 / /mnt/x rw",  # neúplný
            b"40 21 8:1 / /mnt/x rw shared:1 ext4 /dev/sda1 rw",  # chybí oddělovač
            b"40 21 8:1 / /mnt/x rw shared:1 - ext4 /dev/sda1",  # za oddělovačem 2 pole
            b"40 21 8:1 / /mnt/x rw shared:1 - ext4 /dev/sda1 rw extra",  # za oddělovačem 4 pole
            line(mount_id=b"x"),
            line(parent=b"-1"),
            line(device=b"8-1"),
            line(point=b"mnt/x"),  # relativní bod připojení
            line(point=b"/mnt/a\\x"),  # neplatná escape sekvence
            line(point=b"/mnt/a\\400"),  # mimo rozsah bajtu
            line(point=b"/mnt/a\\04"),  # neúplná sekvence
            line(point=b"/mnt/a\\"),
            good.replace(b" - ", b"  - "),  # dvojitá mezera
            good + b" ",  # mezera na konci
            good.replace(b"/mnt/x", b"/mnt/\0x"),
            b"\xff\xfe garbage",
        )
        for number, broken in enumerate(bad):
            with self.subTest(case=number, line=broken[:50]):
                parsed = table(good + b"\n" + broken + b"\n" + line(mount_id=b"50", point=b"/mnt/y") + b"\n")
                self.assertEqual(parsed.invalid_lines, (2,))
                self.assertFalse(parsed.complete)
                self.assertEqual([m.mount_point for m in parsed.mounts], ["/mnt/x", "/mnt/y"])

    def test_read_only_from_mount_options_and_superblock(self):
        # M5-S2: ro ze superbloku (ext4 po chybě s errors=remount-ro) se čte zvlášť od voleb připojení.
        flags = [(m.mount_point, m.read_only, m.super_read_only) for m in table().mounts
                 if m.read_only or m.super_read_only]
        self.assertEqual(flags, [("/mnt/remote", True, False), ("/snap/core22/1", True, True)])
        # Bez ro/rw na začátku voleb by se režim tiše bral jako rw: řádek je neplatný.
        for broken in (line(options=b"defaults"), line(super_options=b"errors=remount-ro"), line(options=b"rwx")):
            with self.subTest(line=broken):
                self.assertEqual(table(line(mount_id=b"21", point=b"/") + b"\n" + broken).invalid_lines, (2,))

    def test_unrecognized_optional_fields_are_ignored(self):
        # proc_pid_mountinfo(5): „Parsers should ignore all unrecognized optional fields.“
        parsed = table(line(optional=b"future-tag:a/b shared:1 x"))
        self.assertTrue(parsed.complete)
        self.assertEqual(parsed.mounts[0].fstype, "ext4")

    def test_duplicate_mount_id_and_edge_inputs(self):
        parsed = table(line() + b"\n" + line(point=b"/mnt/y") + b"\n")
        self.assertEqual((len(parsed.mounts), parsed.invalid_lines), (1, (2,)))
        self.assertEqual(table(b""), MountTable((), ()))
        self.assertTrue(table(b"").complete)
        self.assertEqual(len(table(line()).mounts), 1)  # poslední řádek bez konce řádku
        with self.assertRaises(TypeError):
            parse_mountinfo(TABLE.decode())


class LookupTest(unittest.TestCase):
    """Čistě lexikální vyhledání mountu pro cestu: bez přístupu k cestě ani k bodu připojení."""

    def test_nested_mounts_use_the_deepest_mount_point(self):
        parsed = table()
        cases = {
            "/": "/", "/home/user/file": "/", "/boot/firmware/config.txt": "/boot/firmware",
            "/mnt/share/movies/a.mkv": "/mnt/share", "/mnt/nfs": "/mnt/nfs", "/mnt": "/",
            "/mnt/share2/x": "/",  # /mnt/share2 neleží pod /mnt/share
            "/mnt/nfs-old": "/", "//mnt//nfs//": "/mnt/nfs", "/mnt/./nfs/x": "/mnt/nfs",
        }
        for path, point in cases.items():
            with self.subTest(path=path):
                self.assertEqual(parsed.covering(path).mount_point, point)

    def test_autofs_stack_is_ordered_by_parent_not_table_order(self):
        autofs = line(mount_id=b"28", parent=b"21", point=b"/mnt/share", fstype=b"autofs", source=b"systemd-1")
        cifs = line(mount_id=b"29", parent=b"28", point=b"/mnt/share", fstype=b"cifs", source=b"//192.0.2.20/share")
        root = line(mount_id=b"21", parent=b"1", point=b"/")
        for order in ((root, autofs, cifs), (root, cifs, autofs)):
            with self.subTest(order=[o.split()[0] for o in order]):
                parsed = table(b"\n".join(order))
                self.assertEqual([m.kind for m in parsed.stack("/mnt/share/a")], [AUTOFS, NETWORK])
                self.assertEqual(parsed.covering("/mnt/share/a").kind, NETWORK)
        # Bez připojeného cíle je nahoře jen autofs: cíl se připojí až při přístupu.
        self.assertEqual(table(root + b"\n" + autofs).covering("/mnt/share").kind, AUTOFS)

    def test_ambiguous_stack_gives_no_conclusion(self):
        # Dva mounty se stejným rodičem na stejném bodě: který je vidět, nejde určit.
        root = line(mount_id=b"21", parent=b"1", point=b"/")
        first = line(mount_id=b"30", parent=b"21", point=b"/mnt/x", fstype=b"ext4")
        second = line(mount_id=b"31", parent=b"21", point=b"/mnt/x", fstype=b"nfs4", source=b"192.0.2.21:/e")
        for order in ((root, first, second), (root, second, first)):
            parsed = table(b"\n".join(order))
            self.assertEqual(parsed.stack("/mnt/x/y"), [])
            self.assertIsNone(parsed.covering("/mnt/x/y"))
            self.assertEqual(parsed.covering("/srv").mount_id, 21)  # mimo nejednoznačné místo platí dál

    def test_mount_below_a_hidden_parent_is_not_visible(self):
        # proc_pid_mountinfo(5): horní mount je dostupný, jen když nejdelší nadřazený bod
        # připojení není sám skrytý. Regrese z review: dřív vyhrál nejdelší bod připojení.
        root = line(mount_id=b"21", parent=b"1", point=b"/")
        disk = line(mount_id=b"30", parent=b"21", point=b"/mnt", device=b"8:1")
        data = line(mount_id=b"31", parent=b"30", point=b"/mnt/data", device=b"8:2")
        cifs = line(mount_id=b"32", parent=b"30", point=b"/mnt", fstype=b"cifs", source=b"//192.0.2.20/share")
        parsed = table(b"\n".join((root, disk, data, cifs)))
        self.assertEqual(parsed.covering("/mnt/data/db.sqlite3").kind, NETWORK)  # ne skrytý lokální /mnt/data
        self.assertEqual([m.mount_id for m in parsed.stack("/mnt/data")], [30, 32])
        share = line(mount_id=b"31", parent=b"30", point=b"/mnt/share", fstype=b"nfs4", source=b"192.0.2.21:/e")
        tmpfs = line(mount_id=b"32", parent=b"30", point=b"/mnt", fstype=b"tmpfs", source=b"tmpfs")
        self.assertEqual(table(b"\n".join((root, disk, share, tmpfs))).covering("/mnt/share/x").kind, VIRTUAL)

    def test_root_whose_parent_is_itself(self):
        # Kořen jmenného prostoru má za rodiče sám sebe; mount nad ním je nahoře v obou pořadích.
        root = line(mount_id=b"30", parent=b"30", point=b"/", device=b"259:2")
        over = line(mount_id=b"31", parent=b"30", point=b"/", fstype=b"nfs4", source=b"192.0.2.21:/root")
        for order in ((root, over), (over, root)):
            with self.subTest(first=order[0].split()[0]):
                self.assertEqual(table(b"\n".join(order)).covering("/etc").kind, NETWORK)

    def test_incomplete_table_gives_no_conclusion(self):
        # Nečitelný řádek mohl být právě mount nad cestou: neúplná tabulka nic nevylučuje.
        data = TABLE + b"33 21 0:35 / /mnt/backup rw - cifs //192.0.2.20/backup\n"  # chybí pole superbloku
        parsed = table(data)
        self.assertFalse(parsed.complete)
        for path in ("/mnt/backup/x", "/", "/home/user"):
            with self.subTest(path=path):
                self.assertEqual(parsed.stack(path), [])
                self.assertIsNone(parsed.covering(path))
        self.assertEqual(len(parsed.by_kind()[LOCAL]), 3)  # inventura přečtených řádků zůstává, s complete=False

    def test_paths_without_mount_and_invalid_paths(self):
        without_root = table(line(point=b"/mnt/x"))
        self.assertIsNone(without_root.covering("/srv/data"))
        self.assertEqual(without_root.stack("/srv/data"), [])
        for path in ("relative/path", "/mnt/share/../nfs", "..", ""):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    table().covering(path)


OBSERVED = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def root_line(options=b"rw,noatime", super_options=b"rw,errors=remount-ro", fstype=b"ext4", mount_id=b"21",
              parent=b"1", device=b"259:2", source=b"/dev/nvme0n1p2"):
    return line(point=b"/", fstype=fstype, source=source, mount_id=mount_id, parent=parent, device=device,
                options=options, super_options=super_options)


class RootModeTest(unittest.TestCase):
    """M5-S2: režim a typ kořenového souborového systému jen z tabulky připojení."""

    def collect(self, data, now=1000.0):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mountinfo"
            path.write_bytes(data)
            [result] = RootMountMonitor(5400, path).collect(now, OBSERVED)
        return result

    def test_local_read_write_root(self):
        result = self.collect(TABLE)
        self.assertEqual((result.id, result.state, result.summary),
                         (ROOT_MODE_ID, State.INFO, "Režim / rw (ext4, lokální)"))
        self.assertEqual(dict(result.details), {"mode": "rw", "fstype": "ext4", "kind": LOCAL})
        self.assertEqual((result.observed_utc, result.expires_monotonic, result.measurement), (OBSERVED, 6400.0, None))
        self.assertTrue(result.simulated)  # podstrčená tabulka, ne tento počítač
        self.assertEqual(check_name(ROOT_MODE_ID), "ROOT MODE")  # id, které displej už zná
        # errors=remount-ro je jen reakce na budoucí chybu, ne důkaz přepnutí: rozhoduje první volba rw.
        self.assertEqual(root_mode(table(root_line(super_options=b"rw,errors=remount-ro")))[:2],
                         (State.INFO, "rw (ext4, lokální)"))

    def test_read_only_root_from_mount_options_or_superblock(self):
        cases = {(b"ro,noatime", b"rw"): "mount", (b"rw,noatime", b"ro,errors=remount-ro"): "superblock",
                 (b"ro", b"ro"): "both"}
        for (options, super_options), ro_by in cases.items():
            with self.subTest(ro_by=ro_by):
                state, text, details = root_mode(table(root_line(options=options, super_options=super_options)))
                # Doložené ro je závada ERROR (ROOT READ-ONLY); nedůvěryhodná data jsou UNKNOWN.
                self.assertIs(state, State.ERROR)
                self.assertEqual(details, {"mode": "ro", "ro_by": ro_by, "fstype": "ext4", "kind": LOCAL})
        self.assertEqual(root_mode(table(root_line(super_options=b"ro")))[1],
                         "jen pro čtení (ext4, lokální; ro v superbloku)")

    def test_overmounted_root_uses_the_visible_mount(self):
        hidden = root_line()
        over = root_line(mount_id=b"31", parent=b"21", device=b"8:1", fstype=b"xfs", options=b"ro",
                         source=b"/dev/sda1")
        for order in ((hidden, over), (over, hidden)):
            with self.subTest(first=order[0].split()[0]):
                self.assertEqual(root_mode(table(b"\n".join(order)))[2],
                                 {"mode": "ro", "ro_by": "mount", "fstype": "xfs", "kind": LOCAL})

    def test_network_virtual_and_unknown_root_types(self):
        cases = {b"nfs4": NETWORK, b"fuse.sshfs": NETWORK, b"overlay": VIRTUAL, b"tmpfs": VIRTUAL, b"foo": UNKNOWN}
        for fstype, kind in cases.items():
            with self.subTest(fstype=fstype):
                state, text, details = root_mode(table(root_line(fstype=fstype, source=b"192.0.2.21:/root")))
                self.assertIs(state, State.INFO)
                self.assertEqual(details, {"mode": "rw", "fstype": fstype.decode(), "kind": kind})
                self.assertNotIn("192.0.2.21", text)  # zdroj (může být adresa NAS) se nevypisuje
        self.assertEqual(root_mode(table(root_line(fstype=b"foo")))[1], "rw (foo, neznámý typ)")

    def test_incomplete_or_untrustworthy_data_is_unknown(self):
        root = root_line()
        cases = [
            # Nečitelný řádek mohl být právě mount nad /.
            ("incomplete", root + b"\n33 21 0:35 / /mnt/backup rw - cifs //192.0.2.20/backup\n"),
            ("incomplete", root_line(options=b"defaults")),  # režim kořene nejde přečíst
            ("ambiguous", root + b"\n" + root_line(mount_id=b"22", device=b"8:1")),  # dva nezávislé kořeny
            ("ambiguous", line(point=b"/mnt/x")),  # tabulka bez kořene
            ("invalid_type", root_line(fstype=b"ext\\0114")),  # tabulátor v typu
        ]
        for cause, data in cases:
            with self.subTest(cause=cause, data=data[-40:]):
                result = self.collect(data)
                self.assertIs(result.state, State.UNKNOWN)
                self.assertEqual(dict(result.details), {"cause": cause})  # žádný odhad režimu
        self.assertEqual(self.collect(b"x" * (mounts.MAX_MOUNTINFO_BYTES + 1)).details["cause"], "invalid")
        [missing] = RootMountMonitor(5400, "/nonexistent/mountinfo").collect(1000.0, OBSERVED)
        self.assertEqual((missing.state, missing.summary, dict(missing.details)),
                         (State.UNKNOWN, "Režim /: tabulka připojení nedostupná", {"cause": "unreadable"}))

    def test_transitions_reach_status_nexus_and_rgb(self):
        # rw → ro → rw → nečitelná tabulka po 60 s přes Scheduled; ro je závada jako každá jiná.
        reads, results, seen = [], [], []
        real_read = mounts.read_mountinfo

        def counting_read(path):
            reads.append(path)
            return real_read(path)

        tables = ((1000.0, root_line()), (1060.0, root_line(super_options=b"ro,errors=remount-ro")),
                  (1120.0, root_line()), (1180.0, None))
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(mounts, "read_mountinfo", counting_read):
            path = Path(directory) / "mountinfo"
            scheduled = Scheduled(RootMountMonitor(180, path), 30, interval_seconds=60)
            for now, data in tables:
                if data is None:
                    path.unlink()
                else:
                    path.write_bytes(data)
                [result] = scheduled.collect(now, OBSERVED)
                self.assertIs(scheduled.collect(now + 59, OBSERVED)[0], result)  # mezi termíny se nic nečte
                results.append(result)
                signal = rgb.rgb_signal([result], rgb.Acknowledgements(), True, False, now)
                seen.append((result.state, [status_texts(p, now)[0] for p in active_problems([result], now)],
                             home_symbol([result], now), signal.kind, signal.steps))
        self.assertEqual(len(reads), 4)
        self.assertEqual(seen, [
            (State.INFO, [], None, rgb.LIGHT, ()),
            (State.ERROR, ["ROOT READ-ONLY"], ALERT_SYMBOL, rgb.CODES, ((State.ERROR, 1),)),  # kategorie systém
            (State.INFO, [], None, rgb.LIGHT, ()),
            (State.UNKNOWN, ["ROOT MODE NO DATA"], UNKNOWN_SYMBOL, rgb.OFF, ()),  # ne předchozí rw
        ])
        self.assertEqual(dict(result.details), {"cause": "unreadable"})
        # Když měření nepřijde, i ERROR po platnosti 3 × 60 s zestárne na UNKNOWN bez údajů.
        error = results[1]
        self.assertIs(effective(error, 1060.0 + 179), error)
        stale = effective(error, 1060.0 + 180)
        self.assertEqual((stale.state, dict(stale.details)), (State.UNKNOWN, {}))

    def test_monitor_opens_only_the_mountinfo_file(self):
        # V tabulce jsou CIFS, NFS, sshfs i autofs: na žádný bod připojení se nesmí sáhnout.
        opened = []

        def recording_open(path, mode="r", *args, **kwargs):
            opened.append((str(path), mode))
            if str(path) != mounts.MOUNTINFO_PATH:
                raise AssertionError(f"zakázané otevření: {path}")
            return io.BytesIO(TABLE)

        with contextlib.ExitStack() as stack:
            for name in SafetyTest.FORBIDDEN:
                if name != "builtins.open":
                    stack.enter_context(mock.patch(name, side_effect=AssertionError(f"zakázaný přístup: {name}")))
            stack.enter_context(mock.patch("builtins.open", recording_open))
            [result] = RootMountMonitor(5400).collect(1000.0, OBSERVED)
        self.assertEqual(opened, [("/proc/self/mountinfo", "rb")])
        self.assertEqual((result.state, result.details["kind"], result.simulated), (State.INFO, LOCAL, False))
        with self.assertRaises(ValueError):
            RootMountMonitor(0)


class SafetyTest(unittest.TestCase):
    """Parser ani vyhledání nesmí sáhnout na body připojení, síť ani jiné soubory."""

    FORBIDDEN = ("os.stat", "os.lstat", "os.statvfs", "os.listdir", "os.scandir", "os.access", "os.path.realpath",
                 "os.path.exists", "os.path.ismount", "os.path.isdir", "pathlib.Path.resolve", "pathlib.Path.stat",
                 "pathlib.Path.exists", "pathlib.Path.iterdir", "builtins.open", "os.open", "socket.socket")

    def test_parse_and_lookup_touch_nothing(self):
        with contextlib.ExitStack() as stack:
            for name in self.FORBIDDEN:
                stack.enter_context(mock.patch(name, side_effect=AssertionError(f"zakázaný přístup: {name}")))
            parsed = table()
            parsed.by_kind()
            parsed.stack("/mnt/share/a")
            self.assertEqual(parsed.covering("/mnt/share/a").kind, NETWORK)

    def test_module_calls_no_filesystem_or_network_functions(self):
        tree = ast.parse(Path(mounts.__file__).read_text(encoding="utf-8"))
        called = {node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                  for node in ast.walk(tree) if isinstance(node, ast.Call)}
        for name in ("stat", "lstat", "statvfs", "listdir", "scandir", "realpath", "resolve", "exists", "ismount",
                     "is_dir", "iterdir", "walk", "access", "socket", "run", "Popen", "system"):
            self.assertNotIn(name, called)
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        self.assertFalse({"subprocess", "socket", "glob", "shutil"} & imported)

    def test_read_opens_only_the_mountinfo_file(self):
        opened = []
        real_open = open

        def recording_open(path, mode="r", *args, **kwargs):
            opened.append((str(path), mode))
            return io.BytesIO(TABLE) if str(path) == mounts.MOUNTINFO_PATH else real_open(path, mode, *args, **kwargs)

        with mock.patch("builtins.open", recording_open):
            parsed = read_mountinfo()
        self.assertEqual(opened, [("/proc/self/mountinfo", "rb")])
        self.assertEqual(len(parsed.mounts), 12)

    def test_read_from_file_limits_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mountinfo"
            path.write_bytes(TABLE)
            self.assertEqual(read_mountinfo(path), table())
            path.write_bytes(b"x" * (mounts.MAX_MOUNTINFO_BYTES + 1))
            with self.assertRaises(MountInfoError):
                read_mountinfo(path)
            with self.assertRaises(OSError):
                read_mountinfo(Path(directory) / "chybi")

    def test_used_only_by_storage_checks_and_the_loop(self):
        # Kontrola cest historie a snímků (storage.py), od M5-S2 režim / ve smyčce (run.py) a od M5-S3
        # identifikace zařízení ROOT na požádání (root_device.py); displej ne.
        users = [path.name for path in SRC.glob("*.py") if path.name != "mounts.py"
                 and "mounts" in {getattr(node, "module", None) for node in ast.walk(ast.parse(path.read_text(
                     encoding="utf-8"))) if isinstance(node, ast.ImportFrom)}]
        self.assertEqual(sorted(users), ["root_device.py", "run.py", "storage.py"])

    def test_manual_listing_never_prints_the_source(self):
        output = io.StringIO()
        with mock.patch.object(mounts, "read_mountinfo", return_value=table()), contextlib.redirect_stdout(output):
            mounts.main()
        text = output.getvalue()
        self.assertIn("network: 3", text)
        self.assertIn("'/mnt/share'", text)
        for secret in ("192.0.2.20", "192.0.2.21", "user@", "addr="):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
