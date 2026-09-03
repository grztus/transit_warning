"""Model-neutral authoritative transit prediction lifecycle."""

from dataclasses import dataclass
import datetime
from enum import Enum
import threading


class PredictionGeometry(str, Enum):
    LEGACY = "LEGACY"
    TRUE_2D = "TRUE_2D"


class AuthoritativeLifecycleState(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class AuthoritativeTransitPrediction:
    observer_epoch: int
    observer_source: str
    icao: str
    callsign: str
    body: str
    encounter_generation: int
    encounter_id: str
    predicted_transit_utc: datetime.datetime
    separation_deg: float
    body_radius_deg: float
    aircraft_azimuth_deg: float
    aircraft_altitude_deg: float
    body_azimuth_deg: float
    body_altitude_deg: float
    aircraft_altitude_m: float
    slant_range_km: float
    model: str
    boundary_status: str
    lifecycle_state: str
    updated_at_utc: datetime.datetime


@dataclass
class _ActiveEncounter:
    prediction: AuthoritativeTransitPrediction
    last_success_utc: datetime.datetime


class AuthoritativeTransitLifecycle:
    """Track stable true-2D encounters without publishing to consumers.

    A generation is opened only by a future INTERIOR exact minimum. Once it
    closes, boundary-only results cannot re-arm it; the next generation needs
    another genuine future INTERIOR solution.
    """

    def __init__(self, geometry=PredictionGeometry.LEGACY,
                 grace_seconds=3.0, horizon_seconds=900.0):
        value = geometry.value if isinstance(geometry, PredictionGeometry) else geometry
        self.geometry = PredictionGeometry(str(value).upper())
        self.grace_seconds = float(grace_seconds)
        self.horizon_seconds = float(horizon_seconds)
        self._active = {}
        self._generations = {}
        self._lock = threading.RLock()

    @property
    def enabled(self):
        return self.geometry == PredictionGeometry.TRUE_2D

    def active_prediction(self, observer_epoch, icao, body):
        key = self._key(observer_epoch, icao, body)
        with self._lock:
            state = self._active.get(key)
            return state.prediction if state is not None else None

    def consider(self, context, result, now_utc):
        """Open/update an INTERIOR exact result, or apply grace on absence."""
        if not self.enabled:
            return None
        key = self._key(context.observer_context.epoch,
                        context.icao, context.body)
        exact = result.exact if result is not None else None
        valid = bool(
            exact is not None and exact.succeeded
            and exact.boundary_status == "INTERIOR"
            and exact.tca_seconds is not None
            and 0.0 < exact.tca_seconds <= self.horizon_seconds)
        with self._lock:
            if not valid:
                return self._missing_locked(key, now_utc)
            active = self._active.get(key)
            if active is None:
                generation = self._generations.get(key, 0) + 1
                self._generations[key] = generation
            else:
                generation = active.prediction.encounter_generation
            prediction = self._prediction(
                context, exact, now_utc, generation)
            self._active[key] = _ActiveEncounter(prediction, now_utc)
            return prediction

    def unavailable(self, observer_epoch, icao, body, now_utc):
        """Apply the same grace policy when no trustworthy result is available."""
        if not self.enabled:
            return None
        key = self._key(observer_epoch, icao, body)
        with self._lock:
            return self._missing_locked(key, now_utc)

    def invalidate(self):
        """Immediately discard all observer-dependent encounters."""
        with self._lock:
            self._active.clear()

    def discard_aircraft(self, icao):
        """Immediately discard encounters for an aircraft removed upstream."""
        icao = str(icao).upper()
        with self._lock:
            for key in [item for item in self._active if item[1] == icao]:
                self._active.pop(key, None)

    def _missing_locked(self, key, now_utc):
        active = self._active.get(key)
        if active is None:
            return None
        age = (now_utc - active.last_success_utc).total_seconds()
        if age < self.grace_seconds:
            return active.prediction
        self._active.pop(key, None)
        return None

    @staticmethod
    def _key(observer_epoch, icao, body):
        return (int(observer_epoch), str(icao).upper(), str(body).upper())

    @staticmethod
    def _prediction(context, exact, now_utc, generation):
        epoch = int(context.observer_context.epoch)
        icao = str(context.icao).upper()
        body = str(context.body).upper()
        encounter_id = "{}:{}:{}:{}".format(
            epoch, icao, body, generation)
        return AuthoritativeTransitPrediction(
            observer_epoch=epoch,
            observer_source=str(context.observer_context.effective_source),
            icao=icao,
            callsign=str(context.callsign or ""),
            body=body,
            encounter_generation=generation,
            encounter_id=encounter_id,
            predicted_transit_utc=(context.prediction_base_utc
                                   + datetime.timedelta(
                                       seconds=exact.tca_seconds)),
            separation_deg=float(exact.separation_deg),
            body_radius_deg=float(exact.body_radius_deg),
            aircraft_azimuth_deg=float(exact.aircraft_azimuth_deg),
            aircraft_altitude_deg=float(exact.aircraft_altitude_deg),
            body_azimuth_deg=float(exact.body_azimuth_deg),
            body_altitude_deg=float(exact.body_altitude_deg),
            aircraft_altitude_m=float(exact.aircraft_altitude_m),
            slant_range_km=float(exact.slant_range_km),
            model=PredictionGeometry.TRUE_2D.value,
            boundary_status=str(exact.boundary_status),
            lifecycle_state=AuthoritativeLifecycleState.ACTIVE.value,
            updated_at_utc=now_utc,
        )
