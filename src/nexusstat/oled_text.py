"""Krátké anglické texty pro OLED (prezentační vrstva).

Texty se skládají jen ze stabilního id kontroly, stavu a číselné hodnoty
`measurement`. Text `summary` (česky, pro terminál) se tu nečte, takže jeho
jazyk ani formát displej neovlivní. Funkce výsledky jen čtou: stavy, prahy,
hodnoty ani id kontrol se tu nemění.

Na displeji je jen ASCII velkými písmeny a symboly °C, %, /, :, ., _, - a „…“
u zkrácení. Jedinou výjimkou s malým písmenem jsou jednotky rychlosti B/s,
KB/s a MB/s (výslovně zadané pro stránku NETWORK).
"""

import hashlib
import re

from .backup_monitor import BACKUP, FAILED, NO_JOB, NO_TIMER, RUNNING, SKIPPED
from .core import ALERT_STATES, State, effective
from .fan_monitor import CPU_FAN_ID, FAN_ON, FAN_STATE, SOURCE, SOURCE_REQUEST
from .mounts import FSTYPE, MODE, ROOT_MODE_ID
from .network_monitor import ADDRESS, BYTES_PER_SECOND, INTERFACE, IPV4_ID, LINK, LINK_ID, RX_RATE_ID, TX_RATE_ID
from .root_device import BUS
from .services import BACKUP_ID, DOWN, HEALTH, INVALID, NAS_ID, SERVICE_LABELS, UP
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
    CPU_FAN_ID: "CPU FAN",
    DISK_ROOT_ID: "ROOT",
    ROOT_MODE_ID: "ROOT MODE",  # režim souborového systému / (M5-S2); závada = ROOT READ-ONLY
    # Síť: nejvýš 6 znaků, aby se ve STATUS vešlo i "NET RX NO DATA" (15 znaků).
    LINK_ID: "LINK",
    RX_RATE_ID: "NET RX",
    TX_RATE_ID: "NET TX",
    IPV4_ID: "IPV4",
    **SERVICE_LABELS,
    # Stav ukládání historie (history_link.py, M3.3b-1 a M3.3b-2).
    "history.storage": "HISTORY",
    # Připravený název pro budoucí kontrolu snímku historie: snímek ani jeho
    # kontrola ještě neexistují (M3.4), displej proto o záloze nic netvrdí.
    "history.snapshot": "HIST SNAPSHOT",
    # Plánované kontroly, zatím jen ve fiktivních datech simulátoru.
    "system.time_sync": "TIME SYNC",
    "system.load": "LOAD",
    "disk.nvme_temp": "NVME TEMP",
    "disk.smart": "SMART",
    "docker.engine": "DOCKER",
    "public.status": "PUBLIC",
}

# Hláška potvrzené závady (WARNING/ERROR/CRITICAL) bez číselné hodnoty. Jen pro
# kontroly, jejichž id odpovídá na jedinou otázku: NAS = dostupnost (od
# SERVICES-2 služby SMB), BACKUP = stáří poslední zálohy. Jiný druh závady
# (např. zaplnění NAS nebo chybějící sdílená složka) dostane vlastní id, ne
# jiný text. Ostatní závady bez hodnoty ukážou jen název; závažnost nese
# sloupec stavu.
FAULT_TEXT = {
    NAS_ID: "NAS OFFLINE",
    BACKUP_ID: "BACKUP OVERDUE",
    "system.time_sync": "TIME UNSYNCED",
    ROOT_MODE_ID: "ROOT READ-ONLY",
}

# Důvod stavu kontroly dostupnosti (AnimeDB, NAS) pro STATUS podle (stav,
# details["health"]), nejvýš 7 znaků, aby se celý řádek (např. "ANIMEDB FAILING")
# vešel do 15 znaků. Potvrzený výpadek (ERROR) je DOWN, ještě nepotvrzený
# (UNKNOWN) FAILING; neplatná odpověď INVALID odliší neznámý výsledek kontroly
# od výpadku. Potvrzený výpadek kontroly s hláškou ve FAULT_TEXT (NAS) ukáže
# tu hlášku (NAS OFFLINE), ne DOWN.
HEALTH_TEXT = {
    (State.ERROR, DOWN): "DOWN",
    (State.UNKNOWN, DOWN): "FAILING",
    (State.UNKNOWN, INVALID): "INVALID",
    (State.ERROR, UP): "RECOVER",  # odpovídá, ale výpadek ještě neodezněl
}

# Důvod stavu kontroly záloh (SERVICES-3) pro STATUS podle details["backup"],
# nejvýš 8 znaků, aby se "BACKUP NO TIMER" vešlo do 15 znaků. Stará záloha nemá
# vlastní text: ukáže BACKUP OVERDUE z FAULT_TEXT.
BACKUP_TEXT = {
    FAILED: "FAILED",  # poslední pokus selhal
    NO_JOB: "NO JOB",  # nastavená služba neexistuje, je maskovaná nebo nejde načíst
    NO_TIMER: "NO TIMER",  # nastavený timer neexistuje, je maskovaný nebo není aktivní
    RUNNING: "RUNNING",  # úloha běží a předchozí úspěch není doložený (UNKNOWN)
    SKIPPED: "SKIPPED",  # ExecCondition úlohu přeskočila a předchozí úspěch není doložený (UNKNOWN)
}

# Proč je stav UNKNOWN, rozlišené jen podle času, ne podle textu summary.
NO_DATA = "NO DATA"  # platná hodnota chybí: nezměřeno, selhání, bez kolektoru
STALE = "STALE"  # výsledek prošel a nový nepřišel (BACKUP OVERDUE je jiná věc: stará záloha)

# Které výsledky (podle id) ukazují stránky s hodnotami, řádek po řádku.
PAGE_CHECKS = {
    "SYSTEM": (CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID, CPU_FAN_ID),
}

# Stránka STORAGE (M5-S4). Režim RW/RO z details["mode"] platného výsledku
# disk.root_readonly, nikdy ze summary. Typ souborového systému a sběrnice
# zařízení ROOT jako slovo velkými písmeny; jiné znaky (nebo nic) jsou UNKN.
STORAGE_PAGE = "STORAGE"
MODE_TEXT = {"rw": "RW", "ro": "RO"}
ROOT_DEVICE_LABEL = "ROOT DEV"
FS_LABEL = "FS"
DISPLAY_WORD = re.compile(r"[A-Z0-9][A-Z0-9._-]*")

# Předpona stavu, který je znám jen z požadavku řízení. Displej tím nikdy
# netvrdí, že se ventilátor otáčí (REQUIREMENTS.md 85).
REQUEST_PREFIX = "REQ"

# Stránka NETWORK. Řádek linky má místo názvu jméno rozhraní; když rozhraní
# neznáme (žádné nenalezeno, data chybí nebo prošla), zůstane obecné LINK.
LINK_LABEL = "LINK"
NO_ADDRESS = "NONE"  # rozhraní platně nemá IPv4 (např. linka DOWN); jiné než neznámá adresa (UNKN)

# Jednotky rychlosti přenosu od nejmenší, desítkově: 1 KB/s = 1000 B/s.
RATE_UNITS = ("B/s", "KB/s", "MB/s")


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


def rate_text(bytes_per_second):
    """Rychlost přenosu: nejvýš 3 platné číslice a jednotka B/s, KB/s nebo MB/s.

    Desetinné místo jen pod 10 KB/s nebo MB/s (1.2 MB/s), jinak celé číslo
    (85 KB/s), aby se řádek vešel. Jednotka se volí až po zaokrouhlení, takže
    nevznikne "1000 B/s" ani "1000 KB/s". Nad 999 MB/s zůstává MB/s.
    """
    value = bytes_per_second
    for unit in RATE_UNITS:
        text = f"{value:.1f}" if unit != RATE_UNITS[0] and value < 9.95 else f"{value:.0f}"
        if float(text) < 1000 or unit == RATE_UNITS[-1]:
            return f"{text} {unit}"
        value /= 1000


def measurement_text(measurement):
    """Hodnota na celé jednotky stejně jako v summary (48°C, 24%). Jen ASCII a °.

    Rychlost přenosu (B/s) se převede na vhodnou jednotku (rate_text).
    """
    if measurement.unit == BYTES_PER_SECOND:
        return rate_text(measurement.value)
    unit = "".join(char for char in measurement.unit if char == "°" or "!" <= char <= "~")
    return f"{measurement.value:.0f}{unit}"


def value_text(result, now):
    """Hodnota pro stránky SYSTEM, STORAGE a RX/TX na NETWORK: číslo s jednotkou, jinak text stavu.

    Chybějící, prošlý nebo neměřitelný výsledek je UNKN, nikdy staré číslo.
    Ne N/A: to znamená záměrně vypnutou integraci, ne neznámou hodnotu.
    """
    if result is None:
        return STATE_TEXT[State.UNKNOWN]
    current = effective(result, now)
    if current.measurement is not None:
        return measurement_text(current.measurement)
    return STATE_TEXT[current.state]


def fan_value(result, now):
    """Hodnota řádku CPU FAN: jednoduchý stav, u měřených otáček s číslem.

    `ON 3200` jsou doložené otáčky s doplňujícím číslem, `OFF` doložené stání,
    `REQ ON` a `REQ OFF` stav známý **jen z požadavku řízení** (žádné tvrzení
    o otáčení). `UNKN` je nečitelný, nejednoznačný nebo prošlý údaj, `N/A`
    znamená, že prokazatelný zdroj na stroji není.
    """
    if result is None:
        return STATE_TEXT[State.UNKNOWN]
    current = effective(result, now)
    state = current.details.get(FAN_STATE)
    if state is None:
        # Bez stavu v details (prošlý výsledek, chyba, chybějící zdroj) rozhoduje stav kontroly.
        return STATE_TEXT[current.state]
    if current.details.get(SOURCE) == SOURCE_REQUEST:
        return f"{REQUEST_PREFIX} {state}"
    if state == FAN_ON and current.measurement is not None:
        return f"{state} {current.measurement.value:.0f}"
    return state


def display_word(value):
    """Údaj z details jako slovo velkými písmeny (EXT4, NVME); chybějící nebo nevhodný je UNKN."""
    text = value.upper() if isinstance(value, str) else ""
    return text if DISPLAY_WORD.fullmatch(text) else STATE_TEXT[State.UNKNOWN]


def storage_rows(results, now, root_device=None):
    """Řádky stránky STORAGE: ROOT, ROOT MODE, ROOT DEV a FS jako (název, hodnota).

    ROOT je obsazenost z disk.root. ROOT MODE a FS se čtou jen z details
    platného výsledku disk.root_readonly, takže i ERROR s doloženým ro ukáže
    RO; chybějící, neznámý nebo prošlý výsledek je UNKN. `root_device` je
    (stav, text, details) z root_device.identify_root_device(), nebo None:
    jen doplňující údaj pro displej, který volající předává zvlášť od
    výsledků kontrol, proto jeho UNKNOWN nemění stav, STATUS, NEXUS ani RGB.
    Sběrnici ukáže jen INFO, jinak UNKN.
    """
    by_id = {result.id: result for result in results}
    mode_result = by_id.get(ROOT_MODE_ID)
    details = effective(mode_result, now).details if mode_result is not None else {}
    bus = root_device[2].get(BUS) if root_device is not None and root_device[0] is State.INFO else None
    return [
        (check_name(DISK_ROOT_ID), value_text(by_id.get(DISK_ROOT_ID), now)),
        (check_name(ROOT_MODE_ID), MODE_TEXT.get(details.get(MODE), STATE_TEXT[State.UNKNOWN])),
        (ROOT_DEVICE_LABEL, display_word(bus)),
        (FS_LABEL, display_word(details.get(FSTYPE))),
    ]


def page_rows(page, results, now, root_device=None):
    """Řádky stránek SYSTEM a STORAGE: (název, hodnota) v pevném pořadí.

    `root_device` použije jen STORAGE (storage_rows).
    """
    if page == STORAGE_PAGE:
        return storage_rows(results, now, root_device)
    by_id = {result.id: result for result in results}
    rows = []
    for check_id in PAGE_CHECKS.get(page, ()):
        # Řádek CPU FAN ukazuje stav z details (ON/OFF), ostatní číselnou hodnotu.
        value = fan_value(by_id.get(check_id), now) if check_id == CPU_FAN_ID else value_text(by_id.get(check_id), now)
        rows.append((check_name(check_id), value))
    return rows


def detail_text(result, key, missing):
    """Textový údaj z details (UP, adresa): UNKN bez platného výsledku, N/A u vypnuté
    integrace, `missing`, když platný výsledek údaj nemá."""
    if result is None or result.state is State.UNKNOWN:
        return STATE_TEXT[State.UNKNOWN]
    if result.state is State.NA:
        return STATE_TEXT[State.NA]
    return result.details.get(key, missing)


def network_rows(results, now):
    """Řádky stránky NETWORK: rozhraní a linka, RX, TX, IPv4 jako (název, hodnota).

    Chybějící, prošlý nebo neměřitelný údaj je UNKN, nikdy stará hodnota
    (effective() u prošlého výsledku smaže i název rozhraní a adresu).
    Ukazuje se jen samotná lokální IPv4: adresa webového rozhraní ne, dokud
    NexusSTAT žádný běžící web nemá.
    """
    current = {result.id: effective(result, now) for result in results}
    link = current.get(LINK_ID)
    interface = link.details.get(INTERFACE) if link is not None else None
    return [
        (interface.upper() if interface else LINK_LABEL, detail_text(link, LINK, STATE_TEXT[State.UNKNOWN])),
        ("RX", value_text(current.get(RX_RATE_ID), now)),
        ("TX", value_text(current.get(TX_RATE_ID), now)),
        ("IPV4", detail_text(current.get(IPV4_ID), ADDRESS, NO_ADDRESS)),
    ]


def service_rows(results, now):
    """Řádky stránky SERVICES: (název, text stavu) v pořadí katalogu služeb.

    Výsledky mají projít services.complete_service_results(), aby sledovaná
    služba bez dat byla UNKNOWN stejně v SERVICES, v symbolu NEXUS i ve STATUS.
    Prošlý výsledek je UNKN; N/A (vypnuté sledování AnimeDB) zůstává N/A. Kdyby
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
    # Jen výsledky kontrol dostupnosti AnimeDB a NAS mají details "health"
    # (SERVICES-1, SERVICES-2); prošlý výsledek je nemá (effective() je smaže),
    # takže zůstane STALE.
    health = current.details.get(HEALTH)
    # Výsledky kontroly záloh mají místo "health" details "backup" (SERVICES-3).
    reason = HEALTH_TEXT.get((current.state, health)) or BACKUP_TEXT.get(current.details.get(BACKUP))
    if current.measurement is not None:
        detail = measurement_text(current.measurement)  # potvrzená závada ukáže naměřenou hodnotu
    elif current.state is State.UNKNOWN:
        # effective() u prošlého výsledku nemění expires_monotonic, takže se
        # prošlá data poznají z času, ne z textu. Nepotvrzený výpadek (FAILING)
        # a neplatná odpověď (INVALID) nejsou totéž co chybějící data (NO DATA).
        detail = STALE if now >= current.expires_monotonic else (reason or NO_DATA)
    elif current.state in ALERT_STATES and current.id in FAULT_TEXT and health != UP and (
            reason is None or health == DOWN):
        # NAS OFFLINE i při potvrzeném výpadku z kontroly NAS; když už NAS
        # odpovídá a výpadek jen odeznívá, hláška by neplatila (NAS RECOVER níže).
        # BACKUP OVERDUE jen u staré zálohy, ne u jiného doloženého důvodu (FAILED).
        return [FAULT_TEXT[current.id], name]
    elif current.state in ALERT_STATES and reason is not None:
        detail = reason  # ANIMEDB DOWN, ANIMEDB RECOVER, NAS RECOVER, BACKUP FAILED
    elif current.state is State.NA:
        detail = STATE_TEXT[State.NA]
    else:
        detail = ""
    return [f"{name} {detail}", name] if detail else [name]
