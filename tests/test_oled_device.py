"""Testy výstupu na skutečný OLED (M7-S9) bez hardwaru: převod obrazu, SSD1306 nad
podvrženým i2c-dev, zápis ve vlákně a nezávislost měřicí smyčky. Čas je simulovaný."""

import errno
import io
import os
import threading
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from unittest import mock

from nexusstat import oled_device
from nexusstat.alarms import AlarmEvaluator
from nexusstat.config import AlarmsConfig, CollectorConfig, Config, ConfigError, OledConfig
from nexusstat.core import CheckResult, Measurement, State
from nexusstat.oled import CAROUSEL_PAGES, Mode, OledController
from nexusstat.oled_device import (DATA, FRAME_BYTES, FULL_WINDOW, I2C_SLAVE, INIT_SEQUENCE, OledDisplay, Ssd1306,
                                   frame_bytes)
from nexusstat.run import run_loop
from nexusstat.services import NAS_ID, complete_service_results
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID

try:
    from PIL import Image

    from nexusstat.render import render_screen
except ImportError:  # Pillow není nainstalované
    render_screen = None

OBSERVED = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)
WALL_TIME = datetime(2026, 11, 21, 9, 0)
START = 1000.0
DEVICE_CONFIG = OledConfig(device="/dev/i2c-1")
# Inicializace podle Pironmanu 5 na Nexusu, zapsaná nezávisle na modulu (M7-S9).
EXPECTED_INIT = bytes.fromhex("AE D5 80 A8 3F D3 00 40 8D 14 20 00 A1 C8 DA 12 81 CF D9 F1 DB 40 A4 A6")


def measured(now=START, state=State.INFO):
    """Skutečná (ne simulovaná) měření zdravého systému, platná 30 s."""
    def check(check_id, value, unit):
        return CheckResult(check_id, state, "popis", OBSERVED, now + 30, simulated=False,
                           measurement=Measurement(value, unit))
    return [check(CPU_USAGE_ID, 3, "%"), check(CPU_TEMP_ID, 35, "°C"), check(MEMORY_ID, 8, "%"),
            check(DISK_ROOT_ID, 2, "%")]


class FakeBus:
    """Náhrada os.open, ioctl, os.write a os.close pro zařízení i2c-dev."""

    def __init__(self, short_write=False, ioctl_error=None):
        self.opened, self.ioctls, self.writes, self.closed = [], [], [], []
        self.short_write = short_write
        self.ioctl_error = ioctl_error

    def device(self):
        return Ssd1306("/dev/i2c-1", 0x3C, open_device=self.open, ioctl=self.ioctl, write=self.write,
                       close=self.close)

    def open(self, path, flags):
        self.opened.append((path, flags))
        return 7

    def ioctl(self, fd, request, argument):
        self.ioctls.append((fd, request, argument))
        if self.ioctl_error:
            raise self.ioctl_error

    def write(self, fd, data):
        self.writes.append(bytes(data))
        return len(data) - 1 if self.short_write else len(data)

    def close(self, fd):
        self.closed.append(fd)


class FakeDevice:
    """Displej pro OledDisplay: zaznamená volání, umí selhat a umí zablokovat zápis."""

    def __init__(self, fail_open=0, fail_show=0, block=None):
        self.is_open = False
        self.calls = []
        self.fail_open, self.fail_show = fail_open, fail_show
        self.block = block  # threading.Event: show() čeká, dokud se nenastaví
        self.shown = threading.Event()

    def open(self):
        if self.fail_open:
            self.fail_open -= 1
            raise OSError(errno.EREMOTEIO, "Remote I/O error")
        self.is_open = True
        self.calls.append("open")

    def show(self, frame):
        if self.block is not None:
            self.block.wait(5)
        if self.fail_show:
            self.fail_show -= 1
            raise OSError(errno.EREMOTEIO, "Remote I/O error")
        self.calls.append(("show", frame))
        self.shown.set()

    def off(self):
        self.calls.append("off")

    def close(self):
        self.is_open = False
        self.calls.append("close")

    def frames(self):
        return [call[1] for call in self.calls if isinstance(call, tuple)]


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class FrameBytesTest(unittest.TestCase):
    def image(self, *pixels):
        image = Image.new("1", (128, 64), 0)
        for xy in pixels:
            image.putpixel(xy, 1)
        return image

    def test_page_layout_low_bit_on_top(self):
        self.assertEqual(frame_bytes(self.image()), bytes(FRAME_BYTES))
        cases = {(0, 0): (0, 0x01), (127, 63): (7 * 128 + 127, 0x80), (5, 10): (128 + 5, 0x04)}
        for xy, (index, value) in cases.items():
            with self.subTest(pixel=xy):
                frame = frame_bytes(self.image(xy))
                self.assertEqual(frame[index], value)
                self.assertEqual(sum(1 for byte in frame if byte), 1)
        column = frame_bytes(self.image(*((3, y) for y in range(8))))
        self.assertEqual(column[3], 0xFF)

    def test_rendered_screen_round_trips(self):
        controller = OledController(OledConfig())
        controller.press(START, 0)
        image = render_screen(controller, START, measured(), WALL_TIME)
        frame = frame_bytes(image)
        for x, y in ((x, y) for x in range(128) for y in range(64)):
            bit = frame[(y // 8) * 128 + x] >> (y % 8) & 1
            self.assertEqual(bit, 1 if image.getpixel((x, y)) else 0)

    def test_wrong_image_is_rejected(self):
        for image in (Image.new("L", (128, 64)), Image.new("1", (128, 32))):
            with self.subTest(mode=image.mode, size=image.size):
                with self.assertRaises(ValueError):
                    frame_bytes(image)


class Ssd1306Test(unittest.TestCase):
    def test_open_sets_address_and_initializes_with_display_off(self):
        bus = FakeBus()
        device = bus.device()
        device.open()
        self.assertEqual(bus.opened, [("/dev/i2c-1", os.O_RDWR | os.O_CLOEXEC)])
        self.assertEqual(bus.ioctls, [(7, I2C_SLAVE, 0x3C)])
        self.assertEqual(INIT_SEQUENCE, EXPECTED_INIT)
        self.assertEqual(bus.writes, [b"\x00" + EXPECTED_INIT])  # řídicí bajt příkazů, displej vypnutý

    def test_show_writes_pages_and_turns_on_once(self):
        bus = FakeBus()
        device = bus.device()
        device.open()
        frame = bytes(range(256)) * 4
        device.show(frame)
        device.show(frame)
        writes = bus.writes[1:]
        page_writes = [b"\x00" + FULL_WINDOW] + [bytes((DATA,)) + frame[p * 128:(p + 1) * 128] for p in range(8)]
        self.assertEqual(writes, page_writes + [b"\x00\xaf"] + page_writes)  # 0xAF jen poprvé
        self.assertEqual(len(writes[1]), 129)

    def test_off_only_when_on_and_close(self):
        bus = FakeBus()
        device = bus.device()
        device.open()
        device.off()
        self.assertEqual(len(bus.writes), 1)  # vypnutý displej se znovu nevypíná
        device.show(bytes(FRAME_BYTES))
        device.off()
        self.assertEqual(bus.writes[-1], b"\x00\xae")
        device.close()
        self.assertEqual((bus.closed, device.is_open), ([7], False))

    def test_errors(self):
        short = FakeBus(short_write=True)
        with self.assertRaises(OSError):
            short.device().open()
        refused = FakeBus(ioctl_error=OSError(errno.EBUSY, "busy"))
        device = refused.device()
        with self.assertRaises(OSError):
            device.open()
        self.assertEqual((refused.closed, device.is_open), ([7], False))  # deskriptor se nezapomene otevřený
        bus = FakeBus()
        device = bus.device()
        device.open()
        with self.assertRaises(ValueError):
            device.show(bytes(10))
        with self.assertRaises(OSError):
            Ssd1306("/dev/i2c-1", 0x3C).show(bytes(FRAME_BYTES))  # neotevřený


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class OledDisplayTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.device = FakeDevice()
        self.lines = []

    def display(self, **kwargs):
        return OledDisplay(DEVICE_CONFIG, monitored=(), validity_seconds=30, root_device=None, device=self.device,
                           output=self.lines.append, clock=self.clock, wall_clock=lambda: WALL_TIME, start=False,
                           **kwargs)

    def test_sleeps_by_default_and_initializes_dark(self):
        display = self.display()
        display.tick(measured(), START, OBSERVED)
        self.assertTrue(display.write_once())
        self.assertEqual(self.device.calls, ["open", "off"])
        self.assertFalse(display.write_once())  # spící displej se neobnovuje
        display.tick(measured(), START + 1, OBSERVED)
        self.assertEqual(self.lines, ["OLED: displej inicializován."])

    def test_wake_shows_five_real_pages_then_sleeps(self):
        display = self.display()
        results = measured(START, State.INFO)
        display.wake()
        shown = []
        for offset in (0, 10, 20, 30, 40):
            now = START + offset
            display.tick(results, now, OBSERVED)
            shown.append(display.controller.current_page(now))
            display.write_once()
        self.assertEqual(shown, list(CAROUSEL_PAGES))
        frames = self.device.frames()
        self.assertEqual(len(set(frames)), 5)
        # Každý obraz je přesně render_screen() nad výsledky smyčky s doplněnými službami (NEXUS N/A).
        completed = complete_service_results(results, OBSERVED, START + 30)
        for offset, page, frame in zip((0, 10, 20, 30, 40), CAROUSEL_PAGES, frames):
            controller = OledController(DEVICE_CONFIG, pages=(page,))
            controller.press(START + offset, 0)
            expected = frame_bytes(render_screen(controller, START + offset, completed, WALL_TIME, None))
            self.assertEqual(frame, expected, page)
        display.tick(results, START + 300, OBSERVED)  # 300 s od probuzení
        self.assertIs(display.controller.mode, Mode.SLEEP)
        display.write_once()
        self.assertEqual(self.device.calls[-1], "off")

    def test_second_wake_opens_status_with_real_problems(self):
        display = self.display()
        results = measured() + [CheckResult(NAS_ID, State.ERROR, "NAS", OBSERVED, START + 30, simulated=False)]
        display.wake()
        display.tick(results, START, OBSERVED)
        display.wake()
        display.tick(results, START + 1, OBSERVED)
        self.assertIs(display.controller.mode, Mode.STATUS)

    def test_unchanged_frame_is_not_rewritten(self):
        display = self.display()
        display.wake()
        display.tick(measured(), START, OBSERVED)
        self.assertTrue(display.write_once())
        display.tick(measured(), START + 0.5, OBSERVED)  # stejný obraz
        self.assertFalse(display.write_once())
        self.assertEqual(len(self.device.frames()), 1)

    def test_i2c_error_reports_once_and_retries_later(self):
        self.device.fail_show = 3
        display = self.display()
        display.wake()
        display.tick(measured(), START, OBSERVED)
        self.assertFalse(display.write_once())
        self.assertFalse(display.write_once())  # před uplynutím RETRY_SECONDS žádný další pokus
        for second in range(1, 4):
            display.tick(measured(), START + second, OBSERVED)
        failures = [line for line in self.lines if "chyba I²C" in line]
        self.assertEqual(len(failures), 1)
        self.assertIn("Remote I/O error", failures[0])
        self.clock.now += 60
        self.device.fail_show = 0
        self.assertTrue(display.write_once())
        display.tick(measured(), START + 4, OBSERVED)
        self.assertEqual(self.lines[-1], "OLED: displej znovu odpovídá.")
        self.assertEqual(self.device.calls.count("open"), 2)  # po chybě zavřeno a znovu otevřeno

    def test_missing_display_does_not_raise(self):
        self.device.fail_open = 99
        display = self.display()
        display.tick(measured(), START, OBSERVED)
        self.assertFalse(display.write_once())
        display.tick(measured(), START + 1, OBSERVED)
        self.assertEqual(len(self.lines), 1)
        self.assertIn("chyba I²C", self.lines[0])

    def test_render_error_turns_display_off_and_keeps_running(self):
        def broken(*args):
            raise RuntimeError("písmo")
        display = self.display(render=broken)
        display.wake()
        for second in range(3):
            display.tick(measured(), START + second, OBSERVED)
        self.assertEqual(len([line for line in self.lines if "nejde vykreslit" in line]), 1)
        display.write_once()
        self.assertEqual(self.device.frames(), [])

    def test_close_turns_off_and_closes(self):
        display = self.display()
        display.wake()
        display.tick(measured(), START, OBSERVED)
        display.write_once()
        display.close()
        self.assertEqual(self.device.calls[-2:], ["off", "close"])


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class ThreadAndLoopTest(unittest.TestCase):
    def test_thread_writes_and_close_turns_display_off(self):
        device = FakeDevice()
        display = OledDisplay(DEVICE_CONFIG, (), 30, None, device=device, output=lambda line: None,
                              wall_clock=lambda: WALL_TIME)
        display.wake()
        display.tick(measured(START), START, OBSERVED)
        self.assertTrue(device.shown.wait(5))
        display.close()
        self.assertEqual(device.calls[-2:], ["off", "close"])

    def test_blocked_i2c_never_blocks_the_tick(self):
        release = threading.Event()
        device = FakeDevice(block=release)
        display = OledDisplay(DEVICE_CONFIG, (), 30, None, device=device, output=lambda line: None,
                              wall_clock=lambda: WALL_TIME)
        display.wake()
        try:
            for second in range(20):  # vlákno visí v zápisu, takt se vrací hned
                display.tick(measured(START + second), START + second, OBSERVED)
        finally:
            release.set()
            display.close()

    def test_loop_feeds_display_with_the_same_results_and_survives_failures(self):
        class Collector:
            labels = {}
            simulated = False
            calls = 0

            def collect(self, now, observed_utc):
                Collector.calls += 1
                return measured(now)

        class Clock:
            def __init__(self):
                self.now = START

            def __call__(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds
                if self.now >= START + 5:
                    raise KeyboardInterrupt

        clock = Clock()
        device = FakeDevice(fail_open=99)  # displej chybí: měření musí běžet dál
        display = OledDisplay(DEVICE_CONFIG, (), 30, None, device=device, output=lambda line: None,
                              clock=clock, wall_clock=lambda: WALL_TIME, start=False)
        ticks = []
        original = display.tick
        display.tick = lambda results, now, observed: (ticks.append((now, results)), original(results, now, observed))
        lines = []
        results = run_loop(Collector(), CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=clock,
                           utc_clock=lambda: OBSERVED, sleep=clock.sleep, output=lines.append, tick_seconds=1,
                           display=display)
        self.assertEqual([now - START for now, _ in ticks], [0, 1, 2, 3, 4])
        self.assertEqual([r.id for r in ticks[-1][1]], [r.id for r in results])  # tytéž výsledky jako smyčka
        self.assertEqual(Collector.calls, 5)  # OLED nic neměří: jedno měření na takt jako bez displeje
        self.assertIn("Ukončeno", lines[-1])


@unittest.skipIf(render_screen is None, "Pillow není dostupné")
class HardwareTestCommandTest(unittest.TestCase):
    def test_shows_five_pages_and_status_then_turns_off(self):
        device = FakeDevice()
        lines = []
        with mock.patch("nexusstat.simulate.live_results", return_value=(
                complete_service_results(measured(), OBSERVED, START + 30), START)), \
                mock.patch("nexusstat.root_device.identify_root_device", return_value=None):
            oled_device.hardware_test(Config(oled=DEVICE_CONFIG), 3, device=device, sleep=lambda s: None,
                                      output=lines.append)
        self.assertEqual(lines, [f"OLED test: {name}" for name in (*CAROUSEL_PAGES, "STATUS")]
                         + ["OLED test: hotovo, displej vypnutý."])
        self.assertEqual(device.calls[0], "open")
        self.assertEqual(device.calls[-2:], ["off", "close"])
        self.assertEqual(len(device.frames()), 6)

    def test_invalid_arguments_stop_before_hardware(self):
        for argv in (["--address", "0x3E"], ["--device", "/dev/sda"], ["--seconds", "0"]):
            with self.subTest(argv=argv):
                with mock.patch.object(oled_device, "hardware_test") as test, redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        oled_device.main(argv)
                self.assertEqual(raised.exception.code, 2)
                test.assert_not_called()


class OledConfigTest(unittest.TestCase):
    def test_default_has_no_hardware(self):
        self.assertIsNone(OledConfig().device)
        self.assertEqual(OledConfig().address, 0x3C)

    def test_device_and_address_validation(self):
        self.assertEqual(OledConfig(device="/dev/i2c-1", address=0x3D).address, 0x3D)
        for kwargs in ({"device": "/dev/sda"}, {"device": "/dev/i2c-1/../x"}, {"device": 1},
                       {"address": 0x3E}, {"address": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ConfigError):
                    OledConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
