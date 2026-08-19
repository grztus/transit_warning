import datetime
import unittest
from unittest.mock import Mock

import pytz

import transit_warning as transit
from config import InstallationConfig
from transit_clock import ReplayClock


NOW = datetime.datetime(2026, 8, 19, 12, 0, tzinfo=pytz.utc)
TEST_CONFIG = InstallationConfig(
    observer_lat=51.0,
    observer_lon=21.0,
    observer_elevation_m=200.0,
    transition_altitude_ft=6500,
    adsb_host="adsb.example",
    adsb_port=30003,
    adsb_timestamp_timezone="UTC",
    mlat_host="mlat.example",
    mlat_port=30106,
    metar_station="EPRA",
)


def parameter(age, source="adsb", value=1.0):
    if age is None:
        return None
    return transit.MotionParameter(
        value, NOW - datetime.timedelta(seconds=age), source)


def position(age, source="adsb"):
    if age is None:
        return None
    return transit.PositionParameter(
        51.2, 21.2, NOW - datetime.timedelta(seconds=age), source)


def state(position_age=0, altitude_age=0, track_age=0,
          groundspeed_age=0, vertical_rate_age=None,
          position_source="adsb", track_source="adsb",
          groundspeed_source="adsb"):
    return transit.AircraftMotionState(
        position=position(position_age, position_source),
        altitude=parameter(altitude_age),
        track=parameter(track_age, track_source, 180),
        groundspeed=parameter(groundspeed_age, groundspeed_source, 800),
        vertical_rate=parameter(vertical_rate_age, value=0),
    )


def sbs(prefix, subtype, timestamp, icao="ABC123", altitude="",
        groundspeed="", track="", latitude="", longitude="",
        vertical_rate=""):
    date, timestamp_time = timestamp.split()
    return ",".join([
        prefix, str(subtype), "1", "1", icao, "1",
        date, timestamp_time, date, timestamp_time, "",
        str(altitude), str(groundspeed), str(track),
        str(latitude), str(longitude), str(vertical_rate),
    ])


class MotionFreshnessClassificationTests(unittest.TestCase):
    def assess(self, value):
        return transit.assess_motion_freshness(value, NOW)

    def assert_status(self, expected, **ages):
        self.assertEqual(self.assess(state(**ages)).status, expected)

    def test_fresh_complete_state_and_missing_vertical_rate(self):
        result = self.assess(state())
        self.assertEqual(result.status, transit.MotionFreshnessStatus.FRESH)
        self.assertEqual(result.assessed_at_utc, NOW)
        self.assertIsNone(result.vertical_rate_age)

        result_with_vr = self.assess(state(vertical_rate_age=4))
        self.assertEqual(result_with_vr.status,
                         transit.MotionFreshnessStatus.FRESH)
        self.assertEqual(result_with_vr.vertical_rate_age, 4.0)

    def test_position_age_boundaries(self):
        cases = [
            (3.000, transit.MotionFreshnessStatus.FRESH),
            (3.001, transit.MotionFreshnessStatus.DEGRADED),
            (10.000, transit.MotionFreshnessStatus.DEGRADED),
            (10.001, transit.MotionFreshnessStatus.STALE),
        ]
        for age, expected in cases:
            with self.subTest(age=age):
                self.assert_status(
                    expected, position_age=age, altitude_age=age,
                    track_age=age, groundspeed_age=age)

    def test_track_and_groundspeed_age_boundaries(self):
        for name in ("track_age", "groundspeed_age"):
            cases = [
                (5.000, 2.000, transit.MotionFreshnessStatus.FRESH),
                (5.001, 2.000, transit.MotionFreshnessStatus.DEGRADED),
                (10.000, 0.000, transit.MotionFreshnessStatus.DEGRADED),
                (10.001, 0.000, transit.MotionFreshnessStatus.STALE),
            ]
            for age, other_age, expected in cases:
                values = dict(position_age=other_age, altitude_age=other_age,
                              track_age=other_age,
                              groundspeed_age=other_age)
                values[name] = age
                with self.subTest(parameter=name, age=age):
                    self.assert_status(expected, **values)

    def test_altitude_age_boundaries(self):
        cases = [
            (5.000, transit.MotionFreshnessStatus.FRESH),
            (5.001, transit.MotionFreshnessStatus.DEGRADED),
            (10.000, transit.MotionFreshnessStatus.DEGRADED),
            (10.001, transit.MotionFreshnessStatus.STALE),
        ]
        for age, expected in cases:
            with self.subTest(age=age):
                self.assert_status(expected, altitude_age=age)

    def test_position_track_and_groundspeed_delta_boundaries(self):
        for name in ("track_age", "groundspeed_age"):
            cases = [
                (3.000, transit.MotionFreshnessStatus.FRESH),
                (3.001, transit.MotionFreshnessStatus.DEGRADED),
                (10.000, transit.MotionFreshnessStatus.DEGRADED),
                (10.001, transit.MotionFreshnessStatus.STALE),
            ]
            for delta, expected in cases:
                values = {name: delta}
                with self.subTest(parameter=name, delta=delta):
                    self.assert_status(expected, **values)

    def test_representative_degraded_and_stale_states(self):
        self.assert_status(
            transit.MotionFreshnessStatus.DEGRADED,
            position_age=4, altitude_age=4, track_age=4,
            groundspeed_age=4)
        self.assert_status(
            transit.MotionFreshnessStatus.DEGRADED,
            position_age=0, track_age=6, groundspeed_age=6)
        self.assert_status(
            transit.MotionFreshnessStatus.STALE,
            position_age=0, track_age=11, groundspeed_age=11)
        self.assert_status(
            transit.MotionFreshnessStatus.STALE,
            position_age=11, track_age=0, groundspeed_age=0)
        self.assert_status(
            transit.MotionFreshnessStatus.STALE,
            position_age=11, altitude_age=11, track_age=11,
            groundspeed_age=11)

    def test_missing_required_parameters_are_stale(self):
        for name in ("position_age", "altitude_age", "track_age",
                     "groundspeed_age"):
            with self.subTest(parameter=name):
                result = self.assess(state(**{name: None}))
                self.assertEqual(
                    result.status, transit.MotionFreshnessStatus.STALE)
                self.assertIn(
                    "MISSING_{}".format(name.removesuffix("_age").upper()),
                    result.reason_codes)

    def test_small_negative_age_is_clamped_without_changing_timestamp(self):
        future = NOW + datetime.timedelta(milliseconds=25)
        value = state()
        value.position = transit.PositionParameter(51.2, 21.2, future, "adsb")

        result = self.assess(value)

        self.assertEqual(result.status, transit.MotionFreshnessStatus.FRESH)
        self.assertEqual(result.position_age, 0.0)
        self.assertEqual(value.position.updated_at_utc, future)

    def test_mixed_fresh_sources_are_coherent_and_allowed(self):
        value = state(
            position_age=1, track_age=2, groundspeed_age=2,
            position_source="mlat", track_source="adsb",
            groundspeed_source="adsb")

        result = self.assess(value)

        self.assertEqual(result.status, transit.MotionFreshnessStatus.FRESH)
        self.assertTrue(result.horizontal_source_coherent)

        stale = self.assess(state(
            position_age=0, track_age=11, groundspeed_age=11,
            position_source="mlat", track_source="adsb",
            groundspeed_source="adsb"))
        self.assertEqual(stale.status, transit.MotionFreshnessStatus.STALE)
        self.assertFalse(stale.horizontal_source_coherent)


class MotionFreshnessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.originals = {
            name: getattr(transit, name) for name in (
                "clock", "plane_dict", "altitude_sources",
                "aircraft_motion_states", "aircraft_motion_freshness_status",
                "pressure", "tabela", "transit_pred", "gong")
        }
        transit.clock = ReplayClock()
        transit.apply_installation_config(TEST_CONFIG)
        transit.replay_time_initialized = False
        transit.plane_dict = {}
        transit.altitude_sources = {}
        transit.aircraft_motion_states = {}
        transit.aircraft_motion_freshness_status = {}
        transit.sun_prediction_last_valid.clear()
        transit.moon_prediction_last_valid.clear()
        transit.sun_predicted_transit_utc.clear()
        transit.moon_predicted_transit_utc.clear()
        transit.pressure = 1013.25
        transit.sun_alt = 30.0
        transit.moon_alt = 20.0
        transit.tabela = lambda: (30.0, 120.0, 20.0, 90.0)
        transit.gong = lambda: None

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(transit, name, value)
        transit.sun_prediction_last_valid.clear()
        transit.moon_prediction_last_valid.clear()
        transit.sun_predicted_transit_utc.clear()
        transit.moon_predicted_transit_utc.clear()

    @staticmethod
    def prediction(seconds):
        return (51.2, 21.2, 120.0, 25.0, 17.9, 33.7,
                seconds, 0, 120.0, 37.9, None)

    def process(self, value, port=30106):
        transit.process_line(value, port)

    def mlat3(self, timestamp, icao="ABC123"):
        return sbs(
            "MLAT", 3, timestamp, icao, altitude=10000,
            groundspeed=450, track=180, latitude=51.2,
            longitude=21.2, vertical_rate=0)

    def msg3(self, timestamp, icao="ABC123"):
        return sbs(
            "MSG", 3, timestamp, icao, altitude=10000,
            latitude=51.2, longitude=21.2)

    def msg4(self, timestamp, icao="ABC123"):
        return sbs(
            "MSG", 4, timestamp, icao, groundspeed=450,
            track=180, vertical_rate=0)

    def test_fresh_mlat_and_adsb_states_call_prediction(self):
        transit.transit_pred = Mock(return_value=0)
        self.process(self.mlat3("2026/08/19 12:00:00.000"))
        self.assertEqual(transit.transit_pred.call_count, 2)
        self.assertEqual(
            transit.get_aircraft_motion_freshness_status("ABC123").status,
            transit.MotionFreshnessStatus.FRESH)

        transit.transit_pred.reset_mock()
        self.process(self.msg4("2026/08/19 12:00:01.000"), 30003)
        self.process(self.msg3("2026/08/19 12:00:02.000"), 30003)
        self.assertEqual(transit.transit_pred.call_count, 4)
        self.assertEqual(
            transit.get_aircraft_motion_freshness_status("ABC123").status,
            transit.MotionFreshnessStatus.FRESH)

    def test_degraded_state_still_calls_prediction(self):
        transit.transit_pred = Mock(return_value=0)
        self.process(self.msg4("2026/08/19 12:00:00.000"))
        transit.transit_pred = Mock(side_effect=[
            self.prediction(90), self.prediction(100)])

        self.process(self.msg3("2026/08/19 12:00:06.000"))

        self.assertEqual(transit.transit_pred.call_count, 2)
        self.assertEqual(
            transit.get_aircraft_motion_freshness_status("ABC123").status,
            transit.MotionFreshnessStatus.DEGRADED)
        self.assertEqual(
            transit.predicted_transit_remaining_seconds("ABC123", "moon"),
            90)

    def test_stale_preserves_last_good_prediction_and_does_not_start_grace(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(120), self.prediction(130)])
        self.process(self.mlat3("2026/08/19 12:00:00.000"))
        moon_target = transit.moon_predicted_transit_utc["ABC123"]
        prediction_block = list(transit.plane_dict["ABC123"][18:28])
        moon_last_valid = transit.moon_prediction_last_valid["ABC123"]
        sun_last_valid = transit.sun_prediction_last_valid["ABC123"]
        transit.transit_pred = Mock()

        self.process(self.msg3("2026/08/19 12:00:15.000"))

        transit.transit_pred.assert_not_called()
        result = transit.get_aircraft_motion_freshness_status("ABC123")
        self.assertEqual(result.status, transit.MotionFreshnessStatus.STALE)
        self.assertEqual(
            transit.moon_predicted_transit_utc["ABC123"], moon_target)
        self.assertEqual(
            transit.moon_prediction_last_valid["ABC123"], moon_last_valid)
        self.assertEqual(
            transit.sun_prediction_last_valid["ABC123"], sun_last_valid)
        self.assertEqual(
            transit.predicted_transit_remaining_seconds("ABC123", "moon"),
            105)
        self.assertEqual(
            transit.plane_dict["ABC123"][18:28], prediction_block)

    def test_fresh_after_stale_resumes_prediction(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(120), self.prediction(130)])
        self.process(self.mlat3("2026/08/19 12:00:00.000"))
        transit.transit_pred = Mock()
        self.process(self.msg3("2026/08/19 12:00:15.000"))
        transit.transit_pred = Mock(side_effect=[
            self.prediction(90), self.prediction(100)])

        self.process(self.msg4("2026/08/19 12:00:15.100"))

        self.assertEqual(transit.transit_pred.call_count, 2)
        self.assertEqual(
            transit.get_aircraft_motion_freshness_status("ABC123").status,
            transit.MotionFreshnessStatus.FRESH)
        self.assertEqual(
            transit.predicted_transit_remaining_seconds("ABC123", "moon"),
            90)

    def test_fresh_zero_result_still_uses_existing_grace(self):
        transit.transit_pred = Mock(side_effect=[
            self.prediction(120), self.prediction(130)])
        self.process(self.mlat3("2026/08/19 12:00:00.000"))
        original_target = transit.moon_predicted_transit_utc["ABC123"]
        transit.transit_pred = Mock(return_value=0)

        self.process(self.mlat3("2026/08/19 12:00:01.000"))

        self.assertEqual(transit.transit_pred.call_count, 2)
        self.assertEqual(
            transit.moon_predicted_transit_utc["ABC123"], original_target)

    def test_stale_without_previous_prediction_creates_nothing(self):
        transit.transit_pred = Mock()
        self.process(self.msg3("2026/08/19 12:00:00.000"))

        transit.transit_pred.assert_not_called()
        self.assertEqual(
            transit.get_aircraft_motion_freshness_status("ABC123").status,
            transit.MotionFreshnessStatus.STALE)
        self.assertNotIn("ABC123", transit.moon_predicted_transit_utc)
        self.assertNotIn("ABC123", transit.sun_predicted_transit_utc)

    def test_cleanup_removes_freshness_status(self):
        transit.transit_pred = Mock(return_value=0)
        self.process(self.mlat3("2026/08/19 12:00:00.000"))
        transit.clock.advance_to(
            datetime.datetime(2026, 8, 19, 12, 1, 1, tzinfo=pytz.utc))

        transit.clean_dict()

        self.assertNotIn("ABC123", transit.aircraft_motion_freshness_status)


if __name__ == "__main__":
    unittest.main()
