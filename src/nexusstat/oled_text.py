"""Krátké anglické texty pro OLED (prezentační vrstva).

Texty se skládají jen ze stabilního id kontroly, stavu a číselné hodnoty
`measurement`. Text `summary` (česky, pro terminál) se tu nečte, takže jeho
jazyk ani formát displej neovlivní. Funkce výsledky jen čtou: stavy, prahy,
hodnoty ani id kontrol se tu nemění.

Na displeji je jen ASCII velkými písmeny a symboly °C, %, / a „…“ u zkrácení.
"""

import hashlib

from .core import ALERT_STATES, State, effective
from .services import BACKUP_ID, NAS_ID, SERVICE_LABELS
from .system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID

# Krátký text stavu pro STATUS, stránku SERVICES a hodnotu bez čísla.
STATE_TEXT = {
    State.OK: "OK",
    State.INFO: "INFO",
    State.WARNING: "WARN",
    State.ERROR: "ERR",
    State.CRITICAL: "CRIT",
    State.UNKNOWN: "UNKN",
    State.NA: "N/A",
}

# Krátké názvy kontrol podle id. Služby mají názvy z katalogu služeb, aby se
# stránka SERVICES a STATUS nelišily. Každý název je jiný, aby dva řádky
# STATUS nevypadaly stejně.
CHECK_NAMES = {
    CPU_USAGE_ID: "CPU",
    CPU_TEMP_ID: "CPU TEMP",
    MEMORY_ID: "RAM",
    DISK_ROOT_ID: "ROOT",
    **SERVICE_LABELS,
    # Plánované kontroly, zatím jen ve fiktivních datech simulátoru.
    "system.time_sync": "TIME SYNC",
    "system.load": "LOAD",
    "disk.root_readonly": "ROOT MODE",  # režim souborového systému /; závada = ROOT READ-ONLY
    "disk.nvme_temp": "NVME TEMP",
    "disk.smart": "SMART",
    "docker.engine": "DOCKER",
    "public.status": "PUBLIC",
}

# Hláška potvrzené závady (WARNING/ERROR/CRITICAL) bez číselné hodnoty. Jen pro
# kontroly, jejichž id odpovídá na jedinou otázku: NAS = dostupnost, BACKUP =
# stáří poslední zálohy. Jiný druh závady (např. zaplnění NAS) dostane vlastní
# id, ne jiný text. Ostatní závady bez hodnoty ukážou jen název; závažnost
# nese sloupec stavu.
FAULT_TEXT = {
    NAS_ID: "NAS OFFLINE",
    BACKUP_ID: "BACKUP OVERDUE",
    "system.time_sync": "TIME UNSYNCED",
    "disk.root_readonly": "ROOT READ-ONLY",
}

# Proč je stav UNKNOWN, rozlišené jen podle času, ne podle textu summary.
NO_DATA = "NO DATA"  # platná hodnota chybí: nezměřeno, selhání, bez kolektoru
STALE = "STALE"  # výsledek prošel a nový nepřišel (BACKUP OVERDUE je jiná věc: stará záloha)

# Které výsledky (podle id) ukazují stránky s hodnotami, řádek po řádku.
PAGE_CHECKS = {
    "SYSTEM": (CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID),
    "DISK": (DISK_ROOT_ID,),
}


def check_code(check_id):
    """Dohledatelný kód kontroly: první 4 znaky SHA-256 jejího id velkými písmeny.

    Na rozdíl od hash() je stejný v každém běhu i na každém stroji, takže kód
    z displeje jde zpětně dohledat výpočtem pro známá id.
    """
    return hashlib.sha256(check_id.encode("utf-8")).hexdigest()[:4].upper()


def check_name(check_id):
    """Krátký anglický název kontroly. Neznámé id: CHECK a jeho kód (např. CHECK 1A2B)."""
    if check_id in CHECK_NAMES:
        return CHECK_NAMES[check_id]
    # Samotné id se na displej nepřepisuje: může být česky, s diakritikou nebo dlouhé.
    return f"CHECK {check_code(check_id)}"


def measurement_text(measurement):
    """Hodnota na celé jednotky stejně jako v summary (48°C, 24%). Jen ASCII a °."""
    unit = "".join(char for char in measurement.unit if char == "°" or "!" <= char <= "~")
    return f"{measurement.value:.0f}{unit}"


def value_text(result, now):
    """Hodnota pro stránky SYSTEM a DISK: číslo s jednotkou, jinak text stavu.

    Chybějící, prošlý nebo neměřitelný výsledek je UNKN, nikdy staré číslo.
    Ne N/A: to znamená záměrně vypnutou integraci, ne neznámou hodnotu.
    """
    if result is None:
        return STATE_TEXT[State.UNKNOWN]
    current = effective(result, now)
    if current.measurement is not None:
        return measurement_text(current.measurement)
    return STATE_TEXT[current.state]


def page_rows(page, results, now):
    """Řádky stránek SYSTEM a DISK: (název, hodnota) v pevném pořadí podle id."""
    by_id = {result.id: result for result in results}
    return [(check_name(check_id), value_text(by_id.get(check_id), now)) for check_id in PAGE_CHECKS.get(page, ())]


def service_rows(results, now):
    """Řádky stránky SERVICES: (název, text stavu) v pořadí katalogu služeb.

    Výsledky mají projít services.complete_service_results(), aby sledovaná
    služba bez dat byla UNKNOWN stejně v SERVICES, v symbolu NEXUS i ve STATUS.
    Prošlý výsledek je UNKN; N/A (nenasazená AnimeDB) zůstává N/A. Kdyby
    doplnění chybělo, chybějící služba se tu i tak ukáže jako UNKN, nikdy OK.
    """
    current = {result.id: effective(result, now) for result in results}
    rows = []
    for service_id, label in SERVICE_LABELS.items():
        result = current.get(service_id)
        state = result.state if result is not None else State.UNKNOWN
        rows.append((label, STATE_TEXT[state]))
    return rows


def status_texts(result, now):
    """Popisy problému pro STATUS od nejúplnějšího po nejkratší.

    Renderer vybere první, který se vejde. Poslední je vždy samotný název:
    když se nevejde nic delšího, zůstane aspoň, čeho se problém týká;
    závažnost je v samostatném sloupci vlevo.
    """
    current = effective(result, now)
    name = check_name(current.id)
    if current.measurement is not None:
        detail = measurement_text(current.measurement)  # potvrzená závada ukáže naměřenou hodnotu
    elif current.state is State.UNKNOWN:
        # effective() u prošlého výsledku nemění expires_monotonic, takže se
        # prošlá data poznají z času, ne z textu.
        detail = STALE if now >= current.expires_monotonic else NO_DATA
    elif current.state in ALERT_STATES and current.id in FAULT_TEXT:
        return [FAULT_TEXT[current.id], name]
    elif current.state is State.NA:
        detail = STATE_TEXT[State.NA]
    else:
        detail = ""
    return [f"{name} {detail}", name] if detail else [name]
