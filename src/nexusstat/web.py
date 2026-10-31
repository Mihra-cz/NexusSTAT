"""Lokální webový dashboard jen pro čtení (M6-1), jen na 127.0.0.1.

Spuštění na vývojovém počítači:
  PYTHONPATH=src python3 -m nexusstat.run --web [--web-port 8765]

Web nic neměří. Měřicí smyčka (run.run_loop) po každém taktu zapíše poslední
vyhodnocené výsledky do StateBoard v paměti a stránka se skládá jen z nich.
Načtení, obnova ani více otevřených záložek proto nespustí žádnou kontrolu.
Zařízení ROOT se zjistí jednou při spuštění webu (root_device, M5-S3).

Texty a hodnoty jsou stejné jako na OLED (oled_text): z id, stavu,
measurement a details, nikdy ze summary, takže web neukáže cesty ani adresy
z konfigurace. Platnost se posuzuje v okamžiku požadavku (effective):
zastavená smyčka nevypadá jako zdravý stav, hodnoty zestárnou na UNKNOWN.

Bezpečnost M6-1: server naslouchá napevno jen na 127.0.0.1 (adresa se nedá
nastavit), přijme jen GET a HEAD na "/", odmítne jiného klienta než loopback
i cizí hlavičku Host (ochrana proti DNS rebinding), nemá zapisovací endpoint,
nic nespouští a neovládá hardware. Všechny texty se escapují. Otevření v LAN
vyžaduje autentizaci a samostatné schválení.
"""

import base64
import hashlib
import html
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address

from .core import State, active_problems, effective, has_monitor_data, overall_state
from .mounts import ROOT_MODE_ID
from .network_monitor import IPV4_ID, LINK_ID, RX_RATE_ID, TX_RATE_ID
from .oled_text import PAGE_CHECKS, network_rows, page_rows, service_rows, status_texts, storage_rows
from .root_device import identify_root_device
from .services import SERVICE_LABELS, complete_service_results
from .system_monitor import DISK_ROOT_ID

LOOPBACK = "127.0.0.1"  # M6-1: jen tento počítač; LAN až s autentizací
DEFAULT_PORT = 8765  # navržený port lokálního webu (REQUIREMENTS.md, Web / přístup)
MIN_PORT, MAX_PORT = 1024, 65535  # bez privilegovaných portů
# Obnova stránky v prohlížeči: jako měření CPU, RAM a teploty (5 s). Jeden
# požadavek jen čte paměť, takže víc záložek měření nenásobí.
REFRESH_SECONDS = 5
# Pomalý nebo nečinný klient nesmí držet vlákno serveru donekonečna.
REQUEST_TIMEOUT_SECONDS = 5
# Jak často nečinný server ověří, zda má skončit (serve_forever); tolik nejvýš trvá stop_web.
POLL_SECONDS = 0.5

# Barva stavu: UNKNOWN je šedý, nikdy zelený jako OK.
CSS = """\
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{margin:0 auto;max-width:64rem;padding:1rem}
h1{font-size:1.4rem;margin:0}h2{font-size:1.05rem;margin:0 0 .5rem}
.t{color:#888;font-size:.9rem}
.cards{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(min(24rem,100%),1fr));margin-top:1rem}
section{border:1px solid #8886;border-radius:.5rem;padding:.75rem;overflow-x:auto}
table{width:100%;border-collapse:collapse}td{padding:.25rem .3rem;border-top:1px solid #8883}
td.v{font-weight:600;text-align:right;font-variant-numeric:tabular-nums}td.v,td.t{white-space:nowrap}
.s{display:inline-block;min-width:4.6rem;text-align:center;border-radius:.3rem;padding:.05rem .4rem;
font-size:.8rem;font-weight:700}
.ok{background:#15803d;color:#fff}.info{background:#1d4ed8;color:#fff}.warning{background:#f59e0b;color:#000}
.error{background:#dc2626;color:#fff}.critical{background:#7f1d1d;color:#fff}.unknown{background:#6b7280;color:#fff}
.na{background:#d1d5db;color:#000}ul{margin:.25rem 0;padding-left:1.2rem}li{margin:.2rem 0}
@media (max-width:32rem){td.t{display:none}}
"""
# CSP povolí jen tento jeden blok stylu (podle otisku): žádný skript, obrázek ani formulář.
CSS_HASH = base64.b64encode(hashlib.sha256(CSS.encode("utf-8")).digest()).decode("ascii")
SECURITY_HEADERS = {
    "Content-Security-Policy": (f"default-src 'none'; style-src 'sha256-{CSS_HASH}'; base-uri 'none'; "
                                "form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
NO_VALUE = "—"


@dataclass(frozen=True)
class Snapshot:
    """Výsledky jednoho taktu smyčky: monotónní čas taktu a skutečný čas UTC."""

    results: tuple
    now: float
    observed_utc: datetime


class StateBoard:
    """Poslední vyhodnocené výsledky pro web, jen v paměti: smyčka zapisuje, web čte.

    Výsledky (CheckResult) jsou neměnné, takže se mezi vlákny předávají celé
    a čtení nikdy nevidí rozpracovaný takt. `monitored` jsou id kontrol
    zapnutých kolektorů (doplnění služeb jako na OLED), `root_device` je
    jednou zjištěné zařízení ROOT, jen údaj pro zobrazení.
    """

    def __init__(self, monitored=(), validity_seconds=30, root_device=None):
        self.monitored = frozenset(monitored)
        self.validity_seconds = validity_seconds
        self.root_device = root_device
        self._lock = threading.Lock()
        self._snapshot = None

    def publish(self, results, now, observed_utc):
        snapshot = Snapshot(tuple(results), now, observed_utc)
        with self._lock:
            self._snapshot = snapshot

    def snapshot(self):
        with self._lock:
            return self._snapshot


def esc(value):
    return html.escape(str(value), quote=True)


def badge(state):
    """Štítek stavu; None = řádek bez vlastního stavu (jen údaj pro zobrazení, ROOT DEV)."""
    if state is None:
        return f'<span class="t">{NO_VALUE}</span>'
    return f'<span class="s {state.name.lower()}">{esc(state.value)}</span>'


def is_valid(result):
    return result.state not in (State.UNKNOWN, State.NA)


def utc_text(moment):
    return f"{moment:%Y-%m-%d %H:%M:%S} UTC"


def sections(results, now, root_device):
    """Tabulky stránky: (nadpis, [(název, hodnota, id kontroly nebo None)]), hodnoty jako na OLED."""
    tables = [
        ("Systém", page_rows("SYSTEM", results, now), PAGE_CHECKS["SYSTEM"]),
        ("Síť", network_rows(results, now), (LINK_ID, RX_RATE_ID, TX_RATE_ID, IPV4_ID)),
        # ROOT DEV nemá vlastní stav: je to jen jednou zjištěný údaj pro zobrazení.
        ("Úložiště", storage_rows(results, now, root_device), (DISK_ROOT_ID, ROOT_MODE_ID, None, ROOT_MODE_ID)),
        ("Služby", service_rows(results, now), tuple(SERVICE_LABELS)),
    ]
    return [(title, [(label, value, check_id) for (label, value), check_id in zip(rows, ids)])
            for title, rows, ids in tables]


def render_page(board, now):
    """Celá stránka HTML z posledního taktu v monotónním čase `now`; nic neměří ani nečte ze systému."""
    snapshot = board.snapshot()
    if snapshot is None:
        results = []
        updated = "Zatím žádné měření."
    else:
        # Služby bez výsledku jako na OLED: sledovaná UNKNOWN, vypnutá N/A, nikdy OK.
        results = complete_service_results(snapshot.results, snapshot.observed_utc,
                                           snapshot.now + board.validity_seconds, monitored=board.monitored)
        age = max(0.0, now - snapshot.now)
        valid = [effective(result, now) for result in results]
        latest = max((result.observed_utc for result in valid if is_valid(result)), default=None)
        updated = f"Časy v UTC. Poslední takt smyčky {utc_text(snapshot.observed_utc)} (před {age:.0f} s)"
        updated += f"; poslední platné měření {utc_text(latest)}." if latest else "; žádné platné měření."
        if age >= board.validity_seconds:
            updated += " Smyčka se neozývá: hodnoty jsou zastaralé."
    current = {result.id: effective(result, now) for result in results}
    overall = overall_state(results, now)
    problems = active_problems(results, now)
    if problems:
        items = "".join(f"<li>{badge(problem.state)} {esc(status_texts(problem, now)[0])}</li>" for problem in problems)
        problem_html = f"<ul>{items}</ul>"
    else:
        # Bez dat nevíme nic: „žádné závady“ jen při skutečně sledovaných výsledcích.
        problem_html = "<p>Žádné aktivní závady.</p>" if has_monitor_data(results) else "<p>Žádná data.</p>"
    cards = []
    for title, rows in sections(results, now, board.root_device):
        lines = []
        for label, value, check_id in rows:
            result = current.get(check_id) if check_id is not None else None
            state = None if check_id is None else (result.state if result is not None else State.UNKNOWN)
            measured = f"{result.observed_utc:%H:%M:%S}" if result is not None and is_valid(result) else NO_VALUE
            lines.append(f"<tr><td>{esc(label)}</td><td class=\"v\">{esc(value)}</td><td>{badge(state)}</td>"
                         f"<td class=\"t\">{esc(measured)}</td></tr>")
        cards.append(f"<section><h2>{esc(title)}</h2><table>{''.join(lines)}</table></section>")
    return (
        '<!DOCTYPE html><html lang="cs"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}"><title>NexusSTAT</title>'
        f"<style>{CSS}</style></head><body>"
        f"<header><h1>NexusSTAT {badge(overall)}</h1><p class=\"t\">{esc(updated)}</p></header>"
        f"<section><h2>Aktivní závady a UNKNOWN</h2>{problem_html}</section>"
        f"<div class=\"cards\">{''.join(cards)}</div>"
        f"<p class=\"t\">Jen pro čtení, jen z tohoto počítače ({LOOPBACK}). Obnova po {REFRESH_SECONDS} s; "
        "měření běží v nexusstat.run nezávisle na stránce.</p></body></html>"
    )


def is_loopback(address):
    try:
        return ip_address(address).is_loopback
    except ValueError:
        return False


class DashboardHandler(BaseHTTPRequestHandler):
    """GET a HEAD na "/" jen z loopbacku a se známým Host; ostatní metody 405, jiné cesty 404."""

    server_version = "NexusSTAT"
    timeout = REQUEST_TIMEOUT_SECONDS

    def version_string(self):
        return self.server_version  # hlavička Server bez verze Pythonu

    def do_GET(self):
        self.respond(body=True)

    def do_HEAD(self):
        self.respond(body=False)

    def refuse_method(self):
        # Tělo požadavku se nečte; spojení se zavře, aby se nevyložilo jako další požadavek.
        self.close_connection = True
        self.send_text(405, "Web je jen pro čtení.\n", {"Allow": "GET, HEAD", "Connection": "close"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = refuse_method

    def respond(self, body):
        if not is_loopback(self.client_address[0]):
            self.send_text(403, "Jen z tohoto počítače.\n", body=body)
        elif self.headers.get("Host") not in self.server.allowed_hosts:
            self.send_text(403, "Nepovolená hlavička Host.\n", body=body)
        elif self.path.split("?", 1)[0] != "/":  # doslovně "/", ne "//jiný-host/" ani "/../"
            self.send_text(404, "Nenalezeno.\n", body=body)
        else:
            self.send_text(200, self.server.page(), content_type="text/html; charset=utf-8", body=body)

    def send_text(self, status, text, headers=None, content_type="text/plain; charset=utf-8", body=True):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(data)

    def log_message(self, format, *args):
        pass  # žádný řádek za každý požadavek: výpis smyčky i journal zůstanou tiché


class DashboardServer(ThreadingHTTPServer):
    """HTTP server napevno na 127.0.0.1; `port` 0 = volný port (testy)."""

    daemon_threads = True

    def __init__(self, board, port, clock=time.monotonic):
        super().__init__((LOOPBACK, port), DashboardHandler)
        self.board = board
        self.clock = clock
        bound = self.server_address[1]
        self.allowed_hosts = {f"{LOOPBACK}:{bound}", f"localhost:{bound}"}

    def page(self):
        return render_page(self.board, self.clock())

    def handle_error(self, request, client_address):
        # Klient odešel dřív, než dostal odpověď (zavřená záložka, přerušená obnova):
        # to není chyba webu a traceback by zaplnil tichý výpis smyčky i journal.
        # Jiná výjimka (např. chyba stránky) se vypíše jako dřív.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def check_port(port):
    if isinstance(port, bool) or not isinstance(port, int) or not MIN_PORT <= port <= MAX_PORT:
        raise ValueError(f"port webu musí být {MIN_PORT}–{MAX_PORT}, ne {port!r}")
    return port


def start_web(monitored, validity_seconds, port=DEFAULT_PORT, device_factory=identify_root_device,
              poll_seconds=POLL_SECONDS):
    """Spustí web ve vlákně na pozadí; vrací (StateBoard, DashboardServer). OSError = port nejde použít.

    Zařízení ROOT se zjistí jen tady, jednou; chyba identifikace dá na stránce UNKN.
    """
    try:
        root_device = device_factory()
    except Exception:
        root_device = None
    board = StateBoard(monitored, validity_seconds, root_device)
    server = DashboardServer(board, port)
    threading.Thread(target=server.serve_forever, args=(poll_seconds,), name="nexusstat-web", daemon=True).start()
    return board, server


def stop_web(server):
    """Zastaví příjem požadavků a uvolní port (čeká nejvýš na jedno kolo serve_forever)."""
    server.shutdown()
    server.server_close()
