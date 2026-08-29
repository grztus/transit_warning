"""Pure prediction mathematics shared by Transit Warning and offline tools."""

import datetime
import math
from dataclasses import dataclass
from enum import Enum


EARTH_RADIUS_KM = 6371.0
VERTICAL_RATE_LEVEL_THRESHOLD_FPM = 300.0
VERTICAL_RATE_VALID_AGE_SECONDS = 2.0
VERTICAL_RATE_IGNORE_AGE_SECONDS = 5.0
VERTICAL_RATE_STABILITY_SAMPLES = 3
VERTICAL_RATE_MAX_SPREAD_FPM = 256.0
VERTICAL_PREDICTION_MAX_SECONDS = 120.0
VERTICAL_ALTITUDE_MAX_AGE_SECONDS = 10.0
INTENT_FRESHNESS_SECONDS = 10.0
QNH_CORRECTION_FT_PER_HPA = 26.0


@dataclass(frozen=True)
class MotionParameter:
    value: float
    updated_at_utc: datetime.datetime
    source: str


@dataclass(frozen=True)
class IntentParameter:
    value: float
    updated_at_utc: datetime.datetime
    source: str


@dataclass(frozen=True)
class VerticalMotionState:
    altitude: MotionParameter | None = None
    vertical_rate: MotionParameter | None = None
    vertical_rate_history: tuple[MotionParameter, ...] = ()


@dataclass(frozen=True)
class VerticalIntentState:
    selected_altitude: IntentParameter | None = None
    nav_qnh: IntentParameter | None = None


class VerticalPredictionMode(str, Enum):
    LEVEL = "LEVEL"
    DYNAMIC_VALID = "DYNAMIC_VALID"
    VR_DEGRADED = "VR_DEGRADED"
    VR_IGNORE = "VR_IGNORE"


@dataclass(frozen=True)
class VerticalPredictionResult:
    predicted_altitude_m: float
    mode: VerticalPredictionMode
    reason: str
    last_vertical_rate_fpm: float | None
    vertical_rate_age_seconds: float | None
    stability_samples: tuple[MotionParameter, ...]
    spread_fpm: float | None
    source: str | None
    applied_seconds: float
    current_altitude_m: float
    altitude_delta_m: float
    altitude_age_seconds: float | None


@dataclass(frozen=True)
class VerticalPredictionPolicy:
    level_threshold_fpm: float
    valid_vr_age_seconds: float
    ignore_vr_age_seconds: float
    altitude_max_age_seconds: float
    stability_sample_count: int
    max_spread_fpm: float
    prediction_limit_seconds: float
    selected_altitude_freshness_seconds: float
    nav_qnh_freshness_seconds: float
    qnh_correction_ft_per_hpa: float


@dataclass(frozen=True)
class VerticalStateAtTime:
    prediction_before_clamp: VerticalPredictionResult
    prediction: VerticalPredictionResult
    intent_details: dict


DEFAULT_VERTICAL_PREDICTION_POLICY = VerticalPredictionPolicy(
    level_threshold_fpm=VERTICAL_RATE_LEVEL_THRESHOLD_FPM,
    valid_vr_age_seconds=VERTICAL_RATE_VALID_AGE_SECONDS,
    ignore_vr_age_seconds=VERTICAL_RATE_IGNORE_AGE_SECONDS,
    altitude_max_age_seconds=VERTICAL_ALTITUDE_MAX_AGE_SECONDS,
    stability_sample_count=VERTICAL_RATE_STABILITY_SAMPLES,
    max_spread_fpm=VERTICAL_RATE_MAX_SPREAD_FPM,
    prediction_limit_seconds=VERTICAL_PREDICTION_MAX_SECONDS,
    selected_altitude_freshness_seconds=INTENT_FRESHNESS_SECONDS,
    nav_qnh_freshness_seconds=INTENT_FRESHNESS_SECONDS,
    qnh_correction_ft_per_hpa=QNH_CORRECTION_FT_PER_HPA,
)


def current_vertical_prediction_policy():
    """Return the single immutable production 2E/2F policy."""
    return DEFAULT_VERTICAL_PREDICTION_POLICY


@dataclass(frozen=True)
class AngularPosition:
    distance_km: float
    azimuth_deg: float
    altitude_angle_deg: float


@dataclass(frozen=True)
class GreatCircleIntersection:
    latitude_deg: float
    longitude_deg: float
    azimuth_from_observer_deg: float
    aircraft_altitude_angle_deg: float
    observer_distance_km: float
    aircraft_distance_km: float
    time_seconds: float


def _haversine_km(origin, destination):
    lat1, lon1 = origin
    lat2, lon2 = destination
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.atan2(
        math.sqrt(a), math.sqrt(1 - a))


def angular_position_from_observer(
        observer_position, observer_elevation_m, target_position,
        target_altitude_m, distance_km=None):
    """Return the production observer geometry without changing rounding."""
    observer_lat, observer_lon = observer_position
    target_lat, target_lon = target_position
    if distance_km is None:
        distance_km = round(_haversine_km(
            observer_position, target_position), 1)
    altitude_angle = math.degrees(math.atan(
        (target_altitude_m - observer_elevation_m)
        / (distance_km * 1000)))
    azimuth = math.atan2(
        math.sin(math.radians(target_lon - observer_lon))
        * math.cos(math.radians(target_lat)),
        math.cos(math.radians(observer_lat))
        * math.sin(math.radians(target_lat))
        - math.sin(math.radians(observer_lat))
        * math.cos(math.radians(target_lat))
        * math.cos(math.radians(target_lon - observer_lon)))
    return AngularPosition(
        distance_km=distance_km,
        azimuth_deg=round(((math.degrees(azimuth) + 360) % 360), 1),
        altitude_angle_deg=altitude_angle,
    )


def solve_great_circle_intersection(
        observer_position, plane_position, track, velocity, elevation,
        body_azimuth, observer_elevation_m):
    """Return the existing production spherical intersection unchanged."""
    lat1, lon1 = observer_position
    lat2, lon2 = plane_position
    lat1, lat2, lon1, lon2 = map(
        math.radians, [lat1, lat2, lon1, lon2])
    body_azimuth = float(body_azimuth)
    track = float(track)
    theta_13, theta_23 = math.radians(body_azimuth), math.radians(track)
    delta_12 = 2 * math.asin(math.sqrt(
        math.sin((lat1 - lat2) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2)
        * math.sin((lon1 - lon2) / 2) ** 2))
    if delta_12 == 0:
        return None
    x = ((math.sin(lat2) - math.sin(lat1) * math.cos(delta_12))
         / (math.sin(delta_12) * math.cos(lat1)))
    theta_a = math.acos(min(1, max(-1, x)))
    y = ((math.sin(lat1) - math.sin(lat2) * math.cos(delta_12))
         / (math.sin(delta_12) * math.cos(lat2)))
    theta_b = math.acos(min(1, max(-1, y)))
    theta_12 = (theta_a if math.sin(lon2 - lon1) > 0
                else 2 * math.pi - theta_a)
    theta_21 = (2 * math.pi - theta_b
                if math.sin(lon2 - lon1) > 0 else theta_b)
    alfa_1, alfa_2 = theta_13 - theta_12, theta_21 - theta_23
    if math.sin(alfa_1) == 0 and math.sin(alfa_2) == 0:
        return None
    if math.sin(alfa_1) * math.sin(alfa_2) < 0:
        return None
    alfa_3 = math.acos(
        -math.cos(alfa_1) * math.cos(alfa_2)
        + math.sin(alfa_1) * math.sin(alfa_2) * math.cos(delta_12))
    delta_13 = math.atan2(
        math.sin(delta_12) * math.sin(alfa_1) * math.sin(alfa_2),
        math.cos(alfa_2) + math.cos(alfa_1) * math.cos(alfa_3))
    lat3 = math.asin(
        math.sin(lat1) * math.cos(delta_13)
        + math.cos(lat1) * math.sin(delta_13) * math.cos(theta_13))
    dlon_13 = math.atan2(
        math.sin(theta_13) * math.sin(delta_13) * math.cos(lat1),
        math.cos(delta_13) - math.sin(lat1) * math.sin(lat3))
    lon3 = lon1 + dlon_13
    lat3 = math.degrees(lat3)
    lon3 = (math.degrees(lon3) + 540) % 360 - 180
    dst_h2x = round(_haversine_km(observer_position, (lat3, lon3)), 1)
    if dst_h2x > 500:
        return None
    if dst_h2x == 0:
        dst_h2x = 0.001
    try:
        int(elevation)
    except ValueError:
        return None
    angular_position = angular_position_from_observer(
        observer_position, observer_elevation_m, (lat3, lon3), elevation,
        distance_km=dst_h2x)
    dst_p2x = round(_haversine_km(plane_position, (lat3, lon3)), 1)
    velocity = int(velocity)
    if velocity <= 0:
        return None
    return GreatCircleIntersection(
        latitude_deg=lat3,
        longitude_deg=lon3,
        azimuth_from_observer_deg=angular_position.azimuth_deg,
        aircraft_altitude_angle_deg=angular_position.altitude_angle_deg,
        observer_distance_km=dst_h2x,
        aircraft_distance_km=dst_p2x,
        time_seconds=(dst_p2x / velocity) * 3600,
    )


def great_circle_forward_bearing_at_point(
        origin_position, initial_track_deg, point_position):
    """Return the local forward bearing of the same oriented great-circle."""
    def vector(position):
        latitude, longitude = map(math.radians, position)
        return (math.cos(latitude) * math.cos(longitude),
                math.cos(latitude) * math.sin(longitude),
                math.sin(latitude))

    def cross(left, right):
        return (left[1] * right[2] - left[2] * right[1],
                left[2] * right[0] - left[0] * right[2],
                left[0] * right[1] - left[1] * right[0])

    def dot(left, right):
        return sum(a * b for a, b in zip(left, right))

    origin_lat, origin_lon = map(math.radians, origin_position)
    origin = vector(origin_position)
    north = (-math.sin(origin_lat) * math.cos(origin_lon),
             -math.sin(origin_lat) * math.sin(origin_lon),
             math.cos(origin_lat))
    east = (-math.sin(origin_lon), math.cos(origin_lon), 0.0)
    track = math.radians(float(initial_track_deg))
    initial_tangent = tuple(
        math.cos(track) * n + math.sin(track) * e
        for n, e in zip(north, east))
    normal = cross(origin, initial_tangent)
    point_lat, point_lon = map(math.radians, point_position)
    point = vector(point_position)
    tangent = cross(normal, point)
    magnitude = math.sqrt(dot(tangent, tangent))
    if magnitude == 0:
        raise ValueError("great-circle forward direction is undefined")
    tangent = tuple(value / magnitude for value in tangent)
    north = (-math.sin(point_lat) * math.cos(point_lon),
             -math.sin(point_lat) * math.sin(point_lon),
             math.cos(point_lat))
    east = (-math.sin(point_lon), math.cos(point_lon), 0.0)
    return ((math.degrees(math.atan2(
        dot(tangent, east), dot(tangent, north))) + 360) % 360)


def propagate_great_circle_position(
        latitude_deg, longitude_deg, bearing_deg, distance_km,
        earth_radius_km=EARTH_RADIUS_KM):
    """Move by signed distance along one oriented spherical great-circle."""
    if distance_km == 0:
        return latitude_deg, longitude_deg
    if earth_radius_km <= 0:
        raise ValueError("earth_radius_km must be positive")
    latitude = math.radians(float(latitude_deg))
    longitude = math.radians(float(longitude_deg))
    bearing = math.radians(float(bearing_deg))
    angular_distance = float(distance_km) / float(earth_radius_km)
    destination_latitude = math.asin(
        math.sin(latitude) * math.cos(angular_distance)
        + math.cos(latitude) * math.sin(angular_distance)
        * math.cos(bearing))
    destination_longitude = longitude + math.atan2(
        math.sin(bearing) * math.sin(angular_distance)
        * math.cos(latitude),
        math.cos(angular_distance)
        - math.sin(latitude) * math.sin(destination_latitude))
    return (math.degrees(destination_latitude),
            (math.degrees(destination_longitude) + 540) % 360 - 180)


def horizontal_position_from_t0(
        t0_latitude_deg, t0_longitude_deg,
        forward_bearing_at_t0_deg, effective_groundspeed_kmh,
        offset_seconds, earth_radius_km=EARTH_RADIUS_KM):
    """Propagate before or after canonical T0 on its oriented great-circle."""
    distance_km = (
        float(effective_groundspeed_kmh) * float(offset_seconds) / 3600.0)
    return propagate_great_circle_position(
        t0_latitude_deg, t0_longitude_deg,
        forward_bearing_at_t0_deg, distance_km, earth_radius_km)


def predict_transit_altitude(current_altitude_m, motion_state,
                             prediction_base_utc, dt_seconds, policy=None):
    """Apply the production 2E policy to one explicit frozen motion state."""
    policy = current_vertical_prediction_policy() if policy is None else policy
    current_altitude_m = float(current_altitude_m)
    altitude = motion_state.altitude if motion_state is not None else None
    vertical_rate = (
        motion_state.vertical_rate if motion_state is not None else None)
    history = tuple(
        list(motion_state.vertical_rate_history)[
            -policy.stability_sample_count:]
        if motion_state is not None else ())
    altitude_age = (
        max(0.0, (prediction_base_utc
                  - altitude.updated_at_utc).total_seconds())
        if altitude is not None else None)
    vertical_rate_age = (
        max(0.0, (prediction_base_utc
                  - vertical_rate.updated_at_utc).total_seconds())
        if vertical_rate is not None else None)
    last_vr = vertical_rate.value if vertical_rate is not None else None
    source = vertical_rate.source if vertical_rate is not None else None
    spread = (
        max(sample.value for sample in history)
        - min(sample.value for sample in history)
        if len(history) == policy.stability_sample_count else None)

    def result(mode, reason, predicted=current_altitude_m,
               applied_seconds=0.0):
        return VerticalPredictionResult(
            predicted_altitude_m=predicted,
            mode=mode,
            reason=reason,
            last_vertical_rate_fpm=last_vr,
            vertical_rate_age_seconds=vertical_rate_age,
            stability_samples=history,
            spread_fpm=spread,
            source=source,
            applied_seconds=applied_seconds,
            current_altitude_m=current_altitude_m,
            altitude_delta_m=predicted - current_altitude_m,
            altitude_age_seconds=altitude_age,
        )

    if altitude is None:
        return result(VerticalPredictionMode.VR_IGNORE, "altitude_missing")
    if (altitude_age is None
            or altitude_age > policy.altitude_max_age_seconds):
        return result(VerticalPredictionMode.VR_IGNORE, "altitude_stale")
    if vertical_rate is None:
        return result(
            VerticalPredictionMode.VR_IGNORE, "vertical_rate_missing")
    if vertical_rate_age > policy.ignore_vr_age_seconds:
        return result(VerticalPredictionMode.VR_IGNORE,
                      "vertical_rate_stale")
    if abs(last_vr) < policy.level_threshold_fpm:
        return result(VerticalPredictionMode.LEVEL,
                      "below_dynamic_threshold")
    if vertical_rate_age > policy.valid_vr_age_seconds:
        return result(VerticalPredictionMode.VR_DEGRADED,
                      "vertical_rate_degraded_age")
    if len(history) < policy.stability_sample_count:
        return result(VerticalPredictionMode.VR_DEGRADED,
                      "insufficient_history")
    if any(abs(sample.value) < policy.level_threshold_fpm
           for sample in history):
        return result(VerticalPredictionMode.VR_DEGRADED,
                      "history_below_dynamic_threshold")
    signs = {sample.value > 0 for sample in history}
    if len(signs) != 1:
        return result(VerticalPredictionMode.VR_DEGRADED,
                      "recent_sign_reversal")
    if spread > policy.max_spread_fpm:
        return result(VerticalPredictionMode.VR_DEGRADED,
                      "vertical_rate_spread")

    applied_seconds = min(
        max(float(dt_seconds), 0.0), policy.prediction_limit_seconds)
    altitude_delta_m = last_vr * applied_seconds / 60.0 * 0.3048
    return result(
        VerticalPredictionMode.DYNAMIC_VALID,
        "confirmed_vertical_trend",
        current_altitude_m + altitude_delta_m,
        applied_seconds,
    )


def clamp_vertical_prediction_to_intent_state(
        prediction, intent_state, prediction_base_utc,
        application_qnh_hpa, policy=None):
    """Apply the production 2F TC29 clamp to explicit frozen intent."""
    policy = current_vertical_prediction_policy() if policy is None else policy
    details = {
        "selected_altitude_ft": None, "selected_altitude_source": None,
        "selected_altitude_age_seconds": None, "nav_qnh_hpa": None,
        "nav_qnh_age_seconds": None, "target_altitude_m": None,
        "target_direction_valid": None, "intent_clamped": False,
        "intent_reason": "TC29_2E_NOT_DYNAMIC",
        "predicted_altitude_before_clamp_m": prediction.predicted_altitude_m,
        "predicted_altitude_after_clamp_m": prediction.predicted_altitude_m,
        "separation_before_clamp": None,
    }
    if prediction.mode != VerticalPredictionMode.DYNAMIC_VALID:
        return prediction, details
    if intent_state is None or intent_state.selected_altitude is None:
        details["intent_reason"] = "TC29_NO_DATA"
        return prediction, details
    selected = intent_state.selected_altitude
    selected_age = max(
        0.0, (prediction_base_utc
              - selected.updated_at_utc).total_seconds())
    details.update(
        selected_altitude_ft=selected.value,
        selected_altitude_source=selected.source,
        selected_altitude_age_seconds=selected_age)
    if intent_state.nav_qnh is None:
        details["intent_reason"] = "TC29_NO_QNH"
        return prediction, details
    nav_qnh = intent_state.nav_qnh
    qnh_age = max(
        0.0, (prediction_base_utc
              - nav_qnh.updated_at_utc).total_seconds())
    details.update(nav_qnh_hpa=nav_qnh.value,
                   nav_qnh_age_seconds=qnh_age)
    if selected.source != "MCP/FCU":
        details["intent_reason"] = "TC29_SOURCE_UNSUPPORTED"
        return prediction, details
    if selected_age > policy.selected_altitude_freshness_seconds:
        details["intent_reason"] = "TC29_STALE"
        return prediction, details
    if qnh_age > policy.nav_qnh_freshness_seconds:
        details["intent_reason"] = "TC29_QNH_STALE"
        return prediction, details
    target_ft = (selected.value
                 + (float(application_qnh_hpa) - nav_qnh.value)
                 * policy.qnh_correction_ft_per_hpa)
    target_m = target_ft * 0.3048
    details["target_altitude_m"] = target_m
    vr = prediction.last_vertical_rate_fpm
    current = prediction.current_altitude_m
    if (vr is None
            or (vr > 0 and target_m <= current)
            or (vr < 0 and target_m >= current)):
        details.update(target_direction_valid=False,
                       intent_reason="TC29_DIRECTION_MISMATCH")
        return prediction, details
    details["target_direction_valid"] = True
    predicted = (min(prediction.predicted_altitude_m, target_m)
                 if vr > 0 else max(prediction.predicted_altitude_m, target_m))
    if predicted == prediction.predicted_altitude_m:
        details["intent_reason"] = "TC29_NOT_NEEDED"
        return prediction, details
    details.update(intent_clamped=True, intent_reason="TC29_CLAMP_APPLIED",
                   predicted_altitude_after_clamp_m=predicted)
    return VerticalPredictionResult(
        predicted_altitude_m=predicted,
        mode=prediction.mode,
        reason=prediction.reason,
        last_vertical_rate_fpm=prediction.last_vertical_rate_fpm,
        vertical_rate_age_seconds=prediction.vertical_rate_age_seconds,
        stability_samples=prediction.stability_samples,
        spread_fpm=prediction.spread_fpm,
        source=prediction.source,
        applied_seconds=prediction.applied_seconds,
        current_altitude_m=prediction.current_altitude_m,
        altitude_delta_m=predicted - prediction.current_altitude_m,
        altitude_age_seconds=prediction.altitude_age_seconds,
    ), details


def predict_vertical_state_at_time(
        current_altitude_m, motion_state, intent_state,
        prediction_base_utc, dt_seconds, application_qnh_hpa, policy=None):
    """Compose unchanged 2E/2F policy at an explicit prediction base UTC."""
    policy = current_vertical_prediction_policy() if policy is None else policy
    before = predict_transit_altitude(
        current_altitude_m, motion_state, prediction_base_utc,
        dt_seconds, policy)
    prediction, details = clamp_vertical_prediction_to_intent_state(
        before, intent_state, prediction_base_utc,
        application_qnh_hpa, policy)
    return VerticalStateAtTime(
        prediction_before_clamp=before,
        prediction=prediction,
        intent_details=details,
    )
