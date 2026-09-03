"""Candidate Auto-Recorder Phase 3B.2 FULL-reference tests."""

import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import candidate_recorder
import transit_warning as transit
from authoritative_transit import AuthoritativeTransition, AuthoritativeTransitionKind
from candidate_recorder import (
    CandidateBundleStore,
    CandidateEncounterManager,
    CandidatePreBuffer,
    FullRecorderReference,
)
from observer_position import ObserverContext, ObserverPosition
from recording import SessionRecorder
from tests.test_candidate_recorder_phase2 import NOW, transition


class CandidateFullReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "recordings" / "candidates"
        self.session_directory = base / "recordings" / "sessions" / "full"
        self.session_directory.mkdir(parents=True)
        self.prebuffer = CandidatePreBuffer(clock=lambda: NOW)
        self.manager = CandidateEncounterManager(self.prebuffer)
        self.reference = FullRecorderReference(
            session_id="20260903_115500",
            session_directory=self.session_directory,
            session_start_utc=NOW - datetime.timedelta(minutes=5),
            streams={
                "adsb_sbs": {"file": "adsb_30003.log", "status": "recording"},
                "mlat_sbs": {"file": "mlat_30106.log", "status": "recording"},
                "raw_adsb": {"file": "raw_30002.log", "status": "recording"},
            },
        )
        self.store = CandidateBundleStore(
            self.root, self.prebuffer,
            full_recorder_reference=self.reference)
        self.context = ObserverContext(
            ObserverPosition(50.123, 20.456, 123.0), "MOBILE",
            "MOBILE_FRESH", mobile_age_seconds=1.0,
            mobile_accuracy_m=4.0, epoch=4)

    def tearDown(self):
        self.store.close_incomplete(NOW)
        self.temporary.cleanup()

    def open(self, **kwargs):
        state = self.manager.process_transition(transition(**kwargs), NOW)
        self.assertIsNotNone(state)
        self.assertTrue(self.store.observe_encounter(
            state, self.manager.capture_state(state.icao), self.context))
        return state

    def manifests(self):
        return list(self.root.rglob("full_references/*/manifest.json"))

    def read_manifest(self, encounter_id):
        for path in self.manifests():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload["encounter_id"] == encounter_id:
                return path, payload
        self.fail("missing FULL-reference marker for {}".format(encounter_id))

    def test_full_inactive_retains_phase_3b1_physical_capture(self):
        physical = CandidateBundleStore(self.root / "physical", self.prebuffer)
        state = self.manager.process_transition(transition(), NOW)
        self.assertTrue(physical.observe_encounter(
            state, self.manager.capture_state(state.icao), self.context))
        self.assertEqual(1, len(list(
            (self.root / "physical").rglob("captures/*"))))
        physical.close_incomplete(NOW)

    def test_full_active_creates_marker_without_stream_files(self):
        self.prebuffer.feed_adsb_sbs(
            "MSG,3,1,1,ABC123,1,2026/09/03,12:00:00.000,"
            "2026/09/03,12:00:00.000,,10000,,,51.0,21.0\n", NOW)
        state = self.open()
        path, payload = self.read_manifest(state.encounter_id)

        self.assertEqual("FULL_REFERENCE", payload["storage_mode"])
        self.assertEqual(state.encounter_id, payload["encounter_id"])
        self.assertEqual("TRUE_2D", payload["prediction_geometry"])
        self.assertEqual("20260903_115500", payload["full_session"]["session_id"])
        self.assertEqual(
            {"adsb_sbs", "mlat_sbs", "raw_adsb"},
            set(payload["full_session"]["streams"]))
        self.assertEqual(
            state.latest_prediction.separation_deg,
            payload["latest_prediction"]["separation_deg"])
        self.assertEqual(
            state.latest_prediction.predicted_transit_utc.isoformat().replace(
                "+00:00", "Z"),
            payload["latest_prediction"]["predicted_transit_utc"])
        self.assertEqual(
            self.session_directory.resolve(),
            (path.parent / payload["full_session"]["relative_path"]).resolve())
        self.assertEqual(
            state.prebuffer_start_utc.isoformat().replace("+00:00", "Z"),
            payload["required_window"]["start_utc"])
        self.assertEqual(
            state.required_end_time_utc.isoformat().replace("+00:00", "Z"),
            payload["required_window"]["applied_end_utc"])
        self.assertFalse(list(self.root.rglob("captures")))
        self.assertFalse(list(self.root.rglob("*.log")))
        self.assertFalse(list(self.root.rglob("*.bin")))

    def test_withdrawn_updates_marker_without_removal(self):
        active = self.open()
        withdrawn = self.manager.process_transition(AuthoritativeTransition(
            AuthoritativeTransitionKind.WITHDRAWN,
            active.latest_prediction), NOW + datetime.timedelta(seconds=1))
        self.assertTrue(self.store.observe_encounter(
            withdrawn, self.manager.capture_state(withdrawn.icao), self.context))

        _, payload = self.read_manifest(active.encounter_id)
        self.assertEqual("WITHDRAWN", payload["outcome"])
        self.assertEqual("active", payload["marker_status"])
        self.assertEqual(1, len(self.manifests()))

        completed = self.manager.complete_due(active.required_end_time_utc)
        self.assertTrue(self.store.finalize_completed(completed, {}))
        _, payload = self.read_manifest(active.encounter_id)
        self.assertEqual("WITHDRAWN", payload["outcome"])
        self.assertEqual("complete", payload["marker_status"])
        self.assertIsNotNone(payload["completed_at_utc"])

    def test_generations_and_bodies_get_distinct_markers_for_same_session(self):
        first = self.open(generation=1, body="SUN")
        second = self.open(generation=2, body="SUN", seconds=121)
        moon = self.open(generation=1, body="MOON", seconds=122)

        self.assertEqual(3, len(self.manifests()))
        for state in (first, second, moon):
            _, payload = self.read_manifest(state.encounter_id)
            self.assertEqual(self.reference.session_id,
                             payload["full_session"]["session_id"])

    def test_mobile_coordinates_exist_only_inside_private_marker(self):
        state = self.open()
        path, payload = self.read_manifest(state.encounter_id)
        self.assertTrue(payload["private_forensic_data"])
        self.assertEqual(50.123,
                         payload["observer_context"]["latitude_deg"])
        self.assertEqual(20.456,
                         payload["observer_context"]["longitude_deg"])
        self.assertNotIn("50.123", str(path))
        self.assertNotIn("20.456", str(path))
        self.assertEqual(1, len(list(self.root.rglob("*.json"))))

    def test_marker_failure_is_fail_open_and_does_not_create_stream_data(self):
        state = self.manager.process_transition(transition(), NOW)
        with patch.object(candidate_recorder, "_atomic_json",
                          side_effect=OSError("denied")):
            self.assertFalse(self.store.observe_encounter(
                state, self.manager.capture_state(state.icao), self.context))
        self.assertIn("denied", self.store.last_error)
        self.store.mark_degraded((state.icao,), "storage_worker_failure")
        self.assertTrue(self.store.observe_encounter(
            state, self.manager.capture_state(state.icao), self.context))
        _, payload = self.read_manifest(state.encounter_id)
        self.assertTrue(payload["degraded"])
        self.assertIn("storage_worker_failure",
                      payload["degradation_reasons"])
        self.assertFalse(list(self.root.rglob("*.log")))
        self.assertFalse(list(self.root.rglob("*.bin")))

    def test_full_reference_is_deeply_immutable(self):
        with self.assertRaises(TypeError):
            self.reference.streams["adsb_sbs"]["status"] = "failed"


class FullRecorderDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.old_raw_port = transit.raw_adsb_port
        self.old_mlat_beast_enabled = transit.mlat_beast_enabled
        transit.raw_adsb_port = 30002
        transit.mlat_beast_enabled = False

    def tearDown(self):
        transit.raw_adsb_port = self.old_raw_port
        transit.mlat_beast_enabled = self.old_mlat_beast_enabled
        self.temporary.cleanup()

    def recorder(self):
        return SessionRecorder(
            NOW, 30003, 30106, "Europe/Warsaw",
            base_dir=Path(self.temporary.name) / "sessions", raw_port=30002)

    def test_active_full_recorder_is_detected_without_mutation(self):
        recorder = self.recorder()
        before = recorder.manifest_data()
        reference = transit.active_full_recorder_reference(recorder)

        self.assertIsNotNone(reference)
        self.assertEqual(recorder.session_id, reference.session_id)
        self.assertEqual(
            {"adsb_sbs", "mlat_sbs", "raw_adsb"}, set(reference.streams))
        self.assertFalse(recorder._closed)
        self.assertEqual(before, recorder.manifest_data())
        recorder.close(NOW)

    def test_partial_or_inactive_full_recorder_uses_physical_mode(self):
        recorder = self.recorder()
        recorder.raw_writer._fail("write", OSError("disk"))
        self.assertIsNone(transit.active_full_recorder_reference(recorder))
        recorder.close(NOW)
        self.assertIsNone(transit.active_full_recorder_reference(recorder))

    def test_full_reference_runtime_does_not_attach_stream_storage_sink(self):
        old = (
            transit.candidate_pre_buffer,
            transit.candidate_encounter_manager,
            transit.candidate_bundle_store,
            transit.candidate_storage_worker,
        )
        reference = FullRecorderReference(
            "full", Path(self.temporary.name) / "sessions" / "full", NOW,
            {"adsb_sbs": {"status": "recording"}})
        try:
            with patch.object(transit, "active_full_recorder_reference",
                              return_value=reference):
                transit.initialize_candidate_recorder_observation()
            self.assertIs(reference,
                          transit.candidate_bundle_store.full_recorder_reference)
            self.assertIsNone(transit.candidate_pre_buffer._record_sink)
        finally:
            if transit.candidate_storage_worker is not None:
                transit.candidate_storage_worker.close()
            (transit.candidate_pre_buffer,
             transit.candidate_encounter_manager,
             transit.candidate_bundle_store,
             transit.candidate_storage_worker) = old


if __name__ == "__main__":
    unittest.main()
