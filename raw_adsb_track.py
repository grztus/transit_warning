"""Decode high-precision ground track from optional RAW ADS-B TC19 frames."""

from dataclasses import dataclass
import math
import re

from beast_intent import modes_crc


RAW_FRAME_RE = re.compile(r"^[0-9A-Fa-f]{28}$")


@dataclass(frozen=True)
class RawAdsbTrack:
    icao: str
    track_deg: float
    east_west_velocity_knots: float
    north_south_velocity_knots: float
    subtype: int


def _bits(value, total_bits, start, end):
    width = end - start
    return (value >> (total_bits - end)) & ((1 << width) - 1)


def extract_modes_hex(line):
    """Return a 112-bit Mode-S payload from one RAW port 30002 line."""
    text = str(line).strip()
    if " " in text:
        text = text.rsplit(None, 1)[-1]
    if not text.endswith(";"):
        return None
    text = text[:-1]
    if text.startswith("@"):
        if len(text) != 1 + 12 + 28:
            return None
        text = text[13:]
    elif text.startswith("*"):
        text = text[1:]
    else:
        return None
    return text.upper() if RAW_FRAME_RE.fullmatch(text) else None


def decode_raw_tc19_track(line):
    """Decode a valid DF17 TC19 subtype 1/2 ground-velocity vector."""
    message_hex = extract_modes_hex(line)
    if message_hex is None:
        return None
    message = bytes.fromhex(message_hex)
    if modes_crc(message) != 0:
        return None
    value = int.from_bytes(message, "big")
    if _bits(value, 112, 0, 5) != 17:
        return None
    if _bits(value, 112, 32, 37) != 19:
        return None
    subtype = _bits(value, 112, 37, 40)
    if subtype not in (1, 2):
        return None
    scale = 4 if subtype == 2 else 1
    east_west_code = _bits(value, 112, 46, 56)
    north_south_code = _bits(value, 112, 57, 67)
    if east_west_code == 0 or north_south_code == 0:
        return None
    east_west = (east_west_code - 1) * scale
    north_south = (north_south_code - 1) * scale
    if _bits(value, 112, 45, 46):
        east_west = -east_west
    if _bits(value, 112, 56, 57):
        north_south = -north_south
    track = math.degrees(math.atan2(east_west, north_south)) % 360.0
    return RawAdsbTrack(
        icao="{:06X}".format(_bits(value, 112, 8, 32)),
        track_deg=track,
        east_west_velocity_knots=float(east_west),
        north_south_velocity_knots=float(north_south),
        subtype=subtype,
    )
