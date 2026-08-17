import datetime
import time
import unittest

import pytz

import transit_time
import transit_warning as transit
from transit_clock import ReplayClock
from transit_time import port_timestamp_to_utc


class PortTimestampContractTests(unittest.TestCase):
    def setUp(self):
        self.timestamp = datetime.datetime(2026, 8, 16, 12, 30, 0, 123000, tzinfo=pytz.utc)

    def test_port_30003_matches_previous_altzone_calculation(self):
        expected = self.timestamp + datetime.timedelta(hours=time.altzone / 60 / 60)
        self.assertEqual(port_timestamp_to_utc(self.timestamp, 30003), expected)

    def test_port_30106_keeps_utc_without_offset(self):
        self.assertEqual(port_timestamp_to_utc(self.timestamp, 30106), self.timestamp)

    def test_result_is_timezone_aware_utc(self):
        for port in (30003, 30106):
            with self.subTest(port=port):
                result = port_timestamp_to_utc(self.timestamp.replace(tzinfo=None), port)
                self.assertIsNotNone(result.tzinfo)
                self.assertEqual(result.utcoffset(), datetime.timedelta(0))


class ProcessLineTimestampConversionTests(unittest.TestCase):
    def test_generated_and_logged_use_the_same_conversion_function(self):
        original_clock = transit.clock
        original_converter = transit.port_timestamp_to_utc
        original_tabela = transit.tabela
        calls = []

        def recording_converter(timestamp, port):
            calls.append((timestamp, port))
            return transit_time.port_timestamp_to_utc(timestamp, port)

        try:
            transit.clock = ReplayClock()
            transit.replay_time_initialized = False
            transit.metar_t = transit.metar_attempt_t = transit.aktual_t = transit.last_t = None
            transit.gong_t = transit.last_update_time = None
            transit.plane_dict = {}
            transit.tabela = lambda: (0, 0, 0, 0)
            transit.port_timestamp_to_utc = recording_converter
            line = "MSG,1,1,1,ABC123,1,2026/08/16,12:30:00.000,2026/08/16,12:30:00.050,TEST123"

            transit.process_line(line, 30003)

            self.assertEqual(len(calls), 2)
            self.assertEqual([port for _, port in calls], [30003, 30003])
            self.assertEqual(calls[0][0].strftime("%H:%M:%S.%f"), "12:30:00.000000")
            self.assertEqual(calls[1][0].strftime("%H:%M:%S.%f"), "12:30:00.050000")
        finally:
            transit.clock = original_clock
            transit.port_timestamp_to_utc = original_converter
            transit.tabela = original_tabela


if __name__ == "__main__":
    unittest.main()
