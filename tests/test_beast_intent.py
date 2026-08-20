import datetime
import io
import unittest
from unittest.mock import Mock, patch

from beast_intent import (BeastFrameParser, decode_tc29, modes_crc)


def tc29_message(icao=0xABC123, altitude_ft=12000, qnh=1009.6, source=0,
                 df=17, type_code=29):
    me = 0
    def put(value, start, end):
        nonlocal me
        me |= value << (56 - end)
    put(type_code, 0, 5)
    put(1, 5, 7)
    put(source, 8, 9)
    put(int(altitude_ft / 32) + 1, 9, 20)
    put(0 if qnh is None else round((qnh - 800) / 0.8) + 1, 20, 29)
    prefix = (df << 83) | (icao << 56) | me
    raw = (prefix << 24).to_bytes(14, "big")
    return (int.from_bytes(raw, "big") | modes_crc(raw)).to_bytes(14, "big")


def wire(message, timestamp=123, signal=42):
    payload = timestamp.to_bytes(6, "big") + bytes((signal,)) + message
    return b"\x1a\x33" + payload.replace(b"\x1a", b"\x1a\x1a")


class BeastIntentTests(unittest.TestCase):
    def test_streaming_parser_handles_chunks_and_escaped_bytes(self):
        message = tc29_message(icao=0x1A0123)
        data = wire(message)
        parser = BeastFrameParser()
        frames = []
        for byte in data:
            frames.extend(parser.feed(bytes((byte,))))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].modes, message)
        self.assertEqual(frames[0].beast_timestamp, 123)

    def test_decodes_valid_mcp_tc29(self):
        frame = BeastFrameParser().feed(wire(tc29_message()))[0]
        intent = decode_tc29(frame)
        self.assertEqual(intent.icao, "ABC123")
        self.assertEqual(intent.selected_altitude_ft, 12000.0)
        self.assertAlmostEqual(intent.nav_qnh_hpa, 1009.6)
        self.assertEqual(intent.selected_altitude_source, "MCP/FCU")

    def test_rejects_bad_crc_and_non_tc29(self):
        message = bytearray(tc29_message())
        message[-1] ^= 1
        frame = BeastFrameParser().feed(wire(bytes(message)))[0]
        self.assertIsNone(decode_tc29(frame))
        for message in (tc29_message(type_code=28), tc29_message(df=18)):
            frame = BeastFrameParser().feed(wire(message))[0]
            self.assertIsNone(decode_tc29(frame))

    def test_selected_altitude_survives_missing_nav_qnh(self):
        frame = BeastFrameParser().feed(wire(tc29_message(qnh=None)))[0]
        intent = decode_tc29(frame)
        self.assertEqual(intent.selected_altitude_ft, 12000.0)
        self.assertIsNone(intent.nav_qnh_hpa)

    def test_type2_frame_is_decoded_but_is_not_tc29(self):
        payload = b"\x00" * 6 + b"\x01" + b"\x00" * 7
        frames = BeastFrameParser().feed(b"\x1a\x32" + payload)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].frame_type, 0x32)
        self.assertIsNone(decode_tc29(frames[0]))

    def test_malformed_frame_resynchronizes_at_next_marker(self):
        valid = wire(tc29_message())
        frames = BeastFrameParser().feed(b"\x1a\x33broken\x1a" + valid[1:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(decode_tc29(frames[0]).icao, "ABC123")


class IntentClampTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import transit_warning as transit
        cls.transit = transit

    def setUp(self):
        self.transit.aircraft_intent_states = {}
        self.now = datetime.datetime(2026, 8, 20, 10, tzinfo=datetime.timezone.utc)

    def prediction(self, predicted=4000, rate=1200):
        t = self.transit
        return t.VerticalPredictionResult(
            predicted, t.VerticalPredictionMode.DYNAMIC_VALID,
            "confirmed_vertical_trend", rate, 0, (), 0, "adsb", 120,
            3000, predicted - 3000, 0)

    def set_intent(self, altitude=11000, qnh=1009, age=0, source="MCP/FCU"):
        t = self.transit
        stamp = self.now - datetime.timedelta(seconds=age)
        state = t.AircraftIntentState(
            selected_altitude=t.IntentParameter(altitude, stamp, source),
            nav_qnh=t.IntentParameter(qnh, stamp, "ADS-B TC29"))
        t.aircraft_intent_states["ABC123"] = state

    def test_climb_is_clamped_at_qnh_adjusted_target(self):
        self.set_intent()
        result, details = self.transit.clamp_vertical_prediction_to_selected_altitude(
            "ABC123", self.prediction(), self.now, 1007)
        target = (11000 + (1007 - 1009) * 26) * 0.3048
        self.assertAlmostEqual(result.predicted_altitude_m, target)
        self.assertTrue(details["intent_clamped"])

    def test_freshness_boundary_is_inclusive(self):
        self.set_intent(age=10.0)
        _, details = self.transit.clamp_vertical_prediction_to_selected_altitude(
            "ABC123", self.prediction(), self.now, 1007)
        self.assertTrue(details["intent_clamped"])
        self.set_intent(age=10.001)
        result, details = self.transit.clamp_vertical_prediction_to_selected_altitude(
            "ABC123", self.prediction(), self.now, 1007)
        self.assertEqual(result.predicted_altitude_m, 4000)
        self.assertEqual(details["intent_reason"], "TC29_STALE")

    def test_nav_qnh_freshness_is_independent_and_inclusive(self):
        self.set_intent(age=0)
        state = self.transit.aircraft_intent_states["ABC123"]
        state.nav_qnh = self.transit.IntentParameter(
            1009, self.now - datetime.timedelta(seconds=10), "ADS-B TC29")
        _, details = self.transit.clamp_vertical_prediction_to_selected_altitude(
            "ABC123", self.prediction(), self.now, 1007)
        self.assertTrue(details["intent_clamped"])
        state.nav_qnh = self.transit.IntentParameter(
            1009, self.now - datetime.timedelta(seconds=10.001), "ADS-B TC29")
        result, details = self.transit.clamp_vertical_prediction_to_selected_altitude(
            "ABC123", self.prediction(), self.now, 1007)
        self.assertEqual(result.predicted_altitude_m, 4000)
        self.assertEqual(details["intent_reason"], "TC29_QNH_STALE")

    def test_level_and_direction_mismatch_are_unchanged(self):
        self.set_intent(altitude=9000)
        result, details = self.transit.clamp_vertical_prediction_to_selected_altitude(
            "ABC123", self.prediction(), self.now, 1007)
        self.assertEqual(result.predicted_altitude_m, 4000)
        self.assertEqual(details["intent_reason"], "TC29_DIRECTION_MISMATCH")

    def test_descent_clamps_with_max_and_missing_intent_is_noop(self):
        result, details = self.transit.clamp_vertical_prediction_to_selected_altitude(
            "ABC123", self.prediction(predicted=2000, rate=-1200), self.now, 1007)
        self.assertEqual(result.predicted_altitude_m, 2000)
        self.assertEqual(details["intent_reason"], "TC29_NO_DATA")
        self.set_intent(altitude=9000)
        result, details = self.transit.clamp_vertical_prediction_to_selected_altitude(
            "ABC123", self.prediction(predicted=2000, rate=-1200), self.now, 1007)
        self.assertGreater(result.predicted_altitude_m, 2000)
        self.assertTrue(details["intent_clamped"])

    def test_fms_is_stored_diagnostically_but_not_used(self):
        from beast_intent import Tc29Intent
        self.transit.update_aircraft_intent(
            Tc29Intent("ABC123", 12000, "FMS", 1010), self.now)
        self.assertEqual(
            self.transit.aircraft_intent_states["ABC123"].selected_altitude.source,
            "FMS")
        result, details = self.transit.clamp_vertical_prediction_to_selected_altitude(
            "ABC123", self.prediction(), self.now, 1007)
        self.assertEqual(result.predicted_altitude_m, 4000)
        self.assertEqual(details["intent_reason"], "TC29_SOURCE_UNSUPPORTED")

    def test_selected_altitude_and_qnh_keep_independent_timestamps(self):
        from beast_intent import Tc29Intent
        t = self.transit
        t.update_aircraft_intent(
            Tc29Intent("ABC123", 12000, "MCP/FCU", 1010), self.now)
        later = self.now + datetime.timedelta(seconds=2)
        t.update_aircraft_intent(
            Tc29Intent("ABC123", 13000, "MCP/FCU", None), later)
        state = t.aircraft_intent_states["ABC123"]
        self.assertEqual(state.selected_altitude.updated_at_utc, later)
        self.assertEqual(state.nav_qnh.updated_at_utc, self.now)
        self.assertEqual([x.selected_altitude_ft for x in state.selected_altitude_history],
                         [12000, 13000])

    def test_level_prediction_does_not_start_climb_from_selected_altitude(self):
        self.set_intent(altitude=39000)
        p = self.prediction()
        level = self.transit.VerticalPredictionResult(
            p.current_altitude_m, self.transit.VerticalPredictionMode.LEVEL,
            "below_dynamic_threshold", 0, 0, (), 0, "adsb", 0,
            p.current_altitude_m, 0, 0)
        result, details = self.transit.clamp_vertical_prediction_to_selected_altitude(
            "ABC123", level, self.now, 1007)
        self.assertIs(result, level)
        self.assertEqual(details["intent_reason"], "TC29_2E_NOT_DYNAMIC")

    def test_target_change_is_used_immediately_and_history_is_bounded(self):
        from beast_intent import Tc29Intent
        t = self.transit
        for index in range(12):
            t.update_aircraft_intent(Tc29Intent(
                "ABC123", 12000 + index * 32, "MCP/FCU", 1009),
                self.now + datetime.timedelta(seconds=index))
        state = t.aircraft_intent_states["ABC123"]
        self.assertEqual(len(state.selected_altitude_history), 10)
        self.assertEqual(state.selected_altitude.value, 12352)


class BeastTerminalOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import transit_warning as transit
        cls.transit = transit

    def test_connection_error_is_diagnostic_only_and_keeps_retry_delay(self):
        transit = self.transit
        now = datetime.datetime(
            2026, 8, 20, 12, 0, tzinfo=datetime.timezone.utc)
        failed_socket = Mock()
        failed_socket.connect.side_effect = OSError("connection refused")
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        diagnostics = transit.BeastIntentDiagnostics()
        output = io.StringIO()

        with patch.object(transit.socket, "socket", return_value=failed_socket), \
                patch.object(transit, "stop_event", stop), \
                patch.object(transit, "beast_intent_diagnostics", diagnostics), \
                patch.object(transit.clock, "now_utc", return_value=now), \
                patch.object(transit.sys, "stdout", output), \
                patch("builtins.print") as print_mock:
            transit.read_beast_intent("receiver", 30005)

        self.assertEqual(output.getvalue(), "")
        print_mock.assert_not_called()
        self.assertEqual(diagnostics.last_error, "connection refused")
        self.assertEqual(diagnostics.last_error_utc, now)
        stop.wait.assert_called_once_with(5)
        failed_socket.close.assert_called()

    def test_gong_emits_only_bel_without_newline(self):
        transit = self.transit
        now = datetime.datetime(
            2026, 8, 20, 12, 0, 3, tzinfo=datetime.timezone.utc)
        previous_gong_t = transit.gong_t
        output = io.StringIO()
        try:
            transit.gong_t = now - datetime.timedelta(seconds=3)
            with patch.object(transit.clock, "now_utc", return_value=now), \
                    patch.object(transit.sys, "stdout", output):
                transit.gong()
        finally:
            transit.gong_t = previous_gong_t

        self.assertEqual(output.getvalue(), "\a")
