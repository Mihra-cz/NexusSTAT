"""Načtení a validace konfiguračního TOML: sekce [oled] a [collector]."""

import tomllib
from dataclasses import dataclass, field

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

# Načítané sekce. Ostatní ([web], [alarms], ...) jsou chyba,
# dokud je kód skutečně nepoužívá, jinak by se tiše ignorovaly.
KNOWN_SECTIONS = {"oled", "collector"}


class ConfigError(ValueError):
    pass


def _check_seconds(name, value):
    # bool je v Pythonu podtřída int, takže TOML `true` by jinak prošlo jako 1 s.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} musí být celé číslo sekund, ne {value!r}")
    if not 1 <= value <= MAX_SECONDS:
        raise ConfigError(f"{name} musí být v rozsahu 1–{MAX_SECONDS} s, ne {value}")


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
class Config:
    """Celá konfigurace. Chybějící sekce mají výchozí hodnoty."""

    oled: OledConfig = field(default_factory=OledConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)


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
    )
