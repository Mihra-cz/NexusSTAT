"""Read-only kontrola dostupnosti NAS spojením TCP na port SMB (SERVICES-2).

Při každém měření jediný pokus o spojení TCP na výslovně nastavenou číselnou
adresu (`[services.nas] host`, port výchozí 445) s limitem `timeout_seconds`,
jen standardní knihovnou. Navázané spojení se hned zavře: nic se neposílá ani
nečte, takže nedojde k přihlášení SMB, procházení sdílených složek ani čtení
souborů. Síťové mounty NAS (CIFS, autofs) se neotevírají ani neprocházejí,
takže kontrola automatické připojení nespustí. Při vypnutém sledování
(výchozí stav) kolektor vůbec nevznikne (run.live_monitor) a nikam se
nepřipojuje.

Výsledkem je jediná kontrola `service.nas`:

* spojení navázáno → `OK`. Znamená jen „služba SMB na NAS přijímá spojení“,
  ne že jsou dostupné všechny sdílené složky, data čitelná, zálohy
  zapisovatelné nebo NAS hardwarově zdravý,
* výpadek (spojení odmítnuto, timeout, doložená nedostupnost sítě nebo adresy)
  → `ERROR` až po potvrzení opakovanými kontrolami; do té doby `UNKNOWN`, takže
  krátký restart NAS nebo sítě nevyvolá blikání,
* chyba na této straně (např. nedostatek deskriptorů) nebo jiná nejasná chyba
  spojení → `UNKNOWN`, nikdy prokázaný výpadek ani OK.

Potvrzování je společné s healthcheckem AnimeDB (services.OutageConfirmation):
výpadek po DEFAULT_CONFIRM_COUNT po sobě jdoucích kontrolách s výpadkem,
odeznění po DEFAULT_CLEAR_COUNT úspěšných; neznámý výsledek ani mezera
v měření výpadek nepotvrdí ani nevyřeší, jen přeruší řadu.

Od SCHED-1 se kontroluje jen jednou za `interval_seconds` (výchozí 30 min,
run.Scheduled); po prvním neúspěchu nejvýš dva rychlé pokusy s odstupem 30 s
a po prvním úspěchu po potvrzeném výpadku jeden.
Výsledek platí do další plánované kontroly plus rezervu (`validity_seconds`
nastaví run.live_monitor).
"""

import socket

from .config import NAS_INTERVAL_DEFAULT, check_health_timeout, check_nas_host, check_port, check_service_interval
from .core import CheckResult, is_finite_number
from .services import CAUSE, DOWN, HEALTH, NAS_ID, UNREACHABLE_ERRNOS, UP, OutageConfirmation

LABELS = {NAS_ID: "NAS"}


def connect_once(host, port, timeout, connect=socket.create_connection):
    """Jediný pokus o spojení TCP; navázaný socket se hned zavře, nic se nepošle ani nepřečte.

    Adresa je číselná (check_nas_host), takže se nic nepřekládá přes DNS, které
    by limit nerespektovalo; jediná adresa znamená jediný pokus nejvýš `timeout` s.
    """
    sock = connect((host, port), timeout=timeout)
    sock.close()


class NasMonitor:
    """Dostupnost NAS (služby SMB) s potvrzováním výpadku opakovanými kontrolami.

    `connect` je socket.create_connection; testy a simulátor dodají podvržené
    spojení a výsledky jsou pak fiktivní data (simulated=True), stejně jako
    u healthchecku AnimeDB.
    """

    labels = LABELS  # id výsledků, které collect() vrací (pro run.failed_results)

    def __init__(self, validity_seconds, host, port, timeout_seconds, connect=socket.create_connection,
                 interval_seconds=NAS_INTERVAL_DEFAULT):
        if not is_finite_number(validity_seconds) or validity_seconds <= 0:
            raise ValueError(f"validity_seconds musí být kladný počet sekund, ne {validity_seconds!r}")
        check_nas_host(host)  # ConfigError je ValueError
        check_port(port)
        check_health_timeout(timeout_seconds)
        check_service_interval(interval_seconds)
        self.validity_seconds = validity_seconds
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self.connect = connect
        self.simulated = connect is not socket.create_connection
        self.outage = OutageConfirmation(validity_seconds)

    def collect(self, now, observed_utc):
        """Jeden výsledek `service.nas`. `now` je monotónní čas, `observed_utc` čas UTC."""
        # Neočekávaná výjimka z probe() přeruší řadu (OutageConfirmation.check).
        state, text, details = self.outage.check(self.probe, now)
        return [CheckResult(NAS_ID, state, f"{LABELS[NAS_ID]}: {text}", observed_utc,
                            now + self.validity_seconds, simulated=self.simulated, details=details)]

    def next_check(self, now):
        """Monotónní čas další skutečné kontroly (run.Scheduled); rychle jen při podezření na výpadek."""
        return now + self.outage.next_delay(self.interval_seconds)

    def probe(self):
        """(výsledek, popis, details) jednoho pokusu o spojení; None = výsledek nic nedokládá."""
        try:
            connect_once(self.host, self.port, self.timeout_seconds, self.connect)
        except TimeoutError:
            return DOWN, f"bez odpovědi do {self.timeout_seconds:g} s", {HEALTH: DOWN, CAUSE: "timeout"}
        except ConnectionRefusedError:
            return DOWN, "spojení odmítnuto", {HEALTH: DOWN, CAUSE: "refused"}
        except OSError as error:
            if error.errno in UNREACHABLE_ERRNOS:
                return DOWN, "síť nebo adresa nedostupná", {HEALTH: DOWN, CAUSE: "unreachable"}
            # Chyba na této straně (např. nedostatek deskriptorů) nebo jiná chyba
            # spojení, která výpadek NAS nedokládá: o NAS nic nevíme.
            return None, f"kontrolu nelze provést ({type(error).__name__})", {}
        # Jen přijaté spojení TCP: o sdílených složkách, datech ani zálohách nic netvrdí.
        return UP, f"služba SMB přijímá spojení (port {self.port})", {HEALTH: UP}
