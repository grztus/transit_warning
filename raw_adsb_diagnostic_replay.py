"""Streaming replay of additive RAW ADS-B altitude diagnostics."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path


class RawDiagnosticFormatError(ValueError):
    pass


@dataclass(frozen=True)
class RawDiagnosticEvent:
    time: datetime
    record: dict


def _utc(value):
    if not isinstance(value, str) or not value:
        raise RawDiagnosticFormatError("time must be an ISO 8601 UTC datetime")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RawDiagnosticFormatError("time must be an ISO 8601 UTC datetime") from error
    if result.tzinfo is None or result.utcoffset() != timezone.utc.utcoffset(result):
        raise RawDiagnosticFormatError("time must be timezone-aware UTC")
    return result.astimezone(timezone.utc)


def parse_raw_diagnostic_event(record):
    if not isinstance(record, dict) or record.get("version") != 1:
        raise RawDiagnosticFormatError("record must be a version 1 object")
    event_time = _utc(record.get("time"))
    icao = record.get("icao")
    if (not isinstance(icao, str) or len(icao) != 6
            or any(character not in "0123456789ABCDEF" for character in icao)):
        raise RawDiagnosticFormatError("icao must be 6 uppercase hex digits")
    message_type = record.get("message_type")
    if message_type == "TC19":
        raw = record.get("raw_encoded_value")
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 127:
            raise RawDiagnosticFormatError("raw_encoded_value must be 0..127")
        available = record.get("available")
        expected_available = raw not in (0, 127)
        if available is not expected_available:
            raise RawDiagnosticFormatError("TC19 availability is inconsistent")
        difference = record.get("gnss_minus_baro_ft")
        if available:
            if not isinstance(difference, (int, float)):
                raise RawDiagnosticFormatError("available TC19 must have a difference")
            if abs(float(difference)) != (raw - 1) * 25:
                raise RawDiagnosticFormatError("TC19 difference is inconsistent")
        elif difference is not None:
            raise RawDiagnosticFormatError("unavailable TC19 difference must be null")
        if record.get("vertical_rate_source") not in ("GNSS", "BAROMETRIC"):
            raise RawDiagnosticFormatError("invalid vertical_rate_source")
    elif message_type == "TC31":
        version = record.get("adsb_version")
        if isinstance(version, bool) or not isinstance(version, int) or not 0 <= version <= 7:
            raise RawDiagnosticFormatError("adsb_version must be 0..7")
    else:
        raise RawDiagnosticFormatError("message_type must be TC19 or TC31")
    return RawDiagnosticEvent(event_time, dict(record))


def iter_raw_diagnostic_events(path):
    previous = None
    source_path = Path(path)
    with source_path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                event = parse_raw_diagnostic_event(json.loads(line))
            except (json.JSONDecodeError, RawDiagnosticFormatError) as error:
                raise RawDiagnosticFormatError(
                    "{}:{}: {}".format(source_path, line_number, error)) from error
            if previous is not None and event.time < previous:
                raise RawDiagnosticFormatError(
                    "{}:{}: event time is earlier than previous event".format(
                        source_path, line_number))
            previous = event.time
            yield event


class RawDiagnosticReplay:
    def __init__(self, events):
        self._events = iter(events)
        self._pending = next(self._events, None)

    def pop_through(self, timestamp):
        while self._pending is not None and self._pending.time <= timestamp:
            event = self._pending
            self._pending = next(self._events, None)
            yield event
