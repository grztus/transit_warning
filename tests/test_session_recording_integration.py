import io
import unittest
from unittest.mock import Mock, call, patch

import transit_warning as transit
from recording import RecordingStatus

from tests.test_transit_warning_config import TEST_CONFIG


class FakeSocket:
    def __init__(self, lines):
        self.file = io.StringIO("".join(lines))

    def connect(self, endpoint):
        self.endpoint = endpoint

    def makefile(self):
        return self.file


class ReadFromPortRecordingTests(unittest.TestCase):
    def run_reader(self, sockets, recorder, processor, port=30003):
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


class MainSessionRecordingTests(unittest.TestCase):
    def setUp(self):
        self.original_recorder = transit.session_recorder
        self.original_requested = transit.session_recording_requested

    def tearDown(self):
        transit.session_recorder = self.original_recorder
        transit.session_recording_requested = self.original_requested

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

        def create_recorder(*args, **kwargs):
            order.append("session")
            return recorder

        def create_thread(*args, **kwargs):
            order.append("thread")
            return Mock()

        patches = self.main_patches(True)
        with patches[0], patches[1], patches[2], patches[3], \
                patch.object(transit, "SessionRecorder", side_effect=create_recorder) as factory, \
                patch.object(transit.threading, "Thread", side_effect=create_thread), patches[5]:
            with self.assertRaises(KeyboardInterrupt):
                transit.main()

        self.assertEqual(order, ["session", "thread", "thread"])
        factory.assert_called_once()
        args = factory.call_args.args
        self.assertEqual(args[1:], (
            TEST_CONFIG.adsb_port, TEST_CONFIG.mlat_port,
            TEST_CONFIG.adsb_timestamp_timezone))

    def test_main_loop_flushes_active_session(self):
        recorder = Mock()
        patches = self.main_patches(True, [None, KeyboardInterrupt()])
        with patches[0], patches[1], patches[2], patches[3], \
                patch.object(transit, "SessionRecorder", return_value=recorder), \
                patches[4], patches[5]:
            with self.assertRaises(KeyboardInterrupt):
                transit.main()
        recorder.flush_if_due.assert_called_once_with()

    def test_session_creation_error_is_fail_open(self):
        patches = self.main_patches(True)
        with patches[0], patches[1], patches[2], patches[3], \
                patch.object(transit, "SessionRecorder", side_effect=OSError("denied")), \
                patches[4] as thread, patches[5]:
            with self.assertRaises(KeyboardInterrupt):
                transit.main()
        self.assertEqual(thread.call_count, 2)
        self.assertEqual(transit.session_recorder_statuses(), ("FAILED", "FAILED"))

    def test_without_record_keeps_both_statuses_off(self):
        patches = self.main_patches(False)
        with patches[0], patches[1], patches[2], patches[3], \
                patch.object(transit, "SessionRecorder") as factory, \
                patches[4], patches[5]:
            with self.assertRaises(KeyboardInterrupt):
                transit.main()
        factory.assert_not_called()
        self.assertEqual(transit.session_recorder_statuses(), ("OFF", "OFF"))

    def test_writer_statuses_are_independent(self):
        transit.session_recording_requested = True
        transit.session_recorder = Mock()
        transit.session_recorder.adsb_writer.status = RecordingStatus.FAILED
        transit.session_recorder.mlat_writer.status = RecordingStatus.RECORDING
        self.assertEqual(
            transit.session_recorder_statuses(), ("FAILED", "RECORDING"))


if __name__ == "__main__":
    unittest.main()
