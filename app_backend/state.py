"""Thread-safe immutable publication of ordinary application state."""

from copy import deepcopy
import threading

from .contracts import SCHEMA_VERSION, serialize_live_state


class ApplicationStateStore:
    """Keep the latest detached public snapshot with a monotonic revision."""

    def __init__(self):
        self._revision = 0
        self._state = serialize_live_state({})
        self._lock = threading.Lock()
        self._subscribers = []

    def subscribe(self, callback):
        if not callable(callback):
            raise TypeError("subscriber must be callable")
        with self._lock:
            self._subscribers.append(callback)

    def publish(self, snapshot):
        detached = serialize_live_state(snapshot)
        with self._lock:
            self._revision += 1
            self._state = detached
            revision = self._revision
            subscribers = tuple(self._subscribers)
            result = self._snapshot_locked() if subscribers else None
        for callback in subscribers:
            try:
                callback(deepcopy(result))
            except Exception:
                pass
        return revision

    def snapshot(self):
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self):
        return {"schema_version": SCHEMA_VERSION, "revision": self._revision,
                "state": deepcopy(self._state)}
