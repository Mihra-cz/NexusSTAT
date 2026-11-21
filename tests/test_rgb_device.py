"""Testy výstupu na skutečné RGB LED (M7-S10) bez hardwaru: kódování WS2812, spidev nad
podvrženými voláními, přehrávání vzorů ze stávajícího rgb.py v simulovaném čase, chyby SPI,
vlákno a souběh s OLED ve stejném taktu měřicí smyčky."""

import errno
import io
import os
import struct
import threading
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from unittest import mock

from nexusstat import rgb, rgb_device
from nexusstat.alarms import AlarmEvaluator
from nexusstat.config import AlarmsConfig, CollectorConfig, ConfigError, OledConfig, RgbConfig
from nexusstat.core import CheckResult, Measurement, State
from nexusstat.oled_device import OledDisplay
from nexusstat.rgb import BLACK, BRIGHTNESS_DEFAULT, CODES, CRITICAL, LIGHT, Acknowledgements, Signal, frame_at
from nexusstat.rgb_device import (BIT0, BIT1, BLINK_STEP, RESET, SPI_HZ, SPI_IOC_WR_BITS_PER_WORD,
                                  SPI_IOC_WR_MAX_SPEED_HZ, SPI_IOC_WR_MODE, RgbOutput, Ws2812, encode)
from nexusstat.run import run_loop
from nexusstat.services import NAS_ID
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, MEMORY_ID

OBSERVED = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)
START = 1000.0
CONFIG = RgbConfig("/dev/spidev0.0")


def healthy(now=START, state=State.INFO):
    """Skutečně monitorovaná zdravá měření, platná 30 s."""
    return [CheckResult(check_id, state, "popis", OBSERVED, now + 30, simulated=False,
                        measurement=Measurement(value, unit))
            for check_id, value, unit in ((CPU_USAGE_ID, 3, "%"), (CPU_TEMP_ID, 35, "°C"), (MEMORY_ID, 8, "%"))]


def fault(state, now=START, check_id=NAS_ID):
    return CheckResult(check_id, state, "závada", OBSERVED, now + 30, simulated=False)


def color(signal, elapsed=0.0, brightness=BRIGHTNESS_DEFAULT):
    """Očekávaná barva přímo ze stávající logiky rgb.py, nezávisle na výstupu."""
    return frame_at(signal, elapsed, brightness).rgb()


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


class FakeSpi:
    """Náhrada os.open, ioctl, os.write a os.close pro zařízení spidev."""

    def __init__(self, short_write=False, ioctl_error=None):
        self.opened, self.ioctls, self.writes, self.closed = [], [], [], []
        self.short_write, self.ioctl_error = short_write, ioctl_error

    def device(self):
        return Ws2812("/dev/spidev0.0", open_device=self.open, ioctl=self.ioctl, write=self.write, close=self.close)

    def open(self, path, flags):
        self.opened.append((path, flags))
        return 5

    def ioctl(self, fd, request, argument):
        self.ioctls.append((request, argument))
        if self.ioctl_error:
            raise self.ioctl_error

    def write(self, fd, data):
        self.writes.append(bytes(data))
        return len(data) - 1 if self.short_write else len(data)

    def close(self, fd):
        self.closed.append(fd)


class FakeLeds:
    """LED pro RgbOutput: zaznamená barvy, umí selhat a umí zablokovat zápis."""

    def __init__(self, fail_open=0, block=None):
        self.is_open = False
        self.calls = []
        self.fail_open = fail_open
        self.block = block
        self.lit = threading.Event()  # nastaví se první nečernou barvou

    def open(self):
        if self.fail_open:
            self.fail_open -= 1
            raise PermissionError(errno.EACCES, "Permission denied")
        self.is_open = True
        self.calls.append("open")

    def show(self, rgb_color):
        if self.block is not None:
            self.block.wait(5)
        self.calls.append(rgb_color)
        if rgb_color != BLACK:
            self.lit.set()

    def close(self):
        self.is_open = False
        self.calls.append("close")

    def colors(self):
        return [call for call in self.calls if isinstance(call, tuple)]


class EncodeTest(unittest.TestCase):
    def test_grb_order_msb_first_with_reset_on_both_sides(self):
        data = encode((255, 0, 0x81), count=1)
        self.assertEqual(len(RESET), 64)  # 80 µs při 6,4 MHz
        self.assertEqual(data[:64], RESET)
        self.assertEqual(data[-64:], RESET)
        bits = data[64:-64]
        self.assertEqual(bits[:8], bytes([BIT0] * 8))  # zelená 0
        self.assertEqual(bits[8:16], bytes([BIT1] * 8))  # červená 255
        self.assertEqual(bits[16:24], bytes([BIT1] + [BIT0] * 6 + [BIT1]))  # modrá 0x81

    def test_four_leds_share_one_color(self):
        data = encode((10, 20, 30))
        self.assertEqual(len(data), 64 + 4 * 24 + 64)
        led = data[64:64 + 24]
        self.assertEqual(data[64:-64], led * 4)

    def test_blink_step_divides_every_pattern_time(self):
        # Přehrávač spí po krocích: každá změna svit/tma z rgb.py musí padnout na krok.
        for seconds in (rgb.CODE_ON, rgb.CODE_OFF, rgb.GROUP_PAUSE, rgb.CRITICAL_ON, rgb.CRITICAL_OFF):
            self.assertAlmostEqual(seconds / BLINK_STEP, round(seconds / BLINK_STEP), places=9)


class Ws2812Test(unittest.TestCase):
    def test_open_sets_mode_bits_and_speed_without_sending(self):
        bus = FakeSpi()
        device = bus.device()
        device.open()
        [(path, flags)] = bus.opened
        self.assertEqual((path, flags & os.O_ACCMODE), ("/dev/spidev0.0", os.O_RDWR))
        self.assertEqual(bus.ioctls, [(SPI_IOC_WR_MODE, struct.pack("B", 0)),
                                      (SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8)),
                                      (SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", SPI_HZ))])
        self.assertEqual(bus.writes, [])

    def test_show_writes_one_transfer_and_errors(self):
        bus = FakeSpi()
        device = bus.device()
        with self.assertRaises(OSError):
            device.show(BLACK)  # neotevřené
        device.open()
        device.show((1, 2, 3))
        self.assertEqual(bus.writes, [encode((1, 2, 3))])
        device.close()
        device.close()
        self.assertEqual(bus.closed, [5])
        short = FakeSpi(short_write=True)
        device = short.device()
        device.open()
        with self.assertRaises(OSError) as raised:
            device.show(BLACK)
        self.assertEqual(raised.exception.errno, errno.EIO)

    def test_ioctl_error_closes_device(self):
        bus = FakeSpi(ioctl_error=OSError(errno.EINVAL, "Invalid argument"))
        device = bus.device()
        with self.assertRaises(OSError):
            device.open()
        self.assertEqual(bus.closed, [5])
        self.assertFalse(device.is_open)


class RgbOutputTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.device = FakeLeds()
        self.lines = []

    def output(self, acks=None, **kwargs):
        return RgbOutput(CONFIG, AlarmsConfig(), acks, device=self.device, output=self.lines.append,
                         clock=self.clock, start=False, **kwargs)

    def step(self, leds, results, awake, woke=False, brightness=BRIGHTNESS_DEFAULT):
        leds.tick(results, self.clock.now, awake, woke, brightness)
        leds.write_once()
        return self.device.colors()[-1]

    def test_starts_dark_and_healthy_sleep_stays_dark(self):
        leds = self.output()
        self.assertEqual(self.step(leds, healthy(), awake=False), BLACK)
        self.assertEqual(self.device.calls, ["open", BLACK])
        self.assertFalse(leds.write_once())  # stejná barva se znovu nezapisuje
        leds.tick(healthy(), START + 1, False, False, BRIGHTNESS_DEFAULT)
        self.assertEqual(self.lines, ["RGB: LED inicializované."])

    def test_awake_healthy_is_dim_white_and_sleep_turns_it_off(self):
        leds = self.output()
        self.assertEqual(self.step(leds, healthy(), awake=True, woke=True), color(Signal(LIGHT)))
        self.assertEqual(color(Signal(LIGHT)), (26, 26, 26))  # 20 % jasu, bílá na polovině
        self.clock.now += 1
        self.assertFalse(leds.write_once())
        self.assertEqual(self.step(leds, healthy(self.clock.now), awake=False), BLACK)
        self.assertEqual(self.device.colors(), [color(Signal(LIGHT)), BLACK])

    def test_brightness_comes_from_the_owner(self):
        leds = self.output()
        self.assertEqual(self.step(leds, healthy(), awake=True, brightness=100), (128, 128, 128))

    def test_unknown_is_never_white(self):
        leds = self.output()
        results = healthy() + [CheckResult("system.cpu_usage2", State.UNKNOWN, "?", OBSERVED, START + 30,
                                           simulated=False)]
        self.assertEqual(self.step(leds, results, awake=True), BLACK)
        expired = healthy(START - 100)  # prošlá data jsou UNKNOWN
        self.assertEqual(self.step(leds, expired, awake=True), BLACK)

    def test_warning_blinks_orange_code_with_pause(self):
        leds = self.output()
        results = healthy() + [fault(State.WARNING)]  # NAS = kód 2
        leds.tick(results, START, False, False, BRIGHTNESS_DEFAULT)
        signal = leds.leds.signal
        self.assertEqual(signal, Signal(CODES, ((State.WARNING, 2),)))
        seen = []
        for elapsed in (0, 0.4, 0.8, 1.2, 1.6, 3.5, 3.6):
            self.clock.now = START + elapsed
            leds.write_once()
            seen.append(self.device.colors()[-1])
        lit = color(signal)
        self.assertEqual(lit, (51, 22, 0))  # oranžová na 20 %
        self.assertEqual(seen, [lit, BLACK, lit, BLACK, BLACK, BLACK, lit])

    def test_error_is_red_and_critical_blinks_fast(self):
        leds = self.output()
        leds.tick(healthy() + [fault(State.ERROR)], START, True, True, BRIGHTNESS_DEFAULT)
        leds.write_once()
        self.assertEqual(self.device.colors()[-1], (51, 0, 0))
        leds.tick(healthy() + [fault(State.CRITICAL)], START + 0.1, True, False, BRIGHTNESS_DEFAULT)
        self.assertEqual(leds.leds.signal.kind, CRITICAL)  # CRITICAL přebíjí hned, i uprostřed skupiny
        seen = []
        for elapsed in (0.1, 0.35, 0.6):
            self.clock.now = START + elapsed
            leds.write_once()
            seen.append(self.device.colors()[-1])
        self.assertEqual(seen, [(51, 0, 0), BLACK, (51, 0, 0)])

    def test_next_step_follows_blink_grid_and_waits_when_steady(self):
        leds = self.output()
        leds.tick(healthy(), START, True, True, BRIGHTNESS_DEFAULT)
        self.assertIsNone(leds.next_step())  # bílá: spí až do změny
        leds.tick(healthy() + [fault(State.CRITICAL)], START, True, False, BRIGHTNESS_DEFAULT)
        self.clock.now = START + 0.12
        self.assertAlmostEqual(leds.next_step(), 0.03)
        self.clock.now = START + 0.25  # přesně na hranici: další krok, ne nula
        self.assertAlmostEqual(leds.next_step(), BLINK_STEP)

    def test_acknowledged_fault_replays_once_after_wake_then_dark(self):
        acks = Acknowledgements(AlarmsConfig())
        results = healthy() + [fault(State.WARNING)]
        acks.acknowledge(results, START)
        leds = self.output(acks)
        self.assertEqual(self.step(leds, results, awake=False), BLACK)  # potvrzené nebliká samo
        leds.tick(results, START + 1, True, True, BRIGHTNESS_DEFAULT)  # probouzecí stisk
        self.assertEqual(leds.leds.signal.kind, rgb.REPLAY)
        self.clock.now = START + 1
        leds.write_once()
        self.assertEqual(self.device.colors()[-1], (51, 22, 0))
        later = START + 1 + rgb.pass_length(((State.WARNING, 2),))
        leds.tick(healthy(later) + [fault(State.WARNING, later)], later, True, False, BRIGHTNESS_DEFAULT)
        self.clock.now = later
        leds.write_once()
        self.assertEqual(leds.leds.signal.kind, rgb.OFF)  # po jednom průchodu zhasnuto, ne bílá
        self.assertEqual(self.device.colors()[-1], BLACK)

    def test_spi_error_reports_once_and_retries_later(self):
        self.device.fail_open = 3
        leds = self.output()
        leds.tick(healthy(), START, True, True, BRIGHTNESS_DEFAULT)
        self.assertFalse(leds.write_once())
        self.assertFalse(leds.write_once())  # před uplynutím RETRY_SECONDS žádný další pokus
        for second in range(1, 4):
            leds.tick(healthy(START + second), START + second, True, False, BRIGHTNESS_DEFAULT)
        failures = [line for line in self.lines if "chyba SPI" in line]
        self.assertEqual(len(failures), 1)
        self.assertIn("Permission denied", failures[0])
        self.assertAlmostEqual(leds.next_step(), 60)
        self.clock.now += 60
        self.device.fail_open = 0
        self.assertTrue(leds.write_once())
        leds.tick(healthy(START + 4), START + 4, True, False, BRIGHTNESS_DEFAULT)
        self.assertEqual(self.lines[-1], "RGB: LED znovu odpovídají.")

    def test_decision_error_turns_leds_off_and_keeps_running(self):
        leds = self.output()
        self.step(leds, healthy(), awake=True)
        for second in range(3):
            leds.tick([object()], START + second, True, False, BRIGHTNESS_DEFAULT)  # nečitelný výsledek
        self.assertEqual(len([line for line in self.lines if "signál nejde určit" in line]), 1)
        leds.write_once()
        self.assertEqual(self.device.colors()[-1], BLACK)

    def test_close_turns_off_and_closes(self):
        leds = self.output()
        self.step(leds, healthy(), awake=True)
        leds.close()
        self.assertEqual(self.device.calls[-2:], [BLACK, "close"])


class ThreadAndLoopTest(unittest.TestCase):
    def test_thread_writes_and_close_turns_leds_off(self):
        device = FakeLeds()
        leds = RgbOutput(CONFIG, device=device, output=lambda line: None)
        leds.tick(healthy(), START, True, True, BRIGHTNESS_DEFAULT)
        self.assertTrue(device.lit.wait(5))
        leds.close()
        self.assertEqual(device.calls[-2:], [BLACK, "close"])

    def test_blocked_spi_never_blocks_the_tick(self):
        release = threading.Event()
        leds = RgbOutput(CONFIG, device=FakeLeds(block=release), output=lambda line: None)
        try:
            for second in range(20):  # vlákno visí v zápisu, takt se vrací hned
                leds.tick(healthy(START + second), START + second, second % 2 == 0, False, BRIGHTNESS_DEFAULT)
        finally:
            release.set()
            leds.close()

    def test_loop_drives_oled_and_leds_from_one_state_without_extra_measurement(self):
        class Collector:
            labels = {}
            simulated = False
            calls = 0

            def collect(self, now, observed_utc):
                Collector.calls += 1
                return healthy(now)

        class LoopClock:
            def __init__(self):
                self.now = START

            def __call__(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds
                if self.now == START + 2:
                    display.wake()  # krátký stisk tlačítka mezi takty
                if self.now >= START + 6:
                    raise KeyboardInterrupt

        class OffDevice:
            is_open = False

            def open(self):
                self.is_open = True

            def show(self, frame):
                pass

            def off(self):
                pass

            def close(self):
                self.is_open = False

        class Image:
            mode, size = "1", (128, 64)

            def tobytes(self):
                return bytes(1024)

        clock = LoopClock()
        display = OledDisplay(OledConfig(device="/dev/i2c-1"), (), 30, None, device=OffDevice(),
                              render=lambda *args: Image(), output=lambda line: None, clock=clock, start=False)
        device = FakeLeds()
        leds = RgbOutput(CONFIG, device=device, output=lambda line: None, clock=clock, start=False)
        seen = []
        original = leds.tick

        def tick(results, now, awake, woke, brightness):
            seen.append((now - START, awake, woke, display.controller.mode.value, [r.id for r in results]))
            original(results, now, awake, woke, brightness)
            leds.write_once()

        leds.tick = tick
        results = run_loop(Collector(), CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=clock,
                           utc_clock=lambda: OBSERVED, sleep=clock.sleep, output=lambda line: None, tick_seconds=1,
                           display=display, leds=leds)
        self.assertEqual([(t, awake, woke, mode) for t, awake, woke, mode, _ in seen],
                         [(0, False, False, "SLEEP"), (1, False, False, "SLEEP"), (2, True, True, "CAROUSEL"),
                          (3, True, False, "CAROUSEL"), (4, True, False, "CAROUSEL"), (5, True, False, "CAROUSEL")])
        self.assertEqual(seen[-1][4], [r.id for r in results])  # tytéž výsledky jako smyčka
        self.assertEqual(Collector.calls, 6)  # LED ani OLED nic neměří: jedno měření na takt
        # První takty UNKNOWN nejsou (měření jsou platná od začátku), po stisku bílá, předtím tma.
        self.assertEqual(device.colors(), [BLACK, color(Signal(LIGHT))])


class HardwareTestCommandTest(unittest.TestCase):
    def test_synthetic_patterns_then_dark(self):
        device = FakeLeds()
        lines = []
        rgb_device.hardware_test("/dev/spidev0.0", 3, device=device, sleep=lambda seconds: None,
                                 output=lines.append)
        self.assertEqual(lines, [f"RGB test: {name}" for name, *_ in rgb_device.test_phases(3)]
                         + ["RGB test: hotovo, LED zhasnuté."])
        colors = device.colors()
        self.assertEqual(device.calls[0], "open")
        self.assertEqual(colors[0], BLACK)
        self.assertEqual(device.calls[-2:], [BLACK, "close"])
        for expected in (color(Signal(LIGHT)), (51, 22, 0), (51, 0, 0)):
            self.assertIn(expected, colors)

    def test_play_writes_only_changes(self):
        device = FakeLeds()
        rgb_device.play(device, Signal(CRITICAL), 1, BRIGHTNESS_DEFAULT, sleep=lambda seconds: None)
        self.assertEqual(device.colors(), [(51, 0, 0), BLACK, (51, 0, 0), BLACK])  # 0,25 s svit / 0,25 s tma

    def test_invalid_arguments_stop_before_hardware(self):
        for argv in (["--device", "/dev/i2c-1"], ["--brightness", "0"], ["--seconds", "0"]):
            with self.subTest(argv=argv):
                with mock.patch.object(rgb_device, "hardware_test") as test, redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        rgb_device.main(argv)
                self.assertEqual(raised.exception.code, 2)
                test.assert_not_called()

    def test_spi_error_exits_with_message(self):
        stderr = io.StringIO()
        with mock.patch.object(rgb_device, "hardware_test", side_effect=PermissionError(13, "Permission denied")), \
                redirect_stderr(stderr):
            self.assertEqual(rgb_device.main([]), 1)
        self.assertIn("Permission denied", stderr.getvalue())


class RgbConfigTest(unittest.TestCase):
    def test_default_has_no_device_and_validation(self):
        self.assertIsNone(RgbConfig().device)
        self.assertEqual(RgbConfig("/dev/spidev0.0").device, "/dev/spidev0.0")
        for device in ("/dev/spidev0", "/dev/i2c-1", "/dev/spidev0.0/../x", 1):
            with self.subTest(device=device):
                with self.assertRaises(ConfigError):
                    RgbConfig(device)


if __name__ == "__main__":
    unittest.main()
