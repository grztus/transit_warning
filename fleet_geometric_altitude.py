"""Bounded fleet estimate of geometric aircraft altitude.

This module is deliberately independent of transit prediction.  It turns
qualified, datum-aware ADS-B altitude observations into a bounded rolling
calibration set and estimates a correction to the application's existing
barometric/QNH altitude basis.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from enum import Enum
import math
import os
from pathlib import Path
import re
import struct
from threading import RLock
from typing import Protocol


FLEET_SAMPLE_TTL_SECONDS = 60.0 * 60.0
FLEET_VERTICAL_SCALE_FT = 1500.0
FLEET_MAX_VERTICAL_DIFFERENCE_FT = 5000.0
FLEET_SPATIAL_SCALE_KM = 150.0
FLEET_MAX_SPATIAL_DISTANCE_KM = 200.0
FLEET_AGE_SCALE_SECONDS = 30.0 * 60.0
FLEET_MIN_AIRCRAFT = 3
FLEET_HUBER_MIN_CUTOFF_M = 25.0


class GeoidProvider(Protocol):
    """Return geoid height above WGS84 ellipsoid at one position."""

    def undulation_m(self, latitude: float, longitude: float) -> float:
        ...


class FleetEstimateConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class FleetEstimatorPolicy:
    sample_ttl_seconds: float = FLEET_SAMPLE_TTL_SECONDS
    vertical_scale_ft: float = FLEET_VERTICAL_SCALE_FT
    maximum_vertical_difference_ft: float = (
        FLEET_MAX_VERTICAL_DIFFERENCE_FT)
    spatial_scale_km: float = FLEET_SPATIAL_SCALE_KM
    maximum_spatial_distance_km: float = FLEET_MAX_SPATIAL_DISTANCE_KM
    age_scale_seconds: float = FLEET_AGE_SCALE_SECONDS
    minimum_aircraft: int = FLEET_MIN_AIRCRAFT


@dataclass(frozen=True)
class FleetAltitudeSample:
    timestamp_utc: dt.datetime
    icao: str
    latitude: float
    longitude: float
    pressure_altitude_ft: float
    gnss_baro_difference_ft: float
    gnss_hae_m: float
    geoid_undulation_m: float
    geometric_altitude_m: float
    production_reference_altitude_m: float
    geometric_correction_m: float
    adsb_version: int
    datum: str
    provenance: str
    pressure_altitude_age_seconds: float
    position_age_seconds: float


@dataclass(frozen=True)
class GeometricAltitudeEstimate:
    altitude_m: float
    correction_m: float
    uncertainty_m: float
    confidence: FleetEstimateConfidence
    source: str
    sample_count: int
    aircraft_count: int
    vertical_span_ft: tuple[float, float]
    age_min_seconds: float
    age_max_seconds: float
    spatial_min_km: float
    spatial_max_km: float
    weighted_residual_rms_m: float
    strongest_contributor_weight_fraction: float
    generated_at_utc: dt.datetime


class PgmGeoidProvider:
    """Cached reader for GeographicLib's EGM PGM grid format."""

    _OFFSET_RE = re.compile(rb"#\s*Offset\s+([-+0-9.eE]+)")
    _SCALE_RE = re.compile(rb"#\s*Scale\s+([-+0-9.eE]+)")

    def __init__(self, path):
        self.path = Path(path)
        payload = self.path.read_bytes()
        header_end, tokens = self._header(payload)
        self._width = int(tokens[1])
        self._height = int(tokens[2])
        self._pixels = payload[header_end:]
        header = payload[:header_end]
        offset = self._OFFSET_RE.search(header)
        scale = self._SCALE_RE.search(header)
        if offset is None or scale is None:
            raise ValueError("geoid PGM is missing Offset/Scale metadata")
        self._offset = float(offset.group(1))
        self._scale = float(scale.group(1))
        expected = self._width * self._height * 2
        if len(self._pixels) != expected:
            raise ValueError("geoid PGM payload has an invalid size")
        self._cache = {}
        self._lock = RLock()

    @staticmethod
    def _header(payload):
        position = 0
        tokens = []
        while len(tokens) < 4:
            newline = payload.find(b"\n", position)
            if newline < 0:
                raise ValueError("invalid geoid PGM header")
            line = payload[position:newline]
            position = newline + 1
            if not line.startswith(b"#"):
                tokens.extend(line.split())
        if tokens[0] != b"P5" or int(tokens[3]) != 65535:
            raise ValueError("unsupported geoid PGM format")
        return position, tokens

    def _pixel(self, row, column):
        row = max(0, min(self._height - 1, row))
        column %= self._width
        index = 2 * (row * self._width + column)
        raw = struct.unpack(">H", self._pixels[index:index + 2])[0]
        return self._offset + self._scale * raw

    def undulation_m(self, latitude, longitude):
        latitude = float(latitude)
        longitude = float(longitude)
        if not (-90.0 <= latitude <= 90.0
                and math.isfinite(longitude)):
            raise ValueError("invalid geoid position")
        key = (round(latitude, 7), round(longitude % 360.0, 7))
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        row_value = (90.0 - latitude) * (self._height - 1) / 180.0
        column_value = (longitude % 360.0) * self._width / 360.0
        row = int(math.floor(row_value))
        column = int(math.floor(column_value))
        row_fraction = row_value - row
        column_fraction = column_value - column
        top = ((1.0 - column_fraction) * self._pixel(row, column)
               + column_fraction * self._pixel(row, column + 1))
        bottom = ((1.0 - column_fraction) * self._pixel(row + 1, column)
                  + column_fraction * self._pixel(row + 1, column + 1))
        result = (1.0 - row_fraction) * top + row_fraction * bottom
        with self._lock:
            self._cache[key] = result
        return result

    @classmethod
    def discover(cls, configured_path=""):
        candidates = []
        if configured_path:
            candidates.append(Path(configured_path))
        environment_path = os.environ.get("GEOGRAPHICLIB_GEOID_PATH")
        if environment_path:
            base = Path(environment_path)
            candidates.extend((base, base / "egm96-5.pgm",
                               base / "egm96-15.pgm"))
        candidates.extend((
            Path("/usr/share/GeographicLib/geoids/egm96-5.pgm"),
            Path("/usr/share/GeographicLib/geoids/egm96-15.pgm"),
            Path("/usr/local/share/GeographicLib/geoids/egm96-5.pgm"),
            Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
            / "GeographicLib/geoids/egm96-5.pgm",
        ))
        for candidate in candidates:
            if candidate.is_file():
                try:
                    return cls(candidate)
                except (OSError, ValueError):
                    continue
        return None


def haversine_km(first, second):
    """Unrounded great-circle distance with longitude-wrap support."""
    lat1, lon1 = map(math.radians, first)
    lat2, lon2 = map(math.radians, second)
    dlat = lat2 - lat1
    dlon = (lon2 - lon1 + math.pi) % (2.0 * math.pi) - math.pi
    value = (math.sin(dlat / 2.0) ** 2
             + math.cos(lat1) * math.cos(lat2)
             * math.sin(dlon / 2.0) ** 2)
    return 6371.0 * 2.0 * math.asin(min(1.0, math.sqrt(value)))


class FleetGeometricAltitudeEstimator:
    """Bounded deterministic estimator shared by diagnostics and selection."""

    source = "FLEET_GEOMETRIC"

    def __init__(self, geoid_provider, policy=None):
        if geoid_provider is None:
            raise ValueError("a geoid provider is required")
        self.geoid_provider = geoid_provider
        self.policy = policy or FleetEstimatorPolicy()
        self._samples = {}
        self._lock = RLock()
        self.rejections = {}

    def geoid_height_m(self, latitude, longitude):
        """Expose the shared cached geoid lookup for diagnostic comparisons."""
        return float(self.geoid_provider.undulation_m(latitude, longitude))

    def _reject(self, reason):
        with self._lock:
            self.rejections[reason] = self.rejections.get(reason, 0) + 1
        return False

    def add_observation(
            self, *, timestamp_utc, icao, latitude, longitude,
            pressure_altitude_ft, gnss_baro_difference_ft,
            production_reference_altitude_m, adsb_version, datum,
            provenance, pressure_altitude_age_seconds,
            position_age_seconds):
        """Validate and store one genuine datum-qualified observation."""
        if provenance != "RAW_ADSB_TC19":
            return self._reject("unsupported_provenance")
        if adsb_version != 2 or datum != "WGS84_HAE":
            return self._reject("unsupported_datum")
        if (pressure_altitude_age_seconds > 2.0
                or position_age_seconds > 5.0):
            return self._reject("stale_alignment")
        values = (latitude, longitude, pressure_altitude_ft,
                  gnss_baro_difference_ft, production_reference_altitude_m)
        if not all(math.isfinite(float(value)) for value in values):
            return self._reject("non_finite")
        try:
            undulation = float(self.geoid_provider.undulation_m(
                latitude, longitude))
        except Exception:
            return self._reject("geoid_unavailable")
        if not math.isfinite(undulation):
            return self._reject("geoid_unavailable")
        gnss_hae_m = ((float(pressure_altitude_ft)
                       + float(gnss_baro_difference_ft)) * 0.3048)
        geometric_m = gnss_hae_m - undulation
        sample = FleetAltitudeSample(
            timestamp_utc=timestamp_utc,
            icao=str(icao).upper(),
            latitude=float(latitude), longitude=float(longitude),
            pressure_altitude_ft=float(pressure_altitude_ft),
            gnss_baro_difference_ft=float(gnss_baro_difference_ft),
            gnss_hae_m=gnss_hae_m,
            geoid_undulation_m=undulation,
            geometric_altitude_m=geometric_m,
            production_reference_altitude_m=float(
                production_reference_altitude_m),
            geometric_correction_m=(
                geometric_m - float(production_reference_altitude_m)),
            adsb_version=2, datum=datum, provenance=provenance,
            pressure_altitude_age_seconds=float(
                pressure_altitude_age_seconds),
            position_age_seconds=float(position_age_seconds),
        )
        with self._lock:
            previous = self._samples.get(sample.icao)
            if previous is None or sample.timestamp_utc >= previous.timestamp_utc:
                self._samples[sample.icao] = sample
        return True

    def prune(self, now_utc):
        with self._lock:
            expired = [icao for icao, sample in self._samples.items()
                       if (now_utc - sample.timestamp_utc).total_seconds()
                       > self.policy.sample_ttl_seconds]
            for icao in expired:
                del self._samples[icao]
            return len(expired)

    @property
    def sample_count(self):
        with self._lock:
            return len(self._samples)

    def _eligible(self, pressure_altitude_ft, position, now_utc, exclude_icao):
        self.prune(now_utc)
        result = []
        with self._lock:
            samples = tuple(self._samples.values())
        for sample in samples:
            if exclude_icao and sample.icao == str(exclude_icao).upper():
                continue
            age = max(0.0, (now_utc - sample.timestamp_utc).total_seconds())
            vertical = abs(sample.pressure_altitude_ft - pressure_altitude_ft)
            distance = haversine_km(
                (sample.latitude, sample.longitude), position)
            if (age > self.policy.sample_ttl_seconds
                    or vertical > self.policy.maximum_vertical_difference_ft
                    or distance > self.policy.maximum_spatial_distance_km):
                continue
            weight = (
                math.exp(-age / self.policy.age_scale_seconds)
                * math.exp(-distance / self.policy.spatial_scale_km)
                / (1.0 + (vertical / self.policy.vertical_scale_ft) ** 2))
            result.append((sample, age, distance, weight))
        return result

    @staticmethod
    def _fit(rows, target_altitude_ft, extra_weights=None):
        weights = [row[3] for row in rows]
        if extra_weights is not None:
            weights = [weight * robust for weight, robust
                       in zip(weights, extra_weights)]
        total = sum(weights)
        if total <= 0.0:
            return None
        x_mean = sum(weight * row[0].pressure_altitude_ft
                     for row, weight in zip(rows, weights)) / total
        y_mean = sum(weight * row[0].geometric_correction_m
                     for row, weight in zip(rows, weights)) / total
        denominator = sum(
            weight * (row[0].pressure_altitude_ft - x_mean) ** 2
            for row, weight in zip(rows, weights))
        slope = (sum(
            weight * (row[0].pressure_altitude_ft - x_mean)
            * (row[0].geometric_correction_m - y_mean)
            for row, weight in zip(rows, weights)) / denominator
                 if denominator > 0.0 else 0.0)
        prediction = y_mean + slope * (target_altitude_ft - x_mean)
        residuals = [
            row[0].geometric_correction_m
            - (y_mean + slope * (row[0].pressure_altitude_ft - x_mean))
            for row in rows]
        rms = math.sqrt(sum(weight * residual ** 2
                            for weight, residual in zip(weights, residuals))
                        / total)
        return prediction, rms, residuals, weights

    @staticmethod
    def _weighted_median(values, weights):
        ordered = sorted(zip(values, weights), key=lambda item: item[0])
        midpoint = sum(weights) / 2.0
        cumulative = 0.0
        for value, weight in ordered:
            cumulative += weight
            if cumulative >= midpoint:
                return value
        return ordered[-1][0]

    def estimate(self, pressure_altitude_ft, aircraft_position, timestamp_utc,
                 *, production_reference_altitude_m, exclude_icao=None):
        pressure_altitude_ft = float(pressure_altitude_ft)
        rows = self._eligible(
            pressure_altitude_ft, aircraft_position, timestamp_utc,
            exclude_icao)
        if len(rows) < self.policy.minimum_aircraft:
            return None
        base_weights = [row[3] for row in rows]
        corrections = [row[0].geometric_correction_m for row in rows]
        center = self._weighted_median(corrections, base_weights)
        deviations = [abs(value - center) for value in corrections]
        mad = self._weighted_median(deviations, base_weights)
        preliminary_cutoff = max(FLEET_HUBER_MIN_CUTOFF_M, 3.0 * mad)
        preliminary = [
            1.0 if value <= preliminary_cutoff
            else preliminary_cutoff / value
            for value in deviations]
        initial = self._fit(rows, pressure_altitude_ft, preliminary)
        if initial is None:
            return None
        _, initial_rms, residuals, _ = initial
        cutoff = max(FLEET_HUBER_MIN_CUTOFF_M, 1.5 * initial_rms)
        robust = [1.0 if abs(value) <= cutoff else cutoff / abs(value)
                  for value in residuals]
        fitted = self._fit(
            rows, pressure_altitude_ft,
            [first * second for first, second in zip(preliminary, robust)])
        if fitted is None:
            return None
        correction, rms, _, final_weights = fitted
        total_weight = sum(final_weights)
        strongest = max(final_weights) / total_weight
        altitudes = [row[0].pressure_altitude_ft for row in rows]
        ages = [row[1] for row in rows]
        distances = [row[2] for row in rows]
        interpolated = min(altitudes) <= pressure_altitude_ft <= max(altitudes)
        count = len(rows)
        if (count >= 8 and interpolated and rms <= 50.0
                and max(ages) <= 30.0 * 60.0
                and max(distances) <= 150.0 and strongest <= 0.35):
            confidence = FleetEstimateConfidence.HIGH
        elif count >= 5 and rms <= 80.0:
            confidence = FleetEstimateConfidence.MEDIUM
        else:
            confidence = FleetEstimateConfidence.LOW
        uncertainty = max(25.0, rms)
        if count < 8:
            uncertainty += (8 - count) * 5.0
        if not interpolated:
            gap = min(abs(pressure_altitude_ft - min(altitudes)),
                      abs(pressure_altitude_ft - max(altitudes)))
            uncertainty += min(50.0, gap / 100.0)
        uncertainty += 15.0 * max(ages) / self.policy.sample_ttl_seconds
        uncertainty += 15.0 * max(distances) / self.policy.maximum_spatial_distance_km
        if strongest > 0.35:
            uncertainty += 50.0 * (strongest - 0.35)
        return GeometricAltitudeEstimate(
            altitude_m=float(production_reference_altitude_m) + correction,
            correction_m=correction,
            uncertainty_m=uncertainty,
            confidence=confidence,
            source=self.source,
            sample_count=count,
            aircraft_count=count,
            vertical_span_ft=(min(altitudes), max(altitudes)),
            age_min_seconds=min(ages), age_max_seconds=max(ages),
            spatial_min_km=min(distances), spatial_max_km=max(distances),
            weighted_residual_rms_m=rms,
            strongest_contributor_weight_fraction=strongest,
            generated_at_utc=timestamp_utc,
        )
