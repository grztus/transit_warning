import datetime
from dataclasses import replace
import json
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from observer_position import ObserverContext, ObserverPosition
from shadow_2d_prediction import (
    Shadow2DConfig,
    Shadow2DDiagnosticWriter,
    ShadowEncounterContext,
    coarse_screen,
    comparison_record,
    evaluate_shadow_geometry,
    exact_refine,
    run_shadow_pipeline,
)
from transit_prediction_model import (
    IntentParameter,
    MotionParameter,
    current_vertical_prediction_policy,
    VerticalIntentState,
    VerticalMotionState,
)


UTC = datetime.timezone.utc
BASE = datetime.datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
R_EARTH = 6371.0


class SyntheticGeometry:
    def __init__(self, *, x_offset=0.0, x_rate=0.01, y_offset=0.0,
                 y_rate=0.0, center=45.0, body_altitude=20.0,
                 body_altitude_rate=0.0, altitude_scale=0.0,
                 body_azimuth=100.0):
        self.x_offset = x_offset
        self.x_rate = x_rate
        self.y_offset = y_offset
        self.y_rate = y_rate
        self.center = center
        self.body_altitude = body_altitude
        self.body_altitude_rate = body_altitude_rate
        self.altitude_scale = altitude_scale
        self.body_azimuth = body_azimuth

    def _seconds(self, position):
        north = math.radians(position[0]) * R_EARTH
        east = math.radians(position[1]) * R_EARTH
        return east / 700.0 * 3600.0

    def aircraft(self, observer, position, altitude_m):
        seconds = self._seconds(position)
        body_alt = self.body_altitude + self.body_altitude_rate * seconds
        return SimpleNamespace(
            azimuth_deg=self.body_azimuth + self.x_offset
            + self.x_rate * (seconds - self.center),
            altitude_angle_deg=body_alt + self.y_offset
            + self.y_rate * (seconds - self.center)
            + self.altitude_scale * (altitude_m - 10000.0),
            distance_km=100.0,
        )

    def body(self, name, when_utc, observer):
        seconds = (when_utc - BASE).total_seconds()
        return SimpleNamespace(
            azimuth_deg=self.body_azimuth % 360.0,
            altitude_deg=self.body_altitude
            + self.body_altitude_rate * seconds,
            angular_diameter_arcsec=1800.0,
        )


def context(geometry=None, *, track=90.0, vertical_rate=None, intent=None,
            latitude=0.0, body="SUN"):
    geometry = geometry or SyntheticGeometry()
    altitude = MotionParameter(10000.0, BASE, "adsb")
    if vertical_rate is None:
        motion = VerticalMotionState(altitude=altitude)
    else:
        samples = tuple(MotionParameter(vertical_rate, BASE, "adsb")
                        for _ in range(3))
        motion = VerticalMotionState(
            altitude=altitude, vertical_rate=samples[-1],
            vertical_rate_history=samples)
    return ShadowEncounterContext(
        icao="ABC123", callsign="TEST1", body=body,
        prediction_base_utc=BASE,
        observer_context=ObserverContext(
            ObserverPosition(1.0, 2.0, 100.0), "STATIC", "STATIC"),
        latitude_deg=latitude, longitude_deg=0.0, track_deg=track,
        groundspeed_kmh=700.0, current_altitude_m=10000.0,
        vertical_motion=motion, vertical_intent=intent,
        vertical_policy=current_vertical_prediction_policy(),
        qnh_hpa=1013.25, geometric_altitude_correction_m=25.0,
        altitude_source="OWN_GNSS_GEOMETRIC", position_source="mlat",
        track_source="MLAT_BEAST_TC19_FRESH",
        aircraft_los_resolver=geometry.aircraft,
        body_position_resolver=geometry.body)


class Shadow2DPredictionTests(unittest.TestCase):
    def setUp(self):
        self.config = Shadow2DConfig(enabled=True)

    def test_center_crossing_and_exact_minimum(self):
        result = run_shadow_pipeline(context(), self.config)
        self.assertTrue(result.coarse.passed)
        self.assertTrue(result.exact.succeeded)
        self.assertAlmostEqual(45.0, result.exact.tca_seconds, places=2)
        self.assertLess(result.exact.separation_deg, 1e-5)
        self.assertEqual("INTERIOR", result.exact.boundary_status)

    def test_tangent_sub_limb_without_legacy_t0_is_shadow_only(self):
        item = context(SyntheticGeometry(
            x_offset=0.20, x_rate=0.0, y_rate=0.01))
        result = run_shadow_pipeline(item, self.config, legacy_tca_seconds=None)
        record = comparison_record(item, result, BASE, legacy_result=None)
        self.assertTrue(result.exact.succeeded)
        self.assertLess(result.exact.separation_body_radii, 1.0)
        self.assertTrue(record["shadow_only"])
        self.assertFalse(record["legacy_available"])

    def test_opposite_side_tangent_is_also_detected(self):
        result = run_shadow_pipeline(context(SyntheticGeometry(
            x_offset=-0.20, x_rate=0.0, y_rate=0.01)), self.config)
        self.assertLess(result.exact.separation_body_radii, 1.0)

    def test_just_inside_and_outside_one_radius(self):
        inside = run_shadow_pipeline(context(SyntheticGeometry(
            x_offset=0.26, x_rate=0.0, y_rate=0.01)), self.config)
        outside = run_shadow_pipeline(context(SyntheticGeometry(
            x_offset=0.27, x_rate=0.0, y_rate=0.01)), self.config)
        self.assertLess(inside.exact.separation_body_radii, 1.0)
        self.assertGreater(outside.exact.separation_body_radii, 1.0)

    def test_coarse_uses_sixteen_nominal_samples_and_local_subdivision(self):
        result = coarse_screen(context(), self.config)
        self.assertTrue(result.locally_refined)
        self.assertGreaterEqual(result.evaluation_count, 16)
        nominal = {float(value) for value in range(0, 901, 60)}
        self.assertTrue(nominal.issubset(
            {sample.dt_seconds for sample in result.samples}))

    def test_dynamic_body_motion_is_evaluated_at_trial_times(self):
        geometry = SyntheticGeometry(body_altitude_rate=0.002)
        item = context(geometry)
        first = evaluate_shadow_geometry(item, 0.0)
        later = evaluate_shadow_geometry(item, 100.0)
        self.assertAlmostEqual(0.2,
                               later.body_altitude_deg-first.body_altitude_deg)

    def test_azimuth_wrap_and_low_moon(self):
        result = run_shadow_pipeline(context(
            SyntheticGeometry(body_azimuth=359.9, body_altitude=1.0),
            body="MOON"), self.config)
        self.assertTrue(result.exact.succeeded)
        self.assertLess(result.exact.separation_deg, 1e-5)

    def test_track_and_latitude_zero_are_valid_inputs(self):
        item = context(track=0.0, latitude=0.0)
        self.assertEqual(0.0, item.track_deg)
        self.assertEqual(0.0, item.latitude_deg)

    def test_climb_descent_level_and_prediction_cap(self):
        level = evaluate_shadow_geometry(context(), 200.0)
        climb = evaluate_shadow_geometry(context(vertical_rate=1200.0), 200.0)
        descent = evaluate_shadow_geometry(context(vertical_rate=-1200.0), 200.0)
        self.assertEqual("VR_IGNORE", level.vertical_mode)
        self.assertEqual("DYNAMIC_VALID", climb.vertical_mode)
        self.assertEqual("DYNAMIC_VALID", descent.vertical_mode)
        self.assertAlmostEqual(10000.0 + 1200.0*2*0.3048 + 25.0,
                               climb.aircraft_altitude_m)
        self.assertLess(descent.aircraft_altitude_m, level.aircraft_altitude_m)

    def test_selected_altitude_clamp_is_preserved(self):
        intent = VerticalIntentState(
            selected_altitude=IntentParameter(33000.0, BASE, "MCP/FCU"),
            nav_qnh=IntentParameter(1013.25, BASE, "TC29"))
        sample = evaluate_shadow_geometry(
            context(vertical_rate=3000.0, intent=intent), 120.0)
        self.assertTrue(sample.intent_clamped)

    def test_start_and_end_boundaries(self):
        start = run_shadow_pipeline(context(SyntheticGeometry(center=-10)),
                                    self.config)
        end = run_shadow_pipeline(context(SyntheticGeometry(center=950)),
                                  self.config)
        self.assertEqual("START_BOUNDARY", start.exact.boundary_status)
        self.assertEqual("END_BOUNDARY_CONTINUING",
                         end.exact.boundary_status)

    def test_low_rising_body_is_not_blocked_at_current_time(self):
        geometry = SyntheticGeometry(
            center=45.0, body_altitude=-0.2, body_altitude_rate=0.01)
        result = run_shadow_pipeline(context(geometry), self.config)
        self.assertTrue(result.coarse.candidate_exists)
        self.assertTrue(result.exact.succeeded)

    def test_optional_legacy_seed_can_disagree_without_controlling_result(self):
        result = run_shadow_pipeline(context(), self.config,
                                     legacy_tca_seconds=500.0)
        self.assertAlmostEqual(45.0, result.exact.tca_seconds, places=2)

    def test_global_minimum_wins_when_legacy_seed_targets_other_minimum(self):
        item = context()

        def multiple_minima(_context, seconds):
            seconds = float(seconds)
            first = 0.10 + abs(seconds - 100.0) * 0.002
            second = 0.20 + abs(seconds - 300.0) * 0.002
            separation = min(first, second)
            base = evaluate_shadow_geometry(item, seconds)
            return replace(
                base, separation_deg=separation,
                objective=1.0 - math.cos(math.radians(separation)),
                relative_x_deg=separation, relative_y_deg=0.0)

        samples = tuple(multiple_minima(item, seconds)
                        for seconds in range(0, 901, 60))
        coarse = replace(
            coarse_screen(item, self.config),
            estimated_tca_seconds=100.0,
            estimated_separation_deg=0.10,
            best_segment_start_seconds=60.0,
            best_segment_end_seconds=120.0,
            samples=samples)
        exact = exact_refine(
            item, coarse, self.config, legacy_tca_seconds=300.0,
            evaluator=multiple_minima)
        self.assertTrue(exact.succeeded)
        self.assertAlmostEqual(100.0, exact.tca_seconds, places=2)
        self.assertAlmostEqual(0.10, exact.separation_deg, places=5)

    def test_exact_failure_is_explicit_and_fail_open(self):
        item = context()
        coarse = coarse_screen(item, self.config)
        def failure(*args):
            raise RuntimeError("forced")
        exact = exact_refine(item, coarse, self.config, evaluator=failure)
        self.assertFalse(exact.succeeded)
        self.assertEqual("FAILED", exact.solver_status)
        self.assertEqual("EXACT_ERROR:RuntimeError", exact.reason)

    def test_diagnostic_writer_is_private_and_deduplicated(self):
        item = context()
        result = run_shadow_pipeline(item, self.config)
        record = comparison_record(item, result, BASE)
        with tempfile.TemporaryDirectory() as directory:
            writer = Shadow2DDiagnosticWriter(directory, minimum_interval=30)
            self.assertTrue(writer.record(record))
            self.assertFalse(writer.record(record))
            path = next(Path(directory).rglob("*.jsonl"))
            stored = json.loads(path.read_text(encoding="utf-8"))
            text = json.dumps(stored).lower()
            self.assertNotIn("observer_lat", text)
            self.assertNotIn("observer_lon", text)
            self.assertNotIn("coordinates", text)
            with self.assertRaisesRegex(ValueError, "private field"):
                writer.record({**record, "observer_latitude": 1.0})

    def test_diagnostic_writer_records_one_withdrawal_then_resets_pair(self):
        item = context()
        record = comparison_record(
            item, run_shadow_pipeline(item, self.config), BASE)
        with tempfile.TemporaryDirectory() as directory:
            writer = Shadow2DDiagnosticWriter(directory, minimum_interval=30)
            self.assertTrue(writer.record(record))
            withdrawn_at = BASE + datetime.timedelta(seconds=2)
            self.assertTrue(writer.withdraw(
                item.icao, item.callsign, item.body, withdrawn_at,
                "AIRCRAFT_EXPIRED"))
            self.assertFalse(writer.withdraw(
                item.icao, item.callsign, item.body,
                withdrawn_at + datetime.timedelta(seconds=2),
                "AIRCRAFT_EXPIRED"))
            rows = [json.loads(line) for line in
                    next(Path(directory).rglob("*.jsonl")).read_text(
                        encoding="utf-8").splitlines()]
            self.assertEqual(["EXACT", "WITHDRAWN"],
                             [row["stage"] for row in rows])


if __name__ == "__main__":
    unittest.main()
