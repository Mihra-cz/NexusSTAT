"""Testy fyzického tlačítka (M7-S10) bez hardwaru: události evdev, výhradní převzetí,
chyby zařízení, vlákno nad skutečnou rourou místo /dev/input a vazba na automat OLED.
Čas stisků je v událostech (simulovaný), nic se neuspává."""

import errno
import os
import struct
import threading
import unittest
from datetime import datetime, timezone

from nexusstat.button import (EV_KEY, EVENT, EVIOCGRAB, EVIOCSCLOCKID, KEY_POWER, SHORT_PRESS_SECONDS, PowerButton,
                              key_events)
from nexusstat.config import ButtonConfig, Config, ConfigError, OledConfig
from nexusstat.core import CheckResult, Measurement, State
from nexusstat.oled import Mode
from nexusstat.oled_device import OledDisplay
from nexusstat.system_monitor import CPU_USAGE_ID

DEVICE = "/dev/input/by-path/platform-pwr_button-event"
EV_SYN = 0
OBSERVED = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)
START = 1000.0


def event(moment, kind, code, value):
    seconds = int(moment)
    return EVENT.pack(seconds, round((moment - seconds) * 1_000_000), kind, code, value)


def press(down, up):
    """Stisk a puštění KEY_POWER včetně SYN, jak je posílá pwr_button."""
    return (event(down, EV_KEY, KEY_POWER, 1) + event(down, EV_SYN, 0, 0)
            + event(up, EV_KEY, KEY_POWER, 0) + event(up, EV_SYN, 0, 0))


class FakeInput:
    """Náhrada os.open, ioctl, os.read a os.close pro vstupní zařízení."""

    def __init__(self, open_error=None, ioctl_error=None):
        self.open_error, self.ioctl_error = open_error, ioctl_error
        self.opened, self.ioctls, self.closed = [], [], []
        self.reads = []  # připravené výsledky čtení: bajty nebo výjimka

    def button(self, on_press, lines):
        return PowerButton(DEVICE, on_press, output=lines.append, open_device=self.open, ioctl=self.ioctl,
                           read=self.read, close=self.close, start=False)

    def open(self, path, flags):
        self.opened.append((path, flags))
        if self.open_error:
            raise self.open_error
        return 9

    def ioctl(self, fd, request, argument):
        self.ioctls.append((fd, request, argument))
        if self.ioctl_error and request == EVIOCGRAB:
            raise self.ioctl_error

    def read(self, fd, size):
        result = self.reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self, fd):
        self.closed.append(fd)


class KeyEventsTest(unittest.TestCase):
    def test_only_key_power_with_time_and_value(self):
        data = press(5.25, 5.5) + event(6, EV_KEY, 30, 1)  # jiná klávesa se ignoruje
        self.assertEqual(list(key_events(data)), [(5.25, 1), (5.5, 0)])

    def test_incomplete_event_is_ignored(self):
        self.assertEqual(list(key_events(press(1, 1.1) + b"\0" * 5)), [(1, 1), (1.1, 0)])


class PressTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.lines = [], []
        self.button = FakeInput().button(lambda: self.calls.append("press"), self.lines)

    def test_short_press_calls_once_per_release(self):
        self.button.feed(press(10.0, 10.2))
        self.button.feed(press(11.0, 11.0 + SHORT_PRESS_SECONDS - 0.01))
        self.assertEqual(self.calls, ["press", "press"])
        self.assertEqual(self.button.presses, 2)

    def test_hold_does_nothing(self):
        # Žádné nové gesto: podržení (u Pironmanu vypnutí) se jen přejde.
        self.button.feed(press(10.0, 10.0 + SHORT_PRESS_SECONDS))
        self.button.feed(press(20.0, 31.0))
        self.assertEqual(self.calls, [])

    def test_press_split_between_reads_and_release_without_press(self):
        self.button.feed(event(3, EV_KEY, KEY_POWER, 0))  # puštění stisku z doby před převzetím
        self.button.feed(event(4, EV_KEY, KEY_POWER, 1))
        self.button.feed(event(4.3, EV_KEY, KEY_POWER, 2))  # opakování se ignoruje
        self.assertEqual(self.calls, [])
        self.button.feed(event(4.4, EV_KEY, KEY_POWER, 0))
        self.assertEqual(self.calls, ["press"])


class DeviceTest(unittest.TestCase):
    def test_connect_grabs_with_monotonic_timestamps(self):
        fake, lines = FakeInput(), []
        button = fake.button(lambda: None, lines)
        self.assertTrue(button.connect())
        [(path, flags)] = fake.opened
        self.assertEqual(path, DEVICE)
        self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)  # tlačítko se jen čte
        self.assertEqual(fake.ioctls, [(9, EVIOCSCLOCKID, struct.pack("i", 1)), (9, EVIOCGRAB, 1)])
        self.assertEqual(lines, [f"Tlačítko: {DEVICE} výhradně převzaté."])

    def test_missing_device_reports_once_and_mentions_sigusr1(self):
        fake, lines = FakeInput(open_error=FileNotFoundError(errno.ENOENT, "No such file or directory")), []
        button = fake.button(lambda: None, lines)
        self.assertFalse(button.connect())
        self.assertFalse(button.connect())
        self.assertEqual(len(lines), 1)
        self.assertIn("No such file or directory", lines[0])
        self.assertIn("SIGUSR1", lines[0])
        self.assertIn("další pokus nejdřív za 60 s", lines[0])

    def test_foreign_grab_closes_device_and_recovers(self):
        fake, lines = FakeInput(ioctl_error=OSError(errno.EBUSY, "Device or resource busy")), []
        button = fake.button(lambda: None, lines)
        self.assertFalse(button.connect())
        self.assertEqual(fake.closed, [9])
        self.assertIn("výhradně drží jiný proces", lines[0])
        fake.ioctl_error = None
        self.assertTrue(button.connect())
        self.assertEqual(lines[-1], f"Tlačítko: {DEVICE} výhradně převzaté.")

    def test_read_errors_disconnect_and_report(self):
        for result, reason in ((OSError(errno.ENODEV, "No such device"), "No such device"),
                               (b"", "zařízení zmizelo")):
            with self.subTest(reason):
                fake, lines, calls = FakeInput(), [], []
                button = fake.button(lambda: calls.append(1), lines)
                button.connect()
                fake.reads = [press(1, 1.1), BlockingIOError(), result]
                button.read_once()
                button.read_once()  # nic k přečtení: bez chyby
                button.read_once()
                self.assertEqual(calls, [1])
                self.assertEqual(fake.closed, [9])
                self.assertIn(reason, lines[-1])
                self.assertTrue(button.connect())  # další pokus zařízení znovu převezme
                self.assertEqual(lines[-1], "Tlačítko: znovu převzaté.")


class ThreadTest(unittest.TestCase):
    """Vlákno nad skutečnou rourou místo /dev/input: select() bez aktivního čekání."""

    def test_thread_reads_presses_and_close_releases_device(self):
        read_end, write_end = os.pipe()
        pressed = threading.Event()
        closed = []

        def close(fd):
            closed.append(fd)
            os.close(fd)

        button = PowerButton(DEVICE, pressed.set, output=lambda line: None, open_device=lambda path, flags: read_end,
                             ioctl=lambda fd, request, argument: None, close=close)
        try:
            os.write(write_end, press(1, 1.2))
            self.assertTrue(pressed.wait(5))
        finally:
            button.close()
            os.close(write_end)
        self.assertFalse(button._thread.is_alive())
        self.assertEqual(closed, [read_end])
        self.assertEqual(button.presses, 1)

    def test_close_stops_thread_waiting_for_retry(self):
        lines = []

        def missing(path, flags):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")

        button = PowerButton(DEVICE, lambda: None, output=lines.append, open_device=missing)
        button.close()  # vlákno čeká na další pokus (60 s), ukončení ho probudí hned
        self.assertFalse(button._thread.is_alive())
        self.assertEqual(len(lines), 1)


class FakeImage:
    """Prázdný obraz 128 × 64 bez Pillow (zápis na displej tu není předmětem testu)."""

    mode, size = "1", (128, 64)

    def tobytes(self):
        return bytes(128 * 64 // 8)


class FakeDisplayDevice:
    is_open = False

    def open(self):
        self.is_open = True

    def show(self, frame):
        pass

    def off(self):
        pass

    def close(self):
        self.is_open = False


class ButtonDrivesOledTest(unittest.TestCase):
    """Tlačítko jde do skutečného OledController stejnou cestou jako SIGUSR1."""

    def test_press_wakes_opens_status_returns_and_sleeps(self):
        display = OledDisplay(OledConfig(device="/dev/i2c-1"), (), 30, None, device=FakeDisplayDevice(),
                              render=lambda *args: FakeImage(), output=lambda line: None, start=False)
        button = FakeInput().button(display.wake, [])

        def results(now):
            return [CheckResult(CPU_USAGE_ID, State.INFO, "CPU", OBSERVED, now + 30, simulated=False,
                                measurement=Measurement(3, "%"))]

        display.tick(results(START), START, OBSERVED)
        self.assertEqual((display.controller.mode, display.awake, display.woke), (Mode.SLEEP, False, False))
        button.feed(press(START + 1, START + 1.2))  # první krátký stisk probudí OLED
        display.tick(results(START + 2), START + 2, OBSERVED)
        self.assertEqual((display.controller.mode, display.awake, display.woke), (Mode.CAROUSEL, True, True))
        button.feed(press(START + 3, START + 3.1))  # další krátký stisk otevře STATUS
        display.tick(results(START + 4), START + 4, OBSERVED)
        self.assertEqual((display.controller.mode, display.woke), (Mode.STATUS, False))
        display.tick(results(START + 34), START + 34, OBSERVED)  # 30 s bez stisku: zpět do kolotoče
        self.assertIs(display.controller.mode, Mode.CAROUSEL)
        display.tick(results(START + 334), START + 334, OBSERVED)  # nové okno 300 s uplynulo
        self.assertEqual((display.controller.mode, display.awake), (Mode.SLEEP, False))
        button.feed(press(START + 400, START + 404))  # podržení OLED neprobudí
        display.tick(results(START + 405), START + 405, OBSERVED)
        self.assertIs(display.controller.mode, Mode.SLEEP)


class ButtonConfigTest(unittest.TestCase):
    def test_default_has_no_device(self):
        self.assertIsNone(ButtonConfig().device)

    def test_only_stable_udev_paths(self):
        self.assertEqual(ButtonConfig(DEVICE).device, DEVICE)
        for device in ("/dev/input/event5", "/dev/input/by-path/..", "/dev/input/by-path/a/../b", "/dev/sda", 5):
            with self.subTest(device=device):
                with self.assertRaises(ConfigError):
                    ButtonConfig(device)

    def test_button_needs_oled(self):
        with self.assertRaises(ConfigError):
            Config(button=ButtonConfig(DEVICE))
        Config(oled=OledConfig(device="/dev/i2c-1"), button=ButtonConfig(DEVICE))


if __name__ == "__main__":
    unittest.main()
