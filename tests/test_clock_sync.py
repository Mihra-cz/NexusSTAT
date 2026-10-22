"""Testy read-only zdroje stavu synchronizace hodin a boot_id (M3.3b-1).

Odpověď jádra se simuluje náhradní funkcí adjtimex, soubory systemd-timesyncd
dočasným adresářem. Systémový čas se nikde nenastavuje a žádná služba NTP se
nespouští ani nekonfiguruje; jediný skutečný dotaz na systém je smoke test,
který jen kontroluje typ odpovědi.
"""

import ctypes
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nexusstat import clock_sync
from nexusstat.clock_sync import (
    MAX_ERROR_LIMIT_US,
    MAX_TIME_RESULT,
    STA_UNSYNC,
    UNKNOWN_SOURCE,
    ClockSync,
    Timex,
    first_sync_note,
    kernel_sync,
    read_boot_id,
    read_timex,
    timesyncd_first_sync,
)

BOOT_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"  # vymyšlený, ne z žádného stroje


def fake_adjtimex(result=0, status=0, maxerror=1000, errno=0):
    """Náhradní adjtimex: naplní strukturu zadanými hodnotami a vrátí kód."""

    def call(pointer):
        timex = pointer._obj
        timex.status = status
        timex.maxerror = maxerror
        if result < 0:
            ctypes.set_errno(errno)
        return result

    return call


class TimexTest(unittest.TestCase):
    def test_structure_matches_kernel_layout(self):
        # Rozložení `struct timex` jádra: 208 B na 64 bitech, 128 B na 32 bitech.
        expected = 208 if ctypes.sizeof(ctypes.c_long) == 8 else 128
        self.assertEqual(ctypes.sizeof(Timex), expected)

    def test_query_does_not_set_anything(self):
        # modes = 0 je čistý dotaz: jádro podle něj nic nemění.
        seen = []

        def call(pointer):
            seen.append(pointer._obj.modes)
            pointer._obj.maxerror = 5
            return 0

        read_timex(adjtimex=call)
        self.assertEqual(seen, [0])


class ReadTimexTest(unittest.TestCase):
    def test_returns_result_status_and_maxerror(self):
        self.assertEqual(read_timex(adjtimex=fake_adjtimex(5, 0x2041, 213500)), (5, 0x2041, 213500))

    def test_missing_library_is_error(self):
        with mock.patch.object(clock_sync, "_adjtimex", return_value=None):
            with self.assertRaises(OSError):
                read_timex()

    def test_failed_call_is_error(self):
        with self.assertRaises(OSError):
            read_timex(adjtimex=fake_adjtimex(-1, errno=1))

    def test_implausible_answer_is_error(self):
        # Neznámé rozložení struktury dá nesmyslné hodnoty: nikdy se nevydají za stav hodin.
        for result, maxerror in ((MAX_TIME_RESULT + 1, 1000), (0, -1), (0, MAX_ERROR_LIMIT_US + 1)):
            with self.subTest(result=result, maxerror=maxerror):
                with self.assertRaises(OSError):
                    read_timex(adjtimex=fake_adjtimex(result, 0, maxerror))

    def test_real_kernel_answer_is_plausible_or_unavailable(self):
        try:
            result, _, maxerror = read_timex()
        except OSError:
            self.skipTest("adjtimex na tomto systému není k dispozici")
        self.assertTrue(0 <= result <= MAX_TIME_RESULT)
        self.assertTrue(0 <= maxerror <= MAX_ERROR_LIMIT_US)


class KernelSyncTest(unittest.TestCase):
    def test_bounded_error_is_synchronized(self):
        self.assertIs(kernel_sync(read=lambda: (0, 0, 213500)), True)

    def test_limit_itself_is_not_synchronized(self):
        # Mez přesně 16 s = jádro žádnou korekci nedostalo (NTP_PHASE_LIMIT).
        self.assertIs(kernel_sync(read=lambda: (5, STA_UNSYNC, MAX_ERROR_LIMIT_US)), False)

    def test_unsync_flag_alone_does_not_decide(self):
        # systemd-timesyncd nechává STA_UNSYNC nastavený i při správné synchronizaci.
        self.assertIs(kernel_sync(read=lambda: (5, STA_UNSYNC, 213500)), True)

    def test_query_error_is_unknown(self):
        def failing():
            raise OSError("bez odpovědi")

        self.assertIsNone(kernel_sync(read=failing))


class MarkerTest(unittest.TestCase):
    """Soubor timesyncd dokládá jen dřívější synchronizaci, nikdy aktuální stav."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name) / "timesync"
        self.marker = self.runtime / "synchronized"

    def test_marker_proves_only_an_earlier_synchronization(self):
        self.runtime.mkdir()
        self.marker.touch()
        self.assertIs(timesyncd_first_sync(self.marker, self.runtime), True)

    def test_running_service_without_marker_never_synchronized_yet(self):
        self.runtime.mkdir()
        self.assertIs(timesyncd_first_sync(self.marker, self.runtime), False)

    def test_without_service_state_is_unknown(self):
        self.assertIsNone(timesyncd_first_sync(self.marker, self.runtime))

    def test_unreadable_marker_is_unknown(self):
        with mock.patch.object(os.path, "exists", side_effect=OSError("nelze")):
            self.assertIsNone(timesyncd_first_sync(self.marker, self.runtime))

    def test_marker_is_not_a_source_of_the_current_state(self):
        # Zdroj rozhodnutí je jediný: dotaz jádra. Marker mezi nimi není.
        self.assertEqual([name for name, _ in clock_sync.SOURCES], ["adjtimex"])
        self.assertNotIn(timesyncd_first_sync, [probe for _, probe in clock_sync.SOURCES])

    def test_note_for_the_output_never_claims_current_synchronization(self):
        for state, expected in ((True, "doložena"), (False, "nedoložena"), (None, "")):
            with self.subTest(state=state):
                with mock.patch.object(clock_sync, "timesyncd_first_sync", return_value=state):
                    note = first_sync_note()
                if expected:
                    self.assertIn("v tomto bootu", note)
                    self.assertTrue(note.endswith(expected))
                else:
                    self.assertEqual(note, "")


class ClockSyncTest(unittest.TestCase):
    def test_first_known_source_decides(self):
        sync = ClockSync((("jádro", lambda: True), ("soubor", lambda: False)))
        self.assertIs(sync.synced(), True)
        self.assertEqual(sync.source, "jádro")

    def test_unknown_source_falls_through(self):
        sync = ClockSync((("jádro", lambda: None), ("soubor", lambda: False)))
        self.assertIs(sync.synced(), False)
        self.assertEqual(sync.source, "soubor")

    def test_all_sources_unknown(self):
        sync = ClockSync((("jádro", lambda: None), ("soubor", lambda: None)))
        self.assertIsNone(sync.synced())
        self.assertEqual(sync.source, UNKNOWN_SOURCE)

    def test_failing_source_is_never_synchronized(self):
        def broken():
            raise RuntimeError("chyba zdroje")

        sync = ClockSync((("rozbitý", broken),))
        self.assertIsNone(sync.synced())
        self.assertEqual(sync.source, UNKNOWN_SOURCE)

    def test_loss_and_recovery_follow_the_source(self):
        states = [True, False, True]
        sync = ClockSync((("jádro", states.pop),))
        self.assertEqual([sync.synced(), sync.synced(), sync.synced()], [True, False, True])

    def test_kernel_is_the_only_source_of_the_current_state(self):
        self.assertEqual([name for name, _ in clock_sync.SOURCES], ["adjtimex"])

    def test_real_system_answer_is_one_of_three_states(self):
        sync = ClockSync()
        self.assertIn(sync.synced(), (True, False, None))
        self.assertIsInstance(sync.source, str)


class ChainTest(unittest.TestCase):
    """Celý řetězec zdrojů: co smí a nesmí přebít aktuální odpověď jádra."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name) / "timesync"
        self.marker = self.runtime / "synchronized"
        patch = mock.patch.multiple(clock_sync, TIMESYNCD_MARKER=str(self.marker),
                                    TIMESYNCD_RUNTIME=str(self.runtime))
        patch.start()
        self.addCleanup(patch.stop)

    def kernel(self, *maxerrors, result=5, status=STA_UNSYNC, time_sec=1_800_000_000):
        """Náhradní adjtimex pro celý řetězec; postupně vrací zadané meze chyby."""
        values = list(maxerrors)

        def call(pointer):
            timex = pointer._obj
            timex.status = status
            timex.time_sec = time_sec
            timex.maxerror = values[0] if len(values) == 1 else values.pop(0)
            return result

        return mock.patch.object(clock_sync, "_adjtimex", return_value=call)

    def mark_synchronized(self):
        self.runtime.mkdir(exist_ok=True)
        self.marker.touch()

    def test_all_four_combinations_of_kernel_answer_and_marker(self):
        """Marker nesmí rozhodovat o aktuálním stavu hodin v žádném směru."""
        cases = (
            ("jádro True, marker je", 1000, True, True, "adjtimex"),
            ("jádro False, marker je", MAX_ERROR_LIMIT_US, True, False, "adjtimex"),
            # Regrese: dřív marker vrátil True, i když jádro odpověď nedalo.
            ("jádro neznámé, marker je", None, True, None, UNKNOWN_SOURCE),
            # Regrese: dřív chybějící marker sám rozhodl o nesynchronizovaném stavu.
            ("jádro neznámé, marker není", None, False, None, UNKNOWN_SOURCE),
        )
        for name, maxerror, has_marker, expected, source in cases:
            with self.subTest(name):
                self.runtime.mkdir(exist_ok=True)
                if has_marker:
                    self.marker.touch()
                else:
                    self.marker.unlink(missing_ok=True)
                kernel = (mock.patch.object(clock_sync, "_adjtimex", return_value=None) if maxerror is None
                          else self.kernel(maxerror))
                with kernel:
                    sync = ClockSync()
                    self.assertIs(sync.synced(), expected)
                    self.assertEqual(sync.source, source)

    def test_old_marker_does_not_override_negative_kernel_answer(self):
        # Soubor timesyncd dokládá jen dřívější synchronizaci. Aktuální "ne" od
        # jádra proto musí zvítězit, i když marker z dřívějška zůstal.
        self.mark_synchronized()
        with self.kernel(MAX_ERROR_LIMIT_US):
            sync = ClockSync()
            self.assertIs(sync.synced(), False)
            self.assertEqual(sync.source, "adjtimex")

    def test_marker_alone_is_never_a_proof(self):
        # Bez nezávislého důkazu aktuální synchronizace je stav neznámý, i když
        # marker existuje a tvrdí dřívější úspěšnou synchronizaci.
        self.mark_synchronized()
        with mock.patch.object(clock_sync, "_adjtimex", return_value=None):
            sync = ClockSync()
            self.assertIsNone(sync.synced())
            self.assertEqual(sync.source, UNKNOWN_SOURCE)

    def test_unreadable_marker_does_not_change_the_decision(self):
        self.mark_synchronized()
        with mock.patch.object(os.path, "exists", side_effect=OSError("nelze")):
            with self.kernel(1000):
                self.assertIs(ClockSync().synced(), True)
            with mock.patch.object(clock_sync, "_adjtimex", return_value=None):
                self.assertIsNone(ClockSync().synced())

    def test_no_source_at_all_is_unknown(self):
        with mock.patch.object(clock_sync, "_adjtimex", return_value=None):
            sync = ClockSync()
            self.assertIsNone(sync.synced())
            self.assertEqual(sync.source, UNKNOWN_SOURCE)

    def test_limits_around_sixteen_seconds(self):
        # Těsně pod mezí synchronizováno, přesně na mezi ne, nad mezí je odpověď
        # nevěrohodná (jádro mez nepřekračuje), takže stav zůstává neznámý.
        for maxerror, expected in ((MAX_ERROR_LIMIT_US - 1, True), (MAX_ERROR_LIMIT_US, False),
                                   (MAX_ERROR_LIMIT_US + 1, None)):
            with self.subTest(maxerror=maxerror), self.kernel(maxerror):
                self.assertIs(ClockSync().synced(), expected)

    def test_first_synchronization_then_loss_and_recovery(self):
        # Marker po první synchronizaci zůstává; ztrátu i obnovu hlásí jen jádro.
        self.mark_synchronized()
        with self.kernel(1000, MAX_ERROR_LIMIT_US, 2000):
            sync = ClockSync()
            self.assertEqual([sync.synced(), sync.synced(), sync.synced()], [True, False, True])
            self.assertEqual(sync.source, "adjtimex")
        self.assertIs(timesyncd_first_sync(), True)  # marker jako historický doklad zůstal

    def test_wrong_system_time_decides_nothing(self):
        # Rok v kalendáři není důkaz ani protidůkaz: rozhoduje jen mez chyby.
        with self.kernel(1000, time_sec=0):  # hodiny v roce 1970
            self.assertIs(ClockSync().synced(), True)
        with self.kernel(MAX_ERROR_LIMIT_US, time_sec=1_800_000_000):  # správný rok
            self.assertIs(ClockSync().synced(), False)

    def test_clock_jump_does_not_change_the_answer(self):
        # Skok systémového času mezi dotazy nemění mez chyby, tedy ani výsledek;
        # skok řeší historie (clock.jump), ne tento zdroj.
        with self.kernel(1000, time_sec=1_800_000_000) as _:
            first = ClockSync().synced()
        with self.kernel(1000, time_sec=1_800_000_000 + 3600):
            second = ClockSync().synced()
        self.assertEqual((first, second), (True, True))

    def test_structure_layout_matches_the_real_kernel(self):
        # Kontrola rozložení struktury na této platformě: při chybném posunu polí
        # by čas z jádra neodpovídal skutečnému času procesu.
        call = clock_sync._adjtimex()
        if call is None:
            self.skipTest("adjtimex na tomto systému není k dispozici")
        timex = Timex()
        result = call(ctypes.byref(timex))
        self.assertTrue(0 <= result <= MAX_TIME_RESULT)
        self.assertLess(abs(timex.time_sec - time.time()), 60)
        # Se zapnutým STA_NANO (0x2000) je zlomek sekundy v nanosekundách, jinak
        # v mikrosekundách; mimo rozsah by znamenal posunutá pole struktury.
        self.assertLess(timex.time_usec, 1_000_000_000 if timex.status & 0x2000 else 1_000_000)
        self.assertGreaterEqual(timex.time_usec, 0)
        self.assertEqual(timex.modes, 0)  # dotaz nic nenastavil


class BootIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "boot_id"

    def test_valid_identifier(self):
        self.path.write_text(f"{BOOT_ID}\n", encoding="ascii")
        self.assertEqual(read_boot_id(self.path), BOOT_ID)

    def test_missing_file(self):
        self.assertIsNone(read_boot_id(self.path))

    def test_invalid_content(self):
        for content in ("", "neni-uuid\n", BOOT_ID.upper() + "\n", BOOT_ID[:-1] + "\n", "\x00\n"):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="ascii", errors="ignore")
                self.assertIsNone(read_boot_id(self.path))

    def test_real_boot_id_is_valid_or_none(self):
        value = read_boot_id()
        self.assertTrue(value is None or clock_sync.BOOT_ID.fullmatch(value))

    def test_boot_id_is_not_in_public_files(self):
        # Jedinečný identifikátor stroje nesmí být ve veřejném repozitáři.
        real = read_boot_id()
        if real is None:
            self.skipTest("boot_id není k dispozici")
        repo = Path(__file__).resolve().parent.parent
        for path in sorted(repo.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".md", ".toml"):
                self.assertNotIn(real, path.read_text(encoding="utf-8", errors="replace"), str(path))
