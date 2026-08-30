"""Decode FlightAware synthetic MLAT velocity from Beast port 30105."""

from dataclasses import dataclass
import math

from beast_intent import BeastFrame, modes_crc


MLAT_BEAST_TIMESTAMP = int.from_bytes(b"\xFF\x00MLAT", "big")


@dataclass(frozen=True)
class MlatBeastTrack:
    icao: str
    track_deg: float
    east_west_velocity_knots: float
    north_south_velocity_knots: float
    groundspeed_knots: float
    subtype: int
    angular_interval_low_deg: float
    angular_interval_high_deg: float

    @property
    def angular_interval_width_deg(self):
        return self.angular_interval_high_deg - self.angular_interval_low_deg


def _bits(value, total_bits, start, end):
    width = end - start
    return (value >> (total_bits - end)) & ((1 << width) - 1)


def _component(code, sign, scale):
    if code == 0 or code == 1023:
        return None
    value = float((code - 1) * scale)
    return -value if sign else value


def _angular_interval(east_west, north_south, component_half_width):
    """Return an unwrapped heading interval around the decoded heading."""
    if (abs(east_west) <= component_half_width
            and abs(north_south) <= component_half_width):
        return None
    center = math.degrees(math.atan2(east_west, north_south)) % 360.0
    headings = []
    for ew in (east_west - component_half_width,
               east_west + component_half_width):
        for ns in (north_south - component_half_width,
                   north_south + component_half_width):
            heading = math.degrees(math.atan2(ew, ns)) % 360.0
            headings.append(
                center + ((heading - center + 180.0) % 360.0 - 180.0))
    return min(headings), max(headings)


def truncation_bin_consistent(track, coarse_track):
    """Whether a decoded uncertainty interval intersects [C, C+1)."""
    try:
        coarse = float(coarse_track)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(coarse) or not coarse.is_integer():
        return False
    coarse = int(coarse) % 360
    low = track.angular_interval_low_deg
    high = track.angular_interval_high_deg
    for turn in range(-2, 3):
        bin_low = coarse + 360.0 * turn
        if low < bin_low + 1.0 and high >= bin_low:
            return True
    return False


def decode_mlat_beast_tc19(frame):
    """Return a validated synthetic MLAT TC19 track, or ``None``."""
    if not isinstance(frame, BeastFrame):
        return None
    message = frame.modes
    if (frame.frame_type != 0x33
            or frame.beast_timestamp != MLAT_BEAST_TIMESTAMP
            or frame.signal != 0
            or len(message) != 14
            or modes_crc(message) != 0):
        return None
    value = int.from_bytes(message, "big")
    if (_bits(value, 112, 0, 5) != 18
            or _bits(value, 112, 5, 8) != 2
            or _bits(value, 112, 40, 41) != 0
            or _bits(value, 112, 32, 37) != 19):
        return None
    subtype = _bits(value, 112, 37, 40)
    if subtype not in (1, 2):
        return None
    scale = 4 if subtype == 2 else 1
    east_west = _component(
        _bits(value, 112, 46, 56), _bits(value, 112, 45, 46), scale)
    north_south = _component(
        _bits(value, 112, 57, 67), _bits(value, 112, 56, 57), scale)
    if east_west is None or north_south is None:
        return None
    groundspeed = math.hypot(east_west, north_south)
    if not math.isfinite(groundspeed) or groundspeed == 0.0:
        return None
    interval = _angular_interval(east_west, north_south, scale / 2.0)
    if interval is None or interval[1] - interval[0] >= 1.0:
        return None
    track = math.degrees(math.atan2(east_west, north_south)) % 360.0
    return MlatBeastTrack(
        icao="{:06X}".format(_bits(value, 112, 8, 32)),
        track_deg=track,
        east_west_velocity_knots=east_west,
        north_south_velocity_knots=north_south,
        groundspeed_knots=groundspeed,
        subtype=subtype,
        angular_interval_low_deg=interval[0],
        angular_interval_high_deg=interval[1],
    )
