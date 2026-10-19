"""Katalog služeb pro stránku SLUŽBY (interně SERVICES).

Zatím jen definice: id výsledků, které v budoucnu dodají kolektory (služby
Nexusu, AnimeDB, NAS, zálohy), a krátké názvy pro displej. Žádný sběr dat:
nečte Docker, NAS, Restic ani AnimeDB.
"""

from .core import CheckResult, State

NEXUS_SERVICES_ID = "service.nexus"  # souhrn služeb hostitele Nexus
ANIMEDB_ID = "service.animedb"
NAS_ID = "service.nas"
BACKUP_ID = "service.backup"  # zálohy (Restic)

# Řádky stránky SLUŽBY v tomto pořadí; názvy se vejdou vlevo na 128 px.
SERVICE_LABELS = {
    NEXUS_SERVICES_ID: "NEXUS",
    ANIMEDB_ID: "ANIMEDB",
    NAS_ID: "NAS",
    BACKUP_ID: "BACKUP",
}

# Záměrně nenasazené integrace: N/A, tedy ani alarm, ani neznámý stav.
# Rozhodnutí o nasazení, ne měření; změní se jen výslovně.
NOT_DEPLOYED = {
    ANIMEDB_ID: "AnimeDB není nasazena",
}


def complete_service_results(results, observed_utc, expires_monotonic):
    """Doplní služby z katalogu, pro které nepřišel žádný výsledek.

    Sledovaná služba bez výsledku je UNKNOWN ("bez dat"), nenasazená N/A.
    Stránka SLUŽBY, symbol na stránce NEXUS i STATUS pak vycházejí ze stejných
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
        if service_id in NOT_DEPLOYED:
            state, summary = State.NA, NOT_DEPLOYED[service_id]
        else:
            state, summary = State.UNKNOWN, f"{label}: bez dat"
        added.append(CheckResult(service_id, state, summary, observed_utc, expires_monotonic, simulated=False))
    return list(results) + added
