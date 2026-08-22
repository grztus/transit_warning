"""Pure spherical geometry shared by Transit Warning and offline tools."""

import math
from dataclasses import dataclass


EARTH_RADIUS_KM = 6371.0


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
