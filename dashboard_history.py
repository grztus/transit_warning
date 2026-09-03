"""Lightweight persistent JSONL history for the live dashboard."""

import csv
import datetime
import io
import json
from pathlib import Path
import re
import threading


UTC = datetime.timezone.utc
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
HISTORY_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
CSV_FIELDS = (
    "event_id", "body", "icao", "callsign", "predicted_event_utc",
    "outcome", "first_separation_deg", "minimum_separation_deg",
    "final_separation_deg", "first_seen_utc", "last_seen_utc",
    "history_recorded_at_utc", "body_azimuth_deg", "body_elevation_deg",
    "aircraft_elevation_deg", "distance_km", "transit_distance_km",
    "telegram_range", "prediction_geometry",
)


def records_to_csv(records):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS,
                            extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow({name: record.get(name) for name in CSV_FIELDS})
    return output.getvalue().encode("utf-8-sig")


def _utc_date_from_record(record):
    value = record.get("predicted_event_utc")
    if not value:
        raise ValueError("predicted_event_utc is required")
    parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("history timestamp must be timezone-aware")
    return parsed.astimezone(UTC).date().isoformat()


def _matches(record, utc_date=None, callsign=None, body=None):
    if utc_date is not None:
        timestamp = str(record.get("predicted_event_utc") or "")
        if not timestamp.startswith(utc_date):
            return False
    if body and body != "ALL" and str(record.get("body") or "").upper() != body:
        return False
    if callsign:
        value = str(record.get("callsign") or "")
        if callsign.casefold() not in value.casefold():
            return False
    return True


class DashboardHistoryStore:
    """Append retained events and query daily JSONL partitions fail-open."""

    def __init__(self, base_dir, error_handler=None):
        path = Path(base_dir)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path
        self.base_dir = path
        self._error_handler = error_handler or (lambda message: None)
        self._failed = False
        self._error_reported = False
        self._lock = threading.RLock()
        self._seen_by_path = {}

    @property
    def failed(self):
        return self._failed

    def _report_once(self, error):
        self._failed = True
        if self._error_reported:
            return
        self._error_reported = True
        try:
            self._error_handler(
                "Dashboard history failed: {}".format(type(error).__name__))
        except Exception:
            pass

    def append(self, record):
        if self._failed:
            return False
        try:
            date_text = _utc_date_from_record(record)
            line = json.dumps(record, separators=(",", ":"), sort_keys=True)
            path = self.base_dir / "{}.jsonl".format(date_text)
            with self._lock:
                self.base_dir.mkdir(parents=True, exist_ok=True)
                if self._event_exists(path, record.get("event_id")):
                    return False
                with path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(line + "\n")
                    stream.flush()
                self._seen_by_path[path].add(record.get("event_id"))
            return True
        except Exception as error:
            self._report_once(error)
            return False

    def _event_exists(self, path, event_id):
        if not event_id:
            return False
        if path not in self._seen_by_path:
            self._seen_by_path[path] = {
                record.get("event_id") for record in self._read_file(path)
                if record.get("event_id")
            }
        return event_id in self._seen_by_path[path]

    @staticmethod
    def _read_file(path):
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(value, dict):
                        yield value
        except OSError:
            return

    def _paths(self, utc_date=None):
        if utc_date is not None:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", utc_date) is None:
                return []
            path = self.base_dir / "{}.jsonl".format(utc_date)
            return [path] if path.is_file() else []
        try:
            return sorted(
                (path for path in self.base_dir.iterdir()
                 if path.is_file() and HISTORY_FILENAME_RE.match(path.name)),
                reverse=True)
        except OSError:
            return []

    def iter_records(self, utc_date=None, callsign=None, body="ALL"):
        body = str(body or "ALL").upper()
        for path in self._paths(utc_date):
            # One UTC partition is bounded; reverse only that day's records.
            records = list(self._read_file(path))
            for record in reversed(records):
                if _matches(record, utc_date, callsign, body):
                    yield record

    def query(self, utc_date=None, callsign=None, body="ALL", offset=0,
              limit=DEFAULT_PAGE_SIZE):
        offset = max(0, int(offset))
        limit = min(MAX_PAGE_SIZE, max(1, int(limit)))
        selected = []
        for index, record in enumerate(
                self.iter_records(utc_date, callsign, body)):
            if index < offset:
                continue
            selected.append(record)
            if len(selected) > limit:
                break
        has_more = len(selected) > limit
        records = selected[:limit]
        return {
            "records": records,
            "offset": offset,
            "limit": limit,
            "next_offset": offset + len(records) if has_more else None,
            "has_more": has_more,
        }

    def export_csv(self, utc_date=None, callsign=None, body="ALL"):
        return records_to_csv(self.iter_records(utc_date, callsign, body))

    def close(self):
        return None
