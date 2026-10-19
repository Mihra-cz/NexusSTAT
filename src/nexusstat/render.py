"""Vykreslení monochromatických obrazovek 128 × 64 (Pillow).

Jen kreslí podle stavu z OledController a výsledků kontrol; nic nemění.
Výstupem je obrázek v režimu "1" (1 bit na pixel), stejně jako u SSD1306.
"""

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .config import DISPLAY_HEIGHT, DISPLAY_WIDTH
from .core import active_problems, has_monitor_data
from .oled import (
    ALERT_SYMBOL,
    STATE_TEXT,
    UNKNOWN_SYMBOL,
    Mode,
    home_symbol,
    page_lines,
    service_rows,
    split_status_pages,
)

# Nadpisy stránek na displeji; ostatní stránky mají nadpis stejný jako interní název.
PAGE_TITLES = {"SERVICES": "SLUŽBY"}

# Rozvržení stránky SLUŽBY: 4 řádky písmem 11 px pod čarou na y = 15.
# Inkoust řádku leží zhruba 3–12 px pod jeho y, takže řádky 17–62 px
# nepřekrývají čáru ani sebe navzájem a vejdou se do 64 px.
SERVICES_ROW_TOP = 14
SERVICES_ROW_STEP = 12
SERVICES_ROWS_PER_PAGE = 4

# Oblast symbolu (trojúhelník nebo "?") v pravém horním rohu stránky NEXUS (x, y, velikost).
SYMBOL_BOX = (112, 1, 15)

# Značka simulovaných dat. Simulace se nesmí na displeji plést se skutečným měřením.
SIM_MARK = "SIM"


@lru_cache(maxsize=None)
def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        # Bez DejaVu použijeme vestavěné písmo Pillow: hrubší vzhled, ale funkční.
        return ImageFont.load_default()


def ink_width(draw, text, text_font):
    """Šířka skutečně vykreslených znaků od počátku textu (pravý okraj inkoustu).

    Šířka pro posun (textlength) bývá o zlomek pixelu menší, takže poslední
    sloupec písmene by na okraji displeje mohl chybět.
    """
    return draw.textbbox((0, 0), text, font=text_font)[2]


def fit_text(draw, text, text_font, max_width):
    """Zkrátí text s „…“, aby se jeho vykreslené znaky vešly do max_width pixelů."""
    if ink_width(draw, text, text_font) <= max_width:
        return text
    while text and ink_width(draw, text + "…", text_font) > max_width:
        text = text[:-1]
    return text + "…"


def draw_warning_triangle(draw, x, y, size):
    """Obrysový trojúhelník s vykřičníkem, levý horní roh v (x, y)."""
    bottom = y + size - 1
    middle = x + size // 2
    draw.polygon([(middle, y), (x + size - 1, bottom), (x, bottom)], outline=1)
    draw.line([(middle, y + 5), (middle, bottom - 5)], fill=1)
    draw.point((middle, bottom - 3), fill=1)


def draw_unknown_symbol(draw, x, y, size):
    """Otazník ve stejné oblasti jako trojúhelník: chybí platná data."""
    # Písmo o něco větší než oblast, protože samotný znak "?" je nižší než výška písma.
    symbol_font = font(size + 4, bold=True)
    left, top, right, bottom = draw.textbbox((0, 0), "?", font=symbol_font)
    text_x = x + (size - (right - left)) / 2 - left
    text_y = y + (size - (bottom - top)) / 2 - top
    draw.text((text_x, text_y), "?", font=symbol_font, fill=1)


def draw_sim_mark(draw, right, y):
    """Značka SIM zarovnaná vpravo na souřadnici `right`."""
    mark_font = font(9, bold=True)
    draw.text((right - draw.textlength(SIM_MARK, font=mark_font), y), SIM_MARK, font=mark_font, fill=1)


def draw_nexus_page(draw, wall_time, symbol, simulated):
    draw.text((0, 0), "NEXUS", font=font(20, bold=True), fill=1)
    if symbol == ALERT_SYMBOL:
        draw_warning_triangle(draw, *SYMBOL_BOX)
    elif symbol == UNKNOWN_SYMBOL:
        draw_unknown_symbol(draw, *SYMBOL_BOX)
    draw.text((0, 23), wall_time.strftime("%H:%M"), font=font(24, bold=True), fill=1)
    # Datum bez názvu dne: vystačí si s ASCII a nezávisí na locale systému.
    draw.text((0, 51), f"{wall_time.day}. {wall_time.month}. {wall_time.year}", font=font(11), fill=1)
    if simulated:
        draw_sim_mark(draw, DISPLAY_WIDTH - 1, 53)


def draw_value_page(draw, title, lines, simulated):
    draw.text((0, 0), title, font=font(12, bold=True), fill=1)
    if simulated:
        draw_sim_mark(draw, DISPLAY_WIDTH - 1, 2)
    draw.line([(0, 15), (DISPLAY_WIDTH - 1, 15)], fill=1)
    for row, line in enumerate(lines):
        draw.text((0, 19 + row * 15), fit_text(draw, line, font(11), DISPLAY_WIDTH), font=font(11), fill=1)


def layout_service_rows(draw, rows):
    """Pozice textů stránky SLUŽBY: [(label_xy, state_xy)], název vlevo, stav zarovnaný vpravo."""
    positions = []
    for index, (label, state) in enumerate(rows[:SERVICES_ROWS_PER_PAGE]):
        y = SERVICES_ROW_TOP + index * SERVICES_ROW_STEP
        # Zarovnání podle skutečného inkoustu, ne podle šířky pro posun.
        positions.append(((0, y), (DISPLAY_WIDTH - 1 - ink_width(draw, state, font(11)), y)))
    return positions


def draw_services_page(draw, rows, simulated):
    draw.text((0, 0), PAGE_TITLES["SERVICES"], font=font(12, bold=True), fill=1)
    if simulated:
        draw_sim_mark(draw, DISPLAY_WIDTH - 1, 2)
    draw.line([(0, 15), (DISPLAY_WIDTH - 1, 15)], fill=1)
    for (label, state), (label_xy, state_xy) in zip(rows, layout_service_rows(draw, rows)):
        draw.text(label_xy, label, font=font(11), fill=1)
        draw.text(state_xy, state, font=font(11), fill=1)


def draw_status_page(draw, problems, page_number, page_total, has_data, simulated):
    draw.text((0, 0), "STATUS", font=font(11, bold=True), fill=1)
    counter = f"{page_number}/{page_total}"
    counter_width = draw.textlength(counter, font=font(10))
    draw.text((DISPLAY_WIDTH - 1 - counter_width, 1), counter, font=font(10), fill=1)
    if simulated:
        draw_sim_mark(draw, DISPLAY_WIDTH - 1 - counter_width - 6, 2)
    draw.line([(0, 13), (DISPLAY_WIDTH - 1, 13)], fill=1)

    if not problems:
        # Bez sledovaných výsledků (žádné nebo jen N/A) nevíme nic,
        # takže nesmíme hlásit, že je vše v pořádku.
        # "NO ACTIVE ISSUES", ne "ALL OK": hodnoty se stavem INFO nejsou
        # vyhodnocené prahy, víme jen, že nic aktivně nehlásí problém.
        message = "NO ACTIVE ISSUES" if has_data else "NO MONITOR DATA"
        draw.text((0, 30), message, font=font(11, bold=True), fill=1)
        return

    for row, problem in enumerate(problems):
        y = 15 + row * 12
        draw.text((0, y), STATE_TEXT[problem.state], font=font(9, bold=True), fill=1)
        summary = fit_text(draw, problem.summary, font(10), DISPLAY_WIDTH - 33)
        draw.text((33, y), summary, font=font(10), fill=1)


def render_screen(controller, now, results, wall_time):
    """Vykreslí, co má displej ukazovat v čase `now`.

    Před voláním má proběhnout controller.update(now). `wall_time` je místní
    čas pro hodiny a datum; `now` je monotónní čas automatu a platnosti kontrol.
    """
    image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 0)
    if controller.mode is Mode.SLEEP:
        return image  # zhasnutý displej

    draw = ImageDraw.Draw(image)
    simulated = any(result.simulated for result in results)
    if controller.mode is Mode.STATUS:
        pages = split_status_pages(active_problems(results, now))
        # Problémy mohly mezitím ubýt; nezobrazujeme neexistující stránku.
        index = min(controller.status_page, len(pages) - 1)
        draw_status_page(
            draw, pages[index], index + 1, len(pages), has_monitor_data(results), simulated
        )
        return image

    page = controller.current_page(now)
    if page == "NEXUS":
        draw_nexus_page(draw, wall_time, home_symbol(results, now), simulated)
    elif page == "SERVICES":
        draw_services_page(draw, service_rows(results, now), simulated)
    else:
        draw_value_page(draw, page, page_lines(page, results, now), simulated)
    return image
