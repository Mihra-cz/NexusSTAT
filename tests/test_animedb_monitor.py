"""Testy healthchecku AnimeDB přes HTTP /health (SERVICES-1) nad syntetickými odpověďmi.

Žádný test neotevírá síťové spojení: socket nahrazuje FakeNetwork se syntetickou
odpovědí HTTP a čas je simulovaný (monotónní sekundy jako čísla), nic se nečeká.
"""

import errno
import io
import itertools
import os
import socket
import unittest
from datetime import datetime, timezone
from unittest import mock

from nexusstat import __version__, rgb
from nexusstat.alarms import AlarmEvaluator
from nexusstat.animedb_monitor import CAUSE, DOWN, HEALTH, INVALID, MAX_BODY_BYTES, UP, AnimeDbMonitor
from nexusstat.config import (DEFAULT_CLEAR_COUNT, DEFAULT_CONFIRM_COUNT, AlarmRule, AlarmsConfig, AnimeDbConfig,
                              CollectorConfig, Config, ServicesConfig)
from nexusstat.core import CheckResult, State, active_problems, effective
from nexusstat.oled import ALERT_SYMBOL, UNKNOWN_SYMBOL, home_symbol
from nexusstat.oled_text import service_rows, status_texts
from nexusstat.preview import LIVE, PreviewSession, SessionClock
from nexusstat.run import COLLECTOR_FAILURE, Collectors, live_monitor, run_loop
from nexusstat.services import ANIMEDB_ID, complete_service_results
from nexusstat.system_monitor import LABELS as SYSTEM_LABELS

OBSERVED = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
URL = "http://192.0.2.10:8080/health"  # dokumentační rozsah RFC 5737, ne skutečná adresa
TIMEOUT = 2
NOW = 1000.0
VALIDITY = 30
INTERVAL = 10


def http(status=200, body=b'{"status":"ok"}', reason="OK", headers=None):
    """Syntetická odpověď HTTP/1.1 s Content-Length (pokud není v `headers` jinak)."""
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body)), **(headers or {})}
    head = f"HTTP/1.1 {status} {reason}\r\n" + "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return head.encode() + b"\r\n" + body


HEALTHY = http()
REFUSED = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")


class FakeClock:
    """Monotónní hodiny pod kontrolou testu."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSocket:
    """Socket se syntetickou odpovědí: zaznamená požadavek a vrací data po kouscích."""

    def __init__(self, data, chunk=1024, error=None, clock=None, recv_seconds=0):
        self.data, self.chunk, self.error = data, chunk, error
        self.clock, self.recv_seconds = clock, recv_seconds
        self.sent = b""
        self.timeouts = []
        self.recvs = 0
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data):
        self.sent += data

    def recv_into(self, buffer):
        self.recvs += 1
        if self.clock is not None:
            self.clock.advance(self.recv_seconds)  # pomalý server: každé čtení ubere čas
        if self.error is not None:
            raise self.error
        size = min(len(buffer), len(self.data), self.chunk)
        buffer[:size] = self.data[:size]
        self.data = self.data[size:]
        return size

    def close(self):
        self.closed = True


class TimedSocket:
    """Řízený socket, který dodržuje nastavený timeout jako skutečné jádro.

    `segments` jsou dvojice (sekundy čekání, bajty), klidně nekonečný iterátor.
    Každé recv počká danou dobu (posune hodiny) a vrátí další úsek; kdyby čekání
    trvalo déle než nastavený timeout, posune hodiny jen o timeout a vyvolá
    socket.timeout. Stejně se chová spojení (`connect`). Na rozdíl od FakeSocket
    tak ukáže, kolik času kontrola opravdu stráví, i když server posílá data
    pomalu po kouscích. `served` počítá bajty, které si kolektor přečetl.
    """

    # Pojistky: kontrola bez celkového limitu nebo bez limitu velikosti by jinak
    # nad nekonečným proudem dat nikdy neskončila.
    GUARD_SECONDS = 100
    GUARD_BYTES = 1_000_000

    def __init__(self, clock, segments, connect_seconds=0):
        self.clock = clock
        self.segments = iter(segments)
        self.connect_seconds = connect_seconds
        self.timeout = None
        self.timeouts = []
        self.pending = b""
        self.served = 0
        self.connected = False
        self.closed = False

    def connect(self, address, timeout=None):
        """Náhrada socket.create_connection: spojení trvá `connect_seconds`."""
        self.timeouts.append(timeout)
        self.wait(self.connect_seconds, timeout)
        self.connected = True
        return self

    def wait(self, seconds, timeout):
        if self.clock.now > self.GUARD_SECONDS:
            raise AssertionError("kontrola nemá celkový časový limit")
        if timeout is not None and seconds > timeout:
            self.clock.advance(timeout)
            raise socket.timeout("timed out")
        self.clock.advance(seconds)

    def settimeout(self, value):
        self.timeout = value
        self.timeouts.append(value)

    def sendall(self, data):
        pass

    def recv_into(self, buffer):
        if not self.pending:
            seconds, self.pending = next(self.segments, (0, b""))  # konec dat = server zavřel spojení
            self.wait(seconds, self.timeout)
        size = min(len(buffer), len(self.pending))
        buffer[:size] = self.pending[:size]
        self.pending = self.pending[size:]
        self.served += size
        if self.served > self.GUARD_BYTES:
            raise AssertionError("kontrola nemá limit velikosti odpovědi")
        return size

    def close(self):
        self.closed = True


def timed_check(segments, connect_seconds=0):
    """(výsledek, doba kontroly v s, socket) jedné kontroly nad řízeným socketem."""
    clock = FakeClock()
    sock = TimedSocket(clock, segments, connect_seconds)
    [result] = AnimeDbMonitor(VALIDITY, URL, TIMEOUT, connect=sock.connect, clock=clock).collect(NOW, OBSERVED)
    return result, clock.now, sock


def slowly(data, seconds, size=1):
    """Úseky dat po `size` bajtech, každý po `seconds` sekundách."""
    return [(seconds, data[index:index + size]) for index in range(0, len(data), size)]


HEALTHY_HEAD, HEALTHY_BODY = HEALTHY.split(b"\r\n\r\n", 1)
HEALTHY_HEAD += b"\r\n\r\n"
CHUNKED_HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
CLOSE_HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n"


def chunk(data):
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


class FakeNetwork:
    """Náhrada socket.create_connection s připraveným scénářem kontrol.

    Každý krok je odpověď HTTP (bytes), výjimka vyvolaná při spojení, nebo
    hotový FakeSocket. Po posledním kroku se opakuje poslední.
    """

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []
        self.sockets = []

    def __call__(self, address, timeout=None):
        self.calls.append((address, timeout))
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            raise step
        sock = step if isinstance(step, FakeSocket) else FakeSocket(step)
        self.sockets.append(sock)
        return sock


class FakeSystem:
    """Skutečně vypadající systémová měření bez čtení /proc (pro skládání kolektorů)."""

    simulated = False
    labels = SYSTEM_LABELS

    def __init__(self, validity_seconds=VALIDITY):
        self.validity_seconds = validity_seconds

    def collect(self, now, observed_utc):
        return [CheckResult(check_id, State.INFO, label, observed_utc, now + self.validity_seconds, simulated=False)
                for check_id, label in SYSTEM_LABELS.items()]


def monitor_for(network, url=URL, clock=None):
    return AnimeDbMonitor(VALIDITY, url, TIMEOUT, connect=network, clock=clock or FakeClock())


def check_once(step):
    [result] = monitor_for(FakeNetwork(step)).collect(NOW, OBSERVED)
    return result


def run_checks(*steps):
    """Výsledky po sobě jdoucích kontrol v intervalu měření (jedna kontrola na krok)."""
    monitor = monitor_for(FakeNetwork(*steps))
    return [monitor.collect(NOW + INTERVAL * index, OBSERVED)[0] for index in range(len(steps))]


class RequestTest(unittest.TestCase):
    def test_single_get_to_configured_address(self):
        network = FakeNetwork(HEALTHY)
        monitor_for(network).collect(NOW, OBSERVED)
        self.assertEqual(network.calls, [(("192.0.2.10", 8080), TIMEOUT)])
        [sock] = network.sockets
        self.assertEqual(sock.sent, (f"GET /health HTTP/1.1\r\nHost: 192.0.2.10:8080\r\nAccept: application/json\r\n"
                                     f"User-Agent: NexusSTAT/{__version__}\r\nConnection: close\r\n\r\n").encode())
        self.assertTrue(sock.closed)
        self.assertTrue(all(0 < value <= TIMEOUT for value in sock.timeouts))

    def test_ipv6_address_and_default_port(self):
        network = FakeNetwork(HEALTHY)
        monitor_for(network, url="http://[::1]/api/health").collect(NOW, OBSERVED)
        self.assertEqual(network.calls[0][0], ("::1", 80))
        self.assertIn(b"GET /api/health HTTP/1.1\r\nHost: [::1]\r\n", network.sockets[0].sent)

    def test_proxy_from_environment_is_ignored(self):
        proxy = "http://203.0.113.1:3128"
        network = FakeNetwork(HEALTHY)
        with mock.patch.dict(os.environ, {"http_proxy": proxy, "HTTP_PROXY": proxy, "all_proxy": proxy}):
            result = monitor_for(network).collect(NOW, OBSERVED)[0]
        self.assertEqual([address for address, _ in network.calls], [("192.0.2.10", 8080)])
        self.assertIs(result.state, State.OK)

    def test_redirect_is_never_followed(self):
        network = FakeNetwork(http(302, b"", "Found", {"Location": "http://203.0.113.5/health"}))
        result = monitor_for(network).collect(NOW, OBSERVED)[0]
        self.assertEqual(len(network.calls), 1)  # jediné spojení, na cizí adresu nic
        self.assertIs(result.state, State.UNKNOWN)
        self.assertEqual(dict(result.details), {HEALTH: INVALID, CAUSE: "http_302"})

    def test_whole_check_has_one_deadline(self):
        # Pomalý server posílá po bajtu a každé čtení trvá 0,3 s: bez celkového
        # termínu by kontrola trvala minuty, takto skončí po 2 s jako timeout.
        clock = FakeClock()
        slow = FakeSocket(HEALTHY, chunk=1, clock=clock, recv_seconds=0.3)
        result = monitor_for(FakeNetwork(slow), clock=clock).collect(NOW, OBSERVED)[0]
        self.assertEqual(dict(result.details), {HEALTH: DOWN, CAUSE: "timeout"})
        self.assertLessEqual(slow.recvs, TIMEOUT / 0.3 + 1)
        self.assertLessEqual(clock.now, TIMEOUT + 0.3)
        # Každé čtení dostalo jen zbývající čas, ne znovu celý limit.
        self.assertEqual(slow.timeouts, sorted(slow.timeouts, reverse=True))
        self.assertTrue(all(0 < value <= TIMEOUT for value in slow.timeouts))
        self.assertTrue(slow.closed)

    def test_connect_gets_the_configured_limit(self):
        network = FakeNetwork(socket.timeout("timed out"))
        result = monitor_for(network).collect(NOW, OBSERVED)[0]
        self.assertEqual(network.calls, [(("192.0.2.10", 8080), TIMEOUT)])
        self.assertEqual(dict(result.details), {HEALTH: DOWN, CAUSE: "timeout"})

    def test_invalid_constructor_arguments(self):
        for url, timeout, validity in (("https://192.0.2.10/health", 2, 30), ("http://animedb.lan/health", 2, 30),
                                       (URL, 0, 30), (URL, 6, 30), (URL, True, 30), (URL, 2, 0), (None, 2, 30)):
            with self.subTest(url=url, timeout=timeout, validity=validity):
                with self.assertRaises(ValueError):
                    AnimeDbMonitor(validity, url, timeout, connect=FakeNetwork(HEALTHY))

    def test_real_connect_is_real_data_and_fake_is_simulated(self):
        monitor = AnimeDbMonitor(VALIDITY, URL, TIMEOUT)  # vytvoření se nikam nepřipojuje
        self.assertIs(monitor.connect, socket.create_connection)
        self.assertFalse(monitor.simulated)
        self.assertTrue(check_once(HEALTHY).simulated)


class TotalTimeoutTest(unittest.TestCase):
    """timeout_seconds omezuje celou kontrolu, ne jen jednotlivé blokující operace.

    Každý úsek dat by sám o sobě limit 2 s nepřekročil; bez celkového termínu
    by kontrola trvala desítky sekund nebo nikdy neskončila (pojistka TimedSocket).
    """

    def assert_timeout(self, segments, connect_seconds=0):
        result, elapsed, sock = timed_check(segments, connect_seconds)
        self.assertIs(result.state, State.UNKNOWN)  # první kontrola: výpadek zatím nepotvrzen
        self.assertEqual(dict(result.details), {HEALTH: DOWN, CAUSE: "timeout"})
        self.assertLessEqual(elapsed, TIMEOUT + 1e-9)
        # Žádná operace nedostala víc času, než zbýval do termínu.
        self.assertTrue(all(0 < value <= TIMEOUT for value in sock.timeouts))
        self.assertEqual(sock.timeouts, sorted(sock.timeouts, reverse=True))
        self.assertEqual(sock.closed, sock.connected)  # otevřené spojení se vždy zavře
        return elapsed, sock

    def test_slow_connect(self):
        # Spojení delší než limit: skončí přesně po 2 s.
        elapsed, sock = self.assert_timeout([(0, HEALTHY)], connect_seconds=10)
        self.assertEqual((elapsed, sock.timeouts, sock.connected), (TIMEOUT, [TIMEOUT], False))
        # Čas spotřebovaný spojením se odečte: na odpověď zbývá jen zbytek.
        self.assert_timeout([(0.6, HEALTHY)], connect_seconds=1.5)
        result, elapsed, _ = timed_check([(0.4, HEALTHY)], connect_seconds=1.5)
        self.assertEqual((result.state, elapsed), (State.OK, 1.9))

    def test_slow_headers(self):
        # Hlavičky po bajtu každých 0,3 s: bez celkového limitu asi 25 s.
        elapsed, sock = self.assert_timeout(slowly(HEALTHY_HEAD, 0.3) + [(0, HEALTHY_BODY)])
        self.assertEqual(elapsed, TIMEOUT)
        self.assertLess(sock.served, len(HEALTHY_HEAD))  # hlavičky nedorazily celé

    def test_slow_body(self):
        # Hlavičky hned, tělo po čtyřech bajtech každých 0,8 s (s Content-Length).
        self.assert_timeout([(0, HEALTHY_HEAD)] + slowly(HEALTHY_BODY, 0.8, size=4))

    def test_slow_chunked_body(self):
        body = b"".join(chunk(HEALTHY_BODY[index:index + 3]) for index in range(0, len(HEALTHY_BODY), 3)) + b"0\r\n\r\n"
        self.assert_timeout([(0, CHUNKED_HEAD)] + slowly(body, 0.7, size=6))

    def test_trickle_just_before_timeout_never_completes(self):
        # Server pošle pár bajtů vždy 1,9 s po předchozích (těsně pod limitem
        # jedné operace) a odpověď nikdy nedokončí: v hlavičkách i v těle.
        endless_header = itertools.chain([(1.9, b"HTTP/1.1 200 OK\r\nX-Pad: ")], itertools.repeat((1.9, b"abc")))
        endless_body = itertools.chain([(0, CLOSE_HEAD)], itertools.repeat((1.9, b"   ")))
        endless_chunks = itertools.chain([(0, CHUNKED_HEAD)], itertools.repeat((1.9, chunk(b" "))))
        for name, segments in (("hlavičky", endless_header), ("tělo", endless_body), ("chunked", endless_chunks)):
            with self.subTest(case=name):
                elapsed, sock = self.assert_timeout(segments)
                self.assertEqual(elapsed, TIMEOUT)

    def test_slow_but_complete_answer_within_limit_is_ok(self):
        # Limit kontrolu neusekne předčasně: celá odpověď za 1,5 s projde.
        for name, segments in (("Content-Length", slowly(HEALTHY, 0.5, size=40)),
                               ("chunked", [(0.5, CHUNKED_HEAD), (0.5, chunk(HEALTHY_BODY)), (0.5, b"0\r\n\r\n")])):
            with self.subTest(case=name):
                result, elapsed, sock = timed_check(segments)
                self.assertEqual((result.state, dict(result.details)), (State.OK, {HEALTH: UP}))
                self.assertLessEqual(elapsed, 1.5)
                self.assertTrue(sock.closed)


class ResponseSizeTest(unittest.TestCase):
    """Tělo odpovědi se čte nejvýš do MAX_BODY_BYTES + 1 bajtů, i z nekonečného proudu."""

    # Hlavičky, tělo a nejvýš dvakrát vyrovnávací paměť čtení: konečné množství
    # dat nezávislé na tom, kolik toho server posílá.
    READ_BOUND = 1024 + MAX_BODY_BYTES + 1 + 2 * io.DEFAULT_BUFFER_SIZE

    def assert_too_long(self, segments):
        result, _, sock = timed_check(segments)
        self.assertIs(result.state, State.UNKNOWN)
        self.assertEqual(dict(result.details), {HEALTH: INVALID, CAUSE: "too_long"})
        self.assertLessEqual(sock.served, self.READ_BOUND)
        self.assertTrue(sock.closed)
        return sock

    def test_limit_value(self):
        self.assertEqual(MAX_BODY_BYTES, 4096)

    def test_endless_body_without_content_length(self):
        self.assert_too_long(itertools.chain([(0, CLOSE_HEAD)], itertools.repeat((0, b" " * 1000))))

    def test_endless_chunked_body(self):
        self.assert_too_long(itertools.chain([(0, CHUNKED_HEAD)], itertools.repeat((0, chunk(b" " * 1000)))))

    def test_endless_body_with_huge_content_length(self):
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 10000000\r\n\r\n"
        self.assert_too_long(itertools.chain([(0, head)], itertools.repeat((0, b" " * 1000))))

    def test_boundary_for_every_framing(self):
        # Přesně 4096 bajtů platného JSON projde, o bajt víc je UNKNOWN.
        for size, expected in ((MAX_BODY_BYTES, State.OK), (MAX_BODY_BYTES + 1, State.UNKNOWN)):
            body = HEALTHY_BODY + b" " * (size - len(HEALTHY_BODY))
            framings = (
                ("Content-Length", [(0, http(body=body))]),
                ("bez Content-Length", [(0, CLOSE_HEAD), (0, body)]),
                ("chunked", [(0, CHUNKED_HEAD), (0, chunk(body[:3000])), (0, chunk(body[3000:])), (0, b"0\r\n\r\n")]),
            )
            for name, segments in framings:
                with self.subTest(size=size, framing=name):
                    result, _, _ = timed_check(segments)
                    self.assertIs(result.state, expected)


class ResponseTest(unittest.TestCase):
    """Jedna kontrola: co odpověď dokládá (OK, výpadek, nebo neznámý výsledek)."""

    def assert_check(self, step, state, details):
        result = check_once(step)
        self.assertIs(result.state, state)
        self.assertEqual(dict(result.details), details)
        self.assertIsNone(result.measurement)
        return result

    def test_http_200_with_status_ok_is_ok(self):
        result = self.assert_check(HEALTHY, State.OK, {HEALTH: UP})
        self.assertEqual((result.id, result.observed_utc, result.expires_monotonic),
                         (ANIMEDB_ID, OBSERVED, NOW + VALIDITY))
        self.assertEqual(result.summary, "AnimeDB: /health odpovídá, status ok")
        # I bez Content-Length (chunked) a s dalšími položkami JSON.
        chunked = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                   b"1d\r\n{\"status\":\"ok\",\"version\":\"1\"}\r\n0\r\n\r\n")
        self.assert_check(chunked, State.OK, {HEALTH: UP})

    def test_outage_evidence_is_unknown_until_confirmed(self):
        cases = (
            (http(500, b"error", "Internal Server Error"), "http_500"),
            (http(503, b'{"status":"starting"}', "Service Unavailable"), "http_503"),
            (REFUSED, "refused"),
            (socket.timeout("timed out"), "timeout"),
            (FakeSocket(b"", error=TimeoutError("timed out")), "timeout"),
            (FakeSocket(b"", error=ConnectionResetError(errno.ECONNRESET, "reset")), "disconnected"),
            (FakeSocket(b""), "disconnected"),  # spojení zavřené bez odpovědi
            (OSError(errno.EHOSTUNREACH, "No route to host"), "unreachable"),
        )
        for step, cause in cases:
            with self.subTest(cause=cause):
                result = self.assert_check(step, State.UNKNOWN, {HEALTH: DOWN, CAUSE: cause})
                self.assertIn(f"výpadek zatím nepotvrzen, 1/{DEFAULT_CONFIRM_COUNT}", result.summary)

    def test_unexpected_answer_is_unknown_never_ok(self):
        deep = b"[" * 3000 + b"]" * 1000
        cases = (
            (http(body=b'{"status": "ok"'), "json"),  # neplatný JSON
            (http(body=b"\xff\xfe"), "json"),
            (http(body=deep[:MAX_BODY_BYTES]), "json"),
            (http(body=b'{"status": "degraded"}'), "status"),  # jiné než očekávané status
            (http(body=b'{"status": "OK"}'), "status"),
            (http(body=b'{"status": null}'), "status"),
            (http(body=b'{"state": "ok"}'), "status"),  # chybějící status
            (http(body=b'["ok"]'), "status"),
            (http(body=b'"ok"'), "status"),
            (http(body=b'{"status":"ok"}' + b" " * MAX_BODY_BYTES), "too_long"),
            (http(404, b"not found", "Not Found"), "http_404"),
            (http(401, b"", "Unauthorized"), "http_401"),
            (http(204, b"", "No Content"), "http_204"),
            (b"SSH-2.0-OpenSSH_9.6\r\n\r\n", "protocol"),
        )
        for step, cause in cases:
            with self.subTest(cause=cause, step=step[:40]):
                self.assert_check(step, State.UNKNOWN, {HEALTH: INVALID, CAUSE: cause})

    def test_local_failure_is_unknown_without_details(self):
        result = self.assert_check(OSError(errno.EMFILE, "Too many open files"), State.UNKNOWN, {})
        self.assertIn("kontrolu nelze provést (OSError)", result.summary)

    def test_summary_never_contains_the_address(self):
        for step in (HEALTHY, REFUSED, http(500, b"", "Error"), http(body=b"x")):
            self.assertNotIn("192.0.2.10", check_once(step).summary)


class ConfirmationTest(unittest.TestCase):
    """Potvrzení výpadku stejným principem jako alarmové prahy (3 kontroly, odeznění po 2)."""

    def states(self, *steps):
        return [result.state for result in run_checks(*steps)]

    def test_counts_are_the_alarm_defaults(self):
        self.assertEqual((DEFAULT_CONFIRM_COUNT, DEFAULT_CLEAR_COUNT),
                         (AlarmRule().confirm_count, AlarmRule().clear_count))
        self.assertEqual((DEFAULT_CONFIRM_COUNT, DEFAULT_CLEAR_COUNT), (3, 2))

    def test_short_outage_and_recovery_never_errors(self):
        # Krátký restart aplikace: dvě nepovedené kontroly a zase odpovídá.
        results = run_checks(HEALTHY, REFUSED, socket.timeout("timed out"), HEALTHY, HEALTHY)
        self.assertEqual([r.state for r in results],
                         [State.OK, State.UNKNOWN, State.UNKNOWN, State.OK, State.OK])
        for result in results:
            signal = rgb.rgb_signal(FakeSystem().collect(NOW, OBSERVED) + [result], rgb.Acknowledgements(), True,
                                    False, result.expires_monotonic - VALIDITY)
            self.assertNotIn(signal.kind, rgb.BLINKING)  # žádné výstražné blikání

    def test_sustained_outage_is_error_and_recovers_after_two_checks(self):
        results = run_checks(REFUSED, REFUSED, REFUSED, REFUSED, HEALTHY, HEALTHY, HEALTHY)
        self.assertEqual([r.state for r in results],
                         [State.UNKNOWN, State.UNKNOWN, State.ERROR, State.ERROR, State.ERROR, State.OK, State.OK])
        self.assertEqual(results[2].summary, "AnimeDB: spojení odmítnuto")
        self.assertEqual(dict(results[4].details), {HEALTH: UP})
        self.assertIn("výpadek odeznívá, 1/2", results[4].summary)

    def test_different_outage_causes_confirm_together(self):
        self.assertEqual(self.states(socket.timeout("timed out"), http(500, b"", "Error"), REFUSED)[-1], State.ERROR)

    def test_unknown_result_interrupts_the_series(self):
        invalid = http(body=b"not json")
        self.assertEqual(self.states(REFUSED, REFUSED, invalid, REFUSED, REFUSED),
                         [State.UNKNOWN] * 5)  # neplatná odpověď výpadek nepotvrdí

    def test_unknown_result_does_not_resolve_confirmed_outage(self):
        invalid = http(body=b"not json")
        self.assertEqual(self.states(REFUSED, REFUSED, REFUSED, invalid, REFUSED, HEALTHY, invalid, HEALTHY, HEALTHY),
                         [State.UNKNOWN, State.UNKNOWN, State.ERROR, State.UNKNOWN, State.ERROR, State.ERROR,
                          State.UNKNOWN, State.ERROR, State.OK])

    def test_unexpected_exception_interrupts_the_series(self):
        # Regrese (review SERVICES-2): výjimka, kterou zachytí až run.Collectors, je kontrola
        # bez výsledku. Dřív se nepočítala vůbec, takže timeout, výjimka, timeout, timeout dalo
        # ERROR jako tři výpadky po sobě a úspěch, výjimka, úspěch ukončil výpadek (falešné OK).
        def states(*steps):
            collectors = Collectors(monitor_for(FakeNetwork(*steps)))
            return [collectors.collect(NOW + INTERVAL * index, OBSERVED)[0] for index in range(len(steps))]

        timeout, bug = socket.timeout("timed out"), RuntimeError("chyba")
        results = states(timeout, bug, timeout, timeout)
        self.assertEqual([r.state for r in results], [State.UNKNOWN] * 4)
        self.assertEqual(results[1].details[COLLECTOR_FAILURE], "RuntimeError")
        self.assertIn("2/3", results[3].summary)  # řada začala znovu až po výjimce
        self.assertEqual([r.state for r in states(REFUSED, REFUSED, REFUSED, HEALTHY, bug, HEALTHY, HEALTHY)],
                         [State.UNKNOWN, State.UNKNOWN, State.ERROR, State.ERROR, State.UNKNOWN, State.ERROR,
                          State.OK])

    def test_gap_in_measurement_restarts_the_series(self):
        monitor = monitor_for(FakeNetwork(REFUSED))
        first = monitor.collect(NOW, OBSERVED)[0]
        second = monitor.collect(NOW + INTERVAL, OBSERVED)[0]
        # Další kontrola až po vypršení předchozího výsledku: řada začne znovu.
        after_gap = monitor.collect(second.expires_monotonic, OBSERVED)[0]
        self.assertEqual([r.state for r in (first, second, after_gap)], [State.UNKNOWN] * 3)
        self.assertIn("1/3", after_gap.summary)

    def test_confirmed_outage_survives_a_gap(self):
        # Jako u alarmových prahů: mezera výpadek nevyřeší, jen přeruší řadu.
        monitor = monitor_for(FakeNetwork(REFUSED))
        for index in range(3):
            last = monitor.collect(NOW + INTERVAL * index, OBSERVED)[0]
        self.assertIs(last.state, State.ERROR)
        self.assertIs(monitor.collect(last.expires_monotonic + 100, OBSERVED)[0].state, State.ERROR)


def screens(result, now=None):
    """(SERVICES, první text STATUS, symbol NEXUS, signál RGB) pro výsledek AnimeDB vedle zdravého systému."""
    now = result.expires_monotonic - VALIDITY if now is None else now
    monitored = FakeSystem().collect(now, OBSERVED) + [result]
    results = complete_service_results(monitored, OBSERVED, now + VALIDITY, monitored={ANIMEDB_ID})
    problems = {problem.id: problem for problem in active_problems(results, now)}
    status = status_texts(problems[ANIMEDB_ID], now)[0] if ANIMEDB_ID in problems else None
    signal = rgb.rgb_signal(monitored, rgb.Acknowledgements(), True, False, now)
    return dict(service_rows(results, now))["ANIMEDB"], status, home_symbol(results, now), signal


class DisplayAndRgbTest(unittest.TestCase):
    def test_every_stage_on_services_status_nexus_and_rgb(self):
        outage = run_checks(REFUSED, REFUSED, REFUSED, HEALTHY, HEALTHY)
        cases = (
            ("zdravá", check_once(HEALTHY), ("OK", None), rgb.LIGHT),
            ("nepotvrzený výpadek", outage[0], ("UNKN", "ANIMEDB FAILING"), rgb.OFF),
            ("potvrzený výpadek", outage[2], ("ERR", "ANIMEDB DOWN"), rgb.CODES),
            ("odeznívající výpadek", outage[3], ("ERR", "ANIMEDB RECOVER"), rgb.CODES),
            ("po odeznění", outage[4], ("OK", None), rgb.LIGHT),
            ("neplatná odpověď", check_once(http(body=b"x")), ("UNKN", "ANIMEDB INVALID"), rgb.OFF),
            ("kontrolu nelze provést", check_once(OSError(errno.EMFILE, "x")), ("UNKN", "ANIMEDB NO DATA"), rgb.OFF),
        )
        for name, result, (row, status), kind in cases:
            with self.subTest(case=name):
                shown_row, shown_status, symbol, signal = screens(result)
                self.assertEqual((shown_row, shown_status), (row, status))
                self.assertEqual(signal.kind, kind)
                if kind == rgb.CODES:
                    # Žádný nový RGB stav: kategorie 3 (AnimeDB) jako dosud.
                    self.assertEqual(signal.steps, ((State.ERROR, 3),))
                    self.assertEqual(symbol, ALERT_SYMBOL)
                else:
                    # NEXUS a BACKUP nemají kolektor (UNKNOWN), proto "?" i při zdravé AnimeDB.
                    self.assertEqual(symbol, UNKNOWN_SYMBOL)

    def test_expired_result_is_unknown_never_ok(self):
        for result in (check_once(HEALTHY), run_checks(REFUSED, REFUSED, REFUSED)[-1]):
            later = result.expires_monotonic
            with self.subTest(state=result.state):
                self.assertIs(effective(result, later).state, State.UNKNOWN)
                self.assertEqual(dict(effective(result, later).details), {})
                row, status, _, signal = screens(result, now=later)
                self.assertEqual((row, status, signal.kind), ("UNKN", "ANIMEDB STALE", rgb.OFF))

    def test_alarm_evaluator_leaves_health_state_unchanged(self):
        evaluator = AlarmEvaluator(AlarmsConfig())
        for result in run_checks(HEALTHY, REFUSED, REFUSED, REFUSED, http(body=b"x")):
            self.assertEqual(evaluator.evaluate([result], NOW), [result])

    def test_enabled_service_without_result_is_unknown_not_na(self):
        added = {r.id: r for r in complete_service_results([], OBSERVED, NOW + VALIDITY, monitored={ANIMEDB_ID})}
        self.assertEqual((added[ANIMEDB_ID].state, added[ANIMEDB_ID].summary), (State.UNKNOWN, "ANIMEDB: bez dat"))
        added = {r.id: r for r in complete_service_results([], OBSERVED, NOW + VALIDITY)}
        self.assertIs(added[ANIMEDB_ID].state, State.NA)  # vypnuté sledování
        self.assertIs(effective(added[ANIMEDB_ID], 10 ** 9).state, State.NA)

    def test_collector_result_is_never_replaced_by_na(self):
        result = run_checks(REFUSED, REFUSED, REFUSED)[-1]
        for monitored in ((), {ANIMEDB_ID}):
            completed = complete_service_results([result], OBSERVED, NOW + VALIDITY, monitored=monitored)
            self.assertEqual([r for r in completed if r.id == ANIMEDB_ID], [result])

    def test_unexpected_exception_stays_with_animedb(self):
        collectors = Collectors(FakeSystem(), monitor_for(FakeNetwork(RuntimeError("chyba"))))
        results = {result.id: result for result in collectors.collect(NOW, OBSERVED)}
        self.assertIs(results[ANIMEDB_ID].state, State.UNKNOWN)
        self.assertEqual(results[ANIMEDB_ID].details[COLLECTOR_FAILURE], "RuntimeError")
        self.assertEqual({results[check_id].state for check_id in SYSTEM_LABELS}, {State.INFO})


class ConfigurationTest(unittest.TestCase):
    def test_disabled_monitoring_never_opens_a_connection(self):
        disabled = (Config(), Config(services=ServicesConfig(AnimeDbConfig(enabled=False, url=URL))))
        forbidden = AssertionError("síťové volání při vypnutém sledování")
        with mock.patch("nexusstat.run.AnimeDbMonitor", side_effect=forbidden) as monitor_class, \
                mock.patch("socket.create_connection", side_effect=forbidden) as connect_mock, \
                mock.patch("socket.socket", side_effect=forbidden) as socket_mock:
            for config in disabled:
                monitor = live_monitor(config)
                self.assertNotIn(ANIMEDB_ID, monitor.labels)
                completed = complete_service_results([], OBSERVED, NOW + VALIDITY, monitored=monitor.labels)
                self.assertIs({r.id: r for r in completed}[ANIMEDB_ID].state, State.NA)
        monitor_class.assert_not_called()
        connect_mock.assert_not_called()
        socket_mock.assert_not_called()

    def test_enabled_monitoring_adds_one_health_collector(self):
        config = Config(collector=CollectorConfig(interval_seconds=5),
                        services=ServicesConfig(AnimeDbConfig(enabled=True, url=URL, timeout_seconds=1.5)))
        monitor = live_monitor(config)  # jen vytvoření, nic se nepřipojuje
        health = monitor.monitors[-1]
        self.assertIsInstance(health, AnimeDbMonitor)
        self.assertEqual((health.url, health.timeout_seconds, health.validity_seconds), (URL, 1.5, 15))
        self.assertIs(health.connect, socket.create_connection)
        self.assertEqual(sum(isinstance(m, AnimeDbMonitor) for m in monitor.monitors), 1)
        self.assertIn(ANIMEDB_ID, monitor.labels)
        self.assertFalse(monitor.simulated)

    def test_terminal_lines_show_confirmation(self):
        lines = []
        clock = FakeClock(NOW)

        def sleep(seconds):
            if len(lines) > 3:
                raise KeyboardInterrupt  # po třech měřeních
            clock.advance(seconds)

        monitor = Collectors(FakeSystem(), monitor_for(FakeNetwork(REFUSED)))
        results = run_loop(monitor, CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=clock,
                           utc_clock=lambda: OBSERVED, sleep=sleep, output=lines.append)
        self.assertIn("AnimeDB: spojení odmítnuto (výpadek zatím nepotvrzen, 1/3)", lines[1])
        self.assertNotIn("[ERROR]", lines[2])
        self.assertIn("AnimeDB: spojení odmítnuto [ERROR]", lines[3])
        self.assertIn("UTC  SIM ERROR", lines[3])  # syntetické odpovědi = fiktivní data
        self.assertIs({r.id: r for r in results}[ANIMEDB_ID].state, State.ERROR)


class PreviewTest(unittest.TestCase):
    """Živý náhled (LIVE) se zapnutým sledováním: SERVICES, STATUS a LED ze syntetických odpovědí."""

    def session(self, network, enabled=True):
        config = Config(services=ServicesConfig(AnimeDbConfig(enabled=enabled, url=URL)))

        def factory(config):
            validity = config.collector.validity_seconds
            monitors = [FakeSystem(validity)]
            animedb = config.services.animedb
            if animedb.enabled:
                monitors.append(AnimeDbMonitor(validity, animedb.url, animedb.timeout_seconds, connect=network,
                                               clock=FakeClock()))
            return Collectors(*monitors)

        self.real = FakeClock(5000.0)
        return PreviewSession(config, scenario=LIVE, clock=SessionClock(real_clock=self.real),
                              utc_clock=lambda: OBSERVED, wall_clock=lambda: datetime(2026, 10, 24, 14, 0),
                              monitor_factory=factory)

    def next_measurement(self, session):
        self.real.advance(INTERVAL)
        session._refresh_if_due(session.clock.now())

    def test_sustained_outage_blinks_only_after_confirmation(self):
        session = self.session(FakeNetwork(REFUSED))
        session.press()  # probuzení OLED
        frames = [session.led_frame()]
        for _ in range(2):
            self.next_measurement(session)
            frames.append(session.led_frame())
        self.assertEqual([frame.lit for frame in frames[:2]], [False, False])  # nepotvrzeno: tma, ne bílá
        self.assertEqual(frames[2].color, rgb.RED)  # potvrzeno: červený kód 3
        now = session.clock.now()
        self.assertEqual(dict(service_rows(session.results, now))["ANIMEDB"], "ERR")
        self.assertIn("ANIMEDB DOWN", [status_texts(p, now)[0] for p in active_problems(session.results, now)])

    def test_healthy_gives_white(self):
        session = self.session(FakeNetwork(HEALTHY))
        self.assertEqual(dict(service_rows(session.results, session.clock.now()))["ANIMEDB"], "OK")
        session.press()
        self.assertEqual(session.led_frame().color, rgb.WHITE)
        self.assertIn("AnimeDB z HTTP healthchecku /health", session.source_text())

    def test_disabled_shows_na_without_network(self):
        network = FakeNetwork(HEALTHY)
        session = self.session(network, enabled=False)
        self.assertEqual(dict(service_rows(session.results, session.clock.now()))["ANIMEDB"], "N/A")
        self.assertEqual(network.calls, [])
        session.press()
        self.assertEqual(session.led_frame().color, rgb.WHITE)  # N/A bílou neblokuje
        self.assertIn("AnimeDB N/A (sledování vypnuto)", session.source_text())


if __name__ == "__main__":
    unittest.main()
