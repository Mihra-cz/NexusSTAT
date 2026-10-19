"""Testy pravidelné smyčky měření. Hodiny, čekání i kolektor jsou simulované, nic se skutečně nečeká."""

import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from nexusstat.alarms import AlarmEvaluator
from nexusstat.config import AlarmRule, AlarmsConfig, CollectorConfig, OledConfig
from nexusstat.core import CheckResult, Measurement, State, active_problems, effective, overall_state
from nexusstat.oled import UNKNOWN_SYMBOL, Mode, OledController, home_symbol, page_lines
from nexusstat.run import failed_results, format_line, main, run_loop
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, LABELS, MEMORY_ID

# Monotónní čas nezačíná nulou, aby nic nezáviselo na náhodné shodě s 0.
START = 5000.0
START_UTC = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
CONFIG = CollectorConfig()  # interval 10 s, platnost 30 s

VALUES = {
    CPU_USAGE_ID: "CPU 5 %",
    CPU_TEMP_ID: "Teplota CPU 48 °C",
    MEMORY_ID: "RAM 24 % z 7,9 GiB",
    DISK_ROOT_ID: "Disk / 31 % z 117 GiB",
}


class RunawayLoop(BaseException):
    """Pojistka testu: smyčka měří bez konce. BaseException, aby ji smyčka nespolkla jako výpadek."""


class FakeClock:
    """Simulované monotónní hodiny, UTC hodiny a sleep. Od času `stop_at` vyvolá Ctrl+C."""

    def __init__(self, stop_at, start=START):
        self.now = start
        self.stop_at = stop_at
        self.sleeps = []
        self.oversleep = 0.0  # nepřesnost uspání, přičte se ke každému sleep
        self.pauses = {}  # pořadí volání sleep (od 1) -> sekundy navíc (pozastavený proces)

    def __call__(self):
        return self.now

    def utc(self):
        return START_UTC + timedelta(seconds=self.now - START)

    def sleep(self, seconds):
        if seconds < 0:
            raise ValueError("záporné čekání")  # stejně jako time.sleep
        self.sleeps.append(seconds)
        if len(self.sleeps) > 10_000:
            raise RunawayLoop  # aktivní čekání: smyčka spí, ale čas neubíhá
        self.now += seconds + self.oversleep + self.pauses.get(len(self.sleeps), 0)
        if self.now >= self.stop_at:
            raise KeyboardInterrupt


class FakeCollector:
    """Náhrada SystemMonitor s fiktivními výsledky; umí pomalé měření i výpadek."""

    simulated = True

    def __init__(self, clock, duration=0.25):
        self.clock = clock
        self.duration = duration  # doba jednoho měření
        self.stalls = {}  # pořadí měření (od 1) -> sekundy navíc (zaseknuté měření)
        self.errors = {}  # pořadí měření (od 1) -> výjimka místo výsledků
        self.temperatures = []  # volitelně: číselná teplota CPU pro jednotlivá měření
        self.calls = []  # monotónní časy `now` předané kolektoru
        self.finished = []  # kdy byly výsledky k dispozici (konec měření)
        self.returned = []

    def collect(self, now, observed_utc):
        self.calls.append(now)
        number = len(self.calls)
        if number > 10_000:
            raise RunawayLoop
        self.clock.now += self.duration + self.stalls.get(number, 0)
        if number in self.errors:
            raise self.errors[number]
        results = [
            CheckResult(check_id, State.INFO, summary, observed_utc, now + CONFIG.validity_seconds, simulated=True)
            for check_id, summary in VALUES.items()
        ]
        if self.temperatures:
            celsius = self.temperatures[number - 1]
            results[1] = CheckResult(
                CPU_TEMP_ID, State.INFO, f"Teplota CPU {celsius:.0f} °C", observed_utc, now + CONFIG.validity_seconds,
                simulated=True, measurement=Measurement(celsius, "°C"),
            )
        self.finished.append(self.clock.now)
        self.returned.append(results)
        return results


def run(clock, collector, config=CONFIG, utc_clock=None, alarms=AlarmsConfig()):
    """Spustí smyčku se simulovaným časem; vrací (poslední výsledky, řádky výpisu)."""
    lines = []
    results = run_loop(
        collector, config, AlarmEvaluator(alarms),
        clock=clock, utc_clock=utc_clock or clock.utc, sleep=clock.sleep, output=lines.append,
    )
    return results, lines


# Ilustrační prahy jen pro testy; nejde o schválené provozní hodnoty.
TEMP_RULE = AlarmsConfig(cpu_temp=AlarmRule(enabled=True, warning=70, error=80, hysteresis=5))


def offsets(times):
    """Časy relativně k START, aby se v testech daly číst plánované sekundy."""
    return [time - START for time in times]


class ScheduleTest(unittest.TestCase):
    def test_first_measurement_immediately_then_every_interval(self):
        clock = FakeClock(stop_at=START + 35)
        collector = FakeCollector(clock)
        run(clock, collector)
        self.assertEqual(offsets(collector.calls), [0, 10, 20, 30])

    def test_sleeps_once_per_interval_without_busy_waiting(self):
        clock = FakeClock(stop_at=START + 35)
        collector = FakeCollector(clock, duration=0.25)
        run(clock, collector)
        # Jedno uspání na měření, dlouhé zbytek intervalu po měření. Žádné krátké
        # opakované čekání ani volání bez uspání.
        self.assertEqual(clock.sleeps, [9.75] * 4)

    def test_measurement_time_and_oversleep_do_not_drift(self):
        clock = FakeClock(stop_at=START + 9995)
        clock.oversleep = 0.004
        collector = FakeCollector(clock, duration=0.37)
        run(clock, collector)
        self.assertEqual(len(collector.calls), 1000)
        # Naivní sleep(interval) po měření by za 1000 měření ujel o 374 s.
        # Tady zůstává každé měření jen o jedno nepřesné uspání po plánu.
        for index, now in enumerate(collector.calls):
            with self.subTest(index=index):
                self.assertGreaterEqual(now - (START + 10 * index), 0)
                self.assertLess(now - (START + 10 * index), 0.01)

    def test_interval_from_config(self):
        clock = FakeClock(stop_at=START + 10)
        collector = FakeCollector(clock)
        run(clock, collector, config=CollectorConfig(interval_seconds=3))
        self.assertEqual(offsets(collector.calls), [0, 3, 6, 9])

    def test_schedule_uses_monotonic_clock_not_utc(self):
        # Skok systémového času (např. NTP o hodinu zpět) nesmí posunout plán.
        # Do výsledků jde UTC čas tak, jak ho systém v okamžiku měření hlásí.
        utc_values = [START_UTC, START_UTC - timedelta(hours=1), START_UTC + timedelta(days=1), START_UTC]
        utc_clock = iter(utc_values).__next__
        clock = FakeClock(stop_at=START + 35)
        collector = FakeCollector(clock)
        _, lines = run(clock, collector, utc_clock=utc_clock)
        self.assertEqual(offsets(collector.calls), [0, 10, 20, 30])
        self.assertEqual([results[0].observed_utc for results in collector.returned], utc_values)
        self.assertTrue(lines[2].startswith("2026-10-19 11:00:00 UTC"))


class DelayTest(unittest.TestCase):
    def test_short_delay_keeps_schedule(self):
        clock = FakeClock(stop_at=START + 35)
        collector = FakeCollector(clock)
        collector.stalls = {2: 4}  # druhé měření trvá 4,25 s
        _, lines = run(clock, collector)
        self.assertEqual(offsets(collector.calls), [0, 10, 20, 30])
        self.assertFalse(any("Zpoždění" in line for line in lines))

    def test_long_measurement_restarts_interval_without_catch_up(self):
        clock = FakeClock(stop_at=START + 70)
        collector = FakeCollector(clock)
        collector.stalls = {2: 35}  # měření v čase 10 skončí až v 45,25
        _, lines = run(clock, collector)
        # Zmeškané termíny 20, 30 a 40 se nedohánějí: jedno měření hned
        # po zpoždění a další až o celý interval později.
        self.assertEqual(offsets(collector.calls), [0, 10, 45.25, 55.25, 65.25])
        self.assertEqual(sum("Zpoždění" in line for line in lines), 1)

    def test_paused_process_restarts_interval(self):
        clock = FakeClock(stop_at=START + 135)
        clock.pauses = {2: 100}  # proces pozastavený 100 s během druhého čekání
        collector = FakeCollector(clock)
        run(clock, collector)
        self.assertEqual(offsets(collector.calls), [0, 10, 120, 130])

    def test_delay_of_exactly_one_interval_does_not_burst(self):
        # Hranice: o přesně jeden interval pozdě. Bez restartu by další termín
        # (30) už uplynul a hned by následovalo druhé, doháněcí měření.
        clock = FakeClock(stop_at=START + 45)
        collector = FakeCollector(clock)
        collector.stalls = {2: 19.75}  # měření v čase 10 skončí přesně v 30
        run(clock, collector)
        self.assertEqual(offsets(collector.calls), [0, 10, 30, 40])

    def test_delay_shorter_than_interval_stays_on_original_schedule(self):
        # Zdokumentované chování: menší zpoždění plán neposouvá, takže další
        # měření může přijít dřív než za celý interval.
        clock = FakeClock(stop_at=START + 45)
        collector = FakeCollector(clock)
        collector.stalls = {2: 15}  # měření v čase 10 skončí v 25,25
        run(clock, collector)
        self.assertEqual(offsets(collector.calls), [0, 10, 25.25, 30, 40])


class ExpiryTest(unittest.TestCase):
    def test_results_are_valid_for_three_intervals(self):
        clock = FakeClock(stop_at=START + 25)
        results, _ = run(clock, FakeCollector(clock))  # poslední měření v čase 20
        for result in results:
            self.assertEqual(result.expires_monotonic, START + 20 + 30)
            self.assertIs(effective(result, START + 49.999).state, State.INFO)
            stale = effective(result, START + 50)
            self.assertIs(stale.state, State.UNKNOWN)
            self.assertIn("zastaralá data", stale.summary)

    def test_values_do_not_expire_between_regular_or_slightly_late_measurements(self):
        clock = FakeClock(stop_at=START + 60)
        collector = FakeCollector(clock)
        collector.stalls = {3: 15}  # měření v čase 20 se zasekne na 15 s
        run(clock, collector)
        self.assertEqual(len(collector.returned), 6)
        # Předchozí výsledky platí, dokud nedorazí nové (konec dalšího měření).
        for previous, new_available_at in zip(collector.returned, collector.finished[1:]):
            with self.subTest(new_available_at=new_available_at - START):
                self.assertIs(overall_state(previous, new_available_at), State.INFO)

    def test_hung_collector_lets_values_expire(self):
        clock = FakeClock(stop_at=START + 50)
        collector = FakeCollector(clock)
        collector.stalls = {2: 35}  # měření v čase 10 visí do 45,25
        run(clock, collector)
        first = collector.returned[0]  # z času 0, platí do 30
        # Během zaseknutí by displej ukazoval jen to, co víme: od 30 s UNKNOWN.
        self.assertIs(overall_state(first, START + 29.9), State.INFO)
        self.assertIs(overall_state(first, START + 30), State.UNKNOWN)
        self.assertEqual(page_lines("SYSTEM", first, START + 40), ["CPU N/A", "Teplota CPU N/A", "RAM N/A"])


class CollectorFailureTest(unittest.TestCase):
    def test_failure_is_reported_and_loop_continues(self):
        clock = FakeClock(stop_at=START + 25)
        collector = FakeCollector(clock)
        collector.errors = {2: RuntimeError("simulovaný výpadek")}
        results, lines = run(clock, collector)
        self.assertEqual(len(collector.calls), 3)
        failure_line = lines[2]
        self.assertIn("UNKNOWN", failure_line)
        self.assertIn("měření selhalo: RuntimeError: simulovaný výpadek", failure_line)
        # Po výpadku kolektor znovu měří a hodnoty jsou zpět.
        self.assertIs(overall_state(results, START + 20), State.INFO)

    def test_failure_replaces_still_valid_values_with_unknown(self):
        clock = FakeClock(stop_at=START + 25)
        collector = FakeCollector(clock)
        collector.errors = {3: RuntimeError("simulovaný výpadek")}
        results, _ = run(clock, collector)
        now = START + 20
        # Výsledky z času 10 by ještě platily do 40, ale nesmí se podržet jako čerstvé.
        self.assertEqual([result.id for result in results], list(LABELS))
        self.assertEqual({result.state for result in results}, {State.UNKNOWN})
        self.assertEqual(results[0].summary, "CPU: měření selhalo")
        self.assertIs(overall_state(results, now), State.UNKNOWN)
        self.assertEqual(home_symbol(results, now), UNKNOWN_SYMBOL)
        self.assertEqual(page_lines("DISK", results, now), ["Disk / N/A"])
        self.assertEqual(len(active_problems(results, now)), 4)
        for result in results:
            self.assertEqual(result.observed_utc, START_UTC + timedelta(seconds=20))
            self.assertEqual(result.expires_monotonic, now + CONFIG.validity_seconds)
            self.assertTrue(result.simulated)  # selhal simulovaný kolektor

    def test_failure_validity_comes_from_config(self):
        clock = FakeClock(stop_at=START + 2)
        collector = FakeCollector(clock)
        collector.errors = {1: OSError("simulovaný výpadek")}
        results, _ = run(clock, collector, config=CollectorConfig(interval_seconds=4))
        self.assertEqual({result.expires_monotonic for result in results}, {START + 12})

    def test_repeated_failures_never_report_valid_state(self):
        clock = FakeClock(stop_at=START + 35)
        collector = FakeCollector(clock)
        collector.errors = {number: ValueError("vadná data") for number in range(1, 5)}
        results, lines = run(clock, collector)
        self.assertEqual(len(collector.calls), 4)
        measurement_lines = lines[1:-1]
        self.assertEqual(len(measurement_lines), 4)
        for line in measurement_lines:
            self.assertIn(" UNKNOWN ", line)
            self.assertNotIn(" INFO ", line)
            self.assertNotIn(" OK ", line)
        self.assertIs(overall_state(results, START + 30), State.UNKNOWN)

    def test_failure_of_real_collector_is_not_marked_simulated(self):
        real_collector = mock.Mock(simulated=False)
        results = failed_results(real_collector, START, START_UTC, 30)
        self.assertFalse(any(result.simulated for result in results))
        self.assertNotIn("SIM", format_line(results, START, START_UTC, "měření selhalo"))


class TerminationTest(unittest.TestCase):
    def test_ctrl_c_while_waiting_ends_cleanly(self):
        clock = FakeClock(stop_at=START + 35)
        results, lines = run(clock, FakeCollector(clock))  # KeyboardInterrupt ze sleep
        self.assertEqual(lines[-1], "\nUkončeno (Ctrl+C), počet měření: 4.")
        self.assertEqual(len(results), 4)

    def test_ctrl_c_during_measurement_is_not_a_failure(self):
        clock = FakeClock(stop_at=START + 1000)
        collector = FakeCollector(clock)
        collector.errors = {3: KeyboardInterrupt()}
        results, lines = run(clock, collector)
        self.assertEqual(len(collector.calls), 3)
        self.assertFalse(any("selhalo" in line for line in lines))
        self.assertEqual(lines[-1], "\nUkončeno (Ctrl+C), počet měření: 2.")
        # Vrátí poslední dokončené měření, ne náhradní UNKNOWN.
        self.assertEqual(results, collector.returned[-1])

    def test_ctrl_c_before_first_measurement(self):
        clock = FakeClock(stop_at=START + 1000)
        collector = FakeCollector(clock)
        collector.errors = {1: KeyboardInterrupt()}
        results, lines = run(clock, collector)
        self.assertEqual(results, [])
        self.assertEqual(lines[-1], "\nUkončeno (Ctrl+C), počet měření: 0.")


class OutputTest(unittest.TestCase):
    def test_header_and_measurement_lines(self):
        clock = FakeClock(stop_at=START + 15)
        _, lines = run(clock, FakeCollector(clock))
        self.assertIn("každých 10 s, platnost výsledků 30 s", lines[0])
        self.assertIn("Prahy: Teplota CPU vypnuto; Disk / vypnuto", lines[0])
        self.assertIn("Ctrl+C", lines[0])
        self.assertEqual(
            lines[1],
            "2026-10-19 12:00:00 UTC  SIM INFO     CPU 5 % | Teplota CPU 48 °C | RAM 24 % z 7,9 GiB | Disk / 31 % z 117 GiB",
        )
        self.assertTrue(lines[2].startswith("2026-10-19 12:00:10 UTC  SIM INFO "))

    def test_real_results_have_no_sim_mark(self):
        result = CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", START_UTC, START + 30, simulated=False)
        self.assertEqual(format_line([result], START, START_UTC, "CPU 5 %"), "2026-10-19 12:00:00 UTC  INFO     CPU 5 %")

    def test_no_results_are_not_ok(self):
        self.assertIn(" UNKNOWN ", format_line([], START, START_UTC, ""))


class AlarmLoopTest(unittest.TestCase):
    def test_default_config_never_raises_alarm(self):
        clock = FakeClock(stop_at=START + 55)
        collector = FakeCollector(clock)
        collector.temperatures = [95] * 6
        results, lines = run(clock, collector)
        self.assertNotIn("WARNING", "".join(lines[1:]))
        self.assertNotIn("ERROR", "".join(lines[1:]))
        self.assertIs(overall_state(results, START + 50), State.INFO)

    def test_loop_reports_confirmed_alarm_and_which_value(self):
        clock = FakeClock(stop_at=START + 55)
        collector = FakeCollector(clock)
        collector.temperatures = [60, 72, 72, 72, 85, 85]
        results, lines = run(clock, collector, alarms=TEMP_RULE)
        self.assertIn("Prahy: Teplota CPU WARNING od 70 °C, ERROR od 80 °C; Disk / vypnuto", lines[0])
        states = [line.split()[4] for line in lines[1:7]]  # za "UTC  SIM"
        self.assertEqual(states, ["INFO", "INFO", "INFO", "WARNING", "WARNING", "WARNING"])
        self.assertIn("Teplota CPU 72 °C [WARNING]", lines[4])
        self.assertIn("CPU 5 % |", lines[4])  # ostatní hodnoty beze značky
        self.assertTrue(lines[4].startswith("2026-10-19 12:00:30 UTC  SIM WARNING"))  # simulace je vidět
        temperature = results[1]
        self.assertEqual((temperature.state, temperature.measurement.value), (State.WARNING, 85))

    def test_collector_failure_interrupts_confirmation(self):
        clock = FakeClock(stop_at=START + 55)
        collector = FakeCollector(clock)
        collector.temperatures = [72, 72, None, 72, 72, 72]
        collector.errors = {3: RuntimeError("simulovaný výpadek")}
        _, lines = run(clock, collector, alarms=TEMP_RULE)
        states = [line.split()[4] for line in lines[1:7]]
        # Výpadek přeruší řadu: WARNING až po třech platných měřeních po něm.
        self.assertEqual(states, ["INFO", "INFO", "UNKNOWN", "INFO", "INFO", "WARNING"])

    def test_collector_failure_does_not_resolve_alarm(self):
        clock = FakeClock(stop_at=START + 65)
        collector = FakeCollector(clock)
        collector.temperatures = [72, 72, 72, None, 50, 50, 50]
        collector.errors = {4: RuntimeError("simulovaný výpadek")}
        _, lines = run(clock, collector, alarms=TEMP_RULE)
        states = [line.split()[4] for line in lines[1:8]]
        self.assertEqual(states, ["INFO", "INFO", "WARNING", "UNKNOWN", "WARNING", "INFO", "INFO"])


class OledCompatibilityTest(unittest.TestCase):
    def test_loop_results_drive_oled_until_they_expire(self):
        clock = FakeClock(stop_at=START + 25)
        results, _ = run(clock, FakeCollector(clock))  # poslední měření v čase 20, platí do 50
        controller = OledController(OledConfig())
        controller.press(START + 21, problem_count=len(active_problems(results, START + 21)))
        controller.update(START + 31)
        self.assertEqual(controller.current_page(START + 31), "SYSTEM")
        self.assertEqual(page_lines("SYSTEM", results, START + 31), [VALUES[CPU_USAGE_ID], VALUES[CPU_TEMP_ID], VALUES[MEMORY_ID]])
        self.assertIsNone(home_symbol(results, START + 31))

        # Další měření nepřišlo: po třech intervalech hodnoty zmizí a STATUS ukáže UNKNOWN.
        expired = START + 50
        self.assertEqual(page_lines("DISK", results, expired), ["Disk / N/A"])
        self.assertEqual(home_symbol(results, expired), UNKNOWN_SYMBOL)
        problems = active_problems(results, expired)
        self.assertEqual({problem.state for problem in problems}, {State.UNKNOWN})
        controller.press(expired, problem_count=len(problems))
        self.assertIs(controller.mode, Mode.STATUS)


class MainTest(unittest.TestCase):
    def write_config(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def run_main(self, argv):
        stderr = io.StringIO()
        with mock.patch("nexusstat.run.run_loop") as run_loop_mock, contextlib.redirect_stderr(stderr):
            main(argv)
        return run_loop_mock, stderr.getvalue()

    def test_validity_derived_from_configured_interval(self):
        run_loop_mock, _ = self.run_main(["--config", self.write_config("[collector]\ninterval_seconds = 5\n")])
        monitor, config, _ = run_loop_mock.call_args.args
        self.assertEqual(config, CollectorConfig(interval_seconds=5))
        self.assertEqual(monitor.validity_seconds, 15)
        self.assertFalse(monitor.simulated)  # skutečný read-only kolektor

    def test_defaults_without_config(self):
        run_loop_mock, _ = self.run_main([])
        monitor, config, evaluator = run_loop_mock.call_args.args
        self.assertEqual(config, CollectorConfig())
        self.assertEqual(monitor.validity_seconds, 30)
        self.assertFalse(any(rule.enabled for rule, _ in evaluator.rules.values()))

    def test_alarm_rules_from_config(self):
        text = "[alarms.disk_root]\nenabled = true\nwarning = 85\nerror = 95\nhysteresis = 3\n"
        run_loop_mock, _ = self.run_main(["--config", self.write_config(text)])
        _, _, evaluator = run_loop_mock.call_args.args
        self.assertEqual(evaluator.describe(), "Teplota CPU vypnuto; Disk / WARNING od 85 %, ERROR od 95 %")

    def test_invalid_config_stops_before_measuring(self):
        cases = {
            "neplatný interval": "[collector]\ninterval_seconds = 0\n",
            "neznámý klíč": "[collector]\nvalidity_seconds = 30\n",
            "neplatné OLED": "[oled]\npage_seconds = 0\n",
            "neznámá sekce": "[web]\nadmin_port = 8765\n",
            # Regrese: obří celé číslo shodilo main() tracebackem (OverflowError, kód 1).
            "obří číslo": "[alarms.cpu_temp]\nwarning = 1" + "0" * 400 + "\nerror = 80\nhysteresis = 1\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(SystemExit) as raised:
                    self.run_main(["--config", self.write_config(text)])
                self.assertEqual(raised.exception.code, 2)

    def test_invalid_config_does_not_start_loop(self):
        stderr = io.StringIO()
        path = self.write_config("[collector]\ninterval_seconds = 0\n")
        with mock.patch("nexusstat.run.run_loop") as run_loop_mock, contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                main(["--config", path])
        run_loop_mock.assert_not_called()
        self.assertIn("interval_seconds", stderr.getvalue())

    def test_missing_config_file(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_main(["--config", "/neexistujici/nexusstat.toml"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
