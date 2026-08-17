import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

from metar import AWC_METAR_URL, AwcMetar, fetch_awc_metar, fetch_metar_text, parse_metar_qnh


class ParseMetarQnhTests(unittest.TestCase):
    def test_parses_q1013(self):
        self.assertEqual(parse_metar_qnh("METAR TEST 101200Z Q1013"), 1013)

    def test_parses_q0998(self):
        self.assertEqual(parse_metar_qnh("METAR TEST 101200Z Q0998"), 998)

    def test_returns_none_when_qnh_is_missing(self):
        self.assertIsNone(parse_metar_qnh("METAR TEST 101200Z"))

    def test_returns_none_when_qnh_is_outside_range(self):
        for text in ("METAR TEST Q0800", "METAR TEST Q1100"):
            with self.subTest(text=text):
                self.assertIsNone(parse_metar_qnh(text))


class FetchMetarTextTests(unittest.TestCase):
    @patch("metar.requests.get")
    def test_returns_text_for_http_200(self, get):
        get.return_value = Mock(status_code=200, text="METAR TEST Q1013")

        self.assertEqual(fetch_metar_text("https://weather.example/metar"), "METAR TEST Q1013")

        get.assert_called_once_with("https://weather.example/metar", timeout=5)

    @patch("metar.requests.get")
    def test_returns_none_for_non_200_response(self, get):
        get.return_value = Mock(status_code=503, text="unavailable")

        self.assertIsNone(fetch_metar_text("https://weather.example/metar"))

        get.assert_called_once_with("https://weather.example/metar", timeout=5)

    @patch("metar.requests.get")
    def test_request_exception_propagates(self, get):
        get.side_effect = requests.exceptions.ConnectionError("offline")

        with self.assertRaises(requests.exceptions.ConnectionError):
            fetch_metar_text("https://weather.example/metar")

        get.assert_called_once_with("https://weather.example/metar", timeout=5)


class FetchAwcMetarTests(unittest.TestCase):
    def response(self, payload, status=200):
        response = Mock(status_code=status)
        response.json.return_value = payload
        return response

    @patch("metar.requests.get")
    def test_returns_validated_epra_record(self, get):
        raw_ob = "METAR EPRA 172000Z 18008KT 8000 RA Q1005"
        get.return_value = self.response([{
            "icaoId": "EPRA",
            "obsTime": 1786996800,
            "altim": 1005,
            "rawOb": raw_ob,
        }])

        result = fetch_awc_metar("epra")

        self.assertEqual(
            result,
            AwcMetar(
                icao_id="EPRA",
                obs_time=datetime(2026, 8, 17, 20, 0, tzinfo=timezone.utc),
                altim=1005.0,
                raw_ob=raw_ob,
            ),
        )
        get.assert_called_once_with(
            AWC_METAR_URL,
            params={"ids": "EPRA", "format": "json"},
            headers={"User-Agent": "TransitWarning/1.0"},
            timeout=5,
        )

    @patch("metar.requests.get")
    def test_returns_standardized_kjfk_altimeter(self, get):
        raw_ob = "METAR KJFK 171951Z 01008KT 10SM BKN020 27/21 A2981"
        get.return_value = self.response([{
            "icaoId": "KJFK",
            "obsTime": 1786996260,
            "altim": 1009.6,
            "rawOb": raw_ob,
        }])

        result = fetch_awc_metar("KJFK")

        self.assertEqual(result.altim, 1009.6)
        self.assertEqual(result.raw_ob, raw_ob)
        self.assertEqual(result.obs_time, datetime(2026, 8, 17, 19, 51, tzinfo=timezone.utc))

    @patch("metar.requests.get")
    def test_returns_none_for_http_errors_and_no_content(self, get):
        for status in (204, 400, 500):
            with self.subTest(status=status):
                get.return_value = self.response([], status=status)
                self.assertIsNone(fetch_awc_metar("EPRA"))

    @patch("metar.requests.get")
    def test_returns_none_for_invalid_json(self, get):
        get.return_value = self.response([])
        get.return_value.json.side_effect = ValueError("invalid JSON")

        self.assertIsNone(fetch_awc_metar("EPRA"))

    @patch("metar.requests.get")
    def test_returns_none_for_invalid_payload_or_station_match(self, get):
        invalid_payloads = (
            {},
            [],
            [{"icaoId": "EPWA", "obsTime": 1786996800, "altim": 1005, "rawOb": "METAR"}],
            [
                {"icaoId": "EPRA", "obsTime": 1786996800, "altim": 1005, "rawOb": "METAR"},
                {"icaoId": "EPRA", "obsTime": 1786996800, "altim": 1005, "rawOb": "METAR"},
            ],
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                get.return_value = self.response(payload)
                self.assertIsNone(fetch_awc_metar("EPRA"))

    @patch("metar.requests.get")
    def test_returns_none_for_invalid_altimeter(self, get):
        for altim in (None, "1005", float("nan"), 800, 1100):
            with self.subTest(altim=altim):
                get.return_value = self.response([{
                    "icaoId": "EPRA",
                    "obsTime": 1786996800,
                    "altim": altim,
                    "rawOb": "METAR EPRA",
                }])
                self.assertIsNone(fetch_awc_metar("EPRA"))

    @patch("metar.requests.get")
    def test_returns_none_for_missing_or_invalid_observation_time(self, get):
        for obs_time in (None, "not-a-time", float("inf")):
            with self.subTest(obs_time=obs_time):
                get.return_value = self.response([{
                    "icaoId": "EPRA",
                    "obsTime": obs_time,
                    "altim": 1005,
                    "rawOb": "METAR EPRA",
                }])
                self.assertIsNone(fetch_awc_metar("EPRA"))

    @patch("metar.requests.get")
    def test_returns_none_for_request_exception(self, get):
        get.side_effect = requests.exceptions.Timeout("timeout")

        self.assertIsNone(fetch_awc_metar("EPRA"))

if __name__ == "__main__":
    unittest.main()
