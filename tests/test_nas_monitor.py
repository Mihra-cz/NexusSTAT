"""Testy kontroly dostupnosti NAS spojením TCP na port SMB (SERVICES-2) nad podvrženým spojením.

Žádný test neotevírá síťové spojení ani se nedotkne skutečného NAS: socket
nahrazuje FakeConnect s připraveným průběhem pokusů a čas je simulovaný
(monotónní sekundy jako čísla), nic se nečeká. Adresy jsou jen z dokumentačních
rozsahů (RFC 5737, RFC 3849).
"""

import errno
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from nexusstat import nas_monitor, rgb
from nexusstat.alarms import AlarmEvaluator
from nexusstat.animedb_monitor import AnimeDbMonitor
from nexusstat.config import (DEFAULT_CLEAR_COUNT, DEFAULT_CONFIRM_COUNT, AlarmsConfig, AnimeDbConfig,
                              CollectorConfig, Config, ConfigError, NasConfig, ServicesConfig, load_config,
                              services_config_from_dict)
from nexusstat.core import CheckResult, State, active_problems, effective
from nexusstat.nas_monitor import NasMonitor, connect_once
from nexusstat.oled import ALERT_SYMBOL, UNKNOWN_SYMBOL, home_symbol
from nexusstat.oled_text import service_rows, status_texts
from nexusstat.preview import LIVE, PreviewSession, ScriptedConnection, SessionClock
from nexusstat.run import COLLECTOR_FAILURE, Collectors, Scheduled, live_monitor, run_loop
from nexusstat.services import (ANIMEDB_ID, CAUSE, DOWN, HEALTH, NAS_ID, UP, OutageConfirmation,
                                complete_service_results)
from nexusstat.system_monitor import LABELS as SYSTEM_LABELS

OBSERVED = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
HOST = "192.0.2.20"  # dokumentační rozsah RFC 5737, ne skutečná adresa NAS
HOST_V6 = "2001:db8::20"  # dokumentační rozsah RFC 3849
PORT = 445
TIMEOUT = 2
NOW = 1000.0
VALIDITY = 30
INTERVAL = 10
EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.example.toml"

# Pokus o spojení: None = spojení přijato, jinak výjimka, kterou by vyvolalo skutečné spojení.
ACCEPTED = None
REFUSED = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
TIMED_OUT = socket.timeout("timed out")  # od Pythonu 3.10 totéž co TimeoutError
NET_UNREACHABLE = OSError(errno.ENETUNREACH, "Network is unreachable")
HOST_UNREACHABLE = OSError(errno.EHOSTUNREACH, "No route to host")
NO_DESCRIPTORS = OSError(errno.EMFILE, "Too many open files")  # chyba na této straně
NO_BUFFERS = OSError(errno.ENOBUFS, "No buffer space available")
RESET = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
ANIMEDB_URL = "http://192.0.2.10:8080/health"


class FakeClock:
    """Monotónní hodiny pod kontrolou testu."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSystem:
    """Skutečně vypadající systémová měření bez čtení /proc (pro skládání kolektorů)."""

    simulated = False
    labels = SYSTEM_LABELS

    def __init__(self, validity_seconds=VALIDITY):
        self.validity_seconds = validity_seconds

    def collect(self, now, observed_utc):
        return [CheckResult(check_id, State.INFO, label, observed_utc, now + self.validity_seconds, simulated=False)
                for check_id, label in SYSTEM_LABELS.items()]


class HealthySocket:
    """Odpověď zdravé AnimeDB (HTTP 200, status ok) pro souběh s kontrolou NAS."""

    def __init__(self, address=None, timeout=None):
        body = b'{"status":"ok"}'
        self.data = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)

    def settimeout(self, value):
        pass

    def sendall(self, data):
        pass

    def recv_into(self, buffer):
        size = min(len(buffer), len(self.data))
        buffer[:size], self.data = self.data[:size], self.data[size:]
        return size

    def close(self):
        pass


def animedb_monitor(connect):
    return AnimeDbMonitor(VALIDITY, ANIMEDB_URL, TIMEOUT, connect=connect, clock=FakeClock())


class FakeSocket:
    """Navázané spojení: smí se jen zavřít. Odeslání nebo čtení by bylo SMB provozem."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def __getattr__(self, name):
        raise AssertionError(f"kontrola NAS nesmí se socketem dělat nic než close(), ne {name}")


class FakeConnect:
    """Náhrada socket.create_connection s připraveným průběhem; po posledním kroku se opakuje poslední."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []
        self.sockets = []

    def __call__(self, address, timeout=None):
        self.calls.append((address, timeout))
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            raise step
        sock = FakeSocket()
        self.sockets.append(sock)
        return sock


def monitor_for(connect, host=HOST, port=PORT):
    return NasMonitor(VALIDITY, host, port, TIMEOUT, connect=connect)


def check_once(step):
    [result] = monitor_for(FakeConnect(step)).collect(NOW, OBSERVED)
    return result


def run_checks(*steps):
    """Výsledky po sobě jdoucích kontrol v intervalu měření (jeden pokus na krok)."""
    monitor = monitor_for(FakeConnect(*steps))
    return [monitor.collect(NOW + INTERVAL * index, OBSERVED)[0] for index in range(len(steps))]


def nas_config(**keys):
    return Config(services=ServicesConfig(nas=NasConfig(**keys)))


class ConnectionTest(unittest.TestCase):
    """Jediný pokus o spojení TCP na nastavenou adresu, okamžité zavření, nic dalšího."""

    def test_single_connection_to_configured_address_with_limit(self):
        connect = FakeConnect(ACCEPTED)
        monitor_for(connect).collect(NOW, OBSERVED)
        self.assertEqual(connect.calls, [((HOST, PORT), TIMEOUT)])

    def test_socket_is_closed_immediately_without_traffic(self):
        # FakeSocket odmítne cokoli kromě close(): žádné odeslání, čtení ani SMB vyjednávání.
        connect = FakeConnect(ACCEPTED)
        connect_once(HOST, PORT, TIMEOUT, connect)
        self.assertEqual([sock.closed for sock in connect.sockets], [True])
        connect = FakeConnect(ACCEPTED)
        monitor = monitor_for(connect)
        for index in range(3):
            monitor.collect(NOW + INTERVAL * index, OBSERVED)
        self.assertEqual([sock.closed for sock in connect.sockets], [True] * 3)

    def test_ipv6_address_and_other_port(self):
        connect = FakeConnect(ACCEPTED)
        [result] = monitor_for(connect, host=HOST_V6, port=1445).collect(NOW, OBSERVED)
        self.assertEqual(connect.calls, [((HOST_V6, 1445), TIMEOUT)])
        self.assertEqual(result.summary, "NAS: služba SMB přijímá spojení (port 1445)")

    def test_invalid_constructor_arguments(self):
        for arguments in ((0, HOST, PORT, TIMEOUT), (VALIDITY, "nas.lan", PORT, TIMEOUT),
                          (VALIDITY, HOST, 0, TIMEOUT), (VALIDITY, HOST, PORT, 6), (VALIDITY, "0.0.0.0", PORT, 1)):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    NasMonitor(*arguments, connect=FakeConnect(ACCEPTED))

    def test_real_connect_is_real_data_and_fake_is_simulated(self):
        self.assertFalse(NasMonitor(VALIDITY, HOST, PORT, TIMEOUT).simulated)  # jen vytvoření, nic se nepřipojuje
        self.assertTrue(monitor_for(FakeConnect(ACCEPTED)).simulated)

    def test_standard_library_path_closes_socket_on_success_and_error(self):
        # Skutečné socket.create_connection ze standardní knihovny, ale nad podvrženou třídou
        # socketu a getaddrinfo: žádné systémové síťové volání. Úspěch zavře connect_once(),
        # chybu spojení zavře už create_connection(); nic se nepošle ani nepřečte.
        created = []

        class RawSocket:
            def __init__(self, family, kind, proto):
                self.outcome = outcomes.pop(0)
                self.calls = []
                created.append(self)

            def settimeout(self, value):
                self.calls.append(("settimeout", value))

            def connect(self, address):
                self.calls.append(("connect", address))
                if self.outcome is not None:
                    raise self.outcome

            def close(self):
                self.calls.append(("close",))

            def __getattr__(self, name):
                raise AssertionError(f"kontrola NAS nesmí volat {name}")

        lookups = []

        def getaddrinfo(host, port, family=0, kind=0, proto=0, flags=0):
            lookups.append((host, port))
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

        outcomes = [ACCEPTED, REFUSED, TIMED_OUT, NET_UNREACHABLE, NO_DESCRIPTORS]
        with mock.patch("socket.socket", RawSocket), mock.patch("socket.getaddrinfo", getaddrinfo):
            monitor = NasMonitor(VALIDITY, HOST, PORT, TIMEOUT)  # výchozí connect = create_connection
            results = [monitor.collect(NOW + INTERVAL * index, OBSERVED)[0] for index in range(5)]
        self.assertEqual([dict(r.details).get(CAUSE, dict(r.details).get(HEALTH)) for r in results],
                         [UP, "refused", "timeout", "unreachable", None])
        self.assertEqual(lookups, [(HOST, PORT)] * 5)  # jediná adresa, jediný pokus na kontrolu
        for sock in created:
            self.assertEqual(sock.calls, [("settimeout", TIMEOUT), ("connect", (HOST, PORT)), ("close",)])
        # Selhání už při vytvoření socketu (nedostatek deskriptorů): žádný socket nevznikl.
        with mock.patch("socket.socket", side_effect=NO_DESCRIPTORS), mock.patch("socket.getaddrinfo", getaddrinfo):
            [result] = NasMonitor(VALIDITY, HOST, PORT, TIMEOUT).collect(NOW, OBSERVED)
        self.assertEqual((result.state, dict(result.details)), (State.UNKNOWN, {}))

    def test_no_mount_or_file_access(self):
        # Kontrola nesmí sáhnout na žádnou cestu: síťový mount by se zasekl a autofs by
        # se při přístupu připojil. Jakýkoli přístup k souborovému systému test zastaví.
        forbidden = AssertionError("přístup k souborovému systému při kontrole NAS")
        names = ("builtins.open", "os.open", "os.stat", "os.lstat", "os.statvfs", "os.scandir", "os.listdir",
                 "os.access", "os.path.exists", "os.path.ismount", "os.path.isdir")
        patches = [mock.patch(name, side_effect=forbidden) for name in names]
        for patch in patches:
            patch.start()
        try:
            results = run_checks(ACCEPTED, REFUSED, TIMED_OUT, NET_UNREACHABLE, NO_DESCRIPTORS)
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(len(results), 5)
        # Modul ani nenačítá nic, čím by soubory nebo mounty procházel či spouštěl příkazy.
        self.assertFalse({"os", "pathlib", "glob", "shutil", "subprocess"} & set(vars(nas_monitor)))
        source = Path(nas_monitor.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/mnt", source)


class ProbeTest(unittest.TestCase):
    """Jeden pokus: co dokládá (dostupnost, výpadek, nebo nic)."""

    def assert_check(self, step, state, details):
        result = check_once(step)
        self.assertIs(result.state, state)
        self.assertEqual(dict(result.details), details)
        self.assertIsNone(result.measurement)
        return result

    def test_accepted_connection_is_ok(self):
        result = self.assert_check(ACCEPTED, State.OK, {HEALTH: UP})
        self.assertEqual((result.id, result.observed_utc, result.expires_monotonic),
                         (NAS_ID, OBSERVED, NOW + VALIDITY))
        # Jen dostupnost služby SMB: nic o sdílených složkách, datech, zálohách ani hardwaru.
        self.assertEqual(result.summary, "NAS: služba SMB přijímá spojení (port 445)")

    def test_outage_evidence_is_unknown_until_confirmed(self):
        cases = (
            (REFUSED, "refused", "spojení odmítnuto"),
            (TIMED_OUT, "timeout", "bez odpovědi do 2 s"),
            (TimeoutError("timed out"), "timeout", "bez odpovědi do 2 s"),
            (NET_UNREACHABLE, "unreachable", "síť nebo adresa nedostupná"),
            (HOST_UNREACHABLE, "unreachable", "síť nebo adresa nedostupná"),
        )
        for step, cause, text in cases:
            with self.subTest(cause=cause, step=step):
                result = self.assert_check(step, State.UNKNOWN, {HEALTH: DOWN, CAUSE: cause})
                self.assertEqual(result.summary, f"NAS: {text} (výpadek zatím nepotvrzen, 1/{DEFAULT_CONFIRM_COUNT})")

    def test_local_or_unclear_error_is_unknown_never_outage(self):
        for step in (NO_DESCRIPTORS, NO_BUFFERS, OSError(errno.EADDRNOTAVAIL, "x"), OSError(errno.EPERM, "x"),
                     socket.gaierror(socket.EAI_FAMILY, "x"), RESET):
            with self.subTest(step=step):
                result = self.assert_check(step, State.UNKNOWN, {})
                self.assertIn("kontrolu nelze provést", result.summary)
                self.assertNotIn("výpadek", result.summary)

    def test_summary_never_contains_the_address(self):
        for host in (HOST, HOST_V6):
            for step in (ACCEPTED, REFUSED, TIMED_OUT, NET_UNREACHABLE, NO_DESCRIPTORS):
                [result] = monitor_for(FakeConnect(step), host=host).collect(NOW, OBSERVED)
                self.assertNotIn(host, result.summary)
                self.assertNotIn(host, dict(result.details).values())


class ConfirmationTest(unittest.TestCase):
    """Potvrzení výpadku stejným principem jako AnimeDB a alarmové prahy (3 kontroly, odeznění po 2)."""

    def states(self, *steps):
        return [result.state for result in run_checks(*steps)]

    def test_shared_confirmation_with_alarm_defaults(self):
        self.assertEqual((DEFAULT_CONFIRM_COUNT, DEFAULT_CLEAR_COUNT), (3, 2))
        self.assertIsInstance(monitor_for(FakeConnect(ACCEPTED)).outage, OutageConfirmation)
        self.assertIsInstance(animedb_monitor(FakeConnect(REFUSED)).outage, OutageConfirmation)  # tutéž logiku má AnimeDB

    def test_first_and_second_failure_are_unknown_without_blinking(self):
        results = run_checks(ACCEPTED, REFUSED, TIMED_OUT, ACCEPTED, ACCEPTED)
        self.assertEqual([r.state for r in results], [State.OK, State.UNKNOWN, State.UNKNOWN, State.OK, State.OK])
        self.assertIn("1/3", results[1].summary)
        self.assertIn("2/3", results[2].summary)
        for result in results:
            signal = rgb.rgb_signal(FakeSystem().collect(NOW, OBSERVED) + [result], rgb.Acknowledgements(), True,
                                    False, result.expires_monotonic - VALIDITY)
            self.assertNotIn(signal.kind, rgb.BLINKING)

    def test_three_failures_are_error_and_two_successes_clear_it(self):
        results = run_checks(REFUSED, REFUSED, REFUSED, REFUSED, ACCEPTED, ACCEPTED, ACCEPTED)
        self.assertEqual([r.state for r in results],
                         [State.UNKNOWN, State.UNKNOWN, State.ERROR, State.ERROR, State.ERROR, State.OK, State.OK])
        self.assertEqual(results[2].summary, "NAS: spojení odmítnuto")
        self.assertEqual(dict(results[4].details), {HEALTH: UP})
        self.assertIn("výpadek odeznívá, 1/2", results[4].summary)

    def test_different_outage_causes_confirm_together(self):
        self.assertEqual(self.states(TIMED_OUT, NET_UNREACHABLE, REFUSED)[-1], State.ERROR)
        self.assertEqual(self.states(HOST_UNREACHABLE, HOST_UNREACHABLE, TIMED_OUT)[-1], State.ERROR)

    def test_unknown_result_interrupts_the_series(self):
        self.assertEqual(self.states(REFUSED, REFUSED, NO_DESCRIPTORS, REFUSED, REFUSED), [State.UNKNOWN] * 5)

    def test_unknown_result_does_not_resolve_confirmed_outage(self):
        self.assertEqual(
            self.states(REFUSED, REFUSED, REFUSED, NO_DESCRIPTORS, REFUSED, ACCEPTED, NO_DESCRIPTORS, ACCEPTED,
                        ACCEPTED),
            [State.UNKNOWN, State.UNKNOWN, State.ERROR, State.UNKNOWN, State.ERROR, State.ERROR, State.UNKNOWN,
             State.ERROR, State.OK])

    def test_unexpected_exception_interrupts_the_series(self):
        # Neočekávaná výjimka (UNKNOWN z run.Collectors) přeruší řadu stejně jako neznámý výsledek:
        # výpadek ani odeznění se nepotvrdí přes kontrolu, jejíž výsledek chybí.
        def states(*steps):
            collectors = Collectors(FakeSystem(), monitor_for(FakeConnect(*steps)))
            return [{r.id: r for r in collectors.collect(NOW + INTERVAL * index, OBSERVED)}[NAS_ID]
                    for index in range(len(steps))]

        bug = RuntimeError("chyba")
        # Řada ze zadání review: timeout, výjimka zachycená v Collectors, timeout, timeout.
        results = states(TIMED_OUT, bug, TIMED_OUT, TIMED_OUT)
        self.assertEqual([r.state for r in results], [State.UNKNOWN] * 4)
        self.assertIn("2/3", results[3].summary)
        results = states(REFUSED, REFUSED, bug, REFUSED)
        self.assertEqual([r.state for r in results], [State.UNKNOWN] * 4)
        self.assertEqual(results[2].details[COLLECTOR_FAILURE], "RuntimeError")
        self.assertIn("1/3", results[3].summary)
        self.assertEqual([r.state for r in states(REFUSED, REFUSED, REFUSED, ACCEPTED, bug, ACCEPTED, ACCEPTED)],
                         [State.UNKNOWN, State.UNKNOWN, State.ERROR, State.ERROR, State.UNKNOWN, State.ERROR,
                          State.OK])

    def test_shared_confirmation_rules(self):
        # Přímo nad sdílenou třídou: platí pro NAS i AnimeDB.
        confirmation = OutageConfirmation(VALIDITY)
        states = [confirmation.confirm(outcome, NOW + INTERVAL * index)[0]
                  for index, outcome in enumerate((DOWN, DOWN, DOWN, None, DOWN, UP, None, UP, UP))]
        # Potvrzená závada přežije nejistotu (None): další výpadek je hned ERROR a odeznění
        # potřebuje dva úspěchy bez přerušení.
        self.assertEqual(states, [State.UNKNOWN, State.UNKNOWN, State.ERROR, State.UNKNOWN, State.ERROR,
                                  State.ERROR, State.UNKNOWN, State.ERROR, State.OK])
        # Reset po vypršení: přesně v okamžiku konce platnosti (jako effective()) už řada neplatí.
        for later, expected in ((NOW + VALIDITY - 0.001, " (výpadek zatím nepotvrzen, 2/3)"),
                                (NOW + VALIDITY, " (výpadek zatím nepotvrzen, 1/3)")):
            confirmation = OutageConfirmation(VALIDITY)
            confirmation.confirm(DOWN, NOW)
            self.assertEqual(confirmation.confirm(DOWN, later), (State.UNKNOWN, expected))

    def test_check_passes_unexpected_exception_after_interrupting(self):
        confirmation = OutageConfirmation(VALIDITY)
        confirmation.check(lambda: (DOWN, "x", {}), NOW)
        with self.assertRaises(RuntimeError):
            confirmation.check(lambda: (_ for _ in ()).throw(RuntimeError("chyba")), NOW + INTERVAL)
        self.assertEqual((confirmation.failures, confirmation.successes), (0, 0))
        self.assertEqual(confirmation.check(lambda: (DOWN, "x", {}), NOW + 2 * INTERVAL),
                         (State.UNKNOWN, "x (výpadek zatím nepotvrzen, 1/3)", {}))
        # Ctrl+C a SIGTERM (KeyboardInterrupt není Exception) projdou beze změny paměti.
        with self.assertRaises(KeyboardInterrupt):
            confirmation.check(lambda: (_ for _ in ()).throw(KeyboardInterrupt()), NOW + 3 * INTERVAL)
        self.assertEqual(confirmation.failures, 1)

    def test_gap_in_measurement_restarts_the_series(self):
        monitor = monitor_for(FakeConnect(REFUSED))
        first = monitor.collect(NOW, OBSERVED)[0]
        second = monitor.collect(NOW + INTERVAL, OBSERVED)[0]
        after_gap = monitor.collect(second.expires_monotonic, OBSERVED)[0]
        self.assertEqual([r.state for r in (first, second, after_gap)], [State.UNKNOWN] * 3)
        self.assertIn("1/3", after_gap.summary)
        # Mezera přeruší i odeznění: po ní jsou potřeba znovu dvě úspěšné kontroly.
        monitor = monitor_for(FakeConnect(REFUSED, REFUSED, REFUSED, ACCEPTED))
        for index in range(4):
            last = monitor.collect(NOW + INTERVAL * index, OBSERVED)[0]
        self.assertIn("výpadek odeznívá, 1/2", last.summary)
        self.assertIs(monitor.collect(last.expires_monotonic, OBSERVED)[0].state, State.ERROR)

    def test_confirmed_outage_survives_a_gap(self):
        monitor = monitor_for(FakeConnect(REFUSED))
        for index in range(3):
            last = monitor.collect(NOW + INTERVAL * index, OBSERVED)[0]
        self.assertIs(last.state, State.ERROR)
        self.assertIs(monitor.collect(last.expires_monotonic + 100, OBSERVED)[0].state, State.ERROR)

    def test_expired_result_is_unknown_never_ok(self):
        for result in (check_once(ACCEPTED), run_checks(REFUSED, REFUSED, REFUSED)[-1]):
            with self.subTest(state=result.state):
                expired = effective(result, result.expires_monotonic)
                self.assertIs(expired.state, State.UNKNOWN)
                self.assertEqual(dict(expired.details), {})


def screens(result, now=None):
    """(SERVICES, první text STATUS, symbol NEXUS, signál RGB) pro výsledek NAS vedle zdravého systému."""
    now = result.expires_monotonic - VALIDITY if now is None else now
    monitored = FakeSystem().collect(now, OBSERVED) + [result]
    results = complete_service_results(monitored, OBSERVED, now + VALIDITY, monitored={NAS_ID})
    problems = {problem.id: problem for problem in active_problems(results, now)}
    status = status_texts(problems[NAS_ID], now)[0] if NAS_ID in problems else None
    signal = rgb.rgb_signal(monitored, rgb.Acknowledgements(), True, False, now)
    return dict(service_rows(results, now))["NAS"], status, home_symbol(results, now), signal


class DisplayAndRgbTest(unittest.TestCase):
    def test_every_stage_on_services_status_nexus_and_rgb(self):
        outage = run_checks(REFUSED, REFUSED, REFUSED, ACCEPTED, ACCEPTED)
        cases = (
            ("dostupný", check_once(ACCEPTED), ("OK", None), rgb.LIGHT),
            ("1. neúspěšný pokus", outage[0], ("UNKN", "NAS FAILING"), rgb.OFF),
            ("2. neúspěšný pokus", outage[1], ("UNKN", "NAS FAILING"), rgb.OFF),
            ("potvrzený výpadek", outage[2], ("ERR", "NAS OFFLINE"), rgb.CODES),
            ("odeznívající výpadek", outage[3], ("ERR", "NAS RECOVER"), rgb.CODES),
            ("po odeznění", outage[4], ("OK", None), rgb.LIGHT),
            ("chyba na této straně", check_once(NO_DESCRIPTORS), ("UNKN", "NAS NO DATA"), rgb.OFF),
        )
        for name, result, (row, status), kind in cases:
            with self.subTest(case=name):
                shown_row, shown_status, symbol, signal = screens(result)
                self.assertEqual((shown_row, shown_status), (row, status))
                self.assertEqual(signal.kind, kind)
                if kind == rgb.CODES:
                    # Žádný nový RGB stav: kategorie 2 (NAS), červeně jako každý ERROR.
                    self.assertEqual(signal.steps, ((State.ERROR, 2),))
                    self.assertEqual(symbol, ALERT_SYMBOL)
                else:
                    # NEXUS nemá kolektor (UNKNOWN), proto "?" i při dostupném NAS.
                    self.assertEqual(symbol, UNKNOWN_SYMBOL)

    def test_status_texts_fit_and_offline_only_for_confirmed_outage(self):
        outage = run_checks(REFUSED, REFUSED, REFUSED, ACCEPTED)
        for result in outage + [check_once(NO_DESCRIPTORS)]:
            texts = status_texts(result, result.expires_monotonic - VALIDITY)
            self.assertLessEqual(len(texts[0]), 15)
            self.assertEqual("OFFLINE" in texts[0], result is outage[2])

    def test_expired_result_is_stale(self):
        for result in (check_once(ACCEPTED), run_checks(REFUSED, REFUSED, REFUSED)[-1]):
            with self.subTest(state=result.state):
                row, status, _, signal = screens(result, now=result.expires_monotonic)
                self.assertEqual((row, status, signal.kind), ("UNKN", "NAS STALE", rgb.OFF))

    def test_alarm_evaluator_leaves_nas_state_unchanged(self):
        evaluator = AlarmEvaluator(AlarmsConfig())
        for result in run_checks(ACCEPTED, REFUSED, REFUSED, REFUSED, NO_DESCRIPTORS):
            self.assertEqual(evaluator.evaluate([result], NOW), [result])

    def test_enabled_without_result_is_unknown_disabled_is_na(self):
        added = {r.id: r for r in complete_service_results([], OBSERVED, NOW + VALIDITY, monitored={NAS_ID})}
        self.assertEqual((added[NAS_ID].state, added[NAS_ID].summary), (State.UNKNOWN, "NAS: bez dat"))
        added = {r.id: r for r in complete_service_results([], OBSERVED, NOW + VALIDITY)}
        self.assertEqual((added[NAS_ID].state, added[NAS_ID].summary), (State.NA, "NAS: sledování vypnuto"))
        self.assertIs(effective(added[NAS_ID], 10 ** 9).state, State.NA)  # N/A nevyprší
        result = run_checks(REFUSED, REFUSED, REFUSED)[-1]
        for monitored in ((), {NAS_ID}):
            completed = complete_service_results([result], OBSERVED, NOW + VALIDITY, monitored=monitored)
            self.assertEqual([r for r in completed if r.id == NAS_ID], [result])  # výsledek nikdy nenahradí N/A

    def test_simultaneous_nas_and_animedb_outage(self):
        collectors = Collectors(FakeSystem(), animedb_monitor(FakeConnect(REFUSED)), monitor_for(FakeConnect(REFUSED)))
        for index in range(3):
            now = NOW + INTERVAL * index
            measured = collectors.collect(now, OBSERVED)
        states = {r.id: r.state for r in measured}
        self.assertEqual((states[ANIMEDB_ID], states[NAS_ID]), (State.ERROR, State.ERROR))
        results = complete_service_results(measured, OBSERVED, now + VALIDITY, monitored=collectors.labels)
        # Potvrzené výpadky první; NEXUS bez kolektoru za nimi jako UNKNOWN.
        self.assertEqual([status_texts(p, now)[0] for p in active_problems(results, now)],
                         ["ANIMEDB DOWN", "NAS OFFLINE", "NEXUS NO DATA"])
        self.assertEqual(rgb.rgb_signal(measured, rgb.Acknowledgements(), True, False, now),
                         rgb.Signal(rgb.CODES, ((State.ERROR, 2), (State.ERROR, 3))))

    def test_nas_outage_does_not_change_animedb(self):
        # Potvrzování je společný kód, ale paměť má každá kontrola vlastní.
        collectors = Collectors(FakeSystem(), animedb_monitor(HealthySocket), monitor_for(FakeConnect(REFUSED)))
        for index in range(4):
            states = {r.id: r.state for r in collectors.collect(NOW + INTERVAL * index, OBSERVED)}
            self.assertIs(states[ANIMEDB_ID], State.OK)
        self.assertIs(states[NAS_ID], State.ERROR)


def blink_groups(signal, brightness=rgb.BRIGHTNESS_DEFAULT):
    """Skupiny bliknutí jednoho průchodu ze skutečných snímků frame_at: [(barva, počet), ...].

    Snímky po 0,1 s mimo hrany svitu; tma delší než 1 s (pauza 2 s) odděluje skupiny.
    """
    groups, lit, dark = [], False, 0
    for index in range(round(rgb.pass_length(signal.steps) / 0.1)):
        frame = rgb.frame_at(signal, 0.05 + index * 0.1, brightness)
        if frame.lit and not lit:
            if not groups or dark >= 10:
                groups.append([frame.color, 0])
            groups[-1][1] += 1
        dark = 0 if frame.lit else dark + 1
        lit = frame.lit
    return [tuple(group) for group in groups]


class RgbRegressionTest(unittest.TestCase):
    """Schválený RGB automat beze změny: kód kategorie 2 pro NAS a přednosti signálů."""

    def outage(self):
        return run_checks(REFUSED, REFUSED, REFUSED)[-1]

    def test_nas_error_blinks_red_twice(self):
        signal = rgb.rgb_signal(FakeSystem().collect(NOW, OBSERVED) + [self.outage()], rgb.Acknowledgements(),
                                True, False, NOW + 2 * INTERVAL)
        self.assertEqual(blink_groups(signal), [(rgb.RED, 2)])

    def test_nas_and_animedb_error_blink_two_then_three(self):
        animedb = animedb_monitor(FakeConnect(REFUSED))
        animedb_error = [animedb.collect(NOW + INTERVAL * index, OBSERVED)[0] for index in range(3)][-1]
        signal = rgb.rgb_signal(FakeSystem().collect(NOW, OBSERVED) + [animedb_error, self.outage()],
                                rgb.Acknowledgements(), True, False, NOW + 2 * INTERVAL)
        self.assertEqual(blink_groups(signal), [(rgb.RED, 2), (rgb.RED, 3)])

    def test_critical_always_wins_over_nas_error(self):
        critical = CheckResult("disk.root_readonly", State.CRITICAL, "Disk / jen pro čtení", OBSERVED,
                               NOW + 3 * VALIDITY, simulated=True)
        results = FakeSystem().collect(NOW, OBSERVED) + [self.outage(), critical]
        leds = rgb.RgbLeds()
        now = NOW + 2 * INTERVAL
        self.assertEqual(leds.acks.acknowledge(results, now), [NAS_ID])  # CRITICAL potvrdit nejde
        self.assertEqual(leds.wanted(results, True, now).kind, rgb.CRITICAL)
        # CRITICAL přebije rozehranou skupinu NAS okamžitě, ne až na její hranici.
        leds = rgb.RgbLeds()
        leds.frame(results[:-1], True, now)
        self.assertEqual(leds.signal.kind, rgb.CODES)
        frame = leds.frame(results, True, now + 0.2)
        self.assertEqual((leds.signal.kind, frame.color), (rgb.CRITICAL, rgb.RED))

    def test_monitored_nas_without_valid_data_is_unknown_not_na_or_ok(self):
        # Zapnuté sledování, ale kolektor selže: UNKNOWN na SERVICES i ve STATUS, LED neblikají ani nesvítí bíle.
        collectors = Collectors(FakeSystem(), monitor_for(FakeConnect(RuntimeError("chyba"))))
        measured = collectors.collect(NOW, OBSERVED)
        results = complete_service_results(measured, OBSERVED, NOW + VALIDITY, monitored=collectors.labels)
        self.assertEqual(dict(service_rows(results, NOW))["NAS"], "UNKN")
        nas = {r.id: r for r in results}[NAS_ID]
        self.assertEqual(status_texts(nas, NOW)[0], "NAS NO DATA")
        self.assertEqual(rgb.rgb_signal(measured, rgb.Acknowledgements(), True, False, NOW),
                         rgb.Signal(rgb.OFF, reason="chybí platná data (UNKNOWN)"))
        # Zapnuté sledování bez jakéhokoli výsledku (např. první měření ještě neproběhlo).
        [missing] = [r for r in complete_service_results([], OBSERVED, NOW + VALIDITY, monitored={NAS_ID})
                     if r.id == NAS_ID]
        self.assertIs(missing.state, State.UNKNOWN)
        # Vypnuté sledování: N/A, bílou neblokuje.
        disabled = complete_service_results(FakeSystem().collect(NOW, OBSERVED), OBSERVED, NOW + VALIDITY)
        self.assertEqual(dict(service_rows(disabled, NOW))["NAS"], "N/A")
        self.assertEqual(rgb.rgb_signal(FakeSystem().collect(NOW, OBSERVED), rgb.Acknowledgements(), True, False,
                                        NOW).kind, rgb.LIGHT)


class ConfigurationTest(unittest.TestCase):
    """[services.nas]: výchozí vypnuto, adresa jen výslovně, jen číselná IP bez DNS."""

    def test_defaults_are_off_without_address(self):
        self.assertEqual(Config().services.nas, NasConfig(enabled=False, host=None, port=445, timeout_seconds=2))
        self.assertEqual(services_config_from_dict({}).nas, NasConfig())

    def test_example_config_keeps_nas_off_without_address(self):
        nas = load_config(EXAMPLE_CONFIG).services.nas
        self.assertEqual((nas.enabled, nas.host, nas.port, nas.timeout_seconds), (False, None, 445, 2))
        # Ve veřejné ukázce jen zakomentovaná adresa z dokumentačního rozsahu.
        text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
        section = text[text.index("[services.nas]"):]
        self.assertIn('# host = "192.0.2.', section)

    def test_valid_addresses_and_ports(self):
        for host in ("192.0.2.20", "2001:db8::20", "::ffff:192.0.2.20", "198.51.100.7", "127.0.0.1"):
            for port in (1, 445, 65535):
                with self.subTest(host=host, port=port):
                    config = services_config_from_dict({"nas": {"enabled": True, "host": host, "port": port,
                                                                "timeout_seconds": 1.5}})
                    self.assertEqual(config.nas, NasConfig(True, host, port, 1.5))
        self.assertFalse(services_config_from_dict({"nas": {"host": HOST}}).nas.enabled)

    def test_hostnames_and_invalid_addresses_are_rejected(self):
        for host in ("nas.lan", "synology", "localhost", "192.0.2", "192.0.2.256",
                     " 192.0.2.20", "192.0.2.20 ", "192.0.2.20:445", "[2001:db8::20]", "fe80::1%eth0",
                     "0.0.0.0", "::", "", "smb://192.0.2.20", 3221225492, b"192.0.2.20"):
            with self.subTest(host=host):
                with self.assertRaises(ConfigError):
                    services_config_from_dict({"nas": {"host": host}})

    def test_invalid_values(self):
        sections = (
            {"nas": {"enabled": True}},  # zapnuto bez adresy
            {"nas": {"enabled": "true", "host": HOST}},
            {"nas": {"enabled": 1, "host": HOST}},
            {"nas": {"port": 0}},
            {"nas": {"port": 65536}},
            {"nas": {"port": -445}},
            {"nas": {"port": True}},
            {"nas": {"port": "445"}},
            {"nas": {"port": 445.0}},
            {"nas": {"timeout_seconds": 0}},
            {"nas": {"timeout_seconds": 5.5}},
            {"nas": {"timeout_seconds": float("inf")}},
            {"nas": {"timeout_seconds": float("nan")}},
            {"nas": {"timeout_seconds": True}},
            {"nas": {"timeout_seconds": "2"}},
            {"nas": {"host": HOST, "share": "anime"}},  # žádné sdílené složky
            {"nas": {"host": HOST, "username": "nexus"}},  # žádné přihlašování SMB
            {"nas": {"host": HOST, "mount": "/mnt/nas"}},  # žádné mounty
            {"nas": "on"},
        )
        for section in sections:
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    services_config_from_dict(section)

    def test_section_from_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(f'[services.nas]\nenabled = true\nhost = "{HOST_V6}"\nport = 445\ntimeout_seconds = 2\n',
                            encoding="utf-8")
            self.assertEqual(load_config(path).services.nas, NasConfig(True, HOST_V6, 445, 2))
            self.assertEqual(load_config(path).services.animedb, AnimeDbConfig())  # AnimeDB beze změny
            path.write_text("[services.nas]\nenabled = true\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, r"\[services.nas\] enabled = true vyžaduje host"):
                load_config(path)

    def test_disabled_monitoring_never_opens_a_connection(self):
        disabled = (Config(), nas_config(enabled=False, host=HOST), load_config(EXAMPLE_CONFIG))
        forbidden = AssertionError("síťové volání při vypnutém sledování NAS")
        with mock.patch("nexusstat.run.NasMonitor", side_effect=forbidden) as monitor_class, \
                mock.patch("socket.create_connection", side_effect=forbidden) as connect_mock, \
                mock.patch("socket.socket", side_effect=forbidden) as socket_mock:
            for config in disabled:
                monitor = live_monitor(config)
                self.assertNotIn(NAS_ID, monitor.labels)
                completed = complete_service_results([], OBSERVED, NOW + VALIDITY, monitored=monitor.labels)
                self.assertIs({r.id: r for r in completed}[NAS_ID].state, State.NA)
        monitor_class.assert_not_called()
        connect_mock.assert_not_called()
        socket_mock.assert_not_called()

    def test_enabled_monitoring_adds_one_nas_collector(self):
        config = Config(collector=CollectorConfig(interval_seconds=5),
                        services=ServicesConfig(nas=NasConfig(enabled=True, host=HOST, port=4450,
                                                              timeout_seconds=1.5)))
        monitor = live_monitor(config)  # jen vytvoření, nic se nepřipojuje
        scheduled = monitor.monitors[-1]
        self.assertIsInstance(scheduled, Scheduled)  # kontrola jen jednou za interval (SCHED-1)
        nas = scheduled.monitor
        self.assertIsInstance(nas, NasMonitor)
        # Výsledek platí do další kontroly (výchozí 30 min) a ještě obvyklou platnost 3 taktů.
        self.assertEqual((nas.host, nas.port, nas.timeout_seconds, nas.interval_seconds, nas.validity_seconds),
                         (HOST, 4450, 1.5, 1800, 1800 + 15))
        self.assertIs(nas.connect, socket.create_connection)
        inner = [getattr(m, "monitor", m) for m in monitor.monitors]
        self.assertEqual(sum(isinstance(m, NasMonitor) for m in inner), 1)
        # AnimeDB zůstává vypnutá a N/A: zapnutí NAS ji nijak neovlivní.
        self.assertFalse(any(isinstance(m, AnimeDbMonitor) for m in inner))
        self.assertEqual(set(monitor.labels) & {NAS_ID, ANIMEDB_ID}, {NAS_ID})
        self.assertFalse(monitor.simulated)

    def test_both_services_enabled(self):
        config = Config(services=ServicesConfig(AnimeDbConfig(True, ANIMEDB_URL), NasConfig(True, HOST)))
        kinds = [type(getattr(m, "monitor", m)) for m in live_monitor(config).monitors]
        self.assertEqual(kinds[-2:], [AnimeDbMonitor, NasMonitor])  # pořadí jako katalog služeb

    def test_terminal_lines_show_confirmation(self):
        lines = []
        clock = FakeClock(NOW)

        def sleep(seconds):
            if clock.now >= NOW + 2 * INTERVAL:
                raise KeyboardInterrupt  # po třech měřeních
            clock.advance(seconds)

        monitor = Collectors(FakeSystem(), monitor_for(FakeConnect(REFUSED)))
        results = run_loop(monitor, CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=clock,
                           utc_clock=lambda: OBSERVED, sleep=sleep, output=lines.append)
        self.assertIn("NAS: spojení odmítnuto (výpadek zatím nepotvrzen, 1/3)", lines[1])
        # Druhá kontrola (2/3) stav ani důvod nezmění: řádek nevznikne (SCHED-2).
        self.assertIn("NAS: spojení odmítnuto [ERROR]", lines[2])
        self.assertIn("UTC  SIM ERROR", lines[2])  # podvržené spojení = fiktivní data
        self.assertEqual(lines[3], "\nUkončeno (Ctrl+C), počet měření: 3.")
        self.assertFalse(any(HOST in line for line in lines))
        self.assertIs({r.id: r for r in results}[NAS_ID].state, State.ERROR)


class SlowConnect:
    """Podvržené spojení, které spotřebuje `seconds` simulovaného času (nic se skutečně nečeká).

    Při `seconds >= timeout` vyprší limit jako v jádře: hodiny se posunou jen o limit
    a vznikne TimeoutError. Pokus číslo `stop_at` místo toho uprostřed čekání vyvolá
    KeyboardInterrupt, tedy Ctrl+C nebo SIGTERM (run.Stopper) během pomalého spojení.
    """

    def __init__(self, clock, seconds, make_socket, stop_at=None):
        self.clock = clock
        self.seconds = seconds
        self.make_socket = make_socket
        self.stop_at = stop_at
        self.calls = 0

    def __call__(self, address, timeout=None):
        self.calls += 1
        if self.calls == self.stop_at:
            self.clock.advance(min(self.seconds, timeout) / 2)
            raise KeyboardInterrupt
        if self.seconds >= timeout:
            self.clock.advance(timeout)
            raise TimeoutError("timed out")
        self.clock.advance(self.seconds)
        return self.make_socket()


class SlowServicesLoopTest(unittest.TestCase):
    """run_loop se zapnutou AnimeDB i NAS, když kontroly spotřebují část nebo celý limit.

    Smyčka je jednovláknová: kontroly běží po sobě a jejich doba se sčítá. Ověřuje se,
    že plán nezamrzne ani se nezacyklí, výsledky nejsou čerstvější než skutečnost a že
    ukončení během pomalého spojení funguje jako dosud.
    """

    def run_slow(self, interval, animedb_seconds, nas_seconds, measurements, timeout=TIMEOUT):
        clock = FakeClock(NOW)
        validity = 3 * interval
        produced = []  # (plánovaný čas měření `now`, skutečný čas po měření, výsledky)

        class Recorder(Collectors):
            def collect(self, now, observed_utc):
                results = super().collect(now, observed_utc)
                produced.append((now, clock.now, results))
                return results

        sleeps = []

        def sleep(seconds):
            if len(sleeps) > 1000:
                raise AssertionError("smyčka neskončila")
            sleeps.append(seconds)
            clock.advance(seconds)

        animedb = AnimeDbMonitor(validity, ANIMEDB_URL, timeout, clock=clock,
                                 connect=SlowConnect(clock, animedb_seconds, HealthySocket))
        nas = NasMonitor(validity, HOST, PORT, timeout,
                         connect=SlowConnect(clock, nas_seconds, FakeSocket, stop_at=measurements + 1))
        lines = []
        last = run_loop(Recorder(FakeSystem(validity), animedb, nas), CollectorConfig(interval),
                        AlarmEvaluator(AlarmsConfig()), clock=clock,
                        utc_clock=lambda: OBSERVED + timedelta(seconds=clock.now - NOW), sleep=sleep,
                        output=lines.append)
        self.assertEqual(lines[-1], f"\nUkončeno (Ctrl+C), počet měření: {measurements}.")
        self.assertEqual(len(produced), measurements)
        self.assertEqual(last, produced[-1][2])  # přerušené měření nic nepřepsalo
        for now, finished, results in produced:
            for result in results:
                # Čas i platnost od začátku měření: výsledek nikdy nevypadá čerstvější, než je.
                self.assertEqual(result.expires_monotonic, now + validity)
                self.assertEqual(result.observed_utc, OBSERVED + timedelta(seconds=now - NOW))
                self.assertLessEqual(now, finished)
        starts = [now - NOW for now, _, _ in produced]
        states = [{r.id: r.state for r in results} for _, _, results in produced]
        return starts, states, lines

    def test_both_answer_immediately(self):
        starts, states, lines = self.run_slow(10, 0, 0, 3)
        self.assertEqual(starts, [0, 10, 20])
        self.assertEqual({s[NAS_ID] for s in states} | {s[ANIMEDB_ID] for s in states}, {State.OK})
        self.assertFalse(any("Zpoždění" in line for line in lines))

    def test_one_check_uses_two_seconds(self):
        # NAS spotřebuje celé 2 s a vyprší: plán drží, výpadek se potvrdí třetí kontrolou.
        starts, states, lines = self.run_slow(10, 0, 2, 3)
        self.assertEqual(starts, [0, 10, 20])
        self.assertEqual([s[NAS_ID] for s in states], [State.UNKNOWN, State.UNKNOWN, State.ERROR])
        self.assertEqual({s[ANIMEDB_ID] for s in states}, {State.OK})

    def test_both_use_almost_the_whole_limit(self):
        starts, states, lines = self.run_slow(10, 1.9, 1.9, 3)
        self.assertEqual(starts, [0, 10, 20])
        self.assertEqual({s[NAS_ID] for s in states} | {s[ANIMEDB_ID] for s in states}, {State.OK})

    def test_limits_add_up_to_the_interval(self):
        # 2 + 2 s = interval 4 s: měření jdou těsně po sobě, bez čekání, bez hlášky o zpoždění.
        starts, states, lines = self.run_slow(4, 2, 2, 4)
        self.assertEqual(starts, [0, 4, 8, 12])
        self.assertFalse(any("Zpoždění" in line for line in lines))
        self.assertEqual([s[NAS_ID] for s in states], [State.UNKNOWN, State.UNKNOWN, State.ERROR, State.ERROR])

    def test_limits_exceed_the_interval(self):
        # 4 s měření při intervalu 3 s: měří se hned po sobě, zmeškaná měření se nedohánějí
        # a plán se po celém intervalu zpoždění začne znovu. Platnost (9 s) je delší než
        # měření, takže výpadek se pořád potvrdí třemi kontrolami po sobě.
        starts, states, lines = self.run_slow(3, 2, 2, 5)
        self.assertEqual(starts, [0, 4, 8, 12, 16])
        self.assertIn("Zpoždění 3.0 s: zmeškaná měření se nedohánějí, interval začíná znovu.", lines)
        self.assertEqual([s[NAS_ID] for s in states][:3], [State.UNKNOWN, State.UNKNOWN, State.ERROR])

    def test_measurement_longer_than_validity_never_confirms(self):
        # Známé omezení jednovláknové smyčky: 4 s měření při platnosti 3 s (interval 1 s).
        # Každá kontrola přijde až po vypršení předchozího výsledku (mezera), takže se výpadek
        # nikdy nepotvrdí: zůstane UNKNOWN, ne falešný ERROR ani OK.
        starts, states, lines = self.run_slow(1, 2, 2, 4)
        self.assertEqual(starts, [0, 4, 8, 12])
        self.assertEqual({s[NAS_ID] for s in states}, {State.UNKNOWN})
        self.assertEqual({s[ANIMEDB_ID] for s in states}, {State.UNKNOWN})


class FakeRealClock:
    def __init__(self, now=5000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class PreviewTest(unittest.TestCase):
    """Živý náhled: LIVE se zapnutou kontrolou a scénáře NAS 18 a 19, vše bez sítě."""

    def session(self, scenario, connect=None, enabled=True):
        config = nas_config(enabled=enabled, host=HOST)

        def factory(config):
            validity = config.collector.validity_seconds
            monitors = [FakeSystem(validity)]
            nas = config.services.nas
            if nas.enabled:
                monitors.append(NasMonitor(validity, nas.host, nas.port, nas.timeout_seconds, connect=connect))
            return Collectors(*monitors)

        self.real = FakeRealClock()
        return PreviewSession(config, scenario=scenario, clock=SessionClock(real_clock=self.real),
                              utc_clock=lambda: OBSERVED, wall_clock=lambda: datetime(2026, 10, 28, 13, 0),
                              monitor_factory=factory)

    def next_measurement(self, session):
        self.real.advance(INTERVAL)
        session._refresh_if_due(session.clock.now())

    def view(self, session):
        """(SERVICES, texty STATUS, požadovaný signál LED) v aktuálním čase okna.

        Požadovaný signál, ne právě přehrávaný: přehrávač ho převezme až po dohrání
        rozehrané skupiny bliknutí (to ověřuje test potvrzení).
        """
        now = session.clock.now()
        session.led_frame()
        signal = rgb.rgb_signal(session.monitored, session.leds.acks, True, False, now)
        return (dict(service_rows(session.results, now))["NAS"],
                [status_texts(p, now)[0] for p in active_problems(session.results, now)], signal)

    def test_live_outage_blinks_only_after_confirmation(self):
        session = self.session(LIVE, FakeConnect(REFUSED))
        session.press()  # probuzení OLED
        frames = [session.led_frame()]
        for _ in range(2):
            self.next_measurement(session)
            frames.append(session.led_frame())
        self.assertEqual([frame.lit for frame in frames[:2]], [False, False])  # nepotvrzeno: tma, ne bílá
        self.assertEqual(frames[2].color, rgb.RED)  # potvrzeno: červený kód 2
        row, status, signal = self.view(session)
        # LIVE na notebooku: NEXUS nemá kolektor, ve STATUS je za potvrzeným výpadkem.
        self.assertEqual((row, status, signal.steps),
                         ("ERR", ["NAS OFFLINE", "NEXUS NO DATA"], ((State.ERROR, 2),)))

    def test_live_reachable_gives_white_and_disabled_is_na(self):
        session = self.session(LIVE, FakeConnect(ACCEPTED))
        session.press()
        self.assertEqual(session.led_frame().color, rgb.WHITE)
        self.assertEqual(self.view(session)[0], "OK")
        self.assertIn("NAS ze spojení TCP na port SMB", session.source_text())
        connect = FakeConnect(ACCEPTED)
        session = self.session(LIVE, connect, enabled=False)
        session.press()
        self.assertEqual((self.view(session)[0], session.led_frame().color), ("N/A", rgb.WHITE))
        self.assertEqual(connect.calls, [])
        self.assertIn("NAS N/A (sledování vypnuto)", session.source_text())
        self.assertIn("AnimeDB N/A (sledování vypnuto)", session.source_text())

    def test_outage_scenario_runs_the_real_confirmation_without_network(self):
        forbidden = AssertionError("síťové volání ve scénáři NAS")
        with mock.patch("socket.create_connection", side_effect=forbidden), \
                mock.patch("socket.socket", side_effect=forbidden):
            session = self.session("nas_outage")
            self.assertIsInstance(session.nas, NasMonitor)
            self.assertIsInstance(session.nas.connect, ScriptedConnection)
            self.assertTrue(session.nas.simulated)
            session.press()
            stages = [self.view(session)]
            for _ in range(11):
                self.next_measurement(session)
                stages.append(self.view(session))
        rows = [row for row, _, _ in stages]
        self.assertEqual(rows, ["OK", "UNKN", "UNKN"] + ["ERR"] * 7 + ["OK", "OK"])
        self.assertEqual([status for _, status, _ in stages[:4]], [[], ["NAS FAILING"], ["NAS FAILING"],
                                                                     ["NAS OFFLINE"]])
        self.assertEqual(stages[9][1], ["NAS RECOVER"])
        kinds = [signal.kind for _, _, signal in stages]
        self.assertEqual(kinds[:3], [rgb.LIGHT, rgb.OFF, rgb.OFF])  # 1. a 2. pokus nebliká
        self.assertEqual({signal for _, _, signal in stages[3:10]}, {rgb.Signal(rgb.CODES, ((State.ERROR, 2),))})
        self.assertEqual(kinds[10:], [rgb.LIGHT, rgb.LIGHT])
        # Okno ukáže pořadí pokusu a důvod, které OLED (NAS FAILING) nerozliší.
        self.assertIn("NAS: spojení odmítnuto (výpadek zatím nepotvrzen, 1/3)", self._source_at(1))
        self.assertIn("NAS: bez odpovědi do 2 s (výpadek zatím nepotvrzen, 2/3)", self._source_at(2))

    def _source_at(self, measurements):
        session = self.session("nas_outage")
        for _ in range(measurements):
            self.next_measurement(session)
        return session.source_text()

    def test_outage_scenario_starts_again_after_switch(self):
        session = self.session("nas_outage")
        for _ in range(3):
            self.next_measurement(session)
        self.assertEqual(self.view(session)[0], "ERR")
        session.set_scenario("ok")
        self.assertIsNone(session.nas)
        session.set_scenario("nas_outage")
        self.assertEqual(self.view(session)[0], "OK")

    def test_simultaneous_scenario_matches_real_collectors(self):
        session = self.session("nas_animedb")
        by_id = {r.id: r for r in session.monitored}
        real_nas = run_checks(REFUSED, REFUSED, REFUSED)[-1]
        animedb = animedb_monitor(FakeConnect(REFUSED))
        real_animedb = [animedb.collect(NOW + INTERVAL * i, OBSERVED)[0] for i in range(3)][-1]
        for scenario, real in ((by_id[NAS_ID], real_nas), (by_id[ANIMEDB_ID], real_animedb)):
            with self.subTest(check=real.id):
                self.assertEqual((scenario.state, scenario.summary, dict(scenario.details)),
                                 (real.state, real.summary, dict(real.details)))
        session.press()
        self.assertEqual(self.view(session)[1:], (["ANIMEDB DOWN", "NAS OFFLINE"],
                                                  rgb.Signal(rgb.CODES, ((State.ERROR, 2), (State.ERROR, 3)))))

    def test_acknowledgement_lets_the_current_group_finish(self):
        session = self.session("nas_animedb")
        session.press()
        self.assertTrue(session.led_frame().lit)  # t = 0: první bliknutí skupiny NAS (2×)
        self.real.advance(0.2)
        self.assertEqual(set(session.acknowledge()), {NAS_ID, ANIMEDB_ID})
        # Rozehraná skupina NAS se dohraje (druhé bliknutí 0,8–1,2 s), pak LED zhasnou
        # v pauze a skupina AnimeDB (3×) se už nepřehraje.
        self.real.advance(0.7)  # t = 0,9 s
        self.assertEqual((session.leds.signal.kind, session.led_frame().lit), (rgb.CODES, True))
        self.real.advance(0.8)  # t = 1,7 s: pauza po skupině
        self.assertFalse(session.led_frame().lit)
        self.assertEqual(session.leds.signal, rgb.Signal(rgb.OFF, reason="potvrzená závada trvá"))
        for _ in range(12):  # 1,7–6,5 s: tady by jinak blikala skupina AnimeDB (3,6–6,0 s)
            self.real.advance(0.4)
            self.assertFalse(session.led_frame().lit)
        now = session.clock.now()
        self.assertEqual({p.id: p.state for p in active_problems(session.results, now)},
                         {NAS_ID: State.ERROR, ANIMEDB_ID: State.ERROR})  # závada trvá, OLED ji ukazuje
        self.assertEqual(home_symbol(session.results, now), ALERT_SYMBOL)


if __name__ == "__main__":
    unittest.main()
