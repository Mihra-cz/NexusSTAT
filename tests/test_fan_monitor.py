"""Testy read-only kolektoru ventilátoru CPU a čtvrtého řádku stránky SYSTEM (M4-1).

Vše nad syntetickými adresáři `/sys` v `tempfile.TemporaryDirectory`: žádný
skutečný hardware se nečte ani nemění, nic se nezapisuje do sysfs a ventilátor
se nijak neřídí. Časy jsou pevné, žádné skutečné čekání.
"""

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from nexusstat.config import OledConfig
from nexusstat.core import CheckResult, Measurement, State, active_problems, overall_state
from nexusstat.fan_monitor import (
    CPU_FAN_ID,
    DEVICE,
    FAN_OFF,
    FAN_ON,
    FAN_STATE,
    LABELS,
    PERCENT,
    RPM,
    SOURCE,
    SOURCE_REQUEST,
    SOURCE_RPM,
    Ambiguous,
    FanMonitor,
    find_cpu_fan,
    other_fans,
    parse_cooling_percent,
    parse_pwm_percent,
    parse_rpm,
)
from nexusstat.oled import OledController, home_symbol
from nexusstat.oled_text import REQUEST_PREFIX, fan_value, page_rows, service_rows, status_texts
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, MEMORY_ID

try:
    from PIL import Image

    from nexusstat import render
    from nexusstat.render import render_screen
except ImportError:  # Pillow není povinná závislost
    Image = render = render_screen = None

OBSERVED = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
WALL_TIME = OBSERVED
NOW = 100.0
VALIDITY = 30.0


def write(root, files):
    """Vytvoří syntetické soubory sysfs (adresáře podle potřeby)."""
    for name, value in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{value}\n", encoding="ascii")


class FanCase(unittest.TestCase):
    """Dočasný /sys: hwmon zařízení a chladicí zařízení kernelu."""

    def setUp(self):
        self._temp = TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.hwmon = self.root / "hwmon"
        self.thermal = self.root / "thermal"
        self.hwmon.mkdir()
        self.thermal.mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def device(self, index, name, **files):
        """Zařízení hwmon<index> se jménem ovladače a soubory kanálů."""
        write(self.hwmon, {f"hwmon{index}/name": name,
                           **{f"hwmon{index}/{key}": value for key, value in files.items()}})
        return self.hwmon / f"hwmon{index}"

    def cooling(self, index, kind, current=0, maximum=4):
        write(self.thermal, {f"cooling_device{index}/type": kind,
                             f"cooling_device{index}/cur_state": current,
                             f"cooling_device{index}/max_state": maximum})

    def monitor(self, validity=VALIDITY):
        return FanMonitor(validity, hwmon_dir=self.hwmon, thermal_dir=self.thermal)

    def fan(self, now=NOW):
        """Jediný výsledek kolektoru."""
        results = self.monitor().collect(now, OBSERVED)
        self.assertEqual(len(results), 1)  # jedna kontrola, žádná další veličina
        return results[0]


class MeasuredRpmTest(FanCase):
    """Skutečně naměřené otáčky (jediný doklad o fyzickém otáčení)."""

    def test_positive_rpm_is_info_with_measurement(self):
        self.device(0, "pwmfan", fan1_input=3200, pwm1=153)
        result = self.fan()
        self.assertIs(result.state, State.INFO)
        self.assertEqual(result.measurement, Measurement(3200, RPM))
        self.assertEqual(result.details[FAN_STATE], FAN_ON)
        self.assertEqual(result.details[SOURCE], SOURCE_RPM)
        self.assertEqual(result.details[DEVICE], "pwmfan")
        self.assertIn("ON (3200 RPM)", result.summary)
        self.assertEqual(result.id, CPU_FAN_ID)

    def test_zero_rpm_is_measured_off_not_a_fault(self):
        self.device(0, "pwmfan", fan1_input=0, pwm1=0)
        result = self.fan()
        # Nula je naměřená hodnota, ne chybějící údaj: stav OFF, žádná závada.
        self.assertIs(result.state, State.INFO)
        self.assertEqual(result.measurement, Measurement(0, RPM))
        self.assertEqual(result.details[FAN_STATE], FAN_OFF)
        self.assertEqual(active_problems([result], NOW), [])
        self.assertNotIn(result.state, (State.WARNING, State.ERROR, State.CRITICAL))

    def test_lowest_channel_is_used_deterministically(self):
        self.device(0, "pwmfan", fan2_input=900, fan1_input=1800)
        self.assertEqual(self.fan().measurement, Measurement(1800, RPM))

    def test_rpm_is_never_estimated_from_pwm(self):
        # Bez tachometru se otáčky nedopočítávají z řídicího signálu: stav se zná
        # jen z požadavku řízení a výsledek nemá žádné číslo.
        self.device(0, "pwmfan", pwm1=255)
        result = self.fan()
        self.assertIsNone(result.measurement)
        self.assertIs(result.state, State.INFO)
        self.assertEqual(result.details[SOURCE], SOURCE_REQUEST)
        self.assertEqual(result.details[FAN_STATE], FAN_ON)
        self.assertIn("otáčky neměřené", result.summary)

    def test_details_carry_no_changing_number(self):
        # Do details patří jen kategorické údaje: měnící se RPM by v historii
        # vyrobilo řádek změny při každém měření (R4).
        self.device(0, "pwmfan", fan1_input=3200)
        details = dict(self.fan().details)
        self.assertEqual(set(details), {FAN_STATE, SOURCE, DEVICE})
        for value in details.values():
            self.assertIsInstance(value, str)
            self.assertFalse(value.isdigit())


class RequestTest(FanCase):
    """Stav z požadavku řízení: jen ON/OFF, nikdy tvrzení o otáčení."""

    def test_pwm_above_zero_without_tachometer_is_request_on(self):
        self.device(0, "pwmfan", pwm1=128)
        result = self.fan()
        self.assertIs(result.state, State.INFO)
        self.assertEqual(result.details[FAN_STATE], FAN_ON)
        self.assertEqual(result.details[SOURCE], SOURCE_REQUEST)
        self.assertIsNone(result.measurement)  # žádné procento ani odhad otáček
        self.assertIn("požadavku řízení", result.summary)

    def test_pwm_zero_is_request_off(self):
        self.device(0, "pwmfan", pwm1=0)
        result = self.fan()
        self.assertEqual(result.details[FAN_STATE], FAN_OFF)
        self.assertEqual(result.details[SOURCE], SOURCE_REQUEST)
        self.assertIsNone(result.measurement)

    def test_cooling_device_is_the_second_source(self):
        self.device(0, "pwmfan")  # prokázané zařízení bez pwm i tachometru
        self.cooling(0, "Processor", 0, 3)
        self.cooling(1, "pwm-fan", 2, 4)
        result = self.fan()
        self.assertEqual(result.details[FAN_STATE], FAN_ON)
        self.assertEqual(result.details[DEVICE], "pwm-fan")

    def test_cooling_device_at_zero_is_request_off(self):
        self.cooling(0, "pwm-fan", 0, 4)
        result = self.fan()
        self.assertEqual(result.details[FAN_STATE], FAN_OFF)
        self.assertEqual(result.details[SOURCE], SOURCE_REQUEST)

    def test_cooling_device_works_without_any_hwmon_fan(self):
        self.cooling(0, "pwm-fan", 1, 4)
        self.assertEqual(self.fan().details[FAN_STATE], FAN_ON)

    def test_measured_rpm_has_priority_over_the_request(self):
        # Doložené otáčky mají přednost: PWM by řeklo ON, tachometr říká OFF.
        self.device(0, "pwmfan", fan1_input=0, pwm1=255)
        result = self.fan()
        self.assertEqual(result.details[SOURCE], SOURCE_RPM)
        self.assertEqual(result.details[FAN_STATE], FAN_OFF)
        self.assertEqual(result.measurement, Measurement(0, RPM))

    def test_hwmon_pwm_has_priority_over_cooling_device(self):
        self.device(0, "pwmfan", pwm1=0)
        self.cooling(0, "pwm-fan", 4, 4)
        result = self.fan()
        self.assertEqual(result.details[DEVICE], "pwmfan")
        self.assertEqual(result.details[FAN_STATE], FAN_OFF)

    def test_two_fan_cooling_devices_are_ambiguous(self):
        self.cooling(0, "pwm-fan", 1, 4)
        self.cooling(1, "pwm-fan", 2, 4)
        result = self.fan()
        self.assertIs(result.state, State.UNKNOWN)
        self.assertIn("víc chladicích zařízení", result.summary)

    def test_no_source_at_all_is_na(self):
        self.device(0, "pwmfan")  # ani tachometr, ani pwm, ani chladicí zařízení
        self.cooling(0, "Processor", 1, 3)
        result = self.fan()
        self.assertIs(result.state, State.NA)
        self.assertIn("nenalezen", result.summary)


class IdentityTest(FanCase):
    """Výběr zařízení podle identity, nikdy podle čísla hwmon ani jména souboru."""

    def test_hwmon_number_does_not_decide(self):
        self.device(0, "coretemp")
        self.device(1, "iwlwifi_1_0")
        self.device(7, "pwmfan", fan1_input=2400)
        source = find_cpu_fan(self.hwmon)
        self.assertEqual((source.name, source.channel, source.evidence), ("pwmfan", 1, "hwmon-name"))
        self.assertEqual(self.fan().measurement, Measurement(2400, RPM))

    def test_fan_input_alone_is_not_proof(self):
        # Ventilátor cizího ovladače bez dokladu se nepřiřadí CPU.
        self.device(0, "dell_smm", fan1_input=2500)
        self.assertIsNone(find_cpu_fan(self.hwmon))
        result = self.fan()
        self.assertIs(result.state, State.NA)
        self.assertIn("dell_smm/fan1", result.summary)  # co se našlo, je dohledatelné
        self.assertEqual(other_fans(self.hwmon), ["dell_smm/fan1"])
        self.assertNotIn(FAN_STATE, result.details)

    def test_device_label_is_proof(self):
        self.device(0, "dell_smm", fan1_input=2500, fan1_label="CPU Fan")
        source = find_cpu_fan(self.hwmon)
        self.assertEqual((source.name, source.channel, source.evidence), ("dell_smm", 1, "fan-label"))
        self.assertEqual(self.fan().measurement, Measurement(2500, RPM))

    def test_several_proven_sources_are_unknown_not_a_guess(self):
        self.device(0, "pwmfan", fan1_input=1000)
        self.device(1, "dell_smm", fan2_input=2000, fan2_label="cpu fan")
        with self.assertRaises(Ambiguous):
            find_cpu_fan(self.hwmon)
        result = self.fan()
        self.assertIs(result.state, State.UNKNOWN)
        self.assertIn("víc zdrojů", result.summary)
        self.assertIn("pwmfan/fan1", result.summary)
        self.assertIsNone(result.measurement)

    def test_several_devices_only_one_proven(self):
        self.device(0, "coretemp")
        self.device(1, "acpitz")
        self.device(2, "dell_smm", fan1_input=3000)  # bez dokladu
        self.device(3, "pwmfan", fan1_input=1500)  # prokázané
        self.assertEqual(self.fan().measurement, Measurement(1500, RPM))

    def test_identification_change_after_restart_is_visible(self):
        # Po restartu se zařízení přečíslovalo i přejmenovalo: kolektor hledá
        # znovu při každém měření a změnu identity ukáže v details.
        self.device(3, "pwmfan", fan1_input=1200)
        first = self.fan()
        self.assertEqual(first.details[DEVICE], "pwmfan")
        for entry in sorted(self.hwmon.glob("hwmon*/*")):
            entry.unlink()
        for entry in sorted(self.hwmon.glob("hwmon*")):
            entry.rmdir()
        self.device(0, "dell_smm", fan1_input=2600, fan1_label="cpu_fan")
        second = self.fan()
        self.assertEqual(second.details[DEVICE], "dell_smm")
        self.assertEqual(second.measurement, Measurement(2600, RPM))

    def test_unreadable_device_does_not_stop_the_search(self):
        broken = self.device(0, "pwmfan", fan1_input=1000)
        (broken / "name").unlink()  # zařízení bez jména se přeskočí
        self.device(1, "pwmfan", fan1_input=2000)
        self.assertEqual(self.fan().measurement, Measurement(2000, RPM))


class MissingAndInvalidTest(FanCase):
    """Nedostupná, nečitelná a neplatná data."""

    def test_missing_hwmon_directory_is_na(self):
        monitor = FanMonitor(VALIDITY, hwmon_dir=self.root / "neni", thermal_dir=self.root / "taky-ne")
        results = monitor.collect(NOW, OBSERVED)
        self.assertEqual(len(results), 1)
        self.assertIs(results[0].state, State.NA)
        self.assertEqual(active_problems(results, NOW), [])  # není co sledovat, ne závada

    def test_empty_hwmon_is_na(self):
        self.assertIs(self.fan().state, State.NA)

    def test_unreadable_rpm_is_unknown(self):
        device = self.device(0, "pwmfan", fan1_input=1500)
        (device / "fan1_input").unlink()
        (device / "fan1_input").mkdir()  # adresář místo souboru: čtení skončí OSError
        result = self.fan()
        self.assertIs(result.state, State.UNKNOWN)
        self.assertIn("nedostupné", result.summary)
        self.assertEqual(result.details[DEVICE], "pwmfan")

    def test_invalid_values_are_unknown(self):
        for value in ("", "x", "1200 rpm", "-5", "999999", "1e9"):
            with self.subTest(value=value):
                self.setUp()
                self.device(0, "pwmfan", fan1_input=value)
                result = self.fan()
                self.assertIs(result.state, State.UNKNOWN)
                self.assertIsNone(result.measurement)

    def test_invalid_request_values_are_unknown(self):
        for files in ({"pwm1": "300"}, {"pwm1": "-1"}, {"pwm1": "nic"}):
            with self.subTest(files=files):
                self.setUp()
                self.device(0, "pwmfan", **files)  # bez tachometru rozhoduje požadavek
                result = self.fan()
                self.assertIs(result.state, State.UNKNOWN)
                self.assertIsNone(result.measurement)

    def test_invalid_cooling_state_is_unknown(self):
        self.cooling(0, "pwm-fan", current=5, maximum=4)
        self.assertIs(self.fan().state, State.UNKNOWN)

    def test_parsers_refuse_nonsense(self):
        self.assertEqual(parse_rpm(" 1200\n"), 1200)
        self.assertEqual(parse_pwm_percent("255"), 100)
        self.assertEqual(parse_cooling_percent("1", "4"), 25)
        for text in ("-1", "40000", "abc", ""):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_rpm(text)
        with self.assertRaises(ValueError):
            parse_pwm_percent("256")
        for current, maximum in (("1", "0"), ("5", "4"), ("-1", "4")):
            with self.subTest(current=current):
                with self.assertRaises(ValueError):
                    parse_cooling_percent(current, maximum)


class ResultContractTest(FanCase):
    """Model CheckResult: platnost, expirace, simulace a popisky."""

    def test_expiry_turns_values_into_unkn(self):
        self.device(0, "pwmfan", fan1_input=3000, pwm1=100)
        results = list(self.monitor().collect(NOW, OBSERVED))
        for result in results:
            self.assertEqual(result.expires_monotonic, NOW + VALIDITY)
        rows = dict(page_rows("SYSTEM", results, NOW))
        self.assertEqual(rows["CPU FAN"], "ON 3000")
        # Po vypršení platnosti se stará hodnota ani stav nikdy nepoužije.
        expired = dict(page_rows("SYSTEM", results, NOW + VALIDITY + 1))
        self.assertEqual(expired["CPU FAN"], "UNKN")
        self.assertEqual(overall_state(results, NOW + VALIDITY + 1), State.UNKNOWN)

    def test_synthetic_paths_are_simulated_data(self):
        self.device(0, "pwmfan", fan1_input=1000)
        for result in self.monitor().collect(NOW, OBSERVED):
            self.assertTrue(result.simulated)

    def test_real_paths_are_not_simulated(self):
        # Skutečné cesty se jen porovnají; nic se z nich v testu nečte jako hodnota.
        monitor = FanMonitor(VALIDITY)
        self.assertFalse(monitor.simulated)
        self.assertEqual((monitor.hwmon_dir, monitor.thermal_dir),
                         (Path("/sys/class/hwmon"), Path("/sys/class/thermal")))

    def test_labels_cover_returned_ids(self):
        # run.failed_results staví UNKNOWN výsledky podle labels kolektoru.
        self.device(0, "pwmfan", fan1_input=1000)
        returned = [result.id for result in self.monitor().collect(NOW, OBSERVED)]
        self.assertEqual(returned, list(LABELS))
        self.assertEqual(set(LABELS), {CPU_FAN_ID})  # jediná kontrola

    def test_validity_must_be_positive(self):
        for value in (0, -1, None, "30", True, float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    FanMonitor(value, hwmon_dir=self.hwmon, thermal_dir=self.thermal)

    def test_collector_never_writes_to_sysfs(self):
        self.device(0, "pwmfan", fan1_input=2000, pwm1=120)
        self.cooling(0, "pwm-fan", 1, 4)
        before = {path: path.read_bytes() for path in sorted(self.root.rglob("*")) if path.is_file()}
        listing = sorted(str(path) for path in self.root.rglob("*"))
        for _ in range(3):
            self.monitor().collect(NOW, OBSERVED)
        self.assertEqual(sorted(str(path) for path in self.root.rglob("*")), listing)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)


class SystemPageTest(FanCase):
    """Čtvrtý řádek stránky SYSTEM a hodnota řádku CPU FAN."""

    def rows(self, now=NOW):
        return dict(page_rows("SYSTEM", list(self.monitor().collect(now, OBSERVED)), now))

    def test_four_rows_in_fixed_order(self):
        self.device(0, "pwmfan", fan1_input=3200, pwm1=153)
        names = [name for name, _ in page_rows("SYSTEM", list(self.monitor().collect(NOW, OBSERVED)), NOW)]
        self.assertEqual(names, ["CPU", "CPU TEMP", "RAM", "CPU FAN"])

    def test_measured_on_shows_state_with_the_number(self):
        self.device(0, "pwmfan", fan1_input=3200)
        self.assertEqual(self.rows()["CPU FAN"], f"{FAN_ON} 3200")

    def test_measured_off_shows_only_the_state(self):
        self.device(0, "pwmfan", fan1_input=0)
        self.assertEqual(self.rows()["CPU FAN"], FAN_OFF)

    def test_request_only_is_marked_as_request(self):
        for pwm, expected in ((153, FAN_ON), (0, FAN_OFF)):
            with self.subTest(pwm=pwm):
                self.setUp()
                self.device(0, "pwmfan", pwm1=pwm)
                self.assertEqual(self.rows()["CPU FAN"], f"{REQUEST_PREFIX} {expected}")

    def test_request_from_cooling_device_is_marked_too(self):
        self.cooling(0, "pwm-fan", 2, 4)
        self.assertEqual(self.rows()["CPU FAN"], f"{REQUEST_PREFIX} {FAN_ON}")

    def test_no_source_is_na(self):
        self.assertEqual(self.rows()["CPU FAN"], "N/A")

    def test_ambiguous_source_is_unkn(self):
        self.device(0, "pwmfan", fan1_input=1000)
        self.device(1, "dell_smm", fan1_input=2000, fan1_label="cpu fan")
        self.assertEqual(self.rows()["CPU FAN"], "UNKN")

    def test_missing_result_is_unkn(self):
        self.assertEqual(fan_value(None, NOW), "UNKN")

    def test_expired_request_state_is_unkn(self):
        # Prošlý stav se nikdy neukazuje jako platný: platnost výsledku rozhoduje.
        self.device(0, "pwmfan", pwm1=153)
        results = list(self.monitor().collect(NOW, OBSERVED))
        self.assertEqual(dict(page_rows("SYSTEM", results, NOW))["CPU FAN"],
                         f"{REQUEST_PREFIX} {FAN_ON}")
        self.assertEqual(dict(page_rows("SYSTEM", results, NOW + VALIDITY + 1))["CPU FAN"], "UNKN")

    def test_no_source_stays_na_even_later(self):
        # Bez jakéhokoli zdroje zůstává N/A: platnost se u N/A nepoužívá.
        self.assertEqual(dict(page_rows("SYSTEM", list(self.monitor().collect(NOW, OBSERVED)),
                                        NOW + VALIDITY + 1))["CPU FAN"], "N/A")

    def test_status_shows_the_fan_name(self):
        self.device(0, "pwmfan", fan1_input=0, pwm1=153)
        result = self.monitor().collect(NOW, OBSERVED)[0]
        self.assertEqual(status_texts(replace(result, state=State.UNKNOWN, measurement=None),
                                      NOW)[-1], "CPU FAN")


@unittest.skipIf(Image is None, "Pillow není dostupné")
class RenderTest(FanCase):
    """Skutečný renderer: čtyři řádky se vejdou do 128 × 64 ve všech stavech."""

    def page(self, rows):
        image = Image.new("1", (render.DISPLAY_WIDTH, render.DISPLAY_HEIGHT), 0)
        render.draw_value_page(image, "SYSTEM", rows, False)
        return image

    def row_boxes(self, image):
        """Obálky inkoustu jednotlivých řádků pod čarou záhlaví."""
        boxes = []
        for index in range(4):
            top = render.SERVICES_ROW_TOP + index * render.SERVICES_ROW_STEP
            boxes.append(image.crop((0, top, render.DISPLAY_WIDTH, top + 13)).getbbox())
        return boxes

    def test_four_rows_fit_and_do_not_overlap(self):
        for value in (f"{FAN_ON} 3200", FAN_OFF, f"{REQUEST_PREFIX} {FAN_ON}", "N/A", "UNKN"):
            with self.subTest(value=value):
                image = self.page([("CPU", "23%"), ("CPU TEMP", "48°C"), ("RAM", "24%"), ("CPU FAN", value)])
                self.assertEqual(image.size, (128, 64))
                ink = image.getbbox()
                self.assertLessEqual(ink[3], render.DISPLAY_HEIGHT)  # nic nepřeteče dolů
                boxes = self.row_boxes(image)
                self.assertTrue(all(box is not None for box in boxes), boxes)
                self.assertEqual(len(boxes), 4)

    def test_value_is_right_aligned_with_a_gap(self):
        image = self.page([("CPU", "23%"), ("CPU TEMP", "48°C"), ("RAM", "24%"), ("CPU FAN", f"{FAN_ON} 3200")])
        top = render.SERVICES_ROW_TOP + 3 * render.SERVICES_ROW_STEP
        label = image.crop((0, top, 64, top + 13)).getbbox()
        value = image.crop((64, top, 128, top + 13)).getbbox()
        self.assertIsNotNone(value)
        self.assertLessEqual(label[2] + render.LABEL_GAP, 64 + value[0])
        self.assertLessEqual(64 + value[2], render.RIGHT_EDGE + 1)

    def test_three_row_pages_keep_their_layout(self):
        # Stránka DISK (jeden řádek) se nesmí posunout: rozteč 15 px platí dál.
        image = Image.new("1", (128, 64), 0)
        render.draw_value_page(image, "DISK", [("ROOT", "31%")], False)
        box = image.crop((0, render.VALUE_ROW_TOP, 128, render.VALUE_ROW_TOP + 13)).getbbox()
        self.assertIsNotNone(box)

    def test_whole_screen_in_every_fan_state(self):
        states = {
            "ON s otáčkami": {"hwmon0/name": "pwmfan", "hwmon0/fan1_input": 3200},
            "OFF z otáček": {"hwmon0/name": "pwmfan", "hwmon0/fan1_input": 0},
            "REQ ON": {"hwmon0/name": "pwmfan", "hwmon0/pwm1": 153},
            "REQ OFF": {"hwmon0/name": "pwmfan", "hwmon0/pwm1": 0},
            "UNKN": {"hwmon0/name": "pwmfan", "hwmon0/fan1_input": "x"},
            "N/A": {"hwmon0/name": "coretemp"},
        }
        controller = OledController(OledConfig(), pages=("SYSTEM",))
        controller.press(NOW, 0)
        for name, files in states.items():
            with self.subTest(state=name):
                self.setUp()
                write(self.hwmon, files)
                results = list(self.monitor().collect(NOW, OBSERVED))
                image = render_screen(controller, NOW, results, WALL_TIME)
                self.assertEqual(image.size, (128, 64))
                self.assertIsNotNone(image.getbbox())  # něco se nakreslilo
                self.assertLessEqual(image.getbbox()[3], 64)


class OtherPagesTest(FanCase):
    """Ostatní stránky a alarmy zůstávají beze změny."""

    def setUp(self):
        super().setUp()
        self.device(0, "pwmfan", fan1_input=3200, pwm1=153)
        self.fan_results = list(self.monitor().collect(NOW, OBSERVED))

    def test_disk_page_has_one_row(self):
        disk = CheckResult("disk.root", State.INFO, "Disk / 31 %", OBSERVED, NOW + VALIDITY,
                           simulated=False, measurement=Measurement(31, PERCENT))
        self.assertEqual(page_rows("DISK", [*self.fan_results, disk], NOW), [("ROOT", "31%")])

    def test_services_page_ignores_fan_results(self):
        self.assertEqual([name for name, _ in service_rows(self.fan_results, NOW)],
                         ["NEXUS", "ANIMEDB", "NAS", "BACKUP"])

    def test_fan_result_adds_no_problem_and_no_symbol(self):
        self.assertEqual(active_problems(self.fan_results, NOW), [])
        self.assertIsNone(home_symbol(self.fan_results, NOW))
        self.assertEqual(overall_state(self.fan_results, NOW), State.INFO)

    def test_na_results_are_not_problems_either(self):
        self.setUp()
        for entry in sorted(self.hwmon.rglob("*"), reverse=True):
            entry.unlink() if entry.is_file() else entry.rmdir()
        results = list(self.monitor().collect(NOW, OBSERVED))
        self.assertEqual({result.state for result in results}, {State.NA})
        self.assertEqual(active_problems(results, NOW), [])
        # Samotné N/A nejsou sledovaná data, proto symbol „?“ (core.overall_state).
        # Vedle platného měření jiného kolektoru ale ventilátor symbol nepřidá.
        measured = CheckResult(CPU_USAGE_ID, State.INFO, "CPU 20 %", OBSERVED, NOW + VALIDITY,
                               simulated=True, measurement=Measurement(20, PERCENT))
        self.assertIsNone(home_symbol([*results, measured], NOW))
        self.assertEqual(active_problems([*results, measured], NOW), [])

    def test_ambiguous_source_is_a_problem(self):
        self.device(1, "dell_smm", fan1_input=2000, fan1_label="cpu fan")
        results = list(self.monitor().collect(NOW, OBSERVED))
        problems = active_problems(results, NOW)
        self.assertEqual([problem.id for problem in problems], [CPU_FAN_ID])
        self.assertIs(problems[0].state, State.UNKNOWN)

    def test_collector_adds_no_alarm_states(self):
        # Kolektor nikdy nevyhodnocuje závadu: ani při rozporu požadavek > 0 a 0 RPM.
        self.setUp()
        self.device(0, "pwmfan", fan1_input=0, pwm1=255)
        results = list(self.monitor().collect(NOW, OBSERVED))
        self.assertEqual({result.state for result in results}, {State.INFO})
        self.assertEqual(active_problems(results, NOW), [])


if __name__ == "__main__":
    unittest.main()
