import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config import ConfigurationError, InstallationConfig, load_installation_config


REQUIRED = {
    "OBSERVER_LAT": "50.25",
    "OBSERVER_LON": "19.75",
    "OBSERVER_ELEVATION_M": "245.5",
    "TRANSITION_ALTITUDE_FT": "6500",
    "ADSB_TIMESTAMP_TIMEZONE": "Europe/Warsaw",
    "METAR_STATION": "epra",
}


class InstallationConfigTests(unittest.TestCase):
    def load(self, values):
        with tempfile.TemporaryDirectory() as directory:
            missing_dotenv = Path(directory) / ".env"
            return load_installation_config(values, missing_dotenv)

    def test_loads_complete_configuration_and_converts_types(self):
        values = {
            **REQUIRED,
            "ADSB_HOST": "adsb.example",
            "ADSB_PORT": "31003",
            "MLAT_HOST": "mlat.example",
            "MLAT_PORT": "31106",
        }

        result = self.load(values)

        self.assertEqual(
            result,
            InstallationConfig(
                observer_lat=50.25,
                observer_lon=19.75,
                observer_elevation_m=245.5,
                transition_altitude_ft=6500,
                adsb_host="adsb.example",
                adsb_port=31003,
                adsb_timestamp_timezone="Europe/Warsaw",
                mlat_host="mlat.example",
                mlat_port=31106,
                metar_station="EPRA",
                mlat_beast_enabled=False,
                mlat_beast_host="mlat.example",
                mlat_beast_port=30105,
            ),
        )

    def test_uses_network_defaults_without_a_dotenv_file(self):
        result = self.load(REQUIRED)

        self.assertEqual(result.adsb_host, "127.0.0.1")
        self.assertEqual(result.adsb_port, 30003)
        self.assertEqual(result.mlat_host, "127.0.0.1")
        self.assertEqual(result.mlat_port, 30106)
        self.assertEqual(result.beast_host, "192.168.56.1")
        self.assertEqual(result.beast_port, 30005)
        self.assertEqual(result.raw_adsb_host, "127.0.0.1")
        self.assertEqual(result.raw_adsb_port, 30002)
        self.assertFalse(result.fleet_geometric_altitude_enabled)
        self.assertFalse(result.geometric_altitude_selection_enabled)
        self.assertEqual(result.fleet_geoid_pgm_path, "")
        self.assertFalse(result.mlat_beast_enabled)
        self.assertEqual(result.mlat_beast_host, result.mlat_host)
        self.assertEqual(result.mlat_beast_port, 30105)
        self.assertEqual(
            (3.0, 5.0, 7.0),
            (result.tmux_sep_green_max_deg,
             result.tmux_sep_yellow_max_deg,
             result.tmux_sep_visible_max_deg))
        self.assertEqual(
            (3.0, 5.0, 7.0),
            (result.dashboard_sep_green_max_deg,
             result.dashboard_sep_yellow_max_deg,
             result.dashboard_sep_visible_max_deg))

    def test_presentation_threshold_sets_are_independently_configurable(self):
        result = self.load({
            **REQUIRED,
            "TMUX_SEP_GREEN_MAX_DEG": "1",
            "TMUX_SEP_YELLOW_MAX_DEG": "2",
            "TMUX_SEP_VISIBLE_MAX_DEG": "3",
            "DASHBOARD_SEP_GREEN_MAX_DEG": "4",
            "DASHBOARD_SEP_YELLOW_MAX_DEG": "5",
            "DASHBOARD_SEP_VISIBLE_MAX_DEG": "6",
        })
        self.assertEqual((1.0, 2.0, 3.0), (
            result.tmux_sep_green_max_deg,
            result.tmux_sep_yellow_max_deg,
            result.tmux_sep_visible_max_deg))
        self.assertEqual((4.0, 5.0, 6.0), (
            result.dashboard_sep_green_max_deg,
            result.dashboard_sep_yellow_max_deg,
            result.dashboard_sep_visible_max_deg))

    def test_rejects_invalid_presentation_thresholds(self):
        cases = (
            ("TMUX_SEP_GREEN_MAX_DEG", "bad", "must be a number"),
            ("DASHBOARD_SEP_VISIBLE_MAX_DEG", "0", "greater than"),
            ("TMUX_SEP_YELLOW_MAX_DEG", "3", "GREEN < YELLOW < VISIBLE"),
            ("DASHBOARD_SEP_GREEN_MAX_DEG", "8", "GREEN < YELLOW < VISIBLE"),
        )
        for name, value, message in cases:
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(ConfigurationError, message):
                    self.load({**REQUIRED, name: value})

    def test_accepts_and_validates_optional_mlat_beast_source(self):
        result = self.load({
            **REQUIRED,
            "MLAT_BEAST_ENABLED": "yes",
            "MLAT_BEAST_HOST": "mlat-precision.example",
            "MLAT_BEAST_PORT": "31105",
        })
        self.assertTrue(result.mlat_beast_enabled)
        self.assertEqual(result.mlat_beast_host, "mlat-precision.example")
        self.assertEqual(result.mlat_beast_port, 31105)
        with self.assertRaisesRegex(
                ConfigurationError, "MLAT_BEAST_ENABLED must be true or false"):
            self.load({**REQUIRED, "MLAT_BEAST_ENABLED": "sometimes"})

    def test_accepts_custom_optional_raw_source(self):
        result = self.load({
            **REQUIRED,
            "RAW_ADSB_HOST": "receiver.example",
            "RAW_ADSB_PORT": "32002",
        })

        self.assertEqual(result.raw_adsb_host, "receiver.example")
        self.assertEqual(result.raw_adsb_port, 32002)

    def test_accepts_optional_fleet_geometric_altitude_configuration(self):
        result = self.load({
            **REQUIRED,
            "FLEET_GEOMETRIC_ALTITUDE_ENABLED": "true",
            "FLEET_GEOID_PGM_PATH": "C:/geoid/egm96-5.pgm",
        })

        self.assertTrue(result.fleet_geometric_altitude_enabled)
        self.assertEqual(
            result.fleet_geoid_pgm_path, "C:/geoid/egm96-5.pgm")

        with self.assertRaisesRegex(
                ConfigurationError,
                "FLEET_GEOMETRIC_ALTITUDE_ENABLED must be true or false"):
            self.load({
                **REQUIRED,
                "FLEET_GEOMETRIC_ALTITUDE_ENABLED": "sometimes",
            })

    def test_accepts_production_geometric_altitude_selection_flag(self):
        result = self.load({
            **REQUIRED,
            "GEOMETRIC_ALTITUDE_SELECTION_ENABLED": "true",
        })
        self.assertTrue(result.geometric_altitude_selection_enabled)
        with self.assertRaisesRegex(
                ConfigurationError,
                "GEOMETRIC_ALTITUDE_SELECTION_ENABLED must be true or false"):
            self.load({
                **REQUIRED,
                "GEOMETRIC_ALTITUDE_SELECTION_ENABLED": "sometimes",
            })

    def test_rejects_invalid_optional_raw_source(self):
        with self.assertRaises(ConfigurationError) as caught:
            self.load({
                **REQUIRED,
                "RAW_ADSB_HOST": " ",
                "RAW_ADSB_PORT": "70000",
            })

        message = str(caught.exception)
        self.assertIn("RAW_ADSB_HOST must not be empty", message)
        self.assertIn("RAW_ADSB_PORT must be in the range 1..65535", message)

    def test_environment_overrides_dotenv_values(self):
        with tempfile.TemporaryDirectory() as directory:
            dotenv_path = Path(directory) / ".env"
            dotenv_path.write_text(
                "OBSERVER_LAT=10\n"
                "OBSERVER_LON=20\n"
                "OBSERVER_ELEVATION_M=30\n"
                "TRANSITION_ALTITUDE_FT=6500\n"
                "ADSB_HOST=from-file\n"
                "ADSB_TIMESTAMP_TIMEZONE=Europe/Warsaw\n"
                "METAR_STATION=EPWA\n",
                encoding="utf-8",
            )
            environment = {
                "OBSERVER_LAT": "40",
                "ADSB_HOST": "from-environment",
            }

            result = load_installation_config(environment, dotenv_path)

        self.assertEqual(result.observer_lat, 40.0)
        self.assertEqual(result.observer_lon, 20.0)
        self.assertEqual(result.adsb_host, "from-environment")
        self.assertEqual(result.metar_station, "EPWA")

    def test_default_dotenv_path_is_independent_of_current_directory(self):
        expected = Path(__file__).resolve().parents[1] / ".env"
        with tempfile.TemporaryDirectory() as directory, \
                patch("config.dotenv_values", return_value=REQUIRED) as dotenv_values_mock, \
                patch.dict(os.environ, {}, clear=True), \
                patch("os.getcwd", return_value=directory):
            load_installation_config()

        dotenv_values_mock.assert_called_once_with(expected)

    def test_reports_all_missing_required_values(self):
        with self.assertRaises(ConfigurationError) as raised:
            self.load({})

        message = str(raised.exception)
        for name in ("OBSERVER_LAT", "OBSERVER_LON", "OBSERVER_ELEVATION_M",
                     "TRANSITION_ALTITUDE_FT", "ADSB_TIMESTAMP_TIMEZONE",
                     "METAR_STATION"):
            self.assertIn("{} is required".format(name), message)

    def test_rejects_invalid_coordinates_and_elevation(self):
        invalid_cases = (
            ("OBSERVER_LAT", "north", "must be a number"),
            ("OBSERVER_LAT", "90.1", "range -90..90"),
            ("OBSERVER_LON", "-180.1", "range -180..180"),
            ("OBSERVER_ELEVATION_M", "nan", "finite number"),
            ("OBSERVER_ELEVATION_M", "inf", "finite number"),
        )
        for name, value, expected in invalid_cases:
            with self.subTest(name=name, value=value):
                values = {**REQUIRED, name: value}
                with self.assertRaisesRegex(ConfigurationError, expected):
                    self.load(values)

    def test_rejects_invalid_ports(self):
        for name in ("ADSB_PORT", "MLAT_PORT", "BEAST_PORT"):
            for value in ("not-a-port", "0", "65536"):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ConfigurationError):
                        self.load({**REQUIRED, name: value})

    def test_validates_transition_altitude(self):
        self.assertEqual(self.load(REQUIRED).transition_altitude_ft, 6500)
        for value in ("not-an-integer", "1.5", "0", "-1"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    self.load({**REQUIRED, "TRANSITION_ALTITUDE_FT": value})

    def test_rejects_empty_hosts(self):
        for name in ("ADSB_HOST", "MLAT_HOST", "BEAST_HOST"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ConfigurationError, "{} must not be empty".format(name)):
                    self.load({**REQUIRED, name: "  "})

    def test_rejects_identical_source_endpoints(self):
        with self.assertRaisesRegex(ConfigurationError, r"host\+port pairs must be different"):
            self.load({
                **REQUIRED,
                "ADSB_HOST": "Receiver.Example",
                "MLAT_HOST": "receiver.example",
                "MLAT_PORT": "30003",
            })

    def test_normalizes_metar_station_to_uppercase(self):
        self.assertEqual(self.load(REQUIRED).metar_station, "EPRA")

    def test_accepts_valid_iana_timezone(self):
        self.assertEqual(
            self.load(REQUIRED).adsb_timestamp_timezone, "Europe/Warsaw")

    def test_rejects_invalid_iana_timezone(self):
        with self.assertRaisesRegex(ConfigurationError, "valid IANA timezone"):
            self.load({**REQUIRED, "ADSB_TIMESTAMP_TIMEZONE": "Mars/Olympus_Mons"})

    def test_rejects_invalid_metar_stations(self):
        for value in ("", "EP", "EPRAA", "EP1A", "ĘPRA"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    self.load({**REQUIRED, "METAR_STATION": value})


if __name__ == "__main__":
    unittest.main()
