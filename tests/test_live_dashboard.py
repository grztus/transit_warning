import datetime
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config import ConfigurationError, load_installation_config
import live_dashboard as dashboard
from dashboard_history import DashboardHistoryStore
from app_backend.state import ApplicationStateStore
from app_backend.contracts import serialize_bootstrap
from app_backend.settings import RuntimeSettingsStore
from app_backend.sse import live_envelope
import transit_warning as transit


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def candidate(icao="A00001", body="SUN", seconds=120,
              separation=1.5, update=None, transit_distance=None):
    return dashboard.DashboardCandidate(
        body=body, icao=icao, callsign="FLT" + icao[-1],
        predicted_event_utc=NOW + datetime.timedelta(seconds=seconds),
        separation_deg=separation, body_azimuth_deg=120.0,
        body_elevation_deg=20.0, aircraft_elevation_deg=21.5,
        distance_km=100.0, last_prediction_update_utc=update or NOW,
        telegram_range=separation < 2.0,
        transit_distance_km=transit_distance)


class DashboardStateTests(unittest.TestCase):
    def test_late_sbs_callsign_updates_open_candidates_before_history(self):
        for geometry in ("LEGACY", "TRUE_2D"):
            for message_type in ("1", "5"):
                for outcome in ("PASSED", "WITHDRAWN"):
                    with self.subTest(geometry=geometry, message_type=message_type,
                                      outcome=outcome):
                        state = dashboard.DashboardState()
                        app = ApplicationStateStore()
                        runtime = dashboard.DashboardRuntime(state, application_state_store=app)
                        original = replace(candidate("ABC123", seconds=2),
                                           callsign=None, prediction_geometry=geometry)
                        runtime.publish(original)
                        runtime.publish(replace(original, body="MOON"))
                        before = state.snapshot(NOW)["sun"]["candidates"][0]
                        with patch.multiple(transit, plane_dict={}, dashboard_runtime=runtime,
                                            last_update_time=None), \
                                patch.object(transit, "current_observer_context", return_value=SimpleNamespace(
                                    position=SimpleNamespace(coordinates=(0.0, 0.0, 0.0)))), \
                                patch.object(transit, "port_timestamp_to_utc", return_value=NOW), \
                                patch.object(transit, "tabela_for_observer", return_value=(0, 0, 0, 0)), \
                                patch.object(transit, "clean_dict"), \
                                patch.object(transit, "clean_transit_dict"):
                            line = ("MSG," + message_type + ",1,1,ABC123,1,"
                                    "2026/08/31,12:00:00.000,2026/08/31,12:00:00.000, LATE123 ,")
                            transit.process_line(line, transit.adsb_port)
                            self.assertEqual("LATE123", transit.plane_dict["ABC123"][1])
                        after = state.snapshot(NOW)["sun"]["candidates"][0]
                        self.assertEqual({**before, "callsign": "LATE123"}, after)
                        self.assertEqual(None, original.callsign)
                        revision = app.snapshot()["revision"]
                        self.assertFalse(runtime.update_callsign("ABC123", "   "))
                        self.assertFalse(runtime.update_callsign("ABC123", "LATE123"))
                        self.assertEqual(revision, app.snapshot()["revision"])

                        def check_wire(history):
                            snapshot = app.snapshot()
                            bootstrap = serialize_bootstrap(snapshot,
                                RuntimeSettingsStore().snapshot(), {}, NOW)
                            for payload in (bootstrap, live_envelope(snapshot)["payload"]):
                                records = payload["recent_events"] if history else [
                                    payload["bodies"][body]["candidates"][0]
                                    for body in ("sun", "moon")]
                                self.assertEqual(["LATE123", "LATE123"],
                                                 [row["callsign"] for row in records])
                        check_wire(False)
                        if outcome == "PASSED":
                            runtime.tick(NOW + datetime.timedelta(seconds=2))
                        else:
                            runtime.withdraw_aircraft("ABC123", NOW)
                        check_wire(True)
                        self.assertFalse(runtime.update_callsign("ABC123", "LATER456"))
                        self.assertEqual(["LATE123", "LATE123"], [
                            row["callsign"] for row in state.query_history()["records"]])

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

    def test_retained_events_persist_but_insignificant_transient_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DashboardHistoryStore(Path(directory) / "history")
            state = dashboard.DashboardState(history_store=store)
            state.publish(candidate("PASSED", seconds=1))
            state.tick(NOW + datetime.timedelta(seconds=2))
            state.publish(candidate("WITHD", seconds=600))
            state.mark_history_worthy("WITHD", "SUN")
            state.withdraw("WITHD", "SUN", NOW)
            state.publish(candidate("SHORT", seconds=600))
            state.withdraw("SHORT", "SUN", NOW)
            restarted = DashboardHistoryStore(Path(directory) / "history")
            records = restarted.query(limit=10)["records"]
        self.assertEqual(["WITHD", "PASSED"],
                         [item["icao"] for item in records])
        self.assertEqual(["WITHDRAWN", "PASSED"],
                         [item["outcome"] for item in records])

    def test_default_separation_classes_come_from_backend_thresholds(self):
        for index, (separation, expected) in enumerate((
                (2.999, "GREEN"), (3.0, "YELLOW"),
                (4.999, "YELLOW"), (5.0, "RED"),
                (6.999, "RED"), (7.0, "HIDDEN"))):
            self.state.publish(candidate(
                "C{:05d}".format(index), separation=separation))
        snapshot = self.state.snapshot(NOW)
        self.assertEqual({
            "sep_green_max_deg": 3.0,
            "sep_yellow_max_deg": 5.0,
            "sep_visible_max_deg": 7.0,
        }, snapshot["presentation"])
        self.assertEqual(
            ["GREEN", "YELLOW", "YELLOW", "RED", "RED", "HIDDEN"],
            [item["separation_class"]
             for item in snapshot["sun"]["candidates"]])

    def test_custom_dashboard_thresholds_do_not_use_terminal_globals(self):
        state = dashboard.DashboardState(
            sep_green_max_deg=1, sep_yellow_max_deg=2,
            sep_visible_max_deg=3)
        state.publish(candidate(separation=1.5))
        self.assertEqual("YELLOW", state.snapshot(NOW)[
            "sun"]["candidates"][0]["separation_class"])

    def test_late_new_indicator_threshold_boundaries(self):
        for index, (seconds, expected) in enumerate((
                (120, False), (61, False), (60, True), (59, True),
                (1, True), (0, False), (-1, False))):
            with self.subTest(seconds=seconds):
                state = dashboard.DashboardState()
                state.publish(candidate(
                    "N{:05d}".format(index), seconds=seconds))
                item = state.snapshot(NOW)["sun"]["candidates"][0]
                self.assertEqual(expected, item["is_new_late_candidate"])

    def test_late_classification_is_fixed_at_first_live_appearance(self):
        event_t0 = NOW + datetime.timedelta(seconds=180)
        state = dashboard.DashboardState()
        state.publish(candidate(
            "LATE01", seconds=180,
            update=NOW + datetime.timedelta(seconds=137)))
        late = state.snapshot(
            NOW + datetime.timedelta(seconds=137))["sun"]["candidates"][0]
        self.assertTrue(late["is_new_late_candidate"])
        state.publish(candidate(
            "LATE01", seconds=180, separation=0.2,
            update=NOW + datetime.timedelta(seconds=150)))
        self.assertTrue(state.snapshot(
            NOW + datetime.timedelta(seconds=150))[
                "sun"]["candidates"][0]["is_new_late_candidate"])
        self.assertFalse(state.snapshot(event_t0)[
            "sun"]["candidates"][0]["is_new_late_candidate"])

        known = dashboard.DashboardState()
        known.publish(candidate("KNOWN1", seconds=180))
        known.publish(candidate(
            "KNOWN1", seconds=180, separation=0.1,
            update=NOW + datetime.timedelta(seconds=137)))
        self.assertFalse(known.snapshot(
            NOW + datetime.timedelta(seconds=137))[
                "sun"]["candidates"][0]["is_new_late_candidate"])

    def test_new_state_survives_api_snapshots_and_is_body_specific(self):
        self.state.publish(candidate("DUAL01", "SUN", seconds=43))
        self.state.publish(candidate("DUAL01", "MOON", seconds=120))
        first = self.state.snapshot(NOW)
        second = self.state.snapshot(NOW + datetime.timedelta(seconds=5))
        self.assertTrue(first["sun"]["candidates"][0][
            "is_new_late_candidate"])
        self.assertTrue(second["sun"]["candidates"][0][
            "is_new_late_candidate"])
        self.assertFalse(first["moon"]["candidates"][0][
            "is_new_late_candidate"])

    def test_new_indicator_can_be_disabled_or_use_custom_threshold(self):
        disabled = dashboard.DashboardState(
            new_transit_indicator_enabled=False)
        disabled.publish(candidate(seconds=10))
        self.assertFalse(disabled.snapshot(NOW)["sun"]["candidates"][0][
            "is_new_late_candidate"])
        custom = dashboard.DashboardState(new_transit_threshold_seconds=90)
        custom.publish(candidate(seconds=90))
        self.assertTrue(custom.snapshot(NOW)["sun"]["candidates"][0][
            "is_new_late_candidate"])

    def test_new_annotation_is_live_only(self):
        self.state.publish(candidate(seconds=10))
        self.state.tick(NOW + datetime.timedelta(seconds=11))
        history = self.state.snapshot(
            NOW + datetime.timedelta(seconds=11))["recent_events"][0]
        self.assertNotIn("is_new_late_candidate", history)

    def test_transit_distance_is_preserved_in_history(self):
        self.state.publish(candidate(seconds=10, transit_distance=42.4))
        live = self.state.snapshot(NOW)["sun"]["candidates"][0]
        self.assertEqual(42.4, live["transit_distance_km"])
        self.state.tick(NOW + datetime.timedelta(seconds=11))
        history = self.state.snapshot(
            NOW + datetime.timedelta(seconds=11))["recent_events"][0]
        self.assertEqual(42.4, history["transit_distance_km"])

    def test_old_history_record_without_transit_distance_is_supported(self):
        old = candidate(seconds=1)
        self.state.publish(old)
        self.state.tick(NOW + datetime.timedelta(seconds=2))
        history = self.state.snapshot(NOW)["recent_events"][0]
        self.assertIsNone(history["transit_distance_km"])


class MobileGpsStateTests(unittest.TestCase):
    VALID = {
        "latitude": 50.123,
        "longitude": 20.456,
        "accuracy": 6.0,
        "altitude": 210.5,
        "altitudeAccuracy": 10.0,
        "timestamp": 1788187200123.0,
    }

    def test_valid_update_retains_position_only_in_dedicated_state(self):
        state = dashboard.MobileGpsState(enabled=True, fresh_seconds=15)
        diagnostics = state.update(self.VALID, NOW)
        position = state.latest_position()
        self.assertEqual(self.VALID["latitude"], position.latitude)
        self.assertEqual(self.VALID["longitude"], position.longitude)
        self.assertEqual("ACTIVE", diagnostics["status"])
        self.assertEqual(6.0, diagnostics["accuracy_m"])
        self.assertTrue(diagnostics["altitude_available"])
        self.assertNotIn("latitude", diagnostics)
        self.assertNotIn("longitude", diagnostics)

    def test_missing_optional_altitude_fields_are_valid(self):
        state = dashboard.MobileGpsState(enabled=True)
        payload = dict(self.VALID)
        payload.pop("altitude")
        payload.pop("altitudeAccuracy")
        diagnostics = state.update(payload, NOW)
        self.assertFalse(diagnostics["altitude_available"])
        self.assertIsNone(diagnostics["altitude_accuracy_m"])

    def test_invalid_coordinates_nonfinite_and_malformed_values_are_rejected(self):
        cases = (
            {"latitude": 91}, {"longitude": -181},
            {"latitude": float("nan")}, {"longitude": float("inf")},
            {"accuracy": "6"}, {"accuracy": -1},
            {"timestamp": None}, {"altitudeAccuracy": -1},
        )
        for replacement in cases:
            with self.subTest(replacement=replacement):
                state = dashboard.MobileGpsState(enabled=True)
                with self.assertRaisesRegex(ValueError, "Invalid"):
                    state.update({**self.VALID, **replacement}, NOW)

    def test_freshness_uses_server_receive_time(self):
        state = dashboard.MobileGpsState(enabled=True, fresh_seconds=15)
        state.update(self.VALID, NOW)
        self.assertEqual("ACTIVE", state.diagnostics(
            NOW + datetime.timedelta(seconds=15))["status"])
        stale = state.diagnostics(NOW + datetime.timedelta(seconds=15.001))
        self.assertEqual("STALE", stale["status"])
        self.assertAlmostEqual(15.001, stale["age_seconds"])
        state.clear()
        self.assertEqual("OFF", state.diagnostics(NOW)["status"])

    def test_disabled_state_rejects_updates_and_remains_empty(self):
        state = dashboard.MobileGpsState(enabled=False)
        with self.assertRaises(PermissionError):
            state.update(self.VALID, NOW)
        self.assertIsNone(state.latest_position())
        self.assertEqual(
            {"available": False, "status": "OFF"},
            state.diagnostics(NOW))


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
                    "http://127.0.0.1:{}/legacy".format(port),
                    timeout=2) as response:
                html = response.read().decode("utf-8")
        finally:
            runtime.close()
        self.assertEqual("A00001", state["sun"]["candidates"][0]["icao"])
        self.assertIn("SUN", html)
        encoded = json.dumps(state).lower()
        self.assertNotIn("observer", encoded)
        self.assertNotIn("token", encoded)

    def test_v1_bootstrap_matches_legacy_live_bodies_and_candidates(self):
        runtime = dashboard.start_dashboard(
            True, "127.0.0.1", 0, lambda: NOW)
        try:
            runtime.update_body_position("SUN", 31.0, 217.0, NOW)
            runtime.update_body_position("MOON", 9.0, 298.0, NOW)
            runtime.publish(candidate("SUN001", "SUN"))
            runtime.publish(candidate("MON001", "MOON"))
            port = runtime.server.server_address[1]
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/api/state".format(port),
                    timeout=2) as response:
                legacy = json.loads(response.read().decode("utf-8"))
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/api/v1/bootstrap".format(port),
                    timeout=2) as response:
                bootstrap = json.loads(response.read().decode("utf-8"))
        finally:
            runtime.close()
        for body in ("sun", "moon"):
            self.assertEqual(
                legacy[body]["current_position"],
                bootstrap["bodies"][body]["current_position"])
            self.assertEqual(
                [item["icao"] for item in legacy[body]["candidates"]],
                [item["icao"]
                 for item in bootstrap["bodies"][body]["candidates"]])
        encoded = json.dumps(bootstrap).lower()
        self.assertNotIn("latitude", encoded)
        self.assertNotIn("longitude", encoded)

    def test_mobile_gps_endpoint_accepts_update_without_returning_coordinates(self):
        runtime = dashboard.start_dashboard(
            True, "127.0.0.1", 0, lambda: NOW,
            mobile_gps_enabled=True)
        payload = json.dumps(MobileGpsStateTests.VALID).encode("utf-8")
        try:
            port = runtime.server.server_address[1]
            request = urllib.request.Request(
                "http://127.0.0.1:{}/api/mobile-gps".format(port),
                data=payload, headers={"Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(request, timeout=2) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            runtime.close()
        self.assertEqual("ACTIVE", result["status"])
        self.assertNotIn("latitude", result)
        self.assertNotIn("longitude", result)

    def test_observer_control_is_private_and_reports_effective_source(self):
        from observer_position import ObserverPosition, RuntimeObserverPositionProvider
        provider = RuntimeObserverPositionProvider(
            ObserverPosition(50.0, 20.0, 200.0), mode="MOBILE",
            fallback_enabled=False)
        runtime = dashboard.start_dashboard(
            True, "127.0.0.1", 0, lambda: NOW,
            mobile_gps_enabled=True, observer_position_provider=provider)
        try:
            runtime.mobile_gps_state.update(MobileGpsStateTests.VALID, NOW)
            port = runtime.server.server_address[1]
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/api/observer".format(port),
                    timeout=2) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            runtime.close()
        self.assertEqual("MOBILE_FRESH", result["effective_source"])
        encoded = json.dumps(result).lower()
        self.assertNotIn("latitude", encoded)
        self.assertNotIn("longitude", encoded)

    def test_mobile_gps_endpoint_rejects_invalid_and_disabled_updates(self):
        for enabled, payload, expected_status in (
                (True, {**MobileGpsStateTests.VALID, "latitude": 91}, 400),
                (True, {**MobileGpsStateTests.VALID, "accuracy": "bad"}, 400),
                (False, MobileGpsStateTests.VALID, 403)):
            with self.subTest(enabled=enabled, expected=expected_status):
                runtime = dashboard.start_dashboard(
                    True, "127.0.0.1", 0, lambda: NOW,
                    mobile_gps_enabled=enabled)
                try:
                    port = runtime.server.server_address[1]
                    request = urllib.request.Request(
                        "http://127.0.0.1:{}/api/mobile-gps".format(port),
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST")
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request, timeout=2)
                    self.assertEqual(expected_status, caught.exception.code)
                finally:
                    runtime.close()

    def test_telegram_body_controls_are_runtime_only_and_independent(self):
        runtime = dashboard.start_dashboard(
            True, "127.0.0.1", 0, lambda: NOW,
            telegram_sun_enabled=False, telegram_moon_enabled=True)
        try:
            self.assertFalse(runtime.telegram_enabled("SUN"))
            self.assertTrue(runtime.telegram_enabled("MOON"))
            port = runtime.server.server_address[1]
            request = urllib.request.Request(
                "http://127.0.0.1:{}/api/telegram".format(port),
                data=json.dumps({"body": "SUN", "enabled": True}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=2) as response:
                result = json.loads(response.read().decode())
        finally:
            runtime.close()
        self.assertTrue(result["sun_enabled"])
        self.assertTrue(result["moon_enabled"])

    def test_telegram_switch_does_not_remove_live_candidate(self):
        controls = dashboard.TelegramBodyControls(False, True)
        runtime = dashboard.DashboardRuntime(
            dashboard.DashboardState(), telegram_controls=controls)
        runtime.publish(candidate("A00001", "SUN"))
        runtime.set_telegram_enabled("SUN", True)
        self.assertEqual(1, len(runtime.state.snapshot(NOW)[
            "sun"]["candidates"]))

    def test_mobile_coordinates_never_enter_normal_state_history_or_export(self):
        runtime = dashboard.start_dashboard(
            True, "127.0.0.1", 0, lambda: NOW,
            mobile_gps_enabled=True, history_enabled=False)
        private_text = "50.123456789"
        payload = {
            **MobileGpsStateTests.VALID,
            "latitude": float(private_text),
            "longitude": 20.987654321,
        }
        try:
            runtime.mobile_gps_state.update(payload, NOW)
            runtime.publish(candidate(seconds=1))
            runtime.tick(NOW + datetime.timedelta(seconds=2))
            normal_state = json.dumps(runtime.state.snapshot(NOW))
            history = json.dumps(runtime.state.query_history())
            export = runtime.state.export_history_csv().decode("utf-8")
        finally:
            runtime.close()
        for output in (normal_state, history, export):
            self.assertNotIn(private_text, output)
            self.assertNotIn("latitude", output.lower())
            self.assertNotIn("longitude", output.lower())

    def test_browser_countdown_uses_absolute_utc_without_one_second_polling(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn("Date.parse(utc)-Date.now()", html)
        self.assertIn("setInterval(refresh,3000)", html)
        self.assertIn("document.querySelectorAll('[data-utc]')", html)

    def test_legacy_telegram_controls_refresh_external_changes(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn(
            "setInterval(()=>telegramRequest().catch(()=>{}),3000)", html)
        self.assertIn(
            "async function telegramRequest(body=null,enabled=null)", html)

    def test_legacy_history_sends_body_and_max_sep_filters(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn('id="history-body"', html)
        self.assertIn('id="history-max-sep"', html)
        self.assertIn("q.set('max_sep_deg',m)", html)
        self.assertIn("value.trim().replace(',','.')", html)
        self.assertIn("'history-body','history-max-sep'", html)

    def test_legacy_operational_active_states_are_green(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn(
            "button.active{background:#173b31;color:#eef;border:1px solid #55d982}",
            html)
        self.assertIn(
            ".observer-controls input[type=checkbox]{accent-color:#55d982}",
            html)

    def test_candidate_countdown_and_identity_share_compact_mobile_row(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn(".candidate-heading{display:flex", html)
        self.assertIn("gap:.65rem", html)
        self.assertIn('class="candidate-heading"><div class="countdown"', html)
        self.assertIn("<h2>${esc(c.callsign||c.icao)}</h2>", html)
        self.assertIn("overflow-wrap:anywhere", html)

    def test_live_geometry_row_uses_predicted_transit_distance(self):
        html = dashboard.DASHBOARD_HTML
        live_renderer = html.split("function renderBody", 1)[1].split(
            "function renderHistory", 1)[0]
        self.assertIn('class="transit-geometry">AZ ', live_renderer)
        self.assertIn("ALT ${c.body_elevation_deg.toFixed(1)}° · ",
                      live_renderer)
        self.assertIn("c.transit_distance_km?.toFixed(0)", live_renderer)
        self.assertNotIn("Aircraft ALT", live_renderer)
        self.assertNotIn("c.distance_km?.toFixed(0)", live_renderer)

    def test_body_headers_render_integer_alt_before_azimuth(self):
        state = dashboard.DashboardState()
        state.update_body_position("SUN", 31.4, 216.6, NOW)
        state.update_body_position("MOON", 8.6, 297.6, NOW)
        snapshot = state.snapshot(NOW)
        self.assertEqual(31.4,
                         snapshot["sun"]["current_position"]["altitude_deg"])
        html = dashboard.DASHBOARD_HTML
        self.assertIn("ALT ${Math.round(p.altitude_deg)}° · AZ ${Math.round(p.azimuth_deg)}°", html)
        self.assertLess(html.index("ALT ${Math.round(p.altitude_deg)}"),
                        html.index("AZ ${Math.round(p.azimuth_deg)}"))

    def test_history_renders_t0_slant_range_and_handles_legacy_record(self):
        renderer = dashboard.DASHBOARD_HTML.split(
            "function renderHistory", 1)[1].split(
                "function historyQuery", 1)[0]
        self.assertIn("x.transit_distance_km", renderer)
        self.assertIn("+' km'", renderer)
        self.assertIn("x.transit_distance_km!=null", renderer)
        self.assertIn("Number.isFinite", renderer)

    def test_moon_has_monochrome_identity_distinct_from_sun(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn('class="body-icon">☀️</span> SUN', html)
        self.assertIn('class="body-icon moon-icon">☾</span> MOON', html)
        self.assertIn(".moon-icon{color:#e6edf7", html)

    def test_late_new_badge_is_live_only_and_respects_reduced_motion(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn("c.is_new_late_candidate?", html)
        self.assertIn('class="new-badge">NEW</span>', html)
        self.assertIn("@keyframes new-pulse", html)
        self.assertIn("@media(prefers-reduced-motion:reduce)", html)
        history_renderer = html.split("function renderHistory", 1)[1].split(
            "function historyQuery", 1)[0]
        self.assertNotIn("new-badge", history_renderer)

    def test_mobile_gps_ui_requires_explicit_high_accuracy_watch(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn('class="observer-panel"', html)
        self.assertIn("Start GPS", html)
        self.assertIn("Stop GPS", html)
        self.assertIn("navigator.geolocation.watchPosition", html)
        self.assertIn("enableHighAccuracy:true", html)
        self.assertIn("gpsEnabled=false", html)
        self.assertNotIn("localStorage", html)

    def test_observer_ui_is_one_compact_extensible_control(self):
        html = dashboard.DASHBOARD_HTML
        self.assertEqual(1, html.count('class="observer-panel"'))
        self.assertIn('class="observer-modes"', html)
        self.assertIn('>OBSERVER</strong>', html)
        self.assertIn('> fallback</label>', html)
        self.assertIn('title="Fallback to STATIC when mobile GPS becomes stale"',
                      html)
        self.assertNotIn("static fallback", html)
        self.assertIn("padding:6px 8px", html)

    def test_observer_status_uses_compact_order_and_labels(self):
        html = dashboard.DASHBOARD_HTML
        javascript = html.split("<script>", 1)[1]
        self.assertIn("MOBILE_LAST_KNOWN:'MOBILE LAST KNOWN'", javascript)
        self.assertIn("STATIC_FALLBACK:'STATIC FALLBACK'", javascript)
        self.assertIn("MOBILE_NO_FIX:'NO POSITION'", javascript)
        self.assertIn("parts=[`<span>${esc(requested)}</span>`", javascript)
        self.assertIn('`<span class="arrow">→</span>`', javascript)
        self.assertIn("GPS: ${esc(gpsLabel)}", javascript)
        self.assertIn("AGE: ${formatAge", javascript)
        self.assertIn("if(requested==='MOBILE')", javascript)
        self.assertIn("value<60?`${value} s`", javascript)

    def test_gps_controls_are_contextual_and_compact(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn('id="gps-start" class="gps-secondary hidden"', html)
        self.assertIn('id="gps-stop" class="gps-secondary hidden"', html)
        self.assertIn("requested!=='MOBILE'||gpsEnabled||!gpsAvailable",
                      html)
        self.assertIn("classList.toggle('hidden',!gpsEnabled)", html)

    def test_mobile_sep_uses_backend_class_and_time_is_seconds_only(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn("sepClass(c.separation_class)", html)
        self.assertIn("eventTime(c.predicted_event_utc)", html)
        self.assertIn("slice(11,19)+' UTC'", html)
        javascript = html.split("<script>", 1)[1]
        self.assertNotIn("separation_deg < 3", javascript)
        self.assertNotIn("separation_deg < 5", javascript)
        self.assertNotIn("separation_deg < 7", javascript)

    def test_api_keeps_full_timestamp_and_history_is_separate_bounded_endpoint(self):
        precise = NOW.replace(microsecond=697668)
        with tempfile.TemporaryDirectory() as directory:
            runtime = dashboard.start_dashboard(
                True, "127.0.0.1", 0, lambda: NOW,
                history_dir=directory)
            try:
                runtime.publish(candidate(update=precise))
                port = runtime.server.server_address[1]
                with urllib.request.urlopen(
                        "http://127.0.0.1:{}/api/state".format(port),
                        timeout=2) as response:
                    state = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(
                        "http://127.0.0.1:{}/api/history?limit=25".format(port),
                        timeout=2) as response:
                    history = json.loads(response.read().decode("utf-8"))
            finally:
                runtime.close()
        self.assertEqual(
            "2026-08-31T12:00:00.697668Z",
            state["sun"]["candidates"][0]["last_prediction_update_utc"])
        self.assertLessEqual(history["limit"], 100)
        self.assertEqual([], history["records"])

    def test_history_http_filters_paginates_and_exports_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = dashboard.start_dashboard(
                True, "127.0.0.1", 0, lambda: NOW,
                history_dir=directory)
            try:
                for index in range(3):
                    runtime.publish(candidate(
                        "H{:05d}".format(index),
                        body="MOON" if index == 1 else "SUN",
                        seconds=index + 1))
                    runtime.tick(NOW + datetime.timedelta(seconds=index + 1))
                port = runtime.server.server_address[1]
                url = ("http://127.0.0.1:{}/api/history?body=SUN&limit=1"
                       .format(port))
                with urllib.request.urlopen(url, timeout=2) as response:
                    page = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(
                        "http://127.0.0.1:{}/api/history/export.csv?body=MOON"
                        .format(port), timeout=2) as response:
                    exported = response.read().decode("utf-8-sig")
                    disposition = response.headers["Content-Disposition"]
            finally:
                runtime.close()
        self.assertEqual(1, len(page["records"]))
        self.assertTrue(page["has_more"])
        self.assertIn("H00001", exported)
        self.assertNotIn("H00000", exported)
        self.assertIn("attachment", disposition)

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


class ProductionFrontendServingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dist = self.root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text(
            "<!doctype html><title>production-react</title>", encoding="utf-8")
        (self.dist / "assets" / "example.js").write_text(
            "globalThis.productionReact = true;", encoding="utf-8")
        self.runtime = dashboard.start_dashboard(
            True, "127.0.0.1", 0, lambda: NOW,
            history_enabled=False, frontend_dist_dir=self.dist)
        self.base = "http://127.0.0.1:{}".format(
            self.runtime.server.server_address[1])

    def tearDown(self):
        self.runtime.close()
        self.temporary.cleanup()

    def fetch(self, path):
        with urllib.request.urlopen(self.base + path, timeout=2) as response:
            return (response.status, response.headers,
                    response.read().decode("utf-8"))

    def test_root_and_index_serve_production_index(self):
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                status, headers, body = self.fetch(path)
                self.assertEqual(200, status)
                self.assertIn("text/html", headers["Content-Type"])
                self.assertIn("production-react", body)

    def test_vite_asset_is_served_with_javascript_mime_type(self):
        status, headers, body = self.fetch("/assets/example.js")
        self.assertEqual(200, status)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertIn("productionReact", body)

    def test_legacy_always_serves_embedded_dashboard(self):
        status, headers, body = self.fetch("/legacy")
        self.assertEqual(200, status)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(dashboard.DASHBOARD_HTML, body)

    def test_root_falls_back_when_production_index_is_absent(self):
        (self.dist / "index.html").unlink()
        _, _, body = self.fetch("/")
        self.assertEqual(dashboard.DASHBOARD_HTML, body)

    def test_api_routes_take_precedence_when_dist_exists(self):
        status, headers, body = self.fetch("/api/state")
        self.assertEqual(200, status)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIn("sun", json.loads(body))

    def test_asset_path_traversal_is_rejected(self):
        (self.root / "secret.js").write_text("secret", encoding="utf-8")
        request = urllib.request.Request(
            self.base + "/assets/%2e%2e/secret.js")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(404, raised.exception.code)


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
        self.assertEqual((3.0, 5.0, 7.0), (
            default.dashboard_sep_green_max_deg,
            default.dashboard_sep_yellow_max_deg,
            default.dashboard_sep_visible_max_deg))
        self.assertTrue(default.dashboard_history_enabled)
        self.assertEqual("recordings/dashboard_history",
                         default.dashboard_history_dir)
        self.assertFalse(default.dashboard_mobile_gps_enabled)
        self.assertEqual(15.0, default.dashboard_mobile_gps_fresh_seconds)
        self.assertTrue(default.new_transit_indicator_enabled)
        self.assertEqual(60.0, default.new_transit_threshold_seconds)

    def test_new_indicator_configuration_is_optional_and_validated(self):
        configured = self.load({
            **self.BASE,
            "NEW_TRANSIT_INDICATOR_ENABLED": "false",
            "NEW_TRANSIT_THRESHOLD_SECONDS": "90",
        })
        self.assertFalse(configured.new_transit_indicator_enabled)
        self.assertEqual(90.0, configured.new_transit_threshold_seconds)
        for invalid in ("-1", "901", "nan", "bad"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    ConfigurationError, "NEW_TRANSIT_THRESHOLD_SECONDS"):
                self.load({
                    **self.BASE,
                    "NEW_TRANSIT_THRESHOLD_SECONDS": invalid,
                })

    def test_mobile_gps_configuration_is_optional_and_validated(self):
        configured = self.load({
            **self.BASE,
            "DASHBOARD_MOBILE_GPS_ENABLED": "true",
            "DASHBOARD_MOBILE_GPS_FRESH_SECONDS": "30",
        })
        self.assertTrue(configured.dashboard_mobile_gps_enabled)
        self.assertEqual(30.0, configured.dashboard_mobile_gps_fresh_seconds)
        for invalid in ("0", "-1", "3601", "nan", "bad"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                        ConfigurationError,
                        "DASHBOARD_MOBILE_GPS_FRESH_SECONDS"):
                    self.load({
                        **self.BASE,
                        "DASHBOARD_MOBILE_GPS_FRESH_SECONDS": invalid,
                    })

    def test_invalid_dashboard_configuration(self):
        with self.assertRaises(ConfigurationError):
            self.load({**self.BASE, "DASHBOARD_HOST": " ",
                       "DASHBOARD_PORT": "70000"})
        with self.assertRaisesRegex(ConfigurationError,
                                   "DASHBOARD_HISTORY_DIR"):
            self.load({**self.BASE, "DASHBOARD_HISTORY_DIR": " "})


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

    def test_publish_uses_dashboard_seven_degree_visibility(self):
        result = (51, 21, 88, 12.0, 10, 100, 120, 0, 88, 6.0, NOW)
        self.assertTrue(transit.publish_dashboard_prediction(
            "A00001", "FLT1", "moon", result, NOW, 100))
        visible = transit.dashboard_runtime.state.snapshot(
            NOW)["moon"]["candidates"]
        self.assertEqual(6.0, visible[0]["separation_deg"])
        self.assertEqual("CANDIDATE", visible[0]["state"])
        outside = list(result)
        outside[3] = 13.0
        self.assertFalse(transit.publish_dashboard_prediction(
            "A00001", "FLT1", "moon", tuple(outside), NOW, 100))
        self.assertEqual([], transit.dashboard_runtime.state.snapshot(
            NOW)["moon"]["candidates"])

    def test_dashboard_visibility_ignores_terminal_thresholds(self):
        result = (51, 21, 88, 11.0, 10, 100, 120, 0, 88, 6.0, NOW)
        with patch.object(transit, "dashboard_sep_visible_max_deg", 6.0), \
                patch.object(transit, "tmux_sep_visible_max_deg", 4.0):
            self.assertTrue(transit.publish_dashboard_prediction(
                "A00001", "FLT1", "moon", result, NOW, 100))

    def test_dashboard_publication_failure_does_not_escape(self):
        transit.dashboard_runtime = Mock()
        transit.dashboard_runtime.publish.side_effect = RuntimeError("failure")
        result = (51, 21, 88, 6.5, 10, 100, 120, 0, 88, 6.0, NOW)
        self.assertFalse(transit.publish_dashboard_prediction(
            "A00001", "FLT1", "sun", result, NOW, 100))

    def test_dashboard_uses_t0_slant_range_not_current_aircraft_range(self):
        result = (51.2, 21.3, 88, 6.5, 42.0, 100, 120,
                  0, 88, 6.0, NOW)
        observer = SimpleNamespace(
            coordinates=(51.0, 21.0), elevation_m=200.0)
        diagnostic = SimpleNamespace(
            prediction=SimpleNamespace(predicted_altitude_m=7000.0))
        with patch.dict(
                transit.vertical_transit_diagnostics,
                {("A00001", "sun"): diagnostic}, clear=False), \
                patch.object(
                    transit, "aircraft_slant_distance_from_observer",
                    return_value=42.4) as geometry:
            self.assertTrue(transit.publish_dashboard_prediction(
                "A00001", "FLT1", "sun", result, NOW, 999.0,
                observer_position=observer))
        item = transit.dashboard_runtime.state.snapshot(NOW)[
            "sun"]["candidates"][0]
        self.assertEqual(999.0, item["distance_km"])
        self.assertEqual(42.4, item["transit_distance_km"])
        geometry.assert_called_once_with(
            observer.coordinates, observer.elevation_m,
            (result[0], result[1]), 7000.0,
            horizontal_distance_km=result[4])

    def test_current_body_positions_use_static_and_mobile_observer_contexts(self):
        for effective in ("STATIC", "MOBILE_FRESH", "MOBILE_LAST_KNOWN"):
            with self.subTest(effective=effective):
                position = SimpleNamespace(marker=effective)
                context = SimpleNamespace(position=position)
                observed = []

                def ephemeris(body, when, observer):
                    observed.append((body, observer))
                    return SimpleNamespace(
                        altitude_deg=31.4 if body == "sun" else 8.6,
                        azimuth_deg=216.6 if body == "sun" else 297.6)

                with patch.object(
                        transit, "current_observer_context",
                        return_value=context), patch.object(
                            transit, "body_position_at_utc",
                            side_effect=ephemeris):
                    self.assertTrue(
                        transit.update_dashboard_body_positions(NOW))
                self.assertEqual([position, position],
                                 [item[1] for item in observed])
                state = transit.dashboard_runtime.state.snapshot(NOW)
                self.assertEqual(31.4,
                                 state["sun"]["current_position"]["altitude_deg"])
                self.assertNotIn("latitude", json.dumps(state).lower())

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
        self.assertEqual(0.5, transit.TRANSIT_SNAPSHOT_SEP_THRESHOLD_DEG)
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

    def test_mobile_gps_diagnostics_do_not_change_prediction_result(self):
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
                mobile = dashboard.MobileGpsState(enabled=True)
                mobile.update(MobileGpsStateTests.VALID, NOW)
                after = transit.transit_pred(
                    (51.0, 21.0), (51.5, 21.5), 200, 800,
                    10986.668, 10, 180)
        finally:
            (transit.my_lat, transit.my_lon,
             transit.my_elevation_const) = old_geometry
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
