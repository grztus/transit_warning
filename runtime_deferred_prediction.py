"""Live TRUE_2D integration. All state transitions are serialized by aircraft lock.

Lock order: source -> aircraft -> scheduler condition. Worker callbacks are
invoked without the scheduler condition. Solves acquire neither runtime lock.
"""
from dataclasses import dataclass, replace
from copy import deepcopy
from contextlib import contextmanager
import datetime
import json
import logging
import time
import threading

from deferred_prediction import LatestPredictionScheduler, PredictionInput, result_compatible, MAX_RESULT_AGE_SECONDS
from shadow_2d_prediction import message_aircraft_cache
from transit_prediction_model import precise_angular_position_from_observer


PENDING_AIRCRAFT_CAPACITY = 32


class SolveCancelled(Exception):
    """Cooperative shutdown at numerical sample boundaries."""


@dataclass(frozen=True)
class FrozenAircraftLos:
    # The geoid grid is read-only; its own interpolation cache is synchronized.
    geoid: object
    stop: object = None

    def __call__(self, observer, target, altitude):
        if self.stop is not None and self.stop.is_set():
            raise SolveCancelled()
        if self.geoid is None:
            raise RuntimeError("datum-consistent aircraft LOS unavailable")
        return precise_angular_position_from_observer(
            observer.coordinates,
            observer.elevation_m + self.geoid.undulation_m(*observer.coordinates),
            target, altitude + self.geoid.undulation_m(*target))


@dataclass(frozen=True)
class FrozenPair:
    context: object
    config: object
    provenance_json: str
    legacy_results: tuple
    legacy_base_utc: datetime.datetime
    measurement_utc: datetime.datetime
    distance_km: float
    encounters: tuple


@dataclass
class AircraftGeneration:
    entry: object
    incarnation: int
    cancellation: int = 0
    committed: int = 0


class RuntimeDeferredPrediction:
    def __init__(self, runtime):
        self.runtime = runtime
        self.source_generation = 0
        self.sequence = 0
        self.aircraft = {}
        self.closed = False
        self.stop = threading.Event()
        self.committing_icao = None
        self.scheduler = LatestPredictionScheduler(self.solve, self.commit,
            capacity=PENDING_AIRCRAFT_CAPACITY, max_input_age_seconds=MAX_RESULT_AGE_SECONDS)
        # Covers main-loop AND AUTO bridge tick callers without locking solves.
        runtime.dashboard_runtime.state._prediction_guard = self.dashboard_tick_guard

    def invalidate(self, icao=None, source=False):
        """Caller owns aircraft lock; forgetting assigns a new incarnation later."""
        self.sequence += 1  # Never reuse a cancellation/incarnation generation.
        if source:
            self.source_generation += 1
        if icao is None:
            self.aircraft.clear()
        else:
            state = self.aircraft.pop(icao, None)
            if state is not None:
                state.cancellation = self.sequence
        self.scheduler.cancel(icao)

    def submit(self, icao, callsign, observer, track, velocity, altitude,
               prediction_base, legacy_results=(None, None), legacy_base=None):
        """Freeze once under aircraft lock; only numerical geometry is deferred."""
        r = self.runtime
        if self.closed:
            return False
        captured = time.monotonic()
        entry = r.plane_dict.get(icao)
        motion = r.aircraft_motion_states.get(icao)
        if entry is None or motion is None or motion.position is None:
            return False
        self.sequence += 1
        version = self.sequence
        state = self.aircraft.get(icao)
        if state is None or state.entry is not entry:
            state = AircraftGeneration(entry, version, version)
            self.aircraft[icao] = state
        try:
            context = r.build_shadow_2d_context(
                icao, callsign, 'MOON', observer, motion.position, track,
                velocity, altitude, prediction_base)
            provenance_json = json.dumps(context.fusion_provenance, allow_nan=False)
            context = replace(context, fusion_provenance=None,
                              aircraft_los_resolver=FrozenAircraftLos(r.aircraft_los_geoid_provider, self.stop))
            payload = FrozenPair(context, r.shadow_2d_config, provenance_json,
                                 deepcopy(tuple(legacy_results)), legacy_base or prediction_base,
                                 motion.position.updated_at_utc, float(entry[5]),
                                 r.dashboard_runtime.state.prediction_encounters(icao))
            job = PredictionInput(icao, state.incarnation, state.cancellation,
                                  self.source_generation, r.aircraft_source_mode,
                                  version, captured, observer, payload)
            return self.scheduler.submit(job)
        except Exception:
            # Private coordinates/provenance must not appear in normal logs.
            logging.getLogger(__name__).warning("Deferred TRUE_2D input capture failed")
            return False

    def solve(self, job):
        """No lifecycle, recorder, dashboard, or mutable receiver-state access."""
        r, payload = self.runtime, job.payload
        outputs = []
        failed = False
        # Preserve the existing exact aircraft-sample reuse across both bodies.
        with message_aircraft_cache():
            for body, legacy in zip(('MOON', 'SUN'), payload.legacy_results):
                if self.stop.is_set():
                    return None
                context = replace(payload.context, body=body,
                                  fusion_provenance=json.loads(payload.provenance_json))
                try:
                    coarse = r.shadow_coarse_screen(context, payload.config)
                    if self.stop.is_set():
                        return None
                    result = r.compute_shadow_2d_result(
                        context, coarse, legacy, payload.legacy_base_utc, payload.config)
                except SolveCancelled:
                    return None
                except Exception:
                    result = None  # Same per-body unavailable/grace path as before.
                    failed = True
                outputs.append((context, result, legacy))
        if failed:
            self.scheduler.record_compute_failure()
        return tuple(outputs)

    def commit(self, job, outputs):
        r = self.runtime
        with r.aircraft_source_lock, r.plane_dict_lock:
            if self.closed or self.stop.is_set() or outputs is None:
                return False
            observer = r.current_observer_context()  # May invalidate generations.
            state = self.aircraft.get(job.icao)
            entry = r.plane_dict.get(job.icao)
            if state is None or entry is not state.entry:
                return False
            now = r.clock.now_utc()
            if (now - entry[0]).total_seconds() > r.MAX_AGE_SECONDS:
                return False
            if not result_compatible(job, incarnation=state.incarnation,
                    cancellation=state.cancellation,
                    source_generation=self.source_generation, source_mode=r.aircraft_source_mode,
                    observer=observer, committed_version=state.committed,
                    now_monotonic=time.monotonic()):
                return False
            freshness = r.assess_motion_freshness(r.effective_motion_state(job.icao, now), now)
            if freshness.status == r.MotionFreshnessStatus.STALE:
                return False
            encounters = r.dashboard_runtime.state.prediction_encounters(job.icao)
            if any(old != current and (
                       old is not None or
                       (current is not None and current[1] is not None))
                   for old, current in zip(job.payload.encounters, encounters)):
                return False
            # Recheck the deadline after other potentially contended guards.
            if time.monotonic() - job.captured_monotonic > MAX_RESULT_AGE_SECONDS:
                return False
            state.committed = job.version
            self.committing_icao = job.icao
            try:
                for context, result, legacy in outputs:
                    exact = result.exact if result is not None else None
                    if (exact is not None and exact.succeeded and exact.tca_seconds is not None
                            and context.prediction_base_utc + datetime.timedelta(seconds=exact.tca_seconds) <= now):
                        # Reject this body's obsolete geometry, not its paired body.
                        result = None
                    if result is None:
                        transition = r.authoritative_transit_lifecycle.unavailable_transition(
                            job.observer.epoch, job.icao, context.body, now)
                    else:
                        transition = r.commit_shadow_2d_result(
                            context, result, legacy, job.payload.legacy_base_utc,
                            job.payload.config, now)
                    r.consume_authoritative_transition(
                        transition, context, entry, job.payload.distance_km, now)
            finally:
                self.committing_icao = None
            return True

    def close(self):
        self.stop.set()
        with self.runtime.plane_dict_lock:
            self.closed = True
            self.invalidate()
        # Never join while holding the aircraft/source locks.
        joined = self.scheduler.close()
        if not joined:
            logging.getLogger(__name__).warning("Deferred TRUE_2D worker shutdown timed out")
        return joined

    def tick(self, now):
        self.runtime.dashboard_runtime.tick(now)

    @contextmanager
    def dashboard_tick_guard(self):
        """Serialize finalization/replacement against commit before dashboard locking."""
        r = self.runtime
        with r.plane_dict_lock:
            before = {icao: r.dashboard_runtime.state.prediction_encounters(icao)
                      for icao in self.aircraft}
            try:
                yield
            finally:
                for icao, previous in before.items():
                    current = r.dashboard_runtime.state.prediction_encounters(icao)
                    if icao != self.committing_icao and previous != current:
                        self.invalidate(icao)
