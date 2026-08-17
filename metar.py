"""Transport and parsing helpers for METAR data."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re

import requests


AWC_METAR_URL = "https://aviationweather.gov/api/data/metar"
AWC_USER_AGENT = "TransitWarning/1.0"


@dataclass(frozen=True)
class AwcMetar:
    icao_id: str
    obs_time: datetime
    altim: float
    raw_ob: str


def fetch_metar_text(url):
    """Return the response text for HTTP 200, or None for another status."""
    response = requests.get(url, timeout=5)
    if response.status_code == 200:
        return response.text
    return None


def _parse_awc_obs_time(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    return None


def fetch_awc_metar(station):
    """Return one validated AWC METAR record, or None when unavailable."""
    if not isinstance(station, str) or not station:
        return None
    station_id = station.upper()
    try:
        response = requests.get(
            AWC_METAR_URL,
            params={"ids": station_id, "format": "json"},
            headers={"User-Agent": AWC_USER_AGENT},
            timeout=5,
        )
    except requests.exceptions.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, list):
        return None
    matches = [
        record for record in payload
        if isinstance(record, dict) and record.get("icaoId") == station_id
    ]
    if len(matches) != 1:
        return None
    record = matches[0]
    altim = record.get("altim")
    if isinstance(altim, bool) or not isinstance(altim, (int, float)):
        return None
    altim = float(altim)
    if not math.isfinite(altim) or not 800 < altim < 1100:
        return None
    obs_time = _parse_awc_obs_time(record.get("obsTime"))
    if obs_time is None:
        return None
    raw_ob = record.get("rawOb")
    if not isinstance(raw_ob, str):
        return None
    return AwcMetar(
        icao_id=record["icaoId"],
        obs_time=obs_time,
        altim=altim,
        raw_ob=raw_ob,
    )


def parse_metar_qnh(text):
    """Return a plausible QNH value from METAR text, or None."""
    pressure_match = re.search(r"Q(\d{4})", text)
    if not pressure_match:
        return None
    qnh = int(pressure_match.group(1))
    if 800 < qnh < 1100:
        return qnh
    return None
