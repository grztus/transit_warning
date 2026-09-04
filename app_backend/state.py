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

    def publish(self, snapshot):
        detached = serialize_live_state(snapshot)
        with self._lock:
            self._revision += 1
            self._state = detached
            return self._revision

    def snapshot(self):
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "revision": self._revision,
                "state": deepcopy(self._state),
            }
