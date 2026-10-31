"""Stavový automat OLED: SLEEP → CAROUSEL ⇄ STATUS.

Neobsahuje kreslení, texty displeje (ty jsou v oled_text) ani přístup
k hardwaru. Čas dostává jako parametr `now` (v provozu `time.monotonic()`),
takže testy mohou čas libovolně posouvat a nemusí skutečně čekat.
"""

from enum import Enum

from .core import ALERT_STATES, State, overall_state


class Mode(Enum):
    SLEEP = "SLEEP"
    CAROUSEL = "CAROUSEL"
    STATUS = "STATUS"


# Stránky kolotoče. První musí být NEXUS, protože na ni vede každý návrat.
# Názvy jsou zároveň nadpisy na displeji; texty stránek sestavuje oled_text.
# STORAGE nahradila od M5-S4 stránku DISK.
CAROUSEL_PAGES = ("NEXUS", "SYSTEM", "STORAGE", "NETWORK", "SERVICES")

# Počet řádků s problémy na jedné stránce STATUS (128 × 64 px).
STATUS_LINES_PER_PAGE = 4

# Symboly v pravém horním rohu stránky NEXUS.
ALERT_SYMBOL = "!"  # trojúhelník s vykřičníkem: potvrzená závada
UNKNOWN_SYMBOL = "?"  # chybí platná data, závadu nelze potvrdit ani vyloučit


def home_symbol(results, now):
    """Symbol pro stránku NEXUS: ALERT_SYMBOL, UNKNOWN_SYMBOL nebo None (vše platně OK)."""
    # Rozhoduje nejzávažnější stav, takže trojúhelník má přednost před "?".
    overall = overall_state(results, now)
    if overall in ALERT_STATES:
        return ALERT_SYMBOL
    if overall is State.UNKNOWN:
        return UNKNOWN_SYMBOL
    return None


def split_status_pages(problems):
    """Rozdělí problémy po stránkách. Vždy vrátí aspoň jednu (případně prázdnou) stránku."""
    pages = [
        problems[start:start + STATUS_LINES_PER_PAGE]
        for start in range(0, len(problems), STATUS_LINES_PER_PAGE)
    ]
    return pages or [[]]


def status_page_count(problem_count):
    # Stránka "bez závad" je také stránka, proto minimum 1.
    return max(1, -(-problem_count // STATUS_LINES_PER_PAGE))


class OledController:
    def __init__(self, config, pages=CAROUSEL_PAGES):
        if not pages:
            raise ValueError("kolotoč musí mít alespoň jednu stránku")
        self.config = config
        self.pages = tuple(pages)
        self.mode = Mode.SLEEP
        self.carousel_started_at = None  # začátek aktuálního okna kolotoče
        self.status_page = 0
        self.status_pressed_at = None  # poslední stisk ve STATUS, od něj běží nečinnost

    def _enter_carousel(self, at):
        # Každý návrat začíná na stránce NEXUS a s novým celým oknem.
        # Číslo stránky se počítá z carousel_started_at, takže se tím vynuluje i ono.
        self.mode = Mode.CAROUSEL
        self.carousel_started_at = at
        self.status_page = 0
        self.status_pressed_at = None

    def _enter_sleep(self):
        self.mode = Mode.SLEEP
        self.carousel_started_at = None
        self.status_page = 0
        self.status_pressed_at = None

    def update(self, now):
        """Uplatní časové limity. Volá se pravidelně ve smyčce a před vykreslením."""
        # Pořadí kontrol je záměrné: návrat ze STATUS otevře nové okno kolotoče
        # od okamžiku vypršení. Při velkém skoku času (simulace, zaseknutá
        # smyčka) mohlo i to nové okno už uplynout, a pak má displej usnout.
        if self.mode is Mode.STATUS:
            deadline = self.status_pressed_at + self.config.status_idle_seconds
            if now >= deadline:
                self._enter_carousel(deadline)
        if self.mode is Mode.CAROUSEL:
            if now >= self.carousel_started_at + self.config.carousel_seconds:
                self._enter_sleep()

    def press(self, now, problem_count):
        """Krátký stisk tlačítka. `problem_count` = počet aktivních problémů v čase `now`."""
        # Nejdřív doženeme časové limity: stisk po uplynutí okna kolotoče
        # má displej probudit, ne otevřít STATUS ze stavu, který už neplatí.
        self.update(now)
        if self.mode is Mode.SLEEP:
            self._enter_carousel(now)
        elif self.mode is Mode.CAROUSEL:
            self.mode = Mode.STATUS
            self.status_page = 0
            self.status_pressed_at = now
        elif self.status_page + 1 < status_page_count(problem_count):
            self.status_page += 1
            self.status_pressed_at = now
        else:
            # Poslední (nebo jediná) stránka STATUS: necyklí se, vrací se do kolotoče.
            # Platí i tehdy, když mezitím problémy ubyly a stránek je méně.
            self._enter_carousel(now)

    def current_page(self, now):
        """Název aktuální stránky kolotoče, mimo CAROUSEL None. Volat po update(now)."""
        if self.mode is not Mode.CAROUSEL:
            return None
        # Stránka se odvozuje z času od začátku okna, takže automatické
        # přepínání okno kolotoče nijak neprodlužuje.
        elapsed = now - self.carousel_started_at
        index = int(elapsed // self.config.page_seconds) % len(self.pages)
        return self.pages[index]
