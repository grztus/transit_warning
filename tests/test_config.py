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
            "BEAST_HOST": "beast.example",
            "BEAST_PORT": "31005",
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
                beast_host="beast.example",
                beast_port=31005,
                mlat_host="mlat.example",
                mlat_port=31106,
                metar_station="EPRA",
            ),
        )

    def test_uses_network_defaults_without_a_dotenv_file(self):
        result = self.load(REQUIRED)

        self.assertEqual(result.adsb_host, "127.0.0.1")
        self.assertEqual(result.adsb_port, 30003)
        self.assertEqual(result.beast_host, "192.168.56.1")
        self.assertEqual(result.beast_port, 30005)
        self.assertEqual(result.mlat_host, "127.0.0.1")
        self.assertEqual(result.mlat_port, 30106)

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
        for name in ("ADSB_PORT", "BEAST_PORT", "MLAT_PORT"):
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
        for name in ("ADSB_HOST", "BEAST_HOST", "MLAT_HOST"):
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
