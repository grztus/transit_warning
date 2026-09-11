"""Regression coverage for synchronous SBS cleanup publication amplification."""
import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import transit_warning as transit
from app_backend.state import ApplicationStateStore
from app_backend.sse import SseBroker, live_envelope
from live_dashboard import DashboardCandidate, DashboardRuntime, DashboardState, DisabledDashboard


NOW = datetime.datetime(2026, 9, 7, 12, tzinfo=datetime.timezone.utc)


class CleanupPublicationTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.history = Mock()
        self.history.query.return_value = {"records": []}
        self.history.append.return_value = True
        self.state = DashboardState(history_store=self.history,
            finalization_journal=SimpleNamespace(append=self.records.append))
        self.app = ApplicationStateStore()
        self.broker = SseBroker()
        self.app.subscribe(lambda snapshot: self.broker.publish(live_envelope(snapshot)))
        self.dashboard = DashboardRuntime(self.state, application_state_store=self.app)
        self.publications = self.enterContext(patch.object(
            self.app, "publish", wraps=self.app.publish))
        self.enterContext(patch.object(transit, "dashboard_runtime", self.dashboard))
        self.enterContext(patch.object(transit, "clock", SimpleNamespace(now_utc=lambda: NOW)))
        for name in ("plane_dict", "altitude_sources", "aircraft_motion_states",
                     "raw_adsb_tracks", "raw_adsb_versions", "gnss_altitude_states",
                     "mlat_beast_tracks", "mlat_coarse_tracks", "aircraft_intent_states",
                     "aircraft_motion_freshness_status", "sun_prediction_last_valid",
                     "moon_prediction_last_valid", "sun_predicted_transit_utc",
                     "moon_predicted_transit_utc", "transit_solver_diagnostics",
                     "vertical_transit_diagnostics", "geometric_altitude_selections",
                     "authoritative_terminal_predictions"):
            self.enterContext(patch.object(transit, name, {}))
        self.shadow = self.enterContext(patch.object(transit, "withdraw_shadow_2d"))
        self.cancel = self.enterContext(patch.object(transit, "cancel_pending_transit_notification"))
        self.drop = self.enterContext(patch.object(transit, "drop_transit_snapshot_buffer"))
        self.lifecycle = self.enterContext(patch.object(transit, "authoritative_transit_lifecycle"))
        self.lifecycle.discard_aircraft_transitions.side_effect = lambda icao: (icao,)
        self.recorder = self.enterContext(patch.object(transit, "observe_candidate_authoritative_transition"))

    def seed_expired(self, count):
        for i in range(count):
            transit.plane_dict[f"{i:06X}"] = [NOW - datetime.timedelta(seconds=90), "TEST"]

    def candidate(self, icao, body="SUN", seconds=0):
        self.state.publish(DashboardCandidate(
            body=body, icao=icao, callsign="TEST", body_azimuth_deg=120.,
            body_elevation_deg=20., aircraft_elevation_deg=21., distance_km=10.,
            telegram_range=False, predicted_event_utc=NOW + datetime.timedelta(seconds=seconds),
            separation_deg=1., last_prediction_update_utc=NOW))

    def test_thirty_expired_without_candidates_publish_nothing(self):
        self.seed_expired(30)
        transit.clean_dict()
        self.assertEqual({}, transit.plane_dict)
        self.publications.assert_not_called()
        self.assertEqual(30, self.recorder.call_count)

    def test_batch_preserves_every_finalization_and_publishes_once(self):
        self.seed_expired(30)
        self.candidate("000000")
        self.candidate("000000", "MOON")
        self.candidate("000001", seconds=120)  # Existing early-withdrawal policy.
        transit.clean_dict()
        self.publications.assert_called_once()
        self.assertEqual({}, transit.plane_dict)
        self.assertEqual(3, len(self.records))
        self.assertEqual(["AIRCRAFT_EXPIRED"] * 3, [r["reason"] for r in self.records])
        self.assertEqual(["PERSISTED", "PERSISTED", "SKIPPED"],
                         [r["history_decision"] for r in self.records])
        self.assertEqual(2, self.history.append.call_count)
        for action in (self.shadow, self.cancel, self.drop,
                       self.lifecycle.discard_aircraft_transitions, self.recorder):
            self.assertEqual(30, action.call_count)
        self.assertEqual([], self.app.snapshot()["state"]["bodies"]["sun"]["candidates"])
        self.assertEqual(2, len(self.app.snapshot()["state"]["recent_events"]))

    def test_repeated_old_sbs_messages_do_not_publish(self):
        self.enterContext(patch.object(transit, "local_aircraft_source_enabled", return_value=True))
        self.enterContext(patch.object(transit, "current_observer_context", return_value=
            SimpleNamespace(position=SimpleNamespace(coordinates=(0., 0., 0.)))))
        old = NOW - datetime.timedelta(seconds=90)
        self.enterContext(patch.object(transit, "port_timestamp_to_utc", return_value=old))
        self.enterContext(patch.object(transit, "tabela_for_observer", return_value=(0, 0, 0, 0)))
        self.enterContext(patch.object(transit, "clean_transit_dict"))
        self.enterContext(patch.object(transit, "capture_transit_observation"))
        self.enterContext(patch.object(transit, "last_update_time", None))
        line = "MSG,1,1,1,ABC123,1,2026/09/07,11:58:30.000,2026/09/07,11:58:30.000,TEST"
        for _ in range(100):
            transit.process_line(line, transit.mlat_port)
            self.assertNotIn("ABC123", transit.plane_dict)
        self.assertEqual(100, self.recorder.call_count)
        self.publications.assert_not_called()

    def test_single_real_withdrawal_publishes_once_then_no_op(self):
        self.candidate("ABC123")
        self.candidate("ABC123", "MOON")
        self.assertTrue(self.dashboard.withdraw_aircraft("ABC123", NOW))
        self.assertFalse(self.dashboard.withdraw_aircraft("ABC123", NOW))
        self.publications.assert_called_once()
        self.assertEqual(2, len(self.records))

    def test_motion_stale_no_op_and_real_withdrawal(self):
        details = {"motion_freshness": {"position_age_seconds": 90}}
        self.assertFalse(self.dashboard.withdraw_aircraft(
            "ABC123", NOW, reason="MOTION_STALE", details=details))
        self.publications.assert_not_called()
        self.candidate("ABC123")
        self.assertTrue(self.dashboard.withdraw_aircraft(
            "ABC123", NOW, reason="MOTION_STALE", details=details))
        self.publications.assert_called_once()
        self.assertEqual("MOTION_STALE", self.records[0]["reason"])
        self.assertEqual(details, self.records[0]["triggering_details"])

    def test_single_body_no_op_suppressed(self):
        self.assertFalse(self.dashboard.withdraw("ABC123", "SUN", NOW))
        self.publications.assert_not_called()
        self.candidate("ABC123")
        self.assertTrue(self.dashboard.withdraw("ABC123", "SUN", NOW))
        self.publications.assert_called_once()

    def test_completed_withdrawal_published_if_later_cleanup_fails(self):
        self.seed_expired(2)
        self.candidate("000000")
        self.drop.side_effect = RuntimeError("cleanup failure")
        with self.assertRaisesRegex(RuntimeError, "cleanup failure"):
            transit.clean_dict()
        self.publications.assert_called_once()
        self.assertEqual(1, len(self.records))

    def test_disabled_dashboard_accepts_batch_keyword(self):
        self.assertFalse(DisabledDashboard().withdraw_aircraft("ABC123", NOW, publish=False))

    def test_local_cleanup_preserves_remote_replacement_with_same_encounter(self):
        self.seed_expired(1)
        self.candidate("000000")
        owner = object()
        candidate = self.state._live["SUN"]["000000"]["candidate"]
        self.state.publish(candidate, source_owner=owner)
        transit.clean_dict()
        self.assertIs(owner, self.state._live["SUN"]["000000"]["source_owner"])
        self.assertEqual([], self.records)
        self.history.append.assert_not_called()
        self.publications.assert_not_called()
        self.assertEqual({}, transit.plane_dict)

    def test_local_prediction_withdrawal_preserves_remote_replacement(self):
        self.candidate("ABC123")
        candidate = self.state._live["SUN"]["ABC123"]["candidate"]
        self.state.publish(candidate, source_owner=object())
        transit.clear_transit_prediction_state("ABC123", [""] * 32, "sun", 18)
        self.assertIn("ABC123", self.state._live["SUN"])
        self.assertEqual([], self.records)
        self.publications.assert_not_called()

    def test_remote_cleanup_preserves_local_replacement_and_publishes_no_op_never(self):
        owner = object()
        self.candidate("ABC123")
        candidate = self.state._live["SUN"]["ABC123"]["candidate"]
        self.state.publish(candidate, source_owner=owner)
        self.state.publish(candidate)
        self.assertFalse(self.dashboard.withdraw_source(
            "ABC123", "SUN", owner, NOW, reason="MOTION_STALE"))
        self.assertFalse(self.dashboard.invalidate_source(owner, NOW))
        self.assertIn("ABC123", self.state._live["SUN"])
        self.assertEqual([], self.records)
        self.publications.assert_not_called()

    def test_diagnostic_and_history_failures_do_not_interrupt_batch_cleanup(self):
        self.seed_expired(2)
        self.candidate("000000")
        self.candidate("000001")
        self.state.finalization_journal = Mock()
        self.state.finalization_journal.append.side_effect = OSError("journal unavailable")
        self.history.append.side_effect = OSError("history unavailable")
        transit.clean_dict()
        self.assertEqual({}, transit.plane_dict)
        self.assertEqual({}, self.state._live["SUN"])
        self.assertEqual(2, len(self.state._history))
        self.assertEqual(2, self.recorder.call_count)
        self.assertEqual(2, self.drop.call_count)
        self.publications.assert_called_once()

    def test_completed_mutation_is_visible_if_later_cleanup_fails(self):
        self.seed_expired(2)
        self.candidate("000000")
        self.candidate("000001")
        self.dashboard._publish_application_state()
        self.publications.reset_mock()
        self.drop.side_effect = RuntimeError("later failure")
        with self.assertRaisesRegex(RuntimeError, "later failure"):
            transit.clean_dict()
        candidates = self.app.snapshot()["state"]["bodies"]["sun"]["candidates"]
        self.assertEqual(["000001"], [item["icao"] for item in candidates])
        self.assertEqual(1, len(self.app.snapshot()["state"]["recent_events"]))
        self.publications.assert_called_once()

    def test_post_transit_cleanup_batches_and_preserves_remote_owner(self):
        self.seed_expired(3)
        for entry in transit.plane_dict.values():
            entry.extend([""] * 30)
            entry[30] = NOW - datetime.timedelta(seconds=121)
            entry[31] = True
        for icao in transit.plane_dict:
            self.candidate(icao)
        candidate = self.state._live["SUN"]["000002"]["candidate"]
        self.state.publish(candidate, source_owner=object())
        transit.clean_transit_dict()
        self.assertEqual({}, transit.plane_dict)
        self.assertEqual({"000002"}, set(self.state._live["SUN"]))
        self.assertEqual(2, len(self.records))
        self.publications.assert_called_once()
