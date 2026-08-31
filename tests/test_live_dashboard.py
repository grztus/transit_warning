import datetime
import json
import unittest
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config import ConfigurationError, load_installation_config
import live_dashboard as dashboard
import transit_warning as transit


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def candidate(icao="A00001", body="SUN", seconds=120,
              separation=1.5, update=None):
    return dashboard.DashboardCandidate(
        body=body, icao=icao, callsign="FLT" + icao[-1],
        predicted_event_utc=NOW + datetime.timedelta(seconds=seconds),
        separation_deg=separation, body_azimuth_deg=120.0,
        body_elevation_deg=20.0, aircraft_elevation_deg=21.5,
        distance_km=100.0, last_prediction_update_utc=update or NOW,
        telegram_range=separation < 2.0)


class DashboardStateTests(unittest.TestCase):
    def setUp(self):
        self.state = dashboard.DashboardState(history_limit=3)

    def test_sun_and_moon_queues_are_independent(self):
        self.state.publish(candidate("SUN001", "SUN"))
        self.state.publish(candidate("MON001", "MOON"))
        result = self.state.snapshot(NOW)
        self.assertEqual(["SUN001"], [
            item["icao"] for item in result["sun"]["candidates"]])
        self.assertEqual(["MON001"], [
            item["icao"] for item in result["moon"]["candidates"]])

    def test_multiple_candidates_sort_by_time_not_separation(self):
        self.state.publish(candidate("EARLY1", seconds=120, separation=1.8))
        self.state.publish(candidate("LATER1", seconds=135, separation=0.08))
        result = self.state.snapshot(NOW)["sun"]["candidates"]
        self.assertEqual(["EARLY1", "LATER1"], [item["icao"] for item in result])

    def test_new_earlier_candidate_reorders_queue(self):
        self.state.publish(candidate("LATER1", seconds=200))
        self.state.publish(candidate("EARLY1", seconds=100))
        result = self.state.snapshot(NOW)["sun"]["candidates"]
        self.assertEqual(["EARLY1", "LATER1"], [item["icao"] for item in result])

    def test_update_replaces_row_and_preserves_final_history_values(self):
        self.state.publish(candidate("A00001", seconds=10, separation=0.62))
        self.state.publish(candidate(
            "A00001", seconds=10, separation=0.27,
            update=NOW + datetime.timedelta(seconds=7)))
        live = self.state.snapshot(NOW)["sun"]["candidates"]
        self.assertEqual(1, len(live))
        self.state.tick(NOW + datetime.timedelta(seconds=10))
        result = self.state.snapshot(NOW + datetime.timedelta(seconds=10))
        self.assertEqual([], result["sun"]["candidates"])
        self.assertEqual(1, len(result["recent_events"]))
        history = result["recent_events"][0]
        self.assertEqual(0.27, history["final_separation_deg"])
        self.assertEqual(0.62, history["first_separation_deg"])
        self.assertEqual(0.27, history["minimum_separation_deg"])
        self.assertEqual("PASSED", history["outcome"])

    def test_repeated_updates_do_not_duplicate_history(self):
        for separation in (1.0, 0.8, 0.7):
            self.state.publish(candidate(
                "A00001", seconds=1, separation=separation))
        self.state.tick(NOW + datetime.timedelta(seconds=2))
        self.state.tick(NOW + datetime.timedelta(seconds=3))
        self.assertEqual(1, len(self.state.snapshot(NOW)["recent_events"]))

    def test_early_withdrawal_is_removed_without_history(self):
        self.state.publish(candidate("A00001", seconds=120))
        self.assertTrue(self.state.withdraw("A00001", "SUN", NOW))
        result = self.state.snapshot(NOW)
        self.assertEqual([], result["sun"]["candidates"])
        self.assertEqual([], result["recent_events"])

    def test_stale_aircraft_removes_both_body_candidates(self):
        self.state.publish(candidate("A00001", "SUN", seconds=120))
        self.state.publish(candidate("A00001", "MOON", seconds=180))
        self.assertTrue(self.state.withdraw_aircraft("A00001", NOW))
        result = self.state.snapshot(NOW)
        self.assertEqual([], result["sun"]["candidates"])
        self.assertEqual([], result["moon"]["candidates"])
        self.assertEqual([], result["recent_events"])

    def test_near_event_withdrawal_preserves_last_meaningful_state(self):
        self.state.publish(candidate("A00001", seconds=2, separation=0.4))
        self.state.withdraw("A00001", "SUN", NOW)
        history = self.state.snapshot(NOW)["recent_events"]
        self.assertEqual(1, len(history))
        self.assertEqual("WITHDRAWN", history[0]["outcome"])

    def test_telegram_triggered_event_withdrawn_early_is_preserved(self):
        self.state.publish(candidate("A00001", seconds=600, separation=1.8))
        self.assertTrue(self.state.mark_history_worthy("A00001", "SUN"))
        self.state.publish(candidate(
            "A00001", seconds=580, separation=1.2,
            update=NOW + datetime.timedelta(seconds=20)))
        self.state.publish(candidate(
            "A00001", seconds=560, separation=1.6,
            update=NOW + datetime.timedelta(seconds=40)))
        self.state.withdraw(
            "A00001", "SUN", NOW + datetime.timedelta(seconds=40))
        history = self.state.snapshot(NOW)["recent_events"]
        self.assertEqual(1, len(history))
        event = history[0]
        self.assertEqual("WITHDRAWN", event["outcome"])
        self.assertEqual(1.8, event["first_separation_deg"])
        self.assertEqual(1.2, event["minimum_separation_deg"])
        self.assertEqual(1.6, event["final_separation_deg"])
        self.assertEqual(dashboard.utc_text(NOW), event["first_seen_utc"])
        self.assertEqual(
            dashboard.utc_text(NOW + datetime.timedelta(seconds=40)),
            event["last_seen_utc"])

    def test_repeated_withdrawal_does_not_duplicate_history(self):
        self.state.publish(candidate("A00001", seconds=600))
        self.state.mark_history_worthy("A00001", "SUN")
        self.assertTrue(self.state.withdraw("A00001", "SUN", NOW))
        self.assertFalse(self.state.withdraw("A00001", "SUN", NOW))
        self.assertEqual(1, len(self.state.snapshot(NOW)["recent_events"]))

    def test_candidate_can_be_replaced_after_event_passes(self):
        self.state.publish(candidate("A00001", seconds=1))
        self.state.tick(NOW + datetime.timedelta(seconds=1))
        self.state.publish(candidate("A00001", seconds=300, separation=0.2))
        self.assertEqual(1, len(
            self.state.snapshot(NOW)["sun"]["candidates"]))

    def test_history_is_bounded_newest_first(self):
        for index in range(5):
            self.state.publish(candidate(
                "A{:05d}".format(index), seconds=index + 1))
            self.state.tick(NOW + datetime.timedelta(seconds=index + 1))
        history = self.state.snapshot(
            NOW + datetime.timedelta(seconds=10))["recent_events"]
        self.assertEqual(3, len(history))
        self.assertEqual(["A00004", "A00003", "A00002"],
                         [item["icao"] for item in history])

    def test_json_state_contains_no_private_configuration(self):
        self.state.publish(candidate())
        encoded = json.dumps(self.state.snapshot(NOW)).lower()
        for forbidden in ("observer_lat", "observer_lon", "telegram_bot",
                          "chat_id", "token"):
            self.assertNotIn(forbidden, encoded)


class DashboardRuntimeTests(unittest.TestCase):
    def test_disabled_creates_no_server(self):
        server_factory = Mock()
        runtime = dashboard.start_dashboard(
            False, "127.0.0.1", 8765, lambda: NOW,
            server_factory=server_factory)
        self.assertIsInstance(runtime, dashboard.DisabledDashboard)
        server_factory.assert_not_called()

    def test_server_start_failure_is_fail_open(self):
        errors = []

        def fail(*args):
            raise OSError("occupied")

        runtime = dashboard.start_dashboard(
            True, "127.0.0.1", 8765, lambda: NOW,
            errors.append, server_factory=fail)
        self.assertIsInstance(runtime, dashboard.DashboardRuntime)
        self.assertTrue(runtime.publish(candidate()))
        self.assertEqual(1, len(runtime.state.snapshot(
            NOW)["sun"]["candidates"]))
        self.assertEqual(["Dashboard server failed: OSError"], errors)

    def test_http_api_and_html_are_read_only_and_private(self):
        runtime = dashboard.start_dashboard(
            True, "127.0.0.1", 0, lambda: NOW)
        try:
            runtime.publish(candidate())
            port = runtime.server.server_address[1]
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/api/state".format(port),
                    timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/".format(port),
                    timeout=2) as response:
                html = response.read().decode("utf-8")
        finally:
            runtime.close()
        self.assertEqual("A00001", state["sun"]["candidates"][0]["icao"])
        self.assertIn("SUN", html)
        encoded = json.dumps(state).lower()
        self.assertNotIn("observer", encoded)
        self.assertNotIn("token", encoded)

    def test_browser_countdown_uses_absolute_utc_without_one_second_polling(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn("Date.parse(utc)-Date.now()", html)
        self.assertIn("setInterval(refresh,3000)", html)
        self.assertIn("document.querySelectorAll('[data-utc]')", html)

    def test_health_is_active_with_empty_queues_and_fresh_heartbeat(self):
        state = dashboard.DashboardState()
        state.tick(NOW)
        snapshot = state.snapshot(NOW)
        self.assertEqual([], snapshot["sun"]["candidates"])
        self.assertEqual([], snapshot["moon"]["candidates"])
        self.assertEqual(dashboard.utc_text(NOW), snapshot["generated_at_utc"])
        self.assertIn("now-t<=STALE_AFTER_MS?'ACTIVE':'STALE'",
                      dashboard.DASHBOARD_HTML)

    def test_health_detects_stale_timestamp_and_repeated_failure(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn("STALE_AFTER_MS=10000", html)
        self.assertIn("failedPolls>=DISCONNECT_AFTER_FAILURES", html)
        self.assertIn("DISCONNECT_AFTER_FAILURES=2", html)
        self.assertIn("new AbortController()", html)
        self.assertIn("controller.abort(),2500", html)

    def test_api_timestamp_is_main_loop_heartbeat_not_request_time(self):
        current = [NOW]
        runtime = dashboard.start_dashboard(
            True, "127.0.0.1", 0, lambda: current[0])
        try:
            port = runtime.server.server_address[1]

            def fetch_generated():
                with urllib.request.urlopen(
                        "http://127.0.0.1:{}/api/state".format(port),
                        timeout=2) as response:
                    return json.loads(response.read().decode("utf-8"))[
                        "generated_at_utc"]

            initial = fetch_generated()
            current[0] = NOW + datetime.timedelta(seconds=30)
            self.assertEqual(initial, fetch_generated())
            runtime.tick(current[0])
            self.assertEqual(dashboard.utc_text(current[0]), fetch_generated())
        finally:
            runtime.close()

    def test_candidate_presence_is_not_used_for_health(self):
        health_function = dashboard.DASHBOARD_HTML.split(
            "function healthStatus", 1)[1].split(
                "function renderHealth", 1)[0]
        self.assertNotIn("candidates", health_function)


class DashboardConfigTests(unittest.TestCase):
    BASE = {
        "OBSERVER_LAT": "50", "OBSERVER_LON": "20",
        "OBSERVER_ELEVATION_M": "200", "TRANSITION_ALTITUDE_FT": "6500",
        "ADSB_TIMESTAMP_TIMEZONE": "UTC", "METAR_STATION": "EPRA",
    }

    def load(self, values):
        with patch("config.dotenv_values", return_value={}):
            return load_installation_config(values)

    def test_safe_defaults_and_custom_endpoint(self):
        default = self.load(self.BASE)
        self.assertFalse(default.dashboard_enabled)
        self.assertEqual("127.0.0.1", default.dashboard_host)
        self.assertEqual(8765, default.dashboard_port)
        custom = self.load({
            **self.BASE, "DASHBOARD_ENABLED": "true",
            "DASHBOARD_HOST": "192.0.2.10", "DASHBOARD_PORT": "9876"})
        self.assertTrue(custom.dashboard_enabled)
        self.assertEqual(("192.0.2.10", 9876),
                         (custom.dashboard_host, custom.dashboard_port))

    def test_invalid_dashboard_configuration(self):
        with self.assertRaises(ConfigurationError):
            self.load({**self.BASE, "DASHBOARD_HOST": " ",
                       "DASHBOARD_PORT": "70000"})


class ProductionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.old_dashboard = transit.dashboard_runtime
        self.old_threshold = transit.telegram_alert_separation_deg
        transit.telegram_alert_separation_deg = 2.0
        transit.dashboard_runtime = dashboard.DashboardRuntime(
            dashboard.DashboardState())

    def tearDown(self):
        transit.dashboard_runtime = self.old_dashboard
        transit.telegram_alert_separation_deg = self.old_threshold

    def test_publish_reuses_existing_fifteen_degree_population(self):
        result = (51, 21, 88, 18.0, 10, 100, 120, 0, 88, 6.0, NOW)
        self.assertTrue(transit.publish_dashboard_prediction(
            "A00001", "FLT1", "moon", result, NOW, 100))
        visible = transit.dashboard_runtime.state.snapshot(
            NOW)["moon"]["candidates"]
        self.assertEqual(12.0, visible[0]["separation_deg"])
        self.assertEqual("CANDIDATE", visible[0]["state"])
        outside = list(result)
        outside[3] = 21.0
        self.assertFalse(transit.publish_dashboard_prediction(
            "A00001", "FLT1", "moon", tuple(outside), NOW, 100))
        self.assertEqual([], transit.dashboard_runtime.state.snapshot(
            NOW)["moon"]["candidates"])

    def test_dashboard_publication_failure_does_not_escape(self):
        transit.dashboard_runtime = Mock()
        transit.dashboard_runtime.publish.side_effect = RuntimeError("failure")
        result = (51, 21, 88, 6.5, 10, 100, 120, 0, 88, 6.0, NOW)
        self.assertFalse(transit.publish_dashboard_prediction(
            "A00001", "FLT1", "sun", result, NOW, 100))

    def test_telegram_trigger_signal_marks_existing_dashboard_event(self):
        result = (51, 21, 88, 6.5, 10, 100, 600, 0, 88, 6.0, NOW)
        self.assertTrue(transit.publish_dashboard_prediction(
            "A00001", "FLT1", "sun", result, NOW, 100))
        self.assertTrue(transit.mark_dashboard_history_worthy(
            "A00001", "sun"))
        transit.dashboard_runtime.withdraw("A00001", "SUN", NOW)
        history = transit.dashboard_runtime.state.snapshot(NOW)[
            "recent_events"]
        self.assertEqual("WITHDRAWN", history[0]["outcome"])

    def test_dashboard_publication_is_immediate_while_telegram_is_pending(self):
        result = (51, 21, 88, 6.5, 10, 100, 600, 0, 88, 6.0, NOW)
        notifier = Mock()
        notifier.consider.return_value = False
        old_notifier = transit.telegram_notifier
        transit.telegram_notifier = notifier
        try:
            self.assertTrue(transit.publish_dashboard_prediction(
                "A00001", "FLT1", "sun", result, NOW, 100))
            self.assertFalse(transit.emit_transit_notification(
                "A00001", "FLT1", "sun", result, NOW, 100, 0.5))
            self.assertEqual(1, len(
                transit.dashboard_runtime.state.snapshot(NOW)[
                    "sun"]["candidates"]))
        finally:
            transit.telegram_notifier = old_notifier

    def test_telegram_and_solver_behavior_are_unchanged(self):
        self.assertEqual(2.0, transit.telegram_alert_separation_deg)
        self.assertEqual(3, transit.transit_separation_sound_alert)
        old_geometry = (transit.my_lat, transit.my_lon,
                        transit.my_elevation_const)
        transit.my_lat, transit.my_lon, transit.my_elevation_const = (
            51.0, 21.0, 200.0)
        fixed_clock = SimpleNamespace(now_utc=lambda: NOW)
        try:
            with patch.object(transit, "clock", fixed_clock):
                before = transit.transit_pred(
                    (51.0, 21.0), (51.5, 21.5), 200, 800,
                    10986.668, 10, 180)
                transit.dashboard_runtime = dashboard.DisabledDashboard()
                after = transit.transit_pred(
                    (51.0, 21.0), (51.5, 21.5), 200, 800,
                    10986.668, 10, 180)
        finally:
            (transit.my_lat, transit.my_lon,
             transit.my_elevation_const) = old_geometry
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
