import math
import unittest
from unittest.mock import Mock, patch

import transit_warning as transit
from geometric_altitude_selection import (
    GeometricAltitudeSelector,
    OwnGnssGeometricInput,
)
from transit_prediction_model import (
    angular_position_from_observer,
    legacy_flat_angular_position_from_observer,
    precise_angular_position_from_observer,
    slant_distance_km_from_observer,
)


class Wgs84EcefEnuGeometryTests(unittest.TestCase):
    def test_known_equatorial_reference(self):
        result = angular_position_from_observer(
            (0.0, 0.0), 0.0, (0.0, 1.0), 0.0)
        self.assertEqual(result.azimuth_deg, 90.0)
        self.assertAlmostEqual(result.altitude_angle_deg, -0.5, places=10)
        expected_chord_km = 2.0 * 6378.137 * math.sin(
            math.radians(1.0) / 2.0)
        self.assertAlmostEqual(slant_distance_km_from_observer(
            (0.0, 0.0), 0.0, (0.0, 1.0), 0.0),
            expected_chord_km, places=9)

    def test_equal_height_target_is_below_local_horizontal(self):
        result = angular_position_from_observer(
            (51.0, 21.0), 100.0, (51.5, 21.0), 100.0)
        self.assertLess(result.altitude_angle_deg, 0.0)

    def test_curvature_increases_with_range(self):
        near = angular_position_from_observer(
            (0.0, 0.0), 0.0, (0.0, 0.01), 0.0)
        far = angular_position_from_observer(
            (0.0, 0.0), 0.0, (0.0, 1.0), 0.0)
        self.assertLess(far.altitude_angle_deg, near.altitude_angle_deg)

    def test_short_range_approaches_flat_result(self):
        curved = angular_position_from_observer(
            (51.0, 21.0), 100.0, (51.01, 21.0), 100.0)
        flat = legacy_flat_angular_position_from_observer(
            (51.0, 21.0), 100.0, (51.01, 21.0), 100.0)
        self.assertLess(abs(
            curved.altitude_angle_deg - flat.altitude_angle_deg), 0.01)

    def test_curvature_sign_is_consistent_in_cardinal_directions(self):
        origin = (51.0, 21.0)
        results = [
            angular_position_from_observer(origin, 100.0, target, 100.0)
            for target in ((51.9, 21.0), (50.1, 21.0),
                           (51.0, 22.4), (51.0, 19.6))
        ]
        self.assertTrue(all(item.altitude_angle_deg < 0 for item in results))
        self.assertAlmostEqual(
            results[0].altitude_angle_deg,
            results[1].altitude_angle_deg, delta=0.001)
        self.assertAlmostEqual(
            results[2].altitude_angle_deg,
            results[3].altitude_angle_deg, delta=1e-10)

    def test_different_elevations_and_near_horizon_are_finite(self):
        result = angular_position_from_observer(
            (45.0, 10.0), 150.0, (45.5, 11.0), 10150.0)
        self.assertAlmostEqual(result.azimuth_deg, 54.4, places=1)
        self.assertAlmostEqual(result.altitude_angle_deg, 5.499273777, places=8)
        self.assertTrue(math.isfinite(result.altitude_angle_deg))

    def test_north_azimuth_is_normalized_to_zero(self):
        result = angular_position_from_observer(
            (51.0, 21.0), 100.0, (51.9, 21.0), 100.0)
        self.assertEqual(result.azimuth_deg, 0.0)


class DatumBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.original_provider = transit.aircraft_los_geoid_provider

    def tearDown(self):
        transit.aircraft_los_geoid_provider = self.original_provider

    def test_orthometric_endpoints_receive_their_own_geoid_once(self):
        provider = Mock()
        provider.undulation_m.side_effect = [30.0, 35.0]
        transit.aircraft_los_geoid_provider = provider
        actual = transit.aircraft_angular_position_from_observer(
            (50.0, 20.0), 100.0, (50.5, 21.0), 7000.0)
        expected = angular_position_from_observer(
            (50.0, 20.0), 130.0, (50.5, 21.0), 7035.0)
        self.assertEqual(actual, expected)
        self.assertEqual(provider.undulation_m.call_count, 2)

    def test_shadow_precise_los_preserves_unrounded_geometry(self):
        provider = Mock()
        provider.undulation_m.side_effect = [30.0, 35.0]
        transit.aircraft_los_geoid_provider = provider
        actual = transit.precise_aircraft_angular_position_from_observer(
            (50.0, 20.0), 100.0, (50.5, 21.0), 7000.0)
        expected = precise_angular_position_from_observer(
            (50.0, 20.0), 130.0, (50.5, 21.0), 7035.0)
        self.assertEqual(actual, expected)
        self.assertNotEqual(actual.azimuth_deg, round(actual.azimuth_deg, 1))

    def test_own_gnss_hae_is_not_geoid_corrected_twice(self):
        selector = GeometricAltitudeSelector()
        own = OwnGnssGeometricInput(
            predicted_pressure_altitude_ft=23000.0,
            gnss_minus_baro_ft=500.0,
            geoid_undulation_m=35.0,
        )
        selected = selector.select(7000.0, own_gnss=own)
        expected_hae = (23000.0 + 500.0) * 0.3048
        provider = Mock()
        provider.undulation_m.side_effect = [30.0, 35.0]
        transit.aircraft_los_geoid_provider = provider
        with patch.object(
                transit, "angular_position_from_observer",
                wraps=angular_position_from_observer) as geometry:
            transit.aircraft_angular_position_from_observer(
                (50.0, 20.0), 100.0, (50.5, 21.0),
                selected.altitude_m)
        self.assertAlmostEqual(geometry.call_args.args[3], expected_hae)

    def test_baro_and_fleet_msl_like_altitudes_use_local_geoid(self):
        for altitude_m in (7000.0, 7210.0):
            with self.subTest(altitude_m=altitude_m):
                provider = Mock()
                provider.undulation_m.side_effect = [30.0, 35.0]
                transit.aircraft_los_geoid_provider = provider
                with patch.object(
                        transit, "angular_position_from_observer",
                        wraps=angular_position_from_observer) as geometry:
                    transit.aircraft_angular_position_from_observer(
                        (50.0, 20.0), 100.0, (50.5, 21.0), altitude_m)
                self.assertEqual(geometry.call_args.args[1], 130.0)
                self.assertEqual(geometry.call_args.args[3], altitude_m + 35.0)

    def test_missing_or_failed_geoid_uses_explicit_flat_fallback(self):
        transit.aircraft_los_geoid_provider = None
        expected = legacy_flat_angular_position_from_observer(
            (50.0, 20.0), 100.0, (50.5, 21.0), 7000.0)
        self.assertEqual(
            transit.aircraft_angular_position_from_observer(
                (50.0, 20.0), 100.0, (50.5, 21.0), 7000.0),
            expected)
        provider = Mock()
        provider.undulation_m.side_effect = ValueError("unavailable")
        transit.aircraft_los_geoid_provider = provider
        self.assertEqual(
            transit.aircraft_angular_position_from_observer(
                (50.0, 20.0), 100.0, (50.5, 21.0), 7000.0),
            expected)

    def test_mobile_position_controls_active_geoid_lookup(self):
        provider = Mock()
        provider.undulation_m.side_effect = [31.0, 35.0]
        transit.aircraft_los_geoid_provider = provider
        mobile_position = (49.5, 19.5)
        transit.aircraft_angular_position_from_observer(
            mobile_position, 100.0, (50.5, 21.0), 7000.0)
        self.assertEqual(provider.undulation_m.call_args_list[0].args,
                         mobile_position)


if __name__ == "__main__":
    unittest.main()
