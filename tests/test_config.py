import tempfile
import tomllib
import unittest
from pathlib import Path

from nexusstat.config import (
    CollectorConfig,
    Config,
    ConfigError,
    OledConfig,
    collector_config_from_dict,
    load_config,
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
        # Ostatní sekce (web, alarms, storage) jsou v ukázce jen jako komentář,
        # aby nevypadaly funkčně, dokud je kód nenačítá.
        with open(EXAMPLE_CONFIG, "rb") as file:
            self.assertEqual(set(tomllib.load(file)), {"oled", "collector"})

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

    def test_strict_oled_validation_is_kept(self):
        for text in ("[oled]\npage_second = 6\n", "[oled]\nwidth = 256\n", "[oled]\nhome_title = \"X\"\n"):
            with self.subTest(text=text):
                with self.assertRaises(ConfigError):
                    self.load_text(text)


if __name__ == "__main__":
    unittest.main()
