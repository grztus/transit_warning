import datetime
import math
import unittest
from unittest.mock import Mock, patch

import pytz

import transit_warning as transit
from config import InstallationConfig
from environment import EnvironmentEvent, EnvironmentReplay
from transit_clock import RealClock, ReplayClock, clock_from_args


TEST_CONFIG = InstallationConfig(
    observer_lat=51.1111,
    observer_lon=21.1111,
    observer_elevation_m=111.0,
    transition_altitude_ft=6500,
    adsb_host="127.0.0.1",
    adsb_port=30003,
    adsb_timestamp_timezone="Europe/Warsaw",
    mlat_host="127.0.0.1",
    mlat_port=30106,
    metar_station="EPRA",
)


def message(generated, logged, icao="ABC123"):
    generated_date, generated_time = generated.split()
    logged_date, logged_time = logged.split()
    return "MSG,1,1,1,{icao},1,{gd},{gt},{ld},{lt},TEST123".format(
        icao=icao,
        gd=generated_date,
        gt=generated_time,
        ld=logged_date,
        lt=logged_time,
    )


def utc(value):
    return datetime.datetime.strptime(value, "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=pytz.utc)


class SunMoonTableContractTests(unittest.TestCase):
    def test_real_table_returns_sun_then_moon(self):
        now = utc("2024/05/18 12:00:00.000")
        sun = Mock(alt=math.radians(31.5), az=math.radians(141.2))
        moon = Mock(alt=math.radians(-17.4), az=math.radians(278.6))
        original_clock = transit.clock
        original_gatech = transit.gatech
        original_last_t = transit.last_t
        try:
            transit.clock = Mock()
            transit.clock.now_utc.return_value = now
            transit.clock.ephem_now.return_value = "controlled ephem date"
            transit.gatech = Mock()
            transit.last_t = now
            with patch.object(transit.ephem, "Sun", return_value=sun), \
                    patch.object(transit.ephem, "Moon", return_value=moon):
                result = transit.tabela()
        finally:
            transit.clock = original_clock
            transit.gatech = original_gatech
            transit.last_t = original_last_t

        self.assertEqual(result, (31.5, 141.2, -17.4, 278.6))
        sun.compute.assert_called_once()
        moon.compute.assert_called_once()


class PressureAltitudeCorrectionTests(unittest.TestCase):
    def test_standard_pressure_does_not_change_altitude(self):
        self.assertEqual(transit.correct_pressure_altitude(10000, 1013.25), 10000)

    def test_lower_qnh_reduces_altitude(self):
        self.assertLess(transit.correct_pressure_altitude(10000, 1000), 10000)

    def test_higher_qnh_increases_altitude(self):
        self.assertGreater(transit.correct_pressure_altitude(10000, 1020), 10000)

    def test_decimal_qnh_is_preserved_in_linear_correction(self):
        self.assertAlmostEqual(
            transit.correct_pressure_altitude(10000, 1008.5),
            10000 + (1008.5 - 1013.25) * 26,
        )


class ProcessLineReplayClockTests(unittest.TestCase):
    def setUp(self):
        transit.apply_installation_config(TEST_CONFIG)
        self.original_clock = transit.clock
        self.original_tabela = transit.tabela
        self.original_transit_pred = transit.transit_pred
        self.original_environment_replay = transit.environment_replay
        self.original_pressure = transit.pressure
        transit.clock = ReplayClock()
        transit.replay_time_initialized = False
        transit.metar_t = None
        transit.metar_attempt_t = None
        transit.aktual_t = None
        transit.last_t = None
        transit.gong_t = None
        transit.last_update_time = None
        transit.plane_dict = {}
        transit.environment_replay = None
        transit.pressure = 1013
        transit.tabela = lambda: (0, 0, 0, 0)

    def tearDown(self):
        transit.clock = self.original_clock
        transit.tabela = self.original_tabela
        transit.transit_pred = self.original_transit_pred
        transit.environment_replay = self.original_environment_replay
        transit.pressure = self.original_pressure

    def process(self, generated, logged, port=30106, icao="ABC123"):
        transit.process_line(message(generated, logged, icao), port)

    def qnh(self, timestamp, value):
        return EnvironmentEvent(1, utc(timestamp), "qnh", value, "test")

    def set_environment(self, *events):
        transit.environment_replay = EnvironmentReplay(events)

    def msg3(self, timestamp, icao="ABC123", altitude="10000",
             latitude="51.2", longitude="21.2"):
        return (
            "MSG,3,1,1,{icao},1,{date},{time},{date},{time},,{altitude},"
            "180,,{latitude},{longitude}".format(
                icao=icao,
                date=timestamp.split()[0],
                time=timestamp.split()[1],
                altitude=altitude,
                latitude=latitude,
                longitude=longitude,
            )
        )

    @staticmethod
    def prediction(altitude, time_to_transit):
        return (51.2, 21.2, 120.0, altitude, 17.9, 33.7,
                time_to_transit, 0, 120.0, 37.9, None)

    def test_generated_stays_on_record_and_logged_initializes_clock_and_globals(self):
        generated = "2024/05/18 12:00:00.000"
        logged = "2024/05/18 12:00:00.250"
        self.process(generated, logged)

        generated_utc = utc(generated)
        logged_utc = utc(logged)
        self.assertEqual(transit.plane_dict["ABC123"][0], generated_utc)
        self.assertEqual(transit.clock.now_utc(), logged_utc)
        self.assertIsNone(transit.metar_t)
        self.assertIsNone(transit.metar_attempt_t)
        self.assertEqual(transit.aktual_t, logged_utc)
        self.assertEqual(transit.last_t, logged_utc - datetime.timedelta(seconds=10))
        self.assertEqual(transit.gong_t, logged_utc)
        self.assertEqual(transit.last_update_time, logged_utc)
        self.assertTrue(transit.replay_time_initialized)

    def test_generated_can_move_backward_while_logged_moves_forward(self):
        self.process("2024/05/18 12:00:10.000", "2024/05/18 12:00:10.100")
        self.process("2024/05/18 12:00:09.000", "2024/05/18 12:00:11.100")
        self.assertEqual(transit.plane_dict["ABC123"][0], utc("2024/05/18 12:00:09.000"))
        self.assertEqual(transit.clock.now_utc(), utc("2024/05/18 12:00:11.100"))

    def test_older_and_equal_logged_values_do_not_move_clock_backward(self):
        self.process("2024/05/18 12:00:00.000", "2024/05/18 12:00:10.000")
        self.process("2024/05/18 12:00:01.000", "2024/05/18 12:00:09.000")
        self.process("2024/05/18 12:00:02.000", "2024/05/18 12:00:10.000")
        self.assertEqual(transit.clock.now_utc(), utc("2024/05/18 12:00:10.000"))
        self.assertEqual(transit.plane_dict["ABC123"][0], utc("2024/05/18 12:00:02.000"))

    def test_port_30003_applies_configured_timezone_to_both_timestamps(self):
        self.process("2024/05/18 12:00:00.000", "2024/05/18 12:00:01.000", port=30003)
        self.assertEqual(transit.plane_dict["ABC123"][0], utc("2024/05/18 10:00:00.000"))
        self.assertEqual(transit.clock.now_utc(), utc("2024/05/18 10:00:01.000"))

    def test_port_30106_keeps_both_timestamps_as_utc(self):
        self.process("2024/05/18 12:00:00.000", "2024/05/18 12:00:01.000", port=30106)
        self.assertEqual(transit.plane_dict["ABC123"][0], utc("2024/05/18 12:00:00.000"))
        self.assertEqual(transit.clock.now_utc(), utc("2024/05/18 12:00:01.000"))

    def test_historical_record_age_uses_replay_time_and_is_not_removed(self):
        self.process("2024/05/18 12:00:00.000", "2024/05/18 12:00:00.050")
        age = (transit.clock.now_utc() - transit.plane_dict["ABC123"][0]).total_seconds()
        self.assertEqual(age, 0.05)
        self.assertIn("ABC123", transit.plane_dict)

    def test_single_qnh_event_is_applied(self):
        self.set_environment(self.qnh("2024/05/18 12:00:00.000", 1008.5))
        self.process("2024/05/18 12:00:00.000", "2024/05/18 12:00:00.000")
        self.assertEqual(transit.pressure, 1008.5)

    def test_multiple_qnh_changes_are_applied(self):
        self.set_environment(
            self.qnh("2024/05/18 12:00:00.000", 1008.5),
            self.qnh("2024/05/18 12:00:05.000", 1009.2),
        )
        self.process("2024/05/18 12:00:00.000", "2024/05/18 12:00:00.000")
        self.assertEqual(transit.pressure, 1008.5)
        self.process("2024/05/18 12:00:05.000", "2024/05/18 12:00:05.000")
        self.assertEqual(transit.pressure, 1009.2)

    def test_event_between_messages_is_applied_at_next_message(self):
        self.set_environment(self.qnh("2024/05/18 12:00:03.000", 1007.0))
        self.process("2024/05/18 12:00:00.000", "2024/05/18 12:00:00.000")
        self.assertEqual(transit.pressure, 1013)
        self.process("2024/05/18 12:00:04.000", "2024/05/18 12:00:04.000")
        self.assertEqual(transit.pressure, 1007.0)

    def test_equal_timestamp_qnh_is_used_before_altitude_correction(self):
        self.set_environment(self.qnh("2024/05/18 12:00:00.000", 1000.0))
        line = (
            "MSG,5,1,1,ABC123,1,2024/05/18,12:00:00.000,"
            "2024/05/18,12:00:00.000,TEST123,10000"
        )
        transit.process_line(line, 30106)
        expected_metres = (10000 + (1000.0 - 1013.25) * 26) * 0.3048
        self.assertEqual(transit.plane_dict["ABC123"][4], expected_metres)

    def test_msg3_and_msg5_use_identical_correction_below_old_threshold(self):
        transit.pressure = 1000.0
        msg5 = (
            "MSG,5,1,1,MSG005,1,2024/05/18,12:00:00.000,"
            "2024/05/18,12:00:00.000,TEST123,5000"
        )
        msg3 = (
            "MSG,3,1,1,MSG003,1,2024/05/18,12:00:00.000,"
            "2024/05/18,12:00:00.000,,5000,,,51.2,21.2"
        )

        transit.process_line(msg5, 30106)
        transit.process_line(msg3, 30106)

        expected_metres = transit.correct_pressure_altitude(5000, 1000.0) * 0.3048
        self.assertEqual(transit.plane_dict["MSG005"][4], expected_metres)
        self.assertEqual(transit.plane_dict["MSG003"][4], expected_metres)

    def test_missing_prediction_clears_old_sun_and_moon_blocks(self):
        timestamp = "2024/05/18 12:00:00.000"
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3(timestamp), 30106)
        self.assertEqual(transit.plane_dict["ABC123"][22], 130)
        self.assertEqual(transit.plane_dict["ABC123"][26], 120)

        transit.transit_pred = Mock(side_effect=[0, 0])
        transit.process_line(
            self.msg3("2024/05/18 12:00:01.000"), 30106)

        self.assertEqual(transit.plane_dict["ABC123"][18:28], [""] * 10)

    def test_moon_below_horizon_clears_previous_moon_prediction(self):
        timestamp = "2024/05/18 12:00:00.000"
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3(timestamp), 30106)
        transit.moon_alt = -35.3
        transit.sun_alt = 37.9

        original_prediction = self.prediction(38.0, 140)
        transit.transit_pred = Mock(side_effect=lambda *args: (
            0 if args[-2] < 0.1 else original_prediction))
        transit.process_line(
            self.msg3("2024/05/18 12:00:01.000"), 30106)

        self.assertEqual(transit.plane_dict["ABC123"][23:28], [""] * 5)
        self.assertEqual(transit.plane_dict["ABC123"][22], 140)

    def test_prediction_over_900_seconds_clears_old_candidate(self):
        timestamp = "2024/05/18 12:00:00.000"
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3(timestamp), 30106)

        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 901), self.prediction(38.0, 901)])
        transit.process_line(
            self.msg3("2024/05/18 12:00:01.000"), 30106)

        self.assertEqual(transit.plane_dict["ABC123"][18:28], [""] * 10)

    def test_msg3_without_altitude_updates_position_without_type_error(self):
        transit.process_line(
            self.msg3("2024/05/18 12:00:00.000", altitude=""), 30003)
        transit.process_line(
            self.msg3("2024/05/18 12:00:01.000", icao="VALID"), 30003)

        self.assertEqual(transit.plane_dict["ABC123"][2:4], [51.2, 21.2])
        self.assertEqual(transit.plane_dict["ABC123"][4], "")
        self.assertIn("VALID", transit.plane_dict)

    def test_msg3_without_altitude_preserves_height_and_updates_position(self):
        transit.process_line(
            self.msg3("2024/05/18 12:00:00.000"), 30003)
        previous_elevation = transit.plane_dict["ABC123"][4]

        transit.process_line(self.msg3(
            "2024/05/18 12:00:01.000", altitude="",
            latitude="51.3", longitude="21.4"), 30003)

        self.assertEqual(transit.plane_dict["ABC123"][2:4], [51.3, 21.4])
        self.assertEqual(transit.plane_dict["ABC123"][4], previous_elevation)
        self.assertTrue(isinstance(transit.plane_dict["ABC123"][7], float))

    def test_missing_environment_file_keeps_fallback(self):
        transit.configure_environment_replay(None)
        self.process("2024/05/18 12:00:00.000", "2024/05/18 12:00:00.000")
        self.assertEqual(transit.pressure, 1013)

    @patch.object(transit, "fetch_awc_metar")
    def test_replay_altitude_processing_does_not_request_awc(self, fetch):
        line = (
            "MSG,5,1,1,ABC123,1,2024/05/18,12:00:00.000,"
            "2024/05/18,12:00:00.000,TEST123,10000"
        )
        transit.process_line(line, 30106)
        fetch.assert_not_called()

    def test_first_complete_mlat_message_initializes_ephemerides_before_prediction(self):
        historical_time = utc("2024/05/18 12:13:09.187")
        table_times = []
        prediction_states = []

        def historical_table():
            table_times.append(transit.clock.now_utc())
            return 31.5, 141.2, -17.4, 278.6

        def record_prediction(*args):
            prediction_states.append((
                transit.sun_alt,
                transit.sun_az,
                transit.moon_alt,
                transit.moon_az,
                transit.clock.now_utc(),
                args[-2],
                args[-1],
            ))
            return 0

        transit.tabela = historical_table
        transit.transit_pred = record_prediction
        line = "MLAT,3,1,1,48B5CF,1,2024/05/18,12:13:09.187,2024/05/18,12:13:09.187,,1687,97,295,51.7608,21.0416,200,,,,,,,,"

        transit.process_line(line, 30106)

        self.assertEqual(table_times, [historical_time, historical_time])
        self.assertEqual(
            prediction_states,
            [
                (31.5, 141.2, -17.4, 278.6,
                 historical_time, -17.4, 278.6),
                (31.5, 141.2, -17.4, 278.6,
                 historical_time, 31.5, 141.2),
            ],
        )
        self.assertEqual(transit.plane_dict["48B5CF"][0], historical_time)
        self.assertEqual(transit.plane_dict["48B5CF"][2:4], [51.7608, 21.0416])


class RealClockIntegrationTests(unittest.TestCase):
    def test_default_clock_remains_real_and_replay_update_is_noop(self):
        self.assertIsInstance(clock_from_args([]), RealClock)
        original_clock = transit.clock
        try:
            transit.clock = RealClock()
            before = transit.clock.now_utc()
            transit.advance_replay_time(utc("2024/05/18 12:00:00.000"))
            after = transit.clock.now_utc()
            self.assertGreaterEqual(after, before)
            self.assertLess((after - before).total_seconds(), 1)
        finally:
            transit.clock = original_clock


if __name__ == "__main__":
    unittest.main()
