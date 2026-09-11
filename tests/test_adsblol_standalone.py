"""Isolated bridge tests: synthetic aircraft, no external HTTP or recordings."""
import copy
import datetime as dt
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from authoritative_transit import AuthoritativeTransitLifecycle
from app_backend.contracts import serialize_bootstrap
from app_backend.state import ApplicationStateStore
from live_dashboard import DashboardRuntime, DashboardState
from observer_position import ObserverPosition, RuntimeObserverPositionProvider
from shadow_2d_prediction import Shadow2DConfig, run_shadow_pipeline
from tools.adsblol_live import AdsbLolProvider, normalize_response
from tools.adsblol_standalone_runtime import SnapshotPoller, StandaloneBridge, scope
from tests.test_adsblol_live import fixture, response, NOW
from source_geometry_fixture import ConstantGeoid, canonical_context, parallel_sun


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.mono = 100.
        self.now = NOW
        self.observer = RuntimeObserverPositionProvider(
            ObserverPosition(1, 2, 3), mode="MANUAL", manual_position=ObserverPosition(4, 5, 6))
        self.provider = Mock()
        self.poller = SnapshotPoller(self.observer, provider=self.provider,
                                    monotonic=lambda: self.mono, now=lambda: self.now)
        self.state = DashboardState()
        self.dashboard = DashboardRuntime(self.state, application_state_store=ApplicationStateStore())
        self.bridge = StandaloneBridge(self.poller, self.dashboard, ConstantGeoid(35))

    def report(self, payload=None):
        normalized = normalize_response(payload or fixture(), self.now, self.mono)
        return dict(normalized, status="OK", retry_after_seconds=None, response_latency_seconds=.1,
                    request_finished_monotonic=self.mono)

    def acquire(self, report=None):
        self.provider.acquire.return_value = report or self.report()
        self.poller.fetch_once()
        self.bridge.step()

    def test_manual_query_and_mobile_with_fallback_blocked_before_http(self):
        self.acquire()
        self.assertEqual(self.provider.acquire.call_args.kwargs,
                         dict(observer_mode="MANUAL", lat=4, lon=5, radius_nm=100))
        for fallback in (False, True):
            self.provider.acquire.reset_mock()
            self.observer.set_fallback_enabled(fallback, self.now)
            self.observer.set_mode("MOBILE", self.now)
            self.poller.fetch_once()
            self.bridge.step()
            self.provider.acquire.assert_not_called()
            self.assertEqual(self.bridge.status, "PRIVACY_BLOCKED")
            self.assertEqual(self.bridge.aircraft, {})
            self.assertNotIn("center", json.dumps(self.poller.snapshot()[2]))

    def test_static_query_is_allowed(self):
        self.observer.set_mode("STATIC", self.now)
        self.provider.acquire.return_value = self.report()
        self.poller.fetch_once()
        self.bridge.step()
        self.assertEqual(self.provider.acquire.call_args.kwargs,
                         dict(observer_mode="STATIC", lat=1, lon=2,
                              radius_nm=100))

    def test_radius_cap_invalid_poll_and_unknown_position_age(self):
        poller = SnapshotPoller(self.observer, provider=self.provider, radius_nm=999)
        self.provider.acquire.return_value = self.report()
        poller.fetch_once()
        self.assertEqual(self.provider.acquire.call_args.kwargs["radius_nm"], 250)
        with self.assertRaises(ValueError):
            SnapshotPoller(self.observer, poll_seconds=1)
        payload = fixture()
        payload["ac"][0].pop("seen_pos")
        self.acquire(self.report(payload))
        self.assertEqual(self.bridge.aircraft, {})

    def test_provider_health_independent_of_aircraft_expiry_and_slow_refresh(self):
        self.acquire()
        self.assertEqual(self.bridge.status, "HEALTHY")
        # One stale aircraft must not mark successful transport stale.
        self.bridge.anchors["ABC123"] = ((), 100, self.mono)
        self.bridge.step()
        self.assertEqual(self.bridge.status, "HEALTHY")
        entered, release = threading.Event(), threading.Event()
        def delayed(**_):
            entered.set()
            release.wait(3)
            return self.report()
        self.provider.acquire.side_effect = delayed
        self.mono += 10  # Default polling interval after last completed request.
        thread = threading.Thread(target=self.poller.fetch_once)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            for elapsed in (0, 7.5, .5):
                self.mono += elapsed
                self.bridge.step()
                self.assertEqual(self.bridge.status, "REFRESHING")
        finally:
            release.set()
            thread.join(3)
        self.bridge.step()
        self.assertEqual(self.bridge.status, "HEALTHY")
        self.mono += 30
        self.bridge.step()
        self.assertEqual(self.bridge.status, "HEALTHY")
        self.mono += .01
        self.bridge.step()
        self.assertEqual(self.bridge.status, "STALE")
        self.provider.acquire.side_effect = None
        self.acquire({"status": "PROVIDER_ERROR"})
        self.assertEqual(self.bridge.status, "ERROR")
        self.acquire()
        self.assertEqual(self.bridge.status, "HEALTHY")

    def test_provider_stale_interval_validation_and_empty_success(self):
        for invalid in (0, -1, float("nan")):
            with self.assertRaises(ValueError):
                StandaloneBridge(self.poller, self.dashboard, ConstantGeoid(35),
                                 provider_stale_seconds=invalid)
        self.bridge.provider_stale_seconds = 2
        self.acquire(dict(self.report(), aircraft=[]))
        self.assertEqual(self.bridge.status, "HEALTHY")
        self.mono += 3
        self.bridge.step()
        self.assertEqual(self.bridge.status, "STALE")

    def test_single_in_flight_and_latest_mailbox_no_queue(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(**_):
            entered.set()
            release.wait(3)
            return self.report()
        self.provider.acquire.side_effect = delayed
        thread = threading.Thread(target=self.poller.fetch_once)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertIsNone(self.poller.fetch_once())
            self.bridge.step()  # Never waits for HTTP.
            self.assertEqual(self.bridge.status, "WAITING")
            self.assertEqual(self.provider.acquire.call_count, 1)
        finally:
            release.set()
            thread.join(3)
        self.provider.acquire.side_effect = None
        for _ in range(3):
            self.provider.acquire.return_value = self.report()
            self.poller.fetch_once()
        self.assertEqual(self.poller.snapshot()[0], 4)
        self.bridge.step()
        self.assertEqual(self.bridge.sequence, 4)

    def test_query_scope_change_during_http_discards_old_response(self):
        def changed(**_):
            self.observer.set_mode("MOBILE", self.now)
            return self.report()
        self.provider.acquire.side_effect = changed
        self.assertIsNone(self.poller.fetch_once())
        self.assertIsNone(self.poller.snapshot())

    def test_relative_age_and_monotonic_expiry_ignore_absolute_clock_skew(self):
        payload = fixture()
        payload["now"] += 3600_000
        payload["ctime"] += 3600_000
        self.acquire(self.report(payload))
        self.assertIn("ABC123", self.bridge.aircraft)
        for name in ("ground_track", "groundspeed", "barometric_altitude", "geometric_altitude"):
            self.assertIsNone(self.bridge.aircraft["ABC123"]["fields"][name]["age_seconds"])
        self.now -= dt.timedelta(hours=2)  # Host UTC jumps, monotonic safety does not.
        self.mono += 21
        self.bridge.step()
        self.assertEqual(self.bridge.aircraft, {})

    def test_stale_and_disappearing_positions_remove_aircraft(self):
        self.acquire()
        payload = fixture()
        payload["ac"][0]["seen_pos"] = 21
        self.acquire(self.report(payload))
        self.assertEqual(self.bridge.aircraft, {})
        self.acquire()
        payload.update(ac=[], total=0)
        self.acquire(self.report(payload))
        self.assertEqual(self.bridge.aircraft, {})

    def test_cached_position_cannot_extend_expiry_or_repeated_provider_errors(self):
        report = self.report()
        self.acquire(report)
        self.mono += 15
        cached = copy.deepcopy(report)
        cached["request_finished_monotonic"] = self.mono
        self.acquire(cached)
        self.mono += 6
        self.bridge.step()
        self.assertEqual(self.bridge.aircraft, {})
        cached["request_finished_monotonic"] = self.mono
        self.acquire(cached)
        self.assertEqual(self.bridge.aircraft, {})
        self.provider.acquire.side_effect = RuntimeError("private URL")
        self.poller.fetch_once()
        self.bridge.step()
        self.assertEqual(self.bridge.status, "HEALTHY")  # Last good receipt is still recent.
        self.assertNotIn("private", json.dumps(self.poller.snapshot()[2]))

    def test_altitude_context_uses_geoid_and_keeps_unknown_vertical_and_intent_times(self):
        ac = self.report()["aircraft"][0]
        context = self.bridge.context(ac, self.observer.resolve(self.now), self.now, 0, "SUN")
        self.assertAlmostEqual(context.current_altitude_m, 10000 * .3048)
        self.assertAlmostEqual(context.current_altitude_m + context.geometric_altitude_correction_m, 10800 * .3048 - 35)
        self.assertEqual(context.altitude_source, "ADSBLOL_STANDALONE_GEOMETRIC_HAE_UNSPECIFIED_DERIVATION")
        self.assertIsNone(context.vertical_motion)
        self.assertIsNone(context.vertical_intent)
        self.assertEqual(ac["fields"]["barometric_vertical_rate"]["value"], -512)
        self.assertEqual(ac["fields"]["selected_altitude_mcp"]["value"], 12000)
        with patch("tools.adsblol_standalone_runtime.precise_angular_position_from_observer") as los:
            self.bridge.aircraft_los(self.observer.manual_position, (7, 8), 1000)
            self.assertEqual(los.call_args.args, ((4, 5), 41, (7, 8), 1035))
        ac["fields"]["geometric_altitude"]["availability"] = "MISSING"
        baro = self.bridge.context(ac, self.observer.resolve(self.now), self.now, 0, "SUN")
        self.assertEqual(baro.geometric_altitude_correction_m, 0)
        self.assertEqual(baro.altitude_source, "ADSBLOL_STANDALONE_BARO_EXPLICIT_QNH")

    def test_fresh_aircraft_reaches_real_solver_and_map_free_publication(self):
        context = canonical_context()
        self.now = context.prediction_base_utc
        self.observer.set_manual_position(context.observer_context.position, self.now)
        self.bridge.geoid = ConstantGeoid()
        self.bridge.body_position = lambda name, when, observer: parallel_sun(when, observer)
        payload = fixture()
        payload["ac"][0].update(lat=context.latitude_deg, lon=context.longitude_deg,
                                alt_baro=context.current_altitude_m / .3048,
                                alt_geom=context.current_altitude_m / .3048,
                                gs=context.groundspeed_kmh / 1.852, track=context.track_deg, seen_pos=0)
        report = self.report(payload)
        report["response_latency_seconds"] = 0
        # Exercise the actual HTTP adapter/parser, not just a pre-normalized object.
        self.poller.provider = AdsbLolProvider(
            session=Mock(get=Mock(return_value=response(payload))), max_retries=0,
            now=lambda: self.now, monotonic=lambda: self.mono)
        self.poller.fetch_once()
        self.bridge.step()
        self.poller.provider = self.provider
        candidate = self.state._live["SUN"]["ABC123"]["candidate"]
        self.assertEqual(candidate.prediction_geometry, "TRUE_2D")
        self.assertTrue(candidate.encounter_id.endswith(":1"))
        public = self.dashboard.application_state_store.snapshot()
        self.assertNotIn("centerline", json.dumps(public))
        self.assertNotIn("map_privacy", json.dumps(public))
        # A healthy snapshot is not a missing prediction between polls.
        self.now += dt.timedelta(seconds=5)
        self.mono += 5
        self.bridge.step()
        self.assertIn("ABC123", self.state._live["SUN"])
        self.acquire(report)
        self.assertEqual(self.state._live["SUN"]["ABC123"]["candidate"].encounter_id, candidate.encounter_id)
        empty = dict(report, aircraft=[])
        self.acquire(empty)
        self.assertNotIn("ABC123", self.state._live["SUN"])
        self.acquire(report)
        self.assertTrue(self.state._live["SUN"]["ABC123"]["candidate"].encounter_id.endswith(":2"))

    def test_source_label_public_contract_has_no_coordinate_leak(self):
        self.acquire()
        snapshot = self.dashboard.application_state_store.snapshot()
        public = serialize_bootstrap(snapshot, {"revision": 0, "values": {}, "capabilities": {}}, {}, self.now)
        self.assertEqual(public["aircraft_source"]["mode"], "ADSBLOL_STANDALONE")
        self.assertIn("UNKNOWN_FIELD_AGES", public["aircraft_source"]["trust_policy"])
        self.assertNotIn("lat", json.dumps(public["aircraft_source"]))

    def test_local_runtime_has_no_reference_to_standalone_state(self):
        # Separate entry point: not merely gating writes to a shared plane_dict.
        script = ("import sys; import tools.adsblol_standalone_runtime; "
                  "assert 'transit_warning' not in sys.modules; "
                  "assert 'raw_adsb_track' not in sys.modules; "
                  "assert 'mlat_beast_track' not in sys.modules")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_sbs_raw_and_mlat_updates_cannot_overwrite_bridge_inputs(self):
        import transit_warning as local
        from raw_adsb_track import RawAdsbTrack
        from tests.test_motion_freshness import sbs
        self.acquire()
        before = copy.deepcopy(self.bridge.aircraft)
        with patch.dict(local.plane_dict, clear=True), patch.dict(local.aircraft_motion_states, clear=True), patch.dict(
                local.raw_adsb_tracks, clear=True), patch.object(local, "adsb_timestamp_validator", None), patch.object(
                local, "adsb_timestamp_timezone", "UTC"), patch.object(local, "current_observer_context",
                return_value=self.observer.resolve(self.now)), patch.object(
                local, "tabela_for_observer", return_value=(0, 0, 0, 0)):
            local.process_line(sbs("MSG", 4, "2026/09/06 12:00:00.000", groundspeed=999, track=123),
                               local.adsb_port)
            local._update_motion_parameter("ABC123", "track", 234, self.now, local.mlat_port)
            decoded = RawAdsbTrack("ABC123", 321, 1, 1, 1, 0, None, False, "GNSS", None)
            local.update_raw_adsb_track(decoded, self.now)
            self.assertEqual(self.bridge.aircraft, before)

    def test_canonical_real_ephemeris_and_los_match_existing_runtime(self):
        import transit_warning as local
        observer = self.observer.manual_position
        for body in ("sun", "moon"):
            expected = local.body_position_at_utc(body, self.now, observer)
            actual = self.bridge.body_position(body, self.now, observer)
            self.assertEqual(actual.azimuth_deg, expected.azimuth_deg)
            self.assertEqual(actual.altitude_deg, expected.altitude_deg)
        with patch.object(local, "aircraft_los_geoid_provider", self.bridge.geoid):
            expected = local.precise_aircraft_angular_position_from_observer(
                observer.coordinates, observer.elevation_m, (7, 8), 1000)
            actual = self.bridge.aircraft_los(observer, (7, 8), 1000)
            self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
