import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from environment import (
    EnvironmentEvent,
    EnvironmentFormatError,
    EnvironmentRecorder,
    EnvironmentRecordError,
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
