"""Trvalý uživatelský stav: potvrzení závad a jas RGB (M6-4A).

Jediným vlastníkem je hlavní smyčka měření (run.run_loop): jen ona mění
potvrzení a jas a jen ona zapisuje soubor. Administrace webu (M6-4B, admin.py)
z vláken HTTP jen podá požadavek (`request`) a počká na odpověď; RgbLeds,
alarmovou paměť ani soubor nikdy nemění. Fronta požadavků je jediné místo
sdílené mezi vlákny, takže víc synchronizace není potřeba.

Soubor je malý JSON v appdata: verze formátu, jas a potvrzení (kontrola,
potvrzená závažnost, čas potvrzení). Počítadla odeznění se neukládají: po
restartu začínají od nuly, takže restart zánik potvrzení jen oddálí, nikdy
neurychlí. Poškozený, cizí, cizím zapisovatelný nebo nečitelný soubor znamená
žádná potvrzení a výchozí jas (raději znovu blikat než falešně mlčet); soubor
se přitom nemaže a přepíše ho až první skutečná změna. Zapisuje se jen při
skutečné změně uloženého obsahu, nikdy v každém taktu.
"""

import contextlib
import json
import math
import os
import queue
import re
import stat
import tempfile
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass
from pathlib import Path

from . import storage
from .core import State
from .rgb import ACK_LEVELS, BRIGHTNESS_DEFAULT, Acknowledgement, Acknowledgements, check_brightness

FORMAT_VERSION = 1
MAX_FILE_BYTES = 64 * 1024
# NexusSTAT má desítky kontrol; víc záznamů znamená poškozený nebo cizí soubor.
MAX_ACKNOWLEDGEMENTS = 256
CHECK_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+")
MAX_CHECK_ID = 64
# Po selhání zápisu se to zkusí znovu nejdřív za minutu, ne v každém taktu.
SAVE_RETRY_SECONDS = 60.0
# Požadavky čekající na hlavní smyčku; víc znamená zahlcení a odmítá se hned.
MAX_PENDING = 32

ACKNOWLEDGED_EVENT = "alarm.acknowledged"
CLEARED_EVENT = "alarm.ack_cleared"
BRIGHTNESS_EVENT = "settings.brightness"


@dataclass(frozen=True)
class Acknowledge:
    """Požadavek: potvrdit závadu kontroly na závažnosti, kterou správce viděl."""

    check_id: str
    seen_state: State


@dataclass(frozen=True)
class SetBrightness:
    """Požadavek: nastavit jas LED v procentech (5–100)."""

    percent: int


@dataclass(frozen=True)
class Reply:
    """Odpověď hlavní smyčky; `persisted` = výsledný stav je zapsaný v souboru."""

    accepted: bool
    reason: str
    persisted: bool = False


@dataclass(frozen=True)
class View:
    """Neměnný přehled pro čtení z jiných vláken (administrace webu)."""

    brightness: int
    acknowledged: tuple = ()  # ((check_id, State), …) seřazené podle id


@dataclass(frozen=True)
class Stored:
    """Obsah souboru po načtení; `problem` = proč se soubor nepoužil."""

    brightness: int = BRIGHTNESS_DEFAULT
    acknowledgements: tuple = ()  # ((check_id, State, acknowledged_utc | None), …)
    problem: str | None = None
    found: bool = False


class StateFileError(ValueError):
    """Soubor uživatelského stavu nelze bezpečně použít."""


class RequestInterrupted(Exception):
    """NexusSTAT skončil (Ctrl+C, SIGTERM) uprostřed vyřizování; výsledek není jistý, ověřte ho po startu."""


def _claim(future):
    """Převezme požadavek k provedení; False = volající ho zrušil nebo dokončil, neprovede se."""
    if future.done():
        return False
    try:
        return future.set_running_or_notify_cancel()  # po převzetí už ho zrušit nejde
    except RuntimeError:  # volající ho dokončil právě teď
        return False


def _resolve(future, reply=None, error=None):
    """Dokončí převzatý požadavek; dokončení volajícím nesmí zastavit měřicí smyčku."""
    try:
        if error is None:
            future.set_result(reply)
        else:
            future.set_exception(error)
    except InvalidStateError:
        pass


def _check_id(value):
    return (isinstance(value, str) and len(value) <= MAX_CHECK_ID
            and CHECK_ID_PATTERN.fullmatch(value) is not None)


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def parse_state(text):
    """Přísně ověří obsah souboru; vrací (jas, potvrzení), jinak StateFileError."""

    def reject_constant(name):
        raise StateFileError(f"neplatná hodnota {name}")

    try:
        data = json.loads(text, parse_constant=reject_constant)
    except json.JSONDecodeError as error:
        raise StateFileError(f"není platný JSON ({error.msg})") from None
    if not isinstance(data, dict) or set(data) != {"format", "brightness", "acknowledgements"}:
        raise StateFileError("neočekávaná struktura")
    if data["format"] != FORMAT_VERSION or isinstance(data["format"], bool):
        raise StateFileError(f"nepodporovaná verze formátu {data['format']!r}")
    try:
        brightness = check_brightness(data["brightness"])
    except ValueError as error:
        raise StateFileError(str(error)) from None
    records = data["acknowledgements"]
    if not isinstance(records, dict) or len(records) > MAX_ACKNOWLEDGEMENTS:
        raise StateFileError("neplatný seznam potvrzení")
    acknowledgements = []
    for check_id, record in sorted(records.items()):
        if not _check_id(check_id) or not isinstance(record, dict) or set(record) != {"state", "acknowledged_utc"}:
            raise StateFileError("neplatné potvrzení")
        state = {item.value: item for item in ACK_LEVELS}.get(record["state"])
        moment = record["acknowledged_utc"]
        if state is None or not (moment is None or _positive_int(moment)):
            raise StateFileError(f"neplatné potvrzení kontroly {check_id}")
        acknowledgements.append((check_id, state, moment))
    return brightness, tuple(acknowledgements)


def load_state(path):
    """Načte soubor; jakýkoli problém dá výchozí stav s důvodem, nikdy výjimku.

    Symlink, jiný než obyčejný soubor, soubor jiného uživatele nebo soubor
    zapisovatelný pro skupinu či ostatní se odmítne: cizí zápis by jinak mohl
    podstrčit potvrzení a umlčet závadu.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return Stored()
    except OSError as error:
        return Stored(problem=f"nelze otevřít ({error.strerror or error})", found=True)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return Stored(problem="není obyčejný soubor", found=True)
        if info.st_uid != os.geteuid():
            return Stored(problem="patří jinému uživateli", found=True)
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return Stored(problem="je zapisovatelný i pro skupinu nebo ostatní", found=True)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            return Stored(problem=f"je větší než {MAX_FILE_BYTES} B", found=True)
        brightness, acknowledgements = parse_state(content.decode("utf-8"))
    except UnicodeDecodeError:
        return Stored(problem="není text UTF-8", found=True)
    except StateFileError as error:
        return Stored(problem=str(error), found=True)
    except OSError as error:
        return Stored(problem=f"nelze přečíst ({error.strerror or error})", found=True)
    finally:
        os.close(descriptor)
    return Stored(brightness, acknowledgements, found=True)


def encode_state(brightness, acknowledgements):
    """Obsah souboru: seřazené klíče, takže stejný stav dá vždy stejný text."""
    data = {
        "format": FORMAT_VERSION,
        "brightness": brightness,
        "acknowledgements": {check_id: {"state": state.value, "acknowledged_utc": moment}
                             for check_id, state, moment in acknowledgements},
    }
    return (json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def save_state(path, brightness, acknowledgements):
    """Atomicky zapíše soubor s oprávněními 0600.

    Dočasný soubor ve stejném adresáři → fsync → přejmenování → fsync
    adresáře: po výpadku napájení zůstane buď starý, nebo celý nový obsah.
    Adresář se nevytváří (appdata připravuje správce).
    """
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encode_state(brightness, acknowledgements))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    storage.fsync_directory(path.parent)


def _utc_seconds(observed_utc):
    # Nesmyslný čas se neukládá (soubor by se po restartu odmítl celý); je jen pro přehled.
    moment = math.floor(observed_utc.timestamp())
    return moment if moment > 0 else None


class UserState:
    """Potvrzení a jas s jediným vlastníkem: hlavní smyčkou měření.

    `request()` smí volat kterékoli vlákno, vše ostatní jen hlavní smyčka.
    `on_event(kind, severity, message, observed_utc, check_id, data)` dostane
    potvrzení, jeho zánik a změnu jasu (run.py je předá historii); bez něj se
    jen vypíšou. Bez `path` žije stav jen v paměti procesu.
    """

    def __init__(self, alarms_config=None, path=None, *, output=print, on_event=None):
        self.path = None if path is None else Path(path)
        self.acks = Acknowledgements(alarms_config)
        self.brightness = BRIGHTNESS_DEFAULT
        self.saves = 0  # skutečné zápisy souboru
        self.save_failures = 0
        self.view = View(self.brightness)
        self._output = output
        self._on_event = on_event
        self._requests = queue.Queue(MAX_PENDING)
        self._stored = self._persisted()  # obsah, který odpovídá souboru
        self._retry_at = None
        self._failing = False
        self._closed = False

    def load(self):
        """Načte soubor při startu; vrací větu pro výpis. Nic nezapisuje."""
        if self.path is None:
            return "jen v paměti procesu ([user_state] bez path); restart potvrzení i jas zapomene."
        stored = load_state(self.path)
        self.brightness = stored.brightness
        self.acks.records = {check_id: Acknowledgement(state, acknowledged_utc=moment)
                             for check_id, state, moment in stored.acknowledgements}
        # Výchozí stav po poškozeném souboru se nezapisuje: přepíše ho až skutečná změna.
        self._stored = self._persisted()
        self._publish()
        if stored.problem is not None:
            return (f"{self.path} nepoužit ({stored.problem}); bez potvrzení, jas {self.brightness} %. "
                    "Soubor zůstává beze změny do první skutečné změny.")
        if not stored.found:
            return f"{self.path} zatím neexistuje; bez potvrzení, jas {self.brightness} %."
        return f"{self.path}: jas {self.brightness} %, potvrzení {len(self.acks.records)}."

    def request(self, command):
        """Podá požadavek z libovolného vlákna; vrací Future s Reply.

        Vyřídí ho hlavní smyčka v nejbližším taktu (do 1 s); volající čeká
        s vlastním limitem. Plná fronta se odmítne hned, nečeká se. Po
        vypršení limitu volající zavolá `future.cancel()`: True = požadavek
        se nikdy neprovede; False = smyčka ho už vyřizuje a odpověď přijde
        (Reply, nebo RequestInterrupted při ukončení uprostřed vyřizování).
        """
        if not isinstance(command, (Acknowledge, SetBrightness)):
            raise TypeError(f"neznámý požadavek {type(command).__name__}")
        future = Future()
        if self._closed:
            future.set_result(Reply(False, "NexusSTAT se ukončuje"))
            return future
        try:
            self._requests.put_nowait((command, future))
        except queue.Full:
            future.set_result(Reply(False, "příliš mnoho čekajících požadavků"))
            return future
        if self._closed:
            # Souběh s ukončením: smyčka už frontu nevyřídí, odmítne ji tedy volající.
            self.close()
        return future

    def tick(self, results, now, observed_utc):
        """Jeden takt hlavní smyčky: požadavky, započtení měření a zápis při změně."""
        running = []  # [future, odpověď] převzatých požadavků; odpověď None = vyřizování přerušeno
        try:
            for command, future in self._pending():
                if _claim(future):  # zrušený nebo dokončený požadavek se neprovede
                    running.append([future, None])
                    running[-1][1] = self._apply(command, results, now, observed_utc)
            before = dict(self.acks.records)
            self.acks.update(results, now)
            for check_id in sorted(set(before) - set(self.acks.records)):
                self._event(CLEARED_EVENT, f"potvrzení {check_id} zaniklo: závada doloženě odezněla",
                            check_id, {"state": before[check_id].state.value}, observed_utc)
            self._save_if_changed(now)
            self._publish()
        finally:
            # I po přerušení (Ctrl+C, SIGTERM) dostane každý převzatý požadavek odpověď;
            # „uloženo“ jen podle skutečně potvrzeného zápisu.
            persisted = self.path is not None and self._persisted() == self._stored
            for future, reply in running:
                if reply is None:
                    _resolve(future, error=RequestInterrupted("NexusSTAT skončil uprostřed vyřizování"))
                else:
                    _resolve(future, Reply(reply.accepted, reply.reason, persisted and reply.accepted))

    def close(self):
        """Odmítne požadavky, které už hlavní smyčka nevyřídí (při ukončení)."""
        self._closed = True
        for _, future in self._pending():
            if _claim(future):
                _resolve(future, Reply(False, "NexusSTAT se ukončuje"))

    def _pending(self):
        while True:
            try:
                yield self._requests.get_nowait()
            except queue.Empty:
                return

    def _apply(self, command, results, now, observed_utc):
        if isinstance(command, SetBrightness):
            try:
                percent = check_brightness(command.percent)
            except ValueError as error:
                return Reply(False, str(error))
            if percent == self.brightness:
                return Reply(True, "jas beze změny")
            previous, self.brightness = self.brightness, percent
            self._event(BRIGHTNESS_EVENT, f"jas LED {previous} % → {percent} %", None,
                        {"previous": previous, "brightness": percent}, observed_utc)
            return Reply(True, "jas nastaven")
        if not isinstance(command.check_id, str) or not isinstance(command.seen_state, State):
            return Reply(False, "neplatný požadavek")
        before = self.acks.records.get(command.check_id)
        accepted, reason = self.acks.acknowledge_seen(results, command.check_id, command.seen_state, now,
                                                      _utc_seconds(observed_utc))
        record = self.acks.records.get(command.check_id)
        if record is not before:  # nové potvrzení, nebo vyšší závažnost
            state = record.state
            self._event(ACKNOWLEDGED_EVENT, f"potvrzena závada {command.check_id} {state.value} (beru na vědomí)",
                        command.check_id, {"state": state.value}, observed_utc)
        return Reply(accepted, reason)

    def _event(self, kind, message, check_id, data, observed_utc):
        self._output(f"Uživatelský stav: {message}.")
        if self._on_event is not None:
            self._on_event(kind, "INFO", message, observed_utc, check_id, data)

    def _persisted(self):
        """Jen to, co patří do souboru; počítadla odeznění ne."""
        return self.brightness, tuple((check_id, record.state, record.acknowledged_utc)
                                      for check_id, record in sorted(self.acks.records.items()))

    def _save_if_changed(self, now):
        current = self._persisted()
        if self.path is None or current == self._stored:
            self._retry_at = None
            return
        if self._retry_at is not None and now < self._retry_at:
            return
        try:
            save_state(self.path, *current)
        except OSError as error:
            self.save_failures += 1
            self._retry_at = now + SAVE_RETRY_SECONDS
            if not self._failing:  # jednou za řadu selhání, ne každou minutu
                self._output(f"Uživatelský stav: {self.path} nelze zapsat ({error}); platí v paměti, "
                             f"další pokus za {SAVE_RETRY_SECONDS:.0f} s.")
            self._failing = True
            return
        self.saves += 1
        self._stored, self._retry_at = current, None
        if self._failing:
            self._output(f"Uživatelský stav: {self.path} znovu uložen.")
        self._failing = False

    def _publish(self):
        # Jedno přiřazení neměnného objektu: čtenář z jiného vlákna vidí celý starý nebo celý nový stav.
        self.view = View(self.brightness, tuple((check_id, record.state)
                                                for check_id, record in sorted(self.acks.records.items())))
