"""Vykreslení monochromatických obrazovek 128 × 64 (Pillow).

Jen kreslí podle stavu z OledController a výsledků kontrol; nic nemění.
Výstupem je obrázek v režimu "1" (1 bit na pixel), stejně jako u SSD1306.

Písmo je přiložené bitmapové X11 „Fixed“ (src/nexusstat/fonts, volné dílo):
každý znak je navržený pixel po pixelu, takže na 1 bitu zůstává ostrý a
nezávisí na písmech nainstalovaných v systému. Rozměry a zarovnání se počítají
ze skutečně vykreslených pixelů, ne z metrik písma, a souřadnice jsou celá čísla.

Texty (anglicky, podle id kontroly a číselné hodnoty) připravuje oled_text;
renderer jen vybírá, co se vejde, a kreslí.
"""

import gzip
import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import DISPLAY_HEIGHT, DISPLAY_WIDTH
from .core import active_problems, has_monitor_data
from .oled import ALERT_SYMBOL, UNKNOWN_SYMBOL, Mode, home_symbol, split_status_pages
from .oled_text import STATE_TEXT, network_rows, page_rows, service_rows, status_texts

FONT_DIR = Path(__file__).resolve().parent / "fonts"
# Bitmapová písma mají jedinou velikost: buňku vysokou 13 px.
FONT_PIXEL_SIZE = 13
# Jen dvě písma: tučné pro nápisy a stavy, obyčejné pro text. V užších tučných
# 6x13B a 7x13B splývá „M“ a „W“ s „H“ („SIM“ by se četlo jako „SIH“).
TITLE_FONT = "8x13B"  # nápis NEXUS, nadpisy stránek, hodiny (2×), hodnoty a stavy CRIT/WARN/OK
TEXT_FONT = "6x13"  # názvy kontrol, popisy ve STATUS, datum, značka SIM

# Pravý okraj textu zarovnaného vpravo: sloupec 127 zůstává volný.
RIGHT_EDGE = DISPLAY_WIDTH - 2

# Stránka NEXUS: horní okraj inkoustu nápisu, hodin a data. Nápis i datum mají
# inkoust 10 px, hodiny 20 px (písmo 8x13B zvětšené 2×): pásma 2–11, 21–40
# a 50–59 px, mezi nimi 9 px. Hodiny jsou největší prvek stránky.
NEXUS_TITLE_TOP = 2
NEXUS_CLOCK_TOP = 21
NEXUS_DATE_TOP = 50
CLOCK_SCALE = 2

# Záhlaví stránek SYSTEM, STORAGE, NETWORK, SERVICES a STATUS: nadpis nahoře, pod ním čára.
HEADER_LINE_Y = 13

# Stránky s hodnotami: do 3 řádků rozteč 15 px (inkoust 17–58 px), název
# vlevo, hodnota tučně vpravo jako stav na stránce SERVICES. Od čtyř řádků
# (SYSTEM s CPU FAN, STORAGE) se použije rozteč stránky SERVICES, jinak by
# poslední řádek nevyšel na 64 px.
VALUE_ROW_TOP = 16
VALUE_ROW_STEP = 15
VALUE_ROWS_COMPACT = 4
VALUE_MAX_WIDTH = 72  # 9 tučných znaků; nesmyslně dlouhé číslo se zkrátí, název zůstane
LABEL_GAP = 4  # nejméně volných sloupců mezi názvem a hodnotou

# SERVICES a STATUS: 4 řádky po 12 px pod čarou (inkoust 15–62 px). Písma 6x13
# i 8x13B mají inkoust nejvýš 0–12 px pod svým y, takže se řádky nepřekrývají.
SERVICES_ROW_TOP = 14
SERVICES_ROW_STEP = 12
SERVICES_ROWS_PER_PAGE = 4
STATUS_TEXT_X = 36  # popis za čtyřznakovým stavem (CRIT, WARN, ...) v 8x13B; vejde se 15 znaků 6x13

# NETWORK: 4 řádky jako SERVICES, název 6x13 vlevo, hodnota vpravo. Linka a
# RX/TX tučně jako hodnoty jinde; IPv4 obyčejným 6x13, protože 15 znaků
# adresy tučně (120 px) by se vedle názvu nevešlo.
NETWORK_ADDRESS_ROW = 3
ADDRESS_MAX_WIDTH = 96  # 16 znaků 6x13; nejdelší IPv4 má 15

# Oblast symbolu (trojúhelník nebo "?") v pravém horním rohu stránky NEXUS (x, y, velikost).
SYMBOL_BOX = (112, 1, 15)

# Značka simulovaných dat. Simulace se nesmí na displeji plést se skutečným měřením.
SIM_MARK = "SIM"


@lru_cache(maxsize=None)
def font(name):
    """Přiložené bitmapové písmo. Bez náhrady: chybějící soubor je chyba, ne jiný vzhled.

    Soubor .pcf.gz se jednou rozbalí do paměti. Z gzipu by FreeType při každém
    skoku rozbaloval od začátku (měření textu 4–11 ms místo 0,1 ms). Z paměti
    zároveň Pillow nemůže při chybějícím souboru potichu dohledat písmo
    stejného jména v systému.
    """
    data = gzip.decompress((FONT_DIR / f"{name}.pcf.gz").read_bytes())
    return ImageFont.truetype(io.BytesIO(data), FONT_PIXEL_SIZE)


@lru_cache(maxsize=4096)
def ink_box(text, text_font):
    """Hranice skutečně vykreslených pixelů vůči počátku kreslení.

    Vrací (left, top, right, bottom), pravý a dolní okraj bez (jako crop).
    Metriky písma (getbbox) zahrnují i prázdné řádky a sloupce buňky,
    proto se text vykreslí a změří podle pixelů.
    """
    left, top, right, bottom = text_font.getbbox(text)
    margin_x, margin_y = abs(left) + 2, abs(top) + 2
    canvas = Image.new("1", (right + 2 * margin_x, bottom + 2 * margin_y), 0)
    ImageDraw.Draw(canvas).text((margin_x, margin_y), text, font=text_font, fill=1)
    box = canvas.getbbox()
    if box is None:
        return (0, 0, 0, 0)
    return (box[0] - margin_x, box[1] - margin_y, box[2] - margin_x, box[3] - margin_y)


def ink_width(text, text_font):
    """Pravý okraj vykreslených pixelů od počátku textu."""
    return ink_box(text, text_font)[2]


def fit_text(text, text_font, max_width):
    """Zkrátí text s „…“, aby se jeho vykreslené pixely vešly do max_width pixelů od počátku."""
    if ink_width(text, text_font) <= max_width:
        return text
    while text and ink_width(text + "…", text_font) > max_width:
        text = text[:-1]
    return text.rstrip() + "…"  # „CIDLA…“, ne „CIDLA …“


def first_fitting(texts, text_font, max_width):
    """První z textů (od nejúplnějšího), který se vejde; jinak poslední zkrácený s „…“."""
    for text in texts:
        if ink_width(text, text_font) <= max_width:
            return text
    return fit_text(texts[-1], text_font, max_width)


def text_glyphs(text, text_font, scale=1):
    """Text jako 1bitový obrázek oříznutý na vykreslené pixely, zvětšený celočíselně (NEAREST)."""
    left, top, right, bottom = ink_box(text, text_font)
    canvas = Image.new("1", (right - left, bottom - top), 0)
    ImageDraw.Draw(canvas).text((-left, -top), text, font=text_font, fill=1)
    if scale > 1:
        canvas = canvas.resize((canvas.width * scale, canvas.height * scale), resample=Image.NEAREST)
    return canvas


def paste_centered(image, glyphs, top):
    """Vloží vykreslený text vodorovně na střed displeje, celočíselně."""
    image.paste(1, ((DISPLAY_WIDTH - glyphs.width) // 2, top), glyphs)


def draw_right(draw, text, text_font, y):
    """Text zarovnaný vpravo tak, aby jeho poslední pixel ležel ve sloupci RIGHT_EDGE."""
    x = RIGHT_EDGE + 1 - ink_width(text, text_font)
    draw.text((x, y), text, font=text_font, fill=1)
    return x + ink_box(text, text_font)[0]  # levý okraj inkoustu


def draw_warning_triangle(draw, x, y, size):
    """Obrysový trojúhelník s vykřičníkem, levý horní roh v (x, y)."""
    bottom = y + size - 1
    middle = x + size // 2
    draw.polygon([(middle, y), (x + size - 1, bottom), (x, bottom)], outline=1)
    draw.line([(middle, y + 5), (middle, bottom - 5)], fill=1)
    draw.point((middle, bottom - 3), fill=1)


def draw_unknown_symbol(image, x, y, size):
    """Otazník vystředěný ve stejné oblasti jako trojúhelník: chybí platná data."""
    glyphs = text_glyphs("?", font(TITLE_FONT))
    image.paste(1, (x + (size - glyphs.width) // 2, y + (size - glyphs.height) // 2), glyphs)


def draw_sim_mark(image, right, bottom):
    """Značka SIM: poslední pixel ve sloupci `right`, spodní v řádku `bottom`."""
    glyphs = text_glyphs(SIM_MARK, font(TEXT_FONT))
    image.paste(1, (right + 1 - glyphs.width, bottom + 1 - glyphs.height), glyphs)


def date_text(wall_time):
    # Datum bez názvu dne: vystačí si s ASCII a nezávisí na locale systému.
    return f"{wall_time.day}. {wall_time.month}. {wall_time.year}"


def draw_nexus_page(image, wall_time, symbol, simulated):
    # Vystředění podle vykreslených pixelů celé šířky displeje: symbol vpravo
    # nahoře ani značka SIM na něj nemají vliv.
    title = text_glyphs("NEXUS", font(TITLE_FONT))
    clock = text_glyphs(wall_time.strftime("%H:%M"), font(TITLE_FONT), CLOCK_SCALE)
    date = text_glyphs(date_text(wall_time), font(TEXT_FONT))
    paste_centered(image, title, NEXUS_TITLE_TOP)
    paste_centered(image, clock, NEXUS_CLOCK_TOP)
    paste_centered(image, date, NEXUS_DATE_TOP)
    if symbol == ALERT_SYMBOL:
        draw_warning_triangle(ImageDraw.Draw(image), *SYMBOL_BOX)
    elif symbol == UNKNOWN_SYMBOL:
        draw_unknown_symbol(image, *SYMBOL_BOX)
    if simulated:
        # Vpravo v řádku data; nejdelší datum (12 znaků) končí daleko před ní.
        draw_sim_mark(image, RIGHT_EDGE, NEXUS_DATE_TOP + date.height - 1)


def draw_header(image, title, simulated, right_text=None):
    """Nadpis stránky vlevo, volitelně text vpravo (čítač STATUS), značka SIM a čára."""
    draw = ImageDraw.Draw(image)
    draw.text((0, 0), title, font=font(TITLE_FONT), fill=1)
    sim_right = RIGHT_EDGE
    if right_text is not None:
        # 6 volných sloupců mezi SIM a čítačem, jinak by se četlo "SIM1/2".
        sim_right = draw_right(draw, right_text, font(TEXT_FONT), 0) - 7
    if simulated:
        # Spodní okraj SIM na úrovni spodního okraje písmen nadpisu.
        title_bottom = ink_box("H", font(TITLE_FONT))[3] - 1  # spodní řádek velkých písmen
        draw_sim_mark(image, sim_right, title_bottom)
    draw.line([(0, HEADER_LINE_Y), (DISPLAY_WIDTH - 1, HEADER_LINE_Y)], fill=1)
    return draw


def draw_value_page(image, title, rows, simulated):
    """Stránky SYSTEM a STORAGE: řádky (název, hodnota), hodnoty pod sebou zarovnané vpravo."""
    draw = draw_header(image, title, simulated)
    compact = len(rows) >= VALUE_ROWS_COMPACT
    top = SERVICES_ROW_TOP if compact else VALUE_ROW_TOP
    step = SERVICES_ROW_STEP if compact else VALUE_ROW_STEP
    for row, (label, value) in enumerate(rows):
        y = top + row * step
        value_left = draw_right(draw, fit_text(value, font(TITLE_FONT), VALUE_MAX_WIDTH), font(TITLE_FONT), y)
        draw.text((0, y), fit_text(label, font(TEXT_FONT), value_left - LABEL_GAP), font=font(TEXT_FONT), fill=1)


def draw_network_page(image, rows, simulated):
    """Stránka NETWORK: rozhraní a linka, RX, TX, IPv4; hodnoty pod sebou zarovnané vpravo."""
    draw = draw_header(image, "NETWORK", simulated)
    for index, (label, value) in enumerate(rows[:SERVICES_ROWS_PER_PAGE]):
        y = SERVICES_ROW_TOP + index * SERVICES_ROW_STEP
        if index == NETWORK_ADDRESS_ROW:
            value_font, max_width = font(TEXT_FONT), ADDRESS_MAX_WIDTH
        else:
            value_font, max_width = font(TITLE_FONT), VALUE_MAX_WIDTH
        value_left = draw_right(draw, fit_text(value, value_font, max_width), value_font, y)
        draw.text((0, y), fit_text(label, font(TEXT_FONT), value_left - LABEL_GAP), font=font(TEXT_FONT), fill=1)


def layout_service_rows(rows):
    """Pozice textů stránky SERVICES: [(label_xy, state_xy)], název vlevo, stav vpravo podle pixelů."""
    positions = []
    for index, (_, state) in enumerate(rows[:SERVICES_ROWS_PER_PAGE]):
        y = SERVICES_ROW_TOP + index * SERVICES_ROW_STEP
        positions.append(((0, y), (RIGHT_EDGE + 1 - ink_width(state, font(TITLE_FONT)), y)))
    return positions


def draw_services_page(image, rows, simulated):
    draw = draw_header(image, "SERVICES", simulated)
    for (label, state), (label_xy, state_xy) in zip(rows, layout_service_rows(rows)):
        draw.text(label_xy, label, font=font(TEXT_FONT), fill=1)
        draw.text(state_xy, state, font=font(TITLE_FONT), fill=1)  # stav je to podstatné, proto tučně


def draw_status_page(image, problems, now, page_number, page_total, has_data, simulated):
    """Problémy pod sebou: stav tučně vlevo, krátký anglický popis za ním.

    `now` rozliší prošlá data (STALE) od chybějících (NO DATA).
    """
    draw = draw_header(image, "STATUS", simulated, right_text=f"{page_number}/{page_total}")

    if not problems:
        # Bez sledovaných výsledků (žádné nebo jen N/A) nevíme nic,
        # takže nesmíme hlásit, že je vše v pořádku.
        # "NO ACTIVE ISSUES", ne "ALL OK": hodnoty se stavem INFO nejsou
        # vyhodnocené prahy, víme jen, že nic aktivně nehlásí problém.
        message = text_glyphs("NO ACTIVE ISSUES" if has_data else "NO MONITOR DATA", font(TEXT_FONT))
        content_top = HEADER_LINE_Y + 1
        paste_centered(image, message, content_top + (DISPLAY_HEIGHT - content_top - message.height) // 2)
        return

    for row, problem in enumerate(problems):
        y = SERVICES_ROW_TOP + row * SERVICES_ROW_STEP
        draw.text((0, y), STATE_TEXT[problem.state], font=font(TITLE_FONT), fill=1)
        # Nejúplnější text, který se vejde celý; výpustka až když se nevejde ani název.
        text = first_fitting(status_texts(problem, now), font(TEXT_FONT), RIGHT_EDGE + 1 - STATUS_TEXT_X)
        draw.text((STATUS_TEXT_X, y), text, font=font(TEXT_FONT), fill=1)


def render_screen(controller, now, results, wall_time, root_device=None):
    """Vykreslí, co má displej ukazovat v čase `now`.

    Před voláním má proběhnout controller.update(now). `wall_time` je místní
    čas pro hodiny a datum; `now` je monotónní čas automatu a platnosti kontrol.
    `root_device` je jen údaj ROOT DEV pro stránku STORAGE (už zjištěný
    výsledek root_device.identify_root_device(), nebo None). Kreslení nic
    neměří ani nečte ze systému.
    """
    image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 0)
    if controller.mode is Mode.SLEEP:
        return image  # zhasnutý displej

    simulated = any(result.simulated for result in results)
    if controller.mode is Mode.STATUS:
        pages = split_status_pages(active_problems(results, now))
        # Problémy mohly mezitím ubýt; nezobrazujeme neexistující stránku.
        index = min(controller.status_page, len(pages) - 1)
        draw_status_page(image, pages[index], now, index + 1, len(pages), has_monitor_data(results), simulated)
        return image

    page = controller.current_page(now)
    if page == "NEXUS":
        draw_nexus_page(image, wall_time, home_symbol(results, now), simulated)
    elif page == "NETWORK":
        draw_network_page(image, network_rows(results, now), simulated)
    elif page == "SERVICES":
        draw_services_page(image, service_rows(results, now), simulated)
    else:
        draw_value_page(image, page, page_rows(page, results, now, root_device), simulated)
    return image
