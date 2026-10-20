"""Stavový model diagnostických výsledků a výběr aktivních problémů."""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType


class State(Enum):
    OK = "OK"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"
    NA = "N/A"  # záměrně nepoužitá integrace (např. AnimeDB před nasazením), není alarm


# Od nejzávažnějšího. N/A sem nepatří: vypnutá integrace nemá závažnost
# a nepočítá se ani jako sledovaná data.
SEVERITY_ORDER = (State.CRITICAL, State.ERROR, State.WARNING, State.UNKNOWN, State.INFO, State.OK)

# Problémy pro STATUS. OK, INFO a N/A problémem nejsou.
PROBLEM_STATES = {State.CRITICAL, State.ERROR, State.WARNING, State.UNKNOWN}

# Potvrzené závady (platná data). UNKNOWN sem nepatří: nevíme, zda závada trvá.
ALERT_STATES = {State.WARNING, State.ERROR, State.CRITICAL}


def is_finite_number(value):
    """True pro konečné int/float. Ne bool, NaN, nekonečno ani int mimo rozsah float.

    NaN se s ničím neporovná jako větší ani menší (prah ani expirace by nikdy
    nezabraly) a nekonečno by nevypršelo nikdy. Obří celé číslo by při
    převodu na float vyvolalo OverflowError místo srozumitelné chyby.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class Measurement:
    """Naměřená číselná hodnota s jednotkou (např. 55.4 a "°C").

    Alarmy se vyhodnocují výhradně z ní, nikdy parsováním textu `summary`,
    který slouží jen k zobrazení a jeho formát se může měnit.
    """

    value: float
    unit: str

    def __post_init__(self):
        if not is_finite_number(self.value):
            raise ValueError(f"hodnota měření musí být konečné číslo, ne {self.value!r}")
        if not isinstance(self.unit, str) or not self.unit:
            raise ValueError("jednotka měření musí být neprázdný text")


# Stavy bez platné hodnoty: měření selhalo, data jsou prošlá, nebo se nesleduje.
STATES_WITHOUT_VALUE = {State.UNKNOWN, State.NA}


@dataclass(frozen=True)
class CheckResult:
    """Výsledek jedné kontroly.

    Dvě časové základny, které se nikdy neporovnávají mezi sebou:
    - `observed_utc`: skutečný čas měření (UTC) pro budoucí historii a události,
    - `expires_monotonic`: konec platnosti v sekundách monotónních hodin, stejný
      zdroj jako `now` (time.monotonic()). Platnost tak nerozhodí skok systémového
      času, např. po synchronizaci NTP po startu.
    U stavu N/A se platnost nepoužívá (viz effective()).
    `simulated` odlišuje vymyšlená data simulace od skutečného měření.
    `measurement` je číselná hodnota pro vyhodnocení prahů; None = hodnota
    nedostupná. UNKNOWN a N/A ji mít nesmějí.
    `details` jsou krátké strojové údaje jako text ASCII (např. název síťového
    rozhraní nebo IPv4 adresa), které se do čísla převést nedají. Displej je
    čte místo `summary`; prahy je nečtou. Po vytvoření je nejde změnit.
    """

    id: str
    state: State
    summary: str
    observed_utc: datetime
    expires_monotonic: float
    simulated: bool = field(kw_only=True)
    measurement: Measurement | None = field(default=None, kw_only=True)
    details: Mapping[str, str] = field(default_factory=dict, kw_only=True)

    def __post_init__(self):
        # Řetězec "OK" místo State.OK by tiše obešel řazení i vypršení.
        if not isinstance(self.state, State):
            raise TypeError(f"state musí být State, ne {self.state!r}")
        if not self.id:
            raise ValueError("id kontroly nesmí být prázdné")
        # Kontroly typů brání záměně časových základen: datetime místo
        # monotónních sekund (nebo naopak) se odhalí hned, ne až při porovnání.
        if not isinstance(self.observed_utc, datetime) or self.observed_utc.utcoffset() != timedelta(0):
            raise ValueError("observed_utc musí být datetime s časovou zónou UTC")
        if isinstance(self.expires_monotonic, bool) or not isinstance(self.expires_monotonic, (int, float)):
            raise TypeError("expires_monotonic musí být číslo v sekundách monotónních hodin")
        # Výsledek s NaN nebo nekonečnou platností by nikdy nevypršel na UNKNOWN.
        if not is_finite_number(self.expires_monotonic):
            raise ValueError("expires_monotonic musí být konečné číslo")
        if not isinstance(self.simulated, bool):
            raise TypeError("simulated musí být True nebo False")
        if self.measurement is not None and not isinstance(self.measurement, Measurement):
            raise TypeError("measurement musí být Measurement nebo None")
        # Neznámý stav s číslem by mohl dál vypadat jako platně naměřená hodnota.
        if self.measurement is not None and self.state in STATES_WITHOUT_VALUE:
            raise ValueError(f"stav {self.state.value} nesmí mít naměřenou hodnotu")
        if not isinstance(self.details, Mapping):
            raise TypeError("details musí být slovník textů")
        for key, value in self.details.items():
            # Jen tisknutelné ASCII: údaj jde beze změny na displej i do výpisu
            # a řídicí znaky nebo diakritika by tam nadělaly nepořádek.
            if not (isinstance(key, str) and key and isinstance(value, str) and value
                    and value.isascii() and value.isprintable()):
                raise ValueError(f"neplatný údaj details: {key!r}={value!r}")
        # Kopie jen pro čtení: výsledek je neměnný i pro toho, kdo slovník předal.
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


def effective(result, now):
    """Vrátí výsledek platný v monotónním čase `now`.

    Vypršelý výsledek se mění na UNKNOWN pro jakýkoli původní stav: staré OK
    nesmí vypadat jako aktuální OK a staré ERROR nesmí vypadat jako potvrzená
    trvající závada. Mění se i popis, protože původní text (např. "SMART bez
    chyb") by vedle UNKNOWN dál tvrdil něco, co už neplatí. Ze stejného důvodu
    zmizí i číselná hodnota a údaje `details` (např. staré rozhraní a IP adresa).

    N/A nevyprší: je to rozhodnutí v konfiguraci, ne měření. Přechod z N/A na
    aktivní sledování musí přijít jako nový výsledek, ne jako timeout.
    """
    if result.state is State.NA:
        return result
    if now >= result.expires_monotonic:
        return replace(result, state=State.UNKNOWN, summary=f"{result.id}: zastaralá data", measurement=None,
                       details={})
    return result


def active_problems(results, now):
    """Aktivní problémy seřazené CRITICAL → ERROR → WARNING → UNKNOWN.

    Při stejné závažnosti řadí podle id, aby se pořadí na displeji mezi
    jednotlivými obnoveními nepřeskupovalo.
    """
    current = [effective(result, now) for result in results]
    problems = [result for result in current if result.state in PROBLEM_STATES]
    return sorted(problems, key=lambda result: (SEVERITY_ORDER.index(result.state), result.id))


def overall_state(results, now):
    """Nejzávažnější aktuální stav ze všech výsledků.

    Bez výsledků nebo jen s N/A vrací UNKNOWN: když se nic nesleduje,
    nesmí to vypadat jako OK.
    """
    states = {effective(result, now).state for result in results}
    for state in SEVERITY_ORDER:
        if state in states:
            return state
    return State.UNKNOWN


def has_monitor_data(results):
    """True, pokud existuje aspoň jeden sledovaný výsledek (cokoli kromě N/A)."""
    return any(result.state is not State.NA for result in results)
