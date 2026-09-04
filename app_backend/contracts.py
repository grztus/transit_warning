"""Explicit schema-v1 serializers for ordinary application clients."""

import datetime

from .privacy import assert_public_payload


SCHEMA_VERSION = 1

_POSITION_FIELDS = (
    "altitude_deg", "azimuth_deg", "evaluated_at_utc",
)
_CANDIDATE_FIELDS = (
    "body", "icao", "callsign", "predicted_event_utc", "separation_deg",
    "body_azimuth_deg", "body_elevation_deg", "aircraft_elevation_deg",
    "distance_km", "last_prediction_update_utc", "telegram_range",
    "transit_distance_km", "encounter_id", "prediction_geometry", "state",
    "separation_class", "is_new_late_candidate",
)
_HISTORY_FIELDS = _CANDIDATE_FIELDS + (
    "event_id", "final_separation_deg", "first_separation_deg",
    "minimum_separation_deg", "first_seen_utc", "last_seen_utc",
    "history_recorded_at_utc", "outcome",
)
_PRESENTATION_FIELDS = (
    "sep_green_max_deg", "sep_yellow_max_deg", "sep_visible_max_deg",
)
_OBSERVER_FIELDS = (
    "requested_mode", "effective_source", "fallback_enabled",
    "fallback_active", "mobile_age_seconds", "mobile_accuracy_m",
    "gps_health",
)


def _select(source, fields):
    if not isinstance(source, dict):
        return {}
    return {field: source[field] for field in fields if field in source}


def serialize_observer_status(status):
    result = _select(status, _OBSERVER_FIELDS)
    assert_public_payload(result)
    return result


def serialize_live_state(snapshot):
    """Copy only the established public dashboard state into schema v1."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    body_source = snapshot.get("bodies")
    if not isinstance(body_source, dict):
        body_source = snapshot
    bodies = {}
    for body in ("sun", "moon"):
        source = (body_source.get(body)
                  if isinstance(body_source.get(body), dict) else {})
        position = source.get("current_position")
        bodies[body] = {
            "current_position": (
                _select(position, _POSITION_FIELDS)
                if isinstance(position, dict) else None),
            "candidates": [
                _select(item, _CANDIDATE_FIELDS)
                for item in source.get("candidates", ())
                if isinstance(item, dict)
            ],
        }
    result = {
        "generated_at_utc": snapshot.get("generated_at_utc"),
        "bodies": bodies,
        "recent_events": [
            _select(item, _HISTORY_FIELDS)
            for item in snapshot.get("recent_events", ())
            if isinstance(item, dict)
        ],
        "presentation": _select(
            snapshot.get("presentation"), _PRESENTATION_FIELDS),
    }
    assert_public_payload(result)
    return result


def application_health(generated_at_utc, now_utc, stale_seconds=10.0):
    if not generated_at_utc:
        return "STALE"
    try:
        generated = datetime.datetime.fromisoformat(
            str(generated_at_utc).replace("Z", "+00:00"))
        age = (now_utc.astimezone(datetime.timezone.utc)
               - generated.astimezone(datetime.timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return "STALE"
    return "ACTIVE" if age <= float(stale_seconds) else "STALE"


def serialize_bootstrap(application_snapshot, settings, observer_status, now_utc):
    live = serialize_live_state(application_snapshot["state"])
    result = {
        "schema_version": SCHEMA_VERSION,
        "live_revision": application_snapshot["revision"],
        "settings_revision": settings["revision"],
        "generated_at_utc": live["generated_at_utc"],
        "health": application_health(live["generated_at_utc"], now_utc),
        "observer": serialize_observer_status(observer_status),
        "bodies": live["bodies"],
        "recent_events": live["recent_events"],
        "presentation": live["presentation"],
        "capabilities": settings["capabilities"],
    }
    assert_public_payload(result)
    return result
