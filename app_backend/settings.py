"""Server-authoritative, revisioned operational runtime settings."""

from collections import OrderedDict
from copy import deepcopy
import json
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
        },
    }

    def __init__(self, telegram_sun_enabled=True, telegram_moon_enabled=True,
                 observer_requested_mode="STATIC",
                 observer_fallback_enabled=False, apply_callback=None,
                 validate_callback=None):
        self._values = {
            "telegram": {
                "sun_enabled": bool(telegram_sun_enabled),
                "moon_enabled": bool(telegram_moon_enabled),
            },
            "observer": {
                "requested_mode": self._mode(observer_requested_mode),
                "fallback_enabled": bool(observer_fallback_enabled),
            },
        }
        self._revision = 0
        self._apply_callback = apply_callback
        self._validate_callback = validate_callback
        self._accepted_commands = OrderedDict()
        self._subscribers = []
        self._lock = threading.RLock()

    @staticmethod
    def _mode(value):
        mode = str(value).upper()
        if mode not in ("STATIC", "MOBILE"):
            raise SettingsValidationError("Invalid observer mode")
        return mode

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
            if self._validate_callback is not None:
                self._validate_callback(deepcopy(prospective))
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
            if self._validate_callback is not None:
                self._validate_callback(deepcopy(prospective))
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
            "persistence": "RUNTIME_ONLY_RESET_TO_CONFIG_ON_RESTART",
        }
        assert_public_payload(result)
        return result

    def _merged_locked(self, changes):
        result = deepcopy(self._values)
        for group, values in changes.items():
            result[group].update(values)
        return result

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
                allowed = {"requested_mode", "fallback_enabled"}
                if set(values) - allowed:
                    raise SettingsValidationError("Unknown observer setting")
                observer = {}
                if "requested_mode" in values:
                    observer["requested_mode"] = cls._mode(
                        values["requested_mode"])
                if "fallback_enabled" in values:
                    if not isinstance(values["fallback_enabled"], bool):
                        raise SettingsValidationError("Invalid fallback setting")
                    observer["fallback_enabled"] = values["fallback_enabled"]
                result[group] = observer
        return result
