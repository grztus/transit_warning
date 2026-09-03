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
    beast_host: str = "192.168.56.1"
    beast_port: int = 30005
    raw_adsb_host: str = "127.0.0.1"
    raw_adsb_port: int = 30002
    mlat_beast_enabled: bool = False
    mlat_beast_host: str = "127.0.0.1"
    mlat_beast_port: int = 30105
    telegram_notifications_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_alert_separation_deg: float = 2.0
    telegram_alert_horizon_seconds: float = 300.0
    telegram_alert_stability_seconds: float = 5.0
    telegram_sun_enabled: bool = True
    telegram_moon_enabled: bool = True
    dashboard_enabled: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    dashboard_history_enabled: bool = True
    dashboard_history_dir: str = "recordings/dashboard_history"
    dashboard_mobile_gps_enabled: bool = False
    dashboard_mobile_gps_fresh_seconds: float = 15.0
    observer_mode: str = "STATIC"
    mobile_gps_stale_warning_seconds: float = 30.0
    mobile_gps_critical_warning_seconds: float = 300.0
    mobile_gps_static_fallback_enabled: bool = False
    tmux_sep_green_max_deg: float = 3.0
    tmux_sep_yellow_max_deg: float = 5.0
    tmux_sep_visible_max_deg: float = 7.0
    dashboard_sep_green_max_deg: float = 3.0
    dashboard_sep_yellow_max_deg: float = 5.0
    dashboard_sep_visible_max_deg: float = 7.0
    new_transit_indicator_enabled: bool = True
    new_transit_threshold_seconds: float = 60.0
    fleet_geometric_altitude_enabled: bool = False
    geometric_altitude_selection_enabled: bool = False
    fleet_geoid_pgm_path: str = ""
    shadow_2d_enabled: bool = False
    shadow_2d_horizon_seconds: float = 900.0
    shadow_2d_segment_seconds: float = 60.0
    shadow_2d_local_segment_seconds: float = 15.0
    shadow_2d_safety_margin_deg: float = 0.052
    shadow_2d_refinement_target_deg: float = 7.0
    authoritative_prediction_geometry: str = "LEGACY"


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


def _boolean(values, name, default, errors):
    value = str(values.get(name, "true" if default else "false")).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    errors.append("{} must be true or false".format(name))
    return None


def _optional_finite_float(values, name, default, errors,
                           minimum=None, maximum=None):
    raw_value = str(values.get(name, default)).strip()
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        errors.append("{} must be a number".format(name))
        return None
    if not math.isfinite(value):
        errors.append("{} must be a finite number".format(name))
        return None
    if ((minimum is not None and value <= minimum)
            or (maximum is not None and value > maximum)):
        errors.append("{} must be greater than {} and at most {}".format(
            name, minimum, maximum))
        return None
    return value


def _optional_nonnegative_float(values, name, default, errors,
                                maximum=None):
    raw_value = str(values.get(name, default)).strip()
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        errors.append("{} must be a number".format(name))
        return None
    if not math.isfinite(value):
        errors.append("{} must be a finite number".format(name))
        return None
    if value < 0 or (maximum is not None and value > maximum):
        errors.append("{} must be in the range 0..{}".format(
            name, maximum))
        return None
    return value


def _presentation_thresholds(values, prefix, errors):
    names = (
        "{}_SEP_GREEN_MAX_DEG".format(prefix),
        "{}_SEP_YELLOW_MAX_DEG".format(prefix),
        "{}_SEP_VISIBLE_MAX_DEG".format(prefix),
    )
    defaults = (3.0, 5.0, 7.0)
    result = tuple(
        _optional_finite_float(
            values, name, default, errors, minimum=0.0, maximum=180.0)
        for name, default in zip(names, defaults)
    )
    if all(value is not None for value in result) and not (
            result[0] < result[1] < result[2]):
        errors.append(
            "{} presentation thresholds must satisfy GREEN < YELLOW < VISIBLE"
            .format(prefix))
    return result


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
    beast_host = _host(values, "BEAST_HOST", "192.168.56.1", errors)
    beast_port = _port(values, "BEAST_PORT", 30005, errors)
    raw_adsb_host = _host(values, "RAW_ADSB_HOST", "127.0.0.1", errors)
    raw_adsb_port = _port(values, "RAW_ADSB_PORT", 30002, errors)
    mlat_beast_enabled = _boolean(
        values, "MLAT_BEAST_ENABLED", False, errors)
    mlat_beast_host = _host(
        values, "MLAT_BEAST_HOST", mlat_host or "127.0.0.1", errors)
    mlat_beast_port = _port(values, "MLAT_BEAST_PORT", 30105, errors)
    telegram_notifications_enabled = _boolean(
        values, "TELEGRAM_NOTIFICATIONS_ENABLED", False, errors)
    telegram_bot_token = str(values.get("TELEGRAM_BOT_TOKEN", "")).strip()
    telegram_chat_id = str(values.get("TELEGRAM_CHAT_ID", "")).strip()
    telegram_alert_separation_deg = _optional_finite_float(
        values, "TELEGRAM_ALERT_SEPARATION_DEG", 2.0, errors,
        minimum=0.0, maximum=180.0)
    telegram_alert_horizon_seconds = _optional_finite_float(
        values, "TELEGRAM_ALERT_HORIZON_SECONDS", 300.0, errors,
        minimum=0.0, maximum=900.0)
    telegram_alert_stability_seconds = _optional_nonnegative_float(
        values, "TELEGRAM_ALERT_STABILITY_SECONDS", 5.0, errors,
        maximum=900.0)
    telegram_sun_enabled = _boolean(
        values, "TELEGRAM_SUN_ENABLED", True, errors)
    telegram_moon_enabled = _boolean(
        values, "TELEGRAM_MOON_ENABLED", True, errors)
    dashboard_enabled = _boolean(values, "DASHBOARD_ENABLED", False, errors)
    dashboard_host = _host(values, "DASHBOARD_HOST", "127.0.0.1", errors)
    dashboard_port = _port(values, "DASHBOARD_PORT", 8765, errors)
    dashboard_history_enabled = _boolean(
        values, "DASHBOARD_HISTORY_ENABLED", True, errors)
    dashboard_history_dir = str(values.get(
        "DASHBOARD_HISTORY_DIR", "recordings/dashboard_history")).strip()
    if not dashboard_history_dir:
        errors.append("DASHBOARD_HISTORY_DIR must not be empty")
    dashboard_mobile_gps_enabled = _boolean(
        values, "DASHBOARD_MOBILE_GPS_ENABLED", False, errors)
    dashboard_mobile_gps_fresh_seconds = _optional_finite_float(
        values, "DASHBOARD_MOBILE_GPS_FRESH_SECONDS", 15.0, errors,
        minimum=0.0, maximum=3600.0)
    observer_mode = str(values.get("OBSERVER_MODE", "STATIC")).strip().upper()
    if observer_mode not in ("STATIC", "MOBILE"):
        errors.append("OBSERVER_MODE must be STATIC or MOBILE")
    mobile_gps_stale_warning_seconds = _optional_finite_float(
        values, "MOBILE_GPS_STALE_WARNING_SECONDS", 30.0, errors,
        minimum=0.0, maximum=86400.0)
    mobile_gps_critical_warning_seconds = _optional_finite_float(
        values, "MOBILE_GPS_CRITICAL_WARNING_SECONDS", 300.0, errors,
        minimum=0.0, maximum=86400.0)
    mobile_gps_static_fallback_enabled = _boolean(
        values, "MOBILE_GPS_STATIC_FALLBACK_ENABLED", False, errors)
    if (mobile_gps_stale_warning_seconds is not None
            and mobile_gps_critical_warning_seconds is not None
            and mobile_gps_critical_warning_seconds
            <= mobile_gps_stale_warning_seconds):
        errors.append("MOBILE_GPS_CRITICAL_WARNING_SECONDS must be greater than MOBILE_GPS_STALE_WARNING_SECONDS")
    if observer_mode == "MOBILE" and (
            dashboard_enabled is False
            or dashboard_mobile_gps_enabled is False):
        errors.append("DASHBOARD_ENABLED and DASHBOARD_MOBILE_GPS_ENABLED must be true when OBSERVER_MODE=MOBILE")
    tmux_thresholds = _presentation_thresholds(values, "TMUX", errors)
    dashboard_thresholds = _presentation_thresholds(
        values, "DASHBOARD", errors)
    new_transit_indicator_enabled = _boolean(
        values, "NEW_TRANSIT_INDICATOR_ENABLED", True, errors)
    new_transit_threshold_seconds = _optional_nonnegative_float(
        values, "NEW_TRANSIT_THRESHOLD_SECONDS", 60.0, errors,
        maximum=900.0)
    fleet_geometric_altitude_enabled = _boolean(
        values, "FLEET_GEOMETRIC_ALTITUDE_ENABLED", False, errors)
    geometric_altitude_selection_enabled = _boolean(
        values, "GEOMETRIC_ALTITUDE_SELECTION_ENABLED", False, errors)
    fleet_geoid_pgm_path = str(values.get(
        "FLEET_GEOID_PGM_PATH", "")).strip()
    shadow_2d_enabled = _boolean(values, "SHADOW_2D_ENABLED", False, errors)
    shadow_2d_horizon_seconds = _optional_finite_float(
        values, "SHADOW_2D_HORIZON_SECONDS", 900.0, errors,
        minimum=0.0, maximum=900.0)
    shadow_2d_segment_seconds = _optional_finite_float(
        values, "SHADOW_2D_SEGMENT_SECONDS", 60.0, errors,
        minimum=0.0, maximum=900.0)
    shadow_2d_local_segment_seconds = _optional_finite_float(
        values, "SHADOW_2D_LOCAL_SEGMENT_SECONDS", 15.0, errors,
        minimum=0.0, maximum=900.0)
    shadow_2d_safety_margin_deg = _optional_nonnegative_float(
        values, "SHADOW_2D_SAFETY_MARGIN_DEG", 0.052, errors,
        maximum=180.0)
    shadow_2d_refinement_target_deg = _optional_finite_float(
        values, "SHADOW_2D_REFINEMENT_TARGET_DEG", 7.0, errors,
        minimum=0.0, maximum=180.0)
    authoritative_prediction_geometry = str(values.get(
        "AUTHORITATIVE_PREDICTION_GEOMETRY", "LEGACY")).strip().upper()
    if authoritative_prediction_geometry not in ("LEGACY", "TRUE_2D"):
        errors.append(
            "AUTHORITATIVE_PREDICTION_GEOMETRY must be LEGACY or TRUE_2D")
    if (shadow_2d_horizon_seconds is not None
            and shadow_2d_segment_seconds is not None
            and shadow_2d_segment_seconds > shadow_2d_horizon_seconds):
        errors.append("SHADOW_2D_SEGMENT_SECONDS must not exceed SHADOW_2D_HORIZON_SECONDS")
    if (shadow_2d_segment_seconds is not None
            and shadow_2d_local_segment_seconds is not None
            and shadow_2d_local_segment_seconds > shadow_2d_segment_seconds):
        errors.append("SHADOW_2D_LOCAL_SEGMENT_SECONDS must not exceed SHADOW_2D_SEGMENT_SECONDS")
    if telegram_notifications_enabled:
        if not telegram_bot_token:
            errors.append(
                "TELEGRAM_BOT_TOKEN is required when Telegram notifications are enabled")
        if not telegram_chat_id:
            errors.append(
                "TELEGRAM_CHAT_ID is required when Telegram notifications are enabled")

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
        beast_host=beast_host,
        beast_port=beast_port,
        raw_adsb_host=raw_adsb_host,
        raw_adsb_port=raw_adsb_port,
        mlat_beast_enabled=mlat_beast_enabled,
        mlat_beast_host=mlat_beast_host,
        mlat_beast_port=mlat_beast_port,
        telegram_notifications_enabled=telegram_notifications_enabled,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        telegram_alert_separation_deg=telegram_alert_separation_deg,
        telegram_alert_horizon_seconds=telegram_alert_horizon_seconds,
        telegram_alert_stability_seconds=telegram_alert_stability_seconds,
        telegram_sun_enabled=telegram_sun_enabled,
        telegram_moon_enabled=telegram_moon_enabled,
        dashboard_enabled=dashboard_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        dashboard_history_enabled=dashboard_history_enabled,
        dashboard_history_dir=dashboard_history_dir,
        dashboard_mobile_gps_enabled=dashboard_mobile_gps_enabled,
        dashboard_mobile_gps_fresh_seconds=(
            dashboard_mobile_gps_fresh_seconds),
        observer_mode=observer_mode,
        mobile_gps_stale_warning_seconds=mobile_gps_stale_warning_seconds,
        mobile_gps_critical_warning_seconds=(
            mobile_gps_critical_warning_seconds),
        mobile_gps_static_fallback_enabled=(
            mobile_gps_static_fallback_enabled),
        tmux_sep_green_max_deg=tmux_thresholds[0],
        tmux_sep_yellow_max_deg=tmux_thresholds[1],
        tmux_sep_visible_max_deg=tmux_thresholds[2],
        dashboard_sep_green_max_deg=dashboard_thresholds[0],
        dashboard_sep_yellow_max_deg=dashboard_thresholds[1],
        dashboard_sep_visible_max_deg=dashboard_thresholds[2],
        new_transit_indicator_enabled=new_transit_indicator_enabled,
        new_transit_threshold_seconds=new_transit_threshold_seconds,
        fleet_geometric_altitude_enabled=fleet_geometric_altitude_enabled,
        geometric_altitude_selection_enabled=(
            geometric_altitude_selection_enabled),
        fleet_geoid_pgm_path=fleet_geoid_pgm_path,
        shadow_2d_enabled=shadow_2d_enabled,
        shadow_2d_horizon_seconds=shadow_2d_horizon_seconds,
        shadow_2d_segment_seconds=shadow_2d_segment_seconds,
        shadow_2d_local_segment_seconds=shadow_2d_local_segment_seconds,
        shadow_2d_safety_margin_deg=shadow_2d_safety_margin_deg,
        shadow_2d_refinement_target_deg=shadow_2d_refinement_target_deg,
        authoritative_prediction_geometry=authoritative_prediction_geometry,
    )
