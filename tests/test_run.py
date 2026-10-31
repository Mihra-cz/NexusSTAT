"""Testy pravidelné smyčky měření. Hodiny, čekání i kolektor jsou simulované, nic se skutečně nečeká."""

import contextlib
import io
import os
import signal
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from nexusstat import __version__, history
from nexusstat.alarms import AlarmEvaluator
from nexusstat.config import AlarmRule, AlarmsConfig, CollectorConfig, OledConfig
from nexusstat.core import CheckResult, Measurement, State, active_problems, effective, overall_state
from nexusstat.oled import UNKNOWN_SYMBOL, Mode, OledController, home_symbol
from nexusstat.oled_text import page_rows
from nexusstat.config import Config, HistoryConfig, NetworkConfig, SnapshotConfig
from nexusstat.fan_monitor import LABELS as FAN_LABELS, FanMonitor
from nexusstat.network_monitor import LABELS as NETWORK_LABELS, LINK_ID, NetworkMonitor
from nexusstat.run import (COLLECTOR_FAILURE, Collectors, Stopper, failed_results, failure_name,
                          format_line, live_monitor, main, run_loop, start_history)
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, LABELS, MEMORY_ID, SystemMonitor

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
# Číselné hodnoty k VALUES, jako je dodává skutečný kolektor.
MEASUREMENTS = {
    CPU_USAGE_ID: Measurement(5, "%"),
    CPU_TEMP_ID: Measurement(48, "°C"),
    MEMORY_ID: Measurement(24, "%"),
    DISK_ROOT_ID: Measurement(31, "%"),
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
    labels = LABELS

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
            CheckResult(check_id, State.INFO, summary, observed_utc, now + CONFIG.validity_seconds, simulated=True,
                        measurement=MEASUREMENTS[check_id])
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


def printed_states(lines):
    """(sekunda od startu, celkový stav) vypsaných řádků měření."""
    printed = []
    for line in lines:
        if " UTC  SIM " in line:
            stamp, rest = line.split(" UTC  SIM ")
            second = (datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) - START_UTC)
            printed.append((int(second.total_seconds()), rest.split()[0]))
    return printed


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
        self.assertTrue(lines[1].startswith("2026-10-19 12:00:00 UTC"))


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
        self.assertEqual(page_rows("SYSTEM", first, START + 40),
                         [("CPU", "UNKN"), ("CPU TEMP", "UNKN"), ("RAM", "UNKN"), ("CPU FAN", "UNKN")])


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
        self.assertEqual(page_rows("DISK", results, now), [("ROOT", "UNKN")])
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
        real_collector = mock.Mock(simulated=False, labels=LABELS)
        results = failed_results(real_collector, START, START_UTC, 30)
        self.assertFalse(any(result.simulated for result in results))
        self.assertNotIn("SIM", format_line(results, START, START_UTC, "měření selhalo"))


class FakeNetwork:
    """Náhrada NetworkMonitor: čtyři platné výsledky, nebo výjimka místo nich."""

    simulated = True
    labels = NETWORK_LABELS
    validity_seconds = CONFIG.validity_seconds

    def __init__(self, error=None):
        self.error = error

    def collect(self, now, observed_utc):
        if self.error is not None:
            raise self.error
        return [CheckResult(check_id, State.INFO, f"{label} ok", observed_utc, now + self.validity_seconds,
                            simulated=True) for check_id, label in NETWORK_LABELS.items()]


class CollectorsTest(unittest.TestCase):
    """Systém a síť se měří společně, ale selhání jednoho kolektoru nezasáhne druhý."""

    def collectors(self, network_error=None):
        clock = FakeClock(stop_at=START + 1000)
        system = FakeCollector(clock)
        system.validity_seconds = CONFIG.validity_seconds
        return Collectors(system, FakeNetwork(network_error)), system

    def test_results_of_all_collectors_in_order(self):
        collectors, _ = self.collectors()
        results = collectors.collect(START, START_UTC)
        self.assertEqual([r.id for r in results], list(LABELS) + list(NETWORK_LABELS))
        self.assertEqual(collectors.labels, {**LABELS, **NETWORK_LABELS})
        self.assertEqual(collectors.validity_seconds, 30)

    def test_failure_of_network_keeps_system_values(self):
        collectors, _ = self.collectors(network_error=OSError("simulovaný výpadek"))
        by_id = {r.id: r for r in collectors.collect(START, START_UTC)}
        self.assertEqual({by_id[i].state for i in LABELS}, {State.INFO})  # systém změřen normálně
        self.assertEqual({by_id[i].state for i in NETWORK_LABELS}, {State.UNKNOWN})
        self.assertEqual(by_id["network.link"].summary, "Síť: měření selhalo (OSError)")
        self.assertEqual({by_id[i].expires_monotonic for i in NETWORK_LABELS}, {START + 30})

    def test_failure_of_system_keeps_network_values(self):
        collectors, system = self.collectors()
        system.errors = {1: RuntimeError("simulovaný výpadek")}
        by_id = {r.id: r for r in collectors.collect(START, START_UTC)}
        self.assertEqual({by_id[i].state for i in LABELS}, {State.UNKNOWN})
        self.assertEqual({by_id[i].state for i in NETWORK_LABELS}, {State.INFO})

    def test_loop_with_failing_network_still_reports_system(self):
        clock = FakeClock(stop_at=START + 5)
        system = FakeCollector(clock)
        system.validity_seconds = CONFIG.validity_seconds
        results, lines = run(clock, Collectors(system, FakeNetwork(ValueError("vadná data"))))
        self.assertIn("CPU 5 %", lines[1])
        self.assertIn("Síť RX: měření selhalo (ValueError)", lines[1])
        self.assertIs(overall_state(results, START), State.UNKNOWN)  # síť chybí: ne INFO/OK

    def test_ctrl_c_is_not_swallowed(self):
        collectors, _ = self.collectors(network_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            collectors.collect(START, START_UTC)

    def test_invalid_combination(self):
        with self.assertRaises(ValueError):
            Collectors()
        other = FakeNetwork()
        other.validity_seconds = 15
        with self.assertRaises(ValueError):
            Collectors(FakeNetwork(), other)

    def test_live_monitor_measures_each_collector_on_its_own_interval(self):
        # SCHED-2: schválené intervaly; platnost výsledku 3 × interval kolektoru.
        monitor = live_monitor(Config(collector=CollectorConfig(interval_seconds=5), network=NetworkConfig("end0")))
        wiring = [(type(scheduled.monitor), tuple(scheduled.monitor.labels), scheduled.interval_seconds,
                   scheduled.monitor.validity_seconds) for scheduled in monitor.monitors]
        self.assertEqual(wiring, [
            (SystemMonitor, (CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID), 5, 15),
            (SystemMonitor, (DISK_ROOT_ID,), 1800, 5400),
            (FanMonitor, tuple(FAN_LABELS), 10, 30),
            (NetworkMonitor, tuple(NETWORK_LABELS), 1, 3),
        ])
        network = monitor.monitors[3].monitor
        self.assertEqual((network.interface, network.details_interval, network.details_validity), ("end0", 60, 180))
        self.assertEqual(network.gap_seconds, 15)  # rychlost i po zdržení smyčky do obvyklé platnosti
        self.assertEqual({scheduled.validity_seconds for scheduled in monitor.monitors}, {15})
        self.assertEqual(monitor.validity_seconds, 15)
        self.assertFalse(monitor.simulated)  # skutečné zdroje, nic se při vytvoření nečte
        self.assertIsNone(live_monitor(Config()).monitors[3].monitor.interface)  # výchozí: automatický výběr


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
        self.assertIn("takt 10 s, historie a její kontroly každých 10 s, platnost jejich výsledků 30 s", lines[0])
        self.assertIn("Prahy: Teplota CPU vypnuto; Disk / vypnuto", lines[0])
        self.assertIn("Výpis při změně stavu, jinak nejvýš jednou za 30 min", lines[0])
        self.assertIn("Ctrl+C", lines[0])
        self.assertEqual(
            lines[1],
            "2026-10-19 12:00:00 UTC  SIM INFO     CPU 5 % | Teplota CPU 48 °C | RAM 24 % z 7,9 GiB | Disk / 31 % z 117 GiB",
        )
        # Druhé měření nic významného nezměnilo (jen čas): řádek nevznikne (SCHED-2).
        self.assertEqual(lines[2:], ["\nUkončeno (Ctrl+C), počet měření: 2."])

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
        # Řádek jen při změně stavu (SCHED-2): první měření a potvrzený WARNING.
        self.assertEqual(printed_states(lines), [(0, "INFO"), (30, "WARNING")])
        self.assertIn("Teplota CPU 72 °C [WARNING]", lines[2])
        self.assertIn("CPU 5 % |", lines[2])  # ostatní hodnoty beze značky
        self.assertTrue(lines[2].startswith("2026-10-19 12:00:30 UTC  SIM WARNING"))  # simulace je vidět
        temperature = results[1]
        self.assertEqual((temperature.state, temperature.measurement.value), (State.WARNING, 85))

    def test_collector_failure_interrupts_confirmation(self):
        clock = FakeClock(stop_at=START + 55)
        collector = FakeCollector(clock)
        collector.temperatures = [72, 72, None, 72, 72, 72]
        collector.errors = {3: RuntimeError("simulovaný výpadek")}
        _, lines = run(clock, collector, alarms=TEMP_RULE)
        # Výpadek přeruší řadu: WARNING až po třech platných měřeních po něm.
        self.assertEqual(printed_states(lines), [(0, "INFO"), (20, "UNKNOWN"), (30, "INFO"), (50, "WARNING")])

    def test_collector_failure_does_not_resolve_alarm(self):
        clock = FakeClock(stop_at=START + 65)
        collector = FakeCollector(clock)
        collector.temperatures = [72, 72, 72, None, 50, 50, 50]
        collector.errors = {4: RuntimeError("simulovaný výpadek")}
        _, lines = run(clock, collector, alarms=TEMP_RULE)
        self.assertEqual(printed_states(lines),
                         [(0, "INFO"), (20, "WARNING"), (30, "UNKNOWN"), (40, "WARNING"), (50, "INFO")])


class TickCollector:
    """Kolektor pro takt 1 s: CPU a RAM při každém volání, okno CPU pro historii, volitelně změna linky."""

    simulated = True
    labels = {CPU_USAGE_ID: "CPU", MEMORY_ID: "RAM", LINK_ID: "Síť"}
    validity_seconds = 30

    def __init__(self, link_down=(), stalls=None):
        self.calls, self.windows = [], []
        self.link_down = link_down  # (od, do) sekund od START: linka DOWN
        self.stalls = stalls or {}  # sekunda od START -> zdržení měření (pomalá služba)
        self.clock = None

    def collect(self, now, observed_utc):
        self.calls.append(now)
        if self.clock is not None:
            self.clock.now += self.stalls.get(now - START, 0)
        down = bool(self.link_down) and self.link_down[0] <= now - START < self.link_down[1]
        return [
            CheckResult(CPU_USAGE_ID, State.INFO, "CPU 5 %", observed_utc, now + 15, simulated=True,
                        measurement=Measurement(5, "%")),
            CheckResult(MEMORY_ID, State.INFO, "RAM 24 %", observed_utc, now + 15, simulated=True,
                        measurement=Measurement(24, "%")),
            CheckResult(LINK_ID, State.INFO, "Síť eth0", observed_utc, now + 180, simulated=True,
                        details={"link": "DOWN" if down else "UP"}),
        ]

    def window_results(self, now, observed_utc, validity_seconds):
        self.windows.append(now)
        return {CPU_USAGE_ID}, [CheckResult(CPU_USAGE_ID, State.INFO, "CPU okno", observed_utc, now + validity_seconds,
                                            simulated=True, measurement=Measurement(7, "%"))]


class LiveLoopTest(unittest.TestCase):
    """SCHED-2: takt 1 s, historie a její kontroly po intervalu, výpis jen při změně."""

    def run_live(self, collector, seconds, link=None):
        clock = FakeClock(stop_at=START + seconds)
        collector.clock = clock
        lines = []
        results = run_loop(collector, CONFIG, AlarmEvaluator(AlarmsConfig()), clock=clock, utc_clock=clock.utc,
                           sleep=clock.sleep, output=lines.append, history_link=link, tick_seconds=1)
        return results, lines

    def test_every_tick_measures_but_history_only_every_interval(self):
        collector, link = TickCollector(), FakeLink()
        results, _ = self.run_live(collector, 35, link)
        self.assertEqual(offsets(collector.calls), list(range(35)))  # takty 0–34 s
        for times in ([measured for _, measured in link.recorded], link.plans, link.checks, link.snapshot_checks,
                      collector.windows):
            self.assertEqual(offsets(times), [0, 10, 20, 30])
        # Okno CPU nahradí živé měření a jde první (časová základna historie); RAM beze změny.
        for recorded, measured in link.recorded:
            self.assertEqual([(r.id, r.summary) for r in recorded],
                             [(CPU_USAGE_ID, "CPU okno"), (MEMORY_ID, "RAM 24 %"), (LINK_ID, "Síť eth0")])
            self.assertEqual(recorded[0].expires_monotonic, measured + CONFIG.validity_seconds)
        # Kontroly historie mezi zápisy platí dál (3 × interval), nevznikají každý takt.
        storage = {r.id: r for r in results}["history.storage"]
        self.assertEqual(storage.expires_monotonic, START + 30 + 30)

    def test_output_only_on_change_or_every_30_minutes(self):
        _, lines = self.run_live(TickCollector(link_down=(100, 110)), 2 * 3600)
        printed = [line[11:19] for line in lines if " UTC  SIM " in line]
        self.assertEqual(printed, ["12:00:00", "12:01:40", "12:01:50", "12:31:50", "13:01:50", "13:31:50"])
        self.assertEqual(len(lines), 1 + len(printed) + 1)  # úvod, řádky, ukončení: nic za každý takt

    def test_delay_message_only_after_a_whole_history_interval(self):
        collector = TickCollector(stalls={5: 3, 20: 12})
        _, lines = self.run_live(collector, 60)
        delays = [line for line in lines if line.startswith("Zpoždění")]
        # Zdržení 3 s takt jen posune; 12 s v taktu 20 je 11 s po taktu 21 (nad 10 s), ohlásí se.
        self.assertEqual(delays, ["Zpoždění 11.0 s: zmeškaná měření se nedohánějí, interval začíná znovu."])
        calls = offsets(collector.calls)
        self.assertEqual(calls[5:8], [5, 8, 9])  # zmeškané takty se nedohánějí


class OledCompatibilityTest(unittest.TestCase):
    def test_loop_results_drive_oled_until_they_expire(self):
        clock = FakeClock(stop_at=START + 25)
        results, _ = run(clock, FakeCollector(clock))  # poslední měření v čase 20, platí do 50
        controller = OledController(OledConfig())
        controller.press(START + 21, problem_count=len(active_problems(results, START + 21)))
        controller.update(START + 31)
        self.assertEqual(controller.current_page(START + 31), "SYSTEM")
        self.assertEqual(page_rows("SYSTEM", results, START + 31),
                         [("CPU", "5%"), ("CPU TEMP", "48°C"), ("RAM", "24%"), ("CPU FAN", "UNKN")])
        self.assertIsNone(home_symbol(results, START + 31))

        # Další měření nepřišlo: po třech intervalech hodnoty zmizí a STATUS ukáže UNKNOWN.
        expired = START + 50
        self.assertEqual(page_rows("DISK", results, expired), [("ROOT", "UNKN")])
        self.assertEqual(home_symbol(results, expired), UNKNOWN_SYMBOL)
        problems = active_problems(results, expired)
        self.assertEqual({problem.state for problem in problems}, {State.UNKNOWN})
        controller.press(expired, problem_count=len(problems))
        self.assertIs(controller.mode, Mode.STATUS)


class FakeLink:
    """Náhradní napojení historie: zaznamená předaná měření a vrátí svou kontrolu."""

    def __init__(self, state=State.INFO, record_error=None, snapshot_state=State.NA):
        self.recorded = []
        self.checks = []
        self.snapshot_checks = []
        self.plans = []
        self.state = state
        self.snapshot_state = snapshot_state
        self.record_error = record_error
        self.stops = 0
        self.path = "/tmp/historie.sqlite3"
        self.sync_source = "test"
        self.sync_note = ""  # doplňující informace o markeru; nerozhoduje o stavu hodin
        self.snapshot_error = None

    def start(self):
        self.started = True

    def record(self, results, measured_monotonic):
        if self.record_error is not None:
            raise self.record_error
        self.recorded.append((list(results), measured_monotonic))
        return True

    def check(self, now, observed_utc):
        self.checks.append(now)
        return CheckResult("history.storage", self.state, "HISTORY: test", observed_utc, now + 30,
                           simulated=False)

    def plan_snapshot(self, now, observed_utc):
        self.plans.append(now)
        return False

    def snapshot_check(self, now, observed_utc):
        self.snapshot_checks.append(now)
        return CheckResult("history.snapshot", self.snapshot_state, "HIST SNAPSHOT: test", observed_utc,
                           now + 30, simulated=False)

    def stop(self):
        self.stops += 1


class LoopHistoryTest(unittest.TestCase):
    """Smyčka s volitelným napojením historie; bez něj se chová jako dosud."""

    def test_history_gets_final_results_and_measurement_time(self):
        clock = FakeClock(stop_at=START + 25)
        collector = FakeCollector(clock)
        link = FakeLink()
        rule = AlarmRule(enabled=True, warning=40, error=60, hysteresis=2, confirm_count=1, clear_count=1)
        lines = []
        run_loop(collector, CONFIG, AlarmEvaluator(AlarmsConfig(cpu_temp=rule)), clock=clock,
                 utc_clock=clock.utc, sleep=clock.sleep, output=lines.append, history_link=link)
        self.assertEqual(len(link.recorded), 3)
        for (results, monotonic), measured in zip(link.recorded, collector.calls):
            self.assertEqual(monotonic, measured)  # skutečný monotónní čas měření
            states = {result.id: result.state for result in results}
            self.assertIs(states[CPU_TEMP_ID], State.WARNING)  # vyhodnocený stav, ne surové INFO
            self.assertNotIn("history.storage", states)  # vlastní kontroly se nepředávají
            self.assertNotIn("history.snapshot", states)

    def test_history_check_is_in_output_and_results(self):
        clock = FakeClock(stop_at=START + 15)
        link = FakeLink(state=State.ERROR)
        collector = FakeCollector(clock)
        lines = []
        results = run_loop(collector, CONFIG, AlarmEvaluator(AlarmsConfig()), clock=clock, utc_clock=clock.utc,
                           sleep=clock.sleep, output=lines.append, history_link=link)
        collector_calls = collector.calls
        self.assertEqual([result.id for result in results[-2:]], ["history.storage", "history.snapshot"])
        self.assertIn("HISTORY: test", lines[1])
        self.assertIn("HIST SNAPSHOT: test", lines[1])
        self.assertIn("SIM ERROR", lines[1])  # celkový stav řádku vidí chybu historie
        self.assertEqual(link.checks, collector_calls)  # kontrola po každém měření
        self.assertEqual(link.snapshot_checks, collector_calls)
        self.assertEqual(link.plans, collector_calls)  # plán se vyhodnotí při každém měření

    def test_loop_without_history_is_unchanged(self):
        clock = FakeClock(stop_at=START + 25)
        results, lines = run(clock, FakeCollector(clock))
        self.assertEqual(results[-1].id, DISK_ROOT_ID)
        self.assertFalse(any("HISTORY" in line for line in lines))
        self.assertEqual(lines[-1], "\nUkončeno (Ctrl+C), počet měření: 3.")

    def test_broken_history_does_not_stop_measuring(self):
        clock = FakeClock(stop_at=START + 25)
        collector = FakeCollector(clock)
        link = FakeLink(record_error=RuntimeError("historie spadla"))
        lines = []
        with self.assertRaises(RuntimeError):
            run_loop(collector, CONFIG, AlarmEvaluator(AlarmsConfig()), clock=clock, utc_clock=clock.utc,
                     sleep=clock.sleep, output=lines.append, history_link=link)
        # Výjimku z record() smyčka nemaskuje; chybu zachytává HistoryLink.record
        # (viz tests/test_history_link.py), takže se do smyčky nikdy nedostane.
        self.assertEqual(len(collector.calls), 1)


class CollectorFailureTest(unittest.TestCase):
    """Strukturovaný údaj o selhání sběru (docs/HISTORY.md 4.4)."""

    class Broken:
        simulated = False
        validity_seconds = 30
        labels = LABELS

        def __init__(self, error):
            self.error = error

        def collect(self, now, observed_utc):
            raise self.error

    class Working:
        simulated = False
        validity_seconds = 30
        labels = NETWORK_LABELS

        def collect(self, now, observed_utc):
            return [CheckResult(check_id, State.INFO, f"{label} ok", observed_utc, now + 30, simulated=False)
                    for check_id, label in NETWORK_LABELS.items()]

    def test_failure_name_is_short_ascii(self):
        self.assertEqual(failure_name(OSError("x")), "OSError")

        class Chyba(Exception):
            pass

        self.assertEqual(failure_name(Chyba()), "Chyba")
        weird = type("Tep lota°", (Exception,), {})
        self.assertEqual(failure_name(weird()), "Teplota")  # jen tisknutelné ASCII bez mezer

    def test_collector_failure_is_marked_in_details(self):
        monitor = Collectors(self.Broken(RuntimeError("čtení /proc selhalo")), self.Working())
        results = monitor.collect(START, START_UTC)
        failed = {result.id: result for result in results if result.id in LABELS}
        working = [result for result in results if result.id in NETWORK_LABELS]
        self.assertEqual(len(failed), len(LABELS))
        for result in working:  # druhý kolektor měří dál a žádný údaj o selhání nemá
            self.assertIs(result.state, State.INFO)
            self.assertEqual(dict(result.details), {})
        for result in failed.values():
            self.assertIs(result.state, State.UNKNOWN)  # nic nevypadá jako OK
            self.assertEqual(dict(result.details), {COLLECTOR_FAILURE: "RuntimeError"})
            self.assertIsNone(result.measurement)
            self.assertIn("měření selhalo (RuntimeError)", result.summary)

    def test_valid_unknown_measurement_has_no_failure_mark(self):
        # Platné měření bez hodnoty (UNKNOWN) se od selhání sběru liší.
        observed, expires = START_UTC, START + 30
        valid = CheckResult(CPU_TEMP_ID, State.UNKNOWN, "Teplota CPU: nedostupná", observed, expires,
                            simulated=False)
        self.assertEqual(dict(valid.details), {})
        failed = failed_results(self.Working(), START, observed, 30, failure="OSError")
        self.assertEqual(dict(failed[0].details), {COLLECTOR_FAILURE: "OSError"})

    def test_output_line_is_unchanged_by_the_failure_mark(self):
        clock = FakeClock(stop_at=START + 15)
        collector = FakeCollector(clock)
        collector.errors[1] = OSError("čtení selhalo")
        _, lines = run(clock, collector)
        self.assertIn("měření selhalo: OSError", lines[1])
        self.assertNotIn(COLLECTOR_FAILURE, lines[1])  # údaj je jen v datech, ne ve výpisu

    def test_thresholds_are_not_disturbed_by_the_failure_mark(self):
        # UNKNOWN ze selhání přeruší řadu potvrzování stejně jako dosud: po
        # selhání se potvrzuje znovu od začátku (confirm_count = 2).
        rule = AlarmRule(enabled=True, warning=40, error=60, hysteresis=2, confirm_count=2, clear_count=1)
        clock = FakeClock(stop_at=START + 45)
        collector = FakeCollector(clock)
        collector.temperatures = [70.0] * 6
        collector.errors[2] = OSError("čtení selhalo")
        results, lines = run(clock, collector, alarms=AlarmsConfig(cpu_temp=rule))
        self.assertEqual(printed_states(lines), [(0, "INFO"), (10, "UNKNOWN"), (20, "INFO"), (30, "ERROR")])
        self.assertIs({result.id: result.state for result in results}[CPU_TEMP_ID], State.ERROR)


class StopperTest(unittest.TestCase):
    """SIGINT i SIGTERM vedou na řízené ukončení, obsluha jen v hlavním vlákně."""

    def stopper(self):
        stopper = Stopper()
        self.assertTrue(stopper.install())
        self.addCleanup(stopper.restore)
        return stopper

    def deliver(self, number):
        os.kill(os.getpid(), number)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            time.sleep(0.01)  # čekání na doručení signálu hlavnímu vláknu

    def test_default_label_is_ctrl_c(self):
        self.assertEqual(Stopper().label, "Ctrl+C")

    def test_sigint_raises_keyboard_interrupt(self):
        stopper = self.stopper()
        with self.assertRaises(KeyboardInterrupt):
            self.deliver(signal.SIGINT)
        self.assertEqual(stopper.label, "Ctrl+C")

    def test_sigterm_raises_keyboard_interrupt(self):
        stopper = self.stopper()
        with self.assertRaises(KeyboardInterrupt):
            self.deliver(signal.SIGTERM)
        self.assertEqual(stopper.signal, signal.SIGTERM)
        self.assertEqual(stopper.label, "SIGTERM")

    def test_second_signal_during_stopping_does_not_interrupt(self):
        stopper = self.stopper()
        stopper.stopping = True
        self.deliver(signal.SIGTERM)  # nevyvolá KeyboardInterrupt: dopisování front se nepřeruší
        self.assertEqual(stopper.signal, signal.SIGTERM)

    def test_restore_puts_back_previous_handlers(self):
        before = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        stopper = Stopper()
        stopper.install()
        self.assertIsNot(signal.getsignal(signal.SIGTERM), before[signal.SIGTERM])
        stopper.restore()
        self.assertEqual({number: signal.getsignal(number) for number in before}, before)

    def test_not_installed_outside_main_thread(self):
        before = signal.getsignal(signal.SIGTERM)
        result = []
        thread = threading.Thread(target=lambda: result.append(Stopper().install()))
        thread.start()
        thread.join()
        self.assertEqual(result, [False])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_loop_names_the_signal_that_stopped_it(self):
        clock = FakeClock(stop_at=START + 15)
        stopper = Stopper()
        stopper.signal = signal.SIGTERM
        lines = []
        run_loop(FakeCollector(clock), CONFIG, AlarmEvaluator(AlarmsConfig()), clock=clock, utc_clock=clock.utc,
                 sleep=clock.sleep, output=lines.append, stopper=stopper)
        self.assertEqual(lines[-1], "\nUkončeno (SIGTERM), počet měření: 2.")


class StartHistoryTest(unittest.TestCase):
    """Příprava historie v main(): vypnutá nic nespustí, chyba nezastaví měření."""

    def test_disabled_history_returns_nothing(self):
        lines = []
        self.assertIsNone(start_history(Config(), output=lines.append))
        self.assertEqual(lines, [])

    def test_enabled_history_is_started_and_announced(self):
        lines = []
        link = FakeLink()
        with mock.patch("nexusstat.run.open_history", return_value=link) as factory:
            self.assertIs(start_history(Config(), output=lines.append), link)
        self.assertTrue(link.started)
        self.assertEqual(factory.call_args.kwargs["app_version"], __version__)
        self.assertIn("historie.sqlite3", lines[0])
        self.assertIn("development", lines[0])
        self.assertIn("nikdy nevytváří", lines[0])

    def test_snapshot_note_says_snapshots_are_off(self):
        lines = []
        link = FakeLink()
        with mock.patch("nexusstat.run.open_history", return_value=link):
            start_history(Config(), output=lines.append)
        self.assertIn("snímky vypnuté", lines[1])

    def test_snapshot_note_describes_the_daily_plan(self):
        lines = []
        link = FakeLink()
        config = Config(history=HistoryConfig(
            enabled=True, path="/tmp/h.sqlite3",
            snapshot=SnapshotConfig(enabled=True, work_dir="/tmp/w", published_dir="/tmp/p",
                                    daily_time="02:00", timezone="Europe/Prague")))
        with mock.patch("nexusstat.run.open_history", return_value=link):
            start_history(config, output=lines.append)
        self.assertIn("každý den v 02:00 (Europe/Prague)", lines[1])
        self.assertIn("/tmp/p", lines[1])
        self.assertIn("nemažou", lines[1])

    def test_snapshot_note_admits_unusable_directories(self):
        lines = []
        link = FakeLink()
        link.snapshot_error = "adresář /tmp/p neexistuje"
        config = Config(history=HistoryConfig(
            enabled=True, path="/tmp/h.sqlite3",
            snapshot=SnapshotConfig(enabled=True, work_dir="/tmp/w", published_dir="/tmp/p",
                                    daily_time="02:00", timezone="UTC")))
        with mock.patch("nexusstat.run.open_history", return_value=link):
            start_history(config, output=lines.append)
        self.assertIn("nepoužitelné", lines[1])
        self.assertIn("neexistuje", lines[1])
        self.assertIn("snímek nevznikne", lines[1])

    def test_unavailable_history_keeps_measuring(self):
        lines = []
        error = history.HistoryError("host", "nelze přečíst /etc/machine-id")
        with mock.patch("nexusstat.run.open_history", side_effect=error):
            self.assertIsNone(start_history(Config(), output=lines.append))
        self.assertIn("nedostupná", lines[0])
        self.assertIn("pokračuje bez historie", lines[0])


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
        # Smyčka měří systém i síť, rozhraní se vybírá automaticky.
        self.assertEqual(list(monitor.labels), list(LABELS) + list(FAN_LABELS) + list(NETWORK_LABELS))
        self.assertIsNone(monitor.monitors[3].monitor.interface)
        self.assertEqual(run_loop_mock.call_args.kwargs["tick_seconds"], 1)  # živé měření po 1 s

    def test_network_interface_from_config(self):
        run_loop_mock, _ = self.run_main(["--config", self.write_config('[network]\ninterface = "eth0"\n')])
        monitor, _, _ = run_loop_mock.call_args.args
        self.assertEqual(monitor.monitors[3].monitor.interface, "eth0")

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
            "neplatné rozhraní": '[network]\ninterface = "../eth0"\n',
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

    def test_history_is_off_without_section(self):
        run_loop_mock, _ = self.run_main([])
        self.assertIsNone(run_loop_mock.call_args.kwargs["history_link"])
        self.assertIsInstance(run_loop_mock.call_args.kwargs["stopper"], Stopper)

    def test_history_link_is_passed_to_the_loop_and_stopped(self):
        link = FakeLink()
        with mock.patch("nexusstat.run.start_history", return_value=link):
            run_loop_mock, _ = self.run_main([])
        self.assertIs(run_loop_mock.call_args.kwargs["history_link"], link)
        self.assertEqual(link.stops, 1)

    def test_history_is_stopped_even_after_failure_in_the_loop(self):
        link = FakeLink()
        stderr = io.StringIO()
        with mock.patch("nexusstat.run.start_history", return_value=link), \
                mock.patch("nexusstat.run.run_loop", side_effect=RuntimeError("pád smyčky")), \
                contextlib.redirect_stderr(stderr):
            with self.assertRaises(RuntimeError):
                main([])
        self.assertEqual(link.stops, 1)

    def test_signal_handlers_are_restored_after_the_loop(self):
        before = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        self.run_main([])
        self.assertEqual({number: signal.getsignal(number) for number in before}, before)


if __name__ == "__main__":
    unittest.main()
