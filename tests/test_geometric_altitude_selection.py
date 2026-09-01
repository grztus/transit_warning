import datetime
import math
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import transit_warning as transit
from fleet_geometric_altitude import (
    FleetEstimateConfidence,
    GeometricAltitudeEstimate,
)
from geometric_altitude_selection import (
    GeometricAltitudeSelector,
    GeometricAltitudeSource,
    OwnGnssGeometricInput,
)


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 31, 21, 34, 0, tzinfo=UTC)


def fleet_estimate(confidence, altitude=7210.0, correction=110.0):
    return GeometricAltitudeEstimate(
        altitude_m=altitude,
        correction_m=correction,
        uncertainty_m=35.0,
        confidence=confidence,
        source="FLEET_GEOMETRIC",
        sample_count=8,
        aircraft_count=8,
        vertical_span_ft=(22000.0, 25000.0),
        age_min_seconds=1.0,
        age_max_seconds=60.0,
        spatial_min_km=5.0,
        spatial_max_km=100.0,
        weighted_residual_rms_m=25.0,
        strongest_contributor_weight_fraction=0.2,
        generated_at_utc=NOW,
    )


class GeometricAltitudeSelectorTests(unittest.TestCase):
    def setUp(self):
        self.selector = GeometricAltitudeSelector()

    def test_qualified_own_gnss_wins_over_fleet(self):
        own = OwnGnssGeometricInput(23700.0, 700.0, 30.0)
        result = self.selector.select(
            7200.0, own_gnss=own,
            fleet_estimate=fleet_estimate(FleetEstimateConfidence.HIGH))
        self.assertEqual(
            GeometricAltitudeSource.OWN_GNSS_GEOMETRIC, result.source)
        self.assertAlmostEqual((23700 + 700) * 0.3048 - 30,
                               result.altitude_m)

    def test_correct_sign_and_no_qnh_double_application(self):
        own = OwnGnssGeometricInput(23000.0, 500.0, 25.0)
        result = self.selector.select(7100.0, own_gnss=own)
        self.assertAlmostEqual(7137.8, result.altitude_m)
        self.assertAlmostEqual(37.8, result.correction_m)
        self.assertNotAlmostEqual(7100.0 + 500.0 * 0.3048 - 25.0,
                                  result.altitude_m)

    def test_high_and_medium_fleet_are_usable(self):
        for confidence in (FleetEstimateConfidence.HIGH,
                           FleetEstimateConfidence.MEDIUM):
            with self.subTest(confidence=confidence):
                result = self.selector.select(
                    7100.0, fleet_estimate=fleet_estimate(confidence),
                    own_unavailable_reason="no_tc19")
                self.assertEqual(
                    GeometricAltitudeSource.FLEET_GEOMETRIC, result.source)
                self.assertEqual(confidence.value, result.confidence)

    def test_low_fleet_is_rejected(self):
        for confidence in (FleetEstimateConfidence.LOW,
                           FleetEstimateConfidence.UNAVAILABLE):
            with self.subTest(confidence=confidence):
                result = self.selector.select(
                    7100.0, fleet_estimate=fleet_estimate(confidence))
                self.assertEqual(
                    GeometricAltitudeSource.BARO_QNH, result.source)
                self.assertEqual(
                    "fleet_confidence_{}_rejected".format(
                        confidence.value.lower()), result.fallback_reason)

    def test_unavailable_sources_fall_back_to_baro(self):
        result = self.selector.select(
            7100.0, own_unavailable_reason="datum_unknown",
            fleet_unavailable_reason="fleet_unavailable")
        self.assertEqual(7100.0, result.altitude_m)
        self.assertEqual(GeometricAltitudeSource.BARO_QNH, result.source)
        self.assertEqual("fleet_unavailable", result.fallback_reason)

    def test_selection_is_deterministic(self):
        arguments = dict(
            production_baro_altitude_m=7100.0,
            own_gnss=OwnGnssGeometricInput(23000, 500, 25),
            fleet_estimate=fleet_estimate(FleetEstimateConfidence.HIGH))
        self.assertEqual(self.selector.select(**arguments),
                         self.selector.select(**arguments))


class RuntimeSelectionTests(unittest.TestCase):
    def setUp(self):
        names = (
            "geometric_altitude_selection_enabled",
            "fleet_geometric_altitude_estimator", "altitude_sources",
            "aircraft_motion_states", "gnss_altitude_states",
            "raw_adsb_versions", "geometric_altitude_selections",
        )
        self.original = {name: getattr(transit, name) for name in names}
        transit.geometric_altitude_selection_enabled = True
        transit.altitude_sources = {}
        transit.aircraft_motion_states = {}
        transit.gnss_altitude_states = {}
        transit.raw_adsb_versions = {}
        transit.geometric_altitude_selections = {}

    def tearDown(self):
        for name, value in self.original.items():
            setattr(transit, name, value)

    @staticmethod
    def prediction(delta_m=100.0):
        return SimpleNamespace(
            predicted_altitude_m=7100.0 + delta_m,
            altitude_delta_m=delta_m)

    def install_target(self, *, tc19_age=0.0, datum="WGS84_HAE"):
        transit.altitude_sources["TARGET"] = {
            "adsb": transit.AltitudeMeasurement(
                "adsb", "barometric", 23000.0, 7100.0, NOW, "MSG,3")}
        transit.aircraft_motion_states["TARGET"] = transit.AircraftMotionState(
            position=transit.PositionParameter(51.0, 21.0, NOW, "adsb"))
        transit.gnss_altitude_states["TARGET"] = (
            transit.GnssAltitudeDiagnosticState(
                500.0, 21, 1, NOW - datetime.timedelta(seconds=tc19_age),
                True, "BARO", None))
        transit.raw_adsb_versions["TARGET"] = transit.RawAdsbVersionState(
            2, NOW, datum)

    def test_vertical_delta_is_applied_in_pressure_domain(self):
        self.install_target()
        estimator = Mock()
        estimator.geoid_height_m.return_value = 25.0
        transit.fleet_geometric_altitude_estimator = estimator
        result = transit.select_geometric_altitude_for_prediction(
            "TARGET", self.prediction(100.0), NOW)
        expected = (23000.0 + 100.0 / 0.3048 + 500.0) * 0.3048 - 25.0
        self.assertAlmostEqual(expected, result.altitude_m)
        self.assertEqual(
            GeometricAltitudeSource.OWN_GNSS_GEOMETRIC, result.source)
        estimator.estimate.assert_not_called()

    def test_stale_or_unknown_own_gnss_uses_fleet(self):
        for age, datum in ((5.001, "WGS84_HAE"), (0.0, "UNKNOWN")):
            with self.subTest(age=age, datum=datum):
                self.install_target(tc19_age=age, datum=datum)
                estimator = Mock()
                estimator.estimate.return_value = fleet_estimate(
                    FleetEstimateConfidence.MEDIUM, altitude=7222.0)
                transit.fleet_geometric_altitude_estimator = estimator
                result = transit.select_geometric_altitude_for_prediction(
                    "TARGET", self.prediction(), NOW)
                self.assertEqual(
                    GeometricAltitudeSource.FLEET_GEOMETRIC, result.source)
                self.assertEqual("TARGET",
                                 estimator.estimate.call_args.kwargs["exclude_icao"])

    def test_stale_position_prevents_own_gnss_selection(self):
        self.install_target()
        transit.aircraft_motion_states["TARGET"].position = (
            transit.PositionParameter(
                51.0, 21.0, NOW - datetime.timedelta(seconds=5.001),
                "adsb"))
        estimator = Mock()
        estimator.estimate.return_value = None
        transit.fleet_geometric_altitude_estimator = estimator
        result = transit.select_geometric_altitude_for_prediction(
            "TARGET", self.prediction(), NOW)
        self.assertEqual(GeometricAltitudeSource.BARO_QNH, result.source)

    def test_missing_geoid_and_legacy_state_fall_back(self):
        transit.fleet_geometric_altitude_estimator = None
        result = transit.select_geometric_altitude_for_prediction(
            "TARGET", self.prediction(), NOW)
        self.assertEqual(GeometricAltitudeSource.BARO_QNH, result.source)
        self.assertEqual("geoid_unavailable", result.fallback_reason)

    def test_disabled_path_is_exact_baro_and_does_no_estimator_work(self):
        transit.geometric_altitude_selection_enabled = False
        transit.fleet_geometric_altitude_estimator = Mock()
        result = transit.select_geometric_altitude_for_prediction(
            "TARGET", self.prediction(), NOW)
        self.assertEqual(7200.0, result.altitude_m)
        self.assertEqual(GeometricAltitudeSource.BARO_QNH, result.source)
        transit.fleet_geometric_altitude_estimator.assert_not_called()

    def test_selected_altitude_changes_only_final_aircraft_elevation(self):
        baro_prediction = SimpleNamespace(
            predicted_altitude_m=7200.0,
            altitude_delta_m=100.0,
            mode=transit.VerticalPredictionMode.LEVEL,
            reason="LEVEL",
            last_vertical_rate_fpm=0.0,
            vertical_rate_age_seconds=0.0,
            stability_samples=(),
            spread_fpm=0.0,
            source="adsb",
            applied_seconds=0.0,
            current_altitude_m=7100.0,
            altitude_age_seconds=0.0,
        )
        vertical_state = SimpleNamespace(
            prediction_before_clamp=baro_prediction,
            prediction=baro_prediction,
            intent_details={})
        selected = transit.SelectedGeometricAltitude(
            altitude_m=7400.0,
            source=GeometricAltitudeSource.OWN_GNSS_GEOMETRIC,
            correction_m=200.0,
            uncertainty_m=None,
            confidence="QUALIFIED",
            own_gnss_available=True,
            own_gnss_altitude_m=7400.0,
            fleet_available=False,
            fleet_altitude_m=None,
            fleet_confidence=None,
            fleet_uncertainty_m=None,
            fallback_reason=None)
        original = (51.1, 21.1, 120.0, 20.0, 100.0, 50.0,
                    10.0, 0, 120.0, 21.0, NOW)
        observer = transit.ObserverPosition(51.0, 21.0, 200.0)
        with patch.object(transit, "predict_vertical_state_at_time",
                          return_value=vertical_state), patch.object(
                transit, "select_geometric_altitude_for_prediction",
                return_value=selected):
            updated = transit.apply_vertical_prediction_to_transit_result(
                "TARGET", "moon", original, 7100.0, NOW, observer)
        expected = math.degrees(math.atan((7400.0 - 200.0) / 100000.0))
        self.assertAlmostEqual(expected, updated[3])
        for index in (0, 1, 2, 4, 5, 6, 7, 8, 9, 10):
            self.assertEqual(original[index], updated[index])

    def test_snapshot_records_actual_selected_source(self):
        manager = Mock()
        old_manager = transit.transit_snapshot_manager
        old_times = dict(transit.sun_predicted_transit_utc)
        old_vertical = dict(transit.vertical_transit_diagnostics)
        transit.transit_snapshot_manager = manager
        prediction = SimpleNamespace(
            mode=transit.VerticalPredictionMode.LEVEL, reason="LEVEL",
            last_vertical_rate_fpm=0.0, vertical_rate_age_seconds=0.0,
            applied_seconds=0.0, predicted_altitude_m=7200.0)
        transit.vertical_transit_diagnostics[("TARGET", "sun")] = (
            transit.VerticalTransitDiagnostic(
                "sun", prediction, 1.0, 0.5))
        transit.geometric_altitude_selections[("TARGET", "sun")] = (
            transit.SelectedGeometricAltitude(
                7400.0, GeometricAltitudeSource.OWN_GNSS_GEOMETRIC,
                200.0, None, "QUALIFIED", True, 7400.0,
                False, None, None, None, None))
        transit.sun_predicted_transit_utc["TARGET"] = (
            NOW + datetime.timedelta(seconds=10))
        result = (51.1, 21.1, 120.0, 4.1, 100.0, 2.0,
                  10.0, 0, 120.0, 4.0, NOW)
        solver_input = {
            "aircraft_altitude_m": 7100.0,
            "groundspeed": 800.0,
            "track": 180.0,
        }
        try:
            with patch.object(transit, "build_frozen_prediction_state",
                              return_value={}), patch.object(
                    transit, "gnss_altitude_snapshot_diagnostics",
                    return_value={}), patch.object(
                    transit, "fleet_geometric_altitude_snapshot_diagnostics",
                    return_value=None):
                transit.capture_transit_prediction(
                    "TARGET", "TEST", "sun", result, NOW, solver_input,
                    transit.ObserverPosition(51.0, 21.0, 200.0))
            captured = manager.consider_prediction.call_args.args[0]
            selection = captured["geometric_altitude_selection"]
            self.assertEqual("OWN_GNSS_GEOMETRIC",
                             selection["selected_source"])
            self.assertEqual(7400.0, captured["aircraft_altitude_m"])
            self.assertEqual(7200.0,
                             selection["production_baro_altitude_m"])
        finally:
            transit.transit_snapshot_manager = old_manager
            transit.sun_predicted_transit_utc.clear()
            transit.sun_predicted_transit_utc.update(old_times)
            transit.vertical_transit_diagnostics.clear()
            transit.vertical_transit_diagnostics.update(old_vertical)

    def test_disabled_snapshot_has_no_additive_selection_block(self):
        transit.geometric_altitude_selection_enabled = False
        transit.geometric_altitude_selections = {}
        manager = Mock()
        old_manager = transit.transit_snapshot_manager
        transit.transit_snapshot_manager = manager
        transit.sun_predicted_transit_utc["TARGET"] = (
            NOW + datetime.timedelta(seconds=10))
        result = (51.1, 21.1, 120.0, 4.1, 100.0, 2.0,
                  10.0, 0, 120.0, 4.0, NOW)
        solver_input = {
            "aircraft_altitude_m": 7100.0,
            "groundspeed": 800.0,
            "track": 180.0,
        }
        try:
            with patch.object(transit, "build_frozen_prediction_state",
                              return_value={}), patch.object(
                    transit, "gnss_altitude_snapshot_diagnostics",
                    return_value={}), patch.object(
                    transit, "fleet_geometric_altitude_snapshot_diagnostics",
                    return_value=None):
                transit.capture_transit_prediction(
                    "TARGET", "TEST", "sun", result, NOW, solver_input,
                    transit.ObserverPosition(51.0, 21.0, 200.0))
            captured = manager.consider_prediction.call_args.args[0]
            self.assertNotIn("geometric_altitude_selection", captured)
            self.assertEqual(7100.0, captured["aircraft_altitude_m"])
        finally:
            transit.transit_snapshot_manager = old_manager
            transit.sun_predicted_transit_utc.pop("TARGET", None)

    def test_compact_photographic_regression_directions(self):
        selector = GeometricAltitudeSelector()
        ryr_baro_m = 7232.864
        ryr_geoid_m = (
            (23675.0 + 875.0) * 0.3048
            - (ryr_baro_m + 218.611))
        ryr_result = selector.select(
            ryr_baro_m,
            own_gnss=OwnGnssGeometricInput(
                23675.0, 875.0, ryr_geoid_m))
        self.assertEqual(
            GeometricAltitudeSource.OWN_GNSS_GEOMETRIC,
            ryr_result.source)
        self.assertAlmostEqual(218.611, ryr_result.correction_m, places=3)

        ent_baro_m = transit.correct_pressure_altitude(24175.0, 1015.0) * 0.3048
        ent_geoid_m = 650.0 * 0.3048 - 73.0 - (
            ent_baro_m - 24175.0 * 0.3048)
        ent_result = selector.select(
            ent_baro_m,
            own_gnss=OwnGnssGeometricInput(
                24175.0, 650.0, ent_geoid_m))
        self.assertEqual(
            GeometricAltitudeSource.OWN_GNSS_GEOMETRIC,
            ent_result.source)
        self.assertAlmostEqual(73.0, ent_result.correction_m, places=6)


if __name__ == "__main__":
    unittest.main()
