"""Vyhodnocení naměřených hodnot proti alarmovým prahům (zatím teplota CPU a disk /).

Evaluator dostává výsledky kolektoru a vrací je se stavem podle pravidel
z konfigurace [alarms]. Pracuje jen s číselnou hodnotou `measurement`, nikdy
s textem `summary`. Nic nevypíná, neřídí ventilátory a nespouští žádné akce:
jen určuje stav INFO / WARNING / ERROR.

Pravidla (pro každou veličinu zvlášť):
- Hodnota rovná prahu je nad prahem (>=).
- Vznik nebo zhoršení závady: `confirm_count` po sobě jdoucích měření
  alespoň na dané úrovni (výchozí 3). Hodnota nad ERROR se počítá i pro
  WARNING, takže při postupném růstu přijde WARNING dřív než ERROR.
- Odeznění nebo zmírnění: `clear_count` po sobě jdoucích měření pod
  prahem minus hystereze (výchozí 2).
- UNKNOWN závadu nevyřeší ani nepotvrdí: přeruší jen řadu měření.
- N/A a výsledky bez zapnutého pravidla se nemění.

Identita vzorku: (id, simulated, expires_monotonic). Platnost je monotónní čas
měření plus pevná platnost z [collector], takže každé měření jedné veličiny
má v běžícím procesu jinou a vyšší hodnotu (run_loop měří nejvýš jednou za
interval). Vzorek, který není novější než poslední vyhodnocený, se do řady
nezapočítá: opakované vyhodnocení (např. při obnově displeje) je idempotentní
a starší vzorek nic nezmění. Takový vzorek dostane aktuální potvrzený stav.
"""

from dataclasses import dataclass, field, replace

from .core import State
from .system_monitor import CELSIUS, CPU_TEMP_ID, DISK_ROOT_ID, LABELS, PERCENT

# Výsledek (podle id) -> (název pravidla v [alarms], jednotka prahů).
RULED_CHECKS = {
    CPU_TEMP_ID: ("cpu_temp", CELSIUS),
    DISK_ROOT_ID: ("disk_root", PERCENT),
}

# Úrovně od nejlepší po nejhorší; index určuje pořadí.
LEVELS = (State.INFO, State.WARNING, State.ERROR)


def rank(state):
    return LEVELS.index(state)


def measured_level(value, rule, confirmed):
    """Úroveň, kterou ukazuje jedna hodnota, vzhledem k potvrzenému stavu.

    Hystereze drží potvrzenou úroveň, dokud hodnota neklesne pod práh minus
    hysterezi. Hodnota těsně pod prahem tak závadu neukončí a kolísání kolem
    prahu nevyvolává opakované alarmy. Hodnota rovná `práh − hystereze` závadu
    ještě drží.
    """
    if value >= rule.error or (confirmed is State.ERROR and value >= rule.error_hold):
        return State.ERROR
    if value >= rule.warning or (confirmed is not State.INFO and value >= rule.warning_hold):
        return State.WARNING
    return State.INFO


@dataclass
class History:
    """Paměť jedné veličiny mezi měřeními."""

    confirmed: State = State.INFO
    recent: list = field(default_factory=list)  # úrovně posledních platných měření, nejnovější na konci
    # expires_monotonic posledního vyhodnoceného vzorku (i UNKNOWN): jeho identita
    # a zároveň okamžik, kdy by bez nového měření prošel.
    last_sample: float | None = None


class AlarmEvaluator:
    """Vyhodnocuje prahy a pamatuje si potvrzený stav každé veličiny.

    Změna pravidel = nový evaluator: rozpracované i potvrzené stavy začnou znovu.
    """

    def __init__(self, alarms_config):
        self.rules = {
            check_id: (getattr(alarms_config, name), unit)
            for check_id, (name, unit) in RULED_CHECKS.items()
        }
        self.history = {}

    def describe(self):
        """Krátký popis pravidel pro úvodní řádek výpisu."""
        parts = []
        for check_id, (rule, unit) in self.rules.items():
            if rule.enabled:
                parts.append(
                    f"{LABELS[check_id]} WARNING od {rule.warning:g} {unit}, ERROR od {rule.error:g} {unit}"
                )
            else:
                parts.append(f"{LABELS[check_id]} vypnuto")
        return "; ".join(parts)

    def evaluate(self, results, now):
        """Vrátí výsledky se stavem podle prahů.

        `now` je monotónní čas, kdy výsledky vznikly (v run_loop čas měření);
        slouží jen k poznání mezery v datech.
        """
        return [self._evaluate(result, now) for result in results]

    def _evaluate(self, result, now):
        rule, unit = self.rules.get(result.id, (None, None))
        if rule is None or not rule.enabled or result.state is State.NA:
            return result

        # Simulovaná a skutečná data mají oddělenou paměť, aby simulace nikdy
        # nepomohla potvrdit skutečný alarm (ani naopak).
        history = self.history.setdefault((result.id, result.simulated), History())

        measurement = result.measurement
        if result.state is State.UNKNOWN:
            unknown = result
        elif measurement is None or measurement.unit != unit:
            # Pravidlo je zapnuté, ale chybí hodnota ve správné jednotce. Text
            # summary se nečte, stav proto nesmí zůstat INFO.
            unknown = replace(
                result, state=State.UNKNOWN, summary=f"{LABELS[result.id]}: chybí hodnota v {unit}", measurement=None
            )
        else:
            unknown = None

        previous = history.last_sample
        if previous is not None and result.expires_monotonic <= previous:
            # Tento nebo novější vzorek už byl vyhodnocen: nic se nezapočítá znovu.
            return unknown if unknown is not None else replace(result, state=history.confirmed)
        history.last_sample = result.expires_monotonic

        if unknown is not None:
            # Bez platné hodnoty nevíme nic: řada po sobě jdoucích měření se
            # přeruší, potvrzená závada ale trvá (UNKNOWN ji nevyřeší).
            history.recent.clear()
            return unknown
        if previous is not None and now >= previous:
            # Předchozí vzorek mezitím prošel (mezera v datech): stejné jako UNKNOWN.
            history.recent.clear()

        level = measured_level(measurement.value, rule, history.confirmed)
        history.recent = (history.recent + [level])[-max(rule.confirm_count, rule.clear_count):]

        # Zhoršení: všech posledních confirm_count měření je aspoň na nové úrovni.
        if len(history.recent) >= rule.confirm_count:
            lowest = min(history.recent[-rule.confirm_count:], key=rank)
            if rank(lowest) > rank(history.confirmed):
                history.confirmed = lowest
        # Zlepšení: všech posledních clear_count měření je nejvýš na nové úrovni.
        if len(history.recent) >= rule.clear_count:
            highest = max(history.recent[-rule.clear_count:], key=rank)
            if rank(highest) < rank(history.confirmed):
                history.confirmed = highest

        # Popis i hodnota zůstávají z kolektoru, mění se jen stav.
        return replace(result, state=history.confirmed)
