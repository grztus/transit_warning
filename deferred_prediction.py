"""Bounded latest-state prediction scheduling, independent of any frontend.

Only unevaluated inputs coalesce. Consumers own ordered, lossless lifecycle
commits. No observer positions or job payloads are included in diagnostics.
"""
from collections import OrderedDict
from dataclasses import dataclass
import logging
import math
import threading
import time


MAX_RESULT_AGE_SECONDS = 2.0
WORKER_JOIN_SECONDS = 2.0
MOBILE_FALLBACK_TOLERANCE_M = 5.0
MOBILE_MAX_TOLERANCE_M = 50.0


def mobile_displacement_allowance(frozen_accuracy, current_accuracy):
    """Horizontal reported accuracy in metres; compatibility only, not geometry."""
    values = (frozen_accuracy, current_accuracy)
    if any(type(value) not in (int, float) or not math.isfinite(value)
           or value < 0 for value in values):
        return MOBILE_FALLBACK_TOLERANCE_M
    return min(MOBILE_MAX_TOLERANCE_M,
               max(MOBILE_FALLBACK_TOLERANCE_M, sum(values)))


@dataclass(frozen=True)
class PredictionInput:
    icao: str
    incarnation: int
    cancellation: int
    source_generation: int
    source_mode: str
    version: int
    captured_monotonic: float
    observer: object
    payload: object


def observer_compatible(frozen, current):
    if (frozen.epoch != current.epoch
            or frozen.requested_mode != current.requested_mode
            or frozen.effective_source != current.effective_source):
        return False
    a, b = frozen.position, current.position
    # MOBILE geometry uses configured static elevation, not GPS altitude.
    if a is None or b is None or a.elevation_m != b.elevation_m:
        return False
    if frozen.requested_mode != "MOBILE":
        return a == b
    lat1, lat2 = math.radians(a.latitude_deg), math.radians(b.latitude_deg)
    dlat = lat2 - lat1
    dlon = math.radians(b.longitude_deg - a.longitude_deg)
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    distance = 6371008.8 * 2 * math.asin(min(1., math.sqrt(max(0., hav))))
    return distance <= mobile_displacement_allowance(
        frozen.mobile_accuracy_m, current.mobile_accuracy_m)


def result_compatible(job, *, incarnation, cancellation, source_generation,
                      source_mode, observer, committed_version, now_monotonic):
    age = now_monotonic - job.captured_monotonic
    return (job.incarnation == incarnation and job.cancellation == cancellation
            and job.source_generation == source_generation
            and job.source_mode == source_mode
            and job.version > committed_version
            and 0 <= age <= MAX_RESULT_AGE_SECONDS
            and observer_compatible(job.observer, observer))


class LatestPredictionScheduler:
    """One worker, bounded distinct pending aircraft, FIFO fairness by aircraft.

    Replacement retains queue position. Capacity overflow rejects a new key;
    existing keys can still refresh. Shutdown cancels unevaluated inputs and
    cooperatively stops the running solve before dependent consumers are closed.
    Joins wait at most two seconds; the daemon handle remains owned on timeout.
    Neither solve nor commit runs with the scheduler condition held.
    """
    def __init__(self, solve, commit, capacity=32, monotonic=time.monotonic,
                 max_input_age_seconds=None):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.solve, self.commit = solve, commit
        self.capacity, self.monotonic = capacity, monotonic
        self.max_input_age_seconds = max_input_age_seconds
        self._condition = threading.Condition()
        self._pending = OrderedDict()
        self._running = None
        self._closed = False
        self._stats = dict(submitted=0, replacements=0, rejected_capacity=0,
                           executed=0, committed=0, stale_discarded=0, expired_before_solve=0,
                           failures=0, cancelled=0, shutdown_timeouts=0, execution_seconds=0.,
                           max_execution_seconds=0., sbs_messages_received=0)
        self._thread = threading.Thread(target=self._run, name="true2d-prediction", daemon=True)
        self._thread.start()

    def received(self):
        with self._condition:
            self._stats["sbs_messages_received"] += 1

    def record_compute_failure(self):
        """Count a paired solve with body-local failure while retaining its peer."""
        with self._condition:
            self._stats["failures"] += 1
        logging.getLogger(__name__).warning("Deferred TRUE_2D body computation failed")

    def submit(self, job):
        with self._condition:
            if self._closed:
                return False
            previous = self._pending.get(job.icao)
            if previous is not None and previous.version >= job.version:
                return False
            if self._running is not None and self._running.icao == job.icao and self._running.version >= job.version:
                return False
            if previous is None and len(self._pending) >= self.capacity:
                self._stats["rejected_capacity"] += 1
                return False
            self._stats["submitted"] += 1
            if previous is not None:
                self._stats["replacements"] += 1
            self._pending[job.icao] = job
            self._condition.notify()
            return True

    def cancel(self, icao=None):
        with self._condition:
            if icao is None:
                self._stats["cancelled"] += len(self._pending)
                self._pending.clear()
            elif self._pending.pop(icao, None) is not None:
                self._stats["cancelled"] += 1

    def snapshot(self):
        with self._condition:
            result = dict(self._stats)
            count = result["executed"]
            result.update(pending_aircraft=len(self._pending), running=int(self._running is not None),
                          capacity=self.capacity, closed=self._closed,
                          worker_alive=self._thread.is_alive(),
                          mean_execution_seconds=result["execution_seconds"] / count if count else 0.)
            return result

    def close(self):
        with self._condition:
            self._closed = True
            self._stats["cancelled"] += len(self._pending)
            self._pending.clear()
            self._condition.notify_all()
        self._thread.join(timeout=WORKER_JOIN_SECONDS)
        joined = not self._thread.is_alive()
        if not joined:
            with self._condition:
                self._stats["shutdown_timeouts"] += 1
        return joined

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending)
                if self._closed:
                    return
                _, job = self._pending.popitem(last=False)
                self._running = job
            started = self.monotonic()
            elapsed = 0.
            try:
                if (self.max_input_age_seconds is not None
                        and started - job.captured_monotonic > self.max_input_age_seconds):
                    with self._condition:
                        self._stats["expired_before_solve"] += 1
                    continue
                with self._condition:
                    self._stats["executed"] += 1
                result = self.solve(job)
                elapsed = self.monotonic() - started
                with self._condition:
                    closed = self._closed
                accepted = False if closed else self.commit(job, result)
                with self._condition:
                    self._stats["committed" if accepted else "stale_discarded"] += 1
            except Exception:
                elapsed = self.monotonic() - started
                with self._condition:
                    self._stats["failures"] += 1
                # No exception text/traceback: callbacks may contain private inputs.
                logging.getLogger(__name__).warning("Deferred TRUE_2D job failed")
            finally:
                with self._condition:
                    self._stats["execution_seconds"] += elapsed
                    self._stats["max_execution_seconds"] = max(self._stats["max_execution_seconds"], elapsed)
                    self._running = None
                    self._condition.notify_all()
