"""Minimal streaming Beast/Mode-S parser for ADS-B TC29 intent data."""

from dataclasses import dataclass

ESCAPE = 0x1A
FRAME_LENGTHS = {0x32: 14, 0x33: 21}
MODES_CRC_POLY = 0xFFF409

@dataclass(frozen=True)
class BeastFrame:
    frame_type: int
    beast_timestamp: int
    signal: int
    modes: bytes

@dataclass(frozen=True)
class Tc29Intent:
    icao: str
    selected_altitude_ft: float
    selected_altitude_source: str
    nav_qnh_hpa: float | None
    beast_timestamp: int | None = None

def modes_crc(message):
    value = int.from_bytes(message, "big")
    for bit in range(len(message) * 8 - 1, 23, -1):
        if value & (1 << bit):
            value ^= MODES_CRC_POLY << (bit - 24)
    return value & 0xFFFFFF

def _bits(value, total_bits, start, end):
    width = end - start
    return (value >> (total_bits - end)) & ((1 << width) - 1)

def decode_tc29(frame):
    message = frame.modes
    if len(message) != 14 or modes_crc(message) != 0:
        return None
    value = int.from_bytes(message, "big")
    if _bits(value, 112, 0, 5) != 17:
        return None
    me = _bits(value, 112, 32, 88)
    if _bits(me, 56, 0, 5) != 29 or _bits(me, 56, 5, 7) != 1:
        return None
    source = "FMS" if _bits(me, 56, 8, 9) else "MCP/FCU"
    altitude_code = _bits(me, 56, 9, 20)
    qnh_code = _bits(me, 56, 20, 29)
    if altitude_code == 0:
        return None
    return Tc29Intent(
        icao="{:06X}".format(_bits(value, 112, 8, 32)),
        selected_altitude_ft=float((altitude_code - 1) * 32),
        selected_altitude_source=source,
        nav_qnh_hpa=(800.0 + (qnh_code - 1) * 0.8
                     if qnh_code else None),
        beast_timestamp=frame.beast_timestamp,
    )

class BeastFrameParser:
    """Incrementally decode escaped Beast type 2/3 frames."""
    def __init__(self):
        self._buffer = bytearray()
        self.frames_decoded = 0
        self.resync_count = 0

    def feed(self, chunk):
        self._buffer.extend(chunk)
        frames = []
        while True:
            start = self._buffer.find(bytes((ESCAPE,)))
            if start < 0:
                self._buffer.clear()
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 2:
                break
            frame_type = self._buffer[1]
            expected = FRAME_LENGTHS.get(frame_type)
            if expected is None:
                self.resync_count += 1
                del self._buffer[0]
                continue
            decoded = bytearray()
            index = 2
            restart = False
            while len(decoded) < expected:
                if index >= len(self._buffer):
                    return frames
                byte = self._buffer[index]
                index += 1
                if byte == ESCAPE:
                    if index >= len(self._buffer):
                        return frames
                    if self._buffer[index] != ESCAPE:
                        self.resync_count += 1
                        del self._buffer[:index - 1]
                        restart = True
                        break
                    index += 1
                decoded.append(byte)
            if restart:
                continue
            del self._buffer[:index]
            frames.append(BeastFrame(frame_type,
                int.from_bytes(decoded[:6], "big"), decoded[6], bytes(decoded[7:])))
            self.frames_decoded += 1
        return frames
