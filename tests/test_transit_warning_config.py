import importlib
import datetime
import unittest
from unittest.mock import Mock, call, patch

import ephem
import pytz

import transit_warning as transit
from config import ConfigurationError, InstallationConfig
from metar import AwcMetar
from transit_clock import ReplayClock
from transit_time import AdsBTimestampOffsetValidator


TEST_CONFIG = InstallationConfig(
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
        self.assertEqual(
            transit.transition_altitude_ft, TEST_CONFIG.transition_altitude_ft)
        self.assertEqual(transit.metar_station, TEST_CONFIG.metar_station)
        self.assertEqual(
            transit.adsb_timestamp_timezone, TEST_CONFIG.adsb_timestamp_timezone)
        self.assertEqual(transit.raw_adsb_host, TEST_CONFIG.raw_adsb_host)
        self.assertEqual(transit.raw_adsb_port, TEST_CONFIG.raw_adsb_port)
        self.assertIsInstance(
            transit.adsb_timestamp_validator, AdsBTimestampOffsetValidator)
        self.assertIsInstance(transit.gatech, ephem.Observer)
        self.assertAlmostEqual(float(transit.gatech.lat) * 180.0 / ephem.pi, TEST_CONFIG.observer_lat)
        self.assertAlmostEqual(float(transit.gatech.lon) * 180.0 / ephem.pi, TEST_CONFIG.observer_lon)
        self.assertEqual(transit.gatech.elevation, TEST_CONFIG.observer_elevation_m)

    def test_metar_request_uses_configured_station(self):
        transit.apply_installation_config(TEST_CONFIG)
        transit.metar_t = None
        transit.metar_attempt_t = None
        observation = AwcMetar(
            icao_id="EPRA",
            obs_time=datetime.datetime.now(pytz.utc),
            altim=1015.0,
            raw_ob="EPRA METAR Q1015",
        )

        with patch.object(transit, "fetch_awc_metar", return_value=observation) as fetch:
            self.assertEqual(transit.get_metar_press(), 1015.0)

        fetch.assert_called_once_with(TEST_CONFIG.metar_station)

    def test_main_starts_independent_configured_sources(self):
        threads = [Mock(), Mock(), Mock(), Mock()]
        thread_factory = Mock(side_effect=threads)
        with patch.object(transit, "load_installation_config", return_value=TEST_CONFIG), \
                patch.object(transit, "initialize_daily_environment") as initialize_daily, \
                patch.object(transit, "get_metar_press") as get_pressure, \
                patch.object(transit.threading, "Thread", thread_factory), \
                patch.object(transit.time, "sleep", side_effect=KeyboardInterrupt):
            transit.main()

        initialize_daily.assert_called_once_with()
        get_pressure.assert_called_once_with()

        self.assertEqual(
            thread_factory.call_args_list,
            [
                call(target=transit.read_from_port,
                     args=(TEST_CONFIG.adsb_host, TEST_CONFIG.adsb_port,
                           transit.process_line, None)),
                call(target=transit.read_from_port,
                     args=(TEST_CONFIG.mlat_host, TEST_CONFIG.mlat_port,
                           transit.process_line, None)),
                call(target=transit.read_beast_intent,
                     args=(TEST_CONFIG.beast_host, TEST_CONFIG.beast_port)),
                call(target=transit.read_raw_adsb_track,
                     args=(TEST_CONFIG.raw_adsb_host,
                           TEST_CONFIG.raw_adsb_port, None)),
            ],
        )
        for thread in threads:
            thread.start.assert_called_once_with()
            thread.join.assert_called_once_with(timeout=2.0)
        self.assertEqual(
            transit.port_status,
            {TEST_CONFIG.adsb_port: False, TEST_CONFIG.mlat_port: False},
        )

    def test_replay_does_not_start_optional_live_raw_reader(self):
        original_clock = transit.clock
        threads = [Mock(), Mock()]
        thread_factory = Mock(side_effect=threads)
        try:
            transit.clock = ReplayClock()
            transit.clock.advance_to(datetime.datetime(
                2026, 8, 30, 10, 0, tzinfo=datetime.timezone.utc))
            with patch.object(
                    transit, "load_installation_config",
                    return_value=TEST_CONFIG), patch.object(
                    transit.threading, "Thread", thread_factory), patch.object(
                    transit.time, "sleep", side_effect=KeyboardInterrupt):
                transit.main()
        finally:
            transit.clock = original_clock

        self.assertEqual(len(thread_factory.call_args_list), 2)
        self.assertNotIn(
            transit.read_raw_adsb_track,
            [item.kwargs["target"] for item in thread_factory.call_args_list])

    def test_configuration_error_exits_cleanly_before_starting_threads(self):
        error = ConfigurationError("Invalid installation configuration:\n- OBSERVER_LAT is required")
        with patch.object(transit, "load_installation_config", side_effect=error), \
                patch.object(transit.threading, "Thread") as thread:
            with self.assertRaisesRegex(SystemExit, "OBSERVER_LAT is required"):
                transit.main()

        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
