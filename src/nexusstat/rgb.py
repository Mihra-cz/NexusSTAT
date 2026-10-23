"""Rozhodovací logika čtyř RGB LED: co mají v daném okamžiku svítit.

Bez hardwaru a bez tkinter. Vstupem jsou existující výsledky CheckResult
(po AlarmEvaluatoru), potvrzení uživatele a stav OLED, výstupem jedna barva
s jasem pro všechny čtyři LED najednou. Nový alarmový systém tu nevzniká:
o závažnosti dál rozhodují kolektory, AlarmEvaluator a effective(). Pravidla
jsou v docs/REQUIREMENTS.md (RGB LED a potvrzování závad).

Čas je monotónní `now` jako v oled.py, takže testy na bliknutí nečekají.
Hardwarový ovladač ani trvalé ukládání potvrzení zatím neexistují.
"""

from dataclasses import dataclass

from .alarms import RULED_CHECKS
from .core import State, effective
from .services import ANIMEDB_ID, BACKUP_ID, NAS_ID, NEXUS_SERVICES_ID

RED = (255, 0, 0)
ORANGE = (255, 110, 0)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

# Časování vzorů v sekundách (rozhodnutí správce 2026-10-23).
CODE_ON = 0.4
CODE_OFF = 0.4
GROUP_PAUSE = 2.0
CRITICAL_ON = 0.25
CRITICAL_OFF = 0.25

BRIGHTNESS_MIN = 5
BRIGHTNESS_MAX = 100
BRIGHTNESS_DEFAULT = 20
WHITE_FACTOR = 0.5  # bílá při probuzeném OLED svítí asi na polovině nastaveného jasu

# Odeznění se dokládá nejméně třemi platnými měřeními bez závady. Pravidlo
# s delším potvrzováním chce delší řadu, jinak by po restartu potvrzení
# zaniklo dřív, než AlarmEvaluator trvající závadu znovu potvrdí.
MIN_CLEAN_SAMPLES = 3

CATEGORY_NAMES = {
    1: "Nexus a systém",
    2: "NAS",
    3: "AnimeDB",
    4: "zálohy",
    5: "Docker a ostatní služby",
}
# history.* jsou zapsané textem jako v oled_text, aby se kvůli kategorii
# nenačítala celá historie; shodu s history_link hlídá test.
CATEGORY_BY_ID = {
    NEXUS_SERVICES_ID: 1,
    "history.storage": 1,
    NAS_ID: 2,
    ANIMEDB_ID: 3,
    BACKUP_ID: 4,
    "history.snapshot": 4,
}
CATEGORY_BY_PREFIX = (("system.", 1), ("disk.", 1), ("network.", 1))
# Nezařazená skutečná závada se nesmí ztratit: dostane obecnou kategorii.
FALLBACK_CATEGORY = 5

# Závady, které lze potvrdit, a jejich pořadí. CRITICAL se umlčet nedá.
ACK_LEVELS = {State.WARNING: 1, State.ERROR: 2}

# Druhy signálu.
OFF = "off"
LIGHT = "white"
CODES = "codes"
REPLAY = "replay"
CRITICAL = "critical"
BLINKING = {CODES, REPLAY, CRITICAL}


def category_of(check_id):
    """Kód kategorie 1–5 (počet bliknutí) podle id kontroly; není to číslo LED."""
    if check_id in CATEGORY_BY_ID:
        return CATEGORY_BY_ID[check_id]
    for prefix, category in CATEGORY_BY_PREFIX:
        if check_id.startswith(prefix):
            return category
    return FALLBACK_CATEGORY


def check_brightness(percent):
    # bool je podtřída int a True by jinak prošlo jako 1 %.
    if isinstance(percent, bool) or not isinstance(percent, int) or not BRIGHTNESS_MIN <= percent <= BRIGHTNESS_MAX:
        raise ValueError(f"jas musí být celé číslo {BRIGHTNESS_MIN}–{BRIGHTNESS_MAX} %, ne {percent!r}")
    return percent


@dataclass
class Acknowledgement:
    state: State  # nejvyšší potvrzená závažnost (WARNING nebo ERROR)
    clean_samples: int = 0  # po sobě jdoucí platná měření bez závady (jen v paměti)
    last_sample: float | None = None  # expires_monotonic posledního započteného měření


class Acknowledgements:
    """Potvrzení závad uživatelem („beru na vědomí / pracuji na nápravě“).

    Potvrzení nikdy nemění CheckResult, jen vypne automatické blikání dané
    závady do potvrzené závažnosti včetně. Zhoršení nad ni potvrzení nepokrývá.
    Zanikne až po doloženém odeznění: `required()` po sobě jdoucích platných
    měření OK nebo INFO. UNKNOWN, N/A, prošlá data ani chybějící výsledek se
    jako odstranění problému nepočítají a řadu přeruší.
    V simulátoru žije jen v paměti; trvalé uložení je samostatný budoucí krok.
    """

    def __init__(self, alarms_config=None):
        self.alarms_config = alarms_config
        self.records = {}

    def required(self, check_id):
        """Počet platných měření bez závady, který doloží odeznění."""
        name = RULED_CHECKS.get(check_id, (None, None))[0]
        rule = getattr(self.alarms_config, name, None) if name else None
        if rule is None:
            return MIN_CLEAN_SAMPLES
        return max(MIN_CLEAN_SAMPLES, rule.confirm_count)

    def acknowledge(self, results, now):
        """Potvrdí všechny aktivní WARNING a ERROR; vrátí id nově potvrzených."""
        done = []
        for result in results:
            current = effective(result, now)
            if current.state not in ACK_LEVELS:
                continue
            record = self.records.get(result.id)
            if record is not None and ACK_LEVELS[record.state] >= ACK_LEVELS[current.state]:
                continue  # už potvrzené na této nebo vyšší úrovni
            self.records[result.id] = Acknowledgement(current.state)
            done.append(result.id)
        return done

    def clear(self):
        """Zruší všechna potvrzení (jen pro zkoušení v simulátoru)."""
        self.records.clear()

    def covers(self, result):
        """True, když je závada výsledku (už po effective) potvrzená."""
        record = self.records.get(result.id)
        return (record is not None and result.state in ACK_LEVELS
                and ACK_LEVELS[result.state] <= ACK_LEVELS[record.state])

    def update(self, results, now):
        """Započte nová měření a zruší potvrzení doloženě odezněných závad."""
        current = {result.id: effective(result, now) for result in results}
        for check_id, record in list(self.records.items()):
            result = current.get(check_id)
            if result is None or result.state not in (State.OK, State.INFO):
                # Závada trvá, nebo chybí platná data: odeznění není doložené.
                record.clean_samples, record.last_sample = 0, None
                continue
            # Totéž měření (např. při každém překreslení okna) se nepočítá dvakrát.
            if result.expires_monotonic == record.last_sample:
                continue
            record.clean_samples += 1
            record.last_sample = result.expires_monotonic
            if record.clean_samples >= self.required(check_id):
                del self.records[check_id]


@dataclass(frozen=True)
class Signal:
    """Co mají LED ukazovat; `steps` jsou (stav, kategorie) v pořadí přehrání."""

    kind: str
    steps: tuple = ()
    reason: str = ""  # proč LED nesvítí nebo svítí bíle (popis pro simulátor)


def _steps(faults, states):
    return tuple((state, category)
                 for state in states
                 for category in sorted({category_of(result.id) for result in faults if result.state is state}))


def rgb_signal(results, acks, oled_awake, replay, now):
    """Rozhodne signál LED v monotónním čase `now`.

    `results` jsou jen skutečně monitorované kontroly: výsledky, které dodal
    kolektor (i UNKNOWN po jeho selhání a prošlé výsledky). Služby, které
    complete_service_results() doplní jen proto, že pro ně kolektor ještě
    neexistuje, sem nepatří, takže bílou neblokují. `replay` = po probuzení
    OLED se mají jednou přehrát potvrzené závady.
    """
    current = [effective(result, now) for result in results]
    if any(result.state is State.CRITICAL for result in current):
        return Signal(CRITICAL)
    faults = [result for result in current if result.state in ACK_LEVELS]
    # Automaticky jen nepotvrzené závady a z nich jen nejvyšší závažnost.
    unacknowledged = [result for result in faults if not acks.covers(result)]
    for state in (State.ERROR, State.WARNING):
        steps = _steps(unacknowledged, (state,))
        if steps:
            return Signal(CODES, steps)
    if faults:
        if replay:
            return Signal(REPLAY, _steps(faults, (State.ERROR, State.WARNING)))
        # Potvrzená, ale trvající závada se nesmí ukázat bílou jako úplné OK.
        return Signal(OFF, reason="potvrzená závada trvá" if oled_awake else "OLED spí")
    if not oled_awake:
        return Signal(OFF, reason="OLED spí")
    states = {result.state for result in current}
    if State.UNKNOWN in states:
        # Neznámý stav nebliká, ale není to ani ověřené OK.
        return Signal(OFF, reason="chybí platná data (UNKNOWN)")
    if not states & {State.OK, State.INFO}:
        return Signal(OFF, reason="žádná sledovaná data")
    return Signal(LIGHT, reason="zdravý ověřený stav")


def pass_length(steps):
    """Délka jednoho průchodu kódů včetně pauzy po každé skupině."""
    return sum(category * (CODE_ON + CODE_OFF) + GROUP_PAUSE for _, category in steps)


def _code_phase(steps, position):
    """(stav, svítí, je pauza) v čase `position` od začátku průchodu."""
    for state, category in steps:
        group = category * (CODE_ON + CODE_OFF)
        if position < group:
            return state, position % (CODE_ON + CODE_OFF) < CODE_ON, False
        position -= group
        if position < GROUP_PAUSE:
            return state, False, True
        position -= GROUP_PAUSE
    return None, False, True  # po konci průchodu (jen REPLAY)


def _code_position(signal, elapsed):
    # Automatické kódy se opakují dokola, přehrání proběhne jen jednou.
    return elapsed % pass_length(signal.steps) if signal.kind == CODES else elapsed


@dataclass(frozen=True)
class LedFrame:
    """Stav čtyř společně řízených LED: základní barva a podíl jasu (0 = zhasnuto)."""

    color: tuple
    level: float

    @property
    def lit(self):
        return self.level > 0

    def rgb(self):
        """Barva po uplatnění jasu, lineárně, jak ji dostane budoucí ovladač."""
        return tuple(round(channel * self.level) for channel in self.color)


DARK = LedFrame(BLACK, 0.0)


def frame_at(signal, elapsed, brightness, steady=False):
    """Barva LED `elapsed` sekund od začátku signálu.

    `steady=True` ukáže barvu vzoru trvale, bez blikání (simulátor při
    zrychleném čase, kdy by blikání bylo rychlejší, než okno vykreslí).
    """
    level = check_brightness(brightness) / 100
    if signal.kind == LIGHT:
        return LedFrame(WHITE, level * WHITE_FACTOR)
    if signal.kind == CRITICAL:
        lit = steady or elapsed % (CRITICAL_ON + CRITICAL_OFF) < CRITICAL_ON
        return LedFrame(RED, level) if lit else DARK
    if signal.kind in (CODES, REPLAY):
        state, lit, _ = _code_phase(signal.steps, _code_position(signal, elapsed))
        if state is None or not (lit or steady):
            return DARK
        return LedFrame(RED if state is State.ERROR else ORANGE, level)
    return DARK


def at_boundary(signal, elapsed):
    """True, když lze signál vyměnit bez useknuté skupiny bliknutí."""
    if signal.kind not in (CODES, REPLAY):
        return True
    return _code_phase(signal.steps, _code_position(signal, elapsed))[2]


def describe(signal):
    """Krátký český popis signálu pro okno simulátoru."""
    if signal.kind == CRITICAL:
        return "CRITICAL: nepřetržité červené blikání 0,25 s / 0,25 s"
    if signal.kind in (CODES, REPLAY):
        groups = ", ".join(f"{state.value} {category}× ({CATEGORY_NAMES[category]})" for state, category in signal.steps)
        prefix = "jednorázové přehrání potvrzených závad" if signal.kind == REPLAY else "blikání"
        return f"{prefix}: {groups}"
    if signal.kind == LIGHT:
        return f"tlumená bílá: {signal.reason}"
    return f"zhasnuto: {signal.reason}"


class RgbLeds:
    """Stav LED v čase: potvrzení, přehrání po probuzení OLED a přehrávač vzorů.

    Změna signálu se u skupiny bliknutí projeví až v pauze po ní, aby
    přerušená skupina neukázala špatný počet; CRITICAL přebíjí okamžitě.
    """

    def __init__(self, alarms_config=None, brightness=BRIGHTNESS_DEFAULT):
        self.acks = Acknowledgements(alarms_config)
        self.brightness = check_brightness(brightness)
        self.replay = False
        self.signal = None  # právě přehrávaný signál
        self.started = None  # kdy začal (monotónní čas)

    def set_brightness(self, percent):
        self.brightness = check_brightness(percent)

    def wake(self, results, now):
        """Po probouzecím stisku: potvrzené aktivní závady se jednou přehrají.

        Jen když v tu chvíli nebliká nic důležitějšího (CRITICAL nebo
        nepotvrzená závada); ty mají přednost a přehrání se nekoná.
        """
        signal = rgb_signal(results, self.acks, True, True, now)
        self.replay = signal.kind == REPLAY
        if self.replay:
            # Přehrání začíná okamžikem stisku, ne až dalším překreslením.
            self._switch(signal, now)

    def wanted(self, results, oled_awake, now):
        self.acks.update(results, now)
        signal = rgb_signal(results, self.acks, oled_awake, self.replay, now)
        if signal.kind != REPLAY:
            # Přehrání přebilo něco důležitějšího, nebo už není co přehrávat.
            self.replay = False
        return signal

    def frame(self, results, oled_awake, now, steady=False):
        """Barva LED v čase `now` (volá se při každém překreslení)."""
        self._switch(self.wanted(results, oled_awake, now), now)
        if self.signal.kind == REPLAY and now - self.started >= pass_length(self.signal.steps):
            # Jeden průchod je hotový; potvrzená závada trvá, takže LED zhasnou.
            self.replay = False
            self._switch(self.wanted(results, oled_awake, now), now)
        return frame_at(self.signal, now - self.started, self.brightness, steady)

    def _switch(self, signal, now):
        if signal == self.signal:
            return
        if self.signal is None or signal.kind == CRITICAL or at_boundary(self.signal, now - self.started):
            self.signal, self.started = signal, now
