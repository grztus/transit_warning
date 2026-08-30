import datetime
import io
import unittest
from unittest.mock import Mock, patch

import transit_warning as transit
from raw_adsb_track import decode_raw_tc19_track, extract_modes_hex
from transit_prediction_model import MotionParameter


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 30, 10, 43, 5, tzinfo=UTC)
FRAME_SOUTHEAST = "@0097635C74DC8D4BAA929908E3B2F0042FBB4B20;"
FRAME_NORTHEAST = "@0097636D9F5E8D461F319909062FF8082CB82FF0;"
FRAME_NORTHWEST = "@00979D4465E48D48ADAB9914033038842028A648;"
FRAME_SOUTHWEST = "@009764E040B88D4D2135990C4AB3E8082E566A5E;"


class RawTc19DecoderTests(unittest.TestCase):
    def test_real_frame_extracts_icao_components_and_fractional_track(self):
        decoded = decode_raw_tc19_track(FRAME_SOUTHEAST)

        self.assertEqual("4BAA92", decoded.icao)
        self.assertEqual(1, decoded.subtype)
        self.assertEqual(226.0, decoded.east_west_velocity_knots)
        self.assertEqual(-406.0, decoded.north_south_velocity_knots)
        self.assertAlmostEqual(150.8974897419251, decoded.track_deg)
        self.assertNotEqual(round(decoded.track_deg), decoded.track_deg)

    def test_all_velocity_sign_quadrants_and_normalization(self):
        cases = (
            (FRAME_NORTHEAST, 261.0, 382.0, 34.34268906720732),
            (FRAME_NORTHWEST, -2.0, 384.0, 359.7015871800051),
            (FRAME_SOUTHWEST, -73.0, -414.0, 190.00008455834495),
        )
        for frame, east_west, north_south, track in cases:
            with self.subTest(frame=frame):
                decoded = decode_raw_tc19_track(frame)
                self.assertEqual(east_west, decoded.east_west_velocity_knots)
                self.assertEqual(north_south, decoded.north_south_velocity_knots)
                self.assertAlmostEqual(track, decoded.track_deg)
                self.assertGreaterEqual(decoded.track_deg, 0.0)
                self.assertLess(decoded.track_deg, 360.0)

    def test_timestamped_capture_line_and_star_format_are_accepted(self):
        timestamped = "2026-08-30T10:43:04.5126970Z " + FRAME_SOUTHEAST
        payload = extract_modes_hex(FRAME_SOUTHEAST)
        self.assertEqual("8D4BAA929908E3B2F0042FBB4B20", payload)
        self.assertEqual("4BAA92", decode_raw_tc19_track(timestamped).icao)
        self.assertEqual("4BAA92", decode_raw_tc19_track("*" + payload + ";").icao)

    def test_malformed_bad_crc_and_unsupported_frames_are_rejected(self):
        self.assertIsNone(decode_raw_tc19_track("garbage"))
        self.assertIsNone(decode_raw_tc19_track(FRAME_SOUTHEAST[:-2] + "1;"))
        self.assertIsNone(decode_raw_tc19_track(
            "@00976060AD1E8D48E85F590BB5A968242724AFE3;"))


class RawTrackPrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.original_clock = transit.clock
        self.original_states = transit.aircraft_motion_states
        self.original_raw = transit.raw_adsb_tracks
        transit.clock = Mock()
        transit.clock.now_utc.return_value = NOW
        transit.aircraft_motion_states = {}
        transit.raw_adsb_tracks = {}

    def tearDown(self):
        transit.clock = self.original_clock
        transit.aircraft_motion_states = self.original_states
        transit.raw_adsb_tracks = self.original_raw

    def add_fallback(self, icao="4BAA92", value=150.0, source="adsb"):
        transit.aircraft_motion_states[icao] = transit.AircraftMotionState(
            track=MotionParameter(value, NOW, source))

    def add_raw(self, icao="4BAA92", precise=150.8974897419251,
                raw_age=0.0, anchor=150.0, hold_valid=True):
        transit.raw_adsb_tracks[icao] = transit.RawAdsbTrackState(
            precise_value_deg=precise,
            raw_updated_at_utc=NOW - datetime.timedelta(seconds=raw_age),
            coarse_anchor_deg=anchor,
            hold_valid=hold_valid)

    def test_unavailable_or_nonmatching_raw_uses_existing_track(self):
        self.add_fallback()
        self.add_raw("461F31", 34.342689, anchor=34.0)

        effective = transit.effective_track_parameter("4BAA92", 150, NOW)

        self.assertEqual(150.0, effective.value)
        self.assertEqual("adsb", effective.source)

    def test_fresh_matching_raw_uses_full_precision_and_source(self):
        self.add_fallback()
        precise = 150.8974897419251
        self.add_raw(precise=precise, raw_age=5.0)

        effective = transit.effective_track_parameter("4BAA92", 150, NOW)

        self.assertEqual(precise, effective.value)
        self.assertEqual("RAW_ADSB_TC19_FRESH", effective.source)
        self.assertEqual("150.9", transit.format_track_for_display(
            "4BAA92", "150", NOW))

    def test_stale_raw_is_held_while_fresh_coarse_track_matches_anchor(self):
        self.add_fallback()
        self.add_raw(raw_age=5.001)

        effective = transit.effective_track_parameter("4BAA92", "150", NOW)

        self.assertEqual(150.8974897419251, effective.value)
        self.assertEqual("RAW_ADSB_TC19_HELD", effective.source)
        self.assertEqual("150.9", transit.format_track_for_display(
            "4BAA92", "150", NOW))

    def test_repeated_matching_coarse_updates_keep_stale_raw_held(self):
        self.add_fallback()
        self.add_raw(raw_age=20.0)
        for seconds in (1, 2, 3):
            transit.aircraft_motion_states["4BAA92"].track = MotionParameter(
                150.0, NOW - datetime.timedelta(seconds=seconds), "adsb")
            effective = transit.effective_track_parameter("4BAA92", 150, NOW)
            self.assertEqual("RAW_ADSB_TC19_HELD", effective.source)

    def test_coarse_track_change_up_invalidates_hold_immediately(self):
        self.add_fallback(value=151.0)
        self.add_raw(raw_age=10.0)
        effective = transit.effective_track_parameter("4BAA92", 151, NOW)
        self.assertEqual((151.0, "adsb"), (effective.value, effective.source))
        self.assertFalse(transit.raw_adsb_tracks["4BAA92"].hold_valid)

    def test_coarse_track_change_down_invalidates_hold_immediately(self):
        self.add_fallback(value=149.0)
        self.add_raw(raw_age=10.0)
        effective = transit.effective_track_parameter("4BAA92", 149, NOW)
        self.assertEqual((149.0, "adsb"), (effective.value, effective.source))
        self.assertFalse(transit.raw_adsb_tracks["4BAA92"].hold_valid)

    def test_invalidated_raw_does_not_resurrect_when_coarse_returns_to_anchor(self):
        self.add_fallback(value=151.0)
        self.add_raw(raw_age=10.0)
        transit.effective_track_parameter("4BAA92", 151, NOW)
        transit.aircraft_motion_states["4BAA92"].track = MotionParameter(
            150.0, NOW, "adsb")
        effective = transit.effective_track_parameter("4BAA92", 150, NOW)
        self.assertEqual((150.0, "adsb"), (effective.value, effective.source))

    def test_new_raw_frame_reestablishes_anchor_after_invalidation(self):
        self.add_fallback(value=151.0)
        self.add_raw(raw_age=10.0)
        transit.effective_track_parameter("4BAA92", 151, NOW)
        decoded = decode_raw_tc19_track(FRAME_SOUTHEAST)
        transit.update_raw_adsb_track(decoded, NOW)
        effective = transit.effective_track_parameter("4BAA92", 151, NOW)
        self.assertEqual(decoded.track_deg, effective.value)
        self.assertEqual("RAW_ADSB_TC19_FRESH", effective.source)
        self.assertEqual(151.0,
                         transit.raw_adsb_tracks["4BAA92"].coarse_anchor_deg)

    def test_stale_raw_and_stale_coarse_does_not_use_held_value(self):
        transit.aircraft_motion_states["4BAA92"] = transit.AircraftMotionState(
            track=MotionParameter(
                150.0, NOW - datetime.timedelta(seconds=5.001), "adsb"))
        self.add_raw(raw_age=10.0)
        effective = transit.effective_track_parameter("4BAA92", 150, NOW)
        self.assertEqual((150.0, "adsb"), (effective.value, effective.source))

    def test_source_change_to_matching_mlat_keeps_hold(self):
        self.add_fallback(source="mlat")
        self.add_raw(raw_age=10.0)
        effective = transit.effective_track_parameter("4BAA92", 150, NOW)
        self.assertEqual("RAW_ADSB_TC19_HELD", effective.source)

    def test_source_change_to_different_mlat_track_invalidates_hold(self):
        self.add_fallback(value=151.0, source="mlat")
        self.add_raw(raw_age=10.0)
        effective = transit.effective_track_parameter("4BAA92", 151, NOW)
        self.assertEqual((151.0, "mlat"), (effective.value, effective.source))

    def test_display_uses_decimal_for_fresh_and_held_only(self):
        self.add_fallback()
        self.add_raw(raw_age=0.0)
        self.assertEqual("150.9", transit.format_track_for_display(
            "4BAA92", "150", NOW))
        self.add_raw(raw_age=10.0)
        self.assertEqual("150.9", transit.format_track_for_display(
            "4BAA92", "150", NOW))
        transit.raw_adsb_tracks["4BAA92"].hold_valid = False
        self.assertEqual("150", transit.format_track_for_display(
            "4BAA92", "150", NOW))

    def test_mlat_only_fallback_is_unchanged(self):
        self.add_fallback("ABC123", 271.0, "mlat")
        effective = transit.effective_track_parameter("ABC123", 271, NOW)
        self.assertEqual((271.0, "mlat"), (effective.value, effective.source))

    def test_snapshot_solver_input_records_effective_track_provenance(self):
        self.add_fallback()
        precise = 150.8974897419251
        self.add_raw(precise=precise)
        state = transit.aircraft_motion_states["4BAA92"]
        state.position = transit.PositionParameter(51.0, 21.0, NOW, "adsb")
        state.altitude = MotionParameter(10000.0, NOW, "adsb")
        state.groundspeed = MotionParameter(850.0, NOW, "adsb")

        result = transit.build_snapshot_solver_input(
            "4BAA92", 51.0, 21.0, 10000, 50, 180, 10, 850, precise)

        self.assertEqual(precise, result["track"])
        self.assertEqual("RAW_ADSB_TC19_FRESH", result["track_source"])
        self.assertEqual(utc_text(NOW), result["track_timestamp_utc"])
        self.assertEqual(utc_text(NOW), result["raw_track_timestamp_utc"])
        self.assertEqual(150.0, result["track_coarse_anchor_deg"])


class RawTrackReaderTests(unittest.TestCase):
    def setUp(self):
        transit.stop_event.clear()

    def tearDown(self):
        transit.stop_event.clear()

    def test_malformed_input_is_ignored_and_valid_input_updates_matching_icao(self):
        socket_instance = Mock()
        socket_instance.makefile.return_value = io.StringIO(
            "malformed\n" + FRAME_NORTHEAST + "\n")
        with patch.object(transit.socket, "socket", return_value=socket_instance), \
                patch.object(transit, "_register_active_socket", return_value=True), \
                patch.object(transit, "_unregister_active_socket"), \
                patch.object(transit, "update_raw_adsb_track") as update, \
                patch.object(transit.stop_event, "wait", return_value=True):
            transit.read_raw_adsb_track("receiver", 30002)

        update.assert_called_once()
        self.assertEqual("461F31", update.call_args.args[0].icao)

    def test_connection_failure_is_diagnostic_and_retries_fail_open(self):
        socket_instance = Mock()
        socket_instance.connect.side_effect = OSError("unavailable")
        with patch.object(transit.socket, "socket", return_value=socket_instance), \
                patch.object(transit, "_register_active_socket", return_value=True), \
                patch.object(transit, "_unregister_active_socket"), \
                patch.object(transit.stop_event, "wait", return_value=True) as wait:
            transit.read_raw_adsb_track("receiver", 30002)

        wait.assert_called_once_with(5)
        self.assertEqual("unavailable", transit.raw_adsb_track_diagnostics.last_error)


def utc_text(value):
    return value.isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    unittest.main()
