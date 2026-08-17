"""Validated, streaming environmental events for deterministic replay."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import re
import threading
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


class DailyEnvironmentRecorder:
    """Fail-open, UTC-day-rotated recorder for environmental state."""

    RECORDING = "RECORDING"
    FAILED = "FAILED"

    def __init__(
        self,
        now_utc,
        base_dir=Path("recordings/environment"),
        fallback_station=None,
        error_handler=None,
    ):
        self.base_dir = Path(base_dir)
        self.fallback_station = fallback_station
        self.error_handler = error_handler
        self.status = self.RECORDING
        self.error_message = None
        self._error_reported = False
        self._file = None
        self._lock = threading.RLock()
        self._active_date = None
        self._last_event = None
        self.next_midnight_utc = None
        try:
            now_utc = self._require_utc(now_utc)
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self._open_day(now_utc.date())
            self.next_midnight_utc = self._midnight_after(now_utc.date())
        except Exception as error:
            self._fail("initialization", error)

    @property
    def last_event(self):
        """Return the last persisted QNH event, if one is known."""
        with self._lock:
            return self._last_event

    @property
    def last_qnh(self):
        with self._lock:
            return self._last_event.value_hpa if self._last_event is not None else None

    def consume_error(self):
        """Return the failure message once for polling integrations."""
        with self._lock:
            if self.error_message is None or self._error_reported:
                return None
            self._error_reported = True
            return self.error_message

    def recover_recent_qnh(self, now_utc):
        """Return the last QNH from today or yesterday, without searching further."""
        with self._lock:
            if self.status == self.FAILED:
                return None
            try:
                now_utc = self._require_utc(now_utc)
                if self._last_event is not None:
                    return self._last_event

                previous_day = now_utc.date() - timedelta(days=1)
                previous_path = self._path_for(previous_day)
                previous_event = None
                if previous_path.exists():
                    for previous_event in iter_environment_events(previous_path):
                        pass
                if previous_event is None:
                    return None

                carryover = EnvironmentEvent(
                    1,
                    datetime.combine(now_utc.date(), time.min, tzinfo=timezone.utc),
                    "qnh",
                    previous_event.value_hpa,
                    "carryover",
                    previous_event.station,
                    previous_event.obs_time,
                )
                self._write_event(carryover)
                return carryover
            except Exception as error:
                self._fail("history recovery", error)
                return None

    @staticmethod
    def _require_utc(value):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("time must be a timezone-aware UTC datetime")
        if value.utcoffset() != timedelta(0):
            raise ValueError("time must use UTC")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _midnight_after(day):
        return datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone.utc)

    def _path_for(self, day):
        return self.base_dir / "environment_{}.jsonl".format(day.strftime("%Y%m%d"))

    def _open_day(self, day):
        path = self._path_for(day)
        last_event = None
        if path.exists():
            for last_event in iter_environment_events(path):
                pass
        self._file = path.open("a", encoding="utf-8", newline="\n")
        self._active_date = day
        self._last_event = last_event

    def _write_event(self, event):
        validated = parse_environment_event(_event_record(event))
        if self._last_event is not None and validated.time < self._last_event.time:
            raise EnvironmentRecordError(
                "event time {} is earlier than the last event {}".format(
                    validated.time.isoformat(), self._last_event.time.isoformat()
                )
            )
        self._file.write(json.dumps(_event_record(validated), separators=(",", ":")) + "\n")
        self._file.flush()
        self._last_event = validated

    def _fail(self, operation, error):
        if self.status == self.FAILED:
            return
        self.status = self.FAILED
        self.error_message = "Environment recorder {} failed: {}".format(operation, error)
        try:
            if self._file is not None:
                self._file.close()
        except Exception:
            pass
        self._file = None
        if self.error_handler is not None and not self._error_reported:
            self._error_reported = True
            try:
                self.error_handler(self.error_message)
            except Exception:
                pass

    def rotate_if_needed(self, now_utc):
        with self._lock:
            if self.status == self.FAILED:
                return False
            try:
                if now_utc < self.next_midnight_utc:
                    return False
                now_utc = self._require_utc(now_utc)
                previous_event = self._last_event
                target_day = now_utc.date()
                target_midnight = datetime.combine(target_day, time.min, tzinfo=timezone.utc)
                self._file.close()
                self._file = None
                self._open_day(target_day)
                if self._last_event is None:
                    if previous_event is None:
                        carryover = EnvironmentEvent(
                            1, target_midnight, "qnh", 1013.0, "fallback",
                            self.fallback_station,
                        )
                    else:
                        carryover = EnvironmentEvent(
                            1, target_midnight, "qnh", previous_event.value_hpa, "carryover",
                            previous_event.station, previous_event.obs_time,
                        )
                    self._write_event(carryover)
                self.next_midnight_utc = self._midnight_after(target_day)
                return True
            except Exception as error:
                self._fail("rotation", error)
                return False

    def record_qnh(self, event):
        with self._lock:
            if self.status == self.FAILED:
                return False
            try:
                if event.type != "qnh":
                    raise EnvironmentFormatError('type must be "qnh"')
                event_time = self._require_utc(event.time)
                self.rotate_if_needed(event_time)
                if self.status == self.FAILED:
                    return False
                validated = parse_environment_event(_event_record(event))
                if self._last_event is not None and validated.time < self._last_event.time:
                    raise EnvironmentRecordError(
                        "event time {} is earlier than the last event {}".format(
                            validated.time.isoformat(), self._last_event.time.isoformat()
                        )
                    )
                if (self._last_event is not None
                        and validated.value_hpa == self._last_event.value_hpa):
                    self._last_event = validated
                    return False
                self._write_event(validated)
                return True
            except Exception as error:
                self._fail("write", error)
                return False

    def close(self):
        with self._lock:
            if self._file is None:
                return
            try:
                self._file.flush()
                self._file.close()
            except Exception as error:
                self._fail("close", error)
            finally:
                self._file = None
