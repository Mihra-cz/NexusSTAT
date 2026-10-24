"""Katalog služeb pro stránku SERVICES.

Definice id výsledků služeb (služby Nexusu, AnimeDB, NAS, zálohy) a krátké
názvy pro displej. Sám nesbírá žádná data. Jediný skutečný kolektor služby je
zatím healthcheck AnimeDB přes HTTP /health (animedb_monitor.py, SERVICES-1), a to
jen po výslovném zapnutí v konfiguraci; NAS, Restic ani služby Nexusu se nečtou.
"""

from .core import CheckResult, State

NEXUS_SERVICES_ID = "service.nexus"  # souhrn služeb hostitele Nexus
ANIMEDB_ID = "service.animedb"
NAS_ID = "service.nas"  # dostupnost NAS; jiné kontroly NAS dostanou vlastní id
BACKUP_ID = "service.backup"  # stáří poslední zálohy (Restic)

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
}


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
