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
