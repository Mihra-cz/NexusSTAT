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
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from nexusstat import web
from nexusstat.alarms import AlarmEvaluator
from nexusstat.config import AlarmsConfig, CollectorConfig
from nexusstat.core import CheckResult, Measurement, State
from nexusstat.fan_monitor import CPU_FAN_ID, FAN_ON, FAN_STATE, SOURCE, SOURCE_RPM
from nexusstat.mounts import ROOT_MODE_ID
from nexusstat.network_monitor import ADDRESS, INTERFACE, IPV4_ID, LINK, LINK_ID, LINK_UP, RX_RATE_ID, TX_RATE_ID
from nexusstat.run import main, run_loop
from nexusstat.services import ANIMEDB_ID, BACKUP_ID, NAS_ID
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID
from nexusstat.web import DashboardServer, StateBoard, is_loopback, render_page, start_web, stop_web

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


class MainWebTest(unittest.TestCase):
    def test_web_flag_shares_the_loop_board_and_stops(self):
        started = []

        def fake_start(monitored, validity, port):
            board = StateBoard(monitored, validity)
            server = mock.Mock(server_address=(web.LOOPBACK, port))
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

    def test_invalid_port_stops_before_measuring(self):
        for port in ("80", "70000", "0"):
            with self.subTest(port=port), mock.patch("nexusstat.run.run_loop") as loop, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["--web", "--web-port", port])
            loop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
