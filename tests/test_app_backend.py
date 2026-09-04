import datetime
import json
import threading
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


class VersionedDashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.provider = RuntimeObserverPositionProvider(
            ObserverPosition(51.0, 20.0, 200.0), mode="STATIC")
        self.runtime = dashboard.start_dashboard(
            True, "127.0.0.1", 0, lambda: NOW,
            observer_position_provider=self.provider,
            mobile_gps_enabled=True, history_enabled=False,
            telegram_sun_enabled=True, telegram_moon_enabled=True)
        self.base = "http://127.0.0.1:{}".format(
            self.runtime.server.server_address[1])

    def tearDown(self):
        self.runtime.close()

    def get_json(self, path):
        with urllib.request.urlopen(self.base + path, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_bootstrap_and_settings_contracts(self):
        _, settings = self.get_json("/api/v1/settings")
        _, bootstrap = self.get_json("/api/v1/bootstrap")
        self.assertEqual(1, settings["schema_version"])
        self.assertEqual(settings["revision"], bootstrap["settings_revision"])
        self.assertIn("bodies", bootstrap)
        self.assertIn("observer", bootstrap)
        encoded = json.dumps(bootstrap).lower()
        for forbidden in ("latitude", "longitude", "bot_token", "chat_id",
                          "session_directory", "manifest_path"):
            self.assertNotIn(forbidden, encoded)

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
                    "observer": {"requested_mode": "MANUAL"},
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
