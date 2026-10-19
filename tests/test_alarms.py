"""Testy vyhodnocení alarmových prahů. Hodnoty i čas jsou simulované, nic se nečeká.

Prahy v testech (70/80 °C, 85/95 %) jsou jen ilustrační, nejde o schválené
provozní hodnoty.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from nexusstat.alarms import AlarmEvaluator, measured_level
from nexusstat.config import AlarmRule, AlarmsConfig, OledConfig
from nexusstat.core import CheckResult, Measurement, State, active_problems, effective, overall_state
from nexusstat.oled import ALERT_SYMBOL, UNKNOWN_SYMBOL, Mode, OledController, home_symbol, page_lines
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID, SystemMonitor

try:
    from nexusstat.render import SYMBOL_BOX, render_screen
except ImportError:  # Pillow není nainstalované
    render_screen = None

OBSERVED = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
WALL_TIME = datetime(2026, 10, 19, 14, 0)
INTERVAL = 10
VALIDITY = 30

TEMP_RULE = AlarmRule(enabled=True, warning=70, error=80, hysteresis=5)
DISK_RULE = AlarmRule(enabled=True, warning=85, error=95, hysteresis=3)

I, W, E, U = State.INFO, State.WARNING, State.ERROR, State.UNKNOWN


def temperature(value, now, simulated=True, summary=None):
    """Výsledek teploty CPU jako z kolektoru; None = měření selhalo (UNKNOWN bez hodnoty)."""
    if value is None:
        return CheckResult(CPU_TEMP_ID, U, "Teplota CPU: nedostupné", OBSERVED, now + VALIDITY, simulated=simulated)
    return CheckResult(
        CPU_TEMP_ID, I, summary or f"Teplota CPU {value:.0f} °C", OBSERVED, now + VALIDITY,
        simulated=simulated, measurement=Measurement(value, "°C"),
    )


def disk(value, now, simulated=True):
    return CheckResult(
        DISK_ROOT_ID, I, f"Disk / {value:.0f} % z 117 GiB", OBSERVED, now + VALIDITY,
        simulated=simulated, measurement=Measurement(value, "%"),
    )


def evaluator(cpu_temp=TEMP_RULE, disk_root=AlarmRule()):
    return AlarmEvaluator(AlarmsConfig(cpu_temp=cpu_temp, disk_root=disk_root))


def feed(alarm_evaluator, values, make=temperature, start=0, **kwargs):
    """Pošle hodnoty po 10 s a vrátí výsledné stavy."""
    states = []
    for index, value in enumerate(values):
        now = start + index * INTERVAL
        [result] = alarm_evaluator.evaluate([make(value, now, **kwargs)], now)
        states.append(result.state)
    return states


class DisabledRulesTest(unittest.TestCase):
    def test_default_config_keeps_valid_measurement_info(self):
        alarm_evaluator = AlarmEvaluator(AlarmsConfig())
        self.assertEqual(feed(alarm_evaluator, [20, 75, 95, 120, 120, 120]), [I] * 6)
        self.assertEqual(feed(alarm_evaluator, [99, 99, 99], make=disk), [I] * 3)

    def test_disabled_rule_with_thresholds_keeps_info(self):
        disabled = AlarmRule(enabled=False, warning=70, error=80, hysteresis=5)
        self.assertEqual(feed(evaluator(cpu_temp=disabled), [95] * 5), [I] * 5)
        disabled_disk = AlarmRule(enabled=False, warning=85, error=95, hysteresis=3)
        alarm_evaluator = evaluator(cpu_temp=disabled, disk_root=disabled_disk)
        values = [60 + (index * 7) % 65 for index in range(200)]  # 60–124, opakovaně nad oběma prahy
        self.assertEqual(set(feed(alarm_evaluator, values)), {I})
        self.assertEqual(set(feed(evaluator(cpu_temp=disabled, disk_root=disabled_disk), [99.9] * 10, make=disk)), {I})

    def test_results_are_unchanged_without_rule(self):
        alarm_evaluator = evaluator(disk_root=DISK_RULE)
        cpu = CheckResult(CPU_USAGE_ID, I, "CPU 99 %", OBSERVED, 30, simulated=True, measurement=Measurement(99, "%"))
        memory = CheckResult(MEMORY_ID, I, "RAM 99 %", OBSERVED, 30, simulated=True, measurement=Measurement(99, "%"))
        for _ in range(4):
            evaluated = alarm_evaluator.evaluate([cpu, memory], 0)
            self.assertIs(evaluated[0], cpu)
            self.assertIs(evaluated[1], memory)


class MeasuredLevelTest(unittest.TestCase):
    def test_threshold_boundaries_without_alarm(self):
        cases = [(69.999, I), (70, W), (79.999, W), (80, E), (125, E)]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertIs(measured_level(value, TEMP_RULE, I), expected)

    def test_hysteresis_keeps_confirmed_level(self):
        cases = [
            (W, 65, W),  # warning − hysteresis závadu ještě drží
            (W, 64.999, I),
            (E, 75, E),  # error − hysteresis drží ERROR
            (E, 74.999, W),
            (E, 65, W),
            (E, 64.999, I),
            (W, 79.999, W),  # z WARNING do ERROR až od prahu error
        ]
        for confirmed, value, expected in cases:
            with self.subTest(confirmed=confirmed, value=value):
                self.assertIs(measured_level(value, TEMP_RULE, confirmed), expected)


class EscalationTest(unittest.TestCase):
    def test_three_consecutive_measurements_raise_warning(self):
        self.assertEqual(feed(evaluator(), [72, 72, 72]), [I, I, W])

    def test_warning_before_error_on_rising_value(self):
        self.assertEqual(feed(evaluator(), [72, 72, 72, 85, 85, 85]), [I, I, W, W, W, E])
        # Hodnota nad ERROR se počítá i pro WARNING: WARNING přijde dřív.
        self.assertEqual(feed(evaluator(), [72, 85, 85, 85]), [I, I, W, E])

    def test_jump_straight_to_error_level(self):
        # Zdokumentované chování: tři měření nad ERROR potvrdí rovnou ERROR,
        # bez zvláštní rychlé cesty a bez automatické akce.
        self.assertEqual(feed(evaluator(), [85, 85, 85]), [I, I, E])

    def test_value_exactly_at_threshold_is_above(self):
        self.assertEqual(feed(evaluator(), [70] * 6), [I, I, W, W, W, W])
        self.assertEqual(feed(evaluator(), [80] * 4), [I, I, E, E])

    def test_value_just_below_threshold_never_alarms(self):
        self.assertEqual(feed(evaluator(), [69.99] * 10), [I] * 10)

    def test_counts_from_config(self):
        immediate = AlarmRule(enabled=True, warning=70, error=80, hysteresis=5, confirm_count=1, clear_count=1)
        self.assertEqual(feed(evaluator(cpu_temp=immediate), [72, 85, 60]), [W, E, I])
        slow = AlarmRule(enabled=True, warning=70, error=80, hysteresis=5, confirm_count=5, clear_count=4)
        self.assertEqual(feed(evaluator(cpu_temp=slow), [72] * 5 + [60] * 4), [I] * 4 + [W] * 4 + [I])


class SpikeTest(unittest.TestCase):
    def test_single_spikes_do_not_raise_alarm(self):
        self.assertEqual(feed(evaluator(), [60, 95, 60, 95, 60, 95, 60]), [I] * 7)

    def test_two_measurement_spikes_do_not_raise_alarm(self):
        self.assertEqual(feed(evaluator(), [60, 95, 95, 60, 95, 95, 60]), [I] * 7)

    def test_single_dip_does_not_clear_and_reraise(self):
        # Krátký pokles během závady ji neukončí, takže nevznikají opakované alarmy.
        self.assertEqual(feed(evaluator(), [72, 72, 72, 50, 72, 50, 72]), [I, I, W, W, W, W, W])

    def test_oscillation_around_threshold_stays_warning(self):
        self.assertEqual(feed(evaluator(), [72, 72, 72, 69, 71, 66, 70, 65]), [I, I] + [W] * 6)


class ClearingTest(unittest.TestCase):
    def test_warning_clears_after_two_measurements_below_hysteresis(self):
        self.assertEqual(feed(evaluator(), [72, 72, 72, 66, 65, 64.9, 64.9]), [I, I, W, W, W, W, I])

    def test_error_steps_down_to_warning_in_band(self):
        self.assertEqual(feed(evaluator(), [85, 85, 85, 75, 74.9, 74.9]), [I, I, E, E, E, W])

    def test_error_clears_straight_to_info(self):
        self.assertEqual(feed(evaluator(), [85, 85, 85, 60, 60]), [I, I, E, E, I])

    def test_documented_transitions(self):
        # Příklady přechodů z dokumentace (ARCHITECTURE.md).
        self.assertEqual(
            feed(evaluator(), [65, 72, 72, 72, 85, 85, 85, 60, 60]),
            [I, I, I, W, W, W, E, E, I],
        )
        self.assertEqual(
            feed(evaluator(), [65, 71, 72, 73, 79, 81, 82, 83, 77, 74, 72, 66, 64, 63]),
            [I, I, I, W, W, W, W, E, E, E, W, W, W, I],
        )


class IdempotenceTest(unittest.TestCase):
    """Identita vzorku = (id, simulated, expires_monotonic); stejný vzorek se počítá jen jednou."""

    def test_same_sample_repeated_counts_once(self):
        alarm_evaluator = evaluator()
        sample = temperature(72, 0)
        self.assertEqual([alarm_evaluator.evaluate([sample], 0)[0].state for _ in range(5)], [I] * 5)
        # Závadu potvrdí až třetí různý vzorek.
        self.assertEqual(feed(alarm_evaluator, [72, 72], start=10), [I, W])

    def test_reevaluation_at_later_time_counts_once(self):
        # Např. obnova displeje volá vyhodnocení se stejnými výsledky v jiném čase.
        alarm_evaluator = evaluator()
        sample = temperature(72, 0)
        self.assertEqual([alarm_evaluator.evaluate([sample], now)[0].state for now in (0, 3, 6, 9)], [I] * 4)

    def test_same_sample_twice_in_one_call(self):
        sample = temperature(72, 0)
        self.assertEqual([result.state for result in evaluator().evaluate([sample] * 3, 0)], [I] * 3)

    def test_repeated_sample_does_not_advance_clearing(self):
        alarm_evaluator = evaluator()
        self.assertEqual(feed(alarm_evaluator, [72, 72, 72]), [I, I, W])
        low = temperature(50, 30)
        self.assertEqual([alarm_evaluator.evaluate([low], 30)[0].state for _ in range(3)], [W, W, W])
        self.assertEqual(feed(alarm_evaluator, [50], start=40), [I])  # až druhé různé měření pod hranicí

    def test_old_unknown_reevaluated_does_not_break_streak(self):
        alarm_evaluator = evaluator()
        old_unknown = temperature(None, 10)
        alarm_evaluator.evaluate([temperature(72, 0)], 0)
        self.assertIs(alarm_evaluator.evaluate([old_unknown], 10)[0].state, U)
        states = feed(alarm_evaluator, [72, 72], start=20)
        self.assertIs(alarm_evaluator.evaluate([old_unknown], 35)[0].state, U)  # starý vzorek znovu
        states += feed(alarm_evaluator, [72], start=40)
        self.assertEqual(states, [I, I, W])

    def test_older_sample_is_not_counted(self):
        alarm_evaluator = evaluator()
        self.assertIs(alarm_evaluator.evaluate([temperature(72, 20)], 20)[0].state, I)
        self.assertIs(alarm_evaluator.evaluate([temperature(72, 10)], 20)[0].state, I)  # starší
        self.assertEqual(feed(alarm_evaluator, [72, 72], start=30), [I, W])


class DecimalThresholdTest(unittest.TestCase):
    """Desetinné prahy: hranice odeznění přesně podle konfigurace, ne podle binární aritmetiky.

    65.2 − 1.1 je ve float 64.10000000000001 a 78.2 − 1.1 je 77.10000000000001;
    hodnota přesně na hranici by bez zaokrouhlení závadu ukončila.
    """

    RULE = AlarmRule(enabled=True, warning=65.2, error=78.2, hysteresis=1.1)

    def test_hold_boundaries_match_decimal_config(self):
        self.assertEqual((self.RULE.warning_hold, self.RULE.error_hold), (64.1, 77.1))
        self.assertEqual(AlarmRule(True, 80.2, 95.1, 0.6).warning_hold, 79.6)

    def test_value_exactly_at_hold_boundary_keeps_level(self):
        self.assertIs(measured_level(64.1, self.RULE, W), W)
        self.assertIs(measured_level(64.1, self.RULE, E), W)
        self.assertIs(measured_level(77.1, self.RULE, E), E)
        self.assertIs(measured_level(64.099, self.RULE, W), I)
        self.assertIs(measured_level(77.099, self.RULE, E), W)

    def test_sequence_with_sensor_resolution(self):
        # Teplota ze /sys je v tisícinách °C: 64100 / 1000 je přesně hranice odeznění.
        values = [66, 66, 66, 64100 / 1000, 64100 / 1000, 64.0, 64.0]
        self.assertEqual(feed(evaluator(cpu_temp=self.RULE), values), [I, I, W, W, W, W, I])

    def test_error_to_warning_at_exact_boundary(self):
        values = [79, 79, 79, 77.1, 77.1, 77.0, 77.0]
        self.assertEqual(feed(evaluator(cpu_temp=self.RULE), values), [I, I, E, E, E, E, W])


class UnknownAndNaTest(unittest.TestCase):
    def test_unknown_is_shown_as_unknown_not_info(self):
        self.assertEqual(feed(evaluator(), [72, 72, 72, None]), [I, I, W, U])

    def test_unknown_does_not_resolve_alarm(self):
        self.assertEqual(feed(evaluator(), [72, 72, 72, None, 50, 50]), [I, I, W, U, W, I])
        self.assertEqual(feed(evaluator(), [72, 72, 72, None, None, None, 72]), [I, I, W, U, U, U, W])

    def test_unknown_interrupts_confirmation(self):
        self.assertEqual(feed(evaluator(), [72, 72, None, 72, 72, 72]), [I, I, U, I, I, W])

    def test_unknown_interrupts_escalation_to_error(self):
        self.assertEqual(feed(evaluator(), [72, 72, 72, 85, 85, None, 85, 85, 85]), [I, I, W, W, W, U, W, W, E])

    def test_confirmed_error_survives_unknown(self):
        # 76 °C je v pásmu hystereze ERROR (od 75): po obnovení měření ERROR trvá.
        self.assertEqual(feed(evaluator(), [85, 85, 85, None, None, 76, 60, 60]), [I, I, E, U, U, E, E, I])

    def test_unknown_interrupts_clearing(self):
        self.assertEqual(feed(evaluator(), [72, 72, 72, 50, None, 50, 50]), [I, I, W, W, U, W, I])

    def test_na_is_unchanged(self):
        alarm_evaluator = evaluator()
        na = CheckResult(CPU_TEMP_ID, State.NA, "Teplota CPU se nesleduje", OBSERVED, 30, simulated=False)
        for _ in range(4):
            self.assertIs(alarm_evaluator.evaluate([na], 0)[0], na)

    def test_missing_value_with_enabled_rule_is_unknown(self):
        # Text by tvrdil 99 °C, ale číselná hodnota chybí: summary se nikdy neparsuje.
        no_value = CheckResult(CPU_TEMP_ID, I, "Teplota CPU 99 °C", OBSERVED, 30, simulated=True)
        [result] = evaluator().evaluate([no_value], 0)
        self.assertIs(result.state, U)
        self.assertIsNone(result.measurement)
        self.assertEqual(result.summary, "Teplota CPU: chybí hodnota v °C")

    def test_wrong_unit_is_unknown(self):
        kelvin = CheckResult(
            CPU_TEMP_ID, I, "Teplota CPU 350 K", OBSERVED, 30, simulated=True, measurement=Measurement(350, "K")
        )
        self.assertIs(evaluator().evaluate([kelvin], 0)[0].state, U)

    def test_alarm_uses_value_not_summary(self):
        misleading_low = feed(evaluator(), [90] * 3, summary="Teplota CPU 20 °C")
        misleading_high = feed(evaluator(), [20] * 3, summary="Teplota CPU 99 °C")
        self.assertEqual(misleading_low, [I, I, E])
        self.assertEqual(misleading_high, [I, I, I])


class ExpiryTest(unittest.TestCase):
    def evaluated_warning(self):
        alarm_evaluator = evaluator()
        for now in (0, 10, 20):
            [result] = alarm_evaluator.evaluate([temperature(72, now)], now)
        return result  # z času 20, platí do 50

    def test_expired_alarm_becomes_unknown_without_value(self):
        result = self.evaluated_warning()
        self.assertIs(effective(result, 49.9).state, W)
        stale = effective(result, 50)
        self.assertIs(stale.state, U)
        self.assertIsNone(stale.measurement)
        self.assertIn("zastaralá data", stale.summary)
        # Prošlá závada není potvrzená ani vyřešená: "?" místo trojúhelníku i místo "vše v pořádku".
        self.assertEqual(home_symbol([result], 50), UNKNOWN_SYMBOL)
        self.assertIs(overall_state([result], 50), U)

    def test_gap_in_data_interrupts_confirmation(self):
        alarm_evaluator = evaluator()
        states = []
        for now in (0, 10, 100, 110, 120):  # mezi 10 a 100 data prošla (platnost do 40)
            states.append(alarm_evaluator.evaluate([temperature(72, now)], now)[0].state)
        self.assertEqual(states, [I, I, I, I, W])

    def test_measurement_at_expiry_boundary_counts_as_gap(self):
        alarm_evaluator = evaluator()
        states = [alarm_evaluator.evaluate([temperature(72, now)], now)[0].state for now in (0, 10, 40, 50)]
        self.assertEqual(states, [I, I, I, I])

    def test_delayed_but_valid_measurements_keep_counting(self):
        alarm_evaluator = evaluator()
        states = [alarm_evaluator.evaluate([temperature(72, now)], now)[0].state for now in (0, 10, 39.9)]
        self.assertEqual(states, [I, I, W])

    def test_gap_does_not_resolve_alarm(self):
        alarm_evaluator = evaluator()
        states = [alarm_evaluator.evaluate([temperature(value, now)], now)[0].state
                  for value, now in ((72, 0), (72, 10), (72, 20), (50, 200), (50, 210))]
        self.assertEqual(states, [I, I, W, W, I])


class SimulatedDataTest(unittest.TestCase):
    def test_simulated_flag_is_kept(self):
        alarm_evaluator = evaluator()
        for now in (0, 10, 20):
            [result] = alarm_evaluator.evaluate([temperature(72, now, simulated=True)], now)
        self.assertIs(result.state, W)
        self.assertTrue(result.simulated)

    def test_simulated_data_never_confirm_real_alarm(self):
        alarm_evaluator = evaluator()
        feed(alarm_evaluator, [72, 72], simulated=True)
        self.assertEqual(feed(alarm_evaluator, [72], start=20, simulated=False), [I])

    def test_simulated_data_never_clear_real_alarm(self):
        alarm_evaluator = evaluator()
        self.assertEqual(feed(alarm_evaluator, [72, 72, 72], simulated=False), [I, I, W])
        self.assertEqual(feed(alarm_evaluator, [50, 50], start=30, simulated=True), [I, I])
        self.assertEqual(feed(alarm_evaluator, [72], start=50, simulated=False), [W])

    @unittest.skipIf(render_screen is None, "Pillow není dostupné")
    def test_simulated_alarm_is_marked_on_display(self):
        images = {}
        for simulated in (True, False):
            alarm_evaluator = evaluator()
            for now in (0, 10, 20):
                results = alarm_evaluator.evaluate([temperature(72, now, simulated=simulated)], now)
            self.assertIs(results[0].state, W)
            controller = OledController(OledConfig())
            controller.press(20, 0)
            images[simulated] = render_screen(controller, 20, results, WALL_TIME)
        # Oba ukazují trojúhelník, jen simulovaný alarm nese značku SIM (pravý dolní roh stránky NEXUS).
        x, y, size = SYMBOL_BOX
        for image in images.values():
            self.assertIsNotNone(image.crop((x, y, x + size, y + size)).getbbox())
        self.assertIsNotNone(images[True].crop((100, 50, 128, 64)).getbbox())
        self.assertIsNone(images[False].crop((100, 50, 128, 64)).getbbox())


class DiskRuleTest(unittest.TestCase):
    def test_disk_rule_uses_percent(self):
        alarm_evaluator = evaluator(cpu_temp=AlarmRule(), disk_root=DISK_RULE)
        self.assertEqual(feed(alarm_evaluator, [86, 86, 86, 96, 96, 96, 81.9, 81.9], make=disk),
                         [I, I, W, W, W, E, E, I])

    def test_rules_are_independent(self):
        alarm_evaluator = evaluator(disk_root=DISK_RULE)
        for now in (0, 10, 20):
            results = alarm_evaluator.evaluate([temperature(72, now), disk(50, now)], now)
        self.assertEqual([result.state for result in results], [W, I])


class OledCompatibilityTest(unittest.TestCase):
    def evaluated(self):
        alarm_evaluator = evaluator(disk_root=DISK_RULE)
        for now in (0, 10, 20):
            results = alarm_evaluator.evaluate([temperature(72, now), disk(96, now)], now)
        return results

    def test_pages_show_values_and_status_orders_problems(self):
        results = self.evaluated()
        # Text hodnot na stránkách zůstává stejný jako bez prahů.
        self.assertEqual(page_lines("SYSTEM", results, 25), ["CPU N/A", "Teplota CPU 72 °C", "RAM N/A"])
        self.assertEqual(page_lines("DISK", results, 25), ["Disk / 96 % z 117 GiB"])
        self.assertEqual(home_symbol(results, 25), ALERT_SYMBOL)
        problems = active_problems(results, 25)
        self.assertEqual([(problem.id, problem.state) for problem in problems], [(DISK_ROOT_ID, E), (CPU_TEMP_ID, W)])

    def test_status_paging_with_evaluated_problems(self):
        results = self.evaluated()
        controller = OledController(OledConfig())
        controller.press(21, 0)
        controller.press(22, len(active_problems(results, 22)))
        self.assertIs(controller.mode, Mode.STATUS)
        controller.press(23, len(active_problems(results, 23)))  # jediná stránka → zpět
        self.assertIs(controller.mode, Mode.CAROUSEL)

    @unittest.skipIf(render_screen is None, "Pillow není dostupné")
    def test_status_screen_renders_evaluated_problems(self):
        results = self.evaluated()
        controller = OledController(OledConfig())
        controller.press(21, 0)
        controller.press(22, 2)
        with_problems = render_screen(controller, 22, results, WALL_TIME)
        without = render_screen(controller, 22, [temperature(50, 20), disk(50, 20)], WALL_TIME)
        self.assertNotEqual(with_problems.tobytes(), without.tobytes())


def unavailable_statvfs(path):
    raise OSError("disk se v tomto testu neměří")


class CollectorIntegrationTest(unittest.TestCase):
    """Skutečný SystemMonitor nad vzorovými soubory: číselná cesta bez parsování textu."""

    def test_temperature_alarm_from_collector(self):
        with tempfile.TemporaryDirectory() as directory:
            thermal = Path(directory) / "thermal" / "thermal_zone0"
            thermal.mkdir(parents=True)
            (thermal / "type").write_text("cpu-thermal\n", encoding="ascii")
            monitor = SystemMonitor(
                VALIDITY, proc_dir=Path(directory) / "proc", thermal_dir=thermal.parent, statvfs=unavailable_statvfs
            )
            alarm_evaluator = evaluator()
            states = []
            for now, millidegrees in ((0, 72400), (10, 73100), (20, 71900), (30, 64000), (40, 63000)):
                (thermal / "temp").write_text(f"{millidegrees}\n", encoding="ascii")
                results = {r.id: r for r in alarm_evaluator.evaluate(monitor.collect(now, OBSERVED), now)}
                states.append(results[CPU_TEMP_ID].state)
            self.assertEqual(states, [I, I, W, W, I])
            temperature_result = results[CPU_TEMP_ID]
            self.assertEqual(temperature_result.measurement, Measurement(63.0, "°C"))
            self.assertEqual(temperature_result.summary, "Teplota CPU 63 °C")
            # Chybějící /proc: CPU a RAM jsou UNKNOWN bez hodnoty, evaluator je nemění.
            self.assertIs(results[MEMORY_ID].state, U)
            self.assertIsNone(results[MEMORY_ID].measurement)


class DescribeTest(unittest.TestCase):
    def test_describe(self):
        self.assertEqual(AlarmEvaluator(AlarmsConfig()).describe(), "Teplota CPU vypnuto; Disk / vypnuto")
        rule = AlarmRule(enabled=True, warning=72.5, error=85, hysteresis=2.5)
        self.assertEqual(
            evaluator(cpu_temp=rule).describe(), "Teplota CPU WARNING od 72.5 °C, ERROR od 85 °C; Disk / vypnuto"
        )


if __name__ == "__main__":
    unittest.main()
