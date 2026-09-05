"""Server-authoritative, revisioned operational runtime settings."""

from collections import OrderedDict
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import threading

from .contracts import SCHEMA_VERSION
from .privacy import assert_public_payload


class SettingsValidationError(ValueError):
    pass


class SettingsConflictError(RuntimeError):
    def __init__(self, current):
        super().__init__("Runtime settings revision conflict")
        self.current = current


class RuntimeSettingsStore:
    """Own operational settings and apply validated changes atomically.

    The optional apply callback is the compatibility boundary to existing
    runtime controls. Accepted-state subscribers are a narrow extension point
    for a later server update channel.
    """

    MAX_COMMANDS = 1024
    CAPABILITIES = {
        "runtime_settings": {
            "telegram_sun_enabled": True,
            "telegram_moon_enabled": True,
            "observer_requested_mode": True,
            "observer_fallback_enabled": True,
            "observer_manual_position": True,
        },
    }

    def __init__(self, telegram_sun_enabled=True, telegram_moon_enabled=True,
                 observer_requested_mode="STATIC",
                 observer_fallback_enabled=False,
                 observer_manual_lat_deg=0.0,
                 observer_manual_lon_deg=0.0,
                 observer_manual_elevation_amsl_m=0.0,
                 apply_callback=None,
                 validate_callback=None,
                 manual_persistence=None,
                 observer_manual_position_saved=False):
        self._values = {
            "telegram": {
                "sun_enabled": bool(telegram_sun_enabled),
                "moon_enabled": bool(telegram_moon_enabled),
            },
            "observer": {
                "requested_mode": self._mode(observer_requested_mode),
                "fallback_enabled": bool(observer_fallback_enabled),
                "manual_lat_deg": self._finite_number(
                    observer_manual_lat_deg, "manual latitude", -90.0, 90.0),
                "manual_lon_deg": self._finite_number(
                    observer_manual_lon_deg, "manual longitude", -180.0, 180.0),
                "manual_elevation_amsl_m": self._finite_number(
                    observer_manual_elevation_amsl_m, "manual elevation",
                    -500.0, 10000.0),
                "manual_position_saved": bool(observer_manual_position_saved),
            },
        }
        self._revision = 0
        self._apply_callback = apply_callback
        self._validate_callback = validate_callback
        self._manual_persistence = manual_persistence
        self._accepted_commands = OrderedDict()
        self._subscribers = []
        self._lock = threading.RLock()

    @staticmethod
    def _mode(value):
        mode = str(value).upper()
        if mode not in ("STATIC", "MOBILE", "MANUAL"):
            raise SettingsValidationError("Invalid observer mode")
        return mode

    @staticmethod
    def _finite_number(value, label, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsValidationError("Invalid {}".format(label))
        result = float(value)
        if not math.isfinite(result) or not minimum <= result <= maximum:
            raise SettingsValidationError("Invalid {}".format(label))
        return result

    def subscribe(self, callback):
        if not callable(callback):
            raise TypeError("subscriber must be callable")
        with self._lock:
            self._subscribers.append(callback)

    def snapshot(self):
        with self._lock:
            return self._snapshot_locked()

    def update(self, expected_revision, command_id, changes):
        if isinstance(expected_revision, bool) or not isinstance(
                expected_revision, int) or expected_revision < 0:
            raise SettingsValidationError("Invalid expected_revision")
        if not isinstance(command_id, str) or not command_id.strip() or len(
                command_id) > 128:
            raise SettingsValidationError("Invalid command_id")
        normalized = self._validate_changes(changes)
        fingerprint = json.dumps(
            {"expected_revision": expected_revision, "changes": normalized},
            sort_keys=True, separators=(",", ":"))
        subscribers = ()
        with self._lock:
            previous = self._accepted_commands.get(command_id)
            if previous is not None:
                if previous != fingerprint:
                    raise SettingsValidationError("command_id was already used")
                result = self._snapshot_locked()
                result["idempotent_replay"] = True
                return result
            if expected_revision != self._revision:
                raise SettingsConflictError(self._snapshot_locked())
            prospective = self._merged_locked(normalized)
            self._validate_manual_activation(normalized, prospective)
            if self._validate_callback is not None:
                self._validate_callback(deepcopy(prospective))
            self._persist_manual_if_changed(normalized, prospective)
            if self._apply_callback is not None:
                self._apply_callback(deepcopy(prospective), deepcopy(self._values))
            self._values = prospective
            self._revision += 1
            self._accepted_commands[command_id] = fingerprint
            self._accepted_commands.move_to_end(command_id)
            while len(self._accepted_commands) > self.MAX_COMMANDS:
                self._accepted_commands.popitem(last=False)
            result = self._snapshot_locked()
            subscribers = tuple(self._subscribers)
        for callback in subscribers:
            try:
                callback(deepcopy(result))
            except Exception:
                pass
        return result

    def legacy_update(self, changes):
        normalized = self._validate_changes(changes)
        subscribers = ()
        with self._lock:
            prospective = self._merged_locked(normalized)
            self._validate_manual_activation(normalized, prospective)
            if self._validate_callback is not None:
                self._validate_callback(deepcopy(prospective))
            self._persist_manual_if_changed(normalized, prospective)
            if self._apply_callback is not None:
                self._apply_callback(deepcopy(prospective), deepcopy(self._values))
            self._values = prospective
            self._revision += 1
            result = self._snapshot_locked()
            subscribers = tuple(self._subscribers)
        for callback in subscribers:
            try:
                callback(deepcopy(result))
            except Exception:
                pass
        return result

    def _snapshot_locked(self):
        result = {
            "schema_version": SCHEMA_VERSION,
            "revision": self._revision,
            "values": deepcopy(self._values),
            "capabilities": deepcopy(self.CAPABILITIES),
            "persistence": "RUNTIME_WITH_DURABLE_MANUAL_OBSERVER",
        }
        assert_public_payload(result)
        return result

    def _merged_locked(self, changes):
        result = deepcopy(self._values)
        for group, values in changes.items():
            result[group].update(values)
        observer = changes.get("observer", {})
        if any(key in observer for key in MANUAL_POSITION_KEYS):
            result["observer"]["manual_position_saved"] = True
        return result

    @staticmethod
    def _validate_manual_activation(changes, prospective):
        if (changes.get("observer", {}).get("requested_mode") == "MANUAL"
                and not prospective["observer"]["manual_position_saved"]):
            raise SettingsValidationError(
                "Save a complete MANUAL observer position before activation")

    def _persist_manual_if_changed(self, changes, prospective):
        observer = changes.get("observer", {})
        if (self._manual_persistence is not None
                and any(key in observer for key in MANUAL_POSITION_KEYS)):
            self._manual_persistence.save(prospective["observer"])

    @classmethod
    def _validate_changes(cls, changes):
        if not isinstance(changes, dict) or not changes:
            raise SettingsValidationError("Invalid settings changes")
        if set(changes) - {"telegram", "observer"}:
            raise SettingsValidationError("Unknown settings group")
        result = {}
        for group, values in changes.items():
            if not isinstance(values, dict) or not values:
                raise SettingsValidationError("Invalid settings changes")
            if group == "telegram":
                allowed = {"sun_enabled", "moon_enabled"}
                if set(values) - allowed:
                    raise SettingsValidationError("Unknown Telegram setting")
                if any(not isinstance(value, bool) for value in values.values()):
                    raise SettingsValidationError("Invalid Telegram setting")
                result[group] = dict(values)
            else:
                allowed = {
                    "requested_mode", "fallback_enabled", "manual_lat_deg",
                    "manual_lon_deg", "manual_elevation_amsl_m",
                }
                if set(values) - allowed:
                    raise SettingsValidationError("Unknown observer setting")
                manual_keys = set(values).intersection(MANUAL_POSITION_KEYS)
                if manual_keys and manual_keys != set(MANUAL_POSITION_KEYS):
                    raise SettingsValidationError(
                        "Manual observer position must be complete")
                observer = {}
                if "requested_mode" in values:
                    observer["requested_mode"] = cls._mode(
                        values["requested_mode"])
                if "fallback_enabled" in values:
                    if not isinstance(values["fallback_enabled"], bool):
                        raise SettingsValidationError("Invalid fallback setting")
                    observer["fallback_enabled"] = values["fallback_enabled"]
                for key, label, minimum, maximum in (
                        ("manual_lat_deg", "manual latitude", -90.0, 90.0),
                        ("manual_lon_deg", "manual longitude", -180.0, 180.0),
                        ("manual_elevation_amsl_m", "manual elevation",
                         -500.0, 10000.0)):
                    if key in values:
                        observer[key] = cls._finite_number(
                            values[key], label, minimum, maximum)
                result[group] = observer
        return result


MANUAL_POSITION_KEYS = (
    "manual_lat_deg",
    "manual_lon_deg",
    "manual_elevation_amsl_m",
)


class ManualObserverSettingsFile:
    """Durable storage for the last complete, validated MANUAL position only."""

    FORMAT_VERSION = 1

    def __init__(self, path, error_handler=None):
        self.path = Path(path)
        self._error_handler = error_handler or (lambda message: None)

    def load(self):
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(payload, dict)
                    or payload.get("format_version") != self.FORMAT_VERSION
                    or not isinstance(payload.get("manual_observer"), dict)):
                raise SettingsValidationError("Invalid saved MANUAL position")
            values = payload["manual_observer"]
            if set(values) != set(MANUAL_POSITION_KEYS):
                raise SettingsValidationError("Invalid saved MANUAL position")
            return self._validated(values)
        except (OSError, ValueError, TypeError) as error:
            self._report("Manual observer settings load failed", error)
            return None

    def save(self, observer):
        values = self._validated(observer)
        payload = {
            "format_version": self.FORMAT_VERSION,
            "manual_observer": values,
        }
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="\n") as output:
                json.dump(payload, output, sort_keys=True, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(str(temporary), str(self.path))
        except OSError as error:
            try:
                temporary.unlink()
            except OSError:
                pass
            self._report("Manual observer settings save failed", error)
            raise SettingsValidationError(
                "Manual observer position could not be saved") from error

    @staticmethod
    def _validated(values):
        return {
            "manual_lat_deg": RuntimeSettingsStore._finite_number(
                values["manual_lat_deg"], "manual latitude", -90.0, 90.0),
            "manual_lon_deg": RuntimeSettingsStore._finite_number(
                values["manual_lon_deg"], "manual longitude", -180.0, 180.0),
            "manual_elevation_amsl_m": RuntimeSettingsStore._finite_number(
                values["manual_elevation_amsl_m"], "manual elevation",
                -500.0, 10000.0),
        }

    def _report(self, message, error):
        try:
            self._error_handler("{}: {}".format(message, type(error).__name__))
        except Exception:
            pass
