import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytz

from replay_server import (
    ADSB_PORT,
    MLAT_PORT,
    DualReplayServer,
    DualScenario,
    ReplayPacer,
    ReplayServer,
    Scenario,
    logged_timestamp,
    merge_logged_streams,
    message_timestamp,
    replay_dual_streams,
    replay_lines,
)


LINES = [
    "MSG,3,1,1,ABC001,1,2026/08/16,12:00:00.000,2026/08/16,12:00:00.010,,10000,,,51.0,21.0\n",
    "MSG,3,1,1,ABC002,1,2026/08/16,12:00:00.100,2026/08/16,12:00:00.110,,11000,,,52.0,22.0\n",
]


def line(icao, generated, logged):
    return f"MSG,3,1,1,{icao},1,2026/08/16,{generated},2026/08/16,{logged},,10000,,,51.0,21.0\n"


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

    def test_logged_timestamp_uses_shared_port_conversion(self):
        value = line("ABC001", "12:00:00.000", "12:00:00.010")
        converted = datetime(2026, 8, 16, 10, 0, tzinfo=pytz.utc)
        with patch("replay_server.port_timestamp_to_utc", return_value=converted) as converter:
            self.assertEqual(logged_timestamp(value, ADSB_PORT), converted)
        parsed, port = converter.call_args.args
        self.assertEqual(parsed, datetime(2026, 8, 16, 12, 0, 0, 10000))
        self.assertEqual(port, ADSB_PORT)

    def test_ports_keep_their_distinct_timestamp_semantics(self):
        value = line("ABC001", "12:00:00.000", "12:00:00.010")
        adsb = logged_timestamp(value, ADSB_PORT)
        mlat = logged_timestamp(value, MLAT_PORT)
        self.assertEqual(adsb - mlat, timedelta(hours=time.altzone / 60 / 60))
        self.assertEqual(adsb.utcoffset(), timedelta(0))
        self.assertEqual(mlat.utcoffset(), timedelta(0))


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


class DualStreamTests(unittest.TestCase):
    def test_merge_interleaves_ties_and_continues_after_early_eof(self):
        adsb = [
            line("A00001", "12:00:00.000", "12:00:00.000"),
            line("A00002", "12:00:01.000", "12:00:02.000"),
        ]
        mlat = [
            line("B00001", "10:00:00.000", "10:00:00.000"),
            line("B00002", "10:00:01.000", "10:00:01.000"),
            line("B00003", "10:00:03.000", "10:00:03.000"),
        ]
        merged = [(port, item.split(",")[4]) for _, port, item in merge_logged_streams(adsb, mlat)]
        self.assertEqual(merged, [
            (ADSB_PORT, "A00001"),
            (MLAT_PORT, "B00001"),
            (MLAT_PORT, "B00002"),
            (ADSB_PORT, "A00002"),
            (MLAT_PORT, "B00003"),
        ])

    def test_merge_is_streaming_with_one_pending_record_per_source(self):
        consumed = {ADSB_PORT: 0, MLAT_PORT: 0}

        def source(port, prefix):
            for index in range(1000):
                consumed[port] += 1
                yield line(f"{prefix}{index:05d}", "12:00:00.000", f"10:00:{index // 1000:02d}.{index % 1000:03d}")

        merged = merge_logged_streams(source(ADSB_PORT, "A"), source(MLAT_PORT, "B"))
        next(merged)
        self.assertLessEqual(consumed[ADSB_PORT], 2)
        self.assertLessEqual(consumed[MLAT_PORT], 2)

    def test_first_last_and_internal_order_are_preserved(self):
        adsb = [line("A00001", "12:00:00.000", "12:00:00.000"),
                line("A00002", "12:00:01.000", "12:00:02.000")]
        mlat = [line("B00001", "10:00:01.000", "10:00:01.000"),
                line("B00002", "10:00:03.000", "10:00:03.000")]
        pairs = [socket.socketpair(), socket.socketpair()]
        senders = {ADSB_PORT: pairs[0][0], MLAT_PORT: pairs[1][0]}
        try:
            self.assertEqual(replay_dual_streams(adsb, mlat, senders, None, threading.Event()), 4)
            for sender in senders.values():
                sender.shutdown(socket.SHUT_WR)
            self.assertEqual(receive_all(pairs[0][1]), "".join(adsb).replace("\n", "\r\n").encode())
            self.assertEqual(receive_all(pairs[1][1]), "".join(mlat).replace("\n", "\r\n").encode())
        finally:
            for pair in pairs:
                for sock in pair:
                    sock.close()

    def test_all_pacing_modes_use_one_pacer(self):
        streams = ([line("A00001", "12:00:00.000", "12:00:00.000")],
                   [line("B00001", "10:00:01.000", "10:00:01.000")])
        for speed in (1.0, 10.0, 100.0, None):
            with self.subTest(speed=speed), patch("replay_server.ReplayPacer") as pacer_type:
                pacer_type.return_value.pace.return_value = False
                clients = {ADSB_PORT: unittest.mock.Mock(), MLAT_PORT: unittest.mock.Mock()}
                replay_dual_streams(*streams, clients, speed, threading.Event())
                if speed is None:
                    pacer_type.assert_not_called()
                else:
                    pacer_type.assert_called_once()
                    self.assertEqual(pacer_type.return_value.pace.call_count, 2)


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


class DualServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.adsb_lines = [line("A00001", "12:00:00.000", "12:00:00.000")]
        self.mlat_lines = [line("B00001", "10:00:01.000", "10:00:01.000")]
        adsb_path, mlat_path = root / "adsb.log", root / "mlat.log"
        adsb_path.write_text("".join(self.adsb_lines), encoding="utf-8")
        mlat_path.write_text("".join(self.mlat_lines), encoding="utf-8")
        self.adsb_port, self.mlat_port = free_port(), free_port()
        self.server = DualReplayServer(
            DualScenario("dual-test", adsb_path, mlat_path), None,
            ports=(self.adsb_port, self.mlat_port),
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.temp.cleanup()

    def test_waits_for_both_clients_and_serves_both_ports(self):
        adsb = socket.create_connection(("127.0.0.1", self.adsb_port), timeout=2)
        adsb.settimeout(0.15)
        with self.assertRaises(socket.timeout):
            adsb.recv(1)
        mlat = socket.create_connection(("127.0.0.1", self.mlat_port), timeout=2)
        adsb.settimeout(2)
        mlat.settimeout(2)
        try:
            self.assertEqual(adsb.makefile("rb").readline(), self.adsb_lines[0].replace("\n", "\r\n").encode())
            self.assertEqual(mlat.makefile("rb").readline(), self.mlat_lines[0].replace("\n", "\r\n").encode())
        finally:
            adsb.close()
            mlat.close()


if __name__ == "__main__":
    unittest.main()
