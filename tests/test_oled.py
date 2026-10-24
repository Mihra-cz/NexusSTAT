"""Testy stavového automatu OLED. Čas je simulovaný, nic se skutečně nečeká."""

import contextlib
import io
import unittest
from datetime import datetime, timezone
from itertools import product
from unittest import mock

from nexusstat import simulate

from nexusstat.config import OledConfig
from nexusstat.core import CheckResult, State, active_problems, effective, has_monitor_data
from nexusstat.oled import (
    ALERT_SYMBOL,
    CAROUSEL_PAGES,
    STATUS_LINES_PER_PAGE,
    UNKNOWN_SYMBOL,
    Mode,
    OledController,
    home_symbol,
    split_status_pages,
    status_page_count,
)
from nexusstat.oled_text import service_rows
from nexusstat.services import (
    ANIMEDB_ID,
    BACKUP_ID,
    NAS_ID,
    NEXUS_SERVICES_ID,
    SERVICE_LABELS,
    complete_service_results,
)
from nexusstat.system_monitor import LABELS as SYSTEM_LABELS

OBSERVED = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)


def result(check_id, state, expires=1000):
    return CheckResult(check_id, state, f"popis {check_id}", OBSERVED, expires, simulated=False)


def awake_controller(at=0, config=None):
    """Controller probuzený v čase `at` na stránce NEXUS."""
    controller = OledController(config or OledConfig())
    controller.press(at, problem_count=0)
    return controller


def at(controller, now):
    """Posune simulovaný čas a vrátí (režim, stránka kolotoče)."""
    controller.update(now)
    return controller.mode, controller.current_page(now)


class SleepAndWakeTest(unittest.TestCase):
    def test_starts_asleep(self):
        controller = OledController(OledConfig())
        self.assertIs(controller.mode, Mode.SLEEP)
        self.assertIsNone(controller.current_page(0))

    def test_stays_asleep_without_press(self):
        controller = OledController(OledConfig())
        self.assertEqual(at(controller, 10_000), (Mode.SLEEP, None))

    def test_first_press_wakes_on_nexus(self):
        controller = OledController(OledConfig())
        controller.press(5, problem_count=3)
        self.assertEqual(at(controller, 5), (Mode.CAROUSEL, "NEXUS"))


class CarouselTest(unittest.TestCase):
    def test_page_changes_every_10_seconds(self):
        controller = awake_controller(at=100)
        self.assertEqual(at(controller, 109.9), (Mode.CAROUSEL, "NEXUS"))
        self.assertEqual(at(controller, 110), (Mode.CAROUSEL, "SYSTEM"))
        self.assertEqual(at(controller, 120), (Mode.CAROUSEL, "DISK"))
        self.assertEqual(at(controller, 130), (Mode.CAROUSEL, "NETWORK"))
        self.assertEqual(at(controller, 140), (Mode.CAROUSEL, "SERVICES"))
        self.assertEqual(at(controller, 149.9), (Mode.CAROUSEL, "SERVICES"))
        self.assertEqual(at(controller, 150), (Mode.CAROUSEL, "NEXUS"))  # cyklus znovu

    def test_five_pages_in_order(self):
        # STORAGE (místo DISK) a CPU FAN jsou plán další fáze, zatím ne.
        self.assertEqual(CAROUSEL_PAGES, ("NEXUS", "SYSTEM", "DISK", "NETWORK", "SERVICES"))
        self.assertEqual(OledController(OledConfig()).pages, CAROUSEL_PAGES)

    def test_rotation_over_whole_window_returns_to_nexus(self):
        controller = awake_controller(at=0)
        seen = [at(controller, now)[1] for now in range(0, 300, 10)]
        # 300 s / 10 s = 30 přepnutí: přesně 6 celých cyklů, pořád v pořadí.
        self.assertEqual(seen, list(CAROUSEL_PAGES) * 6)
        self.assertEqual(at(controller, 300), (Mode.SLEEP, None))  # okno se pátou stránkou neprodloužilo

    def test_custom_page_interval_with_five_pages(self):
        config = OledConfig(carousel_seconds=60, page_seconds=4, status_idle_seconds=30)
        controller = awake_controller(at=0, config=config)
        self.assertEqual([at(controller, now)[1] for now in (0, 4, 8, 12, 16, 20)],
                         ["NEXUS", "SYSTEM", "DISK", "NETWORK", "SERVICES", "NEXUS"])
        self.assertEqual(at(controller, 60), (Mode.SLEEP, None))

    def test_sleeps_after_300_seconds(self):
        controller = awake_controller(at=100)
        self.assertEqual(at(controller, 399.9)[0], Mode.CAROUSEL)
        self.assertEqual(at(controller, 400), (Mode.SLEEP, None))

    def test_page_changes_do_not_extend_window(self):
        controller = awake_controller(at=0)
        for now in range(0, 300, 10):  # projde všemi přepnutími stránek
            controller.update(now)
        self.assertEqual(at(controller, 300)[0], Mode.SLEEP)

    def test_press_after_sleep_wakes_again_on_nexus(self):
        controller = awake_controller(at=0)
        at(controller, 300)
        controller.press(500, problem_count=2)
        self.assertEqual(at(controller, 500), (Mode.CAROUSEL, "NEXUS"))
        self.assertEqual(at(controller, 799.9)[0], Mode.CAROUSEL)

    def test_late_press_wakes_instead_of_opening_status(self):
        # Mezi koncem okna a stiskem nikdo nevolal update(): stisk musí displej
        # probudit, ne otevřít STATUS z kolotoče, který už vypršel.
        controller = awake_controller(at=0)
        controller.press(305, problem_count=2)
        self.assertEqual(at(controller, 305), (Mode.CAROUSEL, "NEXUS"))

    def test_custom_timing(self):
        config = OledConfig(carousel_seconds=20, page_seconds=5, status_idle_seconds=7)
        controller = awake_controller(at=0, config=config)
        self.assertEqual(at(controller, 5), (Mode.CAROUSEL, "SYSTEM"))
        self.assertEqual(at(controller, 20), (Mode.SLEEP, None))


class StatusTest(unittest.TestCase):
    def test_press_in_carousel_opens_status_first_page(self):
        controller = awake_controller(at=0)
        controller.press(15, problem_count=2)
        self.assertIs(controller.mode, Mode.STATUS)
        self.assertEqual(controller.status_page, 0)
        self.assertIsNone(controller.current_page(15))

    def test_status_without_problems_returns_on_press(self):
        controller = awake_controller(at=0)
        controller.press(10, problem_count=0)
        self.assertIs(controller.mode, Mode.STATUS)
        controller.press(12, problem_count=0)
        self.assertEqual(at(controller, 12), (Mode.CAROUSEL, "NEXUS"))

    def test_status_single_page_returns_on_press(self):
        controller = awake_controller(at=0)
        controller.press(10, problem_count=STATUS_LINES_PER_PAGE)
        controller.press(12, problem_count=STATUS_LINES_PER_PAGE)
        self.assertEqual(at(controller, 12), (Mode.CAROUSEL, "NEXUS"))

    def test_status_multiple_pages_then_return(self):
        problem_count = 2 * STATUS_LINES_PER_PAGE + 1  # 3 stránky
        controller = awake_controller(at=0)
        controller.press(10, problem_count)
        self.assertEqual((controller.mode, controller.status_page), (Mode.STATUS, 0))
        controller.press(11, problem_count)
        self.assertEqual((controller.mode, controller.status_page), (Mode.STATUS, 1))
        controller.press(12, problem_count)
        self.assertEqual((controller.mode, controller.status_page), (Mode.STATUS, 2))
        # Z poslední stránky se necyklí na první, ale vrací do kolotoče.
        controller.press(13, problem_count)
        self.assertEqual(at(controller, 13), (Mode.CAROUSEL, "NEXUS"))

    def test_problems_shrank_while_in_status(self):
        controller = awake_controller(at=0)
        controller.press(10, problem_count=9)
        controller.press(11, problem_count=9)  # stránka 2 ze 3
        # Problémy mezitím ubyly na jednu stránku → stisk vrací do kolotoče.
        controller.press(12, problem_count=1)
        self.assertIs(controller.mode, Mode.CAROUSEL)

    def test_status_returns_after_30_seconds_idle(self):
        controller = awake_controller(at=0)
        controller.press(50, problem_count=1)
        self.assertEqual(at(controller, 79.9)[0], Mode.STATUS)
        self.assertEqual(at(controller, 80), (Mode.CAROUSEL, "NEXUS"))

    def test_press_in_status_resets_idle_timer(self):
        controller = awake_controller(at=0)
        controller.press(50, problem_count=9)
        controller.press(70, problem_count=9)  # na stránku 2, časovač znovu od 70
        self.assertEqual(at(controller, 99.9)[0], Mode.STATUS)
        self.assertEqual(at(controller, 100)[0], Mode.CAROUSEL)


class CarouselResetTest(unittest.TestCase):
    def test_return_by_press_starts_new_window(self):
        controller = awake_controller(at=0)
        controller.press(290, problem_count=0)  # STATUS těsně před koncem okna
        controller.press(295, problem_count=0)  # návrat do CAROUSEL
        self.assertEqual(at(controller, 295), (Mode.CAROUSEL, "NEXUS"))
        self.assertEqual(at(controller, 305), (Mode.CAROUSEL, "SYSTEM"))
        self.assertEqual(at(controller, 594.9)[0], Mode.CAROUSEL)
        self.assertEqual(at(controller, 595), (Mode.SLEEP, None))

    def test_return_by_timeout_starts_new_window_on_nexus(self):
        controller = awake_controller(at=0)
        controller.press(25, problem_count=1)  # STATUS ze stránky DISK
        self.assertEqual(at(controller, 55), (Mode.CAROUSEL, "NEXUS"))
        self.assertEqual(at(controller, 65), (Mode.CAROUSEL, "SYSTEM"))
        self.assertEqual(at(controller, 354.9)[0], Mode.CAROUSEL)
        self.assertEqual(at(controller, 355), (Mode.SLEEP, None))

    def test_late_update_after_status_timeout_sleeps(self):
        # Nové okno začíná okamžikem vypršení STATUS (40 s), ne pozdním
        # voláním update(); při skoku o 1000 s tedy i ono už uplynulo.
        controller = awake_controller(at=0)
        controller.press(10, problem_count=1)
        self.assertEqual(at(controller, 1000), (Mode.SLEEP, None))


class FullScenarioTest(unittest.TestCase):
    def test_timeline(self):
        """Regrese dosavadního časování: celý průchod SLEEP → CAROUSEL ⇄ STATUS → SLEEP."""
        controller = OledController(OledConfig())
        problems = 5  # 2 stránky STATUS
        steps = [
            (0, None, (Mode.SLEEP, None)),
            (1, "press", (Mode.CAROUSEL, "NEXUS")),
            (11, None, (Mode.CAROUSEL, "SYSTEM")),
            (21, None, (Mode.CAROUSEL, "DISK")),
            (25, "press", (Mode.STATUS, None)),
            (30, "press", (Mode.STATUS, None)),  # stránka 2/2
            (35, "press", (Mode.CAROUSEL, "NEXUS")),  # z poslední stránky zpět, nové okno
            (40, "press", (Mode.STATUS, None)),
            (69.9, None, (Mode.STATUS, None)),
            (70, None, (Mode.CAROUSEL, "NEXUS")),  # 30 s nečinnosti
            # 299,9 s od návratu v čase 70: 29. přepnutí, s pěti stránkami SERVICES
            # (dříve se čtyřmi SYSTEM, se třemi DISK). Časování okna se nezměnilo.
            (369.9, None, (Mode.CAROUSEL, "SERVICES")),
            (370, None, (Mode.SLEEP, None)),  # 300 s od návratu
        ]
        for now, action, expected in steps:
            with self.subTest(now=now):
                if action == "press":
                    controller.press(now, problems)
                self.assertEqual(at(controller, now), expected)


class ServicesPageStatusTest(unittest.TestCase):
    def test_press_on_services_page_opens_status(self):
        controller = awake_controller(at=0)
        self.assertEqual(at(controller, 45), (Mode.CAROUSEL, "SERVICES"))
        controller.press(45, problem_count=1)
        self.assertIs(controller.mode, Mode.STATUS)
        # Nečinnost 30 s: návrat vždy na NEXUS s novým oknem, ne na SERVICES.
        self.assertEqual(at(controller, 75), (Mode.CAROUSEL, "NEXUS"))
        self.assertEqual(at(controller, 115), (Mode.CAROUSEL, "SERVICES"))
        self.assertEqual(at(controller, 375), (Mode.SLEEP, None))

    def test_return_by_press_from_services_starts_on_nexus(self):
        controller = awake_controller(at=0)
        controller.press(42, problem_count=0)  # ze SERVICES do STATUS
        controller.press(43, problem_count=0)  # jediná stránka → zpět
        self.assertEqual(at(controller, 43), (Mode.CAROUSEL, "NEXUS"))
        self.assertEqual(at(controller, 83), (Mode.CAROUSEL, "SERVICES"))


class NetworkPageStatusTest(unittest.TestCase):
    """Stránka NETWORK se ovládá stejně jako ostatní; STATUS a jeho timeouty beze změny."""

    def test_press_on_network_page_opens_status_and_returns_by_timeout(self):
        controller = awake_controller(at=0)
        self.assertEqual(at(controller, 35), (Mode.CAROUSEL, "NETWORK"))
        controller.press(35, problem_count=5)  # 2 stránky STATUS
        self.assertIs(controller.mode, Mode.STATUS)
        controller.press(40, problem_count=5)  # stránka 2/2, 30 s běží znovu od stisku
        self.assertEqual(at(controller, 69.9)[0], Mode.STATUS)
        self.assertEqual(at(controller, 70), (Mode.CAROUSEL, "NEXUS"))  # nové okno od vypršení
        self.assertEqual(at(controller, 100), (Mode.CAROUSEL, "NETWORK"))
        self.assertEqual(at(controller, 369.9)[0], Mode.CAROUSEL)
        self.assertEqual(at(controller, 370), (Mode.SLEEP, None))  # 300 s od návratu

    def test_return_by_press_from_network_starts_on_nexus(self):
        controller = awake_controller(at=0)
        controller.press(31, problem_count=0)  # z NETWORK do STATUS (bez závad = 1 stránka)
        controller.press(32, problem_count=0)
        self.assertEqual(at(controller, 32), (Mode.CAROUSEL, "NEXUS"))
        self.assertEqual(at(controller, 62), (Mode.CAROUSEL, "NETWORK"))


def service(service_id, state, expires=1000, simulated=True):
    return CheckResult(service_id, state, f"popis {service_id}", OBSERVED, expires, simulated=simulated)


class ServiceRowsTest(unittest.TestCase):
    def test_rows_in_catalog_order(self):
        self.assertEqual(list(SERVICE_LABELS.values()), ["NEXUS", "ANIMEDB", "NAS", "BACKUP"])

    def test_simulator_example(self):
        results = [
            service(NEXUS_SERVICES_ID, State.OK), service(ANIMEDB_ID, State.OK),
            service(NAS_ID, State.OK), service(BACKUP_ID, State.WARNING),
        ]
        self.assertEqual(service_rows(results, 10),
                         [("NEXUS", "OK"), ("ANIMEDB", "OK"), ("NAS", "OK"), ("BACKUP", "WARN")])

    def test_missing_service_rows_are_unknown_never_ok(self):
        # Pojistka i bez doplnění: chybějící služba se na SERVICES nikdy neukáže jako OK.
        self.assertEqual(service_rows([], 10), [(label, "UNKN") for label in SERVICE_LABELS.values()])


def real_system_results(state=State.INFO):
    """Skutečná (ne simulovaná) systémová měření bez závady."""
    return [CheckResult(check_id, state, f"{label} 50 %", OBSERVED, 1000, simulated=False)
            for check_id, label in SYSTEM_LABELS.items()]


def problem_states(results, now=10):
    return {problem.id: problem.state for problem in active_problems(results, now)}


class ServiceConsistencyTest(unittest.TestCase):
    """SERVICES, symbol na stránce NEXUS a STATUS vycházejí ze stejných výsledků."""

    def test_completion_adds_unknown_and_na(self):
        added = complete_service_results([], OBSERVED, 30)
        self.assertEqual([(r.id, r.state) for r in added], [
            (NEXUS_SERVICES_ID, State.UNKNOWN), (ANIMEDB_ID, State.NA), (NAS_ID, State.UNKNOWN), (BACKUP_ID, State.UNKNOWN),
        ])
        self.assertEqual(added[2].summary, "NAS: bez dat")
        # Doplněné výsledky tvrdí jen skutečnost (data nejsou), proto nejsou simulované a nemají hodnotu.
        self.assertFalse(any(result.simulated for result in added))
        self.assertEqual({result.measurement for result in added}, {None})

    def test_unmonitored_services_are_unknown_on_all_screens(self):
        # Skutečný režim dnes: systém měřený bez závady, kolektory služeb neexistují.
        results = complete_service_results(real_system_results(), OBSERVED, 30)
        self.assertEqual(service_rows(results, 10),
                         [("NEXUS", "UNKN"), ("ANIMEDB", "N/A"), ("NAS", "UNKN"), ("BACKUP", "UNKN")])
        self.assertEqual(home_symbol(results, 10), UNKNOWN_SYMBOL)  # "?", ne trojúhelník
        self.assertEqual(problem_states(results),
                         {NEXUS_SERVICES_ID: State.UNKNOWN, NAS_ID: State.UNKNOWN, BACKUP_ID: State.UNKNOWN})
        # UNKNOWN není potvrzená závada.
        self.assertFalse(any(state in (State.WARNING, State.ERROR, State.CRITICAL) for state in problem_states(results).values()))

    def test_nexus_is_never_ok_without_healthcheck(self):
        for results in ([], real_system_results(), real_system_results(State.OK)):
            completed = complete_service_results(results, OBSERVED, 30)
            with self.subTest(results=len(results)):
                self.assertEqual(service_rows(completed, 10)[0], ("NEXUS", "UNKN"))
                self.assertIs(problem_states(completed)[NEXUS_SERVICES_ID], State.UNKNOWN)

    def test_unmonitored_animedb_is_na_without_alarm(self):
        results = complete_service_results(real_system_results(), OBSERVED, 30)
        [animedb] = [r for r in results if r.id == ANIMEDB_ID]
        self.assertEqual((animedb.state, animedb.simulated, animedb.summary),
                         (State.NA, False, "AnimeDB: sledování vypnuto"))
        self.assertNotIn(ANIMEDB_ID, problem_states(results))
        self.assertIs(effective(animedb, 10**9).state, State.NA)  # N/A nevyprší
        # Samotné N/A nejsou sledovaná data ani alarm.
        only_na = complete_service_results([service(NEXUS_SERVICES_ID, State.NA), service(NAS_ID, State.NA),
                                            service(BACKUP_ID, State.NA)], OBSERVED, 30)
        self.assertFalse(has_monitor_data(only_na))
        self.assertEqual(active_problems(only_na, 10), [])

    def test_confirmed_fault_has_priority_over_unknown(self):
        results = complete_service_results(real_system_results() + [service(BACKUP_ID, State.WARNING)], OBSERVED, 30)
        self.assertEqual(home_symbol(results, 10), ALERT_SYMBOL)  # trojúhelník má přednost před "?"
        problems = active_problems(results, 10)
        self.assertEqual([(p.id, p.state) for p in problems],
                         [(BACKUP_ID, State.WARNING), (NAS_ID, State.UNKNOWN), (NEXUS_SERVICES_ID, State.UNKNOWN)])
        self.assertEqual(service_rows(results, 10)[3], ("BACKUP", "WARN"))

    def test_existing_results_are_kept(self):
        given = [service(NEXUS_SERVICES_ID, State.OK), service(ANIMEDB_ID, State.OK),
                 service(NAS_ID, State.OK), service(BACKUP_ID, State.WARNING)]
        self.assertEqual(complete_service_results(given, OBSERVED, 30), given)
        # Simulátor: všechny služby má, doplnění nic nepřidá ani nezmění.
        sample = simulate.sample_results(OBSERVED)
        self.assertEqual(complete_service_results(sample, OBSERVED, 30), sample)

    def test_every_state_combination_is_consistent(self):
        options = [State.OK, State.WARNING, State.ERROR, State.UNKNOWN, State.NA, None]  # None = výsledek nepřišel
        row_problem = {"OK": None, "N/A": None, "UNKN": State.UNKNOWN, "WARN": State.WARNING, "ERR": State.ERROR}
        for combination in product(options, repeat=len(SERVICE_LABELS)):
            given = [service(service_id, state, simulated=False)
                     for service_id, state in zip(SERVICE_LABELS, combination) if state is not None]
            results = complete_service_results(real_system_results() + given, OBSERVED, 30)
            rows = dict(service_rows(results, 10))
            problems = problem_states(results)
            with self.subTest(combination=combination):
                for service_id, label in SERVICE_LABELS.items():
                    self.assertIs(problems.get(service_id), row_problem[rows[label]])
                expected_symbol = (ALERT_SYMBOL if {"WARN", "ERR"} & set(rows.values())
                                   else UNKNOWN_SYMBOL if "UNKN" in rows.values() else None)
                self.assertEqual(home_symbol(results, 10), expected_symbol)

    def test_expired_service_is_unknown_everywhere(self):
        results = complete_service_results([service(NAS_ID, State.OK, expires=20)] + real_system_results(), OBSERVED, 30)
        self.assertEqual(service_rows(results, 19)[2], ("NAS", "OK"))
        self.assertNotIn(NAS_ID, problem_states(results, 19))
        self.assertEqual(service_rows(results, 20)[2], ("NAS", "UNKN"))
        self.assertIs(problem_states(results, 20)[NAS_ID], State.UNKNOWN)

    def test_all_states_and_expiry(self):
        cases = [(State.OK, "OK"), (State.INFO, "INFO"), (State.WARNING, "WARN"), (State.ERROR, "ERR"),
                 (State.CRITICAL, "CRIT"), (State.UNKNOWN, "UNKN"), (State.NA, "N/A")]
        for state, text in cases:
            with self.subTest(state=state):
                self.assertEqual(service_rows([service(NAS_ID, state)], 10)[2], ("NAS", text))
        # Prošlé OK už není OK; N/A nevyprší.
        results = [service(NAS_ID, State.OK, expires=5), service(ANIMEDB_ID, State.NA, expires=5)]
        self.assertEqual(service_rows(results, 4)[1:3], [("ANIMEDB", "N/A"), ("NAS", "OK")])
        self.assertEqual(service_rows(results, 5)[1:3], [("ANIMEDB", "N/A"), ("NAS", "UNKN")])

    def test_service_problems_appear_in_status(self):
        results = [service(BACKUP_ID, State.WARNING), service(NAS_ID, State.OK)]
        self.assertEqual([p.id for p in active_problems(results, 10)], [BACKUP_ID])
        self.assertEqual(home_symbol(results, 10), ALERT_SYMBOL)


class FakeMonitor:
    """Náhrada skutečných kolektorů pro simulate --live: skutečná (ne simulovaná) data bez závady."""

    labels = SYSTEM_LABELS  # stejné rozhraní jako run.Collectors

    def __init__(self, config):
        self.validity_seconds = config.collector.validity_seconds

    def collect(self, now, observed_utc):
        return [CheckResult(check_id, State.INFO, f"{label} 50 %", observed_utc, now + self.validity_seconds,
                            simulated=False) for check_id, label in SYSTEM_LABELS.items()]


class SimulatorScenarioTest(unittest.TestCase):
    def test_live_mode_completes_services(self):
        output = io.StringIO()
        with mock.patch.object(simulate, "live_monitor", FakeMonitor), \
                mock.patch.object(simulate.time, "sleep"), \
                mock.patch.object(simulate, "load_renderer", return_value=None), \
                contextlib.redirect_stdout(output):
            simulate.main(["/neexistujici-vystup", "--live"])
        lines = {line.split()[0]: line.split()[1] for line in output.getvalue().splitlines()}
        self.assertEqual({service_id: lines[service_id] for service_id in SERVICE_LABELS}, {
            NEXUS_SERVICES_ID: "UNKNOWN", ANIMEDB_ID: "N/A", NAS_ID: "UNKNOWN", BACKUP_ID: "UNKNOWN",
        })

    def test_scenario_visits_all_pages_and_keeps_timeouts(self):
        output = io.StringIO()
        # Bez rendereru: jen průběh scénáře, žádné PNG.
        with mock.patch.object(simulate, "load_renderer", return_value=None), contextlib.redirect_stdout(output):
            simulate.run(None, OledConfig())
        states = [line.split()[-1] for line in output.getvalue().splitlines()]
        self.assertEqual(states, [
            "SLEEP", "CAROUSEL/NEXUS", "CAROUSEL/SYSTEM", "CAROUSEL/DISK", "CAROUSEL/NETWORK", "CAROUSEL/SERVICES",
            "CAROUSEL/NEXUS",  # po SERVICES zpět na NEXUS
            "STATUS/1", "STATUS/2", "CAROUSEL/NEXUS", "STATUS/1",
            "CAROUSEL/NEXUS",  # 30 s bez stisku
            "SLEEP",  # 300 s od návratu
        ])

    def test_simulator_services_match_example_and_are_simulated(self):
        results = simulate.sample_results(OBSERVED)
        self.assertEqual(service_rows(results, 10),
                         [("NEXUS", "OK"), ("ANIMEDB", "OK"), ("NAS", "OK"), ("BACKUP", "WARN")])
        self.assertTrue(all(result.simulated for result in results))
        self.assertEqual(status_page_count(len(active_problems(results, 10))), 2)


class HomeSymbolTest(unittest.TestCase):
    def symbol(self, *results, now=10):
        return home_symbol(list(results), now)

    def test_all_valid_ok_info_na_has_no_symbol(self):
        self.assertIsNone(self.symbol(result("a", State.OK), result("b", State.INFO), result("c", State.NA)))

    def test_active_fault_shows_triangle(self):
        for state in (State.WARNING, State.ERROR, State.CRITICAL):
            with self.subTest(state=state):
                self.assertEqual(self.symbol(result("a", state), result("b", State.OK)), ALERT_SYMBOL)

    def test_unknown_without_fault_shows_question_mark(self):
        self.assertEqual(self.symbol(result("a", State.UNKNOWN), result("b", State.OK)), UNKNOWN_SYMBOL)

    def test_triangle_has_priority_over_unknown(self):
        for state in (State.WARNING, State.ERROR, State.CRITICAL):
            with self.subTest(state=state):
                self.assertEqual(self.symbol(result("a", State.UNKNOWN), result("b", state)), ALERT_SYMBOL)

    def test_expired_warning_or_error_shows_question_mark(self):
        # Prošlá závada už není potvrzená, ale ani vyřešená.
        for state in (State.WARNING, State.ERROR):
            with self.subTest(state=state):
                self.assertEqual(self.symbol(result("a", state, expires=5), result("b", State.OK)), UNKNOWN_SYMBOL)

    def test_expired_fault_with_valid_fault_shows_triangle(self):
        results = (result("a", State.ERROR, expires=5), result("b", State.WARNING))
        self.assertEqual(self.symbol(*results), ALERT_SYMBOL)

    def test_no_data_shows_question_mark(self):
        self.assertEqual(self.symbol(), UNKNOWN_SYMBOL)

    def test_only_na_shows_question_mark(self):
        self.assertEqual(self.symbol(result("animedb", State.NA)), UNKNOWN_SYMBOL)

    def test_na_after_long_time_does_not_add_symbol(self):
        results = (result("animedb", State.NA, expires=5), result("system", State.OK, expires=10**9))
        self.assertIsNone(self.symbol(*results, now=10**6))


class StatusPagesTest(unittest.TestCase):
    def test_page_count(self):
        per_page = STATUS_LINES_PER_PAGE
        self.assertEqual(status_page_count(0), 1)
        self.assertEqual(status_page_count(1), 1)
        self.assertEqual(status_page_count(per_page), 1)
        self.assertEqual(status_page_count(per_page + 1), 2)
        self.assertEqual(status_page_count(3 * per_page), 3)

    def test_split_keeps_order_and_matches_count(self):
        problems = list(range(2 * STATUS_LINES_PER_PAGE + 1))
        pages = split_status_pages(problems)
        self.assertEqual(len(pages), status_page_count(len(problems)))
        self.assertEqual([item for page in pages for item in page], problems)

    def test_split_empty_gives_one_empty_page(self):
        self.assertEqual(split_status_pages([]), [[]])

    def test_controller_needs_pages(self):
        with self.assertRaises(ValueError):
            OledController(OledConfig(), pages=())


if __name__ == "__main__":
    unittest.main()
