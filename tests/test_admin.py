"""Testy chráněné administrace lokálního webu (M6-4B).

Data jsou syntetická, soubory jen v dočasných adresářích. Certifikát a klíč
pro testy vytvoří openssl v dočasném adresáři (aplikace sama nic negeneruje);
server běží jen na 127.0.0.1 s volným portem. Souběh a timeouty se řídí
událostmi a podstrčenými Future, ne náhodným čekáním.
"""

import contextlib
import http.client
import inspect
import io
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

from nexusstat import admin, history, history_link, history_worker, web
from nexusstat.admin import (ACKNOWLEDGE, BRIGHTNESS, COOKIE, DONE, LOGIN, LOGIN_BLOCKED_EVENT, LOGIN_FAILED_EVENT,
                             LOGOUT, REFUSED, UNCERTAIN, Admin, AdminError, LoginGuard, Sessions,
                             check_new_password, hash_password, load_password_file, parse_hash, write_password_file)
from nexusstat.alarms import AlarmEvaluator
from nexusstat.config import AlarmsConfig, CollectorConfig
from nexusstat.core import CheckResult, State
from nexusstat.run import main, run_loop
from nexusstat.services import BACKUP_ID, NAS_ID
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID
from nexusstat.user_state import Acknowledge, Reply, RequestInterrupted, SetBrightness, UserState
from nexusstat.web import ADMIN_OFF, StateBoard, start_web, stop_web
from nexusstat.web_tls import tls_context

UTC = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
PASSWORD = "správné heslo 42"
OPENSSL = shutil.which("openssl")


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeHash:
    """Otisk bez scrypt pro testy omezení pokusů; počítá ověření."""

    def __init__(self, secret=PASSWORD):
        self.secret, self.checked = secret, 0

    def verify(self, password):
        self.checked += 1
        return password == self.secret


def result(check_id, state, expires=None, simulated=False):
    return CheckResult(check_id, state, "popis", UTC, time.monotonic() + 600 if expires is None else expires,
                       simulated=simulated)


def headers(origin="https://127.0.0.1:8765", cookie=None, extra=()):
    message = http.client.HTTPMessage()
    if origin is not None:
        message["Origin"] = origin
    if cookie is not None:
        message["Cookie"] = cookie
    for name, value in extra:
        message[name] = value
    return message


def form(**fields):
    return urlencode(fields).encode("ascii")


def make_certificate(directory):
    """Testovací certifikát s SAN 127.0.0.1 a localhost; klíč s oprávněními 0600."""
    cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"],
                   check=True, capture_output=True, timeout=30)
    os.chmod(key, 0o600)
    return cert, key


class Ticker:
    """Simulovaná hlavní smyčka: vyřizuje požadavky UserState, dokud test běží."""

    def __init__(self, state, results):
        self.state, self.results, self.stop = state, results, threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            self.state.tick(self.results(), time.monotonic(), UTC)
            self.stop.wait(0.005)

    def close(self):
        self.stop.set()
        self.thread.join(5)


class PasswordTest(unittest.TestCase):
    def test_salted_scrypt_hash(self):
        line = hash_password(PASSWORD)
        self.assertTrue(line.startswith("scrypt$16384$8$1$"))
        self.assertNotIn(PASSWORD, line)
        self.assertNotEqual(line, hash_password(PASSWORD))  # jiná sůl
        parsed = parse_hash(line)
        self.assertTrue(parsed.verify(PASSWORD))
        self.assertFalse(parsed.verify(PASSWORD + " "))
        self.assertNotIn(admin._b64(parsed.key), repr(parsed))  # otisk se nevypíše ani omylem
        for broken in ("", "bcrypt$1$2$3$a$b", line.replace("$8$", "$9$"), line.replace("16384", "1024"),
                       line.replace("16384", "20000"), line[:-10] + "!!!!!!!!!\n"):
            with self.subTest(broken=broken[:30]), self.assertRaises(AdminError):
                parse_hash(broken)

    def test_new_password_rules(self):
        # Jediné pravidlo: aspoň 4 znaky; čísla i jiné znaky a délka bez dalších podmínek.
        for good in ("1234", "12345", "123456", "1234567", "abcd", "žluť", " a b", "dlouhé heslo s mezerami",
                     "x" * 1015):
            with self.subTest(good=good[:12]):
                self.assertEqual(check_new_password(good), good)
        for bad in ("", "1", "123", "abc", "ž  "):
            with self.subTest(bad=bad), self.assertRaisesRegex(AdminError, "aspoň 4 znaky"):
                check_new_password(bad)
        # Jen technická mez: heslo, které se nevejde do přihlašovacího formuláře (1 KiB), by nešlo zadat.
        with self.assertRaisesRegex(AdminError, "formulářem"):
            check_new_password("x" * 1016)

    def test_private_file_written_atomically_and_loaded_strictly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.scrypt"
            write_password_file(path, PASSWORD)
            self.assertEqual((path.stat().st_mode & 0o777, sorted(os.listdir(directory))), (0o600, ["admin.scrypt"]))
            self.assertTrue(load_password_file(path).verify(PASSWORD))
            with self.assertRaises(AdminError):
                write_password_file(path, "jiné dlouhé heslo")  # bez --replace se nepřepíše
            write_password_file(path, "jiné dlouhé heslo", replace=True)
            self.assertTrue(load_password_file(path).verify("jiné dlouhé heslo"))
            os.chmod(path, 0o640)
            with self.assertRaisesRegex(AdminError, "jen vlastník"):
                load_password_file(path)
            os.chmod(path, 0o600)
            link = Path(directory) / "link.scrypt"
            link.symlink_to(path)
            with self.assertRaisesRegex(AdminError, "nelze otevřít"):
                load_password_file(link)
            with mock.patch("nexusstat.admin.os.geteuid", return_value=os.geteuid() + 1), \
                    self.assertRaisesRegex(AdminError, "jinému uživateli"):
                load_password_file(path)
            path.write_text("nesmysl\n", encoding="ascii")
            with self.assertRaises(AdminError):
                load_password_file(path)
            with self.assertRaisesRegex(AdminError, "nelze otevřít"):
                load_password_file(Path(directory) / "chybí.scrypt")
        inside = admin.PROJECT_ROOT / "config" / "admin.scrypt"
        with self.assertRaisesRegex(AdminError, "repozitáři"):
            write_password_file(inside, PASSWORD)
        self.assertFalse(inside.exists())

    def test_command_never_prints_the_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.scrypt"
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("nexusstat.admin.getpass.getpass", side_effect=[PASSWORD, PASSWORD]), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                admin.main([str(path)])
            self.assertTrue(load_password_file(path).verify(PASSWORD))
            for bad in ([PASSWORD, PASSWORD + "x"], ["abc", "abc"]):
                with mock.patch("nexusstat.admin.getpass.getpass", side_effect=bad), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                        self.assertRaises(SystemExit):
                    admin.main([str(path), "--replace"])
            self.assertTrue(load_password_file(path).verify(PASSWORD))  # nic se nezapsalo
            self.assertNotIn(PASSWORD, out.getvalue() + err.getvalue())
            self.assertNotIn("abc", err.getvalue())
            with mock.patch("nexusstat.admin.getpass.getpass", side_effect=["1234", "1234"]), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                admin.main([str(path), "--replace"])  # i čtyřmístný PIN
            self.assertTrue(load_password_file(path).verify("1234"))


class LoginGuardTest(unittest.TestCase):
    def test_global_lockout_grows_and_skips_scrypt(self):
        clock, secret, lines = Clock(), FakeHash(), []
        guard = LoginGuard(secret, clock, lines.append)
        # Rozhraní nezná adresu klienta: omezení je společné pro všechny.
        self.assertEqual(list(inspect.signature(guard.attempt).parameters), ["password"])
        for _ in range(4):
            self.assertEqual(guard.attempt("špatně"), (False, "nesprávné heslo"))
        self.assertEqual(guard.attempt("špatně"), (False, "příliš mnoho chybných pokusů; zkuste to za 60 s"))
        checked = secret.checked
        clock.now += 59
        self.assertEqual(guard.attempt(PASSWORD), (False, "příliš mnoho chybných pokusů; zkuste to za 1 s"))
        self.assertEqual(secret.checked, checked)  # během blokace se scrypt nepočítá
        clock.now += 1
        for expected in (300, 900, 900):  # každá další blokace delší, nejvýš 900 s
            for _ in range(5):
                reason = guard.attempt("špatně")[1]
            self.assertEqual(reason, f"příliš mnoho chybných pokusů; zkuste to za {expected} s")
            clock.now += expected
        self.assertEqual(guard.attempt(PASSWORD), (True, ""))  # úspěch vše vynuluje
        for _ in range(5):
            reason = guard.attempt("špatně")[1]
        self.assertIn("za 60 s", reason)
        self.assertEqual(len(lines), 5)  # jeden řádek za blokaci, ne za pokus
        self.assertTrue(all("špatně" not in line and PASSWORD not in line for line in lines))
        clock.now += 60
        before = secret.checked
        self.assertFalse(guard.attempt("abc")[0])  # kratší než 4 znaky: chybný pokus bez výpočtu scrypt
        self.assertEqual(secret.checked, before)

    def test_one_scrypt_at_a_time_for_concurrent_attempts(self):
        inside, peak, lock = [0], [0], threading.Lock()

        class SlowHash(FakeHash):
            def verify(self, password):
                with lock:
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                time.sleep(0.01)
                with lock:
                    inside[0] -= 1
                return False

        guard = LoginGuard(SlowHash(), Clock(), lambda line: None)
        barrier = threading.Barrier(8)
        threads = [threading.Thread(target=lambda: (barrier.wait(5), guard.attempt("špatně"))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(peak[0], 1)
        self.assertEqual(guard.verifications, 5)  # po pěti chybách blokace pro všechny


class AuditTest(unittest.TestCase):
    """Auditní události chybného přihlášení a blokace (M6-4B)."""

    def guard(self, on_event):
        self.clock = Clock()
        return LoginGuard(FakeHash(), self.clock, lambda line: None, on_event,
                          utc_clock=lambda: UTC)

    def test_failures_and_lockout_are_distinct_events_without_secrets(self):
        events = []
        guard = self.guard(lambda *event: events.append(event))
        self.assertTrue(guard.attempt(PASSWORD)[0])  # úspěch se nezapisuje
        for _ in range(5):
            guard.attempt("tajné-špatně-1")
        self.assertEqual([(kind, severity) for kind, severity, *_ in events],
                         [(LOGIN_FAILED_EVENT, "WARNING")] * 5 + [(LOGIN_BLOCKED_EVENT, "ERROR")])
        self.assertEqual(events[-1][5], {"failures": 5, "seconds": 60})
        self.assertEqual([event[5]["failures"] for event in events[:5]], [1, 2, 3, 4, 5])
        self.assertTrue(all(event[3] == UTC and event[4] is None for event in events))
        for _ in range(1000):  # záplava během blokace: žádný zápis, jen počet
            guard.attempt("tajné-špatně-2")
        self.assertEqual(len(events), 6)
        self.clock.now += 60
        guard.attempt("tajné-špatně-3")
        self.assertEqual(events[-1][5], {"failures": 1, "refused_while_blocked": 1000})
        text = repr(events)
        for secret in ("tajné", PASSWORD):
            self.assertNotIn(secret, text)

    def test_audit_bound_per_lockout_window(self):
        events = []
        guard = self.guard(lambda *event: events.append(event))
        for window in range(6):  # pět chyb a blokace v každém okně, mezi tím záplava odmítnutí
            for _ in range(5):
                guard.attempt("špatně")
            for _ in range(200):
                guard.attempt("špatně")
            self.clock.now += admin.LOCKOUT_SECONDS[min(window, 2)]
        self.assertEqual(len(events), 6 * (admin.FREE_FAILURES + 1))
        self.assertEqual(guard.verifications, 6 * admin.FREE_FAILURES)

    def test_broken_or_missing_history_never_blocks_login(self):
        def broken(*event):
            raise RuntimeError("zapisovač nedostupný")

        for on_event in (None, broken):
            guard = self.guard(on_event)
            for _ in range(5):
                self.assertFalse(guard.attempt("špatně")[0])
            self.clock.now += 60
            self.assertEqual(guard.attempt(PASSWORD), (True, ""))

    def test_events_go_through_the_bounded_writer_queue(self):
        # Skutečný HistoryWorker (bez vlákna a databáze): submit_event jen vloží do omezené fronty.
        worker = history_worker.HistoryWorker(Path("/nonexistent/history.sqlite3"), "test", "0" * 16,
                                              app_version="test", interval_s=10, event_capacity=8)
        link = history_link.HistoryLink(worker, output=lambda line: None)
        guard = LoginGuard(FakeHash(), Clock(), lambda line: None, link.record_event, utc_clock=lambda: UTC)
        started = time.perf_counter()
        for _ in range(5):
            guard.attempt("špatně")
        self.assertLess(time.perf_counter() - started, 1)  # na I/O se nečeká
        queued = [item.event for item in worker._events]
        self.assertEqual([event.kind for event in queued], [LOGIN_FAILED_EVENT] * 5 + [LOGIN_BLOCKED_EVENT])
        self.assertEqual({event.ts_quality for event in queued}, {"unsynced"})  # bez doložené synchronizace
        self.assertNotIn("špatně", repr([event.row(None) for event in queued]))
        for _ in range(20):
            link.record_event(LOGIN_FAILED_EVENT, "WARNING", "x", UTC)
        self.assertEqual(len(worker._events), 8)  # fronta zůstane omezená, nejstarší se započte jako ztráta


class SessionTest(unittest.TestCase):
    def test_lifetime_limit_and_hashed_keys(self):
        clock = Clock()
        sessions = Sessions(clock)
        token, session = sessions.create()
        self.assertTrue(admin.TOKEN.fullmatch(token) and admin.TOKEN.fullmatch(session.csrf))
        self.assertNotIn(token, repr(sessions._sessions))  # v paměti jen otisk tokenu
        clock.now += admin.SESSION_SECONDS - 0.1
        self.assertIs(sessions.find(token), session)  # bez prodlužování
        clock.now += 0.1
        self.assertIsNone(sessions.find(token))
        tokens = [sessions.create()[0] for _ in range(admin.MAX_SESSIONS + 2)]
        self.assertEqual(len(sessions), admin.MAX_SESSIONS)
        self.assertIsNone(sessions.find(tokens[0]))  # nejstarší vytlačená
        sessions.end(tokens[-1])
        self.assertIsNone(sessions.find(tokens[-1]))


class FakeState:
    """Podstrčený vlastník stavu: jen request() a view, odpověď řídí test."""

    def __init__(self, make_future=Future):
        self.commands, self.make_future = [], make_future
        self.view = SimpleNamespace(brightness=20, acknowledged=())

    def request(self, command):
        self.commands.append(command)
        return self.make_future()


class AdminLogicTest(unittest.TestCase):
    HOST = "127.0.0.1:8765"

    def setUp(self):
        self.clock = Clock(time.monotonic())
        self.lines = []
        self.board = StateBoard({NAS_ID, BACKUP_ID, CPU_TEMP_ID})
        self.state = UserState(output=self.lines.append)
        self.admin = Admin(FakeHash(), self.state, self.board, clock=self.clock, output=self.lines.append)
        self.faults = [result(NAS_ID, State.WARNING), result(BACKUP_ID, State.CRITICAL),
                       result(CPU_USAGE_ID, State.INFO)]
        self.board.publish(self.faults, self.clock(), UTC)
        self.ticker = Ticker(self.state, lambda: self.faults)
        self.addCleanup(self.ticker.close)

    def post(self, path, cookie=None, origin="https://127.0.0.1:8765", **fields):
        return self.admin.post(path, self.HOST, headers(origin, cookie), form(**fields))

    def login(self):
        response = self.post(LOGIN, password=PASSWORD)
        self.assertEqual(response.status, 303)
        cookie = response.headers["Set-Cookie"].split(";")[0]
        page = self.admin.get(headers(cookie=cookie)).html
        return cookie, re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def test_login_cookie_and_wrong_password(self):
        response = self.post(LOGIN, password="špatně")
        self.assertEqual(response.status, 403)
        self.assertNotIn("Set-Cookie", response.headers)
        response = self.post(LOGIN, password=PASSWORD)
        cookie = response.headers["Set-Cookie"]
        self.assertRegex(cookie, rf"^{COOKIE}=[A-Za-z0-9_-]{{43}}; Path=/; Secure; HttpOnly; SameSite=Strict; "
                                 r"Max-Age=600$")
        self.assertNotIn("Domain", cookie)
        self.assertIn("form-action 'self'", response.headers["Content-Security-Policy"])

    def test_origin_and_csrf_are_required(self):
        cookie, csrf = self.login()
        for origin in (None, "", "null", "http://127.0.0.1:8765", "https://evil.example", "https://127.0.0.1:8766",
                       "https://127.0.0.1:8765/", "https://127.0.0.1", "HTTPS://127.0.0.1:8765",
                       "https://127.0.0.1:8765 ", "https://localhost:8765"):
            with self.subTest(origin=origin):
                self.assertEqual(self.post(BRIGHTNESS, cookie, origin, csrf=csrf, percent="50").status, 403)
        doubled = self.admin.post(BRIGHTNESS, self.HOST, headers(cookie=cookie, extra=[("Origin", "https://"
                                  + self.HOST)]), form(csrf=csrf, percent="50"))
        self.assertEqual(doubled.status, 403)  # dvě hlavičky Origin
        other_cookie, other_csrf = self.login()
        for bad in ("", "x" * 43, other_csrf):
            with self.subTest(csrf=bad[:5]):
                self.assertEqual(self.post(BRIGHTNESS, cookie, csrf=bad, percent="50").status, 403)
        self.assertEqual(self.post(BRIGHTNESS, None, csrf=csrf, percent="50").status, 403)  # bez cookie
        self.assertEqual(self.state.brightness, 20)  # nic z toho nic nezměnilo
        self.assertEqual(self.post(BRIGHTNESS, cookie, csrf=csrf, percent="50").status, 200)
        self.assertEqual(self.state.brightness, 50)

    def test_strict_form_fields(self):
        cookie, csrf = self.login()
        for body in (form(csrf=csrf), form(csrf=csrf, percent="50", extra="1"),
                     form(csrf=csrf, percent="50") + b"&percent=60", b"csrf=" + csrf.encode() + b"&percent=\xff",
                     b"\x00", b"=&="):
            with self.subTest(body=body[:20]):
                response = self.admin.post(BRIGHTNESS, self.HOST, headers(cookie=cookie), body)
                self.assertEqual(response.status, 400)
        for percent in ("abc", "1000", "-5", "5.5"):
            with self.subTest(percent=percent):
                self.assertEqual(self.post(BRIGHTNESS, cookie, csrf=csrf, percent=percent).status, 400)
        self.assertEqual(self.post(BRIGHTNESS, cookie, csrf=csrf, percent="4").status, 409)  # mimo rozsah
        self.assertEqual(self.post("/admin/settings", cookie, csrf=csrf).status, 404)  # žádné skryté akce
        self.assertEqual(self.state.brightness, 20)

    def test_session_expires_after_ten_minutes(self):
        cookie, csrf = self.login()
        self.clock.now += admin.SESSION_SECONDS
        response = self.post(BRIGHTNESS, cookie, csrf=csrf, percent="50")
        self.assertEqual(response.status, 403)
        self.assertIn("vypršelo", response.html)
        self.assertEqual(self.state.brightness, 20)
        self.assertIn('name="password"', self.admin.get(headers(cookie=cookie)).html)

    def test_acknowledge_one_fault_with_seen_severity(self):
        cookie, csrf = self.login()
        page = self.admin.get(headers(cookie=cookie)).html
        self.assertIn(f'name="check_id" value="{NAS_ID}"', page)
        self.assertNotIn(f'name="check_id" value="{BACKUP_ID}"', page)  # CRITICAL bez tlačítka
        self.assertIn("CRITICAL nelze umlčet", page)
        response = self.post(ACKNOWLEDGE, cookie, csrf=csrf, check_id=NAS_ID, state="WARNING")
        self.assertEqual(response.status, 200)
        self.assertIn("Provedeno: potvrzeno", response.html)
        self.assertEqual(self.state.view.acknowledged, ((NAS_ID, State.WARNING),))
        self.assertEqual(self.board.snapshot().results[0].state, State.WARNING)  # stav závady beze změny
        page = self.admin.get(headers(cookie=cookie)).html
        self.assertIn("Potvrzeno (WARNING)", page)
        again = self.post(ACKNOWLEDGE, cookie, csrf=csrf, check_id=NAS_ID, state="WARNING")
        self.assertIn("už potvrzeno", again.html)  # opakovaný požadavek nic nezdvojí

    def test_worse_fault_and_critical_are_refused(self):
        cookie, csrf = self.login()
        self.faults[0] = result(NAS_ID, State.ERROR)  # zhoršilo se po vykreslení stránky s WARNING
        response = self.post(ACKNOWLEDGE, cookie, csrf=csrf, check_id=NAS_ID, state="WARNING")
        self.assertEqual(response.status, 409)
        self.assertIn("zhoršila", response.html)
        for state in ("CRITICAL", "WARNING", "ERROR"):
            with self.subTest(state=state):
                response = self.post(ACKNOWLEDGE, cookie, csrf=csrf, check_id=BACKUP_ID, state=state)
                self.assertEqual(response.status, 409)
                self.assertIn("CRITICAL", response.html)
        for check_id, state in (("NAS", "WARNING"), (NAS_ID, "OK"), (NAS_ID, "warning")):
            self.assertEqual(self.post(ACKNOWLEDGE, cookie, csrf=csrf, check_id=check_id, state=state).status, 400)
        self.assertEqual(self.state.acks.records, {})

    def test_concurrent_identical_requests(self):
        cookie, csrf = self.login()
        barrier, responses = threading.Barrier(4), []

        def acknowledge():
            barrier.wait(5)
            responses.append(self.post(ACKNOWLEDGE, cookie, csrf=csrf, check_id=NAS_ID, state="WARNING"))

        threads = [threading.Thread(target=acknowledge) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual([response.status for response in responses], [200] * 4)
        self.assertEqual(sum("Provedeno: potvrzeno (" in response.html for response in responses), 1)
        self.assertEqual(len([line for line in self.lines if "potvrzena závada" in line]), 1)

    def test_logout_ends_only_that_session(self):
        cookie, csrf = self.login()
        other_cookie, other_csrf = self.login()
        response = self.post(LOGOUT, cookie, csrf=csrf)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers["Set-Cookie"].endswith("Max-Age=0"))
        self.assertEqual(self.post(BRIGHTNESS, cookie, csrf=csrf, percent="50").status, 403)
        self.assertEqual(self.post(LOGOUT, cookie, csrf=csrf).status, 403)  # odhlášená relace nic neumí
        self.assertEqual(self.post(BRIGHTNESS, other_cookie, csrf=other_csrf, percent="50").status, 200)

    def test_concurrent_logins_at_the_session_limit(self):
        barrier, responses = threading.Barrier(8), []

        def login():
            barrier.wait(5)
            responses.append(self.post(LOGIN, password=PASSWORD))

        threads = [threading.Thread(target=login) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual([response.status for response in responses], [303] * 8)
        self.assertEqual(len(self.admin.sessions), admin.MAX_SESSIONS)  # nikdy víc než limit
        cookies = [response.headers["Set-Cookie"].split(";")[0] for response in responses]
        valid = [cookie for cookie in cookies if self.admin._session(headers(cookie=cookie))[1] is not None]
        self.assertEqual(len(valid), admin.MAX_SESSIONS)

    def test_session_is_never_taken_from_url_or_form(self):
        cookie, csrf = self.login()
        token = cookie.split("=", 1)[1]
        # Token jen v poli formuláře nebo jako jiný název cookie neplatí.
        response = self.admin.post(BRIGHTNESS, self.HOST, headers(), form(csrf=csrf, percent="50", session=token))
        self.assertEqual(response.status, 400)
        for fake in (f"session={token}", f"{COOKIE.lower()}={token}", f"{COOKIE}={token}; {COOKIE}={token}"):
            with self.subTest(cookie=fake[:20]):
                self.assertEqual(self.post(BRIGHTNESS, fake, csrf=csrf, percent="50").status, 403)
        self.assertEqual(self.state.brightness, 20)

    def test_http_threads_only_request_and_read(self):
        # Administrace sahá na vlastníka stavu jen přes request() a neměnný view.
        owner = mock.Mock(spec=["request", "view"])
        owner.view = SimpleNamespace(brightness=20, acknowledged=())
        owner.request.side_effect = lambda command: done_future(Reply(True, "jas nastaven", True))
        guarded = Admin(FakeHash(), owner, self.board, clock=self.clock, output=lambda line: None)
        response = guarded.post(LOGIN, self.HOST, headers(), form(password=PASSWORD))
        cookie = response.headers["Set-Cookie"].split(";")[0]
        csrf = re.search(r'name="csrf" value="([^"]+)"', guarded.get(headers(cookie=cookie)).html).group(1)
        guarded.post(BRIGHTNESS, self.HOST, headers(cookie=cookie), form(csrf=csrf, percent="50"))
        owner.request.assert_called_once_with(SetBrightness(50))


def done_future(reply):
    future = Future()
    future.set_result(reply)
    return future


class OutcomeTest(unittest.TestCase):
    """Provedeno, odmítnuto a nejisté podle stavu Future (bez čekání: limity 0)."""

    def ask(self, make_future):
        owner = FakeState(make_future)
        handler = Admin(FakeHash(), owner, StateBoard(), output=lambda line: None, reply_seconds=0, grace_seconds=0)
        outcome = handler.ask(SetBrightness(50))
        return outcome, owner

    def test_cancelled_before_claim_is_a_refusal(self):
        futures = []
        outcome, _ = self.ask(lambda: futures.append(Future()) or futures[-1])
        self.assertEqual(outcome.kind, REFUSED)
        self.assertTrue(futures[0].cancelled())  # zrušení je důkaz, že se nic neprovede

    def test_running_request_is_uncertain_not_refused(self):
        def running():
            future = Future()
            future.set_running_or_notify_cancel()  # smyčka ho už převzala: zrušit nejde
            return future

        outcome, _ = self.ask(running)
        self.assertEqual(outcome.kind, UNCERTAIN)
        self.assertIn("ověřte", outcome.text)

    def test_answer_arriving_after_failed_cancel_is_used(self):
        class LateFuture(Future):
            def __init__(self):
                super().__init__()
                self.set_running_or_notify_cancel()

            def cancel(self):
                cancelled = super().cancel()
                self.set_result(Reply(True, "jas nastaven", True))  # odpověď přijde právě teď
                return cancelled

        outcome, _ = self.ask(LateFuture)
        self.assertEqual((outcome.kind, outcome.text), (DONE, "Provedeno: jas nastaven (uloženo do souboru stavu)."))

    def test_interrupted_refused_and_unsaved_replies(self):
        interrupted = Future()
        interrupted.set_exception(RequestInterrupted("konec"))
        self.assertEqual(self.ask(lambda: interrupted)[0].kind, UNCERTAIN)
        self.assertEqual(self.ask(lambda: done_future(Reply(False, "jas beze změny?")))[0].kind, REFUSED)
        outcome, _ = self.ask(lambda: done_future(Reply(True, "jas nastaven", False)))
        self.assertEqual(outcome.kind, DONE)
        self.assertIn("zatím není uloženo", outcome.text)


@unittest.skipUnless(OPENSSL, "testovací certifikát vyžaduje openssl")
class HttpsTest(unittest.TestCase):
    """Skutečný server HTTPS na 127.0.0.1 s administrací a simulovanou smyčkou."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.cert, cls.key = make_certificate(cls.directory.name)
        cls.password_hash = parse_hash(hash_password(PASSWORD))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.lines = []
        self.board, self.server = start_web({NAS_ID, BACKUP_ID}, 30, port=0, poll_seconds=0.01,
                                            device_factory=lambda: None, tls=tls_context(self.cert, self.key))
        self.addCleanup(stop_web, self.server)
        self.port = self.server.server_address[1]
        self.state = UserState(output=self.lines.append)
        self.events = []
        self.server.admin = Admin(self.password_hash, self.state, self.board, output=self.lines.append,
                                  on_event=lambda *event: self.events.append(event))
        self.faults = [result(NAS_ID, State.ERROR)]
        self.board.publish(self.faults, time.monotonic(), UTC)
        ticker = Ticker(self.state, lambda: self.faults)
        self.addCleanup(ticker.close)
        self.context = ssl.create_default_context(cafile=str(self.cert))

    def request(self, method, path, body=None, extra=None, length=True):
        connection = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=5, context=self.context)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", f"127.0.0.1:{self.port}")
            for name, value in (extra or {}).items():
                connection.putheader(name, value)
            if body is not None and length:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            return response, response.read().decode("utf-8")
        finally:
            connection.close()

    def post(self, path, body, cookie=None, **extra):
        fields = {"Origin": f"https://127.0.0.1:{self.port}", "Content-Type": web.FORM_TYPE, **extra}
        if cookie:
            fields["Cookie"] = cookie
        return self.request("POST", path, body, fields)

    def login(self):
        response, _ = self.post(LOGIN, form(password=PASSWORD))
        self.assertEqual(response.status, 303)
        cookie = response.getheader("Set-Cookie").split(";")[0]
        _, page = self.request("GET", "/admin", extra={"Cookie": cookie})
        return cookie, re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def test_admin_flow_over_tls(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            response, page = self.request("GET", "/admin")
            self.assertEqual(response.status, 200)
            self.assertIn('type="password"', page)
            self.assertIn("form-action 'self'", response.getheader("Content-Security-Policy"))
            cookie, csrf = self.login()
            response, page = self.post(BRIGHTNESS, form(csrf=csrf, percent="35"), cookie)
            self.assertEqual((response.status, self.state.brightness), (200, 35))
            response, page = self.post(ACKNOWLEDGE, form(csrf=csrf, check_id=NAS_ID, state="ERROR"), cookie)
            self.assertEqual(response.status, 200)
            self.assertEqual(self.state.view.acknowledged, ((NAS_ID, State.ERROR),))
            response, _ = self.post(LOGOUT, form(csrf=csrf), cookie)
            self.assertEqual(response.status, 200)
            _, page = self.request("GET", "/admin", extra={"Cookie": cookie})
            self.assertIn('type="password"', page)  # relace skončila
        response, _ = self.post(LOGIN, form(password="tajné špatné heslo"))
        self.assertEqual(response.status, 403)
        self.assertEqual([event[0] for event in self.events], [LOGIN_FAILED_EVENT])
        # Heslo, token relace ani token CSRF se nikam nevypsaly ani nezapsaly do auditu.
        logged = "\n".join(self.lines) + err.getvalue() + repr(self.events)
        self.assertNotIn("tajné", logged)
        for secret in (PASSWORD, cookie.split("=", 1)[1], csrf):
            self.assertNotIn(secret, logged)
        self.assertIn("Administrace: přihlášení (relace na 10 min).", self.lines)

    def test_dashboard_and_history_stay_read_only(self):
        response, page = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn('<a href="/admin">Administrace</a>', page)
        self.assertIn("form-action 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual(self.request("GET", "/history")[0].status, 200)
        for path in ("/", "/history", "/api"):
            with self.subTest(path=path):
                response, _ = self.post(path, b"")
                self.assertEqual((response.status, response.getheader("Allow")), (405, "GET, HEAD"))
        self.assertEqual(self.request("PUT", LOGIN, b"")[0].status, 405)
        self.assertEqual(self.request("GET", LOGIN)[0].status, 404)  # akce jen POST
        self.assertEqual(self.request("GET", "/admin?password=x")[0].status, 400)

    def test_form_transport_limits(self):
        cookie, csrf = self.login()
        body = form(csrf=csrf, percent="50")
        cases = [
            (self.request("POST", BRIGHTNESS, None, {"Content-Length": str(web.MAX_FORM_BYTES + 1),
                                                     "Content-Type": web.FORM_TYPE}, length=False), 413),
            (self.post(BRIGHTNESS, body, cookie, **{"Content-Type": "text/plain"}), 415),
            (self.post(BRIGHTNESS, body, cookie, **{"Content-Type": "multipart/form-data"}), 415),
            (self.request("POST", BRIGHTNESS, None, {"Origin": f"https://127.0.0.1:{self.port}",
                                                     "Content-Type": web.FORM_TYPE, "Cookie": cookie},
                          length=False), 411),
            (self.request("POST", BRIGHTNESS, None, {"Transfer-Encoding": "chunked", "Content-Type": web.FORM_TYPE},
                          length=False), 411),
            (self.post(BRIGHTNESS + "?percent=50", body, cookie), 400),
        ]
        for (response, _), status in cases:
            self.assertEqual(response.status, status)
        self.assertEqual(self.state.brightness, 20)

    def test_conflicting_framing_is_refused(self):
        cookie, csrf = self.login()
        body = form(csrf=csrf, percent="50")
        base = {"Origin": f"https://127.0.0.1:{self.port}", "Content-Type": web.FORM_TYPE, "Cookie": cookie}
        for framing in ({"Content-Length": f"{len(body)}, {len(body)}"},
                        {"Content-Length": "+" + str(len(body))},
                        {"Content-Length": str(len(body)), "Transfer-Encoding": "chunked"},
                        {"Transfer-Encoding": "identity"}):
            with self.subTest(framing=framing):
                response, _ = self.request("POST", BRIGHTNESS, body, {**base, **framing}, length=False)
                self.assertEqual(response.status, 411)
        # Dvě hlavičky Content-Length (i se stejnou hodnotou) se odmítnou.
        connection = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=5, context=self.context)
        connection.putrequest("POST", BRIGHTNESS, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", f"127.0.0.1:{self.port}")
        for name, value in base.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(len(body)))
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        self.assertEqual(connection.getresponse().status, 411)
        connection.close()
        self.assertEqual(self.state.brightness, 20)

    def raw_post(self, body_part, length):
        tls = self.context.wrap_socket(socket.create_connection(("127.0.0.1", self.port)), server_hostname="127.0.0.1")
        tls.settimeout(5)
        tls.sendall((f"POST {BRIGHTNESS} HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\n"
                     f"Origin: https://127.0.0.1:{self.port}\r\nContent-Type: {web.FORM_TYPE}\r\n"
                     f"Content-Length: {length}\r\n\r\n").encode("ascii") + body_part)
        return tls

    def wait_idle(self):
        deadline = time.monotonic() + 5
        while self.server.active and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.server.active

    def test_incomplete_body_is_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            tls = self.raw_post(b"csrf=x", 50)
            socket.socket.shutdown(tls, socket.SHUT_WR)  # klient skončí uprostřed těla (bez close_notify)
            try:
                data = tls.recv(4096)
            except (ssl.SSLError, OSError):
                data = b""  # server spojení ukončil chybou TLS
            tls.close()
            self.assertEqual(self.wait_idle(), 0)
        self.assertFalse(data.startswith(b"HTTP/1.0 2"))
        self.assertEqual(self.state.brightness, 20)
        self.assertNotIn("Traceback", err.getvalue())

    def test_slow_post_is_ended_by_the_deadline(self):
        self.server.deadline_seconds = 0.3
        tls = self.raw_post(b"csrf=x", 50)  # zbytek těla nepřijde
        started = time.monotonic()
        try:
            data = tls.recv(4096)
        except (ssl.SSLError, OSError):
            data = b""
        self.assertEqual(data, b"")  # server spojení ukončil bez odpovědi
        self.assertLess(time.monotonic() - started, 4)
        tls.close()
        self.assertEqual((self.wait_idle(), self.state.brightness), (0, 20))

    def test_session_in_url_is_refused(self):
        cookie, csrf = self.login()
        token = cookie.split("=", 1)[1]
        self.assertEqual(self.request("GET", f"/admin?{COOKIE}={token}")[0].status, 400)
        response, _ = self.post(f"{BRIGHTNESS}?{COOKIE}={token}", form(csrf=csrf, percent="50"))
        self.assertEqual(response.status, 400)
        self.assertEqual(self.state.brightness, 20)

    def test_connection_limit_applies_before_tls(self):
        self.server._slots = threading.BoundedSemaphore(1)
        silent = socket.create_connection(("127.0.0.1", self.port))  # obsadí jediné místo, bez handshaku
        self.addCleanup(silent.close)
        deadline = time.monotonic() + 5
        while not self.server.active and time.monotonic() < deadline:
            time.sleep(0.01)
        refused = self.server.refused
        raw = socket.create_connection(("127.0.0.1", self.port))
        try:
            with self.assertRaises((ssl.SSLError, OSError)):
                self.context.wrap_socket(raw, server_hostname="127.0.0.1")
        finally:
            raw.close()
        self.assertEqual(self.server.refused, refused + 1)  # zavřeno hned po přijetí, bez vlákna

    def test_plain_http_and_silent_clients_do_not_block_tls(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            silent = socket.create_connection(("127.0.0.1", self.port))  # žádný handshake
            self.addCleanup(silent.close)
            plain = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            with self.assertRaises((ConnectionError, http.client.HTTPException, OSError)):
                plain.request("GET", "/admin", headers={"Host": f"127.0.0.1:{self.port}"})
                plain.getresponse().read()
            plain.close()
            self.assertEqual(self.request("GET", "/")[0].status, 200)  # vlákno přijímání nečeká na handshake
        self.assertNotIn("Traceback", err.getvalue())


@unittest.skipUnless(OPENSSL, "testovací certifikát vyžaduje openssl")
class TlsDeadlineTest(unittest.TestCase):
    def test_silent_tls_client_is_closed_by_the_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = make_certificate(directory)
            server = web.DashboardServer(StateBoard(), 0, deadline_seconds=0.2, tls=tls_context(cert, key))
        thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        silent = socket.create_connection(server.server_address)
        self.addCleanup(silent.close)
        silent.settimeout(5)
        started = time.monotonic()
        self.assertEqual(silent.recv(1), b"")  # server spojení zavřel, handshake nezačal
        self.assertLess(time.monotonic() - started, 4)
        deadline = time.monotonic() + 5
        while server.active and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(server.active, 0)  # místo se uvolnilo


@unittest.skipUnless(OPENSSL, "testovací certifikát vyžaduje openssl")
class StuckLoopHttpsTest(unittest.TestCase):
    def test_request_not_taken_by_the_loop_is_cancelled_and_never_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = make_certificate(directory)
            context = tls_context(cert, key)
            client_context = ssl.create_default_context(cafile=str(cert))
        board, server = start_web({NAS_ID}, 30, port=0, poll_seconds=0.01, device_factory=lambda: None, tls=context)
        self.addCleanup(stop_web, server)
        state = UserState(output=lambda line: None)  # smyčka netiká
        server.admin = Admin(FakeHash(), state, board, output=lambda line: None, reply_seconds=0.2, grace_seconds=0.2)
        token, session = server.admin.sessions.create()
        port = server.server_address[1]
        connection = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=client_context)
        body = form(csrf=session.csrf, percent="50")
        connection.request("POST", BRIGHTNESS, body=body, headers={
            "Host": f"127.0.0.1:{port}", "Origin": f"https://127.0.0.1:{port}", "Content-Type": web.FORM_TYPE,
            "Cookie": f"{COOKIE}={token}"})
        response = connection.getresponse()
        page = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 409)
        self.assertIn("nic se nezměnilo", page)
        state.tick([], time.monotonic(), UTC)  # smyčka se probudí později: zrušený požadavek se neprovede
        self.assertEqual(state.brightness, 20)


class AdminOffTest(unittest.TestCase):
    def test_plain_http_never_serves_admin(self):
        board, server = start_web({NAS_ID}, 30, port=0, poll_seconds=0.01, device_factory=lambda: None)
        self.addCleanup(stop_web, server)
        server.admin = mock.Mock()  # ani omylem nastavená administrace se bez HTTPS nepoužije
        port = server.server_address[1]
        for method, path, status in (("GET", "/admin", 403), ("POST", LOGIN, 405), ("POST", BRIGHTNESS, 405)):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(method, path, body=b"" if method == "POST" else None,
                               headers={"Host": f"127.0.0.1:{port}"})
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, status, path)
            if status == 403:
                self.assertEqual(body, ADMIN_OFF)
        self.assertEqual(server.admin.mock_calls, [])
        self.assertNotIn("/admin", server.page())


@unittest.skipUnless(OPENSSL, "testovací certifikát vyžaduje openssl")
class MainAdminTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cert, self.key = make_certificate(self.directory.name)
        self.secret = Path(self.directory.name) / "admin.scrypt"
        write_password_file(self.secret, PASSWORD)

    def run_main(self, arguments):
        started = []

        def fake_start(monitored, validity, port, access, tls=None):
            board = StateBoard(monitored, validity)
            server = SimpleNamespace(server_address=(access.bind, port), admin=None, history=None)
            started.append((tls, server))
            return board, server

        output = io.StringIO()
        with mock.patch("nexusstat.run.run_loop") as loop, mock.patch("nexusstat.run.start_web", fake_start), \
                mock.patch("nexusstat.run.stop_web"), contextlib.redirect_stdout(output):
            main(arguments)
        return started, loop, output.getvalue()

    def test_admin_needs_tls_and_valid_files(self):
        tls = ["--web-tls-cert", str(self.cert), "--web-tls-key", str(self.key)]
        encrypted = Path(self.directory.name) / "enc.pem"
        subprocess.run([OPENSSL, "pkey", "-in", str(self.key), "-aes256", "-passout", "pass:x", "-out",
                        str(encrypted)], check=True, capture_output=True, timeout=30)
        os.chmod(encrypted, 0o600)
        open_key = Path(self.directory.name) / "open.pem"
        shutil.copy(self.key, open_key)
        os.chmod(open_key, 0o644)
        other = Path(self.directory.name) / "jiný"
        other.mkdir()
        other_cert, other_key = make_certificate(other)
        garbage = Path(self.directory.name) / "garbage.pem"
        garbage.write_text("není certifikát\n", encoding="utf-8")
        broken = Path(self.directory.name) / "broken.scrypt"
        broken.write_text("x\n", encoding="ascii")
        os.chmod(broken, 0o600)
        for arguments in (["--web", "--web-admin-password", str(self.secret)],  # bez HTTPS
                          ["--web", "--web-tls-cert", str(self.cert)],
                          tls + ["--web-admin-password", str(self.secret)],  # bez --web
                          ["--web", "--web-tls-cert", str(self.cert), "--web-tls-key", str(open_key)],
                          ["--web", "--web-tls-cert", str(self.cert), "--web-tls-key", str(encrypted)],
                          ["--web", *tls, "--web-admin-password", str(broken)],
                          ["--web", "--web-tls-cert", str(other_cert), "--web-tls-key", str(self.key)],  # nesedí
                          ["--web", "--web-tls-cert", str(garbage), "--web-tls-key", str(self.key)],
                          ["--web", "--web-tls-cert", str(self.cert) + ".chybí", "--web-tls-key", str(self.key)],
                          ["--web", "--web-tls-cert", str(self.cert), "--web-tls-key", str(other_key) + ".x"]):
            with self.subTest(arguments=arguments[1:3]), mock.patch("nexusstat.run.run_loop") as loop, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(arguments)
            loop.assert_not_called()
        started, loop, output = self.run_main(["--web", *tls, "--web-admin-password", str(self.secret)])
        [(context, server)] = started
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertIsInstance(server.admin, Admin)
        self.assertIs(server.admin.user_state, loop.call_args.kwargs["user_state"])
        self.assertIn("Web: https://127.0.0.1:8765/", output)
        self.assertIn("administrace na /admin jen s heslem", output)
        self.assertNotIn(PASSWORD, output)
        started, _, output = self.run_main(["--web", *tls])
        self.assertIsNone(started[0][1].admin)  # HTTPS bez hesla: jen čtení
        self.assertIn("administrace vypnutá", output)


class LoopTest(unittest.TestCase):
    def test_admin_request_does_not_change_the_collector_schedule(self):
        class Collector:
            labels = {NAS_ID: "NAS"}

            def __init__(self):
                self.calls = []

            def collect(self, now, observed_utc):
                self.calls.append(now)
                return [CheckResult(NAS_ID, State.WARNING, "NAS", observed_utc, now + 15, simulated=False)]

        class Clock:
            def __init__(self, on_sleep=None):
                self.now, self.on_sleep = 100.0, on_sleep

            def __call__(self):
                return self.now

            def utc(self):
                return UTC + timedelta(seconds=self.now)

            def sleep(self, seconds):
                self.now += seconds
                if self.on_sleep is not None:
                    self.on_sleep(self.now)
                if self.now >= 130:
                    raise KeyboardInterrupt

        def run(state=None, on_sleep=None):
            clock, collector = Clock(on_sleep), Collector()
            run_loop(collector, CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=clock, utc_clock=clock.utc,
                     sleep=clock.sleep, output=lambda line: None, tick_seconds=1, user_state=state)
            return collector.calls

        state = UserState(output=lambda line: None)
        handler = Admin(FakeHash(), state, StateBoard(), output=lambda line: None)
        queued, outcomes, threads = threading.Event(), [], []
        original = state.request
        state.request = lambda command: (original(command), queued.set())[0]

        def between_ticks(now):
            if now == 105.0:  # „web“ podá požadavek mezi takty a smyčka pokračuje až po vložení
                threads.append(threading.Thread(
                    target=lambda: outcomes.append(handler.ask(Acknowledge(NAS_ID, State.WARNING)))))
                threads[0].start()
                self.assertTrue(queued.wait(5))

        calls = run(state, between_ticks)
        threads[0].join(10)
        self.assertEqual(calls, run())  # stejné takty kolektoru jako bez administrace
        self.assertEqual(outcomes[0].kind, DONE)
        self.assertEqual(list(state.acks.records), [NAS_ID])


if __name__ == "__main__":
    unittest.main()
