import datetime
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import transit_warning as transit
from authoritative_transit import (
    AuthoritativeTransitPrediction,
    AuthoritativeTransition,
    AuthoritativeTransitionKind,
)
from live_dashboard import DashboardCandidate, DashboardState
from observer_position import ObserverContext, ObserverPosition
from shadow_2d_prediction import FrozenExactVerticalState
from telegram_notifications import TelegramNotifier, TransitNotification
from transit_prediction_model import (
    MotionParameter,
    VerticalMotionState,
    VerticalPredictionMode,
    VerticalPredictionResult,
    current_vertical_prediction_policy,
)


UTC = datetime.timezone.utc
BASE = datetime.datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def frozen_vertical(tca=60.0):
    altitude = MotionParameter(9000.0, BASE, "adsb")
    rate = MotionParameter(600.0, BASE, "adsb")
    motion = VerticalMotionState(
        altitude=altitude, vertical_rate=rate,
        vertical_rate_history=(rate, rate, rate))
    prediction = VerticalPredictionResult(
        predicted_altitude_m=9182.88,
        mode=VerticalPredictionMode.DYNAMIC_VALID,
        reason="VR_VALID", last_vertical_rate_fpm=600.0,
        vertical_rate_age_seconds=0.0,
        stability_samples=(rate, rate, rate), spread_fpm=0.0,
        source="adsb", applied_seconds=tca,
        current_altitude_m=9000.0, altitude_delta_m=182.88,
        altitude_age_seconds=0.0)
    return FrozenExactVerticalState(
        prediction_base_utc=BASE, tca_seconds=tca,
        motion_state=motion, intent_state=None,
        policy=current_vertical_prediction_policy(),
        application_qnh_hpa=1013.25,
        prediction_before_clamp=prediction, prediction=prediction,
        intent_details=MappingProxyType({
            "intent_clamped": False, "intent_reason": "TC29_NO_DATA"}),
        altitude_source="OWN_GNSS_GEOMETRIC",
        geometric_altitude_correction_m=20.0,
        final_altitude_m=9202.88)


def prediction(seconds=60.0, separation=0.4, encounter_id="7:ABC123:SUN:1"):
    return AuthoritativeTransitPrediction(
        observer_epoch=7, observer_source="STATIC", icao="ABC123",
        callsign="TEST1", body="SUN", encounter_generation=1,
        encounter_id=encounter_id,
        predicted_transit_utc=BASE + datetime.timedelta(seconds=seconds),
        separation_deg=separation, body_radius_deg=0.25,
        aircraft_azimuth_deg=120.1, aircraft_altitude_deg=10.2,
        body_azimuth_deg=120.0, body_altitude_deg=10.1,
        aircraft_altitude_m=9202.88,
        aircraft_latitude_deg=50.1, aircraft_longitude_deg=20.2,
        frozen_vertical_state=frozen_vertical(seconds),
        slant_range_km=43.21, model="TRUE_2D",
        boundary_status="INTERIOR", lifecycle_state="ACTIVE",
        updated_at_utc=BASE)


def context(requested="STATIC"):
    return SimpleNamespace(
        icao="ABC123", callsign="TEST1", body="sun",
        prediction_base_utc=BASE, latitude_deg=50.0,
        longitude_deg=20.0, track_deg=180.5, groundspeed_kmh=700.0,
        observer_context=ObserverContext(
            ObserverPosition(10.0, 20.0, 100.0), requested,
            "MOBILE" if requested == "MOBILE" else "STATIC", epoch=7))


def dashboard_candidate(seconds=60.0):
    item = prediction(seconds)
    return DashboardCandidate(
        body=item.body, icao=item.icao, callsign=item.callsign,
        predicted_event_utc=item.predicted_transit_utc,
        separation_deg=item.separation_deg,
        body_azimuth_deg=item.body_azimuth_deg,
        body_elevation_deg=item.body_altitude_deg,
        aircraft_elevation_deg=item.aircraft_altitude_deg,
        distance_km=20.0, last_prediction_update_utc=BASE,
        telegram_range=True, transit_distance_km=item.slant_range_km,
        encounter_id=item.encounter_id,
        prediction_geometry=item.model)


class AuthoritativeConsumerTests(unittest.TestCase):
    def setUp(self):
        self.old_dashboard = transit.dashboard_runtime
        self.old_notifier = transit.telegram_notifier
        self.old_snapshots = transit.transit_snapshot_manager
        self.old_terminal = dict(transit.authoritative_terminal_predictions)

    def tearDown(self):
        transit.dashboard_runtime = self.old_dashboard
        transit.telegram_notifier = self.old_notifier
        transit.transit_snapshot_manager = self.old_snapshots
        transit.authoritative_terminal_predictions.clear()
        transit.authoritative_terminal_predictions.update(self.old_terminal)

    def test_true_2d_values_and_slant_range_reach_dashboard(self):
        runtime = Mock()
        transit.dashboard_runtime = runtime
        item = prediction(seconds=75.0, separation=0.33)
        self.assertTrue(transit.publish_authoritative_dashboard_prediction(
            item, BASE, 12.0))
        candidate = runtime.publish.call_args.args[0]
        self.assertEqual(item.predicted_transit_utc,
                         candidate.predicted_event_utc)
        self.assertEqual(0.33, candidate.separation_deg)
        self.assertEqual(43.21, candidate.transit_distance_km)
        self.assertEqual(item.encounter_id, candidate.encounter_id)
        self.assertEqual("TRUE_2D", candidate.prediction_geometry)

    def test_opened_routes_all_fresh_consumers_once(self):
        transit.dashboard_runtime = Mock()
        transit.dashboard_runtime.telegram_enabled.return_value = True
        transit.telegram_notifier = Mock()
        transit.telegram_notifier.consider.return_value = False
        transit.transit_snapshot_manager = Mock()
        entry = [""] * 32
        item = prediction()
        transition = AuthoritativeTransition(
            AuthoritativeTransitionKind.OPENED, item)
        with patch.object(transit, "gong") as gong:
            self.assertTrue(transit.consume_authoritative_transition(
                transition, context(), entry, 20.0, BASE))
            gong.assert_called_once()
        transit.dashboard_runtime.publish.assert_called_once()
        transit.telegram_notifier.consider.assert_called_once()
        transit.transit_snapshot_manager.consider_prediction.assert_called_once()
        self.assertEqual(item.predicted_transit_utc,
                         transit.sun_predicted_transit_utc["ABC123"])
        transit.sun_prediction_last_valid.pop("ABC123", None)
        transit.sun_predicted_transit_utc.pop("ABC123", None)

    def test_t0_drift_keeps_one_dashboard_history_identity_and_new_state(self):
        state = DashboardState(new_transit_threshold_seconds=120)
        state.publish(dashboard_candidate(60.0))
        state.publish(dashboard_candidate(65.0))
        live = state.snapshot(BASE)["sun"]["candidates"]
        self.assertEqual(1, len(live))
        self.assertTrue(live[0]["is_new_late_candidate"])
        state.withdraw("ABC123", "SUN", BASE + datetime.timedelta(seconds=70))
        history = state.snapshot(
            BASE + datetime.timedelta(seconds=70))["recent_events"]
        self.assertEqual(1, len(history))
        self.assertEqual("7:ABC123:SUN:1", history[0]["event_id"])

    def test_held_and_none_do_not_invoke_fresh_consumers(self):
        transit.dashboard_runtime = Mock()
        transit.telegram_notifier = Mock()
        transit.transit_snapshot_manager = Mock()
        entry = [""] * 32
        for kind in (AuthoritativeTransitionKind.HELD,
                     AuthoritativeTransitionKind.NONE):
            transition = AuthoritativeTransition(
                kind, prediction() if kind == AuthoritativeTransitionKind.HELD
                else None)
            with patch.object(transit, "gong") as gong:
                transit.consume_authoritative_transition(
                    transition, context(), entry, 20.0, BASE)
                gong.assert_not_called()
        transit.dashboard_runtime.publish.assert_not_called()
        transit.telegram_notifier.consider.assert_not_called()
        transit.transit_snapshot_manager.consider_prediction.assert_not_called()

    def test_withdrawal_clears_all_consumer_state_after_grace(self):
        transit.dashboard_runtime = Mock()
        transit.telegram_notifier = Mock()
        entry = [""] * 32
        item = prediction()
        transit.authoritative_terminal_predictions[("ABC123", "SUN")] = item
        transition = AuthoritativeTransition(
            AuthoritativeTransitionKind.WITHDRAWN, item)
        transit.consume_authoritative_transition(
            transition, context(), entry, 20.0, BASE)
        self.assertNotIn(("ABC123", "SUN"),
                         transit.authoritative_terminal_predictions)
        transit.dashboard_runtime.withdraw.assert_called_once()
        transit.telegram_notifier.cancel.assert_called_once()

    def test_snapshot_uses_exact_frozen_state_and_hides_mobile_coordinates(self):
        manager = Mock()
        transit.transit_snapshot_manager = manager
        item = prediction()
        poison = Mock(side_effect=AssertionError("legacy lookup"))
        with patch.object(transit, "build_frozen_prediction_state", poison):
            transit.capture_authoritative_transit_prediction(
                item, context("MOBILE"), BASE)
        payload = manager.consider_prediction.call_args.args[0]
        self.assertEqual("TRUE_2D", payload["prediction_geometry"])
        self.assertEqual(item.encounter_id, payload["encounter_id"])
        self.assertEqual(item.aircraft_latitude_deg,
                         payload["closest_point"]["aircraft_lat"])
        self.assertEqual(item.aircraft_altitude_m,
                         payload["frozen_prediction_state"]["vertical"]
                         ["decision"]["final_geometric_altitude_m"])
        self.assertNotIn("lat", payload["observer"])
        self.assertNotIn("lon", payload["observer"])

    def test_terminal_uses_true_sep_and_does_not_invent_legacy_distances(self):
        manager = transit.authoritative_transit_lifecycle
        transit.authoritative_transit_lifecycle = SimpleNamespace(enabled=True)
        try:
            transit.authoritative_terminal_predictions[("ABC123", "SUN")] = (
                prediction())
            values = transit.visible_transit_candidate(
                [""] * 32, "sun", "ABC123", BASE)
            self.assertEqual((0.4, None, None, 60), values)
            rendered = transit.format_terminal_transit_candidate(values)
            self.assertEqual(2, rendered.count("---"))
        finally:
            transit.authoritative_transit_lifecycle = manager


class AuthoritativeTelegramIdentityTests(unittest.TestCase):
    def test_t0_drift_does_not_restart_stabilization(self):
        transport = Mock()
        transport.send.return_value = (True, None)
        notifier = TelegramNotifier(transport, stability_seconds=5.0)
        try:
            def event(created, predicted):
                return TransitNotification(
                    created_at_utc=created, body="SUN", icao="ABC123",
                    callsign="TEST1", predicted_transit_utc=predicted,
                    time_to_event_seconds=(predicted-created).total_seconds(),
                    separation_deg=0.4, body_azimuth_deg=120.0,
                    body_altitude_deg=10.0, aircraft_altitude_deg=10.1,
                    distance_km=40.0, encounter_id="7:ABC123:SUN:1",
                    prediction_geometry="TRUE_2D")
            self.assertFalse(notifier.consider(event(
                BASE, BASE + datetime.timedelta(seconds=60))))
            self.assertTrue(notifier.consider(event(
                BASE + datetime.timedelta(seconds=5),
                BASE + datetime.timedelta(seconds=70))))
        finally:
            notifier.close()


if __name__ == "__main__":
    unittest.main()
