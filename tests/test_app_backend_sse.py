import datetime
import json
import time
import unittest
import urllib.request

from app_backend.sse import SseBroker, encode_sse, live_envelope, settings_envelope
from app_backend.settings import RuntimeSettingsStore
from app_backend.state import ApplicationStateStore
import live_dashboard


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class SseBrokerTests(unittest.TestCase):
    def test_latest_state_delivery_is_bounded_and_ordered_by_event_type(self):
        broker = SseBroker()
        client = broker.subscribe()
        broker.publish({"event": "live_state", "live_revision": 1})
        broker.publish({"event": "live_state", "live_revision": 2})
        broker.publish({"event": "settings", "settings_revision": 1})
        values = client.next(0)
        self.assertEqual(2, len(values))
        self.assertEqual(2, next(v for v in values if v["event"] == "live_state")["live_revision"])
        client.close()
        self.assertEqual(0, broker.client_count)

    def test_duplicate_and_older_revisions_are_not_reemitted(self):
        broker = SseBroker()
        client = broker.subscribe()
        broker.publish({"event": "live_state", "live_revision": 4})
        self.assertEqual(4, client.next(0)[0]["live_revision"])
        broker.publish({"event": "live_state", "live_revision": 4})
        broker.publish({"event": "live_state", "live_revision": 3})
        self.assertEqual((), client.next(0))

    def test_slow_client_does_not_block_publication(self):
        broker = SseBroker()
        client = broker.subscribe()
        started = time.perf_counter()
        for revision in range(1000):
            broker.publish({"event": "live_state", "live_revision": revision})
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(999, client.next(0)[0]["live_revision"])

    def test_envelopes_are_versioned_private_safe_and_encoded(self):
        app = ApplicationStateStore()
        app.publish({"generated_at_utc": "2026-09-04T12:00:00Z",
                     "sun": {"candidates": [{"icao": "ABC123", "latitude": 50.0}]}})
        live = live_envelope(app.snapshot())
        settings = settings_envelope(RuntimeSettingsStore().snapshot())
        self.assertEqual(("live_state", 1), (live["event"], live["live_revision"]))
        self.assertEqual("settings", settings["event"])
        encoded = (encode_sse(live) + encode_sse(settings)).lower()
        self.assertNotIn("latitude", encoded)
        self.assertNotIn("longitude", encoded)
        self.assertTrue(encoded.startswith("event: live_state\ndata: "))

    def test_application_subscriber_failure_is_fail_open(self):
        app = ApplicationStateStore()
        received = []
        app.subscribe(lambda snapshot: received.append(snapshot["revision"]))
        app.subscribe(lambda _snapshot: (_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(1, app.publish({"generated_at_utc": "one"}))
        self.assertEqual([1], received)

    def test_heartbeat_wait_does_not_change_application_revision(self):
        app = ApplicationStateStore()
        broker = SseBroker()
        client = broker.subscribe()
        before = app.snapshot()["revision"]
        self.assertEqual((), client.next(0))
        self.assertEqual(before, app.snapshot()["revision"])

    def test_http_stream_has_initial_live_and_settings_events(self):
        runtime = live_dashboard.start_dashboard(True, "127.0.0.1", 0, lambda: NOW)
        try:
            port = runtime.server.server_address[1]
            response = urllib.request.urlopen(
                "http://127.0.0.1:{}/api/v1/stream".format(port), timeout=2)
            self.assertEqual("text/event-stream", response.headers.get_content_type())
            lines = []
            while len([line for line in lines if line.startswith("data:")]) < 2:
                lines.append(response.readline().decode("utf-8").strip())
            response.close()
        finally:
            runtime.close()
        self.assertIn("event: live_state", lines)
        self.assertIn("event: settings", lines)
        payloads = [json.loads(line[6:]) for line in lines if line.startswith("data:")]
        self.assertEqual({"live_state", "settings"}, {item["event"] for item in payloads})


if __name__ == "__main__":
    unittest.main()
