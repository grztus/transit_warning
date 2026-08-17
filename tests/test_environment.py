import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from environment import (
    EnvironmentEvent,
    EnvironmentFormatError,
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


if __name__ == "__main__":
    unittest.main()
