"""Testy rozhodovací logiky RGB LED (rgb.py) s virtuálním časem.

Nic se nečeká: bliknutí se ověřují vzorkováním frame_at() a RgbLeds.frame()
v libovolných okamžicích monotónního času. Bez hardwaru, tkinter i Pillow.
"""

import unittest
from datetime import datetime, timezone

from nexusstat import history_link, rgb
from nexusstat.alarms import AlarmEvaluator
from nexusstat.config import AlarmRule, AlarmsConfig
from nexusstat.core import CheckResult, Measurement, State
from nexusstat.services import ANIMEDB_ID, BACKUP_ID, NAS_ID, NEXUS_SERVICES_ID
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID

UTC = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
VALIDITY = 30.0
STEP = 0.05


def check(check_id, state, at=0.0, valid=VALIDITY, value=None):
    """Jedno měření kontroly v monotónním čase `at` (platnost od něj)."""
    return CheckResult(check_id, state, "popis", UTC, at + valid, simulated=False, measurement=value)


def healthy(at=0.0):
    return [check(CPU_USAGE_ID, State.INFO, at), check(CPU_TEMP_ID, State.INFO, at),
            check(NEXUS_SERVICES_ID, State.OK, at), check(ANIMEDB_ID, State.NA, at)]


def with_fault(check_id, state, at=0.0):
    return [result for result in healthy(at) if result.id != check_id] + [check(check_id, state, at)]


def blink_groups(frames):
    """[(barva, počet bliknutí), …] z posloupnosti snímků po STEP sekundách.

    Nová skupina začíná po tmě delší než 1 s (pauza mezi skupinami je 2 s,
    tma mezi bliknutími 0,4 s).
    """
    groups, dark_for, was_lit = [], 0.0, False
    for frame in frames:
        if frame.lit and not was_lit:
            if not groups or dark_for > 1.0:
                groups.append([frame.color, 0])
            groups[-1][1] += 1
        dark_for = 0.0 if frame.lit else dark_for + STEP
        was_lit = frame.lit
    return [tuple(group) for group in groups]


def sample(signal, duration, brightness=rgb.BRIGHTNESS_DEFAULT, start=0.0):
    count = round(duration / STEP)
    return [rgb.frame_at(signal, start + STEP / 2 + index * STEP, brightness) for index in range(count)]


def led_frames(leds, results, awake, start, duration):
    count = round(duration / STEP)
    return [leds.frame(results, awake, start + STEP / 2 + index * STEP) for index in range(count)]


class CategoryTest(unittest.TestCase):
    def test_stable_category_codes(self):
        expected = {
            CPU_TEMP_ID: 1, CPU_USAGE_ID: 1, DISK_ROOT_ID: 1, "system.cpu_fan": 1, "network.link": 1,
            NEXUS_SERVICES_ID: 1, history_link.HISTORY_STORAGE_ID: 1,
            NAS_ID: 2,
            ANIMEDB_ID: 3,
            BACKUP_ID: 4, history_link.HISTORY_SNAPSHOT_ID: 4,
            "docker.container_restarts": 5, "nexusstat.neznama_kontrola": 5,
        }
        for check_id, category in expected.items():
            with self.subTest(check_id=check_id):
                self.assertEqual(rgb.category_of(check_id), category)

    def test_history_ids_match_history_link(self):
        # rgb.py historii nenačítá a id má zapsaná textem; nesmí se rozejít.
        self.assertIn(history_link.HISTORY_STORAGE_ID, rgb.CATEGORY_BY_ID)
        self.assertIn(history_link.HISTORY_SNAPSHOT_ID, rgb.CATEGORY_BY_ID)


class NormalStateTest(unittest.TestCase):
    def test_sleeping_oled_and_all_ok_is_dark(self):
        signal = rgb.rgb_signal(healthy(), rgb.Acknowledgements(), False, False, 1.0)
        self.assertEqual(signal.kind, rgb.OFF)
        self.assertFalse(rgb.frame_at(signal, 0.1, 20).lit)

    def test_awake_oled_and_all_ok_is_dim_white(self):
        signal = rgb.rgb_signal(healthy(), rgb.Acknowledgements(), True, False, 1.0)
        self.assertEqual(signal.kind, rgb.LIGHT)
        frames = sample(signal, 10)
        self.assertTrue(all(frame.lit and frame.color == rgb.WHITE for frame in frames))  # nebliká
        self.assertAlmostEqual(frames[0].level, 0.10)  # polovina výchozích 20 %

    def test_unknown_never_blinks_and_blocks_white(self):
        results = with_fault(CPU_USAGE_ID, State.UNKNOWN)
        for awake in (False, True):
            with self.subTest(awake=awake):
                signal = rgb.rgb_signal(results, rgb.Acknowledgements(), awake, False, 1.0)
                self.assertEqual(signal.kind, rgb.OFF)
        self.assertIn("UNKNOWN", rgb.rgb_signal(results, rgb.Acknowledgements(), True, False, 1.0).reason)

    def test_expired_result_is_not_ok(self):
        # Prošlé OK je UNKNOWN (effective), takže bílá zhasne a nic nebliká.
        results = healthy(at=0.0)
        self.assertEqual(rgb.rgb_signal(results, rgb.Acknowledgements(), True, False, VALIDITY - 1).kind, rgb.LIGHT)
        self.assertEqual(rgb.rgb_signal(results, rgb.Acknowledgements(), True, False, VALIDITY).kind, rgb.OFF)

    def test_expired_fault_stops_blinking(self):
        results = with_fault(NAS_ID, State.ERROR)
        self.assertEqual(rgb.rgb_signal(results, rgb.Acknowledgements(), False, False, 1.0).kind, rgb.CODES)
        self.assertEqual(rgb.rgb_signal(results, rgb.Acknowledgements(), False, False, VALIDITY).kind, rgb.OFF)

    def test_not_monitored_and_na_do_not_block_white(self):
        # Služby bez kolektoru do vstupu nepatří (doplňuje je až complete_service_results);
        # N/A je záměrně nesledovaná integrace.
        results = healthy() + [check("system.cpu_fan", State.NA)]
        self.assertEqual(rgb.rgb_signal(results, rgb.Acknowledgements(), True, False, 1.0).kind, rgb.LIGHT)

    def test_no_monitored_data_is_not_white(self):
        for results in ([], [check(ANIMEDB_ID, State.NA)]):
            with self.subTest(results=len(results)):
                signal = rgb.rgb_signal(results, rgb.Acknowledgements(), True, False, 1.0)
                self.assertEqual(signal.kind, rgb.OFF)


class BlinkPatternTest(unittest.TestCase):
    def test_every_category_blinks_its_code(self):
        check_ids = {1: CPU_TEMP_ID, 2: NAS_ID, 3: ANIMEDB_ID, 4: BACKUP_ID, 5: "docker.container_restarts"}
        for state, color in ((State.WARNING, rgb.ORANGE), (State.ERROR, rgb.RED)):
            for category, check_id in check_ids.items():
                with self.subTest(state=state, category=category):
                    signal = rgb.rgb_signal(with_fault(check_id, state), rgb.Acknowledgements(), False, False, 1.0)
                    self.assertEqual(signal, rgb.Signal(rgb.CODES, ((state, category),)))
                    # Dva průchody: kód se opakuje dokola, pokaždé stejně.
                    self.assertEqual(blink_groups(sample(signal, 2 * rgb.pass_length(signal.steps))),
                                     [(color, category), (color, category)])

    def test_code_timing(self):
        signal = rgb.Signal(rgb.CODES, ((State.ERROR, 2),))
        self.assertEqual(rgb.pass_length(signal.steps), 2 * 0.8 + 2.0)
        lit = {time: rgb.frame_at(signal, time, 20).lit for time in (0.2, 0.6, 1.0, 1.4, 1.8, 3.4, 3.8)}
        # 0,4 s svit, 0,4 s tma, po skupině 2 s pauza a pak znovu od začátku.
        self.assertEqual(lit, {0.2: True, 0.6: False, 1.0: True, 1.4: False, 1.8: False, 3.4: False, 3.8: True})

    def test_several_faults_of_same_severity_play_in_category_order(self):
        results = healthy() + [check(BACKUP_ID, State.ERROR), check(ANIMEDB_ID + "x", State.ERROR),
                               check(ANIMEDB_ID, State.ERROR)]
        signal = rgb.rgb_signal(results, rgb.Acknowledgements(), False, False, 1.0)
        # Dvě závady kategorie 5 (nezařazené id) a jedna 3 a 4: každá kategorie jednou, vzestupně.
        self.assertEqual(signal.steps, ((State.ERROR, 3), (State.ERROR, 4), (State.ERROR, 5)))
        self.assertEqual(blink_groups(sample(signal, rgb.pass_length(signal.steps))),
                         [(rgb.RED, 3), (rgb.RED, 4), (rgb.RED, 5)])

    def test_error_has_priority_over_warning(self):
        results = healthy() + [check(NAS_ID, State.WARNING), check(BACKUP_ID, State.ERROR)]
        signal = rgb.rgb_signal(results, rgb.Acknowledgements(), True, False, 1.0)
        self.assertEqual(signal.steps, ((State.ERROR, 4),))

    def test_critical_blinks_continuously_and_wins(self):
        results = healthy() + [check(NAS_ID, State.CRITICAL), check(BACKUP_ID, State.ERROR),
                               check(CPU_TEMP_ID + "x", State.WARNING)]
        for awake in (False, True):
            with self.subTest(awake=awake):
                signal = rgb.rgb_signal(results, rgb.Acknowledgements(), awake, True, 1.0)
                self.assertEqual(signal.kind, rgb.CRITICAL)
        frames = sample(rgb.Signal(rgb.CRITICAL), 10)
        self.assertTrue(all(frame.color == rgb.RED for frame in frames if frame.lit))
        # 0,25 s svit a 0,25 s tma bez pauzy: 20 bliknutí za 10 s, nikdy delší tma.
        self.assertEqual(sum(1 for before, after in zip(frames, frames[1:]) if after.lit and not before.lit), 19)
        self.assertEqual({rgb.frame_at(rgb.Signal(rgb.CRITICAL), time, 20).lit for time in (0.1, 0.6, 9.6)}, {True})
        self.assertEqual({rgb.frame_at(rgb.Signal(rgb.CRITICAL), time, 20).lit for time in (0.35, 0.85)}, {False})


class AcknowledgementTest(unittest.TestCase):
    def test_acknowledged_fault_goes_dark_and_is_never_white(self):
        results = with_fault(BACKUP_ID, State.ERROR)
        acks = rgb.Acknowledgements()
        self.assertEqual(acks.acknowledge(results, 1.0), [BACKUP_ID])
        for awake in (False, True):
            with self.subTest(awake=awake):
                signal = rgb.rgb_signal(results, acks, awake, False, 1.0)
                self.assertEqual(signal.kind, rgb.OFF)
        self.assertEqual(rgb.rgb_signal(results, acks, True, False, 1.0).reason, "potvrzená závada trvá")

    def test_acknowledgement_does_not_change_check_results(self):
        results = with_fault(NAS_ID, State.WARNING)
        before = list(results)
        rgb.Acknowledgements().acknowledge(results, 1.0)
        self.assertEqual(results, before)
        self.assertEqual({result.state for result in results if result.id == NAS_ID}, {State.WARNING})

    def test_critical_cannot_be_acknowledged(self):
        results = with_fault(NAS_ID, State.CRITICAL)
        acks = rgb.Acknowledgements()
        self.assertEqual(acks.acknowledge(results, 1.0), [])
        self.assertEqual(rgb.rgb_signal(results, acks, False, False, 1.0).kind, rgb.CRITICAL)

    def test_new_fault_after_acknowledgement_blinks(self):
        acks = rgb.Acknowledgements()
        acks.acknowledge(with_fault(NAS_ID, State.WARNING), 1.0)
        results = with_fault(NAS_ID, State.WARNING) + [check(BACKUP_ID, State.ERROR)]
        self.assertEqual(rgb.rgb_signal(results, acks, False, False, 1.0).steps, ((State.ERROR, 4),))

    def test_warning_to_error_needs_new_acknowledgement(self):
        acks = rgb.Acknowledgements()
        acks.acknowledge(with_fault(NAS_ID, State.WARNING), 1.0)
        worse = with_fault(NAS_ID, State.ERROR)
        self.assertEqual(rgb.rgb_signal(worse, acks, False, False, 1.0).steps, ((State.ERROR, 2),))
        acks.acknowledge(worse, 2.0)
        self.assertEqual(rgb.rgb_signal(worse, acks, False, False, 2.0).kind, rgb.OFF)
        # Zmírnění zpět na WARNING nové potvrzení nevyžaduje.
        self.assertEqual(rgb.rgb_signal(with_fault(NAS_ID, State.WARNING), acks, False, False, 3.0).kind, rgb.OFF)

    def test_acknowledgement_ends_only_after_three_clean_measurements(self):
        acks = rgb.Acknowledgements()
        acks.acknowledge(with_fault(NAS_ID, State.WARNING), 0.0)
        for at in (10.0, 20.0):
            acks.update(healthy(at) + [check(NAS_ID, State.OK, at)], at)
        # Závada se vrátila dřív než po třech měřeních bez závady: stejná epizoda, potvrzená.
        again = with_fault(NAS_ID, State.WARNING, at=30.0)
        acks.update(again, 30.0)
        self.assertEqual(rgb.rgb_signal(again, acks, False, False, 30.0).kind, rgb.OFF)
        for at in (40.0, 50.0, 60.0):
            acks.update(healthy(at) + [check(NAS_ID, State.OK, at)], at)
        self.assertEqual(acks.records, {})
        # Nová epizoda závady vyžaduje nové potvrzení.
        self.assertEqual(rgb.rgb_signal(with_fault(NAS_ID, State.WARNING, 70.0), acks, False, False, 70.0).kind,
                         rgb.CODES)

    def test_unknown_and_repeated_samples_do_not_count_as_recovery(self):
        acks = rgb.Acknowledgements()
        acks.acknowledge(with_fault(NAS_ID, State.ERROR), 0.0)

        def measure(state, at):
            acks.update(healthy(at) + [check(NAS_ID, state, at)], at)

        measure(State.OK, 10.0)
        measure(State.OK, 20.0)
        measure(State.UNKNOWN, 30.0)  # přeruší řadu
        measure(State.OK, 40.0)
        measure(State.OK, 50.0)
        # Stejné měření při každém překreslení se nezapočte znovu.
        for _ in range(5):
            acks.update(healthy(50.0) + [check(NAS_ID, State.OK, 50.0)], 51.0)
        self.assertIn(NAS_ID, acks.records)
        # Prošlý výsledek (OK z času 50, platnost 30 s) je UNKNOWN, ne odeznění.
        acks.update(healthy(50.0) + [check(NAS_ID, State.OK, 50.0)], 80.0)
        self.assertIn(NAS_ID, acks.records)
        measure(State.OK, 90.0)
        measure(State.OK, 100.0)
        self.assertIn(NAS_ID, acks.records)
        measure(State.OK, 110.0)
        self.assertNotIn(NAS_ID, acks.records)

    def test_missing_result_does_not_count_as_recovery(self):
        acks = rgb.Acknowledgements()
        acks.acknowledge(with_fault(NAS_ID, State.ERROR), 0.0)
        for at in (10.0, 20.0, 30.0, 40.0):
            acks.update(healthy(at), at)
        self.assertIn(NAS_ID, acks.records)

    def test_longer_rule_needs_longer_recovery(self):
        long_rule = AlarmRule(enabled=True, warning=70, error=80, hysteresis=5, confirm_count=5)
        short_rule = AlarmRule(enabled=True, warning=85, error=95, hysteresis=3, confirm_count=2)
        acks = rgb.Acknowledgements(AlarmsConfig(cpu_temp=long_rule, disk_root=short_rule))
        self.assertEqual(acks.required(CPU_TEMP_ID), 5)
        self.assertEqual(acks.required(DISK_ROOT_ID), 3)  # nejméně tři i u kratšího pravidla
        self.assertEqual(acks.required(NAS_ID), 3)
        self.assertEqual(rgb.Acknowledgements().required(CPU_TEMP_ID), 3)  # výchozí confirm_count 3

    def test_acknowledgement_survives_initial_confirmation_after_restart(self):
        # Potvrzení načtené po restartu (zde vytvořené ručně) a nový AlarmEvaluator,
        # který trvající závadu první dvě měření hlásí jako INFO a třetím ji potvrdí.
        rule = AlarmRule(enabled=True, warning=70, error=80, hysteresis=5)
        evaluator = AlarmEvaluator(AlarmsConfig(cpu_temp=rule))
        acks = rgb.Acknowledgements(AlarmsConfig(cpu_temp=rule))
        acks.records[CPU_TEMP_ID] = rgb.Acknowledgement(State.WARNING)
        states = []
        for at in (0.0, 10.0, 20.0):
            temperature = check(CPU_TEMP_ID, State.INFO, at, value=Measurement(74.0, "°C"))
            [evaluated] = evaluator.evaluate([temperature], at)
            states.append(evaluated.state)
            acks.update([evaluated], at)
        self.assertEqual(states, [State.INFO, State.INFO, State.WARNING])
        self.assertTrue(acks.covers(evaluated))  # potvrzení nezaniklo
        self.assertEqual(rgb.rgb_signal([evaluated], acks, False, False, 20.0).kind, rgb.OFF)


class ReplayTest(unittest.TestCase):
    def leds_with_acknowledged(self, results):
        leds = rgb.RgbLeds()
        leds.acks.acknowledge(results, 0.0)
        return leds

    def test_wake_replays_acknowledged_faults_once_then_dark(self):
        results = with_fault(BACKUP_ID, State.ERROR) + [check(NAS_ID, State.WARNING)]
        leds = self.leds_with_acknowledged(results)
        self.assertFalse(leds.frame(results, False, 1.0).lit)  # spící OLED: potvrzené nebliká
        leds.wake(results, 2.0)
        length = rgb.pass_length(((State.ERROR, 4), (State.WARNING, 2)))
        frames = led_frames(leds, results, True, 2.0, length + 10)
        # Nejdřív ERROR červeně, pak WARNING oranžově, jednou; pak tma, ne bílá.
        self.assertEqual(blink_groups(frames), [(rgb.RED, 4), (rgb.ORANGE, 2)])
        self.assertEqual(leds.signal, rgb.Signal(rgb.OFF, reason="potvrzená závada trvá"))
        self.assertFalse(any(frame.color == rgb.WHITE for frame in frames))

    def test_replay_starts_at_the_wake_press(self):
        results = with_fault(BACKUP_ID, State.ERROR)
        leds = self.leds_with_acknowledged(results)
        leds.wake(results, 2.0)
        # První překreslení až po 0,6 s: přehrání už běží od stisku, je tma mezi bliknutími.
        self.assertFalse(leds.frame(results, True, 2.6).lit)
        self.assertTrue(leds.frame(results, True, 2.9).lit)

    def test_no_replay_without_acknowledged_fault(self):
        leds = rgb.RgbLeds()
        leds.wake(healthy(), 0.0)
        self.assertFalse(leds.replay)
        self.assertEqual(leds.frame(healthy(), True, 0.1).color, rgb.WHITE)

    def test_unacknowledged_fault_keeps_automatic_signal_on_wake(self):
        acked = with_fault(BACKUP_ID, State.ERROR)
        leds = self.leds_with_acknowledged(acked)
        results = acked + [check(NAS_ID, State.WARNING)]
        leds.wake(results, 1.0)
        self.assertFalse(leds.replay)
        frames = led_frames(leds, results, True, 1.0, rgb.pass_length(((State.WARNING, 2),)))
        self.assertEqual(blink_groups(frames), [(rgb.ORANGE, 2)])

    def test_critical_preempts_replay(self):
        acked = with_fault(BACKUP_ID, State.ERROR)
        leds = self.leds_with_acknowledged(acked)
        leds.wake(acked, 1.0)
        self.assertTrue(leds.frame(acked, True, 1.1).lit)  # přehrání běží
        critical = acked + [check(NAS_ID, State.CRITICAL)]
        leds.frame(critical, True, 1.3)  # uprostřed bliknutí, přesto hned
        self.assertEqual(leds.signal.kind, rgb.CRITICAL)
        self.assertFalse(leds.replay)

    def test_change_waits_for_end_of_group(self):
        results = with_fault(BACKUP_ID, State.ERROR)
        leds = rgb.RgbLeds()
        leds.frame(results, False, 0.0)
        leds.acks.acknowledge(results, 1.0)  # uprostřed druhého ze čtyř bliknutí
        frames = led_frames(leds, results, False, 1.0, 4.0)
        # Skupina se dohraje (4 bliknutí celkem), pak LED zhasnou.
        self.assertEqual(sum(1 for before, after in zip(frames, frames[1:]) if after.lit and not before.lit), 2)
        self.assertEqual(leds.signal.kind, rgb.OFF)


class BrightnessTest(unittest.TestCase):
    def test_range_and_default(self):
        self.assertEqual(rgb.RgbLeds().brightness, 20)
        for value in (5, 20, 100):
            self.assertEqual(rgb.check_brightness(value), value)
        for value in (4, 0, 101, 20.5, True, "20", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    rgb.check_brightness(value)

    def test_brightness_scales_every_colour(self):
        critical, white = rgb.Signal(rgb.CRITICAL), rgb.Signal(rgb.LIGHT)
        self.assertEqual(rgb.frame_at(critical, 0.1, 100).rgb(), (255, 0, 0))
        self.assertEqual(rgb.frame_at(critical, 0.1, 20).rgb(), (51, 0, 0))
        self.assertEqual(rgb.frame_at(white, 0.1, 100).rgb(), (128, 128, 128))  # bílá na polovině
        # Ani nejnižší jas CRITICAL nezhasne.
        self.assertTrue(rgb.frame_at(critical, 0.1, 5).lit)
        self.assertEqual(rgb.frame_at(critical, 0.1, 5).rgb(), (13, 0, 0))

    def test_steady_shows_pattern_colour_without_blinking(self):
        codes = rgb.Signal(rgb.CODES, ((State.WARNING, 2),))
        self.assertFalse(rgb.frame_at(codes, 0.6, 20).lit)
        self.assertEqual(rgb.frame_at(codes, 0.6, 20, steady=True).color, rgb.ORANGE)
        self.assertTrue(rgb.frame_at(rgb.Signal(rgb.CRITICAL), 0.35, 20, steady=True).lit)
        replay = rgb.Signal(rgb.REPLAY, ((State.ERROR, 1),))
        self.assertFalse(rgb.frame_at(replay, 10.0, 20, steady=True).lit)  # po přehrání tma


if __name__ == "__main__":
    unittest.main()
