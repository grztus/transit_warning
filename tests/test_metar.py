import unittest
from unittest.mock import Mock, patch

import requests

from metar import fetch_metar_text, parse_metar_qnh


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


if __name__ == "__main__":
    unittest.main()
