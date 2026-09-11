import unittest

from auto_fusion import (
    FieldCandidate, ProviderSnapshotCache, fuse_aircraft, fuse_local_aircraft,
    select_field,
    predictor_view, serialize_fused_state,
)


def local(name, value, age, source="adsb", unit=None, datum=None):
    return FieldCandidate(name, value, source, "LOCAL", unit, datum,
                          freshness_state="KNOWN", age_seconds=age,
                          observed_at_utc="2026-09-06T12:00:00Z")


def remote(name, value, age=None, availability="PRESENT", unit=None,
           datum=None):
    return FieldCandidate(name, value, "ADSBLOL", "ADSBLOL", unit, datum,
                          availability, "KNOWN" if age is not None else "UNKNOWN",
                          age_seconds=age, snapshot_id="snapshot")


def observation(name, value, *, age=None, unit=None, datum=None):
    return {
        "field_name": name, "value": value, "source": "ADSBLOL",
        "availability": "PRESENT", "freshness_state": (
            "KNOWN" if age is not None else "UNKNOWN"),
        "age_seconds": age, "unit": unit, "datum_or_reference": datum,
        "observed_at_utc": None, "observed_at_basis": "UNKNOWN",
        "received_at_monotonic": 100.0, "snapshot_id": "snapshot",
        "provenance": {"provider": "ADSB_LOL"}, "quality": {},
    }


class AutoFusionTests(unittest.TestCase):
    def select(self, name, local_value, remote_value, position=True, lease=True):
        return select_field(name, local_value, remote_value,
                            remote_position_eligible=position,
                            snapshot_lease_valid=lease)

    def test_fresh_and_degraded_local_win(self):
        for age, reason in ((1, "LOCAL_FRESH_PREFERRED"),
                            (7, "LOCAL_DEGRADED_KNOWN_AGE_PREFERRED")):
            result = self.select("position", local("position", {"lat": 1, "lon": 2}, age),
                                 remote("position", {"lat": 3, "lon": 4}, 1))
            self.assertEqual("LOCAL", result.selected.source_family)
            self.assertEqual(reason, result.selection_reason)

    def test_unknown_remote_does_not_replace_known_local(self):
        for age, reason in (
                (1, "UNKNOWN_AGE_NOT_SELECTED_OVER_FRESH_LOCAL"),
                (7, "UNKNOWN_AGE_NOT_SELECTED_OVER_DEGRADED_LOCAL")):
            result = self.select("groundspeed", local("groundspeed", 100, age),
                                 remote("groundspeed", 200))
            self.assertEqual(100, result.selected.value)
            self.assertEqual(reason, result.selection_reason)

    def test_stale_and_unavailable_local_fall_back(self):
        for candidate, reason in (
                (local("groundspeed", 100, 11), "LOCAL_STALE_ADSBLOL_FALLBACK"),
                (None, "ADSBLOL_ONLY")):
            result = self.select("groundspeed", candidate,
                                 remote("groundspeed", 200))
            self.assertEqual(200, result.selected.value)
            self.assertEqual(reason, result.selection_reason)
            self.assertEqual("UNKNOWN", result.selected.freshness_state)

    def test_expired_position_or_lease_blocks_remote(self):
        self.assertIsNone(self.select("position", None, remote("position", {}),
                                      position=False).selected)
        self.assertEqual("ADSBLOL_POSITION_EXPIRED",
                         self.select("position", None, remote("position", {}),
                                     position=False).selection_reason)
        self.assertEqual("ADSBLOL_SNAPSHOT_LEASE_EXPIRED",
                         self.select("position", None, remote("position", {}),
                                     lease=False).selection_reason)

    def test_precision_track_reason_codes(self):
        for source, reason in (
                ("RAW_ADSB_TC19_FRESH", "LOCAL_RAW_FRESH_PREFERRED"),
                ("RAW_ADSB_TC19_HELD", "LOCAL_RAW_HELD_PREFERRED"),
                ("MLAT_BEAST_TC19_FRESH", "LOCAL_MLAT_PRECISION_FRESH_PREFERRED")):
            result = self.select("ground_track", local("ground_track", 90, 1, source),
                                 remote("ground_track", 91))
            self.assertEqual(reason, result.selection_reason)

    def test_remote_vertical_and_intent_are_diagnostic_only(self):
        for name in ("barometric_vertical_rate", "geometric_vertical_rate",
                     "selected_altitude_mcp", "selected_altitude_fms",
                     "selected_altimeter_setting"):
            self.assertEqual("DIAGNOSTIC_ONLY",
                             self.select(name, None, remote(name, 1)).predictor_eligibility)

    def test_altitudes_stay_distinct_and_metadata_survives(self):
        aircraft = {"snapshot_id": "snapshot", "fields": {
            "position": observation("position", {"lat": 1, "lon": 2}, age=1),
            "barometric_altitude": observation("barometric_altitude", 10000,
                                                 unit="ft", datum="PRESSURE"),
            "geometric_altitude": observation("geometric_altitude", 10500,
                                                unit="ft", datum="WGS84_HAE")}}
        state = fuse_aircraft("ABC123", {}, aircraft,
                              evaluated_at_utc="2026-09-06T12:00:00Z",
                              evaluated_at_monotonic=101, receipt_monotonic=100,
                              provider_health="OK")
        self.assertEqual("PRESSURE", state.fields["barometric_altitude"].selected.datum_or_reference)
        self.assertEqual("WGS84_HAE", state.fields["geometric_altitude"].selected.datum_or_reference)
        self.assertIn("selection_reason", serialize_fused_state(state)["fields"]["geometric_altitude"])

    def test_callsign_fallback(self):
        result = self.select("callsign", None, remote("callsign", "LOT71"))
        self.assertEqual("LOT71", result.selected.value)

    def test_identical_snapshot_does_not_reset_anchor(self):
        cache = ProviderSnapshotCache()
        report = {"status": "OK", "snapshot_id": "same", "aircraft": [],
                  "request_finished_monotonic": 100}
        cache.consume(report, 100)
        report["request_finished_monotonic"] = 110
        cache.consume(report, 110)
        self.assertEqual(100, cache.receipt_monotonic)

    def test_hae_predictor_conversion_and_frozen_remote_vertical(self):
        aircraft = {"snapshot_id": "snapshot", "fields": {
            "position": observation("position", {"lat": 1, "lon": 2}, age=1),
            "ground_track": observation("ground_track", 90, unit="deg"),
            "groundspeed": observation("groundspeed", 100, unit="knots"),
            "geometric_altitude": observation("geometric_altitude", 1000,
                                                unit="ft", datum="WGS84_HAE"),
            "barometric_vertical_rate": observation("barometric_vertical_rate", 1000)}}
        state = fuse_aircraft("ABC123", {}, aircraft,
                              evaluated_at_utc="2026-09-06T12:00:00Z",
                              evaluated_at_monotonic=101, receipt_monotonic=100,
                              provider_health="OK")
        geoid = type("Geoid", (), {"undulation_m": lambda self, lat, lon: 30})()
        view = predictor_view(state, application_qnh_hpa=1013.25, geoid=geoid)
        self.assertAlmostEqual(274.8, view["altitude_m"])
        self.assertEqual("EGM96_AMSL", view["altitude_candidate"].datum_or_reference)
        self.assertEqual("HAE_MINUS_EGM96", view["altitude_candidate"].provenance["derivation"])
        self.assertEqual("DEGRADED_UNKNOWN_FIELD_AGE", view["classification"])
        self.assertTrue(view["freeze_altitude"])
        self.assertIsNone(view["vertical_rate"])

    def test_local_only_uses_same_serializable_contract(self):
        state = fuse_local_aircraft(
            "ABC123", {"ground_track": local(
                "ground_track", 165.02, .4, "RAW_ADSB_TC19_FRESH")},
            evaluated_at_utc="2026-09-06T12:00:00Z",
            evaluated_at_monotonic=100)
        field = serialize_fused_state(state)["fields"]["ground_track"]
        self.assertEqual("LOCAL_RAW_FRESH_PREFERRED",
                         field["selection_reason"])
        self.assertEqual("LOCAL", field["selected"]["source_family"])


if __name__ == "__main__":
    unittest.main()
