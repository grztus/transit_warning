import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from transit_clock import ReplayClock, RealClock
from app_backend.settings import RuntimeSettingsStore
import transit_warning as transit


class FakeDashboard:
    def __init__(self):
        self.state = self
        self.sources = []
        self.invalidations = 0

    def set_aircraft_source(self, value):
        self.sources.append(dict(value))

    def _publish_application_state(self):
        pass

    def invalidate_live(self, now_utc=None, reason="OBSERVER_INVALIDATED", details=None):
        self.invalidations += 1


class AircraftSourceModeTests(unittest.TestCase):
    def setUp(self):
        self.old = (transit.aircraft_source_mode, transit.dashboard_runtime,
                    transit.internet_source_poller,
                    transit.internet_source_bridge,
                    transit.aircraft_los_geoid_provider)
        transit.aircraft_source_mode = "LOCAL"
        transit.internet_source_poller = None
        transit.internet_source_bridge = None
        transit.dashboard_runtime = FakeDashboard()
        transit.plane_dict.clear()
        transit.raw_adsb_tracks.clear()

    def tearDown(self):
        (transit.aircraft_source_mode, transit.dashboard_runtime,
         transit.internet_source_poller, transit.internet_source_bridge,
         transit.aircraft_los_geoid_provider) = self.old

    def test_auto_keeps_local_and_starts_provider(self):
        poller = Mock()
        with patch.object(transit, "SnapshotPoller", return_value=poller):
            transit.set_aircraft_source_mode("AUTO")
        poller.start.assert_called_once_with()
        self.assertTrue(transit.local_aircraft_source_enabled())
        status = transit.dashboard_runtime.sources[-1]
        self.assertEqual("LOCAL+ADSBLOL", status["effective_mode"])
        self.assertEqual("WAITING", status["status"])

    def test_internet_starts_provider_and_local_stops_it(self):
        transit.aircraft_los_geoid_provider = object()
        poller, bridge = Mock(), Mock()
        with patch.object(transit, "SnapshotPoller", return_value=poller), \
                patch.object(transit, "ProductionInternetBridge", return_value=bridge):
            transit.set_aircraft_source_mode("INTERNET")
            poller.start.assert_called_once_with()
            bridge.step.assert_called_once_with()
            self.assertFalse(transit.local_aircraft_source_enabled())
            transit.set_aircraft_source_mode("LOCAL")
        poller.close.assert_called_once_with()

    def test_switch_clears_local_state_and_gates_raw_updates(self):
        transit.plane_dict["LOCAL1"] = [None] * 32
        transit.raw_adsb_tracks["LOCAL1"] = object()
        transit.aircraft_los_geoid_provider = None
        transit.set_aircraft_source_mode("INTERNET")
        self.assertEqual({}, transit.plane_dict)
        decoded = SimpleNamespace(icao="NET001", track_deg=123.0)
        transit.update_raw_adsb_track(
            decoded, dt.datetime.now(dt.timezone.utc))
        self.assertNotIn("NET001", transit.raw_adsb_tracks)
        self.assertEqual("ERROR", transit.dashboard_runtime.sources[-1]["status"])

    def test_internet_gates_sbs_mlat_and_precision_mlat(self):
        transit.aircraft_source_mode = "INTERNET"
        transit.process_line("local SBS must not be parsed", transit.adsb_port)
        transit._update_motion_parameter(
            "MLAT01", "track", 42, dt.datetime.now(dt.timezone.utc),
            transit.mlat_port)
        transit.update_mlat_beast_track(
            SimpleNamespace(icao="MLAT02"), dt.datetime.now(dt.timezone.utc))
        self.assertEqual({}, transit.plane_dict)
        self.assertNotIn("MLAT01", transit.aircraft_motion_states)
        self.assertNotIn("MLAT02", transit.mlat_beast_tracks)

    def test_mode_transition_clears_auto_snapshot_and_fusion_state(self):
        transit.adsblol_auto_cache.snapshot_id = "old"
        transit.adsblol_auto_cache.aircraft = {"ABC123": {}}
        transit.fused_aircraft_states["ABC123"] = object()
        transit.set_aircraft_source_mode("LOCAL")
        self.assertIsNone(transit.adsblol_auto_cache.snapshot_id)
        self.assertEqual({}, transit.adsblol_auto_cache.aircraft)
        self.assertEqual({}, transit.fused_aircraft_states)


    def test_replay_overrides_each_requested_mode_without_rewriting_preference(self):
        for requested in ("LOCAL", "INTERNET", "AUTO"):
            with self.subTest(requested=requested):
                replay = ReplayClock()
                preference = RuntimeSettingsStore(aircraft_source_requested_mode=requested)
                with patch.object(transit, "clock", replay), patch.object(transit, "SnapshotPoller") as provider:
                    # Startup has no replay timestamp yet and must still be safe.
                    transit.set_aircraft_source_mode(requested)
                    provider.assert_not_called()
                    self.assertTrue(transit.local_aircraft_source_enabled())
                    status = transit.dashboard_runtime.sources[-1]
                    self.assertEqual(requested, status["requested_mode"])
                    self.assertEqual("LOCAL", status["effective_mode"])
                    self.assertEqual("REPLAY", status["status"])
                    self.assertFalse(replay.is_ready())
                    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
                    replay.advance_to(now)
                    transit._update_motion_parameter("REPLAY1", "track", 42., now, transit.adsb_port)
                    self.assertEqual(42., transit.aircraft_motion_states["REPLAY1"].track.value)
                    self.assertIsNone(transit.internet_source_bridge)
                self.assertEqual(requested, preference.snapshot()["values"]["aircraft_source"]["requested_mode"])

    def test_leaving_replay_restores_each_requested_real_clock_mode(self):
        transit.aircraft_los_geoid_provider = object()
        for requested in ("LOCAL", "INTERNET", "AUTO"):
            with self.subTest(requested=requested):
                with patch.object(transit, "clock", ReplayClock()):
                    transit.set_aircraft_source_mode(requested)
                poller = Mock()
                with patch.object(transit, "clock", RealClock()), \
                        patch.object(transit, "SnapshotPoller", return_value=poller) as provider, \
                        patch.object(transit, "ProductionInternetBridge"), \
                        patch.object(transit, "AutoFusionBridge"):
                    transit.set_aircraft_source_mode(requested)
                    self.assertEqual(requested, transit.aircraft_source_mode)
                    self.assertEqual(requested != "INTERNET", transit.local_aircraft_source_enabled())
                    if requested == "LOCAL":
                        provider.assert_not_called()
                    else:
                        poller.start.assert_called_once_with()
