"""Odchozí HTTPS heartbeat pro veřejný web (M6-5): jen celkový stav a čas.

Veřejný stav počítají stávající pravidla (complete_service_results, effective
v overall_state) nad výsledky měřicí smyčky. Jediný rozdíl proti lokálnímu
webu a OLED: NEXUS, který zatím nemá vlastní kontrolu a je jen doplněné
UNKNOWN, se nezapočítá (jinak by zdravý systém nikdy nebyl OK). Zapnutá
kontrola bez výsledku a prošlý výsledek jsou UNKNOWN, vypnuté (N/A) se
nepočítají, INFO i OK jsou veřejně OK. Potvrzení závad veřejný stav nemění
a simulovaná data nikdy nejsou OK.

Tělo je přesně {"v":1,"state":"…","ts":…} (Unix čas UTC vzniku hlášení),
podpis HMAC-SHA256 nad přesnými bajty těla v hlavičce X-NexusSTAT-Signature.
Žádné adresy, názvy služeb, hodnoty, logy ani texty chyb.

Provoz: výchozí vypnuto; první hlášení FIRST_DELAY_SECONDS po startu smyčky,
pak na mřížce INTERVAL_SECONDS bez dohánění a bez opakování při selhání.
Plánuje a hlášení skládá měřicí smyčka (`tick`), přenos dělá jediné vlákno na
pozadí; běží-li ještě předchozí přenos, nové hlášení se vynechá (žádná fronta
ani další vlákno). HTTPS s ověřením certifikátu i názvu serveru, bez
přesměrování a bez proxy (http.client je nezná). Každá operace se soketem má
limit OPERATION_SECONDS a celý přenos od začátku po odpověď TRANSFER_SECONDS:
po něm smyčka soket ukončí, i když server odpovídá po bajtech. Překlad názvu
(DNS) tímto limitem přerušit nejde, trvá podle systémového resolveru; běží ale
jen ve vlákně přenosu, takže smyčku nezdrží a další hlášení se jen vynechají.
Po DNS a těsně před odesláním vlákno znovu ověří limit i ukončení, takže po
limitu ani po ukončení NexusSTAT se pozdě dokončený přenos nikam nepošle.
"""

import hashlib
import hmac
import http.client
import json
import math
import os
import socket
import ssl
import stat
import threading
import time
from socket import SHUT_RDWR
from urllib.parse import urlsplit

from .core import CheckResult, State, overall_state
from .services import NEXUS_SERVICES_ID, SERVICE_LABELS, complete_service_results

FIRST_DELAY_SECONDS = 60
INTERVAL_SECONDS = 300
OPERATION_SECONDS = 5
TRANSFER_SECONDS = 15
MAX_RESPONSE_BYTES = 1024
MIN_SECRET_CHARS = 32
MAX_SECRET_FILE_BYTES = 4096
CONTRACT_VERSION = 1
SIGNATURE_HEADER = "X-NexusSTAT-Signature"
# Veřejné stavy; INFO (platné měření bez prahu) je veřejně OK.
PUBLIC_STATES = {State.CRITICAL: "CRITICAL", State.ERROR: "ERROR", State.WARNING: "WARNING",
                 State.UNKNOWN: "UNKNOWN", State.INFO: "OK", State.OK: "OK"}


class HeartbeatError(ValueError):
    """Tajemství heartbeatu nelze bezpečně použít."""


def public_state(results, now, observed_utc, monitored, validity_seconds):
    """Veřejný celkový stav: OK, WARNING, ERROR, CRITICAL nebo UNKNOWN.

    `results` jsou výsledky taktu smyčky, `monitored` id kontrol zapnutých
    kolektorů (labels). Služby doplní complete_service_results stejně jako na
    lokálním webu (zapnutá bez výsledku UNKNOWN, vypnutá N/A); vynechá se jen
    NEXUS doplněný bez vlastní kontroly. Pro ostatní sledované kontroly platí
    totéž: chybí-li výsledek, počítá se UNKNOWN (pojistka, kolektory dnes vždy
    vracejí všechny své kontroly).
    """
    present = {result.id for result in results}
    expires = now + validity_seconds
    counted = [result for result in complete_service_results(results, observed_utc, expires, monitored=monitored)
               if result.id != NEXUS_SERVICES_ID or result.id in present]
    seen = {result.id for result in counted}
    counted += [CheckResult(check_id, State.UNKNOWN, "bez dat", observed_utc, expires, simulated=False)
                for check_id in sorted(set(monitored) - seen - set(SERVICE_LABELS))]
    if any(result.simulated for result in counted):
        return PUBLIC_STATES[State.UNKNOWN]  # simulace nikdy nejde ven jako skutečný stav
    return PUBLIC_STATES[overall_state(counted, now)]


def encode_report(state, ts):
    """Přesné bajty těla podle kontraktu v1."""
    if state not in set(PUBLIC_STATES.values()) or isinstance(ts, bool) or not isinstance(ts, int) or ts <= 0:
        raise ValueError(f"neplatné hlášení {state!r}, {ts!r}")
    return json.dumps({"v": CONTRACT_VERSION, "state": state, "ts": ts}, separators=(",", ":")).encode("ascii")


def sign(secret, body):
    return "v1=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def load_secret(path):
    """Sdílené tajemství ze souboru správce: obyčejný soubor vlastníka bez přístupu
    skupiny a ostatních, ne symlink, ASCII bez mezer, aspoň MIN_SECRET_CHARS znaků."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as error:
        raise HeartbeatError(f"soubor tajemství {path} nelze otevřít ({error.strerror or error})") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HeartbeatError(f"{path} není obyčejný soubor")
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise HeartbeatError(f"{path} smí patřit jen tomuto uživateli a číst jen on (chmod 600)")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(MAX_SECRET_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    secret = content.strip()
    if len(content) > MAX_SECRET_FILE_BYTES or len(secret) < MIN_SECRET_CHARS or \
            not all(0x21 <= byte <= 0x7E for byte in secret):
        raise HeartbeatError(f"tajemství v {path} musí být aspoň {MIN_SECRET_CHARS} znaků ASCII bez mezer")
    return secret


def client_context(ca_file=None):
    """TLS klienta: ověření certifikátu a názvu serveru, TLS 1.2+; `ca_file` = jen tato CA."""
    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _shutdown(sock):
    try:
        # U TLS jen soket jádra: SSLSocket.shutdown by z cizího vlákna rušil stav TLS přenosu.
        (super(ssl.SSLSocket, sock) if isinstance(sock, ssl.SSLSocket) else sock).shutdown(SHUT_RDWR)
    except OSError:
        pass


class Heartbeat:
    """Plán hlášení (měřicí smyčka) a jediné vlákno přenosu.

    `tick()` volá jen měřicí smyčka; `close()` při ukončení. Počítadla
    `attempts`, `delivered`, `failures` a `skipped` slouží výpisu a testům.
    """

    def __init__(self, url, secret, context, monitored, validity_seconds, *, output=print,
                 connect=socket.create_connection, clock=time.monotonic,
                 operation_seconds=OPERATION_SECONDS, transfer_seconds=TRANSFER_SECONDS):
        parts = urlsplit(url)
        self.host, self.port, self.path = parts.hostname, parts.port or 443, parts.path
        self._secret = secret
        self._context = context
        self._monitored = frozenset(monitored)
        self._validity_seconds = validity_seconds
        self._output = output
        self._connect = connect
        self._clock = clock
        self._operation_seconds = operation_seconds
        self._transfer_seconds = transfer_seconds
        self._cond = threading.Condition()
        self._job = None  # tělo pro vlákno přenosu (nejvýš jedno)
        self._busy = False
        self._socket = None  # soket probíhajícího přenosu, aby ho šlo po termínu ukončit
        self._deadline = None
        self._expired = False
        self._outcome = None  # (doručeno, důvod) posledního přenosu pro výpis ve smyčce
        self._stopping = False
        self._origin = self._next_due = None
        self._failing = False
        self._delivered_once = False
        self.attempts = self.delivered = self.failures = self.skipped = 0
        self._thread = threading.Thread(target=self._run, name="nexusstat-heartbeat", daemon=True)
        self._thread.start()

    def tick(self, results, now, observed_utc):
        """Takt smyčky: termín přenosu, výpis výsledku a v plánovaný čas nové hlášení."""
        self._enforce_deadline()
        self._report()
        if self._next_due is None:
            self._origin = self._next_due = now + FIRST_DELAY_SECONDS
        if now < self._next_due:
            return
        # Zmeškané termíny se nedohánějí: další je až po `now` na téže mřížce.
        self._next_due = self._origin + (math.floor((now - self._origin) / INTERVAL_SECONDS) + 1) * INTERVAL_SECONDS
        state = public_state(results, now, observed_utc, self._monitored, self._validity_seconds)
        body = encode_report(state, max(1, math.floor(observed_utc.timestamp())))
        with self._cond:
            if self._busy or self._stopping:
                self.skipped += 1
                busy = self._busy
            else:
                self._job, self._busy = body, True
                self._cond.notify_all()
                return
        if busy:
            self._output("Heartbeat: předchozí přenos ještě běží, hlášení vynecháno (žádná fronta).")

    def close(self, timeout=2.0):
        """Ukončí vlákno; probíhající přenos přeruší (kromě čekání na DNS, které skončí samo)."""
        with self._cond:
            self._stopping = True
            self._job = None
            sock = self._socket
            self._cond.notify_all()
        if sock is not None:
            _shutdown(sock)
        self._thread.join(timeout)

    def wait_idle(self, timeout):
        """Počká, až nic neběží (výpis, testy); vrací False po limitu."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._busy, timeout)

    def _enforce_deadline(self):
        with self._cond:
            sock, deadline = self._socket, self._deadline
            if sock is None or deadline is None or self._clock() < deadline:
                return
            self._expired, self._deadline = True, None
        _shutdown(sock)

    def _report(self):
        with self._cond:
            outcome, self._outcome = self._outcome, None
        if outcome is None:
            return
        delivered, reason = outcome
        if delivered:
            self.delivered += 1
            if self._failing:
                self._output("Heartbeat: hlášení znovu doručeno.")
            elif not self._delivered_once:
                self._output("Heartbeat: první hlášení doručeno.")
            self._failing, self._delivered_once = False, True
            return
        self.failures += 1
        if not self._failing:  # jeden řádek za řadu selhání, ne za každé hlášení
            self._output(f"Heartbeat: hlášení nedoručeno ({reason}); další až v plánovaném termínu, bez opakování.")
        self._failing = True

    def _run(self):
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._job is not None or self._stopping)
                if self._job is None:
                    return
                body, self._job = self._job, None
                self._deadline = self._clock() + self._transfer_seconds
                self._expired = False
            try:
                delivered, reason = self._send(body)
            except Exception as error:  # noqa: BLE001 - přenos nesmí ukončit vlákno ani monitoring
                delivered, reason = False, type(error).__name__
            with self._cond:
                if self._expired:
                    delivered, reason = False, f"celkový limit {self._transfer_seconds} s"
                self._outcome = (delivered, reason)
                self._busy, self._socket, self._deadline = False, None, None
                self._cond.notify_all()

    def _abandoned(self):
        """True, když přenos nesmí pokračovat: NexusSTAT končí, nebo už vypršel celkový limit.

        Překlad názvu (DNS) přerušit nejde; po jeho skončení se proto limit
        i ukončení kontrolují znovu a pozdě dokončený přenos nic neodešle.
        """
        with self._cond:
            expired = self._deadline is None or self._clock() >= self._deadline
            if expired and not self._stopping:
                self._expired = True  # výsledek se ohlásí jako celkový limit
            return self._stopping or expired

    def _send(self, body):
        """Jeden pokus: (doručeno, důvod). Úspěch je jen odpověď 204 podle kontraktu."""
        self.attempts += 1
        headers = {"Content-Type": "application/json", SIGNATURE_HEADER: sign(self._secret, body),
                   "Connection": "close"}
        try:
            raw = self._connect((self.host, self.port), timeout=self._operation_seconds)  # DNS a spojení
        except socket.gaierror:
            return False, "DNS"
        except TimeoutError:
            return False, "timeout spojení"
        except OSError as error:
            return False, f"spojení ({error.strerror or type(error).__name__})"
        if self._abandoned():  # DNS trvalo přes celkový limit, nebo NexusSTAT mezitím končí
            raw.close()
            return False, "přerušeno"
        try:
            tls = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
        except (ssl.SSLError, ValueError):
            raw.close()
            return False, "TLS"
        with self._cond:
            self._socket = tls
        try:
            tls.do_handshake()
            connection = http.client.HTTPSConnection(self.host, self.port, timeout=self._operation_seconds,
                                                     context=self._context)
            connection.sock = tls  # spojení už je navázané a ověřené; http.client nepřesměrovává
            if self._abandoned():  # poslední kontrola před odesláním: po limitu ani po ukončení nic nejde ven
                return False, "přerušeno"
            connection.request("POST", self.path, body=body, headers=headers)
            response = connection.getresponse()
            response.read(MAX_RESPONSE_BYTES)
            status = response.status
        except ssl.SSLCertVerificationError:
            return False, "certifikát serveru neověřen"
        except TimeoutError:
            return False, "timeout"
        except ssl.SSLError:
            return False, "TLS"
        except http.client.HTTPException:
            return False, "neplatná odpověď"
        except OSError as error:
            return False, f"spojení ({error.strerror or type(error).__name__})"
        finally:
            with self._cond:
                self._socket = None
            tls.close()
        if status != 204:
            return False, f"HTTP {status}"
        return True, ""
