"""Inspect frozen A4.3 fusion provenance without consulting runtime state."""
import argparse
import json
from pathlib import Path


IMPORTANT_FIELDS = (
    "position", "ground_track", "groundspeed", "barometric_altitude",
    "geometric_altitude", "production_altitude", "predictor_altitude",
    "barometric_vertical_rate", "geometric_vertical_rate",
    "selected_altitude_mcp", "selected_altitude_fms",
    "selected_altimeter_setting", "heading_true", "heading_magnetic",
    "selected_heading", "track_rate", "callsign", "nic", "rc", "nac_p",
    "nac_v", "sil", "sil_type", "nic_baro", "gva", "sda",
)


def load_event(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("event must be a JSON object")
    return value


def final_prediction(event):
    if isinstance(event.get("final_prediction"), dict):
        return event["final_prediction"]
    updates = event.get("prediction_updates")
    if isinstance(updates, list) and updates and isinstance(updates[-1], dict):
        return updates[-1]
    if isinstance(event.get("trigger_prediction"), dict):
        return event["trigger_prediction"]
    return event


def fusion_provenance(event):
    prediction = final_prediction(event)
    frozen = prediction.get("frozen_prediction_state", {})
    fusion = frozen.get("fusion_provenance")
    if not isinstance(fusion, dict):
        fusion = prediction.get("fusion_provenance")
    if not isinstance(fusion, dict):
        fusion = event.get("fusion_provenance")
    return fusion if isinstance(fusion, dict) else None


def summarize_event(event):
    fusion = fusion_provenance(event)
    fields = fusion.get("fields", {}) if fusion else {}
    rows = []
    internet_affected = False
    for name in IMPORTANT_FIELDS:
        item = fields.get(name)
        if not isinstance(item, dict):
            continue
        selected = item.get("selected")
        alternatives = item.get("alternatives", [])
        if isinstance(selected, dict) and selected.get("source_family") == "ADSBLOL":
            if item.get("predictor_eligibility") != "DIAGNOSTIC_ONLY":
                internet_affected = True
        rows.append({
            "field": name, "selected": selected,
            "alternatives": alternatives,
            "selection_reason": item.get("selection_reason"),
            "predictor_eligibility": item.get("predictor_eligibility"),
        })
    prediction = final_prediction(event)
    return {
        "event_id": event.get("event_id"),
        "encounter_id": prediction.get("encounter_id"),
        "body": event.get("body") or prediction.get("body"),
        "predicted_transit_utc": prediction.get("predicted_transit_utc"),
        "internet_enrichment_affected_prediction": internet_affected,
        "fields": rows,
    }


def format_summary(summary):
    lines = [
        "Event: {}  Encounter: {}  Body: {}".format(
            summary.get("event_id") or "UNKNOWN",
            summary.get("encounter_id") or "UNKNOWN",
            summary.get("body") or "UNKNOWN"),
        "Internet enrichment affected prediction: {}".format(
            "YES" if summary["internet_enrichment_affected_prediction"] else "NO"),
    ]
    for row in summary["fields"]:
        selected = row["selected"] or {}
        age = selected.get("age_seconds")
        age_text = "UNKNOWN" if age is None else "{} s".format(age)
        lines.extend((
            "", row["field"].upper(),
            "selected: {} {} {} age {} freshness {}".format(
                selected.get("source_id") or selected.get("source") or "NONE",
                selected.get("value"), selected.get("unit") or "",
                age_text, selected.get("freshness_state") or "UNAVAILABLE"),
            "reason: {}  eligibility: {}".format(
                row["selection_reason"], row["predictor_eligibility"]),
        ))
        for alternative in row["alternatives"]:
            alt_age = alternative.get("age_seconds")
            lines.append("alternative: {} {} {} age {} freshness {}".format(
                alternative.get("source_id") or alternative.get("source"),
                alternative.get("value"), alternative.get("unit") or "",
                "UNKNOWN" if alt_age is None else "{} s".format(alt_age),
                alternative.get("freshness_state")))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Summarize frozen fusion provenance in one event JSON")
    parser.add_argument("event_json")
    parser.add_argument("--json", action="store_true",
                        help="emit the summary as deterministic JSON")
    args = parser.parse_args(argv)
    summary = summarize_event(load_event(args.event_json))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
    else:
        print(format_summary(summary))


if __name__ == "__main__":
    main()
