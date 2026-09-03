"""Candidate Auto-Recorder Phase 3A dormant runtime wiring tests."""

import datetime
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import transit_warning as transit
from authoritative_transit import (
    AuthoritativeTransition,
    AuthoritativeTransitionKind,
)
from candidate_recorder import (
    CandidateEncounterManager,
    CandidatePreBuffer,
    encode_beast_wire,
)
from tests.test_authoritative_consumers import context, prediction
from tests.test_candidate_recorder import make_beast_frame, make_raw_df17
from tests.test_mlat_beast_track import frame, velocity_message, wire
from tests.test_raw_adsb_track import FRAME_NORTHEAST


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
SBS = (
    "MSG,3,1,1,4BAA92,1,2026/09/03,12:00:00.000,"
    "2026/09/03,12:00:00.000,,10000,,,51.0,21.0\n")


class FakeSocket:
    def __init__(self, text="", chunks=()):
        self._file = io.StringIO(text)
        self._chunks = iter(chunks)

    def connect(self, endpoint):
        self.endpoint = endpoint

    def makefile(self):
        return self._file

    def recv(self, size):
        return next(self._chunks, b"")

    def close(self):
        pass


class CandidateRecorderRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.old_buffer = transit.candidate_pre_buffer
        self.old_manager = transit.candidate_encounter_manager
        self.old_store = transit.candidate_bundle_store
        self.old_worker = transit.candidate_storage_worker
        self.old_adsb_port = transit.adsb_port
        self.old_mlat_port = transit.mlat_port
        self.old_stop = transit.stop_event.is_set()
        transit.stop_event.clear()
        self.buffer = CandidatePreBuffer(clock=lambda: NOW)
        self.manager = CandidateEncounterManager(self.buffer)
        transit.candidate_pre_buffer = self.buffer
        transit.candidate_encounter_manager = self.manager
        transit.candidate_bundle_store = None
        transit.candidate_storage_worker = None
        transit.adsb_port = 30003
        transit.mlat_port = 30106

    def tearDown(self):
        transit.candidate_pre_buffer = self.old_buffer
        transit.candidate_encounter_manager = self.old_manager
        transit.candidate_bundle_store = self.old_store
        transit.candidate_storage_worker = self.old_worker
        transit.adsb_port = self.old_adsb_port
        transit.mlat_port = self.old_mlat_port
        if self.old_stop:
            transit.stop_event.set()
        else:
            transit.stop_event.clear()

    def run_sbs_reader(self, port, line, processor=None, recorder=None):
        socket_instance = FakeSocket(text=line)
        socket_factory = Mock(side_effect=(socket_instance,
                                           KeyboardInterrupt()))
        with patch.object(transit.socket, "socket", socket_factory), \
                patch.object(transit, "_register_active_socket",
                             return_value=True), \
                patch.object(transit, "_unregister_active_socket"), \
                patch.object(transit.clock, "now_utc", return_value=NOW), \
                patch.object(transit.stop_event, "wait", return_value=True):
            with self.assertRaises(KeyboardInterrupt):
                transit.read_from_port(
                    "receiver", port, processor or Mock(), recorder)

    def test_production_adsb_and_mlat_sbs_reach_shared_prebuffer(self):
        self.run_sbs_reader(30003, SBS)
        self.run_sbs_reader(30106, SBS)

        records = self.buffer.get_records("4BAA92")
        self.assertEqual(("adsb_sbs", "mlat_sbs"),
                         tuple(item.stream_type.value for item in records))
        self.assertTrue(all(item.raw_data == SBS for item in records))

    def test_raw_reader_uses_phase1_conservative_attribution(self):
        valid = make_raw_df17() + "\n"
        socket_instance = FakeSocket(text="invalid\n" + valid)
        with patch.object(transit.socket, "socket",
                          return_value=socket_instance), \
                patch.object(transit, "_register_active_socket",
                             return_value=True), \
                patch.object(transit, "_unregister_active_socket"), \
                patch.object(transit.clock, "now_utc", return_value=NOW), \
                patch.object(transit, "update_raw_adsb_track"), \
                patch.object(transit.stop_event, "wait", return_value=True):
            transit.read_raw_adsb_track("receiver", 30002)

        records = self.buffer.get_records("4BAA92")
        self.assertEqual(1, len(records))
        self.assertEqual(valid, records[0].raw_data)

    def test_mlat_beast_reader_feeds_existing_parsed_frame(self):
        frame = make_beast_frame()
        socket_instance = FakeSocket(chunks=(
            encode_beast_wire(frame),))
        with patch.object(transit.socket, "socket",
                          return_value=socket_instance), \
                patch.object(transit, "_register_active_socket",
                             return_value=True), \
                patch.object(transit, "_unregister_active_socket"), \
                patch.object(transit.clock, "now_utc", return_value=NOW), \
                patch.object(transit, "update_mlat_beast_track"), \
                patch.object(transit.stop_event, "wait", return_value=True):
            transit.read_mlat_beast_track("receiver", 30105)

        records = self.buffer.get_records("4BAA6D")
        self.assertEqual(1, len(records))
        self.assertEqual("mlat_beast", records[0].stream_type.value)

    def test_candidate_failure_cannot_block_sbs_processing_or_full_recorder(self):
        failing_buffer = Mock()
        failing_buffer.feed_adsb_sbs.side_effect = RuntimeError("candidate")
        transit.candidate_pre_buffer = failing_buffer
        processor = Mock()
        recorder = Mock()

        self.run_sbs_reader(30003, SBS, processor, recorder)

        recorder.record_line.assert_called_once_with(30003, SBS)
        processor.assert_called_once_with(SBS.strip(), 30003)

    def test_candidate_failure_cannot_block_raw_decoder(self):
        valid = FRAME_NORTHEAST + "\n"
        socket_instance = FakeSocket(text=valid)
        failing_buffer = Mock()
        failing_buffer.feed_raw_adsb.side_effect = RuntimeError("candidate")
        transit.candidate_pre_buffer = failing_buffer
        with patch.object(transit.socket, "socket",
                          return_value=socket_instance), \
                patch.object(transit, "_register_active_socket",
                             return_value=True), \
                patch.object(transit, "_unregister_active_socket"), \
                patch.object(transit.clock, "now_utc", return_value=NOW), \
                patch.object(transit, "update_raw_adsb_track") as update, \
                patch.object(transit.stop_event, "wait", return_value=True):
            transit.read_raw_adsb_track("receiver", 30002)

        update.assert_called_once()

    def test_candidate_failure_cannot_block_mlat_beast_decoder(self):
        encoded = wire(frame(velocity_message()))
        socket_instance = FakeSocket(chunks=(encoded,))
        failing_buffer = Mock()
        failing_buffer.feed_mlat_beast_frame.side_effect = RuntimeError(
            "candidate")
        transit.candidate_pre_buffer = failing_buffer
        with patch.object(transit.socket, "socket",
                          return_value=socket_instance), \
                patch.object(transit, "_register_active_socket",
                             return_value=True), \
                patch.object(transit, "_unregister_active_socket"), \
                patch.object(transit.clock, "now_utc", return_value=NOW), \
                patch.object(transit, "update_mlat_beast_track") as update, \
                patch.object(transit.stop_event, "wait", return_value=True):
            transit.read_mlat_beast_track("receiver", 30105)

        update.assert_called_once()

    def test_authoritative_transition_reaches_manager_before_consumers(self):
        manager = Mock()
        transit.candidate_encounter_manager = manager
        item = prediction(seconds=60, separation=0.4)
        held = AuthoritativeTransition(
            AuthoritativeTransitionKind.HELD, item)

        self.assertTrue(transit.consume_authoritative_transition(
            held, context(), [""] * 32, 20.0, NOW))
        manager.process_transition.assert_called_once_with(held, NOW)

    def test_candidate_transition_failure_does_not_change_consumer_result(self):
        manager = Mock()
        manager.process_transition.side_effect = RuntimeError("candidate")
        transit.candidate_encounter_manager = manager
        item = prediction(seconds=60, separation=0.4)
        held = AuthoritativeTransition(
            AuthoritativeTransitionKind.HELD, item)

        self.assertTrue(transit.consume_authoritative_transition(
            held, context(), [""] * 32, 20.0, NOW))

    def test_legacy_and_nontriggering_transitions_create_no_candidate(self):
        legacy = replace(
            prediction(seconds=60, separation=0.4), model="LEGACY")
        transit.observe_candidate_authoritative_transition(
            AuthoritativeTransition(AuthoritativeTransitionKind.OPENED,
                                    legacy), NOW)
        transit.observe_candidate_authoritative_transition(
            AuthoritativeTransition(AuthoritativeTransitionKind.NONE, None),
            NOW)

        self.assertEqual((), self.manager.encounters_for_icao("ABC123"))

    def test_runtime_wiring_without_trigger_creates_no_disk_output(self):
        with tempfile.TemporaryDirectory() as directory:
            before = tuple(Path(directory).iterdir())
            transit.observe_candidate_sbs_input(SBS, 30003, NOW)
            transit.observe_candidate_raw_input(make_raw_df17() + "\n", NOW)
            transit.complete_candidate_observation_windows(NOW)
            after = tuple(Path(directory).iterdir())

        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
