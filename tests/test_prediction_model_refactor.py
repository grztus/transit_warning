import datetime
import math
import unittest
from collections import deque

import pytz

import transit_warning as transit


UTC_NOW = datetime.datetime(2026, 8, 21, 12, 0, tzinfo=pytz.utc)


def legacy_horizontal(observer, plane, track, velocity, elevation, body_azimuth):
    """Frozen copy of the pre-refactor horizontal implementation."""
    lat1, lon1 = observer
    lat2, lon2 = plane
    lat1, lat2, lon1, lon2 = map(
        math.radians, [lat1, lat2, lon1, lon2])
    body_azimuth = float(body_azimuth)
    track = float(track)
    theta_13, theta_23 = math.radians(body_azimuth), math.radians(track)
    delta_12 = 2 * math.asin(math.sqrt(
        math.sin((lat1 - lat2) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2)
        * math.sin((lon1 - lon2) / 2) ** 2))
    if delta_12 == 0:
        return None
    x = ((math.sin(lat2) - math.sin(lat1) * math.cos(delta_12))
         / (math.sin(delta_12) * math.cos(lat1)))
    x = min(1, max(-1, x))
    theta_a = math.acos(x)
    y = ((math.sin(lat1) - math.sin(lat2) * math.cos(delta_12))
         / (math.sin(delta_12) * math.cos(lat2)))
    y = min(1, max(-1, y))
    theta_b = math.acos(y)
    theta_12 = (
        theta_a if math.sin(lon2 - lon1) > 0
        else 2 * math.pi - theta_a)
    theta_21 = (
        2 * math.pi - theta_b if math.sin(lon2 - lon1) > 0
        else theta_b)
    alfa_1, alfa_2 = theta_13 - theta_12, theta_21 - theta_23
    if math.sin(alfa_1) == 0 and math.sin(alfa_2) == 0:
        return None
    if math.sin(alfa_1) * math.sin(alfa_2) < 0:
        return None
    alfa_3 = math.acos(
        -math.cos(alfa_1) * math.cos(alfa_2)
        + math.sin(alfa_1) * math.sin(alfa_2) * math.cos(delta_12))
    delta_13 = math.atan2(
        math.sin(delta_12) * math.sin(alfa_1) * math.sin(alfa_2),
        math.cos(alfa_2) + math.cos(alfa_1) * math.cos(alfa_3))
    lat3 = math.asin(
        math.sin(lat1) * math.cos(delta_13)
        + math.cos(lat1) * math.sin(delta_13) * math.cos(theta_13))
    dlon_13 = math.atan2(
        math.sin(theta_13) * math.sin(delta_13) * math.cos(lat1),
        math.cos(delta_13) - math.sin(lat1) * math.sin(lat3))
    lon3 = lon1 + dlon_13
    lat3 = math.degrees(lat3)
    lon3 = (math.degrees(lon3) + 540) % 360 - 180
    h2x = round(transit.haversine(
        (transit.my_lat, transit.my_lon), (lat3, lon3)), 1)
    if h2x > 500:
        return None
    if h2x == 0:
        h2x = 0.001
    if not transit.is_int_try(elevation):
        return None
    altitude = math.degrees(math.atan(
        (elevation - transit.my_elevation_const) / (h2x * 1000)))
    azimuth = math.atan2(
        math.sin(math.radians(lon3 - transit.my_lon))
        * math.cos(math.radians(lat3)),
        math.cos(math.radians(transit.my_lat)) * math.sin(math.radians(lat3))
        - math.sin(math.radians(transit.my_lat))
        * math.cos(math.radians(lat3))
        * math.cos(math.radians(lon3 - transit.my_lon)))
    azimuth = round((math.degrees(azimuth) + 360) % 360, 1)
    p2x = round(transit.haversine(plane, (lat3, lon3)), 1)
    velocity = int(velocity)
    if velocity <= 0:
        return None
    time2x = p2x / velocity * 3600
    return lat3, lon3, azimuth, altitude, h2x, p2x, time2x


class HorizontalExtractionEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.original = (
            transit.my_lat, transit.my_lon, transit.my_elevation_const)
        transit.my_elevation_const = 100.0

    def tearDown(self):
        (transit.my_lat, transit.my_lon,
         transit.my_elevation_const) = self.original

    def assert_case(self, observer, plane, track, body_azimuth,
                    velocity=800.9, elevation=10000):
        transit.my_lat, transit.my_lon = observer
        expected = legacy_horizontal(
            observer, plane, track, velocity, elevation, body_azimuth)
        actual = transit.solve_great_circle_intersection(
            observer, plane, track, velocity, elevation, body_azimuth,
            transit.my_elevation_const)
        if expected is None:
            self.assertIsNone(actual)
            return
        self.assertEqual(expected, (
            actual.latitude_deg,
            actual.longitude_deg,
            actual.azimuth_from_observer_deg,
            actual.aircraft_altitude_angle_deg,
            actual.observer_distance_km,
            actual.aircraft_distance_km,
            actual.time_seconds,
        ))

    def test_normal_short_and_long_intersections_are_exact(self):
        observer = (51.1, 21.1)
        plane = (50.3, 22.2)
        for track in (155, 190, 250):
            with self.subTest(track=track):
                self.assert_case(observer, plane, track, 150.5)

    def test_dateline_and_near_north_bearing_are_exact(self):
        self.assert_case((10, 179.5), (10, -179), 130, 90)
        self.assert_case((51.1, 21.1), (51.2, 21.2), 250, 359.9)

    def test_no_intersection_behind_and_over_500_are_preserved(self):
        observer = (51.1, 21.1)
        plane = (50.3, 22.2)
        for track in (0, 150):
            with self.subTest(track=track):
                self.assert_case(observer, plane, track, 150.5)

    def test_rounded_zero_h2x_uses_existing_point_zero_zero_one(self):
        transit.my_lat = transit.my_lon = 0.0
        result = transit.solve_great_circle_intersection(
            (0, 0), (1, 1), 225, 800, 10000, 60,
            transit.my_elevation_const)
        self.assertEqual(result.observer_distance_km, 0.001)
        self.assert_case((0, 0), (1, 1), 225, 60)

    def test_coincident_points_and_nonpositive_velocity_are_preserved(self):
        self.assert_case((1, 1), (1, 1), 180, 90)
        self.assert_case((51.1, 21.1), (50.3, 22.2), 190, 150.5,
                         velocity=0)

    def test_transit_pred_tuple_uses_extracted_result_unchanged(self):
        observer = (51.1, 21.1)
        plane = (50.3, 22.2)
        transit.my_lat, transit.my_lon = observer
        expected = legacy_horizontal(observer, plane, 190, 800.9, 10000, 150.5)
        result = transit.transit_pred(
            observer, plane, 190, 800.9, 10000, 25.9, 150.5)
        self.assertEqual(result[:7], expected)

    def test_forward_bearing_at_t0_continues_same_oriented_great_circle(self):
        observer = (51.1, 21.1)
        plane = (50.3, 22.2)
        transit.my_lat, transit.my_lon = observer
        intersection = transit.solve_great_circle_intersection(
            observer, plane, 190, 800, 10000, 150.5,
            transit.my_elevation_const)
        bearing = transit.great_circle_forward_bearing_at_point(
            plane, 190, (intersection.latitude_deg,
                         intersection.longitude_deg))

        def vector(position):
            lat, lon = map(math.radians, position)
            return (math.cos(lat) * math.cos(lon),
                    math.cos(lat) * math.sin(lon), math.sin(lat))

        def cross(left, right):
            return (left[1] * right[2] - left[2] * right[1],
                    left[2] * right[0] - left[0] * right[2],
                    left[0] * right[1] - left[1] * right[0])

        origin = vector(plane)
        lat, lon = map(math.radians, plane)
        north = (-math.sin(lat) * math.cos(lon),
                 -math.sin(lat) * math.sin(lon), math.cos(lat))
        east = (-math.sin(lon), math.cos(lon), 0.0)
        track = math.radians(190)
        initial_tangent = tuple(
            math.cos(track) * n + math.sin(track) * e
            for n, e in zip(north, east))
        normal = cross(origin, initial_tangent)

        point_lat = math.radians(intersection.latitude_deg)
        point_lon = math.radians(intersection.longitude_deg)
        point = vector((intersection.latitude_deg,
                        intersection.longitude_deg))
        point_north = (-math.sin(point_lat) * math.cos(point_lon),
                       -math.sin(point_lat) * math.sin(point_lon),
                       math.cos(point_lat))
        point_east = (-math.sin(point_lon), math.cos(point_lon), 0.0)
        bearing_rad = math.radians(bearing)
        tangent = tuple(
            math.cos(bearing_rad) * n + math.sin(bearing_rad) * e
            for n, e in zip(point_north, point_east))
        epsilon = 1e-6
        next_point = tuple(
            math.cos(epsilon) * p + math.sin(epsilon) * tangent_component
            for p, tangent_component in zip(point, tangent))
        self.assertAlmostEqual(
            sum(a * b for a, b in zip(normal, next_point)), 0.0,
            delta=1e-12)
        self.assertGreater(
            sum(a * b for a, b in zip(tangent, next_point)), 0.0)


def motion_state(values, age=0.0, altitude_age=0.0):
    samples = []
    for index, value in enumerate(values):
        samples.append(transit.MotionParameter(
            float(value),
            UTC_NOW - datetime.timedelta(
                seconds=age + len(values) - index - 1),
            "adsb"))
    return transit.AircraftMotionState(
        altitude=transit.MotionParameter(
            10000.0,
            UTC_NOW - datetime.timedelta(seconds=altitude_age), "adsb"),
        vertical_rate=samples[-1] if samples else None,
        vertical_rate_history=deque(
            samples, maxlen=transit.VERTICAL_RATE_HISTORY_MAXLEN))


def intent_state(selected=36000, qnh=1010, selected_age=0,
                 qnh_age=0, source="MCP/FCU"):
    return transit.AircraftIntentState(
        selected_altitude=transit.IntentParameter(
            selected, UTC_NOW - datetime.timedelta(seconds=selected_age),
            source) if selected is not None else None,
        nav_qnh=transit.IntentParameter(
            qnh, UTC_NOW - datetime.timedelta(seconds=qnh_age),
            "ADS-B TC29") if qnh is not None else None)


class FrozenVerticalModelEquivalenceTests(unittest.TestCase):
    def test_frozen_helper_matches_existing_two_step_policy(self):
        cases = (
            ("LEVEL", motion_state([64, 64, 64]), None, 60),
            ("IGNORE", motion_state([]), None, 60),
            ("DEGRADED", motion_state([512, 576, 960]), None, 60),
            ("CLIMB_LT_120", motion_state([512, 576, 640]), None, 60),
            ("CLIMB_EQ_120", motion_state([512, 576, 640]), None, 120),
            ("CLIMB_GT_120", motion_state([512, 576, 640]), None, 500),
            ("DESCENT", motion_state([-512, -576, -640]), None, 60),
            ("CLAMP", motion_state([512, 576, 640]),
             intent_state(selected=33000), 120),
            ("TARGET_REACHED", motion_state([512, 576, 640]),
             intent_state(selected=50000), 60),
            ("DIRECTION", motion_state([512, 576, 640]),
             intent_state(selected=30000), 60),
            ("STALE_SELECTED", motion_state([512, 576, 640]),
             intent_state(selected_age=10.001), 60),
            ("MISSING_QNH", motion_state([512, 576, 640]),
             intent_state(qnh=None), 60),
            ("STALE_QNH", motion_state([512, 576, 640]),
             intent_state(qnh_age=10.001), 60),
        )
        for name, motion, intent, horizon in cases:
            with self.subTest(name=name):
                before = transit.predict_transit_altitude(
                    10000.0, motion, UTC_NOW, horizon)
                expected, expected_details = (
                    transit.clamp_vertical_prediction_to_intent_state(
                        before, intent, UTC_NOW, 1009.0))
                actual = transit.predict_vertical_state_at_time(
                    10000.0, motion, intent, UTC_NOW, horizon, 1009.0)
                self.assertEqual(actual.prediction_before_clamp, before)
                self.assertEqual(actual.prediction, expected)
                self.assertEqual(actual.intent_details, expected_details)

    def test_final_altitude_angle_matches_existing_production_composition(self):
        original = (
            transit.aircraft_motion_states,
            transit.aircraft_intent_states,
            transit.vertical_transit_diagnostics,
            transit.my_elevation_const,
            transit.pressure,
        )
        motion = motion_state([512, 576, 640])
        intent = intent_state(selected=33000)
        transit.aircraft_motion_states = {"ABC123": motion}
        transit.aircraft_intent_states = {"ABC123": intent}
        transit.vertical_transit_diagnostics = {}
        transit.my_elevation_const = 200.0
        transit.pressure = 1009.0
        result = (
            51.2, 21.3, 123.4, 29.0, 75.4, 25.7,
            120.0, 0, 123.5, 30.0, UTC_NOW)
        try:
            frozen = transit.predict_vertical_state_at_time(
                10000.0, motion, intent, UTC_NOW, result[6], 1009.0)
            expected_angle = math.degrees(math.atan(
                (frozen.prediction.predicted_altitude_m - 200.0)
                / (result[4] * 1000)))
            updated = transit.apply_vertical_prediction_to_transit_result(
                "ABC123", "sun", result, 10000.0, UTC_NOW)
            diagnostic = transit.get_vertical_transit_diagnostic(
                "ABC123", "sun")
        finally:
            (transit.aircraft_motion_states,
             transit.aircraft_intent_states,
             transit.vertical_transit_diagnostics,
             transit.my_elevation_const,
             transit.pressure) = original
        self.assertAlmostEqual(updated[3], expected_angle, delta=1e-12)
        self.assertEqual(diagnostic.prediction, frozen.prediction)
        state = transit.production_aircraft_state_at_transit(
            updated, diagnostic.prediction.predicted_altitude_m)
        self.assertEqual(state.latitude_deg, updated[0])
        self.assertEqual(state.longitude_deg, updated[1])
        self.assertEqual(state.azimuth_from_observer_deg, updated[2])
        self.assertEqual(state.altitude_angle_deg, updated[3])
        self.assertEqual(
            state.altitude_m, diagnostic.prediction.predicted_altitude_m)

    def test_production_state_uses_intersection_without_propagation(self):
        result = (
            51.234567890123, 21.765432109876, 123.4, 29.25,
            17.9, 2.1, 9.45, 0, 123.4, 30.0, UTC_NOW)
        state = transit.production_aircraft_state_at_transit(result, 10456.7)
        self.assertEqual(state.latitude_deg, result[0])
        self.assertEqual(state.longitude_deg, result[1])
        self.assertEqual(state.azimuth_from_observer_deg, result[2])
        self.assertEqual(state.altitude_angle_deg, result[3])
        self.assertEqual(state.altitude_m, 10456.7)


if __name__ == "__main__":
    unittest.main()
