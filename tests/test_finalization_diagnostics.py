import datetime
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from dashboard_history import DashboardHistoryStore
from finalization_diagnostics import FinalizationJournal
from live_dashboard import DashboardCandidate, DashboardState, MobileGpsState
from observer_position import ObserverPosition, RuntimeObserverPositionProvider

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 7, 8, 6, 40, tzinfo=UTC)


class FinalizationTests(unittest.TestCase):
    def test_diagnostic_failure_preserves_withdrawal_and_observer_invalidation(self):
        import transit_warning as runtime
        journal = Mock()
        journal.append.side_effect = ValueError("diagnostic failure")
        candidate = DashboardCandidate(body='SUN', icao='ABC123', callsign='TEST',
            body_azimuth_deg=130., body_elevation_deg=35., aircraft_elevation_deg=36.,
            distance_km=14., telegram_range=False, predicted_event_utc=NOW,
            separation_deg=1., last_prediction_update_utc=NOW)
        state = DashboardState(finalization_journal=journal)
        state.publish(candidate)
        self.assertTrue(state.withdraw('ABC123', 'SUN', NOW))
        self.assertEqual(1, len(state._history))
        state.publish(candidate)
        state.invalidate_live(NOW)
        self.assertEqual({}, state._live['SUN'])
        dashboard = Mock()
        with patch.object(runtime, 'dashboard_runtime', dashboard), \
                patch.object(runtime, 'plane_dict', {}), \
                patch.object(runtime, 'authoritative_transit_lifecycle') as lifecycle:
            lifecycle.invalidate_transitions.return_value = ()
            runtime.invalidate_observer_dependent_state(object())
            dashboard.invalidate_live.assert_called_once()

    def test_mobile_stale_withdrawal_and_frozen_history_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = FinalizationJournal(Path(directory) / 'diagnostics')
            history = DashboardHistoryStore(Path(directory) / 'history')
            state = DashboardState(history_store=history, finalization_journal=journal)
            gps = MobileGpsState(enabled=True)
            gps.update(dict(latitude=50., longitude=20., accuracy=5., timestamp=1.), NOW)
            provider = RuntimeObserverPositionProvider(ObserverPosition(0., 0., 100.), mode='MOBILE')
            provider.attach_mobile_state(gps)
            candidate = DashboardCandidate(body='SUN', icao='ABC123', callsign='TEST',
                body_azimuth_deg=130., body_elevation_deg=35., aircraft_elevation_deg=36.,
                distance_km=14., telegram_range=False,
                predicted_event_utc=NOW + datetime.timedelta(seconds=59),
                separation_deg=1.6, last_prediction_update_utc=NOW,
                encounter_id='1:ABC123:SUN:1', prediction_geometry='TRUE_2D')
            state.publish(candidate)
            detail = {'position_age_seconds': 11.}
            state.withdraw_aircraft('ABC123', NOW + datetime.timedelta(seconds=11),
                                    reason='MOTION_STALE', details=detail)
            detail['position_age_seconds'] = 0.
            journal.close()
            record = json.loads(next((Path(directory) / 'diagnostics').glob('*.jsonl')).read_text())
            self.assertEqual('MOTION_STALE', record['reason'])
            self.assertEqual('EARLY_WITHDRAWAL', record['history_reason'])
            self.assertEqual('SKIPPED', record['history_decision'])
            self.assertEqual(11., record['triggering_details']['position_age_seconds'])
            self.assertNotIn('prediction_context', record)
            self.assertEqual([], history.query()['records'])

    def test_passed_and_observer_invalidation_decisions(self):
        records = []
        with tempfile.TemporaryDirectory() as directory:
            state = DashboardState(history_store=DashboardHistoryStore(directory),
                finalization_journal=SimpleNamespace(append=records.append))
            candidate = DashboardCandidate(body='SUN', icao='ABC123', callsign='TEST',
                body_azimuth_deg=130., body_elevation_deg=35., aircraft_elevation_deg=36.,
                distance_km=14., telegram_range=False,
                predicted_event_utc=NOW, separation_deg=1., last_prediction_update_utc=NOW,
                encounter_id='1:ABC123:SUN:1')
            state.publish(candidate)
            state.tick(NOW)
            self.assertEqual('PERSISTED', records[-1]['history_decision'])
            self.assertTrue(records[-1]['history_succeeded'])
            state.publish(replace(candidate, encounter_id='2:ABC123:SUN:1'))
            state.invalidate_live(NOW)
            self.assertEqual('OBSERVER_INVALIDATED', records[-1]['reason'])
            self.assertEqual('SKIPPED', records[-1]['history_decision'])

    def test_mobile_coordinates_change_geometry_without_epoch_changes(self):
        gps = MobileGpsState(enabled=True)
        changes = []
        provider = RuntimeObserverPositionProvider(ObserverPosition(0., 0., 100.),
            mode='MOBILE', change_handler=changes.append)
        provider.attach_mobile_state(gps)
        contexts = []
        for latitude in (50., 50.00001, 49.99999, 50.001, 50.1):
            gps.update(dict(latitude=latitude, longitude=20., accuracy=5., timestamp=1.), NOW)
            contexts.append(provider.resolve(NOW))
        self.assertEqual(1, len({item.epoch for item in contexts}))
        self.assertEqual(5, len({item.position for item in contexts}))
        self.assertTrue(all(item.mobile_accuracy_m == 5. for item in contexts))
        self.assertEqual([], changes)
        stale = provider.resolve(NOW + datetime.timedelta(seconds=16))
        self.assertEqual('MOBILE_LAST_KNOWN', stale.effective_source)
        self.assertEqual(contexts[-1].epoch + 1, stale.epoch)
        self.assertEqual(contexts[-1].position, stale.position)

    def test_history_failure_is_not_reported_as_persisted(self):
        records = []
        store = SimpleNamespace(query=lambda **_: {'records': []},
                                append=lambda _: False, failed=True)
        state = DashboardState(history_store=store,
            finalization_journal=SimpleNamespace(append=records.append))
        candidate = DashboardCandidate(body='SUN', icao='ABC123', callsign='TEST',
            body_azimuth_deg=130., body_elevation_deg=35., aircraft_elevation_deg=36.,
            distance_km=14., telegram_range=False, predicted_event_utc=NOW,
            separation_deg=1., last_prediction_update_utc=NOW,
            encounter_id='1:ABC123:SUN:1')
        state.publish(candidate)
        state.tick(NOW)
        self.assertEqual('FAILED', records[-1]['history_decision'])
        self.assertTrue(records[-1]['history_attempted'])
        self.assertFalse(records[-1]['history_succeeded'])

    def candidate(self, encounter_id="1:ABC123:SUN:1", seconds=0, separation=1.):
        return DashboardCandidate(body="SUN", icao="ABC123", callsign="TEST",
            body_azimuth_deg=130., body_elevation_deg=35., aircraft_elevation_deg=36.,
            distance_km=14., telegram_range=False,
            predicted_event_utc=NOW + datetime.timedelta(seconds=seconds),
            separation_deg=separation, last_prediction_update_utc=NOW,
            encounter_id=encounter_id)

    def test_replacement_and_duplicate_bind_final_encounter(self):
        records = []
        state = DashboardState(finalization_journal=SimpleNamespace(append=records.append))
        first = self.candidate()
        second = self.candidate("2:ABC123:SUN:1")
        state.publish(first)
        state.publish(second)
        self.assertEqual(first.encounter_id, records[0]["encounter_id"])
        self.assertEqual("PREDICTION_REPLACED", records[0]["reason"])
        self.assertEqual(first.encounter_id, records[0]["last_prediction"]["encounter_id"])
        self.assertEqual([], state._history)
        state.tick(NOW)
        self.assertEqual(second.encounter_id, state._history[0]["event_id"])
        self.assertEqual("PASSED", records[-1]["final_state"])
        self.assertEqual("HISTORY_DISABLED", records[-1]["history_reason"])
        state.publish(second)
        state.tick(NOW)
        self.assertEqual("DUPLICATE_EVENT", records[-1]["history_reason"])
        self.assertEqual(1, len(state._history))

    def test_invisible_candidate_reports_not_history_worthy(self):
        records = []
        state = DashboardState(finalization_journal=SimpleNamespace(append=records.append))
        state.publish(self.candidate(seconds=120, separation=99.))
        state.withdraw("ABC123", "SUN", NOW)
        self.assertEqual("NOT_HISTORY_WORTHY", records[-1]["history_reason"])
        self.assertEqual([], state._history)

    def test_journal_serialization_failure_preserves_replacement_and_reset(self):
        records = []
        state = DashboardState(finalization_journal=SimpleNamespace(append=records.append))
        state.publish(self.candidate())
        with patch.object(state, "_candidate_dict", side_effect=ValueError("diagnostic failure")):
            self.assertTrue(state.publish(self.candidate("2:ABC123:SUN:1")))
            state.invalidate_live(NOW, reason="SOURCE_RESET")
        self.assertEqual({}, state._live["SUN"])

    def test_source_switch_resets_all_owners_without_new_recorder_capture(self):
        import transit_warning as runtime
        from live_dashboard import DashboardRuntime
        records = []
        state = DashboardState(finalization_journal=SimpleNamespace(append=records.append))
        state.publish(self.candidate())
        state.publish(replace(self.candidate("1:DEF456:SUN:1"), icao="DEF456"),
                      source_owner=object())
        with patch.object(runtime, "dashboard_runtime", DashboardRuntime(state)), \
                patch.object(runtime, "plane_dict", {}), \
                patch.object(runtime, "clock", SimpleNamespace(now_utc=lambda: NOW)), \
                patch.object(runtime, "internet_source_poller", None), \
                patch.object(runtime, "internet_source_bridge", None), \
                patch.object(runtime, "aircraft_source_mode", "AUTO"), \
                patch.object(runtime, "authoritative_transit_lifecycle") as lifecycle, \
                patch.object(runtime, "observe_candidate_authoritative_transition") as observe, \
                patch.object(runtime, "transit_snapshot_manager") as snapshots, \
                patch.object(runtime, "session_recorder") as full_session:
            transition = object()
            lifecycle.invalidate_transitions.return_value = (transition,)
            # The reset already removed these identities before dashboard callbacks.
            lifecycle.finalize_encounter.return_value = None
            runtime.set_aircraft_source_mode("LOCAL")
            observe.assert_called_once_with(transition, NOW)
            snapshots.invalidate_active_predictions.assert_called_once_with()
            self.assertEqual([], full_session.mock_calls)
        self.assertEqual({}, state._live["SUN"])
        self.assertEqual(["SOURCE_RESET", "SOURCE_RESET"], [r["reason"] for r in records])
        self.assertEqual([], state._history)

    def test_source_scope_invalidation_is_owned_and_no_history(self):
        from live_dashboard import DashboardRuntime
        from tools.adsblol_standalone_runtime import StandaloneBridge
        records = []
        state = DashboardState(finalization_journal=SimpleNamespace(append=records.append))
        bridge = StandaloneBridge(SimpleNamespace(now=lambda: NOW, monotonic=lambda: 0),
                                  DashboardRuntime(state), object(), source_mode="AUTO")
        state.publish(self.candidate(), source_owner=bridge)
        state.publish(replace(self.candidate(), icao="LOCAL1"))
        bridge._clear(NOW)
        self.assertEqual({"LOCAL1"}, set(state._live["SUN"]))
        self.assertEqual("OBSERVER_INVALIDATED", records[0]["reason"])
        self.assertEqual("SKIPPED", records[0]["history_decision"])
        self.assertEqual([], state._history)

    def test_journal_queue_is_bounded_and_io_failure_is_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("finalization_diagnostics.threading.Thread") as thread:
                journal = FinalizationJournal(directory, capacity=1)
                self.assertTrue(journal.append({"finalized_at_utc": "2026-09-07"}))
                self.assertFalse(journal.append({"finalized_at_utc": "2026-09-07"}))
                self.assertEqual(1, journal.dropped)
                thread.return_value.start.assert_called_once()
            path = Path(directory) / "not-a-directory"
            path.write_text("blocked", encoding="utf-8")
            journal = FinalizationJournal(path)
            journal.append({"finalized_at_utc": "2026-09-07"})
            journal.close()
            self.assertEqual(1, journal.failed)
