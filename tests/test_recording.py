import io
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from recording import RecordingStatus, SessionRecorder, StreamWriter


UTC = timezone.utc
START = datetime(2026, 8, 17, 20, 44, 17, 502832, tzinfo=UTC)


class FailingWriteFile(io.StringIO):
    def write(self, value):
        raise OSError("disk full")


class StreamWriterTests(unittest.TestCase):
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
        self.assertEqual(manifest["mlat"]["timestamp_semantics"], "utc")
        self.assertNotIn("timestamp_timezone", manifest["mlat"])

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
        self.assertEqual(manifest["adsb"]["lines_written"], 2)
        self.assertEqual(manifest["mlat"]["lines_written"], 1)
        self.assertEqual(manifest["recording_status"], "complete")

    def test_one_failed_writer_does_not_stop_the_other(self):
        self.recorder.adsb_writer._file.close()
        self.recorder.adsb_writer._file = FailingWriteFile()
        self.assertFalse(self.recorder.record_line(30003, "ADS-B\n"))
        self.assertEqual(self.recorder.adsb_writer.status, RecordingStatus.FAILED)
        self.assertTrue(self.recorder.record_line(30106, "MLAT\n"))
        self.assertEqual(self.recorder.mlat_writer.status, RecordingStatus.RECORDING)
        self.assertEqual(self.recorder.manifest_data()["recording_status"], "partial")

    def test_unknown_port_is_ignored(self):
        self.assertFalse(self.recorder.record_line(39999, "UNKNOWN\n"))
        self.assertEqual(self.recorder.adsb_writer.lines_written, 0)
        self.assertEqual(self.recorder.mlat_writer.lines_written, 0)


if __name__ == "__main__":
    unittest.main()
