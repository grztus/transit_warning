import math
import os
import time
import unittest

from transit_prediction_model import (
    EARTH_RADIUS_KM,
    great_circle_forward_bearing_at_point,
    horizontal_position_from_t0,
    propagate_great_circle_position,
    solve_great_circle_intersection,
)


def distance_km(left, right):
    lat1, lon1 = map(math.radians, left)
    lat2, lon2 = map(math.radians, right)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    value = (math.sin(delta_lat / 2) ** 2
             + math.cos(lat1) * math.cos(lat2)
             * math.sin(delta_lon / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.atan2(
        math.sqrt(value), math.sqrt(1 - value))


class GreatCirclePropagationTests(unittest.TestCase):
    def test_zero_distance_returns_exact_input(self):
        self.assertEqual(
            propagate_great_circle_position(51.25, 21.5, 123.0, 0),
            (51.25, 21.5))
        self.assertEqual(
            horizontal_position_from_t0(51.25, 21.5, 123.0, 800, 0),
            (51.25, 21.5))

    def test_normal_propagation_follows_expected_great_circle(self):
        origin = (51.0, 21.0)
        north = propagate_great_circle_position(*origin, 0.0, 111.1949266)
        east = propagate_great_circle_position(0.0, 0.0, 90.0,
                                                111.1949266)
        self.assertAlmostEqual(north[0], 52.0, places=7)
        self.assertAlmostEqual(north[1], 21.0, places=7)
        self.assertAlmostEqual(east[0], 0.0, places=7)
        self.assertAlmostEqual(east[1], 1.0, places=7)

    def test_origin_propagation_reaches_canonical_intersection_with_rounding_error(self):
        observer = (51.1, 21.1)
        origin = (50.3, 22.2)
        track = 190.0
        speed = 800.9
        intersection = solve_great_circle_intersection(
            observer, origin, track, speed, 10000, 150.5, 100.0)
        propagated = propagate_great_circle_position(
            *origin, track,
            int(speed) * intersection.time_seconds / 3600.0)
        canonical = (intersection.latitude_deg, intersection.longitude_deg)
        self.assertLessEqual(distance_km(propagated, canonical), 0.051)

    def test_t0_offsets_follow_one_oriented_great_circle_and_reverse(self):
        origin = (50.3, 22.2)
        intersection = solve_great_circle_intersection(
            (51.1, 21.1), origin, 190.0, 800, 10000, 150.5, 100.0)
        t0 = (intersection.latitude_deg, intersection.longitude_deg)
        bearing = great_circle_forward_bearing_at_point(origin, 190.0, t0)
        before = horizontal_position_from_t0(*t0, bearing, 800, -1.0)
        after = horizontal_position_from_t0(*t0, bearing, 800, 1.0)
        self.assertEqual(horizontal_position_from_t0(
            *t0, bearing, 800, 0), t0)
        self.assertAlmostEqual(distance_km(before, t0), 800 / 3600,
                               places=9)
        self.assertAlmostEqual(distance_km(t0, after), 800 / 3600,
                               places=9)
        advanced = horizontal_position_from_t0(*t0, bearing, 800, 20)
        advanced_bearing = great_circle_forward_bearing_at_point(
            origin, 190.0, advanced)
        returned = propagate_great_circle_position(
            *advanced, advanced_bearing, -800 * 20 / 3600)
        self.assertLess(distance_km(returned, t0), 1e-9)

    def test_dateline_and_equivalent_zero_bearings(self):
        crossed = propagate_great_circle_position(
            0.0, 179.9, 90.0, 30.0)
        self.assertLess(crossed[1], -179.8)
        north_zero = propagate_great_circle_position(
            45.0, 12.0, 0.0, 10.0)
        north_full = propagate_great_circle_position(
            45.0, 12.0, 360.0, 10.0)
        self.assertAlmostEqual(north_zero[0], north_full[0], places=12)
        self.assertAlmostEqual(north_zero[1], north_full[1], places=12)

    def test_propagation_is_independent_of_system_timezone(self):
        original = os.environ.get("TZ")
        results = []
        try:
            for zone in ("UTC", "Europe/Warsaw", "America/New_York"):
                os.environ["TZ"] = zone
                if hasattr(time, "tzset"):
                    time.tzset()
                results.append(horizontal_position_from_t0(
                    51.0, 21.0, 123.0, 800, 15.0))
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            if hasattr(time, "tzset"):
                time.tzset()
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])


if __name__ == "__main__":
    unittest.main()
