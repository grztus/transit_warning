"""Isolated ADSB.lol diagnostics. Never imports or updates production state."""

import copy
import datetime as dt
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import random
import re
import threading
import time

import requests


UTC = dt.timezone.utc
BASE_URL = "https://api.adsb.lol"
MAX_RADIUS_NM = 250
MIN_POLL_SECONDS = 10  # Diagnostic request pacing, NOT measurement freshness.

# raw key: normalized name, unit, datum/semantic reference, kind
FIELD_SPECS = {
    "hex": ("aircraft_address", None, "AIRCRAFT_ADDRESS", "address"),
    "flight": ("callsign", None, "CALLSIGN", "text"),
    "alt_baro": ("barometric_altitude", "ft", "PRESSURE", "altitude"),
    "alt_geom": ("geometric_altitude", "ft", "WGS84_HAE", "number"),
    "gs": ("groundspeed", "knots", "GROUND_SPEED", "nonnegative"),
    "track": ("ground_track", "deg", "TRUE_NORTH", "angle"),
    "baro_rate": ("barometric_vertical_rate", "ft/min", "PRESSURE_RATE", "number"),
    "geom_rate": ("geometric_vertical_rate", "ft/min", "GEOMETRIC_RATE", "number"),
    "nav_altitude_mcp": ("selected_altitude_mcp", "ft", "SELECTED_REFERENCE_UNKNOWN", "number"),
    "nav_altitude_fms": ("selected_altitude_fms", "ft", "SELECTED_REFERENCE_UNKNOWN", "number"),
    "nav_qnh": ("selected_altimeter_setting", "hPa", "QFE_QNH_QNE_UNSPECIFIED", "nonnegative"),
    "mag_heading": ("heading_magnetic", "deg", "MAGNETIC_NORTH", "angle"),
    "true_heading": ("heading_true", "deg", "TRUE_NORTH", "angle"),
    "nav_heading": ("selected_heading", "deg", "UNKNOWN", "angle"),
    "track_rate": ("track_rate", "deg/s", "GROUND_TRACK_RATE", "number"),
    "roll": ("roll", "deg", "LEFT_NEGATIVE", "number"),
    "squawk": ("squawk", None, "MODE_A_OCTAL", "squawk"),
    "nic": ("nic", None, "POSITION_INTEGRITY_CATEGORY", "category"),
    "rc": ("rc", "m", "POSITION_CONTAINMENT_RADIUS", "nonnegative"),
    "nac_p": ("nac_p", None, "POSITION_ACCURACY_CATEGORY", "category"),
    "nac_v": ("nac_v", None, "VELOCITY_ACCURACY_CATEGORY", "category"),
    "sil": ("sil", None, "SOURCE_INTEGRITY_LEVEL", "category"),
    "sil_type": ("sil_type", None, "SIL_INTERPRETATION", "text"),
    "nic_baro": ("nic_baro", None, "BAROMETRIC_INTEGRITY_CATEGORY", "category"),
    "gva": ("gva", None, "GEOMETRIC_VERTICAL_ACCURACY", "category"),
    "sda": ("sda", None, "SYSTEM_DESIGN_ASSURANCE", "category"),
    "version": ("adsb_version", None, "AIRCRAFT_ADSB_VERSION_NOT_API_VERSION", "category"),
    "type": ("aircraft_source_type", None, "AIRCRAFT_LEVEL_SOURCE", "text"),
    "mlat": ("mlat_fields", None, "FIELD_SOURCE_LIST", "list"),
    "tisb": ("tisb_fields", None, "FIELD_SOURCE_LIST", "list"),
    "seen": ("aircraft_message_age", "s", "LATEST_MESSAGE_AGE_NOT_FIELD_AGE", "nonnegative"),
    "seen_pos": ("position_age", "s", "POSITION_UPDATE_AGE", "nonnegative"),
    "calc_track": ("calculated_track", "deg", "CALCULATED_NOT_REPORTED_TRACK", "angle"),
    "nav_modes": ("nav_modes", None, "AUTOMATION_INTENT", "list"),
}
QUALITY = {
    "position": ["nic", "rc", "nac_p", "sil", "sil_type"],
    "barometric_altitude": ["nic_baro"],
    "geometric_altitude": ["gva"],
    "groundspeed": ["nac_v"],
    "ground_track": ["nac_v"],
}
ALIASES = {"flight": "callsign", "alt_baro": "altitude"}


def utc_text(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _valid(value, kind):
    if kind in ("text", "address", "squawk"):
        if not isinstance(value, str) or not value.strip():
            return False
        if kind == "address":
            return re.fullmatch(r"~?[0-9a-fA-F]{6}", value) is not None
        if kind == "squawk":
            return re.fullmatch(r"[0-7]{4}", value) is not None
        return True
    if kind == "list":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if not finite(value):
        return False
    if kind in ("nonnegative", "category") and value < 0:
        return False
    if kind == "category" and int(value) != value:
        return False
    return kind != "angle" or 0 <= value < 360


def _epoch_ms(value):
    # Unit/format guard for a modern v2 epoch; not a stale-data threshold.
    if not finite(value) or not 946684800000 <= value < 253402300799000:
        raise ValueError("Invalid v2 timestamp: expected Unix milliseconds")
    return dt.datetime.fromtimestamp(value / 1000, UTC)


def _safe_json(value):
    """Retain diagnostic extensions without producing non-standard JSON."""
    return json.loads(json.dumps(value, allow_nan=False))


def normalize_response(payload, received_at, received_monotonic):
    """Pure normalization. All times are snapshot/receipt, never invented RF time."""
    if not isinstance(payload, dict) or not isinstance(payload.get("ac"), list):
        raise ValueError("Expected v2 aircraft array")
    if payload.get("msg") != "No error":
        raise ValueError("Provider did not return a successful v2 envelope")
    total = payload.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total != len(payload["ac"]):
        raise ValueError("Invalid aircraft count")
    snapshot = _epoch_ms(payload.get("now"))
    _epoch_ms(payload.get("ctime"))
    if not _valid(payload.get("ptime"), "nonnegative"):
        raise ValueError("Invalid provider processing time")
    utc_text(received_at)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    snapshot_id = hashlib.sha256(canonical.encode()).hexdigest()
    aircraft = []
    for ac in payload["ac"]:
        if not isinstance(ac, dict) or not _valid(ac.get("hex"), "address"):
            raise ValueError("Invalid aircraft identity")

        def observation(raw, name, unit, datum, kind):
            value = ac.get(raw)
            availability = "MISSING" if value is None else "PRESENT"
            missing_reason = ("ABSENT" if raw not in ac else "NULL") if value is None else None
            ground = raw == "alt_baro" and value == "ground"
            if ground:
                availability = "NOT_APPLICABLE"
            elif value is not None and not _valid(value, kind):
                availability = "INVALID"
            if availability != "PRESENT":
                value = None
            elif kind == "text":
                value = value.strip()
            elif kind == "address":
                value = value.upper()
            flags = []
            for source in ("mlat", "tisb"):
                entries = ac.get(source)
                if _valid(entries, "list") and (raw in entries or ALIASES.get(raw) in entries):
                    flags.append(source.upper())
            return {
                "field_name": name, "value": copy.deepcopy(value), "source": "ADSBLOL",
                "availability": availability, "missing_reason": missing_reason,
                "state": "GROUND" if ground else None,
                "unit": unit, "datum_or_reference": datum,
                "provenance": {
                    "provider": "ADSB_LOL", "transport": "HTTP_V2", "raw_field": raw,
                    "aircraft_source_type": ac.get("type") if isinstance(ac.get("type"), str) else None,
                    "field_source_flags": flags, "derivation": "UNSPECIFIED", "lineage": [],
                    "datum_basis": "PROVIDER_DECLARED" if raw == "alt_geom" else None,
                },
                "observed_at_utc": None, "observed_at_basis": "UNKNOWN",
                "received_at_utc": utc_text(received_at), "received_at_monotonic": received_monotonic,
                "provider_snapshot_at_utc": utc_text(snapshot), "snapshot_id": snapshot_id,
                "age_seconds": None, "age_reference": None,
                "freshness_state": "UNKNOWN" if availability == "PRESENT" else "UNAVAILABLE",
                "time_uncertainty_seconds": None, "confidence": None,
                "quality": {"field_refs": QUALITY.get(name, [])},
            }

        fields = {spec[0]: observation(raw, *spec) for raw, spec in FIELD_SPECS.items()}
        pos = observation("lat", "position", "deg", "WGS84_LAT_LON", "number")
        lat, lon = ac.get("lat"), ac.get("lon")
        valid_position = finite(lat) and finite(lon) and abs(lat) <= 90 and abs(lon) <= 180
        pos["provenance"]["raw_field"] = ["lat", "lon"]
        pos["provenance"]["field_source_flags"] = sorted(set(
            pos["provenance"]["field_source_flags"]
            + observation("lon", "position", "deg", "WGS84_LAT_LON", "number")["provenance"]["field_source_flags"]))
        if valid_position:
            pos.update(value={"lat": lat, "lon": lon}, availability="PRESENT",
                       missing_reason=None, freshness_state="UNKNOWN")
            age = fields["position_age"]["value"]
            if age is not None:
                pos.update(age_seconds=age, age_reference="PROVIDER_SNAPSHOT", freshness_state="KNOWN")
                try:
                    pos["observed_at_utc"] = utc_text(snapshot - dt.timedelta(seconds=age))
                    pos["observed_at_basis"] = "PROVIDER_POSITION_UPDATE_ESTIMATE"
                except (OverflowError, ValueError):
                    pos.update(age_seconds=None, age_reference=None, freshness_state="UNKNOWN")
        else:
            pos.update(value=None, availability="MISSING" if lat is None or lon is None else "INVALID",
                       missing_reason="INCOMPLETE_PAIR" if lat is None or lon is None else None,
                       freshness_state="UNAVAILABLE")
        fields["position"] = pos
        for name in ("nic", "rc"):
            if valid_position and fields[name]["availability"] == "PRESENT":
                fields[name]["quality"]["position_context_ref"] = "position"
        message_age = fields["aircraft_message_age"]["value"]
        last_message = None
        if message_age is not None:
            try:
                last_message = utc_text(snapshot - dt.timedelta(seconds=message_age))
            except (ValueError, OverflowError):
                pass
        warnings = []
        clock_delta = (received_at - snapshot).total_seconds()
        if clock_delta < 0:
            warnings.append("PROVIDER_SNAPSHOT_IN_FUTURE_OR_CLOCK_SKEW")
        aircraft.append({
            "icao": ac["hex"].upper() if not ac["hex"].startswith("~") else None,
            "aircraft_address": ac["hex"].upper(),
            "address_namespace": "NON_ICAO" if ac["hex"].startswith("~") else "ICAO",
            "fields": fields,
            "latest_message_at_utc_estimate": last_message,
            "apparent_position_age_at_receipt_seconds": (
                pos["age_seconds"] + clock_delta if pos["age_seconds"] is not None else None),
            "clock_uncertainty_seconds": None, "warnings": warnings,
            # Historical/rough positions remain segregated, never promoted.
            "extensions": _safe_json({k: v for k, v in ac.items()
                                      if k not in FIELD_SPECS and k not in ("lat", "lon")}),
        })
    return {
        "snapshot_id": snapshot_id, "aircraft": aircraft,
        "provider_snapshot_at_utc": utc_text(snapshot),
        "provider_now_ms": payload["now"], "provider_ctime_ms": payload["ctime"],
        "provider_processing_ms": payload["ptime"], "provider_message": payload["msg"],
    }


def _retry_after(value, now):
    if not value:
        return None
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except (TypeError, ValueError):
        pass
    try:
        return max(0, (parsedate_to_datetime(value) - now).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class AdsbLolProvider:
    """Synchronous bounded acquisition, exclusively for a standalone diagnostic."""

    def __init__(self, *, session=None, timeout_seconds=10, max_retries=1,
                 cancel_event=None, now=None, monotonic=None, jitter=None):
        if not finite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Timeout must be finite and positive")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 3:
            raise ValueError("Retries must be an integer from 0 to 3")
        self.session = session or requests
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self.cancel = cancel_event or threading.Event()
        self.now = now or (lambda: dt.datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.jitter = jitter or random.random

    def acquire(self, *, observer_mode, lat=None, lon=None, radius_nm=100, icao=None):
        report = {
            "schema_version": 1, "provider": "ADSBLOL", "api_contract": "v2",
            "provider_api_version": None, "provider_schema_version": None,
            "observer_mode": None, "query": None, "status": None, "error": None,
            "request_started_utc": None, "request_finished_utc": None,
            "http_status": None, "response_latency_seconds": None,
            "aircraft_count": 0, "aircraft": [], "attempts": [],
            "retry_after_seconds": None,
        }
        # Must precede coordinate inspection, URL construction and diagnostics.
        if isinstance(observer_mode, str) and observer_mode.upper().startswith("MOBILE"):
            report.update(observer_mode="MOBILE", status="PRIVACY_BLOCKED",
                          error="Automatic ADSB.lol acquisition is blocked for MOBILE privacy.")
            return report
        mode = observer_mode.upper() if isinstance(observer_mode, str) else None
        if mode not in ("STATIC", "MANUAL"):
            report.update(status="INVALID_QUERY", error="Explicit STATIC or MANUAL mode is required.")
            return report
        report["observer_mode"] = mode
        if icao is not None:
            if lat is not None or lon is not None or not isinstance(icao, str) or not re.fullmatch(r"[0-9a-fA-F]{6}", icao):
                report.update(status="INVALID_QUERY", error="Use one six-digit ICAO or a geographic query, not both.")
                return report
            path = "/v2/icao/" + icao.upper()
            report["query"] = {"kind": "ICAO", "icao": icao.upper()}
        else:
            if (not finite(lat) or not finite(lon) or abs(lat) > 90 or abs(lon) > 180
                    or not finite(radius_nm) or radius_nm < 0 or int(radius_nm) != radius_nm):
                report.update(status="INVALID_QUERY", error="Geographic query requires valid coordinates and a non-negative integer NM radius.")
                return report
            radius = min(int(radius_nm), MAX_RADIUS_NM)
            report["query"] = {"kind": "GEOGRAPHIC", "center": {"lat": lat, "lon": lon},
                               "radius_nm": radius, "radius_capped": radius != radius_nm}
            path = f"/v2/point/{lat}/{lon}/{radius}"

        for attempt in range(self.max_retries + 1):
            if self.cancel.is_set():
                report.update(status="CANCELLED", error="Acquisition cancelled.")
                break
            start, mono_start = self.now(), self.monotonic()
            record = {"started_utc": utc_text(start), "http_status": None}
            retryable, retry_after = False, None
            response = None
            normalized = None
            try:
                response = self.session.get(
                    BASE_URL + path, timeout=self.timeout, verify=True, allow_redirects=False,
                    headers={"Accept": "application/json", "User-Agent": "TransitWarning-A4.2-Diagnostic"})
                record["http_status"] = response.status_code
                finished, mono_finished = self.now(), self.monotonic()
                retry_after = _retry_after(response.headers.get("Retry-After"), finished)
                if response.status_code != 200:
                    status = ("RATE_LIMITED" if response.status_code == 429 else
                              "AUTH_ERROR" if response.status_code in (401, 403) else "HTTP_ERROR")
                    report.update(status=status, error=f"ADSB.lol returned HTTP {response.status_code}.")
                    retryable = response.status_code == 429 or 500 <= response.status_code < 600
                else:
                    try:
                        payload = response.json()
                        normalized = normalize_response(payload, finished, mono_finished)
                        report.update(status="OK", error=None)
                    except (ValueError, TypeError, OverflowError):
                        report.update(status="MALFORMED_RESPONSE",
                                      error="Invalid or unsuccessful ADSB.lol v2 JSON envelope.")
            except requests.exceptions.SSLError:
                report.update(status="TLS_ERROR", error="TLS certificate verification failed; verification remains enabled.")
            except requests.exceptions.Timeout:
                report.update(status="TIMEOUT", error="ADSB.lol request timed out.")
                retryable = True
            except requests.exceptions.RequestException:
                report.update(status="NETWORK_ERROR", error="ADSB.lol network request failed.")
                retryable = True
            finally:
                if response is None:
                    finished, mono_finished = self.now(), self.monotonic()
                else:
                    response.close()
            record.update(finished_utc=utc_text(finished),
                          latency_seconds=max(0, mono_finished - mono_start), status=report["status"])
            report["attempts"].append(record)
            report.update(request_started_utc=record["started_utc"], request_finished_utc=record["finished_utc"],
                          http_status=record["http_status"], response_latency_seconds=record["latency_seconds"],
                          retry_after_seconds=retry_after)
            if self.cancel.is_set():
                report.update(status="CANCELLED", error="Acquisition cancelled.")
                break
            if normalized is not None:
                report.update(normalized)
                report["aircraft_count"] = len(normalized["aircraft"])
                break
            if not retryable:
                break
            delay = max(MIN_POLL_SECONDS * 2 ** attempt + self.jitter(), retry_after or 0)
            report["retry_after_seconds"] = delay
            # Long Retry-After is returned to the caller rather than blocked on.
            if attempt == self.max_retries or delay > 60:
                break
            if self.cancel.wait(delay):
                report.update(status="CANCELLED", error="Acquisition cancelled.")
                break
        return report
