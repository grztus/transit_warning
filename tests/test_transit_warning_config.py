import importlib
import unittest
from unittest.mock import Mock, call, patch

import ephem

import transit_warning as transit
from config import ConfigurationError, InstallationConfig


TEST_CONFIG = InstallationConfig(
    observer_lat=50.25,
    observer_lon=19.75,
    observer_elevation_m=245.5,
    adsb_host="adsb.example",
    adsb_port=31003,
    mlat_host="mlat.example",
    mlat_port=31106,
    metar_url="https://weather.example/metar?airport=TEST",
)


class ApplicationConfigurationTests(unittest.TestCase):
    def test_import_does_not_load_installation_configuration(self):
        with patch("config.load_installation_config") as loader:
            importlib.reload(transit)
            loader.assert_not_called()
        importlib.reload(transit)

    def test_applies_location_and_builds_observer(self):
        transit.apply_installation_config(TEST_CONFIG)

        self.assertEqual(transit.my_lat, TEST_CONFIG.observer_lat)
        self.assertEqual(transit.my_lon, TEST_CONFIG.observer_lon)
        self.assertEqual(transit.my_elevation_const, TEST_CONFIG.observer_elevation_m)
        self.assertIsInstance(transit.gatech, ephem.Observer)
        self.assertAlmostEqual(float(transit.gatech.lat) * 180.0 / ephem.pi, TEST_CONFIG.observer_lat)
        self.assertAlmostEqual(float(transit.gatech.lon) * 180.0 / ephem.pi, TEST_CONFIG.observer_lon)
        self.assertEqual(transit.gatech.elevation, TEST_CONFIG.observer_elevation_m)

    def test_metar_request_uses_configured_url(self):
        transit.apply_installation_config(TEST_CONFIG)
        transit.metar_t = transit.clock.now_utc() - transit.datetime.timedelta(seconds=901)
        response = Mock(status_code=200, text="METAR TEST 101200Z Q1015")

        with patch.object(transit.requests, "get", return_value=response) as get:
            self.assertEqual(transit.get_metar_press(), 1015)

        get.assert_called_once_with(TEST_CONFIG.metar_url)

    def test_main_starts_independent_configured_sources(self):
        threads = [Mock(), Mock()]
        thread_factory = Mock(side_effect=threads)
        with patch.object(transit, "load_installation_config", return_value=TEST_CONFIG), \
                patch.object(transit.threading, "Thread", thread_factory), \
                patch.object(transit.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                transit.main()

        self.assertEqual(
            thread_factory.call_args_list,
            [
                call(target=transit.read_from_port,
                     args=(TEST_CONFIG.adsb_host, TEST_CONFIG.adsb_port, transit.process_line)),
                call(target=transit.read_from_port,
                     args=(TEST_CONFIG.mlat_host, TEST_CONFIG.mlat_port, transit.process_line)),
            ],
        )
        for thread in threads:
            thread.start.assert_called_once_with()
        self.assertEqual(
            transit.port_status,
            {TEST_CONFIG.adsb_port: False, TEST_CONFIG.mlat_port: False},
        )

    def test_configuration_error_exits_cleanly_before_starting_threads(self):
        error = ConfigurationError("Invalid installation configuration:\n- OBSERVER_LAT is required")
        with patch.object(transit, "load_installation_config", side_effect=error), \
                patch.object(transit.threading, "Thread") as thread:
            with self.assertRaisesRegex(SystemExit, "OBSERVER_LAT is required"):
                transit.main()

        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
