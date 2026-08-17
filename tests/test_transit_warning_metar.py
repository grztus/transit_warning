import datetime
import unittest
from unittest.mock import patch

import pytz
import requests

import transit_warning as transit


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
        self.original_metar_url = transit.metar_url
        transit.clock = FakeClock()
        transit.metar_t = None
        transit.metar_attempt_t = None
        transit.pressure = 1013
        transit.metar_url = "https://weather.example/metar"

    def tearDown(self):
        transit.clock = self.original_clock
        transit.metar_t = self.original_metar_t
        transit.metar_attempt_t = self.original_metar_attempt_t
        transit.pressure = self.original_pressure
        transit.metar_url = self.original_metar_url

    @patch.object(transit, "fetch_metar_text", return_value="METAR TEST Q1015")
    def test_first_valid_qnh_is_stored(self, fetch):
        self.assertEqual(transit.get_metar_press(), 1015)
        self.assertEqual(transit.pressure, 1015)
        self.assertEqual(transit.metar_t, transit.clock.now_utc())
        self.assertEqual(transit.metar_attempt_t, transit.clock.now_utc())

    @patch.object(
        transit,
        "fetch_metar_text",
        side_effect=("METAR Q1015", "METAR Q1009", "METAR Q1008"),
    )
    def test_valid_qnh_cache_boundary(self, fetch):
        self.assertEqual(transit.get_metar_press(), 1015)
        transit.clock.advance(899.999)

        self.assertEqual(transit.get_metar_press(), 1015)
        self.assertEqual(fetch.call_count, 1)

        transit.clock.advance(0.001)

        self.assertEqual(transit.get_metar_press(), 1009)
        self.assertEqual(fetch.call_count, 2)

        transit.clock.advance(900.001)

        self.assertEqual(transit.get_metar_press(), 1008)
        self.assertEqual(fetch.call_count, 3)

    @patch.object(transit, "fetch_metar_text", side_effect=("METAR Q1015", "METAR Q1009"))
    def test_retries_after_valid_cache_expires(self, fetch):
        self.assertEqual(transit.get_metar_press(), 1015)
        transit.clock.advance(901)

        self.assertEqual(transit.get_metar_press(), 1009)
        self.assertEqual(fetch.call_count, 2)

    def set_previous_qnh_expired(self):
        transit.pressure = 1007
        transit.metar_t = transit.clock.now_utc() - datetime.timedelta(seconds=901)

    @patch.object(transit, "fetch_metar_text", return_value=None)
    def test_http_failure_keeps_previous_qnh(self, fetch):
        self.set_previous_qnh_expired()
        self.assertEqual(transit.get_metar_press(), 1007)
        self.assertEqual(transit.pressure, 1007)

    @patch.object(transit, "fetch_metar_text")
    def test_request_exception_keeps_previous_qnh(self, fetch):
        self.set_previous_qnh_expired()
        fetch.side_effect = requests.exceptions.ConnectionError("offline")

        with patch("builtins.print"):
            self.assertEqual(transit.get_metar_press(), 1007)

        self.assertEqual(transit.pressure, 1007)

    @patch.object(transit, "fetch_metar_text", return_value="METAR WITHOUT PRESSURE")
    def test_missing_qnh_keeps_previous_qnh(self, fetch):
        self.set_previous_qnh_expired()
        self.assertEqual(transit.get_metar_press(), 1007)
        self.assertEqual(transit.pressure, 1007)

    @patch.object(transit, "fetch_metar_text", return_value="METAR Q1200")
    def test_out_of_range_qnh_does_not_change_pressure(self, fetch):
        self.set_previous_qnh_expired()
        self.assertEqual(transit.get_metar_press(), 1007)
        self.assertEqual(transit.pressure, 1007)

    @patch.object(transit, "fetch_metar_text", return_value=None)
    def test_first_failure_returns_initial_pressure(self, fetch):
        self.assertEqual(transit.get_metar_press(), 1013)
        self.assertEqual(transit.pressure, 1013)
        self.assertIsNone(transit.metar_t)

    @patch.object(transit, "fetch_metar_text", return_value=None)
    def test_does_not_retry_before_60_seconds_after_failure(self, fetch):
        self.assertEqual(transit.get_metar_press(), 1013)
        transit.clock.advance(59)

        self.assertEqual(transit.get_metar_press(), 1013)

        fetch.assert_called_once_with(transit.metar_url)

    @patch.object(transit, "fetch_metar_text", side_effect=(None, "METAR Q1009"))
    def test_retries_at_60_seconds_after_failure(self, fetch):
        self.assertEqual(transit.get_metar_press(), 1013)
        transit.clock.advance(60)

        self.assertEqual(transit.get_metar_press(), 1009)
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
