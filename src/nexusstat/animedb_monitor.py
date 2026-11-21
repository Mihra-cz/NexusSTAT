"""Read-only healthcheck AnimeDB přes její HTTP endpoint /health (SERVICES-1).

Při každém měření jediný požadavek `GET` na výslovně nastavenou adresu
(`[services.animedb] url`), jen standardní knihovnou a bez Dockeru. Spojení
otevírá přímo socket, takže se nepoužije žádná proxy z prostředí, a
přesměrování se nenásleduje. Celá kontrola (spojení, odeslání i příjem) má
jeden pevný termín `timeout_seconds`: každé čtení dostane jen zbývající čas,
takže ani pomalu odpovídající server měření nezdrží déle. Při vypnutém
sledování (výchozí stav) kolektor vůbec nevznikne (run.live_monitor) a nikam
se nepřipojuje.

Výsledkem je jediná kontrola `service.animedb`:

* HTTP 200 a JSON `{"status": "ok"}` → `OK`,
* výpadek (spojení odmítnuto nebo přerušeno, timeout, nedostupná adresa,
  HTTP 5xx) → `ERROR` až po potvrzení opakovanými kontrolami; do té doby
  `UNKNOWN`, takže krátký restart aplikace nevyvolá blikání,
* jiná nebo neplatná odpověď (neplatný JSON, jiný `status`, HTTP 3xx a 4xx)
  → `UNKNOWN`, nikdy OK.

Potvrzování je stejné jako u alarmových prahů (alarms.py): výpadek vznikne po
DEFAULT_CONFIRM_COUNT po sobě jdoucích kontrolách s výpadkem a odezní po
DEFAULT_CLEAR_COUNT po sobě jdoucích úspěšných kontrolách; neznámý výsledek ani
mezera v měření výpadek nepotvrdí ani nevyřeší, jen přeruší řadu. Logika je
společná s kontrolou NAS (services.OutageConfirmation).

Řízená údržba (M7-S11, jen s `status_url`): po /health se přečte veřejný
`progress.json` koordinátoru reorganizace, který vydává týž Nginx. Doložená
probíhající operace (`active` true, `updated_at` čerstvé, bez rozporu) je
INFO s details `health` = maintenance („MAINT“): ani výpadek, ani ověřené
zdraví; řada potvrzování výpadku se jí přeruší. Bez dokladu (soubor chybí,
koordinátor nic neřídí, STOP, nedostupný stav) platí potvrzování výpadku
beze změny, takže skutečný výpadek se nikdy neschová. Zastaralý nebo
neplatný doklad dá UNKNOWN, pokud zdraví nedoloží samo /health.

Od SCHED-1 se kontroluje jen jednou za `interval_seconds` (výchozí 15 min,
run.Scheduled); po prvním neúspěchu nejvýš dva rychlé pokusy s odstupem 30 s
a po prvním úspěchu po potvrzeném výpadku jeden.
Výsledek platí do další plánované kontroly plus rezervu (`validity_seconds`
nastaví run.live_monitor).
"""

import http.client
import io
import json
import socket
import time

from . import __version__
from .config import (ANIMEDB_INTERVAL_DEFAULT, HEALTH_PATH_SUFFIX, STATUS_PATH_SUFFIX, check_health_timeout,
                     check_health_url, check_service_interval)
from .core import CheckResult, State, is_finite_number
# Pevné texty `health`/`cause` a chyby nedostupnosti jsou společné s kontrolou NAS.
from .services import ANIMEDB_ID, CAUSE, DOWN, HEALTH, INVALID, MAINTENANCE, UNREACHABLE_ERRNOS, UP, OutageConfirmation

LABELS = {ANIMEDB_ID: "AnimeDB"}

# Odpověď /health je krátký JSON; delší tělo je neočekávané a dál se nečte.
MAX_BODY_BYTES = 4096
# progress.json koordinátoru má pár set bajtů (fáze, popis); větší je neplatný doklad.
STATUS_MAX_BYTES = 64 * 1024
# Koordinátor obnovuje updated_at během operace každých 5 s (HEARTBEAT_SECONDS
# v AnimeDB); delší ticho znamená zastavený nebo zaseknutý koordinátor.
MAINTENANCE_FRESH_SECONDS = 60
# Výsledky operace v progress.json; STOP při běžící operaci by byl rozpor.
STATUS_OUTCOMES = {"UNVERIFIED", "NORMAL", "STOP"}
ACTIVE, STALE = "active", "stale"  # doklad údržby z maintenance_evidence(), vedle None a INVALID


class _DeadlineReader(io.RawIOBase):
    """Čtení ze socketu, kde každé recv dostane jen čas zbývající do termínu."""

    def __init__(self, sock, remaining):
        self.sock = sock
        self.remaining = remaining

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(self.remaining())
        return self.sock.recv_into(buffer)


class _ResponseSource:
    """Předá http.client.HTTPResponse čtení s termínem místo běžného makefile()."""

    def __init__(self, sock, remaining):
        self.sock = sock
        self.remaining = remaining

    def makefile(self, mode):
        return io.BufferedReader(_DeadlineReader(self.sock, self.remaining))


def request_bytes(parts):
    """Požadavek GET; adresa je už ověřená (tisknutelné ASCII bez mezer)."""
    return (f"GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\nAccept: application/json\r\n"
            f"User-Agent: NexusSTAT/{__version__}\r\nConnection: close\r\n\r\n").encode("ascii")


def fetch(url, timeout, connect=socket.create_connection, clock=time.monotonic, suffix=HEALTH_PATH_SUFFIX,
          limit=MAX_BODY_BYTES):
    """(HTTP stav, tělo nejvýš `limit` + 1 bajtů) z jednoho GET s pevným termínem.

    Překročení termínu kdekoli vyvolá TimeoutError. Adresa je IP (check_health_url),
    takže se nic nepřekládá přes DNS, které by limit nerespektovalo.
    """
    parts = check_health_url(url, suffix)
    deadline = clock() + timeout

    def remaining():
        left = deadline - clock()
        if left <= 0:
            raise TimeoutError(f"bez odpovědi do {timeout:g} s")
        return left

    sock = connect((parts.hostname, parts.port or 80), timeout=remaining())
    try:
        sock.settimeout(remaining())
        sock.sendall(request_bytes(parts))
        # Jen rozbor odpovědi ze standardní knihovny; přesměrování se nenásleduje.
        response = http.client.HTTPResponse(_ResponseSource(sock, remaining), method="GET")
        response.begin()
        return response.status, response.read(limit + 1)
    finally:
        sock.close()


def classify(status, body):
    """(výsledek, popis, details) podle odpovědi HTTP."""
    if 500 <= status <= 599:
        # Doložená chyba serveru: aplikace odpověděla, že není v pořádku.
        return DOWN, f"HTTP {status}", {HEALTH: DOWN, CAUSE: f"http_{status}"}
    if status != 200:
        # Přesměrování, chyba požadavku (např. 404 špatná cesta) a podobně:
        # o zdraví aplikace to nic nedokládá.
        return INVALID, f"neočekávaná odpověď HTTP {status}", {HEALTH: INVALID, CAUSE: f"http_{status}"}
    if len(body) > MAX_BODY_BYTES:
        return INVALID, "neočekávaně dlouhá odpověď", {HEALTH: INVALID, CAUSE: "too_long"}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        return INVALID, "neplatný JSON", {HEALTH: INVALID, CAUSE: "json"}
    if not isinstance(data, dict) or "status" not in data:
        return INVALID, "odpověď bez položky status", {HEALTH: INVALID, CAUSE: "status"}
    if data["status"] != "ok":
        return INVALID, "status není \"ok\"", {HEALTH: INVALID, CAUSE: "status"}
    return UP, "/health odpovídá, status ok", {HEALTH: UP}


def maintenance_evidence(status, body, wall_now):
    """Doklad řízené údržby z progress.json: ACTIVE, None (žádná údržba), STALE nebo INVALID.

    Jen pole, která koordinátor AnimeDB skutečně zapisuje (version, active,
    outcome, updated_at v sekundách Unix času). 404 = koordinátor zatím nic
    nezveřejnil. `active` false (NORMAL i STOP) žádnou údržbu nedokládá.
    """
    if status == 404:
        return None
    if status != 200 or len(body) > STATUS_MAX_BYTES:
        return INVALID
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        return INVALID
    if not isinstance(data, dict) or data.get("version") != 1:
        return INVALID
    active, outcome, updated = data.get("active"), data.get("outcome"), data.get("updated_at")
    if not isinstance(active, bool) or outcome not in STATUS_OUTCOMES or not is_finite_number(updated):
        return INVALID
    if not active:
        return None
    if outcome == "STOP":
        return INVALID  # rozpor: zastavená operace nemůže zároveň běžet
    if abs(wall_now - updated) > MAINTENANCE_FRESH_SECONDS:
        return STALE
    return ACTIVE


class AnimeDbMonitor:
    """Healthcheck AnimeDB s potvrzováním výpadku opakovanými kontrolami.

    `connect` je socket.create_connection a `clock` time.monotonic; testy
    dodají syntetické odpovědi HTTP a výsledky jsou pak fiktivní data
    (simulated=True), stejně jako u ostatních kolektorů se vzorovými soubory.
    """

    labels = LABELS  # id výsledků, které collect() vrací (pro run.failed_results)

    def __init__(self, validity_seconds, url, timeout_seconds, connect=socket.create_connection, clock=time.monotonic,
                 interval_seconds=ANIMEDB_INTERVAL_DEFAULT, status_url=None, wall_clock=time.time):
        if not is_finite_number(validity_seconds) or validity_seconds <= 0:
            raise ValueError(f"validity_seconds musí být kladný počet sekund, ne {validity_seconds!r}")
        check_health_url(url)  # ConfigError je ValueError
        check_health_timeout(timeout_seconds)
        check_service_interval(interval_seconds)
        if status_url is not None:
            check_health_url(status_url, STATUS_PATH_SUFFIX, "status_url")
        self.validity_seconds = validity_seconds
        self.url = url
        self.status_url = status_url
        self.wall_clock = wall_clock
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self.connect = connect
        self.clock = clock
        self.simulated = connect is not socket.create_connection
        self.outage = OutageConfirmation(validity_seconds)

    def collect(self, now, observed_utc):
        """Jeden výsledek `service.animedb`. `now` je monotónní čas, `observed_utc` čas UTC."""
        state, text, details = self.outage.check(self.probe, now)
        if details.get(HEALTH) == MAINTENANCE:
            # Doložená řízená údržba: informace, ne závada ani ověřené zdraví (alarm ani RGB ji nehlásí).
            state = State.INFO
        return [CheckResult(ANIMEDB_ID, state, f"{LABELS[ANIMEDB_ID]}: {text}", observed_utc,
                            now + self.validity_seconds, simulated=self.simulated, details=details)]

    def next_check(self, now):
        """Monotónní čas další skutečné kontroly (run.Scheduled); rychle jen při podezření na výpadek."""
        return now + self.outage.next_delay(self.interval_seconds)

    def probe(self):
        """(výsledek, popis, details) jedné kontroly; None = výpadek nepotvrdí ani nevyřeší.

        Doložená údržba i nečitelný doklad u nedoloženého zdraví vrací None:
        přeruší řadu potvrzování, takže z nich nikdy nevznikne ERROR ani OK.
        """
        outcome, text, details = self.health()
        evidence = self.maintenance()
        if evidence == ACTIVE:
            return None, "plánovaná údržba (reorganizace řízená koordinátorem)", {HEALTH: MAINTENANCE}
        if evidence in (STALE, INVALID):
            problem = "zastaralý" if evidence == STALE else "neplatný"
            if outcome == UP:
                return UP, f"{text} ({problem} stav reorganizace)", details
            return None, f"{text}; {problem} stav reorganizace", {HEALTH: INVALID, CAUSE: f"status_{evidence}"}
        return outcome, text, details

    def maintenance(self):
        """Doklad údržby z `status_url`; bez něj nebo při nedostupném stavu None."""
        if self.status_url is None:
            return None
        try:
            status, body = fetch(self.status_url, self.timeout_seconds, self.connect, self.clock,
                                 suffix=STATUS_PATH_SUFFIX, limit=STATUS_MAX_BYTES)
        except (OSError, http.client.HTTPException):
            # Nedostupný stav nic nedokládá: výpadek /health se posoudí jako dřív (i výpadek Nginxu).
            return None
        return maintenance_evidence(status, body, self.wall_clock())

    def health(self):
        """(výsledek, popis, details) jednoho dotazu na /health; None = dotaz nešlo provést."""
        try:
            status, body = fetch(self.url, self.timeout_seconds, self.connect, self.clock)
        except TimeoutError:
            return DOWN, f"bez odpovědi do {self.timeout_seconds:g} s", {HEALTH: DOWN, CAUSE: "timeout"}
        except ConnectionRefusedError:
            return DOWN, "spojení odmítnuto", {HEALTH: DOWN, CAUSE: "refused"}
        except ConnectionError:
            # Reset, přerušení i spojení zavřené bez odpovědi (RemoteDisconnected).
            return DOWN, "spojení přerušeno", {HEALTH: DOWN, CAUSE: "disconnected"}
        except http.client.HTTPException:
            return INVALID, "neplatná odpověď HTTP", {HEALTH: INVALID, CAUSE: "protocol"}
        except OSError as error:
            if error.errno in UNREACHABLE_ERRNOS:
                return DOWN, "adresa nedostupná", {HEALTH: DOWN, CAUSE: "unreachable"}
            # Chyba na této straně (např. nedostatek deskriptorů): o AnimeDB nic nevíme.
            return None, f"kontrolu nelze provést ({type(error).__name__})", {}
        return classify(status, body)
