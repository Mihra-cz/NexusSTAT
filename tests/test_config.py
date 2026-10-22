import tempfile
import tomllib
import unittest
from pathlib import Path

from nexusstat.config import (
    HISTORY_ROLE,
    HISTORY_SUFFIX,
    THRESHOLD_LIMITS,
    AlarmRule,
    AlarmsConfig,
    CollectorConfig,
    Config,
    ConfigError,
    HistoryConfig,
    NetworkConfig,
    OledConfig,
    alarms_config_from_dict,
    collector_config_from_dict,
    history_config_from_dict,
    load_config,
    network_config_from_dict,
    oled_config_from_dict,
)

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.example.toml"


class DefaultsTest(unittest.TestCase):
    def test_defaults(self):
        config = OledConfig()
        self.assertEqual(config.carousel_seconds, 300)
        self.assertEqual(config.page_seconds, 10)
        self.assertEqual(config.status_idle_seconds, 30)

    def test_example_config_matches_defaults(self):
        self.assertEqual(load_config(EXAMPLE_CONFIG), Config())
        self.assertEqual(load_config(EXAMPLE_CONFIG).oled, OledConfig())

    def test_example_config_has_only_used_sections(self):
        # Ostatní sekce (web, storage) jsou v ukázce jen jako komentář,
        # aby nevypadaly funkčně, dokud je kód nenačítá.
        with open(EXAMPLE_CONFIG, "rb") as file:
            self.assertEqual(set(tomllib.load(file)), {"oled", "collector", "network", "alarms", "history"})

    def test_example_config_keeps_history_off_without_path(self):
        # Ukázka nesmí zapnout ukládání ani nabídnout výchozí cestu k databázi.
        history = load_config(EXAMPLE_CONFIG).history
        self.assertFalse(history.enabled)
        self.assertIsNone(history.path)

    def test_example_config_selects_interface_automatically(self):
        # Ukázka nesmí natvrdo určit rozhraní: na Nexusu se může jmenovat jinak.
        self.assertIsNone(load_config(EXAMPLE_CONFIG).network.interface)

    def test_example_config_has_no_active_thresholds(self):
        # Ukázka nesmí obsahovat neodsouhlasené prahy, ani vypnuté.
        alarms = load_config(EXAMPLE_CONFIG).alarms
        for rule in (alarms.cpu_temp, alarms.disk_root):
            self.assertFalse(rule.enabled)
            self.assertEqual((rule.warning, rule.error, rule.hysteresis), (None, None, None))

    def test_missing_keys_use_defaults(self):
        self.assertEqual(oled_config_from_dict({}), OledConfig())

    def test_custom_values(self):
        config = oled_config_from_dict(
            {"carousel_seconds": 120, "page_seconds": 5, "status_idle_seconds": 15}
        )
        self.assertEqual(config, OledConfig(120, 5, 15))


class InvalidValuesTest(unittest.TestCase):
    def test_invalid_seconds(self):
        invalid = [0, -10, 3601, 2.5, True, "10", None]
        for key in ("carousel_seconds", "page_seconds", "status_idle_seconds"):
            for value in invalid:
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ConfigError):
                        oled_config_from_dict({key: value})

    def test_page_longer_than_carousel(self):
        with self.assertRaises(ConfigError):
            oled_config_from_dict({"carousel_seconds": 20, "page_seconds": 30})

    def test_direct_construction_is_validated(self):
        with self.assertRaises(ConfigError):
            OledConfig(page_seconds=0)

    def test_unknown_key(self):
        with self.assertRaises(ConfigError):
            oled_config_from_dict({"page_second": 10})

    def test_unimplemented_options_are_rejected(self):
        # Neimplementované volby nesmí projít a tvářit se, že něco nastavují.
        for section in ({"home_title": "NEXUS"}, {"clock_24h": True}, {"show_warning_triangle": False}):
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    oled_config_from_dict(section)

    def test_unsupported_display_size(self):
        for section in ({"width": 256}, {"height": 32}):
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    oled_config_from_dict(section)

    def test_oled_must_be_table(self):
        with self.assertRaises(ConfigError):
            oled_config_from_dict(5)


class CollectorConfigTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(CollectorConfig().interval_seconds, 10)
        self.assertEqual(CollectorConfig().validity_seconds, 30)
        self.assertEqual(Config().collector, CollectorConfig())

    def test_validity_is_three_intervals(self):
        for interval in (1, 7, 60, 3600):
            with self.subTest(interval=interval):
                self.assertEqual(CollectorConfig(interval).validity_seconds, 3 * interval)

    def test_missing_keys_use_defaults(self):
        self.assertEqual(collector_config_from_dict({}), CollectorConfig())

    def test_custom_interval(self):
        self.assertEqual(collector_config_from_dict({"interval_seconds": 5}), CollectorConfig(5))

    def test_invalid_interval(self):
        for value in (0, -10, 3601, 2.5, True, "10", None):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    collector_config_from_dict({"interval_seconds": value})

    def test_direct_construction_is_validated(self):
        with self.assertRaises(ConfigError):
            CollectorConfig(interval_seconds=0)

    def test_validity_is_not_configurable(self):
        # Platnost se odvozuje z intervalu; samostatný klíč by se mohl rozejít s intervalem.
        for section in ({"validity_seconds": 30}, {"interval_second": 10}):
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    collector_config_from_dict(section)

    def test_collector_must_be_table(self):
        with self.assertRaises(ConfigError):
            collector_config_from_dict(10)


class NetworkConfigTest(unittest.TestCase):
    def test_default_is_automatic_selection(self):
        self.assertIsNone(NetworkConfig().interface)
        self.assertEqual(Config().network, NetworkConfig())
        self.assertEqual(network_config_from_dict({}), NetworkConfig())

    def test_explicit_interface(self):
        # Fyzická i virtuální rozhraní: výslovná volba se použije přesně.
        for name in ("eth0", "end0", "wlp2s0", "enp0s31f6", "br0", "eth0.10", "wlan_ap", "a" * 15):
            with self.subTest(name=name):
                self.assertEqual(network_config_from_dict({"interface": name}).interface, name)

    def test_invalid_interface(self):
        # Prázdný, dlouhý (jádro dovolí nejvýš 15 znaků), s cestou, mezerou,
        # dvojtečkou, diakritikou nebo jiného typu: chyba, ne tichý automatický výběr.
        # (TOML nemá null; automatický výběr = klíč vynechat.)
        for value in ("", "a" * 16, ".", "..", "../eth0", "eth 0", "eth0:1", "ethé", True, 0, ["eth0"]):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    network_config_from_dict({"interface": value})

    def test_direct_construction_is_validated(self):
        with self.assertRaises(ConfigError):
            NetworkConfig(interface="eth/0")

    def test_unknown_key_and_non_table(self):
        for section in ({"interfaces": "eth0"}, {"interface": "eth0", "ipv6": True}):
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    network_config_from_dict(section)
        with self.assertRaises(ConfigError):
            network_config_from_dict("eth0")


# Ilustrační prahy jen pro testy validace; nejde o schválené hodnoty.
VALID_RULE = {"enabled": True, "warning": 70, "error": 80, "hysteresis": 5}


def alarms(name="cpu_temp", **rule):
    return alarms_config_from_dict({name: rule})


class AlarmsConfigTest(unittest.TestCase):
    def test_defaults_are_disabled_without_thresholds(self):
        for rule in (AlarmsConfig().cpu_temp, AlarmsConfig().disk_root, AlarmRule()):
            self.assertFalse(rule.enabled)
            self.assertEqual((rule.warning, rule.error, rule.hysteresis), (None, None, None))
            self.assertEqual((rule.confirm_count, rule.clear_count), (3, 2))
        self.assertEqual(Config().alarms, AlarmsConfig())
        self.assertEqual(alarms_config_from_dict({}), AlarmsConfig())

    def test_valid_rules(self):
        config = alarms(**VALID_RULE)
        self.assertEqual(config.cpu_temp, AlarmRule(True, 70, 80, 5))
        self.assertEqual(config.disk_root, AlarmRule())
        disk = alarms("disk_root", enabled=True, warning=85.5, error=95, hysteresis=0, confirm_count=1, clear_count=5)
        self.assertEqual(disk.disk_root, AlarmRule(True, 85.5, 95, 0, 1, 5))

    def test_disabled_rule_may_keep_prepared_thresholds(self):
        rule = alarms(**{**VALID_RULE, "enabled": False}).cpu_temp
        self.assertFalse(rule.enabled)
        self.assertEqual(rule.warning, 70)

    def test_enabled_requires_all_thresholds(self):
        for missing in ("warning", "error", "hysteresis"):
            rule = {key: value for key, value in VALID_RULE.items() if key != missing}
            with self.subTest(missing=missing):
                with self.assertRaises(ConfigError):
                    alarms(**rule)
        with self.assertRaises(ConfigError):
            alarms(enabled=True)

    def test_partial_thresholds_rejected_even_when_disabled(self):
        with self.assertRaises(ConfigError):
            alarms(enabled=False, warning=70)

    def test_error_must_be_above_warning(self):
        for warning, error in ((80, 80), (80, 70)):
            with self.subTest(warning=warning, error=error):
                with self.assertRaises(ConfigError):
                    alarms(**{**VALID_RULE, "warning": warning, "error": error})

    def test_hysteresis_range(self):
        # Záporná, nebo tak velká, že by pásmo ERROR sahalo pod práh WARNING.
        for hysteresis in (-1, 10, 15):
            with self.subTest(hysteresis=hysteresis):
                with self.assertRaises(ConfigError):
                    alarms(**{**VALID_RULE, "hysteresis": hysteresis})
        self.assertEqual(alarms(**{**VALID_RULE, "hysteresis": 9.9}).cpu_temp.hysteresis, 9.9)

    def test_clear_level_must_be_reachable(self):
        # Disk nemůže mít méně než 0 %: závada s warning − hysteresis ≤ 0 by nikdy neodezněla.
        with self.assertRaises(ConfigError):
            alarms("disk_root", enabled=True, warning=5, error=90, hysteresis=5)
        self.assertTrue(alarms("disk_root", enabled=True, warning=5, error=90, hysteresis=4.9).disk_root.enabled)

    def test_thresholds_within_quantity_range(self):
        invalid = [
            ("cpu_temp", 0, 80), ("cpu_temp", -10, 80), ("cpu_temp", 70, 126),
            ("disk_root", 0, 90), ("disk_root", 85, 101),
        ]
        for name, warning, error in invalid:
            with self.subTest(name=name, warning=warning, error=error):
                with self.assertRaises(ConfigError):
                    alarms(name, enabled=True, warning=warning, error=error, hysteresis=0)
        self.assertEqual(alarms("cpu_temp", enabled=True, warning=100, error=125, hysteresis=1).cpu_temp.error, 125)
        self.assertEqual(alarms("disk_root", enabled=True, warning=90, error=100, hysteresis=1).disk_root.error, 100)

    def test_temperature_limit_matches_collector_plausibility(self):
        from nexusstat.system_monitor import MAX_PLAUSIBLE_CELSIUS, MIN_PLAUSIBLE_CELSIUS
        self.assertEqual(THRESHOLD_LIMITS["cpu_temp"], (MIN_PLAUSIBLE_CELSIUS, MAX_PLAUSIBLE_CELSIUS))

    def test_invalid_types(self):
        cases = [
            {"enabled": "true"}, {"enabled": 1},
            {"warning": True}, {"warning": "70"}, {"warning": float("nan")}, {"error": float("inf")},
            {"hysteresis": None},
        ]
        for change in cases:
            with self.subTest(change=change):
                with self.assertRaises(ConfigError):
                    alarms(**{**VALID_RULE, **change})

    def test_non_finite_thresholds_and_hysteresis(self):
        for key in ("warning", "error", "hysteresis"):
            for value in (float("nan"), float("inf"), -float("inf"), True, False):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ConfigError):
                        alarms(**{**VALID_RULE, key: value})

    def test_huge_integer_is_config_error(self):
        # Regrese: TOML dovolí libovolně velké celé číslo; dříve OverflowError místo ConfigError.
        for key in ("warning", "error", "hysteresis"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    alarms(**{**VALID_RULE, key: 10**400})

    def test_hysteresis_equal_to_decimal_gap_is_rejected(self):
        # Regrese: 75.2 − 70.1 je ve float 5.1000000000000085, takže hystereze 5.1
        # (přesně rovná odstupu prahů) dříve prošla.
        with self.assertRaises(ConfigError):
            alarms(enabled=True, warning=70.1, error=75.2, hysteresis=5.1)
        self.assertEqual(alarms(enabled=True, warning=70.1, error=75.2, hysteresis=5.0).cpu_temp.error_hold, 70.2)

    def test_invalid_counts(self):
        for key in ("confirm_count", "clear_count"):
            for value in (0, -1, 61, 2.5, True, "3"):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ConfigError):
                        alarms(**{key: value})

    def test_unknown_names_and_keys(self):
        for section in ({"cpu_usage": {}}, {"show_warning_triangle": False},
                        {"cpu_temp": {"critical": 90}}, {"cpu_temp": {"enable": True}}):
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    alarms_config_from_dict(section)

    def test_rule_must_be_table(self):
        with self.assertRaises(ConfigError):
            alarms_config_from_dict({"cpu_temp": True})
        with self.assertRaises(ConfigError):
            alarms_config_from_dict(5)

    def test_error_message_names_rule(self):
        with self.assertRaises(ConfigError) as raised:
            alarms("disk_root", enabled=True, warning=95, error=85, hysteresis=1)
        self.assertIn("[alarms.disk_root]", str(raised.exception))


class HistoryConfigTest(unittest.TestCase):
    """Sekce [history]: výchozí vypnuto, bez výchozí cesty, striktní validace."""

    def test_default_is_disabled_without_path(self):
        config = HistoryConfig()
        self.assertFalse(config.enabled)
        self.assertIsNone(config.path)
        self.assertEqual(Config().history, config)

    def test_no_implicit_path_anywhere_in_code(self):
        # Žádná výchozí cesta: zapnutí bez výslovné cesty je chyba, ne odhad.
        with self.assertRaises(ConfigError):
            HistoryConfig(enabled=True)

    def test_enabled_needs_absolute_file_path(self):
        config = HistoryConfig(enabled=True, path="/var/lib/nexusstat/history.sqlite3")
        self.assertTrue(config.enabled)
        self.assertEqual(config.path, "/var/lib/nexusstat/history.sqlite3")

    def test_invalid_paths_are_refused(self):
        for value in ("", "relativni/history.sqlite3", "~/history.sqlite3", "/var/lib/nexusstat/",
                      "/var/lib/nexusstat/history.db", "/var/lib/nexusstat/history",
                      "/var/lib/nexusstat/history.sqlite3.lock", "/history\x00.sqlite3", 5, True, ["/x.sqlite3"]):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    HistoryConfig(enabled=True, path=value)

    def test_enabled_must_be_boolean(self):
        for value in ("true", 1, None):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    HistoryConfig(enabled=value, path="/x/history.sqlite3")

    def test_path_without_enabled_stays_disabled(self):
        # Cesta sama historii nezapne: zapnutí je výslovné rozhodnutí.
        config = HistoryConfig(path="/x/history.sqlite3")
        self.assertFalse(config.enabled)

    def test_unknown_keys_are_refused(self):
        for section in ({"role": "production"}, {"enable": True}, {"path_": "/x.sqlite3"},
                        {"retention_days": 30}, {"interval_seconds": 10}):
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    history_config_from_dict(section)

    def test_section_must_be_a_table(self):
        with self.assertRaises(ConfigError):
            history_config_from_dict("zapnuto")

    def test_role_is_fixed_to_development(self):
        # Roli nelze konfigurovat, takže produkční databázi nelze otevřít omylem.
        self.assertEqual(HISTORY_ROLE, "development")
        self.assertEqual(HISTORY_SUFFIX, ".sqlite3")


class LoadFileTest(unittest.TestCase):
    def load_text(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def test_missing_sections_use_defaults(self):
        self.assertEqual(self.load_text("# prázdná konfigurace\n"), Config())

    def test_unimplemented_sections_are_rejected(self):
        # Např. odkomentovaná budoucí volba se nesmí tiše ignorovat.
        for text in ("[alarms]\nshow_warning_triangle = false\n", "[web]\nadmin_port = 8765\n"):
            with self.subTest(text=text):
                with self.assertRaises(ConfigError):
                    self.load_text(text)

    def test_values_from_file(self):
        config = self.load_text("[oled]\ncarousel_seconds = 60\npage_seconds = 6\n")
        self.assertEqual(config.oled, OledConfig(carousel_seconds=60, page_seconds=6))
        self.assertEqual(config.collector, CollectorConfig())

    def test_both_sections_from_file(self):
        config = self.load_text("[oled]\npage_seconds = 6\n\n[collector]\ninterval_seconds = 5\n")
        self.assertEqual(config, Config(OledConfig(page_seconds=6), CollectorConfig(5)))
        self.assertEqual(config.collector.validity_seconds, 15)

    def test_network_from_file(self):
        config = self.load_text('[network]\ninterface = "end0"\n')
        self.assertEqual(config.network, NetworkConfig("end0"))
        self.assertEqual((config.oled, config.collector), (OledConfig(), CollectorConfig()))

    def test_invalid_network_rejects_whole_file(self):
        with self.assertRaises(ConfigError):
            self.load_text('[collector]\ninterval_seconds = 5\n\n[network]\ninterface = "eth 0"\n')

    def test_collector_only_keeps_oled_defaults(self):
        config = self.load_text("[collector]\ninterval_seconds = 20\n")
        self.assertEqual(config.oled, OledConfig())
        self.assertEqual(config.collector.interval_seconds, 20)

    def test_invalid_toml(self):
        with self.assertRaises(ConfigError):
            self.load_text("[oled\ncarousel_seconds = 60\n")

    def test_invalid_value_in_file(self):
        for text in ("[oled]\nstatus_idle_seconds = 0\n", "[collector]\ninterval_seconds = 0\n"):
            with self.subTest(text=text):
                with self.assertRaises(ConfigError):
                    self.load_text(text)

    def test_invalid_collector_rejects_whole_file(self):
        # Platné [oled] nestačí: chyba v jiné sekci se nesmí ztratit jen proto,
        # že ji volající (např. simulátor) zrovna nepotřebuje.
        with self.assertRaises(ConfigError):
            self.load_text("[oled]\npage_seconds = 6\n\n[collector]\ninterval = 10\n")

    def test_alarms_from_file(self):
        config = self.load_text(
            "[alarms.cpu_temp]\nenabled = true\nwarning = 70\nerror = 80.5\nhysteresis = 4\n"
        )
        self.assertEqual(config.alarms.cpu_temp, AlarmRule(True, 70, 80.5, 4))
        self.assertEqual(config.alarms.disk_root, AlarmRule())
        self.assertEqual((config.oled, config.collector), (OledConfig(), CollectorConfig()))

    def test_invalid_alarms_reject_whole_file(self):
        for text in (
            "[alarms.cpu_temp]\nwarning = 1" + "0" * 400 + "\nerror = 80\nhysteresis = 1\n",
            "[alarms.cpu_temp]\nenabled = true\n",
            "[alarms.cpu_temp]\nenabled = true\nwarning = 80\nerror = 70\nhysteresis = 1\n",
            "[alarms.cpu_temp]\nwarning = inf\nerror = 80\nhysteresis = 1\n",
            "[alarms.fan]\nenabled = false\n",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ConfigError):
                    self.load_text(text)

    def test_history_from_file(self):
        config = self.load_text('[history]\nenabled = true\npath = "/var/lib/nexusstat/history.sqlite3"\n')
        self.assertEqual(config.history, HistoryConfig(True, "/var/lib/nexusstat/history.sqlite3"))
        self.assertEqual((config.oled, config.collector, config.alarms, config.network),
                         (OledConfig(), CollectorConfig(), AlarmsConfig(), NetworkConfig()))

    def test_history_disabled_in_file(self):
        config = self.load_text("[history]\nenabled = false\n")
        self.assertEqual(config.history, HistoryConfig())

    def test_invalid_history_rejects_whole_file(self):
        for text in ("[history]\nenabled = true\n",
                     '[history]\nenabled = true\npath = "historie.sqlite3"\n',
                     '[history]\npath = "/x/history.sqlite3"\nrole = "production"\n',
                     '[history]\nenabled = true\npath = "/x/history.db"\n',
                     "[history]\nenabled = 1\n"):
            with self.subTest(text=text):
                with self.assertRaises(ConfigError):
                    self.load_text(text)

    def test_file_without_history_keeps_other_sections(self):
        config = self.load_text('[collector]\ninterval_seconds = 5\n\n[network]\ninterface = "end0"\n')
        self.assertFalse(config.history.enabled)
        self.assertEqual(config.collector.interval_seconds, 5)
        self.assertEqual(config.network.interface, "end0")

    def test_strict_oled_validation_is_kept(self):
        for text in ("[oled]\npage_second = 6\n", "[oled]\nwidth = 256\n", "[oled]\nhome_title = \"X\"\n"):
            with self.subTest(text=text):
                with self.assertRaises(ConfigError):
                    self.load_text(text)


if __name__ == "__main__":
    unittest.main()
