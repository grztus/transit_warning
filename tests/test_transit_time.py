import datetime
import time
import unittest
from unittest.mock import Mock, patch

import transit_time
import transit_warning as transit
from transit_clock import RealClock, ReplayClock
from transit_time import (
    AdsBTimestampOffsetValidator,
    AmbiguousPortTimestampError,
    NonexistentPortTimestampError,
    port_timestamp_to_utc,
)


UTC = datetime.timezone.utc
WARSAW = "Europe/Warsaw"


class PortTimestampContractTests(unittest.TestCase):
    def test_warsaw_summer_uses_utc_plus_two(self):
        value = datetime.datetime(2026, 8, 17, 23, 26, 42)
        self.assertEqual(port_timestamp_to_utc(value, 30003, WARSAW),
                         datetime.datetime(2026, 8, 17, 21, 26, 42, tzinfo=UTC))

    def test_warsaw_winter_uses_utc_plus_one(self):
        value = datetime.datetime(2026, 1, 17, 23, 26, 42)
        self.assertEqual(port_timestamp_to_utc(value, 30003, WARSAW),
                         datetime.datetime(2026, 1, 17, 22, 26, 42, tzinfo=UTC))

    def test_utc_source_timezone(self):
        value = datetime.datetime(2026, 8, 17, 23, 26, 42)
        self.assertEqual(port_timestamp_to_utc(value, 30003, "UTC"), value.replace(tzinfo=UTC))

    def test_real_recording_characteristic(self):
        value = datetime.datetime(2026, 8, 16, 12, 4, 18)
        self.assertEqual(port_timestamp_to_utc(value, 30003, WARSAW),
                         datetime.datetime(2026, 8, 16, 10, 4, 18, tzinfo=UTC))

    def test_result_does_not_depend_on_system_timezone_globals(self):
        value = datetime.datetime(2026, 8, 17, 23, 26, 42)
        expected = port_timestamp_to_utc(value, 30003, WARSAW)
        with patch.object(time, "timezone", 0), patch.object(time, "altzone", 0):
            self.assertEqual(port_timestamp_to_utc(value, 30003, WARSAW), expected)

    def test_port_30106_keeps_utc_semantics(self):
        value = datetime.datetime(2026, 8, 16, 12, 30, 0, 123000)
        self.assertEqual(port_timestamp_to_utc(value, 30106, WARSAW), value.replace(tzinfo=UTC))

    def test_configured_adsb_port_uses_adsb_semantics(self):
        value = datetime.datetime(2026, 8, 17, 23, 26, 42)
        self.assertEqual(port_timestamp_to_utc(value, 31003, WARSAW, 31003),
                         datetime.datetime(2026, 8, 17, 21, 26, 42, tzinfo=UTC))

    def test_rejects_ambiguous_autumn_timestamp(self):
        with self.assertRaises(AmbiguousPortTimestampError):
            port_timestamp_to_utc(datetime.datetime(2026, 10, 25, 2, 30), 30003, WARSAW)

    def test_rejects_nonexistent_spring_timestamp(self):
        with self.assertRaises(NonexistentPortTimestampError):
            port_timestamp_to_utc(datetime.datetime(2026, 3, 29, 2, 30), 30003, WARSAW)


class LiveOffsetValidatorTests(unittest.TestCase):
    def test_reports_ok_after_several_close_samples(self):
        messages = []
        validator = AdsBTimestampOffsetValidator(WARSAW, messages.append, sample_count=3)
        for microseconds in (100000, 200000, 300000):
            raw = datetime.datetime(2026, 8, 17, 23, 26, 42, microseconds)
            validator.observe(raw, datetime.datetime(2026, 8, 17, 21, 26, 42, tzinfo=UTC))
        self.assertTrue(validator.complete)
        self.assertEqual(len(messages), 1)
        self.assertIn("Expected offset:           UTC+02:00", messages[0])
        self.assertIn("Timestamp check:           OK", messages[0])

    def test_mismatched_offset_warns_but_does_not_raise(self):
        messages = []
        validator = AdsBTimestampOffsetValidator(WARSAW, messages.append, sample_count=1)
        validator.observe(datetime.datetime(2026, 8, 17, 21, 26, 42),
                          datetime.datetime(2026, 8, 17, 21, 26, 42, tzinfo=UTC))
        self.assertIn("Timestamp check:           WARNING", messages[0])

    def test_diagnostic_failure_is_fail_open(self):
        reporter = Mock(side_effect=OSError("console unavailable"))
        validator = AdsBTimestampOffsetValidator(WARSAW, reporter, sample_count=1)
        validator.observe(datetime.datetime(2026, 8, 17, 23, 26, 42),
                          datetime.datetime(2026, 8, 17, 21, 26, 42, tzinfo=UTC))
        self.assertTrue(validator.complete)


class ProcessLineTimestampConversionTests(unittest.TestCase):
    def test_generated_and_logged_use_the_same_conversion_function(self):
        original = (transit.clock, transit.port_timestamp_to_utc, transit.tabela,
                    transit.adsb_timestamp_timezone, transit.adsb_port,
                    transit.adsb_timestamp_validator)
        calls = []

        def recording_converter(timestamp, port, timezone_name, adsb_port):
            calls.append((timestamp, port, timezone_name, adsb_port))
            return transit_time.port_timestamp_to_utc(timestamp, port, timezone_name, adsb_port)

        try:
            transit.clock = ReplayClock()
            transit.replay_time_initialized = False
            transit.metar_t = transit.metar_attempt_t = transit.aktual_t = transit.last_t = None
            transit.gong_t = transit.last_update_time = None
            transit.plane_dict = {}
            transit.tabela = lambda: (0, 0, 0, 0)
            transit.adsb_timestamp_timezone = WARSAW
            transit.adsb_port = 30003
            transit.adsb_timestamp_validator = None
            transit.port_timestamp_to_utc = recording_converter
            line = "MSG,1,1,1,ABC123,1,2026/08/16,12:30:00.000,2026/08/16,12:30:00.050,TEST123"
            transit.process_line(line, 30003)
            self.assertEqual(len(calls), 2)
            self.assertEqual([item[1] for item in calls], [30003, 30003])
            self.assertTrue(all(item[2] == WARSAW for item in calls))
            self.assertEqual(transit.clock.now_utc(),
                             datetime.datetime(2026, 8, 16, 10, 30, 0, 50000, tzinfo=UTC))
        finally:
            (transit.clock, transit.port_timestamp_to_utc, transit.tabela,
             transit.adsb_timestamp_timezone, transit.adsb_port,
             transit.adsb_timestamp_validator) = original

    def test_live_validator_failure_does_not_prevent_processing(self):
        original = (transit.clock, transit.adsb_timestamp_validator,
                    transit.adsb_timestamp_timezone, transit.adsb_port, transit.plane_dict)
        try:
            transit.clock = RealClock()
            transit.adsb_timestamp_timezone = WARSAW
            transit.adsb_port = 30003
            transit.plane_dict = {}
            transit.adsb_timestamp_validator = AdsBTimestampOffsetValidator(
                WARSAW, Mock(side_effect=OSError), sample_count=1)
            transit.process_line(
                "MSG,1,1,1,ABC123,1,2026/08/16,12:30:00.000,2026/08/16,12:30:00.050,TEST123",
                30003)
            self.assertTrue(transit.adsb_timestamp_validator.complete)
        finally:
            (transit.clock, transit.adsb_timestamp_validator,
             transit.adsb_timestamp_timezone, transit.adsb_port, transit.plane_dict) = original


if __name__ == "__main__":
    unittest.main()
