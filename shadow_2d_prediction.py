"""Independent true-2D transit screening and refinement for shadow diagnostics.

This module deliberately has no dependency on the legacy azimuth-intersection
solver.  Callers provide frozen production motion inputs and geometry resolvers.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import json
import math
from pathlib import Path
import threading
import time

from observer_position import ObserverContext
from transit_prediction_model import (
    VerticalIntentState,
    VerticalMotionState,
    VerticalPredictionPolicy,
    horizontal_position_from_t0,
    predict_vertical_state_at_time,
)


UTC = datetime.timezone.utc


@dataclass(frozen=True)
class Shadow2DConfig:
    enabled: bool = False
    horizon_seconds: float = 900.0
    segment_seconds: float = 60.0
    local_segment_seconds: float = 15.0
    safety_margin_deg: float = 0.052
    refinement_target_deg: float = 7.0


@dataclass(frozen=True)
class ShadowEncounterContext:
    icao: str
    callsign: str
    body: str
    prediction_base_utc: datetime.datetime
    observer_context: ObserverContext
    latitude_deg: float
    longitude_deg: float
    track_deg: float
    groundspeed_kmh: float
    current_altitude_m: float
    vertical_motion: VerticalMotionState | None
    vertical_intent: VerticalIntentState | None
    vertical_policy: VerticalPredictionPolicy
    qnh_hpa: float
    geometric_altitude_correction_m: float
    altitude_source: str
    position_source: str | None
    track_source: str | None
    aircraft_los_resolver: object
    body_position_resolver: object

    def __post_init__(self):
        if self.prediction_base_utc.tzinfo is None:
            raise ValueError("prediction_base_utc must be timezone-aware")
        if self.observer_context.position is None:
            raise ValueError("observer position is unavailable")
        values = (
            self.latitude_deg, self.longitude_deg, self.track_deg,
            self.groundspeed_kmh, self.current_altitude_m,
            self.geometric_altitude_correction_m, self.qnh_hpa,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("shadow encounter inputs must be finite")
        if self.groundspeed_kmh <= 0:
            raise ValueError("groundspeed must be positive")


@dataclass(frozen=True)
class ShadowGeometry:
    dt_seconds: float
    separation_deg: float
    objective: float
    relative_x_deg: float
    relative_y_deg: float
    aircraft_azimuth_deg: float
    aircraft_altitude_deg: float
    body_azimuth_deg: float
    body_altitude_deg: float
    body_radius_deg: float
    slant_range_km: float
    aircraft_altitude_m: float
    vertical_mode: str
    intent_clamped: bool


@dataclass(frozen=True)
class Coarse2DResult:
    candidate_exists: bool
    passed: bool
    estimated_tca_seconds: float | None
    estimated_separation_deg: float | None
    best_segment_start_seconds: float | None
    best_segment_end_seconds: float | None
    body_radius_deg: float | None
    evaluation_count: int
    duration_ms: float
    locally_refined: bool
    reason: str
    samples: tuple[ShadowGeometry, ...] = ()


@dataclass(frozen=True)
class Exact2DResult:
    succeeded: bool
    tca_seconds: float | None
    separation_deg: float | None
    body_radius_deg: float | None
    separation_body_radii: float | None
    aircraft_azimuth_deg: float | None
    aircraft_altitude_deg: float | None
    body_azimuth_deg: float | None
    body_altitude_deg: float | None
    slant_range_km: float | None
    aircraft_altitude_m: float | None
    altitude_source: str | None
    evaluation_count: int
    duration_ms: float
    boundary_status: str | None
    solver_status: str
    reason: str | None = None


@dataclass(frozen=True)
class Shadow2DResult:
    coarse: Coarse2DResult
    exact: Exact2DResult | None


def _unit_vector(azimuth_deg, altitude_deg):
    azimuth = math.radians(float(azimuth_deg))
    altitude = math.radians(float(altitude_deg))
    return (
        math.cos(altitude) * math.sin(azimuth),
        math.cos(altitude) * math.cos(azimuth),
        math.sin(altitude),
    )


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def _relative_tangent_offset(aircraft, body, body_azimuth_deg,
                             body_altitude_deg):
    """Return logarithmic-map offsets: +x increasing azimuth, +y altitude."""
    azimuth = math.radians(float(body_azimuth_deg))
    altitude = math.radians(float(body_altitude_deg))
    right = (math.cos(azimuth), -math.sin(azimuth), 0.0)
    up = (-math.sin(altitude) * math.sin(azimuth),
          -math.sin(altitude) * math.cos(azimuth),
          math.cos(altitude))
    cosine = max(-1.0, min(1.0, _dot(aircraft, body)))
    theta = math.acos(cosine)
    if theta < 1e-15:
        return 0.0, 0.0
    sine = math.sin(theta)
    if abs(sine) < 1e-15:
        return math.degrees(theta), 0.0
    scale = theta / sine
    return (math.degrees(scale * _dot(aircraft, right)),
            math.degrees(scale * _dot(aircraft, up)))


def evaluate_shadow_geometry(context, dt_seconds):
    """Evaluate the frozen production motion and exact LOS at arbitrary dt."""
    dt_seconds = float(dt_seconds)
    position = horizontal_position_from_t0(
        context.latitude_deg, context.longitude_deg, context.track_deg,
        context.groundspeed_kmh, dt_seconds)
    vertical = predict_vertical_state_at_time(
        context.current_altitude_m, context.vertical_motion,
        context.vertical_intent, context.prediction_base_utc, dt_seconds,
        context.qnh_hpa, context.vertical_policy)
    altitude_m = (vertical.prediction.predicted_altitude_m
                  + context.geometric_altitude_correction_m)
    observer = context.observer_context.position
    aircraft = context.aircraft_los_resolver(observer, position, altitude_m)
    when_utc = context.prediction_base_utc + datetime.timedelta(
        seconds=dt_seconds)
    body_position = context.body_position_resolver(
        context.body.lower(), when_utc, observer)
    body_radius = float(body_position.angular_diameter_arcsec) / 7200.0
    aircraft_unit = _unit_vector(
        aircraft.azimuth_deg, aircraft.altitude_angle_deg)
    body_unit = _unit_vector(
        body_position.azimuth_deg, body_position.altitude_deg)
    cosine = max(-1.0, min(1.0, _dot(aircraft_unit, body_unit)))
    objective = max(0.0, 1.0 - cosine)
    separation = math.degrees(math.acos(cosine))
    x, y = _relative_tangent_offset(
        aircraft_unit, body_unit, body_position.azimuth_deg,
        body_position.altitude_deg)
    return ShadowGeometry(
        dt_seconds=dt_seconds, separation_deg=separation,
        objective=objective, relative_x_deg=x, relative_y_deg=y,
        aircraft_azimuth_deg=float(aircraft.azimuth_deg),
        aircraft_altitude_deg=float(aircraft.altitude_angle_deg),
        body_azimuth_deg=float(body_position.azimuth_deg),
        body_altitude_deg=float(body_position.altitude_deg),
        body_radius_deg=body_radius,
        slant_range_km=float(aircraft.distance_km),
        aircraft_altitude_m=float(altitude_m),
        vertical_mode=vertical.prediction.mode.value,
        intent_clamped=bool(vertical.intent_details.get("intent_clamped")),
    )


def _segment_estimate(left, right):
    duration = right.dt_seconds - left.dt_seconds
    vx = (right.relative_x_deg - left.relative_x_deg) / duration
    vy = (right.relative_y_deg - left.relative_y_deg) / duration
    speed_squared = vx * vx + vy * vy
    if speed_squared <= 1e-18:
        local_seconds = 0.0
    else:
        local_seconds = -(
            left.relative_x_deg * vx + left.relative_y_deg * vy
        ) / speed_squared
    local_seconds = min(duration, max(0.0, local_seconds))
    separation = math.hypot(
        left.relative_x_deg + vx * local_seconds,
        left.relative_y_deg + vy * local_seconds)
    return left.dt_seconds + local_seconds, separation


def coarse_screen(context, config):
    """Run independent segmented local-linear screening over the horizon."""
    started = time.perf_counter()
    cache = {}

    def evaluate(value):
        key = round(float(value), 9)
        if key not in cache:
            cache[key] = evaluate_shadow_geometry(context, value)
        return cache[key]

    try:
        count = int(math.floor(config.horizon_seconds /
                               config.segment_seconds))
        times = [index * config.segment_seconds for index in range(count + 1)]
        if not math.isclose(times[-1], config.horizon_seconds):
            times.append(config.horizon_seconds)
        samples = [evaluate(value) for value in times]
        candidates = []
        for left, right in zip(samples, samples[1:]):
            # A rising body is retained whenever either endpoint is visible.
            if max(left.body_altitude_deg, right.body_altitude_deg) < 0.1:
                continue
            tca, separation = _segment_estimate(left, right)
            candidates.append((separation, tca, left.dt_seconds,
                               right.dt_seconds))
        if not candidates:
            return Coarse2DResult(
                False, False, None, None, None, None, None, len(cache),
                (time.perf_counter() - started) * 1000.0, False,
                "BODY_NOT_VISIBLE_IN_HORIZON", tuple(samples))
        separation, tca, lower, upper = min(candidates)
        gate = config.refinement_target_deg + config.safety_margin_deg
        locally_refined = False
        if separation <= gate:
            local_times = []
            value = lower
            while value <= upper + 1e-9:
                local_times.append(min(value, upper))
                value += config.local_segment_seconds
            if not math.isclose(local_times[-1], upper):
                local_times.append(upper)
            local = [evaluate(value) for value in local_times]
            local_candidates = [
                (*_segment_estimate(left, right),
                 left.dt_seconds, right.dt_seconds)
                for left, right in zip(local, local[1:])]
            if local_candidates:
                # tuples are tca, sep, lower, upper
                tca, separation, lower, upper = min(
                    local_candidates, key=lambda item: item[1])
                locally_refined = True
        near = evaluate(tca)
        passed = separation <= gate
        reason = "PASSED" if passed else "OUTSIDE_REFINEMENT_GATE"
        return Coarse2DResult(
            True, passed, tca, separation, lower, upper,
            near.body_radius_deg, len(cache),
            (time.perf_counter() - started) * 1000.0,
            locally_refined, reason,
            tuple(sorted(cache.values(), key=lambda item: item.dt_seconds)))
    except Exception as error:
        return Coarse2DResult(
            False, False, None, None, None, None, None, len(cache),
            (time.perf_counter() - started) * 1000.0, False,
            "COARSE_ERROR:{}".format(type(error).__name__),
            tuple(sorted(cache.values(), key=lambda item: item.dt_seconds)))


def _golden_minimum(objective, lower, upper, tolerance_seconds):
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    c = upper - ratio * (upper - lower)
    d = lower + ratio * (upper - lower)
    fc, fd = objective(c), objective(d)
    while upper - lower > tolerance_seconds:
        if fc <= fd:
            upper, d, fd = d, c, fc
            c = upper - ratio * (upper - lower)
            fc = objective(c)
        else:
            lower, c, fc = c, d, fd
            d = lower + ratio * (upper - lower)
            fd = objective(d)
    return (lower + upper) / 2.0


def exact_refine(context, coarse, config, legacy_tca_seconds=None,
                 tolerance_seconds=0.005, evaluator=evaluate_shadow_geometry):
    """Refine every plausible coarse/legacy bracket and choose global minimum."""
    started = time.perf_counter()
    cache = {round(item.dt_seconds, 9): item for item in coarse.samples}
    initial_count = len(cache)

    def evaluate(value):
        value = min(config.horizon_seconds, max(0.0, float(value)))
        key = round(value, 9)
        if key not in cache:
            cache[key] = evaluator(context, value)
        return cache[key]

    try:
        if not coarse.passed:
            return Exact2DResult(
                False, None, None, None, None, None, None, None, None,
                None, None, None, 0, 0.0, None, "NOT_RUN",
                coarse.reason)
        brackets = {(float(coarse.best_segment_start_seconds),
                     float(coarse.best_segment_end_seconds))}
        ordered = sorted(coarse.samples, key=lambda item: item.dt_seconds)
        for index in range(1, len(ordered) - 1):
            if (ordered[index].separation_deg <= ordered[index - 1].separation_deg
                    and ordered[index].separation_deg
                    <= ordered[index + 1].separation_deg):
                brackets.add((ordered[index - 1].dt_seconds,
                              ordered[index + 1].dt_seconds))
        if legacy_tca_seconds is not None:
            legacy = float(legacy_tca_seconds)
            if 0.0 <= legacy <= config.horizon_seconds:
                brackets.add((max(0.0, legacy - config.segment_seconds),
                              min(config.horizon_seconds,
                                  legacy + config.segment_seconds)))
        candidates = [evaluate(0.0), evaluate(config.horizon_seconds)]
        for lower, upper in sorted(brackets):
            optimum = _golden_minimum(
                lambda value: (evaluate(value).objective
                               if evaluate(value).body_altitude_deg >= 0.1
                               else 2.0),
                lower, upper, tolerance_seconds)
            candidates.extend((evaluate(lower), evaluate(optimum),
                               evaluate(upper)))
        visible_candidates = [
            item for item in candidates if item.body_altitude_deg >= 0.1]
        if not visible_candidates:
            raise ValueError("no visible exact candidate")
        best = min(visible_candidates, key=lambda item: item.objective)
        epsilon = max(tolerance_seconds, 0.01)
        if best.dt_seconds <= epsilon:
            boundary = "START_BOUNDARY"
        elif best.dt_seconds >= config.horizon_seconds - epsilon:
            boundary = "END_BOUNDARY_CONTINUING"
        else:
            boundary = "INTERIOR"
        radius = best.body_radius_deg
        return Exact2DResult(
            True, best.dt_seconds, best.separation_deg, radius,
            best.separation_deg / radius if radius > 0 else None,
            best.aircraft_azimuth_deg, best.aircraft_altitude_deg,
            best.body_azimuth_deg, best.body_altitude_deg,
            best.slant_range_km, best.aircraft_altitude_m,
            context.altitude_source, len(cache) - initial_count,
            (time.perf_counter() - started) * 1000.0, boundary,
            "SUCCESS", None)
    except Exception as error:
        return Exact2DResult(
            False, None, None, None, None, None, None, None, None,
            None, None, context.altitude_source, len(cache) - initial_count,
            (time.perf_counter() - started) * 1000.0, None, "FAILED",
            "EXACT_ERROR:{}".format(type(error).__name__))


def run_shadow_pipeline(context, config, legacy_tca_seconds=None,
                        evaluator=evaluate_shadow_geometry):
    coarse = coarse_screen(context, config)
    exact = exact_refine(
        context, coarse, config, legacy_tca_seconds=legacy_tca_seconds,
        evaluator=evaluator) if coarse.passed else None
    return Shadow2DResult(coarse=coarse, exact=exact)


class Shadow2DDiagnosticWriter:
    """Privacy-safe, rate-limited JSONL comparison sink and counters."""

    def __init__(self, root="diagnostics/shadow_2d", minimum_interval=30.0):
        self.root = Path(root)
        self.minimum_interval = float(minimum_interval)
        self._last = {}
        self._lock = threading.RLock()
        self.counters = {
            "screened": 0, "passed": 0, "coarse_rejected": 0,
            "exact_success": 0, "exact_failure": 0, "shadow_only": 0,
        }

    @staticmethod
    def _signature(record):
        separation = record.get("exact_sep_2d_deg")
        tca = record.get("exact_t0_2d_seconds")
        return (
            record.get("stage"), record.get("solver_status"),
            record.get("legacy_available"), record.get("shadow_only"),
            record.get("boundary_status"), record.get("reason"),
            (round(float(separation) / 0.05) if separation is not None
             else None),
            (round(float(tca) / 5.0) if tca is not None else None),
        )

    def record(self, record):
        safe = dict(record)
        forbidden = {
            "observer_lat", "observer_lon", "observer_elevation",
            "latitude", "longitude", "coordinates", "token", "secret",
        }
        if any(any(word in key.lower() for word in forbidden)
               for key in safe):
            raise ValueError("private field rejected from shadow diagnostics")
        now = datetime.datetime.fromisoformat(
            safe["utc"].replace("Z", "+00:00"))
        key = (safe.get("icao"), safe.get("body"))
        signature = self._signature(safe)
        with self._lock:
            previous = self._last.get(key)
            if previous is not None:
                age = (now - previous[1]).total_seconds()
                if age < 1.0:
                    return False
                if previous[0] == signature and age < self.minimum_interval:
                    return False
            self.root.joinpath(now.strftime("%Y-%m-%d")).mkdir(
                parents=True, exist_ok=True)
            path = self.root / now.strftime("%Y-%m-%d") / "shadow_2d.jsonl"
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(safe, sort_keys=True) + "\n")
            self._last[key] = (signature, now)
        return True

    def withdraw(self, icao, callsign, body, now_utc, reason):
        """Persist one terminal lifecycle state only for a known shadow pair."""
        key = (str(icao), str(body).upper())
        with self._lock:
            if key not in self._last:
                return False
        record = {
            "utc": now_utc.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "icao": str(icao), "callsign": str(callsign or ""),
            "body": str(body).upper(), "stage": "WITHDRAWN",
            "solver_status": "WITHDRAWN", "reason": str(reason),
            "legacy_available": None, "shadow_only": None,
            "boundary_status": None,
        }
        written = self.record(record)
        if written:
            with self._lock:
                self._last.pop(key, None)
        return written


def comparison_record(context, result, now_utc, legacy_result=None,
                      relevance_threshold_deg=7.0,
                      legacy_prediction_base_utc=None):
    """Build one deliberately coordinate-free legacy/coarse/exact record."""
    legacy_available = bool(legacy_result)
    legacy_tca = float(legacy_result[6]) if legacy_available else None
    legacy_base = legacy_prediction_base_utc or context.prediction_base_utc
    legacy_utc = (legacy_base + datetime.timedelta(seconds=legacy_tca)
                  if legacy_tca is not None else None)
    # Match the currently published legacy SEP, including its existing
    # two-decimal aircraft-altitude presentation rounding.
    legacy_sep = (abs(round(float(legacy_result[3]), 2)
                      - float(legacy_result[9]))
                  if legacy_available else None)
    exact = result.exact
    exact_relevant = bool(
        exact and exact.succeeded
        and exact.separation_deg <= float(relevance_threshold_deg))
    legacy_relevant = bool(
        legacy_available
        and legacy_sep <= float(relevance_threshold_deg))
    shadow_only = bool(exact_relevant and not legacy_relevant)
    record = {
        "utc": now_utc.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "icao": context.icao, "callsign": context.callsign,
        "body": context.body.upper(),
        "stage": ("EXACT" if exact is not None else "COARSE"),
        "legacy_available": legacy_available,
        "legacy_relevant": legacy_relevant,
        "legacy_t0_seconds": legacy_tca,
        "legacy_t0_utc": (legacy_utc.astimezone(UTC).isoformat()
                           .replace("+00:00", "Z")
                           if legacy_utc is not None else None),
        "legacy_sep_deg": legacy_sep,
        "coarse_tca_seconds": result.coarse.estimated_tca_seconds,
        "coarse_sep_deg": result.coarse.estimated_separation_deg,
        "coarse_evaluations": result.coarse.evaluation_count,
        "coarse_duration_ms": result.coarse.duration_ms,
        "exact_t0_2d_seconds": exact.tca_seconds if exact else None,
        "exact_t0_2d_utc": (
            (context.prediction_base_utc + datetime.timedelta(
                seconds=exact.tca_seconds)).astimezone(UTC).isoformat()
            .replace("+00:00", "Z")
            if exact and exact.succeeded else None),
        "exact_sep_2d_deg": exact.separation_deg if exact else None,
        "body_radius_deg": exact.body_radius_deg if exact else None,
        "sep_body_radii": exact.separation_body_radii if exact else None,
        "delta_t_seconds": (
            ((context.prediction_base_utc + datetime.timedelta(
                seconds=exact.tca_seconds)) - legacy_utc).total_seconds()
            if exact and exact.succeeded and legacy_utc is not None else None),
        "delta_sep_deg": (
            exact.separation_deg - legacy_sep
            if exact and exact.succeeded and legacy_sep is not None else None),
        "slant_range_km": exact.slant_range_km if exact else None,
        "altitude_source": context.altitude_source,
        "position_source": context.position_source,
        "track_source": context.track_source,
        "exact_evaluations": exact.evaluation_count if exact else 0,
        "exact_duration_ms": exact.duration_ms if exact else 0.0,
        "boundary_status": exact.boundary_status if exact else None,
        "solver_status": exact.solver_status if exact else "NOT_RUN",
        "reason": (exact.reason if exact else result.coarse.reason),
        "shadow_only": shadow_only,
    }
    return record
