"""Load and validate installation-specific configuration."""

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DOTENV_PATH = PROJECT_DIR / ".env"


class ConfigurationError(ValueError):
    """Raised when installation configuration is missing or invalid."""


@dataclass(frozen=True)
class InstallationConfig:
    observer_lat: float
    observer_lon: float
    observer_elevation_m: float
    transition_altitude_ft: int
    adsb_host: str
    adsb_port: int
    adsb_timestamp_timezone: str
    mlat_host: str
    mlat_port: int
    metar_station: str


def _required(values, name, errors):
    value = values.get(name)
    if value is None or not str(value).strip():
        errors.append("{} is required".format(name))
        return None
    return str(value).strip()


def _finite_float(values, name, errors, minimum=None, maximum=None):
    raw_value = _required(values, name, errors)
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        errors.append("{} must be a number".format(name))
        return None
    if not math.isfinite(value):
        errors.append("{} must be a finite number".format(name))
        return None
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        errors.append("{} must be in the range {}..{}".format(name, minimum, maximum))
        return None
    return value


def _port(values, name, default, errors):
    raw_value = str(values.get(name, default)).strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        errors.append("{} must be an integer".format(name))
        return None
    if not 1 <= value <= 65535:
        errors.append("{} must be in the range 1..65535".format(name))
        return None
    return value


def _positive_integer(values, name, errors):
    raw_value = _required(values, name, errors)
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        errors.append("{} must be an integer".format(name))
        return None
    if value <= 0:
        errors.append("{} must be a positive integer".format(name))
        return None
    return value


def _host(values, name, default, errors):
    value = str(values.get(name, default)).strip()
    if not value:
        errors.append("{} must not be empty".format(name))
        return None
    return value


def _metar_station(values, errors):
    value = _required(values, "METAR_STATION", errors)
    if value is None:
        return None
    value = value.upper()
    if re.fullmatch(r"[A-Z]{4}", value, flags=re.ASCII) is None:
        errors.append("METAR_STATION must contain exactly 4 ASCII letters A-Z")
        return None
    return value


def _iana_timezone(values, errors):
    value = _required(values, "ADSB_TIMESTAMP_TIMEZONE", errors)
    if value is None:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append("ADSB_TIMESTAMP_TIMEZONE must be a valid IANA timezone name")
        return None
    return value


def load_installation_config(
    environ: Mapping[str, str] | None = None,
    dotenv_path: str | os.PathLike[str] = DEFAULT_DOTENV_PATH,
) -> InstallationConfig:
    """Return validated configuration, with environment overriding ``.env``.

    The default ``.env`` path is anchored to the project directory. The optional
    arguments allow callers and tests to provide explicit input without changing
    the process environment.
    """
    environment = os.environ if environ is None else environ
    file_values = dotenv_values(dotenv_path)
    values = {key: value for key, value in file_values.items() if value is not None}
    values.update(environment)

    errors = []
    observer_lat = _finite_float(values, "OBSERVER_LAT", errors, -90, 90)
    observer_lon = _finite_float(values, "OBSERVER_LON", errors, -180, 180)
    observer_elevation_m = _finite_float(values, "OBSERVER_ELEVATION_M", errors)
    transition_altitude_ft = _positive_integer(
        values, "TRANSITION_ALTITUDE_FT", errors)
    adsb_host = _host(values, "ADSB_HOST", "127.0.0.1", errors)
    adsb_port = _port(values, "ADSB_PORT", 30003, errors)
    adsb_timestamp_timezone = _iana_timezone(values, errors)
    mlat_host = _host(values, "MLAT_HOST", "127.0.0.1", errors)
    mlat_port = _port(values, "MLAT_PORT", 30106, errors)
    metar_station = _metar_station(values, errors)

    if (adsb_host is not None and adsb_port is not None
            and mlat_host is not None and mlat_port is not None
            and (adsb_host.casefold(), adsb_port) == (mlat_host.casefold(), mlat_port)):
        errors.append("ADS-B and MLAT host+port pairs must be different")

    if errors:
        raise ConfigurationError("Invalid installation configuration:\n- " + "\n- ".join(errors))

    return InstallationConfig(
        observer_lat=observer_lat,
        observer_lon=observer_lon,
        observer_elevation_m=observer_elevation_m,
        transition_altitude_ft=transition_altitude_ft,
        adsb_host=adsb_host,
        adsb_port=adsb_port,
        adsb_timestamp_timezone=adsb_timestamp_timezone,
        mlat_host=mlat_host,
        mlat_port=mlat_port,
        metar_station=metar_station,
    )
