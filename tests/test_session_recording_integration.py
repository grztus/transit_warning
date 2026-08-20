import io
import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch
import zipfile

import transit_warning as transit
from recording import RecordingStatus, SessionRecorder

from tests.test_transit_warning_config import TEST_CONFIG


class FakeSocket:
    def __init__(self, lines):
        self.file = io.StringIO("".join(lines))

    def connect(self, endpoint):
        self.endpoint = endpoint

    def makefile(self):
        return self.file

    def shutdown(self, how):
        pass

    def close(self):
        pass


class BlockingSocket:
    def __init__(self):
        self.readline_entered = threading.Event()
        self.closed = threading.Event()

    def connect(self, endpoint):
        self.endpoint = endpoint

    def makefile(self):
        return self

    def readline(self):
        self.readline_entered.set()
        self.closed.wait(2)
        return ""

    def shutdown(self, how):
        self.closed.set()

    def close(self):
        self.closed.set()


class StopAfterLinesSocket:
    def __init__(self, lines):
        self.lines = iter(lines)

    def connect(self, endpoint):
        self.endpoint = endpoint

    def makefile(self):
        return self

    def readline(self):
        try:
            return next(self.lines)
        except StopIteration:
            transit.stop_event.set()
            return ""

    def close(self):
        pass


class ReadFromPortRecordingTests(unittest.TestCase):
    def run_reader(self, sockets, recorder, processor, port=30003):
        transit.stop_event.clear()
        socket_factory = Mock(side_effect=list(sockets) + [KeyboardInterrupt()])
        with patch.object(transit.socket, "socket", socket_factory):
            with self.assertRaises(KeyboardInterrupt):
                transit.read_from_port("receiver", port, processor, recorder)

    def test_records_original_adsb_text_before_processing(self):
        recorder = Mock()
        processor = Mock()
        line = "MSG,3,ADS-B,with spaces  \r\n"
        self.run_reader([FakeSocket([line])], recorder, processor)
        recorder.record_line.assert_called_once_with(30003, line)
        processor.assert_called_once_with(line.strip(), 30003)

    def test_routes_adsb_and_mlat_without_mixing(self):
        recorder = Mock()
        adsb_processor = Mock()
        mlat_processor = Mock()
        self.run_reader([FakeSocket(["ADS-B\n"])], recorder, adsb_processor, 30003)
        self.run_reader([FakeSocket(["MLAT\n"])], recorder, mlat_processor, 30106)
        self.assertEqual(recorder.record_line.call_args_list, [
            call(30003, "ADS-B\n"), call(30106, "MLAT\n")])

    def test_reconnect_uses_the_same_recorder(self):
        recorder = Mock()
        processor = Mock()
        self.run_reader(
            [FakeSocket(["first\n"]), FakeSocket(["second\n"])],
            recorder, processor)
        self.assertEqual(recorder.record_line.call_args_list, [
            call(30003, "first\n"), call(30003, "second\n")])

    def test_unexpected_recorder_error_is_fail_open(self):
        recorder = Mock()
        recorder.record_line.side_effect = OSError("disk failed")
        processor = Mock()
        self.run_reader([FakeSocket(["line\n"])], recorder, processor)
        processor.assert_called_once_with("line", 30003)

    def test_invalid_msg3_altitude_does_not_reconnect_healthy_socket(self):
        transit.apply_installation_config(TEST_CONFIG)
        invalid = (
            "MSG,3,1,1,BADALT,1,2024/05/18,12:00:00.000,"
            "2024/05/18,12:00:00.000,,,180,,51.2,21.2\n"
        )
        following = "NEXT LINE\n"
        sock = StopAfterLinesSocket([invalid, following])
        socket_factory = Mock(return_value=sock)
        processed = []

        def processor(line, port):
            processed.append(line)
            if len(processed) == 1:
                transit.process_line(line, port)

        transit.stop_event.clear()
        with patch.object(transit.socket, "socket", socket_factory):
            transit.read_from_port(
                "receiver", TEST_CONFIG.adsb_port, processor, None)
        transit.stop_event.clear()

        self.assertEqual(processed, [invalid.strip(), following.strip()])
        self.assertEqual(socket_factory.call_count, 1)

    def test_shutdown_closes_socket_and_wakes_blocking_readline(self):
        transit.stop_event.clear()
        blocking_socket = BlockingSocket()
        socket_factory = Mock(return_value=blocking_socket)
        worker = threading.Thread(
            target=transit.read_from_port,
            args=("receiver", 30003, Mock(), None),
        )
        with patch.object(transit.socket, "socket", socket_factory):
            worker.start()
            self.assertTrue(blocking_socket.readline_entered.wait(1))
            transit.stop_event.set()
            transit.close_active_sockets()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(blocking_socket.closed.is_set())
        self.assertEqual(socket_factory.call_count, 1)

    def test_set_stop_event_prevents_reconnect(self):
        transit.stop_event.set()
        socket_factory = Mock()
        with patch.object(transit.socket, "socket", socket_factory):
            transit.read_from_port("receiver", 30003, Mock(), None)
        socket_factory.assert_not_called()


class MainSessionRecordingTests(unittest.TestCase):
    def setUp(self):
        self.original_recorder = transit.session_recorder
        self.original_requested = transit.session_recording_requested
        self.original_stop_state = transit.stop_event.is_set()
        transit.stop_event.clear()

    def tearDown(self):
        transit.session_recorder = self.original_recorder
        transit.session_recording_requested = self.original_requested
        if self.original_stop_state:
            transit.stop_event.set()
        else:
            transit.stop_event.clear()

    def main_patches(self, record, sleep_effect=KeyboardInterrupt()):
        runtime_args = Mock(
            record=record, environment_replay=None, environment_record=None)
        return (
            patch.object(transit, "runtime_args", runtime_args),
            patch.object(transit, "load_installation_config", return_value=TEST_CONFIG),
            patch.object(transit, "initialize_daily_environment"),
            patch.object(transit, "get_metar_press"),
            patch.object(transit.threading, "Thread", return_value=Mock()),
            patch.object(transit.time, "sleep", side_effect=sleep_effect),
        )

    def test_record_creates_one_session_before_threads(self):
        order = []
        recorder = Mock()
        recorder.adsb_writer.status = RecordingStatus.RECORDING
        recorder.mlat_writer.status = RecordingStatus.RECORDING
        beast = Mock()
        beast.status = RecordingStatus.RECORDING

        def create_recorder(*args, **kwargs):
            order.append("session")
            return recorder

        def create_thread(*args, **kwargs):
            order.append("thread")
            return Mock()

        def create_beast(*args, **kwargs):
            order.append("beast")
            return beast

        patches = self.main_patches(True)
        with patches[0], patches[1], patches[2], patches[3], \
                patch.object(transit, "SessionRecorder", side_effect=create_recorder) as factory, \
                patch.object(transit, "BeastRecorder", side_effect=create_beast) as beast_factory, \
                patch.object(transit, "archive_session", return_value=True), \
                patch.object(transit.threading, "Thread", side_effect=create_thread), patches[5]:
            transit.main()

        self.assertEqual(order, ["session", "beast", "thread", "thread", "thread"])
        factory.assert_called_once()
        beast_factory.assert_called_once_with(
            recorder.session_dir, TEST_CONFIG.beast_host, TEST_CONFIG.beast_port,
            error_handler=unittest.mock.ANY)
        args = factory.call_args.args
        self.assertEqual(args[1:], (
            TEST_CONFIG.adsb_port, TEST_CONFIG.mlat_port,
            TEST_CONFIG.adsb_timestamp_timezone))

    def test_main_loop_flushes_active_session(self):
        recorder = Mock()
        patches = self.main_patches(True, [None, KeyboardInterrupt()])
        with patches[0], patches[1], patches[2], patches[3], \
                patch.object(transit, "SessionRecorder", return_value=recorder), \
                patch.object(transit, "archive_session", return_value=True), \
                patches[4], patches[5]:
            transit.main()
        recorder.flush_if_due.assert_called_once_with()
        recorder.close.assert_called_once()

    def test_ctrl_c_closes_recorder_with_current_utc_and_joins_threads(self):
        recorder = Mock()
        recorder.manifest_data.return_value = {"recording_status": "complete"}
        ended_at = datetime.datetime(
            2026, 8, 18, 20, 0, tzinfo=datetime.timezone.utc)
        threads = [Mock(), Mock()]
        patches = self.main_patches(True)
        with patches[0], patches[1], patches[2], patches[3], \
                patch.object(transit, "SessionRecorder", return_value=recorder), \
                patch.object(transit.threading, "Thread", side_effect=threads), \
                patch.object(transit.clock, "now_utc", return_value=ended_at), \
                patch.object(transit, "archive_session", return_value=True) as archive, \
                patches[5]:
            transit.main()
        recorder.close.assert_called_once_with(ended_at)
        archive.assert_called_once_with(
            recorder.session_dir, delete_raw=True,
            error_handler=unittest.mock.ANY)
        for thread in threads:
            thread.join.assert_called_once_with(timeout=2.0)

    def test_shutdown_is_idempotent(self):
        transit.stop_event.clear()
        transit.shutdown_complete = False
        recorder = Mock()
        thread = Mock()
        with patch.object(transit, "archive_session", return_value=True):
            transit.shutdown_runtime([thread], recorder)
            transit.shutdown_runtime([thread], recorder)
        thread.join.assert_called_once_with(timeout=2.0)
        recorder.close.assert_called_once()

    def test_session_creation_error_is_fail_open(self):
        patches = self.main_patches(True)
        with patches[0], patches[1], patches[2], patches[3], \
                patch.object(transit, "SessionRecorder", side_effect=OSError("denied")), \
                patches[4] as thread, patches[5]:
            transit.main()
        self.assertEqual(thread.call_count, 2)
        self.assertEqual(transit.session_recorder_statuses(), ("FAILED", "FAILED"))

    def test_without_record_keeps_both_statuses_off(self):
        patches = self.main_patches(False)
        with patches[0], patches[1], patches[2], patches[3], \
                patch.object(transit, "SessionRecorder") as factory, \
                patch.object(transit, "archive_session") as archive, \
                patches[4], patches[5]:
            transit.main()
        factory.assert_not_called()
        archive.assert_not_called()
        self.assertEqual(transit.session_recorder_statuses(), ("OFF", "OFF"))

    def test_writer_statuses_are_independent(self):
        transit.session_recording_requested = True
        transit.session_recorder = Mock()
        transit.session_recorder.adsb_writer.status = RecordingStatus.FAILED
        transit.session_recorder.mlat_writer.status = RecordingStatus.RECORDING
        self.assertEqual(
            transit.session_recorder_statuses(), ("FAILED", "RECORDING"))

    def test_combined_source_status_lines_without_recording(self):
        transit.session_recording_requested = False
        with patch.object(transit, "adsb_port", 30003), \
                patch.object(transit, "mlat_port", 30106), \
                patch.object(transit, "port_status", {30003: True, 30106: True}):
            self.assertEqual(transit.source_status_lines(), (
                "ADS-B  Port 30003: Listening  |  Recorder: OFF",
                "MLAT   Port 30106: Listening  |  Recorder: OFF",
            ))

    def test_combined_source_status_lines_keep_independent_failures(self):
        transit.session_recording_requested = True
        transit.session_recorder = Mock()
        transit.session_recorder.adsb_writer.status = RecordingStatus.FAILED
        transit.session_recorder.mlat_writer.status = RecordingStatus.RECORDING
        with patch.object(transit, "adsb_port", 30003), \
                patch.object(transit, "mlat_port", 30106), \
                patch.object(transit, "port_status", {30003: True, 30106: False}):
            self.assertEqual(transit.source_status_lines(), (
                "ADS-B  Port 30003: Listening  |  Recorder: FAILED",
                "MLAT   Port 30106: Not listening  |  Recorder: RECORDING",
            ))


class AutomaticSessionArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp.name) / "sessions"
        transit.stop_event.clear()
        transit.shutdown_complete = False

    def tearDown(self):
        transit.stop_event.clear()
        transit.shutdown_complete = False
        self.temp.cleanup()

    def make_recorder(self, second=0):
        started = datetime.datetime(
            2026, 8, 18, 20, 0, second, tzinfo=datetime.timezone.utc)
        recorder = SessionRecorder(
            started, 30003, 30106, "Europe/Warsaw", self.base_dir)
        recorder.record_line(30003, "ADS-B {}\n".format(second))
        recorder.record_line(30106, "MLAT {}\n".format(second))
        return recorder, started

    def shutdown(self, recorder, ended_at):
        with patch.object(transit.clock, "now_utc", return_value=ended_at), \
                patch("builtins.print"):
            transit.shutdown_runtime([], recorder)

    def test_complete_archive_runs_after_close_and_uses_delete_raw_true(self):
        recorder = Mock()
        recorder.session_dir = Path("current-session")
        recorder.manifest_data.return_value = {"recording_status": "complete"}
        ended_at = datetime.datetime(
            2026, 8, 18, 21, 0, tzinfo=datetime.timezone.utc)
        order = []
        recorder.close.side_effect = lambda value: order.append(("close", value))

        def archive_side_effect(*args, **kwargs):
            order.append(("archive", args[0], kwargs["delete_raw"]))
            return True

        with patch.object(transit.clock, "now_utc", return_value=ended_at), \
                patch.object(transit, "archive_session", side_effect=archive_side_effect), \
                patch("builtins.print"):
            transit.shutdown_runtime([], recorder)
        self.assertEqual(order, [
            ("close", ended_at),
            ("archive", recorder.session_dir, True),
        ])

    def test_complete_leaves_only_manifest_and_zip_with_both_full_streams(self):
        previous, _ = self.make_recorder(0)
        previous.close(datetime.datetime(
            2026, 8, 18, 20, 0, 1, tzinfo=datetime.timezone.utc))
        current, ended_at = self.make_recorder(2)
        environment_dir = Path(self.temp.name) / "environment"
        environment_dir.mkdir()
        environment_path = environment_dir / "environment_20260818.jsonl"
        environment_path.write_text(
            '{"type":"qnh"}\n', encoding="utf-8")

        self.shutdown(current, ended_at)

        self.assertFalse((previous.session_dir / "streams.zip").exists())
        archive_path = current.session_dir / "streams.zip"
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(set(archive.namelist()), {
                "adsb_30003.log", "mlat_30106.log"})
            self.assertEqual(archive.read("adsb_30003.log"), b"ADS-B 2\n")
            self.assertEqual(archive.read("mlat_30106.log"), b"MLAT 2\n")
        self.assertEqual(
            {path.name for path in current.session_dir.iterdir()},
            {"manifest.json", "streams.zip"})
        self.assertEqual(
            environment_path.read_text(encoding="utf-8"), '{"type":"qnh"}\n')

    def test_archive_ignores_and_preserves_beast_sidecars(self):
        recorder, ended_at = self.make_recorder(5)
        beast = recorder.session_dir / "beast.bin"
        timing = recorder.session_dir / "beast_timing.jsonl"
        beast.write_bytes(b"\x1a3raw")
        timing.write_text('{"version":1}\n', encoding="utf-8")

        self.shutdown(recorder, ended_at)

        with zipfile.ZipFile(recorder.session_dir / "streams.zip") as archive:
            self.assertEqual(set(archive.namelist()), {
                "adsb_30003.log", "mlat_30106.log"})
        self.assertEqual(beast.read_bytes(), b"\x1a3raw")
        self.assertEqual(timing.read_text(encoding="utf-8"), '{"version":1}\n')

    def test_partial_session_archives_without_deleting_raw(self):
        recorder, ended_at = self.make_recorder(3)
        recorder.adsb_writer._fail("write", OSError("failed"))
        with patch.object(transit, "archive_session", wraps=transit.archive_session) as archive:
            self.shutdown(recorder, ended_at)
        archive.assert_called_once_with(
            recorder.session_dir, delete_raw=False,
            error_handler=unittest.mock.ANY)
        self.assertEqual(recorder.manifest_data()["recording_status"], "partial")
        self.assertTrue((recorder.session_dir / "adsb_30003.log").exists())
        self.assertTrue((recorder.session_dir / "mlat_30106.log").exists())
        self.assertTrue((recorder.session_dir / "streams.zip").exists())

    def test_failed_session_archives_without_deleting_raw(self):
        recorder, ended_at = self.make_recorder(4)
        recorder._session_failed = True
        with patch.object(transit, "archive_session", wraps=transit.archive_session) as archive:
            self.shutdown(recorder, ended_at)
        archive.assert_called_once_with(
            recorder.session_dir, delete_raw=False,
            error_handler=unittest.mock.ANY)
        self.assertEqual(recorder.manifest_data()["recording_status"], "failed")
        self.assertTrue((recorder.session_dir / "adsb_30003.log").exists())
        self.assertTrue((recorder.session_dir / "mlat_30106.log").exists())
        self.assertTrue((recorder.session_dir / "streams.zip").exists())

    def test_consecutive_sessions_get_independent_archives(self):
        first, first_end = self.make_recorder(10)
        self.shutdown(first, first_end)
        transit.shutdown_complete = False
        transit.stop_event.clear()
        second, second_end = self.make_recorder(20)
        self.shutdown(second, second_end)

        first_zip = first.session_dir / "streams.zip"
        second_zip = second.session_dir / "streams.zip"
        self.assertTrue(first_zip.exists())
        self.assertTrue(second_zip.exists())
        self.assertNotEqual(first_zip, second_zip)
        with zipfile.ZipFile(first_zip) as archive:
            self.assertEqual(archive.read("adsb_30003.log"), b"ADS-B 10\n")
        with zipfile.ZipFile(second_zip) as archive:
            self.assertEqual(archive.read("adsb_30003.log"), b"ADS-B 20\n")

    def test_archive_failure_is_fail_open_and_manifest_stays_complete(self):
        recorder, ended_at = self.make_recorder(30)
        with patch.object(transit.clock, "now_utc", return_value=ended_at), \
                patch.object(transit, "archive_session", return_value=False) as archive, \
                patch("builtins.print") as output:
            transit.shutdown_runtime([], recorder)

        manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["recording_status"], "complete")
        self.assertTrue((recorder.session_dir / "adsb_30003.log").exists())
        self.assertTrue((recorder.session_dir / "mlat_30106.log").exists())
        self.assertFalse((recorder.session_dir / "streams.zip").exists())
        archive.assert_called_once_with(
            recorder.session_dir, delete_raw=True,
            error_handler=unittest.mock.ANY)
        output.assert_any_call("Session archive: FAILED (unknown error)")


if __name__ == "__main__":
    unittest.main()
