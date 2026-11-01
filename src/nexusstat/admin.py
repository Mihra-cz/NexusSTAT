"""Chráněná administrace lokálního webu (M6-4B): potvrzení závady, jas LED a odhlášení.

Nic dalšího: intervaly měření, alarmové prahy, časování OLED ani jiná
konfigurace se webem nemění, zůstávají v config.toml. Administrace existuje
jen na HTTPS (web.py ji bez TLS a bez souboru hesla nenabídne) a jen po zadání
jednoho administračního hesla nebo PINu; žádné účty ani role.

- Heslo: v souboru je jen solený otisk scrypt (0600, mimo repozitář).
  Nastavuje ho správce v terminálu: `python3 -m nexusstat.admin SOUBOR`.
  Heslo se nikdy nevypisuje ani neukládá do konfigurace, URL nebo historie.
- Chybné pokusy se omezují globálně, ne podle adresy: po FREE_FAILURES chybách
  blokace 60 s, další 300 s a pak 900 s; scrypt počítá vždy jen jeden pokus.
  Každý chybný pokus a každá blokace jsou auditní událost historie (frontou
  zapisovače, bez hesla a bez údajů požadavku); pokusy během blokace se jen
  sečtou do další události, takže záplava požadavků zápisy nenásobí.
- Relace jen v paměti procesu: nejvýš MAX_SESSIONS, každá SESSION_SECONDS bez
  prodlužování, restart je zruší. Cookie `__Host-` se Secure, HttpOnly,
  SameSite=Strict a Path=/, bez Domain.
- Každý POST ověří Origin proti skutečnému HTTPS originu a (kromě přihlášení)
  token CSRF relace. Akce jdou jen přes UserState.request(): vlákno HTTP samo
  nic nemění a odpověď rozliší provedeno, odmítnuto a nejisté.
"""

import argparse
import base64
import binascii
import contextlib
import getpass
import hashlib
import hmac
import math
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from . import storage
from .core import State, effective
from .oled_text import status_texts
from .rgb import ACK_LEVELS
from .user_state import CHECK_ID_PATTERN, MAX_CHECK_ID, Acknowledge, RequestInterrupted, SetBrightness
from .web import CSS, CSS_HASH, badge, esc

COOKIE = "__Host-nexusstat-admin"
SESSION_SECONDS = 600
MAX_SESSIONS = 4
FREE_FAILURES = 5
LOCKOUT_SECONDS = (60, 300, 900)
# scrypt: 16 MiB paměti na pokus (2**14 × 8 × 128 B); vyšší N se při načtení přijme.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SCRYPT_N_RANGE = (2 ** 14, 2 ** 17)
SALT_BYTES, KEY_BYTES = 16, 32
MIN_PIN_DIGITS, MIN_PASSWORD_CHARS, MAX_PASSWORD_CHARS = 8, 12, 128
MAX_HASH_FILE_BYTES = 512
# Smyčka tiká po 1 s: běžná odpověď přijde hned v dalším taktu.
REPLY_SECONDS = 3.0
# Po neúspěšném zrušení smyčka požadavek už vyřizuje; tolik se ještě počká na odpověď.
GRACE_SECONDS = 2.0
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32)
PERCENT = re.compile(r"[0-9]{1,3}")
PROJECT_ROOT = Path(__file__).resolve().parents[2]

LOGIN = "/admin/login"
ACKNOWLEDGE = "/admin/acknowledge"
BRIGHTNESS = "/admin/brightness"
LOGOUT = "/admin/logout"
# Jediné povolené akce a jejich přesná pole; jiná cesta pod /admin/ je 404.
FIELDS = {
    LOGIN: {"password"},
    ACKNOWLEDGE: {"csrf", "check_id", "state"},
    BRIGHTNESS: {"csrf", "percent"},
    LOGOUT: {"csrf"},
}
ADMIN_CSP = (f"default-src 'none'; style-src 'sha256-{CSS_HASH}'; base-uri 'none'; form-action 'self'; "
             "frame-ancestors 'none'")

# Auditní události historie (tabulka event, schéma beze změny).
LOGIN_FAILED_EVENT = "admin.login_failed"
LOGIN_BLOCKED_EVENT = "admin.login_blocked"

DONE, REFUSED, UNCERTAIN = "provedeno", "odmítnuto", "nejisté"
OUTCOME_STATUS = {DONE: 200, REFUSED: 409, UNCERTAIN: 503}


class AdminError(ValueError):
    """Heslo nebo jeho soubor nelze bezpečně použít."""


def check_new_password(password):
    """PIN aspoň 8 číslic nebo heslo aspoň 12 znaků, nejvýš 128, bez řídicích znaků."""
    if len(password) > MAX_PASSWORD_CHARS:
        raise AdminError(f"heslo smí mít nejvýš {MAX_PASSWORD_CHARS} znaků")
    if any(unicodedata.category(char).startswith("C") for char in password):
        raise AdminError("heslo nesmí obsahovat řídicí znaky")
    if re.fullmatch(r"[0-9]+", password):
        if len(password) < MIN_PIN_DIGITS:
            raise AdminError(f"PIN musí mít aspoň {MIN_PIN_DIGITS} číslic")
    elif len(password) < MIN_PASSWORD_CHARS:
        raise AdminError(f"heslo musí mít aspoň {MIN_PASSWORD_CHARS} znaků (nebo PIN aspoň {MIN_PIN_DIGITS} číslic)")
    return password


def _scrypt(password, salt, n, r, p):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=256 * r * n, dklen=KEY_BYTES)


def _b64(data):
    return base64.b64encode(data).decode("ascii")


@dataclass(frozen=True, repr=False)  # repr by otisk vypsal do logu
class PasswordHash:
    n: int
    r: int
    p: int
    salt: bytes
    key: bytes

    def verify(self, password):
        return hmac.compare_digest(_scrypt(password, self.salt, self.n, self.r, self.p), self.key)


def hash_password(password, salt=None):
    """Řádek souboru otisku: `scrypt$N$r$p$sůl$otisk` (base64)."""
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    key = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}\n"


def parse_hash(text):
    parts = text.strip().split("$")
    if len(parts) != 6 or parts[0] != "scrypt" or not all(re.fullmatch(r"[0-9]{1,7}", part) for part in parts[1:4]):
        raise AdminError("neplatný formát otisku")
    n, r, p = (int(part) for part in parts[1:4])
    if n & (n - 1) or not SCRYPT_N_RANGE[0] <= n <= SCRYPT_N_RANGE[1] or r != SCRYPT_R or p != SCRYPT_P:
        raise AdminError("nepodporované parametry scrypt")
    try:
        salt, key = (base64.b64decode(part, validate=True) for part in parts[4:])
    except (binascii.Error, ValueError):
        raise AdminError("neplatný formát otisku") from None
    if len(salt) != SALT_BYTES or len(key) != KEY_BYTES:
        raise AdminError("neplatná délka soli nebo otisku")
    return PasswordHash(n, r, p, salt, key)


def load_password_file(path):
    """Přísně načte otisk: obyčejný soubor vlastníka, bez přístupu skupiny a ostatních, bez symlinku."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as error:
        raise AdminError(f"soubor otisku {path} nelze otevřít ({error.strerror or error})") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise AdminError(f"{path} není obyčejný soubor")
        if info.st_uid != os.geteuid():
            raise AdminError(f"{path} patří jinému uživateli")
        if info.st_mode & 0o077:
            raise AdminError(f"{path} smí číst jen vlastník (chmod 600)")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(MAX_HASH_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(content) > MAX_HASH_FILE_BYTES:
        raise AdminError(f"{path} je příliš velký")
    try:
        return parse_hash(content.decode("ascii"))
    except UnicodeDecodeError:
        raise AdminError("neplatný formát otisku") from None


def write_password_file(path, password, replace=False):
    """Atomicky zapíše otisk (0600). Adresář musí existovat; soubor v repozitáři se odmítne."""
    path = Path(path)
    check_new_password(password)
    target = path.parent.resolve() / path.name
    if target == PROJECT_ROOT or PROJECT_ROOT in target.parents:
        raise AdminError("soubor otisku nesmí být v repozitáři NexusSTAT")
    if not replace and os.path.lexists(path):
        raise AdminError(f"{path} už existuje; přepsat jen s --replace")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(hash_password(password).encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    storage.fsync_directory(path.parent)


class LoginGuard:
    """Ověření hesla s globálním omezením chybných pokusů (pro všechny klienty společně).

    Adresa klienta se nepoužívá: v LAN ji lze měnit. Během blokace se scrypt
    vůbec nepočítá, takže ani záplava pokusů nezabere procesor.
    `on_event(kind, severity, message, observed_utc, check_id, data)` dostane
    auditní události (run.py: HistoryLink.record_event); jen chybné pokusy
    a blokace, nikdy heslo ani nic z požadavku. Na jednu blokaci připadá
    nejvýš FREE_FAILURES + 1 událostí.
    """

    def __init__(self, password_hash, clock=time.monotonic, output=print, on_event=None,
                 utc_clock=lambda: datetime.now(timezone.utc)):
        self._hash = password_hash
        self._clock = clock
        self._output = output
        self._on_event = on_event
        self._utc_clock = utc_clock
        self._lock = threading.Lock()
        self._failures = 0
        self._level = 0  # kolikátá blokace přijde příště
        self._blocked_until = None
        self._refused = 0  # pokusy odmítnuté během blokace od poslední události
        self.verifications = 0  # skutečné výpočty scrypt

    def attempt(self, password):
        """(True, "") při správném hesle, jinak (False, důvod)."""
        with self._lock:
            now = self._clock()
            if self._blocked_until is not None and now < self._blocked_until:
                self._refused += 1  # bez události: jen se sečte do příští
                return False, f"příliš mnoho chybných pokusů; zkuste to za {math.ceil(self._blocked_until - now)} s"
            self.verifications += 1
            if len(password) <= MAX_PASSWORD_CHARS and self._hash.verify(password):
                self._failures = self._level = 0
                self._blocked_until = None
                return True, ""
            self._failures += 1
            failures, refused, self._refused = self._failures, self._refused, 0
            seconds = None
            if self._failures >= FREE_FAILURES:
                seconds = LOCKOUT_SECONDS[min(self._level, len(LOCKOUT_SECONDS) - 1)]
                self._level += 1
                self._failures = 0
                self._blocked_until = now + seconds
        # Události a výpis až mimo zámek: pomalý zapisovač nesmí zdržet další pokusy.
        self._report(LOGIN_FAILED_EVENT, "WARNING", f"neúspěšné přihlášení do administrace ({failures}. chybný "
                     "pokus po sobě)", {"failures": failures, "refused_while_blocked": refused})
        if seconds is None:
            return False, "nesprávné heslo"
        self._report(LOGIN_BLOCKED_EVENT, "ERROR", f"přihlašování do administrace zablokováno na {seconds} s po "
                     f"{FREE_FAILURES} chybných pokusech", {"failures": FREE_FAILURES, "seconds": seconds})
        self._output(f"Administrace: {FREE_FAILURES} chybných pokusů o přihlášení, přihlašování blokováno na {seconds} s.")
        return False, f"příliš mnoho chybných pokusů; zkuste to za {seconds} s"

    def _report(self, kind, severity, message, data):
        if self._on_event is None:
            return  # historie vypnutá nebo nedostupná: přihlašování běží dál
        try:
            self._on_event(kind, severity, message, self._utc_clock(), None, data)
        except Exception:  # noqa: BLE001 - audit nesmí zablokovat ani shodit administraci
            pass


@dataclass
class Session:
    csrf: str
    expires: float


class Sessions:
    """Relace jen v paměti: klíčem je otisk tokenu, nejvýš `limit`, pevná doba života."""

    def __init__(self, clock=time.monotonic, lifetime=SESSION_SECONDS, limit=MAX_SESSIONS):
        self._clock = clock
        self._lifetime = lifetime
        self._limit = limit
        self._lock = threading.Lock()
        self._sessions = {}

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode("ascii")).digest()

    def create(self):
        token = secrets.token_urlsafe(32)
        session = Session(secrets.token_urlsafe(32), self._clock() + self._lifetime)
        with self._lock:
            self._purge()
            while len(self._sessions) >= self._limit:
                # Nová relace vytlačí tu, které zbývá nejméně času.
                del self._sessions[min(self._sessions, key=lambda key: self._sessions[key].expires)]
            self._sessions[self._key(token)] = session
        return token, session

    def find(self, token):
        with self._lock:
            self._purge()
            return self._sessions.get(self._key(token))

    def end(self, token):
        with self._lock:
            self._sessions.pop(self._key(token), None)

    def __len__(self):
        with self._lock:
            self._purge()
            return len(self._sessions)

    def _purge(self):
        now = self._clock()
        for key in [key for key, session in self._sessions.items() if now >= session.expires]:
            del self._sessions[key]


@dataclass(frozen=True)
class Response:
    status: int
    html: str
    headers: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    kind: str  # DONE, REFUSED nebo UNCERTAIN
    text: str


def parse_form(body, fields):
    """Pole formuláře urlencoded: přesně `fields`, každé jednou, UTF-8; jinak ValueError."""
    pairs = parse_qsl(body.decode("ascii"), keep_blank_values=True, strict_parsing=bool(body),
                      max_num_fields=len(fields), encoding="utf-8", errors="strict")
    form = dict(pairs)
    if len(pairs) != len(fields) or set(form) != fields:
        raise ValueError("nečekaná pole formuláře")
    return form


def _page(title, content):
    return ('<!DOCTYPE html><html lang="cs"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
            f'<header><h1>{esc(title)}</h1><nav><a href="/">Aktuální stav</a><a href="/history">Historie</a>'
            f'<a href="/admin" aria-current="page">Administrace</a></nav></header>{content}</body></html>')


def _hidden(name, value):
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


class Admin:
    """Tři akce administrace nad StateBoard (jen čtení) a UserState (jen request())."""

    def __init__(self, password_hash, user_state, board, clock=time.monotonic, output=print,
                 reply_seconds=REPLY_SECONDS, grace_seconds=GRACE_SECONDS, on_event=None):
        self.guard = LoginGuard(password_hash, clock, output, on_event)
        self.sessions = Sessions(clock)
        self.user_state = user_state
        self.board = board
        self._clock = clock
        self._output = output
        self._reply_seconds = reply_seconds
        self._grace_seconds = grace_seconds

    def get(self, headers):
        """GET /admin: přihlášení, nebo stránka administrace."""
        _, session = self._session(headers)
        if session is None:
            return self._login_page(200)
        return self._respond(200, self._admin_page(session))

    def post(self, path, host, headers, body):
        """POST jedné z akcí FIELDS; `host` je už ověřená hlavička Host (malými písmeny)."""
        if path not in FIELDS:
            return self._respond(404, _page("Nenalezeno", "<p>Taková akce administrace neexistuje.</p>"))
        if (headers.get_all("Origin") or []) != [f"https://{host}"]:
            return self._message(403, "Odmítnuto", "Nepovolený původ požadavku (Origin).")
        try:
            form = parse_form(body, FIELDS[path])
        except ValueError:
            return self._message(400, "Odmítnuto", "Neplatný formulář.")
        if path == LOGIN:
            return self._login(form["password"])
        token, session = self._session(headers)
        if session is None:
            return self._login_page(403, "Přihlášení chybí nebo vypršelo; přihlaste se znovu. Nic se nezměnilo.")
        if not hmac.compare_digest(form["csrf"].encode("utf-8"), session.csrf.encode("ascii")):
            return self._message(403, "Odmítnuto", "Neplatný token formuláře (CSRF). Nic se nezměnilo.")
        if path == LOGOUT:
            self.sessions.end(token)
            self._output("Administrace: odhlášení.")
            return self._respond(200, _page("Odhlášeno", '<p>Administrace je odhlášená.</p>'),
                                 {"Set-Cookie": f"{COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"})
        if path == ACKNOWLEDGE:
            command = self._acknowledge_command(form)
        else:
            if not PERCENT.fullmatch(form["percent"]):
                return self._message(400, "Odmítnuto", "Jas musí být celé číslo 5–100 %.")
            command = SetBrightness(int(form["percent"]))
        if isinstance(command, Outcome):
            return self._outcome(command)
        if command is None:
            return self._message(400, "Odmítnuto", "Neplatná kontrola nebo závažnost.")
        return self._outcome(self.ask(command))

    def ask(self, command):
        """Předá příkaz vlastníkovi stavu a rozliší provedeno, odmítnuto a nejisté.

        Po vypršení limitu se požadavek zruší; zrušení, které se povede, je
        důkaz, že se nic nezmění. Nepovede-li se (smyčka ho už vyřizuje),
        čeká se ještě chvíli a jinak je výsledek nejistý, nikdy „neprovedeno“.
        """
        future = self.user_state.request(command)
        try:
            reply = future.result(timeout=self._reply_seconds)
        except TimeoutError:
            if future.cancel():
                return Outcome(REFUSED, "Měřicí smyčka požadavek včas nepřevzala; byl zrušen a nic se nezměnilo.")
            try:
                reply = future.result(timeout=self._grace_seconds)
            except TimeoutError:
                return Outcome(UNCERTAIN, "Požadavek se právě vyřizuje; výsledek ověřte na stránce administrace.")
            except RequestInterrupted:
                return Outcome(UNCERTAIN, "NexusSTAT skončil uprostřed vyřizování; výsledek ověřte po startu.")
        except RequestInterrupted:
            return Outcome(UNCERTAIN, "NexusSTAT skončil uprostřed vyřizování; výsledek ověřte po startu.")
        except CancelledError:
            return Outcome(REFUSED, "Požadavek byl zrušen; nic se nezměnilo.")
        if not reply.accepted:
            return Outcome(REFUSED, f"Odmítnuto: {reply.reason}.")
        saved = "uloženo do souboru stavu" if reply.persisted else "platí, ale zatím není uloženo do souboru stavu"
        return Outcome(DONE, f"Provedeno: {reply.reason} ({saved}).")

    def _acknowledge_command(self, form):
        check_id, state = form["check_id"], form["state"]
        if len(check_id) > MAX_CHECK_ID or not CHECK_ID_PATTERN.fullmatch(check_id):
            return None
        if state == State.CRITICAL.value:
            return Outcome(REFUSED, "CRITICAL nelze umlčet; signalizace trvá.")
        seen = {level.value: level for level in ACK_LEVELS}.get(state)
        return None if seen is None else Acknowledge(check_id, seen)

    def _login(self, password):
        accepted, reason = self.guard.attempt(password)
        if not accepted:
            return self._login_page(403, f"Přihlášení odmítnuto: {reason}.")
        token, _ = self.sessions.create()
        self._output(f"Administrace: přihlášení (relace na {SESSION_SECONDS // 60} min).")
        return self._respond(303, _page("Přihlášeno", '<p><a href="/admin">Pokračovat do administrace</a></p>'),
                             {"Location": "/admin",
                              "Set-Cookie": (f"{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; "
                                             f"Max-Age={SESSION_SECONDS}")})

    def _session(self, headers):
        values = []
        for header in headers.get_all("Cookie") or []:
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE:
                    values.append(value)
        if len(values) != 1 or not TOKEN.fullmatch(values[0]):
            return None, None
        return values[0], self.sessions.find(values[0])

    def _respond(self, status, html, headers=None):
        return Response(status, html, {"Content-Security-Policy": ADMIN_CSP, **(headers or {})})

    def _message(self, status, title, text):
        return self._respond(status, _page(title, f'<p>{esc(text)}</p><p><a href="/admin">Zpět do administrace</a></p>'))

    def _outcome(self, outcome):
        return self._message(OUTCOME_STATUS[outcome.kind], outcome.kind.capitalize(), outcome.text)

    def _login_page(self, status, note=""):
        message = f"<p>{esc(note)}</p>" if note else ""
        form = (f'<form method="post" action="{LOGIN}"><label>Heslo nebo PIN '
                f'<input type="password" name="password" autocomplete="current-password" '
                f'maxlength="{MAX_PASSWORD_CHARS}" required autofocus></label> <button>Přihlásit</button></form>')
        return self._respond(status, _page("NexusSTAT – administrace", f"{message}<section>{form}</section>"
                                           "<p class=\"t\">Jen potvrzení závad a jas LED. Relace platí "
                                           f"{SESSION_SECONDS // 60} min.</p>"))

    def _admin_page(self, session):
        now = self._clock()
        snapshot = self.board.snapshot()
        results = [] if snapshot is None else [effective(result, now) for result in snapshot.results]
        view = self.user_state.view
        acknowledged = dict(view.acknowledged)
        csrf = _hidden("csrf", session.csrf)
        rows = []
        for result in results:
            if result.state not in (*ACK_LEVELS, State.CRITICAL) or result.simulated:
                continue
            if result.state is State.CRITICAL:
                action = "CRITICAL nelze umlčet; signalizace trvá."
            elif result.id in acknowledged and ACK_LEVELS[acknowledged[result.id]] >= ACK_LEVELS[result.state]:
                action = f"Potvrzeno ({acknowledged[result.id].value}); závada dál trvá a zůstává vidět."
            else:
                action = (f'<form method="post" action="{ACKNOWLEDGE}">{csrf}{_hidden("check_id", result.id)}'
                          f'{_hidden("state", result.state.value)}<button>Beru na vědomí / pracuji na nápravě'
                          "</button></form>")
            rows.append(f"<tr><td>{badge(result.state)}</td><td>{esc(status_texts(result, now)[0])}"
                        f'<br><span class="t">{esc(result.id)}</span></td><td>{action}</td></tr>')
        faults = f"<table>{''.join(rows)}</table>" if rows else "<p>Žádná aktivní WARNING, ERROR ani CRITICAL.</p>"
        brightness = (f'<form method="post" action="{BRIGHTNESS}">{csrf}<label>Jas LED (5–100 %) '
                      f'<input type="number" name="percent" min="5" max="100" step="1" value="{view.brightness}" '
                      'required></label> <button>Nastavit</button></form>')
        logout = f'<form method="post" action="{LOGOUT}">{csrf}<button>Odhlásit</button></form>'
        left = max(0, math.ceil((session.expires - now) / 60))
        return _page("NexusSTAT – administrace", (
            f"<section><h2>Aktivní závady</h2>{faults}<p class=\"t\">Potvrzení nemění stav závady: OLED, dashboard "
            "i historie ji ukazují dál. Vypne jen automatické blikání LED do potvrzené závažnosti; zhoršení nebo "
            "nová epizoda chtějí nové potvrzení.</p></section>"
            f"<section><h2>Jas LED</h2>{brightness}<p class=\"t\">Fyzické LED NexusSTAT zatím neovládá; jas se "
            "jen uloží pro budoucí ovladač.</p></section>"
            f"<section>{logout}<p class=\"t\">Relace vyprší nejpozději za {left} min.</p></section>"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Nastaví administrační heslo nebo PIN lokálního webu NexusSTAT "
                                                 "(uloží jen solený otisk scrypt s oprávněními 0600)")
    parser.add_argument("path", type=Path, help="soubor otisku mimo repozitář; adresář musí existovat")
    parser.add_argument("--replace", action="store_true", help="přepsat existující otisk")
    args = parser.parse_args(argv)
    first = getpass.getpass("Nové heslo nebo PIN: ")
    if getpass.getpass("Znovu: ") != first:
        parser.error("zadání se neshodují; nic se nezapsalo")
    try:
        write_password_file(args.path, first, replace=args.replace)
    except (AdminError, OSError) as error:
        parser.error(f"{error}; nic se nezapsalo")
    print(f"Otisk uložen do {args.path} (0600). Platí od dalšího startu nexusstat.run s --web-admin-password.",
          file=sys.stdout)


if __name__ == "__main__":
    main()
