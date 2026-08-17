"""Validated, streaming environmental events for deterministic replay."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Iterator


class EnvironmentFormatError(ValueError):
    """Raised when an environment JSONL record is invalid."""


class EnvironmentRecordError(ValueError):
    """Raised when an environment recording cannot remain chronological."""


@dataclass(frozen=True)
class EnvironmentEvent:
    version: int
    time: datetime
    type: str
    value_hpa: float
    source: str
    station: str | None = None
    obs_time: datetime | None = None


def _utc_datetime(value, field):
    if not isinstance(value, str) or not value:
        raise EnvironmentFormatError("{} must be an ISO 8601 UTC datetime".format(field))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EnvironmentFormatError(
            "{} must be an ISO 8601 UTC datetime".format(field)
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EnvironmentFormatError("{} must be timezone-aware UTC".format(field))
    if parsed.utcoffset().total_seconds() != 0:
        raise EnvironmentFormatError("{} must use UTC".format(field))
    return parsed.astimezone(timezone.utc)


def parse_environment_event(record):
    """Validate and convert one decoded JSON object."""
    if not isinstance(record, dict):
        raise EnvironmentFormatError("record must be a JSON object")
    version = record.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise EnvironmentFormatError("version must be 1")
    event_time = _utc_datetime(record.get("time"), "time")
    if record.get("type") != "qnh":
        raise EnvironmentFormatError('type must be "qnh"')

    value = record.get("value_hpa")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EnvironmentFormatError("value_hpa must be a number")
    value = float(value)
    if not math.isfinite(value) or not 800 < value < 1100:
        raise EnvironmentFormatError("value_hpa must be finite and in the range 800..1100")

    source = record.get("source")
    if not isinstance(source, str):
        raise EnvironmentFormatError("source must be text")

    station = record.get("station")
    if station is not None:
        if not isinstance(station, str) or re.fullmatch(
                r"[A-Z]{4}", station, flags=re.ASCII) is None:
            raise EnvironmentFormatError("station must be 4 uppercase ASCII letters A-Z")

    raw_obs_time = record.get("obs_time")
    obs_time = _utc_datetime(raw_obs_time, "obs_time") if raw_obs_time is not None else None
    return EnvironmentEvent(1, event_time, "qnh", value, source, station, obs_time)


def iter_environment_events(path) -> Iterator[EnvironmentEvent]:
    """Yield validated JSONL events without loading the whole file."""
    previous_time = None
    source_path = Path(path)
    with source_path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise EnvironmentFormatError(
                    "{}:{}: invalid JSON: {}".format(source_path, line_number, error.msg)
                ) from error
            try:
                event = parse_environment_event(record)
            except EnvironmentFormatError as error:
                raise EnvironmentFormatError(
                    "{}:{}: {}".format(source_path, line_number, error)
                ) from error
            if previous_time is not None and event.time < previous_time:
                raise EnvironmentFormatError(
                    "{}:{}: event time is earlier than the previous event".format(
                        source_path, line_number
                    )
                )
            previous_time = event.time
            yield event


class EnvironmentReplay:
    """One-event look-ahead cursor over a streaming environment file."""

    def __init__(self, events):
        self._events = iter(events)
        self._pending = next(self._events, None)

    def pop_through(self, timestamp):
        while self._pending is not None and self._pending.time <= timestamp:
            event = self._pending
            self._pending = next(self._events, None)
            yield event


def _event_record(event):
    record = {
        "version": event.version,
        "time": event.time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "type": event.type,
        "value_hpa": event.value_hpa,
        "source": event.source,
    }
    if event.station is not None:
        record["station"] = event.station
    if event.obs_time is not None:
        record["obs_time"] = (
            event.obs_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )
    return record


class EnvironmentRecorder:
    """Append validated QNH changes to an environment JSONL file."""

    def __init__(self, path, initial_event):
        self.path = Path(path)
        last_event = None
        if self.path.exists():
            for last_event in iter_environment_events(self.path):
                pass
        if last_event is not None and initial_event.time < last_event.time:
            raise EnvironmentRecordError(
                "recording time {} is earlier than the last event {}".format(
                    initial_event.time.isoformat(), last_event.time.isoformat()
                )
            )

        needs_separator = self.path.exists() and self.path.stat().st_size > 0
        if needs_separator:
            with self.path.open("rb") as existing:
                existing.seek(-1, 2)
                needs_separator = existing.read(1) not in (b"\n", b"\r")

        self._file = self.path.open("a", encoding="utf-8", newline="\n")
        self._needs_separator = needs_separator
        self.last_time = last_event.time if last_event is not None else None
        self.last_value_hpa = last_event.value_hpa if last_event is not None else None
        if last_event is None:
            self.record(initial_event, force=True)

    def record(self, event, force=False):
        validated = parse_environment_event(_event_record(event))
        if self.last_time is not None and validated.time < self.last_time:
            raise EnvironmentRecordError(
                "event time {} is earlier than the last event {}".format(
                    validated.time.isoformat(), self.last_time.isoformat()
                )
            )
        if not force and validated.value_hpa == self.last_value_hpa:
            return False
        if self._needs_separator:
            self._file.write("\n")
            self._needs_separator = False
        self._file.write(json.dumps(_event_record(validated), separators=(",", ":")) + "\n")
        self._file.flush()
        self.last_time = validated.time
        self.last_value_hpa = validated.value_hpa
        return True

    def close(self):
        self._file.close()
