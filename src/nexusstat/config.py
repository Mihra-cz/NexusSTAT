"""Načtení a validace konfiguračního TOML: sekce [oled], [collector] a [alarms]."""

import tomllib
from dataclasses import dataclass, field

from .core import is_finite_number

DISPLAY_WIDTH = 128
DISPLAY_HEIGHT = 64

# Horní mez chrání před překlepem (např. 3000 místo 300). OLED nemá svítit
# hodiny a měření řidší než jednou za hodinu by diagnostice nic neřeklo.
MAX_SECONDS = 3600

# Výsledek měření platí trojnásobek intervalu. Běžné zpoždění ani jedno
# vynechané měření tak nezpůsobí UNKNOWN; když kolektor přestane běžet úplně,
# data nejpozději po třech intervalech zestárnou.
VALIDITY_INTERVALS = 3

# Jen klíče, které kód skutečně používá. Neznámý klíč je chyba, aby se překlep
# (např. "page_second") tiše nenahradil výchozí hodnotou a aby nefunkční
# volba (např. budoucí "home_title") nevypadala, že něco dělá.
KNOWN_OLED_KEYS = {
    "width",
    "height",
    "carousel_seconds",
    "page_seconds",
    "status_idle_seconds",
}
KNOWN_COLLECTOR_KEYS = {"interval_seconds"}
KNOWN_RULE_KEYS = {"enabled", "warning", "error", "hysteresis", "confirm_count", "clear_count"}

# Veličiny s alarmovým pravidlem a rozsah, ve kterém smějí ležet jejich prahy
# (dolní mez vyloučená, horní včetně). Teplota odpovídá kontrole věrohodnosti
# v system_monitor (0, 125] °C, obsazení disku je v procentech.
THRESHOLD_LIMITS = {
    "cpu_temp": (0, 125),
    "disk_root": (0, 100),
}

# Hranice odeznění (práh − hystereze) se zaokrouhlují na 6 desetinných míst,
# aby 70.1 − 5.1 dalo přesně 65.0 jako v konfiguraci, ne 65.00000000000001
# z binární aritmetiky. Jinak by hodnota přesně na hranici závadu ukončila.
BOUNDARY_DECIMALS = 6

# Horní mez počtu měření pro potvrzení a odeznění: chrání před překlepem.
# Při intervalu 10 s je 60 měření deset minut.
MAX_CONFIRM_COUNT = 60

# Načítané sekce. Ostatní ([web], [storage], ...) jsou chyba,
# dokud je kód skutečně nepoužívá, jinak by se tiše ignorovaly.
KNOWN_SECTIONS = {"oled", "collector", "alarms"}


class ConfigError(ValueError):
    pass


def _check_seconds(name, value):
    # bool je v Pythonu podtřída int, takže TOML `true` by jinak prošlo jako 1 s.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} musí být celé číslo sekund, ne {value!r}")
    if not 1 <= value <= MAX_SECONDS:
        raise ConfigError(f"{name} musí být v rozsahu 1–{MAX_SECONDS} s, ne {value}")


def _check_count(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} musí být celé číslo, ne {value!r}")
    if not 1 <= value <= MAX_CONFIRM_COUNT:
        raise ConfigError(f"{name} musí být v rozsahu 1–{MAX_CONFIRM_COUNT}, ne {value}")


def _check_number(name, value):
    # TOML umí i inf, nan a libovolně velká celá čísla; s nimi by porovnání s prahem nedávalo smysl.
    if not is_finite_number(value):
        raise ConfigError(f"{name} musí být konečné číslo, ne {value!r}")


def _check_section(name, section, known_keys):
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] musí být tabulka")
    unknown = set(section) - known_keys
    if unknown:
        raise ConfigError(f"neznámé klíče v [{name}]: {', '.join(sorted(unknown))}")


@dataclass(frozen=True)
class OledConfig:
    carousel_seconds: int = 300
    page_seconds: int = 10
    status_idle_seconds: int = 30

    def __post_init__(self):
        # Validace přímo v konstruktoru: neplatná konfigurace nemůže vzniknout
        # ani při ručním vytvoření v kódu nebo v testech.
        _check_seconds("carousel_seconds", self.carousel_seconds)
        _check_seconds("page_seconds", self.page_seconds)
        _check_seconds("status_idle_seconds", self.status_idle_seconds)
        if self.page_seconds > self.carousel_seconds:
            raise ConfigError("page_seconds nesmí být delší než carousel_seconds")


@dataclass(frozen=True)
class CollectorConfig:
    interval_seconds: int = 10

    def __post_init__(self):
        _check_seconds("interval_seconds", self.interval_seconds)

    @property
    def validity_seconds(self):
        """Platnost výsledků. Nenastavuje se zvlášť, aby nemohla být kratší než
        interval a hodnoty mezi dvěma měřeními neblikaly na UNKNOWN."""
        return VALIDITY_INTERVALS * self.interval_seconds


@dataclass(frozen=True)
class AlarmRule:
    """Prahy jedné veličiny, u které je vyšší hodnota horší (teplota, obsazení disku).

    Výchozí pravidlo je vypnuté a nemá žádné prahy: kód neobsahuje výchozí
    alarmové hodnoty, takže se nic nezapne bez výslovně zadaných prahů.
    Význam prahů a hystereze popisuje alarms.measured_level().
    """

    @property
    def warning_hold(self):
        """Nejnižší hodnota, která ještě drží potvrzený WARNING (i ERROR na úrovni WARNING)."""
        return round(self.warning - self.hysteresis, BOUNDARY_DECIMALS)

    @property
    def error_hold(self):
        """Nejnižší hodnota, která ještě drží potvrzený ERROR."""
        return round(self.error - self.hysteresis, BOUNDARY_DECIMALS)

    enabled: bool = False
    warning: float | None = None
    error: float | None = None
    hysteresis: float | None = None
    confirm_count: int = 3  # po sobě jdoucí měření nad prahem pro vznik závady
    clear_count: int = 2  # po sobě jdoucí měření pod prahem s hysterezí pro odeznění

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ConfigError(f"enabled musí být true nebo false, ne {self.enabled!r}")
        _check_count("confirm_count", self.confirm_count)
        _check_count("clear_count", self.clear_count)
        thresholds = {"warning": self.warning, "error": self.error, "hysteresis": self.hysteresis}
        given = [name for name, value in thresholds.items() if value is not None]
        if not given:
            if self.enabled:
                raise ConfigError("enabled = true vyžaduje warning, error a hysteresis")
            return
        # Neúplné prahy by se snadno přehlédly a po zapnutí by chyběla část pravidla.
        if len(given) != len(thresholds):
            raise ConfigError("warning, error a hysteresis se zadávají všechny tři najednou")
        for name, value in thresholds.items():
            _check_number(name, value)
        if not self.warning < self.error:
            raise ConfigError("error musí být vyšší než warning")
        # Hystereze menší než odstup prahů: ERROR se při poklesu vrací nad
        # práh WARNING, ne až pod něj. Porovnává se stejná zaokrouhlená
        # hranice, jakou používá evaluator.
        if not (self.hysteresis >= 0 and self.error_hold > self.warning):
            raise ConfigError("hysteresis musí být v rozsahu 0 ≤ hysteresis < error − warning")


@dataclass(frozen=True)
class AlarmsConfig:
    """Pravidla pro jednotlivé veličiny. Výchozí: vše vypnuto, platné měření zůstává INFO."""

    cpu_temp: AlarmRule = field(default_factory=AlarmRule)
    disk_root: AlarmRule = field(default_factory=AlarmRule)

    def __post_init__(self):
        for name, (low, high) in THRESHOLD_LIMITS.items():
            rule = getattr(self, name)
            if not isinstance(rule, AlarmRule):
                raise ConfigError(f"[alarms.{name}] musí být AlarmRule")
            if rule.warning is None:
                continue
            if not (low < rule.warning and rule.error <= high):
                raise ConfigError(f"[alarms.{name}] prahy musí ležet v rozsahu ({low}, {high}]")
            # Hodnota pod hranicí odeznění musí jít vůbec naměřit, jinak by závada nikdy neodezněla.
            if not rule.warning_hold > low:
                raise ConfigError(f"[alarms.{name}] warning − hysteresis musí být vyšší než {low}")


@dataclass(frozen=True)
class Config:
    """Celá konfigurace. Chybějící sekce mají výchozí hodnoty."""

    oled: OledConfig = field(default_factory=OledConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    alarms: AlarmsConfig = field(default_factory=AlarmsConfig)


def oled_config_from_dict(section):
    """Vytvoří OledConfig ze slovníku sekce [oled]. Chybějící klíče = výchozí."""
    _check_section("oled", section, KNOWN_OLED_KEYS)
    # Rozvržení obrazovek je navržené jen pro tento displej.
    if section.get("width", DISPLAY_WIDTH) != DISPLAY_WIDTH:
        raise ConfigError(f"podporovaná šířka displeje je pouze {DISPLAY_WIDTH}")
    if section.get("height", DISPLAY_HEIGHT) != DISPLAY_HEIGHT:
        raise ConfigError(f"podporovaná výška displeje je pouze {DISPLAY_HEIGHT}")

    timing_keys = ("carousel_seconds", "page_seconds", "status_idle_seconds")
    return OledConfig(**{key: section[key] for key in timing_keys if key in section})


def collector_config_from_dict(section):
    """Vytvoří CollectorConfig ze slovníku sekce [collector]. Chybějící klíče = výchozí."""
    _check_section("collector", section, KNOWN_COLLECTOR_KEYS)
    return CollectorConfig(**section)


def alarms_config_from_dict(section):
    """Vytvoří AlarmsConfig ze slovníku sekce [alarms]. Chybějící pravidla = vypnutá."""
    _check_section("alarms", section, set(THRESHOLD_LIMITS))
    rules = {}
    for name, rule_section in section.items():
        _check_section(f"alarms.{name}", rule_section, KNOWN_RULE_KEYS)
        try:
            rules[name] = AlarmRule(**rule_section)
        except ConfigError as error:
            raise ConfigError(f"[alarms.{name}] {error}") from error
    return AlarmsConfig(**rules)


def load_config(path):
    """Načte Config z TOML souboru. Validuje se celý soubor, i sekce, kterou volající nepotřebuje."""
    try:
        with open(path, "rb") as file:
            data = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"neplatný TOML v {path}: {error}") from error
    unknown = set(data) - KNOWN_SECTIONS
    if unknown:
        raise ConfigError(f"nepodporované sekce (zatím neimplementováno): {', '.join(sorted(unknown))}")
    return Config(
        oled=oled_config_from_dict(data.get("oled", {})),
        collector=collector_config_from_dict(data.get("collector", {})),
        alarms=alarms_config_from_dict(data.get("alarms", {})),
    )
