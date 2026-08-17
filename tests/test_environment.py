import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from environment import (
    EnvironmentEvent,
    EnvironmentFormatError,
    EnvironmentRecorder,
    EnvironmentRecordError,
    DailyEnvironmentRecorder,
    EnvironmentReplay,
    iter_environment_events,
)


VALID = {
    "version": 1,
    "time": "2026-08-16T10:04:22.315Z",
    "type": "qnh",
    "value_hpa": 1011.8,
    "source": "awc",
    "station": "EPRA",
    "obs_time": "2026-08-16T10:00:00Z",
}


class EnvironmentEventTests(unittest.TestCase):
    def events(self, records):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environment.jsonl"
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            return list(iter_environment_events(path))

    def test_parses_valid_qnh(self):
        event = self.events([VALID])[0]
        self.assertIsInstance(event, EnvironmentEvent)
        self.assertEqual(event.value_hpa, 1011.8)
        self.assertEqual(event.station, "EPRA")
        self.assertEqual(event.time.isoformat(), "2026-08-16T10:04:22.315000+00:00")
        self.assertEqual(event.obs_time.isoformat(), "2026-08-16T10:00:00+00:00")

    def test_reports_invalid_json_with_line_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environment.jsonl"
            path.write_text("{not json}\n", encoding="utf-8")
            with self.assertRaisesRegex(EnvironmentFormatError, r":1: invalid JSON"):
                list(iter_environment_events(path))

    def test_file_iterator_does_not_parse_later_lines_eagerly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environment.jsonl"
            path.write_text(json.dumps(VALID) + "\n{not json}\n", encoding="utf-8")
            events = iter_environment_events(path)
            self.assertEqual(next(events).value_hpa, 1011.8)
            with self.assertRaisesRegex(EnvironmentFormatError, r":2: invalid JSON"):
                next(events)

    def test_rejects_invalid_fields(self):
        cases = (
            ("version", 2),
            ("version", True),
            ("type", "temperature"),
            ("value_hpa", 800),
            ("value_hpa", 1100),
            ("value_hpa", float("inf")),
            ("time", "2026-08-16T10:04:22"),
            ("time", "not-a-time"),
            ("source", 123),
            ("station", "epra"),
            ("station", "EP1A"),
            ("obs_time", "2026-08-16T10:00:00+02:00"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaises(EnvironmentFormatError):
                    self.events([{**VALID, field: value}])

    def test_allows_optional_station_and_observation_time(self):
        record = dict(VALID)
        record.pop("station")
        record.pop("obs_time")
        event = self.events([record])[0]
        self.assertIsNone(event.station)
        self.assertIsNone(event.obs_time)

    def test_rejects_decreasing_timestamps(self):
        later = {**VALID, "time": "2026-08-16T10:05:00Z"}
        earlier = {**VALID, "time": "2026-08-16T10:04:59Z"}
        with self.assertRaisesRegex(EnvironmentFormatError, "earlier than the previous"):
            self.events([later, earlier])

    def test_equal_timestamps_are_allowed(self):
        self.assertEqual(len(self.events([VALID, VALID])), 2)

    def test_replay_cursor_consumes_only_one_event_ahead(self):
        consumed = []
        event_time = datetime(2026, 8, 16, 10, 4, 22, tzinfo=timezone.utc)

        def source():
            for index in range(1000):
                consumed.append(index)
                yield EnvironmentEvent(
                    1,
                    event_time,
                    "qnh",
                    1000.0,
                    "test",
                )

        replay = EnvironmentReplay(source())
        self.assertEqual(consumed, [0])
        before_first_event = datetime(2026, 8, 16, 10, 4, 21, tzinfo=timezone.utc)
        self.assertEqual(list(replay.pop_through(before_first_event)), [])
        self.assertEqual(consumed, [0])


class EnvironmentRecorderTests(unittest.TestCase):
    def event(self, timestamp, value=1013.0, source="fallback", obs_time=None):
        return EnvironmentEvent(
            1,
            datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
            "qnh",
            value,
            source,
            "EPRA",
            datetime.fromisoformat(obs_time.replace("Z", "+00:00")) if obs_time else None,
        )

    def test_creates_new_file_with_initial_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environment.jsonl"
            recorder = EnvironmentRecorder(path, self.event("2026-08-16T10:00:00Z"))
            recorder.close()

            events = list(iter_environment_events(path))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].value_hpa, 1013.0)
            self.assertEqual(events[0].source, "fallback")
            self.assertIsNone(events[0].obs_time)


class DailyEnvironmentRecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp.name) / "environment"
        self.now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, timestamp, value, source="awc", station="EPRA", obs_time=None):
        return EnvironmentEvent(
            1,
            datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
            "qnh",
            value,
            source,
            station,
            datetime.fromisoformat(obs_time.replace("Z", "+00:00")) if obs_time else None,
        )

    def path(self, day):
        return self.base_dir / "environment_{}.jsonl".format(day)

    def test_creates_file_for_current_utc_day(self):
        recorder = DailyEnvironmentRecorder(self.now, self.base_dir, "EPRA")
        self.assertEqual(recorder.status, recorder.RECORDING)
        self.assertTrue(self.path("20260817").is_file())
        self.assertEqual(
            recorder.next_midnight_utc,
            datetime(2026, 8, 18, tzinfo=timezone.utc),
        )
        recorder.close()

    def test_utc_day_selection_does_not_depend_on_local_timezone_setting(self):
        utc_time = datetime(2026, 8, 17, 23, 30, tzinfo=timezone.utc)
        results = []
        for zone in ("UTC", "Europe/Warsaw", "America/New_York"):
            directory = self.base_dir / zone.replace("/", "_")
            with patch.dict(os.environ, {"TZ": zone}):
                recorder = DailyEnvironmentRecorder(utc_time, directory)
                results.append((recorder._active_date, recorder.next_midnight_utc))
                recorder.close()
        self.assertEqual(results, [
            (datetime(2026, 8, 17).date(), datetime(2026, 8, 18, tzinfo=timezone.utc)),
        ] * 3)

    def test_records_first_qnh_and_deduplicates_same_decimal_value(self):
        recorder = DailyEnvironmentRecorder(self.now, self.base_dir)
        self.assertTrue(recorder.record_qnh(self.event("2026-08-17T12:01:00Z", 1004.25)))
        self.assertFalse(recorder.record_qnh(self.event(
            "2026-08-17T12:02:00Z", 1004.25,
            obs_time="2026-08-17T12:00:00Z",
        )))
        recorder.close()
        events = list(iter_environment_events(self.path("20260817")))
        self.assertEqual([event.value_hpa for event in events], [1004.25])

    def test_does_not_rotate_before_midnight(self):
        recorder = DailyEnvironmentRecorder(self.now, self.base_dir)
        before = datetime(2026, 8, 17, 23, 59, 59, 999000, tzinfo=timezone.utc)
        self.assertFalse(recorder.rotate_if_needed(before))
        self.assertFalse(self.path("20260818").exists())
        recorder.close()

    def test_rotates_exactly_at_midnight_with_carryover_metadata(self):
        recorder = DailyEnvironmentRecorder(self.now, self.base_dir)
        recorder.record_qnh(self.event(
            "2026-08-17T23:45:00Z", 1004.0, "awc", "EPRA",
            "2026-08-17T23:30:00Z",
        ))
        midnight = datetime(2026, 8, 18, tzinfo=timezone.utc)
        self.assertTrue(recorder.rotate_if_needed(midnight))
        carryover = list(iter_environment_events(self.path("20260818")))[0]
        self.assertEqual(carryover.time, midnight)
        self.assertEqual(carryover.value_hpa, 1004.0)
        self.assertEqual(carryover.source, "carryover")
        self.assertEqual(carryover.station, "EPRA")
        self.assertEqual(
            carryover.obs_time,
            datetime(2026, 8, 17, 23, 30, tzinfo=timezone.utc),
        )
        recorder.close()

    def test_carryover_precedes_awc_at_the_same_midnight(self):
        recorder = DailyEnvironmentRecorder(self.now, self.base_dir)
        recorder.record_qnh(self.event("2026-08-17T23:45:00Z", 1004.0))
        recorder.record_qnh(self.event("2026-08-18T00:00:00Z", 1005.5))
        recorder.close()
        events = list(iter_environment_events(self.path("20260818")))
        self.assertEqual([event.source for event in events], ["carryover", "awc"])
        self.assertEqual([event.value_hpa for event in events], [1004.0, 1005.5])
        self.assertEqual(events[0].time, events[1].time)

    def test_rotation_without_known_state_writes_fallback(self):
        recorder = DailyEnvironmentRecorder(self.now, self.base_dir, "EPRA")
        recorder.rotate_if_needed(datetime(2026, 8, 18, tzinfo=timezone.utc))
        event = list(iter_environment_events(self.path("20260818")))[0]
        self.assertEqual(event.value_hpa, 1013.0)
        self.assertEqual(event.source, "fallback")
        self.assertEqual(event.station, "EPRA")
        recorder.close()

    def test_restart_recovers_last_state_without_appending_fallback(self):
        first = DailyEnvironmentRecorder(self.now, self.base_dir)
        expected = self.event(
            "2026-08-17T12:01:00Z", 1004.5, "awc", "EPRA",
            "2026-08-17T12:00:00Z",
        )
        first.record_qnh(expected)
        first.close()
        original = self.path("20260817").read_text(encoding="utf-8")

        restarted = DailyEnvironmentRecorder(
            datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc), self.base_dir
        )
        self.assertEqual(restarted.last_qnh, 1004.5)
        self.assertEqual(restarted.last_event.station, "EPRA")
        self.assertEqual(restarted.last_event.obs_time, expected.obs_time)
        self.assertEqual(self.path("20260817").read_text(encoding="utf-8"), original)
        restarted.close()

    def test_recovers_previous_day_into_today_as_carryover(self):
        previous = DailyEnvironmentRecorder(
            datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc), self.base_dir
        )
        previous.record_qnh(self.event(
            "2026-08-16T23:30:00Z", 1006.5, "awc", "EPRA",
            "2026-08-16T23:00:00Z",
        ))
        previous.close()

        current = DailyEnvironmentRecorder(self.now, self.base_dir)
        recovered = current.recover_recent_qnh(self.now)
        self.assertEqual(recovered.value_hpa, 1006.5)
        self.assertEqual(recovered.source, "carryover")
        self.assertEqual(recovered.time, datetime(2026, 8, 17, tzinfo=timezone.utc))
        self.assertEqual(recovered.obs_time, datetime(2026, 8, 16, 23, tzinfo=timezone.utc))
        current.close()

    def test_does_not_search_older_than_previous_day(self):
        old = DailyEnvironmentRecorder(
            datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc), self.base_dir
        )
        old.record_qnh(self.event("2026-08-15T12:01:00Z", 1002.0))
        old.close()

        current = DailyEnvironmentRecorder(self.now, self.base_dir)
        self.assertIsNone(current.recover_recent_qnh(self.now))
        self.assertIsNone(current.last_event)
        current.close()

    def test_jump_over_multiple_days_opens_only_current_day(self):
        recorder = DailyEnvironmentRecorder(self.now, self.base_dir)
        recorder.record_qnh(self.event("2026-08-17T12:01:00Z", 1004.0))
        recorder.rotate_if_needed(datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc))
        self.assertFalse(self.path("20260818").exists())
        self.assertFalse(self.path("20260819").exists())
        self.assertTrue(self.path("20260820").exists())
        event = list(iter_environment_events(self.path("20260820")))[0]
        self.assertEqual(event.time, datetime(2026, 8, 20, tzinfo=timezone.utc))
        self.assertEqual(event.value_hpa, 1004.0)
        self.assertEqual(
            recorder.next_midnight_utc,
            datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
        recorder.close()

    def test_initialization_error_fails_open_and_reports_once(self):
        errors = Mock()
        with patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            recorder = DailyEnvironmentRecorder(
                self.now, self.base_dir, error_handler=errors
            )
        self.assertEqual(recorder.status, recorder.FAILED)
        errors.assert_called_once()
        self.assertFalse(recorder.record_qnh(self.event("2026-08-17T12:01:00Z", 1004.0)))
        errors.assert_called_once()

    def test_file_open_error_sets_failed_without_raising(self):
        self.base_dir.mkdir(parents=True)
        with patch.object(Path, "open", side_effect=PermissionError("open denied")):
            recorder = DailyEnvironmentRecorder(self.now, self.base_dir)
        self.assertEqual(recorder.status, recorder.FAILED)
        self.assertIn("open denied", recorder.error_message)

    def test_write_error_fails_open_and_later_calls_are_noop(self):
        errors = Mock()
        recorder = DailyEnvironmentRecorder(self.now, self.base_dir, error_handler=errors)
        recorder._file.close()
        failed_file = Mock()
        failed_file.write.side_effect = OSError("disk full")
        recorder._file = failed_file
        self.assertFalse(recorder.record_qnh(self.event("2026-08-17T12:01:00Z", 1004.0)))
        self.assertEqual(recorder.status, recorder.FAILED)
        self.assertFalse(recorder.record_qnh(self.event("2026-08-17T12:02:00Z", 1005.0)))
        failed_file.write.assert_called_once()
        errors.assert_called_once()

    def test_flush_error_sets_failed(self):
        recorder = DailyEnvironmentRecorder(self.now, self.base_dir)
        recorder._file.close()
        recorder._file = Mock()
        recorder._file.flush.side_effect = OSError("flush failed")
        self.assertFalse(recorder.record_qnh(self.event("2026-08-17T12:01:00Z", 1004.0)))
        self.assertEqual(recorder.status, recorder.FAILED)

    def test_rotation_error_sets_failed_and_is_not_retried(self):
        recorder = DailyEnvironmentRecorder(self.now, self.base_dir)
        with patch.object(recorder, "_open_day", side_effect=OSError("open failed")) as open_day:
            self.assertFalse(recorder.rotate_if_needed(
                datetime(2026, 8, 18, tzinfo=timezone.utc)
            ))
            self.assertFalse(recorder.rotate_if_needed(
                datetime(2026, 8, 19, tzinfo=timezone.utc)
            ))
        self.assertEqual(recorder.status, recorder.FAILED)
        open_day.assert_called_once()

    def test_error_can_be_consumed_only_once_without_handler(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            recorder = DailyEnvironmentRecorder(self.now, self.base_dir)
        self.assertIn("denied", recorder.consume_error())
        self.assertIsNone(recorder.consume_error())

    def test_restart_scans_existing_history_without_loading_it_as_a_list(self):
        path = self.path("20260817")
        path.parent.mkdir(parents=True)
        with path.open("w", encoding="utf-8") as target:
            for second in range(1000):
                event = self.event(
                    "2026-08-17T12:{:02d}:{:02d}Z".format(
                        (second // 60) % 60, second % 60
                    ),
                    1000.0 + (second % 2),
                )
                target.write(json.dumps({
                    "version": 1,
                    "time": event.time.isoformat().replace("+00:00", "Z"),
                    "type": "qnh",
                    "value_hpa": event.value_hpa,
                    "source": "awc",
                    "station": "EPRA",
                }) + "\n")
        recorder = DailyEnvironmentRecorder(self.now + timedelta(hours=1), self.base_dir)
        self.assertEqual(recorder.last_qnh, 1001.0)
        recorder.close()


class EnvironmentRecorderAdditionalTests(unittest.TestCase):
    def event(self, timestamp, value=1013.0, source="fallback", obs_time=None):
        return EnvironmentEvent(
            1,
            datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
            "qnh",
            value,
            source,
            "EPRA",
            datetime.fromisoformat(obs_time.replace("Z", "+00:00")) if obs_time else None,
        )

    def test_records_decimal_awc_change_and_skips_same_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environment.jsonl"
            recorder = EnvironmentRecorder(path, self.event("2026-08-16T10:00:00Z"))
            changed = recorder.record(self.event(
                "2026-08-16T10:01:00Z", 1011.8, "awc", "2026-08-16T10:00:00Z"
            ))
            unchanged = recorder.record(self.event(
                "2026-08-16T10:02:00Z", 1011.8, "awc", "2026-08-16T10:00:00Z"
            ))
            recorder.close()

            events = list(iter_environment_events(path))
            self.assertTrue(changed)
            self.assertFalse(unchanged)
            self.assertEqual([event.value_hpa for event in events], [1013.0, 1011.8])
            self.assertEqual(events[-1].obs_time.isoformat(), "2026-08-16T10:00:00+00:00")

    def test_flushes_each_written_record(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = EnvironmentRecorder(
                Path(directory) / "environment.jsonl",
                self.event("2026-08-16T10:00:00Z"),
            )
            with patch.object(
                    recorder._file, "flush", wraps=recorder._file.flush) as flush:
                recorder.record(self.event("2026-08-16T10:01:00Z", 1012.0, "awc"))
                flush.assert_called_once_with()
            recorder.close()

    def test_append_preserves_existing_data_and_skips_new_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environment.jsonl"
            first = EnvironmentRecorder(path, self.event("2026-08-16T10:00:00Z"))
            first.record(self.event("2026-08-16T10:01:00Z", 1012.0, "awc"))
            first.close()
            original = path.read_text(encoding="utf-8")

            resumed = EnvironmentRecorder(path, self.event("2026-08-16T10:02:00Z"))
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            resumed.record(self.event("2026-08-16T10:03:00Z", 1011.0, "awc"))
            resumed.close()

            self.assertEqual(
                [event.value_hpa for event in iter_environment_events(path)],
                [1013.0, 1012.0, 1011.0],
            )

    def test_rejects_recording_start_before_existing_last_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environment.jsonl"
            recorder = EnvironmentRecorder(path, self.event("2026-08-16T10:05:00Z"))
            recorder.close()
            with self.assertRaises(EnvironmentRecordError):
                EnvironmentRecorder(path, self.event("2026-08-16T10:04:59Z"))

    def test_rejects_decreasing_appended_event(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = EnvironmentRecorder(
                Path(directory) / "environment.jsonl",
                self.event("2026-08-16T10:05:00Z"),
            )
            with self.assertRaises(EnvironmentRecordError):
                recorder.record(self.event("2026-08-16T10:04:59Z", 1012.0, "awc"))
            recorder.close()


if __name__ == "__main__":
    unittest.main()
