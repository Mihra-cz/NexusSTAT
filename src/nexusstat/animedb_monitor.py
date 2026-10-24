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
mezera v měření výpadek nepotvrdí ani nevyřeší, jen přeruší řadu.
"""

import errno
import http.client
import io
import json
import socket
import time

from . import __version__
from .config import DEFAULT_CLEAR_COUNT, DEFAULT_CONFIRM_COUNT, check_health_timeout, check_health_url
from .core import CheckResult, State, is_finite_number
from .services import ANIMEDB_ID

LABELS = {ANIMEDB_ID: "AnimeDB"}

# Odpověď /health je krátký JSON; delší tělo je neočekávané a dál se nečte.
MAX_BODY_BYTES = 4096

# Výsledek jedné kontroly; zároveň hodnota `health` v details (jen tyto texty,
# takže historie zapíše řádek jen při skutečné změně).
HEALTH = "health"
CAUSE = "cause"  # proč výpadek nebo neplatná odpověď: refused, timeout, http_503, json, ...
UP = "ok"
DOWN = "down"
INVALID = "invalid"

# Chyby spojení, které dokládají nedostupnost adresy (ne chybu na této straně).
UNREACHABLE_ERRNOS = (errno.EHOSTUNREACH, errno.ENETUNREACH)


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


def fetch(url, timeout, connect=socket.create_connection, clock=time.monotonic):
    """(HTTP stav, tělo nejvýš MAX_BODY_BYTES + 1 bajtů) z jednoho GET s pevným termínem.

    Překročení termínu kdekoli vyvolá TimeoutError. Adresa je IP (check_health_url),
    takže se nic nepřekládá přes DNS, které by limit nerespektovalo.
    """
    parts = check_health_url(url)
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
        return response.status, response.read(MAX_BODY_BYTES + 1)
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


class AnimeDbMonitor:
    """Healthcheck AnimeDB s potvrzováním výpadku opakovanými kontrolami.

    `connect` je socket.create_connection a `clock` time.monotonic; testy
    dodají syntetické odpovědi HTTP a výsledky jsou pak fiktivní data
    (simulated=True), stejně jako u ostatních kolektorů se vzorovými soubory.
    """

    labels = LABELS  # id výsledků, které collect() vrací (pro run.failed_results)

    def __init__(self, validity_seconds, url, timeout_seconds, connect=socket.create_connection, clock=time.monotonic):
        if not is_finite_number(validity_seconds) or validity_seconds <= 0:
            raise ValueError(f"validity_seconds musí být kladný počet sekund, ne {validity_seconds!r}")
        check_health_url(url)  # ConfigError je ValueError
        check_health_timeout(timeout_seconds)
        self.validity_seconds = validity_seconds
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.connect = connect
        self.clock = clock
        self.simulated = connect is not socket.create_connection
        # Paměť potvrzování (jako alarms.History): po sobě jdoucí kontroly
        # s výpadkem a úspěšné kontroly, potvrzený výpadek a platnost
        # posledního výsledku (pro poznání mezery v měření).
        self.failures = 0
        self.successes = 0
        self.down = False
        self.last_expires = None

    def collect(self, now, observed_utc):
        """Jeden výsledek `service.animedb`. `now` je monotónní čas, `observed_utc` čas UTC."""
        outcome, text, details = self.probe()
        state, note = self.confirm(outcome, now)
        return [CheckResult(ANIMEDB_ID, state, f"{LABELS[ANIMEDB_ID]}: {text}{note}", observed_utc,
                            now + self.validity_seconds, simulated=self.simulated, details=details)]

    def probe(self):
        """(výsledek, popis, details) jednoho dotazu; None = kontrolu nešlo provést."""
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

    def confirm(self, outcome, now):
        """(stav, doplněk popisu) po potvrzení stejným principem jako alarmové prahy."""
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
