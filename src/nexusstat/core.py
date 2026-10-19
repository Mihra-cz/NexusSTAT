"""Stavový model diagnostických výsledků a výběr aktivních problémů."""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum


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
    """

    id: str
    state: State
    summary: str
    observed_utc: datetime
    expires_monotonic: float
    simulated: bool = field(kw_only=True)

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
        if not isinstance(self.simulated, bool):
            raise TypeError("simulated musí být True nebo False")


def effective(result, now):
    """Vrátí výsledek platný v monotónním čase `now`.

    Vypršelý výsledek se mění na UNKNOWN pro jakýkoli původní stav: staré OK
    nesmí vypadat jako aktuální OK a staré ERROR nesmí vypadat jako potvrzená
    trvající závada. Mění se i popis, protože původní text (např. "SMART bez
    chyb") by vedle UNKNOWN dál tvrdil něco, co už neplatí.

    N/A nevyprší: je to rozhodnutí v konfiguraci, ne měření. Přechod z N/A na
    aktivní sledování musí přijít jako nový výsledek, ne jako timeout.
    """
    if result.state is State.NA:
        return result
    if now >= result.expires_monotonic:
        return replace(result, state=State.UNKNOWN, summary=f"{result.id}: zastaralá data")
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
