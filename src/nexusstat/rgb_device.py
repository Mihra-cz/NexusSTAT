"""Čtyři RGB LED Pironmanu 5 (M7-S10): WS2812 na SPI.

Co mají LED ukazovat, rozhoduje dál jen rgb.py (rgb_signal, RgbLeds) ze
stejných výsledků po AlarmEvaluatoru jako OLED a web; tento modul je jen
výstupní vrstva. LED jsou na SPI0 (/dev/spidev0.0, MOSI GPIO10): čtyři WS2812
v pořadí barev GRB, všechny vždy stejnou barvou. Parametry přenosu odpovídají
tomu, co na Nexusu používal Pironman 5 (knihovna Adafruit NeoPixel SPI):
6,4 MHz, každý bit WS2812 jako jeden bajt SPI (0 = 0b11000000, 1 = 0b11110000)
a 80 µs klidové úrovně před daty i po nich. Kód se nepřebírá; komunikace jen
standardní knihovnou (zařízení spidev, ioctl a prostý zápis).

Provoz: měřicí smyčka v taktu (`tick`) rozhodne signál stávajícím RgbLeds
(potvrzení závad mění jen tato smyčka, user_state.py) a vláknu předá jen
neměnný Signal, čas jeho začátku a jas. Vlákno z nich přehrává frame_at(),
spí do dalšího kroku blikání nebo do změny a zapisuje jen změněnou barvu.
Chyba SPI zařízení zavře a další pokus je nejdřív za RETRY_SECONDS; měření,
OLED, web i historie běží dál. Při startu a řízeném ukončení LED zhasnou; po
pádu procesu drží WS2812 poslední barvu (provozní omezení M7).

Krátká zkouška LED mimo službu (jen když běžící služba LED neovládá):
  PYTHONPATH=src python3 -m nexusstat.rgb_device [--device /dev/spidev0.0] [--seconds 3] [--brightness 20]
"""

import argparse
import errno
import fcntl
import math
import os
import struct
import sys
import threading
import time

from .config import ConfigError, RgbConfig
from .core import State
from .rgb import (BLACK, BLINKING, BRIGHTNESS_DEFAULT, CODES, CRITICAL, LIGHT, OFF, RgbLeds, Signal,
                  check_brightness, frame_at, pass_length)

SPI_IOC_WR_MODE = 0x40016B01  # _IOW('k', 1, __u8)
SPI_IOC_WR_BITS_PER_WORD = 0x40016B03  # _IOW('k', 3, __u8)
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04  # _IOW('k', 4, __u32)
SPI_MODE = 0
SPI_HZ = 6_400_000  # 8 bitů SPI = 1,25 µs = jeden bit WS2812
BIT0 = 0b11000000  # krátký puls: bit 0
BIT1 = 0b11110000  # dlouhý puls: bit 1
RESET = bytes(round(SPI_HZ * 80e-6 / 8))  # 80 µs nízké úrovně: konec dat a klidová úroveň MOSI
LED_COUNT = 4

# Všechny časy vzorů v rgb.py (0,4 s, 2,0 s, 0,25 s) jsou násobky kroku, takže
# přehrávač trefí každou změnu svit/tma a mezi nimi spí.
BLINK_STEP = 0.05
RETRY_SECONDS = 60  # po chybě SPI nejdřív za minutu, ne v každém kroku
TEST_SECONDS = 3  # zkouška LED: jak dlouho svítí každá barva
DEFAULT_DEVICE = "/dev/spidev0.0"

UNSHOWN = object()  # barvu LED po startu a po chybě SPI neznáme


def step_index(elapsed):
    """Pořadí kroku BLINK_STEP od začátku vzoru; okamžik na hranici patří už dalšímu kroku.

    Rezerva 1e-6 kroku pohltí chybu zaokrouhlení rozdílu velkých monotónních časů.
    """
    return math.floor(elapsed / BLINK_STEP + 1e-6)


def step_middle(step):
    """Čas uprostřed kroku: barva v celém kroku je stejná a střed je vždy 25 ms od každé změny."""
    return (step + 0.5) * BLINK_STEP


def encode(color, count=LED_COUNT):
    """Data SPI pro `count` LED stejné barvy (r, g, b): GRB, nejvyšší bit první, s klidem před i po."""
    red, green, blue = color
    data = bytearray()
    for byte in (green, red, blue) * count:
        for bit in range(7, -1, -1):
            data.append(BIT1 if byte >> bit & 1 else BIT0)
    return RESET + bytes(data) + RESET


class Ws2812:
    """LED WS2812 na zařízení spidev; každé `show()` je jeden celý přenos SPI."""

    def __init__(self, device, *, open_device=os.open, ioctl=fcntl.ioctl, write=os.write, close=os.close):
        self.device = device
        self._open = open_device
        self._ioctl = ioctl
        self._write = write
        self._close = close
        self._fd = None

    @property
    def is_open(self):
        return self._fd is not None

    def open(self):
        """Otevře zařízení a nastaví režim 0, 8 bitů a 6,4 MHz; na LED nic neposílá."""
        fd = self._open(self.device, os.O_RDWR | os.O_CLOEXEC)
        try:
            self._ioctl(fd, SPI_IOC_WR_MODE, struct.pack("B", SPI_MODE))
            self._ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
            self._ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", SPI_HZ))
        except OSError:
            self._close(fd)
            raise
        self._fd = fd

    def show(self, color):
        """Rozsvítí všechny LED barvou (r, g, b), BLACK = zhasnout."""
        if self._fd is None:
            raise OSError(errno.EBADF, "LED nejsou otevřené")
        data = encode(color)
        written = self._write(self._fd, data)
        if written != len(data):
            raise OSError(errno.EIO, f"neúplný zápis SPI ({written} z {len(data)} B)")

    def close(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                self._close(fd)
            except OSError:
                pass  # deskriptor už nepoužijeme; na LED to nic nemění


class RgbOutput:
    """Výstup měřicí smyčky na LED: rozhodnutí v taktu (RgbLeds), přehrávání a zápis ve vlákně.

    `tick()` volá jen měřicí smyčka, `close()` ukončení. `write_once()` je jeden
    krok vlákna; testy ho volají přímo (bez vlákna) se simulovaným časem.
    """

    def __init__(self, config, alarms_config=None, acks=None, *, device=None, output=print, clock=time.monotonic,
                 retry_seconds=RETRY_SECONDS, start=True):
        # Potvrzení patří vlastníkovi stavu (user_state.UserState.acks); RgbLeds je jen čte a započítává
        # měření, obojí v taktu smyčky, takže vlákno přehrávání na ně nikdy nesahá.
        self.leds = RgbLeds(alarms_config, acks=acks)
        self._device = device if device is not None else Ws2812(config.device)
        self._output = output
        self._clock = clock
        self._retry_seconds = retry_seconds
        self._cond = threading.Condition()
        self._wanted = None  # (Signal, začátek, jas) od smyčky; None = zhasnuto
        self._played = UNSHOWN  # _wanted, podle kterého vlákno naposledy kreslilo
        self._shown = UNSHOWN
        self._retry_at = None
        self._stopping = False
        self._outcome = None  # (zapsáno, důvod) pro výpis ve smyčce
        self._failing = False
        self._decide_failing = False
        self.writes = self.failures = 0
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="nexusstat-rgb", daemon=True)
            self._thread.start()

    def tick(self, results, now, awake, woke, brightness):
        """Takt smyčky: signál ze stejných výsledků a stavu OLED jako displej, předání vláknu.

        `results` jsou skutečně monitorované výsledky (bez služeb doplněných
        katalogem), `awake` = OLED nespí, `woke` = tento takt ho probudil stisk.
        """
        self._report()
        try:
            self.leds.set_brightness(brightness)
            if woke:
                # Probouzecí stisk: potvrzené aktivní závady se jednou přehrají (RgbLeds.wake).
                self.leds.wake(results, now)
            self.leds.frame(results, awake, now)  # rozhodne signál a vymění ho na hranici skupiny
            wanted = (self.leds.signal, self.leds.started, self.leds.brightness)
            self._decide_failing = False
        except Exception as error:  # noqa: BLE001 - chyba LED nesmí zastavit měření
            if not self._decide_failing:
                self._output(f"RGB: signál nejde určit ({type(error).__name__}: {error}); LED zhasnou, "
                             "měření pokračuje.")
            self._decide_failing = True
            wanted = None
        with self._cond:
            if wanted != self._wanted:
                self._wanted = wanted
                self._cond.notify_all()

    def write_once(self):
        """Jeden krok vlákna: barva v tomto okamžiku, pokud se liší od zobrazené. True = zapsáno."""
        with self._cond:
            wanted = self._played = self._wanted
            if self._retry_at is not None and self._clock() < self._retry_at:
                return False
        if wanted is None:
            color = BLACK
        else:
            signal, started, brightness = wanted
            color = frame_at(signal, step_middle(step_index(self._clock() - started)), brightness).rgb()
        if color == self._shown:
            return False
        opened = False
        try:
            if not self._device.is_open:
                self._device.open()
                opened = True
            self._device.show(color)
        except Exception as error:  # noqa: BLE001 - chyba SPI nesmí ukončit vlákno ani měření
            self._device.close()
            reason = getattr(error, "strerror", None) or f"{type(error).__name__}: {error}"
            with self._cond:
                self._shown, self._retry_at = UNSHOWN, self._clock() + self._retry_seconds
                self._outcome = (False, reason)
                self.failures += 1
            return False
        with self._cond:
            self._shown, self._retry_at = color, None
            self.writes += 1
            if opened:
                self._outcome = (True, None)
        return True

    def next_step(self):
        """Za kolik sekund má vlákno kreslit znovu: krok blikání nebo další pokus; None = až po změně."""
        deadlines = []
        if self._retry_at is not None:
            deadlines.append(self._retry_at)
        if self._wanted is not None and self._wanted[0].kind in BLINKING:
            _, started, _ = self._wanted
            deadlines.append(started + (step_index(self._clock() - started) + 1) * BLINK_STEP)
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - self._clock())

    def close(self, timeout=2.0):
        """Zhasne LED a ukončí vlákno; na zaseknuté SPI nečeká déle než `timeout`."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._thread is None:
            self._shutdown()
        else:
            self._thread.join(timeout)

    def _report(self):
        with self._cond:
            outcome, self._outcome = self._outcome, None
        if outcome is None:
            return
        written, reason = outcome
        if written:
            self._output("RGB: LED znovu odpovídají." if self._failing else "RGB: LED inicializované.")
            self._failing = False
            return
        if not self._failing:  # jeden řádek za řadu chyb, ne za každý pokus
            self._output(f"RGB: chyba SPI ({reason}); měření, OLED, web i historie běží dál, "
                         f"další pokus nejdřív za {self._retry_seconds} s.")
        self._failing = True

    def _run(self):
        while True:
            self.write_once()
            with self._cond:
                self._cond.wait_for(lambda: self._stopping or self._wanted != self._played, self.next_step())
                if self._stopping:
                    break
        self._shutdown()

    def _shutdown(self):
        # Řízené ukončení: LED zhasnout, ať nesvítí zastaralý stav.
        with self._cond:
            self._wanted, self._retry_at = None, None
        self.write_once()
        self._device.close()


def play(device, signal, seconds, brightness, steady=False, sleep=time.sleep):
    """Přehraje signál na LED po krocích BLINK_STEP (jen pro zkoušku); zapisuje jen změny."""
    shown = None
    for step in range(max(1, round(seconds / BLINK_STEP))):
        color = frame_at(signal, step_middle(step), brightness, steady).rgb()
        if color != shown:
            device.show(color)
            shown = color
        sleep(BLINK_STEP)


def test_phases(seconds):
    """(název, signál, délka v s, trvale) pro zkoušku: barvy a dva syntetické vzory, nic skutečného."""
    warning_code = Signal(CODES, ((State.WARNING, 2),))
    return (
        ("zhasnuto", Signal(OFF), seconds, False),
        ("bílá (zdravý stav, probuzený OLED)", Signal(LIGHT), seconds, False),
        ("oranžová (WARNING)", Signal(CODES, ((State.WARNING, 1),)), seconds, True),
        ("červená (ERROR)", Signal(CODES, ((State.ERROR, 1),)), seconds, True),
        ("WARNING kód 2: oranžově 2× a pauza", warning_code, pass_length(warning_code.steps), False),
        ("CRITICAL: červené blikání 0,25 s / 0,25 s", Signal(CRITICAL), seconds, False),
    )


def hardware_test(device_path, seconds=TEST_SECONDS, brightness=BRIGHTNESS_DEFAULT, *, device=None,
                  sleep=time.sleep, output=print):
    """Krátká zkouška skutečných LED syntetickými vzory; na konci LED zhasne.

    Služba NexusSTAT přitom LED ovládat nesmí (dva zapisovatelé na SPI by se přepisovali).
    """
    if device is None:
        device = Ws2812(device_path)
    device.open()
    try:
        for name, signal, duration, steady in test_phases(seconds):
            output(f"RGB test: {name}")
            play(device, signal, duration, brightness, steady, sleep)
    finally:
        try:
            device.show(BLACK)
        finally:
            device.close()
    output("RGB test: hotovo, LED zhasnuté.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Krátká zkouška skutečných RGB LED (WS2812 na SPI)")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help=f"zařízení SPI (výchozí {DEFAULT_DEVICE})")
    parser.add_argument("--seconds", type=int, default=TEST_SECONDS, help="jak dlouho svítí každá barva")
    parser.add_argument("--brightness", type=int, default=BRIGHTNESS_DEFAULT, help="jas v procentech, 5–100")
    args = parser.parse_args(argv)
    try:
        RgbConfig(args.device)  # stejná kontrola cesty jako konfigurace
        check_brightness(args.brightness)
        if not 1 <= args.seconds <= 60:
            raise ConfigError("--seconds musí být 1–60")
    except (ConfigError, ValueError) as error:
        parser.error(str(error))
    try:
        hardware_test(args.device, args.seconds, args.brightness)
    except OSError as error:
        print(f"RGB test: chyba SPI na {args.device}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
