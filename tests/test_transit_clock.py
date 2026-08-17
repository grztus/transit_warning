import datetime
import threading
import unittest

import ephem
import pytz

import transit_warning as transit
from transit_clock import RealClock, ReplayClock, clock_from_args


class ClockSelectionTests(unittest.TestCase):
    def test_default_mode_is_real_clock(self):
        self.assertIsInstance(clock_from_args([]), RealClock)

    def test_replay_mode_is_not_ready(self):
        clock = clock_from_args(["--clock", "replay"])
        self.assertIsInstance(clock, ReplayClock)
        self.assertFalse(clock.is_ready())

    def test_environment_replay_argument_requires_replay_clock(self):
        with self.assertRaises(SystemExit):
            transit.parse_runtime_args(["--environment-replay", "environment.jsonl"])

    def test_environment_replay_argument_is_accepted_with_replay_clock(self):
        args = transit.parse_runtime_args(
            ["--clock", "replay", "--environment-replay", "environment.jsonl"]
        )
        self.assertEqual(args.environment_replay, "environment.jsonl")

    def test_environment_record_argument_is_accepted_with_real_clock(self):
        args = transit.parse_runtime_args(["--environment-record", "environment.jsonl"])
        self.assertEqual(args.environment_record, "environment.jsonl")

    def test_environment_record_argument_rejects_replay_clock(self):
        with self.assertRaises(SystemExit):
            transit.parse_runtime_args(
                ["--clock", "replay", "--environment-record", "environment.jsonl"]
            )

    def test_environment_record_and_replay_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            transit.parse_runtime_args([
                "--clock", "replay",
                "--environment-replay", "replay.jsonl",
                "--environment-record", "record.jsonl",
            ])

    def test_record_argument_is_accepted_with_real_clock(self):
        self.assertTrue(transit.parse_runtime_args(["--record"]).record)

    def test_record_argument_rejects_replay_clock(self):
        with self.assertRaises(SystemExit):
            transit.parse_runtime_args(["--clock", "replay", "--record"])


class ReplayClockTests(unittest.TestCase):
    def setUp(self):
        self.clock = ReplayClock()
        self.first = datetime.datetime(2024, 5, 18, 12, 0, tzinfo=pytz.utc)

    def test_uninitialized_clock_rejects_reads(self):
        with self.assertRaises(RuntimeError):
            self.clock.now_utc()
        with self.assertRaises(RuntimeError):
            self.clock.ephem_now()

    def test_first_utc_timestamp_initializes_both_views(self):
        self.clock.advance_to(self.first)
        self.assertTrue(self.clock.is_ready())
        self.assertEqual(self.clock.now_utc(), self.first)
        self.assertEqual(ephem.Date(self.clock.now_utc()), self.clock.ephem_now())

    def test_rejects_naive_and_non_utc_timestamps(self):
        with self.assertRaises(ValueError):
            self.clock.advance_to(self.first.replace(tzinfo=None))
        non_utc = self.first.astimezone(datetime.timezone(datetime.timedelta(hours=2)))
        with self.assertRaises(ValueError):
            self.clock.advance_to(non_utc)

    def test_moves_only_forward_and_ignores_equal_or_older_values(self):
        later = self.first + datetime.timedelta(seconds=2)
        self.clock.advance_to(self.first)
        self.clock.advance_to(later)
        self.clock.advance_to(later)
        self.clock.advance_to(self.first)
        self.assertEqual(self.clock.now_utc(), later)

    def test_concurrent_updates_keep_latest_timestamp(self):
        timestamps = [self.first + datetime.timedelta(milliseconds=value) for value in range(100)]
        threads = [threading.Thread(target=self.clock.advance_to, args=(timestamp,)) for timestamp in reversed(timestamps)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.clock.now_utc(), timestamps[-1])


if __name__ == "__main__":
    unittest.main()
