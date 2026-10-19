"""Testy vykreslení 128 × 64. Bez Pillow se přeskočí; Core a automat na něm nezávisí."""

import unittest
from datetime import datetime, timezone
from itertools import product

from nexusstat.config import OledConfig
from nexusstat.core import CheckResult, State
from nexusstat.oled import CAROUSEL_PAGES, STATE_TEXT, OledController, service_rows
from nexusstat.services import SERVICE_LABELS, complete_service_results
from nexusstat.simulate import sample_results

try:
    from PIL import Image, ImageDraw

    from nexusstat.render import (
        SERVICES_ROWS_PER_PAGE,
        SYMBOL_BOX,
        font,
        layout_service_rows,
        render_screen,
    )
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
class ServicesRenderTest(RenderCase):
    def services(self, results):
        self.controller = OledController(OledConfig())
        self.controller.press(0, problem_count=0)
        self.assertEqual(self.controller.current_page(30), "SERVICES")  # čtvrtá stránka po 30 s
        return self.render(results, now=30)

    def test_layout_fits_128x64_for_every_label_and_state(self):
        draw = ImageDraw.Draw(Image.new("1", (128, 64)))
        row_font = font(11)
        self.assertLessEqual(len(SERVICE_LABELS), SERVICES_ROWS_PER_PAGE)
        # Nejhorší kombinace: každý název s každým textem stavu na každém řádku.
        for label, state in product(SERVICE_LABELS.values(), set(STATE_TEXT.values())):
            rows = [(label, state)] * SERVICES_ROWS_PER_PAGE
            boxes = [
                (draw.textbbox(label_xy, label, font=row_font), draw.textbbox(state_xy, state, font=row_font))
                for label_xy, state_xy in layout_service_rows(draw, rows)
            ]
            for index, (label_box, state_box) in enumerate(boxes):
                with self.subTest(label=label, state=state, row=index):
                    # textbbox: levý a horní okraj včetně, pravý a dolní bez (jako crop).
                    for box in (label_box, state_box):
                        self.assertGreater(box[1], 15)  # pod oddělovací čarou nadpisu
                        self.assertLessEqual(box[3], 64)
                        self.assertGreaterEqual(box[0], 0)
                        self.assertLessEqual(box[2], 128)
                    self.assertLess(label_box[2] + 4, state_box[0])  # název a stav se nedotýkají
                    if index + 1 < len(boxes):
                        self.assertLess(max(label_box[3], state_box[3]), min(boxes[index + 1][0][1], boxes[index + 1][1][1]))

    def test_title_and_sim_mark_do_not_overlap(self):
        draw = ImageDraw.Draw(Image.new("1", (128, 64)))
        title = draw.textbbox((0, 0), "SLUŽBY", font=font(12, bold=True))
        mark_font = font(9, bold=True)
        sim_left = 127 - draw.textlength("SIM", font=mark_font)
        self.assertLess(title[2] + 4, sim_left)

    def test_state_text_is_not_clipped(self):
        # Stav vpravo končí sloupcem 126; sloupec 127 zůstává prázdný (žádné oříznutí).
        for state in set(STATE_TEXT.values()):
            rows = [("NAS", state)]
            image = Image.new("1", (128, 64))
            draw = ImageDraw.Draw(image)
            [(_, state_xy)] = layout_service_rows(draw, rows)
            draw.text(state_xy, state, font=font(11), fill=1)
            with self.subTest(state=state):
                self.assertIsNone(image.crop((127, 0, 128, 64)).getbbox())
                self.assertIsNotNone(image.crop((120, 0, 127, 64)).getbbox())

    def test_every_row_is_drawn_left_and_right(self):
        image = self.services(sample_results(OBSERVED))
        for index in range(SERVICES_ROWS_PER_PAGE):
            top = 14 + index * 12
            with self.subTest(row=index):
                self.assertIsNotNone(image.crop((0, top + 2, 60, top + 13)).getbbox())  # název
                self.assertIsNotNone(image.crop((90, top + 2, 128, top + 13)).getbbox())  # stav

    def test_simulated_services_have_sim_mark(self):
        simulated = self.services(sample_results(OBSERVED))
        self.assertIsNotNone(simulated.crop((100, 0, 128, 13)).getbbox())
        real = self.services(complete_service_results([], OBSERVED, 1000))
        self.assertIsNone(real.crop((100, 0, 128, 13)).getbbox())

    def test_unknown_services_differ_from_ok(self):
        ok = [CheckResult(i, State.OK, "ok", OBSERVED, 1000, simulated=False) for i in SERVICE_LABELS]
        self.assertNotEqual(self.services(ok).tobytes(), self.services([]).tobytes())
        # Bez kolektorů nikde OK: UNKN a N/A se kreslí jinak.
        unmonitored = complete_service_results([], OBSERVED, 1000)
        self.assertEqual(service_rows(unmonitored, 30)[0], ("NEXUS", "UNKN"))
        self.assertNotEqual(self.services(unmonitored).tobytes(), self.services(ok).tobytes())

    def test_services_page_differs_from_value_pages(self):
        results = sample_results(OBSERVED)
        services = self.services(results).tobytes()
        self.controller = OledController(OledConfig())
        self.controller.press(0, problem_count=0)
        disk = self.render(results, now=20).tobytes()
        self.assertNotEqual(services, disk)


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class ServiceConsistencyRenderTest(RenderCase):
    """Neznámé služby jsou vidět i na stránce NEXUS ("?") a ve STATUS, nejen na SLUŽBY."""

    def real_results(self):
        system = [CheckResult(i, State.INFO, "50 %", OBSERVED, 1000, simulated=False) for i in ("a", "b")]
        return complete_service_results(system, OBSERVED, 1000)

    def test_nexus_page_shows_question_mark(self):
        question = symbol_area(self.nexus([result("u", State.UNKNOWN)]))
        self.assertEqual(symbol_area(self.nexus(self.real_results())), question)

    def test_status_lists_unknown_services(self):
        # Tři neznámé služby: STATUS není "NO ACTIVE ISSUES" (to by tvrdilo, že nic nechybí).
        with_unknown = self.status(self.real_results()).tobytes()
        no_issues = self.status([result("ok", State.OK)]).tobytes()
        self.assertNotEqual(with_unknown, no_issues)


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class TextFitTest(unittest.TestCase):
    """Zkrácený text se měří podle skutečně vykreslených znaků, ne podle šířky pro posun."""

    def test_fitted_text_ink_stays_inside(self):
        from nexusstat.render import fit_text, ink_width
        draw = ImageDraw.Draw(Image.new("1", (400, 40)))
        # Regrese: tyto texty se vešly podle šířky pro posun, ale inkoust přesáhl o 1 px.
        cases = [("chybí disk.smart: v", 10, 95), ("Záloha Teplota Čas", 10, 95), ("pro 26 h v Záloha čtení", 11, 128)]
        for text, size, max_width in cases:
            with self.subTest(text=text):
                self.assertLessEqual(ink_width(draw, fit_text(draw, text, font(size), max_width), font(size)), max_width)

    def test_many_czech_texts_fit(self):
        import random
        from nexusstat.render import fit_text, ink_width
        draw = ImageDraw.Draw(Image.new("1", (400, 40)))
        rng = random.Random(4)
        alphabet = "aábcčdďeéěfghiíjklmnňoópqrřsštťuúůvwxyýzžAÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0123456789 %°/:.,-"
        for _ in range(300):  # explicitní regrese jsou v testu výše; tady šíře vzorku
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(3, 35)))
            for size, max_width in ((10, 95), (11, 128)):
                fitted = fit_text(draw, text, font(size), max_width)
                self.assertLessEqual(ink_width(draw, fitted, font(size)), max_width, (text, fitted))


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
