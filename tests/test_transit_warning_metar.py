import datetime
import unittest
from unittest.mock import patch

import pytz

import transit_warning as transit
from metar import AwcMetar
from transit_clock import ReplayClock


class FakeClock:
    def __init__(self):
        self.current = datetime.datetime(2026, 8, 17, 12, 0, tzinfo=pytz.utc)

    def now_utc(self):
        return self.current

    def advance(self, seconds):
        self.current += datetime.timedelta(seconds=seconds)


class GetMetarPressTests(unittest.TestCase):
    def setUp(self):
        self.original_clock = transit.clock
        self.original_metar_t = transit.metar_t
        self.original_metar_attempt_t = transit.metar_attempt_t
        self.original_pressure = transit.pressure
        self.original_metar_station = transit.metar_station
        transit.clock = FakeClock()
        transit.metar_t = None
        transit.metar_attempt_t = None
        transit.pressure = 1013
        transit.metar_station = "EPRA"

    def tearDown(self):
        transit.clock = self.original_clock
        transit.metar_t = self.original_metar_t
        transit.metar_attempt_t = self.original_metar_attempt_t
        transit.pressure = self.original_pressure
        transit.metar_station = self.original_metar_station

    def observation(self, altim=1015.0, age_seconds=0, station="EPRA", raw_ob="METAR"):
        return AwcMetar(
            icao_id=station,
            obs_time=transit.clock.now_utc() - datetime.timedelta(seconds=age_seconds),
            altim=altim,
            raw_ob=raw_ob,
        )

    @patch.object(transit, "fetch_awc_metar")
    def test_first_valid_epra_qnh_is_stored(self, fetch):
        fetch.return_value = self.observation()
        self.assertEqual(transit.get_metar_press(), 1015.0)
        self.assertEqual(transit.pressure, 1015.0)
        self.assertEqual(transit.metar_t, transit.clock.now_utc())
        self.assertEqual(transit.metar_attempt_t, transit.clock.now_utc())
        fetch.assert_called_once_with("EPRA")

    @patch.object(transit, "fetch_awc_metar")
    def test_kjfk_uses_altim_without_parsing_raw_a_value(self, fetch):
        transit.metar_station = "KJFK"
        fetch.return_value = self.observation(
            altim=1009.6, station="KJFK", raw_ob="KJFK 171151Z A2981"
        )
        self.assertEqual(transit.get_metar_press(), 1009.6)
        self.assertEqual(transit.pressure, 1009.6)

    @patch.object(transit, "fetch_awc_metar")
    def test_valid_qnh_cache_boundary(self, fetch):
        fetch.side_effect = (
            self.observation(1015.0),
            self.observation(1009.0),
            self.observation(1008.0),
        )
        self.assertEqual(transit.get_metar_press(), 1015.0)
        transit.clock.advance(899.999)
        self.assertEqual(transit.get_metar_press(), 1015.0)
        self.assertEqual(fetch.call_count, 1)
        transit.clock.advance(0.001)
        self.assertEqual(transit.get_metar_press(), 1009.0)
        self.assertEqual(fetch.call_count, 2)
        transit.clock.advance(900.001)
        self.assertEqual(transit.get_metar_press(), 1008.0)
        self.assertEqual(fetch.call_count, 3)

    @patch.object(transit, "fetch_awc_metar")
    def test_accepts_observation_age_89_59_999(self, fetch):
        fetch.return_value = self.observation(age_seconds=5399.999)
        self.assertEqual(transit.get_metar_press(), 1015.0)

    @patch.object(transit, "fetch_awc_metar")
    def test_accepts_observation_age_exactly_90_minutes(self, fetch):
        fetch.return_value = self.observation(age_seconds=5400)
        self.assertEqual(transit.get_metar_press(), 1015.0)

    @patch.object(transit, "fetch_awc_metar")
    def test_stale_observation_keeps_previous_qnh(self, fetch):
        transit.pressure = 1007.0
        transit.metar_t = transit.clock.now_utc() - datetime.timedelta(seconds=901)
        previous_metar_t = transit.metar_t
        fetch.return_value = self.observation(age_seconds=5400.001)
        self.assertEqual(transit.get_metar_press(), 1007.0)
        self.assertEqual(transit.pressure, 1007.0)
        self.assertEqual(transit.metar_t, previous_metar_t)

    @patch.object(transit, "fetch_awc_metar")
    def test_stale_first_observation_keeps_initial_pressure(self, fetch):
        fetch.return_value = self.observation(age_seconds=5400.001)
        self.assertEqual(transit.get_metar_press(), 1013)
        self.assertIsNone(transit.metar_t)

    @patch.object(transit, "fetch_awc_metar")
    def test_future_observation_is_rejected(self, fetch):
        fetch.return_value = self.observation(age_seconds=-0.001)
        self.assertEqual(transit.get_metar_press(), 1013)
        self.assertIsNone(transit.metar_t)

    @patch.object(transit, "fetch_awc_metar")
    def test_retry_after_stale_observation_uses_60_second_boundary(self, fetch):
        fetch.side_effect = (
            self.observation(age_seconds=5401),
            self.observation(1009.0),
        )
        self.assertEqual(transit.get_metar_press(), 1013)
        transit.clock.advance(59.999)
        self.assertEqual(transit.get_metar_press(), 1013)
        self.assertEqual(fetch.call_count, 1)
        transit.clock.advance(0.001)
        self.assertEqual(transit.get_metar_press(), 1009.0)
        self.assertEqual(fetch.call_count, 2)

    @patch.object(transit, "fetch_awc_metar")
    def test_retry_after_future_observation_uses_60_second_boundary(self, fetch):
        fetch.side_effect = (
            self.observation(age_seconds=-1),
            self.observation(1009.0),
        )
        self.assertEqual(transit.get_metar_press(), 1013)
        transit.clock.advance(60)
        self.assertEqual(transit.get_metar_press(), 1009.0)
        self.assertEqual(fetch.call_count, 2)

    @patch.object(transit, "fetch_awc_metar", return_value=None)
    def test_provider_failure_keeps_previous_qnh(self, fetch):
        transit.pressure = 1007.0
        transit.metar_t = transit.clock.now_utc() - datetime.timedelta(seconds=901)
        self.assertEqual(transit.get_metar_press(), 1007.0)
        self.assertEqual(transit.pressure, 1007.0)

    @patch.object(transit, "fetch_awc_metar")
    def test_replay_does_not_fetch_or_change_metar_state(self, fetch):
        replay_clock = ReplayClock()
        replay_clock.advance_to(transit.clock.now_utc())
        transit.clock = replay_clock
        transit.pressure = 1007.0
        original_metar_t = datetime.datetime(2026, 8, 17, 10, 0, tzinfo=pytz.utc)
        original_attempt_t = datetime.datetime(2026, 8, 17, 10, 1, tzinfo=pytz.utc)
        transit.metar_t = original_metar_t
        transit.metar_attempt_t = original_attempt_t
        self.assertEqual(transit.get_metar_press(), 1007.0)
        fetch.assert_not_called()
        self.assertEqual(transit.pressure, 1007.0)
        self.assertIs(transit.metar_t, original_metar_t)
        self.assertIs(transit.metar_attempt_t, original_attempt_t)


if __name__ == "__main__":
    unittest.main()
