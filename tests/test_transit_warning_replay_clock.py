import copy
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
        self.original_moving_body_transit_pred = (
            transit.moving_body_transit_pred)
        self.original_environment_replay = transit.environment_replay
        self.original_altitude_sources = transit.altitude_sources
        self.original_motion_states = transit.aircraft_motion_states
        self.original_freshness_status = (
            transit.aircraft_motion_freshness_status)
        self.original_pressure = transit.pressure
        self.original_sun_alt = getattr(transit, "sun_alt", None)
        self.original_moon_alt = getattr(transit, "moon_alt", None)
        transit.clock = ReplayClock()
        transit.replay_time_initialized = False
        transit.metar_t = None
        transit.metar_attempt_t = None
        transit.aktual_t = None
        transit.last_t = None
        transit.gong_t = None
        transit.last_update_time = None
        transit.plane_dict = {}
        transit.altitude_sources = {}
        transit.aircraft_motion_states = {}
        transit.aircraft_motion_freshness_status = {}
        transit.sun_prediction_last_valid.clear()
        transit.moon_prediction_last_valid.clear()
        transit.sun_predicted_transit_utc.clear()
        transit.moon_predicted_transit_utc.clear()
        transit.environment_replay = None
        transit.pressure = 1013
        transit.sun_alt = 30.0
        transit.moon_alt = 20.0
        transit.tabela = lambda: (30.0, 120.0, 20.0, 90.0)
        transit.moving_body_transit_pred = (
            lambda body, observer, plane, track, velocity, elevation,
            prediction_base_utc, fallback_body_position=None:
            transit.transit_pred(
                observer, plane, track, velocity, elevation,
                fallback_body_position[0], fallback_body_position[1]))

    def tearDown(self):
        transit.clock = self.original_clock
        transit.tabela = self.original_tabela
        transit.transit_pred = self.original_transit_pred
        transit.moving_body_transit_pred = (
            self.original_moving_body_transit_pred)
        transit.environment_replay = self.original_environment_replay
        transit.altitude_sources = self.original_altitude_sources
        transit.aircraft_motion_states = self.original_motion_states
        transit.aircraft_motion_freshness_status = (
            self.original_freshness_status)
        transit.pressure = self.original_pressure
        transit.sun_alt = self.original_sun_alt
        transit.moon_alt = self.original_moon_alt
        transit.sun_prediction_last_valid.clear()
        transit.moon_prediction_last_valid.clear()
        transit.sun_predicted_transit_utc.clear()
        transit.moon_predicted_transit_utc.clear()

    def test_valid_prediction_records_absolute_transit_times(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])

        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)

        now = utc("2024/05/18 12:00:00.000")
        self.assertEqual(
            transit.moon_predicted_transit_utc["ABC123"],
            now + datetime.timedelta(seconds=120))
        self.assertEqual(
            transit.sun_predicted_transit_utc["ABC123"],
            now + datetime.timedelta(seconds=130))

    def test_remaining_time_counts_down_without_new_messages(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)

        transit.clock.advance_to(utc("2024/05/18 12:00:01.000"))
        self.assertEqual(transit.predicted_transit_remaining_seconds(
            "ABC123", "moon"), 119)
        transit.clock.advance_to(utc("2024/05/18 12:00:20.000"))
        self.assertEqual(transit.predicted_transit_remaining_seconds(
            "ABC123", "moon"), 100)
        transit.plane_dict["ABC123"][24] = 38.0
        self.assertEqual(
            transit.visible_transit_candidate(
                transit.plane_dict["ABC123"], "moon", "ABC123")[3],
            100)

    def test_lot3pw_like_gap_continues_countdown(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 119), self.prediction(38.0, 130)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)

        transit.clock.advance_to(utc("2024/05/18 12:00:28.000"))

        self.assertEqual(transit.predicted_transit_remaining_seconds(
            "ABC123", "moon"), 91)
        self.assertEqual(transit.plane_dict["ABC123"][26], 119)

    def test_new_predictions_replace_absolute_time_upward_and_downward(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)
        transit.clock.advance_to(utc("2024/05/18 12:00:30.000"))
        self.assertEqual(transit.predicted_transit_remaining_seconds(
            "ABC123", "moon"), 90)

        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 105), self.prediction(38.0, 80)])
        transit.process_line(self.msg3("2024/05/18 12:00:30.000"), 30106)

        self.assertEqual(transit.predicted_transit_remaining_seconds(
            "ABC123", "moon"), 105)
        self.assertEqual(transit.predicted_transit_remaining_seconds(
            "ABC123", "sun"), 80)

    def test_grace_keeps_absolute_time_until_expiry(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)
        original = transit.moon_predicted_transit_utc["ABC123"]
        transit.transit_pred = Mock(return_value=0)

        transit.process_line(self.msg3("2024/05/18 12:00:01.000"), 30106)
        self.assertEqual(
            transit.moon_predicted_transit_utc["ABC123"], original)
        self.assertEqual(transit.predicted_transit_remaining_seconds(
            "ABC123", "moon"), 119)

        transit.process_line(self.msg3("2024/05/18 12:00:03.000"), 30106)
        self.assertNotIn("ABC123", transit.moon_predicted_transit_utc)
        self.assertNotIn("ABC123", transit.sun_predicted_transit_utc)

    def test_remaining_zero_and_past_are_not_future_candidates(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 2), self.prediction(38.0, 3)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)
        entry = transit.plane_dict["ABC123"]

        transit.clock.advance_to(utc("2024/05/18 12:00:02.000"))
        self.assertEqual(transit.predicted_transit_remaining_seconds(
            "ABC123", "moon"), 0)
        self.assertIsNone(transit.visible_transit_candidate(
            entry, "moon", "ABC123"))
        transit.clock.advance_to(utc("2024/05/18 12:00:04.000"))
        self.assertEqual(transit.predicted_transit_remaining_seconds(
            "ABC123", "moon"), 0)
        self.assertIsNone(transit.visible_transit_candidate(
            entry, "moon", "ABC123"))

    def test_aircraft_cleanup_removes_both_absolute_transit_times(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)
        transit.clock.advance_to(utc("2024/05/18 12:01:01.000"))

        transit.clean_dict()

        self.assertNotIn("ABC123", transit.plane_dict)
        self.assertNotIn("ABC123", transit.moon_predicted_transit_utc)
        self.assertNotIn("ABC123", transit.sun_predicted_transit_utc)

    def test_explicit_now_avoids_real_clock_path(self):
        now = utc("2024/05/18 12:00:00.000")
        transit.moon_predicted_transit_utc["ABC123"] = (
            now + datetime.timedelta(seconds=10))
        fake_clock = Mock()

        with patch.object(transit, "clock", fake_clock):
            remaining = transit.predicted_transit_remaining_seconds(
                "ABC123", "moon", now)

        self.assertEqual(remaining, 10)
        fake_clock.now_utc.assert_not_called()

    def process(self, generated, logged, port=30106, icao="ABC123"):
        transit.process_line(message(generated, logged, icao), port)

    def qnh(self, timestamp, value):
        return EnvironmentEvent(1, utc(timestamp), "qnh", value, "test")

    def set_environment(self, *events):
        transit.environment_replay = EnvironmentReplay(events)

    def msg3(self, timestamp, icao="ABC123", altitude="10000",
             latitude="51.2", longitude="21.2"):
        return (
            "MLAT,3,1,1,{icao},1,{date},{time},{date},{time},,{altitude},"
            "450,180,{latitude},{longitude},0".format(
                icao=icao,
                date=timestamp.split()[0],
                time=timestamp.split()[1],
                altitude=altitude,
                latitude=latitude,
                longitude=longitude,
            )
        )

    def msg4(self, timestamp, icao="ABC123"):
        return (
            "MSG,4,1,1,{icao},1,{date},{time},{date},{time},,,450,180,,"
            ",0".format(
                icao=icao,
                date=timestamp.split()[0],
                time=timestamp.split()[1],
            )
        )

    def msg5(self, timestamp, icao="ABC123", altitude="11000"):
        return (
            "MSG,5,1,1,{icao},1,{date},{time},{date},{time},,{altitude},"
            ",,,,".format(
                icao=icao,
                date=timestamp.split()[0],
                time=timestamp.split()[1],
                altitude=altitude,
            )
        )

    def mlat3(self, timestamp, icao="ABC123"):
        return (
            "MLAT,3,1,1,{icao},1,{date},{time},{date},{time},,10000,"
            "450,180,51.2,21.2,0".format(
                icao=icao,
                date=timestamp.split()[0],
                time=timestamp.split()[1],
            )
        )

    def test_adsb_msg1_updates_callsign_without_touching_prediction(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(
            self.msg3("2024/05/18 14:00:00.000"), 30003)
        moon_time = transit.moon_predicted_transit_utc["ABC123"]
        sun_time = transit.sun_predicted_transit_utc["ABC123"]
        moon_last_valid = transit.moon_prediction_last_valid["ABC123"]
        sun_last_valid = transit.sun_prediction_last_valid["ABC123"]
        motion_state = copy.deepcopy(
            transit.aircraft_motion_states["ABC123"])
        freshness = transit.aircraft_motion_freshness_status["ABC123"]
        prediction = Mock()
        transit.transit_pred = prediction

        transit.process_line(message(
            "2024/05/18 14:00:35.000",
            "2024/05/18 14:00:35.000"), 30003)

        self.assertEqual(transit.plane_dict["ABC123"][1], "TEST123")
        prediction.assert_not_called()
        self.assertEqual(
            transit.moon_predicted_transit_utc["ABC123"], moon_time)
        self.assertEqual(
            transit.sun_predicted_transit_utc["ABC123"], sun_time)
        self.assertEqual(
            transit.moon_prediction_last_valid["ABC123"], moon_last_valid)
        self.assertEqual(
            transit.sun_prediction_last_valid["ABC123"], sun_last_valid)
        self.assertEqual(
            transit.aircraft_motion_states["ABC123"], motion_state)
        self.assertIs(
            transit.aircraft_motion_freshness_status["ABC123"], freshness)
        self.assertEqual(transit.predicted_transit_remaining_seconds(
            "ABC123", "moon"), 85)

    def test_msg5_then_msg1_does_not_trigger_prediction(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(
            self.msg3("2024/05/18 14:00:00.000"), 30003)
        previous_altitude = transit.plane_dict["ABC123"][4]
        prediction = Mock()
        transit.transit_pred = prediction

        transit.process_line(
            self.msg5("2024/05/18 14:00:10.000"), 30003)
        transit.process_line(message(
            "2024/05/18 14:00:11.000",
            "2024/05/18 14:00:11.000"), 30003)

        self.assertNotEqual(
            transit.plane_dict["ABC123"][4], previous_altitude)
        prediction.assert_not_called()

    def test_msg1_without_previous_prediction_creates_no_prediction_state(self):
        prediction = Mock()
        transit.transit_pred = prediction

        transit.process_line(message(
            "2024/05/18 14:00:00.000",
            "2024/05/18 14:00:00.000"), 30003)

        self.assertEqual(transit.plane_dict["ABC123"][1], "TEST123")
        prediction.assert_not_called()
        self.assertNotIn("ABC123", transit.moon_predicted_transit_utc)
        self.assertNotIn("ABC123", transit.sun_predicted_transit_utc)
        self.assertNotIn("ABC123", transit.moon_prediction_last_valid)
        self.assertNotIn("ABC123", transit.sun_prediction_last_valid)

    def test_msg3_msg4_and_mlat3_remain_prediction_triggers(self):
        transit.transit_pred = Mock(return_value=0)
        transit.process_line(
            self.msg3("2024/05/18 14:00:00.000"), 30003)
        self.assertEqual(transit.transit_pred.call_count, 2)

        transit.transit_pred.reset_mock()
        transit.process_line(
            self.msg4("2024/05/18 14:00:01.000"), 30003)
        self.assertEqual(transit.transit_pred.call_count, 2)

        transit.plane_dict = {}
        transit.transit_pred.reset_mock()
        transit.process_line(
            self.mlat3("2024/05/18 12:00:02.000", "MLAT01"), 30106)
        self.assertEqual(transit.transit_pred.call_count, 2)

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

    def test_missing_prediction_after_one_second_keeps_blocks(self):
        timestamp = "2024/05/18 12:00:00.000"
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3(timestamp), 30106)
        self.assertEqual(transit.plane_dict["ABC123"][22], 130)
        self.assertEqual(transit.plane_dict["ABC123"][26], 120)

        transit.transit_pred = Mock(side_effect=[0, 0])
        transit.process_line(
            self.msg3("2024/05/18 12:00:01.000"), 30106)

        self.assertEqual(transit.plane_dict["ABC123"][22], 130)
        self.assertEqual(transit.plane_dict["ABC123"][26], 120)

    def test_prediction_returning_within_grace_refreshes_without_flicker(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)
        transit.transit_pred = Mock(side_effect=[0, 0])
        transit.process_line(self.msg3("2024/05/18 12:00:01.000"), 30106)

        transit.transit_pred = Mock(side_effect=[
            self.prediction(39.0, 110), self.prediction(39.0, 115)])
        transit.process_line(self.msg3("2024/05/18 12:00:02.000"), 30106)

        self.assertEqual(transit.plane_dict["ABC123"][22], 115)
        self.assertEqual(transit.plane_dict["ABC123"][26], 110)
        self.assertEqual(
            transit.sun_prediction_last_valid["ABC123"],
            utc("2024/05/18 12:00:02.000"))
        self.assertEqual(
            transit.moon_prediction_last_valid["ABC123"],
            utc("2024/05/18 12:00:02.000"))

    def test_missing_prediction_at_2_999_seconds_keeps_blocks(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)
        transit.transit_pred = Mock(side_effect=[0, 0])

        transit.process_line(self.msg3("2024/05/18 12:00:02.999"), 30106)

        self.assertEqual(transit.plane_dict["ABC123"][22], 130)
        self.assertEqual(transit.plane_dict["ABC123"][26], 120)

    def test_missing_prediction_at_exactly_three_seconds_clears_blocks(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)
        transit.transit_pred = Mock(side_effect=[0, 0])

        transit.process_line(self.msg3("2024/05/18 12:00:03.000"), 30106)

        self.assertEqual(transit.plane_dict["ABC123"][18:28], [""] * 10)

    def test_many_missing_frames_do_not_shorten_time_based_grace(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)
        transit.transit_pred = Mock(return_value=0)

        for timestamp in ("12:00:00.500", "12:00:01.000", "12:00:02.999"):
            transit.process_line(self.msg3(
                "2024/05/18 {}".format(timestamp)), 30106)

        self.assertEqual(transit.plane_dict["ABC123"][22], 130)
        self.assertEqual(transit.plane_dict["ABC123"][26], 120)

    def test_sun_and_moon_grace_are_independent(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)
        transit.transit_pred = Mock(side_effect=[
            0, self.prediction(39.0, 115)])

        transit.process_line(self.msg3("2024/05/18 12:00:03.000"), 30106)

        self.assertEqual(transit.plane_dict["ABC123"][23:28], [""] * 5)
        self.assertEqual(transit.plane_dict["ABC123"][22], 115)
        self.assertNotIn("ABC123", transit.moon_prediction_last_valid)
        self.assertIn("ABC123", transit.sun_prediction_last_valid)

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
        self.assertNotIn("ABC123", transit.moon_predicted_transit_utc)
        self.assertIn("ABC123", transit.sun_predicted_transit_utc)

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
        self.assertNotIn("ABC123", transit.moon_predicted_transit_utc)
        self.assertNotIn("ABC123", transit.sun_predicted_transit_utc)

    def test_missing_altitude_without_fallback_does_not_start_expiry(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)
        transit.plane_dict["ABC123"][4] = ""
        transit.transit_pred = Mock(return_value=0)

        transit.process_line(self.msg3(
            "2024/05/18 12:00:10.000", altitude="",
            latitude="51.3", longitude="21.4"), 30106)

        transit.transit_pred.assert_not_called()
        self.assertEqual(transit.plane_dict["ABC123"][2:4], [51.3, 21.4])
        self.assertEqual(transit.plane_dict["ABC123"][22], 130)
        self.assertEqual(transit.plane_dict["ABC123"][26], 120)

    def test_renderer_keeps_candidate_during_grace_and_drops_after_expiry(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(38.0, 120), self.prediction(38.0, 130)])
        transit.process_line(self.msg3("2024/05/18 12:00:00.000"), 30106)
        ordinary = [""] * 32
        ordinary[5] = 10
        transit.transit_pred = Mock(return_value=0)

        transit.process_line(self.msg3("2024/05/18 12:00:01.000"), 30106)
        during_grace = transit.build_terminal_render_plan(
            {"ORDINARY": ordinary, "ABC123": transit.plane_dict["ABC123"]},
            2, 200)
        transit.process_line(self.msg3("2024/05/18 12:00:03.000"), 30106)
        after_expiry = transit.build_terminal_render_plan(
            {"ORDINARY": ordinary, "ABC123": transit.plane_dict["ABC123"]},
            2, 200)

        self.assertEqual(during_grace.aircraft_ids[0], "ABC123")
        self.assertEqual(after_expiry.aircraft_ids[0], "ORDINARY")

    def test_msg3_without_altitude_updates_position_without_type_error(self):
        transit.transit_pred = Mock(return_value=0)
        transit.process_line(
            self.msg3("2024/05/18 12:00:00.000", altitude=""), 30003)
        transit.process_line(
            self.msg3("2024/05/18 12:00:01.000", icao="VALID"), 30003)

        self.assertEqual(transit.plane_dict["ABC123"][2:4], [51.2, 21.2])
        self.assertEqual(transit.plane_dict["ABC123"][4], "")
        self.assertIn("VALID", transit.plane_dict)

    def test_msg3_without_altitude_preserves_height_and_updates_position(self):
        transit.transit_pred = Mock(return_value=0)
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
