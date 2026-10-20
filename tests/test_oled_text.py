"""Testy anglických textů OLED (oled_text) a jejich vykreslení.

Texty vznikají jen z id, stavu a číselné hodnoty, nikdy ze `summary`. Testy
textů nepotřebují Pillow; testy vykreslení se bez Pillow přeskočí. Čas je
virtuální, nic se nečeká.
"""

import hashlib
import unittest
from datetime import datetime, timezone
from unittest import mock

from nexusstat.alarms import AlarmEvaluator
from nexusstat.config import AlarmRule, AlarmsConfig, Config
from nexusstat.core import CheckResult, Measurement, State, active_problems, effective
from nexusstat.network_monitor import (
    ADDRESS,
    BYTES_PER_SECOND,
    INTERFACE,
    IPV4_ID,
    LABELS as NETWORK_LABELS,
    LINK,
    LINK_ID,
    RX_RATE_ID,
    TX_RATE_ID,
)
from nexusstat.oled_text import (
    CHECK_NAMES,
    FAULT_TEXT,
    NO_DATA,
    STALE,
    STATE_TEXT,
    check_code,
    check_name,
    measurement_text,
    network_rows,
    page_rows,
    rate_text,
    service_rows,
    status_texts,
    value_text,
)
from nexusstat.preview import LIVE, SCENARIOS, PreviewSession, SessionClock
from nexusstat.services import ANIMEDB_ID, BACKUP_ID, NAS_ID, NEXUS_SERVICES_ID, SERVICE_LABELS, complete_service_results
from nexusstat.system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, LABELS as SYSTEM_LABELS, MEMORY_ID

try:
    from PIL import Image, ImageDraw

    from nexusstat import render
except ImportError:  # Pillow není nainstalované
    render = None

OBSERVED = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
WALL = datetime(2026, 10, 19, 14, 5)

# Znaky, které smí displej ukázat: ASCII bez malých písmen, ° a výpustka u zkrácení.
ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 %°/:._-?!…")
# Jediná výslovná výjimka s malým písmenem: jednotky rychlosti na stránce NETWORK.
RATE_UNITS = ("B/s", "KB/s", "MB/s")


def drawn_chars(text):
    """Znaky textu displeje; malé "s" je povolené jen v jednotce rychlosti na konci."""
    for unit in RATE_UNITS:
        if text.endswith(" " + unit):
            return set(text[:-len(unit)] + unit.upper())
    return set(text)


def result(check_id, state, value=None, unit="°C", expires=1000, summary="Český popis, který displej nečte",
           details=None):
    measurement = Measurement(value, unit) if value is not None else None
    return CheckResult(check_id, state, summary, OBSERVED, expires, simulated=False, measurement=measurement,
                       details=details or {})


def status_line(problem, now):
    """(stav, nejúplnější popis) jednoho řádku STATUS jako v rendereru, bez vykreslení."""
    return STATE_TEXT[effective(problem, now).state], status_texts(problem, now)[0]


class NamesTest(unittest.TestCase):
    def test_known_names_are_short_english_and_unique(self):
        self.assertEqual(check_name(CPU_USAGE_ID), "CPU")
        self.assertEqual(check_name(CPU_TEMP_ID), "CPU TEMP")
        self.assertEqual(check_name(MEMORY_ID), "RAM")
        self.assertEqual(check_name(DISK_ROOT_ID), "ROOT")
        self.assertEqual([check_name(i) for i in SERVICE_LABELS], ["NEXUS", "ANIMEDB", "NAS", "BACKUP"])
        names = list(CHECK_NAMES.values())
        self.assertEqual(len(names), len(set(names)))  # dva řádky STATUS nevypadají stejně
        for text in names + list(FAULT_TEXT.values()):
            with self.subTest(text=text):
                self.assertLessEqual(set(text), ALLOWED)

    def test_unknown_id_fallback_is_generic_name_with_traceable_code(self):
        # Pevná hodnota: kód z displeje musí jít dohledat i v dalších verzích.
        self.assertEqual(check_name("docker.container_restarts"), "CHECK 0D51")
        for check_id in ("docker.container_restarts", "teplota.čidla_skříně", "ěščřžýáíé", "温度", "x"):
            expected = hashlib.sha256(check_id.encode("utf-8")).hexdigest()[:4].upper()
            with self.subTest(check_id=check_id):
                self.assertEqual(check_name(check_id), f"CHECK {expected}")
                self.assertEqual(check_code(check_id), expected)
                self.assertLessEqual(set(check_name(check_id)), ALLOWED)  # žádná čeština ani id
        self.assertEqual(check_name("teplota.čidla_skříně"), check_name("teplota.čidla_skříně"))  # deterministické
        self.assertNotEqual(check_name("disk.a"), check_name("disk.b"))
        self.assertNotIn("CHECK", " ".join(CHECK_NAMES.values()))  # známé kontroly fallback nepoužijí

    def test_root_mode_name_and_read_only_fault(self):
        self.assertEqual(check_name("disk.root_readonly"), "ROOT MODE")
        self.assertEqual(status_texts(result("disk.root_readonly", State.UNKNOWN, expires=5), 10)[0], "ROOT MODE STALE")
        self.assertEqual(status_texts(result("disk.root_readonly", State.CRITICAL), 10)[0], "ROOT READ-ONLY")

    def test_unknown_id_keeps_severity_and_never_shows_summary(self):
        for state in (State.CRITICAL, State.ERROR, State.WARNING, State.UNKNOWN):
            problem = result("nas.share_media", state, summary="NAS: sdílená složka chybí")
            with self.subTest(state=state):
                texts = status_texts(problem, 10)
                self.assertEqual(texts[-1], check_name("nas.share_media"))
                self.assertTrue(texts[-1].startswith("CHECK "))
                self.assertNotIn("sdílená", " ".join(texts))
                self.assertEqual(STATE_TEXT[problem.state], STATE_TEXT[state])  # závažnost zůstává ve sloupci


class ValueTextTest(unittest.TestCase):
    def test_measurement_rounding_matches_collector_summary(self):
        # Stejné zaokrouhlení jako summary kolektoru (:.0f, polovina k sudé).
        for value in (0.4, 23.0, 47.5, 48.5, 69.5, 70.5, 99.6, 100):
            with self.subTest(value=value):
                self.assertEqual(measurement_text(Measurement(value, "°C")), f"{value:.0f}°C")
                self.assertEqual(measurement_text(Measurement(value, "%")), f"{value:.0f}%")

    def test_unit_is_sanitized(self):
        self.assertEqual(measurement_text(Measurement(5, "ms")), "5ms")
        self.assertEqual(measurement_text(Measurement(5, "Kč")), "5K")  # nic mimo ASCII a °

    def test_page_rows_use_measurement_not_summary(self):
        results = [
            result(CPU_USAGE_ID, State.INFO, 23.2, "%", summary="CPU 99 %"),  # zavádějící popis
            result(CPU_TEMP_ID, State.ERROR, 88.4, summary="Teplota CPU 20 °C"),
            result(MEMORY_ID, State.INFO, 24.4, "%"),
            result(DISK_ROOT_ID, State.WARNING, 91.0, "%"),
        ]
        self.assertEqual(page_rows("SYSTEM", results, 10), [("CPU", "23%"), ("CPU TEMP", "88°C"), ("RAM", "24%")])
        self.assertEqual(page_rows("DISK", results, 10), [("ROOT", "91%")])
        self.assertEqual(page_rows("NEXUS", results, 10), [])

    def test_missing_expired_and_unknown_values_are_unkn_never_old_number(self):
        fresh = [result(CPU_TEMP_ID, State.INFO, 48, expires=30)]
        self.assertEqual(page_rows("SYSTEM", fresh, 29.9)[1], ("CPU TEMP", "48°C"))
        self.assertEqual(page_rows("SYSTEM", fresh, 30)[1], ("CPU TEMP", "UNKN"))  # prošlé
        self.assertEqual(page_rows("SYSTEM", [], 0)[1], ("CPU TEMP", "UNKN"))  # chybí
        self.assertEqual(value_text(result(CPU_TEMP_ID, State.UNKNOWN), 0), "UNKN")
        # N/A jen pro záměrně vypnuté, ne pro neznámou hodnotu.
        self.assertEqual(value_text(result(CPU_TEMP_ID, State.NA), 0), "N/A")

    def test_value_without_number_shows_state_never_ok_by_default(self):
        self.assertEqual(value_text(result(CPU_TEMP_ID, State.ERROR), 0), "ERR")
        self.assertEqual(value_text(None, 0), "UNKN")


class StatusTextTest(unittest.TestCase):
    def test_confirmed_temperature_alarm_shows_measured_value_and_state(self):
        rule = AlarmRule(enabled=True, warning=70, error=80, hysteresis=5)
        evaluator = AlarmEvaluator(AlarmsConfig(cpu_temp=rule))
        for now in (0, 10, 20):
            [evaluated] = evaluator.evaluate([result(CPU_TEMP_ID, State.INFO, 88.4, expires=now + 30)], now)
        self.assertEqual(status_line(evaluated, 20), ("ERR", "CPU TEMP 88°C"))

    def test_nas_without_data_is_unknown_not_fault(self):
        [nas] = [r for r in complete_service_results([], OBSERVED, 1000) if r.id == NAS_ID]
        self.assertEqual(status_line(nas, 10), ("UNKN", "NAS NO DATA"))
        self.assertEqual(service_rows([nas], 10)[2], ("NAS", "UNKN"))
        self.assertEqual(status_line(result(NAS_ID, State.UNKNOWN), 10), ("UNKN", "NAS NO DATA"))

    def test_nas_proven_unreachable_is_offline_with_fault_state(self):
        for state in (State.CRITICAL, State.ERROR):
            with self.subTest(state=state):
                self.assertEqual(status_line(result(NAS_ID, state), 10), (STATE_TEXT[state], "NAS OFFLINE"))
        # OFFLINE jen u potvrzené závady, nikdy u neznámého ani N/A stavu.
        for state in (State.UNKNOWN, State.NA, State.OK):
            self.assertNotIn("OFFLINE", status_texts(result(NAS_ID, state), 10)[0])

    def test_animedb_not_deployed_is_na_not_unknown_or_error(self):
        completed = complete_service_results([], OBSERVED, 1000)
        self.assertEqual(service_rows(completed, 10)[1], ("ANIMEDB", "N/A"))
        self.assertNotIn(ANIMEDB_ID, {p.id for p in active_problems(completed, 10)})  # ve STATUS není
        self.assertEqual(status_texts(result(ANIMEDB_ID, State.NA), 10)[0], "ANIMEDB N/A")

    def test_stale_and_no_data_are_told_apart_by_time(self):
        backup = result(BACKUP_ID, State.OK, expires=40)
        self.assertEqual(status_line(backup, 40), ("UNKN", "BACKUP STALE"))  # ne OK, ne OVERDUE
        self.assertEqual(status_line(result(BACKUP_ID, State.UNKNOWN, expires=40), 39), ("UNKN", "BACKUP NO DATA"))
        # Prošlý alarm: UNKNOWN, ne trvající závada se starou hodnotou.
        old_alarm = result(CPU_TEMP_ID, State.ERROR, 88, expires=40)
        self.assertEqual(status_line(old_alarm, 40), ("UNKN", f"CPU TEMP {STALE}"))

    def test_backup_fault_differs_from_stale_backup_data(self):
        self.assertEqual(status_line(result(BACKUP_ID, State.WARNING), 10), ("WARN", "BACKUP OVERDUE"))
        self.assertNotEqual(status_texts(result(BACKUP_ID, State.ERROR), 10),
                            status_texts(result(BACKUP_ID, State.OK, expires=5), 10))

    def test_user_examples(self):
        cases = {
            ("system.time_sync", State.ERROR, None): "TIME UNSYNCED",
            ("disk.root_readonly", State.CRITICAL, None): "ROOT READ-ONLY",
            (NEXUS_SERVICES_ID, State.UNKNOWN, None): "NEXUS NO DATA",
            (DISK_ROOT_ID, State.WARNING, 42): "ROOT 42%",
            (CPU_USAGE_ID, State.WARNING, 97): "CPU 97%",
        }
        for (check_id, state, value), expected in cases.items():
            with self.subTest(check_id=check_id):
                unit = "%" if check_id in (DISK_ROOT_ID, CPU_USAGE_ID) else "°C"
                self.assertEqual(status_texts(result(check_id, state, value, unit), 10)[0], expected)

    def test_fault_without_known_text_keeps_name(self):
        self.assertEqual(status_texts(result(NEXUS_SERVICES_ID, State.ERROR), 10), ["NEXUS"])
        self.assertEqual(status_texts(result("docker.engine", State.CRITICAL), 10), ["DOCKER"])

    def test_last_candidate_is_always_the_name(self):
        for check_id in list(CHECK_NAMES) + ["x.unknown_check"]:
            for state in State:
                value = 50 if state not in (State.UNKNOWN, State.NA) else None
                with self.subTest(check_id=check_id, state=state):
                    texts = status_texts(result(check_id, state, value), 10)
                    self.assertEqual(texts[-1], check_name(check_id))

    def test_texts_do_not_depend_on_summary(self):
        # Mutace popisu nesmí změnit nic na displeji: překlad je podle id a hodnoty.
        for check_id in list(CHECK_NAMES) + ["x.unknown_check"]:
            for state in State:
                value = 61.5 if state not in (State.UNKNOWN, State.NA) else None
                a = result(check_id, state, value, summary="Záloha starší 3 dny")
                b = result(check_id, state, value, summary="NAS OK, vše v pořádku")
                with self.subTest(check_id=check_id, state=state):
                    self.assertEqual(status_texts(a, 10), status_texts(b, 10))
                    self.assertEqual(value_text(a, 10), value_text(b, 10))
                    self.assertEqual(service_rows([a], 10), service_rows([b], 10))

    def test_presentation_does_not_change_results(self):
        original = result(CPU_TEMP_ID, State.ERROR, 88, expires=5)
        before = repr(original)
        status_texts(original, 10)
        value_text(original, 10)
        page_rows("SYSTEM", [original], 10)
        self.assertEqual(repr(original), before)
        self.assertIs(original.state, State.ERROR)


class NetworkTextTest(unittest.TestCase):
    """Texty stránky NETWORK: jen z id, stavu, measurement a details, nikdy ze summary."""

    def network(self, link="UP", rx=1500.0, tx=0.0, address="192.0.2.10", interface="eth0", expires=1000):
        on_interface = {INTERFACE: interface}
        return [
            result(LINK_ID, State.INFO, details={**on_interface, LINK: link}, expires=expires),
            result(RX_RATE_ID, State.INFO, rx, BYTES_PER_SECOND, details=on_interface, expires=expires),
            result(TX_RATE_ID, State.INFO, tx, BYTES_PER_SECOND, details=on_interface, expires=expires),
            result(IPV4_ID, State.INFO, details={**on_interface, **({ADDRESS: address} if address else {})},
                   expires=expires),
        ]

    def test_rate_units_and_boundaries(self):
        cases = {
            0: "0 B/s", 0.4: "0 B/s", 999.4: "999 B/s",
            999.6: "1.0 KB/s",  # zaokrouhlení by dalo "1000 B/s": jednotka se volí až po něm
            1000: "1.0 KB/s", 9_940: "9.9 KB/s", 9_960: "10 KB/s", 85_300: "85 KB/s", 999_400: "999 KB/s",
            999_600: "1.0 MB/s", 1_234_000: "1.2 MB/s", 112_400_000: "112 MB/s", 999_400_000: "999 MB/s",
            1_250_000_000: "1250 MB/s",  # 10 Gbit/s: nad MB/s už jednotka není
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(rate_text(value), expected)
                self.assertEqual(measurement_text(Measurement(value, BYTES_PER_SECOND)), expected)

    def test_decimal_units_not_binary(self):
        # 1 KB/s = 1000 B/s (jako u síťových rychlostí), ne 1024.
        self.assertEqual(rate_text(1024), "1.0 KB/s")
        self.assertEqual(rate_text(2_000_000), "2.0 MB/s")

    def test_rows_from_valid_results(self):
        self.assertEqual(network_rows(self.network(), 0),
                         [("ETH0", "UP"), ("RX", "1.5 KB/s"), ("TX", "0 B/s"), ("IPV4", "192.0.2.10")])
        self.assertEqual(network_rows(self.network(link="DOWN", address=None, interface="enp0s31f6"), 0),
                         [("ENP0S31F6", "DOWN"), ("RX", "1.5 KB/s"), ("TX", "0 B/s"), ("IPV4", "NONE")])

    def test_rows_never_depend_on_summary(self):
        rows = network_rows(self.network(), 0)
        for item in self.network():
            self.assertNotIn("Český", "".join(label + value for label, value in rows))
            self.assertEqual(item.summary, "Český popis, který displej nečte")

    def test_missing_results_are_unkn(self):
        self.assertEqual(network_rows([], 0), [("LINK", "UNKN"), ("RX", "UNKN"), ("TX", "UNKN"), ("IPV4", "UNKN")])

    def test_expired_results_are_unkn_without_old_interface_or_address(self):
        rows = network_rows(self.network(expires=10), 10)
        self.assertEqual(rows, [("LINK", "UNKN"), ("RX", "UNKN"), ("TX", "UNKN"), ("IPV4", "UNKN")])
        self.assertEqual(effective(self.network(expires=10)[3], 10).details, {})

    def test_unknown_with_known_interface(self):
        # Nastavené rozhraní neexistuje: víme jeho název, stav ne.
        eth1 = {INTERFACE: "eth1"}
        unknown = [result(check_id, State.UNKNOWN, details=eth1) for check_id in NETWORK_LABELS]
        self.assertEqual(network_rows(unknown, 0), [("ETH1", "UNKN"), ("RX", "UNKN"), ("TX", "UNKN"), ("IPV4", "UNKN")])

    def test_unknown_ipv4_differs_from_no_ipv4(self):
        no_address = self.network(address=None)
        unknown = no_address[:3] + [result(IPV4_ID, State.UNKNOWN, details={INTERFACE: "eth0"})]
        self.assertEqual(network_rows(no_address, 0)[3], ("IPV4", "NONE"))
        self.assertEqual(network_rows(unknown, 0)[3], ("IPV4", "UNKN"))

    def test_link_without_value_is_unkn_and_na_stays_na(self):
        without_value = [result(LINK_ID, State.INFO, details={INTERFACE: "eth0"})]
        self.assertEqual(network_rows(without_value, 0)[0], ("ETH0", "UNKN"))
        disabled = [result(check_id, State.NA) for check_id in NETWORK_LABELS]
        self.assertEqual(network_rows(disabled, 0), [("LINK", "N/A"), ("RX", "N/A"), ("TX", "N/A"), ("IPV4", "N/A")])

    def test_no_web_address_is_shown(self):
        # Dokud NexusSTAT nemá běžící web, je na stránce jen samotná IP, bez portu.
        texts = "".join(label + value for label, value in network_rows(self.network(), 0))
        self.assertNotIn("8765", texts)
        self.assertNotIn("HTTP", texts)
        self.assertEqual(network_rows(self.network(), 0)[3][1], "192.0.2.10")

    def test_status_texts(self):
        self.assertEqual([check_name(i) for i in NETWORK_LABELS], ["LINK", "NET RX", "NET TX", "IPV4"])
        self.assertEqual(status_line(result(RX_RATE_ID, State.UNKNOWN), 0), ("UNKN", "NET RX NO DATA"))
        self.assertEqual(status_line(self.network(expires=10)[0], 10), ("UNKN", "LINK STALE"))
        # Potvrzený alarm (zatím žádné pravidlo neexistuje) by ukázal naměřenou rychlost.
        self.assertEqual(status_line(result(TX_RATE_ID, State.WARNING, 112_400_000, BYTES_PER_SECOND), 0),
                         ("WARN", "NET TX 112 MB/s"))
        # Platná síťová měření nejsou problém, ani linka DOWN bez IPv4.
        self.assertEqual(active_problems(self.network(link="DOWN", address=None), 0), [])


class FakeRealClock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


class FakeMonitor:
    """Skutečná (ne simulovaná) data pro LIVE bez čtení /proc a /sys/class/net."""

    simulated = False
    labels = {**SYSTEM_LABELS, **NETWORK_LABELS}

    def __init__(self, config):
        self.validity_seconds = config.collector.validity_seconds

    def collect(self, now, observed_utc):
        def real(check_id, state, value=None, **details):
            return CheckResult(check_id, state, "česky", observed_utc, now + self.validity_seconds, simulated=False,
                               measurement=Measurement(*value) if value else None, details=details)

        values = {CPU_USAGE_ID: (7.4, "%"), CPU_TEMP_ID: (52.6, "°C"), MEMORY_ID: (31.0, "%")}
        wifi = {INTERFACE: "wlp2s0"}
        return [real(check_id, State.INFO, value) for check_id, value in values.items()] + [
            real(DISK_ROOT_ID, State.UNKNOWN),  # Disk / se nepodařilo změřit.
            real(LINK_ID, State.INFO, **wifi, **{LINK: "UP"}),
            real(RX_RATE_ID, State.INFO, (12_345.6, BYTES_PER_SECOND), **wifi),
            real(TX_RATE_ID, State.INFO, (2_100, BYTES_PER_SECOND), **wifi),
            real(IPV4_ID, State.INFO, **wifi, **{ADDRESS: "192.0.2.77"}),  # dokumentační rozsah
        ]


def session(scenario):
    real = FakeRealClock()
    preview_session = PreviewSession(Config(), scenario=scenario, clock=SessionClock(real_clock=real),
                                     utc_clock=lambda: OBSERVED, wall_clock=lambda: WALL, monitor_factory=FakeMonitor)
    return preview_session, real


class ScenarioTextTest(unittest.TestCase):
    """Řádky STATUS ve scénářích simulátoru: krátké, anglické, v pořadí CRIT → ERR → WARN → UNKN."""

    def lines(self, scenario):
        preview_session, _ = session(scenario)
        now = preview_session.clock.now()
        return [status_line(problem, now) for problem in active_problems(preview_session.results, now)]

    def test_many_problems(self):
        self.assertEqual(self.lines("many"), [
            ("CRIT", "NAS OFFLINE"),
            ("ERR", "BACKUP OVERDUE"),
            ("ERR", "CPU TEMP 88°C"),
            ("WARN", "ROOT 91%"),
            ("WARN", "CPU 97%"),
            ("UNKN", "NEXUS NO DATA"),
        ])

    def test_other_scenarios(self):
        self.assertEqual(self.lines("ok"), [])
        self.assertEqual(self.lines("warning"), [("WARN", "CPU TEMP 74°C")])
        self.assertEqual(self.lines("error"), [("CRIT", "NAS OFFLINE"), ("ERR", "CPU TEMP 86°C")])
        self.assertEqual(self.lines("unconnected"),
                         [("UNKN", "BACKUP NO DATA"), ("UNKN", "NAS NO DATA"), ("UNKN", "NEXUS NO DATA")])
        self.assertEqual(self.lines(LIVE), [("UNKN", "ROOT NO DATA"), ("UNKN", "BACKUP NO DATA"),
                                            ("UNKN", "NAS NO DATA"), ("UNKN", "NEXUS NO DATA")])

    def test_network_scenarios(self):
        # Linka DOWN, chybějící IPv4 i nízký či vysoký provoz jsou platná měření (INFO),
        # ne závady: STATUS je nehlásí. Neplatné RX je UNKNOWN.
        for scenario in ("net_down", "net_no_ipv4", "net_low", "net_high"):
            with self.subTest(scenario=scenario):
                self.assertEqual(self.lines(scenario), [])
        self.assertEqual(self.lines("net_invalid"), [("UNKN", "NET RX NO DATA")])

    def test_network_page_in_scenarios(self):
        def rows(scenario):
            preview_session, _ = session(scenario)
            return network_rows(preview_session.results, preview_session.clock.now())

        self.assertEqual(rows("ok"), [("ETH0", "UP"), ("RX", "1.2 MB/s"), ("TX", "85 KB/s"), ("IPV4", "192.0.2.10")])
        self.assertEqual(rows("net_down"), [("ETH0", "DOWN"), ("RX", "0 B/s"), ("TX", "0 B/s"), ("IPV4", "NONE")])
        self.assertEqual(rows("net_no_ipv4"), [("ETH0", "UP"), ("RX", "412 B/s"), ("TX", "0 B/s"), ("IPV4", "NONE")])
        self.assertEqual(rows("net_low"), [("ETH0", "UP"), ("RX", "312 B/s"), ("TX", "96 B/s"), ("IPV4", "192.0.2.10")])
        self.assertEqual(rows("net_high"),
                         [("ETH0", "UP"), ("RX", "112 MB/s"), ("TX", "49 MB/s"), ("IPV4", "192.0.2.10")])
        self.assertEqual(rows("net_invalid"), [("ETH0", "UP"), ("RX", "UNKN"), ("TX", "85 KB/s"), ("IPV4", "192.0.2.10")])
        # Ve scénáři bez síťových výsledků je všechno UNKN, nic se nedomýšlí.
        self.assertEqual(rows("unknown"), [("LINK", "UNKN"), ("RX", "UNKN"), ("TX", "UNKN"), ("IPV4", "UNKN")])
        self.assertEqual(rows(LIVE), [("WLP2S0", "UP"), ("RX", "12 KB/s"), ("TX", "2.1 KB/s"), ("IPV4", "192.0.2.77")])

    def test_stale_network_data(self):
        preview_session, real = session("net_invalid")
        preview_session.set_speed(10)
        for _ in range(25):
            real.now += 0.1  # 25 s simulovaného času: TX a IPv4 změřené jen při startu prošly
            preview_session._refresh_if_due(preview_session.clock.now())
        now = preview_session.clock.now()
        self.assertEqual([status_line(p, now) for p in active_problems(preview_session.results, now)],
                         [("UNKN", "IPV4 STALE"), ("UNKN", "NET RX NO DATA"), ("UNKN", "NET TX STALE")])
        self.assertEqual(network_rows(preview_session.results, now),
                         [("ETH0", "UP"), ("RX", "UNKN"), ("TX", "UNKN"), ("IPV4", "UNKN")])

    def test_stale_data_in_unknown_scenario(self):
        preview_session, real = session("unknown")
        preview_session.set_speed(10)
        for _ in range(45):
            real.now += 0.1  # 45 s simulovaného času
            preview_session._refresh_if_due(preview_session.clock.now())
        now = preview_session.clock.now()
        lines = [status_line(p, now) for p in active_problems(preview_session.results, now)]
        self.assertIn(("UNKN", "ROOT STALE"), lines)  # disk změřen jen při startu, platnost 20 s
        self.assertIn(("UNKN", "BACKUP STALE"), lines)
        self.assertIn(("UNKN", "CPU NO DATA"), lines)
        self.assertEqual(page_rows("SYSTEM", preview_session.results, now)[1], ("CPU TEMP", "47°C"))


@unittest.skipIf(render is None, "Pillow není dostupné")
class RenderedTextTest(unittest.TestCase):
    """Co se na displeji skutečně kreslí: jen anglické texty, nic se neořízne."""

    def recorded_texts(self, draw):
        texts = []
        original = ImageDraw.ImageDraw.text

        def recording(self, xy, text, *args, **kwargs):
            texts.append(text)
            return original(self, xy, text, *args, **kwargs)

        # Bez mezipaměti měření, aby se zaznamenal i text změřený dřív.
        render.ink_box.cache_clear()
        with mock.patch.object(ImageDraw.ImageDraw, "text", recording):
            draw()
        return texts

    def all_screens(self, scenario):
        """Všechny obrazovky scénáře: NEXUS, SYSTEM, DISK, NETWORK, SERVICES a každá stránka STATUS."""
        preview_session, real = session(scenario)
        frames = {}
        preview_session.press()
        start = real.now
        for offset, page in ((0, "NEXUS"), (10, "SYSTEM"), (20, "DISK"), (30, "NETWORK"), (40, "SERVICES")):
            real.now = start + offset
            frames[page] = preview_session.tick()
        preview_session.press()
        page_number = 1
        while preview_session.controller.mode.value == "STATUS":
            frames[f"STATUS{page_number}"] = preview_session.tick()
            preview_session.press()
            page_number += 1
        return frames

    def test_every_drawn_text_is_english_without_diacritics(self):
        for scenario in SCENARIOS:
            texts = self.recorded_texts(lambda: self.all_screens(scenario))
            with self.subTest(scenario=scenario):
                self.assertTrue(texts)
                for text in texts:
                    self.assertLessEqual(drawn_chars(text), ALLOWED, text)

    def test_lowercase_only_in_rate_units(self):
        self.assertEqual(drawn_chars("112 MB/s"), set("112 MB/S"))
        self.assertIn("s", drawn_chars("ETH0 s"))  # jinde malé písmeno neprojde
        self.assertIn("s", drawn_chars("Ms/s B"))

    def test_page_titles_are_english(self):
        texts = set(self.recorded_texts(lambda: self.all_screens("many")))
        self.assertLessEqual({"NEXUS", "SYSTEM", "DISK", "NETWORK", "SERVICES", "STATUS"}, texts)
        self.assertFalse({"SLUŽBY", "SLUZBY"} & texts)

    def test_screens_stay_one_bit_128x64(self):
        for scenario in SCENARIOS:
            for name, frame in self.all_screens(scenario).items():
                with self.subTest(scenario=scenario, screen=name):
                    self.assertEqual((frame.size, frame.mode), ((128, 64), "1"))
                    self.assertIsNotNone(frame.getbbox())

    def test_known_status_texts_fit_without_ellipsis(self):
        width = render.RIGHT_EDGE + 1 - render.STATUS_TEXT_X
        text_font = render.font(render.TEXT_FONT)
        values = {"%": (0, 9.6, 100), "°C": (0.6, 48, 125)}  # rozsah kolektoru
        for check_id in CHECK_NAMES:
            for state in (State.CRITICAL, State.ERROR, State.WARNING, State.UNKNOWN):
                cases = [result(check_id, state), result(check_id, state, expires=5)]  # bez dat, prošlé
                if state is not State.UNKNOWN:
                    cases += [result(check_id, state, v, unit) for unit, vs in values.items() for v in vs]
                for problem in cases:
                    texts = status_texts(problem, 10)
                    drawn = render.first_fitting(texts, text_font, width)
                    with self.subTest(check_id=check_id, state=state, text=texts[0]):
                        self.assertNotIn("…", drawn)
                        self.assertLessEqual(render.ink_width(drawn, text_font), width)

    def test_too_long_status_text_keeps_name(self):
        text_font = render.font(render.TEXT_FONT)
        width = render.RIGHT_EDGE + 1 - render.STATUS_TEXT_X
        # 16 znaků se nevejde: zůstane název (identifikace), ne useknutý text s výpustkou.
        self.assertEqual(render.first_fitting(status_texts(result(CPU_TEMP_ID, State.UNKNOWN), 0), text_font, width),
                         "CPU TEMP")
        self.assertEqual(render.first_fitting(["CPU TEMP 88°C", "CPU TEMP"], text_font, width), "CPU TEMP 88°C")
        # Neznámé dlouhé id se vejde celé jako CHECK s kódem, bez výpustky.
        unknown = render.first_fitting(status_texts(result("nas.share_media_mount", State.CRITICAL), 0),
                                       text_font, width)
        self.assertEqual(unknown, check_name("nas.share_media_mount"))
        # Pojistka pro příliš dlouhý název: výpustka bez mezery před ní.
        self.assertEqual(render.first_fitting(["STORAGE MEDIA SHARE"], text_font, width), "STORAGE MEDIA…")

    def test_value_rows_fit_and_do_not_touch(self):
        values = ["0%", "100%", "0°C", "125°C", "UNKN", "N/A", "ERR"]
        labels = [check_name(i) for i in (CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID, DISK_ROOT_ID)]
        for label in labels:
            for value in values:
                image = Image.new("1", (200, 100))  # větší plátno: přesah by byl vidět
                render.draw_value_page(image, "SYSTEM", [(label, value)] * 3, False)
                with self.subTest(label=label, value=value):
                    self.assertIsNone(image.crop((128, 0, 200, 100)).getbbox())  # nic mimo 128 × 64
                    self.assertIsNone(image.crop((0, 64, 200, 100)).getbbox())
                    self.assertIsNone(image.crop((127, 14, 128, 64)).getbbox())  # sloupec 127 volný pod čarou
                    for row in range(3):
                        top = render.VALUE_ROW_TOP + row * render.VALUE_ROW_STEP
                        label_box = image.crop((0, top, 64, top + 13)).getbbox()
                        value_box = image.crop((64, top, 128, top + 13)).getbbox()
                        self.assertIsNotNone(value_box)
                        # Název končí před hodnotou aspoň o LABEL_GAP sloupců.
                        value_left = 64 + value_box[0]
                        self.assertLessEqual(label_box[2] + render.LABEL_GAP, value_left)

    def test_value_page_shows_real_value(self):
        controller_results = [result(CPU_TEMP_ID, State.ERROR, 88.4)]
        image = Image.new("1", (128, 64))
        render.draw_value_page(image, "SYSTEM", page_rows("SYSTEM", controller_results, 0), False)
        expected = Image.new("1", (128, 64))
        render.draw_value_page(expected, "SYSTEM", [("CPU", "UNKN"), ("CPU TEMP", "88°C"), ("RAM", "UNKN")], False)
        self.assertEqual(image.tobytes(), expected.tobytes())


if __name__ == "__main__":
    unittest.main()
