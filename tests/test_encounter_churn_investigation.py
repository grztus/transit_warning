"""Deterministic characterization of solve gaps versus AUTO input authority."""
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import transit_warning as runtime
from authoritative_transit import AuthoritativeTransitLifecycle
from live_dashboard import DashboardRuntime, DashboardState
from test_authoritative_transit import BASE, context, result
import test_source_integration as sources
import test_deferred_runtime as deferred


class EncounterChurnInvestigationTests(unittest.TestCase):
    def test_no_missing_call_means_no_age_based_closure_on_recovery(self):
        lifecycle = AuthoritativeTransitLifecycle('TRUE_2D')
        first = lifecycle.consider(context(), result(), BASE)
        recovered = lifecycle.consider(context(), result(), BASE + dt.timedelta(seconds=30))
        self.assertEqual(first.encounter_id, recovered.encounter_id)

    def test_gap_boundary_and_cadence_are_elapsed_time_based(self):
        for interval in (0.2, 1.0):
            for gap in (2.999, 3.0, 3.001, 5.0, 9.999, 10.0, 30.0):
                for missing in ('unavailable', 'invalid'):
                    with self.subTest(interval=interval, gap=gap, missing=missing):
                        lifecycle = AuthoritativeTransitLifecycle('TRUE_2D')
                        first = lifecycle.consider(context(), result(), BASE)
                        times = [index * interval for index in range(1, int(gap / interval) + 1)
                                 if index * interval < gap] + [gap]
                        transitions = []
                        for seconds in times:
                            now = BASE + dt.timedelta(seconds=seconds)
                            transition = (lifecycle.unavailable_transition(4, 'ABC123', 'MOON', now)
                                if missing == 'unavailable' else lifecycle.consider_transition(
                                    context(), result(succeeded=False), now))
                            transitions.append(transition.kind.value)
                        recovered = lifecycle.consider(context(), result(),
                            BASE + dt.timedelta(seconds=gap + .001))
                        self.assertEqual(int(gap >= 3), transitions.count('WITHDRAWN'))
                        self.assertEqual(1 if gap < 3 else 2, recovered.encounter_generation)
                        self.assertEqual(gap < 3, first.encounter_id == recovered.encounter_id)

    def test_runtime_withdrawal_precedes_reopen_without_prediction_replaced(self):
        events = []
        dashboard = DashboardRuntime(DashboardState(
            finalization_journal=SimpleNamespace(append=events.append)))
        for name in ('sun_prediction_last_valid', 'moon_prediction_last_valid',
                     'sun_predicted_transit_utc', 'moon_predicted_transit_utc',
                     'vertical_transit_diagnostics', 'geometric_altitude_selections',
                     'authoritative_terminal_predictions'):
            self.enterContext(patch.object(runtime, name, {}))
        for name in ('observe_candidate_authoritative_transition', 'gong',
                     'capture_authoritative_transit_prediction',
                     'cancel_pending_transit_notification'):
            self.enterContext(patch.object(runtime, name))
        self.enterContext(patch.object(runtime, 'emit_authoritative_transit_notification', return_value=False))
        self.enterContext(patch.object(runtime, 'telegram_notifier', None))
        self.enterContext(patch.object(runtime, 'deferred_true2d', None))
        self.enterContext(patch.object(runtime, 'dashboard_runtime', dashboard))
        self.enterContext(patch.object(runtime, 'aircraft_los_geoid_provider', None))
        lifecycle = AuthoritativeTransitLifecycle('TRUE_2D')
        entry = [''] * 32
        ctx = context()
        for seconds, exact, expected in ((0, result(), 'OPENED'),
                                         (2.999, None, 'HELD'),
                                         (3, None, 'WITHDRAWN'),
                                         (3.2, result(), 'OPENED')):
            now = BASE + dt.timedelta(seconds=seconds)
            with patch.object(runtime, 'clock', SimpleNamespace(now_utc=lambda: now)):
                transition = lifecycle.consider_transition(ctx, exact, now)
                self.assertEqual(expected, transition.kind.value)
                runtime.consume_authoritative_transition(transition, ctx, entry, 20, now)
                live = dashboard.state.snapshot(now)['moon']['candidates']
                self.assertEqual(expected != 'WITHDRAWN', bool(live))
                if live:
                    self.assertEqual(transition.prediction.encounter_id, live[0]['encounter_id'])
                if seconds == 0:
                    dashboard.state.mark_history_worthy('ABC123', 'MOON')
        self.assertEqual(['PREDICTION_UNAVAILABLE'], [event['reason'] for event in events])
        self.assertEqual(1, len(dashboard.state.query_history()['records']))
        self.assertEqual('4:ABC123:MOON:2', live[0]['encounter_id'])

    def test_successful_refinement_is_independent_of_generation(self):
        lifecycle = AuthoritativeTransitLifecycle('TRUE_2D')
        observations = []
        for index, (target, sep) in enumerate(((120, .1), (120, .1), (121, .12), (119, .09), (120, .1))):
            now = BASE + dt.timedelta(seconds=index * .2)
            ctx = context()
            ctx.prediction_base_utc = now
            prediction = lifecycle.consider(ctx, result(seconds=target-index*.2, separation=sep), now)
            observations.append(prediction)
        self.assertEqual(1, len({item.encounter_id for item in observations}))
        self.assertEqual([0, 1, -2, 1], [(right.predicted_transit_utc-left.predicted_transit_utc).total_seconds()
            for left, right in zip(observations, observations[1:])])
        for actual, expected in zip([right.separation_deg-left.separation_deg
                for left, right in zip(observations, observations[1:])], (0, .02, -.03, .01)):
            self.assertAlmostEqual(expected, actual)


class AutoChurnInvestigationTests(unittest.TestCase):
    setUp = sources.SourceIntegrationTests.setUp
    report = sources.SourceIntegrationTests.report

    def test_exact_gap_does_not_authorize_remote_but_stale_velocity_does(self):
        report = self.report()
        runtime.adsblol_auto_cache.consume(report, 100.)
        bridge = runtime.AutoFusionBridge(self.poller, self.dashboard, sources.ConstantGeoid())
        bridge.aircraft = {'ABC123': report['aircraft'][0]}
        fields = {
            'position': sources.local('position', {'lat': 1, 'lon': 2}, 0),
            'ground_track': sources.local('ground_track', 90, 0),
            'groundspeed': sources.local('groundspeed', 700, 0, unit='km/h'),
            'geometric_altitude': sources.local('geometric_altitude', 10000, 0,
                                               unit='m', datum='WGS84_HAE')}
        events = []
        self.dashboard.state.finalization_journal = SimpleNamespace(append=events.append)
        self.dashboard.publish(sources.candidate())
        ctx = context()
        ctx.body = 'SUN'
        ctx.observer_context = self.observer.resolve(sources.NOW)
        ctx.prediction_base_utc = sources.NOW
        lifecycle = AuthoritativeTransitLifecycle('TRUE_2D')
        local_prediction = lifecycle.consider(ctx, result(), sources.NOW)
        remote = bridge.lifecycle.consider(ctx, result(), sources.NOW)
        with patch.object(runtime, 'local_fusion_fields', return_value=fields), patch.object(
                runtime, 'capture_authoritative_transit_prediction'), patch.object(runtime, 'deferred_true2d', None):
            for seconds in (1, 3, 5):
                now = sources.NOW + dt.timedelta(seconds=seconds)
                lifecycle.unavailable_transition(ctx.observer_context.epoch, 'ABC123', 'SUN', now)
                with self.assertRaisesRegex(ValueError, 'fallback not required'):
                    bridge.context(bridge.aircraft['ABC123'], ctx.observer_context, now, seconds, 'SUN')
                self.assertFalse(bridge._publish(remote, ctx, now))
                self.assertIsNone(self.dashboard.state._live['SUN']['ABC123']['source_owner'])
            self.assertEqual([], events)
            # Healthy position alone is not a healthy horizontal motion bundle.
            fields['ground_track'] = replace(fields['ground_track'], age_seconds=11)
            now = sources.NOW + dt.timedelta(seconds=5)
            self.assertIsNotNone(bridge.context(bridge.aircraft['ABC123'], ctx.observer_context, now, 5, 'SUN'))
            self.assertTrue(bridge._publish(remote, ctx, now))
            self.assertIs(bridge, self.dashboard.state._live['SUN']['ABC123']['source_owner'])
            fields['ground_track'] = replace(fields['ground_track'], age_seconds=0)
            recovered = lifecycle.consider(ctx, result(), now)
            self.assertNotEqual(local_prediction.encounter_id, recovered.encounter_id)
            self.dashboard.publish(replace(sources.candidate(), encounter_id=recovered.encounter_id))
            self.assertFalse(bridge._publish(remote, ctx, now))
        self.assertEqual(['PREDICTION_REPLACED'] * 2, [event['reason'] for event in events])
        self.assertIsNone(self.dashboard.state._live['SUN']['ABC123']['source_owner'])
        self.assertEqual([], self.dashboard.state.query_history()['records'])


class DeferredChurnInvestigationTests(unittest.TestCase):
    setUp = deferred.DeferredRuntimeTests.setUp
    idle = deferred.DeferredRuntimeTests.idle
    submit = deferred.DeferredRuntimeTests.submit
    pause_worker = deferred.DeferredRuntimeTests.pause_worker

    def test_deferred_unavailable_gap_without_continuity_reopens_generation(self):
        self.submit()
        self.idle()
        original = self.dashboard.state._live['MOON']['ABC123']['candidate']
        self.enterContext(patch.object(runtime, 'clock', SimpleNamespace(
            now_utc=lambda: deferred.NOW + dt.timedelta(seconds=4))))
        self.coarse.side_effect = ValueError('synthetic unavailable solve')
        with self.assertLogs('deferred_prediction', level='WARNING'):
            self.submit()
            self.idle()
        self.assertEqual(['WITHDRAWN', 'WITHDRAWN'],
            [call.args[0].kind.value for call in self.recorder.call_args_list[-2:]])
        self.assertNotIn('ABC123', self.dashboard.state._live['MOON'])
        self.coarse.side_effect = None
        self.submit()
        self.idle()
        recovered = self.dashboard.state._live['MOON']['ABC123']['candidate']
        self.assertNotEqual(original.encounter_id, recovered.encounter_id)
        self.assertTrue(recovered.encounter_id.endswith(':2'))
        self.assertEqual(4, self.telegram.call_count)  # Fresh pair only, never the failed pair.

    def test_unavailable_withdrawal_rejects_already_running_deferred_result(self):
        self.submit()
        self.idle()
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        now = deferred.NOW + dt.timedelta(seconds=3)
        with runtime.plane_dict_lock, patch.object(runtime, 'clock', SimpleNamespace(now_utc=lambda: now)):
            ctx = SimpleNamespace(icao='ABC123', body='MOON', observer_context=self.observer)
            transition = runtime.authoritative_transit_lifecycle.unavailable_transition(
                self.observer.epoch, 'ABC123', 'MOON', now)
            runtime.consume_authoritative_transition(transition, ctx, runtime.plane_dict['ABC123'], 10, now)
        release.set()
        self.idle()
        self.assertEqual(1, self.service.scheduler.snapshot()['committed'])
        self.assertEqual(1, self.service.scheduler.snapshot()['stale_discarded'])
        self.assertNotIn('ABC123', self.dashboard.state._live['MOON'])
        self.assertIsNone(runtime.authoritative_transit_lifecycle.active_prediction(
            self.observer.epoch, 'ABC123', 'MOON'))
        self.assertEqual(2, self.telegram.call_count)


if __name__ == '__main__':
    unittest.main()
