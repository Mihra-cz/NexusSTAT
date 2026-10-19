"""Testy vykreslení 128 × 64. Bez Pillow se přeskočí; Core a automat na něm nezávisí."""

import unittest
from datetime import datetime, timezone

from nexusstat.config import OledConfig
from nexusstat.core import CheckResult, State
from nexusstat.oled import CAROUSEL_PAGES, OledController

try:
    from nexusstat.render import SYMBOL_BOX, render_screen
except ImportError:  # Pillow není nainstalované
    render_screen = None

WALL_TIME = datetime(2026, 10, 19, 14, 5)
OBSERVED = datetime(2026, 10, 19, 12, 5, tzinfo=timezone.utc)


def result(check_id, state, expires=1000, simulated=False):
    return CheckResult(check_id, state, f"popis {check_id}", OBSERVED, expires, simulated=simulated)


def symbol_area(image):
    x, y, size = SYMBOL_BOX
    return image.crop((x, y, x + size, y + size)).tobytes()


def is_blank(image):
    return image.getbbox() is None


class RenderCase(unittest.TestCase):
    """Společné pomocné metody; sama žádné testy neobsahuje."""

    def setUp(self):
        self.controller = OledController(OledConfig())

    def render(self, results, now=0):
        self.controller.update(now)
        return render_screen(self.controller, now, results, WALL_TIME)

    def nexus(self, results, now=0):
        self.controller = OledController(OledConfig())
        self.controller.press(0, problem_count=0)
        return self.render(results, now)

    def status(self, results, now=1):
        self.controller = OledController(OledConfig())
        self.controller.press(0, problem_count=0)
        self.controller.press(1, problem_count=0)
        return self.render(results, now)


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class SleepRenderTest(RenderCase):
    def test_sleep_is_blank_even_with_alarm(self):
        # Alarm sám displej nerozsvěcuje.
        image = self.render([result("nas", State.CRITICAL)])
        self.assertEqual(image.size, (128, 64))
        self.assertEqual(image.mode, "1")
        self.assertTrue(is_blank(image))


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class NexusSymbolRenderTest(RenderCase):
    def setUp(self):
        super().setUp()
        empty = self.nexus([result("ok", State.OK)])
        self.no_symbol = symbol_area(empty)
        self.triangle = symbol_area(self.nexus([result("w", State.WARNING)]))
        self.question = symbol_area(self.nexus([result("u", State.UNKNOWN)]))

    def test_symbols_are_distinct_and_visible(self):
        self.assertNotEqual(self.triangle, self.no_symbol)
        self.assertNotEqual(self.question, self.no_symbol)
        self.assertNotEqual(self.triangle, self.question)

    def test_ok_info_na_has_no_symbol(self):
        results = [result("a", State.OK), result("b", State.INFO), result("c", State.NA)]
        self.assertEqual(symbol_area(self.nexus(results)), self.no_symbol)

    def test_faults_show_triangle(self):
        for state in (State.WARNING, State.ERROR, State.CRITICAL):
            with self.subTest(state=state):
                self.assertEqual(symbol_area(self.nexus([result("x", state)])), self.triangle)

    def test_triangle_has_priority_over_unknown(self):
        results = [result("u", State.UNKNOWN), result("e", State.ERROR)]
        self.assertEqual(symbol_area(self.nexus(results)), self.triangle)

    def test_expired_fault_shows_question_mark(self):
        results = [result("e", State.ERROR, expires=5), result("ok", State.OK)]
        self.assertEqual(symbol_area(self.nexus(results, now=6)), self.question)

    def test_no_data_and_only_na_show_question_mark(self):
        self.assertEqual(symbol_area(self.nexus([])), self.question)
        self.assertEqual(symbol_area(self.nexus([result("animedb", State.NA)])), self.question)


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class PagesRenderTest(RenderCase):
    def test_all_carousel_pages_render(self):
        self.controller.press(0, problem_count=0)
        page_seconds = self.controller.config.page_seconds
        images = []
        for index, page in enumerate(CAROUSEL_PAGES):
            now = index * page_seconds
            self.assertEqual(self.controller.current_page(now), page)
            images.append(self.render([], now).tobytes())
        self.assertTrue(all(any(image) for image in images))
        self.assertEqual(len(set(images)), len(CAROUSEL_PAGES))  # stránky se liší

    def test_status_pages(self):
        results = [result(f"check{i}", State.WARNING) for i in range(6)]  # 2 stránky
        self.controller.press(0, problem_count=0)
        self.controller.press(1, problem_count=6)
        first = self.render(results, now=1)
        self.controller.press(2, problem_count=6)
        self.assertEqual(self.controller.status_page, 1)
        second = self.render(results, now=2)
        self.assertFalse(is_blank(first))
        self.assertFalse(is_blank(second))
        self.assertNotEqual(first.tobytes(), second.tobytes())

    def test_status_page_clamped_when_problems_shrink(self):
        many = [result(f"check{i}", State.ERROR) for i in range(6)]
        self.controller.press(0, problem_count=0)
        self.controller.press(1, problem_count=6)
        self.controller.press(2, problem_count=6)  # stránka 2
        # Zbyl jediný problém: vykreslí se poslední existující stránka, žádná výjimka.
        image = self.render(many[:1], now=3)
        self.assertFalse(is_blank(image))


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class NoDataRenderTest(RenderCase):
    def test_no_data_differs_from_all_ok(self):
        no_data = self.status([]).tobytes()
        all_ok = self.status([result("system", State.OK)]).tobytes()
        self.assertNotEqual(no_data, all_ok)

    def test_only_na_is_no_data(self):
        # Pouze vypnuté integrace nejsou "vše v pořádku".
        only_na = self.status([result("animedb", State.NA)]).tobytes()
        self.assertEqual(only_na, self.status([]).tobytes())


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class SimulationMarkTest(RenderCase):
    def test_simulated_results_are_marked(self):
        real = [result("a", State.OK)]
        simulated = [result("a", State.OK, simulated=True)]
        for draw_screen in (self.nexus, self.status):
            with self.subTest(screen=draw_screen.__name__):
                self.assertNotEqual(draw_screen(real).tobytes(), draw_screen(simulated).tobytes())

    def test_sim_mark_does_not_hide_symbol(self):
        image = self.nexus([result("w", State.WARNING, simulated=True)])
        self.assertEqual(symbol_area(image), symbol_area(self.nexus([result("w", State.WARNING)])))


if __name__ == "__main__":
    unittest.main()
