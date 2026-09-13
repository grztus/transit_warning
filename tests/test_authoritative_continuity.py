"""Approved LOCAL continuity policy through lifecycle and production consumers."""
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
import unittest
import threading
from unittest.mock import Mock, patch

import transit_warning as r
from authoritative_transit import AuthoritativeTransitLifecycle, SOLVE_CONTINUITY_SECONDS
from candidate_recorder import CandidateEncounterManager
from live_dashboard import DisabledDashboard
from telegram_notifications import TelegramNotifier
from test_authoritative_transit import BASE, context, result
import test_deferred_runtime as fixtures
import test_source_integration as sources
import test_motion_stale_live_grace as synchronous


class ContinuityLifecycleTests(unittest.TestCase):
    def manager(self):
        return AuthoritativeTransitLifecycle('TRUE_2D', grace_seconds=3,
                                            continuity_seconds=SOLVE_CONTINUITY_SECONDS)

    def test_gap_matrix_and_intermittent_failures_never_extend_last_success(self):
        for gap in (2.999, 3, 3.001, 4, 9, 9.999, 10, 10.001, 30):
            for unavailable in (True, False):
                with self.subTest(gap=gap, unavailable=unavailable):
                    lifecycle = self.manager()
                    first = lifecycle.consider(context(), result(), BASE)
                    transitions = []
                    for seconds in sorted({min(1, gap), min(3, gap), gap}):
                        now = BASE + dt.timedelta(seconds=seconds)
                        transition = (lifecycle.unavailable_transition(4, 'ABC123', 'MOON', now)
                            if unavailable else lifecycle.consider_transition(context(), result(succeeded=False), now))
                        transitions.append(transition)
                        if transition.kind.value == 'HELD':
                            self.assertIs(first, transition.prediction)
                            self.assertEqual('SOLVE_UNAVAILABLE', transition.prediction_quality_reason)
                            self.assertEqual(BASE, transition.prediction.updated_at_utc)
                    next_prediction = lifecycle.consider(context(), result(), now)
                    self.assertEqual(1 if gap < 10 else 2, next_prediction.encounter_generation)
                    self.assertEqual(0 if gap < 10 else 1,
                                     sum(t.kind.value == 'WITHDRAWN' for t in transitions))

    def test_success_after_unobserved_expiry_opens_new_generation(self):
        lifecycle = self.manager()
        first = lifecycle.consider(context(), result(), BASE)
        second = lifecycle.consider(context(), result(), BASE + dt.timedelta(seconds=10))
        self.assertNotEqual(first.encounter_id, second.encounter_id)

    def test_passed_previous_event_and_explicit_invalidation_end_identity(self):
        for invalidate in (False, True):
            with self.subTest(invalidate=invalidate):
                lifecycle = self.manager()
                first = lifecycle.consider(context(), result(seconds=2), BASE)
                if invalidate:
                    lifecycle.invalidate()
                second = lifecycle.consider(context(), result(), BASE + dt.timedelta(seconds=2))
                self.assertNotEqual(first.encounter_id, second.encounter_id)

    def test_obsolete_exact_cannot_open_or_refresh_a_continuous_encounter(self):
        lifecycle = self.manager()
        self.assertIsNone(lifecycle.consider(context(), result(seconds=1), BASE + dt.timedelta(seconds=2)))
        first = lifecycle.consider(context(), result(), BASE)
        held = lifecycle.consider(context(), result(seconds=1), BASE + dt.timedelta(seconds=4))
        self.assertIs(first, held)
        self.assertEqual(BASE, held.updated_at_utc)


class ContinuityRuntimeTests(unittest.TestCase):
    idle = fixtures.DeferredRuntimeTests.idle
    pause_worker = fixtures.DeferredRuntimeTests.pause_worker

    def setUp(self):
        fixtures.DeferredRuntimeTests.setUp(self)
        r.authoritative_transit_lifecycle = AuthoritativeTransitLifecycle(
            'TRUE_2D', continuity_seconds=SOLVE_CONTINUITY_SECONDS)
        self.now = fixtures.NOW
        self.enterContext(patch.object(r, 'clock', SimpleNamespace(now_utc=lambda: self.now)))
        self.events = []
        self.dashboard.state.finalization_journal = SimpleNamespace(append=self.events.append)
        self.dashboard.state.notification_finalized = r.finalize_authoritative_notification
        self.manager = CandidateEncounterManager()
        self.recorder.side_effect = lambda transition, now, *args: self.manager.process_transition(transition, now)

    def advance(self, seconds):
        self.now = fixtures.NOW + dt.timedelta(seconds=seconds)
        r.aircraft_motion_states['ABC123'] = fixtures.motion_state(
            position_age=-seconds, altitude_age=-seconds, track_age=-seconds, groundspeed_age=-seconds)
        r.plane_dict['ABC123'][0] = self.now

    def submit(self):
        with r.plane_dict_lock:
            self.assertTrue(self.service.submit('ABC123', 'TEST', self.observer,
                r.aircraft_motion_states['ABC123'].track, 800, 1000, self.now))

    def candidate(self, body='MOON'):
        return self.dashboard.state._live[body]['ABC123']['candidate']

    def establish(self):
        self.submit()
        self.idle()
        return self.candidate()

    def missing(self, seconds):
        self.advance(seconds)
        self.coarse.side_effect = ValueError('synthetic exact unavailability')
        with self.assertLogs('deferred_prediction', level='WARNING'):
            self.submit()
            self.idle()
        self.coarse.side_effect = None

    def test_recovery_at_nine_seconds_preserves_identity_geometry_history_and_recorder(self):
        first = self.establish()
        for seconds in (1, 3, 4, 8.9):
            self.missing(seconds)
            degraded = self.candidate()
            self.assertEqual(replace(first, prediction_quality='DEGRADED',
                prediction_quality_reason='SOLVE_UNAVAILABLE',
                prediction_expires_utc=fixtures.NOW+dt.timedelta(seconds=10)), degraded)
            self.assertEqual(2, self.telegram.call_count)
            self.assertEqual(2, self.capture.call_count)
            self.assertEqual([], self.events)
        self.advance(9)
        self.submit()
        self.idle()
        recovered = self.candidate()
        self.assertEqual(first.encounter_id, recovered.encounter_id)
        self.assertEqual('FRESH', recovered.prediction_quality)
        self.assertIsNone(recovered.prediction_quality_reason)
        self.assertIsNone(recovered.prediction_expires_utc)
        self.assertEqual([], self.events)
        self.assertEqual([], self.dashboard.state.query_history()['records'])
        self.assertEqual(2, len(self.manager.encounters_for_icao('ABC123')))




    def test_deadline_without_more_solves_withdraws_once_then_new_generation(self):
        first = self.establish()
        self.dashboard.state.mark_history_worthy('ABC123', 'MOON')
        self.missing(4)
        self.advance(10)
        r.clean_dict()
        r.clean_dict()
        self.dashboard.tick(self.now)
        self.assertNotIn('ABC123', self.dashboard.state._live['MOON'])
        self.assertEqual(['PREDICTION_UNAVAILABLE'] * 2, [event['reason'] for event in self.events])
        self.assertEqual(1, len(self.dashboard.state.query_history()['records']))
        self.assertTrue(all(e.outcome.value == 'WITHDRAWN' for e in self.manager.encounters_for_icao('ABC123')))
        self.advance(10.1)
        self.submit()
        self.idle()
        self.assertNotEqual(first.encounter_id, self.candidate().encounter_id)
        self.assertTrue(self.candidate().encounter_id.endswith(':2'))

    def test_dashboard_deadline_closes_internal_identity_before_late_recovery(self):
        self.establish()
        self.missing(4)
        self.advance(10)
        self.dashboard.tick(self.now)
        self.assertIsNone(r.authoritative_transit_lifecycle.active_prediction(7, 'ABC123', 'MOON'))
        self.assertTrue(all(e.outcome.value == 'WITHDRAWN' for e in self.manager.encounters_for_icao('ABC123')))
        self.submit()
        self.idle()
        self.assertTrue(self.candidate().encounter_id.endswith(':2'))
        self.assertEqual(2, len(self.events))

    def test_passage_during_hold_finalizes_once_per_body_and_never_reuses_identity(self):
        self.exact.return_value.tca_seconds = 5
        first = self.establish()
        self.missing(1)
        self.advance(5)
        r.clean_dict()
        self.dashboard.tick(self.now)
        self.dashboard.tick(self.now)
        self.assertEqual(['PASSED', 'PASSED'], [event['reason'] for event in self.events])
        self.assertEqual(2, len(self.dashboard.state.query_history()['records']))
        self.assertNotIn('ABC123', self.dashboard.state._live['MOON'])
        self.assertEqual(2, self.telegram.call_count)
        self.exact.return_value.tca_seconds = 60
        self.submit()
        self.idle()
        self.assertNotEqual(first.encounter_id, self.candidate().encounter_id)

    def test_explicit_observer_invalidation_immediately_ends_hold(self):
        first = self.establish()
        self.missing(4)
        r.invalidate_observer_dependent_state(self.observer)
        self.assertNotIn('ABC123', self.dashboard.state._live['MOON'])
        self.assertIsNone(r.authoritative_transit_lifecycle.active_prediction(7, 'ABC123', 'MOON'))
        self.assertEqual(['OBSERVER_INVALIDATED'] * 2, [event['reason'] for event in self.events])
        self.observer = replace(self.observer, epoch=8)
        self.submit()
        self.idle()
        self.assertNotEqual(first.encounter_id, self.candidate().encounter_id)
        self.assertTrue(self.candidate().encounter_id.startswith('8:'))

    def test_aircraft_incarnation_replacement_ends_hold(self):
        first = self.establish()
        self.missing(4)
        r.plane_dict['ABC123'] = fixtures.plane_entry(self.now, distance=10)
        self.submit()
        self.idle()
        self.assertNotEqual(first.encounter_id, self.candidate().encounter_id)
        self.assertEqual(2, len(self.events))

    def test_hard_motion_invalidation_on_maintenance_ends_hold(self):
        self.establish()
        self.missing(4)
        r.aircraft_motion_states['ABC123'].altitude = None
        r.clean_dict()
        self.assertNotIn('ABC123', self.dashboard.state._live['MOON'])
        self.assertEqual(['MOTION_STALE'] * 2, [event['reason'] for event in self.events])

    def test_aircraft_disappearance_during_hold_does_not_survive_cleanup(self):
        self.establish()
        self.missing(4)
        r.plane_dict['ABC123'][0] = self.now-dt.timedelta(seconds=r.MAX_AGE_SECONDS+1)
        r.clean_dict()
        self.assertNotIn('ABC123', r.plane_dict)
        self.assertIsNone(r.authoritative_transit_lifecycle.active_prediction(7, 'ABC123', 'MOON'))
        self.assertEqual(['AIRCRAFT_EXPIRED'] * 2, [event['reason'] for event in self.events])

    def test_original_velocity_only_regression_with_continuity_enabled(self):
        fixtures.DeferredRuntimeTests.test_mlat_stale_hold_cancels_old_work_recovers_and_readmits_after_expiry(self)

    def test_velocity_stale_reason_replaces_solve_reason_without_extending_deadline(self):
        first = self.establish()
        self.missing(4)
        r.aircraft_motion_states['ABC123'].track = replace(
            r.aircraft_motion_states['ABC123'].track, updated_at_utc=self.now-dt.timedelta(seconds=11))
        r.clean_dict()
        self.assertEqual('VELOCITY_STALE', self.candidate().prediction_quality_reason)
        self.assertEqual(fixtures.NOW+dt.timedelta(seconds=10), self.candidate().prediction_expires_utc)
        self.advance(9)
        self.submit()
        self.idle()
        self.assertEqual(first.encounter_id, self.candidate().encounter_id)
        self.assertEqual('FRESH', self.candidate().prediction_quality)

    def test_deferred_result_captured_before_deadline_cannot_extend_or_resurrect(self):
        self.establish()
        self.missing(4)
        self.advance(9)
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        self.advance(10)
        release.set()
        self.idle()
        self.assertNotIn('ABC123', self.dashboard.state._live['MOON'])
        self.assertEqual(1, self.service.scheduler.snapshot()['stale_discarded'])
        self.assertEqual(2, self.telegram.call_count)
        self.assertEqual(2, self.capture.call_count)
        self.assertEqual(['PREDICTION_UNAVAILABLE'] * 2, [event['reason'] for event in self.events])
        self.coarse.side_effect = None
        self.submit()
        self.idle()
        self.assertTrue(self.candidate().encounter_id.endswith(':2'))

    def test_telegram_pending_and_sent_state_are_not_refreshed_by_holds(self):
        transport = Mock()
        transport.send.return_value = (True, None)
        notifier = TelegramNotifier(transport, stability_seconds=5)
        self.addCleanup(notifier.close)
        self.enterContext(patch.object(r, 'telegram_notifier', notifier))
        self.telegram.side_effect = self.real_telegram_consumer
        self.establish()
        pending = dict(notifier._pending)
        for seconds in (1, 4, 8):
            self.missing(seconds)
            self.assertEqual(pending, notifier._pending)
            self.assertEqual(0, notifier.diagnostics().get('ENQUEUED', 0))
        self.advance(9)
        self.submit()
        self.idle()
        self.assertEqual(2, notifier.diagnostics()['ENQUEUED'])
        deadlines = dict(notifier._events)
        self.missing(13)
        self.assertEqual(deadlines, notifier._events)
        self.advance(14)
        self.submit()
        self.idle()
        self.assertEqual(2, notifier.diagnostics()['ENQUEUED'])

    def test_disabled_dashboard_has_same_hard_expiry_and_recovery(self):
        r.dashboard_runtime = DisabledDashboard()
        self.submit()
        self.idle()
        self.missing(4)
        self.advance(10)
        r.clean_dict()
        self.assertIsNone(r.authoritative_transit_lifecycle.active_prediction(7, 'ABC123', 'MOON'))
        self.submit()
        self.idle()
        self.assertEqual(2, r.authoritative_transit_lifecycle.active_prediction(7, 'ABC123', 'MOON').encounter_generation)

    def test_auto_blocks_solve_only_handoff_but_accepts_real_health_loss(self):
        self.enterContext(patch.object(r, 'aircraft_source_mode', 'AUTO'))
        first = self.establish()
        poller = sources.SnapshotPoller(SimpleNamespace(resolve=lambda now: self.observer),
            provider=Mock(), now=lambda: self.now, monotonic=lambda: 100.)
        report = dict(sources.normalize_response(sources.fixture(), self.now, 100), status='OK')
        self.enterContext(patch.object(r, 'adsblol_auto_cache', r.ProviderSnapshotCache()))
        r.adsblol_auto_cache.consume(report, 100)
        bridge = r.AutoFusionBridge(poller, self.dashboard, sources.ConstantGeoid())
        bridge.aircraft = {'ABC123': report['aircraft'][0]}
        fields = {
            'position': sources.local('position', {'lat': 1, 'lon': 2}, 0),
            'ground_track': sources.local('ground_track', 90, 0),
            'groundspeed': sources.local('groundspeed', 700, 0, unit='km/h'),
            'geometric_altitude': sources.local('geometric_altitude', 10000, 0, unit='m', datum='WGS84_HAE')}
        ctx = context()
        ctx.observer_context = self.observer
        ctx.prediction_base_utc = self.now
        remote = bridge.lifecycle.consider(ctx, result(), self.now)
        with patch.object(r, 'local_fusion_fields', return_value=fields):
            for seconds in (4, 9):
                self.missing(seconds)
                self.assertFalse(bridge._publish(remote, ctx, self.now))
                self.assertEqual(first.encounter_id, self.candidate().encounter_id)
                self.assertIsNone(self.dashboard.state._live['MOON']['ABC123']['source_owner'])
            self.submit()
            self.idle()
            self.assertEqual(first.encounter_id, self.candidate().encounter_id)
            self.assertEqual([], self.events)
            fields['ground_track'] = replace(fields['ground_track'], age_seconds=11)
            self.assertTrue(bridge._publish(remote, ctx, self.now))
            self.assertIs(bridge, self.dashboard.state._live['MOON']['ABC123']['source_owner'])
            self.assertIsNone(r.authoritative_transit_lifecycle.active_prediction(7, 'ABC123', 'MOON'))
            fields['ground_track'] = replace(fields['ground_track'], age_seconds=0)
            self.submit()
            self.idle()
            self.assertTrue(self.candidate().encounter_id.endswith(':2'))
            self.assertFalse(bridge._publish(remote, ctx, self.now))
        self.assertEqual(['PREDICTION_REPLACED'] * 2, [event['reason'] for event in self.events])


class ContinuitySynchronousTests(unittest.TestCase):
    prediction = synchronous.MotionStaleLiveGraceTests.prediction
    send = synchronous.MotionStaleLiveGraceTests.send
    live = synchronous.MotionStaleLiveGraceTests.live

    def setUp(self):
        synchronous.MotionStaleLiveGraceTests.setUp(self)
        r.authoritative_transit_lifecycle = AuthoritativeTransitLifecycle(
            'TRUE_2D', continuity_seconds=SOLVE_CONTINUITY_SECONDS)
        r.shadow_2d_config = r.Shadow2DConfig(enabled=True)
        self.dashboard.state.prediction_finalized = r.finalize_authoritative_encounter
        self.coarse = self.enterContext(patch.object(r, 'shadow_coarse_screen',
            return_value=SimpleNamespace(passed=True)))
        self.enterContext(patch.object(r, 'shadow_exact_refine', return_value=result().exact))
        self.enterContext(patch.object(r, 'emit_authoritative_transit_notification', return_value=False))
        self.enterContext(patch.object(r, 'capture_authoritative_transit_prediction'))
        self.enterContext(patch.object(r, 'select_geometric_altitude_for_prediction',
            return_value=SimpleNamespace(altitude_m=1000, source=SimpleNamespace(value='BARO'))))
        self.enterContext(patch.object(r, 'internet_source_poller', None))
        self.enterContext(patch.object(r, 'internet_source_bridge', None))
        r.set_aircraft_source_mode('LOCAL')

    def test_synchronous_none_adapter_recovers_without_new_identity(self):
        self.send(0, track=180, speed=450)
        first = self.live()[0]
        with patch.object(r, 'complete_shadow_2d', return_value=None):
            for seconds in (1, 3, 4, 9):
                self.send(seconds, track=180, speed=450)
                held = self.live()[0]
                self.assertEqual(first['encounter_id'], held['encounter_id'])
                self.assertEqual('SOLVE_UNAVAILABLE', held['prediction_quality_reason'])
                self.assertEqual(first['last_prediction_update_utc'], held['last_prediction_update_utc'])
        self.send(9.1, track=180, speed=450)
        self.assertEqual(first['encounter_id'], self.live()[0]['encounter_id'])
        self.assertEqual('FRESH', self.live()[0]['prediction_quality'])
        self.assertEqual([], self.events)

    def test_synchronous_prediction_and_terminal_tick_share_aircraft_lock(self):
        with patch.object(r, 'shadow_exact_refine', return_value=result(seconds=5).exact):
            self.send(0, track=180, speed=450)
            first = self.live()[0]
            started, finished = threading.Event(), threading.Event()
            def tick():
                started.set()
                self.dashboard.tick(synchronous.NOW+dt.timedelta(seconds=5))
                finished.set()
            with r.plane_dict_lock:
                worker = threading.Thread(target=tick)
                worker.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(.05))
                self.send(4.9, track=180, speed=450)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(first['encounter_id'], self.live()[0]['encounter_id'])
            self.assertEqual([], self.events)


if __name__ == '__main__':
    unittest.main()
