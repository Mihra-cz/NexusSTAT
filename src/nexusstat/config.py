"""Načtení a validace konfiguračního TOML: sekce [oled], [collector], [network], [alarms] a [history]."""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
KNOWN_NETWORK_KEYS = {"interface"}
KNOWN_HISTORY_KEYS = {"enabled", "path", "snapshot"}
KNOWN_SNAPSHOT_KEYS = {"enabled", "work_dir", "published_dir", "daily_time", "timezone"}
KNOWN_RULE_KEYS = {"enabled", "warning", "error", "hysteresis", "confirm_count", "clear_count"}

# Názvy síťových rozhraní, se kterými NexusSTAT pracuje: nejvýš 15 znaků jako
# v jádře (IFNAMSIZ − 1), jen písmena, číslice, "_", "." a "-". Jádro povoluje
# víc znaků, ale takový název by nešel bezpečně ukázat na displeji ani poslat
# do ioctl. Rozhraní s jiným názvem automatický výběr přeskočí.
INTERFACE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,15}")


def is_valid_interface_name(name):
    return isinstance(name, str) and INTERFACE_NAME.fullmatch(name) is not None and name not in (".", "..")

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
KNOWN_SECTIONS = {"oled", "collector", "network", "alarms", "history"}

# Role databáze historie v tomto kroku: jen vývojová databáze na notebooku.
# Konfigurace roli nenastavuje, takže produkční databázi nelze zapnout omylem
# ani překlepem; `history` roli production v tomto kroku navíc sama odmítá.
HISTORY_ROLE = "development"

# Denní čas snímku: jediný jednoznačný zápis "HH:MM" ve 24h formátu, vždy dvě
# číslice (00:00–23:59). Sekundy se nenastavují (plán na sekundu nemá smysl)
# a pásmo se zadává výslovně, aby čas neurčovalo prostředí procesu.
DAILY_TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")

# Přípona souboru databáze historie. Stejné pravidlo má .gitignore (*.sqlite3),
# takže ani omylem zadaná cesta v repozitáři nepůjde zaverzovat.
HISTORY_SUFFIX = ".sqlite3"


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
class NetworkConfig:
    """Sledované síťové rozhraní pro stránku NETWORK.

    None (výchozí) = automatický výběr fyzického rozhraní podle výchozí trasy
    (network_monitor.choose_interface). Zadaný název se použije přesně tak,
    i kdyby šlo o virtuální rozhraní (např. most "br0"): je to výslovná volba.
    Existence rozhraní se nekontroluje tady, ale při každém měření, protože
    se může objevit nebo zmizet za běhu.
    """

    interface: str | None = None

    def __post_init__(self):
        if self.interface is not None and not is_valid_interface_name(self.interface):
            raise ConfigError(
                f"interface musí být název rozhraní (1–15 znaků A–Z, a–z, 0–9, _ . -), ne {self.interface!r}"
            )


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


def _check_directory_path(name, value):
    """Absolutní cesta k adresáři, který už existuje a vytvoří ho jen správce.

    Existence a bezpečnost úložiště se tu nekontrolují (mohou se změnit za
    běhu): to dělá `snapshot.prepare()` před prvním měřením. Tady jde jen
    o tvar cesty, aby se omylem nepoužila relativní nebo prázdná.
    """
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} musí být neprázdná cesta k adresáři, ne {value!r}")
    if "\x00" in value:
        raise ConfigError(f"{name} nesmí obsahovat nulový bajt")
    if not value.startswith("/"):
        raise ConfigError(f"{name} musí být absolutní cesta, ne {value!r}")


@dataclass(frozen=True)
class SnapshotConfig:
    """Volitelné denní snímky historie jako neměnné generace (M3.4b-1, HISTORY.md 9).

    Výchozí stav je vypnuto a žádná cesta nemá výchozí hodnotu: bez výslovně
    zadaných adresářů se nic nevytváří ani nezveřejňuje, takže se nelze omylem
    dostat na produkční cestu Nexusu. Zapnutí vyžaduje oba adresáře, denní
    hodinu a časové pásmo; adresáře musí už existovat (aplikace je nikdy
    nevytváří) a ověří se před prvním měřením.
    """

    enabled: bool = False
    work_dir: str | None = None  # rozpracované generace (mimo zálohu)
    published_dir: str | None = None  # zveřejněné generace
    daily_time: str | None = None  # "HH:MM" v zadaném pásmu, např. "02:00"
    timezone: str | None = None  # název z databáze pásem, např. "Europe/Prague"

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ConfigError(f"enabled musí být true nebo false, ne {self.enabled!r}")
        for name in ("work_dir", "published_dir"):
            value = getattr(self, name)
            if value is not None:
                _check_directory_path(name, value)
        if self.daily_time is not None:
            self.daily()  # neplatný čas je chyba konfigurace, ne až běhu
        if self.timezone is not None:
            self.zone()  # stejně tak neplatné pásmo
        if not self.enabled:
            return
        missing = [name for name in ("work_dir", "published_dir", "daily_time", "timezone")
                   if getattr(self, name) is None]
        if missing:
            raise ConfigError(f"enabled = true vyžaduje {', '.join(missing)}")
        # Rozpracované generace nesmí ležet tam, kde zveřejněné: jinak by záloha
        # viděla nehotovou práci. Skutečné cesty (symlinky, mounty) ověří
        # snapshot.prepare(); tady se pozná jen shodný zápis.
        if self.work_dir == self.published_dir:
            raise ConfigError("work_dir a published_dir musí být různé adresáře")

    def daily(self):
        """Denní čas jako (hodina, minuta); jiný zápis než "HH:MM" je chyba.

        Striktně dvě číslice, dvojtečka, dvě číslice: "2:00", "02:0", "24:00",
        "02:60", prázdný řetězec ani čas se sekundami neprojdou. Číslo (např.
        hodina 2) se také odmítne, aby byl zápis v konfiguraci jednoznačný.
        """
        if not isinstance(self.daily_time, str):
            raise ConfigError(f"daily_time musí být text \"HH:MM\", ne {self.daily_time!r}")
        match = DAILY_TIME.fullmatch(self.daily_time)
        if match is None:
            raise ConfigError(f"daily_time musí být čas \"HH:MM\" v rozsahu 00:00–23:59, ne {self.daily_time!r}")
        return int(match[1]), int(match[2])

    def zone(self):
        """Časové pásmo jako ZoneInfo; chybějící databáze pásem je chyba konfigurace."""
        if not isinstance(self.timezone, str) or not self.timezone:
            raise ConfigError(f"timezone musí být název časového pásma, ne {self.timezone!r}")
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ConfigError(f"timezone {self.timezone!r} nelze použít: {error}") from None


@dataclass(frozen=True)
class HistoryConfig:
    """Volitelné ukládání měření do SQLite historie (M3.3b-1, docs/HISTORY.md 5).

    Výchozí stav je vypnuto a `path` nemá žádnou výchozí hodnotu: bez výslovně
    zadané cesty se nikdy neotevírá ani nevytváří žádný soubor, takže se nelze
    omylem dostat na produkční cestu Nexusu. Zapnutá historie vyžaduje
    absolutní cestu k databázi, kterou už vytvořil správce (role
    `development`); aplikace databázi nikdy nevytváří, nepřepisuje ani
    nenahrazuje. Existence souboru, jeho identita (role a stroj) a souborový
    systém se ověřují až při otevření, protože se mohou měnit za běhu.
    """

    enabled: bool = False
    path: str | None = None
    snapshot: SnapshotConfig = field(default_factory=SnapshotConfig)

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ConfigError(f"enabled musí být true nebo false, ne {self.enabled!r}")
        if self.path is not None:
            self._check_path()
        if self.enabled and self.path is None:
            raise ConfigError("enabled = true vyžaduje path k existující databázi historie")
        if not isinstance(self.snapshot, SnapshotConfig):
            raise ConfigError("[history.snapshot] musí být SnapshotConfig")
        # Snímek vzniká ze spojení vlákna zapisovače: bez zapnuté historie není
        # co snímat, takže zapnuté snímky nad vypnutou historií jsou chyba, ne
        # tiše ignorovaná volba.
        if self.snapshot.enabled and not self.enabled:
            raise ConfigError("[history.snapshot] enabled = true vyžaduje zapnutou historii")

    def _check_path(self):
        if not isinstance(self.path, str) or not self.path:
            raise ConfigError(f"path musí být neprázdná cesta k souboru, ne {self.path!r}")
        if "\x00" in self.path:
            raise ConfigError("path nesmí obsahovat nulový bajt")
        # Absolutní cesta bez pomlčky "~": cesta se použije přesně tak, jak je
        # zadaná, aby nezávisela na pracovním adresáři ani na domovském adresáři
        # uživatele, pod kterým služba zrovna běží.
        if not self.path.startswith("/"):
            raise ConfigError(f"path musí být absolutní cesta, ne {self.path!r}")
        name = PurePosixPath(self.path).name
        if not name or self.path.endswith("/"):
            raise ConfigError(f"path musí být soubor databáze, ne adresář: {self.path!r}")
        if PurePosixPath(name).suffix != HISTORY_SUFFIX:
            raise ConfigError(f"path musí mít příponu {HISTORY_SUFFIX}, ne {name!r}")


@dataclass(frozen=True)
class Config:
    """Celá konfigurace. Chybějící sekce mají výchozí hodnoty."""

    oled: OledConfig = field(default_factory=OledConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    alarms: AlarmsConfig = field(default_factory=AlarmsConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)


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


def network_config_from_dict(section):
    """Vytvoří NetworkConfig ze slovníku sekce [network]. Bez klíče interface = automatický výběr."""
    _check_section("network", section, KNOWN_NETWORK_KEYS)
    return NetworkConfig(**section)


def history_config_from_dict(section):
    """Vytvoří HistoryConfig ze slovníku sekce [history] včetně podsekce [history.snapshot]."""
    _check_section("history", section, KNOWN_HISTORY_KEYS)
    snapshot_section = section.get("snapshot", {})
    _check_section("history.snapshot", snapshot_section, KNOWN_SNAPSHOT_KEYS)
    try:
        snapshot = SnapshotConfig(**snapshot_section)
    except ConfigError as error:
        raise ConfigError(f"[history.snapshot] {error}") from error
    keys = {name: value for name, value in section.items() if name != "snapshot"}
    return HistoryConfig(**keys, snapshot=snapshot)


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
        network=network_config_from_dict(data.get("network", {})),
        history=history_config_from_dict(data.get("history", {})),
    )
