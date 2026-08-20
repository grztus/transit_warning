import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from beast_recording import BeastRecorder
from recording import RecordingStatus
import transit_warning as transit


UTC = datetime.timezone.utc


class FakeMonotonic:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class BeastRecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.monotonic = FakeMonotonic()
        self.errors = []
        self.recorder = BeastRecorder(
            self.path, "receiver", 30005, self.errors.append, self.monotonic)

    def tearDown(self):
        self.recorder.close()
        self.temp.cleanup()

    def now(self, second=0):
        return datetime.datetime(2026, 8, 20, 8, 0, second, tzinfo=UTC)

    def test_records_byte_for_byte_including_escaped_1a_and_chunk_boundaries(self):
        chunks = [b"\x1a3\x00\x1a", b"\x1a\xffpayload", b"\x1a2tail"]
        for index, chunk in enumerate(chunks):
            self.monotonic.value = index * 0.2
            self.assertTrue(self.recorder.record_chunk(
                chunk, self.now(index), self.monotonic(), 1))
        self.recorder.close()
        self.assertEqual((self.path / "beast.bin").read_bytes(), b"".join(chunks))

    def test_timing_checkpoints_define_after_write_offsets_and_connections(self):
        self.recorder.record_chunk(b"abc", self.now(), 10.0, 1)
        self.recorder.record_chunk(b"de", self.now(1), 10.5, 1)
        self.recorder.record_chunk(b"f", self.now(2), 10.6, 2)
        self.recorder.close()
        records = [json.loads(line) for line in
                   (self.path / "beast_timing.jsonl").read_text().splitlines()]
        self.assertEqual([record["byte_offset_after"] for record in records], [3, 6])
        self.assertEqual([record["connection_id"] for record in records], [1, 2])
        self.assertEqual(records[0]["reception_utc"], "2026-08-20T08:00:00Z")
        self.assertEqual(records[0]["monotonic_seconds"], 10.0)
        self.assertEqual(records[0]["host"], "receiver")
        self.assertEqual(records[0]["port"], 30005)
        self.assertEqual([record["bytes_since_previous"] for record in records], [3, 3])

    def test_close_adds_final_checkpoint_and_is_idempotent(self):
        self.recorder.record_chunk(b"a", self.now(), 0.0, 1)
        self.recorder.record_chunk(b"b", self.now(1), 0.2, 1)
        self.recorder.close()
        first = (self.path / "beast_timing.jsonl").read_bytes()
        self.recorder.close()
        self.assertEqual((self.path / "beast_timing.jsonl").read_bytes(), first)
        self.assertEqual([json.loads(line)["byte_offset_after"]
                          for line in first.decode().splitlines()], [1, 2])

    def test_open_failure_is_fail_open_and_reported_once(self):
        errors = []
        recorder = BeastRecorder(
            self.path / "missing", "receiver", 30005, errors.append)
        self.assertEqual(recorder.status, RecordingStatus.FAILED)
        self.assertFalse(recorder.record_chunk(b"data", self.now(), 0.0, 1))
        self.assertEqual(len(errors), 1)

    def test_write_failure_is_fail_open(self):
        binary = Mock()
        binary.write.side_effect = OSError("disk full")
        timing = io.StringIO()
        errors = []
        recorder = BeastRecorder(
            self.path, "receiver", 30005, errors.append,
            binary_opener=lambda: binary, timing_opener=lambda: timing)
        self.assertFalse(recorder.record_chunk(b"data", self.now(), 0.0, 1))
        self.assertEqual(recorder.status, RecordingStatus.FAILED)
        self.assertEqual(len(errors), 1)


class FakeBeastSocket:
    def __init__(self, chunks, stop_after=False):
        self.chunks = iter(chunks)
        self.stop_after = stop_after

    def connect(self, endpoint):
        self.endpoint = endpoint

    def recv(self, size):
        try:
            return next(self.chunks)
        except StopIteration:
            if self.stop_after:
                transit.stop_event.set()
            return b""

    def shutdown(self, how):
        pass

    def close(self):
        pass


class BeastNetworkTests(unittest.TestCase):
    def tearDown(self):
        transit.stop_event.clear()
        transit.shutdown_complete = False
        transit.close_active_sockets()

    def test_disconnect_reconnect_continues_same_recorder(self):
        first = FakeBeastSocket([b"first"])
        second = FakeBeastSocket([b"second"], stop_after=True)
        recorder = Mock()
        transit.stop_event.clear()
        with patch.object(transit.socket, "socket", side_effect=[first, second]), \
                patch.object(transit.stop_event, "wait", return_value=False), \
                patch.object(transit.clock, "now_utc", return_value=datetime.datetime(
                    2026, 8, 20, tzinfo=UTC)), \
                patch.object(transit.time, "monotonic", side_effect=[1.0, 2.0]):
            transit.read_beast_port("receiver", 30005, recorder,
                                   monotonic=transit.time.monotonic)
        self.assertEqual(recorder.record_chunk.call_count, 2)
        self.assertEqual(recorder.record_chunk.call_args_list[0].args[0], b"first")
        self.assertEqual(recorder.record_chunk.call_args_list[0].args[3], 1)
        self.assertEqual(recorder.record_chunk.call_args_list[1].args[0], b"second")
        self.assertEqual(recorder.record_chunk.call_args_list[1].args[3], 2)

    def test_unavailable_beast_stops_cleanly_without_touching_other_recorders(self):
        transit.stop_event.clear()
        recorder = Mock()
        with patch.object(transit.socket, "socket", side_effect=OSError("refused")), \
                patch.object(transit.stop_event, "wait", side_effect=lambda timeout: (
                    transit.stop_event.set() or True)), \
                patch("builtins.print") as output:
            transit.read_beast_port("receiver", 30005, recorder)
        recorder.record_chunk.assert_not_called()
        output.assert_called_once()

    def test_shutdown_closes_binary_recorder_fail_open(self):
        transit.stop_event.clear()
        transit.shutdown_complete = False
        recorder = Mock()
        transit.shutdown_runtime([], None, recorder)
        recorder.close.assert_called_once_with()

    def test_recorder_failure_stops_only_beast_reader(self):
        transit.stop_event.clear()
        recorder = Mock()
        recorder.record_chunk.return_value = False
        sock = FakeBeastSocket([b"data"])
        with patch.object(transit.socket, "socket", return_value=sock):
            transit.read_beast_port(
                "receiver", 30005, recorder,
                utc_now=lambda: datetime.datetime(2026, 8, 20, tzinfo=UTC),
                monotonic=lambda: 1.0,
            )
        recorder.record_chunk.assert_called_once()


if __name__ == "__main__":
    unittest.main()
