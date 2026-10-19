import tempfile
import tomllib
import unittest
from pathlib import Path

from nexusstat.config import ConfigError, OledConfig, load_oled_config, oled_config_from_dict

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.example.toml"


class DefaultsTest(unittest.TestCase):
    def test_defaults(self):
        config = OledConfig()
        self.assertEqual(config.carousel_seconds, 300)
        self.assertEqual(config.page_seconds, 10)
        self.assertEqual(config.status_idle_seconds, 30)

    def test_example_config_matches_defaults(self):
        self.assertEqual(load_oled_config(EXAMPLE_CONFIG), OledConfig())

    def test_example_config_has_only_used_sections(self):
        # Ostatní sekce (web, alarms, storage) jsou v ukázce jen jako komentář,
        # aby nevypadaly funkčně, dokud je kód nenačítá.
        with open(EXAMPLE_CONFIG, "rb") as file:
            self.assertEqual(set(tomllib.load(file)), {"oled"})

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


class LoadFileTest(unittest.TestCase):
    def load_text(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return load_oled_config(path)

    def test_missing_oled_section_uses_defaults(self):
        self.assertEqual(self.load_text("# prázdná konfigurace\n"), OledConfig())

    def test_unimplemented_sections_are_rejected(self):
        # Např. odkomentovaná budoucí volba se nesmí tiše ignorovat.
        for text in ("[alarms]\nshow_warning_triangle = false\n", "[web]\nadmin_port = 8765\n"):
            with self.subTest(text=text):
                with self.assertRaises(ConfigError):
                    self.load_text(text)

    def test_values_from_file(self):
        config = self.load_text("[oled]\ncarousel_seconds = 60\npage_seconds = 6\n")
        self.assertEqual(config, OledConfig(carousel_seconds=60, page_seconds=6))

    def test_invalid_toml(self):
        with self.assertRaises(ConfigError):
            self.load_text("[oled\ncarousel_seconds = 60\n")

    def test_invalid_value_in_file(self):
        with self.assertRaises(ConfigError):
            self.load_text("[oled]\nstatus_idle_seconds = 0\n")


if __name__ == "__main__":
    unittest.main()
