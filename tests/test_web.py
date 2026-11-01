"""Testy lokálního webu jen pro čtení (M6-1).

Data jsou syntetická a čas simulovaný. HTTP testy spouštějí skutečný server
jen na 127.0.0.1 s volným portem (0) a ptají se ho z téhož počítače; nic
neposílají ven a nečtou /proc, /sys ani konfiguraci.
"""

import ast
import contextlib
import http.client
import inspect
import io
import ipaddress
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nexusstat import admin, web
from nexusstat.alarms import AlarmEvaluator
from nexusstat.config import AlarmsConfig, CollectorConfig
from nexusstat.core import CheckResult, Measurement, State
from nexusstat.fan_monitor import CPU_FAN_ID, FAN_ON, FAN_STATE, SOURCE, SOURCE_RPM
from nexusstat.history_charts import RANGES, Bucket, ChartError, Charts, HistoryCharts, Series
from nexusstat.mounts import ROOT_MODE_ID
from nexusstat.network_monitor import ADDRESS, INTERFACE, IPV4_ID, LINK, LINK_ID, LINK_UP, RX_RATE_ID, TX_RATE_ID
from nexusstat.run import main, run_loop
from nexusstat.services import ANIMEDB_ID, BACKUP_ID, NAS_ID
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID
from nexusstat.web import (HISTORY_FAILED, HISTORY_OFF, DashboardServer, StateBoard, WebAccess, is_loopback,
                           render_history_page, render_page, start_web, stop_web, web_access)

OBSERVED = datetime(2026, 10, 31, 12, 0, 5, tzinfo=timezone.utc)
NVME = (State.INFO, "ROOT na NVMe (oddíl disku)", {"bus": "nvme", "disk": "nvme0n1", "layout": "partition"})
MONITORED = {CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID, CPU_FAN_ID, DISK_ROOT_ID, ROOT_MODE_ID, LINK_ID, RX_RATE_ID,
             TX_RATE_ID, IPV4_ID, NAS_ID, BACKUP_ID}


def check(check_id, state, value=None, unit="%", expires=130, details=None):
    measurement = None if value is None else Measurement(value, unit)
    return CheckResult(check_id, state, "Český popis, který web nečte", OBSERVED, expires, simulated=False,
                       measurement=measurement, details=details or {})


def results(interface="eth0"):
    """Jeden takt se všemi stavy: INFO, OK, WARNING, ERROR, CRITICAL, UNKNOWN a N/A (AnimeDB vypnutá)."""
    on = {INTERFACE: interface}
    return [
        check(CPU_USAGE_ID, State.UNKNOWN),  # první vzorek
        check(CPU_TEMP_ID, State.WARNING, 74, "°C"),
        check(MEMORY_ID, State.INFO, 24),
        check(CPU_FAN_ID, State.INFO, 3200, "RPM", details={FAN_STATE: FAN_ON, SOURCE: SOURCE_RPM}),
        check(DISK_ROOT_ID, State.INFO, 42),
        check(ROOT_MODE_ID, State.ERROR, details={"mode": "ro", "ro_by": "superblock", "fstype": "ext4",
                                                  "kind": "local"}),
        check(LINK_ID, State.INFO, details={**on, LINK: LINK_UP}),
        check(RX_RATE_ID, State.INFO, 1_234_000, "B/s", details=on),
        check(TX_RATE_ID, State.INFO, 85_300, "B/s", details=on),
        check(IPV4_ID, State.INFO, details={**on, ADDRESS: "192.0.2.10"}),
        check(NAS_ID, State.CRITICAL),
        check(BACKUP_ID, State.OK),
    ]


def board_with(taken=None, root_device=NVME):
    board = StateBoard(MONITORED, validity_seconds=30, root_device=root_device)
    board.publish(results() if taken is None else taken, 100.0, OBSERVED)
    return board


class PageTest(unittest.TestCase):
    def test_states_values_and_problems(self):
        page = render_page(board_with(), 101.0)
        self.assertIn('<h1>NexusSTAT <span class="s critical">CRITICAL</span></h1>', page)
        for value in ("74°C", "24%", "ON 3200", "1.2 MB/s", "85 KB/s", "192.0.2.10", "42%", "RO", "NVME", "EXT4"):
            self.assertIn(f'<td class="v">{value}</td>', page)
        self.assertIn('<td>ETH0</td><td class="v">UP</td>', page)
        for state in ("info", "ok", "warning", "error", "critical", "unknown", "na"):
            self.assertIn(f'class="s {state}"', page)  # AnimeDB bez kolektoru je N/A, NEXUS UNKNOWN
        problems = page.split("<h2>Aktivní závady a UNKNOWN</h2>")[1].split("</section>")[0]
        for text in ("NAS OFFLINE", "ROOT READ-ONLY", "CPU TEMP 74°C", "NEXUS NO DATA", "CPU NO DATA"):
            self.assertIn(text, problems)
        self.assertIn("poslední platné měření 2026-10-31 12:00:05 UTC", page)
        self.assertNotIn("Český popis", page)  # summary se nezobrazuje
        # ROOT DEV je jen údaj pro zobrazení: bez štítku stavu, ani když zařízení nepodporujeme.
        stacked = render_page(board_with(root_device=(State.UNKNOWN, "", {"cause": "stacked"})), 101.0)
        self.assertIn('<td>ROOT DEV</td><td class="v">UNKN</td><td><span class="t">—</span></td>', stacked)

    def test_unknown_is_never_shown_as_healthy(self):
        empty = render_page(StateBoard(), 0.0)
        self.assertIn('<span class="s unknown">UNKNOWN</span>', empty)
        self.assertIn("Zatím žádné měření.", empty)
        self.assertIn("Žádná data.", empty)
        # Všechno změřené INFO, ale NEXUS nemá kolektor: celkový stav UNKNOWN, ne OK.
        healthy = [result for result in results() if result.state in (State.INFO, State.OK)]
        page = render_page(board_with(healthy), 101.0)
        self.assertIn('<h1>NexusSTAT <span class="s unknown">UNKNOWN</span></h1>', page)
        self.assertNotIn('class="s ok">OK</span></h1>', page)

    def test_values_expire_when_the_loop_stops(self):
        board = board_with()
        page = render_page(board, 130.0)  # platnost výsledků i doplněných služeb skončila
        self.assertIn('<span class="s unknown">UNKNOWN</span></h1>', page)
        self.assertIn('<td>ROOT</td><td class="v">UNKN</td>', page)
        self.assertIn('<td>ROOT MODE</td><td class="v">UNKN</td>', page)
        self.assertIn("NAS STALE", page)
        self.assertIn("Smyčka se neozývá", page)
        self.assertIn("žádné platné měření", page)

    def test_all_text_is_escaped(self):
        page = render_page(board_with(results(interface="<script>alert(1)</script>")), 101.0)
        self.assertNotIn("<script", page.lower())
        self.assertIn("&lt;SCRIPT&gt;ALERT(1)&lt;/SCRIPT&gt;", page)


def history_result(subject="eth0"):
    """Hodina historie: CPU s neznámým bodem a mezerou, RX v B/s; ostatní řady bez dat."""
    period = RANGES["1h"]
    end = 1_800_000_000
    start = end - period.seconds

    def bucket(index, value):
        return Bucket(start + index * 10, 1, 0 if value is None else 1, value, value, value)

    cpu = [bucket(i, 20.0 + i) for i in range(6)] + [bucket(6, None)] + [bucket(i, 30.0) for i in range(7, 10)]
    cpu += [bucket(i, 40.0) for i in range(41, 44)]  # 31 bodů nic: mezera, ne čára
    rx = [bucket(i, 1_234_000.0) for i in range(10)]
    series = (Series(CPU_USAGE_ID, "", "%", tuple(cpu)), Series(RX_RATE_ID, subject, "B/s", tuple(rx)))
    return Charts(period, start, end, series, 2, end - 1)


class FakeCharts:
    """Zdroj grafů pro web bez SQLite: počítá, kolikrát se stránka historie ptala."""

    interval_seconds = 10

    def __init__(self, result):
        self.result = result
        self.calls = []

    def charts(self, name):
        self.calls.append(name)
        return self.result


class HistoryPageTest(unittest.TestCase):
    """M6-2: grafy bez JavaScriptu; UNKNOWN a mezery nikdy jako čára, rychlost sítě v B/s."""

    def test_charts_keep_unknown_and_gaps_and_units(self):
        page = render_history_page(FakeCharts(history_result(subject="eth0<x>")), "1h")
        cpu, rx = page.split("<svg")[1:3]
        self.assertEqual(cpu.count("<polyline"), 3)  # úseky 0–5, 7–9, 41–43: přes UNKNOWN ani mezeru nic
        self.assertEqual(cpu.count('class="unk"'), 1)
        self.assertIn(">100%</text>", cpu)  # procenta vždy 0–100
        self.assertIn("NETWORK RX (rychlost) – ETH0&lt;X&gt;", page)  # escapovaný název rozhraní
        self.assertIn(">2.0 MB/s</text>", rx)  # osa v jednotkách rychlosti, ne objemu
        self.assertIn("průměr 1.2 MB/s", page)
        self.assertEqual(page.count("Žádná platná data v tomto rozsahu."), 4)  # teplota, RAM, TX, ROOT
        self.assertIn('href="/history?range=1h" aria-current="page"', page)
        self.assertIn('<meta http-equiv="refresh" content="60">', page)

    def test_disabled_or_unavailable_history_only_explains(self):
        for source, text in ((HISTORY_OFF, "Historie je vypnutá"), (HISTORY_FAILED, "nespustila"),
                             (FakeCharts(ChartError("timeout")), "nedostupná (důvod: timeout)")):
            page = render_history_page(source, "24h")
            with self.subTest(text=text):
                self.assertIn(text, page)
                self.assertNotIn("<svg", page)
                self.assertIn("měření a aktuální stav", page.lower())  # dashboard běží dál


class CountingCollector:
    """Kolektor smyčky, který počítá skutečná měření."""

    labels = {CPU_TEMP_ID: "Teplota CPU"}
    simulated = False
    validity_seconds = 30

    def __init__(self):
        self.calls = 0

    def collect(self, now, observed_utc):
        self.calls += 1
        return [CheckResult(CPU_TEMP_ID, State.INFO, "Teplota CPU 48 °C", observed_utc, now + 30, simulated=False,
                            measurement=Measurement(48, "°C"))]


class HttpTest(unittest.TestCase):
    """Skutečný server na 127.0.0.1 s volným portem; klient na stejném počítači."""

    def setUp(self):
        self.devices = []
        self.board, self.server = start_web(MONITORED, 30, port=0, poll_seconds=0.01,
                                            device_factory=lambda: self.devices.append(1) or NVME)
        self.addCleanup(stop_web, self.server)
        self.port = self.server.server_address[1]

    def request(self, method, path="/", host=None):
        connection = http.client.HTTPConnection(web.LOOPBACK, self.port, timeout=5)
        try:
            connection.putrequest(method, path, skip_host=True)
            connection.putheader("Host", host or f"{web.LOOPBACK}:{self.port}")
            connection.endheaders()
            response = connection.getresponse()
            return response, response.read().decode("utf-8")
        finally:
            connection.close()

    def test_get_and_head_are_read_only_pages_with_safe_headers(self):
        self.board.publish(results(), 100.0, OBSERVED)
        response, body = self.request("GET")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual((response.getheader("Cache-Control"), response.getheader("X-Content-Type-Options")),
                         ("no-store", "nosniff"))
        self.assertEqual(response.getheader("Server"), "NexusSTAT")  # bez verze Pythonu
        self.assertIn("NexusSTAT", body)
        response, body = self.request("GET", "/?x=1", host=f"localhost:{self.port}")
        self.assertEqual(response.status, 200)
        response, body = self.request("HEAD")
        self.assertEqual((response.status, body), (200, ""))

    def test_writes_unknown_paths_and_foreign_hosts_are_refused(self):
        self.board.publish(results(), 100.0, OBSERVED)
        before = self.board.snapshot()
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                response, _ = self.request(method)
                self.assertEqual((response.status, response.getheader("Allow")), (405, "GET, HEAD"))
        for path in ("/api", "/../etc/passwd", "/favicon.ico", "//127.0.0.1/"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0].status, 404)
        # Cizí Host (DNS rebinding) ani loopback na jiném portu neprojde.
        for host in ("evil.example", f"evil.example:{self.port}", f"{web.LOOPBACK}:1"):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", host=host)[0].status, 403)
        self.assertIs(self.board.snapshot(), before)  # nic se nezměnilo

    def test_requests_never_run_collectors(self):
        collector = CountingCollector()
        times = []

        def clock():
            # Simulovaný takt po 1 s; po deseti taktech „Ctrl+C“, nic se nečeká.
            if len(times) == 10:
                raise KeyboardInterrupt
            times.append(100 + len(times))
            return times[-1]

        run_loop(collector, CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=clock,
                 utc_clock=lambda: OBSERVED, sleep=lambda _: None, output=lambda line: None, tick_seconds=1,
                 board=self.board)
        measured = collector.calls
        self.assertGreater(measured, 0)
        for _ in range(20):  # „víc záložek“ a obnovy stránky
            self.assertEqual(self.request("GET")[0].status, 200)
        self.assertEqual(collector.calls, measured)
        self.assertEqual(len(self.devices), 1)  # zařízení ROOT jen jednou při startu webu
        self.assertEqual(self.board.snapshot().results[0].id, CPU_TEMP_ID)  # poslední takt smyčky

    def test_client_disconnect_is_not_reported_as_an_error(self):
        # Regrese z review: RST klienta během požadavku vypsal celý traceback ConnectionResetError.
        for error, reported in ((ConnectionResetError(104, "reset"), False), (BrokenPipeError(32, "pipe"), False),
                                (ValueError("chyba stránky"), True)):
            output = io.StringIO()
            with self.subTest(error=type(error).__name__), contextlib.redirect_stderr(output):
                try:
                    raise error
                except Exception:
                    self.server.handle_error(None, (web.LOOPBACK, 1))
                self.assertEqual("Traceback" in output.getvalue(), reported)

    def test_history_route_reads_only_on_request(self):
        source = FakeCharts(history_result())
        self.server.history = source
        for _ in range(10):  # obnova hlavního dashboardu historii nečte
            self.assertEqual(self.request("GET")[0].status, 200)
        self.assertEqual(source.calls, [])
        for path, status in (("/history", 200), ("/history?range=7d", 200), ("/history?range=1y", 400),
                             ("/history?range=1h&x=1", 400), ("/history?range=1h&range=7d", 400),
                             ("/history/", 404)):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0].status, status)
        self.assertEqual(source.calls, ["24h", "7d"])  # výchozí 24 h; neplatný dotaz nic nečte
        self.assertEqual(self.request("POST", "/history")[0].status, 405)
        self.server.history = HISTORY_OFF
        response, body = self.request("GET", "/history")
        self.assertEqual(response.status, 200)
        self.assertIn("Historie je vypnutá", body)

    def test_listens_only_on_loopback(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for function in (DashboardServer, start_web):
            self.assertNotIn("host", inspect.signature(function).parameters)  # adresa se nedá změnit
        self.assertTrue(is_loopback("127.0.0.1") and is_loopback("::1"))
        for address in ("192.0.2.5", "0.0.0.0", "::", "", "localhost"):
            self.assertFalse(is_loopback(address), address)
        tree = ast.parse(Path(web.__file__).read_text(encoding="utf-8"))
        literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}
        self.assertFalse({"0.0.0.0", "::"} & literals)
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        self.assertFalse({"subprocess", "socket", "os", "shutil"} & imported)
        with self.assertRaises(ValueError):
            web.check_port(80)


def wait_until(condition, seconds=3.0):
    """Čeká na stav serveru (přijetí, uvolnění spojení) po krocích 5 ms; žádné pevné čekání."""
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("stav serveru nenastal včas")
        time.sleep(0.005)


def partial_request(port, header=b""):
    """Spojení s nedokončenou hlavičkou (bez prázdného řádku): drží místo na serveru."""
    connection = socket.create_connection((web.LOOPBACK, port), timeout=3)
    connection.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % port + header)
    return connection


def closed_by_server(connection):
    """True, když server spojení zavřel (prázdné čtení nebo reset)."""
    try:
        return connection.recv(1024) == b""
    except ConnectionError:
        return True


# Skutečné adresy mimo dokumentační rozsahy se do repozitáře nepíšou (PublicDataTest),
# proto se veřejná a link-local adresa pro testy odmítnutí dopočítají.
PUBLIC_ADDRESS = str(ipaddress.IPv4Address(int(ipaddress.IPv4Address("192.0.2.1")) - 2 ** 24))
LINK_LOCAL_ADDRESS = str(ipaddress.IPv4Address((169 << 24) | (254 << 16) | 257))


class AccessTest(unittest.TestCase):
    """M6-3: výchozí jen 127.0.0.1; LAN jen výslovně, se schválenými rozsahy klientů."""

    def test_only_explicit_private_lan_access(self):
        default = web_access()
        self.assertEqual((default.bind, default.networks, default.loopback), ("127.0.0.1", (), True))
        self.assertEqual(default.allowed_hosts(8765), {"127.0.0.1:8765", "localhost:8765"})
        lan = web_access("192.0.2.10", ["192.0.2.0/24"], ["nexus.lan"])
        self.assertEqual(lan.allowed_hosts(8765), {"192.0.2.10:8765", "nexus.lan:8765"})  # bez localhost
        refused = [
            ("0.0.0.0", ["192.0.2.0/24"], []), ("::", ["192.0.2.0/24"], []), ("::1", [], []),
            (PUBLIC_ADDRESS, ["192.0.2.0/24"], []), (LINK_LOCAL_ADDRESS, ["192.0.2.0/24"], []),
            ("255.255.255.255", ["192.0.2.0/24"], []),  # Python ji má za „soukromou“, je ale rezervovaná
            ("192.0.2.10", [], []),  # LAN bez schváleného rozsahu
            ("192.0.2.10", ["0.0.0.0/0"], []), ("192.0.2.10", [f"{PUBLIC_ADDRESS}/32"], []),
            ("192.0.2.10", ["255.255.255.255/32"], []),  # rezervovaný blok
            ("192.0.2.10", ["192.0.2.5/24"], []),  # bity hostitele: rozsah musí být přesný
            ("127.0.0.1", ["192.0.2.0/24"], []),  # rozsah bez adresy LAN nemá smysl
            ("192.0.2.10", ["192.0.2.0/24"], ["Nexus.lan"]), ("192.0.2.10", ["192.0.2.0/24"], ["nexus:8765"]),
        ]
        for bind, allow, hosts in refused:
            with self.subTest(bind=bind, allow=allow, hosts=hosts), self.assertRaises(ValueError):
                web_access(bind, allow, hosts)
        for address, allowed in (("192.0.2.77", True), ("203.0.113.1", False), ("198.51.100.7", False),
                                 ("127.0.0.1", True), ("::1", True), ("::ffff:192.0.2.5", True), ("x", False)):
            self.assertIs(lan.client_allowed(address), allowed, address)

    def test_foreign_clients_are_closed_before_any_thread(self):
        access = WebAccess(networks=(ipaddress.IPv4Network("192.0.2.0/24"),))
        server = DashboardServer(StateBoard(), 0, access=access)
        self.addCleanup(server.server_close)
        request = object()
        self.assertTrue(server.verify_request(request, ("192.0.2.5", 1)))
        self.assertEqual(server.active, 1)
        server._release(request)
        self.assertFalse(server.verify_request(object(), ("198.51.100.7", 1)))  # mimo schválený rozsah
        self.assertEqual((server.active, server.refused), (0, 1))
        # Skutečná adresa klienta je jen adresa spojení: web čte z hlaviček jedině Host a od M6-4B
        # rámování těla formuláře, administrace jen Origin a Cookie (nikdy X-Forwarded-For).
        for module, expected in ((web, {"Host", "Transfer-Encoding", "Content-Length", "Content-Type"}),
                                 (admin, {"Origin", "Cookie"})):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            read = {node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "get_all")
                    and isinstance(node.func.value, (ast.Attribute, ast.Name))
                    and getattr(node.func.value, "attr", getattr(node.func.value, "id", "")) == "headers"}
            self.assertEqual(read, expected, module.__name__)
            self.assertFalse(any(isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                                 and node.value.attr == "headers" for node in ast.walk(tree)))


class ServerLimitTest(unittest.TestCase):
    """M6-3: omezený počet spojení, celkový termín spojení a velikost hlavičky; vše na loopbacku."""

    def serve(self, **options):
        server = DashboardServer(StateBoard(), 0, **options)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(stop_web, server)
        return server, server.server_address[1]

    def get(self, port, data):
        connection = socket.create_connection((web.LOOPBACK, port), timeout=3)
        try:
            connection.sendall(data)
            chunks = []
            while chunk := connection.recv(65536):
                chunks.append(chunk)
            return b"".join(chunks)
        except ConnectionError:
            return b""
        finally:
            connection.close()

    def test_extra_connections_are_closed_without_new_threads(self):
        server, port = self.serve(max_connections=2)
        before = threading.active_count()
        held = [partial_request(port), partial_request(port)]
        wait_until(lambda: server.active == 2)
        extra = partial_request(port)
        self.assertTrue(closed_by_server(extra))  # plno: zavřeno hned, bez vlákna
        self.assertEqual(server.refused, 1)
        self.assertLessEqual(threading.active_count(), before + 2)
        for connection in held + [extra]:
            connection.close()
        wait_until(lambda: server.active == 0)  # místa se uvolní
        self.assertTrue(self.get(port, b"GET / HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % port)
                        .startswith(b"HTTP/1.0 200"))

    def test_total_deadline_ends_slow_clients(self):
        # Klient posílá hlavičku po kouscích, takže timeout jedné operace (5 s) nikdy nevyprší.
        server, port = self.serve(deadline_seconds=0.2)
        slow = partial_request(port)
        started = time.monotonic()
        with self.assertRaises(OSError):
            while time.monotonic() - started < 3:
                slow.sendall(b"X-Slow: 1\r\n")
                time.sleep(0.02)
                if closed_by_server_nowait(slow):
                    raise BrokenPipeError
        self.assertLess(time.monotonic() - started, 1.5)
        wait_until(lambda: server.active == 0)
        slow.close()

    def test_oversized_and_malformed_requests(self):
        server, port = self.serve()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            header = b"GET / HTTP/1.0\r\nHost: 127.0.0.1:%d\r\nX-Big: %s\r\n\r\n" % (port, b"a" * 9000)
            response = self.get(port, header)
            self.assertTrue(response.startswith(b"HTTP/1.0 431"), response[:40])
            self.assertIn(b"Content-Security-Policy", response)
            self.assertEqual(self.get(port, b"GET /" + b"a" * 9000 + b" HTTP/1.0\r\n\r\n"), b"")  # zavřeno
            bad = self.get(port, b"GARBAGE<script>\r\n\r\n")  # jednoslovný řádek = HTTP/0.9: jen tělo
            self.assertEqual(bad, b"Chyba 400.\n")  # chyba neopakuje požadavek
            wait_until(lambda: server.active == 0)
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_accept_pace_and_cached_history_page_bound_the_cost_of_a_flood(self):
        server, port = self.serve(accepts_per_second=50)
        server.history = FakeCharts(history_result())
        request = b"GET /history?range=1h HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % port
        with mock.patch.object(web, "render_history_page", wraps=web.render_history_page) as render:
            started = time.monotonic()
            pages = {self.get(port, request).split(b"\r\n\r\n", 1)[1] for _ in range(10)}  # tělo bez hlavičky Date
            elapsed = time.monotonic() - started
            self.assertEqual(render.call_count, 1)  # SVG se složí jednou, dokud se data grafů nezmění
            server.history.result = history_result()  # nové čtení = nový výsledek
            self.get(port, request)
            self.assertEqual(render.call_count, 2)
        self.assertGreaterEqual(elapsed, 9 / 50 - 0.01)  # nejvýš 50 spojení za sekundu
        self.assertEqual(len(pages), 1)  # stále tatáž stránka

    def test_shutdown_with_busy_slots_and_restart_on_the_same_port(self):
        board, server = start_web(set(), 30, port=0, device_factory=lambda: None, poll_seconds=0.01)
        port = server.server_address[1]
        held = [partial_request(port) for _ in range(web.MAX_CONNECTIONS)]
        wait_until(lambda: server.active == web.MAX_CONNECTIONS)
        board.publish([], 100.0, OBSERVED)  # měřicí smyčka zapisuje i při plném serveru
        started = time.monotonic()
        stop_web(server)
        self.assertLess(time.monotonic() - started, 1.0)
        board2, server2 = start_web(set(), 30, port=port, device_factory=lambda: None, poll_seconds=0.01)
        self.addCleanup(stop_web, server2)
        self.assertTrue(self.get(port, b"GET / HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % port)
                        .startswith(b"HTTP/1.0 200"))
        for connection in held:
            connection.close()


def closed_by_server_nowait(connection):
    """Neblokující kontrola, zda server spojení už zavřel."""
    connection.setblocking(False)
    try:
        return connection.recv(1024) == b""
    except BlockingIOError:
        return False
    except ConnectionError:
        return True
    finally:
        connection.setblocking(True)


class MainWebTest(unittest.TestCase):
    def test_web_flag_shares_the_loop_board_and_stops(self):
        started = []

        def fake_start(monitored, validity, port, access, tls=None):
            board = StateBoard(monitored, validity)
            server = mock.Mock(server_address=(access.bind, port))
            started.append((board, server, port))
            return board, server

        output = io.StringIO()
        with mock.patch("nexusstat.run.run_loop") as loop, mock.patch("nexusstat.run.start_web", fake_start), \
                mock.patch("nexusstat.run.stop_web") as stop, contextlib.redirect_stdout(output):
            main(["--web", "--web-port", "8766"])
        [(board, server, port)] = started
        self.assertEqual(port, 8766)
        self.assertIs(loop.call_args.kwargs["board"], board)  # web čte výsledky téže smyčky
        stop.assert_called_once_with(server)
        self.assertIn("http://127.0.0.1:8766/", output.getvalue())
        with mock.patch("nexusstat.run.run_loop") as loop, contextlib.redirect_stdout(io.StringIO()):
            main([])
        self.assertIsNone(loop.call_args.kwargs["board"])  # bez --web žádný server

    def test_history_source_follows_history_state(self):
        server = SimpleNamespace(server_address=(web.LOOPBACK, 8765), history=HISTORY_OFF)

        def start(link, config_text=None):
            server.history = HISTORY_OFF
            arguments = ["--web"]
            with tempfile.TemporaryDirectory() as directory:
                if config_text is not None:
                    path = Path(directory) / "config.toml"
                    path.write_text(config_text, encoding="utf-8")
                    arguments += ["--config", str(path)]
                with mock.patch("nexusstat.run.run_loop"), mock.patch("nexusstat.run.stop_web"), \
                        mock.patch("nexusstat.run.start_web", return_value=(StateBoard(), server)), \
                        mock.patch("nexusstat.run.start_history", return_value=link), \
                        contextlib.redirect_stdout(io.StringIO()):
                    main(arguments)
            return server.history

        self.assertEqual(start(None), HISTORY_OFF)  # výchozí: historie vypnutá, nic se neotevírá
        enabled = '[history]\nenabled = true\npath = "/nonexistent/history.sqlite3"\n'
        self.assertEqual(start(None, enabled), HISTORY_FAILED)  # zapnutá, ale nespustila se
        link = SimpleNamespace(path=Path("/nonexistent/history.sqlite3"), stop=lambda: None,
                               record_event=lambda *args, **kwargs: True)
        charts = start(link, enabled)
        self.assertIsInstance(charts, HistoryCharts)
        self.assertEqual((charts.path, charts.role, charts.interval_seconds, charts.reads),
                         (link.path, "development", 10, 0))  # při startu se nic nečte

    def test_lan_flags_are_explicit_and_need_web(self):
        captured = []

        def fake_start(monitored, validity, port, access, tls=None):
            captured.append(access)
            return StateBoard(), mock.Mock(server_address=(access.bind, port))

        output = io.StringIO()
        with mock.patch("nexusstat.run.run_loop"), mock.patch("nexusstat.run.start_web", fake_start), \
                mock.patch("nexusstat.run.stop_web"), contextlib.redirect_stdout(output):
            main(["--web", "--web-bind", "192.0.2.10", "--web-allow", "192.0.2.0/24", "--web-host", "nexus.lan"])
        [access] = captured
        self.assertEqual((access.bind, [str(n) for n in access.networks], access.host_names),
                         ("192.0.2.10", ["192.0.2.0/24"], ("nexus.lan",)))
        self.assertIn("bez přihlášení z 192.0.2.0/24", output.getvalue())
        for arguments in (["--web-bind", "192.0.2.10", "--web-allow", "192.0.2.0/24"],  # bez --web
                          ["--web", "--web-bind", "0.0.0.0", "--web-allow", "192.0.2.0/24"],
                          ["--web", "--web-bind", "192.0.2.10"]):
            with self.subTest(arguments=arguments), mock.patch("nexusstat.run.run_loop") as loop, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(arguments)
            loop.assert_not_called()

    def test_invalid_port_stops_before_measuring(self):
        for port in ("80", "70000", "0"):
            with self.subTest(port=port), mock.patch("nexusstat.run.run_loop") as loop, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["--web", "--web-port", port])
            loop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
