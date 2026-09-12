"""Two-client observer synchronization through the production settings/SSE path."""
import datetime as dt
import json
import unittest
import urllib.request

from live_dashboard import start_dashboard
from observer_position import ObserverPosition, RuntimeObserverPositionProvider
from tests.test_app_backend import patch_json

NOW = dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc)


class ObserverSynchronizationTests(unittest.TestCase):
    def test_other_client_receives_static_and_custom_observer_in_settings_event(self):
        provider = RuntimeObserverPositionProvider(ObserverPosition(1, 2, 100), mode="MOBILE")
        runtime = start_dashboard(True, "127.0.0.1", 0, lambda: NOW,
            observer_position_provider=provider, mobile_gps_enabled=True,
            manual_settings_path=None)
        self.addCleanup(runtime.close)
        runtime.mobile_gps_state.update({"latitude": 12.34567, "longitude": 34.56789,
            "accuracy": 5., "timestamp": 0}, NOW)
        base = "http://127.0.0.1:{}".format(runtime.server.server_address[1])
        # Client B stays connected; client A mutates settings over HTTP.
        response = urllib.request.urlopen(base + "/api/v1/stream", timeout=2)
        self.addCleanup(response.close)
        def next_event():
            while True:
                line = response.readline().decode("utf-8")
                if line.startswith("data: "):
                    return json.loads(line[6:])
        initial = [next_event(), next_event()]
        settings = next(item["payload"] for item in initial if item["event"] == "settings")
        self.assertEqual("MOBILE_FRESH", settings["observer"]["effective_source"])
        for index, observer in enumerate((
                {"requested_mode": "STATIC"},
                {"requested_mode": "MANUAL", "manual_lat_deg": 3.,
                 "manual_lon_deg": 4., "manual_elevation_amsl_m": 120.}), 1):
            _, accepted = patch_json(base + "/api/v1/settings", {
                "expected_revision": index - 1, "command_id": str(index),
                "changes": {"observer": observer}})
            event = next_event()
            while event["event"] != "settings":
                event = next_event()
            self.assertEqual(index, event["settings_revision"])
            payload = event["payload"]
            self.assertEqual(observer["requested_mode"], payload["values"]["observer"]["requested_mode"])
            self.assertEqual(observer["requested_mode"], payload["observer"]["effective_source"])
            self.assertEqual(accepted["observer"], payload["observer"])
            self.assertNotIn("12.34567", json.dumps(event))
            self.assertNotIn("34.56789", json.dumps(event))
        runtime.tick(NOW + dt.timedelta(seconds=1))
        self.assertTrue(runtime.publisher.flush())
        live = runtime.application_state_store.snapshot()["state"]
        self.assertEqual(2, live["observer_settings_revision"])
        self.assertEqual("MANUAL", live["observer"]["effective_source"])

    def test_live_status_refreshes_when_gps_expires_without_a_settings_change(self):
        now = [NOW]
        provider = RuntimeObserverPositionProvider(ObserverPosition(1, 2, 100), mode="MOBILE", fallback_enabled=True)
        runtime = start_dashboard(True, "127.0.0.1", 0, lambda: now[0],
            observer_position_provider=provider, mobile_gps_enabled=True,
            manual_settings_path=None)
        self.addCleanup(runtime.close)
        runtime.mobile_gps_state.update({"latitude": 12., "longitude": 34., "accuracy": 5., "timestamp": 0}, NOW)
        runtime.tick(NOW + dt.timedelta(seconds=1))
        self.assertTrue(runtime.publisher.flush())
        first = runtime.application_state_store.snapshot()["state"]
        self.assertEqual("MOBILE_FRESH", first["observer"]["effective_source"])
        now[0] += dt.timedelta(seconds=16)
        runtime.tick(now[0])
        self.assertTrue(runtime.publisher.flush())
        latest = runtime.application_state_store.snapshot()["state"]
        self.assertEqual("STATIC_FALLBACK", latest["observer"]["effective_source"])
        self.assertEqual(first["observer_settings_revision"], latest["observer_settings_revision"])
