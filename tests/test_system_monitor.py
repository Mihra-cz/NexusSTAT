"""Testy read-only systémového kolektoru nad připravenými vzorovými vstupy.

Kromě jednoho smoke testu nečtou skutečný systém, takže nezávisí na teplotě
ani zatížení počítače, na kterém běží.
"""

import os
import tempfile
import unittest
from collections import namedtuple
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from nexusstat.core import ALERT_STATES, Measurement, State, active_problems, overall_state
from nexusstat.oled import UNKNOWN_SYMBOL, OledController, home_symbol
from nexusstat.oled_text import page_rows
from nexusstat.config import CollectorConfig, OledConfig
from nexusstat.simulate import sample_results
from nexusstat.system_monitor import (
    CPU_TEMP_ID,
    CPU_USAGE_ID,
    DISK_ROOT_ID,
    MEMORY_ID,
    CpuSample,
    SystemMonitor,
    cpu_usage_percent,
    disk_usage,
    find_cpu_thermal_zone,
    format_size,
    parse_cpu_sample,
    parse_meminfo,
    parse_temperature,
)

try:
    from nexusstat.render import render_screen
except ImportError:  # Pillow není nainstalované
    render_screen = None

OBSERVED = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
WALL_TIME = datetime(2026, 10, 19, 14, 0)

# Platnost z výchozí konfigurace [collector] (3 × 10 s). Kolektor vlastní nemá.
VALIDITY_SECONDS = CollectorConfig().validity_seconds

# Vzorové /proc/stat: user nice system idle iowait irq softirq steal guest guest_nice.
# První vzorek: celkem 1000, nečinnost (idle + iowait) 850.
STAT_1 = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 100 0 50 800 50 0 0 0 0 0\nintr 12345\n"
# Druhý vzorek: celkem 2000 (guest 30 se nesčítá), nečinnost 1650 → 20 % vytížení.
STAT_2 = "cpu  200 0 100 1550 100 0 50 0 30 0\ncpu0 200 0 100 1550 100 0 50 0 30 0\n"

MEMINFO = "MemTotal:        8000000 kB\nMemFree:         1000000 kB\nMemAvailable:    6000000 kB\n"

FakeStatvfs = namedtuple("FakeStatvfs", "f_frsize f_blocks f_bfree f_bavail")
# 4 KiB bloky: 191 GiB celkem, 10 M použito, 37,5 M dostupných → 21 % jako u `df`.
ROOT_STATVFS = FakeStatvfs(4096, 50_000_000, 40_000_000, 37_500_000)


def write_files(base, files):
    for relative, content in files.items():
        path = Path(base) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="ascii")


class FixtureCase(unittest.TestCase):
    """Dočasný adresář s kopií /proc a /sys/class/thermal ze vzorových souborů."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name) / "proc"
        self.thermal = Path(self.tmp.name) / "thermal"
        write_files(self.proc, {"stat": STAT_1, "meminfo": MEMINFO})
        # Zóna 0 je Wi-Fi, CPU je až zóna 1: volba nesmí záviset na čísle zóny.
        write_files(self.thermal, {
            "thermal_zone0/type": "iwlwifi_1\n",
            "thermal_zone0/temp": "39000\n",
            "thermal_zone1/type": "x86_pkg_temp\n",
            "thermal_zone1/temp": "55400\n",
        })
        self.statvfs_calls = []
        self.statvfs_result = ROOT_STATVFS

    def fake_statvfs(self, path):
        self.statvfs_calls.append(path)
        if isinstance(self.statvfs_result, Exception):
            raise self.statvfs_result
        return self.statvfs_result

    def monitor(self):
        return SystemMonitor(VALIDITY_SECONDS, proc_dir=self.proc, thermal_dir=self.thermal, statvfs=self.fake_statvfs)

    def by_id(self, results):
        return {result.id: result for result in results}

    def collect_twice(self, monitor=None, now=100):
        """První vzorek (STAT_1) a druhý (STAT_2), vrací výsledky druhého měření."""
        monitor = monitor or self.monitor()
        monitor.collect(now - 10, OBSERVED)
        write_files(self.proc, {"stat": STAT_2})
        return monitor.collect(now, OBSERVED)


class CpuParseTest(unittest.TestCase):
    def test_sample(self):
        self.assertEqual(parse_cpu_sample(STAT_1), CpuSample(idle=850, total=1000))

    def test_guest_not_double_counted(self):
        self.assertEqual(parse_cpu_sample(STAT_2), CpuSample(idle=1650, total=2000))

    def test_percent_from_two_samples(self):
        first, second = parse_cpu_sample(STAT_1), parse_cpu_sample(STAT_2)
        self.assertAlmostEqual(cpu_usage_percent(first, second), 20.0)

    def test_iowait_counts_as_idle(self):
        # Mezi vzorky přibyl jen iowait: CPU nepracovalo, vytížení 0 %.
        first = parse_cpu_sample("cpu 10 0 10 100 0 0 0 0\n")
        second = parse_cpu_sample("cpu 10 0 10 100 50 0 0 0\n")
        self.assertEqual(cpu_usage_percent(first, second), 0.0)

    def test_full_load(self):
        self.assertEqual(cpu_usage_percent(CpuSample(100, 200), CpuSample(100, 300)), 100.0)

    def test_old_kernel_with_four_fields(self):
        self.assertEqual(parse_cpu_sample("cpu 1 2 3 4\n"), CpuSample(idle=4, total=10))

    def test_invalid_intervals_give_none(self):
        cases = {
            "stejný vzorek (dělení nulou)": (CpuSample(850, 1000), CpuSample(850, 1000)),
            "čítače zpět": (CpuSample(850, 1000), CpuSample(800, 900)),
            "idle zpět": (CpuSample(850, 1000), CpuSample(840, 1100)),
            "idle víc než celkem": (CpuSample(850, 1000), CpuSample(1000, 1100)),
        }
        for name, (previous, current) in cases.items():
            with self.subTest(name):
                self.assertIsNone(cpu_usage_percent(previous, current))

    def test_corrupted_stat(self):
        for text in ("", "cpu0 1 2 3 4\n", "cpu 1 2 x 4\n", "cpu 1 2\n", "cpu 1 -2 3 4\n"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_cpu_sample(text)


class MemInfoParseTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_meminfo(MEMINFO), (8_000_000, 6_000_000))

    def test_invalid(self):
        cases = {
            "chybí MemAvailable": "MemTotal: 8000 kB\nMemFree: 100 kB\n",
            "chybí MemTotal": "MemAvailable: 6000 kB\n",
            "nulová paměť": "MemTotal: 0 kB\nMemAvailable: 0 kB\n",
            "dostupné víc než celkem": "MemTotal: 8000 kB\nMemAvailable: 9000 kB\n",
            "záporné": "MemTotal: 8000 kB\nMemAvailable: -1 kB\n",
            "nečíselné": "MemTotal: lots kB\nMemAvailable: 6000 kB\n",
            "jiná jednotka": "MemTotal: 8000 MB\nMemAvailable: 6000 MB\n",
            "bez jednotky": "MemTotal: 8000\nMemAvailable: 6000\n",
            "prázdné": "",
        }
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    parse_meminfo(text)


class DiskUsageTest(unittest.TestCase):
    def test_df_style_percent(self):
        percent, size = disk_usage(ROOT_STATVFS)
        # 10 M použitých / (10 M + 37,5 M dostupných); rezervované bloky se nepočítají.
        self.assertAlmostEqual(percent, 100 * 10 / 47.5)
        self.assertEqual(size, 4096 * 50_000_000)

    def test_full_disk(self):
        self.assertEqual(disk_usage(FakeStatvfs(4096, 1000, 0, 0))[0], 100.0)

    def test_invalid(self):
        cases = [
            FakeStatvfs(0, 1000, 500, 400),  # nulová velikost bloku
            FakeStatvfs(4096, 0, 0, 0),  # prázdný souborový systém
            FakeStatvfs(4096, 1000, 500, 600),  # dostupné víc než volné
            FakeStatvfs(4096, 1000, 1200, 100),  # volné víc než celkem
            FakeStatvfs(4096, 1000, 1000, 0),  # nic použitelného (vše rezervováno)
        ]
        for stat in cases:
            with self.subTest(stat=stat):
                with self.assertRaises(ValueError):
                    disk_usage(stat)

    def test_format_size(self):
        self.assertEqual(format_size(int(15.4 * 2**30)), "15,4 GiB")
        self.assertEqual(format_size(int(191.2 * 2**30)), "191 GiB")
        self.assertEqual(format_size(2000 * 2**30), "2,0 TiB")


class TemperatureTest(FixtureCase):
    def test_zone_chosen_by_type_not_number(self):
        self.assertEqual(find_cpu_thermal_zone(self.thermal).name, "thermal_zone1")

    def test_cpu_thermal_has_priority(self):
        write_files(self.thermal, {"thermal_zone7/type": "cpu-thermal\n", "thermal_zone7/temp": "50000\n"})
        self.assertEqual(find_cpu_thermal_zone(self.thermal).name, "thermal_zone7")

    def test_no_cpu_sensor(self):
        write_files(self.thermal, {"thermal_zone1/type": "acpitz\n"})
        self.assertIsNone(find_cpu_thermal_zone(self.thermal))

    def test_missing_thermal_dir(self):
        self.assertIsNone(find_cpu_thermal_zone(Path(self.tmp.name) / "neexistuje"))

    def test_zone_without_type_is_skipped(self):
        write_files(self.thermal, {"thermal_zone2/temp": "40000\n"})
        self.assertEqual(find_cpu_thermal_zone(self.thermal).name, "thermal_zone1")

    def test_parse_temperature(self):
        self.assertAlmostEqual(parse_temperature("55400\n"), 55.4)
        self.assertAlmostEqual(parse_temperature("125000"), 125.0)

    def test_invalid_temperature(self):
        # 0 °C je typická zástupná hodnota nepřipraveného senzoru, ne skutečná teplota.
        for text in ("0", "-5000", "125001", "200000", "abc", "", "55.4"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_temperature(text)


class CollectTest(FixtureCase):
    def test_first_collect(self):
        results = self.by_id(self.monitor().collect(50, OBSERVED))
        self.assertEqual(list(results), [CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID, DISK_ROOT_ID])
        # První vzorek CPU ještě nemá platné procento.
        self.assertEqual(results[CPU_USAGE_ID].state, State.UNKNOWN)
        self.assertEqual(results[CPU_USAGE_ID].summary, "CPU: první vzorek")
        self.assertEqual(results[CPU_TEMP_ID].summary, "Teplota CPU 55 °C")
        self.assertEqual(results[MEMORY_ID].summary, "RAM 25 % z 7,6 GiB")
        self.assertEqual(results[DISK_ROOT_ID].summary, "Disk / 21 % z 191 GiB")
        for check_id in (CPU_TEMP_ID, MEMORY_ID, DISK_ROOT_ID):
            self.assertEqual(results[check_id].state, State.INFO)

    def test_second_collect_has_cpu_percent(self):
        results = self.by_id(self.collect_twice())
        self.assertEqual(results[CPU_USAGE_ID].state, State.INFO)
        self.assertEqual(results[CPU_USAGE_ID].summary, "CPU 20 %")

    def test_numeric_values_and_units(self):
        results = self.by_id(self.collect_twice())
        # Nezaokrouhlené hodnoty pro prahy; text zůstává zaokrouhlený jako dřív.
        self.assertEqual(results[CPU_USAGE_ID].measurement, Measurement(20.0, "%"))
        self.assertEqual(results[CPU_TEMP_ID].measurement, Measurement(55.4, "°C"))
        self.assertEqual(results[MEMORY_ID].measurement, Measurement(25.0, "%"))
        self.assertEqual(results[DISK_ROOT_ID].measurement.unit, "%")
        self.assertAlmostEqual(results[DISK_ROOT_ID].measurement.value, 100 * 10 / 47.5)
        self.assertEqual(results[CPU_TEMP_ID].summary, "Teplota CPU 55 °C")

    def test_first_cpu_sample_has_no_value(self):
        cpu = self.by_id(self.monitor().collect(0, OBSERVED))[CPU_USAGE_ID]
        self.assertEqual((cpu.state, cpu.measurement), (State.UNKNOWN, None))

    def test_unchanged_counters_give_unknown(self):
        monitor = self.monitor()
        monitor.collect(10, OBSERVED)
        cpu = self.by_id(monitor.collect(20, OBSERVED))[CPU_USAGE_ID]
        self.assertEqual((cpu.state, cpu.summary), (State.UNKNOWN, "CPU: neplatný interval"))

    def test_validity_and_time_bases(self):
        for result in self.monitor().collect(1234.5, OBSERVED):
            self.assertEqual(result.expires_monotonic, 1234.5 + VALIDITY_SECONDS)
            self.assertEqual(result.observed_utc, OBSERVED)

    def test_validity_comes_from_collector_config(self):
        # Žádná pevná platnost: interval 7 s dává platnost 21 s.
        validity = CollectorConfig(interval_seconds=7).validity_seconds
        monitor = SystemMonitor(validity, proc_dir=self.proc, thermal_dir=self.thermal, statvfs=self.fake_statvfs)
        for result in monitor.collect(100, OBSERVED):
            self.assertEqual(result.expires_monotonic, 121)

    def test_non_finite_validity_is_rejected(self):
        # Regrese: NaN i nekonečno prošly jako "kladné" a výsledky by nikdy nevypršely.
        for validity in (float("nan"), float("inf"), 10**400):
            with self.subTest(validity=validity):
                with self.assertRaises(ValueError):
                    SystemMonitor(validity, proc_dir=self.proc)

    def test_invalid_validity_is_rejected(self):
        for validity in (0, -30, True, "30", None):
            with self.subTest(validity=validity):
                with self.assertRaises(ValueError):
                    SystemMonitor(validity, proc_dir=self.proc)

    def test_disk_checks_only_root(self):
        self.monitor().collect(10, OBSERVED)
        self.assertEqual(self.statvfs_calls, ["/"])

    def test_no_alarm_from_high_values_without_thresholds(self):
        # Vysoké hodnoty bez konfigurovaných prahů nejsou WARNING ani ERROR.
        write_files(self.proc, {"meminfo": "MemTotal: 8000000 kB\nMemAvailable: 1000 kB\n"})
        write_files(self.thermal, {"thermal_zone1/temp": "120000\n"})
        self.statvfs_result = FakeStatvfs(4096, 1000, 0, 0)
        monitor = self.monitor()
        monitor.collect(0, OBSERVED)
        write_files(self.proc, {"stat": "cpu  1100 0 50 800 50 0 0 0 0 0\n"})  # 100 % vytížení
        results = monitor.collect(10, OBSERVED)
        self.assertEqual({result.state for result in results}, {State.INFO})


class PartialFailureTest(FixtureCase):
    def assert_only_failed(self, results, failed_id, summary):
        results = self.by_id(results)
        self.assertEqual(results[failed_id].state, State.UNKNOWN)
        self.assertEqual(results[failed_id].summary, summary)
        self.assertIsNone(results[failed_id].measurement)  # chyba měření = hodnota nedostupná
        others = [r for check_id, r in results.items() if check_id not in (failed_id, CPU_USAGE_ID)]
        self.assertTrue(all(result.state is State.INFO for result in others), others)

    def test_missing_meminfo(self):
        (self.proc / "meminfo").unlink()
        self.assert_only_failed(self.collect_twice(), MEMORY_ID, "RAM: nedostupné")

    def test_corrupted_meminfo(self):
        write_files(self.proc, {"meminfo": "MemTotal: 8000000 kB\n"})
        self.assert_only_failed(self.collect_twice(), MEMORY_ID, "RAM: neplatná data")

    def test_statvfs_error(self):
        self.statvfs_result = OSError("simulovaná chyba")
        self.assert_only_failed(self.collect_twice(), DISK_ROOT_ID, "Disk /: nedostupné")

    def test_no_temperature_sensor(self):
        write_files(self.thermal, {"thermal_zone1/type": "acpitz\n"})
        self.assert_only_failed(self.collect_twice(), CPU_TEMP_ID, "Teplota CPU: senzor nenalezen")

    def test_invalid_temperature_is_unknown_not_zero(self):
        write_files(self.thermal, {"thermal_zone1/temp": "0\n"})
        self.assert_only_failed(self.collect_twice(), CPU_TEMP_ID, "Teplota CPU: neplatná data")

    def test_unreadable_temperature(self):
        (self.thermal / "thermal_zone1" / "temp").unlink()
        self.assert_only_failed(self.collect_twice(), CPU_TEMP_ID, "Teplota CPU: nedostupné")

    def test_non_ascii_garbage(self):
        (self.proc / "meminfo").write_bytes(b"MemTotal: \xff\xfe kB\n")
        self.assert_only_failed(self.collect_twice(), MEMORY_ID, "RAM: neplatná data")

    def test_corrupted_stat_only_breaks_cpu(self):
        monitor = self.monitor()
        monitor.collect(0, OBSERVED)
        write_files(self.proc, {"stat": "nesmysl\n"})
        results = self.by_id(monitor.collect(10, OBSERVED))
        self.assertEqual(results[CPU_USAGE_ID].summary, "CPU: neplatná data")
        for check_id in (CPU_TEMP_ID, MEMORY_ID, DISK_ROOT_ID):
            self.assertIs(results[check_id].state, State.INFO)

    def test_huge_temperature_value_only_breaks_temperature(self):
        # Regrese: 400místné číslo prošlo int(), ale dělení přeteklo (OverflowError)
        # a shodilo celé collect(), tedy i ostatní kontroly.
        write_files(self.thermal, {"thermal_zone1/temp": "1" + "0" * 400 + "\n"})
        self.assert_only_failed(self.collect_twice(), CPU_TEMP_ID, "Teplota CPU: neplatná data")

    def test_huge_memory_value_only_breaks_memory(self):
        write_files(self.proc, {"meminfo": "MemTotal: 1" + "0" * 400 + " kB\nMemAvailable: 1 kB\n"})
        self.assert_only_failed(self.collect_twice(), MEMORY_ID, "RAM: neplatná data")

    def test_everything_broken_still_returns_all(self):
        empty = Path(self.tmp.name) / "prazdne"
        self.statvfs_result = OSError("simulovaná chyba")
        monitor = SystemMonitor(VALIDITY_SECONDS, proc_dir=empty, thermal_dir=empty, statvfs=self.fake_statvfs)
        results = monitor.collect(10, OBSERVED)
        self.assertEqual(len(results), 4)
        self.assertEqual({result.state for result in results}, {State.UNKNOWN})
        self.assertEqual({result.measurement for result in results}, {None})


class SimulatedFlagTest(FixtureCase):
    def test_real_sources_are_not_simulated(self):
        self.assertFalse(SystemMonitor(VALIDITY_SECONDS).simulated)

    def test_real_paths_in_any_form_are_not_simulated(self):
        # Regrese: Path("/proc") se porovnával s řetězcem "/proc" a skutečná
        # data by se na displeji chybně označila jako SIM.
        self.assertFalse(SystemMonitor(VALIDITY_SECONDS, proc_dir=Path("/proc")).simulated)
        self.assertFalse(
            SystemMonitor(VALIDITY_SECONDS, proc_dir="/proc/", thermal_dir=Path("/sys/class/thermal")).simulated
        )

    def test_any_replaced_source_is_simulated(self):
        self.assertTrue(self.monitor().simulated)
        self.assertTrue(SystemMonitor(VALIDITY_SECONDS, statvfs=self.fake_statvfs).simulated)
        self.assertTrue(SystemMonitor(VALIDITY_SECONDS, proc_dir=self.proc).simulated)
        self.assertTrue(all(result.simulated for result in self.monitor().collect(0, OBSERVED)))

    def test_simulator_sample_data_is_simulated(self):
        self.assertTrue(all(result.simulated for result in sample_results(OBSERVED)))

    @unittest.skipUnless(os.path.exists("/proc/stat"), "není Linux s /proc")
    def test_real_system_smoke(self):
        # Jediný test nad skutečným systémem: kontroluje jen strukturu, ne hodnoty.
        monitor = SystemMonitor(VALIDITY_SECONDS)
        monitor.collect(0, OBSERVED)
        results = monitor.collect(1, OBSERVED)
        self.assertEqual([r.id for r in results], [CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID, DISK_ROOT_ID])
        for result in results:
            self.assertFalse(result.simulated)
            self.assertIn(result.state, (State.INFO, State.UNKNOWN))


class CoreAndOledIntegrationTest(FixtureCase):
    def test_value_pages(self):
        results = self.collect_twice(now=100)
        # Displej ukazuje číselnou hodnotu zaokrouhlenou stejně jako summary, anglicky.
        # Kolektor systému ventilátor neměří: jeho řádek zůstává UNKN, dokud
        # výsledky nepřijdou od FanMonitor (M4-1).
        self.assertEqual(page_rows("SYSTEM", results, 100),
                         [("CPU", "20%"), ("CPU TEMP", "55°C"), ("RAM", "25%"), ("CPU FAN", "UNKN")])
        self.assertEqual(page_rows("STORAGE", results, 100)[:1], [("ROOT", "21%")])

    def test_valid_values_give_no_symbol_and_no_problems(self):
        results = self.collect_twice(now=100)
        self.assertEqual(overall_state(results, 100), State.INFO)
        self.assertIsNone(home_symbol(results, 100))
        self.assertEqual(active_problems(results, 100), [])

    def test_unavailable_value_is_unkn_not_na(self):
        write_files(self.thermal, {"thermal_zone1/type": "acpitz\n"})
        results = self.collect_twice(now=100)
        # UNKN, ne N/A: senzor sledujeme, jen jeho hodnotu neznáme.
        self.assertIn(("CPU TEMP", "UNKN"), page_rows("SYSTEM", results, 100))
        problems = active_problems(results, 100)
        # Diagnostický stav je UNKNOWN, ne State.NA (záměrně nesledovaná integrace).
        self.assertEqual([(p.id, p.state) for p in problems], [(CPU_TEMP_ID, State.UNKNOWN)])
        self.assertEqual(problems[0].summary, "Teplota CPU: senzor nenalezen")
        self.assertEqual(home_symbol(results, 100), UNKNOWN_SYMBOL)

    def test_first_sample_shows_question_mark(self):
        results = self.monitor().collect(0, OBSERVED)
        self.assertEqual(home_symbol(results, 0), UNKNOWN_SYMBOL)
        self.assertEqual(page_rows("SYSTEM", results, 0)[0], ("CPU", "UNKN"))

    def test_expired_results_become_unkn_and_unknown(self):
        results = self.collect_twice(now=100)
        expired_at = 100 + VALIDITY_SECONDS
        self.assertEqual(page_rows("STORAGE", results, expired_at - 0.001)[:1], [("ROOT", "21%")])
        self.assertEqual(page_rows("STORAGE", results, expired_at)[:1], [("ROOT", "UNKN")])
        self.assertEqual(overall_state(results, expired_at), State.UNKNOWN)
        self.assertEqual(len(active_problems(results, expired_at)), 4)

    def test_missing_results_show_unkn(self):
        self.assertEqual(page_rows("SYSTEM", [], 0),
                         [("CPU", "UNKN"), ("CPU TEMP", "UNKN"), ("RAM", "UNKN"), ("CPU FAN", "UNKN")])

    def test_collector_problems_drive_status_paging(self):
        results = self.monitor().collect(0, OBSERVED)  # jediný problém: první vzorek CPU
        controller = OledController(OledConfig())
        controller.press(0, 0)
        controller.press(1, len(active_problems(results, 1)))
        controller.press(2, len(active_problems(results, 2)))  # jedna stránka → zpět
        self.assertEqual(controller.current_page(2), "NEXUS")

    @unittest.skipIf(render_screen is None, "Pillow není dostupné")
    def test_render_value_pages_with_sim_mark(self):
        simulated = self.collect_twice(now=100)
        real = [replace(result, simulated=False) for result in simulated]
        controller = OledController(OledConfig(), pages=("SYSTEM",))
        controller.press(100, 0)
        sim_image = render_screen(controller, 100, simulated, WALL_TIME)
        real_image = render_screen(controller, 100, real, WALL_TIME)
        self.assertNotEqual(sim_image.tobytes(), real_image.tobytes())  # liší se jen značkou SIM
        # Pravý horní roh (značka SIM) je u skutečných dat prázdný.
        self.assertIsNone(real_image.crop((100, 0, 128, 13)).getbbox())
        self.assertIsNotNone(sim_image.crop((100, 0, 128, 13)).getbbox())

    @unittest.skipIf(render_screen is None, "Pillow není dostupné")
    def test_render_unavailable_values(self):
        controller = OledController(OledConfig(), pages=("STORAGE",))
        controller.press(0, 0)
        image = render_screen(controller, 0, [], WALL_TIME)
        self.assertIsNotNone(image.getbbox())

    def test_no_alert_states_from_collector(self):
        results = self.collect_twice(now=100)
        self.assertFalse(any(result.state in ALERT_STATES for result in results))


class LiveScheduleTest(FixtureCase):
    """SCHED-2: výběr kontrol a vytížení CPU za celé okno zápisu do historie."""

    def stat(self, idle, total):
        write_files(self.proc, {"stat": f"cpu  {total - idle} 0 0 {idle} 0 0 0 0 0 0\n"})

    def test_checks_subset(self):
        fast = SystemMonitor(15, proc_dir=self.proc, thermal_dir=self.thermal, statvfs=self.fake_statvfs,
                             checks=(CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID))
        self.assertEqual([r.id for r in fast.collect(0, OBSERVED)], [CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID])
        self.assertEqual(self.statvfs_calls, [])  # disk se v rychlém kolektoru neměří
        disk = SystemMonitor(5400, proc_dir=self.proc, thermal_dir=self.thermal, statvfs=self.fake_statvfs,
                             checks=(DISK_ROOT_ID,))
        self.assertEqual((list(disk.labels), disk.window_results(0, OBSERVED, 30)), ([DISK_ROOT_ID], (set(), [])))
        for checks in ((), ("system.neznamy",), (CPU_USAGE_ID, CPU_USAGE_ID)):
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                SystemMonitor(15, proc_dir=self.proc, checks=checks)

    def test_cpu_window_sums_every_step(self):
        monitor = SystemMonitor(15, proc_dir=self.proc, thermal_dir=self.thermal, statvfs=self.fake_statvfs,
                                checks=(CPU_USAGE_ID,))

        def window(now):
            ids, results = monitor.window_results(now, OBSERVED, 30)
            self.assertEqual(ids, {CPU_USAGE_ID})
            return [(r.state, r.summary, r.expires_monotonic) for r in results]

        self.stat(800, 1000)
        monitor.collect(0, OBSERVED)
        self.assertEqual(window(0), [(State.UNKNOWN, "CPU: první vzorek", 30)])
        self.stat(890, 1100)  # krok 1: 10 %
        monitor.collect(5, OBSERVED)
        self.stat(910, 1200)  # krok 2: 80 %
        monitor.collect(10, OBSERVED)
        self.assertEqual(window(10), [(State.INFO, "CPU 45 %", 40)])  # 90 z 200, ne jen poslední krok
        (self.proc / "stat").unlink()
        self.assertIs(monitor.collect(15, OBSERVED)[0].state, State.UNKNOWN)
        self.stat(1000, 1400)  # další krok přesně překlene selhané čtení: 110 ze 200
        monitor.collect(20, OBSERVED)
        self.assertEqual(window(20), [(State.INFO, "CPU 55 %", 50)])
        self.stat(900, 1300)  # čítače zpět: krok se zahodí
        monitor.collect(25, OBSERVED)
        self.stat(950, 1400)
        monitor.collect(30, OBSERVED)
        self.assertEqual(window(30), [(State.UNKNOWN, "CPU: neplatný interval v okně zápisu", 60)])
        self.assertEqual(window(30), [])  # okno bez kroků: mezera, ne opakovaný vzorek


if __name__ == "__main__":
    unittest.main()
