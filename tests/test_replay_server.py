import socket
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from replay_server import ReplayPacer, ReplayServer, Scenario, message_timestamp, replay_lines


LINES = [
    "MSG,3,1,1,ABC001,1,2026/08/16,12:00:00.000,2026/08/16,12:00:00.010,,10000,,,51.0,21.0\n",
    "MSG,3,1,1,ABC002,1,2026/08/16,12:00:00.100,2026/08/16,12:00:00.110,,11000,,,52.0,22.0\n",
]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def receive_all(sock):
    chunks = []
    while True:
        data = sock.recv(4096)
        if not data:
            return b"".join(chunks)
        chunks.append(data)


class TimestampTests(unittest.TestCase):
    def test_uses_generated_timestamp_fields(self):
        self.assertEqual(message_timestamp(LINES[0]), datetime(2026, 8, 16, 12, 0, 0))

    def test_rejects_short_message(self):
        with self.assertRaises(ValueError):
            message_timestamp("MSG,3")


class PacerTests(unittest.TestCase):
    def test_all_paced_speeds_and_backward_timestamp(self):
        for speed in (1, 10, 100):
            with self.subTest(speed=speed):
                now = [20.0]
                waits = []

                def wait(delay):
                    waits.append(delay)
                    now[0] += delay
                    return False

                pacer = ReplayPacer(speed, threading.Event(), lambda: now[0], wait)
                pacer.pace(datetime(2026, 1, 1, 0, 0, 0))
                pacer.pace(datetime(2026, 1, 1, 0, 0, 1))
                pacer.pace(datetime(2026, 1, 1, 0, 0, 0, 500000))
                self.assertAlmostEqual(waits[0], 1 / speed)
                self.assertEqual(pacer.backward_timestamps, 1)


class StreamTests(unittest.TestCase):
    def test_max_mode_preserves_order_and_normalizes_line_endings(self):
        sender, receiver = socket.socketpair()
        try:
            count, backwards = replay_lines(iter(LINES), sender, None, threading.Event())
            sender.shutdown(socket.SHUT_WR)
            self.assertEqual(receive_all(receiver), "".join(LINES).replace("\n", "\r\n").encode())
            self.assertEqual((count, backwards), (2, 0))
        finally:
            sender.close()
            receiver.close()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "recording.log"
        self.path.write_text("".join(LINES), encoding="utf-8")
        self.active_port, self.silent_port = free_port(), free_port()
        self.scenario = Scenario("test", self.active_port, self.silent_port, self.path)
        self.server = ReplayServer(self.scenario, self.path, None)
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.temp.cleanup()

    def test_active_stream_and_silent_companion_port(self):
        silent = socket.create_connection(("127.0.0.1", self.silent_port), timeout=2)
        silent.settimeout(0.15)
        with self.assertRaises(socket.timeout):
            silent.recv(1)

        with socket.create_connection(("127.0.0.1", self.active_port), timeout=2) as active:
            expected = "".join(LINES).replace("\n", "\r\n").encode()
            received = b""
            while len(received) < len(expected):
                received += active.recv(4096)
            self.assertEqual(received, expected)
        silent.close()

    def test_reconnection_restarts_recording_safely(self):
        expected_first = LINES[0].replace("\n", "\r\n").encode()
        for _ in range(2):
            with socket.create_connection(("127.0.0.1", self.active_port), timeout=2) as active:
                active_file = active.makefile("rb")
                self.assertEqual(active_file.readline(), expected_first)


if __name__ == "__main__":
    unittest.main()
