"""Testy stavového automatu OLED. Čas je simulovaný, nic se skutečně nečeká."""

import unittest
from datetime import datetime, timezone

from nexusstat.config import OledConfig
from nexusstat.core import CheckResult, State
from nexusstat.oled import (
    ALERT_SYMBOL,
    STATUS_LINES_PER_PAGE,
    UNKNOWN_SYMBOL,
    Mode,
    OledController,
    home_symbol,
    split_status_pages,
    status_page_count,
)

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
        self.assertEqual(at(controller, 130), (Mode.CAROUSEL, "NEXUS"))  # cyklus znovu

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
            (369.9, None, (Mode.CAROUSEL, "DISK")),
            (370, None, (Mode.SLEEP, None)),  # 300 s od návratu
        ]
        for now, action, expected in steps:
            with self.subTest(now=now):
                if action == "press":
                    controller.press(now, problems)
                self.assertEqual(at(controller, now), expected)


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
