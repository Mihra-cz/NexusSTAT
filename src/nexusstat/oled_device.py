"""Skutečný OLED Pironmanu 5 (M7-S9): SSD1306 128 × 64 na I²C.

Obrazovky kreslí stávající render.render_screen() podle automatu
oled.OledController z výsledků měřicí smyčky; tento modul je jen výstupní
vrstva: převede 1bitový obraz do paměti řadiče a zapíše ho na displej.

Řadič, adresa a inicializace jsou ověřené na Nexusu: SSD1306 na /dev/i2c-1,
adresa 0x3C, řídicí bajt 0x00 před příkazy a 0x40 před daty, vodorovné
adresování po stránkách o 8 řádcích. Sekvence odpovídá té, kterou používal
Pironman 5; jeho kód se nepřebírá. Komunikace jen standardní knihovnou:
zařízení i2c-dev, ioctl I2C_SLAVE a prostý zápis.

Provoz: měřicí smyčka v taktu (`tick`) jen uplatní časovače automatu, vykreslí
obraz a předá ho jedinému vláknu zápisu; na I²C nikdy nečeká. Vlákno zapisuje
jen poslední obraz (starší se zahodí) a stejný obraz znovu nezapisuje. Chyba
I²C nebo chybějící displej zařízení zavře a další pokus je nejdřív za
RETRY_SECONDS; měření, web i historie běží dál. Displej spí, dokud ho nevzbudí
krátký stisk (REQUIREMENTS, OLED body 1 a 9): fyzické tlačítko (button.py,
M7-S10) nebo záložní signál SIGUSR1 (`systemctl kill -s USR1 nexusstat.service`).
Po každém taktu `awake` a `woke` říkají RGB, zda OLED nespí a zda ho právě
probudil stisk. Při řízeném ukončení se displej vypne; po pádu procesu drží
SSD1306 poslední obraz.

Krátká zkouška displeje mimo službu (jen když běžící služba displej neovládá):
  PYTHONPATH=src python3 -m nexusstat.oled_device [--device /dev/i2c-1] [--address 0x3C] [--seconds 3]
"""

import argparse
import errno
import fcntl
import os
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .config import DISPLAY_HEIGHT, DISPLAY_WIDTH, Config, ConfigError, load_config
from .core import active_problems
from .oled import Mode, OledController
from .services import complete_service_results

I2C_SLAVE = 0x0703  # ioctl i2c-dev: adresa zařízení pro další zápisy
COMMAND = 0x00  # řídicí bajt: následují příkazy
DATA = 0x40  # řídicí bajt: následují data paměti displeje
PAGES = DISPLAY_HEIGHT // 8  # stránka = 8 řádků, jeden bajt na sloupec
FRAME_BYTES = DISPLAY_WIDTH * PAGES

DISPLAY_OFF = 0xAE
DISPLAY_ON = 0xAF
# Inicializace 128 × 64 s vnitřní nábojovou pumpou. Displej zůstane vypnutý,
# rozsvítí ho až první zapsaný obraz, takže po startu nebliká obsah paměti.
INIT_SEQUENCE = bytes((
    DISPLAY_OFF,
    0xD5, 0x80,  # dělič hodin a frekvence oscilátoru
    0xA8, 0x3F,  # multiplex 64 řádků
    0xD3, 0x00,  # bez svislého posunu
    0x40,  # první řádek paměti 0
    0x8D, 0x14,  # nábojová pumpa zapnutá
    0x20, 0x00,  # vodorovné adresování
    0xA1,  # zrcadlení segmentů ...
    0xC8,  # ... a řádků: společně obraz bez otočení, jak je displej ve skříni
    0xDA, 0x12,  # zapojení COM pro 128 × 64
    0x81, 0xCF,  # kontrast
    0xD9, 0xF1,  # předběžné nabití
    0xDB, 0x40,  # úroveň VCOMH
    0xA4,  # zobrazovat obsah paměti
    0xA6,  # bez inverze
))
# Okno zápisu přes celý displej: sloupce 0–127, stránky 0–7.
FULL_WINDOW = bytes((0x21, 0, DISPLAY_WIDTH - 1, 0x22, 0, PAGES - 1))

RETRY_SECONDS = 60  # po chybě I²C nejdřív za minutu, ne v každém taktu
TEST_SECONDS = 3  # zkouška displeje: jak dlouho svítí každá obrazovka
DEFAULT_DEVICE = "/dev/i2c-1"

UNSHOWN = object()  # obsah displeje po startu a po chybě I²C neznáme


def frame_bytes(image):
    """Obraz 128 × 64 (Pillow, režim "1") jako paměť SSD1306: 8 stránek × 128 sloupců.

    Bajt je svislý pruh 8 pixelů jednoho sloupce, nejnižší bit nahoře.
    """
    if image.mode != "1" or image.size != (DISPLAY_WIDTH, DISPLAY_HEIGHT):
        raise ValueError(f"obraz musí být 1bitový {DISPLAY_WIDTH} × {DISPLAY_HEIGHT}, "
                         f"ne {image.mode} {image.size[0]} × {image.size[1]}")
    raw = image.tobytes()  # po řádcích, 1 bit na pixel, nejvyšší bit vlevo
    stride = DISPLAY_WIDTH // 8
    frame = bytearray(FRAME_BYTES)
    for y in range(DISPLAY_HEIGHT):
        row = raw[y * stride:(y + 1) * stride]
        page, bit = divmod(y, 8)
        base = page * DISPLAY_WIDTH
        for x in range(DISPLAY_WIDTH):
            if row[x >> 3] & (0x80 >> (x & 7)):
                frame[base + x] |= 1 << bit
    return bytes(frame)


class Ssd1306:
    """SSD1306 na zařízení i2c-dev; každé volání je jedna nebo několik celých transakcí I²C."""

    def __init__(self, device, address, *, open_device=os.open, ioctl=fcntl.ioctl, write=os.write, close=os.close):
        self.device = device
        self.address = address
        self._open = open_device
        self._ioctl = ioctl
        self._write = write
        self._close = close
        self._fd = None
        self._on = False

    @property
    def is_open(self):
        return self._fd is not None

    def open(self):
        """Otevře zařízení, nastaví adresu a inicializuje řadič; displej zůstane vypnutý."""
        fd = self._open(self.device, os.O_RDWR | os.O_CLOEXEC)
        try:
            self._ioctl(fd, I2C_SLAVE, self.address)
        except OSError:
            self._close(fd)
            raise
        self._fd, self._on = fd, False
        self._command(INIT_SEQUENCE)

    def show(self, frame):
        """Zapíše celý obraz (frame_bytes) a při prvním obrazu displej zapne."""
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"obraz musí mít {FRAME_BYTES} B, ne {len(frame)}")
        self._command(FULL_WINDOW)
        # Po stránkách: kratší transakce, a chyba I²C nenechá viset dlouhý přenos.
        for page in range(PAGES):
            self._send(bytes((DATA,)) + frame[page * DISPLAY_WIDTH:(page + 1) * DISPLAY_WIDTH])
        if not self._on:
            self._command(bytes((DISPLAY_ON,)))
            self._on = True

    def off(self):
        if self._on:
            self._command(bytes((DISPLAY_OFF,)))
            self._on = False

    def close(self):
        fd, self._fd, self._on = self._fd, None, False
        if fd is not None:
            try:
                self._close(fd)
            except OSError:
                pass  # deskriptor už nepoužijeme; chyba zavření na displeji nic nemění

    def _command(self, commands):
        self._send(bytes((COMMAND,)) + commands)

    def _send(self, data):
        if self._fd is None:
            raise OSError(errno.EBADF, "displej není otevřený")
        written = self._write(self._fd, data)
        if written != len(data):
            raise OSError(errno.EIO, f"neúplný zápis I²C ({written} z {len(data)} B)")


class OledDisplay:
    """Výstup měřicí smyčky na skutečný displej: automat a vykreslení v taktu, zápis ve vlákně.

    `tick()` volá jen měřicí smyčka, `wake()` vlákno tlačítka nebo obsluha
    SIGUSR1 (krátký stisk), `close()` ukončení. `write_once()` je jeden krok vlákna zápisu;
    testy ho volají přímo (bez vlákna) se simulovaným časem.
    """

    def __init__(self, config, monitored, validity_seconds, root_device, *, device=None, render=None,
                 output=print, clock=time.monotonic, wall_clock=datetime.now, retry_seconds=RETRY_SECONDS,
                 start=True):
        if render is None:
            from .render import render_screen as render  # Pillow jen se zapnutým displejem
        self.controller = OledController(config)
        self._device = device if device is not None else Ssd1306(config.device, config.address)
        self._render = render
        self._monitored = frozenset(monitored)
        self._validity_seconds = validity_seconds
        self._root_device = root_device
        self._output = output
        self._clock = clock
        self._wall_clock = wall_clock
        self._retry_seconds = retry_seconds
        self._cond = threading.Condition()
        self._wanted = None  # poslední obraz od smyčky; None = displej vypnutý (SLEEP)
        self._shown = UNSHOWN
        self._retry_at = None  # nejdřívější další pokus po chybě I²C
        self._stopping = False
        self._outcome = None  # (zapsáno, důvod) pro výpis ve smyčce
        self._failing = False
        self._render_failing = False
        self._woken = False
        self.awake = False  # po taktu: OLED nespí (CAROUSEL nebo STATUS)
        self.woke = False  # po taktu: tento takt OLED probudil stisk
        self.writes = self.failures = 0
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="nexusstat-oled", daemon=True)
            self._thread.start()

    def wake(self):
        """Krátký stisk (tlačítko nebo SIGUSR1); uplatní se v příštím taktu smyčky."""
        # Jen příznak: obsluha signálu nesmí sahat na automat uprostřed taktu.
        self._woken = True

    def tick(self, results, now, observed_utc):
        """Takt smyčky: výpis výsledku zápisu, probuzení, časovače automatu a obraz pro vlákno."""
        self._report()
        self.woke = False
        try:
            # Služby se doplní jako na webu, takže displej i web ukazují tytéž stavy.
            shown = complete_service_results(results, observed_utc, now + self._validity_seconds,
                                             monitored=self._monitored)
            if self._woken:
                self._woken = False
                # Probouzecí stisk se pozná až po uplatnění časovačů: i stisk po
                # uplynutí okna kolotoče probouzí (press() je uplatní stejně).
                self.controller.update(now)
                asleep = self.controller.mode is Mode.SLEEP
                self.controller.press(now, len(active_problems(shown, now)))
                self.woke = asleep and self.controller.mode is not Mode.SLEEP
            self.controller.update(now)
            frame = None
            if self.controller.mode is not Mode.SLEEP:
                image = self._render(self.controller, now, shown, self._wall_clock(), self._root_device)
                frame = frame_bytes(image)
            self._render_failing = False
        except Exception as error:  # noqa: BLE001 - chyba displeje nesmí zastavit měření
            if not self._render_failing:
                self._output(f"OLED: obraz nejde vykreslit ({type(error).__name__}: {error}); displej se vypne, "
                             "měření pokračuje.")
            self._render_failing = True
            frame = None  # raději zhasnout než ukazovat zastaralý obraz
        self.awake = self.controller.mode is not Mode.SLEEP
        with self._cond:
            if frame != self._wanted:
                self._wanted = frame
                self._cond.notify_all()

    def write_once(self):
        """Jeden krok zápisu: poslední obraz, pokud se liší od zobrazeného. True = zapsáno."""
        with self._cond:
            frame = self._wanted
            if frame == self._shown:
                return False
            if self._retry_at is not None and self._clock() < self._retry_at:
                return False
        opened = False
        try:
            if not self._device.is_open:
                self._device.open()
                opened = True
            if frame is None:
                self._device.off()
            else:
                self._device.show(frame)
        except Exception as error:  # noqa: BLE001 - chyba I²C nesmí ukončit vlákno ani měření
            self._device.close()
            reason = getattr(error, "strerror", None) or f"{type(error).__name__}: {error}"
            with self._cond:
                self._shown, self._retry_at = UNSHOWN, self._clock() + self._retry_seconds
                self._outcome = (False, reason)
                self.failures += 1
            return False
        with self._cond:
            self._shown, self._retry_at = frame, None
            self.writes += 1
            if opened:
                self._outcome = (True, None)
        return True

    def close(self, timeout=2.0):
        """Vypne displej a ukončí vlákno; na zaseknuté I²C nečeká déle než `timeout`."""
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
            self._output("OLED: displej znovu odpovídá." if self._failing else "OLED: displej inicializován.")
            self._failing = False
            return
        if not self._failing:  # jeden řádek za řadu chyb, ne za každý pokus
            self._output(f"OLED: chyba I²C ({reason}); měření, web i historie běží dál, "
                         f"další pokus nejdřív za {self._retry_seconds} s.")
        self._failing = True

    def _due(self):
        if self._stopping:
            return True
        retry_ready = self._retry_at is None or self._clock() >= self._retry_at
        return self._wanted != self._shown and retry_ready

    def _run(self):
        while True:
            with self._cond:
                timeout = None if self._retry_at is None else max(0.0, self._retry_at - self._clock())
                self._cond.wait_for(self._due, timeout)
                if self._stopping:
                    break
            self.write_once()
        self._shutdown()

    def _shutdown(self):
        # Řízené ukončení: displej zhasnout, ať nesvítí zastaralý obraz.
        with self._cond:
            self._wanted, self._retry_at = None, None
        self.write_once()
        self._device.close()


def hardware_test(config, seconds=TEST_SECONDS, *, device=None, sleep=time.sleep, output=print):
    """Krátká zkouška skutečného displeje: pět stránek kolotoče a STATUS z jednoho živého snímku.

    Měří jen read-only jako simulate --live; služba NexusSTAT přitom displej
    ovládat nesmí (dva zapisovatelé na I²C by se přepisovali).
    """
    from . import simulate
    from .render import render_screen
    from .root_device import identify_root_device

    results, now = simulate.live_results(config)
    images = simulate.screens(config.oled, render_screen, now, results, datetime.now(), identify_root_device())
    if device is None:
        device = Ssd1306(config.oled.device or DEFAULT_DEVICE, config.oled.address)
    device.open()
    try:
        for name, image in images:
            device.show(frame_bytes(image))
            output(f"OLED test: {name}")
            sleep(seconds)
    finally:
        try:
            device.off()
        finally:
            device.close()
    output("OLED test: hotovo, displej vypnutý.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Krátká zkouška skutečného OLED (SSD1306 na I²C)")
    parser.add_argument("--config", type=Path, help="TOML se sekcemi [oled], [collector], [network] a [services]")
    parser.add_argument("--device", help=f"zařízení I²C (výchozí z [oled] device, jinak {DEFAULT_DEVICE})")
    parser.add_argument("--address", type=lambda text: int(text, 0), help="adresa displeje, 0x3C nebo 0x3D")
    parser.add_argument("--seconds", type=int, default=TEST_SECONDS, help="jak dlouho svítí každá obrazovka")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config) if args.config else Config()
        # replace() znovu ověří zařízení i adresu stejně jako konfigurace.
        oled = replace(config.oled, device=args.device or config.oled.device or DEFAULT_DEVICE,
                       address=config.oled.address if args.address is None else args.address)
        if not 1 <= args.seconds <= 60:
            raise ConfigError("--seconds musí být 1–60")
    except (ConfigError, OSError, ValueError) as error:
        parser.error(str(error))
    try:
        hardware_test(replace(config, oled=oled), args.seconds)
    except OSError as error:
        print(f"OLED test: chyba I²C na {oled.device} (0x{oled.address:02X}): {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
