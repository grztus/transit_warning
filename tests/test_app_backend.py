import datetime
import json
from pathlib import Path
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock

from app_backend.contracts import (
    serialize_bootstrap,
    serialize_live_state,
    serialize_observer_status,
)
from app_backend.settings import (
    RuntimeSettingsStore,
    SettingsConflictError,
    SettingsValidationError,
)
from app_backend.state import ApplicationStateStore
from app_backend.sse import encode_sse, live_envelope
from dashboard_history import DashboardHistoryStore
import live_dashboard as dashboard
from observer_position import ObserverPosition, RuntimeObserverPositionProvider


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def patch_json(url, value):
    request = urllib.request.Request(
        url, data=json.dumps(value).encode("utf-8"), method="PATCH",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=2) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


class ApplicationContractTests(unittest.TestCase):
    def test_runtime_identity_survives_live_history_bootstrap_and_sse(self):
        for geometry in ("LEGACY", "TRUE_2D"):
            for callsign in ("TEST123", None, "", "   "):
                for outcome in ("PASSED", "WITHDRAWN"):
                    with self.subTest(geometry=geometry, callsign=callsign,
                                      outcome=outcome), tempfile.TemporaryDirectory() as directory:
                        history = DashboardHistoryStore(directory)
                        state = dashboard.DashboardState(history_store=history)
                        app = ApplicationStateStore()
                        runtime = dashboard.DashboardRuntime(
                            state, application_state_store=app)
                        runtime.publish(dashboard.DashboardCandidate(
                            body="SUN", icao="ABC123", callsign=callsign,
                            predicted_event_utc=NOW + datetime.timedelta(seconds=2),
                            separation_deg=0.5, body_azimuth_deg=120.0,
                            body_elevation_deg=20.0, aircraft_elevation_deg=20.5,
                            distance_km=100.0, last_prediction_update_utc=NOW,
                            telegram_range=True, prediction_geometry=geometry))

                        def check_identity(is_history):
                            snapshot = app.snapshot()
                            bootstrap = serialize_bootstrap(
                                snapshot, RuntimeSettingsStore().snapshot(), {}, NOW)
                            wire = encode_sse(live_envelope(snapshot))
                            sse = json.loads(wire.split("data: ", 1)[1])["payload"]
                            for payload in (bootstrap, sse):
                                records = (payload["recent_events"] if is_history else
                                           payload["bodies"]["sun"]["candidates"])
                                self.assertEqual(1, len(records))
                                self.assertEqual(callsign, records[0]["callsign"])
                                self.assertEqual("ABC123", records[0]["icao"])
                            self.assertEqual(bootstrap["bodies"], sse["bodies"])
                            self.assertEqual(bootstrap["recent_events"], sse["recent_events"])

                        check_identity(False)
                        if outcome == "PASSED":
                            runtime.tick(NOW + datetime.timedelta(seconds=2))
                        else:
                            runtime.withdraw("ABC123", "SUN", NOW)
                        check_identity(True)
                        record = history.query()["records"][0]
                        self.assertEqual(callsign, record["callsign"])
                        self.assertEqual(outcome, record["outcome"])

    def test_live_contract_is_explicit_detached_and_privacy_safe(self):
        source = {
            "generated_at_utc": "2026-09-04T12:00:00Z",
            "sun": {
                "current_position": {
                    "altitude_deg": 10.0, "azimuth_deg": 20.0,
                    "evaluated_at_utc": "2026-09-04T12:00:00Z",
                    "latitude": 51.0, "lat": 51.0, "lon": 20.0,
                },
                "candidates": [{
                    "icao": "ABC123", "body": "SUN", "callsign": "TEST",
                    "separation_deg": 1.0, "private_manifest_path": "secret",
                }],
            },
            "moon": {"current_position": None, "candidates": []},
            "recent_events": [{
                "event_id": "event-1", "body": "SUN", "icao": "ABC123",
                "observer_longitude": 20.0, "filesystem_path": "secret",
            }],
            "presentation": {"sep_visible_max_deg": 7.0, "token": "secret"},
        }
        result = serialize_live_state(source)
        source["sun"]["candidates"][0]["icao"] = "MUTATED"
        encoded = json.dumps(result).lower()
        self.assertEqual("ABC123", result["bodies"]["sun"]["candidates"][0]["icao"])
        for forbidden in ("latitude", "longitude", "manifest", "path", "token"):
            self.assertNotIn(forbidden, encoded)
        self.assertNotIn('"lat"', encoded)
        self.assertNotIn('"lon"', encoded)

    def test_observer_contract_excludes_coordinates(self):
        result = serialize_observer_status({
            "requested_mode": "MOBILE", "effective_source": "MOBILE_FRESH",
            "gps_health": "ACTIVE", "mobile_age_seconds": 2.0,
            "mobile_accuracy_m": 6.0, "latitude": 51.0, "longitude": 20.0,
        })
        self.assertEqual("MOBILE", result["requested_mode"])
        self.assertNotIn("latitude", result)
        self.assertNotIn("longitude", result)

    def test_observer_contract_exposes_manual_values_only_in_manual_mode(self):
        fields = {"manual_lat_deg": 51.25, "manual_lon_deg": 21.5,
                  "manual_elevation_amsl_m": 315.0}
        mobile = serialize_observer_status({
            "requested_mode": "MOBILE", **fields})
        manual = serialize_observer_status({
            "requested_mode": "MANUAL", "effective_source": "MANUAL",
            **fields})
        self.assertNotIn("manual_lat_deg", mobile)
        self.assertEqual(fields["manual_lat_deg"], manual["manual_lat_deg"])

    def test_bootstrap_contract_has_versions_and_no_private_state(self):
        app = ApplicationStateStore()
        app.publish({
            "generated_at_utc": "2026-09-04T12:00:00Z",
            "sun": {
                "current_position": {
                    "altitude_deg": 31.0, "azimuth_deg": 217.0,
                    "evaluated_at_utc": "2026-09-04T12:00:00Z",
                },
                "candidates": [{
                    "icao": "ABC123", "callsign": "SUN123",
                    "body": "SUN", "separation_deg": 0.4,
                    "observer_latitude": 51.0,
                }],
            },
            "moon": {
                "current_position": {
                    "altitude_deg": 9.0, "azimuth_deg": 298.0,
                    "evaluated_at_utc": "2026-09-04T12:00:00Z",
                },
                "candidates": [{
                    "icao": "DEF456", "callsign": "MOON456",
                    "body": "MOON", "separation_deg": 0.8,
                    "private_manifest_path": "secret",
                }],
            },
            "recent_events": [], "presentation": {},
        })
        settings = RuntimeSettingsStore().snapshot()
        result = serialize_bootstrap(app.snapshot(), settings, {
            "requested_mode": "STATIC", "effective_source": "STATIC",
            "latitude": 51.0,
        }, NOW)
        self.assertEqual(1, result["schema_version"])
        self.assertEqual(1, result["live_revision"])
        self.assertEqual(0, result["settings_revision"])
        self.assertEqual("ACTIVE", result["health"])
        self.assertEqual(
            31.0, result["bodies"]["sun"]["current_position"][
                "altitude_deg"])
        self.assertEqual(
            298.0, result["bodies"]["moon"]["current_position"][
                "azimuth_deg"])
        self.assertEqual(
            "SUN123", result["bodies"]["sun"]["candidates"][0][
                "callsign"])
        self.assertEqual(
            "MOON456", result["bodies"]["moon"]["candidates"][0][
                "callsign"])
        encoded = json.dumps(result).lower()
        self.assertNotIn("latitude", encoded)
        self.assertNotIn("longitude", encoded)
        self.assertNotIn("buymeacoffee", encoded)
        self.assertNotIn("payment_id", encoded)

    def test_live_serialization_is_idempotent_for_application_store_shape(self):
        legacy = {
            "generated_at_utc": "2026-09-04T12:00:00Z",
            "sun": {
                "current_position": {
                    "altitude_deg": 12.0, "azimuth_deg": 34.0,
                },
                "candidates": [{
                    "icao": "ABC123", "body": "SUN",
                    "separation_deg": 1.2,
                }],
            },
            "moon": {
                "current_position": {
                    "altitude_deg": 56.0, "azimuth_deg": 78.0,
                },
                "candidates": [{
                    "icao": "DEF456", "body": "MOON",
                    "separation_deg": 0.9,
                }],
            },
            "recent_events": [], "presentation": {},
        }
        once = serialize_live_state(legacy)
        twice = serialize_live_state(once)
        self.assertEqual(once, twice)


class ApplicationStateStoreTests(unittest.TestCase):
    def test_revision_is_monotonic_and_state_is_immutable_from_callers(self):
        store = ApplicationStateStore()
        source = {"generated_at_utc": "one"}
        self.assertEqual(1, store.publish(source))
        source["generated_at_utc"] = "mutated"
        first = store.snapshot()
        first["state"]["generated_at_utc"] = "also-mutated"
        self.assertEqual("one", store.snapshot()["state"]["generated_at_utc"])
        self.assertEqual(2, store.publish({"generated_at_utc": "two"}))

    def test_dashboard_runtime_publication_failure_is_fail_open(self):
        state = dashboard.DashboardState()
        store = Mock()
        store.publish.side_effect = RuntimeError("failure")
        runtime = dashboard.DashboardRuntime(
            state, application_state_store=store)
        self.assertIsNone(runtime.tick(NOW))
        self.assertEqual(dashboard.utc_text(NOW),
                         state.snapshot(NOW)["generated_at_utc"])


class RuntimeSettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.applied = []
        self.store = RuntimeSettingsStore(
            apply_callback=lambda current, previous: self.applied.append(
                (current, previous)))

    def test_successful_atomic_update_and_revision(self):
        result = self.store.update(0, "command-1", {
            "telegram": {"sun_enabled": False},
            "observer": {"fallback_enabled": True},
        })
        self.assertEqual(1, result["revision"])
        self.assertFalse(result["values"]["telegram"]["sun_enabled"])
        self.assertTrue(result["values"]["observer"]["fallback_enabled"])
        self.assertEqual(1, len(self.applied))

    def test_manual_observer_values_are_validated_and_persisted(self):
        result = self.store.update(0, "manual", {"observer": {
            "requested_mode": "MANUAL", "manual_lat_deg": 51.25,
            "manual_lon_deg": 21.5, "manual_elevation_amsl_m": 315.0,
        }})
        self.assertEqual("MANUAL", result["values"]["observer"]["requested_mode"])
        self.assertEqual(51.25, result["values"]["observer"]["manual_lat_deg"])
        self.assertTrue(result["values"]["observer"]["manual_position_saved"])
        switched = self.store.update(1, "static", {
            "observer": {"requested_mode": "STATIC"}})
        self.assertEqual(51.25, switched["values"]["observer"]["manual_lat_deg"])

    def test_manual_mode_requires_an_explicit_complete_saved_position(self):
        before = self.store.snapshot()
        with self.assertRaisesRegex(
                SettingsValidationError, "Save a complete MANUAL"):
            self.store.update(0, "manual-without-position", {
                "observer": {"requested_mode": "MANUAL"}})
        self.assertEqual(before, self.store.snapshot())

    def test_invalid_manual_coordinates_are_rejected(self):
        for key, value in (("manual_lat_deg", 90.1),
                           ("manual_lon_deg", -180.1),
                           ("manual_elevation_amsl_m", "high")):
            with self.subTest(key=key):
                with self.assertRaises(SettingsValidationError):
                    self.store.update(0, "invalid-" + key,
                                      {"observer": {key: value}})

    def test_incomplete_manual_position_is_rejected_atomically(self):
        before = self.store.snapshot()
        with self.assertRaises(SettingsValidationError):
            self.store.update(0, "incomplete-manual", {
                "observer": {"manual_lat_deg": 51.25}})
        self.assertEqual(before, self.store.snapshot())

    def test_stale_revision_applies_nothing(self):
        self.store.update(0, "first", {"telegram": {"sun_enabled": False}})
        with self.assertRaises(SettingsConflictError) as raised:
            self.store.update(0, "stale", {
                "telegram": {"moon_enabled": False}})
        self.assertEqual(1, raised.exception.current["revision"])
        self.assertTrue(self.store.snapshot()["values"]["telegram"]["moon_enabled"])
        self.assertEqual(1, len(self.applied))

    def test_invalid_change_is_fully_rejected(self):
        before = self.store.snapshot()
        with self.assertRaises(SettingsValidationError):
            self.store.update(0, "bad", {
                "telegram": {"sun_enabled": False},
                "observer": {"requested_mode": "INVALID"},
            })
        self.assertEqual(before, self.store.snapshot())
        self.assertEqual([], self.applied)

    def test_idempotent_retry_does_not_increment_or_reapply(self):
        changes = {"telegram": {"sun_enabled": False}}
        first = self.store.update(0, "same-command", changes)
        second = self.store.update(0, "same-command", changes)
        self.assertEqual(1, first["revision"])
        self.assertEqual(1, second["revision"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(1, len(self.applied))
        with self.assertRaises(SettingsValidationError):
            self.store.update(1, "same-command", {
                "telegram": {"sun_enabled": True}})

    def test_two_clients_from_same_revision_only_one_wins(self):
        barrier = threading.Barrier(3)
        results = []

        def update(command, body):
            barrier.wait()
            try:
                results.append(("ok", self.store.update(
                    0, command, {"telegram": {body: False}})))
            except SettingsConflictError as error:
                results.append(("conflict", error.current))

        threads = [
            threading.Thread(target=update, args=("sun", "sun_enabled")),
            threading.Thread(target=update, args=("moon", "moon_enabled")),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(["conflict", "ok"], sorted(item[0] for item in results))
        self.assertEqual(1, self.store.snapshot()["revision"])

    def test_change_subscriber_is_fail_open_and_receives_accepted_state(self):
        received = []
        self.store.subscribe(lambda state: received.append(state))
        self.store.subscribe(lambda state: (_ for _ in ()).throw(RuntimeError()))
        result = self.store.update(
            0, "notify", {"telegram": {"sun_enabled": False}})
        self.assertEqual(result, received[0])


class ManualObserverRestartPersistenceTests(unittest.TestCase):
    def test_valid_position_survives_restart_and_invalid_update_preserves_it(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "dashboard_settings.json"
            first_provider = RuntimeObserverPositionProvider(
                ObserverPosition(51.0, 20.0, 200.0), mode="STATIC")
            first = dashboard.start_dashboard(
                False, "127.0.0.1", 0, lambda: NOW,
                observer_position_provider=first_provider,
                manual_settings_path=settings_path)
            first.settings_store.update(0, "save-manual", {
                "observer": {
                    "manual_lat_deg": 51.25,
                    "manual_lon_deg": 21.5,
                    "manual_elevation_amsl_m": 315.0,
                },
            })
            first.close()

            restarted_provider = RuntimeObserverPositionProvider(
                ObserverPosition(51.0, 20.0, 200.0), mode="STATIC")
            restarted = dashboard.start_dashboard(
                False, "127.0.0.1", 0, lambda: NOW,
                observer_position_provider=restarted_provider,
                manual_settings_path=settings_path)
            restored = restarted.settings_store.snapshot()["values"]["observer"]
            self.assertEqual("STATIC", restored["requested_mode"])
            self.assertEqual(51.25, restored["manual_lat_deg"])
            self.assertEqual(21.5, restored["manual_lon_deg"])
            self.assertEqual(315.0, restored["manual_elevation_amsl_m"])
            self.assertTrue(restored["manual_position_saved"])
            restarted.settings_store.update(0, "select-manual", {
                "observer": {"requested_mode": "MANUAL"},
            })
            self.assertEqual(
                ObserverPosition(51.25, 21.5, 315.0),
                restarted_provider.resolve(NOW).position)
            with self.assertRaises(SettingsValidationError):
                restarted.settings_store.update(1, "invalid-manual", {
                    "observer": {
                        "manual_lat_deg": 91.0,
                        "manual_lon_deg": 22.0,
                        "manual_elevation_amsl_m": 400.0,
                    },
                })
            restarted.close()

            final_provider = RuntimeObserverPositionProvider(
                ObserverPosition(51.0, 20.0, 200.0), mode="STATIC")
            final = dashboard.start_dashboard(
                False, "127.0.0.1", 0, lambda: NOW,
                observer_position_provider=final_provider,
                manual_settings_path=settings_path)
            final_observer = final.settings_store.snapshot()["values"]["observer"]
            self.assertEqual(51.25, final_observer["manual_lat_deg"])
            self.assertEqual(21.5, final_observer["manual_lon_deg"])
            self.assertEqual(315.0, final_observer[
                "manual_elevation_amsl_m"])
            final.close()


class VersionedDashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.manual_settings_path = (
            Path(self.temporary_directory.name) / "dashboard_settings.json")
        self.provider = RuntimeObserverPositionProvider(
            ObserverPosition(51.0, 20.0, 200.0), mode="STATIC")
        self.runtime = dashboard.start_dashboard(
            True, "127.0.0.1", 0, lambda: NOW,
            observer_position_provider=self.provider,
            mobile_gps_enabled=True, history_enabled=False,
            telegram_sun_enabled=True, telegram_moon_enabled=True,
            manual_settings_path=self.manual_settings_path)
        self.base = "http://127.0.0.1:{}".format(
            self.runtime.server.server_address[1])

    def tearDown(self):
        self.runtime.close()
        self.temporary_directory.cleanup()

    def get_json(self, path):
        with urllib.request.urlopen(self.base + path, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_late_callsign_matches_legacy_state_history_and_bootstrap_http(self):
        self.runtime.publish(dashboard.DashboardCandidate(
            body="SUN", icao="ABC123", callsign=None,
            predicted_event_utc=NOW + datetime.timedelta(seconds=2),
            separation_deg=0.5, body_azimuth_deg=120.0,
            body_elevation_deg=20.0, aircraft_elevation_deg=20.5,
            distance_km=100.0, last_prediction_update_utc=NOW,
            telegram_range=True))
        self.runtime.update_callsign("ABC123", "LATE123")
        _, legacy = self.get_json("/api/state")
        _, bootstrap = self.get_json("/api/v1/bootstrap")
        row = legacy["sun"]["candidates"][0]
        self.assertEqual("LATE123", row["callsign"])
        self.assertEqual(row, bootstrap["bodies"]["sun"]["candidates"][0])
        self.runtime.tick(NOW + datetime.timedelta(seconds=2))
        _, legacy = self.get_json("/api/state")
        _, history = self.get_json("/api/history")
        _, bootstrap = self.get_json("/api/v1/bootstrap")
        self.assertEqual("LATE123", history["records"][0]["callsign"])
        self.assertEqual(history["records"], legacy["recent_events"])
        self.assertEqual(history["records"], bootstrap["recent_events"])

    def test_bootstrap_and_settings_contracts(self):
        _, settings = self.get_json("/api/v1/settings")
        _, bootstrap = self.get_json("/api/v1/bootstrap")
        self.assertEqual(1, settings["schema_version"])
        self.assertEqual(settings["revision"], bootstrap["settings_revision"])
        self.assertEqual(settings["values"], bootstrap["settings"])
        self.assertIn("bodies", bootstrap)
        self.assertIn("observer", bootstrap)
        encoded = json.dumps(bootstrap).lower()
        for forbidden in ("latitude", "longitude", "bot_token", "chat_id",
                          "session_directory", "manifest_path"):
            self.assertNotIn(forbidden, encoded)

    def test_history_rejects_invalid_max_separation(self):
        for value in ("-0.1", "not-a-number", "NaN", "Infinity"):
            with self.subTest(value=value):
                request = urllib.request.Request(
                    self.base + "/api/history?max_sep_deg=" + value)
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(400, raised.exception.code)

    def test_history_http_max_separation_filters_the_actual_response(self):
        for icao, separation, seconds in (("LOW040", 0.4, 1),
                                           ("HIGH53", 5.3, 2)):
            self.runtime.publish(dashboard.DashboardCandidate(
                body="SUN", icao=icao, callsign=icao,
                predicted_event_utc=NOW + datetime.timedelta(seconds=seconds),
                separation_deg=separation, body_azimuth_deg=120.0,
                body_elevation_deg=20.0, aircraft_elevation_deg=20.5,
                distance_km=100.0, last_prediction_update_utc=NOW,
                telegram_range=True))
            self.runtime.tick(NOW + datetime.timedelta(seconds=seconds))

        _, unfiltered = self.get_json("/api/history?body=ALL")
        _, filtered = self.get_json(
            "/api/history?offset=0&limit=25&body=ALL&max_sep_deg=0.8")
        self.assertEqual([5.3, 0.4], [record["final_separation_deg"]
                                     for record in unfiltered["records"]])
        self.assertEqual([0.4], [record["final_separation_deg"]
                                for record in filtered["records"]])

    def test_patch_success_conflict_and_idempotent_retry(self):
        payload = {
            "expected_revision": 0, "command_id": "client-a-1",
            "changes": {"telegram": {"sun_enabled": False}},
        }
        status, accepted = patch_json(self.base + "/api/v1/settings", payload)
        self.assertEqual(200, status)
        self.assertEqual(1, accepted["revision"])
        self.assertFalse(self.runtime.telegram_enabled("SUN"))
        _, retried = patch_json(self.base + "/api/v1/settings", payload)
        self.assertEqual(1, retried["revision"])
        self.assertTrue(retried["idempotent_replay"])

        stale = dict(payload)
        stale["command_id"] = "client-b-1"
        stale["changes"] = {"telegram": {"moon_enabled": False}}
        request = urllib.request.Request(
            self.base + "/api/v1/settings",
            data=json.dumps(stale).encode("utf-8"), method="PATCH",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(409, raised.exception.code)
        current = json.loads(raised.exception.read().decode("utf-8"))["current"]
        self.assertEqual(1, current["revision"])
        self.assertTrue(current["values"]["telegram"]["moon_enabled"])

    def test_invalid_patch_is_atomic(self):
        request = urllib.request.Request(
            self.base + "/api/v1/settings",
            data=json.dumps({
                "expected_revision": 0, "command_id": "invalid",
                "changes": {
                    "telegram": {"sun_enabled": False},
                    "observer": {"requested_mode": "INVALID"},
                },
            }).encode("utf-8"), method="PATCH",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(400, raised.exception.code)
        _, settings = self.get_json("/api/v1/settings")
        self.assertEqual(0, settings["revision"])
        self.assertTrue(settings["values"]["telegram"]["sun_enabled"])

    def test_legacy_and_v1_endpoints_share_settings_store(self):
        request = urllib.request.Request(
            self.base + "/api/telegram",
            data=json.dumps({"body": "MOON", "enabled": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            legacy = json.loads(response.read().decode("utf-8"))
        _, settings = self.get_json("/api/v1/settings")
        self.assertFalse(legacy["moon_enabled"])
        self.assertFalse(settings["values"]["telegram"]["moon_enabled"])
        self.assertEqual(1, settings["revision"])

        observer = urllib.request.Request(
            self.base + "/api/observer",
            data=json.dumps({"mode": "MOBILE", "fallback_enabled": True}
                            ).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(observer, timeout=2) as response:
            legacy_observer = json.loads(response.read().decode("utf-8"))
        _, settings = self.get_json("/api/v1/settings")
        self.assertEqual("MOBILE", legacy_observer["requested_mode"])
        self.assertEqual("MOBILE",
                         settings["values"]["observer"]["requested_mode"])
        self.assertTrue(settings["values"]["observer"]["fallback_enabled"])
        self.assertEqual(2, settings["revision"])

    def test_legacy_telegram_get_reflects_external_sun_and_moon_changes(self):
        patch_json(self.base + "/api/v1/settings", {
            "expected_revision": 0, "command_id": "external-sun",
            "changes": {"telegram": {"sun_enabled": False}},
        })
        patch_json(self.base + "/api/v1/settings", {
            "expected_revision": 1, "command_id": "external-moon",
            "changes": {"telegram": {"moon_enabled": False}},
        })
        _, legacy = self.get_json("/api/telegram")
        self.assertFalse(legacy["sun_enabled"])
        self.assertFalse(legacy["moon_enabled"])

    def test_v1_observer_update_controls_existing_provider(self):
        _, result = patch_json(self.base + "/api/v1/settings", {
            "expected_revision": 0, "command_id": "observer-1",
            "changes": {"observer": {
                "requested_mode": "MOBILE", "fallback_enabled": True}},
        })
        self.assertEqual("MOBILE",
                         result["values"]["observer"]["requested_mode"])
        context = self.provider.resolve(NOW)
        self.assertEqual("MOBILE", context.requested_mode)
        self.assertTrue(context.fallback_enabled)
        _, restored = patch_json(self.base + "/api/v1/settings", {
            "expected_revision": 1, "command_id": "observer-static",
            "changes": {"observer": {"requested_mode": "STATIC"}},
        })
        self.assertEqual("STATIC", restored["values"]["observer"][
            "requested_mode"])
        self.assertEqual("STATIC", self.provider.resolve(NOW).effective_source)

    def test_first_manual_activation_returns_useful_validation_error(self):
        request = urllib.request.Request(
            self.base + "/api/v1/settings",
            data=json.dumps({
                "expected_revision": 0,
                "command_id": "manual-without-saved-position",
                "changes": {"observer": {"requested_mode": "MANUAL"}},
            }).encode("utf-8"), method="PATCH",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(400, raised.exception.code)
        error = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual(
            "Save a complete MANUAL observer position before activation",
            error["error"])
        self.assertEqual("STATIC", self.provider.resolve(NOW).effective_source)

    def test_manual_observer_update_is_effective_and_publicly_configurable(self):
        payload = {
            "expected_revision": 0, "command_id": "manual-observer",
            "changes": {"observer": {
                "requested_mode": "MANUAL", "manual_lat_deg": 51.25,
                "manual_lon_deg": 21.5,
                "manual_elevation_amsl_m": 315.0}},
        }
        status, settings = patch_json(self.base + "/api/v1/settings", payload)
        self.assertEqual(200, status)
        self.assertEqual("MANUAL", self.provider.resolve(NOW).effective_source)
        self.assertEqual(ObserverPosition(51.25, 21.5, 315.0),
                         self.provider.resolve(NOW).position)
        _, bootstrap = self.get_json("/api/v1/bootstrap")
        self.assertEqual("MANUAL", bootstrap["observer"]["effective_source"])
        self.assertEqual(51.25, bootstrap["observer"]["manual_lat_deg"])
        self.assertEqual(315.0, bootstrap["observer"][
            "manual_elevation_amsl_m"])
        self.assertNotIn("latitude", json.dumps(bootstrap).lower())
        self.assertEqual(51.25, settings["values"]["observer"]["manual_lat_deg"])
        _, switched = patch_json(self.base + "/api/v1/settings", {
            "expected_revision": 1, "command_id": "manual-to-static",
            "changes": {"observer": {"requested_mode": "STATIC"}},
        })
        _, restored = patch_json(self.base + "/api/v1/settings", {
            "expected_revision": 2, "command_id": "manual-restored",
            "changes": {"observer": {"requested_mode": "MANUAL"}},
        })
        self.assertEqual(51.25, switched["values"]["observer"]["manual_lat_deg"])
        self.assertEqual(ObserverPosition(51.25, 21.5, 315.0),
                         self.provider.resolve(NOW).position)
        self.assertEqual(51.25, restored["values"]["observer"]["manual_lat_deg"])

    def test_disabled_dashboard_keeps_legacy_controls_and_has_one_store(self):
        runtime = dashboard.start_dashboard(
            False, "127.0.0.1", 0, lambda: NOW,
            telegram_sun_enabled=True, telegram_moon_enabled=True)
        self.assertIsNotNone(runtime.settings_store)
        self.assertEqual(
            {"sun_enabled": False, "moon_enabled": True},
            runtime.set_telegram_enabled("SUN", False))
        self.assertFalse(runtime.telegram_enabled("SUN"))
        self.assertFalse(runtime.settings_store.snapshot()[
            "values"]["telegram"]["sun_enabled"])

    def test_legacy_observer_mobile_disabled_response_remains_forbidden(self):
        runtime = dashboard.start_dashboard(
            True, "127.0.0.1", 0, lambda: NOW,
            observer_position_provider=RuntimeObserverPositionProvider(
                ObserverPosition(51.0, 20.0, 200.0)),
            mobile_gps_enabled=False, history_enabled=False)
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:{}/api/observer".format(
                    runtime.server.server_address[1]),
                data=json.dumps({"mode": "MOBILE"}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(403, raised.exception.code)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
