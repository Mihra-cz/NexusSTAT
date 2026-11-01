"""Testy trvalého uživatelského stavu: potvrzení závad a jas RGB (M6-4A).

Soubor stavu žije jen v dočasném adresáři (mimo repozitář), čas je simulovaný
monotónní `now` a pevné UTC, nic se nečeká. Žádný hardware, síť ani HTTP.
"""

import json
import os
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

from nexusstat import history, history_link, rgb, user_state
from nexusstat.alarms import AlarmEvaluator
from nexusstat.config import AlarmRule, AlarmsConfig, CollectorConfig, ConfigError, UserStateConfig, load_config
from nexusstat.core import CheckResult, Measurement, State, effective
from nexusstat.run import run_loop
from nexusstat.services import BACKUP_ID, NAS_ID
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID
from nexusstat.user_state import (ACKNOWLEDGED_EVENT, BRIGHTNESS_EVENT, CLEARED_EVENT, Acknowledge, Reply,
                                  RequestInterrupted, SetBrightness, UserState, load_state, save_state)

UTC = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
VALIDITY = 30.0
# Ilustrační práh jen pro testy; nejde o schválenou provozní hodnotu.
TEMP_RULE = AlarmRule(enabled=True, warning=70, error=80, hysteresis=5)


def check(check_id, state, at, value=None, simulated=False):
    return CheckResult(check_id, state, "popis", UTC + timedelta(seconds=at), at + VALIDITY,
                       simulated=simulated, measurement=value)


def results_with(check_id, state, at):
    return [check(CPU_USAGE_ID, State.INFO, at), check(check_id, state, at)]


class Owner:
    """UserState s cestou v dočasném adresáři, zachyceným výpisem a událostmi."""

    def __init__(self, directory, alarms=None, name="user_state.json"):
        self.path = Path(directory) / name
        self.lines, self.events = [], []
        self.state = self.make(alarms)

    def make(self, alarms=None):
        state = UserState(alarms, self.path, output=self.lines.append,
                          on_event=lambda *event: self.events.append(event))
        self.note = state.load()
        return state

    def ask(self, command, results, at):
        """Požadavek jako z vlákna webu a jeden takt hlavní smyčky; vrací odpověď."""
        future = self.state.request(command)
        self.done_before_tick = future.done()  # před taktem se nic nevyřídí
        self.state.tick(results, at, UTC + timedelta(seconds=at))
        return future.result(timeout=0)

    def kinds(self):
        return [event[0] for event in self.events]


def stored(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class AcknowledgeTest(unittest.TestCase):
    def test_warning_and_error_can_be_acknowledged_and_stay_visible(self):
        for state in (State.WARNING, State.ERROR):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                owner = Owner(directory)
                results = results_with(NAS_ID, state, 0.0)
                before = list(results)
                reply = owner.ask(Acknowledge(NAS_ID, state), results, 0.0)
                self.assertEqual((reply.accepted, reply.reason, reply.persisted), (True, "potvrzeno", True))
                self.assertFalse(owner.done_before_tick)
                # Diagnostický stav ani výsledky se nemění; závada zůstává vidět (OLED i web čtou výsledky).
                self.assertEqual(results, before)
                self.assertEqual(effective(results[1], 1.0).state, state)
                signal = rgb.rgb_signal(results, owner.state.acks, True, False, 1.0)
                self.assertEqual((signal.kind, signal.reason), (rgb.OFF, "potvrzená závada trvá"))
                self.assertEqual(stored(owner.path)["acknowledgements"],
                                 {NAS_ID: {"state": state.value, "acknowledged_utc": int(UTC.timestamp())}})
                self.assertEqual(owner.state.view.acknowledged, ((NAS_ID, state),))
                self.assertEqual(owner.kinds(), [ACKNOWLEDGED_EVENT])

    def test_critical_cannot_be_silenced(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            critical = results_with(NAS_ID, State.CRITICAL, 0.0)
            for seen in (State.CRITICAL, State.ERROR, State.WARNING):
                with self.subTest(seen=seen):
                    reply = owner.ask(Acknowledge(NAS_ID, seen), critical, 0.0)
                    self.assertFalse(reply.accepted)
                    self.assertIn("CRITICAL nelze umlčet", reply.reason)
            self.assertEqual(owner.state.acks.records, {})
            self.assertEqual(rgb.rgb_signal(critical, owner.state.acks, False, False, 0.0).kind, rgb.CRITICAL)
            self.assertFalse(owner.path.exists())  # odmítnutí nic nezapsalo
            # Potvrzená ERROR, která se zhorší na CRITICAL, bliká CRITICAL i s potvrzením.
            owner.ask(Acknowledge(NAS_ID, State.ERROR), results_with(NAS_ID, State.ERROR, 10.0), 10.0)
            worse = results_with(NAS_ID, State.CRITICAL, 20.0)
            owner.state.tick(worse, 20.0, UTC)
            self.assertEqual(rgb.rgb_signal(worse, owner.state.acks, False, False, 20.0).kind, rgb.CRITICAL)

    def test_inactive_unknown_expired_and_simulated_faults_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            cases = {
                "OK": results_with(NAS_ID, State.OK, 0.0),
                "UNKNOWN": results_with(NAS_ID, State.UNKNOWN, 0.0),
                "chybí": [check(CPU_USAGE_ID, State.INFO, 0.0)],
                "simulovaný": [check(NAS_ID, State.WARNING, 0.0, simulated=True)],
            }
            for name, results in cases.items():
                with self.subTest(name=name):
                    self.assertFalse(owner.ask(Acknowledge(NAS_ID, State.WARNING), results, 0.0).accepted)
            # Prošlé měření WARNING je UNKNOWN: potvrdit nejde.
            stale = results_with(NAS_ID, State.WARNING, 0.0)
            self.assertFalse(owner.ask(Acknowledge(NAS_ID, State.WARNING), stale, VALIDITY + 1).accepted)
            for command in (Acknowledge(NAS_ID, "WARNING"), Acknowledge(None, State.WARNING),
                            SetBrightness(4), SetBrightness(101), SetBrightness(True), SetBrightness("20")):
                with self.subTest(command=command):
                    self.assertFalse(owner.ask(command, results_with(NAS_ID, State.WARNING, 0.0), 0.0).accepted)
            self.assertEqual((owner.state.acks.records, owner.state.brightness, owner.state.saves),
                             ({}, rgb.BRIGHTNESS_DEFAULT, 0))
            with self.assertRaises(TypeError):
                owner.state.request("potvrdit vše")

    def test_escalation_needs_new_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            owner.ask(Acknowledge(NAS_ID, State.WARNING), results_with(NAS_ID, State.WARNING, 0.0), 0.0)
            worse = results_with(NAS_ID, State.ERROR, 10.0)
            owner.state.tick(worse, 10.0, UTC)
            self.assertEqual(rgb.rgb_signal(worse, owner.state.acks, False, False, 10.0).steps, ((State.ERROR, 2),))
            # Správce viděl WARNING, mezitím je ERROR: odmítne se, rozhodne znovu.
            reply = owner.ask(Acknowledge(NAS_ID, State.WARNING), worse, 10.0)
            self.assertEqual((reply.accepted, reply.reason), (False, "závada se mezitím zhoršila, potvrďte ji znovu"))
            self.assertEqual(owner.ask(Acknowledge(NAS_ID, State.ERROR), worse, 10.0).reason, "potvrzeno")
            self.assertEqual(stored(owner.path)["acknowledgements"][NAS_ID]["state"], "ERROR")
            # Zmírnění na WARNING nové potvrzení nevyžaduje a nic nezapisuje.
            milder = results_with(NAS_ID, State.WARNING, 20.0)
            owner.state.tick(milder, 20.0, UTC)
            self.assertEqual(rgb.rgb_signal(milder, owner.state.acks, False, False, 20.0).kind, rgb.OFF)
            self.assertEqual(owner.ask(Acknowledge(NAS_ID, State.WARNING), milder, 20.0).reason, "už potvrzeno")
            self.assertEqual((owner.state.saves, owner.kinds()), (2, [ACKNOWLEDGED_EVENT, ACKNOWLEDGED_EVENT]))

    def test_seen_error_after_softening_stores_the_current_level(self):
        # Viděl ERROR, teď je WARNING: uloží se WARNING, takže návrat ERROR znovu bliká.
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            owner.ask(Acknowledge(NAS_ID, State.ERROR), results_with(NAS_ID, State.WARNING, 0.0), 0.0)
            self.assertEqual(owner.state.acks.records[NAS_ID].state, State.WARNING)

    def test_new_episode_needs_new_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            owner.ask(Acknowledge(NAS_ID, State.WARNING), results_with(NAS_ID, State.WARNING, 0.0), 0.0)
            for at in (10.0, 20.0):
                owner.state.tick(results_with(NAS_ID, State.OK, at), at, UTC)
            self.assertIn(NAS_ID, owner.state.acks.records)  # dvě měření bez závady nestačí
            owner.state.tick(results_with(NAS_ID, State.OK, 30.0), 30.0, UTC)
            self.assertEqual(owner.state.acks.records, {})
            self.assertEqual(stored(owner.path)["acknowledgements"], {})  # zánik se uloží
            self.assertEqual(owner.kinds(), [ACKNOWLEDGED_EVENT, CLEARED_EVENT])
            again = results_with(NAS_ID, State.WARNING, 40.0)
            owner.state.tick(again, 40.0, UTC)
            self.assertEqual(rgb.rgb_signal(again, owner.state.acks, False, False, 40.0).kind, rgb.CODES)

    def test_unknown_missing_and_expired_data_do_not_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            owner.ask(Acknowledge(NAS_ID, State.ERROR), results_with(NAS_ID, State.ERROR, 0.0), 0.0)
            ticks = [(results_with(NAS_ID, State.OK, 10.0), 10.0), (results_with(NAS_ID, State.OK, 20.0), 20.0),
                     (results_with(NAS_ID, State.UNKNOWN, 30.0), 30.0),  # přeruší řadu
                     (results_with(NAS_ID, State.OK, 40.0), 40.0), (results_with(NAS_ID, State.OK, 50.0), 50.0),
                     ([check(CPU_USAGE_ID, State.INFO, 60.0)], 60.0),  # výsledek chybí
                     (results_with(NAS_ID, State.OK, 70.0), 70.0), (results_with(NAS_ID, State.OK, 80.0), 80.0),
                     (results_with(NAS_ID, State.OK, 80.0), 80.0 + VALIDITY + 1)]  # prošlé = UNKNOWN
            for results, at in ticks:
                for _ in range(5):  # stejné měření v dalších taktech se nezapočte znovu
                    owner.state.tick(results, at, UTC)
            self.assertIn(NAS_ID, owner.state.acks.records)
            self.assertEqual(owner.state.saves, 1)  # počítadla odeznění se nezapisují

    def test_brightness_is_persisted_only_on_change(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            results = results_with(NAS_ID, State.OK, 0.0)
            self.assertEqual(owner.ask(SetBrightness(rgb.BRIGHTNESS_DEFAULT), results, 0.0).reason, "jas beze změny")
            self.assertFalse(owner.path.exists())
            reply = owner.ask(SetBrightness(5), results, 1.0)
            self.assertEqual((reply.accepted, reply.persisted), (True, True))
            self.assertEqual(owner.ask(SetBrightness(5), results, 2.0).reason, "jas beze změny")
            self.assertEqual((stored(owner.path)["brightness"], owner.state.saves), (5, 1))
            self.assertEqual(owner.events[0][:2], (BRIGHTNESS_EVENT, "INFO"))
            self.assertEqual(owner.events[0][5], {"previous": 20, "brightness": 5})


class RestartTest(unittest.TestCase):
    def measure(self, evaluator, owner, celsius, at):
        temperature = check(CPU_TEMP_ID, State.INFO, at, value=Measurement(celsius, "°C"))
        results = evaluator.evaluate([temperature], at)
        owner.state.tick(results, at, UTC + timedelta(seconds=at))
        return results

    def test_restart_during_ongoing_fault_keeps_acknowledgement(self):
        for confirm in (3, 5):
            with self.subTest(confirm_count=confirm), tempfile.TemporaryDirectory() as directory:
                alarms = AlarmsConfig(cpu_temp=AlarmRule(True, 70, 80, 5, confirm_count=confirm))
                owner = Owner(directory, alarms)
                evaluator = AlarmEvaluator(alarms)
                for at in range(confirm):
                    results = self.measure(evaluator, owner, 74.0, at * 10.0)
                self.assertEqual(results[0].state, State.WARNING)
                owner.ask(Acknowledge(CPU_TEMP_ID, State.WARNING), results, confirm * 10.0)
                # Restart: nový proces = nový AlarmEvaluator i UserState, stav jen ze souboru.
                owner.state = owner.make(alarms)
                self.assertIn("potvrzení 1", owner.note)
                evaluator = AlarmEvaluator(alarms)
                states = []
                for step in range(confirm):
                    at = 1000.0 + step * 10.0
                    [result] = self.measure(evaluator, owner, 74.0, at)
                    states.append(result.state)
                    self.assertIn(CPU_TEMP_ID, owner.state.acks.records)  # nezaniklo ani během potvrzování
                self.assertEqual(states, [State.INFO] * (confirm - 1) + [State.WARNING])
                self.assertEqual(rgb.rgb_signal([result], owner.state.acks, False, False, at).kind, rgb.OFF)
                self.assertEqual(owner.state.saves, 0)  # po restartu se nic nepřepisovalo

    def test_restart_after_real_recovery_clears_after_required_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            alarms = AlarmsConfig(cpu_temp=TEMP_RULE)
            owner = Owner(directory, alarms)
            evaluator = AlarmEvaluator(alarms)
            for at in (0.0, 10.0, 20.0):
                results = self.measure(evaluator, owner, 74.0, at)
            owner.ask(Acknowledge(CPU_TEMP_ID, State.WARNING), results, 20.0)
            owner.state = owner.make(alarms)
            evaluator = AlarmEvaluator(alarms)
            for at in (1000.0, 1010.0):
                self.measure(evaluator, owner, 50.0, at)
            self.assertIn(CPU_TEMP_ID, owner.state.acks.records)
            self.measure(evaluator, owner, 50.0, 1020.0)
            self.assertEqual(owner.state.acks.records, {})
            self.assertEqual((load_state(owner.path).acknowledgements, owner.state.saves), ((), 1))

    def test_restored_acknowledgement_drives_shared_rgb_leds(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            results = results_with(BACKUP_ID, State.ERROR, 0.0)
            owner.ask(Acknowledge(BACKUP_ID, State.ERROR), results, 0.0)
            owner.ask(SetBrightness(40), results, 0.0)
            restarted = owner.make()
            leds = rgb.RgbLeds(brightness=restarted.brightness, acks=restarted.acks)
            self.assertEqual(leds.frame(results, False, 1.0), rgb.DARK)  # potvrzená závada nebliká
            leds.wake(results, 2.0)
            self.assertEqual(leds.frame(results, True, 2.1), rgb.LedFrame(rgb.RED, 0.4))  # přehrání s jasem 40 %


class FileTest(unittest.TestCase):
    def test_atomic_save_private_permissions_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user_state.json"
            records = ((NAS_ID, State.ERROR, 1_793_000_000), (BACKUP_ID, State.WARNING, None))
            save_state(path, 35, records)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(sorted(os.listdir(directory)), ["user_state.json"])  # žádný dočasný soubor
            loaded = load_state(path)
            self.assertEqual((loaded.brightness, loaded.problem, loaded.found), (35, None, True))
            self.assertEqual(loaded.acknowledgements, tuple(sorted(records)))
            self.assertEqual(stored(path)["format"], user_state.FORMAT_VERSION)

    def test_failed_write_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user_state.json"
            save_state(path, 30, ())
            original = path.read_bytes()
            for target in ("nexusstat.user_state.os.fsync", "nexusstat.user_state.os.replace"):
                with self.subTest(target=target), mock.patch(target, side_effect=OSError(28, "plný disk")):
                    with self.assertRaises(OSError):
                        save_state(path, 90, ((NAS_ID, State.ERROR, None),))
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(os.listdir(directory), ["user_state.json"])  # rozpracovaný soubor smazán

    def test_directory_sync_failure_is_not_reported_as_saved(self):
        # Po přejmenování selže fsync adresáře: nový obsah na disku je, ale potvrzený není.
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            with mock.patch("nexusstat.user_state.storage.fsync_directory", side_effect=OSError(5, "I/O")):
                reply = owner.ask(SetBrightness(40), results_with(NAS_ID, State.OK, 0.0), 0.0)
            self.assertEqual((reply.accepted, reply.persisted, owner.state.saves), (True, False, 0))
            self.assertEqual(os.listdir(directory), ["user_state.json"])
            owner.state.tick(results_with(NAS_ID, State.OK, 30.0), 30.0, UTC)
            self.assertEqual(owner.state.saves, 0)  # další pokus nejdřív po 60 s
            owner.state.tick(results_with(NAS_ID, State.OK, 60.0), 60.0, UTC)
            self.assertEqual((owner.state.saves, load_state(owner.path).brightness), (1, 40))

    def test_interrupted_write_keeps_previous_file_and_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user_state.json"
            path.write_text("starý", encoding="utf-8")
            os.chmod(path, 0o644)
            with mock.patch("nexusstat.user_state.os.fsync", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    save_state(path, 30, ())
            self.assertEqual((path.read_text(encoding="utf-8"), os.listdir(directory)), ("starý", ["user_state.json"]))
            previous = os.umask(0)  # ani propustná maska nezmění soukromá oprávnění
            try:
                save_state(path, 30, ())
            finally:
                os.umask(previous)
            self.assertEqual((stat.S_IMODE(path.stat().st_mode), load_state(path).brightness), (0o600, 30))

    def test_missing_file_means_defaults_and_no_write(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            self.assertIn("zatím neexistuje", owner.note)
            for at in range(100):
                owner.state.tick(results_with(NAS_ID, State.WARNING, float(at)), float(at), UTC)
            self.assertEqual((owner.state.brightness, owner.state.saves, os.listdir(directory)),
                             (rgb.BRIGHTNESS_DEFAULT, 0, []))

    def test_corrupt_or_untrusted_file_is_ignored_conservatively(self):
        valid = {"format": 1, "brightness": 50,
                 "acknowledgements": {NAS_ID: {"state": "ERROR", "acknowledged_utc": 1_793_000_000}}}

        def variant(**changes):
            data = json.loads(json.dumps(valid))
            data.update(changes)
            return json.dumps(data).encode()

        cases = {
            "prázdný": b"",
            "useknutý": json.dumps(valid).encode()[:-5],
            "není UTF-8": b"\xff\xfe{}",
            "NaN": variant().replace(b"50", b"NaN"),
            "pole": b"[]",
            "verze 2": variant(format=2),
            "verze true": variant(format=True),
            "neznámý klíč": variant(extra=1),
            "jas 3": variant(brightness=3),
            "jas text": variant(brightness="50"),
            "CRITICAL": variant(acknowledgements={NAS_ID: {"state": "CRITICAL", "acknowledged_utc": None}}),
            "UNKNOWN": variant(acknowledgements={NAS_ID: {"state": "UNKNOWN", "acknowledged_utc": None}}),
            "id": variant(acknowledgements={"NAS": {"state": "ERROR", "acknowledged_utc": None}}),
            "čas": variant(acknowledgements={NAS_ID: {"state": "ERROR", "acknowledged_utc": -1}}),
            "chybí čas": variant(acknowledgements={NAS_ID: {"state": "ERROR"}}),
            "moc potvrzení": variant(acknowledgements={f"service.x{i}": {"state": "ERROR", "acknowledged_utc": None}
                                                     for i in range(user_state.MAX_ACKNOWLEDGEMENTS + 1)}),
            "velký": b" " * (user_state.MAX_FILE_BYTES + 1) + variant(),
        }
        for name, content in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                owner_path = Path(directory) / "user_state.json"
                owner_path.write_bytes(content)
                os.chmod(owner_path, 0o600)
                owner = Owner(directory)
                self.assertIn("nepoužit", owner.note)
                self.assertEqual((owner.state.brightness, owner.state.acks.records),
                                 (rgb.BRIGHTNESS_DEFAULT, {}))  # žádné falešné potvrzení
                for at in range(5):
                    owner.state.tick(results_with(NAS_ID, State.ERROR, float(at)), float(at), UTC)
                self.assertEqual(owner_path.read_bytes(), content)  # poškozený soubor zůstal, nic se nezapsalo
                # Teprve skutečná změna ho přepíše platným obsahem.
                owner.ask(SetBrightness(60), results_with(NAS_ID, State.ERROR, 5.0), 5.0)
                self.assertEqual(load_state(owner_path).brightness, 60)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user_state.json"
            path.write_text(json.dumps(valid), encoding="utf-8")
            os.chmod(path, 0o600)
            self.assertEqual(load_state(path).brightness, 50)  # kontrola: platná varianta projde
            os.chmod(path, 0o620)
            self.assertIn("skupinu", load_state(path).problem)  # cizí zápis by mohl podstrčit potvrzení
            os.chmod(path, 0o600)
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            self.assertEqual(load_state(link).problem.split(" (")[0], "nelze otevřít")
            folder = Path(directory) / "folder.json"
            folder.mkdir()
            self.assertEqual(load_state(folder).problem, "není obyčejný soubor")
            with mock.patch("nexusstat.user_state.os.geteuid", return_value=os.geteuid() + 1):
                self.assertEqual(load_state(path).problem, "patří jinému uživateli")

    def test_write_failure_is_retried_rarely_and_kept_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(Path(directory) / "chybí")  # adresář neexistuje; aplikace ho nevytváří
            results = results_with(NAS_ID, State.WARNING, 0.0)
            with mock.patch("nexusstat.user_state.save_state", wraps=save_state) as saving:
                reply = owner.ask(Acknowledge(NAS_ID, State.WARNING), results, 0.0)
                self.assertEqual((reply.accepted, reply.persisted), (True, False))  # platí, ale neuloženo
                for second in range(1, 125):
                    owner.state.tick(results, float(second), UTC)
                self.assertEqual(saving.call_count, 3)  # 0, 60 a 120 s, ne každý takt
                self.assertIn(NAS_ID, owner.state.acks.records)
                self.assertEqual(len([line for line in owner.lines if "nelze zapsat" in line]), 1)
                (Path(directory) / "chybí").mkdir()
                for second in range(125, 185):
                    owner.state.tick(results, float(second), UTC)
                self.assertEqual((saving.call_count, owner.state.saves), (4, 1))
            self.assertTrue(owner.lines[-1].endswith("znovu uložen."))

    def test_memory_only_without_path(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch("nexusstat.user_state.save_state") as saving:
            state = UserState(output=lambda line: None)
            self.assertIn("jen v paměti", state.load())
            future = state.request(Acknowledge(NAS_ID, State.WARNING))
            state.tick(results_with(NAS_ID, State.WARNING, 0.0), 0.0, UTC)
            self.assertEqual((future.result(0).accepted, future.result(0).persisted), (True, False))
            saving.assert_not_called()
            self.assertEqual(os.listdir(directory), [])


class WriteFrequencyTest(unittest.TestCase):
    def test_a_day_of_ticks_writes_only_real_changes(self):
        # 24 h taktů po 1 s s trvající potvrzenou závadou, měřením po 10 s a přechodnými UNKNOWN.
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            owner.ask(Acknowledge(NAS_ID, State.WARNING), results_with(NAS_ID, State.WARNING, 0.0), 0.0)
            owner.ask(SetBrightness(30), results_with(NAS_ID, State.WARNING, 1.0), 1.0)
            with mock.patch("nexusstat.user_state.save_state", wraps=save_state) as saving:
                for second in range(2, 86_400):
                    measured = float(second - second % 10)
                    state = State.UNKNOWN if second % 3600 < 10 else State.WARNING
                    owner.state.tick(results_with(NAS_ID, state, measured), float(second), UTC)
                self.assertEqual(saving.call_count, 0)
                for second in range(86_400, 86_440, 10):  # doložené odeznění: jediný další zápis
                    owner.state.tick(results_with(NAS_ID, State.OK, float(second)), float(second), UTC)
                self.assertEqual(saving.call_count, 1)
            self.assertEqual(owner.state.saves, 3)  # potvrzení, jas, zánik


class ThreadingTest(unittest.TestCase):
    def test_requests_from_other_threads_change_nothing_until_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            results = results_with(NAS_ID, State.ERROR, 0.0)
            futures = []
            threads = [threading.Thread(target=lambda: futures.append(owner.state.request(SetBrightness(50))))
                       for _ in range(4)]
            threads.append(threading.Thread(
                target=lambda: futures.append(owner.state.request(Acknowledge(NAS_ID, State.ERROR)))))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            # Vlákna jen podala požadavky: potvrzení, jas ani soubor se nezměnily.
            self.assertEqual((owner.state.acks.records, owner.state.brightness, owner.path.exists()),
                             ({}, rgb.BRIGHTNESS_DEFAULT, False))
            self.assertFalse(any(future.done() for future in futures))
            owner.state.tick(results, 0.0, UTC)
            self.assertTrue(all(future.result(0).accepted for future in futures))
            self.assertEqual((owner.state.brightness, owner.state.saves), (50, 1))  # jeden zápis za takt

    def test_full_queue_and_shutdown_reject_immediately(self):
        state = UserState(output=lambda line: None)
        pending = [state.request(SetBrightness(30)) for _ in range(user_state.MAX_PENDING)]
        overflow = state.request(SetBrightness(30))
        self.assertEqual(overflow.result(0).reason, "příliš mnoho čekajících požadavků")
        state.close()
        self.assertEqual({future.result(0).reason for future in pending}, {"NexusSTAT se ukončuje"})
        self.assertEqual(state.request(SetBrightness(40)).result(0).accepted, False)
        self.assertEqual(state.brightness, rgb.BRIGHTNESS_DEFAULT)


class Gate:
    """Zastaví vlákno v přesném místě: `entered` = je tam, `release` = smí dál (bez náhodného časování)."""

    def __init__(self, action=None):
        self.entered, self.release, self.action = threading.Event(), threading.Event(), action

    def __call__(self, *args, **kwargs):
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("test vlákno nepustil dál")
        if self.action is not None:
            return self.action(*args, **kwargs)
        return None


class CountingFuture(Future):
    """Future, které počítá skutečná dokončení (každé má být nejvýš jedno)."""

    created = []

    def __init__(self):
        super().__init__()
        self.completions = 0
        CountingFuture.created.append(self)

    def set_result(self, result):
        super().set_result(result)  # dvojí dokončení vyhodí InvalidStateError a nezapočte se
        self.completions += 1

    def set_exception(self, exception):
        super().set_exception(exception)
        self.completions += 1


class Worker(threading.Thread):
    """Vlákno, které si zapamatuje výjimku, aby ji test viděl."""

    def __init__(self, target):
        super().__init__(target=self.guarded)
        self.work, self.error = target, None

    def guarded(self):
        try:
            self.work()
        except BaseException as error:  # noqa: BLE001
            self.error = error


SHUTDOWN = Reply(False, "NexusSTAT se ukončuje")


class ConcurrencyTest(unittest.TestCase):
    """Souběh požadavků z jiných vláken s taktem a ukončením (`close()`), řízený událostmi."""

    def setUp(self):
        CountingFuture.created = []
        patcher = mock.patch("nexusstat.user_state.Future", CountingFuture)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "user_state.json"
        self.state = UserState(path=self.path, output=lambda line: None)
        self.results = results_with(NAS_ID, State.WARNING, 0.0)

    def tearDown(self):
        # Každý požadavek skončil a nejvýš jednou; ve frontě nic nezůstalo.
        for future in CountingFuture.created:
            self.assertTrue(future.done(), future)
            self.assertLessEqual(future.completions, 1, future)
        self.assertTrue(self.state._requests.empty())

    def tick(self, at=0.0):
        self.state.tick(self.results, at, UTC)

    def test_request_entering_the_queue_during_close_is_rejected(self):
        # Vlákno prošlo první kontrolou ukončení a právě vkládá do fronty, když close() frontu vyprázdní.
        gate = Gate(self.state._requests.put_nowait)
        self.state._requests.put_nowait = gate
        futures = []
        thread = Worker(lambda: futures.append(self.state.request(SetBrightness(50))))
        thread.start()
        self.assertTrue(gate.entered.wait(5))
        self.state.close()
        gate.release.set()
        thread.join(5)
        self.assertIsNone(thread.error)
        self.assertEqual(futures[0].result(0), SHUTDOWN)
        self.tick()  # ani další takt odmítnutý požadavek neprovede
        self.assertEqual((self.state.brightness, self.state.saves), (rgb.BRIGHTNESS_DEFAULT, 0))

    def test_request_after_close_is_rejected_immediately(self):
        self.state.close()
        future = self.state.request(Acknowledge(NAS_ID, State.WARNING))
        self.assertEqual(future.result(0), SHUTDOWN)
        self.tick()
        self.assertEqual(self.state.acks.records, {})

    def test_shutdown_while_the_loop_answers(self):
        # Takt právě zapisuje soubor (převzatý požadavek už nejde zrušit); mezitím přijde další požadavek a ukončení.
        gate = Gate(save_state)
        with mock.patch("nexusstat.user_state.save_state", gate):
            first = self.state.request(SetBrightness(50))
            loop = Worker(self.tick)
            loop.start()
            self.assertTrue(gate.entered.wait(5))
            self.assertFalse(first.cancel())  # už se vyřizuje: odpověď přijde
            second = self.state.request(Acknowledge(NAS_ID, State.WARNING))
            self.state.close()
            third = self.state.request(SetBrightness(70))
            self.assertEqual((second.result(0), third.result(0)), (SHUTDOWN, SHUTDOWN))
            gate.release.set()
            loop.join(5)
        self.assertIsNone(loop.error)
        self.assertEqual(first.result(0), Reply(True, "jas nastaven", True))
        # Odmítnuté požadavky nic nezměnily; platí jen ten převzatý.
        self.assertEqual((self.state.brightness, self.state.acks.records), (50, {}))
        self.assertEqual(load_state(self.path).brightness, 50)

    def test_timed_out_request_cancelled_in_queue_never_runs(self):
        future = self.state.request(SetBrightness(50))
        with self.assertRaises(TimeoutError):
            future.result(timeout=0)  # volající nečeká; požadavek je stále ve frontě
        self.assertTrue(future.cancel())
        self.tick()  # dřív: změna jasu a pak InvalidStateError, který by zastavil měřicí smyčku
        self.assertEqual((self.state.brightness, self.state.saves, future.completions), (rgb.BRIGHTNESS_DEFAULT, 0, 0))

    def test_request_completed_by_the_caller_is_not_run(self):
        future = self.state.request(SetBrightness(50))
        future.set_result(Reply(False, "volající to vzdal"))
        self.tick()
        self.assertEqual(self.state.brightness, rgb.BRIGHTNESS_DEFAULT)
        # Dokončí-li ho volající až během vyřizování, smyčka nespadne na InvalidStateError.
        gate = Gate(save_state)
        with mock.patch("nexusstat.user_state.save_state", gate):
            late = self.state.request(SetBrightness(60))
            loop = Worker(lambda: self.tick(1.0))
            loop.start()
            self.assertTrue(gate.entered.wait(5))
            late.set_result(Reply(False, "volající"))
            gate.release.set()
            loop.join(5)
        self.assertIsNone(loop.error)
        self.assertEqual(late.result(0).reason, "volající")

    def test_interrupt_while_saving_answers_claimed_requests_honestly(self):
        ack = self.state.request(Acknowledge(NAS_ID, State.WARNING))
        brightness = self.state.request(SetBrightness(50))
        with mock.patch("nexusstat.user_state.save_state", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()
        # Přijato, ale neuloženo: proces končí a soubor se nezměnil.
        self.assertEqual([future.result(0) for future in (ack, brightness)],
                         [Reply(True, "potvrzeno", False), Reply(True, "jas nastaven", False)])
        self.assertFalse(self.path.exists())
        self.state.close()

    def test_interrupt_while_applying_leaves_nothing_pending(self):
        events = []

        def interrupt(*event):
            events.append(event)
            raise KeyboardInterrupt

        state = self.state = UserState(path=self.path, output=lambda line: None, on_event=interrupt)
        first = state.request(SetBrightness(50))
        second = state.request(SetBrightness(60))
        with self.assertRaises(KeyboardInterrupt):
            self.tick()
        with self.assertRaises(RequestInterrupted):
            first.result(0)  # výsledek nejistý, ne „odmítnuto“
        self.assertFalse(second.done())  # zůstal ve frontě; vyřídí ho ukončení
        state.close()
        self.assertEqual(second.result(0), SHUTDOWN)
        self.assertEqual(len(events), 1)

    def test_request_taken_but_not_claimed_can_still_be_cancelled(self):
        # Přerušení mezi vyjmutím z fronty a převzetím: požadavek zůstane čekající,
        # takže ho volající po svém limitu zruší a nikdy se neprovede.
        future = self.state.request(SetBrightness(50))
        with mock.patch("nexusstat.user_state._claim", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()
        self.assertTrue(future.cancel())
        self.state.close()
        self.tick(1.0)
        self.assertEqual(self.state.brightness, rgb.BRIGHTNESS_DEFAULT)

    def test_many_threads_racing_close_leave_nothing_pending(self):
        barrier = threading.Barrier(17)
        futures = []

        def ask():
            barrier.wait(5)
            futures.append(self.state.request(SetBrightness(50)))

        threads = [Worker(ask) for _ in range(16)]
        for thread in threads:
            thread.start()
        barrier.wait(5)
        self.state.close()
        for thread in threads:
            thread.join(5)
        self.assertEqual([thread.error for thread in threads], [None] * 16)
        # Ať vlákna stihla vložit před vyprázdněním, nebo po něm: všechna odmítnutá.
        self.assertEqual({future.result(0) for future in futures}, {SHUTDOWN})
        self.tick()
        self.assertEqual((self.state.brightness, self.state.saves), (rgb.BRIGHTNESS_DEFAULT, 0))


class HistoryEventTest(unittest.TestCase):
    class Worker:
        def __init__(self, error=None):
            self.events, self.error = [], error

        def submit_event(self, event):
            if self.error is not None:
                raise self.error
            self.events.append(event)
            return True

    def test_event_time_quality_and_failure(self):
        worker = self.Worker()
        lines = []
        link = history_link.HistoryLink(worker, output=lines.append)
        moment = datetime(2026, 11, 1, 12, 0, 0, 900_000, tzinfo=timezone.utc)
        self.assertTrue(link.record_event(ACKNOWLEDGED_EVENT, "INFO", "potvrzena", moment, NAS_ID, {"state": "ERROR"}))
        link._synced = True
        link.record_event(CLEARED_EVENT, "INFO", "zaniklo", moment, NAS_ID)
        self.assertEqual([(event.kind, event.ts, event.ts_quality, event.check_id) for event in worker.events],
                         [(ACKNOWLEDGED_EVENT, int(moment.timestamp()), "unsynced", NAS_ID),
                          (CLEARED_EVENT, int(moment.timestamp()), "synced", NAS_ID)])
        self.assertEqual(worker.events[0].data, {"state": "ERROR"})
        broken = history_link.HistoryLink(self.Worker(RuntimeError("fronta")), output=lines.append)
        self.assertFalse(broken.record_event(ACKNOWLEDGED_EVENT, "INFO", "x", moment))  # do smyčky nic nevyhodí
        self.assertEqual((broken.submit_failures, len(lines)), (1, 1))
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)  # před MIN_TRUSTED_UTC je čas vždy unsynced
        link.record_event(ACKNOWLEDGED_EVENT, "INFO", "x", old)
        self.assertEqual(worker.events[-1].ts_quality, "unsynced")
        self.assertLess(worker.events[-1].ts, history.MIN_TRUSTED_UTC)


class Collector:
    """Skutečně neoznačený (ne simulovaný) kolektor pro takt 1 s: CPU a NAS ve WARNING."""

    labels = {CPU_USAGE_ID: "CPU", NAS_ID: "NAS"}

    def __init__(self):
        self.calls = []

    def collect(self, now, observed_utc):
        self.calls.append(now)
        return [CheckResult(CPU_USAGE_ID, State.INFO, "CPU", observed_utc, now + 15, simulated=False),
                CheckResult(NAS_ID, State.WARNING, "NAS", observed_utc, now + 15, simulated=False)]


class Clock:
    def __init__(self, stop_at, on_sleep=None):
        self.now, self.stop_at, self.on_sleep = 100.0, stop_at, on_sleep

    def __call__(self):
        return self.now

    def utc(self):
        return UTC + timedelta(seconds=self.now)

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.now)
        if self.now >= self.stop_at:
            raise KeyboardInterrupt


class LoopTest(unittest.TestCase):
    def run_loop(self, state=None, on_sleep=None):
        clock, collector = Clock(130.0, on_sleep), Collector()
        run_loop(collector, CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=clock, utc_clock=clock.utc,
                 sleep=clock.sleep, output=lambda line: None, tick_seconds=1, user_state=state)
        return collector.calls

    def test_loop_serves_requests_without_changing_collector_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(directory)
            futures = []

            def web_request(now):  # „web“ podá požadavek mezi takty
                if now == 105.0:
                    futures.append(owner.state.request(Acknowledge(NAS_ID, State.WARNING)))

            calls = self.run_loop(owner.state, web_request)
            self.assertEqual(calls, self.run_loop())  # stejné takty i bez uživatelského stavu
            self.assertEqual(calls, [100.0 + second for second in range(30)])
            self.assertEqual(futures[0].result(0).reason, "potvrzeno")
            self.assertEqual((owner.state.saves, list(owner.state.acks.records)), (1, [NAS_ID]))


class ConfigTest(unittest.TestCase):
    def load_text(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def test_user_state_section(self):
        self.assertEqual(self.load_text("").user_state, UserStateConfig())
        self.assertEqual(self.load_text('[user_state]\npath = "/srv/x/user_state.json"\n').user_state.path,
                         "/srv/x/user_state.json")
        for text in ('[user_state]\npath = "user_state.json"\n', '[user_state]\npath = "/srv/x/state.toml"\n',
                     '[user_state]\npath = "/srv/x/"\n', '[user_state]\npath = ""\n', "[user_state]\npath = 1\n",
                     '[user_state]\npath = "/srv/x/a.json"\nbrightness = 20\n'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self.load_text(text)


if __name__ == "__main__":
    unittest.main()
