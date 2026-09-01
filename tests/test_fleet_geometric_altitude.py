import datetime
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fleet_geometric_altitude import (
    FleetEstimateConfidence,
    FleetEstimatorPolicy,
    FleetGeometricAltitudeEstimator,
    PgmGeoidProvider,
    haversine_km,
)
import transit_warning as transit


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 31, 21, 34, 26, tzinfo=UTC)


class ConstantGeoid:
    def __init__(self, value=34.0):
        self.value = value
        self.calls = 0

    def undulation_m(self, latitude, longitude):
        self.calls += 1
        return self.value


def add_sample(estimator, icao, *, altitude=23700, difference=800,
               correction=None, age=60, latitude=51.0, longitude=21.0,
               datum="WGS84_HAE", provenance="RAW_ADSB_TC19",
               pressure_age=0.2, position_age=0.3):
    geoid = estimator.geoid_provider.undulation_m(latitude, longitude)
    geometric = (altitude + difference) * 0.3048 - geoid
    if correction is None:
        reference = altitude * 0.3048
    else:
        reference = geometric - correction
    return estimator.add_observation(
        timestamp_utc=NOW - datetime.timedelta(seconds=age),
        icao=icao, latitude=latitude, longitude=longitude,
        pressure_altitude_ft=altitude,
        gnss_baro_difference_ft=difference,
        production_reference_altitude_m=reference,
        adsb_version=2, datum=datum, provenance=provenance,
        pressure_altitude_age_seconds=pressure_age,
        position_age_seconds=position_age,
    )


class FleetEstimatorTests(unittest.TestCase):
    def setUp(self):
        self.estimator = FleetGeometricAltitudeEstimator(ConstantGeoid())

    def estimate(self, exclude="TARGET"):
        return self.estimator.estimate(
            23700, (51.0, 21.0), NOW,
            production_reference_altitude_m=7230.0,
            exclude_icao=exclude)

    def test_qualified_sample_is_accepted(self):
        self.assertTrue(add_sample(self.estimator, "ABC001"))
        self.assertEqual(1, self.estimator.sample_count)

    def test_unknown_datum_is_rejected(self):
        self.assertFalse(add_sample(
            self.estimator, "ABC001", datum="UNKNOWN"))
        self.assertEqual(0, self.estimator.sample_count)

    def test_synthetic_mlat_beast_is_rejected(self):
        self.assertFalse(add_sample(
            self.estimator, "ABC001", provenance="MLAT_BEAST_TC19"))

    def test_stale_alignment_is_rejected(self):
        self.assertFalse(add_sample(
            self.estimator, "ABC001", pressure_age=2.001))
        self.assertFalse(add_sample(
            self.estimator, "ABC002", position_age=5.001))

    def test_sample_expires_independently(self):
        add_sample(self.estimator, "OLD001", age=3600.001)
        add_sample(self.estimator, "NEW001", age=1)
        self.assertEqual(1, self.estimator.prune(NOW))
        self.assertEqual(1, self.estimator.sample_count)

    def test_new_aircraft_does_not_refresh_old_sample(self):
        add_sample(self.estimator, "OLD001", age=3599)
        add_sample(self.estimator, "NEW001", age=0)
        later = NOW + datetime.timedelta(seconds=2)
        self.assertEqual(1, self.estimator.prune(later))
        self.assertEqual(1, self.estimator.sample_count)

    def test_repeated_messages_from_one_aircraft_have_one_vote(self):
        for correction in range(100, 200):
            add_sample(self.estimator, "REPEAT", correction=correction,
                       age=200-correction)
        self.assertEqual(1, self.estimator.sample_count)

    def test_target_aircraft_is_excluded(self):
        for index, correction in enumerate((180, 185, 190)):
            add_sample(self.estimator, f"OTHER{index}", correction=correction)
        add_sample(self.estimator, "TARGET", correction=1000)
        result = self.estimate()
        self.assertLess(result.correction_m, 200)
        self.assertEqual(3, result.aircraft_count)

    def test_vertical_weighting_favors_near_altitude(self):
        for index in range(3):
            add_sample(self.estimator, f"NEAR{index}", correction=200,
                       altitude=23700 + index * 25)
        for index in range(3):
            add_sample(self.estimator, f"FAR{index}", correction=50,
                       altitude=28500 + index * 25)
        self.assertGreater(self.estimate().correction_m, 170)

    def test_spatial_weighting_favors_nearby_aircraft(self):
        for index in range(3):
            add_sample(self.estimator, f"NEAR{index}", correction=200,
                       longitude=21.0 + index * 0.01)
        for index in range(3):
            add_sample(self.estimator, f"FAR{index}", correction=50,
                       longitude=23.0 + index * 0.01)
        self.assertGreater(self.estimate().correction_m, 130)

    def test_time_weighting_favors_recent_aircraft(self):
        for index in range(3):
            add_sample(self.estimator, f"NEW{index}", correction=200, age=10)
        for index in range(3):
            add_sample(self.estimator, f"OLD{index}", correction=50, age=3500)
        self.assertGreater(self.estimate().correction_m, 150)

    def test_longitude_wrap_distance(self):
        self.assertLess(haversine_km((0.0, 179.9), (0.0, -179.9)), 23.0)

    def test_outlier_is_robustly_downweighted(self):
        for index, correction in enumerate((190, 195, 200, 205, 210, 5000)):
            add_sample(self.estimator, f"A{index}", correction=correction)
        self.assertLess(self.estimate().correction_m, 400)

    def test_unavailable_with_insufficient_aircraft(self):
        add_sample(self.estimator, "A", correction=200)
        add_sample(self.estimator, "B", correction=200)
        self.assertIsNone(self.estimate())

    def test_confidence_levels(self):
        for index in range(8):
            add_sample(self.estimator, f"H{index}", correction=180 + index,
                       longitude=21.0 + 0.01 * index)
        self.assertEqual(FleetEstimateConfidence.HIGH,
                         self.estimate().confidence)
        low = FleetGeometricAltitudeEstimator(
            ConstantGeoid(), FleetEstimatorPolicy(minimum_aircraft=3))
        for index, correction in enumerate((100, 250, 500)):
            add_sample(low, f"L{index}", correction=correction)
        result = low.estimate(
            23700, (51, 21), NOW,
            production_reference_altitude_m=7230)
        self.assertEqual(FleetEstimateConfidence.LOW, result.confidence)

    def test_deterministic_replay(self):
        def run():
            estimator = FleetGeometricAltitudeEstimator(ConstantGeoid())
            for index in range(6):
                add_sample(estimator, f"A{index}", correction=170 + 5 * index,
                           age=10 * index, longitude=21 + 0.02 * index)
            return estimator.estimate(
                23700, (51, 21), NOW,
                production_reference_altitude_m=7230)
        self.assertEqual(run(), run())

    def test_missing_geoid_fails_closed(self):
        class Missing:
            def undulation_m(self, latitude, longitude):
                raise OSError("missing")
        estimator = FleetGeometricAltitudeEstimator(Missing())
        self.assertFalse(estimator.add_observation(
            timestamp_utc=NOW, icao="ABC001", latitude=51, longitude=21,
            pressure_altitude_ft=23700, gnss_baro_difference_ft=800,
            production_reference_altitude_m=7230, adsb_version=2,
            datum="WGS84_HAE", provenance="RAW_ADSB_TC19",
            pressure_altitude_age_seconds=0.1, position_age_seconds=0.1))
        self.assertEqual(0, estimator.sample_count)

    def test_ryr4yd_blind_fixture(self):
        rows = [
            (23575, 217.607, 73, 35), (22375, 187.252, 1, 25),
            (23450, 166.475, 180, 116), (23450, 193.485, 137, 128),
            (25125, 75.330, 7, 54), (22000, 194.903, 367, 54),
            (23850, 202.364, 1764, 62), (23325, 186.204, 650, 151),
            (23675, 209.784, 1600, 81), (23325, 208.853, 1550, 110),
            (22000, 160.395, 60, 188), (23100, 259.131, 1213, 199),
        ]
        estimator = FleetGeometricAltitudeEstimator(ConstantGeoid())
        for index, (altitude, correction, age, distance) in enumerate(rows):
            # At this latitude, roughly 111 km per longitude degree.
            add_sample(estimator, f"C{index:02}", altitude=altitude,
                       correction=correction, age=age,
                       longitude=21.0 + distance / 111.0)
        result = estimator.estimate(
            23700, (51, 21), NOW,
            production_reference_altitude_m=7232.864,
            exclude_icao="48C135")
        self.assertAlmostEqual(178.0, result.correction_m, delta=20.0)
        self.assertGreater(result.correction_m, 130.0)
        self.assertNotEqual("48C135", next(iter(estimator._samples)))


class PgmGeoidProviderTests(unittest.TestCase):
    def test_small_grid_interpolates_and_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.pgm"
            header = (b"P5\n# Offset -10\n# Scale 0.5\n4 3\n65535\n")
            values = range(12)
            path.write_bytes(header + b"".join(
                int(value).to_bytes(2, "big") for value in values))
            provider = PgmGeoidProvider(path)
            first = provider.undulation_m(0.0, 0.0)
            second = provider.undulation_m(0.0, 360.0)
            self.assertEqual(-8.0, first)
            self.assertEqual(first, second)
            self.assertEqual(1, len(provider._cache))


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.previous = (
            transit.fleet_geometric_altitude_enabled,
            transit.fleet_geometric_altitude_estimator,
            transit.raw_adsb_versions,
            transit.altitude_sources,
            transit.aircraft_motion_states,
            transit.gnss_altitude_states,
            transit.transit_snapshot_manager,
        )
        transit.fleet_geometric_altitude_enabled = True
        transit.fleet_geometric_altitude_estimator = (
            FleetGeometricAltitudeEstimator(ConstantGeoid()))
        transit.raw_adsb_versions = {}
        transit.altitude_sources = {}
        transit.aircraft_motion_states = {}
        transit.gnss_altitude_states = {}
        transit.transit_snapshot_manager = None

    def tearDown(self):
        (transit.fleet_geometric_altitude_enabled,
         transit.fleet_geometric_altitude_estimator,
         transit.raw_adsb_versions,
         transit.altitude_sources,
         transit.aircraft_motion_states,
         transit.gnss_altitude_states,
         transit.transit_snapshot_manager) = self.previous

    def add_runtime_sample(self, icao, longitude, difference=800):
        transit.raw_adsb_versions[icao] = transit.RawAdsbVersionState(
            2, NOW, "WGS84_HAE")
        transit.altitude_sources[icao] = {
            "adsb": transit.AltitudeMeasurement(
                "adsb", "barometric", 23700, 7230.0, NOW, "MSG,3")}
        transit.aircraft_motion_states[icao] = transit.AircraftMotionState(
            position=transit.PositionParameter(
                51.0, longitude, NOW, "adsb"))
        transit.update_gnss_altitude_diagnostic(SimpleNamespace(
            icao=icao, gnss_minus_baro_ft=difference,
            gnss_minus_baro_raw=33, subtype=1, available=True,
            vertical_rate_source="BARO", receiver_timestamp_hex=None), NOW)

    def test_runtime_ingestion_and_snapshot_are_additive(self):
        for index in range(3):
            self.add_runtime_sample(f"CAL{index}", 21.0 + index * 0.01)
        transit.altitude_sources["TARGET"] = {
            "adsb": transit.AltitudeMeasurement(
                "adsb", "barometric", 23700, 7230.0, NOW, "MSG,3")}
        transit.aircraft_motion_states["TARGET"] = transit.AircraftMotionState(
            position=transit.PositionParameter(51.0, 21.0, NOW, "adsb"))

        result = transit.fleet_geometric_altitude_snapshot_diagnostics(
            "TARGET", NOW)

        self.assertTrue(result["available"])
        self.assertEqual("FLEET_GEOMETRIC", result["source"])
        self.assertEqual(3, result["aircraft_count"])
        self.assertNotIn("latitude", result)
        self.assertNotIn("longitude", result)

    def test_own_gnss_comparison_is_separate_and_target_is_excluded(self):
        for index in range(3):
            self.add_runtime_sample(f"CAL{index}", 21.0 + index * 0.01)
        self.add_runtime_sample("TARGET", 21.0, difference=1200)

        result = transit.fleet_geometric_altitude_snapshot_diagnostics(
            "TARGET", NOW)

        self.assertEqual(3, result["aircraft_count"])
        self.assertIn("own_gnss_altitude_m", result)
        self.assertIn("fleet_minus_own_gnss_m", result)

    def test_enabled_diagnostics_do_not_change_production_prediction(self):
        arguments = ((51.0, 21.0), (51.5, 21.5), 200, 800,
                     10986.668, 10, 180)
        fixed_clock = SimpleNamespace(now_utc=lambda: NOW)
        observer = transit.ObserverPosition(51.0, 21.0, 200.0)
        with patch.object(transit, "clock", fixed_clock):
            transit.fleet_geometric_altitude_enabled = False
            baseline = transit.transit_pred(*arguments, observer)
            for index in range(3):
                self.add_runtime_sample(
                    f"CAL{index}", 21.0 + index * 0.01)
            transit.fleet_geometric_altitude_enabled = True
            enabled = transit.transit_pred(*arguments, observer)
        self.assertEqual(baseline, enabled)

    def test_snapshot_block_is_absent_when_feature_is_disabled(self):
        manager = Mock()
        transit.transit_snapshot_manager = manager
        transit.fleet_geometric_altitude_enabled = False
        transit.sun_predicted_transit_utc["TARGET"] = (
            NOW + datetime.timedelta(seconds=5))
        result = (51.1, 21.1, 120.0, 10.0, 20.0, 30.0, 5.0,
                  0, 121.0, 10.5, NOW)
        solver_input = {
            "aircraft_altitude_m": 7000.0,
            "groundspeed": 800.0,
            "track": 180.0,
        }
        try:
            with patch.object(transit, "build_frozen_prediction_state",
                              return_value={}):
                transit.capture_transit_prediction(
                    "TARGET", "TEST", "sun", result, NOW, solver_input,
                    transit.ObserverPosition(51.0, 21.0, 200.0))
            captured = manager.consider_prediction.call_args.args[0]
        finally:
            transit.sun_predicted_transit_utc.pop("TARGET", None)
        self.assertNotIn(
            "fleet_geometric_altitude_diagnostics", captured)


if __name__ == "__main__":
    unittest.main()
