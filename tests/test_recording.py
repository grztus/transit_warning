import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from recording import (
    RecordingStatus,
    MlatBeastWriter,
    SessionRecorder,
    StreamWriter,
    archive_session,
)


UTC = timezone.utc
START = datetime(2026, 8, 17, 20, 44, 17, 502832, tzinfo=UTC)


class FailingWriteFile(io.StringIO):
    def write(self, value):
        raise OSError("disk full")


class FailingBytesFile(io.BytesIO):
    def write(self, value):
        raise OSError("disk full")


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class TrackingFile(io.StringIO):
    def __init__(self, fail_flush=False, fail_close=False):
        super().__init__()
        self.fail_flush = fail_flush
        self.fail_close = fail_close
        self.flush_calls = 0
        self.close_calls = 0

    def flush(self):
        self.flush_calls += 1
        if self.fail_flush:
            raise OSError("flush failed")
        return super().flush()

    def close(self):
        self.close_calls += 1
        if self.fail_close:
            self.fail_close = False
            raise OSError("close failed")
        return super().close()


class StreamWriterTests(unittest.TestCase):
    def make_writer(self, file_object=None):
        self.clock = FakeClock()
        self.file_object = file_object or TrackingFile()
        return StreamWriter(
            "unused.log", "ADS-B", opener=lambda: self.file_object,
            monotonic=self.clock)

    def test_recording_status_values(self):
        self.assertEqual(RecordingStatus.OFF.value, "OFF")
        self.assertEqual(RecordingStatus.RECORDING.value, "RECORDING")
        self.assertEqual(RecordingStatus.FAILED.value, "FAILED")

    def test_writes_exact_text_and_counts_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stream.log"
            writer = StreamWriter(path, "ADS-B")
            line = "MSG,3,...,2026/08/17,23:26:42.689\n"
            self.assertTrue(writer.record_line(line))
            self.assertEqual(writer.lines_written, 1)
            writer.close()
            self.assertEqual(path.read_text(encoding="utf-8"), line)

    def test_open_failure_is_fail_open_and_reported_once(self):
        messages = []

        def fail_open():
            raise PermissionError("denied")

        writer = StreamWriter("unused.log", "ADS-B", messages.append, fail_open)
        self.assertEqual(writer.status, RecordingStatus.FAILED)
        self.assertFalse(writer.record_line("first\n"))
        self.assertFalse(writer.record_line("second\n"))
        self.assertEqual(len(messages), 1)

    def test_write_failure_is_fail_open(self):
        messages = []
        writer = StreamWriter(
            "unused.log", "MLAT", messages.append, lambda: FailingWriteFile())
        self.assertFalse(writer.record_line("MSG,3\n"))
        self.assertEqual(writer.status, RecordingStatus.FAILED)
        self.assertEqual(writer.lines_written, 0)
        self.assertFalse(writer.record_line("MSG,4\n"))
        self.assertEqual(len(messages), 1)

    def test_does_not_flush_before_one_second_or_1000_lines(self):
        writer = self.make_writer()
        for _ in range(999):
            self.assertTrue(writer.record_line("line\n"))
        self.clock.value = 0.999
        self.assertFalse(writer.flush_if_due())
        self.assertEqual(self.file_object.flush_calls, 0)
        self.assertEqual(writer.lines_since_flush, 999)

    def test_flushes_exactly_after_one_second(self):
        writer = self.make_writer()
        writer.record_line("line\n")
        self.clock.value = 1.0
        self.assertTrue(writer.flush_if_due())
        self.assertEqual(self.file_object.flush_calls, 1)
        self.assertEqual(writer.lines_since_flush, 0)

    def test_flushes_exactly_at_1000_lines(self):
        writer = self.make_writer()
        for _ in range(999):
            writer.record_line("line\n")
        self.assertEqual(self.file_object.flush_calls, 0)
        writer.record_line("line\n")
        self.assertEqual(self.file_object.flush_calls, 1)
        self.assertEqual(writer.lines_since_flush, 0)

    def test_flush_resets_time_and_line_thresholds(self):
        writer = self.make_writer()
        writer.record_line("first\n")
        self.clock.value = 1.0
        writer.flush_if_due()
        writer.record_line("second\n")
        self.clock.value = 1.999
        self.assertFalse(writer.flush_if_due())
        self.assertEqual(writer.lines_since_flush, 1)
        self.clock.value = 2.0
        self.assertTrue(writer.flush_if_due())
        self.assertEqual(self.file_object.flush_calls, 2)

    def test_close_performs_final_flush_and_is_idempotent(self):
        writer = self.make_writer()
        writer.record_line("line\n")
        writer.close()
        writer.close()
        self.assertEqual(self.file_object.flush_calls, 1)
        self.assertEqual(self.file_object.close_calls, 1)

    def test_flush_error_is_fail_open(self):
        messages = []
        writer = self.make_writer(TrackingFile(fail_flush=True))
        writer.error_handler = messages.append
        writer.record_line("line\n")
        self.clock.value = 1.0
        self.assertFalse(writer.flush_if_due())
        self.assertEqual(writer.status, RecordingStatus.FAILED)
        self.assertFalse(writer.record_line("later\n"))
        self.assertEqual(len(messages), 1)

    def test_close_error_is_fail_open(self):
        messages = []
        writer = self.make_writer(TrackingFile(fail_close=True))
        writer.error_handler = messages.append
        writer.record_line("line\n")
        writer.close()
        writer.close()
        self.assertEqual(writer.status, RecordingStatus.FAILED)
        self.assertEqual(len(messages), 1)


class MlatBeastWriterTests(unittest.TestCase):
    def test_preserves_binary_chunks_and_writes_ordered_receipt_events(self):
        with tempfile.TemporaryDirectory() as directory:
            binary_path = Path(directory) / "mlat_beast_30105.bin"
            events_path = Path(directory) / "mlat_beast_30105_events.jsonl"
            writer = MlatBeastWriter(binary_path, events_path)
            chunks = (b"\x1a\x33\xff\x00", b"MLAT\x00\x1a\x1a\x7f")
            for chunk in chunks:
                self.assertTrue(writer.record_bytes(chunk))
            first = SimpleNamespace(
                frame_type=0x33, beast_timestamp=0xFF004D4C4154,
                signal=0, modes=bytes.fromhex("920102030405060708090A0B0C0D"))
            second = SimpleNamespace(
                frame_type=0x32, beast_timestamp=0x010203040506,
                signal=17, modes=bytes.fromhex("AABBCCDDEEFF00"))
            self.assertTrue(writer.record_event(first, START, True))
            later = START.replace(microsecond=600000)
            self.assertTrue(writer.record_event(second, later, False))
            writer.close()
            self.assertEqual(binary_path.read_bytes(), b"".join(chunks))
            events = [json.loads(line) for line in
                      events_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([1, 2], [event["sequence"] for event in events])
            self.assertEqual("2026-08-17T20:44:17.502832Z",
                             events[0]["received_at_utc"])
            self.assertEqual(0x33, events[0]["frame_type"])
            self.assertEqual("FF004D4C4154", events[0]["beast_timestamp"])
            self.assertEqual(0, events[0]["signal"])
            self.assertEqual(first.modes.hex().upper(), events[0]["modes_hex"])
            self.assertEqual((len(b"".join(chunks)), 2, 1), (
                writer.bytes_written, writer.frames_recorded,
                writer.tc19_updates))

    def test_optional_binary_failure_is_fail_open_and_reported_once(self):
        messages = []
        writer = MlatBeastWriter(
            "unused.bin", "unused.jsonl", error_handler=messages.append,
            binary_opener=FailingBytesFile,
            events_opener=io.StringIO)
        self.assertFalse(writer.record_bytes(b"first"))
        self.assertFalse(writer.record_bytes(b"second"))
        self.assertEqual(RecordingStatus.FAILED, writer.status)
        self.assertEqual(1, len(messages))


class SessionRecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp.name) / "sessions"
        self.recorder = SessionRecorder(
            START, 30003, 30106, "Europe/Warsaw", self.base_dir)

    def tearDown(self):
        self.recorder.close()
        self.temp.cleanup()

    def test_creates_session_directory_files_and_v1_manifest(self):
        self.assertEqual(self.recorder.session_id, "20260817_204417")
        self.assertTrue(self.recorder.session_dir.is_dir())
        self.assertTrue((self.recorder.session_dir / "adsb_30003.log").is_file())
        self.assertTrue((self.recorder.session_dir / "mlat_30106.log").is_file())
        manifest = json.loads(self.recorder.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["session_start_utc"], "2026-08-17T20:44:17.502832Z")
        self.assertEqual(manifest["recording_status"], "recording")
        self.assertEqual(manifest["adsb"]["timestamp_semantics"], "local")
        self.assertEqual(manifest["adsb"]["timestamp_timezone"], "Europe/Warsaw")
        self.assertEqual(manifest["adsb"]["status"], "recording")
        self.assertEqual(manifest["mlat"]["timestamp_semantics"], "utc")
        self.assertEqual(manifest["mlat"]["status"], "recording")
        self.assertNotIn("timestamp_timezone", manifest["mlat"])
        self.assertNotIn("environment", manifest)
        self.assertNotIn("raw", manifest)
        self.assertNotIn("mlat_beast", manifest)
        self.assertEqual([], list(
            self.recorder.session_dir.glob("mlat_beast_*")))

    def test_optional_raw_writer_records_exact_text_and_extends_manifest(self):
        recorder = SessionRecorder(
            START, 30003, 30106, "Europe/Warsaw", self.base_dir / "raw",
            raw_port=32002)
        line = "@0097635C74DC8D4BAA929908E3B2F0042FBB4B20;  \r\n"
        self.assertTrue(recorder.record_line(32002, line))
        recorder.close(START)
        with (recorder.session_dir / "raw_32002.log").open(
                encoding="utf-8", newline="") as raw_file:
            self.assertEqual(raw_file.read(), line)
        manifest = recorder.manifest_data()
        self.assertEqual(manifest["raw"]["file"], "raw_32002.log")
        self.assertEqual(manifest["raw"]["port"], 32002)
        self.assertEqual(manifest["raw"]["format"], "raw-mode-s-text")
        self.assertEqual(
            manifest["raw"]["timestamp_semantics"], "receiver-clock")
        self.assertEqual(manifest["raw"]["line_count"], 1)
        self.assertTrue(manifest["raw"]["available"])

    def test_optional_raw_stream_may_be_empty_without_making_session_partial(self):
        recorder = SessionRecorder(
            START, 30003, 30106, "Europe/Warsaw", self.base_dir / "empty-raw",
            raw_port=30002)
        recorder.record_line(30003, "ADS-B\n")
        recorder.record_line(30106, "MLAT\n")
        recorder.close(START)
        manifest = recorder.manifest_data()
        self.assertEqual(manifest["recording_status"], "complete")
        self.assertEqual(manifest["raw"]["line_count"], 0)
        self.assertFalse(manifest["raw"]["available"])

    def test_optional_mlat_beast_manifest_counts_and_unavailable_state(self):
        recorder = SessionRecorder(
            START, 30003, 30106, "Europe/Warsaw",
            self.base_dir / "mlat-beast", mlat_beast_port=30105)
        recorder.record_line(30003, "ADS-B\n")
        recorder.record_line(30106, "MLAT\n")
        recorder.close(START)
        manifest = recorder.manifest_data()
        optional = manifest["mlat_beast"]
        self.assertEqual("complete", manifest["recording_status"])
        self.assertEqual("mlat_beast_30105.bin", optional["file"])
        self.assertEqual(
            "mlat_beast_30105_events.jsonl", optional["events_file"])
        self.assertEqual(30105, optional["port"])
        self.assertEqual("beast-binary-synthetic-mlat", optional["format"])
        self.assertEqual("magic-mlat-marker",
                         optional["timestamp_semantics"])
        self.assertEqual("production-receipt-utc",
                         optional["receipt_timestamp_semantics"])
        self.assertFalse(optional["available"])
        self.assertEqual((0, 0, 0), (
            optional["bytes_written"], optional["frames_recorded"],
            optional["tc19_updates"]))

    def test_optional_mlat_beast_failure_does_not_make_core_partial(self):
        recorder = SessionRecorder(
            START, 30003, 30106, "Europe/Warsaw",
            self.base_dir / "failed-mlat-beast", mlat_beast_port=30105)
        recorder.mlat_beast_writer._binary.close()
        recorder.mlat_beast_writer._binary = FailingBytesFile()
        self.assertFalse(recorder.record_mlat_beast_bytes(b"BEAST"))
        self.assertTrue(recorder.record_line(30003, "ADS-B\n"))
        self.assertTrue(recorder.record_line(30106, "MLAT\n"))
        recorder.close(START)
        manifest = recorder.manifest_data()
        self.assertEqual("complete", manifest["recording_status"])
        self.assertEqual("failed", manifest["mlat_beast"]["status"])
        self.assertEqual("complete", manifest["adsb"]["status"])
        self.assertEqual("complete", manifest["mlat"]["status"])

    def test_failed_raw_writer_does_not_stop_adsb_or_mlat_writers(self):
        recorder = SessionRecorder(
            START, 30003, 30106, "Europe/Warsaw", self.base_dir / "failed-raw",
            raw_port=30002)
        recorder.raw_writer._file.close()
        recorder.raw_writer._file = FailingWriteFile()
        self.assertFalse(recorder.record_line(30002, "RAW\n"))
        self.assertTrue(recorder.record_line(30003, "ADS-B\n"))
        self.assertTrue(recorder.record_line(30106, "MLAT\n"))
        recorder.close(START)
        manifest = recorder.manifest_data()
        self.assertEqual(manifest["recording_status"], "partial")
        self.assertEqual(manifest["raw"]["status"], "failed")
        self.assertEqual(manifest["adsb"]["status"], "complete")
        self.assertEqual(manifest["mlat"]["status"], "complete")

    def test_two_writers_keep_streams_separate_and_count_lines(self):
        adsb = "MSG,3,ADS-B,2026/08/17,23:26:42.689\n"
        mlat = "MSG,3,MLAT,2026/08/17,21:26:42.689\n"
        self.assertTrue(self.recorder.record_line(30003, adsb))
        self.assertTrue(self.recorder.record_line(30106, mlat))
        self.assertTrue(self.recorder.record_line(30003, adsb))
        self.recorder.close(START)
        self.assertEqual((self.recorder.session_dir / "adsb_30003.log").read_text(), adsb * 2)
        self.assertEqual((self.recorder.session_dir / "mlat_30106.log").read_text(), mlat)
        manifest = json.loads(self.recorder.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["adsb"]["line_count"], 2)
        self.assertEqual(manifest["mlat"]["line_count"], 1)
        self.assertEqual(manifest["recording_status"], "complete")
        self.assertEqual(manifest["session_end_utc"], "2026-08-17T20:44:17.502832Z")

    def test_one_failed_writer_does_not_stop_the_other(self):
        self.recorder.adsb_writer._file.close()
        self.recorder.adsb_writer._file = FailingWriteFile()
        self.assertFalse(self.recorder.record_line(30003, "ADS-B\n"))
        self.assertEqual(self.recorder.adsb_writer.status, RecordingStatus.FAILED)
        self.assertTrue(self.recorder.record_line(30106, "MLAT\n"))
        self.assertEqual(self.recorder.mlat_writer.status, RecordingStatus.RECORDING)
        self.assertEqual(self.recorder.manifest_data()["recording_status"], "recording")
        self.recorder.close(START)
        self.assertEqual(self.recorder.manifest_data()["recording_status"], "partial")
        self.assertEqual(self.recorder.manifest_data()["adsb"]["status"], "failed")
        self.assertEqual(self.recorder.manifest_data()["mlat"]["status"], "complete")

    def test_unknown_port_is_ignored(self):
        self.assertFalse(self.recorder.record_line(39999, "UNKNOWN\n"))
        self.assertEqual(self.recorder.adsb_writer.lines_written, 0)
        self.assertEqual(self.recorder.mlat_writer.lines_written, 0)

    def test_silent_port_still_allows_complete(self):
        self.recorder.record_line(30003, "ADS-B only\n")
        self.recorder.close(START)
        manifest = self.recorder.manifest_data()
        self.assertEqual(manifest["recording_status"], "complete")
        self.assertEqual(manifest["mlat"]["line_count"], 0)

    def test_close_finishes_both_writers_and_is_idempotent(self):
        adsb_file = self.recorder.adsb_writer._file
        mlat_file = self.recorder.mlat_writer._file
        self.recorder.close(START)
        first_manifest = self.recorder.manifest_path.read_text(encoding="utf-8")
        self.recorder.close(datetime(2026, 8, 17, 21, 0, tzinfo=UTC))
        self.assertTrue(adsb_file.closed)
        self.assertTrue(mlat_file.closed)
        self.assertEqual(
            self.recorder.manifest_path.read_text(encoding="utf-8"), first_manifest)

    def test_manifest_write_failure_is_fail_open(self):
        self.recorder.record_line(30003, "ADS-B\n")
        with patch("recording.os.replace", side_effect=OSError("manifest denied")):
            self.recorder.close(START)
        self.assertIsNotNone(self.recorder.manifest_error)
        self.assertEqual(self.recorder.manifest_data()["recording_status"], "failed")


class ArchiveSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.temp.name) / "20260817_204417"
        self.session_dir.mkdir()
        self.adsb_path = self.session_dir / "adsb_30003.log"
        self.mlat_path = self.session_dir / "mlat_30106.log"
        self.adsb_content = b"MSG,3,ADS-B\r\nMSG,4,ADS-B\n"
        self.mlat_content = b"MSG,3,MLAT\n"
        self.adsb_path.write_bytes(self.adsb_content)
        self.mlat_path.write_bytes(self.mlat_content)

    def tearDown(self):
        self.temp.cleanup()

    @property
    def archive_path(self):
        return self.session_dir / "streams.zip"

    @property
    def temporary_path(self):
        return self.session_dir / "streams.zip.tmp"

    def test_creates_verified_zip_with_both_original_contents(self):
        self.assertTrue(archive_session(self.session_dir))
        with zipfile.ZipFile(self.archive_path) as archive:
            self.assertEqual(
                set(archive.namelist()), {self.adsb_path.name, self.mlat_path.name})
            self.assertEqual(archive.read(self.adsb_path.name), self.adsb_content)
            self.assertEqual(archive.read(self.mlat_path.name), self.mlat_content)
            self.assertIsNone(archive.testzip())

    def test_optional_raw_log_is_included_and_verified_losslessly(self):
        raw_path = self.session_dir / "raw_30002.log"
        raw_content = b"@0097635C74DC8D4BAA929908E3B2F0042FBB4B20;\r\n"
        raw_path.write_bytes(raw_content)
        self.assertTrue(archive_session(self.session_dir))
        with zipfile.ZipFile(self.archive_path) as archive:
            self.assertEqual(set(archive.namelist()), {
                self.adsb_path.name, self.mlat_path.name, raw_path.name})
            self.assertEqual(archive.read(raw_path.name), raw_content)
            self.assertIsNone(archive.testzip())

    def test_manifest_declared_mlat_beast_members_are_archived_and_verified(self):
        recorder = SessionRecorder(
            START, 30003, 30106, "Europe/Warsaw",
            Path(self.temp.name) / "manifest-sessions",
            raw_port=30002, mlat_beast_port=30105)
        recorder.record_line(30003, "ADS-B\n")
        recorder.record_line(30106, "MLAT\n")
        recorder.record_line(30002, "RAW\n")
        beast_bytes = b"\x1a\x33\xff\x00MLAT\x00\x1a\x1a"
        frame = SimpleNamespace(
            frame_type=0x33, beast_timestamp=0xFF004D4C4154,
            signal=0, modes=bytes.fromhex("920102030405060708090A0B0C0D"))
        recorder.record_mlat_beast_bytes(beast_bytes)
        recorder.record_mlat_beast_event(frame, START, True)
        recorder.close(START)
        self.assertTrue(archive_session(recorder.session_dir))
        with zipfile.ZipFile(recorder.session_dir / "streams.zip") as archive:
            self.assertEqual(set(archive.namelist()), {
                "adsb_30003.log", "mlat_30106.log", "raw_30002.log",
                "mlat_beast_30105.bin",
                "mlat_beast_30105_events.jsonl"})
            self.assertEqual(beast_bytes,
                             archive.read("mlat_beast_30105.bin"))
            self.assertIsNone(archive.testzip())

    def test_manifest_count_failure_preserves_all_loose_streams(self):
        recorder = SessionRecorder(
            START, 30003, 30106, "Europe/Warsaw",
            Path(self.temp.name) / "invalid-manifest-sessions",
            mlat_beast_port=30105)
        recorder.record_line(30003, "ADS-B\n")
        recorder.record_line(30106, "MLAT\n")
        recorder.record_mlat_beast_bytes(b"BEAST")
        recorder.close(START)
        manifest = recorder.manifest_data()
        manifest["mlat_beast"]["bytes_written"] += 1
        recorder.manifest_path.write_text(
            json.dumps(manifest), encoding="utf-8")
        self.assertFalse(archive_session(
            recorder.session_dir, delete_raw=True))
        self.assertFalse((recorder.session_dir / "streams.zip").exists())
        for section, key in (("adsb", "file"), ("mlat", "file"),
                             ("mlat_beast", "file"),
                             ("mlat_beast", "events_file")):
            self.assertTrue((recorder.session_dir /
                             manifest[section][key]).exists())

    def test_delete_after_verified_three_stream_archive_removes_all_logs(self):
        raw_path = self.session_dir / "raw_30002.log"
        raw_path.write_bytes(b"RAW\n")
        self.assertTrue(archive_session(self.session_dir, delete_raw=True))
        self.assertFalse(self.adsb_path.exists())
        self.assertFalse(self.mlat_path.exists())
        self.assertFalse(raw_path.exists())
        with zipfile.ZipFile(self.archive_path) as archive:
            self.assertEqual(len(archive.namelist()), 3)

    def test_delete_raw_false_preserves_both_logs(self):
        self.assertTrue(archive_session(self.session_dir, delete_raw=False))
        self.assertTrue(self.adsb_path.exists())
        self.assertTrue(self.mlat_path.exists())

    def test_delete_raw_true_removes_logs_only_after_success(self):
        with patch("recording.os.replace", wraps=os.replace) as replace:
            self.assertTrue(archive_session(self.session_dir, delete_raw=True))
        replace.assert_called_once()
        self.assertTrue(self.archive_path.exists())
        self.assertFalse(self.adsb_path.exists())
        self.assertFalse(self.mlat_path.exists())

    def test_compression_error_preserves_raw_and_removes_temporary(self):
        messages = []
        with patch("recording.zipfile.ZipFile.write", side_effect=OSError("full")):
            self.assertFalse(archive_session(self.session_dir, True, messages.append))
        self.assertTrue(self.adsb_path.exists())
        self.assertTrue(self.mlat_path.exists())
        self.assertFalse(self.archive_path.exists())
        self.assertFalse(self.temporary_path.exists())
        self.assertEqual(len(messages), 1)

    def test_verification_error_preserves_raw_and_removes_temporary(self):
        with patch("recording._verify_stream_archive", return_value=False):
            self.assertFalse(archive_session(self.session_dir, delete_raw=True))
        self.assertTrue(self.adsb_path.exists())
        self.assertTrue(self.mlat_path.exists())
        self.assertFalse(self.archive_path.exists())
        self.assertFalse(self.temporary_path.exists())

    def test_existing_archive_is_idempotent(self):
        self.assertTrue(archive_session(self.session_dir))
        first_bytes = self.archive_path.read_bytes()
        self.assertTrue(archive_session(self.session_dir))
        self.assertEqual(self.archive_path.read_bytes(), first_bytes)
        self.assertFalse(self.temporary_path.exists())

    def test_existing_verified_archive_can_remove_remaining_raw(self):
        self.assertTrue(archive_session(self.session_dir))
        self.assertTrue(archive_session(self.session_dir, delete_raw=True))
        self.assertFalse(self.adsb_path.exists())
        self.assertFalse(self.mlat_path.exists())
        self.assertTrue(archive_session(self.session_dir, delete_raw=True))


if __name__ == "__main__":
    unittest.main()
