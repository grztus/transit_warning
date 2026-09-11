import csv
import datetime
import io
import json
import tempfile
from types import SimpleNamespace
import unittest

from candidate_recorder import _prediction_manifest
from dashboard_history import records_to_csv
from live_dashboard import DashboardCandidate, DashboardState
from tools.fusion_forensics import format_summary, fusion_provenance, summarize_event
from transit_snapshot import TransitSnapshotManager


UTC = datetime.timezone.utc
BASE = datetime.datetime(2026, 9, 6, 12, tzinfo=UTC)


def candidate(value, source, family, age, freshness, datum="TRUE_NORTH"):
    return {
        "field_name": "ground_track", "value": value, "unit": "deg",
        "source": source, "source_id": source, "source_family": family,
        "transport": "RAW_ADSB" if family == "LOCAL" else "HTTP_V2",
        "freshness_state": freshness, "age_seconds": age,
        "observed_at_utc": "2026-09-06T11:59:59Z" if age is not None else None,
        "observed_at_basis": "LOCAL_FIELD_TIMESTAMP" if age is not None else "UNKNOWN",
        "received_at_utc": "2026-09-06T12:00:00Z",
        "received_at_monotonic": 100.0, "snapshot_id": (
            None if family == "LOCAL" else "snapshot-1"),
        "datum_or_reference": datum,
        "availability": "PRESENT", "provenance": {"provider": family},
        "quality": {"field_refs": ["nac_v"]},
    }


def fusion():
    local = candidate(165.02, "RAW_ADSB_TC19_FRESH", "LOCAL", .4, "KNOWN")
    remote = candidate(165.91, "ADSBLOL", "ADSBLOL", None, "UNKNOWN")
    geom = candidate(21750, "ADSBLOL", "ADSBLOL", None, "UNKNOWN", "WGS84_HAE")
    derived = {**geom, "field_name": "predictor_altitude", "value": 6595.0,
               "unit": "m", "source": "ADSBLOL_EGM96_CONVERTED",
               "source_id": "ADSBLOL_EGM96_CONVERTED",
               "datum_or_reference": "EGM96_AMSL",
               "provenance": {"derivation": "HAE_MINUS_EGM96",
                              "lineage": ["geometric_altitude", "position"]}}
    diagnostic_vr = candidate(512, "ADSBLOL", "ADSBLOL", None, "UNKNOWN", "PRESSURE_RATE")
    return {
        "icao": "ABC123", "provider_snapshot_id": "snapshot-1",
        "prediction_trust": "DEGRADED_UNKNOWN_FIELD_AGE",
        "fields": {
            "ground_track": {"field_name": "ground_track", "selected": local,
                             "alternatives": [remote],
                             "selection_reason": "LOCAL_RAW_FRESH_PREFERRED",
                             "predictor_eligibility": "ELIGIBLE"},
            "geometric_altitude": {"field_name": "geometric_altitude", "selected": geom,
                                   "alternatives": [],
                                   "selection_reason": "ADSBLOL_ONLY",
                                   "predictor_eligibility": "DEGRADED"},
            "predictor_altitude": {"field_name": "predictor_altitude", "selected": derived,
                                   "alternatives": [],
                                   "selection_reason": "DERIVED_PREDICTOR_ALTITUDE",
                                   "predictor_eligibility": "DEGRADED"},
            "barometric_vertical_rate": {
                "field_name": "barometric_vertical_rate", "selected": diagnostic_vr,
                "alternatives": [], "selection_reason": "ADSBLOL_ONLY",
                "predictor_eligibility": "DIAGNOSTIC_ONLY"},
        },
    }


class FusionForensicsTests(unittest.TestCase):
    def test_final_prediction_wins_over_later_unbound_or_trigger_state(self):
        trigger = {"encounter_id": "E1", "fusion_provenance": {"fields": {}}}
        final_fusion = fusion()
        event = {"event_id": "E1", "body": "SUN", "trigger_prediction": trigger,
                 "prediction_updates": [trigger, {"encounter_id": "E1",
                                                   "fusion_provenance": final_fusion}]}
        self.assertIs(final_fusion, fusion_provenance(event))
        final_fusion["fields"]["ground_track"]["selected"]["value"] = 165.03
        self.assertEqual(165.03, summarize_event(event)["fields"][0]["selected"]["value"])

    def test_summary_preserves_unknown_age_alternatives_and_diagnostic_only(self):
        event = {"event_id": "E1", "body": "SUN",
                 "final_prediction": {"encounter_id": "E1",
                                      "fusion_provenance": fusion()}}
        summary = summarize_event(event)
        track = next(row for row in summary["fields"] if row["field"] == "ground_track")
        self.assertIsNone(track["alternatives"][0]["age_seconds"])
        self.assertEqual("UNKNOWN", track["alternatives"][0]["freshness_state"])
        vr = next(row for row in summary["fields"]
                  if row["field"] == "barometric_vertical_rate")
        self.assertEqual("DIAGNOSTIC_ONLY", vr["predictor_eligibility"])
        self.assertIn("LOCAL_RAW_FRESH_PREFERRED", format_summary(summary))

    def test_json_round_trip_and_csv_compact_plus_lossless_blob(self):
        provenance = fusion()
        record = {
            "event_id": "E1", "body": "SUN", "icao": "ABC123",
            "predicted_event_utc": "2026-09-06T12:00:00Z",
            "aircraft_source_mode": "AUTO", "fusion_provenance": provenance,
        }
        rows = list(csv.DictReader(io.StringIO(
            records_to_csv([record]).decode("utf-8-sig"))))
        self.assertEqual("RAW_ADSB_TC19_FRESH", rows[0]["track_source"])
        self.assertEqual("0.4", rows[0]["track_age_seconds"])
        self.assertEqual("LOCAL_RAW_FRESH_PREFERRED",
                         rows[0]["track_selection_reason"])
        self.assertEqual("WGS84_HAE",
                         provenance["fields"]["geometric_altitude"]["selected"]
                         ["datum_or_reference"])
        self.assertEqual(provenance, json.loads(rows[0]["fusion_provenance_json"]))

    def test_candidate_manifest_binds_prediction_owned_provenance(self):
        frozen = fusion()
        prediction = SimpleNamespace(
            predicted_transit_utc=BASE, separation_deg=.1, slant_range_km=10,
            model="TRUE_2D", boundary_status="INTERIOR",
            aircraft_latitude_deg=1, aircraft_longitude_deg=2,
            aircraft_altitude_m=1000, aircraft_azimuth_deg=3,
            aircraft_altitude_deg=4, body_azimuth_deg=5,
            body_altitude_deg=6, body_radius_deg=.25,
            frozen_vertical_state=None, fusion_provenance=frozen)
        manifest = _prediction_manifest(prediction)
        frozen["fields"]["ground_track"]["selected"]["value"] = 999
        self.assertEqual(165.02, manifest["fusion_provenance"]["fields"]
                         ["ground_track"]["selected"]["value"])

    def test_local_only_old_record_remains_exportable(self):
        record = {"event_id": "OLD", "body": "MOON", "icao": "ABC123",
                  "predicted_event_utc": "2026-09-06T12:00:00Z"}
        row = list(csv.DictReader(io.StringIO(
            records_to_csv([record]).decode("utf-8-sig"))))[0]
        self.assertEqual("", row["fusion_provenance_json"])
        self.assertEqual("", row["track_source"])

    def test_passed_and_withdrawn_history_keep_frozen_provenance(self):
        for outcome in ("PASSED", "WITHDRAWN"):
            with self.subTest(outcome=outcome):
                state = DashboardState()
                candidate_value = DashboardCandidate(
                    body="SUN", icao="ABC123", callsign="TEST",
                    predicted_event_utc=BASE + datetime.timedelta(seconds=1),
                    separation_deg=.1, body_azimuth_deg=1,
                    body_elevation_deg=2, aircraft_elevation_deg=3,
                    distance_km=4, last_prediction_update_utc=BASE,
                    telegram_range=True, encounter_id="E1",
                    prediction_geometry="TRUE_2D",
                    aircraft_source_mode="AUTO", fusion_provenance=fusion())
                state.publish(candidate_value)
                if outcome == "PASSED":
                    state.tick(BASE + datetime.timedelta(seconds=1))
                else:
                    state.mark_history_worthy("ABC123", "SUN")
                    state.withdraw("ABC123", "SUN", BASE)
                record = state.query_history()["records"][0]
                self.assertEqual(outcome, record["outcome"])
                self.assertEqual("LOCAL_RAW_FRESH_PREFERRED", record[
                    "fusion_provenance"]["fields"]["ground_track"][
                    "selection_reason"])

    def test_snapshot_final_binding_uses_frozen_last_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = TransitSnapshotManager(
                directory, sep_threshold_deg=.5, arm_seconds=15,
                git_commit="test")
            first_fusion = fusion()
            first = {
                "recorded_at_utc": BASE, "predicted_transit_utc": BASE
                + datetime.timedelta(seconds=2), "time2x_seconds": 2,
                "separation_deg": .2, "icao": "ABC123", "body": "SUN",
                "encounter_id": "E1", "frozen_prediction_state": {
                    "fusion_provenance": first_fusion}}
            self.assertTrue(manager.consider_prediction(first))
            first_fusion["fields"]["ground_track"]["selected"]["value"] = 999
            second = json.loads(json.dumps(first, default=str))
            second.update({
                "recorded_at_utc": BASE + datetime.timedelta(seconds=1),
                "predicted_transit_utc": BASE + datetime.timedelta(seconds=2),
                "time2x_seconds": 1, "separation_deg": .1})
            second["frozen_prediction_state"]["fusion_provenance"]["fields"][
                "ground_track"]["selected"]["value"] = 166.0
            manager.consider_prediction(second)
            event = manager.active_events[("ABC123", "SUN", "E1")]
            document = manager._document(
                event, BASE + datetime.timedelta(seconds=8), True, "normal")
            second["frozen_prediction_state"]["fusion_provenance"]["fields"][
                "ground_track"]["selected"]["value"] = 777
            self.assertEqual(165.02, document["trigger_prediction"][
                "frozen_prediction_state"]["fusion_provenance"]["fields"][
                "ground_track"]["selected"]["value"])
            self.assertEqual(166.0, document["final_prediction"][
                "frozen_prediction_state"]["fusion_provenance"]["fields"][
                "ground_track"]["selected"]["value"])
            self.assertEqual(1, document["final_prediction_binding"][
                "prediction_revision_index"])


if __name__ == "__main__":
    unittest.main()
