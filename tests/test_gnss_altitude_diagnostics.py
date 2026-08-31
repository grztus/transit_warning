import datetime
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from beast_intent import modes_crc
import transit_warning as transit
from raw_adsb_diagnostic_replay import (
    RawDiagnosticReplay,
    iter_raw_diagnostic_events,
)
from raw_adsb_track import (
    decode_raw_tc19_altitude,
    decode_raw_tc19_track,
    decode_raw_tc31_version,
)


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 30, 18, 38, 48, tzinfo=UTC)
BASE = "8D4BAA929908E3B2F0042FBB4B20"
TC31 = "8D4BAB26F82100020049B851C4E6"


def frame_with_difference(raw, negative=False):
    value = int(BASE, 16)
    mask = ((1 << 8) - 1) << (112 - 88)
    value &= ~mask
    value |= int(bool(negative)) << (112 - 81)
    value |= raw << (112 - 88)
    value &= ~0xFFFFFF
    message = value.to_bytes(14, "big")
    return "*{:028X};".format(value | modes_crc(message))


class DecoderTests(unittest.TestCase):
    def test_positive_negative_unavailable_and_resolution(self):
        positive = decode_raw_tc19_altitude(frame_with_difference(48))
        negative = decode_raw_tc19_altitude(frame_with_difference(48, True))
        unavailable_zero = decode_raw_tc19_altitude(frame_with_difference(0))
        unavailable_ones = decode_raw_tc19_altitude(frame_with_difference(127))
        self.assertEqual(1175.0, positive.gnss_minus_baro_ft)
        self.assertEqual(-1175.0, negative.gnss_minus_baro_ft)
        self.assertEqual(25.0, decode_raw_tc19_altitude(
            frame_with_difference(2)).gnss_minus_baro_ft)
        self.assertFalse(unavailable_zero.available)
        self.assertFalse(unavailable_ones.available)
        self.assertIsNone(unavailable_zero.gnss_minus_baro_ft)
        self.assertIsNone(unavailable_ones.gnss_minus_baro_ft)

    def test_track_decoder_keeps_existing_track_and_adds_altitude_fields(self):
        decoded = decode_raw_tc19_track(frame_with_difference(48))
        self.assertAlmostEqual(150.8974897419251, decoded.track_deg)
        self.assertEqual(1175.0, decoded.gnss_minus_baro_ft)
        self.assertTrue(decoded.gnss_minus_baro_available)

    def test_tc31_version_two_and_unknown_datum_policy(self):
        decoded = decode_raw_tc31_version("*" + TC31 + ";")
        self.assertEqual(("4BAB26", 2), (decoded.icao, decoded.adsb_version))
        self.assertEqual("WGS84_HAE", transit._adsb_datum_for_version(2))
        self.assertEqual("UNKNOWN", transit._adsb_datum_for_version(1))
        self.assertEqual("UNKNOWN", transit._adsb_datum_for_version(0))


class DiagnosticStateTests(unittest.TestCase):
    def setUp(self):
        self.original = (
            transit.gnss_altitude_states, transit.raw_adsb_versions,
            transit.altitude_sources, transit.aircraft_motion_states,
            transit.pressure, transit.my_lat, transit.my_lon,
            transit.my_elevation_const)
        transit.gnss_altitude_states = {}
        transit.raw_adsb_versions = {}
        transit.altitude_sources = {}
        transit.aircraft_motion_states = {}
        transit.pressure = 1015.0
        transit.my_lat = 51.0
        transit.my_lon = 21.0
        transit.my_elevation_const = 200.0

    def tearDown(self):
        (transit.gnss_altitude_states, transit.raw_adsb_versions,
         transit.altitude_sources, transit.aircraft_motion_states,
         transit.pressure, transit.my_lat, transit.my_lon,
         transit.my_elevation_const) = self.original

    def add_version_and_tc19(self, raw=48, age=0):
        transit.update_raw_adsb_version(
            decode_raw_tc31_version("*" + TC31 + ";"), NOW)
        decoded = decode_raw_tc19_altitude(frame_with_difference(raw))
        decoded = SimpleNamespace(**{
            **decoded.__dict__, "icao": "4BAB26"})
        transit.update_gnss_altitude_diagnostic(
            decoded, NOW - datetime.timedelta(seconds=age))

    def add_pressure_altitude(self, age=0):
        transit.altitude_sources["4BAB26"] = {"adsb": transit.AltitudeMeasurement(
            "adsb", "barometric", 36000, 10986.668,
            NOW - datetime.timedelta(seconds=age), "3")}

    def test_thy7db_reference_is_diagnostic_37175_ft(self):
        self.add_version_and_tc19()
        self.add_pressure_altitude()
        result = transit.gnss_altitude_snapshot_diagnostics("4BAB26", NOW)
        self.assertTrue(result["fresh"])
        self.assertEqual("WGS84_HAE", result["datum"])
        self.assertEqual(37175.0, result["implied_gnss_altitude_ft"])
        self.assertAlmostEqual(11330.94, result["implied_gnss_hae_m"])
        self.assertIsNone(result["derived_orthometric_altitude_m"])
        self.assertEqual("geoid_conversion_unavailable",
                         result["fallback_reason"])

    def test_freshness_expiry_and_new_valid_value(self):
        self.add_version_and_tc19(age=5.001)
        self.add_pressure_altitude()
        stale = transit.gnss_altitude_snapshot_diagnostics("4BAB26", NOW)
        self.assertFalse(stale["fresh"])
        self.assertEqual("tc19_stale", stale["fallback_reason"])
        self.add_version_and_tc19(raw=49)
        fresh = transit.gnss_altitude_snapshot_diagnostics("4BAB26", NOW)
        self.assertTrue(fresh["fresh"])
        self.assertEqual(1200.0, fresh["gnss_minus_baro_ft"])

    def test_unavailable_replaces_prior_value_without_resurrection(self):
        self.add_version_and_tc19()
        self.add_pressure_altitude()
        unavailable = decode_raw_tc19_altitude(frame_with_difference(0))
        transit.update_gnss_altitude_diagnostic(SimpleNamespace(**{
            **unavailable.__dict__, "icao": "4BAB26"}), NOW)
        result = transit.gnss_altitude_snapshot_diagnostics("4BAB26", NOW)
        self.assertFalse(result["available"])
        self.assertEqual("tc19_unavailable", result["fallback_reason"])
        self.assertIsNone(result["gnss_minus_baro_ft"])

    def test_unknown_datum_never_claims_hae(self):
        self.add_version_and_tc19()
        self.add_pressure_altitude()
        transit.raw_adsb_versions["4BAB26"] = transit.RawAdsbVersionState(
            1, NOW, "UNKNOWN")
        result = transit.gnss_altitude_snapshot_diagnostics("4BAB26", NOW)
        self.assertEqual("datum_unknown", result["fallback_reason"])
        self.assertEqual(37175.0, result["implied_gnss_altitude_ft"])
        self.assertIsNone(result["implied_gnss_hae_m"])

    def test_snapshot_capture_serializes_separate_diagnostic_section(self):
        manager = Mock()
        result = (51.1, 21.1, 120.0, 10.0, 20.0, 30.0, 5.0,
                  0, 121.0, 10.5, NOW)
        solver_input = {
            "aircraft_altitude_m": 10000.0, "groundspeed": 800.0,
            "track": 180.0,
        }
        diagnostic = {"available": True, "gnss_minus_baro_ft": 1175.0}
        old_manager = transit.transit_snapshot_manager
        transit.transit_snapshot_manager = manager
        transit.sun_predicted_transit_utc["4BAB26"] = (
            NOW + datetime.timedelta(seconds=5))
        try:
            with (patch.object(transit, "gnss_altitude_snapshot_diagnostics",
                               return_value=diagnostic),
                  patch.object(transit, "build_frozen_prediction_state",
                               return_value={})):
                transit.capture_transit_prediction(
                    "4BAB26", "THY7DB", "sun", result, NOW, solver_input)
            captured = manager.consider_prediction.call_args.args[0]
        finally:
            transit.transit_snapshot_manager = old_manager
            transit.sun_predicted_transit_utc.pop("4BAB26", None)
        self.assertEqual(diagnostic, captured["gnss_altitude_diagnostics"])

    def test_diagnostic_update_does_not_change_production_motion_or_solver(self):
        motion = transit.AircraftMotionState(
            altitude=transit.MotionParameter(10986.668, NOW, "adsb"))
        transit.aircraft_motion_states["4BAB26"] = motion
        fixed_clock = SimpleNamespace(now_utc=lambda: NOW)
        with patch.object(transit, "clock", fixed_clock):
            before = transit.transit_pred(
                (51.0, 21.0), (51.5, 21.5), 200, 800,
                10986.668, 10, 180)
            self.add_version_and_tc19()
            after = transit.transit_pred(
                (51.0, 21.0), (51.5, 21.5), 200, 800,
                10986.668, 10, 180)
        self.assertIs(motion, transit.aircraft_motion_states["4BAB26"])
        self.assertEqual(10986.668, motion.altitude.value)
        self.assertEqual(before, after)

    def test_prediction_digest_is_identical_with_diagnostics_enabled(self):
        cases = (
            ((51.0, 21.0), (51.5, 21.5), 200, 800, 10986.668,
             10, 180),
            ((51.0, 21.0), (50.8, 20.7), 260, 720, 8000.0,
             20, 250),
            ((51.0, 21.0), (51.2, 20.8), 120, 650, 11000.0,
             35, 130),
        )

        def prediction_digest():
            predictions = []
            for arguments in cases:
                result = transit.transit_pred(*arguments)
                if not result:
                    continue
                separation = transit.vertical_transit_separation(
                    result[3], result[9])
                predictions.append({
                    "time": result[10].isoformat(),
                    "separation": separation,
                    "classification": "HIT" if separation < 0.5 else "MISS",
                    "result": tuple(
                        item.isoformat() if isinstance(item, datetime.datetime)
                        else item for item in result),
                })
            payload = json.dumps(
                predictions, sort_keys=True, separators=(",", ":"))
            return len(predictions), hashlib.sha256(
                payload.encode("utf-8")).hexdigest()

        fixed_clock = SimpleNamespace(now_utc=lambda: NOW)
        with patch.object(transit, "clock", fixed_clock):
            without_diagnostics = prediction_digest()
            self.add_version_and_tc19()
            self.add_pressure_altitude()
            with_diagnostics = prediction_digest()
        self.assertEqual(without_diagnostics, with_diagnostics)


class ReplayTests(unittest.TestCase):
    def test_streaming_replay_restores_value_version_and_timestamp(self):
        records = [
            {"version": 1, "time": "2026-08-30T18:38:47Z",
             "icao": "4BAB26", "message_type": "TC31", "subtype": 0,
             "adsb_version": 2, "datum": "WGS84_HAE",
             "receiver_timestamp_hex": None, "provenance": "RAW_ADSB_TC31"},
            {"version": 1, "time": "2026-08-30T18:38:48Z",
             "icao": "4BAB26", "message_type": "TC19", "subtype": 1,
             "raw_encoded_value": 48, "gnss_minus_baro_ft": 1175.0,
             "available": True, "vertical_rate_source": "BAROMETRIC",
             "receiver_timestamp_hex": "001122334455",
             "provenance": "RAW_ADSB_TC19"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("".join(json.dumps(item) + "\n" for item in records),
                            encoding="utf-8")
            replay = RawDiagnosticReplay(iter_raw_diagnostic_events(path))
            first = list(replay.pop_through(NOW - datetime.timedelta(seconds=1)))
            second = list(replay.pop_through(NOW))
        self.assertEqual("TC31", first[0].record["message_type"])
        self.assertEqual(1175.0, second[0].record["gnss_minus_baro_ft"])
        self.assertEqual(NOW, second[0].time)

    def test_legacy_session_has_no_required_diagnostic_file(self):
        transit.configure_raw_diagnostic_replay(None)
        self.assertIsNone(transit.raw_diagnostic_replay)

    def test_application_replay_restores_live_equivalent_state(self):
        records = [
            {"version": 1, "time": "2026-08-30T18:38:47Z",
             "icao": "4BAB26", "message_type": "TC31", "subtype": 0,
             "adsb_version": 2, "datum": "WGS84_HAE",
             "receiver_timestamp_hex": None, "provenance": "RAW_ADSB_TC31"},
            {"version": 1, "time": "2026-08-30T18:38:48Z",
             "icao": "4BAB26", "message_type": "TC19", "subtype": 1,
             "raw_encoded_value": 48, "gnss_minus_baro_ft": 1175.0,
             "available": True, "vertical_rate_source": "BAROMETRIC",
             "receiver_timestamp_hex": "001122334455",
             "provenance": "RAW_ADSB_TC19"},
        ]
        old_states = (transit.gnss_altitude_states,
                      transit.raw_adsb_versions,
                      transit.raw_diagnostic_replay)
        transit.gnss_altitude_states = {}
        transit.raw_adsb_versions = {}
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "events.jsonl"
                path.write_text("".join(
                    json.dumps(item) + "\n" for item in records),
                    encoding="utf-8")
                transit.configure_raw_diagnostic_replay(path)
                transit.apply_replay_raw_diagnostics(NOW)
            state = transit.gnss_altitude_states["4BAB26"]
            version = transit.raw_adsb_versions["4BAB26"]
        finally:
            (transit.gnss_altitude_states, transit.raw_adsb_versions,
             transit.raw_diagnostic_replay) = old_states
        self.assertEqual(1175.0, state.gnss_minus_baro_ft)
        self.assertEqual(NOW, state.updated_at_utc)
        self.assertEqual((2, "WGS84_HAE"),
                         (version.adsb_version, version.datum))


if __name__ == "__main__":
    unittest.main()
