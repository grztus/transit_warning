import datetime
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import transit_warning as transit
from transit_snapshot import (
    BUFFER_MAXLEN,
    PREDICTION_UPDATE_MAXLEN,
    RECENT_EVENT_TTL_SECONDS,
    SCHEMA_VERSION,
    TransitSnapshotManager,
    runtime_git_commit,
)


UTC = datetime.timezone.utc
BASE = datetime.datetime(2026, 8, 21, 18, 43, 22, tzinfo=UTC)


def observation(offset, icao="ABC123", lat=51.0, optional=True):
    timestamp = BASE + datetime.timedelta(seconds=offset)
    return {
        "timestamp_utc": timestamp,
        "icao": icao,
        "lat": lat,
        "lon": 21.0 if optional else None,
        "altitude_m": 10000 if optional else None,
        "groundspeed": 450 if optional else None,
        "track": 180 if optional else None,
        "vertical_rate_fpm": 0 if optional else None,
        "selected_altitude_ft": None,
        "message_source": "MSG",
        "message_type": "MSG,3",
        "parameter_sources": {"position": "adsb"},
        "source_timestamps_utc": {"position": timestamp},
    }


def prediction(separation=0.4, icao="ABC123", body="SUN",
               recorded_offset=-2, callsign="TEST123", time_shift=0,
               time2x=None):
    predicted = BASE + datetime.timedelta(seconds=time_shift)
    if time2x is None:
        time2x = (predicted - (
            BASE + datetime.timedelta(seconds=recorded_offset))).total_seconds()
    solver_input = {
        "aircraft_lat": 51.1, "aircraft_lon": 21.1,
        "aircraft_altitude_m": 10000.0,
        "aircraft_distance_km": 20.0,
        "aircraft_azimuth_deg": 119.0,
        "aircraft_altitude_angle_deg": 25.0,
        "groundspeed": 450.0, "track": 180.0,
        "vertical_rate": 640.0, "selected_altitude": 33000.0,
        "position_source": "adsb", "altitude_source": "adsb",
        "position_timestamp_utc": "2026-08-21T18:43:20Z",
        "altitude_timestamp_utc": "2026-08-21T18:43:20Z",
        "track_timestamp_utc": "2026-08-21T18:43:20Z",
        "groundspeed_timestamp_utc": "2026-08-21T18:43:20Z",
    }
    return {
        "recorded_at_utc": BASE + datetime.timedelta(seconds=recorded_offset),
        "predicted_transit_utc": predicted,
        "icao": icao,
        "callsign": callsign,
        "body": body,
        "observer": {"lat": 51.0, "lon": 21.0, "elevation_m": 200},
        "time2x_seconds": time2x,
        "separation_deg": separation,
        "predicted_aircraft_elevation_deg": 30.1,
        "body_altitude_deg": 30.0,
        "body_azimuth_deg": 120.0,
        "h2x_km": 20.0,
        "p2x_km": 1.0,
        "groundspeed": 450.0,
        "track": 180.0,
        "vertical_prediction": {"mode": "DYNAMIC_VALID"},
        "solver_input": solver_input,
        "intersection": {
            "lat": 51.2, "lon": 21.2,
            "azimuth_from_observer_deg": 120.0,
            "aircraft_altitude_deg": 30.0 - separation,
            "body_azimuth_deg": 120.0,
            "body_altitude_deg": 30.0,
            "signed_vertical_offset_deg": -separation,
        },
        "body_angular_diameter_arcsec": 1900.0,
    }


class TransitSnapshotManagerTests(unittest.TestCase):
    def manager(self, directory):
        return TransitSnapshotManager(
            directory, sep_threshold_deg=0.5, arm_seconds=15,
            finalize_grace_seconds=2, git_commit="abc123")

    def load_only_json(self, directory):
        paths = list(Path(directory).rglob("*.json"))
        self.assertEqual(len(paths), 1)
        return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))

    def test_arm_requires_strict_sep_and_positive_time_within_15_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            self.assertFalse(manager.consider_prediction(
                prediction(0.5, time2x=10)))
            self.assertFalse(manager.consider_prediction(
                prediction(0.4, time2x=15.001)))
            self.assertFalse(manager.consider_prediction(
                prediction(0.4, time2x=0)))
            self.assertTrue(manager.consider_prediction(
                prediction(0.499, time2x=15)))

    def test_reference_drift_moves_window_and_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            for offset in (-4, -2, 3, 6, 7, 8):
                manager.record_observation(observation(offset))
            manager.consider_prediction(prediction(time2x=2))
            manager.consider_prediction(prediction(
                separation=0.3, recorded_offset=-1, time_shift=2,
                time2x=3))
            event = manager.active_events[("ABC123", "SUN")]
            self.assertEqual(event["initial_predicted_transit_utc"], BASE)
            self.assertEqual(event["reference_transit_utc"],
                             BASE + datetime.timedelta(seconds=2))
            self.assertEqual(manager.finalize_due(
                BASE + datetime.timedelta(seconds=8.999)), [])
            self.assertEqual(len(manager.finalize_due(
                BASE + datetime.timedelta(seconds=9))), 1)
            _, document = self.load_only_json(directory)
            self.assertEqual(document["initial_predicted_transit_utc"],
                             "2026-08-21T18:43:22Z")
            self.assertEqual(document["final_reference_transit_utc"],
                             "2026-08-21T18:43:24Z")
            self.assertEqual(document["schema_version"], SCHEMA_VERSION)
            self.assertIn("solver_input", document["trigger_prediction"])
            self.assertIn("intersection", document["trigger_prediction"])
            self.assertEqual(
                [item["timestamp_utc"] for item in document["observations"]],
                ["2026-08-21T18:43:20Z", "2026-08-21T18:43:25Z",
                 "2026-08-21T18:43:28Z", "2026-08-21T18:43:29Z"])

    def test_active_event_accepts_reference_update_beyond_arm_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.consider_prediction(prediction(time2x=2))
            manager.consider_prediction(prediction(
                separation=0.8, recorded_offset=1, time_shift=17,
                time2x=16))
            event = manager.active_events[("ABC123", "SUN")]
            self.assertEqual(event["reference_transit_utc"],
                             BASE + datetime.timedelta(seconds=17))

    def test_grace_accepts_late_out_of_order_sample_and_sorts_output(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.record_observation(observation(4.9))
            manager.record_observation(observation(-4.9, lat=51.1))
            manager.consider_prediction(prediction(time2x=2))
            self.assertEqual(manager.finalize_due(
                BASE + datetime.timedelta(seconds=6.999)), [])
            manager.record_observation(observation(0, lat=99.0))
            manager.finalize_due(BASE + datetime.timedelta(seconds=7))
            _, document = self.load_only_json(directory)
            timestamps = [item["timestamp_utc"]
                          for item in document["observations"]]
            self.assertEqual(timestamps, sorted(timestamps))
            self.assertIn(99.0, [item["lat"]
                                 for item in document["observations"]])

    def test_out_of_order_input_does_not_remove_newer_buffer_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.record_observation(observation(20))
            manager.record_observation(observation(19))
            manager.record_observation(observation(-20))
            self.assertEqual(
                [sample["timestamp_utc"] for sample
                 in manager._buffers["ABC123"]],
                [BASE + datetime.timedelta(seconds=20),
                 BASE + datetime.timedelta(seconds=19)])

    def test_all_samples_optional_nulls_and_bodies_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.consider_prediction(prediction(body="SUN", time2x=2))
            manager.consider_prediction(prediction(body="MOON", time2x=2))
            for offset in (0.1, 0.2, 0.3):
                manager.record_observation(
                    observation(offset, optional=offset != 0.1))
            self.assertEqual(len(manager.active_events), 2)
            manager.finalize_due(BASE + datetime.timedelta(seconds=7))
            documents = [json.loads(path.read_text(encoding="utf-8"))
                         for path in Path(directory).rglob("*.json")]
            self.assertEqual({item["body"] for item in documents},
                             {"SUN", "MOON"})
            self.assertTrue(any(item["altitude_m"] is None
                                for item in documents[0]["observations"]))

    def test_source_duplicates_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            duplicate = observation(0.25)
            manager.record_observation(duplicate)
            manager.record_observation(duplicate)
            manager.consider_prediction(prediction(time2x=2))
            manager.finalize_due(BASE + datetime.timedelta(seconds=7))
            _, document = self.load_only_json(directory)
            self.assertEqual(len(document["observations"]), 2)

    def test_prediction_updates_use_tolerance_and_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.consider_prediction(prediction(time2x=2))
            tiny = prediction(recorded_offset=-1.9, time2x=2)
            tiny["separation_deg"] = 0.4000000001
            manager.consider_prediction(tiny)
            event = manager.active_events[("ABC123", "SUN")]
            self.assertEqual(len(event["prediction_updates"]), 1)
            for index in range(PREDICTION_UPDATE_MAXLEN + 20):
                changed = prediction(recorded_offset=-1, time2x=2)
                changed["separation_deg"] = 0.2 + index / 100000.0
                manager.consider_prediction(changed)
            event = manager.active_events[("ABC123", "SUN")]
            self.assertEqual(len(event["prediction_updates"]),
                             PREDICTION_UPDATE_MAXLEN)
            self.assertEqual(event["trigger_prediction"]["separation_deg"],
                             0.4)
            self.assertIn("solver_input", event["prediction_updates"][-1])
            self.assertIn("intersection", event["prediction_updates"][-1])

    def test_legacy_prediction_without_geometry_remains_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            legacy = prediction(time2x=2)
            legacy.pop("solver_input")
            legacy.pop("intersection")
            legacy.pop("body_angular_diameter_arcsec")
            self.assertTrue(manager.consider_prediction(legacy))
            manager.finalize_due(BASE + datetime.timedelta(seconds=7))
            _, document = self.load_only_json(directory)
            self.assertNotIn("solver_input", document["trigger_prediction"])

    def test_recent_event_blocks_duplicate_then_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.consider_prediction(prediction(time2x=2))
            manager.finalize_due(BASE + datetime.timedelta(seconds=7))
            duplicate = prediction(recorded_offset=8, time_shift=10, time2x=2)
            self.assertFalse(manager.consider_prediction(duplicate))
            manager.cleanup(BASE + datetime.timedelta(
                seconds=7 + RECENT_EVENT_TTL_SECONDS + 0.001))
            self.assertEqual(manager._recent_events, {})
            later = prediction(recorded_offset=68, time_shift=70, time2x=2)
            self.assertTrue(manager.consider_prediction(later))

    def test_stale_tc29_only_buffers_and_per_icao_size_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            for index in range(BUFFER_MAXLEN + 20):
                manager.record_observation(
                    observation(index / 100.0, icao="TC29ONLY"))
            self.assertEqual(len(manager._buffers["TC29ONLY"]), BUFFER_MAXLEN)
            manager.cleanup(BASE + datetime.timedelta(seconds=70))
            self.assertNotIn("TC29ONLY", manager._buffers)
            self.assertNotIn("TC29ONLY", manager._buffer_last_seen)

    def test_unique_filenames_include_icao_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            for icao in ("AAA111", "BBB222"):
                manager.consider_prediction(prediction(
                    icao=icao, callsign="SAME", time2x=2))
            manager.finalize_due(BASE + datetime.timedelta(seconds=7))
            paths = list(Path(directory).rglob("*.json"))
            self.assertEqual(len(paths), 2)
            self.assertTrue(any("AAA111" in path.name for path in paths))
            self.assertTrue(any("BBB222" in path.name for path in paths))
            previous = {path.name: path.read_bytes() for path in paths}
            document = json.loads(paths[0].read_text(encoding="utf-8"))
            manager._write_document(document, BASE)
            new_paths = list(Path(directory).rglob("*.json"))
            self.assertEqual(len(new_paths), 3)
            for name, content in previous.items():
                self.assertEqual((paths[0].parent / name).read_bytes(), content)

    def test_shutdown_writes_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.record_observation(observation(-1))
            manager.consider_prediction(prediction(time2x=2))
            self.assertEqual(len(manager.close(BASE)), 1)
            _, document = self.load_only_json(directory)
            self.assertFalse(document["complete"])
            self.assertEqual(document["finalization_reason"], "shutdown")
            self.assertEqual(manager.active_events, {})

    def test_normal_snapshot_is_complete_and_missing_git_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.consider_prediction(prediction(callsign="", time2x=2))
            manager.finalize_due(BASE + datetime.timedelta(seconds=7))
            _, document = self.load_only_json(directory)
            self.assertTrue(document["complete"])
            self.assertEqual(document["finalization_reason"], "normal")
            self.assertIsNone(document["aircraft"]["callsign"])
        with patch("transit_snapshot.subprocess.run", side_effect=OSError):
            self.assertEqual(runtime_git_commit(), "unknown")

    def test_write_failure_is_silent_fail_open(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.consider_prediction(prediction(time2x=2))
            output = io.StringIO()
            with patch.object(Path, "mkdir", side_effect=OSError("denied")), \
                    patch("sys.stdout", output):
                self.assertEqual(manager.finalize_due(
                    BASE + datetime.timedelta(seconds=7)), [])
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(manager.last_error, "denied")

    def test_file_io_runs_without_manager_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.consider_prediction(prediction(time2x=2))
            acquired = []

            def inspect_lock(document, finalized_at):
                def worker():
                    locked = manager._lock.acquire(blocking=False)
                    acquired.append(locked)
                    if locked:
                        manager._lock.release()
                thread = threading.Thread(target=worker)
                thread.start()
                thread.join()
                return Path(directory) / "not-written.json"

            with patch.object(manager, "_write_document",
                              side_effect=inspect_lock):
                manager.finalize_due(BASE + datetime.timedelta(seconds=7))
            self.assertEqual(acquired, [True])


class TransitSnapshotIntegrationFailOpenTests(unittest.TestCase):
    def setUp(self):
        self.previous_manager = transit.transit_snapshot_manager

    def tearDown(self):
        transit.transit_snapshot_manager = self.previous_manager

    def test_observation_and_prediction_preparation_fail_open(self):
        transit.transit_snapshot_manager = Mock()
        with patch.object(transit, "_capture_transit_observation",
                          side_effect=RuntimeError("observation")):
            transit.capture_transit_observation("ABC123", BASE, "MSG", "MSG,3")
        with patch.object(transit, "_capture_transit_prediction",
                          side_effect=RuntimeError("prediction")):
            transit.capture_transit_prediction(
                "ABC123", "TEST", "sun", (), BASE, {})

    def test_prediction_capture_uses_exact_solver_result_and_input(self):
        manager = Mock()
        transit.transit_snapshot_manager = manager
        solver_input = prediction()["solver_input"]
        result = (
            51.234567, 21.765432, 123.45, 29.25,
            17.9, 2.1, 5.0, 0, 123.4, 30.0, BASE)
        transit.sun_predicted_transit_utc["ABC123"] = (
            BASE + datetime.timedelta(seconds=5))
        transit.transit_solver_diagnostics[("ABC123", "sun")] = (
            transit.MovingBodyTransitDiagnostic(
                body="sun", prediction_base_utc=BASE,
                initial_time2x=5.1, final_time2x=5.0,
                correction_count=1, convergence_residual=0.1,
                outcome=transit.TransitSolverOutcome.CONVERGED,
                final_separation=0.75,
                body_angular_diameter_arcsec=1895.5))
        try:
            transit.capture_transit_prediction(
                "ABC123", "TEST", "sun", result, BASE, solver_input)
            captured = manager.consider_prediction.call_args.args[0]
        finally:
            transit.sun_predicted_transit_utc.pop("ABC123", None)
            transit.transit_solver_diagnostics.pop(("ABC123", "sun"), None)
        self.assertEqual(captured["solver_input"], solver_input)
        self.assertEqual(captured["intersection"], {
            "lat": 51.234567, "lon": 21.765432,
            "azimuth_from_observer_deg": 123.45,
            "aircraft_altitude_deg": 29.25,
            "body_azimuth_deg": 123.4,
            "body_altitude_deg": 30.0,
            "signed_vertical_offset_deg": -0.75,
        })
        self.assertEqual(captured["separation_deg"], 0.75)
        self.assertEqual(captured["body_angular_diameter_arcsec"], 1895.5)

    def test_tc29_capture_failure_does_not_escape(self):
        manager = Mock()
        manager.record_observation.side_effect = RuntimeError("tc29")
        transit.transit_snapshot_manager = manager
        intent = SimpleNamespace(
            icao="ABC123", selected_altitude_ft=30000,
            selected_altitude_source="MCP", nav_qnh_hpa=1013.2)
        try:
            transit.update_aircraft_intent(intent, BASE)
        finally:
            transit.aircraft_intent_states.pop("ABC123", None)

    def test_finalize_and_cleanup_wrappers_remain_fail_open(self):
        manager = Mock()
        manager.finalize_due.side_effect = RuntimeError("finalize")
        manager.drop_aircraft_buffer.side_effect = RuntimeError("cleanup")
        manager.close.side_effect = RuntimeError("close")
        transit.transit_snapshot_manager = manager
        self.assertEqual(transit.finalize_transit_snapshots(BASE), [])
        self.assertFalse(transit.drop_transit_snapshot_buffer("ABC123"))
        self.assertEqual(transit.close_transit_snapshots(BASE), [])

    def test_initialization_failure_disables_snapshot_layer(self):
        with patch.object(transit, "TransitSnapshotManager",
                          side_effect=RuntimeError("init")):
            self.assertIsNone(transit.initialize_transit_snapshots())
        self.assertIsNone(transit.transit_snapshot_manager)


if __name__ == "__main__":
    unittest.main()
