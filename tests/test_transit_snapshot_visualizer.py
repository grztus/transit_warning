import copy
import datetime
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from transit_prediction_model import angular_position_from_observer
from tools import transit_snapshot_visualizer as visualizer


UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 8, 23, 16, 40, 35, tzinfo=UTC)


def utc_text(value):
    return value.isoformat().replace("+00:00", "Z")


def snapshot_fixture():
    observer = {"lat": 51.39309, "lon": 21.18876, "elevation_m": 194.0}
    intersection = (50.75, 20.55)
    altitude_m = 10000.0
    angular = angular_position_from_observer(
        (observer["lat"], observer["lon"]), observer["elevation_m"],
        intersection, altitude_m)
    body = visualizer.body_position_at_utc("MOON", T0, observer)
    base = T0 - datetime.timedelta(seconds=10)
    prediction = {
        "body": "MOON",
        "prediction_base_utc": utc_text(base),
        "predicted_transit_utc": utc_text(T0),
        "time2x_seconds": 10.0,
        "body_altitude_deg": body.altitude_deg,
        "body_azimuth_deg": body.azimuth_deg,
        "body_angular_diameter_arcsec": 1800.0,
        "separation_deg": abs(angular.altitude_angle_deg - body.altitude_deg),
        "intersection": {
            "lat": intersection[0], "lon": intersection[1],
            "azimuth_from_observer_deg": angular.azimuth_deg,
            "aircraft_altitude_deg": angular.altitude_angle_deg,
            "body_azimuth_deg": body.azimuth_deg,
            "body_altitude_deg": body.altitude_deg,
            "signed_vertical_offset_deg": angular.altitude_angle_deg - body.altitude_deg,
        },
        "frozen_prediction_state": {
            "horizontal": {
                "forward_bearing_at_t0_deg": 90.0,
                "effective_groundspeed_kmh": 720.0,
                "earth_radius_km": 6371.0,
            },
            "vertical": {
                "application_qnh_hpa": 1013.0,
                "current_altitude": {
                    "value_m": altitude_m, "timestamp_utc": utc_text(base),
                    "source": "adsb",
                },
                "latest_vertical_rate": {
                    "value_fpm": 0.0, "timestamp_utc": utc_text(base),
                    "source": "adsb",
                },
                "vertical_rate_history": [],
                "selected_altitude": None,
                "nav_qnh": None,
                "evaluated_at_utc": utc_text(base),
                "policy": {
                    "level_threshold_fpm": 300.0,
                    "valid_vr_age_seconds": 2.0,
                    "ignore_vr_age_seconds": 5.0,
                    "altitude_max_age_seconds": 10.0,
                    "stability_sample_count": 3,
                    "max_spread_fpm": 256.0,
                    "prediction_limit_seconds": 120.0,
                    "selected_altitude_freshness_seconds": 10.0,
                    "nav_qnh_freshness_seconds": 10.0,
                    "qnh_correction_ft_per_hpa": 26.0,
                },
                "decision": {
                    "mode": "LEVEL", "reason": "below_dynamic_threshold",
                    "applied_seconds_at_t0": 0.0,
                    "predicted_altitude_before_clamp_m": altitude_m,
                    "predicted_altitude_m": altitude_m,
                    "target_altitude_m": None, "intent_clamped": False,
                    "intent_reason": "TC29_2E_NOT_DYNAMIC", "spread_fpm": None,
                },
            },
            "astronomy": {
                "provider": "PyEphem",
                "provider_version": str(ephem_version()),
                "altitude_deg": body.altitude_deg,
                "azimuth_deg": body.azimuth_deg,
            },
        },
    }
    trigger = copy.deepcopy(prediction)
    update = copy.deepcopy(prediction)
    return {
        "schema_version": 3,
        "aircraft": {"icao": "ABC123", "callsign": "TEST1"},
        "body": "MOON", "observer": observer,
        "trigger_prediction": trigger,
        "prediction_updates": [copy.deepcopy(trigger), update],
    }


def ephem_version():
    import ephem
    return getattr(ephem, "__version__", getattr(ephem, "version", "unknown"))


class SnapshotVisualizerTests(unittest.TestCase):
    def test_schema_v3_is_accepted_and_legacy_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot_fixture()), encoding="utf-8")
            self.assertEqual(3, visualizer.load_snapshot(path)["schema_version"])
            path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
            with self.assertRaisesRegex(visualizer.VisualizerError,
                                        "schema_version >= 3"):
                visualizer.load_snapshot(path)

    def test_prediction_selection(self):
        document = snapshot_fixture()
        trigger, trigger_label = visualizer.select_prediction(document, "trigger")
        final, final_label = visualizer.select_prediction(document, "final")
        update, update_label = visualizer.select_prediction(document, "update:0")
        self.assertIs(trigger, document["trigger_prediction"])
        self.assertIs(final, document["prediction_updates"][-1])
        self.assertIs(update, document["prediction_updates"][0])
        self.assertEqual((trigger_label, final_label, update_label),
                         ("trigger", "final", "update:0"))
        with self.assertRaises(visualizer.VisualizerError):
            visualizer.select_prediction(document, "update:99")

    def test_sample_grid_is_arbitrary_and_contains_exact_t0(self):
        offsets = visualizer.sample_offsets(2.3, 4.7, 0.6)
        self.assertIn(0.0, offsets)
        self.assertEqual(-2.3, offsets[0])
        self.assertEqual(4.7, offsets[-1])

    def test_tangent_plane_signs_and_azimuth_wrap(self):
        right, level = visualizer.tangent_plane_offset(11, 20, 10, 20)
        left_wrap, _ = visualizer.tangent_plane_offset(359, 20, 0, 20)
        _, up = visualizer.tangent_plane_offset(10, 21, 10, 20)
        self.assertGreater(right, 0)
        self.assertLess(left_wrap, 0)
        self.assertGreater(up, 0)
        self.assertLess(abs(level), 0.01)

    def test_body_radius_and_sampled_minimum(self):
        prediction = snapshot_fixture()["trigger_prediction"]
        self.assertEqual(0.25, visualizer.body_radius_deg(prediction))
        samples = (
            visualizer.TrajectorySample(0, T0, 0, 0, 0, 0, 0, 0, 0,
                                        0.3, 0.4, 0.5),
            visualizer.TrajectorySample(1, T0, 0, 0, 0, 0, 0, 0, 0,
                                        0.1, 0.1, math.sqrt(0.02)),
        )
        closest, ratio, crossing = visualizer.trajectory_summary(samples, 0.25)
        self.assertEqual(1, closest.offset_seconds)
        self.assertAlmostEqual(math.sqrt(0.02) / 0.25, ratio)
        self.assertTrue(crossing)

    def test_zoom_limits_are_derived_from_body_radius(self):
        self.assertEqual(
            ((-0.75, 0.75), (-0.75, 0.75)),
            visualizer.zoom_plot_limits(0.25, 3.0))
        self.assertIsNone(visualizer.zoom_plot_limits(0.25, None))

    def test_invalid_zoom_is_rejected(self):
        for zoom in (0.0, 0.999, float("inf"), float("nan")):
            with self.subTest(zoom=zoom), self.assertRaisesRegex(
                    visualizer.VisualizerError, "zoom must"):
                visualizer.zoom_plot_limits(0.25, zoom)

    def test_shared_horizontal_and_vertical_reconstruction_and_t0_validation(self):
        document = snapshot_fixture()
        prediction = document["trigger_prediction"]
        samples = visualizer.reconstruct_samples(document, prediction, 1, 1, 0.7)
        t0 = visualizer.validate_t0(document, prediction, samples)
        self.assertEqual(prediction["intersection"]["lat"], t0.latitude_deg)
        self.assertEqual(prediction["intersection"]["lon"], t0.longitude_deg)
        self.assertEqual(10000.0, t0.altitude_m)
        broken = copy.deepcopy(prediction)
        broken["intersection"]["lat"] += 0.01
        with self.assertRaisesRegex(visualizer.VisualizerError,
                                    "T0 validation failed"):
            visualizer.validate_t0(document, broken, samples)

    def test_non_empty_png_generation_with_agg_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "snapshot.json"
            output_path = Path(directory) / "plot.png"
            snapshot_path.write_text(json.dumps(snapshot_fixture()), encoding="utf-8")
            result = visualizer.visualize(
                snapshot_path, output_path, "trigger", 1, 1, 0.5)
            self.assertTrue(output_path.exists())
            self.assertGreater(output_path.stat().st_size, 1000)
            self.assertEqual(output_path, result.output_path)

    def test_zoom_changes_only_plot_limits_not_computed_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "snapshot.json"
            full_path = Path(directory) / "full.png"
            zoom_path = Path(directory) / "zoom.png"
            snapshot_path.write_text(json.dumps(snapshot_fixture()), encoding="utf-8")
            full = visualizer.visualize(
                snapshot_path, full_path, "final", 2.3, 4.7, 0.6)
            zoomed = visualizer.visualize(
                snapshot_path, zoom_path, "final", 2.3, 4.7, 0.6, zoom=3)
            self.assertEqual(full.sample_count, zoomed.sample_count)
            self.assertEqual(full.minimum_separation_deg,
                             zoomed.minimum_separation_deg)
            self.assertEqual(full.minimum_ratio, zoomed.minimum_ratio)
            self.assertEqual(full.closest_offset_seconds,
                             zoomed.closest_offset_seconds)
            self.assertEqual(full.closest_utc, zoomed.closest_utc)
            self.assertEqual(full.disk_crossing, zoomed.disk_crossing)
            self.assertGreater(full_path.stat().st_size, 1000)
            self.assertGreater(zoom_path.stat().st_size, 1000)

    def test_invalid_cli_arguments_exit_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot_fixture()), encoding="utf-8")
            with self.assertRaises(SystemExit):
                visualizer.main([str(snapshot_path), "--step", "0",
                                 "--output", str(Path(directory) / "x.png")])


if __name__ == "__main__":
    unittest.main()
