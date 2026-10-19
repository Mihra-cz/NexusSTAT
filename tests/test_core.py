import unittest
from datetime import datetime, timedelta, timezone

from nexusstat.core import (
    CheckResult,
    State,
    active_problems,
    effective,
    has_monitor_data,
    overall_state,
)

OBSERVED = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)


def result(check_id, state, expires=1000, simulated=False):
    return CheckResult(check_id, state, f"popis {check_id}", OBSERVED, expires, simulated=simulated)


class ActiveProblemsTest(unittest.TestCase):
    def test_order_by_severity_then_id(self):
        results = [
            result("b.unknown", State.UNKNOWN),
            result("ok", State.OK),
            result("b.warning", State.WARNING),
            result("info", State.INFO),
            result("critical", State.CRITICAL),
            result("a.warning", State.WARNING),
            result("na", State.NA),
            result("error", State.ERROR),
            result("a.unknown", State.UNKNOWN),
        ]
        ids = [problem.id for problem in active_problems(results, now=10)]
        self.assertEqual(
            ids, ["critical", "error", "a.warning", "b.warning", "a.unknown", "b.unknown"]
        )

    def test_ok_info_and_na_are_not_problems(self):
        results = [result("ok", State.OK), result("info", State.INFO), result("na", State.NA)]
        self.assertEqual(active_problems(results, now=10), [])

    def test_order_does_not_depend_on_input_order(self):
        results = [result(f"check{i}", State.WARNING) for i in range(5)]
        forward = active_problems(results, now=10)
        backward = active_problems(list(reversed(results)), now=10)
        self.assertEqual(forward, backward)

    def test_expired_result_sorted_as_unknown(self):
        results = [result("old.critical", State.CRITICAL, expires=5), result("warning", State.WARNING)]
        problems = active_problems(results, now=10)
        self.assertEqual([(p.id, p.state) for p in problems],
                         [("warning", State.WARNING), ("old.critical", State.UNKNOWN)])


class ExpiryTest(unittest.TestCase):
    def test_exact_expiry_boundary(self):
        check = result("disk", State.OK, expires=60)
        self.assertEqual(effective(check, now=59.999).state, State.OK)
        self.assertEqual(effective(check, now=60).state, State.UNKNOWN)  # hranice už neplatí
        self.assertEqual(effective(check, now=60.001).state, State.UNKNOWN)

    def test_expired_ok_is_not_ok(self):
        check = CheckResult("disk.smart", State.OK, "SMART bez chyb", OBSERVED, 60, simulated=False)
        current = effective(check, now=60)
        self.assertEqual(current.state, State.UNKNOWN)
        # Popis nesmí dál tvrdit, že je vše v pořádku, a musí říct, že data jsou stará.
        self.assertNotIn("bez chyb", current.summary)
        self.assertIn("disk.smart", current.summary)
        self.assertIn("zastaralá data", current.summary)

    def test_expired_warning_error_critical_are_not_confirmed_faults(self):
        for state in (State.WARNING, State.ERROR, State.CRITICAL):
            with self.subTest(state=state):
                check = result("x", state, expires=60)
                self.assertEqual(effective(check, now=60).state, State.UNKNOWN)
                self.assertEqual(overall_state([check], now=60), State.UNKNOWN)
                self.assertIn("zastaralá data", effective(check, now=60).summary)

    def test_na_does_not_expire(self):
        check = result("animedb", State.NA, expires=60)
        later = effective(check, now=10**9)
        self.assertEqual(later.state, State.NA)
        self.assertEqual(later.summary, "popis animedb")
        self.assertEqual(active_problems([check], now=10**9), [])

    def test_valid_result_is_unchanged(self):
        check = result("x", State.ERROR, expires=60)
        self.assertIs(effective(check, now=10), check)


class OverallStateTest(unittest.TestCase):
    def overall(self, *results, now=10):
        return overall_state(list(results), now)

    def test_no_results_is_unknown(self):
        self.assertEqual(self.overall(), State.UNKNOWN)

    def test_only_na_is_unknown(self):
        # Vše vypnuté = nic se nesleduje; to není "vše v pořádku".
        self.assertEqual(self.overall(result("a", State.NA), result("b", State.NA)), State.UNKNOWN)

    def test_valid_ok_info_na(self):
        self.assertEqual(self.overall(result("a", State.OK), result("b", State.NA)), State.OK)
        self.assertEqual(self.overall(result("a", State.OK), result("b", State.INFO)), State.INFO)

    def test_unknown_beats_ok(self):
        self.assertEqual(self.overall(result("a", State.OK), result("b", State.UNKNOWN)), State.UNKNOWN)

    def test_fault_beats_unknown(self):
        for state in (State.WARNING, State.ERROR, State.CRITICAL):
            with self.subTest(state=state):
                self.assertEqual(self.overall(result("a", State.UNKNOWN), result("b", state)), state)

    def test_worst_fault_wins(self):
        results = [result("a", State.WARNING), result("b", State.CRITICAL), result("c", State.ERROR)]
        self.assertEqual(self.overall(*results), State.CRITICAL)

    def test_expired_error_with_valid_ok_is_unknown(self):
        results = [result("a", State.ERROR, expires=5), result("b", State.OK)]
        self.assertEqual(overall_state(results, now=10), State.UNKNOWN)


class MonitorDataTest(unittest.TestCase):
    def test_has_monitor_data(self):
        self.assertFalse(has_monitor_data([]))
        self.assertFalse(has_monitor_data([result("a", State.NA)]))
        self.assertTrue(has_monitor_data([result("a", State.NA), result("b", State.OK)]))
        self.assertTrue(has_monitor_data([result("a", State.UNKNOWN)]))


class CheckResultTest(unittest.TestCase):
    def test_state_must_be_enum(self):
        with self.assertRaises(TypeError):
            CheckResult("x", "OK", "popis", OBSERVED, 10, simulated=False)

    def test_id_must_not_be_empty(self):
        with self.assertRaises(ValueError):
            CheckResult("", State.OK, "popis", OBSERVED, 10, simulated=False)

    def test_observed_must_be_aware_utc_datetime(self):
        invalid = [
            datetime(2026, 10, 19, 10, 0),  # bez časové zóny
            datetime(2026, 10, 19, 12, 0, tzinfo=timezone(timedelta(hours=2))),
            12345.0,  # monotónní čas na místě skutečného
        ]
        for observed in invalid:
            with self.subTest(observed=observed):
                with self.assertRaises(ValueError):
                    CheckResult("x", State.OK, "popis", observed, 10, simulated=False)

    def test_expiry_must_be_monotonic_seconds(self):
        for expires in (OBSERVED, True, "60"):
            with self.subTest(expires=expires):
                with self.assertRaises(TypeError):
                    CheckResult("x", State.OK, "popis", OBSERVED, expires, simulated=False)

    def test_simulated_flag_is_explicit(self):
        with self.assertRaises(TypeError):
            CheckResult("x", State.OK, "popis", OBSERVED, 10)  # chybí simulated
        with self.assertRaises(TypeError):
            CheckResult("x", State.OK, "popis", OBSERVED, 10, simulated="ano")
        self.assertTrue(result("x", State.OK, simulated=True).simulated)

    def test_na_label(self):
        self.assertEqual(State.NA.value, "N/A")


if __name__ == "__main__":
    unittest.main()
