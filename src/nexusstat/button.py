"""Fyzické tlačítko Pironmanu 5 (M7-S10): krátký stisk pro OLED.

Tlačítko je vstupní zařízení Linuxu `pwr_button` s jedinou klávesou KEY_POWER.
Čte se stálou cestou z udev (/dev/input/by-path/platform-pwr_button-event),
číslo eventN se mezi starty může změnit. Na Nexusu ho logind nesleduje
(pravidlo udev 71 odebírá tag power-switch), takže stisk sám systém nevypne;
NexusSTAT si zařízení navíc převezme výhradně (EVIOCGRAB), aby ho nikdo jiný
nečetl.

Krátký stisk = puštění do SHORT_PRESS_SECONDS. Zavolá `on_press` (v provozu
OledDisplay.wake, stejně jako záložní SIGUSR1) a uplatní se v příštím taktu
smyčky; význam stisků určuje dál jen OledController. Delší podržení nic
nedělá: dlouhý stisk (řízené vypnutí) se zatím neimplementuje a dlouhé
podržení řeší Raspberry Pi 5 samo hardwarově.

Události čte jediné vlákno blokované v select(): žádné aktivní čekání, žádný
další proces a měřicí smyčku nikdy nezdrží. Chybějící zařízení, chybějící
oprávnění nebo cizí výhradní převzetí se ohlásí jednou a další pokus je
nejdřív za RETRY_SECONDS; měření, OLED (i přes SIGUSR1), web i historie běží dál.
"""

import errno
import fcntl
import os
import select
import struct
import threading

EV_KEY = 0x01
KEY_POWER = 116
RELEASED, PRESSED = 0, 1  # hodnotu 2 (opakování klávesy) pwr_button neposílá; ignoruje se
# struct input_event: čas (struct timeval, dvě long), typ, kód a hodnota.
EVENT = struct.Struct("llHHi")
EVIOCGRAB = 0x40044590  # _IOW('E', 0x90, int): výhradní převzetí zařízení
EVIOCSCLOCKID = 0x400445A0  # _IOW('E', 0xa0, int): časy událostí z daných hodin
CLOCK_MONOTONIC = 1
# Pironman bral podržení 2–5 s jako vypnutí; NexusSTAT na podržení nereaguje vůbec.
SHORT_PRESS_SECONDS = 2.0
RETRY_SECONDS = 60  # po chybě zařízení nejdřív za minutu
READ_EVENTS = 16  # událostí na jedno čtení; jeden stisk jsou čtyři (klávesa a SYN, dvakrát)


def key_events(data):
    """(čas v s, hodnota) událostí KEY_POWER v přečtených datech; ostatní (SYN) se přeskočí."""
    usable = len(data) - len(data) % EVENT.size
    for seconds, micros, kind, code, value in EVENT.iter_unpack(data[:usable]):
        if kind == EV_KEY and code == KEY_POWER:
            yield seconds + micros / 1_000_000, value


class PowerButton:
    """Krátké stisky tlačítka pro OLED; čte vlastní vlákno, `close()` ho ukončí.

    `connect()`, `read_once()` a `feed()` jsou kroky vlákna; testy je volají
    přímo bez vlákna a bez skutečného zařízení.
    """

    def __init__(self, device, on_press, *, output=print, open_device=os.open, ioctl=fcntl.ioctl, read=os.read,
                 close=os.close, retry_seconds=RETRY_SECONDS, start=True):
        self.device = device
        self._on_press = on_press
        self._output = output
        self._open = open_device
        self._ioctl = ioctl
        self._read = read
        self._close = close
        self._retry_seconds = retry_seconds
        self._fd = None
        self._pressed_at = None  # čas stisku, dokud tlačítko nepustí
        self._connected = False  # někdy dřív převzaté (pro hlášku po obnově)
        self._failing = False
        self.presses = 0  # krátké stisky předané OLED
        # Probouzí vlákno ze select() při ukončení; nic jiného se přes něj neposílá.
        self._stop_read, self._stop_write = os.pipe()
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="nexusstat-button", daemon=True)
            self._thread.start()

    def connect(self):
        """Otevře a výhradně převezme zařízení; vrací True, jinak chybu jednou ohlásí."""
        try:
            fd = self._open(self.device, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as error:
            self._fail(error)
            return False
        try:
            # Délku stisku počítají monotónní hodiny: seřízení času ji nezkreslí.
            self._ioctl(fd, EVIOCSCLOCKID, struct.pack("i", CLOCK_MONOTONIC))
            self._ioctl(fd, EVIOCGRAB, 1)
        except OSError as error:
            self._close(fd)
            self._fail(error)
            return False
        self._fd, self._pressed_at = fd, None
        if not self._connected or self._failing:
            self._output("Tlačítko: znovu převzaté." if self._connected else
                         f"Tlačítko: {self.device} výhradně převzaté.")
        self._connected, self._failing = True, False
        return True

    def read_once(self):
        """Přečte dostupné události; chyba nebo zmizelé zařízení ho zavře a ohlásí."""
        try:
            data = self._read(self._fd, EVENT.size * READ_EVENTS)
        except BlockingIOError:
            return
        except OSError as error:
            self._disconnect()
            self._fail(error)
            return
        if not data:
            self._disconnect()
            self._fail(OSError(errno.ENODEV, "zařízení zmizelo"))
            return
        self.feed(data)

    def feed(self, data):
        """Zpracuje události; každý krátký stisk zavolá `on_press`."""
        for moment, value in key_events(data):
            if value == PRESSED:
                self._pressed_at = moment
            elif value == RELEASED and self._pressed_at is not None:
                # Puštění bez zaznamenaného stisku (stisk před převzetím) se nepočítá.
                held, self._pressed_at = moment - self._pressed_at, None
                if held < SHORT_PRESS_SECONDS:
                    self.presses += 1
                    self._on_press()

    def close(self, timeout=2.0):
        """Ukončí vlákno a pustí zařízení; na vlákno nečeká déle než `timeout`."""
        os.write(self._stop_write, b"x")
        if self._thread is None:
            self._disconnect()
        else:
            self._thread.join(timeout)
            if self._thread.is_alive():
                return  # vlákno roury ještě používá; uvolní je konec procesu
        os.close(self._stop_read)
        os.close(self._stop_write)

    def _fail(self, error):
        if error.errno == errno.EBUSY:
            reason = "zařízení výhradně drží jiný proces"
        else:
            reason = error.strerror or str(error)
        if not self._failing:  # jeden řádek za řadu chyb, ne za každý pokus
            self._output(f"Tlačítko: {self.device} nejde použít ({reason}); OLED dál probudí SIGUSR1, "
                         f"měření, web i historie běží dál, další pokus nejdřív za {self._retry_seconds} s.")
        self._failing = True

    def _disconnect(self):
        fd, self._fd, self._pressed_at = self._fd, None, None
        if fd is not None:
            try:
                self._close(fd)  # zavření uvolní i výhradní převzetí
            except OSError:
                pass

    def _stopped(self, timeout):
        """Čeká na ukončení nejvýš `timeout` s (None = bez limitu); True = ukončit."""
        ready, _, _ = select.select([self._stop_read], [], [], timeout)
        return bool(ready)

    def _run(self):
        while True:
            if self._fd is None and not self.connect():
                if self._stopped(self._retry_seconds):
                    break
                continue
            ready, _, _ = select.select([self._fd, self._stop_read], [], [])
            if self._stop_read in ready:
                break
            self.read_once()
        self._disconnect()
