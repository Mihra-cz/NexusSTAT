"""Testy odchozího heartbeatu (M6-5).

Syntetické výsledky a simulovaný čas plánu; síť jen přes lokální náhradu
přijímače na 127.0.0.1 s testovacím certifikátem z openssl v dočasném
adresáři. Do internetu se nic neposílá: jinde se spojení podstrkuje.
"""

import contextlib
import hashlib
import hmac
import io
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from nexusstat import heartbeat, rgb
from nexusstat.alarms import AlarmEvaluator
from nexusstat.config import AlarmsConfig, CollectorConfig, ConfigError, HeartbeatConfig, load_config
from nexusstat.core import CheckResult, State, overall_state
from nexusstat.heartbeat import (Heartbeat, HeartbeatError, client_context, encode_report, load_secret,
                                 public_state, sign)
from nexusstat.network_monitor import ADDRESS, INTERFACE, IPV4_ID
from nexusstat.run import Stopper, main, run_loop
from nexusstat.services import BACKUP_ID, NAS_ID, NEXUS_SERVICES_ID, complete_service_results
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID
from nexusstat.user_state import Acknowledge, UserState

UTC = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
TS = int(UTC.timestamp())
SECRET = b"syntetick-tajemstvi-jen-pro-testy-0123456789"
MONITORED = {CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID, DISK_ROOT_ID}
OPENSSL = shutil.which("openssl")


def result(check_id, state, expires=10_000.0, simulated=False):
    return CheckResult(check_id, state, "popis 192.0.2.10 eth0", UTC, expires, simulated=simulated,
                       details={INTERFACE: "eth0", ADDRESS: "192.0.2.10"})


def healthy():
    return [result(check_id, State.INFO) for check_id in sorted(MONITORED)]


def refused(address, timeout):
    raise ConnectionRefusedError(111, "Connection refused")


def make_certificate(directory, names="IP:127.0.0.1,DNS:localhost"):
    """Testovací certifikát (jen v dočasném adresáři); stejný postup jako v test_admin."""
    cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=heartbeat-test",
                    "-addext", f"subjectAltName={names}"], check=True, capture_output=True, timeout=30)
    return cert, key


class PublicStateTest(unittest.TestCase):
    def state(self, results, monitored=MONITORED, now=50.0):
        return public_state(results, now, UTC, monitored, 30)

    def test_healthy_with_unimplemented_nexus_is_ok(self):
        self.assertEqual(self.state(healthy()), "OK")
        # Lokální web a OLED počítají s doplněným NEXUS (UNKNOWN); rozdíl je jen tento a je dokumentovaný.
        local = overall_state(complete_service_results(healthy(), UTC, 100.0, monitored=MONITORED), 50.0)
        self.assertEqual(local, State.UNKNOWN)
        # Skutečný výsledek NEXUS (budoucí kolektor) se už započítá.
        self.assertEqual(self.state(healthy() + [result(NEXUS_SERVICES_ID, State.WARNING)]), "WARNING")

    def test_mapping(self):
        cases = {
            "zapnutý NAS bez výsledku": (healthy(), MONITORED | {NAS_ID}, "UNKNOWN"),
            "vypnuté služby N/A": (healthy() + [result(BACKUP_ID, State.NA)], MONITORED, "OK"),
            "prošlý výsledek": (healthy()[:-1] + [result(MEMORY_ID, State.INFO, expires=40.0)], MONITORED, "UNKNOWN"),
            "ERROR a UNKNOWN": (healthy() + [result(NAS_ID, State.ERROR), result(BACKUP_ID, State.UNKNOWN)],
                                MONITORED | {NAS_ID, BACKUP_ID}, "ERROR"),
            "CRITICAL nad vším": (healthy() + [result(NAS_ID, State.CRITICAL), result(BACKUP_ID, State.ERROR)],
                                  MONITORED | {NAS_ID, BACKUP_ID}, "CRITICAL"),
            "WARNING nad UNKNOWN": ([result(CPU_USAGE_ID, State.UNKNOWN), result(NAS_ID, State.WARNING)],
                                    {CPU_USAGE_ID, NAS_ID}, "WARNING"),
            "UNKNOWN nad OK": (healthy()[:-1] + [result(MEMORY_ID, State.UNKNOWN), result(NAS_ID, State.OK)],
                               MONITORED | {NAS_ID}, "UNKNOWN"),
            "INFO a OK": (healthy() + [result(NAS_ID, State.OK)], MONITORED | {NAS_ID}, "OK"),
            "žádná sledovaná data": ([], set(), "UNKNOWN"),
            "jen N/A": ([result(BACKUP_ID, State.NA)], set(), "UNKNOWN"),
            "sledovaná kontrola bez výsledku": (healthy()[1:], MONITORED, "UNKNOWN"),
            "simulovaná data": ([result(check_id, State.OK, simulated=True) for check_id in MONITORED], MONITORED,
                                "UNKNOWN"),
        }
        for name, (results, monitored, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.state(results, monitored), expected)

    def test_acknowledged_error_stays_error(self):
        results = healthy() + [result(NAS_ID, State.ERROR)]
        owner = UserState(output=lambda line: None)
        owner.request(Acknowledge(NAS_ID, State.ERROR))
        owner.tick(results, 50.0, UTC)
        self.assertEqual(rgb.rgb_signal(results, owner.acks, False, False, 50.0).kind, rgb.OFF)  # potvrzeno
        self.assertEqual(self.state(results, MONITORED | {NAS_ID}), "ERROR")  # veřejně beze změny


class ReportTest(unittest.TestCase):
    def test_exact_body_and_signature(self):
        body = encode_report("OK", TS)
        self.assertEqual(body, b'{"v":1,"state":"OK","ts":%d}' % TS)
        self.assertEqual(sign(SECRET, body), "v1=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest())
        for state, ts in (("INFO", TS), ("ok", TS), ("NO DATA", TS), ("OK", 0), ("OK", True), ("OK", 1.5)):
            with self.subTest(state=state, ts=ts), self.assertRaises(ValueError):
                encode_report(state, ts)

    def test_secret_file_is_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.secret"
            path.write_bytes(SECRET + b"\n")
            os.chmod(path, 0o600)
            self.assertEqual(load_secret(path), SECRET)
            cases = {"krátké": b"x" * 31, "mezera": b"x" * 20 + b" " + b"x" * 20, "diakritika": "ž".encode() * 20}
            for name, content in cases.items():
                with self.subTest(name):
                    path.write_bytes(content)
                    with self.assertRaises(HeartbeatError):
                        load_secret(path)
            path.write_bytes(SECRET)
            os.chmod(path, 0o640)
            with self.assertRaisesRegex(HeartbeatError, "chmod 600"):
                load_secret(path)
            os.chmod(path, 0o600)
            link = Path(directory) / "link.secret"
            link.symlink_to(path)
            with self.assertRaisesRegex(HeartbeatError, "nelze otevřít"):
                load_secret(link)
            with mock.patch("nexusstat.heartbeat.os.geteuid", return_value=os.geteuid() + 1), \
                    self.assertRaises(HeartbeatError):
                load_secret(path)

    def test_config_section(self):
        self.assertEqual(load_config_text(""), HeartbeatConfig())
        good = '[heartbeat]\nenabled = true\nurl = "https://status.example.invalid/hb"\nsecret_file = "/x/hb.secret"\n'
        self.assertTrue(load_config_text(good).enabled)
        for text in ('[heartbeat]\nenabled = true\n',
                     '[heartbeat]\nenabled = true\nurl = "http://status.example.invalid/hb"\nsecret_file = "/x/s"\n',
                     '[heartbeat]\nurl = "https://user:pw@status.example.invalid/hb"\n',
                     '[heartbeat]\nurl = "https://status.example.invalid/hb?token=1"\n',
                     '[heartbeat]\nurl = "https://status.example.invalid"\n',
                     '[heartbeat]\nsecret_file = "hb.secret"\n',
                     '[heartbeat]\nenabled = "yes"\n',
                     '[heartbeat]\ninterval_seconds = 60\n'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_config_text(text)


def load_config_text(text):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path).heartbeat


class Blocking:
    """Podstrčené spojení, které visí (např. pomalé DNS), dokud ho test nepustí."""

    def __init__(self):
        self.entered, self.release, self.calls = threading.Event(), threading.Event(), 0

    def __call__(self, address, timeout):
        self.calls += 1
        self.entered.set()
        self.release.wait(10)
        raise TimeoutError("timed out")


class SlowResolver:
    """Pomalé DNS: překlad visí, dokud ho test nepustí; pak se skutečně připojí k lokálnímu přijímači."""

    def __init__(self, port):
        self.port, self.entered, self.release, self.calls = port, threading.Event(), threading.Event(), 0

    def __call__(self, address, timeout):
        self.calls += 1
        self.entered.set()
        self.release.wait(10)
        return socket.create_connection(("127.0.0.1", self.port), timeout=timeout)


class Clock:
    def __init__(self, now=5000.0):
        self.now = now

    def __call__(self):
        return self.now


class ScheduleTest(unittest.TestCase):
    def make(self, connect=refused):
        self.lines = []
        beat = Heartbeat("https://status.example.invalid/hb", SECRET, client_context(), MONITORED, 30,
                         output=self.lines.append, connect=connect)
        self.addCleanup(beat.close)
        return beat

    def run_ticks(self, beat, seconds, start=1000.0):
        for second in seconds:
            beat.tick(healthy(), start + second, UTC + timedelta(seconds=second))
            self.assertTrue(beat.wait_idle(5))

    def test_first_after_60_s_then_every_300_s_without_catch_up_or_retries(self):
        beat = self.make()
        self.run_ticks(beat, range(0, 1000))
        self.assertEqual(beat.attempts, 4)  # 60, 360, 660, 960 s; selhání se neopakuje
        self.run_ticks(beat, [1000, 3000, 3001])  # pozastavený proces: jedno hlášení, nic se nedohání
        self.assertEqual(beat.attempts, 5)
        self.run_ticks(beat, range(3002, 3060))
        self.assertEqual(beat.attempts, 5)
        self.run_ticks(beat, [3060, 3061])  # mřížka 60 + k × 300 s od startu pokračuje
        self.assertEqual((beat.attempts, beat.failures), (6, 6))
        self.assertEqual(len([line for line in self.lines if "nedoručeno" in line]), 1)  # jeden řádek za řadu

    def test_stuck_transfer_is_never_duplicated_and_never_blocks_the_loop(self):
        stuck = Blocking()
        beat = self.make(stuck)
        threads = threading.active_count()
        self.run_ticks_without_wait(beat, [0, 60])
        self.assertTrue(stuck.entered.wait(5))
        started = time.monotonic()
        self.run_ticks_without_wait(beat, range(61, 1000))
        self.assertLess(time.monotonic() - started, 1)  # smyčka nečeká na DNS ani spojení
        self.assertEqual((stuck.calls, beat.skipped), (1, 3))  # 360, 660 a 960 s vynechány, žádná fronta
        self.assertLessEqual(threading.active_count(), threads)  # žádná další vlákna
        self.assertEqual(len([line for line in self.lines if "vynecháno" in line]), 3)
        stuck.release.set()
        self.assertTrue(beat.wait_idle(5))
        self.run_ticks(beat, [1000, 1260])
        self.assertEqual(stuck.calls, 2)

    def run_ticks_without_wait(self, beat, seconds, start=1000.0):
        for second in seconds:
            beat.tick(healthy(), start + second, UTC)

    def test_close_returns_even_while_stuck(self):
        stuck = Blocking()
        beat = self.make(stuck)
        self.run_ticks_without_wait(beat, [0, 60])
        self.assertTrue(stuck.entered.wait(5))
        started = time.monotonic()
        beat.close(timeout=0.5)
        self.assertLess(time.monotonic() - started, 2)
        stuck.release.set()
        beat._thread.join(5)
        self.assertFalse(beat._thread.is_alive())  # po uvolnění vlákno skončí, nic dalšího nezačne
        self.assertEqual(stuck.calls, 1)


class Receiver(ThreadingHTTPServer):
    """Lokální náhrada budoucího přijímače na 127.0.0.1 (HTTPS, chování podle `mode`)."""

    daemon_threads = True

    def __init__(self, context, mode="204"):
        super().__init__(("127.0.0.1", 0), ReceiverHandler)
        self.socket = context.wrap_socket(self.socket, server_side=True)
        self.mode, self.requests, self.release = mode, [], threading.Event()

    def handle_error(self, request, client_address):
        pass  # odmítnutý handshake nebo klient ukončený limitem není chyba testu


class ReceiverHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append((self.path, dict(self.headers), body))
        mode = self.server.mode
        if mode == "silent":
            self.server.release.wait(10)
            return
        if mode == "trickle":  # odpověď po bajtech: každá operace v limitu, celek nikdy
            with contextlib.suppress(OSError):
                for byte in b"HTTP/1.1 204 No Content\r\n" + b"X: " + b"y" * 10_000:
                    if self.server.release.is_set():
                        return
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.05)
            return
        status = int(mode)
        self.send_response(status)
        if status == 302:
            self.send_header("Location", "https://127.0.0.1:1/jinde")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass


@unittest.skipUnless(OPENSSL, "testovací certifikát vyžaduje openssl")
class HttpsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.cert, cls.key = make_certificate(cls.directory.name)
        other = Path(cls.directory.name) / "jiný"
        other.mkdir()
        cls.other_cert, cls.other_key = make_certificate(other, "DNS:jiny.test")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def receiver(self, mode="204", cert=None, key=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert or self.cert, key or self.key)
        server = Receiver(context, mode)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.release.set)
        return server

    def send(self, server_or_port, *, ca=True, operation=5, transfer=15, results=None):
        port = server_or_port if isinstance(server_or_port, int) else server_or_port.server_address[1]
        self.lines = []
        beat = Heartbeat(f"https://127.0.0.1:{port}/nexusstat/heartbeat", SECRET,
                         client_context(str(self.cert) if ca else None), MONITORED | {IPV4_ID}, 30,
                         output=self.lines.append, operation_seconds=operation, transfer_seconds=transfer)
        self.addCleanup(beat.close)
        beat.tick(results or healthy(), 1000.0, UTC)
        beat.tick(results or healthy(), 1060.0, UTC)  # termín prvního hlášení
        started = time.monotonic()
        while not beat.wait_idle(0.05):
            beat.tick(results or healthy(), 1061.0, UTC)  # smyčka tiká dál a hlídá celkový termín
            self.assertLess(time.monotonic() - started, 10)
        beat.tick(results or healthy(), 1062.0, UTC)  # výpis výsledku
        return beat, time.monotonic() - started

    def slow(self, server, **options):
        resolver = SlowResolver(server.server_address[1])
        self.lines = []
        beat = Heartbeat(f"https://127.0.0.1:{server.server_address[1]}/hb", SECRET, client_context(str(self.cert)),
                         MONITORED, 30, output=self.lines.append, connect=resolver, **options)
        self.addCleanup(resolver.release.set)
        self.addCleanup(beat.close, 0.2)
        beat.tick(healthy(), 1000.0, UTC)
        beat.tick(healthy(), 1060.0, UTC)  # první hlášení: vlákno visí v DNS
        self.assertTrue(resolver.entered.wait(5))
        return beat, resolver

    def test_dns_longer_than_the_transfer_limit_never_sends_late(self):
        server = self.receiver()
        clock = Clock()
        beat, resolver = self.slow(server, clock=clock)
        threads = threading.active_count()
        clock.now += 16  # DNS trvá déle než celkový limit 15 s
        for second in range(1061, 1700):  # smyčka tiká dál; termíny 1360 a 1660 se vynechají
            beat.tick(healthy(), float(second), UTC)
        self.assertEqual((beat.skipped, resolver.calls), (2, 1))
        self.assertLessEqual(threading.active_count(), threads)  # žádné další vlákno ani fronta
        resolver.release.set()  # DNS konečně odpoví
        self.assertTrue(beat.wait_idle(5))
        beat.tick(healthy(), 1700.0, UTC)
        self.assertEqual(server.requests, [])  # po limitu se hlášení už neodešle
        self.assertEqual((beat.attempts, beat.failures), (1, 1))
        self.assertIn("celkový limit", self.lines[-1])
        beat.tick(healthy(), 1960.0, UTC)  # další termín na mřížce: nový pokus, žádné dohánění
        self.assertTrue(beat.wait_idle(5))
        self.assertEqual(resolver.calls, 2)

    def test_shutdown_during_slow_dns_is_bounded_and_never_sends(self):
        server = self.receiver()
        beat, resolver = self.slow(server)
        started = time.monotonic()
        beat.close(timeout=0.3)
        self.assertLess(time.monotonic() - started, 1.5)  # ukončení na DNS nečeká
        resolver.release.set()  # DNS odpoví až po ukončení
        beat._thread.join(5)
        self.assertFalse(beat._thread.is_alive())
        self.assertEqual((server.requests, resolver.calls), ([], 1))  # nic se dodatečně neodeslalo
        beat.tick(healthy(), 2000.0, UTC)  # ani další takt po ukončení nic nespustí
        self.assertEqual(resolver.calls, 1)

    def test_delivered_with_exact_body_signature_and_nothing_internal(self):
        server = self.receiver()
        with_details = healthy() + [result(IPV4_ID, State.INFO)]
        beat, _ = self.send(server, results=with_details)
        self.assertEqual((beat.delivered, beat.failures), (1, 0))
        [(path, headers, body)] = server.requests
        self.assertEqual(path, "/nexusstat/heartbeat")
        self.assertEqual(json.loads(body), {"v": 1, "state": "OK", "ts": TS})
        self.assertEqual(body, encode_report("OK", TS))
        self.assertEqual(headers["X-NexusSTAT-Signature"], sign(SECRET, body))
        self.assertEqual(set(headers), {"Host", "Accept-Encoding", "Content-Length", "Content-Type",
                                        "X-NexusSTAT-Signature", "Connection"})
        sent = body + repr(headers).encode()
        for internal in (b"192.0.2", b"eth0", b"service", b"system", b"popis", SECRET):
            self.assertNotIn(internal, sent)
        self.assertEqual(self.lines, ["Heartbeat: první hlášení doručeno."])

    def test_proxy_settings_from_the_environment_are_ignored(self):
        server = self.receiver()
        proxy = "http://127.0.0.1:9"  # neexistující proxy: kdyby se použila, přenos by selhal
        names = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy")
        with mock.patch.dict(os.environ, {name: proxy for name in names}):
            beat, _ = self.send(server)
        self.assertEqual((beat.delivered, len(server.requests)), (1, 1))  # přímo na přijímač

    def test_refused_answers_and_tls_problems_are_single_failures(self):
        cases = [
            ("HTTP 500", dict(server=self.receiver("500")), 1),
            ("HTTP 302", dict(server=self.receiver("302")), 1),  # přesměrování se nenásleduje
            ("certifikát serveru neověřen", dict(server=self.receiver(), ca=False), 0),
            ("certifikát serveru neověřen", dict(server=self.receiver(cert=self.other_cert, key=self.other_key)), 0),
        ]
        for reason, options, requests in cases:
            with self.subTest(reason=reason):
                server = options.pop("server")
                beat, _ = self.send(server, **options)
                self.assertEqual((beat.attempts, beat.delivered, beat.failures), (1, 0, 1))
                self.assertEqual(len(server.requests), requests)
                self.assertIn(reason, self.lines[0])

    def test_unreachable_receiver(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()  # port je volný: nikdo nenaslouchá
        beat, _ = self.send(port)
        self.assertEqual((beat.attempts, beat.failures), (1, 1))
        self.assertIn("spojení", self.lines[0])

    def test_silent_receiver_times_out(self):
        beat, elapsed = self.send(self.receiver("silent"), operation=0.3)
        self.assertIn("timeout", self.lines[0])
        self.assertLess(elapsed, 3)

    def test_trickling_receiver_is_ended_by_the_total_limit(self):
        # Každý bajt přijde do 0,05 s, takže limit operace (2 s) nikdy nevyprší; celek ukončí smyčka.
        beat, elapsed = self.send(self.receiver("trickle"), operation=2, transfer=0.5)
        self.assertIn("celkový limit", self.lines[0])
        self.assertLess(elapsed, 3)
        self.assertEqual(beat.failures, 1)


class LoopTest(unittest.TestCase):
    def test_collector_schedule_is_unchanged(self):
        class Collector:
            labels = {CPU_USAGE_ID: "CPU"}

            def __init__(self):
                self.calls = []

            def collect(self, now, observed_utc):
                self.calls.append(now)
                return [CheckResult(CPU_USAGE_ID, State.INFO, "CPU", observed_utc, now + 15, simulated=False)]

        def run(beat=None):
            collector, clock = Collector(), [100.0]

            def sleep(seconds):
                clock[0] += seconds
                if beat is not None:
                    beat.wait_idle(5)  # přenos skončí dřív, než smyčka pokračuje (deterministický počet)
                if clock[0] >= 1000:
                    raise KeyboardInterrupt

            results = run_loop(collector, CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=lambda: clock[0],
                               utc_clock=lambda: UTC + timedelta(seconds=clock[0]), sleep=sleep,
                               output=lambda line: None, tick_seconds=1, heartbeat=beat)
            return collector.calls, [(result.id, result.state) for result in results]

        beat = Heartbeat("https://status.example.invalid/hb", SECRET, client_context(), {CPU_USAGE_ID}, 30,
                         output=lambda line: None, connect=refused)
        self.addCleanup(beat.close)
        # Takty kolektorů i výsledky stejné s heartbeatem i bez něj: nedostupný přijímač nepřidá žádný alarm.
        self.assertEqual(run(beat), run())
        self.assertEqual((beat.attempts, beat.failures), (3, 3))  # 160, 460 a 760 s od startu ve 100 s


class SignalTest(unittest.TestCase):
    def test_sigterm_ends_the_loop_while_dns_hangs(self):
        # Skutečný SIGTERM (Stopper jako v main) při visícím DNS; čas smyčky je simulovaný.
        stopper = Stopper()
        self.assertTrue(stopper.install())
        self.addCleanup(stopper.restore)
        stuck = Blocking()
        self.addCleanup(stuck.release.set)
        beat = Heartbeat("https://status.example.invalid/hb", SECRET, client_context(), {CPU_USAGE_ID}, 30,
                         output=lambda line: None, connect=stuck)
        calls, clock, lines = [], [100.0], []

        class Collector:
            labels = {CPU_USAGE_ID: "CPU"}

            def collect(self, now, observed_utc):
                calls.append(now)
                return [CheckResult(CPU_USAGE_ID, State.INFO, "CPU", observed_utc, now + 15, simulated=False)]

        def sleep(seconds):
            clock[0] += seconds
            if clock[0] == 161.0:
                self.assertTrue(stuck.entered.wait(5))  # vlákno přenosu visí v DNS od taktu 160 s
            if clock[0] >= 1000:
                os.kill(os.getpid(), signal.SIGTERM)  # jako systemd nebo kill

        run_loop(Collector(), CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=lambda: clock[0],
                 utc_clock=lambda: UTC, sleep=sleep, output=lines.append, stopper=stopper, tick_seconds=1,
                 heartbeat=beat)
        self.assertEqual(len(calls), 900)  # smyčka měřila dál po celou dobu visícího DNS
        self.assertTrue(lines[-1].endswith("Ukončeno (SIGTERM), počet měření: 900."))
        self.assertEqual((stuck.calls, beat.skipped), (1, 2))  # 460 a 760 s vynechány
        started = time.monotonic()
        beat.close(timeout=0.3)  # main volá close() s limitem 2 s
        self.assertLess(time.monotonic() - started, 1.5)
        stuck.release.set()
        beat._thread.join(5)
        self.assertEqual(stuck.calls, 1)  # přeživší vlákno už nic dalšího nezkouší


class MainTest(unittest.TestCase):
    def run_main(self, config_text, secret=SECRET, mode=0o600):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "heartbeat.secret"
            secret_path.write_bytes(secret)
            os.chmod(secret_path, mode)
            path = Path(directory) / "config.toml"
            path.write_text(config_text.replace("SECRET", str(secret_path)), encoding="utf-8")
            output = io.StringIO()
            with mock.patch("nexusstat.run.run_loop") as loop, contextlib.redirect_stdout(output):
                main(["--config", str(path)])
        return loop.call_args.kwargs["heartbeat"], output.getvalue()

    def test_enabled_only_explicitly_and_fail_closed(self):
        beat, output = self.run_main("")
        self.assertIsNone(beat)  # výchozí: vypnuto, nic se nevytváří
        self.assertNotIn("Heartbeat", output)
        enabled = '[heartbeat]\nenabled = true\nurl = "https://status.example.invalid/hb"\nsecret_file = "SECRET"\n'
        beat, output = self.run_main(enabled)
        self.assertIsInstance(beat, Heartbeat)
        self.assertFalse(beat._thread.is_alive())  # main ho při ukončení zavřel
        self.assertEqual(beat.attempts, 0)  # smyčka neběžela, nic se neposlalo
        self.assertIn("Heartbeat: https://status.example.invalid:443/hb", output)
        self.assertNotIn(SECRET.decode(), output)
        for secret, mode in ((SECRET, 0o644), (b"kratke", 0o600)):
            with self.subTest(mode=oct(mode)), mock.patch("nexusstat.run.run_loop") as loop, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_main(enabled, secret, mode)


if __name__ == "__main__":
    unittest.main()
