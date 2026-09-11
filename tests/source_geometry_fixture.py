"""Synthetic canonical prediction inputs; no map geometry or runtime state."""
import datetime
import math
from types import SimpleNamespace
from observer_position import ObserverContext, ObserverPosition
from shadow_2d_prediction import ShadowEncounterContext
from transit_prediction_model import (
    MotionParameter, VerticalMotionState, current_vertical_prediction_policy,
    precise_angular_position_from_observer,
)

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 9, 6, 12, tzinfo=UTC)


class ConstantGeoid:
    def __init__(self, undulation=0):
        self.undulation = undulation

    def undulation_m(self, lat, lon):
        return self.undulation


def parallel_sun(when, observer):
    """Analytic distant source along +ECEF X, independently projected to ENU."""
    lat, lon = map(math.radians, observer.coordinates)
    east, north, up = -math.sin(lon), -math.sin(lat)*math.cos(lon), math.cos(lat)*math.cos(lon)
    return SimpleNamespace(azimuth_deg=math.degrees(math.atan2(east, north)) % 360,
                           altitude_deg=math.degrees(math.atan2(up, math.hypot(east, north))),
                           angular_diameter_arcsec=1800)


def canonical_context():
    base = T0 - datetime.timedelta(seconds=60)
    observer = ObserverPosition(.01, 0, 0)
    return ShadowEncounterContext(
        icao="ABC123", callsign="SYNTHETIC", body="SUN", prediction_base_utc=base,
        observer_context=ObserverContext(observer, "STATIC", "STATIC"),
        latitude_deg=0, longitude_deg=-.1, track_deg=90, groundspeed_kmh=720,
        current_altitude_m=10000,
        vertical_motion=VerticalMotionState(altitude=MotionParameter(10000, base, "adsb")),
        vertical_intent=None, vertical_policy=current_vertical_prediction_policy(),
        qnh_hpa=1013.25, geometric_altitude_correction_m=0, altitude_source="BARO_QNH",
        position_source="adsb", track_source="adsb",
        aircraft_los_resolver=lambda o, target, height: precise_angular_position_from_observer(
            o.coordinates, o.elevation_m, target, height),
        body_position_resolver=lambda body, when, o: parallel_sun(when, o))
