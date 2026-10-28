"""Katalog služeb pro stránku SERVICES a společné potvrzování výpadku služby.

Definice id výsledků služeb (služby Nexusu, AnimeDB, NAS, zálohy) a krátké
názvy pro displej. Sám nesbírá žádná data. Skutečné kolektory služeb jsou
zatím tři, všechny jen po výslovném zapnutí v konfiguraci: healthcheck AnimeDB
přes HTTP /health (animedb_monitor.py, SERVICES-1), dostupnost NAS spojením
TCP na port SMB (nas_monitor.py, SERVICES-2) a výsledek existující zálohovací
úlohy ze systemd (backup_monitor.py, SERVICES-3). Sdílené SMB složky, repozitář
Resticu ani služby Nexusu se nečtou.
"""

import errno

from .config import DEFAULT_CLEAR_COUNT, DEFAULT_CONFIRM_COUNT
from .core import CheckResult, State

NEXUS_SERVICES_ID = "service.nexus"  # souhrn služeb hostitele Nexus
ANIMEDB_ID = "service.animedb"
NAS_ID = "service.nas"  # dostupnost NAS; jiné kontroly NAS dostanou vlastní id
BACKUP_ID = "service.backup"  # výsledek a stáří poslední zálohovací úlohy (systemd, ne obsah Resticu)

# Řádky stránky SERVICES v tomto pořadí; krátké anglické názvy i pro STATUS.
SERVICE_LABELS = {
    NEXUS_SERVICES_ID: "NEXUS",
    ANIMEDB_ID: "ANIMEDB",
    NAS_ID: "NAS",
    BACKUP_ID: "BACKUP",
}

# Služby, které se sledují jen po výslovném zapnutí v konfiguraci. Bez zapnutého
# kolektoru jsou N/A, tedy ani alarm, ani neznámý stav. Zapnutý kolektor má
# vždy přednost: jeho výsledek zůstává beze změny a bez výsledku je služba
# UNKNOWN, nikdy N/A (complete_service_results, parametr `monitored`).
OPTIONAL_SERVICES = {
    ANIMEDB_ID: "AnimeDB: sledování vypnuto",
    NAS_ID: "NAS: sledování vypnuto",
    BACKUP_ID: "Záloha: sledování vypnuto",
}

# Výsledek jedné kontroly dostupnosti služby (AnimeDB, NAS); zároveň hodnota
# `health` v details. Jen tyto pevné texty, takže historie zapíše řádek jen
# při skutečné změně a displej (oled_text.HEALTH_TEXT) z nich odvodí důvod.
HEALTH = "health"
CAUSE = "cause"  # proč výpadek nebo neplatná odpověď: refused, timeout, unreachable, http_503, json, ...
UP = "ok"
DOWN = "down"
INVALID = "invalid"

# Chyby spojení, které dokládají nedostupnost adresy nebo sítě (ne chybu na této straně).
UNREACHABLE_ERRNOS = (errno.EHOSTUNREACH, errno.ENETUNREACH)


class OutageConfirmation:
    """Potvrzení výpadku služby opakovanými kontrolami, stejným principem jako alarmové prahy.

    Výpadek vznikne po DEFAULT_CONFIRM_COUNT po sobě jdoucích kontrolách
    s výpadkem a odezní po DEFAULT_CLEAR_COUNT po sobě jdoucích úspěšných
    kontrolách; neznámý výsledek (None) ani mezera v měření výpadek nepotvrdí
    ani nevyřeší, jen přeruší řadu. Paměť je jako alarms.History: po sobě
    jdoucí kontroly s výpadkem a úspěšné kontroly, potvrzený výpadek
    a platnost posledního výsledku (pro poznání mezery v měření).
    Používá ho healthcheck AnimeDB (SERVICES-1) i kontrola NAS (SERVICES-2),
    obě přes check().
    """

    def __init__(self, validity_seconds):
        self.validity_seconds = validity_seconds
        self.failures = 0
        self.successes = 0
        self.down = False
        self.last_expires = None

    def check(self, probe, now):
        """(stav, popis, details) jednoho pokusu `probe()` po potvrzení v monotónním čase `now`.

        `probe` vrací (výsledek UP/DOWN/None, popis, details). Neočekávaná
        výjimka z něj je také neznámý výsledek: přeruší řadu a projde dál
        (UNKNOWN pro kontrolu pak doplní run.Collectors). Jinak by se výpadky
        nebo úspěchy oddělené kontrolou bez výsledku sečetly, jako by šly po sobě.
        """
        try:
            outcome, text, details = probe()
        except Exception:
            self.confirm(None, now)
            raise
        state, note = self.confirm(outcome, now)
        return state, f"{text}{note}", details

    def confirm(self, outcome, now):
        """(stav, doplněk popisu) pro výsledek kontroly UP, DOWN nebo None v monotónním čase `now`."""
        if self.last_expires is not None and now >= self.last_expires:
            # Předchozí výsledek mezitím prošel (mezera v měření): řada začíná znovu.
            self.failures = self.successes = 0
        self.last_expires = now + self.validity_seconds
        if outcome == DOWN:
            self.failures, self.successes = self.failures + 1, 0
            if self.failures >= DEFAULT_CONFIRM_COUNT:
                self.down = True
            if self.down:
                return State.ERROR, ""
            # Nepotvrzený výpadek: neznámý stav bez blikání, ne OK ani ERROR.
            return State.UNKNOWN, f" (výpadek zatím nepotvrzen, {self.failures}/{DEFAULT_CONFIRM_COUNT})"
        if outcome == UP:
            self.successes, self.failures = self.successes + 1, 0
            if self.down and self.successes >= DEFAULT_CLEAR_COUNT:
                self.down = False
            if self.down:
                return State.ERROR, f" (výpadek odeznívá, {self.successes}/{DEFAULT_CLEAR_COUNT})"
            return State.OK, ""
        # Neplatná odpověď nebo nemožná kontrola: výpadek nepotvrdí ani nevyřeší.
        self.failures = self.successes = 0
        return State.UNKNOWN, ""


def complete_service_results(results, observed_utc, expires_monotonic, monitored=()):
    """Doplní služby z katalogu, pro které nepřišel žádný výsledek.

    Sledovaná služba bez výsledku je UNKNOWN ("bez dat"), volitelná služba
    bez zapnutého kolektoru N/A. `monitored` jsou id kontrol, které měří
    některý zapnutý kolektor (např. `labels` kolektorů z run.live_monitor):
    taková služba bez výsledku je UNKNOWN i tehdy, když je volitelná, protože
    sledování je zapnuté a stav neznáme.
    Stránka SERVICES, symbol na stránce NEXUS i STATUS pak vycházejí ze stejných
    výsledků, takže stav služby nemůže být na jedné obrazovce neznámý a na
    jiné chybět. Nevykonaná kontrola nikdy nevypadá jako OK; ani NEXUS
    nemá OK, dokud nebude mít jednoznačně definovaný healthcheck s kolektorem.
    Doplněné výsledky nejsou simulované: tvrdí jen to, co skutečně víme.
    Výsledky, které přišly (i simulované), zůstávají beze změny.
    """
    present = {result.id for result in results}
    added = []
    for service_id, label in SERVICE_LABELS.items():
        if service_id in present:
            continue
        if service_id in OPTIONAL_SERVICES and service_id not in monitored:
            state, summary = State.NA, OPTIONAL_SERVICES[service_id]
        else:
            state, summary = State.UNKNOWN, f"{label}: bez dat"
        added.append(CheckResult(service_id, state, summary, observed_utc, expires_monotonic, simulated=False))
    return list(results) + added
