import datetime
import math
import unittest
from unittest.mock import Mock, patch

from beast_intent import BeastFrame, BeastFrameParser, modes_crc
from mlat_beast_track import (
    MLAT_BEAST_TIMESTAMP, decode_mlat_beast_tc19,
    truncation_bin_consistent,
)
import transit_warning as transit
from transit_prediction_model import MotionParameter


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def velocity_message(icao=0x4BAA6D, ew=-59, ns=-415, subtype=1,
                     df=18, cf=2, imf=0, tc=19):
    scale = 4 if subtype == 2 else 1

    def encoded(value):
        return abs(value) // scale + 1 | (0x400 if value < 0 else 0)

    east_west = encoded(ew)
    north_south = encoded(ns)
    message = bytearray(11)
    message[0] = (df << 3) | cf
    message[1:4] = icao.to_bytes(3, "big")
    message[4] = (tc << 3) | subtype
    message[5] = (imf << 7) | ((east_west >> 8) & 7)
    message[6] = east_west & 255
    message[7] = (north_south >> 3) & 255
    message[8] = (north_south & 7) << 5
    raw = bytes(message) + b"\x00\x00\x00"
    return (int.from_bytes(raw, "big") | modes_crc(raw)).to_bytes(14, "big")


def frame(message=None, timestamp=MLAT_BEAST_TIMESTAMP, signal=0,
          frame_type=0x33):
    return BeastFrame(frame_type, timestamp, signal,
                      message or velocity_message())


def wire(value):
    payload = (value.beast_timestamp.to_bytes(6, "big")
               + bytes((value.signal,)) + value.modes)
    return b"\x1a" + bytes((value.frame_type,)) + payload.replace(
        b"\x1a", b"\x1a\x1a")


class MlatBeastDecoderTests(unittest.TestCase):
    def test_decodes_real_synthetic_velocity(self):
        decoded = decode_mlat_beast_tc19(frame())
        self.assertEqual("4BAA6D", decoded.icao)
        self.assertEqual((-59.0, -415.0), (
            decoded.east_west_velocity_knots,
            decoded.north_south_velocity_knots))
        self.assertAlmostEqual(188.091441, decoded.track_deg, places=6)
        self.assertAlmostEqual(math.hypot(59, 415),
                               decoded.groundspeed_knots)

    def test_parser_handles_chunks_multiple_frames_and_escaping(self):
        data = wire(frame(velocity_message(icao=0x1A0102))) + wire(frame(
            velocity_message(icao=0xABCDEF, ew=100, ns=200)))
        parser = BeastFrameParser()
        frames = (parser.feed(data[:7]) + parser.feed(data[7:19])
                  + parser.feed(data[19:]))
        self.assertEqual(["1A0102", "ABCDEF"], [
            decode_mlat_beast_tc19(item).icao for item in frames])

    def test_parser_resynchronizes_after_malformed_data(self):
        parser = BeastFrameParser()
        frames = parser.feed(b"garbage\x1aXbroken" + wire(frame()))
        self.assertEqual(1, len(frames))
        self.assertGreater(parser.resync_count, 0)

    def test_rejects_invalid_envelope_crc_df_cf_imf_tc_and_subtype(self):
        valid = velocity_message()
        bad_crc = valid[:-1] + bytes((valid[-1] ^ 1,))
        cases = [
            frame(timestamp=0), frame(signal=1), frame(frame_type=0x32),
            frame(bad_crc), frame(velocity_message(df=17)),
            frame(velocity_message(cf=1)), frame(velocity_message(imf=1)),
            frame(velocity_message(tc=18)), frame(velocity_message(subtype=3)),
        ]
        for item in cases:
            with self.subTest(item=item):
                self.assertIsNone(decode_mlat_beast_tc19(item))

    def test_subtype_two_and_all_sign_quadrants(self):
        expected = [
            (400, 800, 26.565051), (-400, 800, 333.434949),
            (400, -800, 153.434949), (-400, -800, 206.565051),
        ]
        for ew, ns, track in expected:
            decoded = decode_mlat_beast_tc19(frame(
                velocity_message(ew=ew, ns=ns, subtype=2)))
            self.assertAlmostEqual(track, decoded.track_deg, places=6)

    def test_wrap_and_truncation_use_bins_not_rounding(self):
        decoded = decode_mlat_beast_tc19(frame(
            velocity_message(ew=0, ns=400)))
        self.assertEqual(0.0, decoded.track_deg)
        self.assertTrue(truncation_bin_consistent(decoded, 359))
        self.assertTrue(truncation_bin_consistent(decoded, 0))
        self.assertFalse(truncation_bin_consistent(decoded, 1))

    def test_uncertainty_can_cross_adjacent_integer_bin(self):
        found = None
        for ew in range(1, 80):
            candidate = decode_mlat_beast_tc19(frame(
                velocity_message(ew=ew, ns=400)))
            coarse = int(candidate.track_deg)
            if truncation_bin_consistent(candidate, coarse + 1):
                found = candidate, coarse
                break
        self.assertIsNotNone(found)
        candidate, coarse = found
        self.assertTrue(truncation_bin_consistent(candidate, coarse))
        self.assertTrue(truncation_bin_consistent(candidate, coarse + 1))

    def test_low_information_and_saturated_vectors_are_rejected(self):
        self.assertIsNone(decode_mlat_beast_tc19(frame(
            velocity_message(ew=1, ns=1))))
        self.assertIsNone(decode_mlat_beast_tc19(frame(
            velocity_message(ew=1022, ns=100))))


class MlatBeastSelectionTests(unittest.TestCase):
    def setUp(self):
        names = ("aircraft_motion_states", "raw_adsb_tracks",
                 "mlat_beast_tracks", "mlat_coarse_tracks",
                 "mlat_beast_enabled", "mlat_port")
        self.saved = {name: getattr(transit, name) for name in names}
        transit.aircraft_motion_states = {}
        transit.raw_adsb_tracks = {}
        transit.mlat_beast_tracks = {}
        transit.mlat_coarse_tracks = {}
        transit.mlat_beast_enabled = True
        transit.mlat_port = 30106
        self.decoded = decode_mlat_beast_tc19(frame())

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(transit, name, value)

    def motion(self, source="mlat", coarse=188, when=NOW):
        state = transit.AircraftMotionState(
            position=transit.PositionParameter(51, 21, when, source),
            track=MotionParameter(coarse, when, source))
        transit.aircraft_motion_states[self.decoded.icao] = state
        if source == "mlat":
            transit.mlat_coarse_tracks[self.decoded.icao] = state.track
        return state

    def test_pending_then_30106_confirms_and_reverse_arrival_order(self):
        self.motion()
        transit.mlat_coarse_tracks.clear()
        transit.update_mlat_beast_track(self.decoded, NOW)
        self.assertEqual("mlat", transit.effective_track_parameter(
            self.decoded.icao, 188, NOW).source)
        transit._update_motion_parameter(
            self.decoded.icao, "track", 188, NOW, 30106)
        self.assertEqual("MLAT_BEAST_TC19_FRESH",
            transit.effective_track_parameter(
                self.decoded.icao, 188, NOW).source)

        transit.mlat_beast_tracks.clear()
        transit.update_mlat_beast_track(self.decoded, NOW)
        self.assertTrue(transit.mlat_beast_tracks[self.decoded.icao].confirmed)

    def test_confirmation_requires_fresh_temporally_compatible_30106(self):
        state = self.motion(when=NOW - datetime.timedelta(seconds=3))
        transit.update_mlat_beast_track(self.decoded, NOW)
        self.assertTrue(transit.mlat_beast_tracks[self.decoded.icao].confirmed)

        transit.mlat_beast_tracks.clear()
        state.track = MotionParameter(
            188, NOW - datetime.timedelta(seconds=3.001), "mlat")
        transit.mlat_coarse_tracks[self.decoded.icao] = state.track
        transit.update_mlat_beast_track(self.decoded, NOW)
        self.assertFalse(transit.mlat_beast_tracks[self.decoded.icao].confirmed)

    def test_mismatched_coarse_bin_stays_pending_and_uses_coarse_track(self):
        self.motion(coarse=190)
        transit.update_mlat_beast_track(self.decoded, NOW)
        result = transit.effective_track_parameter(
            self.decoded.icao, 190, NOW)
        self.assertFalse(transit.mlat_beast_tracks[self.decoded.icao].confirmed)
        self.assertEqual((190, "mlat"), (result.value, result.source))

    def test_held_invalidation_is_irreversible_until_new_tc19(self):
        self.motion()
        transit.update_mlat_beast_track(self.decoded, NOW)
        later = NOW + datetime.timedelta(seconds=6)
        transit._update_motion_parameter(
            self.decoded.icao, "track", 188, later, 30106)
        self.assertEqual("MLAT_BEAST_TC19_HELD",
            transit.effective_track_parameter(
                self.decoded.icao, 188, later).source)
        changed = later + datetime.timedelta(seconds=1)
        transit._update_motion_parameter(
            self.decoded.icao, "track", 189, changed, 30106)
        self.assertEqual("mlat", transit.effective_track_parameter(
            self.decoded.icao, 189, changed).source)
        returned = changed + datetime.timedelta(seconds=1)
        transit._update_motion_parameter(
            self.decoded.icao, "track", 188, returned, 30106)
        self.assertEqual("mlat", transit.effective_track_parameter(
            self.decoded.icao, 188, returned).source)
        transit.update_mlat_beast_track(self.decoded, returned)
        self.assertEqual("MLAT_BEAST_TC19_FRESH",
            transit.effective_track_parameter(
                self.decoded.icao, 188, returned).source)

    def test_raw_priority_independence_and_position_transitions(self):
        state = self.motion()
        transit.update_mlat_beast_track(self.decoded, NOW)
        transit.raw_adsb_tracks[self.decoded.icao] = transit.RawAdsbTrackState(
            187.25, NOW, 188)
        self.assertEqual("RAW_ADSB_TC19_FRESH",
            transit.effective_track_parameter(
                self.decoded.icao, 188, NOW).source)
        transit.raw_adsb_tracks.clear()
        state.position = transit.PositionParameter(51, 21, NOW, "adsb")
        self.assertEqual("mlat", transit.effective_track_parameter(
            self.decoded.icao, 188, NOW).source)
        state.position = transit.PositionParameter(51, 21, NOW, "mlat")
        self.assertEqual("MLAT_BEAST_TC19_FRESH",
            transit.effective_track_parameter(
                self.decoded.icao, 188, NOW).source)

    def test_freshness_boundary_and_raw_held_priority(self):
        self.motion()
        transit.update_mlat_beast_track(self.decoded, NOW)
        at_boundary = NOW + datetime.timedelta(seconds=5)
        self.assertEqual("MLAT_BEAST_TC19_FRESH",
            transit.effective_track_parameter(
                self.decoded.icao, 188, at_boundary).source)

        transit.raw_adsb_tracks[self.decoded.icao] = transit.RawAdsbTrackState(
            187.25, NOW - datetime.timedelta(seconds=6), 188,
        )
        result = transit.effective_track_parameter(
            self.decoded.icao, 188, at_boundary)
        self.assertEqual("RAW_ADSB_TC19_HELD", result.source)

    def test_disabled_feature_and_decimal_display(self):
        self.motion()
        transit.update_mlat_beast_track(self.decoded, NOW)
        self.assertEqual("188.1", transit.format_track_for_display(
            self.decoded.icao, "188", NOW))
        transit.mlat_beast_enabled = False
        result = transit.effective_track_parameter(
            self.decoded.icao, 188, NOW)
        self.assertEqual((188, "mlat"), (result.value, result.source))

    def test_disabled_feature_does_not_collect_confirmation_state(self):
        self.motion()
        transit.mlat_coarse_tracks.clear()
        transit.mlat_beast_enabled = False
        transit._update_motion_parameter(
            self.decoded.icao, "track", 188, NOW, 30106)
        self.assertEqual({}, transit.mlat_coarse_tracks)


class MlatBeastReaderTests(unittest.TestCase):
    def test_valid_frame_updates_state_with_receipt_timestamp(self):
        transit.stop_event.clear()
        old = transit.mlat_beast_track_diagnostics
        transit.mlat_beast_track_diagnostics = transit.MlatBeastTrackDiagnostics()
        fake = Mock()
        fake.recv.side_effect = [wire(frame()), b""]
        try:
            with patch.object(transit.socket, "socket", return_value=fake), \
                    patch.object(transit, "update_mlat_beast_track") as update, \
                    patch.object(transit.clock, "now_utc", return_value=NOW), \
                    patch.object(transit.stop_event, "wait", return_value=True):
                transit.read_mlat_beast_track("receiver", 30105)
            decoded, received_at = update.call_args.args
            self.assertEqual(("4BAA6D", NOW), (decoded.icao, received_at))
            self.assertEqual(1,
                transit.mlat_beast_track_diagnostics.valid_track_updates)
        finally:
            transit.mlat_beast_track_diagnostics = old
            transit.stop_event.clear()

    def test_unavailable_reader_is_fail_open(self):
        transit.stop_event.clear()
        old = transit.mlat_beast_track_diagnostics
        transit.mlat_beast_track_diagnostics = transit.MlatBeastTrackDiagnostics()
        fake = Mock()
        fake.connect.side_effect = OSError("unavailable")
        try:
            with patch.object(transit.socket, "socket", return_value=fake), \
                    patch.object(transit.stop_event, "wait", return_value=True):
                transit.read_mlat_beast_track("receiver", 30105)
            self.assertEqual("unavailable",
                transit.mlat_beast_track_diagnostics.last_error)
        finally:
            transit.mlat_beast_track_diagnostics = old
            transit.stop_event.clear()


if __name__ == "__main__":
    unittest.main()
