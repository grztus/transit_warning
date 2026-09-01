"""Production selection of the altitude used by aircraft LOS geometry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

from fleet_geometric_altitude import (
    FleetEstimateConfidence,
)


FEET_TO_METRES = 0.3048


class GeometricAltitudeSource(str, Enum):
    OWN_GNSS_GEOMETRIC = "OWN_GNSS_GEOMETRIC"
    FLEET_GEOMETRIC = "FLEET_GEOMETRIC"
    BARO_QNH = "BARO_QNH"


@dataclass(frozen=True)
class OwnGnssGeometricInput:
    """Qualified TC19/TC31 input in the pressure-altitude domain."""

    predicted_pressure_altitude_ft: float
    gnss_minus_baro_ft: float
    geoid_undulation_m: float

    @property
    def altitude_m(self):
        hae_m = (
            float(self.predicted_pressure_altitude_ft)
            + float(self.gnss_minus_baro_ft)
        ) * FEET_TO_METRES
        return hae_m - float(self.geoid_undulation_m)


@dataclass(frozen=True)
class SelectedGeometricAltitude:
    altitude_m: float
    source: GeometricAltitudeSource
    correction_m: float
    uncertainty_m: float | None
    confidence: str | None
    own_gnss_available: bool
    own_gnss_altitude_m: float | None
    fleet_available: bool
    fleet_altitude_m: float | None
    fleet_confidence: str | None
    fleet_uncertainty_m: float | None
    fallback_reason: str | None


class GeometricAltitudeSelector:
    """Apply the fixed OWN -> FLEET -> BARO production hierarchy."""

    _USABLE_FLEET_CONFIDENCE = {
        FleetEstimateConfidence.HIGH,
        FleetEstimateConfidence.MEDIUM,
    }

    def select(
            self, production_baro_altitude_m, *, own_gnss=None,
            fleet_estimate=None, own_unavailable_reason=None,
            fleet_unavailable_reason=None):
        baro_m = float(production_baro_altitude_m)
        if not math.isfinite(baro_m):
            raise ValueError("production barometric altitude must be finite")

        own_altitude_m = None
        if own_gnss is not None:
            try:
                own_altitude_m = float(own_gnss.altitude_m)
            except (TypeError, ValueError, OverflowError):
                own_altitude_m = None
            if own_altitude_m is not None and math.isfinite(own_altitude_m):
                return SelectedGeometricAltitude(
                    altitude_m=own_altitude_m,
                    source=GeometricAltitudeSource.OWN_GNSS_GEOMETRIC,
                    correction_m=own_altitude_m - baro_m,
                    uncertainty_m=None,
                    confidence="QUALIFIED",
                    own_gnss_available=True,
                    own_gnss_altitude_m=own_altitude_m,
                    fleet_available=fleet_estimate is not None,
                    fleet_altitude_m=(
                        float(fleet_estimate.altitude_m)
                        if fleet_estimate is not None else None),
                    fleet_confidence=(
                        fleet_estimate.confidence.value
                        if fleet_estimate is not None else None),
                    fleet_uncertainty_m=(
                        float(fleet_estimate.uncertainty_m)
                        if fleet_estimate is not None else None),
                    fallback_reason=None,
                )

        fleet_available = fleet_estimate is not None
        fleet_altitude_m = (
            float(fleet_estimate.altitude_m) if fleet_available else None)
        fleet_confidence = (
            fleet_estimate.confidence.value if fleet_available else None)
        fleet_uncertainty_m = (
            float(fleet_estimate.uncertainty_m) if fleet_available else None)
        if (fleet_available
                and fleet_estimate.confidence
                in self._USABLE_FLEET_CONFIDENCE
                and math.isfinite(fleet_altitude_m)):
            return SelectedGeometricAltitude(
                altitude_m=fleet_altitude_m,
                source=GeometricAltitudeSource.FLEET_GEOMETRIC,
                correction_m=fleet_altitude_m - baro_m,
                uncertainty_m=fleet_uncertainty_m,
                confidence=fleet_confidence,
                own_gnss_available=False,
                own_gnss_altitude_m=own_altitude_m,
                fleet_available=True,
                fleet_altitude_m=fleet_altitude_m,
                fleet_confidence=fleet_confidence,
                fleet_uncertainty_m=fleet_uncertainty_m,
                fallback_reason=own_unavailable_reason,
            )

        if fleet_available:
            reason = "fleet_confidence_{}_rejected".format(
                fleet_confidence.lower())
        else:
            reason = fleet_unavailable_reason or own_unavailable_reason
        return SelectedGeometricAltitude(
            altitude_m=baro_m,
            source=GeometricAltitudeSource.BARO_QNH,
            correction_m=0.0,
            uncertainty_m=None,
            confidence=None,
            own_gnss_available=False,
            own_gnss_altitude_m=own_altitude_m,
            fleet_available=fleet_available,
            fleet_altitude_m=fleet_altitude_m,
            fleet_confidence=fleet_confidence,
            fleet_uncertainty_m=fleet_uncertainty_m,
            fallback_reason=reason or "geometric_altitude_unavailable",
        )
