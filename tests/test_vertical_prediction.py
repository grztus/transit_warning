import datetime
import math
import unittest
from collections import deque

import pytz

import transit_warning as transit
from config import InstallationConfig
from transit_clock import ReplayClock


UTC_NOW = datetime.datetime(2026, 8, 19, 12, 0, tzinfo=pytz.utc)
TEST_CONFIG = InstallationConfig(
    observer_lat=51.0,
    observer_lon=21.0,
    observer_elevation_m=200.0,
    transition_altitude_ft=6500,
    adsb_host="adsb.example",
    adsb_port=31003,
    adsb_timestamp_timezone="Europe/Warsaw",
    mlat_host="mlat.example",
    mlat_port=31106,
    metar_station="EPRA",
)


def motion_state(values, age=0.0, altitude_m=10000.0,
                 altitude_age=0.0, sources=None):
    sources = sources or ["adsb"] * len(values)
    samples = []
    for index, (value, source) in enumerate(zip(values, sources)):
        seconds_ago = age + len(values) - index - 1
        samples.append(transit.MotionParameter(
            float(value),
            UTC_NOW - datetime.timedelta(seconds=seconds_ago),
            source,
        ))
    return transit.AircraftMotionState(
        altitude=transit.MotionParameter(
            altitude_m,
            UTC_NOW - datetime.timedelta(seconds=altitude_age),
            "adsb",
        ),
        vertical_rate=samples[-1] if samples else None,
        vertical_rate_history=deque(
            samples, maxlen=transit.VERTICAL_RATE_HISTORY_MAXLEN),
    )


def predict(values, age=0.0, time2x=500.0, **kwargs):
    return transit.predict_transit_altitude(
        kwargs.get("altitude_m", 10000.0),
        motion_state(values, age=age, **kwargs),
        UTC_NOW,
        time2x,
    )


class VerticalPredictionPolicyTests(unittest.TestCase):
    def test_small_rates_are_always_level(self):
        for value in (0, 64, -64, 128, -128, 256, -256, 299, -299):
            with self.subTest(value=value):
                result = predict([value, value, value])
                self.assertEqual(result.mode, transit.VerticalPredictionMode.LEVEL)
                self.assertEqual(result.predicted_altitude_m, 10000.0)
                self.assertEqual(result.applied_seconds, 0.0)

    def test_small_rate_history_does_not_confirm_dynamic_motion(self):
        result = predict([64, 128, 192])
        self.assertEqual(result.mode, transit.VerticalPredictionMode.LEVEL)

    def test_confirmed_climb_and_descent_use_last_rate(self):
        climb = predict([512, 576, 640], time2x=60)
        descent = predict([-1024, -1088, -1152], time2x=60)
        self.assertEqual(
            climb.mode, transit.VerticalPredictionMode.DYNAMIC_VALID)
        self.assertEqual(
            descent.mode, transit.VerticalPredictionMode.DYNAMIC_VALID)
        self.assertAlmostEqual(climb.altitude_delta_m, 640 * 0.3048)
        self.assertAlmostEqual(descent.altitude_delta_m, -1152 * 0.3048)

    def test_stability_rejections_keep_current_altitude(self):
        cases = (
            ([256, 512, 576], "history_below_dynamic_threshold"),
            ([512, 576, 960], "vertical_rate_spread"),
            ([-512, -576, 640], "recent_sign_reversal"),
            ([0, 512, 576], "history_below_dynamic_threshold"),
            ([64, 128, 1408], "history_below_dynamic_threshold"),
        )
        for values, reason in cases:
            with self.subTest(values=values):
                result = predict(values)
                self.assertEqual(
                    result.mode, transit.VerticalPredictionMode.VR_DEGRADED)
                self.assertEqual(result.predicted_altitude_m, 10000.0)
                self.assertEqual(result.reason, reason)

    def test_three_new_samples_reconfirm_after_sign_reversal(self):
        result = predict([-832, 512, 576, 640])
        self.assertEqual(
            result.mode, transit.VerticalPredictionMode.DYNAMIC_VALID)

    def test_age_boundaries(self):
        self.assertEqual(
            predict([512] * 3, age=2.000).mode,
            transit.VerticalPredictionMode.DYNAMIC_VALID)
        self.assertEqual(
            predict([512] * 3, age=2.001).mode,
            transit.VerticalPredictionMode.VR_DEGRADED)
        self.assertEqual(
            predict([512] * 3, age=5.000).mode,
            transit.VerticalPredictionMode.VR_DEGRADED)
        self.assertEqual(
            predict([512] * 3, age=5.001).mode,
            transit.VerticalPredictionMode.VR_IGNORE)

    def test_small_generated_logged_skew_is_clamped_like_motion_freshness(self):
        result = predict([512] * 3, age=-0.050)
        self.assertEqual(
            result.mode, transit.VerticalPredictionMode.DYNAMIC_VALID)
        self.assertEqual(result.vertical_rate_age_seconds, 0.0)

    def test_dynamic_threshold_and_spread_boundaries(self):
        self.assertEqual(
            predict([300, 300, 300]).mode,
            transit.VerticalPredictionMode.DYNAMIC_VALID)
        self.assertEqual(
            predict([512, 640, 768]).mode,
            transit.VerticalPredictionMode.DYNAMIC_VALID)
        self.assertEqual(predict([512, 640, 768]).spread_fpm, 256)
        self.assertEqual(
            predict([512, 640, 769]).mode,
            transit.VerticalPredictionMode.VR_DEGRADED)

    def test_time_cap_boundaries(self):
        for horizon, expected in (
                (119.999, 119.999), (120.000, 120.000), (500, 120.000)):
            with self.subTest(horizon=horizon):
                result = predict([512] * 3, time2x=horizon)
                self.assertAlmostEqual(result.applied_seconds, expected)
                self.assertAlmostEqual(
                    result.altitude_delta_m,
                    512 * expected / 60 * 0.3048,
                )

    def test_missing_or_stale_rate_is_ignored(self):
        missing = predict([])
        stale = predict([512] * 3, age=6)
        self.assertEqual(missing.mode, transit.VerticalPredictionMode.VR_IGNORE)
        self.assertEqual(stale.mode, transit.VerticalPredictionMode.VR_IGNORE)

    def test_stale_altitude_prevents_correction(self):
        result = predict([512] * 3, altitude_age=10.001)
        self.assertEqual(result.mode, transit.VerticalPredictionMode.VR_IGNORE)
        self.assertEqual(result.reason, "altitude_stale")

    def test_mixed_sources_do_not_block_stable_values(self):
        result = predict(
            [512, 576, 640], sources=["adsb", "mlat", "adsb"])
        self.assertEqual(
            result.mode, transit.VerticalPredictionMode.DYNAMIC_VALID)
        self.assertEqual(result.source, "adsb")

    def test_lot3pw_reference_uses_120_second_cap(self):
        result = predict(
            [-960, -960, -1024],
            age=0,
            time2x=135.918,
            altitude_m=11243.6,
        )
        self.assertEqual(
            result.mode, transit.VerticalPredictionMode.DYNAMIC_VALID)
        self.assertAlmostEqual(result.predicted_altitude_m, 10619.37, places=1)

    def test_fin6hl_plus_64_is_level(self):
        result = predict([0, 64, 64], time2x=11.221, altitude_m=11243.9)
        self.assertEqual(result.mode, transit.VerticalPredictionMode.LEVEL)
        self.assertEqual(result.predicted_altitude_m, 11243.9)

    def test_bti26f_single_spike_is_degraded(self):
        result = predict([-53, -3, 404], time2x=868.235, altitude_m=9753.4)
        self.assertEqual(
            result.mode, transit.VerticalPredictionMode.VR_DEGRADED)
        self.assertEqual(result.predicted_altitude_m, 9753.4)


class VerticalTransitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.original_motion_states = transit.aircraft_motion_states
        self.original_diagnostics = transit.vertical_transit_diagnostics
        self.original_elevation = transit.my_elevation_const
        transit.aircraft_motion_states = {}
        transit.vertical_transit_diagnostics = {}
        transit.my_elevation_const = 200.0

    def tearDown(self):
        transit.aircraft_motion_states = self.original_motion_states
        transit.vertical_transit_diagnostics = self.original_diagnostics
        transit.my_elevation_const = self.original_elevation

    @staticmethod
    def result(time2x=500.0):
        return (51.0, 21.0, 120.0, 30.0, 100.0, 50.0,
                time2x, 0, 120.0, 32.0, UTC_NOW)

    def test_only_final_vertical_angle_changes(self):
        transit.aircraft_motion_states["ABC123"] = motion_state(
            [512, 576, 640])
        original = self.result()
        updated = transit.apply_vertical_prediction_to_transit_result(
            "ABC123", "sun", original, 10000.0, UTC_NOW)
        self.assertNotEqual(updated[3], original[3])
        for index in (0, 1, 2, 4, 5, 6, 7, 8, 9, 10):
            self.assertEqual(updated[index], original[index])
        diagnostic = transit.get_vertical_transit_diagnostic(
            "ABC123", "sun")
        self.assertEqual(
            diagnostic.prediction.mode,
            transit.VerticalPredictionMode.DYNAMIC_VALID)
        self.assertEqual(diagnostic.separation_before, 2.0)
        self.assertEqual(
            diagnostic.separation_after,
            transit.vertical_transit_separation(updated[3], updated[9]))

    def test_level_mode_preserves_solver_result_and_sep(self):
        transit.aircraft_motion_states["ABC123"] = motion_state([64] * 3)
        original = self.result()
        updated = transit.apply_vertical_prediction_to_transit_result(
            "ABC123", "moon", original, 10000.0, UTC_NOW)
        self.assertEqual(updated, original)
        diagnostic = transit.get_vertical_transit_diagnostic(
            "ABC123", "moon")
        self.assertEqual(
            diagnostic.prediction.mode, transit.VerticalPredictionMode.LEVEL)
        self.assertEqual(
            diagnostic.separation_before, diagnostic.separation_after)

    def test_clearing_prediction_removes_vertical_diagnostic(self):
        transit.aircraft_motion_states["ABC123"] = motion_state([512] * 3)
        entry = [""] * 32
        entry[18:23] = [32.0, 30.0, 100.0, 50.0, 120.0]
        transit.apply_vertical_prediction_to_transit_result(
            "ABC123", "sun", self.result(), 10000.0, UTC_NOW)
        self.assertIsNotNone(transit.get_vertical_transit_diagnostic(
            "ABC123", "sun"))
        transit.clear_transit_prediction_state(
            "ABC123", entry, "sun", 18)
        self.assertIsNone(transit.get_vertical_transit_diagnostic(
            "ABC123", "sun"))


class VerticalRateHistoryMessageTests(unittest.TestCase):
    def setUp(self):
        self.originals = {
            name: getattr(transit, name) for name in (
                "clock", "plane_dict", "altitude_sources",
                "aircraft_motion_states", "aircraft_motion_freshness_status",
                "pressure", "tabela", "moving_body_transit_pred")
        }
        transit.clock = ReplayClock()
        transit.apply_installation_config(TEST_CONFIG)
        transit.replay_time_initialized = False
        transit.plane_dict = {}
        transit.altitude_sources = {}
        transit.aircraft_motion_states = {}
        transit.aircraft_motion_freshness_status = {}
        transit.pressure = 1013.25
        transit.tabela = lambda: (30.0, 120.0, 20.0, 90.0)
        transit.moving_body_transit_pred = lambda *args, **kwargs: 0

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(transit, name, value)

    @staticmethod
    def line(prefix, subtype, second, vertical_rate=""):
        timestamp = "2026/08/19 12:00:{:02d}.000".format(second)
        date, value_time = timestamp.split()
        return ",".join([
            prefix, str(subtype), "1", "1", "ABC123", "1",
            date, value_time, date, value_time, "", "10000", "450",
            "180", "51.2", "21.2", str(vertical_rate),
        ])

    def test_real_messages_append_only_actual_vertical_rates_with_source(self):
        transit.process_line(self.line("MSG", 4, 0, 512), 31003)
        transit.process_line(self.line("MSG", 1, 1, ""), 31003)
        transit.process_line(self.line("MLAT", 3, 2, 576), 31106)
        history = transit.aircraft_motion_states[
            "ABC123"].vertical_rate_history
        self.assertEqual([sample.value for sample in history], [512, 576])
        self.assertEqual(
            [sample.source for sample in history], ["adsb", "mlat"])

    def test_history_is_bounded(self):
        for second in range(15):
            transit.process_line(
                self.line("MLAT", 3, second, 512 + second), 31106)
        history = transit.aircraft_motion_states[
            "ABC123"].vertical_rate_history
        self.assertEqual(len(history), transit.VERTICAL_RATE_HISTORY_MAXLEN)
        self.assertEqual(history[0].value, 517)
        self.assertEqual(history[-1].value, 526)

    def test_clean_dict_removes_state_and_its_history(self):
        transit.process_line(self.line("MLAT", 3, 0, 512), 31106)
        state = transit.aircraft_motion_states["ABC123"]
        transit.clock.advance_to(UTC_NOW + datetime.timedelta(seconds=61))
        transit.clean_dict()
        self.assertNotIn("ABC123", transit.aircraft_motion_states)
        self.assertEqual([sample.value for sample in state.vertical_rate_history],
                         [512])


if __name__ == "__main__":
    unittest.main()
