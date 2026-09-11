import unittest
from unittest.mock import Mock, patch

import transit_warning as transit
from observer_position import ObserverPosition, RuntimeObserverPositionProvider
from tests.test_adsblol_live import NOW, fixture
from source_geometry_fixture import ConstantGeoid
from tools.adsblol_live import normalize_response
from tools.adsblol_standalone_runtime import SnapshotPoller, StandaloneBridge


class AutoFusionBridgeTests(unittest.TestCase):
    def setUp(self):
        self.monotonic = 100.0
        self.observer = RuntimeObserverPositionProvider(
            ObserverPosition(1, 2, 3), mode="MANUAL",
            manual_position=ObserverPosition(4, 5, 6))
        self.provider = Mock()
        self.poller = SnapshotPoller(
            self.observer, provider=self.provider,
            monotonic=lambda: self.monotonic, now=lambda: NOW)
        self.report = dict(
            normalize_response(fixture(), NOW, self.monotonic), status="OK",
            retry_after_seconds=None, response_latency_seconds=.1,
            request_finished_monotonic=self.monotonic)
        self.provider.acquire.return_value = self.report
        self.poller.fetch_once()
        transit.adsblol_auto_cache.clear()
        transit.fused_aircraft_states.clear()
        transit.adsblol_auto_cache.consume(self.report, self.monotonic)

    def tearDown(self):
        transit.adsblol_auto_cache.clear()
        transit.fused_aircraft_states.clear()

    def test_internet_only_aircraft_builds_auto_predictor_context(self):
        bridge = transit.AutoFusionBridge(
            self.poller, Mock(), ConstantGeoid(35))
        aircraft = self.report["aircraft"][0]

        context = bridge.context(
            aircraft, self.observer.resolve(NOW), NOW,
            aircraft["fields"]["position"]["age_seconds"], "SUN")

        self.assertEqual("ABC123", context.icao)
        self.assertEqual("ADSBLOL", context.position_source)
        self.assertTrue(context.track_source.endswith("_UNKNOWN_AGE"))
        self.assertTrue(context.fusion_provenance["predictor"]["freeze_altitude"])
        state = transit.fused_aircraft_states["ABC123"]
        self.assertEqual("ADSBLOL_ONLY",
                         state.fields["position"].selection_reason)

    def test_internet_and_auto_both_run_same_internet_only_fixture(self):
        standalone = StandaloneBridge(
            self.poller, Mock(), ConstantGeoid(35), solver=Mock(return_value=None))
        auto = transit.AutoFusionBridge(
            self.poller, Mock(), ConstantGeoid(35))
        auto.solver = Mock(return_value=None)

        standalone.step()
        auto.step()

        self.assertEqual({"ABC123"}, set(standalone.aircraft))
        self.assertEqual({"ABC123"}, set(auto.aircraft))
        self.assertEqual(0, standalone.failures)
        self.assertEqual(0, auto.failures)
        self.assertEqual(2, standalone.solver.call_count)
        self.assertEqual(2, auto.solver.call_count)


if __name__ == "__main__":
    unittest.main()
