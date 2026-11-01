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

Bezpečnost: výchozí je naslouchání jen na 127.0.0.1. Od M6-3 jde výslovně
zadat jednu soukromou adresu LAN (`WebAccess`, nikdy 0.0.0.0) se schválenými
rozsahy klientů; jiný klient se zavře hned po přijetí spojení. Přijme GET
a HEAD na "/" a "/history", odmítne cizí hlavičku Host (ochrana proti DNS
rebinding), nic nespouští a neovládá hardware. Všechny texty se escapují.
Server obsluhuje nejvýš MAX_CONNECTIONS spojení naráz (další se zavřou bez
vlákna), jedno spojení nejvýš REQUEST_DEADLINE_SECONDS celkem a hlavičku
požadavku nejvýš MAX_REQUEST_BYTES. Dashboard i historie jsou bez přihlášení
jen pro čtení; otevření v LAN až po bezpečnostním review.

Od M6-4B může server běžet přes HTTPS (`web_tls.tls_context` z existujících
souborů certifikátu a klíče; nic se negeneruje). Jen na HTTPS a jen s nastavenou
administrací (`server.admin`, admin.py) přijme i POST na tři akce pod /admin/
s malým formulářem; bez TLS je administrace vypnutá a POST se dál odmítá.
"""

import base64
import hashlib
import html
import http.client
import ipaddress
import math
import re
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from socket import SHUT_RDWR
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

LOOPBACK = "127.0.0.1"  # výchozí: jen tento počítač; LAN jen výslovně (WebAccess, M6-3)
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
# Souběžně obsluhovaná spojení: další se hned zavřou, nevznikne pro ně vlákno.
MAX_CONNECTIONS = 8
# Celková doba jednoho spojení (hlavičky i odpověď). Timeout operace sám nestačí:
# klient, který posílá po bajtech, by ho obnovoval donekonečna.
REQUEST_DEADLINE_SECONDS = 10
# Řádek požadavku a hlavičky dohromady; tělo čte jen administrace (MAX_FORM_BYTES).
MAX_REQUEST_BYTES = 8192
# Administrace (M6-4B): jen malé formuláře urlencoded s přesnou délkou.
ADMIN_PREFIX = "/admin"
MAX_FORM_BYTES = 1024
FORM_TYPE = "application/x-www-form-urlencoded"
ADMIN_OFF = ("Administrace je vypnutá: vyžaduje HTTPS (--web-tls-cert a --web-tls-key) a soubor otisku hesla "
             "(--web-admin-password).\n")
# Nejvýš tolik přijatých spojení za sekundu. Bez omezení by klient, který stránku
# obnovuje bez přestávky, zabral celé jádro (změřeno asi 700 stránek/s); takto
# další pokusy čekají ve frontě jádra a obsluha i měření běží dál.
MAX_ACCEPTS_PER_SECOND = 20
# Další povolený název v hlavičce Host (např. jméno počítače v LAN), malými písmeny bez portu.
HOST_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")

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
form{margin:.25rem 0}input,button{font:inherit}input[type=number]{width:5rem}
@media (max-width:32rem){td.t{display:none}}
"""
# CSP povolí jen tento jeden blok stylu (podle otisku): žádný skript ani obrázek; formulář jen administrace.
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


def render_page(board, now, admin=False):
    """Celá stránka HTML z posledního taktu v monotónním čase `now`; nic neměří ani nečte ze systému.

    `admin` = přidat odkaz na administraci (jen když ji server na HTTPS nabízí).
    """
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
        f"<a href=\"/history\">Historie</a>{'<a href=\"/admin\">Administrace</a>' if admin else ''}</nav>"
        f"<p class=\"t\">{esc(updated)}</p></header>"
        f"<section><h2>Aktivní závady a UNKNOWN</h2>{problem_html}</section>"
        f"<div class=\"cards\">{''.join(cards)}</div>"
        f"<p class=\"t\">Jen pro čtení, bez přihlášení. Obnova po {REFRESH_SECONDS} s; "
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


def render_history_page(source, name, charts=None):
    """Stránka historie pro rozsah `name` (klíč RANGES).

    `source` je HistoryCharts, nebo text, proč historie není (vypnutá, nespustila se);
    `charts` je už přečtený výsledek `source.charts(name)` (jinak se přečte tady).
    """
    current = ' aria-current="page"'
    links = "".join(f'<a href="/history?range={key}"{current if key == name else ""}>{esc(chart_range.label)}</a>'
                    for key, chart_range in RANGES.items())
    if isinstance(source, str):
        note, body = source, ""
    else:
        charts = source.charts(name) if charts is None else charts
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
        f"<p class=\"t\">Jen pro čtení, bez přihlášení. Historie se čte z SQLite jen "
        "pro čtení a výsledek se drží v paměti; měření ani aktuální stav tím nejsou dotčené.</p>"
        "</body></html>"
    )


@dataclass(frozen=True)
class WebAccess:
    """Kde web naslouchá a kdo se smí připojit (M6-3); výchozí jen 127.0.0.1.

    Klient se posuzuje jen podle skutečné adresy spojení, nikdy podle hlaviček
    (X-Forwarded-For ani jiné se nečtou). Tento počítač (loopback) smí vždy.
    """

    bind: str = LOOPBACK
    networks: tuple = ()  # schválené rozsahy klientů v LAN (IPv4Network)
    host_names: tuple = ()  # další povolené názvy v hlavičce Host

    @property
    def loopback(self):
        return ip_address(self.bind).is_loopback

    def client_allowed(self, address):
        try:
            client = ip_address(address)
        except ValueError:
            return False
        if client.version == 6 and client.ipv4_mapped is not None:
            client = client.ipv4_mapped
        return client.is_loopback or any(client in network for network in self.networks)

    def allowed_hosts(self, port):
        names = [self.bind, *self.host_names] + (["localhost"] if self.loopback else [])
        return {f"{name}:{port}" for name in names}


def web_access(bind=LOOPBACK, allow=(), host_names=()):
    """Ověřený WebAccess z voleb --web-bind, --web-allow a --web-host; ValueError u čehokoli nejistého.

    Adresa je 127.0.0.1, nebo jedna konkrétní soukromá adresa LAN (nikdy 0.0.0.0,
    veřejná ani link-local) a pak musí být aspoň jeden schválený rozsah klientů.
    Rozsahy jsou soukromé sítě IPv4 zadané přesně (bez bitů hostitele), ne
    odvozené z výchozí trasy. IPv6 zatím ne.
    """
    try:
        address = ipaddress.IPv4Address(bind)
    except ValueError:
        raise ValueError(f"--web-bind musí být jedna adresa IPv4, ne {bind!r}") from None
    # Python počítá mezi „soukromé“ i rezervované bloky (třída E a všesměrová adresa):
    # vylučují se zvlášť.
    unusable = address.is_link_local or address.is_unspecified or address.is_reserved or address.is_multicast
    if not (address.is_loopback or (address.is_private and not unusable)):
        raise ValueError(f"--web-bind {bind}: jen 127.0.0.1 nebo konkrétní soukromá adresa LAN, "
                         "ne 0.0.0.0 ani veřejná")
    networks = []
    for text in allow:
        try:
            network = ipaddress.IPv4Network(text, strict=True)
        except ValueError:
            raise ValueError(f"--web-allow musí být rozsah IPv4 bez bitů hostitele (např. 192.0.2.0/24), "
                             f"ne {text!r}") from None
        if (not network.is_private or network.is_loopback or network.is_link_local or network.is_reserved
                or network.is_multicast or network.prefixlen < 8):
            raise ValueError(f"--web-allow {text}: jen soukromý rozsah LAN (nejvýš /8)")
        networks.append(network)
    if address.is_loopback and networks:
        raise ValueError("--web-allow má smysl jen s adresou LAN v --web-bind")
    if not address.is_loopback and not networks:
        raise ValueError("adresa LAN v --web-bind vyžaduje aspoň jeden schválený rozsah --web-allow")
    names = []
    for name in host_names:
        if not HOST_NAME.fullmatch(name):
            raise ValueError(f"--web-host musí být název počítače malými písmeny bez portu, ne {name!r}")
        names.append(name)
    return WebAccess(str(address), tuple(networks), tuple(names))


class RequestTooLarge(http.client.LineTooLong):
    """Řádek požadavku a hlavičky dohromady jsou delší než MAX_REQUEST_BYTES."""


class LimitedReader:
    """rfile obsluhy, který přečte nejvýš `limit` bajtů hlavičky požadavku; tělo nečte vůbec."""

    def __init__(self, raw, limit):
        self.raw = raw
        self.left = limit

    def readline(self, size=-1):
        if self.left <= 0:
            raise RequestTooLarge("hlavičky požadavku")
        line = self.raw.readline(self.left + 1 if size < 0 else min(size, self.left + 1))
        self.left -= len(line)
        if self.left < 0:
            raise RequestTooLarge("hlavičky požadavku")
        return line

    def read(self, size=-1):
        raise RequestTooLarge("tělo požadavku")

    def read_body(self, length):
        """Tělo formuláře administrace: nejvýš `length` bajtů (volající ověří MAX_FORM_BYTES)."""
        return self.raw.read(length)

    def close(self):
        self.raw.close()


def is_loopback(address):
    try:
        return ip_address(address).is_loopback
    except ValueError:
        return False


class DashboardHandler(BaseHTTPRequestHandler):
    """GET a HEAD na "/" a "/history" jen z loopbacku a se známým Host; ostatní metody 405, jiné cesty 404."""

    server_version = "NexusSTAT"
    timeout = REQUEST_TIMEOUT_SECONDS

    def setup(self):
        super().setup()
        if isinstance(self.connection, ssl.SSLSocket):
            # Handshake TLS až tady, ve vlákně obsluhy s jejím timeoutem a celkovým
            # termínem spojení: pomalý klient nezablokuje vlákno přijímání.
            self.connection.do_handshake()
        self.rfile = LimitedReader(self.rfile, MAX_REQUEST_BYTES)

    def version_string(self):
        return self.server_version  # hlavička Server bez verze Pythonu

    def send_error(self, code, message=None, explain=None):
        # Chyby z parseru (400, 414, 431, 501) bez ozvěny požadavku a se stejnými
        # bezpečnostními hlavičkami jako ostatní odpovědi.
        self.close_connection = True
        self.send_text(code, f"Chyba {code}.\n", {"Connection": "close"}, body=self.command != "HEAD")

    def do_GET(self):
        self.respond(body=True)

    def do_HEAD(self):
        self.respond(body=False)

    def refuse_method(self):
        # Tělo požadavku se nečte; spojení se zavře, aby se nevyložilo jako další požadavek.
        self.close_connection = True
        self.send_text(405, "Web je jen pro čtení.\n", {"Allow": "GET, HEAD", "Connection": "close"})

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = refuse_method

    def do_POST(self):
        # POST jen na akce administrace a jen na HTTPS s nastavenou administrací; jinak jako dřív 405.
        admin = self.server.admin_handler()
        path, _, query = self.path.partition("?")
        if admin is None or not path.startswith(ADMIN_PREFIX + "/"):
            self.refuse_method()
            return
        self.close_connection = True
        if self.refused_origin(body=True):
            return
        form, error = self.read_form()  # malé tělo se přečte i před odmítnutím, aby spojení neskončilo RST
        if error is not None:
            self.send_text(*error)
        elif query:
            self.send_text(400, "Parametry v adrese nejsou povolené.\n")  # tajemství nikdy v URL
        else:
            self.send_admin(admin.post(path, self.headers.get("Host", "").lower(), self.headers, form))

    def read_form(self):
        """(tělo, None), nebo (None, (status, text)): jen urlencoded s jednou přesnou délkou do MAX_FORM_BYTES."""
        if self.headers.get_all("Transfer-Encoding"):
            return None, (411, "Tělo jen s přesnou délkou (Content-Length), bez Transfer-Encoding.\n")
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,5}", lengths[0].strip()):
            return None, (411, "Chybí jedna přesná délka těla (Content-Length).\n")
        length = int(lengths[0])
        if length > MAX_FORM_BYTES:
            return None, (413, "Formulář je příliš velký.\n")
        data = self.rfile.read_body(length)
        if len(data) != length:
            return None, (400, "Neúplné tělo formuláře.\n")
        types = self.headers.get_all("Content-Type") or []
        media, _, parameters = (types[0] if len(types) == 1 else "").partition(";")
        if media.strip().lower() != FORM_TYPE or parameters.strip().lower() not in ("", "charset=utf-8"):
            return None, (415, f"Jen formulář {FORM_TYPE}.\n")
        return data, None

    def send_admin(self, response, body=True):
        self.send_text(response.status, response.html, response.headers, content_type="text/html; charset=utf-8",
                       body=body)

    def refused_origin(self, body):
        """Kontrola klienta a hlavičky Host (stejná pro všechny cesty); True = už odmítnuto."""
        if not self.server.access.client_allowed(self.client_address[0]):
            self.send_text(403, "Nepovolený klient.\n", body=body)  # pojistka; verify_request ho už zavřel
        elif self.headers.get("Host", "").lower() not in self.server.allowed_hosts:
            self.send_text(403, "Nepovolená hlavička Host.\n", body=body)
        else:
            return False
        return True

    def respond(self, body):
        if not self.refused_origin(body):
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
            elif path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/"):
                admin = self.server.admin_handler()
                if admin is None:
                    self.send_text(403, ADMIN_OFF, body=body)
                elif path != ADMIN_PREFIX:
                    self.send_text(404, "Nenalezeno.\n", body=body)  # akce jen metodou POST
                elif query:
                    self.send_text(400, "Parametry v adrese nejsou povolené.\n", body=body)
                else:
                    self.send_admin(admin.get(self.headers), body=body)
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
    """HTTP server na adrese z `access` (výchozí 127.0.0.1); `port` 0 = volný port (testy).

    Spojení se přijme jen od povoleného klienta a jen při volném místě
    (`max_connections`); jinak se hned zavře, bez vlákna a bez čtení dat.
    Přijaté spojení má celkový termín `deadline_seconds`: po něm ho
    `service_actions` ukončí, i když klient posílá po bajtech.
    """

    daemon_threads = True

    def __init__(self, board, port, clock=time.monotonic, access=WebAccess(), max_connections=MAX_CONNECTIONS,
                 deadline_seconds=REQUEST_DEADLINE_SECONDS, accepts_per_second=MAX_ACCEPTS_PER_SECOND, tls=None):
        super().__init__((access.bind, port), DashboardHandler)
        if tls is not None:
            # Spojení se přijme bez handshaku; ten proběhne ve vlákně obsluhy (DashboardHandler.setup),
            # takže kontrola klienta a limit spojení platí ještě před TLS.
            self.socket = tls.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)
        self.https = tls is not None
        # Administrace (admin.Admin) se nastaví až po spuštění uživatelského stavu; bez HTTPS se nikdy nepoužije.
        self.admin = None
        self.board = board
        self.clock = clock
        self.access = access
        self.allowed_hosts = access.allowed_hosts(self.server_address[1])
        self.deadline_seconds = deadline_seconds
        self.refused = 0  # spojení zavřená hned po přijetí (cizí klient nebo plno)
        self._slots = threading.BoundedSemaphore(max_connections)
        self._deadlines = {}  # přijaté spojení -> monotónní termín ukončení
        self._deadlines_lock = threading.Lock()
        self._accept_interval = 1 / accepts_per_second
        self._next_accept = 0.0  # jen vlákno přijímání (serve_forever), bez zámku
        self._history_pages = {}  # rozsah -> (výsledek grafů, hotová stránka)
        self._pages_lock = threading.Lock()
        # Zdroj grafů: HistoryCharts, nebo text, proč historie není (výchozí: vypnutá).
        self.history = HISTORY_OFF

    def page(self):
        return render_page(self.board, self.clock(), admin=self.admin_handler() is not None)

    def admin_handler(self):
        """Administrace, jen když běží HTTPS a je nastavená; jinak None (vypnuto)."""
        return self.admin if self.https else None

    def history_page(self, name):
        """Stránka historie; dokud HistoryCharts vrací týž výsledek, posílá se hotové HTML."""
        source = self.history
        if isinstance(source, str):
            return render_history_page(source, name)
        result = source.charts(name)  # z paměti HistoryCharts, nebo jedno krátké čtení SQLite
        with self._pages_lock:
            cached = self._history_pages.get(name)
            if cached is None or cached[0] is not result:
                cached = (result, render_history_page(source, name, result))
                self._history_pages[name] = cached
        return cached[1]

    @property
    def active(self):
        """Počet právě obsluhovaných spojení."""
        with self._deadlines_lock:
            return len(self._deadlines)

    def verify_request(self, request, client_address):
        # Tempo přijímání: mezi dvěma spojeními aspoň _accept_interval (běží ve vlákně serve_forever).
        now = time.monotonic()
        if now < self._next_accept:
            time.sleep(self._next_accept - now)
        self._next_accept = max(now, self._next_accept) + self._accept_interval
        if not self.access.client_allowed(client_address[0]) or not self._slots.acquire(blocking=False):
            self.refused += 1
            return False
        with self._deadlines_lock:
            self._deadlines[request] = time.monotonic() + self.deadline_seconds
        return True

    def process_request(self, request, client_address):
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(request)  # vlákno nevzniklo: místo se uvolní hned
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request)

    def _release(self, request):
        with self._deadlines_lock:
            if self._deadlines.pop(request, None) is None:
                return
        self._slots.release()

    def service_actions(self):
        # serve_forever volá po každém kole (nejvýš po poll_seconds): spojení po
        # celkovém termínu se ukončí, blokované čtení i zápis obsluhy tím skončí.
        now = time.monotonic()
        with self._deadlines_lock:
            overdue = [request for request, deadline in self._deadlines.items() if now >= deadline]
        for request in overdue:
            try:
                # U TLS jen soket jádra: SSLSocket.shutdown by z cizího vlákna rušil stav TLS obsluhy.
                (super(ssl.SSLSocket, request) if isinstance(request, ssl.SSLSocket) else request).shutdown(SHUT_RDWR)
            except OSError:
                pass

    def handle_error(self, request, client_address):
        # Klient odešel dřív, než dostal odpověď (zavřená záložka, přerušená obnova),
        # nebo poslal příliš velký požadavek: to není chyba webu a traceback by
        # zaplnil tichý výpis smyčky i journal. Jiná výjimka se vypíše jako dřív.
        # Nezdařený handshake TLS (např. prosté HTTP na port HTTPS) nebo timeout klienta také ne.
        if not isinstance(sys.exc_info()[1], (ConnectionError, RequestTooLarge, ssl.SSLError, TimeoutError)):
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
              poll_seconds=POLL_SECONDS, access=WebAccess(), tls=None):
    """Spustí web ve vlákně na pozadí; vrací (StateBoard, DashboardServer). OSError = port nejde použít.

    Zařízení ROOT se zjistí jen tady, jednou; chyba identifikace dá na stránce UNKN.
    """
    try:
        root_device = device_factory()
    except Exception:
        root_device = None
    board = StateBoard(monitored, validity_seconds, root_device)
    server = DashboardServer(board, port, access=access, tls=tls)
    threading.Thread(target=server.serve_forever, args=(poll_seconds,), name="nexusstat-web", daemon=True).start()
    return board, server


def stop_web(server):
    """Zastaví příjem požadavků a uvolní port (čeká nejvýš na jedno kolo serve_forever)."""
    server.shutdown()
    server.server_close()
