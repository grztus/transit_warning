"""Fail-open validation snapshots for very close predicted transits."""

from collections import deque
from copy import deepcopy
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import uuid


def authoritative_snapshot_v3_source(prediction):
    """Expose exact T0 state without consulting legacy or mutable live state.

    This is an adapter contract for a later consumer-migration checkpoint; it
    intentionally does not change snapshot triggering or schema output.
    """
    return {
        "predicted_transit_utc": prediction.predicted_transit_utc,
        "aircraft_latitude_deg": prediction.aircraft_latitude_deg,
        "aircraft_longitude_deg": prediction.aircraft_longitude_deg,
        "aircraft_altitude_m": prediction.aircraft_altitude_m,
        "vertical_state": prediction.frozen_vertical_state,
    }


UTC = datetime.timezone.utc
SCHEMA_VERSION = 3
HISTORY_SECONDS = 5.0
AFTER_SECONDS = 5.0
DEFAULT_ARM_SECONDS = 15.0
DEFAULT_FINALIZE_GRACE_SECONDS = 2.0
BUFFER_RETENTION_SECONDS = 30.0
BUFFER_STALE_SECONDS = 60.0
RECENT_EVENT_TTL_SECONDS = 60.0
BUFFER_MAXLEN = 512
PREDICTION_UPDATE_MAXLEN = 256


def utc_text(value):
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def runtime_git_commit(project_dir=None):
    """Read the revision once; installations without Git remain supported."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project_dir,
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def safe_filename_component(value, fallback):
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "", str(value or "").upper())
    return cleaned or fallback


class TransitSnapshotManager:
    """Keep bounded observation history and persist completed events."""

    def __init__(self, base_dir="transit_snapshots", sep_threshold_deg=0.5,
                 arm_seconds=DEFAULT_ARM_SECONDS,
                 finalize_grace_seconds=DEFAULT_FINALIZE_GRACE_SECONDS,
                 git_commit=None, prediction_model="2E/2F"):
        self.base_dir = Path(base_dir)
        self.sep_threshold_deg = float(sep_threshold_deg)
        self.arm_seconds = float(arm_seconds)
        self.finalize_grace_seconds = float(finalize_grace_seconds)
        self.git_commit = (runtime_git_commit(self.base_dir.parent)
                           if git_commit is None else git_commit)
        self.prediction_model = prediction_model
        self._buffers = {}
        self._buffer_last_seen = {}
        self._active = {}
        self._recent_events = {}
        self._lock = threading.RLock()
        self.last_error = None
        self.last_error_utc = None

    @property
    def active_events(self):
        with self._lock:
            return deepcopy(self._active)

    def record_observation(self, observation):
        """Store every accepted pipeline observation; do not filter outliers."""
        try:
            item = deepcopy(observation)
            timestamp = item["timestamp_utc"]
            icao = str(item["icao"])
            self._require_aware(timestamp, "observation timestamp")
            with self._lock:
                latest = max(timestamp, self._buffer_last_seen.get(icao,
                                                                   timestamp))
                self._buffer_last_seen[icao] = latest
                existing = self._buffers.get(icao, ())
                cutoff = latest - datetime.timedelta(
                    seconds=BUFFER_RETENTION_SECONDS)
                retained = [sample for sample in existing
                            if sample["timestamp_utc"] >= cutoff]
                if timestamp >= cutoff:
                    retained.append(item)
                self._buffers[icao] = deque(retained, maxlen=BUFFER_MAXLEN)
                self._cleanup_locked(latest)
            return True
        except Exception as error:
            self._fail(error)
            return False

    def consider_prediction(self, prediction):
        """Arm or update one ICAO/body event from an existing prediction."""
        try:
            item = deepcopy(prediction)
            separation = float(item["separation_deg"])
            time2x = float(item["time2x_seconds"])
            icao = str(item["icao"])
            body = str(item["body"]).upper()
            predicted_utc = item["predicted_transit_utc"]
            recorded_utc = item["recorded_at_utc"]
            self._require_aware(predicted_utc, "predicted transit timestamp")
            self._require_aware(recorded_utc, "prediction timestamp")
            key = (icao, body)
            serialized = self._serialize_prediction(item)
            signature = self._prediction_signature(item)
            with self._lock:
                self._cleanup_locked(recorded_utc)
                event = self._active.get(key)
                if event is not None:
                    if not 0 < time2x <= 900:
                        return False
                    event["reference_transit_utc"] = predicted_utc
                    event["finalize_after_utc"] = self._finalize_after(
                        predicted_utc)
                    if signature != event["last_prediction_signature"]:
                        updates = event["prediction_updates"]
                        if len(updates) >= PREDICTION_UPDATE_MAXLEN:
                            del updates[1]
                        updates.append(serialized)
                        event["last_prediction_signature"] = signature
                    return False

                if (separation >= self.sep_threshold_deg
                        or not 0 < time2x <= self.arm_seconds):
                    return False
                recent_until = self._recent_events.get(key)
                if recent_until is not None and recorded_utc <= recent_until:
                    return False

                event_id = "{}_{}_{}_{}".format(
                    predicted_utc.strftime("%Y%m%d_%H%M%S_%f"),
                    icao, body, uuid.uuid4().hex[:8])
                self._active[key] = {
                    "event_id": event_id,
                    "icao": icao,
                    "body": body,
                    "callsign": item.get("callsign"),
                    "observer": deepcopy(item.get("observer", {})),
                    "initial_predicted_transit_utc": predicted_utc,
                    "reference_transit_utc": predicted_utc,
                    "finalize_after_utc": self._finalize_after(predicted_utc),
                    "trigger_prediction": serialized,
                    "prediction_updates": [serialized],
                    "last_prediction_signature": signature,
                }
            return True
        except Exception as error:
            self._fail(error)
            return False

    def finalize_due(self, now_utc):
        """Detach due payloads under lock, then perform file I/O unlocked."""
        completed = []
        payloads = []
        try:
            self._require_aware(now_utc, "finalization timestamp")
            with self._lock:
                self._cleanup_locked(now_utc)
                due = [key for key, event in self._active.items()
                       if now_utc >= event["finalize_after_utc"]]
                for key in due:
                    event = self._active.pop(key)
                    payloads.append(self._document(
                        event, now_utc, complete=True,
                        finalization_reason="normal"))
                    self._recent_events[key] = now_utc + datetime.timedelta(
                        seconds=RECENT_EVENT_TTL_SECONDS)
            for payload in payloads:
                path = self._write_document(payload, now_utc)
                if path is not None:
                    completed.append(path)
            return completed
        except Exception as error:
            self._fail(error, now_utc)
            return completed

    def close(self, now_utc):
        """Fail-open shutdown flush of all still-active partial events."""
        completed = []
        payloads = []
        try:
            self._require_aware(now_utc, "shutdown timestamp")
            with self._lock:
                events = list(self._active.values())
                self._active.clear()
                payloads = [self._document(
                    event, now_utc, complete=False,
                    finalization_reason="shutdown") for event in events]
            for payload in payloads:
                path = self._write_document(payload, now_utc)
                if path is not None:
                    completed.append(path)
            return completed
        except Exception as error:
            self._fail(error, now_utc)
            return completed

    def drop_aircraft_buffer(self, icao):
        try:
            with self._lock:
                if not any(key[0] == icao for key in self._active):
                    self._buffers.pop(icao, None)
                    self._buffer_last_seen.pop(icao, None)
            return True
        except Exception as error:
            self._fail(error)
            return False

    def invalidate_active_predictions(self):
        """Discard predictions tied to an observer context that changed."""
        with self._lock:
            self._active.clear()

    def cleanup(self, now_utc):
        try:
            with self._lock:
                self._cleanup_locked(now_utc)
            return True
        except Exception as error:
            self._fail(error, now_utc)
            return False

    def _cleanup_locked(self, now_utc):
        recent_expired = [key for key, expires in self._recent_events.items()
                          if expires < now_utc]
        for key in recent_expired:
            del self._recent_events[key]
        active_icaos = {key[0] for key in self._active}
        stale_before = now_utc - datetime.timedelta(
            seconds=BUFFER_STALE_SECONDS)
        stale_icaos = [icao for icao, timestamp
                       in self._buffer_last_seen.items()
                       if icao not in active_icaos and timestamp < stale_before]
        for icao in stale_icaos:
            self._buffers.pop(icao, None)
            self._buffer_last_seen.pop(icao, None)

    def _finalize_after(self, reference_utc):
        return reference_utc + datetime.timedelta(
            seconds=AFTER_SECONDS + self.finalize_grace_seconds)

    @staticmethod
    def _require_aware(value, label):
        if value.tzinfo is None:
            raise ValueError("{} must be timezone-aware".format(label))

    def _prediction_signature(self, prediction):
        def normalized(value):
            if isinstance(value, datetime.datetime):
                return round(value.timestamp(), 3)
            if isinstance(value, float):
                return round(value, 6)
            if isinstance(value, dict):
                return tuple(sorted((key, normalized(item))
                                    for key, item in value.items()
                                    if key != "recorded_at_utc"))
            if isinstance(value, (list, tuple)):
                return tuple(normalized(item) for item in value)
            return value
        return normalized(prediction)

    def _serialize_prediction(self, prediction):
        result = deepcopy(prediction)
        for key in ("predicted_transit_utc", "recorded_at_utc"):
            result[key] = utc_text(result.get(key))
        return result

    def _serialize_observation(self, observation):
        result = deepcopy(observation)
        result["timestamp_utc"] = utc_text(result.get("timestamp_utc"))
        timestamps = result.get("source_timestamps_utc", {})
        result["source_timestamps_utc"] = {
            key: utc_text(value) for key, value in timestamps.items()
        }
        return result

    def _document(self, event, finalized_at, complete,
                  finalization_reason):
        reference = event["reference_transit_utc"]
        window_start = reference - datetime.timedelta(seconds=HISTORY_SECONDS)
        window_end = reference + datetime.timedelta(seconds=AFTER_SECONDS)
        observations = [deepcopy(sample)
                        for sample in self._buffers.get(event["icao"], ())
                        if window_start <= sample["timestamp_utc"] <= window_end]
        observations.sort(key=lambda item: item["timestamp_utc"])
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": event["event_id"],
            "complete": complete,
            "finalization_reason": finalization_reason,
            "software": {
                "git_commit": self.git_commit,
                "prediction_model": self.prediction_model,
            },
            "aircraft": {
                "icao": event["icao"],
                "callsign": event.get("callsign") or None,
            },
            "body": event["body"],
            "observer": deepcopy(event["observer"]),
            "initial_predicted_transit_utc": utc_text(
                event["initial_predicted_transit_utc"]),
            "final_reference_transit_utc": utc_text(reference),
            "window": {
                "start_utc": utc_text(window_start),
                "end_utc": utc_text(window_end),
                "finalize_after_utc": utc_text(event["finalize_after_utc"]),
                "finalized_at_utc": utc_text(finalized_at),
            },
            "trigger_prediction": deepcopy(event["trigger_prediction"]),
            "prediction_updates": deepcopy(event["prediction_updates"]),
            "observations": [self._serialize_observation(item)
                             for item in observations],
        }

    def _write_document(self, document, finalized_at):
        reference = datetime.datetime.fromisoformat(
            document["final_reference_transit_utc"].replace("Z", "+00:00"))
        target_dir = self.base_dir / reference.strftime("%Y-%m-%d")
        aircraft = document["aircraft"]
        callsign = safe_filename_component(
            aircraft.get("callsign"), "NOCALL")
        stem = "{}_{}_{}_{}_{}".format(
            reference.strftime("%Y%m%d_%H%M%S_%f"),
            safe_filename_component(aircraft["icao"], "UNKNOWN"),
            callsign, document["body"], document["event_id"].split("_")[-1])
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            temporary = target_dir / (".{}.tmp".format(uuid.uuid4().hex))
            try:
                with temporary.open("x", encoding="utf-8") as output:
                    json.dump(document, output, indent=2, sort_keys=True,
                              allow_nan=False)
                suffix = 0
                while True:
                    unique = (stem if suffix == 0
                              else "{}_{}".format(stem, suffix))
                    target = target_dir / (unique + ".json")
                    try:
                        os.link(temporary, target)
                        break
                    except FileExistsError:
                        suffix += 1
                return target
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as error:
            self._fail(error, finalized_at)
            return None

    def _fail(self, error, when=None):
        self.last_error = str(error)
        self.last_error_utc = when
