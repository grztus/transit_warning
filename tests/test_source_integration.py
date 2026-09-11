"""STANDARD ownership, handoff race and map-free API regressions."""
import datetime as dt
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import transit_warning as runtime
from app_backend.contracts import serialize_bootstrap
from app_backend.state import ApplicationStateStore
from live_dashboard import DashboardCandidate, DashboardRuntime, DashboardState, start_dashboard
from observer_position import ObserverPosition, RuntimeObserverPositionProvider
from source_geometry_fixture import ConstantGeoid, canonical_context, parallel_sun
from test_adsblol_live import NOW, fixture
from test_auto_fusion import local
from test_aircraft_source_mode import FakeDashboard
from tools.adsblol_live import normalize_response
from tools.adsblol_standalone_runtime import SnapshotPoller, StandaloneBridge


def candidate(icao="ABC123"):
    return DashboardCandidate(body="SUN", icao=icao, callsign="TEST",
        predicted_event_utc=NOW + dt.timedelta(seconds=60), separation_deg=.1,
        body_azimuth_deg=1, body_elevation_deg=2, aircraft_elevation_deg=3,
        distance_km=4, last_prediction_update_utc=NOW, telegram_range=False,
        encounter_id="0:ABC123:SUN:1", prediction_geometry="TRUE_2D",
        aircraft_source_mode="AUTO")


class SourceIntegrationTests(unittest.TestCase):
    def setUp(self):
        for name in ("plane_dict", "raw_adsb_tracks", "aircraft_motion_states",
                     "altitude_sources", "raw_adsb_versions", "gnss_altitude_states",
                     "mlat_beast_tracks", "mlat_coarse_tracks", "aircraft_intent_states",
                     "aircraft_motion_freshness_status", "fused_aircraft_states"):
            self.enterContext(patch.object(runtime, name, {}))
        for name, value in (("aircraft_source_mode", "LOCAL"), ("auto_source_scope", None),
                            ("internet_source_poller", None), ("internet_source_bridge", None),
                            ("aircraft_los_geoid_provider", None)):
            self.enterContext(patch.object(runtime, name, value))
        self.enterContext(patch.object(runtime, "adsblol_auto_cache", runtime.ProviderSnapshotCache()))
        self.enterContext(patch.object(runtime, "dashboard_runtime", FakeDashboard()))
        self.observer = RuntimeObserverPositionProvider(ObserverPosition(1, 2, 3))
        self.enterContext(patch.object(runtime, "observer_position_provider", self.observer))
        self.enterContext(patch.object(runtime, "clock", SimpleNamespace(now_utc=lambda: NOW)))
        self.dashboard = DashboardRuntime(DashboardState(), application_state_store=ApplicationStateStore())
        self.poller = SnapshotPoller(self.observer, provider=Mock(), now=lambda: NOW,
                                     monotonic=lambda: 100.)
        self.bridge = StandaloneBridge(self.poller, self.dashboard, ConstantGeoid(), source_mode="AUTO")

    def report(self):
        return dict(normalize_response(fixture(), NOW, 100.), status="OK",
                    request_finished_monotonic=100.)

    def test_map_free_publication_detaches_provenance_and_hides_owner(self):
        provenance = {"fields": {"ground_track": {"selected": {"value": 42}}}}
        self.dashboard.publish_authoritative(candidate(),
            SimpleNamespace(fusion_provenance=provenance), source_owner=self.bridge)
        provenance["fields"]["ground_track"]["selected"]["value"] = 99
        stored = self.dashboard.state._live["SUN"]["ABC123"]["candidate"]
        self.assertEqual(42, stored.fusion_provenance["fields"]["ground_track"]["selected"]["value"])
        self.dashboard.state.set_aircraft_source({"requested_mode": "AUTO",
            "effective_mode": "LOCAL+ADSBLOL", "status": "HEALTHY", "enrichment": "ACTIVE"})
        self.dashboard._publish_application_state()
        payload = serialize_bootstrap(self.dashboard.application_state_store.snapshot(),
            {"revision": 0, "values": {}, "capabilities": {}}, {}, NOW)
        for forbidden in ("source_owner", "centerline", "map_privacy", "pattern", "fusion_provenance"):
            self.assertNotIn(forbidden, json.dumps(payload))
        self.assertEqual("ACTIVE", payload["aircraft_source"]["enrichment"])

    def test_remote_expiry_cannot_withdraw_local_replacement_even_with_same_id(self):
        self.dashboard.publish_authoritative(candidate(), SimpleNamespace(), source_owner=self.bridge)
        self.dashboard.publish(candidate())
        self.bridge._remove("ABC123", NOW)
        self.assertIn("ABC123", self.dashboard.state._live["SUN"])
        self.assertEqual([], self.dashboard.state.query_history()["records"])

    def test_remote_scope_reset_only_invalidates_owned_candidates(self):
        self.dashboard.publish(candidate("LOCAL"))
        self.dashboard.publish_authoritative(candidate(), SimpleNamespace(), source_owner=self.bridge)
        self.bridge._clear(NOW)
        self.assertEqual({"LOCAL"}, set(self.dashboard.state._live["SUN"]))
        self.assertEqual([], self.dashboard.state.query_history()["records"])

    def test_raw_update_waiting_for_aircraft_lock_cannot_cross_source_reset(self):
        lock = runtime.plane_dict_lock
        attempted = threading.Event()
        class ObservedLock:
            def __enter__(self):
                attempted.set()
                lock.acquire()
            def __exit__(self, *_):
                lock.release()
        errors = []
        def update():
            try:
                runtime.update_raw_adsb_track(SimpleNamespace(icao="ABC123", track_deg=90), NOW)
            except Exception as error:
                errors.append(error)
        with lock, patch.object(runtime, "plane_dict_lock", ObservedLock()):
            thread = threading.Thread(target=update)
            thread.start()
            self.assertTrue(attempted.wait(2))
            runtime.set_aircraft_source_mode("INTERNET")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual({}, runtime.raw_adsb_tracks)

    def test_old_provider_close_runs_without_source_or_aircraft_lock(self):
        old = Mock()
        acquired = threading.Event()
        def close():
            def probe():
                with runtime.aircraft_source_lock, runtime.plane_dict_lock:
                    acquired.set()
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(2)
            self.assertTrue(acquired.is_set())
        old.close.side_effect = close
        runtime.internet_source_poller = old
        runtime.set_aircraft_source_mode("LOCAL")
        old.stop.set.assert_called_once_with()
        old.close.assert_called_once_with()

    def test_auto_scope_change_drops_old_evidence_and_preserves_local(self):
        self.poller.provider.acquire.return_value = self.report()
        self.poller.fetch_once()
        runtime.aircraft_source_mode = "AUTO"
        runtime.internet_source_poller = self.poller
        runtime.dashboard_runtime = self.dashboard
        self.dashboard.publish(candidate("LOCAL"))
        with patch.object(runtime.time, "monotonic", return_value=100.):
            runtime.step_auto_aircraft_source()
            self.assertIn("ABC123", runtime.adsblol_auto_cache.aircraft)
            self.observer.set_mode("MOBILE", NOW)
            runtime.step_auto_aircraft_source()
        self.assertEqual({}, runtime.adsblol_auto_cache.aircraft)
        self.assertEqual({}, runtime.fused_aircraft_states)
        self.assertIn("LOCAL", self.dashboard.state._live["SUN"])
        self.assertEqual("PRIVACY_BLOCKED", self.dashboard.state.snapshot()["aircraft_source"]["status"])

    def test_auto_provider_health_expires_without_new_snapshot(self):
        self.poller.provider.acquire.return_value = self.report()
        self.poller.fetch_once()
        runtime.aircraft_source_mode = "AUTO"
        runtime.internet_source_poller = self.poller
        runtime.dashboard_runtime = self.dashboard
        with patch.object(runtime.time, "monotonic", return_value=131.):
            runtime.step_auto_aircraft_source()
        source = self.dashboard.state.snapshot()["aircraft_source"]
        self.assertEqual("STALE", source["status"])
        self.assertEqual("DEGRADED", source["enrichment"])

    def test_backend_settings_callback_switches_source_without_frontend(self):
        changed = Mock()
        dashboard = start_dashboard(False, "127.0.0.1", 0, lambda: NOW,
            observer_position_provider=self.observer, aircraft_source_change=changed)
        settings = dashboard.settings_store
        settings.update(0, "source", {"aircraft_source": {"requested_mode": "INTERNET"}})
        changed.assert_called_once_with("INTERNET")
        self.assertEqual("INTERNET", settings.snapshot()["values"]["aircraft_source"]["requested_mode"])

    def test_local_recovery_during_auto_solve_prevents_remote_overwrite(self):
        report = self.report()
        runtime.adsblol_auto_cache.consume(report, 100.)
        bridge = runtime.AutoFusionBridge(self.poller, self.dashboard, ConstantGeoid())
        bridge.aircraft = {"ABC123": report["aircraft"][0]}
        self.dashboard.publish(candidate())
        fields = {
            "position": local("position", {"lat": 1, "lon": 2}, 0),
            "ground_track": local("ground_track", 90, 0),
            "groundspeed": local("groundspeed", 700, 0, unit="km/h"),
            "geometric_altitude": local("geometric_altitude", 10000, 0,
                                        unit="m", datum="WGS84_HAE")}
        with patch.object(runtime, "local_fusion_fields", return_value=fields), patch.object(
                runtime, "capture_authoritative_transit_prediction") as capture:
            bridge._publish(SimpleNamespace(icao="ABC123"), None, NOW)
        capture.assert_not_called()
        self.assertIsNone(self.dashboard.state._live["SUN"]["ABC123"]["source_owner"])


    def test_auto_remote_aircraft_reaches_real_solver_and_owned_publication(self):
        ctx = canonical_context()
        now = ctx.prediction_base_utc
        self.observer.set_manual_position(ctx.observer_context.position, now)
        self.observer.set_mode("MANUAL", now)
        self.poller.now = lambda: now
        payload = fixture()
        payload["ac"][0].update(lat=ctx.latitude_deg, lon=ctx.longitude_deg,
            alt_baro=ctx.current_altitude_m / .3048,
            alt_geom=ctx.current_altitude_m / .3048,
            gs=ctx.groundspeed_kmh / 1.852, track=ctx.track_deg, seen_pos=0)
        report = dict(normalize_response(payload, now, 100.), status="OK",
                      request_finished_monotonic=100., response_latency_seconds=0)
        self.poller.provider.acquire.return_value = report
        self.poller.fetch_once()
        runtime.adsblol_auto_cache.consume(report, 100.)
        bridge = runtime.AutoFusionBridge(self.poller, self.dashboard, ConstantGeoid())
        bridge.body_position = lambda name, when, observer: parallel_sun(when, observer)
        with patch.object(runtime, "capture_authoritative_transit_prediction") as capture:
            bridge.step()
        item = self.dashboard.state._live["SUN"]["ABC123"]
        self.assertIs(bridge, item["source_owner"])
        self.assertEqual("AUTO", item["candidate"].aircraft_source_mode)
        self.assertTrue(item["candidate"].fusion_provenance["predictor"]["freeze_altitude"])
        self.assertGreaterEqual(capture.call_count, 1)
