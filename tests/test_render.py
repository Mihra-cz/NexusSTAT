"""Testy vykreslení 128 × 64. Bez Pillow se přeskočí; Core a automat na něm nezávisí."""

import gzip
import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from unittest import mock

from nexusstat.config import OledConfig
from nexusstat.core import CheckResult, Measurement, State
from nexusstat.oled import ALERT_SYMBOL, CAROUSEL_PAGES, UNKNOWN_SYMBOL, OledController
from nexusstat.oled_text import STATE_TEXT, service_rows
from nexusstat.services import SERVICE_LABELS, complete_service_results
from nexusstat.simulate import sample_results

try:
    from PIL import Image, ImageChops, ImageDraw

    from nexusstat import render
    from nexusstat.render import (
        HEADER_LINE_Y,
        RIGHT_EDGE,
        SERVICES_ROWS_PER_PAGE,
        SYMBOL_BOX,
        TEXT_FONT,
        TITLE_FONT,
        fit_text,
        font,
        ink_box,
        ink_width,
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
        def placed(xy, text, text_font):
            left, top, right, bottom = ink_box(text, text_font)  # skutečné pixely, pravý a dolní okraj bez
            return (xy[0] + left, xy[1] + top, xy[0] + right, xy[1] + bottom)

        self.assertLessEqual(len(SERVICE_LABELS), SERVICES_ROWS_PER_PAGE)
        # Nejhorší kombinace: každý název s každým textem stavu na každém řádku.
        for label, state in product(SERVICE_LABELS.values(), set(STATE_TEXT.values())):
            rows = [(label, state)] * SERVICES_ROWS_PER_PAGE
            boxes = [
                (placed(label_xy, label, font(TEXT_FONT)), placed(state_xy, state, font(TITLE_FONT)))
                for label_xy, state_xy in layout_service_rows(rows)
            ]
            for index, (label_box, state_box) in enumerate(boxes):
                with self.subTest(label=label, state=state, row=index):
                    for box in (label_box, state_box):
                        self.assertGreater(box[1], HEADER_LINE_Y)  # pod oddělovací čarou nadpisu
                        self.assertLessEqual(box[3], 64)
                        self.assertGreaterEqual(box[0], 0)
                        self.assertLessEqual(box[2], RIGHT_EDGE + 1)
                    self.assertLess(label_box[2] + 4, state_box[0])  # název a stav se nedotýkají
                    if index + 1 < len(boxes):
                        self.assertLess(max(label_box[3], state_box[3]), min(boxes[index + 1][0][1], boxes[index + 1][1][1]))

    def test_title_and_sim_mark_do_not_overlap(self):
        title_right = ink_width("SERVICES", font(TITLE_FONT))
        sim_left = RIGHT_EDGE + 1 - ink_width("SIM", font(TEXT_FONT))
        self.assertLess(title_right + 4, sim_left)

    def test_state_text_is_not_clipped(self):
        # Stav vpravo končí sloupcem 126; sloupec 127 zůstává prázdný (žádné oříznutí).
        for state in set(STATE_TEXT.values()):
            rows = [("NAS", state)]
            image = Image.new("1", (128, 64))
            draw = ImageDraw.Draw(image)
            [(_, state_xy)] = layout_service_rows(rows)
            draw.text(state_xy, state, font=font(TITLE_FONT), fill=1)
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
    """Neznámé služby jsou vidět i na stránce NEXUS ("?") a ve STATUS, nejen na SERVICES."""

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
        # Regrese (v0.4): tyto texty se vešly podle šířky pro posun, ale inkoust přesáhl o 1 px.
        status_width = RIGHT_EDGE + 1 - render.STATUS_TEXT_X
        cases = [("chybí disk.smart: v", status_width), ("Záloha Teplota Čas", status_width),
                 ("pro 26 h v Záloha čtení", RIGHT_EDGE + 1)]
        for text, max_width in cases:
            for name in (TEXT_FONT, TITLE_FONT):
                with self.subTest(text=text, font=name):
                    self.assertLessEqual(ink_width(fit_text(text, font(name), max_width), font(name)), max_width)

    def test_many_czech_texts_fit(self):
        import random
        rng = random.Random(4)
        alphabet = "aábcčdďeéěfghiíjklmnňoópqrřsštťuúůvwxyýzžAÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0123456789 %°/:.,-"
        for _ in range(300):  # explicitní regrese jsou v testu výše; tady šíře vzorku
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(3, 35)))
            for max_width in (RIGHT_EDGE + 1 - render.STATUS_TEXT_X, RIGHT_EDGE + 1):
                fitted = fit_text(text, font(TEXT_FONT), max_width)
                self.assertLessEqual(ink_width(fitted, font(TEXT_FONT)), max_width, (text, fitted))


def ink_groups(image, top, bottom, merge_gap=2):
    """Vodorovné skupiny inkoustu v řádcích top..bottom: [(levý, pravý)], mezery do merge_gap px se slučují."""
    columns = [x for x in range(image.width) if image.crop((x, top, x + 1, bottom)).getbbox()]
    groups = []
    for x in columns:
        if groups and x - groups[-1][1] <= merge_gap + 1:
            groups[-1][1] = x
        else:
            groups.append([x, x])
    return [tuple(group) for group in groups]


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class HomePageLayoutTest(unittest.TestCase):
    """Stránka NEXUS: nápis, hodiny a datum vystředěné podle skutečně vykreslených pixelů."""

    TIMES = [datetime(2026, 1, 1, 0, 0), datetime(2026, 10, 19, 11, 11), datetime(2026, 12, 28, 23, 58),
             datetime(2026, 7, 4, 19, 28), datetime(2026, 2, 9, 7, 5)]
    # Řádky inkoustu (horní včetně, dolní bez): nápis, hodiny 2×, datum.
    BANDS = {"nápis": (2, 12), "hodiny": (21, 41), "datum": (50, 60)}

    def nexus(self, wall_time, symbol=None, simulated=False):
        image = Image.new("1", (128, 64))
        render.draw_nexus_page(image, wall_time, symbol, simulated)
        return image

    def band_box(self, image, band, right=128):
        top, bottom = self.BANDS[band]
        return image.crop((0, top, right, bottom)).getbbox()

    def test_three_elements_centered_within_one_pixel(self):
        for wall_time in self.TIMES:
            image = self.nexus(wall_time)
            for band in self.BANDS:
                left, _, right, _ = self.band_box(image, band)
                with self.subTest(time=wall_time, band=band):
                    self.assertLessEqual(abs(left - (128 - right)), 1)  # levý a pravý okraj

    def test_vertical_bands_spacing_and_dominant_clock(self):
        image = self.nexus(datetime(2026, 12, 28, 23, 58))
        rows = {y for y in range(64) if image.crop((0, y, 128, y + 1)).getbbox()}
        self.assertEqual(rows, set(range(2, 12)) | set(range(21, 41)) | set(range(50, 60)))  # mezery 9 px
        boxes = {band: self.band_box(image, band) for band in self.BANDS}
        heights = {band: box[3] - box[1] for band, box in boxes.items()}
        widths = {band: box[2] - box[0] for band, box in boxes.items()}
        self.assertEqual(heights, {"nápis": 10, "hodiny": 20, "datum": 10})
        self.assertGreater(widths["hodiny"], max(widths["nápis"], widths["datum"]))  # hodiny dominují

    def test_symbol_does_not_move_or_touch_centered_text(self):
        for wall_time in self.TIMES[:3]:
            plain = self.nexus(wall_time)
            for symbol in (ALERT_SYMBOL, UNKNOWN_SYMBOL):
                with_symbol = self.nexus(wall_time, symbol)
                with self.subTest(time=wall_time, symbol=symbol):
                    # Mimo oblast symbolu jsou obrázky shodné: vystředění na symbolu nezávisí.
                    left_part = (0, 0, SYMBOL_BOX[0], 64)
                    self.assertEqual(plain.crop(left_part).tobytes(), with_symbol.crop(left_part).tobytes())
                    x, y, size = SYMBOL_BOX
                    self.assertIsNotNone(with_symbol.crop((x, y, x + size, y + size)).getbbox())
                    self.assertLess(self.band_box(plain, "nápis")[2] + 4, SYMBOL_BOX[0])  # nápis se nedotýká

    def test_sim_mark_only_when_simulated_and_clear_of_date(self):
        for wall_time in self.TIMES:
            plain, simulated = self.nexus(wall_time), self.nexus(wall_time, simulated=True)
            sim_box = ImageChops.logical_xor(plain, simulated).getbbox()
            date_box = self.band_box(plain, "datum")
            with self.subTest(time=wall_time):
                self.assertIsNotNone(sim_box)
                self.assertGreaterEqual(sim_box[0] - date_box[2], 4)  # aspoň 4 volné sloupce od data
                self.assertGreaterEqual(sim_box[1], 50)
                self.assertLessEqual(sim_box[3], 60)  # v pásmu data
                self.assertLessEqual(sim_box[2], 127)  # sloupec 127 volný
                left_part = (0, 0, sim_box[0], 64)
                self.assertEqual(plain.crop(left_part).tobytes(), simulated.crop(left_part).tobytes())


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class PageBoundsTest(unittest.TestCase):
    """Nic se nekreslí mimo 128 × 64 a texty v záhlaví ani v řádcích se nepřekrývají."""

    LONG = "VERY LONG CHECK NAME THAT CANNOT FIT"

    def problem(self, state):
        # Dlouhé neznámé id a obří hodnota: STATUS ukáže zkrácený anglický název.
        value = None if state is State.UNKNOWN else Measurement(10**12, "°C")
        return CheckResult(f"nas.share_media_mount.{state.value}", state, "Dlouhý český popis, který se nekreslí",
                           OBSERVED, 1000, simulated=True, measurement=value)

    def screens(self):
        late = datetime(2026, 12, 28, 23, 58)
        rows = [(label, "UNKN") for label in SERVICE_LABELS.values()]
        problems = [self.problem(state) for state in (State.CRITICAL, State.ERROR, State.WARNING, State.UNKNOWN)]
        long_rows = [(self.LONG, "1" * 30)] * 3  # nesmyslně dlouhý název i hodnota
        return {
            "NEXUS ! SIM": lambda image: render.draw_nexus_page(image, late, ALERT_SYMBOL, True),
            "NEXUS ? SIM": lambda image: render.draw_nexus_page(image, late, UNKNOWN_SYMBOL, True),
            "SYSTEM": lambda image: render.draw_value_page(image, "SYSTEM", long_rows, True),
            "SERVICES": lambda image: render.draw_services_page(image, rows, True),
            "STATUS 10/10": lambda image: render.draw_status_page(image, problems, 0, 10, 10, True, True),
            "NO ACTIVE ISSUES": lambda image: render.draw_status_page(image, [], 0, 1, 1, True, True),
            "NO MONITOR DATA": lambda image: render.draw_status_page(image, [], 0, 1, 1, False, True),
        }

    def test_nothing_drawn_outside_display(self):
        for name, draw_screen in self.screens().items():
            canvas = Image.new("1", (200, 100))  # větší plátno: přesah by byl vidět
            draw_screen(canvas)
            outside = canvas.copy()
            ImageDraw.Draw(outside).rectangle((0, 0, 127, 63), fill=0)
            with self.subTest(screen=name):
                self.assertIsNone(outside.getbbox())
                self.assertIsNotNone(canvas.getbbox())

    def test_status_rows_label_and_text_do_not_touch(self):
        label_right = max(ink_width(text, font(TITLE_FONT)) for text in STATE_TEXT.values())
        self.assertLessEqual(label_right + 3, render.STATUS_TEXT_X)  # aspoň 3 volné sloupce
        image = Image.new("1", (128, 64))
        problems = [self.problem(state) for state in (State.CRITICAL, State.ERROR, State.WARNING, State.UNKNOWN)]
        render.draw_status_page(image, problems, 0, 1, 1, True, True)
        for row in range(4):
            top = render.SERVICES_ROW_TOP + row * render.SERVICES_ROW_STEP
            with self.subTest(row=row):
                self.assertIsNone(image.crop((label_right, top, render.STATUS_TEXT_X, top + 13)).getbbox())
                self.assertIsNotNone(image.crop((0, top, label_right, top + 13)).getbbox())  # stav
                text_box = image.crop((render.STATUS_TEXT_X, top, 128, top + 13)).getbbox()
                self.assertLessEqual(render.STATUS_TEXT_X + text_box[2], RIGHT_EDGE + 1)  # popis zkrácen

    def test_header_title_sim_and_counter_are_separate(self):
        for counter in ("1/1", "2/2", "10/10"):
            image = Image.new("1", (128, 64))
            render.draw_header(image, "STATUS", True, right_text=counter)
            # V 8x13B má úzké "I" kolem sebe až 3 prázdné sloupce; mezi prvky záhlaví jsou aspoň 4.
            groups = ink_groups(image, 0, HEADER_LINE_Y, merge_gap=3)
            with self.subTest(counter=counter):
                self.assertEqual(len(groups), 3, groups)  # STATUS, SIM, čítač
                self.assertTrue(all(b[0] - a[1] > 6 for a, b in zip(groups, groups[1:])))  # aspoň 6 volných sloupců
        for title in ("SYSTEM", "DISK", "SERVICES"):
            image = Image.new("1", (128, 64))
            render.draw_header(image, title, True)
            with self.subTest(title=title):
                self.assertEqual(len(ink_groups(image, 0, HEADER_LINE_Y, merge_gap=3)), 2)  # nadpis a SIM

    def test_all_screens_are_one_bit(self):
        for name, draw_screen in self.screens().items():
            image = Image.new("1", (128, 64))
            draw_screen(image)
            with self.subTest(screen=name):
                self.assertEqual(image.mode, "1")
                self.assertLessEqual(len(image.getcolors()), 2)  # jen černá a bílá


# Všechny znaky, které může displej ukázat (oled_text: ASCII velkými písmeny, °, …).
OLED_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789%°/:.-?!…"


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class OledGlyphTest(unittest.TestCase):
    """Přiložená písma mají každý znak displeje a žádné dva nesplývají."""

    def glyph(self, char, name):
        image = Image.new("1", (20, 20))
        ImageDraw.Draw(image).text((2, 2), char, font=font(name), fill=1)
        return image.tobytes()

    def test_every_oled_char_is_drawn_and_distinct(self):
        for name in (TEXT_FONT, TITLE_FONT):
            missing = self.glyph("\ue000", name)  # znak, který písmo nemá (náhradní tvar)
            glyphs = {char: self.glyph(char, name) for char in OLED_CHARS}
            with self.subTest(font=name):
                self.assertEqual(len(set(glyphs.values())), len(OLED_CHARS))  # 0/O, 1/I, °/o se liší
                for char, glyph in glyphs.items():
                    self.assertNotEqual(glyph, missing, char)
                    self.assertNotEqual(glyph, self.glyph(" ", name), char)

    def test_page_titles_fit_header(self):
        for title in CAROUSEL_PAGES + ("STATUS",):
            left, top, right, bottom = ink_box(title, font(TITLE_FONT))
            with self.subTest(title=title):
                self.assertGreaterEqual(top, 0)
                self.assertLess(bottom, HEADER_LINE_Y)  # nad čarou záhlaví
                self.assertLess(right, 128 - 30)  # vpravo zbývá místo pro SIM a čítač


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class BundledFontTest(unittest.TestCase):
    def test_fonts_are_bundled_unchanged_and_public_domain(self):
        readme = (render.FONT_DIR / "README.md").read_text(encoding="utf-8")
        for name in (TEXT_FONT, TITLE_FONT):
            path = render.FONT_DIR / f"{name}.pcf.gz"
            with self.subTest(font=name):
                self.assertEqual(path.parent, Path(render.__file__).parent / "fonts")  # v balíčku, ne v systému
                self.assertIn(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}.pcf.gz", readme)
                self.assertIn(b"Public domain font.  Share and enjoy.", gzip.decompress(path.read_bytes()))

    def test_missing_font_is_error_not_silent_fallback(self):
        render.font.cache_clear()
        self.addCleanup(render.font.cache_clear)
        with tempfile.TemporaryDirectory() as empty, mock.patch.object(render, "FONT_DIR", Path(empty)):
            with self.assertRaises(OSError):
                render.font(TEXT_FONT)


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
