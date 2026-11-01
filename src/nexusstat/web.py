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

Stránka /history (M6-2) ukazuje grafy ze SQLite historie jen pro čtení
(history_charts: open_reader, krátká transakce, výsledek v paměti); hlavní
stránka databázi nečte. Bez zapnuté historie jen vysvětlí proč.

Bezpečnost M6-1: server naslouchá napevno jen na 127.0.0.1 (adresa se nedá
nastavit), přijme jen GET a HEAD na "/" a "/history", odmítne jiného klienta než loopback
i cizí hlavičku Host (ochrana proti DNS rebinding), nemá zapisovací endpoint,
nic nespouští a neovládá hardware. Všechny texty se escapují. Otevření v LAN
vyžaduje autentizaci a samostatné schválení.
"""

import base64
import hashlib
import html
import math
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from urllib.parse import parse_qs

from .config import DISK_INTERVAL_SECONDS
from .core import Measurement, State, active_problems, effective, has_monitor_data, overall_state
from .history_charts import CHART_CHECKS, DEFAULT_RANGE, RANGES, ChartError, Series
from .mounts import ROOT_MODE_ID
from .network_monitor import IPV4_ID, LINK_ID, RX_RATE_ID, TX_RATE_ID
from .oled_text import (PAGE_CHECKS, measurement_text, network_rows, page_rows, service_rows, status_texts,
                        storage_rows)
from .root_device import identify_root_device
from .services import SERVICE_LABELS, complete_service_results
from .system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID

LOOPBACK = "127.0.0.1"  # M6-1: jen tento počítač; LAN až s autentizací
DEFAULT_PORT = 8765  # navržený port lokálního webu (REQUIREMENTS.md, Web / přístup)
MIN_PORT, MAX_PORT = 1024, 65535  # bez privilegovaných portů
# Obnova stránky v prohlížeči: jako měření CPU, RAM a teploty (5 s). Jeden
# požadavek jen čte paměť, takže víc záložek měření nenásobí.
REFRESH_SECONDS = 5
# Stránka historie se obnovuje pomaleji: data se mění po minutách, výsledek čtení je v paměti.
HISTORY_REFRESH_SECONDS = 60
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
a{color:inherit}nav{margin:.4rem 0}nav a{margin-right:.9rem}nav a[aria-current]{font-weight:700;text-decoration:none}
svg.chart{display:block;width:100%;height:auto}.chart text{font-size:11px;fill:#888}
.chart .axis{stroke:#8888;stroke-width:1}.chart .grid{stroke:#8884;stroke-width:1}
.chart .line{fill:none;stroke:#3b82f6;stroke-width:2}.chart .band{fill:#3b82f633}.chart .dot{fill:#3b82f6}
.chart .unk{fill:#6b728066}
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
HISTORY_OFF = ("Historie je vypnutá ([history] enabled = false). Grafy se ukážou po jejím zapnutí; "
               "měření a aktuální stav běží beze změny.")
HISTORY_FAILED = ("Historie se při startu nespustila (důvod je ve výpisu nexusstat.run). "
                  "Měření a aktuální stav běží beze změny.")
CHART_TITLES = {CPU_USAGE_ID: "CPU vytížení", CPU_TEMP_ID: "Teplota CPU", MEMORY_ID: "RAM",
                RX_RATE_ID: "NETWORK RX (rychlost)", TX_RATE_ID: "NETWORK TX (rychlost)",
                DISK_ROOT_ID: "Obsazenost ROOT"}
SOURCE_TEXT = {"sample": "surové vzorky", "agg_5m": "5min agregace a nejnovější surové vzorky"}
# Graf v SVG: souřadnice viewBoxu, šířka se přizpůsobí kartě (desktop i mobil).
CHART_WIDTH, CHART_HEIGHT = 400, 150
PLOT_LEFT, PLOT_RIGHT, PLOT_TOP, PLOT_BOTTOM = 52, 394, 8, 128


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
        f"<header><h1>NexusSTAT {badge(overall)}</h1><nav><a href=\"/\" aria-current=\"page\">Aktuální stav</a>"
        f"<a href=\"/history\">Historie</a></nav><p class=\"t\">{esc(updated)}</p></header>"
        f"<section><h2>Aktivní závady a UNKNOWN</h2>{problem_html}</section>"
        f"<div class=\"cards\">{''.join(cards)}</div>"
        f"<p class=\"t\">Jen pro čtení, jen z tohoto počítače ({LOOPBACK}). Obnova po {REFRESH_SECONDS} s; "
        "měření běží v nexusstat.run nezávisle na stránce.</p></body></html>"
    )


def value_text(value, unit):
    """Hodnota osy nebo souhrnu jako na dashboardu (23%, 48°C, 1.2 MB/s)."""
    return measurement_text(Measurement(value, unit))


def nice_ceiling(value):
    """Nejbližší „hezká“ horní mez (1, 2, 5 × 10^n) pro rychlost sítě."""
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    return next(m * magnitude for m in (1, 2, 5, 10) if value <= m * magnitude)


def y_scale(series):
    """(dolní, horní) mez osy: % vždy 0–100, rychlost od nuly, jinak podle dat s rezervou."""
    valid = [bucket for bucket in series.buckets if bucket.n_valid]
    if series.unit == "%":
        return 0.0, 100.0
    if series.unit == "B/s":
        return 0.0, nice_ceiling(max(bucket.maximum for bucket in valid))
    low = math.floor(min(bucket.minimum for bucket in valid)) - 1
    high = math.ceil(max(bucket.maximum for bucket in valid)) + 1
    return float(low), float(high)


def segments(buckets, max_step):
    """Souvislé úseky platných bodů. UNKNOWN i chybějící data úsek přeruší, nikdy se nespojí čarou."""
    parts, current = [], []
    for bucket in buckets:
        if not bucket.n_valid or (current and bucket.start - current[-1].start > max_step):
            if current:
                parts.append(current)
            current = []
        if bucket.n_valid:
            current.append(bucket)
    if current:
        parts.append(current)
    return parts


def chart_svg(series, charts, native_step):
    """Graf jedné řady: průměr čarou, min–max pásem, UNKNOWN šedě, mezery prázdné; časy v UTC."""
    start, end, step = charts.start, charts.end, charts.period.step
    low, high = y_scale(series)

    def x(moment):
        return PLOT_LEFT + (moment - start) / (end - start) * (PLOT_RIGHT - PLOT_LEFT)

    def y(value):
        value = min(max(value, low), high)
        return PLOT_BOTTOM - (value - low) / (high - low) * (PLOT_BOTTOM - PLOT_TOP)

    def point(bucket, value):
        return f"{x(bucket.start + step / 2):.1f},{y(value):.1f}"

    parts = []
    for bucket in series.buckets:
        if not bucket.n_valid:
            parts.append(f'<rect class="unk" x="{x(bucket.start):.1f}" y="{PLOT_TOP}" '
                         f'width="{max(x(bucket.start + step) - x(bucket.start), 1):.1f}" '
                         f'height="{PLOT_BOTTOM - PLOT_TOP}"/>')
    for part in segments(series.buckets, max(step, 2 * native_step)):
        if len(part) == 1:
            parts.append(f'<circle class="dot" cx="{x(part[0].start + step / 2):.1f}" '
                         f'cy="{y(part[0].average):.1f}" r="2.5"/>')
            continue
        band = [point(b, b.maximum) for b in part] + [point(b, b.minimum) for b in reversed(part)]
        parts.append(f'<polygon class="band" points="{" ".join(band)}"/>')
        parts.append(f'<polyline class="line" points="{" ".join(point(b, b.average) for b in part)}"/>')
    # Bez data by 24 h začínalo i končilo stejným časem (11:00 – 11:00).
    seconds = charts.period.seconds
    time_format = "%H:%M" if seconds <= 3600 else "%d.%m. %H:%M" if seconds <= 7 * 86_400 else "%d.%m."
    labels = [
        (PLOT_LEFT - 4, PLOT_TOP + 4, "end", value_text(high, series.unit)),
        (PLOT_LEFT - 4, PLOT_BOTTOM, "end", value_text(low, series.unit)),
        (PLOT_LEFT, CHART_HEIGHT - 6, "start", f"{datetime.fromtimestamp(start, timezone.utc):{time_format}}"),
        (PLOT_RIGHT, CHART_HEIGHT - 6, "end", f"{datetime.fromtimestamp(end, timezone.utc):{time_format}}"),
    ]
    text = "".join(f'<text x="{tx}" y="{ty}" text-anchor="{anchor}">{esc(label)}</text>'
                   for tx, ty, anchor, label in labels)
    middle = (PLOT_TOP + PLOT_BOTTOM) / 2
    return (f'<svg class="chart" viewBox="0 0 {CHART_WIDTH} {CHART_HEIGHT}" role="img" '
            f'aria-label="{esc(CHART_TITLES[series.check_id])}">'
            f'<line class="grid" x1="{PLOT_LEFT}" y1="{middle}" x2="{PLOT_RIGHT}" y2="{middle}"/>'
            f'<line class="axis" x1="{PLOT_LEFT}" y1="{PLOT_BOTTOM}" x2="{PLOT_RIGHT}" y2="{PLOT_BOTTOM}"/>'
            f'<line class="axis" x1="{PLOT_LEFT}" y1="{PLOT_TOP}" x2="{PLOT_LEFT}" y2="{PLOT_BOTTOM}"/>'
            f'{"".join(parts)}{text}</svg>')


def chart_section(series, charts, interval_seconds):
    """Karta jednoho grafu se souhrnem za rozsah (průměr ze součtu platných hodnot, ne z průměrů)."""
    title = CHART_TITLES[series.check_id] + (f" – {series.subject.upper()}" if series.subject else "")
    valid = [bucket for bucket in series.buckets if bucket.n_valid]
    unknown = sum(1 for bucket in series.buckets if not bucket.n_valid)
    if not valid:
        body = "<p class=\"t\">Žádná platná data v tomto rozsahu.</p>"
    else:
        average = sum(bucket.total for bucket in valid) / sum(bucket.n_valid for bucket in valid)
        lowest, highest = min(b.minimum for b in valid), max(b.maximum for b in valid)
        summary = (f"průměr {value_text(average, series.unit)}, min {value_text(lowest, series.unit)}, "
                   f"max {value_text(highest, series.unit)}")
        if unknown:
            summary += f", neznámé body {unknown}"
        native = DISK_INTERVAL_SECONDS if series.check_id == DISK_ROOT_ID else interval_seconds
        body = f"<p class=\"t\">{esc(summary)}</p>{chart_svg(series, charts, native)}"
    return f"<section><h2>{esc(title)}</h2>{body}</section>"


def render_history_page(source, name):
    """Stránka historie pro rozsah `name` (klíč RANGES).

    `source` je HistoryCharts, nebo text, proč historie není (vypnutá, nespustila se).
    """
    current = ' aria-current="page"'
    links = "".join(f'<a href="/history?range={key}"{current if key == name else ""}>{esc(chart_range.label)}</a>'
                    for key, chart_range in RANGES.items())
    if isinstance(source, str):
        note, body = source, ""
    else:
        charts = source.charts(name)
        if isinstance(charts, ChartError):
            note = (f"Historie je teď nedostupná (důvod: {charts.reason}). Měření a aktuální stav "
                    "běží dál; další pokus nejdřív za 30 s.")
            body = ""
        else:
            period = charts.period
            note = (f"Časy v UTC. {period.label} ze zdroje {SOURCE_TEXT[period.source]}, bod po "
                    f"{period.step // 60 or period.step} {'min' if period.step >= 60 else 's'}; načteno "
                    f"{datetime.fromtimestamp(charts.read_utc, timezone.utc):%H:%M:%S} UTC, další čtení nejdřív "
                    f"za {period.cache_seconds} s. Čára = průměr, pás = minimum až maximum, šedě neznámá "
                    "hodnota (UNKNOWN), prázdné místo = žádná data.")
            sections = []
            for check_id in CHART_CHECKS:
                found = [series for series in charts.series if series.check_id == check_id]
                for series in found or [Series(check_id, "", "", ())]:
                    sections.append(chart_section(series, charts, source.interval_seconds))
            body = f"<div class=\"cards\">{''.join(sections)}</div>"
    return (
        '<!DOCTYPE html><html lang="cs"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="refresh" content="{HISTORY_REFRESH_SECONDS}"><title>NexusSTAT – historie</title>'
        f"<style>{CSS}</style></head><body>"
        f"<header><h1>NexusSTAT – historie</h1><nav><a href=\"/\">Aktuální stav</a>{links}</nav>"
        f"<p class=\"t\">{esc(note)}</p></header>{body}"
        f"<p class=\"t\">Jen pro čtení, jen z tohoto počítače ({LOOPBACK}). Historie se čte z SQLite jen "
        "pro čtení a výsledek se drží v paměti; měření ani aktuální stav tím nejsou dotčené.</p>"
        "</body></html>"
    )


def is_loopback(address):
    try:
        return ip_address(address).is_loopback
    except ValueError:
        return False


class DashboardHandler(BaseHTTPRequestHandler):
    """GET a HEAD na "/" a "/history" jen z loopbacku a se známým Host; ostatní metody 405, jiné cesty 404."""

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
        else:
            path, _, query = self.path.partition("?")  # doslovně "/" nebo "/history", ne "//jiný-host/"
            if path == "/":
                self.send_text(200, self.server.page(), content_type="text/html; charset=utf-8", body=body)
            elif path == "/history":
                name = history_range(query)
                if name is None:
                    self.send_text(400, "Neznámý parametr nebo rozsah historie.\n", body=body)
                else:
                    self.send_text(200, self.server.history_page(name), content_type="text/html; charset=utf-8",
                                   body=body)
            else:
                self.send_text(404, "Nenalezeno.\n", body=body)

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
        # Zdroj grafů: HistoryCharts, nebo text, proč historie není (výchozí: vypnutá).
        self.history = HISTORY_OFF

    def page(self):
        return render_page(self.board, self.clock())

    def history_page(self, name):
        return render_history_page(self.history, name)

    def handle_error(self, request, client_address):
        # Klient odešel dřív, než dostal odpověď (zavřená záložka, přerušená obnova):
        # to není chyba webu a traceback by zaplnil tichý výpis smyčky i journal.
        # Jiná výjimka (např. chyba stránky) se vypíše jako dřív.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def history_range(query):
    """Klíč rozsahu z dotazu `range=…` (výchozí DEFAULT_RANGE); jiný parametr nebo hodnota je None."""
    params = parse_qs(query, keep_blank_values=True)
    values = params.pop("range", [DEFAULT_RANGE])
    if params or len(values) != 1 or values[0] not in RANGES:
        return None
    return values[0]


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
