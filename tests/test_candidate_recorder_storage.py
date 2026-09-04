"""Candidate Auto-Recorder Phase 3B.1 private bundle tests."""

import datetime
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import candidate_recorder
from authoritative_transit import AuthoritativeTransition, AuthoritativeTransitionKind
from candidate_recorder import (
    CandidateBundleStore,
    CandidateEncounterManager,
    CandidatePreBuffer,
    CandidateStorageWorker,
    CandidateStreamRecord,
    StreamType,
)
from observer_position import ObserverContext, ObserverPosition
from tests.test_candidate_recorder import make_raw_df17
from tests.test_candidate_recorder_phase2 import NOW, prediction, transition


SBS = (
    "MSG,3,1,1,ABC123,1,2026/09/03,11:59:20.000,"
    "2026/09/03,11:59:20.000,,10000,,,51.0,21.0\n")
OTHER = SBS.replace("ABC123", "DEF456")


class CandidateBundleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "recordings" / "candidates"
        self.prebuffer = CandidatePreBuffer(
            buffer_duration_seconds=60, clock=lambda: NOW)
        self.manager = CandidateEncounterManager(self.prebuffer)
        self.store = CandidateBundleStore(self.root, self.prebuffer)
        self.prebuffer.set_record_sink(self.store.observe_record)
        self.context = ObserverContext(
            ObserverPosition(50.123, 20.456, 123.0), "MOBILE",
            "MOBILE_FRESH", mobile_age_seconds=1.0,
            mobile_accuracy_m=4.0, epoch=4)

    def tearDown(self):
        self.store.close_incomplete(NOW)
        self.temporary.cleanup()

    def open(self, **kwargs):
        item = transition(**kwargs)
        state = self.manager.process_transition(item, NOW)
        self.assertIsNotNone(state)
        self.assertTrue(self.store.observe_encounter(
            state, self.manager.capture_state(state.icao), self.context))
        return state

    def manifests(self):
        return list(self.root.rglob("encounters/*/manifest.json"))

    def capture_directories(self):
        return [item for item in self.root.rglob("captures/*")
                if item.is_dir()]

    def test_trigger_drains_only_target_icao_prebuffer_and_appends_live(self):
        too_old = SBS.replace("11:59:20.000", "11:58:40.000")
        self.prebuffer.feed_adsb_sbs(
            too_old, NOW - datetime.timedelta(seconds=80))
        old = NOW - datetime.timedelta(seconds=40)
        self.prebuffer.feed_adsb_sbs(SBS, old)
        self.prebuffer.feed_adsb_sbs(OTHER, old)
        self.open()
        live = SBS.replace("11:59:20.000", "12:00:01.000")
        self.prebuffer.feed_adsb_sbs(live, NOW + datetime.timedelta(seconds=1))

        capture = self.capture_directories()[0]
        content = (capture / "adsb_sbs.log").read_text(encoding="utf-8")
        self.assertEqual(SBS + live, content)
        self.assertNotIn("DEF456", content)
        self.assertNotIn("11:58:40.000", content)

    def test_prebuffer_drain_and_same_live_record_are_written_once(self):
        self.prebuffer.feed_adsb_sbs(SBS, NOW)
        record = self.prebuffer.get_records("ABC123")[0]
        self.open()
        self.assertFalse(self.store.observe_record(record))
        content = (self.capture_directories()[0] / "adsb_sbs.log").read_text()
        self.assertEqual(SBS, content)

    def test_distinct_identical_records_have_stable_sequences_and_both_survive(self):
        self.prebuffer.feed_adsb_sbs(SBS, NOW)
        self.prebuffer.feed_adsb_sbs(SBS, NOW)
        records = self.prebuffer.get_records("ABC123")
        self.assertEqual((1, 2), tuple(item.sequence_id for item in records))
        self.open()
        content = (self.capture_directories()[0] / "adsb_sbs.log").read_text()
        self.assertEqual(SBS + SBS, content)

    def test_each_stream_preserves_original_representation_and_timing(self):
        self.open()
        records = (
            CandidateStreamRecord(StreamType.MLAT_SBS, "ABC123", NOW, SBS),
            CandidateStreamRecord(StreamType.RAW_ADSB, "ABC123", NOW,
                                  "*8DABC12300112233445566778899;\n"),
            CandidateStreamRecord(StreamType.MLAT_BEAST, "ABC123", NOW,
                                  b"\x1a\x33\x00\xff"),
        )
        for record in records:
            self.assertTrue(self.store.observe_record(record))

        capture = self.capture_directories()[0]
        self.assertEqual(SBS.encode("utf-8"),
                         (capture / "mlat_sbs.log").read_bytes())
        self.assertEqual(b"*8DABC12300112233445566778899;\n",
                         (capture / "raw_adsb.log").read_bytes())
        self.assertEqual(b"\x1a\x33\x00\xff",
                         (capture / "mlat_beast.bin").read_bytes())
        for filename in ("mlat_sbs.log", "raw_adsb.log", "mlat_beast.bin"):
            timing = json.loads(
                (capture / (filename + ".timing.jsonl")).read_text())
            self.assertEqual(0, timing["offset"])
            self.assertEqual(NOW.isoformat().replace("+00:00", "Z"),
                             timing["received_at_utc"])

    def test_overlap_shares_one_physical_capture_and_separate_manifests(self):
        self.open(body="SUN", seconds=100)
        self.open(body="MOON", seconds=200)
        self.prebuffer.feed_adsb_sbs(SBS, NOW + datetime.timedelta(seconds=1))

        self.assertEqual(1, len(self.capture_directories()))
        self.assertEqual(2, len(self.manifests()))
        content = (self.capture_directories()[0] / "adsb_sbs.log").read_text()
        self.assertEqual(1, content.count("ABC123"))

    def test_cross_midnight_encounter_reference_resolves_to_shared_capture(self):
        before_midnight = datetime.datetime(2026, 9, 3, 23, 59, 50,
                                            tzinfo=datetime.timezone.utc)
        first_prediction = replace(
            prediction(body="SUN", generation=1),
            predicted_transit_utc=before_midnight + datetime.timedelta(seconds=60))
        first = self.manager.process_transition(AuthoritativeTransition(
            AuthoritativeTransitionKind.OPENED, first_prediction), before_midnight)
        self.store.observe_encounter(
            first, self.manager.capture_state("ABC123"), self.context)
        after_midnight = before_midnight + datetime.timedelta(seconds=20)
        second_prediction = replace(
            prediction(body="MOON", generation=1),
            predicted_transit_utc=after_midnight + datetime.timedelta(seconds=80))
        second = self.manager.process_transition(AuthoritativeTransition(
            AuthoritativeTransitionKind.OPENED, second_prediction), after_midnight)
        self.store.observe_encounter(
            second, self.manager.capture_state("ABC123"), self.context)

        moon_manifest_path = next(
            item for item in self.manifests() if "MOON" in str(item))
        payload = json.loads(moon_manifest_path.read_text())
        referenced = (moon_manifest_path.parent
                      / payload["physical_capture"]["relative_path"]).resolve()
        self.assertEqual(self.capture_directories()[0].resolve(), referenced)

    def test_rapid_generation_shares_capture_and_extends_coverage(self):
        first = self.open(generation=1, seconds=100)
        second = self.open(generation=2, seconds=200)
        capture = self.manager.capture_state("ABC123")

        self.assertEqual(2, len(capture.encounter_ids))
        self.assertEqual(second.required_end_time_utc, capture.capture_until_utc)
        self.assertGreater(capture.capture_until_utc, first.required_end_time_utc)
        self.assertEqual(1, len(self.capture_directories()))

    def test_withdrawal_does_not_stop_capture(self):
        active = self.open(seconds=20)
        withdrawn_transition = AuthoritativeTransition(
            AuthoritativeTransitionKind.WITHDRAWN, active.latest_prediction)
        withdrawn = self.manager.process_transition(
            withdrawn_transition, NOW + datetime.timedelta(seconds=1))
        self.store.observe_encounter(
            withdrawn, self.manager.capture_state("ABC123"), self.context)
        self.prebuffer.feed_adsb_sbs(SBS, NOW + datetime.timedelta(seconds=2))

        payload = json.loads(self.manifests()[0].read_text())
        self.assertEqual("WITHDRAWN", payload["outcome"])
        self.assertTrue((self.capture_directories()[0] / "adsb_sbs.log").exists())

    def test_later_t0_extends_and_earlier_t0_does_not_shorten_manifest(self):
        self.open(seconds=100)
        later = self.manager.process_transition(transition(
            AuthoritativeTransitionKind.UPDATED, seconds=200),
            NOW + datetime.timedelta(seconds=1))
        self.store.observe_encounter(
            later, self.manager.capture_state("ABC123"), self.context)
        earlier = self.manager.process_transition(transition(
            AuthoritativeTransitionKind.UPDATED, seconds=20),
            NOW + datetime.timedelta(seconds=2))
        self.store.observe_encounter(
            earlier, self.manager.capture_state("ABC123"), self.context)

        payload = json.loads(self.manifests()[0].read_text())
        self.assertEqual(
            (NOW + datetime.timedelta(seconds=380)).isoformat().replace("+00:00", "Z"),
            payload["required_window"]["applied_end_utc"])

    def test_finalization_is_atomic_and_counts_are_consistent(self):
        state = self.open(seconds=10)
        self.prebuffer.feed_adsb_sbs(SBS, NOW + datetime.timedelta(seconds=1))
        completed = self.manager.complete_due(state.required_end_time_utc)
        self.assertTrue(self.store.finalize_completed(
            completed, {"ABC123": self.manager.capture_state("ABC123")}))

        capture = self.capture_directories()[0]
        capture_manifest = json.loads(
            (capture / "capture_manifest.json").read_text())
        stream = capture_manifest["streams"]["adsb_sbs"]
        self.assertEqual("complete", capture_manifest["status"])
        self.assertEqual(1, stream["record_count"])
        self.assertEqual((capture / "adsb_sbs.log").stat().st_size,
                         stream["byte_count"])
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_completed_encounter_late_transition_cannot_reopen_capture(self):
        state = self.open(seconds=10)
        first_record_time = NOW + datetime.timedelta(seconds=1)
        self.prebuffer.feed_adsb_sbs(SBS, first_record_time)
        completed_at = state.required_end_time_utc
        completed = self.manager.complete_due(completed_at)
        self.assertTrue(self.store.finalize_completed(
            completed, {"ABC123": self.manager.capture_state("ABC123")}))
        capture = self.capture_directories()[0]
        original_bytes = (capture / "adsb_sbs.log").read_bytes()

        late_now = completed_at + datetime.timedelta(minutes=10)
        late = self.manager.process_transition(transition(
            AuthoritativeTransitionKind.UPDATED, seconds=10), late_now)
        self.assertIsNotNone(late.completed_at_utc)
        self.assertTrue(self.store.observe_encounter(
            late, self.manager.capture_state("ABC123"), self.context,
            late_now))
        withdrawn_now = late_now + datetime.timedelta(seconds=1)
        withdrawn = self.manager.process_transition(AuthoritativeTransition(
            AuthoritativeTransitionKind.WITHDRAWN,
            late.latest_prediction), withdrawn_now)
        self.assertEqual(withdrawn_now, withdrawn.last_update_utc)
        self.assertTrue(self.store.observe_encounter(
            withdrawn, self.manager.capture_state("ABC123"), self.context,
            withdrawn_now))
        late_line = SBS.replace("11:59:20.000", "12:10:00.000")
        self.prebuffer.feed_adsb_sbs(late_line, withdrawn_now)

        self.assertEqual(1, len(self.capture_directories()))
        self.assertFalse(any(
            item.name.endswith("_01") for item in self.capture_directories()))
        self.assertEqual(original_bytes,
                         (capture / "adsb_sbs.log").read_bytes())
        payload = json.loads(self.manifests()[0].read_text())
        self.assertEqual("WITHDRAWN", payload["outcome"])
        self.assertEqual(
            withdrawn_now.isoformat().replace("+00:00", "Z"),
            payload["last_update_utc"])

    def test_new_generation_after_completion_starts_legitimate_capture(self):
        first = self.open(generation=1, seconds=10)
        self.store.finalize_completed(
            self.manager.complete_due(first.required_end_time_utc),
            {"ABC123": self.manager.capture_state("ABC123")})

        later_now = NOW + datetime.timedelta(seconds=200)
        second = self.manager.process_transition(
            transition(generation=2, seconds=300), later_now)
        self.assertIsNotNone(second)
        self.assertTrue(second.unfinished)
        self.assertTrue(self.store.observe_encounter(
            second, self.manager.capture_state("ABC123"), self.context,
            later_now))

        self.assertEqual(2, len(self.capture_directories()))
        self.assertNotEqual(first.encounter_id, second.encounter_id)

    def test_expired_unstored_encounter_does_not_start_physical_capture(self):
        state = self.manager.process_transition(transition(seconds=10), NOW)
        late_now = state.required_end_time_utc + datetime.timedelta(seconds=1)

        self.assertTrue(self.store.observe_encounter(
            state, None, self.context, late_now))
        self.assertEqual([], self.capture_directories())

    def test_existing_capture_directory_is_preserved_and_collision_is_recorded(self):
        state = self.manager.process_transition(transition(), NOW)
        token = state.encounter_id.replace(":", "_")
        base = "{}_{}_{}".format(
            NOW.strftime("%Y%m%dT%H%M%S_%fZ"), "ABC123", token)
        existing = self.root / "20260903" / "ABC123" / "captures" / base
        existing.mkdir(parents=True)
        sentinel = existing / "partial.bin"
        sentinel.write_bytes(b"preserve")

        self.assertTrue(self.store.observe_encounter(
            state, self.manager.capture_state("ABC123"), self.context))
        captures = self.capture_directories()
        self.assertEqual(2, len(captures))
        self.assertEqual(b"preserve", sentinel.read_bytes())
        created = next(item for item in captures if item != existing)
        payload = json.loads((created / "capture_manifest.json").read_text())
        self.assertEqual(1, payload["collision_index"])
        self.assertIn("capture_directory_collision",
                      payload["degradation_reasons"])

    def test_existing_complete_capture_directory_is_also_preserved(self):
        state = self.manager.process_transition(transition(), NOW)
        token = state.encounter_id.replace(":", "_")
        base = "{}_{}_{}".format(
            NOW.strftime("%Y%m%dT%H%M%S_%fZ"), "ABC123", token)
        existing = self.root / "20260903" / "ABC123" / "captures" / base
        existing.mkdir(parents=True)
        prior = existing / "capture_manifest.json"
        prior.write_text('{"status":"complete"}\n', encoding="utf-8")

        self.assertTrue(self.store.observe_encounter(
            state, self.manager.capture_state("ABC123"), self.context))
        self.assertEqual('{"status":"complete"}\n',
                         prior.read_text(encoding="utf-8"))
        self.assertEqual(2, len(self.capture_directories()))

    def test_graceful_shutdown_marks_active_capture_incomplete(self):
        self.open()
        self.prebuffer.feed_adsb_sbs(SBS, NOW)
        self.assertTrue(self.store.close_incomplete(NOW))
        payload = json.loads(
            (self.capture_directories()[0] / "capture_manifest.json").read_text())
        self.assertEqual("incomplete", payload["status"])

    def test_atomic_complete_manifest_failure_can_be_retried(self):
        state = self.open(seconds=10)
        completed = self.manager.complete_due(state.required_end_time_utc)
        real_atomic = candidate_recorder._atomic_json
        failed = {"done": False}

        def fail_complete(path, payload):
            if payload.get("status") == "complete" and not failed["done"]:
                failed["done"] = True
                raise OSError("replace failed")
            return real_atomic(path, payload)

        with patch.object(candidate_recorder, "_atomic_json", side_effect=fail_complete):
            self.assertFalse(self.store.finalize_completed(
                completed, {"ABC123": None}))
        self.assertTrue(self.store.finalize_completed((), {}))
        payload = json.loads(
            (self.capture_directories()[0] / "capture_manifest.json").read_text())
        self.assertEqual("complete", payload["status"])

    def test_timing_failure_rolls_back_data_and_all_ranges_are_valid(self):
        self.open()
        record = CandidateStreamRecord(
            StreamType.ADSB_SBS, "ABC123", NOW, SBS, sequence_id=100)
        real_dumps = candidate_recorder.json.dumps
        with patch.object(candidate_recorder.json, "dumps",
                          side_effect=OSError("timing failed")):
            self.assertFalse(self.store.observe_record(record))
        capture = self.capture_directories()[0]
        self.assertEqual(b"", (capture / "adsb_sbs.log").read_bytes())
        self.assertEqual("", (capture / "adsb_sbs.log.timing.jsonl").read_text())

        self.assertTrue(self.store.observe_record(replace(record, sequence_id=101)))
        data_size = (capture / "adsb_sbs.log").stat().st_size
        for line in (capture / "adsb_sbs.log.timing.jsonl").read_text().splitlines():
            item = real_dumps and json.loads(line)
            self.assertLessEqual(item["offset"] + item["length"], data_size)

    def test_finalization_truncates_and_reports_unindexed_crash_tail(self):
        state = self.open(seconds=10)
        record = CandidateStreamRecord(
            StreamType.ADSB_SBS, "ABC123", NOW, SBS, sequence_id=110)
        self.assertTrue(self.store.observe_record(record))
        capture = self.capture_directories()[0]
        with (capture / "adsb_sbs.log").open("ab") as handle:
            handle.write(b"unindexed-tail")
        completed = self.manager.complete_due(state.required_end_time_utc)
        self.assertTrue(self.store.finalize_completed(
            completed, {"ABC123": None}))

        payload = json.loads((capture / "capture_manifest.json").read_text())
        stream = payload["streams"]["adsb_sbs"]
        self.assertEqual(len(b"unindexed-tail"),
                         stream["recovered_unindexed_bytes"])
        self.assertEqual(stream["byte_count"],
                         (capture / "adsb_sbs.log").stat().st_size)
        self.assertIn("unindexed_stream_bytes_recovered",
                      payload["degradation_reasons"])

    def test_private_manifest_contains_frozen_prediction_and_observer(self):
        self.open()
        payload = json.loads(self.manifests()[0].read_text())

        self.assertTrue(payload["private_forensic_data"])
        self.assertEqual("TRUE_2D",
                         payload["trigger_prediction"]["prediction_geometry"])
        self.assertEqual(50.123, payload["observer_context"]["latitude_deg"])
        self.assertEqual(20.456, payload["observer_context"]["longitude_deg"])
        self.assertIn("frozen_vertical_state", payload["trigger_prediction"])

    def test_unattributable_input_and_no_trigger_write_nothing(self):
        self.assertFalse(self.prebuffer.feed_raw_adsb("invalid\n", NOW))
        self.prebuffer.feed_adsb_sbs(SBS, NOW)
        self.assertFalse(self.root.exists())

    def test_write_and_finalization_failures_are_fail_open(self):
        state = self.manager.process_transition(transition(), NOW)
        with patch.object(Path, "mkdir", side_effect=OSError("disk full")):
            self.assertFalse(self.store.observe_encounter(
                state, self.manager.capture_state("ABC123"), self.context))
        self.assertIn("disk full", self.store.last_error)
        self.assertFalse(self.store.observe_record(SimpleNamespace(
            icao="ABC123", received_at_utc=NOW)))

    def test_hard_capture_ceiling_prevents_unbounded_extension(self):
        manager = CandidateEncounterManager(
            self.prebuffer, max_capture_duration_seconds=500)
        first = manager.process_transition(transition(seconds=100), NOW)
        late_now = NOW + datetime.timedelta(seconds=400)
        late_prediction = prediction(seconds=300)
        late_prediction = late_prediction.__class__(**{
            **late_prediction.__dict__,
            "predicted_transit_utc": late_now + datetime.timedelta(seconds=300),
        })
        updated = manager.process_transition(AuthoritativeTransition(
            AuthoritativeTransitionKind.UPDATED, late_prediction), late_now)
        self.assertEqual(NOW + datetime.timedelta(seconds=500),
                         updated.required_end_time_utc)

    def test_hard_ceiling_is_explicit_and_later_generation_extends_capture(self):
        manager = CandidateEncounterManager(
            self.prebuffer, max_capture_duration_seconds=500)
        first = manager.process_transition(transition(seconds=100), NOW)
        late_now = NOW + datetime.timedelta(seconds=400)
        drifted = replace(
            prediction(generation=1),
            predicted_transit_utc=late_now + datetime.timedelta(seconds=300))
        first = manager.process_transition(AuthoritativeTransition(
            AuthoritativeTransitionKind.UPDATED, drifted), late_now)
        self.assertTrue(first.hard_ceiling_applied)
        second_prediction = replace(
            prediction(generation=2),
            predicted_transit_utc=late_now + datetime.timedelta(seconds=300))
        second = manager.process_transition(AuthoritativeTransition(
            AuthoritativeTransitionKind.OPENED, second_prediction), late_now)
        self.assertGreater(manager.capture_state("ABC123").capture_until_utc,
                           first.required_end_time_utc)

        store = CandidateBundleStore(self.root / "ceiling", self.prebuffer)
        store.observe_encounter(first, manager.capture_state("ABC123"), self.context)
        payload = json.loads(next(
            (self.root / "ceiling").rglob("encounters/*/manifest.json")).read_text())
        self.assertTrue(payload["required_window"]["hard_ceiling_applied"])
        self.assertEqual("maximum_capture_duration",
                         payload["required_window"]["truncation_reason"])
        store.close_incomplete(NOW)

    def test_async_worker_keeps_enqueue_nonblocking_and_marks_overflow(self):
        self.open()
        started = threading.Event()
        release = threading.Event()
        real_observe = self.store.observe_record

        def slow(record):
            started.set()
            release.wait(2)
            return real_observe(record)

        with patch.object(self.store, "observe_record", side_effect=slow):
            worker = CandidateStorageWorker(self.store, max_queue_size=1)
            first = CandidateStreamRecord(
                StreamType.ADSB_SBS, "ABC123", NOW, SBS, sequence_id=201)
            worker.enqueue_record(first)
            self.assertTrue(started.wait(1))
            worker.enqueue_record(replace(first, sequence_id=202))
            before = time.monotonic()
            self.assertFalse(worker.enqueue_record(replace(first, sequence_id=203)))
            self.assertLess(time.monotonic() - before, 0.05)
            release.set()
            self.assertTrue(worker.flush())
            self.assertTrue(worker.close())

        payload = json.loads(
            (self.capture_directories()[0] / "capture_manifest.json").read_text())
        self.assertTrue(payload["degraded"])
        self.assertIn("storage_queue_overflow", payload["degradation_reasons"])

    def test_worker_serializes_record_before_finalization(self):
        state = self.open(seconds=10)
        worker = CandidateStorageWorker(self.store)
        record = CandidateStreamRecord(
            StreamType.ADSB_SBS, "ABC123", NOW, SBS, sequence_id=301)
        self.assertTrue(worker.enqueue_record(record))
        completed = self.manager.complete_due(state.required_end_time_utc)
        self.assertTrue(worker.enqueue_finalize(completed, {"ABC123": None}))
        self.assertTrue(worker.flush())
        self.assertTrue(worker.close())
        capture = self.capture_directories()[0]
        data_size = (capture / "adsb_sbs.log").stat().st_size
        timing = json.loads(
            (capture / "adsb_sbs.log.timing.jsonl").read_text())
        self.assertLessEqual(timing["offset"] + timing["length"], data_size)



if __name__ == "__main__":
    unittest.main()
