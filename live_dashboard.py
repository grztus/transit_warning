"""Fail-open local web dashboard for already-computed transit predictions."""

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import threading
from urllib.parse import parse_qs, urlsplit

from app_backend.contracts import serialize_bootstrap
from app_backend.settings import (
    RuntimeSettingsStore,
    SettingsConflictError,
    SettingsValidationError,
)
from app_backend.state import ApplicationStateStore
from app_backend.sse import SseBroker, encode_sse, live_envelope, settings_envelope
from dashboard_history import (
    DEFAULT_PAGE_SIZE,
    DashboardHistoryStore,
    records_to_csv,
)


UTC = datetime.timezone.utc
DEFAULT_HISTORY_LIMIT = 100
WITHDRAW_HISTORY_GRACE_SECONDS = 3.0
DEFAULT_SEP_GREEN_MAX_DEG = 3.0
DEFAULT_SEP_YELLOW_MAX_DEG = 5.0
DEFAULT_SEP_VISIBLE_MAX_DEG = 7.0
DEFAULT_NEW_TRANSIT_THRESHOLD_SECONDS = 60.0
DEFAULT_MOBILE_GPS_FRESH_SECONDS = 15.0
MAX_MOBILE_GPS_REQUEST_BYTES = 4096


def utc_text(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class DashboardCandidate:
    body: str
    icao: str
    callsign: str | None
    predicted_event_utc: datetime.datetime
    separation_deg: float
    body_azimuth_deg: float
    body_elevation_deg: float
    aircraft_elevation_deg: float
    distance_km: float | None
    last_prediction_update_utc: datetime.datetime
    telegram_range: bool
    transit_distance_km: float | None = None
    encounter_id: str | None = None
    prediction_geometry: str = "LEGACY"


@dataclass(frozen=True)
class MobileGpsPosition:
    latitude: float
    longitude: float
    accuracy_m: float
    altitude_m: float | None
    altitude_accuracy_m: float | None
    browser_timestamp_ms: float
    received_at_utc: datetime.datetime


class MobileGpsState:
    """Dedicated, in-memory diagnostic phone position state."""

    def __init__(self, enabled=False,
                 fresh_seconds=DEFAULT_MOBILE_GPS_FRESH_SECONDS):
        self.enabled = bool(enabled)
        self.fresh_seconds = float(fresh_seconds)
        self._position = None
        self._lock = threading.Lock()

    @staticmethod
    def _number(payload, name, minimum=None, maximum=None, optional=False):
        value = payload.get(name)
        if optional and value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Invalid mobile GPS payload")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("Invalid mobile GPS payload")
        if minimum is not None and value < minimum:
            raise ValueError("Invalid mobile GPS payload")
        if maximum is not None and value > maximum:
            raise ValueError("Invalid mobile GPS payload")
        return value

    def update(self, payload, received_at_utc):
        if not self.enabled:
            raise PermissionError("Mobile GPS is disabled")
        if not isinstance(payload, dict):
            raise ValueError("Invalid mobile GPS payload")
        latitude = self._number(payload, "latitude", -90.0, 90.0)
        longitude = self._number(payload, "longitude", -180.0, 180.0)
        accuracy = self._number(payload, "accuracy", 0.0)
        altitude = self._number(payload, "altitude", optional=True)
        altitude_accuracy = self._number(
            payload, "altitudeAccuracy", 0.0, optional=True)
        browser_timestamp = self._number(payload, "timestamp", 0.0)
        if altitude is None and altitude_accuracy is not None:
            raise ValueError("Invalid mobile GPS payload")
        if (received_at_utc.tzinfo is None
                or received_at_utc.utcoffset() is None):
            raise ValueError("Mobile GPS receive time must be timezone-aware")
        position = MobileGpsPosition(
            latitude=latitude,
            longitude=longitude,
            accuracy_m=accuracy,
            altitude_m=altitude,
            altitude_accuracy_m=altitude_accuracy,
            browser_timestamp_ms=browser_timestamp,
            received_at_utc=received_at_utc.astimezone(UTC),
        )
        with self._lock:
            self._position = position
        return self.diagnostics(received_at_utc)

    def clear(self):
        with self._lock:
            self._position = None

    def latest_position(self):
        with self._lock:
            return self._position

    def diagnostics(self, now_utc):
        if not self.enabled:
            return {"available": False, "status": "OFF"}
        with self._lock:
            position = self._position
        if position is None:
            return {"available": True, "status": "OFF"}
        age = max(0.0, (now_utc.astimezone(UTC)
                        - position.received_at_utc).total_seconds())
        return {
            "available": True,
            "status": "ACTIVE" if age <= self.fresh_seconds else "STALE",
            "accuracy_m": position.accuracy_m,
            "altitude_available": position.altitude_m is not None,
            "altitude_accuracy_m": position.altitude_accuracy_m,
            "age_seconds": age,
        }


class TelegramBodyControls:
    """Thread-safe runtime-only body notification switches."""

    def __init__(self, sun_enabled=True, moon_enabled=True, on_change=None):
        self._enabled = {
            "SUN": bool(sun_enabled), "MOON": bool(moon_enabled)}
        self._on_change = on_change
        self._lock = threading.Lock()

    def enabled(self, body):
        with self._lock:
            return self._enabled.get(str(body).upper(), False)

    def snapshot(self):
        with self._lock:
            return {key.lower() + "_enabled": value
                    for key, value in self._enabled.items()}

    def set_enabled(self, body, enabled):
        body = str(body).upper()
        if body not in self._enabled or not isinstance(enabled, bool):
            raise ValueError("Invalid Telegram body control")
        with self._lock:
            changed = self._enabled[body] != enabled
            self._enabled[body] = enabled
        if changed and self._on_change is not None:
            self._on_change(body, enabled)
        return self.snapshot()


class DashboardState:
    """Thread-safe live queues and bounded, non-persistent event history."""

    def __init__(self, history_limit=DEFAULT_HISTORY_LIMIT,
                 sep_green_max_deg=DEFAULT_SEP_GREEN_MAX_DEG,
                 sep_yellow_max_deg=DEFAULT_SEP_YELLOW_MAX_DEG,
                 sep_visible_max_deg=DEFAULT_SEP_VISIBLE_MAX_DEG,
                 history_store=None, new_transit_indicator_enabled=True,
                 new_transit_threshold_seconds=(
                     DEFAULT_NEW_TRANSIT_THRESHOLD_SECONDS)):
        self.history_limit = int(history_limit)
        self.sep_green_max_deg = float(sep_green_max_deg)
        self.sep_yellow_max_deg = float(sep_yellow_max_deg)
        self.sep_visible_max_deg = float(sep_visible_max_deg)
        self.history_store = history_store
        self.new_transit_indicator_enabled = bool(
            new_transit_indicator_enabled)
        self.new_transit_threshold_seconds = float(
            new_transit_threshold_seconds)
        self._live = {"SUN": {}, "MOON": {}}
        self._history = []
        self._generated_at_utc = None
        self._body_positions = {"SUN": None, "MOON": None}
        self._lock = threading.RLock()

    def update_body_position(self, body, altitude_deg, azimuth_deg,
                             evaluated_at_utc):
        body = str(body).upper()
        if body not in self._body_positions:
            return False
        with self._lock:
            self._body_positions[body] = {
                "altitude_deg": float(altitude_deg),
                "azimuth_deg": float(azimuth_deg),
                "evaluated_at_utc": utc_text(evaluated_at_utc),
            }
        return True

    def clear_body_positions(self):
        with self._lock:
            self._body_positions = {"SUN": None, "MOON": None}

    def publish(self, candidate):
        body = candidate.body.upper()
        key = candidate.icao.upper()
        if body not in self._live:
            return False
        with self._lock:
            existing = self._live[body].get(key)
            visible_now = candidate.separation_deg < self.sep_visible_max_deg
            first_time_to_event = (
                candidate.predicted_event_utc
                - candidate.last_prediction_update_utc).total_seconds()
            first_separation = (
                existing["first_separation_deg"] if existing is not None
                else candidate.separation_deg)
            minimum_separation = min(
                candidate.separation_deg,
                existing["minimum_separation_deg"] if existing is not None
                else candidate.separation_deg)
            self._live[body][key] = {
                "candidate": candidate,
                "first_separation_deg": first_separation,
                "minimum_separation_deg": minimum_separation,
                "first_seen_utc": (
                    existing["first_seen_utc"] if existing is not None
                    else candidate.last_prediction_update_utc),
                "history_worthy": (
                    existing["history_worthy"] if existing is not None
                    else False),
                "ever_visible": (
                    existing["ever_visible"] if existing is not None
                    else visible_now),
                "late_new_event": (
                    existing["late_new_event"] if existing is not None
                    and existing["ever_visible"]
                    else (visible_now
                          and self.new_transit_indicator_enabled
                          and 0.0 <= first_time_to_event
                          <= self.new_transit_threshold_seconds)),
            }
            if visible_now:
                self._live[body][key]["ever_visible"] = True
        return True

    def update_callsign(self, icao, callsign):
        """Refresh known identity on open rows without a new prediction."""
        callsign = str(callsign or "").strip()
        if not callsign:
            return False
        changed = False
        with self._lock:
            for items in self._live.values():
                item = items.get(icao.upper())
                if item is not None and item["candidate"].callsign != callsign:
                    item["candidate"] = replace(item["candidate"], callsign=callsign)
                    changed = True
        return changed

    def mark_history_worthy(self, icao, body):
        with self._lock:
            item = self._live.get(body.upper(), {}).get(icao.upper())
            if item is None:
                return False
            item["history_worthy"] = True
        return True

    def withdraw(self, icao, body, now_utc):
        body = body.upper()
        with self._lock:
            item = self._live.get(body, {}).pop(icao.upper(), None)
            if item is None:
                return False
            predicted = item["candidate"].predicted_event_utc
            near_event = now_utc >= predicted - datetime.timedelta(
                seconds=WITHDRAW_HISTORY_GRACE_SECONDS)
            if (item["history_worthy"]
                    or (item["ever_visible"] and near_event)):
                self._to_history_locked(item, now_utc, "WITHDRAWN")
        return True

    def withdraw_aircraft(self, icao, now_utc):
        changed = False
        for body in ("SUN", "MOON"):
            changed = self.withdraw(icao, body, now_utc) or changed
        return changed

    def invalidate_live(self):
        """Discard observer-dependent live candidates without history output."""
        with self._lock:
            self._live = {"SUN": {}, "MOON": {}}

    def tick(self, now_utc):
        with self._lock:
            self._generated_at_utc = now_utc
            for body in ("SUN", "MOON"):
                due = [icao for icao, item in self._live[body].items()
                       if item["candidate"].predicted_event_utc <= now_utc]
                for icao in due:
                    item = self._live[body].pop(icao)
                    if item["history_worthy"] or item["ever_visible"]:
                        self._to_history_locked(item, now_utc, "PASSED")

    def _to_history_locked(self, item, recorded_at_utc, reason):
        candidate = item["candidate"]
        event_id = candidate.encounter_id or "{}:{}:{}".format(
            candidate.icao.upper(), candidate.body.upper(),
            utc_text(candidate.predicted_event_utc))
        if any(event["event_id"] == event_id for event in self._history):
            return
        record = self._candidate_dict(candidate)
        record.update({
            "event_id": event_id,
            "final_separation_deg": candidate.separation_deg,
            "first_separation_deg": item["first_separation_deg"],
            "minimum_separation_deg": item["minimum_separation_deg"],
            "first_seen_utc": utc_text(item["first_seen_utc"]),
            "last_seen_utc": utc_text(
                candidate.last_prediction_update_utc),
            "history_recorded_at_utc": utc_text(recorded_at_utc),
            "outcome": reason,
        })
        self._history.insert(0, record)
        del self._history[self.history_limit:]
        if self.history_store is not None:
            try:
                self.history_store.append(record)
            except Exception:
                pass

    def query_history(self, utc_date=None, callsign=None, body="ALL",
                      offset=0, limit=DEFAULT_PAGE_SIZE):
        if self.history_store is not None:
            return self.history_store.query(
                utc_date, callsign, body, offset, limit)
        body = str(body or "ALL").upper()
        records = [record for record in self._history
                   if (utc_date is None or str(record.get(
                       "predicted_event_utc") or "").startswith(utc_date))
                   and (not callsign or callsign.casefold() in str(
                       record.get("callsign") or "").casefold())
                   and (body == "ALL" or str(
                       record.get("body") or "").upper() == body)]
        offset = max(0, int(offset))
        limit = max(1, min(100, int(limit)))
        page = records[offset:offset + limit]
        has_more = offset + limit < len(records)
        return {
            "records": deepcopy(page), "offset": offset, "limit": limit,
            "next_offset": offset + len(page) if has_more else None,
            "has_more": has_more,
        }

    def export_history_csv(self, utc_date=None, callsign=None, body="ALL"):
        if self.history_store is not None:
            return self.history_store.export_csv(utc_date, callsign, body)
        return records_to_csv(self.query_history(
            utc_date, callsign, body, 0, 100)["records"])

    def snapshot(self, now_utc=None):
        with self._lock:
            generated_at = self._generated_at_utc or now_utc
            snapshot_now = now_utc or generated_at
            result = {
                "generated_at_utc": (
                    utc_text(generated_at) if generated_at is not None
                    else None),
                "sun": {
                    "current_position": deepcopy(self._body_positions["SUN"]),
                    "candidates": self._body_snapshot_locked(
                        "SUN", snapshot_now)},
                "moon": {
                    "current_position": deepcopy(self._body_positions["MOON"]),
                    "candidates": self._body_snapshot_locked(
                        "MOON", snapshot_now)},
                "recent_events": deepcopy(self._history),
                "presentation": {
                    "sep_green_max_deg": self.sep_green_max_deg,
                    "sep_yellow_max_deg": self.sep_yellow_max_deg,
                    "sep_visible_max_deg": self.sep_visible_max_deg,
                },
            }
        return result

    def _body_snapshot_locked(self, body, now_utc):
        items = sorted(
            self._live[body].values(),
            key=lambda item: (
                item["candidate"].predicted_event_utc,
                item["candidate"].icao),
        )
        result = []
        for item in items:
            if (item["candidate"].prediction_geometry == "TRUE_2D"
                    and item["candidate"].separation_deg
                    >= self.sep_visible_max_deg):
                continue
            candidate = self._candidate_dict(
                item["candidate"], include_live_fields=True)
            candidate["is_new_late_candidate"] = bool(
                item["late_new_event"]
                and now_utc is not None
                and item["candidate"].predicted_event_utc > now_utc)
            result.append(candidate)
        return result

    def _candidate_dict(self, candidate, include_live_fields=False):
        result = asdict(candidate)
        result["predicted_event_utc"] = utc_text(
            candidate.predicted_event_utc)
        result["last_prediction_update_utc"] = utc_text(
            candidate.last_prediction_update_utc)
        result["state"] = (
            "TELEGRAM RANGE" if candidate.telegram_range else "CANDIDATE")
        result["separation_class"] = self._separation_class(
            candidate.separation_deg)
        return result

    def _separation_class(self, separation_deg):
        separation = float(separation_deg)
        if separation < self.sep_green_max_deg:
            return "GREEN"
        if separation < self.sep_yellow_max_deg:
            return "YELLOW"
        if separation < self.sep_visible_max_deg:
            return "RED"
        return "HIDDEN"


class DisabledDashboard:
    def __init__(self, telegram_controls=None, settings_store=None):
        self.telegram_controls = telegram_controls or TelegramBodyControls()
        self.settings_store = settings_store

    def publish(self, candidate):
        return False

    def update_callsign(self, icao, callsign):
        return False

    def withdraw(self, icao, body, now_utc):
        return False

    def withdraw_aircraft(self, icao, now_utc):
        return False

    def mark_history_worthy(self, icao, body):
        return False

    def tick(self, now_utc):
        return None

    def update_body_position(self, body, altitude_deg, azimuth_deg,
                             evaluated_at_utc):
        return False

    def clear_body_positions(self):
        return None

    def telegram_enabled(self, body):
        return self.telegram_controls.enabled(body)

    def set_telegram_enabled(self, body, enabled):
        if self.settings_store is not None:
            self.settings_store.legacy_update({
                "telegram": {str(body).lower() + "_enabled": enabled},
            })
            return self.telegram_controls.snapshot()
        return self.telegram_controls.set_enabled(body, enabled)

    def invalidate_live(self):
        return None

    def close(self):
        return None


class DashboardRuntime:
    def __init__(self, state, server=None, thread=None, mobile_gps_state=None,
                 telegram_controls=None, application_state_store=None,
                 settings_store=None):
        self.state = state
        self.server = server
        self.thread = thread
        self.mobile_gps_state = mobile_gps_state
        self.telegram_controls = telegram_controls or TelegramBodyControls()
        self.application_state_store = application_state_store
        self.settings_store = settings_store

    def _publish_application_state(self):
        try:
            if self.application_state_store is not None:
                self.application_state_store.publish(self.state.snapshot())
        except Exception:
            pass

    def publish(self, candidate):
        result = self.state.publish(candidate)
        self._publish_application_state()
        return result

    def update_callsign(self, icao, callsign):
        changed = self.state.update_callsign(icao, callsign)
        if changed:
            self._publish_application_state()
        return changed

    def withdraw(self, icao, body, now_utc):
        result = self.state.withdraw(icao, body, now_utc)
        self._publish_application_state()
        return result

    def withdraw_aircraft(self, icao, now_utc):
        result = self.state.withdraw_aircraft(icao, now_utc)
        self._publish_application_state()
        return result

    def mark_history_worthy(self, icao, body):
        result = self.state.mark_history_worthy(icao, body)
        self._publish_application_state()
        return result

    def tick(self, now_utc):
        result = self.state.tick(now_utc)
        self._publish_application_state()
        return result

    def update_body_position(self, body, altitude_deg, azimuth_deg,
                             evaluated_at_utc):
        result = self.state.update_body_position(
            body, altitude_deg, azimuth_deg, evaluated_at_utc)
        self._publish_application_state()
        return result

    def clear_body_positions(self):
        result = self.state.clear_body_positions()
        self._publish_application_state()
        return result

    def telegram_enabled(self, body):
        return self.telegram_controls.enabled(body)

    def set_telegram_enabled(self, body, enabled):
        if self.settings_store is not None:
            self.settings_store.legacy_update({
                "telegram": {str(body).lower() + "_enabled": enabled},
            })
            return self.telegram_controls.snapshot()
        return self.telegram_controls.set_enabled(body, enabled)

    def invalidate_live(self):
        result = self.state.invalidate_live()
        self._publish_application_state()
        return result

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=1.0)
        if self.state.history_store is not None:
            self.state.history_store.close()


def _handler_factory(state, now_utc, mobile_gps_state, telegram_controls,
                     application_state_store=None, settings_store=None,
                     sse_broker=None,
                     observer_position_provider=None,
                     stale_warning_seconds=30.0,
                     critical_warning_seconds=300.0):
    def observer_diagnostics():
        if observer_position_provider is None:
            return {"requested_mode": "STATIC", "effective_source": "STATIC"}
        context = observer_position_provider.resolve(now_utc())
        result = {
            "requested_mode": context.requested_mode,
            "effective_source": context.effective_source,
            "fallback_enabled": context.fallback_enabled,
            "fallback_active": context.fallback_active,
            "mobile_age_seconds": context.mobile_age_seconds,
            "mobile_accuracy_m": context.mobile_accuracy_m,
        }
        age = context.mobile_age_seconds
        result["gps_health"] = (
            "NO_FIX" if age is None else
            "CRITICAL" if age > critical_warning_seconds else
            "STALE" if age > stale_warning_seconds else "ACTIVE")
        return result

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/state":
                self._send("application/json; charset=utf-8", json.dumps(
                    state.snapshot(now_utc()), separators=(",", ":"),
                ).encode("utf-8"))
            elif path == "/api/v1/bootstrap":
                self._send_json(serialize_bootstrap(
                    application_state_store.snapshot(),
                    settings_store.snapshot(), observer_diagnostics(),
                    now_utc()))
            elif path == "/api/v1/stream":
                self._send_sse()
            elif path == "/api/v1/settings":
                self._send_json(settings_store.snapshot())
            elif path == "/api/history":
                query = self._history_query(parsed.query)
                if query is None:
                    return
                body = json.dumps(
                    state.query_history(**query), separators=(",", ":")
                ).encode("utf-8")
                self._send("application/json; charset=utf-8", body)
            elif path == "/api/history/export.csv":
                query = self._history_query(parsed.query, export=True)
                if query is None:
                    return
                self._send(
                    "text/csv; charset=utf-8",
                    state.export_history_csv(**query),
                    disposition="attachment; filename=transit_history.csv")
            elif path == "/api/mobile-gps":
                self._send_json(mobile_gps_state.diagnostics(now_utc()))
            elif path == "/api/observer":
                self._send_json(observer_diagnostics())
            elif path == "/api/telegram":
                self._send_json(telegram_controls.snapshot())
            elif path in ("/", "/index.html"):
                self._send("text/html; charset=utf-8",
                           DASHBOARD_HTML.encode("utf-8"))
            else:
                self.send_error(404)

        def do_POST(self):
            path = urlsplit(self.path).path
            if path == "/api/telegram":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_MOBILE_GPS_REQUEST_BYTES:
                        raise ValueError
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if (not isinstance(payload, dict)
                            or set(payload) != {"body", "enabled"}):
                        raise ValueError
                    body = str(payload["body"]).upper()
                    settings_store.legacy_update({
                        "telegram": {
                            body.lower() + "_enabled": payload["enabled"],
                        },
                    })
                    self._send_json(telegram_controls.snapshot())
                except (UnicodeDecodeError, json.JSONDecodeError,
                        TypeError, ValueError):
                    self._send_json(
                        {"error": "Invalid Telegram control payload"},
                        status=400)
                return
            if path == "/api/observer":
                if observer_position_provider is None:
                    self._send_json({"error": "Observer control unavailable"}, status=403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_MOBILE_GPS_REQUEST_BYTES:
                        raise ValueError
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if set(payload) - {"mode", "fallback_enabled"}:
                        raise ValueError
                    if ("mode" in payload
                            and str(payload["mode"]).upper() == "MOBILE"
                            and not mobile_gps_state.enabled):
                        self._send_json(
                            {"error": "Mobile GPS is disabled"}, status=403)
                        return
                    observer_changes = {}
                    if "mode" in payload:
                        observer_changes["requested_mode"] = payload["mode"]
                    if "fallback_enabled" in payload:
                        observer_changes["fallback_enabled"] = payload[
                            "fallback_enabled"]
                    settings_store.legacy_update({"observer": observer_changes})
                    self._send_json(observer_diagnostics())
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    self._send_json({"error": "Invalid observer control payload"}, status=400)
                return
            if path != "/api/mobile-gps":
                self.send_error(404)
                return
            if not mobile_gps_state.enabled:
                self._send_json(
                    {"error": "Mobile GPS is disabled"}, status=403)
                return
            if not self.headers.get("Content-Type", "").lower().startswith(
                    "application/json"):
                self._send_json(
                    {"error": "Mobile GPS requires JSON"}, status=415)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_MOBILE_GPS_REQUEST_BYTES:
                    raise ValueError
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                diagnostics = mobile_gps_state.update(payload, now_utc())
                if observer_position_provider is not None:
                    observer_position_provider.resolve(now_utc())
            except (UnicodeDecodeError, json.JSONDecodeError,
                    TypeError, ValueError):
                self._send_json(
                    {"error": "Invalid mobile GPS payload"}, status=400)
                return
            self._send_json(diagnostics)

        def do_PATCH(self):
            if urlsplit(self.path).path != "/api/v1/settings":
                self.send_error(404)
                return
            if not self.headers.get("Content-Type", "").lower().startswith(
                    "application/json"):
                self._send_json(
                    {"error": "Settings update requires JSON"}, status=415)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_MOBILE_GPS_REQUEST_BYTES:
                    raise SettingsValidationError("Invalid request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if (not isinstance(payload, dict)
                        or set(payload) != {
                            "expected_revision", "command_id", "changes"}):
                    raise SettingsValidationError("Invalid settings payload")
                result = settings_store.update(
                    payload["expected_revision"], payload["command_id"],
                    payload["changes"])
                self._send_json(result)
            except SettingsConflictError as error:
                self._send_json({
                    "error": "settings_revision_conflict",
                    "current": error.current,
                }, status=409)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError,
                    SettingsValidationError, ValueError):
                self._send_json(
                    {"error": "Invalid settings mutation"}, status=400)

        def do_DELETE(self):
            if urlsplit(self.path).path != "/api/mobile-gps":
                self.send_error(404)
                return
            mobile_gps_state.clear()
            self._send_json(mobile_gps_state.diagnostics(now_utc()))

        def _history_query(self, raw_query, export=False):
            values = parse_qs(raw_query, keep_blank_values=True)
            date = values.get("date", [""])[0].strip() or None
            callsign = values.get("callsign", [""])[0].strip() or None
            body = values.get("body", ["ALL"])[0].strip().upper() or "ALL"
            if body not in ("ALL", "SUN", "MOON") or (
                    date is not None and (len(date) != 10
                                          or date[4] != "-"
                                          or date[7] != "-")):
                self.send_error(400, "Invalid history filter")
                return None
            if date is not None:
                try:
                    datetime.date.fromisoformat(date)
                except ValueError:
                    self.send_error(400, "Invalid history date")
                    return None
            result = {"utc_date": date, "callsign": callsign, "body": body}
            if not export:
                try:
                    result["offset"] = int(values.get("offset", ["0"])[0])
                    result["limit"] = int(values.get(
                        "limit", [str(DEFAULT_PAGE_SIZE)])[0])
                except ValueError:
                    self.send_error(400, "Invalid pagination")
                    return None
            return result

        def _send_json(self, value, status=200):
            self._send(
                "application/json; charset=utf-8",
                json.dumps(value, separators=(",", ":")).encode("utf-8"),
                status=status)

        def _send_sse(self):
            if sse_broker is None:
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            client = sse_broker.subscribe((
                live_envelope(application_state_store.snapshot()),
                settings_envelope(settings_store.snapshot())))
            try:
                while True:
                    events = client.next(20.0)
                    text = ("".join(encode_sse(item) for item in events)
                            if events else ": heartbeat\n\n")
                    self.wfile.write(text.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                client.close()

        def _send(self, content_type, body, disposition=None, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            if disposition is not None:
                self.send_header("Content-Disposition", disposition)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    return DashboardHandler


def start_dashboard(enabled, host, port, now_utc, error_handler=None,
                    server_factory=ThreadingHTTPServer,
                    sep_green_max_deg=DEFAULT_SEP_GREEN_MAX_DEG,
                    sep_yellow_max_deg=DEFAULT_SEP_YELLOW_MAX_DEG,
                    sep_visible_max_deg=DEFAULT_SEP_VISIBLE_MAX_DEG,
                    history_enabled=True,
                    history_dir="recordings/dashboard_history",
                    mobile_gps_enabled=False,
                    mobile_gps_fresh_seconds=DEFAULT_MOBILE_GPS_FRESH_SECONDS,
                    observer_position_provider=None,
                    mobile_gps_stale_warning_seconds=30.0,
                    mobile_gps_critical_warning_seconds=300.0,
                    new_transit_indicator_enabled=True,
                    new_transit_threshold_seconds=(
                        DEFAULT_NEW_TRANSIT_THRESHOLD_SECONDS),
                    telegram_sun_enabled=True,
                    telegram_moon_enabled=True,
                    telegram_body_change=None):
    telegram_controls = TelegramBodyControls(
        telegram_sun_enabled, telegram_moon_enabled, telegram_body_change)
    mobile_gps_state = MobileGpsState(
        enabled=mobile_gps_enabled,
        fresh_seconds=mobile_gps_fresh_seconds)
    if observer_position_provider is not None:
        observer_position_provider.attach_mobile_state(mobile_gps_state)

    def validate_settings(values):
        if (values["observer"]["requested_mode"] == "MOBILE"
                and not mobile_gps_state.enabled):
            raise SettingsValidationError("Mobile GPS is disabled")

    def apply_settings(values, previous):
        for body in ("SUN", "MOON"):
            key = body.lower() + "_enabled"
            if values["telegram"][key] != previous["telegram"][key]:
                telegram_controls.set_enabled(body, values["telegram"][key])
        if observer_position_provider is not None:
            observer = values["observer"]
            old_observer = previous["observer"]
            if observer["requested_mode"] != old_observer["requested_mode"]:
                observer_position_provider.set_mode(
                    observer["requested_mode"], now_utc())
            if observer["fallback_enabled"] != old_observer["fallback_enabled"]:
                observer_position_provider.set_fallback_enabled(
                    observer["fallback_enabled"], now_utc())

    initial_mode = (
        observer_position_provider.resolve(now_utc()).requested_mode
        if observer_position_provider is not None else "STATIC")
    initial_fallback = (
        observer_position_provider.resolve(now_utc()).fallback_enabled
        if observer_position_provider is not None else False)
    settings_store = RuntimeSettingsStore(
        telegram_sun_enabled, telegram_moon_enabled,
        initial_mode, initial_fallback,
        apply_callback=apply_settings, validate_callback=validate_settings)
    if not enabled:
        return DisabledDashboard(telegram_controls, settings_store)
    errors = error_handler or (lambda message: None)
    history_store = (
        DashboardHistoryStore(history_dir, errors)
        if history_enabled else None)
    state = DashboardState(
        sep_green_max_deg=sep_green_max_deg,
        sep_yellow_max_deg=sep_yellow_max_deg,
        sep_visible_max_deg=sep_visible_max_deg,
        history_store=history_store,
        new_transit_indicator_enabled=new_transit_indicator_enabled,
        new_transit_threshold_seconds=new_transit_threshold_seconds)
    application_state_store = ApplicationStateStore()
    sse_broker = SseBroker()
    application_state_store.subscribe(
        lambda snapshot: sse_broker.publish(live_envelope(snapshot)))
    settings_store.subscribe(
        lambda snapshot: sse_broker.publish(settings_envelope(snapshot)))
    try:
        state.tick(now_utc())
        application_state_store.publish(state.snapshot())
        server = server_factory(
            (host, port), _handler_factory(
                state, now_utc, mobile_gps_state,
                telegram_controls,
                application_state_store, settings_store,
                sse_broker,
                observer_position_provider,
                mobile_gps_stale_warning_seconds,
                mobile_gps_critical_warning_seconds))
        thread = threading.Thread(
            target=server.serve_forever, name="transit-dashboard", daemon=True)
        thread.start()
        return DashboardRuntime(
            state, server, thread, mobile_gps_state=mobile_gps_state,
            telegram_controls=telegram_controls,
            application_state_store=application_state_store,
            settings_store=settings_store)
    except Exception as error:
        try:
            errors("Dashboard server failed: {}".format(type(error).__name__))
        except Exception:
            pass
        return DashboardRuntime(
            state, mobile_gps_state=mobile_gps_state,
            telegram_controls=telegram_controls,
            application_state_store=application_state_store,
            settings_store=settings_store)


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transit Warning</title>
<style>
:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#101318;color:#eef}
body{margin:0;padding:12px;max-width:1100px;margin:auto}nav{display:flex;gap:8px;align-items:center;margin-bottom:12px}
button{padding:10px 18px;border:0;border-radius:8px;background:#273040;color:#fff}
button.active{background:#526b95}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.panel,.event{background:#191e27;border-radius:12px;padding:14px}.primary{border:1px solid #627ca9}.body-icon{display:inline-block;width:1.15em;text-align:center}.moon-icon{color:#e6edf7;text-shadow:0 0 .25rem rgba(190,210,235,.45)}
h1,h2,p{margin:4px 0}.candidate-heading{display:flex;align-items:baseline;gap:.65rem;min-width:0}.candidate-heading h2{min-width:0;overflow-wrap:anywhere}.countdown{font-size:2rem;font-variant-numeric:tabular-nums;flex:0 0 auto}.transit-geometry{white-space:nowrap}
.new-badge{display:inline-flex;align-items:center;align-self:center;padding:.12rem .38rem;border-radius:.35rem;background:#d8263e;color:#fff;font-size:.68rem;font-weight:850;line-height:1;letter-spacing:.04em;animation:new-pulse .85s ease-in-out infinite alternate;flex:0 0 auto}@keyframes new-pulse{from{opacity:.45;box-shadow:0 0 0 rgba(255,55,75,0)}to{opacity:1;box-shadow:0 0 .55rem rgba(255,55,75,.8)}}@media(prefers-reduced-motion:reduce){.new-badge{animation:none;opacity:1}}
.candidate{border-top:1px solid #333;padding:10px 0}.muted{color:#9ba7ba}.hidden{display:none}
.sep{font-weight:750}.primary .sep{font-size:1.45rem;margin:7px 0}.sep.GREEN{color:#55d982}.sep.YELLOW{color:#f0c34d}.sep.RED{color:#ff6b6b}
.history-controls{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0 12px}.history-controls input,.history-controls select{min-width:0;padding:8px;border:1px solid #3a4558;border-radius:7px;background:#11161e;color:#eef}.history-controls input[type=date]{flex:1 1 135px}.history-controls input[type=search]{flex:2 1 150px}.history-controls select{flex:1 1 80px}.history-actions{display:flex;gap:8px;margin-top:10px}.event{margin-top:8px}.event .sep{font-size:1.05rem}
.health{margin-left:auto;font-size:.8rem}.dot{display:inline-block;width:.65rem;height:.65rem;border-radius:50%;margin-right:.35rem}.active .dot{background:#36c66b}.stale .dot{background:#e0a62f}.disconnected .dot{background:#e45454}
.observer-panel{display:flex;align-items:center;flex-wrap:wrap;gap:.35rem .7rem;margin:0 0 8px;padding:6px 8px;border:1px solid #303949;border-radius:8px;font-size:.8rem}.observer-controls,.observer-status{display:flex;align-items:center;flex-wrap:wrap;gap:.3rem .55rem}.observer-controls strong{font-size:.82rem}.observer-controls button{padding:5px 9px}.observer-controls label{white-space:nowrap}.gps-secondary{font-size:.75rem;padding:3px 7px!important}.observer-status{min-width:0}.observer-status .arrow{color:#8793a6}.observer-ok{color:#55d982}.observer-warn{color:#f0c34d}.observer-error{color:#ff6b6b;font-weight:700}
.telegram-controls{display:flex;align-items:center;gap:.35rem;margin-left:auto}.telegram-controls button{padding:5px 8px;font-size:.75rem}.body-position{font-size:.72em;color:#9ba7ba;font-weight:500;white-space:nowrap}
@media(max-width:650px){.grid{grid-template-columns:1fr}}
</style></head><body>
<nav><button id="live-tab" class="active">LIVE</button><button id="history-tab">HISTORY</button><span id="health" class="health disconnected"><span class="dot"></span><span class="label">DISCONNECTED</span></span></nav>
<section class="observer-panel"><div class="observer-controls"><strong>OBSERVER</strong><span class="observer-modes"><button id="observer-static">STATIC</button><button id="observer-mobile">MOBILE</button></span><label title="Fallback to STATIC when mobile GPS becomes stale"><input id="observer-fallback" type="checkbox"> fallback</label><button id="gps-start" class="gps-secondary hidden">Start GPS</button><button id="gps-stop" class="gps-secondary hidden">Stop GPS</button></div><div id="observer-fields" class="observer-status"><span>STATIC</span><span class="arrow">→</span><span class="observer-ok">STATIC</span></div><div class="telegram-controls"><strong>TELEGRAM</strong><button id="telegram-sun">SUN ON</button><button id="telegram-moon">MOON ON</button></div></section>
<main id="live" class="grid"><section class="panel"><h1><span class="body-icon">☀️</span> SUN <span id="sun-position" class="body-position">ALT — · AZ —</span></h1><div id="sun"></div></section>
<section class="panel"><h1><span class="body-icon moon-icon">☾</span> MOON <span id="moon-position" class="body-position">ALT — · AZ —</span></h1><div id="moon"></div></section></main>
<main id="history" class="hidden"><section class="panel"><h1>HISTORY</h1><div class="history-controls"><input id="history-date" type="date" aria-label="UTC date"><input id="history-search" type="search" placeholder="Callsign" aria-label="Callsign search"><select id="history-body" aria-label="Celestial body"><option value="ALL">ALL</option><option value="SUN">SUN</option><option value="MOON">MOON</option></select></div><div id="events"></div><div class="history-actions"><button id="load-more" class="hidden">LOAD MORE</button><button id="export-csv">EXPORT CSV</button></div></section></main>
<script>
let state=null,failedPolls=0,historyRecords=[],historyOffset=0,historyHasMore=false,gpsWatchId=null,gpsEnabled=false,gpsAvailable=false,observerData=null,gpsUiOverride=null;const STALE_AFTER_MS=10000,DISCONNECT_AFTER_FAILURES=2,HISTORY_PAGE_SIZE=25;const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function healthStatus(now=Date.now()){if(failedPolls>=DISCONNECT_AFTER_FAILURES)return'DISCONNECTED';let t=Date.parse(state?.generated_at_utc);return Number.isFinite(t)&&now-t<=STALE_AFTER_MS?'ACTIVE':'STALE'}
function renderHealth(){let status=healthStatus(),root=document.getElementById('health');root.className=`health ${status.toLowerCase()}`;root.querySelector('.label').textContent=status}
function countdown(utc){let s=Math.max(0,Math.floor((Date.parse(utc)-Date.now())/1000));return `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`}
function eventTime(utc){return String(utc||'').slice(11,19)+' UTC'}
function sepClass(value){return ['GREEN','YELLOW','RED'].includes(value)?value:''}
function formatAge(seconds){let value=Math.max(0,Math.round(seconds));return value<60?`${value} s`:`${Math.floor(value/60)}m ${value%60}s`}
function renderGps(data,statusOverride=null){gpsUiOverride=statusOverride;renderObserver(observerData)}
function renderObserver(data){if(!data)return;observerData=data;let requested=data.requested_mode,effectiveLabels={STATIC:'STATIC',MOBILE_FRESH:'MOBILE',MOBILE_LAST_KNOWN:'MOBILE LAST KNOWN',STATIC_FALLBACK:'STATIC FALLBACK',MOBILE_NO_FIX:'NO POSITION'},effective=effectiveLabels[data.effective_source]||data.effective_source,health=gpsUiOverride||data.gps_health||'NO_FIX',critical=health==='CRITICAL',gpsLabel=critical?'STALE':health,sourceClass=(data.effective_source==='STATIC'||data.effective_source==='MOBILE_FRESH')?'observer-ok':data.effective_source==='MOBILE_LAST_KNOWN'?'observer-warn':'observer-error',ageClass=critical?'observer-error':health==='STALE'?'observer-warn':health==='ACTIVE'?'observer-ok':'observer-error',parts=[`<span>${esc(requested)}</span>`,`<span class="arrow">→</span>`,`<span class="${sourceClass}">${esc(effective)}</span>`];if(requested==='MOBILE'){parts.push(`<span class="${ageClass}">GPS: ${esc(gpsLabel)}</span>`);if(data.mobile_age_seconds!=null)parts.push(`<span class="${ageClass}">AGE: ${formatAge(data.mobile_age_seconds)}</span>`)}document.getElementById('observer-fields').innerHTML=parts.join('');document.getElementById('observer-static').classList.toggle('active',requested==='STATIC');document.getElementById('observer-mobile').classList.toggle('active',requested==='MOBILE');document.getElementById('observer-mobile').disabled=!gpsAvailable;document.getElementById('observer-fallback').checked=Boolean(data.fallback_enabled);document.getElementById('gps-start').classList.toggle('hidden',requested!=='MOBILE'||gpsEnabled||!gpsAvailable);document.getElementById('gps-stop').classList.toggle('hidden',!gpsEnabled)}
async function observerRequest(payload=null){let options={cache:'no-store'};if(payload){options.method='POST';options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(payload)}let response=await fetch('/api/observer',options),data=await response.json();if(!response.ok)throw Error(data.error||'Observer request failed');renderObserver(data)}
async function gpsRequest(method,payload){let options={method,headers:{'Content-Type':'application/json'}};if(payload)options.body=JSON.stringify(payload);let response=await fetch('/api/mobile-gps',options),data=await response.json();if(!response.ok)throw Error(data.error||'GPS request failed');return data}
async function submitGps(position){if(!gpsEnabled)return;let c=position.coords;try{let data=await gpsRequest('POST',{latitude:c.latitude,longitude:c.longitude,accuracy:c.accuracy,altitude:c.altitude,altitudeAccuracy:c.altitudeAccuracy,timestamp:position.timestamp});gpsUiOverride=null;renderGps(data)}catch(e){renderGps({},'ERROR')}}
function gpsError(){if(gpsEnabled)renderGps({},'ERROR')}
async function startGps(){if(!gpsAvailable||gpsEnabled)return;if(!navigator.geolocation){renderGps({},'ERROR');return}gpsEnabled=true;renderGps({},'STALE');gpsWatchId=navigator.geolocation.watchPosition(submitGps,gpsError,{enableHighAccuracy:true,timeout:10000,maximumAge:0})}
async function stopGps(){if(gpsWatchId!==null&&navigator.geolocation)navigator.geolocation.clearWatch(gpsWatchId);gpsWatchId=null;gpsEnabled=false;gpsUiOverride=null;try{await gpsRequest('DELETE')}catch(e){}renderObserver(observerData)}
async function refreshGpsStatus(){if(!gpsEnabled)return;try{renderGps(await gpsRequest('GET'))}catch(e){renderGps({},'ERROR')}}
async function loadGpsAvailability(){try{let data=await gpsRequest('GET');gpsAvailable=Boolean(data.available);renderObserver(observerData)}catch(e){renderGps({},'ERROR')}}
function renderBody(name){let list=state?.[name]?.candidates||[],root=document.getElementById(name);if(!list.length){root.innerHTML='<p class="muted">No candidates</p>';return}
root.innerHTML=list.slice(0,3).map((c,i)=>`<article class="candidate ${i?'':'primary'}"><div class="candidate-heading"><div class="countdown" data-utc="${esc(c.predicted_event_utc)}">${countdown(c.predicted_event_utc)}</div><h2>${esc(c.callsign||c.icao)}</h2>${c.is_new_late_candidate?'<span class="new-badge">NEW</span>':''}</div><p class="sep ${sepClass(c.separation_class)}">SEP ${c.separation_deg.toFixed(2)}°</p><p>${esc(c.state)}</p>${i?'':`<p class="transit-geometry">AZ ${c.body_azimuth_deg.toFixed(1)}° · ALT ${c.body_elevation_deg.toFixed(1)}° · ${c.transit_distance_km?.toFixed(0)??'—'} km</p><p class="muted">${eventTime(c.predicted_event_utc)}</p>`}</article>`).join('')}
function renderBodyPosition(name){let p=state?.[name]?.current_position,root=document.getElementById(name+'-position');root.textContent=p?`ALT ${Math.round(p.altitude_deg)}° · AZ ${Math.round(p.azimuth_deg)}°`:'ALT — · AZ —'}
function renderHistory(){document.getElementById('events').innerHTML=historyRecords.map(x=>`<article class="event"><b>${esc(x.callsign||x.icao)} · ${esc(x.body)} · ${esc(x.outcome)}</b><p>${esc(x.predicted_event_utc)}</p><p class="sep">Final SEP ${Number(x.final_separation_deg).toFixed(2)}°${x.transit_distance_km!=null&&Number.isFinite(Number(x.transit_distance_km))?' · '+Number(x.transit_distance_km).toFixed(0)+' km':''}</p></article>`).join('')||'<p class="muted">No history events</p>';document.getElementById('load-more').classList.toggle('hidden',!historyHasMore)}
function historyQuery(offset=0){let q=new URLSearchParams({offset:String(offset),limit:String(HISTORY_PAGE_SIZE),body:document.getElementById('history-body').value}),d=document.getElementById('history-date').value,s=document.getElementById('history-search').value.trim();if(d)q.set('date',d);if(s)q.set('callsign',s);return q}
async function loadHistory(reset=true){let offset=reset?0:historyOffset,response=await fetch('/api/history?'+historyQuery(offset),{cache:'no-store'});if(!response.ok)return;let page=await response.json();historyRecords=reset?page.records:historyRecords.concat(page.records);historyOffset=page.next_offset??historyRecords.length;historyHasMore=page.has_more;renderHistory()}
function render(){renderBodyPosition('sun');renderBodyPosition('moon');renderBody('sun');renderBody('moon')}
function renderTelegram(data){for(let body of ['sun','moon']){let enabled=Boolean(data[body+'_enabled']),button=document.getElementById('telegram-'+body);button.textContent=body.toUpperCase()+' '+(enabled?'ON':'OFF');button.classList.toggle('active',enabled)}}
async function telegramRequest(body=null,enabled=null){let options={cache:'no-store'};if(body){options.method='POST';options.headers={'Content-Type':'application/json'};options.body=JSON.stringify({body:body.toUpperCase(),enabled})}let response=await fetch('/api/telegram',options),data=await response.json();if(!response.ok)throw Error(data.error||'Telegram control failed');renderTelegram(data)}
async function refresh(){let controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),2500);try{let response=await fetch('/api/state',{cache:'no-store',signal:controller.signal});if(!response.ok)throw Error('HTTP');state=await response.json();failedPolls=0;render();renderHealth()}catch(e){failedPolls++;renderHealth()}finally{clearTimeout(timeout)}}
setInterval(()=>{document.querySelectorAll('[data-utc]').forEach(x=>x.textContent=countdown(x.dataset.utc));renderHealth()},1000);setInterval(refresh,3000);refresh();
setInterval(refreshGpsStatus,3000);loadGpsAvailability();document.getElementById('gps-start').onclick=startGps;document.getElementById('gps-stop').onclick=stopGps;
setInterval(()=>observerRequest().catch(()=>{}),3000);observerRequest().catch(()=>{});document.getElementById('observer-static').onclick=()=>observerRequest({mode:'STATIC'});document.getElementById('observer-mobile').onclick=()=>observerRequest({mode:'MOBILE'});document.getElementById('observer-fallback').onchange=e=>observerRequest({fallback_enabled:e.target.checked});
telegramRequest().catch(()=>{});for(let body of ['sun','moon'])document.getElementById('telegram-'+body).onclick=e=>telegramRequest(body,!e.currentTarget.classList.contains('active'));
document.getElementById('live-tab').onclick=()=>{document.getElementById('live').classList.remove('hidden');document.getElementById('history').classList.add('hidden')};
document.getElementById('history-tab').onclick=()=>{document.getElementById('history').classList.remove('hidden');document.getElementById('live').classList.add('hidden');loadHistory(true)};
for(let id of ['history-date','history-search','history-body'])document.getElementById(id).onchange=()=>loadHistory(true);
document.getElementById('load-more').onclick=()=>loadHistory(false);
document.getElementById('export-csv').onclick=()=>{location.href='/api/history/export.csv?'+historyQuery(0)};
</script></body></html>"""
