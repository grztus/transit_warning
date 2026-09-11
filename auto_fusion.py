"""Pure A4.3b per-field LOCAL + ADSB.lol fusion.

The objects in this module are observations and decisions.  In particular an
unknown-age internet observation is never converted to a timestamped local
``MotionParameter``.
"""
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


POSITION_MAX_AGE_SECONDS = 20.0
SNAPSHOT_LEASE_SECONDS = 30.0
LOCAL_FRESH_POSITION_SECONDS = 3.0
LOCAL_FRESH_PARAMETER_SECONDS = 5.0
LOCAL_STALE_SECONDS = 10.0


@dataclass(frozen=True)
class FieldCandidate:
    field_name: str
    value: Any
    source: str
    source_family: str
    unit: str | None = None
    datum_or_reference: str | None = None
    availability: str = "PRESENT"
    freshness_state: str = "UNKNOWN"
    age_seconds: float | None = None
    observed_at_utc: str | None = None
    observed_at_basis: str = "UNKNOWN"
    received_at_utc: str | None = None
    received_at_monotonic: float | None = None
    snapshot_id: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    quality: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FusedField:
    field_name: str
    selected: FieldCandidate | None
    alternatives: tuple[FieldCandidate, ...]
    selection_reason: str
    predictor_eligibility: str


@dataclass(frozen=True)
class FusedAircraftState:
    icao: str
    evaluated_at_utc: str
    evaluated_at_monotonic: float
    fields: Mapping[str, FusedField]
    provider_snapshot_id: str | None
    provider_health: str
    prediction_trust: str
    prediction_reason_codes: tuple[str, ...]


REMOTE_DIAGNOSTIC_ONLY = {
    "barometric_vertical_rate", "geometric_vertical_rate",
    "selected_altitude_mcp", "selected_altitude_fms",
    "selected_altimeter_setting", "heading_magnetic", "heading_true",
    "selected_heading", "track_rate",
}


def candidate_from_adsblol(observation):
    """Copy one normalized A4.2 observation without changing its clocks."""
    return FieldCandidate(
        field_name=observation.get("field_name"),
        value=observation.get("value"), source="ADSBLOL",
        source_family="ADSBLOL", unit=observation.get("unit"),
        datum_or_reference=observation.get("datum_or_reference"),
        availability=observation.get("availability", "MISSING"),
        freshness_state=observation.get("freshness_state", "UNAVAILABLE"),
        age_seconds=observation.get("age_seconds"),
        observed_at_utc=observation.get("observed_at_utc"),
        observed_at_basis=observation.get("observed_at_basis", "UNKNOWN"),
        received_at_utc=observation.get("received_at_utc"),
        received_at_monotonic=observation.get("received_at_monotonic"),
        snapshot_id=observation.get("snapshot_id"),
        provenance=observation.get("provenance") or {},
        quality=observation.get("quality") or {})


def _present(candidate):
    return candidate is not None and candidate.availability == "PRESENT"


def _local_class(candidate, position=False):
    if not _present(candidate) or candidate.age_seconds is None:
        return "UNAVAILABLE"
    fresh = LOCAL_FRESH_POSITION_SECONDS if position else LOCAL_FRESH_PARAMETER_SECONDS
    if candidate.age_seconds <= fresh:
        return "FRESH"
    if candidate.age_seconds <= LOCAL_STALE_SECONDS:
        return "DEGRADED"
    return "STALE"


def _track_reason(source, fallback):
    if source == "RAW_ADSB_TC19_FRESH":
        return "LOCAL_RAW_FRESH_PREFERRED"
    if source == "RAW_ADSB_TC19_HELD":
        return "LOCAL_RAW_HELD_PREFERRED"
    if source in ("MLAT_BEAST_TC19_FRESH", "MLAT_BEAST_TC19_HELD"):
        return "LOCAL_MLAT_PRECISION_FRESH_PREFERRED"
    return fallback


def select_field(name, local, remote, *, remote_position_eligible,
                 snapshot_lease_valid):
    """Select one field. Unknown remote age is retained, never synthesized."""
    alternatives = tuple(item for item in (local, remote) if item is not None)
    local_class = _local_class(local, position=name == "position")
    if local_class in ("FRESH", "DEGRADED"):
        base = ("LOCAL_FRESH_PREFERRED" if local_class == "FRESH"
                else "LOCAL_DEGRADED_KNOWN_AGE_PREFERRED")
        if (_present(remote) and remote.freshness_state == "UNKNOWN"):
            base = ("UNKNOWN_AGE_NOT_SELECTED_OVER_FRESH_LOCAL"
                    if local_class == "FRESH" else
                    "UNKNOWN_AGE_NOT_SELECTED_OVER_DEGRADED_LOCAL")
        reason = _track_reason(local.source, base) if name == "ground_track" else base
        return FusedField(name, local, tuple(x for x in alternatives if x is not local),
                          reason, "ELIGIBLE")
    if not snapshot_lease_valid:
        reason = "ADSBLOL_SNAPSHOT_LEASE_EXPIRED"
    elif not remote_position_eligible:
        reason = "ADSBLOL_POSITION_EXPIRED"
    elif remote is None or remote.availability == "MISSING":
        reason = "ADSBLOL_FIELD_MISSING"
    elif remote.availability != "PRESENT":
        reason = "ADSBLOL_FIELD_INVALID"
    else:
        reason = ("LOCAL_STALE_ADSBLOL_FALLBACK" if local_class == "STALE"
                  else "LOCAL_UNAVAILABLE_ADSBLOL_USED" if local is not None
                  else "ADSBLOL_ONLY")
        eligibility = ("DIAGNOSTIC_ONLY" if name in REMOTE_DIAGNOSTIC_ONLY
                       else "DEGRADED")
        return FusedField(name, remote,
                          tuple(x for x in alternatives if x is not remote),
                          reason, eligibility)
    if _present(local):
        return FusedField(name, local, tuple(x for x in alternatives if x is not local),
                          reason, "DIAGNOSTIC_ONLY")
    return FusedField(name, None, alternatives, reason, "DIAGNOSTIC_ONLY")


def internet_lifetime(position, receipt_monotonic, now_monotonic):
    elapsed = max(0.0, now_monotonic - receipt_monotonic)
    lease = elapsed <= SNAPSHOT_LEASE_SECONDS
    age = position.age_seconds if position is not None else None
    eligible = (_present(position) and age is not None
                and age + elapsed <= POSITION_MAX_AGE_SECONDS and lease)
    return eligible, lease, (age + elapsed if age is not None else None)


def fuse_aircraft(icao, local_fields, remote_aircraft, *, evaluated_at_utc,
                  evaluated_at_monotonic, receipt_monotonic, provider_health):
    remote_fields = (remote_aircraft or {}).get("fields", {})
    remote = {name: candidate_from_adsblol(value)
              for name, value in remote_fields.items()}
    position_ok, lease_ok, propagated_age = internet_lifetime(
        remote.get("position"), receipt_monotonic, evaluated_at_monotonic)
    if remote.get("position") is not None and propagated_age is not None:
        position = remote["position"]
        remote["position"] = FieldCandidate(
            **{**asdict(position), "age_seconds": propagated_age})
    names = set(local_fields) | set(remote)
    fields = {name: select_field(
        name, local_fields.get(name), remote.get(name),
        remote_position_eligible=position_ok, snapshot_lease_valid=lease_ok)
        for name in names}
    unknown = tuple(sorted(name for name in ("position", "ground_track", "groundspeed")
                           if fields.get(name) and fields[name].selected is not None
                           and fields[name].selected.source_family == "ADSBLOL"))
    trust = "DEGRADED_UNKNOWN_FIELD_AGE" if unknown else "LOCAL_KNOWN_FIELD_AGES"
    reasons = tuple("UNKNOWN_AGE_{}".format(name.upper()) for name in unknown)
    return FusedAircraftState(
        icao, evaluated_at_utc, evaluated_at_monotonic, fields,
        (remote_aircraft or {}).get("snapshot_id"), provider_health,
        trust, reasons)


def fuse_local_aircraft(icao, local_fields, *, evaluated_at_utc,
                        evaluated_at_monotonic):
    """Create the same forensic contract for a LOCAL-only prediction."""
    fields = {}
    for name, candidate in local_fields.items():
        local_class = _local_class(candidate, position=name == "position")
        if local_class == "FRESH":
            reason, eligibility = "LOCAL_FRESH_PREFERRED", "ELIGIBLE"
        elif local_class == "DEGRADED":
            reason, eligibility = (
                "LOCAL_DEGRADED_KNOWN_AGE_PREFERRED", "ELIGIBLE")
        elif _present(candidate):
            reason, eligibility = "NO_ELIGIBLE_CANDIDATE", "DIAGNOSTIC_ONLY"
        else:
            reason, eligibility = "NO_ELIGIBLE_CANDIDATE", "DIAGNOSTIC_ONLY"
        if name == "ground_track":
            reason = _track_reason(candidate.source, reason)
        fields[name] = FusedField(
            name, candidate if _present(candidate) else None, (), reason,
            eligibility)
    return FusedAircraftState(
        icao, evaluated_at_utc, evaluated_at_monotonic, fields, None,
        "NOT_APPLICABLE", "LOCAL_KNOWN_FIELD_AGES", ())


def _serialize_candidate(candidate):
    if candidate is None:
        return None
    result = asdict(candidate)
    result["source_id"] = candidate.source
    result["transport"] = candidate.provenance.get("transport")
    return result


def serialize_fused_state(state):
    """Stable lossless forensic form shared by recorders and clients."""
    if state is None:
        return None
    return {
        "icao": state.icao,
        "evaluated_at_utc": state.evaluated_at_utc,
        "evaluated_at_monotonic": state.evaluated_at_monotonic,
        "provider_snapshot_id": state.provider_snapshot_id,
        "provider_health": state.provider_health,
        "prediction_trust": state.prediction_trust,
        "prediction_reason_codes": list(state.prediction_reason_codes),
        "fields": {
            name: {
                "field_name": item.field_name,
                "selected": _serialize_candidate(item.selected),
                "alternatives": [_serialize_candidate(candidate)
                                 for candidate in item.alternatives],
                "selection_reason": item.selection_reason,
                "predictor_eligibility": item.predictor_eligibility,
            }
            for name, item in sorted(state.fields.items())
        },
    }


def serialize_predictor_provenance(state, view=None):
    result = serialize_fused_state(state)
    if result is None or view is None:
        return result
    altitude = view.get("altitude_candidate")
    if altitude is not None:
        result["fields"]["predictor_altitude"] = {
            "field_name": "predictor_altitude",
            "selected": _serialize_candidate(altitude),
            "alternatives": [],
            "selection_reason": "DERIVED_PREDICTOR_ALTITUDE",
            "predictor_eligibility": "DEGRADED" if view.get(
                "freeze_altitude") else "ELIGIBLE",
        }
    result["predictor"] = {
        "classification": view.get("classification"),
        "unknown_age_fields": list(view.get("unknown_age_fields", ())),
        "freeze_altitude": bool(view.get("freeze_altitude")),
        "consumed_fields": [
            "position", "ground_track", "groundspeed", "predictor_altitude"],
        "diagnostic_only_fields": sorted(REMOTE_DIAGNOSTIC_ONLY),
    }
    return result


def predictor_view(state, *, application_qnh_hpa, geoid):
    """Derive predictor-compatible values without mutating source observations.

    Unknown-age remote vertical state is deliberately omitted.  A remote HAE
    height is converted through the configured geoid and retains explicit
    derivation lineage in the returned altitude candidate.
    """
    def selected(name):
        fused = state.fields.get(name)
        return fused.selected if fused is not None else None

    position = selected("position")
    track = selected("ground_track")
    groundspeed = selected("groundspeed")
    if position is None or track is None or groundspeed is None:
        return None
    gs_kmh = (float(groundspeed.value) * 1.852
              if groundspeed.unit == "knots" else float(groundspeed.value))
    altitude = selected("geometric_altitude")
    if altitude is not None and altitude.datum_or_reference == "WGS84_HAE":
        value_m = float(altitude.value) * (0.3048 if altitude.unit == "ft" else 1.0)
        value_m -= geoid.undulation_m(
            position.value["lat"], position.value["lon"])
        altitude = FieldCandidate(
            field_name="predictor_altitude", value=value_m,
            source=altitude.source + "_EGM96_CONVERTED",
            source_family=altitude.source_family, unit="m",
            datum_or_reference="EGM96_AMSL",
            freshness_state=altitude.freshness_state,
            age_seconds=altitude.age_seconds,
            observed_at_utc=altitude.observed_at_utc,
            observed_at_basis=altitude.observed_at_basis,
            received_at_utc=altitude.received_at_utc,
            received_at_monotonic=altitude.received_at_monotonic,
            snapshot_id=altitude.snapshot_id,
            provenance={**dict(altitude.provenance), "derivation": "HAE_MINUS_EGM96",
                        "lineage": ["geometric_altitude", "position"]},
            quality=altitude.quality)
    else:
        altitude = selected("production_altitude")
        if altitude is None:
            baro = selected("barometric_altitude")
            if baro is not None and baro.datum_or_reference == "PRESSURE":
                value_ft = float(baro.value) + (
                    float(application_qnh_hpa) - 1013.25) * 26.0
                altitude = FieldCandidate(
                    field_name="predictor_altitude", value=value_ft * 0.3048,
                    source=baro.source + "_APPLICATION_QNH", source_family=baro.source_family,
                    unit="m", datum_or_reference="EGM96_AMSL",
                    freshness_state=baro.freshness_state,
                    age_seconds=baro.age_seconds, observed_at_utc=baro.observed_at_utc,
                    observed_at_basis=baro.observed_at_basis,
                    received_at_utc=baro.received_at_utc,
                    received_at_monotonic=baro.received_at_monotonic,
                    snapshot_id=baro.snapshot_id,
                    provenance={**dict(baro.provenance), "derivation": "PRESSURE_PLUS_APPLICATION_QNH",
                                "lineage": ["barometric_altitude", "application_qnh"]},
                    quality=baro.quality)
    if altitude is None:
        return None
    consumed = (position, track, groundspeed, altitude)
    unknown = tuple(item.field_name for item in consumed
                    if item.source_family == "ADSBLOL"
                    and item.freshness_state == "UNKNOWN")
    return {
        "position": position.value, "position_candidate": position,
        "track_deg": float(track.value),
        "groundspeed_kmh": gs_kmh, "altitude_m": float(altitude.value),
        "altitude_candidate": altitude,
        "classification": ("DEGRADED_UNKNOWN_FIELD_AGE" if unknown
                           else "LOCAL_KNOWN_FIELD_AGES"),
        "unknown_age_fields": unknown,
        "vertical_rate": None, "selected_altitude": None,
        "selected_altimeter_setting": float(application_qnh_hpa),
        "freeze_altitude": bool(unknown or altitude.source_family == "ADSBLOL"),
    }


class ProviderSnapshotCache:
    """Latest normalized snapshot with identity-preserving age anchors."""
    def __init__(self):
        self.aircraft = {}
        self.snapshot_id = None
        self.receipt_monotonic = None
        self.health = "WAITING"

    def clear(self):
        self.__init__()

    def consume(self, report, fallback_receipt_monotonic):
        self.health = report.get("status", "ERROR")
        if self.health != "OK":
            return False
        snapshot_id = report.get("snapshot_id")
        receipt = report.get("request_finished_monotonic", fallback_receipt_monotonic)
        # Identical provider evidence does not become younger on another HTTP receipt.
        if snapshot_id != self.snapshot_id:
            self.snapshot_id = snapshot_id
            self.receipt_monotonic = receipt
        self.aircraft = {a["icao"]: {**a, "snapshot_id": snapshot_id}
                         for a in report.get("aircraft", ()) if a.get("icao")}
        return True
