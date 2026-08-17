import datetime
import unittest
from unittest.mock import patch

import requests

import transit_warning as transit


class GetMetarPressCharacterizationTests(unittest.TestCase):
    def setUp(self):
        self.original_metar_t = transit.metar_t
        self.original_pressure = transit.pressure
        self.original_metar_url = transit.metar_url
        transit.metar_url = "https://weather.example/metar"
        transit.pressure = 1007
        transit.metar_t = transit.clock.now_utc() - datetime.timedelta(seconds=901)

    def tearDown(self):
        transit.metar_t = self.original_metar_t
        transit.pressure = self.original_pressure
        transit.metar_url = self.original_metar_url

    @patch.object(transit, "fetch_metar_text", return_value=None)
    def test_http_failure_returns_1013_but_keeps_previous_pressure(self, fetch):
        self.assertEqual(transit.get_metar_press(), 1013)
        self.assertEqual(transit.pressure, 1007)
        fetch.assert_called_once_with(transit.metar_url)

    @patch.object(transit, "fetch_metar_text")
    def test_request_exception_returns_previous_pressure(self, fetch):
        fetch.side_effect = requests.exceptions.ConnectionError("offline")

        with patch("builtins.print") as output:
            self.assertEqual(transit.get_metar_press(), 1007)

        output.assert_called_once()

    @patch.object(transit, "fetch_metar_text", return_value="METAR TEST Q1200")
    def test_out_of_range_qnh_preserves_existing_global_state_update(self, fetch):
        self.assertEqual(transit.get_metar_press(), 1013)
        self.assertEqual(transit.pressure, 1200)

    @patch.object(transit, "fetch_metar_text")
    def test_cached_pressure_avoids_fetch(self, fetch):
        transit.metar_t = transit.clock.now_utc()

        self.assertEqual(transit.get_metar_press(), 1007)

        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
