import unittest
from datetime import datetime, timedelta, timezone

from nexusstat.core import (
    CheckResult,
    Measurement,
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


class MeasurementTest(unittest.TestCase):
    def test_valid_measurement(self):
        self.assertEqual(Measurement(55.4, "°C").value, 55.4)
        self.assertEqual(Measurement(21, "%").unit, "%")

    def test_invalid_value(self):
        for value in (float("nan"), float("inf"), -float("inf"), True, "55", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Measurement(value, "°C")

    def test_huge_integer_is_rejected_as_value_error(self):
        # Regrese: 10**400 vyvolalo OverflowError místo srozumitelné ValueError.
        with self.assertRaises(ValueError):
            Measurement(10**400, "°C")

    def test_invalid_unit(self):
        for unit in ("", None, 5):
            with self.subTest(unit=unit):
                with self.assertRaises(ValueError):
                    Measurement(1.0, unit)

    def test_result_keeps_measurement(self):
        check = CheckResult("t", State.INFO, "Teplota 55 °C", OBSERVED, 60, simulated=False,
                            measurement=Measurement(55.4, "°C"))
        self.assertEqual(effective(check, now=10).measurement, Measurement(55.4, "°C"))
        self.assertIsNone(result("x", State.INFO).measurement)  # výchozí: bez číselné hodnoty

    def test_unknown_and_na_cannot_have_value(self):
        # Neznámý stav s číslem by se dal omylem vyhodnotit jako platné měření.
        for state in (State.UNKNOWN, State.NA):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    CheckResult("x", state, "popis", OBSERVED, 60, simulated=False, measurement=Measurement(1, "%"))

    def test_measurement_type_is_checked(self):
        with self.assertRaises(TypeError):
            CheckResult("x", State.INFO, "popis", OBSERVED, 60, simulated=False, measurement=55.4)

    def test_expiry_removes_value(self):
        for state in (State.INFO, State.WARNING, State.ERROR):
            with self.subTest(state=state):
                check = CheckResult("t", state, "Teplota 90 °C", OBSERVED, 60, simulated=True,
                                    measurement=Measurement(90, "°C"))
                stale = effective(check, now=60)
                self.assertIs(stale.state, State.UNKNOWN)
                self.assertIsNone(stale.measurement)
                self.assertTrue(stale.simulated)
                self.assertEqual(stale.observed_utc, OBSERVED)


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

    def test_expiry_must_be_finite(self):
        # Regrese: NaN nebo nekonečná platnost by nikdy nevypršela na UNKNOWN.
        for expires in (float("nan"), float("inf"), -float("inf"), 10**400):
            with self.subTest(expires=expires):
                with self.assertRaises(ValueError):
                    CheckResult("x", State.OK, "popis", OBSERVED, expires, simulated=False)

    def test_simulated_flag_is_explicit(self):
        with self.assertRaises(TypeError):
            CheckResult("x", State.OK, "popis", OBSERVED, 10)  # chybí simulated
        with self.assertRaises(TypeError):
            CheckResult("x", State.OK, "popis", OBSERVED, 10, simulated="ano")
        self.assertTrue(result("x", State.OK, simulated=True).simulated)

    def test_na_label(self):
        self.assertEqual(State.NA.value, "N/A")


class DetailsTest(unittest.TestCase):
    """Krátké textové údaje k výsledku (název rozhraní, IPv4) pro displej."""

    def make(self, details, state=State.INFO, expires=10):
        return CheckResult("network.ipv4", state, "popis", OBSERVED, expires, simulated=False, details=details)

    def test_default_is_empty(self):
        self.assertEqual(result("x", State.OK).details, {})

    def test_details_are_kept_and_read_only(self):
        given = {"interface": "eth0", "ipv4": "192.0.2.10"}
        checked = self.make(given)
        self.assertEqual(checked.details, given)
        given["ipv4"] = "198.51.100.1"  # změna předaného slovníku výsledek nezmění
        self.assertEqual(checked.details["ipv4"], "192.0.2.10")
        with self.assertRaises(TypeError):
            checked.details["ipv4"] = "198.51.100.1"

    def test_unknown_may_name_what_is_measured(self):
        self.assertEqual(self.make({"interface": "eth1"}, state=State.UNKNOWN).details, {"interface": "eth1"})

    def test_invalid_details(self):
        for details in ({"": "x"}, {"x": ""}, {"x": 5}, {5: "x"}, {"x": "eth\n0"}, {"x": "ethé"}, ["x"], "x"):
            with self.subTest(details=details):
                with self.assertRaises((TypeError, ValueError)):
                    self.make(details)

    def test_expiry_removes_details(self):
        expired = effective(self.make({"interface": "eth0", "ipv4": "192.0.2.10"}), 10)
        self.assertIs(expired.state, State.UNKNOWN)
        self.assertEqual(expired.details, {})
        self.assertEqual(effective(self.make({"interface": "eth0"}), 9.9).details, {"interface": "eth0"})

    def test_na_keeps_details(self):
        self.assertEqual(effective(self.make({"interface": "eth0"}, state=State.NA), 1000).details,
                         {"interface": "eth0"})

    def test_equality_includes_details(self):
        self.assertEqual(self.make({"interface": "eth0"}), self.make({"interface": "eth0"}))
        self.assertNotEqual(self.make({"interface": "eth0"}), self.make({"interface": "eth1"}))


if __name__ == "__main__":
    unittest.main()
