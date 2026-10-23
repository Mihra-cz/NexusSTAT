"""Testy živého simulátoru OLED bez grafického displeje.

Čas je virtuální (falešné skutečné hodiny), nic se nečeká. Stavová logika
nepotřebuje tkinter ani Pillow; testy obrazu se bez Pillow přeskočí a okno
se ověřuje s falešným modulem tkinter, takže X server není potřeba.
"""

import base64
import contextlib
import inspect
import io
import signal
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

from nexusstat.config import AlarmRule, AlarmsConfig, Config, NetworkConfig
from nexusstat.core import CheckResult, Measurement, State, active_problems
from nexusstat.fan_monitor import LABELS as FAN_LABELS, FanMonitor
from nexusstat.network_monitor import LABELS as NETWORK_LABELS, NetworkMonitor
from nexusstat.oled import UNKNOWN_SYMBOL, Mode, home_symbol
from nexusstat.oled_text import network_rows, page_rows, service_rows
from nexusstat.preview import LIVE, SCENARIOS, SPEEDS, PreviewSession, SessionClock, main
from nexusstat.run import Collectors, live_monitor
from nexusstat.services import BACKUP_ID, NAS_ID, NEXUS_SERVICES_ID, SERVICE_LABELS
from nexusstat.system_monitor import CPU_TEMP_ID, LABELS as SYSTEM_LABELS, SystemMonitor

try:
    from PIL import Image

    from nexusstat import preview, render
except ImportError:  # Pillow není nainstalované
    Image = None

START_UTC = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
WALL = datetime(2026, 10, 19, 14, 0)


class FakeRealClock:
    """Skutečné monotónní hodiny pod kontrolou testu."""

    def __init__(self, start=5000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeMonitor:
    """Náhrada skutečných kolektorů pro LIVE: skutečná (ne simulovaná) data bez čtení /proc."""

    simulated = False
    labels = SYSTEM_LABELS
    temperature = 50.0
    fail = False

    def __init__(self, config):
        self.validity_seconds = config.collector.validity_seconds
        self.calls = []

    def collect(self, now, observed_utc):
        self.calls.append((now, observed_utc))
        if FakeMonitor.fail:
            raise RuntimeError("simulovaný výpadek")
        results = [CheckResult(check_id, State.INFO, f"{label} 50 %", observed_utc, now + self.validity_seconds,
                               simulated=False) for check_id, label in SYSTEM_LABELS.items()]
        results[1] = CheckResult(
            CPU_TEMP_ID, State.INFO, f"Teplota CPU {FakeMonitor.temperature:.0f} °C", observed_utc,
            now + self.validity_seconds, simulated=False, measurement=Measurement(FakeMonitor.temperature, "°C"),
        )
        return results


def make_session(scenario="ok", config=None):
    real = FakeRealClock()
    session = PreviewSession(
        config, scenario=scenario, clock=SessionClock(real_clock=real),
        utc_clock=lambda: START_UTC, wall_clock=lambda: WALL, monitor_factory=FakeMonitor,
    )
    return session, real


def run_for(session, real, real_seconds, step=0.5):
    """Posune skutečný čas po krocích a obnoví data jako okno (bez vykreslení)."""
    for _ in range(round(real_seconds / step)):
        real.advance(step)
        session._refresh_if_due(session.clock.now())


def mode_page(session):
    """(režim, stránka) po uplatnění časových limitů, bez vykreslení."""
    now = session.clock.now()
    session.controller.update(now)
    return session.controller.mode, session.controller.current_page(now)


class SessionClockTest(unittest.TestCase):
    def test_speed_scales_time_without_jumps(self):
        real = FakeRealClock()
        clock = SessionClock(real_clock=real)
        real.advance(10)
        self.assertEqual(clock.now(), 10)
        clock.set_speed(10)
        self.assertEqual(clock.now(), 10)  # změna rychlosti čas neposune
        real.advance(1)
        self.assertEqual(clock.now(), 20)
        clock.set_speed(5)
        real.advance(2)
        self.assertEqual(clock.now(), 30)

    def test_invalid_speed(self):
        clock = SessionClock(real_clock=FakeRealClock())
        for speed in (0, 2, 100, True, "10", None):
            with self.subTest(speed=speed):
                with self.assertRaises(ValueError):
                    clock.set_speed(speed)
        self.assertEqual(clock.speed, 1)


class WakeSleepTest(unittest.TestCase):
    def test_starts_asleep_and_wakes_on_nexus(self):
        session, _ = make_session()
        self.assertEqual(mode_page(session), (Mode.SLEEP, None))
        self.assertTrue(session.describe().startswith("SLEEP"))
        session.press()
        self.assertEqual(mode_page(session), (Mode.CAROUSEL, "NEXUS"))

    def test_sleeps_after_300_seconds(self):
        session, real = make_session()
        session.press()
        real.advance(299.9)
        self.assertIs(mode_page(session)[0], Mode.CAROUSEL)
        real.advance(0.1)
        self.assertEqual(mode_page(session), (Mode.SLEEP, None))

    def test_speedup_shortens_real_wait(self):
        session, real = make_session()
        session.set_speed(10)
        session.press()
        real.advance(29.9)  # 299 s simulovaného času
        self.assertIs(mode_page(session)[0], Mode.CAROUSEL)
        real.advance(0.1)
        self.assertEqual(mode_page(session), (Mode.SLEEP, None))


class CarouselTest(unittest.TestCase):
    def test_five_pages_every_10_seconds(self):
        session, real = make_session()
        session.press()
        pages = []
        for _ in range(6):
            pages.append(mode_page(session)[1])
            real.advance(10)
        self.assertEqual(pages, ["NEXUS", "SYSTEM", "DISK", "NETWORK", "SERVICES", "NEXUS"])

    def test_pages_at_5x_speed(self):
        session, real = make_session()
        session.set_speed(5)
        session.press()
        pages = []
        for _ in range(6):
            pages.append(mode_page(session)[1])
            real.advance(2)  # 2 s skutečného času = 10 s simulovaného
        self.assertEqual(pages, ["NEXUS", "SYSTEM", "DISK", "NETWORK", "SERVICES", "NEXUS"])

    def test_describe_names_network_and_services_pages(self):
        session, real = make_session()
        session.press()
        real.advance(30)
        session.controller.update(session.clock.now())
        self.assertTrue(session.describe().startswith("CAROUSEL – NETWORK"))  # stejně jako nadpis na displeji
        real.advance(10)
        session.controller.update(session.clock.now())
        self.assertTrue(session.describe().startswith("CAROUSEL – SERVICES"))

    def test_window_and_status_timeouts_unchanged_with_five_pages(self):
        session, real = make_session("net_invalid")
        session.press()
        real.advance(35)  # NETWORK
        session.press()  # STATUS (NET RX NO DATA)
        self.assertIs(mode_page(session)[0], Mode.STATUS)
        real.advance(30)
        self.assertEqual(mode_page(session), (Mode.CAROUSEL, "NEXUS"))
        real.advance(299.9)
        self.assertIs(mode_page(session)[0], Mode.CAROUSEL)
        real.advance(0.1)
        self.assertEqual(mode_page(session), (Mode.SLEEP, None))


class StatusTest(unittest.TestCase):
    def test_status_paging_and_return(self):
        session, real = make_session("many")
        self.assertEqual(len(active_problems(session.results, session.clock.now())), 6)
        session.press()  # probudí
        session.press()  # STATUS 1/2
        self.assertEqual((session.controller.mode, session.controller.status_page), (Mode.STATUS, 0))
        self.assertTrue(session.describe().startswith("STATUS – stránka 1/2"))
        session.press()
        self.assertTrue(session.describe().startswith("STATUS – stránka 2/2"))
        session.press()  # z poslední stránky zpět
        self.assertEqual(mode_page(session), (Mode.CAROUSEL, "NEXUS"))

    def test_status_timeout_returns_to_carousel(self):
        session, real = make_session("many")
        session.press()
        session.press()
        real.advance(29.9)
        self.assertIs(mode_page(session)[0], Mode.STATUS)
        real.advance(0.1)
        self.assertEqual(mode_page(session), (Mode.CAROUSEL, "NEXUS"))

    def test_status_timeout_at_10x(self):
        session, real = make_session("many")
        session.set_speed(10)
        session.press()
        session.press()
        real.advance(3)  # 30 s simulovaného času
        self.assertEqual(mode_page(session), (Mode.CAROUSEL, "NEXUS"))

    def test_status_without_problems_returns_on_press(self):
        session, _ = make_session("ok")
        session.press()
        session.press()
        self.assertTrue(session.describe().startswith("STATUS – stránka 1/1"))
        session.press()
        self.assertEqual(mode_page(session), (Mode.CAROUSEL, "NEXUS"))


class ScenarioTest(unittest.TestCase):
    def test_scenarios_and_live(self):
        self.assertEqual(list(SCENARIOS), ["ok", "warning", "error", "unknown", "many", "unconnected", "net_down",
                                           "net_no_ipv4", "net_low", "net_high", "net_invalid", LIVE])

    def test_fictitious_values_are_simulated(self):
        for name in SCENARIOS:
            if name == LIVE:
                continue
            session, _ = make_session(name)
            with self.subTest(scenario=name):
                self.assertTrue(any(result.simulated for result in session.results))  # displej ukáže SIM
                for result in session.results:
                    if not result.simulated:
                        # Jediné nesimulované jsou doplněné služby bez dat: tvrdí jen, že data nejsou.
                        self.assertIn(result.id, SERVICE_LABELS)
                        self.assertIn(result.state, (State.UNKNOWN, State.NA))

    def test_scenario_states(self):
        expected = {
            "ok": set(),
            "warning": {State.WARNING},
            "error": {State.ERROR, State.CRITICAL},
            "unknown": {State.UNKNOWN},
            "many": {State.CRITICAL, State.ERROR, State.WARNING, State.UNKNOWN},
            "unconnected": {State.UNKNOWN},
            # DOWN, chybějící IPv4 i provoz jsou platná měření (INFO), ne závady.
            "net_down": set(),
            "net_no_ipv4": set(),
            "net_low": set(),
            "net_high": set(),
            "net_invalid": {State.UNKNOWN},
        }
        for name, states in expected.items():
            session, _ = make_session(name)
            with self.subTest(scenario=name):
                self.assertEqual({p.state for p in active_problems(session.results, session.clock.now())}, states)

    def test_unconnected_services_match_reality(self):
        session, _ = make_session("unconnected")
        now = session.clock.now()
        self.assertEqual(service_rows(session.results, now),
                         [("NEXUS", "UNKN"), ("ANIMEDB", "N/A"), ("NAS", "UNKN"), ("BACKUP", "UNKN")])
        self.assertEqual(home_symbol(session.results, now), UNKNOWN_SYMBOL)
        self.assertEqual({p.id for p in active_problems(session.results, now)}, {NEXUS_SERVICES_ID, NAS_ID, BACKUP_ID})

    def test_switch_resets_data_and_window(self):
        session, real = make_session("many")
        session.press()
        real.advance(100)
        session.set_scenario("ok")
        now = session.clock.now()
        self.assertEqual(session.started_at, now)
        self.assertEqual(active_problems(session.results, now), [])  # žádné alarmy z minulého scénáře
        self.assertEqual({r.observed_utc for r in session.results}, {session.utc_at(now)})
        self.assertEqual({r.expires_monotonic for r in session.results}, {now + 30})
        # Rozsvícený displej zůstal rozsvícený, ale od NEXUS s novým 300s oknem.
        self.assertEqual(mode_page(session), (Mode.CAROUSEL, "NEXUS"))
        real.advance(299.9)
        self.assertIs(mode_page(session)[0], Mode.CAROUSEL)
        real.advance(0.1)
        self.assertIs(mode_page(session)[0], Mode.SLEEP)

    def test_switch_while_asleep_stays_asleep(self):
        session, _ = make_session("ok")
        session.set_scenario("many")
        self.assertIs(mode_page(session)[0], Mode.SLEEP)

    def test_switch_from_status_starts_on_nexus(self):
        session, _ = make_session("many")
        session.press()
        session.press()
        session.set_scenario("warning")
        self.assertEqual(mode_page(session), (Mode.CAROUSEL, "NEXUS"))

    def test_unknown_scenario_name(self):
        session, _ = make_session()
        with self.assertRaises(ValueError):
            session.set_scenario("neexistuje")

    def test_stale_data_expire_in_simulated_time(self):
        session, real = make_session("unknown")
        session.set_speed(10)
        self.assertEqual(page_rows("DISK", session.results, session.clock.now()), [("ROOT", "42%")])
        self.assertEqual(page_rows("SYSTEM", session.results, session.clock.now())[2], ("RAM", "UNKN"))  # chybí úplně
        run_for(session, real, 1.9, step=0.1)  # 19 s simulovaného času, obnova po 10 s
        self.assertEqual(page_rows("DISK", session.results, session.clock.now()), [("ROOT", "42%")])
        run_for(session, real, 0.1, step=0.1)  # 20 s: disk naposledy změřen při startu scénáře
        now = session.clock.now()
        self.assertEqual(page_rows("DISK", session.results, now), [("ROOT", "UNKN")])
        problems = {p.id: p for p in active_problems(session.results, now)}
        self.assertIn("zastaralá data", problems["disk.root"].summary)
        self.assertNotIn(BACKUP_ID, problems)
        run_for(session, real, 2, step=0.1)  # 40 s: prošla i záloha, obnovované hodnoty platí dál
        now = session.clock.now()
        problems = {p.id: p for p in active_problems(session.results, now)}
        self.assertIs(problems[BACKUP_ID].state, State.UNKNOWN)
        self.assertEqual(page_rows("SYSTEM", session.results, now)[1], ("CPU TEMP", "47°C"))

    def test_regular_data_do_not_expire_during_long_run(self):
        session, real = make_session("ok")
        session.set_speed(10)
        run_for(session, real, 360, step=1)  # hodina simulovaného času, obnova po 10 s
        self.assertEqual(active_problems(session.results, session.clock.now()), [])


class LiveTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, FakeMonitor, "fail", False)
        self.addCleanup(setattr, FakeMonitor, "temperature", 50.0)

    def test_live_forces_real_time_and_labels_notebook(self):
        session, _ = make_session("ok")
        session.set_speed(10)
        session.set_scenario(LIVE)
        self.assertEqual(session.clock.speed, 1)
        with self.assertRaises(ValueError):
            session.set_speed(5)
        self.assertIn("NOTEBOOKU, ne Nexusu", session.source_text())
        self.assertIn("SIMULACE", make_session("ok")[0].source_text())

    def test_live_uses_collector_interval_and_session_time(self):
        session, real = make_session(LIVE)
        monitor = session.monitor
        run_for(session, real, 25, step=1)
        self.assertEqual([now for now, _ in monitor.calls], [0, 10, 20])  # interval kolektoru 10 s
        self.assertEqual({r.expires_monotonic for r in session.results}, {20 + 30})
        self.assertEqual({utc for _, utc in monitor.calls}, {START_UTC})  # skutečný UTC čas měření

    def test_live_results_are_real_with_unconnected_services(self):
        session, _ = make_session(LIVE)
        now = session.clock.now()
        self.assertFalse(any(result.simulated for result in session.results))
        self.assertEqual(service_rows(session.results, now),
                         [("NEXUS", "UNKN"), ("ANIMEDB", "N/A"), ("NAS", "UNKN"), ("BACKUP", "UNKN")])

    def test_live_applies_alarm_rules(self):
        config = Config(alarms=AlarmsConfig(cpu_temp=AlarmRule(True, 70, 80, 5)))
        FakeMonitor.temperature = 75.0
        session, real = make_session(LIVE, config=config)
        states = []
        for _ in range(3):
            states.append({r.id: r.state for r in session.results}[CPU_TEMP_ID])
            real.advance(10)
            session._refresh_if_due(session.clock.now())
        states.append({r.id: r.state for r in session.results}[CPU_TEMP_ID])
        self.assertEqual(states, [State.INFO, State.INFO, State.WARNING, State.WARNING])

    def test_live_collector_failure_is_unknown(self):
        session, real = make_session(LIVE)
        FakeMonitor.fail = True
        real.advance(10)
        session._refresh_if_due(session.clock.now())
        system = [r for r in session.results if r.id in SYSTEM_LABELS]
        self.assertEqual({r.state for r in system}, {State.UNKNOWN})
        self.assertIn("měření selhalo", system[0].summary)

    def test_live_uses_collectors_from_config(self):
        # Výchozí továrna: skutečné kolektory systému a sítě s rozhraním z [network].
        # Jen se vytvoří, nic se nečte: měření je v testu náhradní kolektor.
        created = []

        def factory(config):
            created.append(config)
            return FakeMonitor(config)

        config = Config(network=NetworkConfig("end0"))
        session = PreviewSession(config, scenario=LIVE, clock=SessionClock(real_clock=FakeRealClock()),
                                 utc_clock=lambda: START_UTC, wall_clock=lambda: WALL, monitor_factory=factory)
        self.assertEqual(created, [config])
        self.assertIs(session.monitor_factory, factory)
        self.assertIs(inspect.signature(PreviewSession).parameters["monitor_factory"].default, live_monitor)
        monitor = live_monitor(config)
        self.assertIsInstance(monitor, Collectors)
        self.assertEqual([type(m) for m in monitor.monitors], [SystemMonitor, FanMonitor, NetworkMonitor])
        self.assertEqual(monitor.monitors[2].interface, "end0")
        self.assertEqual(list(monitor.labels),
                         list(SYSTEM_LABELS) + list(FAN_LABELS) + list(NETWORK_LABELS))

    def test_live_without_network_results_shows_unkn(self):
        # Náhradní kolektor bez sítě: stránka NETWORK nic nedomýšlí.
        session, _ = make_session(LIVE)
        self.assertEqual(network_rows(session.results, session.clock.now()),
                         [("LINK", "UNKN"), ("RX", "UNKN"), ("TX", "UNKN"), ("IPV4", "UNKN")])

    def test_back_to_simulation(self):
        session, _ = make_session(LIVE)
        session.set_scenario("ok")
        self.assertIsNone(session.monitor)
        session.set_speed(10)
        self.assertEqual(session.clock.speed, 10)


@unittest.skipIf(Image is None, "Pillow není dostupné")
class FrameTest(unittest.TestCase):
    def test_frame_comes_from_existing_renderer(self):
        session, real = make_session("many")
        session.press()
        real.advance(12)
        with mock.patch.object(render, "render_screen", wraps=render.render_screen) as spy:
            frame = session.tick()
        controller, now, results, wall_time = spy.call_args.args
        self.assertIs(controller, session.controller)
        self.assertEqual(now, 12)
        self.assertIs(results, session.results)
        self.assertEqual(wall_time, datetime(2026, 10, 19, 14, 0, 12))  # simulované hodiny na displeji
        self.assertEqual((frame.size, frame.mode), ((128, 64), "1"))
        self.assertEqual(frame.tobytes(), render.render_screen(controller, now, results, wall_time).tobytes())

    def test_sleep_is_black(self):
        session, _ = make_session("error")
        self.assertIsNone(session.tick().getbbox())

    def test_sim_mark_only_for_fictitious_data(self):
        def nexus_frame(scenario):
            session, _ = make_session(scenario)
            session.press()
            return session.tick()

        for name in SCENARIOS:
            with self.subTest(scenario=name):
                corner = nexus_frame(name).crop((100, 50, 128, 64)).getbbox()  # značka SIM u data
                if name == LIVE:
                    self.assertIsNone(corner)
                else:
                    self.assertIsNotNone(corner)

    def test_scale_is_integer_nearest(self):
        session, _ = make_session("many")
        session.press()
        frame = session.tick()
        for scale in (4, 6):
            scaled = preview.scale_frame(frame, scale)
            with self.subTest(scale=scale):
                self.assertEqual(scaled.size, (128 * scale, 64 * scale))
                self.assertEqual(scaled.mode, "1")
                # Každý pixel OLED je ostrý blok scale × scale se stejnou hodnotou.
                for x, y in ((0, 0), (5, 3), (64, 20), (127, 63), (40, 50)):
                    block = scaled.crop((x * scale, y * scale, (x + 1) * scale, (y + 1) * scale))
                    self.assertEqual(set(block.getdata()), {frame.getpixel((x, y))})
        for scale in (0, -2, 2.5, True):
            with self.subTest(scale=scale):
                with self.assertRaises(ValueError):
                    preview.scale_frame(frame, scale)

    def test_scale_never_smooths_even_grayscale(self):
        # Pillow zvětšuje režim "1" vždy bez vyhlazení; smlouva scale_frame platí i pro šedé obrázky.
        checker = Image.new("L", (4, 2))
        checker.putdata([0, 255, 0, 255, 255, 0, 255, 0])
        scaled = preview.scale_frame(checker, 6)
        self.assertEqual(set(scaled.getdata()), {0, 255})
        self.assertEqual(set(scaled.crop((6, 0, 12, 6)).getdata()), {255})

    def test_png_data_for_tkinter(self):
        session, _ = make_session("ok")
        session.press()
        scaled = preview.scale_frame(session.tick(), 4)
        decoded = Image.open(io.BytesIO(base64.b64decode(preview.png_data(scaled))))
        self.assertEqual(decoded.size, (512, 256))
        self.assertLessEqual(set(decoded.getdata()), {0, 255})  # jen černá a bílá, žádné vyhlazení


# --- Falešný tkinter: ověří zapojení okna bez X serveru a bez nainstalovaného tkinter ---

def fake_tkinter(script):
    module = types.ModuleType("tkinter")
    created = {"widgets": [], "roots": []}

    class Widget:
        def __init__(self, master=None, *args, **options):
            self.args, self.options = args, dict(options)
            created["widgets"].append(self)

        def pack(self, **options):
            pass

        def configure(self, **options):
            self.options.update(options)

    class Var:
        def __init__(self, value=None):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class Tk(Widget):
        def __init__(self):
            super().__init__()
            self.bindings, self.protocols, self.scheduled = {}, {}, {}
            self.next_id, self.destroyed = 0, False
            created["roots"].append(self)

        def title(self, text):
            self.options["title"] = text

        def bind(self, sequence, handler):
            self.bindings[sequence] = handler

        def protocol(self, name, handler):
            self.protocols[name] = handler

        def after(self, ms, callback):
            self.next_id += 1
            self.scheduled[self.next_id] = (ms, callback)
            return self.next_id

        def after_cancel(self, after_id):
            del self.scheduled[after_id]

        def focus_set(self):
            pass

        def destroy(self):
            self.destroyed = True

        def mainloop(self):
            script(self, created)

    class PhotoImage:
        def __init__(self, data):
            self.data = data

    module.Tk, module.Label, module.Frame, module.Button = Tk, Widget, Widget, Widget
    module.OptionMenu, module.Radiobutton, module.PhotoImage = Widget, Widget, PhotoImage
    module.StringVar = module.IntVar = Var
    return module, created


@unittest.skipIf(Image is None, "Pillow není dostupné")
class WindowWiringTest(unittest.TestCase):
    def run_window(self, script, scenario="ok"):
        session, real = make_session(scenario)
        module, created = fake_tkinter(lambda root, created: script(root, created, session, real))
        with mock.patch.dict(sys.modules, {"tkinter": module}):
            preview.run_window(session, 4)
        return session, created

    def run_scheduled(self, root):
        [(after_id, (ms, callback))] = root.scheduled.items()  # vždy jen jedna naplánovaná obnova
        self.assertEqual(ms, preview.TICK_MS)
        del root.scheduled[after_id]
        callback()

    def test_controls_and_clean_close(self):
        def script(root, created, session, real):
            display = created["widgets"][1]  # první Label po Tk = plocha OLED
            self.assertIs(session.controller.mode, Mode.SLEEP)
            root.bindings["<space>"](None)  # mezerník = stisk
            self.assertIs(session.controller.mode, Mode.CAROUSEL)
            root.bindings["<Return>"](None)  # Enter = další stisk → STATUS
            self.assertIs(session.controller.mode, Mode.STATUS)
            image = Image.open(io.BytesIO(base64.b64decode(display.options["image"].data)))
            self.assertEqual(image.size, (512, 256))
            # Tlačítko „Stisk“ a nabídka scénářů.
            button = next(w for w in created["widgets"] if str(w.options.get("text", "")).startswith("Stisk"))
            menu = next(w for w in created["widgets"] if "command" in w.options and len(w.args) > 2)
            menu.options["command"](SCENARIOS["many"][0])
            self.assertEqual(session.scenario, "many")
            self.assertIs(session.controller.mode, Mode.CAROUSEL)  # rozsvícený zůstal, od NEXUS
            button.options["command"]()
            self.assertIs(session.controller.mode, Mode.STATUS)
            # Rychlost 10× a pravidelná obnova přes after().
            radio_10 = next(w for w in created["widgets"] if w.options.get("value") == 10)
            radio_10.options["variable"].set(10)
            radio_10.options["command"]()
            self.assertEqual(session.clock.speed, 10)
            real.advance(3)  # 30 s simulovaného času bez stisku: návrat do kolotoče
            self.run_scheduled(root)
            self.assertIs(session.controller.mode, Mode.CAROUSEL)
            # LIVE vypne volbu rychlosti.
            menu.options["command"](SCENARIOS[LIVE][0])
            self.assertEqual({w.options["state"] for w in created["widgets"] if w.options.get("value") in SPEEDS},
                             {"disabled"})
            root.protocols["WM_DELETE_WINDOW"]()

        handler_before = signal.getsignal(signal.SIGINT)
        _, created = self.run_window(script)
        root = created["roots"][0]
        self.assertTrue(root.destroyed)
        self.assertEqual(root.scheduled, {})  # po zavření nic nezůstává naplánované
        self.assertIs(signal.getsignal(signal.SIGINT), handler_before)  # Ctrl+C obsluha vrácena

    def test_ctrl_c_closes_window(self):
        def script(root, created, session, real):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)  # jako Ctrl+C v terminálu

        _, created = self.run_window(script)
        self.assertTrue(created["roots"][0].destroyed)
        self.assertEqual(created["roots"][0].scheduled, {})

    def test_error_in_tick_is_shown_and_loop_continues(self):
        def script(root, created, session, real):
            with mock.patch.object(session, "tick", side_effect=RuntimeError("chyba kreslení")):
                self.run_scheduled(root)
            mode_label = created["widgets"][2]
            self.assertIn("chyba kreslení", mode_label.options["textvariable"].get())
            self.assertEqual(len(root.scheduled), 1)  # další obnova je naplánovaná
            root.protocols["WM_DELETE_WINDOW"]()

        self.run_window(script)


class EntryPointTest(unittest.TestCase):
    def test_import_does_not_need_tkinter(self):
        self.assertNotIn("tkinter", sys.modules)

    def run_main(self, argv, find_spec=None):
        stderr = io.StringIO()
        patches = [contextlib.redirect_stderr(stderr)]
        if find_spec is not None:
            patches.append(mock.patch("importlib.util.find_spec", find_spec))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with self.assertRaises(SystemExit) as raised:
                main(argv)
        return raised.exception.code, stderr.getvalue()

    def test_missing_tkinter_gives_clear_message(self):
        code, message = self.run_main([], find_spec=lambda name: None if name == "tkinter" else object())
        self.assertEqual(code, 2)
        self.assertIn("sudo apt install python3-tk", message)
        self.assertIn("po schválení", message)

    def test_invalid_config(self):
        code, _ = self.run_main(["--config", "/neexistujici.toml"])
        self.assertEqual(code, 2)

    def test_window_failure_gives_exit_code(self):
        with mock.patch("nexusstat.preview.run_window", side_effect=RuntimeError("no display name")):
            code, message = self.run_main([], find_spec=lambda name: object())
        self.assertEqual(code, 1)
        self.assertIn("Okno nelze otevřít", message)


if __name__ == "__main__":
    unittest.main()
