import datetime
import unittest

from transit_prediction_model import (
    IntentParameter,
    MotionParameter,
    VerticalIntentState,
    VerticalMotionState,
    VerticalPredictionMode,
    current_vertical_prediction_policy,
    predict_vertical_state_at_time,
)


UTC = datetime.timezone.utc
BASE = datetime.datetime(2026, 8, 23, 16, 0, tzinfo=UTC)


def parameter(value, age=0.0, source="ADSB"):
    return MotionParameter(
        value=float(value),
        updated_at_utc=BASE - datetime.timedelta(seconds=age),
        source=source,
    )


def motion(vertical_rate=960.0, vr_age=0.0, history=None,
           altitude_age=0.0):
    if history is None:
        history = (900.0, 960.0, 1024.0)
    return VerticalMotionState(
        altitude=parameter(3000.0, altitude_age),
        vertical_rate=(None if vertical_rate is None
                       else parameter(vertical_rate, vr_age)),
        vertical_rate_history=tuple(parameter(value) for value in history),
    )


def intent(selected=12000.0, selected_age=0.0, source="MCP/FCU",
           qnh=1013.25, qnh_age=0.0):
    return VerticalIntentState(
        selected_altitude=IntentParameter(
            float(selected), BASE - datetime.timedelta(seconds=selected_age),
            source),
        nav_qnh=(None if qnh is None else IntentParameter(
            float(qnh), BASE - datetime.timedelta(seconds=qnh_age), "FMS")),
    )


def predict(state=None, intent_state=None, dt=60.0, qnh=1013.25):
    return predict_vertical_state_at_time(
        3000.0, motion() if state is None else state, intent_state,
        BASE, dt, qnh, current_vertical_prediction_policy())


class VerticalPredictionModelTests(unittest.TestCase):
    def test_modes_and_reasons(self):
        cases = (
            (motion(100.0, history=(100, 100, 100)),
             VerticalPredictionMode.LEVEL, "below_dynamic_threshold"),
            (motion(None), VerticalPredictionMode.VR_IGNORE,
             "vertical_rate_missing"),
            (motion(960, vr_age=5.001), VerticalPredictionMode.VR_IGNORE,
             "vertical_rate_stale"),
            (motion(960, vr_age=2.001), VerticalPredictionMode.VR_DEGRADED,
             "vertical_rate_degraded_age"),
            (motion(960, history=(900, 960)),
             VerticalPredictionMode.VR_DEGRADED, "insufficient_history"),
            (motion(960, history=(-900, 960, 1024)),
             VerticalPredictionMode.VR_DEGRADED, "recent_sign_reversal"),
            (motion(1200, history=(800, 1000, 1200)),
             VerticalPredictionMode.VR_DEGRADED, "vertical_rate_spread"),
        )
        for state, expected_mode, expected_reason in cases:
            with self.subTest(expected_reason):
                result = predict(state).prediction
                self.assertEqual(expected_mode, result.mode)
                self.assertEqual(expected_reason, result.reason)
                self.assertEqual(0.0, result.altitude_delta_m)

    def test_dynamic_climb_and_descent(self):
        climb = predict(motion(960, history=(900, 960, 1024)), dt=60).prediction
        descent = predict(
            motion(-960, history=(-900, -960, -1024)), dt=60).prediction
        self.assertEqual(VerticalPredictionMode.DYNAMIC_VALID, climb.mode)
        self.assertEqual(VerticalPredictionMode.DYNAMIC_VALID, descent.mode)
        self.assertAlmostEqual(292.608, climb.altitude_delta_m, places=12)
        self.assertAlmostEqual(-292.608, descent.altitude_delta_m, places=12)

    def test_prediction_horizon_boundaries(self):
        expected = ((-1, 0), (0, 0), (30, 30), (120, 120), (121, 120))
        for dt, applied in expected:
            with self.subTest(dt=dt):
                result = predict(dt=dt).prediction
                self.assertEqual(float(applied), result.applied_seconds)
                self.assertAlmostEqual(
                    960.0 * applied / 60.0 * 0.3048,
                    result.altitude_delta_m, places=12)

    def test_freshness_uses_prediction_base_not_horizon(self):
        state = motion(960, vr_age=1.5)
        short = predict(state, dt=1).prediction
        long = predict(state, dt=900).prediction
        self.assertEqual(VerticalPredictionMode.DYNAMIC_VALID, short.mode)
        self.assertEqual(short.mode, long.mode)
        self.assertEqual(short.vertical_rate_age_seconds,
                         long.vertical_rate_age_seconds)

    def test_tc29_climb_and_descent_clamp(self):
        climb = predict(intent_state=intent(selected=10500), dt=120)
        descent = predict(
            motion(-960, history=(-900, -960, -1024)),
            intent(selected=9000), dt=120)
        self.assertTrue(climb.intent_details["intent_clamped"])
        self.assertTrue(descent.intent_details["intent_clamped"])
        self.assertEqual(10500 * 0.3048,
                         climb.prediction.predicted_altitude_m)
        self.assertEqual(9000 * 0.3048,
                         descent.prediction.predicted_altitude_m)
        self.assertEqual("TC29_CLAMP_APPLIED",
                         climb.intent_details["intent_reason"])

    def test_tc29_rejection_and_no_clamp_matrix(self):
        cases = (
            (intent(source="FMS"), "TC29_SOURCE_UNSUPPORTED"),
            (intent(selected_age=10.001), "TC29_STALE"),
            (intent(qnh=None), "TC29_NO_QNH"),
            (intent(qnh_age=10.001), "TC29_QNH_STALE"),
            (intent(selected=9000), "TC29_DIRECTION_MISMATCH"),
            (intent(selected=20000), "TC29_NOT_NEEDED"),
        )
        for intent_state, reason in cases:
            with self.subTest(reason):
                result = predict(intent_state=intent_state, dt=30)
                self.assertFalse(result.intent_details["intent_clamped"])
                self.assertEqual(reason, result.intent_details["intent_reason"])
                self.assertEqual(
                    result.prediction_before_clamp.predicted_altitude_m,
                    result.prediction.predicted_altitude_m)

    def test_qnh_adjusts_tc29_target_exactly(self):
        result = predict(intent_state=intent(selected=10500, qnh=1000),
                         dt=120, qnh=1010)
        expected_target = (10500 + (1010 - 1000) * 26) * 0.3048
        self.assertEqual(expected_target,
                         result.intent_details["target_altitude_m"])
        self.assertEqual(expected_target, result.prediction.predicted_altitude_m)

    def test_json_shaped_frozen_state_reconstructs_canonical_result(self):
        frozen = {
            "prediction_base_utc": BASE.isoformat(),
            "current_altitude_m": 3000.0,
            "altitude": {"value": 3000.0, "age": 0.2, "source": "ADSB"},
            "vertical_rate": {"value": 960.0, "age": 0.1,
                              "source": "ADSB"},
            "vertical_rate_history": [900.0, 960.0, 1024.0],
            "selected_altitude": {"value": 10500.0, "age": 0.3,
                                  "source": "MCP/FCU"},
            "nav_qnh": {"value": 1013.25, "age": 0.4, "source": "FMS"},
            "application_qnh_hpa": 1013.25,
            "time2x_seconds": 120.0,
        }
        base = datetime.datetime.fromisoformat(frozen["prediction_base_utc"])
        altitude = frozen["altitude"]
        vr = frozen["vertical_rate"]
        model_motion = VerticalMotionState(
            altitude=MotionParameter(
                altitude["value"], base - datetime.timedelta(
                    seconds=altitude["age"]), altitude["source"]),
            vertical_rate=MotionParameter(
                vr["value"], base - datetime.timedelta(seconds=vr["age"]),
                vr["source"]),
            vertical_rate_history=tuple(MotionParameter(
                value, base, "ADSB")
                for value in frozen["vertical_rate_history"]),
        )
        selected = frozen["selected_altitude"]
        nav_qnh = frozen["nav_qnh"]
        model_intent = VerticalIntentState(
            selected_altitude=IntentParameter(
                selected["value"], base - datetime.timedelta(
                    seconds=selected["age"]), selected["source"]),
            nav_qnh=IntentParameter(
                nav_qnh["value"], base - datetime.timedelta(
                    seconds=nav_qnh["age"]), nav_qnh["source"]),
        )
        result = predict_vertical_state_at_time(
            frozen["current_altitude_m"], model_motion, model_intent, base,
            frozen["time2x_seconds"], frozen["application_qnh_hpa"],
            current_vertical_prediction_policy())
        self.assertEqual(VerticalPredictionMode.DYNAMIC_VALID,
                         result.prediction.mode)
        self.assertEqual("confirmed_vertical_trend",
                         result.prediction.reason)
        self.assertEqual(120.0, result.prediction.applied_seconds)
        self.assertEqual(10500 * 0.3048,
                         result.prediction.predicted_altitude_m)
        self.assertTrue(result.intent_details["intent_clamped"])


if __name__ == "__main__":
    unittest.main()
