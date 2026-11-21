"""Testy souhrnu hostitele NEXUS (M7-S11): odvozený řádek jen pro zobrazení ze stávajících
výsledků kontrol hostitele. Bez nového měření, bez druhého alarmu ve STATUS, RGB a heartbeatu."""

import unittest
from datetime import datetime, timezone

from nexusstat import rgb
from nexusstat.core import CheckResult, State, active_problems, has_monitor_data, overall_state, summary_state
from nexusstat.fan_monitor import CPU_FAN_ID as FAN_ID
from nexusstat.heartbeat import public_state
from nexusstat.mounts import ROOT_MODE_ID
from nexusstat.network_monitor import IPV4_ID, LINK_ID, RX_RATE_ID, TX_RATE_ID
from nexusstat.services import (ANIMEDB_ID, BACKUP_ID, NAS_ID, NEXUS_SERVICES_ID, complete_service_results,
                                host_summary)
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID

OBSERVED = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
NOW = 1000.0
# Skutečné kontroly hostitele z run.live_monitor (ověřeno na Nexusu, M7-S11).
HOST = (CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID, FAN_ID, DISK_ROOT_ID, ROOT_MODE_ID, LINK_ID, RX_RATE_ID, TX_RATE_ID,
        IPV4_ID)
MONITORED = set(HOST) | {ANIMEDB_ID, NAS_ID}


def result(check_id, state, expires=NOW + 30):
    return CheckResult(check_id, state, "popis", OBSERVED, expires, simulated=False)


def healthy(**states):
    """Všechny kontroly hostitele INFO (jako bez prahů na Nexusu), AnimeDB a NAS OK; `states` přepíše stav."""
    taken = [result(check_id, states.get(check_id, State.INFO)) for check_id in HOST]
    return taken + [result(ANIMEDB_ID, states.get(ANIMEDB_ID, State.OK)), result(NAS_ID, states.get(NAS_ID, State.OK))]


def nexus(results, monitored=MONITORED, now=NOW):
    completed = complete_service_results(results, OBSERVED, now + 30, monitored=monitored, now=now)
    return {item.id: item for item in completed}[NEXUS_SERVICES_ID]


class HostSummaryTest(unittest.TestCase):
    def test_states(self):
        cases = {
            "zdravý hostitel": (healthy(), State.OK),
            "CPU WARNING": (healthy(**{CPU_TEMP_ID: State.WARNING}), State.WARNING),
            "ROOT ERROR": (healthy(**{ROOT_MODE_ID: State.ERROR}), State.ERROR),
            "CRITICAL": (healthy(**{DISK_ROOT_ID: State.CRITICAL}), State.CRITICAL),
            "UNKNOWN kontroly": (healthy(**{CPU_USAGE_ID: State.UNKNOWN}), State.UNKNOWN),
            "N/A se ignoruje": (healthy(**{FAN_ID: State.NA}), State.OK),
            "AnimeDB ERROR nemění hostitele": (healthy(**{ANIMEDB_ID: State.ERROR}), State.OK),
            "NAS ERROR nemění hostitele": (healthy(**{NAS_ID: State.ERROR}), State.OK),
            "historie a záloha nemění hostitele": (
                healthy() + [result("history.storage", State.ERROR), result(BACKUP_ID, State.ERROR)], State.OK),
        }
        for name, (taken, state) in cases.items():
            with self.subTest(name):
                row = nexus(taken)
                self.assertIs(row.state, state)
                self.assertTrue(row.derived)
                self.assertFalse(row.simulated)

    def test_expired_or_missing_active_check_is_unknown_unless_worse(self):
        expired = [result(check_id, State.INFO, expires=NOW - 1 if check_id == LINK_ID else NOW + 30)
                   for check_id in HOST]
        self.assertIs(nexus(expired).state, State.UNKNOWN)
        missing = [item for item in healthy() if item.id != IPV4_ID]
        self.assertIs(nexus(missing).state, State.UNKNOWN)
        self.assertEqual(host_summary(missing, MONITORED, NOW, OBSERVED), (State.UNKNOWN, [IPV4_ID]))
        # Závažnější skutečná závada má přednost před chybějícím nebo prošlým měřením.
        worse = [item for item in healthy(**{CPU_TEMP_ID: State.ERROR}) if item.id != IPV4_ID]
        self.assertEqual(host_summary(worse, MONITORED, NOW, OBSERVED), (State.ERROR, [CPU_TEMP_ID]))
        # Kontrola, kterou žádný kolektor neměří, se nedoplňuje.
        self.assertIs(nexus(missing, monitored=MONITORED - {IPV4_ID}).state, State.OK)

    def test_no_measurements_is_unknown(self):
        self.assertIs(nexus([]).state, State.UNKNOWN)
        self.assertIs(nexus([], monitored=()).state, State.UNKNOWN)
        self.assertEqual(nexus([], monitored=()).summary, "NEXUS: žádná měření hostitele")
        self.assertEqual(nexus(healthy()).summary, "NEXUS: hostitel v pořádku (10 kontrol)")
        self.assertEqual(nexus(healthy(**{CPU_TEMP_ID: State.WARNING})).summary, f"NEXUS: WARNING podle {CPU_TEMP_ID}")

    def test_simulated_host_gives_simulated_summary(self):
        taken = [CheckResult(check_id, State.INFO, "sim", OBSERVED, NOW + 30, simulated=True) for check_id in HOST]
        self.assertTrue(nexus(taken).simulated)

    def test_real_nexus_collector_keeps_precedence(self):
        real = result(NEXUS_SERVICES_ID, State.WARNING)
        self.assertIs(nexus(healthy() + [real]), real)
        # Zapnutý kolektor NEXUS bez výsledku je UNKNOWN jako každá jiná sledovaná služba, ne souhrn.
        row = nexus(healthy(), monitored=MONITORED | {NEXUS_SERVICES_ID})
        self.assertEqual((row.state, row.derived), (State.UNKNOWN, False))

    def test_without_time_summary_is_not_evaluated(self):
        completed = complete_service_results(healthy(), OBSERVED, NOW + 30, monitored=MONITORED)
        row = {item.id: item for item in completed}[NEXUS_SERVICES_ID]
        self.assertEqual((row.state, row.derived), (State.NA, True))


class NoDuplicateAlarmTest(unittest.TestCase):
    """Souhrn NEXUS je jen řádek k zobrazení: závadu nese a počítá dál jen původní výsledek."""

    def completed(self, taken):
        return complete_service_results(taken, OBSERVED, NOW + 30, monitored=MONITORED, now=NOW)

    def test_status_lists_the_fault_once_and_overall_includes_services(self):
        taken = healthy(**{CPU_TEMP_ID: State.WARNING, NAS_ID: State.ERROR})
        completed = self.completed(taken)
        self.assertIs({item.id: item for item in completed}[NEXUS_SERVICES_ID].state, State.WARNING)
        self.assertEqual([item.id for item in active_problems(completed, NOW)], [NAS_ID, CPU_TEMP_ID])
        self.assertIs(summary_state(completed, NOW), State.ERROR)  # hostitel, AnimeDB i NAS
        self.assertIs(summary_state(self.completed(healthy(**{ANIMEDB_ID: State.ERROR})), NOW), State.ERROR)
        self.assertIs(summary_state(self.completed(healthy()), NOW), State.OK)

    def test_derived_row_is_never_data_or_a_problem(self):
        completed = complete_service_results([], OBSERVED, NOW + 30, now=NOW)  # NEXUS UNKNOWN, služby N/A
        self.assertIs({item.id: item for item in completed}[NEXUS_SERVICES_ID].state, State.UNKNOWN)
        self.assertFalse(has_monitor_data(completed))
        self.assertEqual(active_problems(completed, NOW), [])
        self.assertIs(overall_state(completed, NOW), State.UNKNOWN)  # stejně jako dřív se samými N/A

    def test_rgb_signal_is_unchanged(self):
        # LED dostávají skutečně monitorované výsledky (run.run_loop), řádek NEXUS mezi nimi není.
        taken = healthy(**{CPU_TEMP_ID: State.WARNING})
        signal = rgb.rgb_signal(taken, rgb.Acknowledgements(), True, False, NOW)
        self.assertEqual(signal, rgb.Signal(rgb.CODES, ((State.WARNING, 1),)))
        self.assertNotIn(NEXUS_SERVICES_ID, {item.id for item in taken})

    def test_heartbeat_keeps_its_evaluation(self):
        cases = {
            "zdravý": (healthy(), "OK"),
            "CPU WARNING": (healthy(**{CPU_TEMP_ID: State.WARNING}), "WARNING"),
            "AnimeDB ERROR": (healthy(**{ANIMEDB_ID: State.ERROR}), "ERROR"),
            # Bez kontrol hostitele by odvozený NEXUS byl UNKNOWN; veřejný stav ho nepočítá jako dřív N/A.
            "jen služby": ([result(ANIMEDB_ID, State.OK), result(NAS_ID, State.OK)], "OK"),
        }
        for name, (taken, state) in cases.items():
            with self.subTest(name):
                monitored = MONITORED if name != "jen služby" else {ANIMEDB_ID, NAS_ID}
                self.assertEqual(public_state(taken, NOW, OBSERVED, monitored, 30), state)

    def test_derived_flag_is_validated(self):
        with self.assertRaises(TypeError):
            CheckResult(NEXUS_SERVICES_ID, State.OK, "x", OBSERVED, NOW, simulated=False, derived=1)


if __name__ == "__main__":
    unittest.main()
