"""Exact message-local reuse and publication detachment regressions."""
import datetime
from contextlib import ExitStack
from dataclasses import fields, replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import shadow_2d_prediction as shadow
from app_backend.state import ApplicationStateStore
from authoritative_transit import AuthoritativeTransitLifecycle
from tests.test_shadow_2d_prediction import context, BASE
from transit_prediction_model import VerticalIntentState, IntentParameter


def without_duration(result):
    return replace(result, duration_ms=0) if result is not None else None


class MessageReuseTests(unittest.TestCase):
    def test_authoritative_consumer_order_and_dashboard_payloads_match(self):
        import transit_warning as runtime
        from live_dashboard import DashboardState
        runs = []
        for enabled in (False, True):
            events, payloads = [], []
            lifecycle = AuthoritativeTransitLifecycle('TRUE_2D')
            state, store = DashboardState(), ApplicationStateStore()
            def publish(candidate):
                events.append(('publish', candidate.body))
                state.publish(candidate)
                store.publish(state.snapshot(BASE))
                payloads.append(store.snapshot())
                return True
            with ExitStack() as stack:
                for name in ('observe_candidate_authoritative_transition', 'clear_transit_prediction',
                             'update_transit_prediction_timestamp', 'emit_authoritative_transit_notification',
                             'capture_authoritative_transit_prediction'):
                    def observe(*args, _name=name, **kwargs):
                        events.append((_name,))
                        return False
                    stack.enter_context(patch.object(runtime, name, side_effect=observe))
                stack.enter_context(patch.object(runtime, 'dashboard_runtime', SimpleNamespace(publish=publish)))
                stack.enter_context(patch.object(runtime, 'aircraft_los_geoid_provider', None))
                stack.enter_context(patch.object(runtime, 'authoritative_terminal_predictions', {}))
                stack.enter_context(patch.object(runtime, 'transit_separation_sound_alert', -1))
                stack.enter_context(patch.object(runtime, 'clock', SimpleNamespace(now_utc=lambda: BASE)))
                for _ in range(2):
                    with shadow.message_aircraft_cache(enabled):
                        original = context()
                        for body in ('MOON', 'SUN'):
                            ctx = replace(original, body=body)
                            result = shadow.run_shadow_pipeline(ctx, shadow.Shadow2DConfig(enabled=True))
                            transition = lifecycle.consider_transition(ctx, result, BASE)
                            runtime.consume_authoritative_transition(transition, ctx, ['']*32, 100, BASE)
            runs.append((events, payloads))
        self.assertEqual(runs[0], runs[1])
        self.assertEqual([p['revision'] for p in runs[1][1]], [1, 2, 3, 4])
        self.assertEqual(len(runs[1][0]), 24)

    def test_paired_coarse_exact_and_lifecycle_are_identical(self):
        original = context(vertical_rate=600)
        config = shadow.Shadow2DConfig(enabled=True)
        outputs = []
        counts = []
        for enabled in (False, True):
            lifecycle = AuthoritativeTransitLifecycle('TRUE_2D')
            run = []
            with patch.object(shadow, 'evaluate_aircraft_geometry', wraps=shadow.evaluate_aircraft_geometry) as aircraft:
                for message in range(2):
                    with shadow.message_aircraft_cache(enabled):
                        for body in ('MOON', 'SUN'):
                            ctx = replace(original, body=body,
                                prediction_base_utc=BASE + datetime.timedelta(seconds=message))
                            coarse = shadow.coarse_screen(ctx, config)
                            exact = shadow.exact_refine(ctx, coarse, config)
                            result = shadow.Shadow2DResult(coarse, exact)
                            transition = lifecycle.consider_transition(ctx, result, ctx.prediction_base_utc)
                            run.append((without_duration(coarse), without_duration(exact), transition))
                counts.append(aircraft.call_count)
            outputs.append(run)
        self.assertEqual(outputs[0], outputs[1])
        self.assertLess(counts[1], counts[0])
        self.assertEqual([item[2].kind.value for item in outputs[1]], ['OPENED', 'OPENED', 'UPDATED', 'UPDATED'])

    def test_every_key_input_invalidates_and_times_are_not_rounded(self):
        ctx = context()
        changes = {
            'icao': 'ABC124', 'prediction_base_utc': BASE + datetime.timedelta(microseconds=1),
            'observer_context': replace(ctx.observer_context, epoch=1),
            'latitude_deg': .001, 'longitude_deg': .001, 'track_deg': 91.,
            'groundspeed_kmh': 701., 'current_altitude_m': 10001.,
            'vertical_motion': replace(ctx.vertical_motion, altitude=replace(ctx.vertical_motion.altitude, value=9999)),
            'vertical_intent': VerticalIntentState(selected_altitude=IntentParameter(30000, BASE, 'adsb')),
            'vertical_policy': replace(ctx.vertical_policy, prediction_limit_seconds=101),
            'qnh_hpa': 1014., 'geometric_altitude_correction_m': 26.,
            'altitude_source': 'BAROMETRIC', 'position_source': 'adsb', 'track_source': 'adsb',
            'aircraft_los_resolver': lambda *args: ctx.aircraft_los_resolver(*args),
        }
        self.assertEqual(set(changes), {f.name for f in fields(ctx)} - {'body', 'callsign', 'body_position_resolver'})
        for name, value in changes.items():
            with self.subTest(name=name), shadow.message_aircraft_cache(), patch.object(
                    shadow, 'evaluate_aircraft_geometry', wraps=shadow.evaluate_aircraft_geometry) as aircraft:
                shadow.evaluate_shadow_geometry(ctx, 10)
                shadow.evaluate_shadow_geometry(replace(ctx, **{name: value}), 10)
                self.assertEqual(aircraft.call_count, 2)
        with shadow.message_aircraft_cache(), patch.object(shadow, 'evaluate_aircraft_geometry', wraps=shadow.evaluate_aircraft_geometry) as aircraft:
            shadow.evaluate_shadow_geometry(ctx, 10)
            shadow.evaluate_shadow_geometry(ctx, 10 + 1e-12)
            self.assertEqual(aircraft.call_count, 2)

    def test_scope_cleanup_and_no_cross_message_reuse(self):
        ctx = context()
        @shadow.reuse_aircraft_within_message
        def message(fail=False):
            shadow.evaluate_shadow_geometry(ctx, 10)
            shadow.evaluate_shadow_geometry(replace(ctx, body='MOON'), 10)
            if fail:
                raise ValueError('message failed')
        with patch.object(shadow, 'evaluate_aircraft_geometry', wraps=shadow.evaluate_aircraft_geometry) as aircraft:
            message()
            message()
            with self.assertRaises(ValueError): message(True)
            shadow.evaluate_shadow_geometry(ctx, 10)
            self.assertEqual(aircraft.call_count, 4)
        self.assertIsNone(shadow._aircraft_cache.get())

    def test_failures_are_not_cached_and_body_failure_is_isolated(self):
        ctx = context()
        with shadow.message_aircraft_cache():
            bad = replace(ctx, body_position_resolver=Mock(side_effect=ValueError('body failed')))
            self.assertEqual(shadow.coarse_screen(bad, shadow.Shadow2DConfig()).reason, 'COARSE_ERROR:ValueError')
            self.assertEqual(shadow.evaluate_shadow_geometry(ctx, 0),
                             shadow.evaluate_shadow_geometry(replace(ctx, body='MOON'), 0))
        los = Mock(side_effect=[ValueError('aircraft failed'), ctx.aircraft_los_resolver(ctx.observer_context.position, (0, 0), 10025)])
        ctx = replace(ctx, aircraft_los_resolver=los)
        with shadow.message_aircraft_cache():
            with self.assertRaises(ValueError): shadow.evaluate_shadow_geometry(ctx, 0)
            shadow.evaluate_shadow_geometry(ctx, 0)
        self.assertEqual(los.call_count, 2)


class PublicationCopyTests(unittest.TestCase):
    def test_no_subscribers_skip_snapshot_but_keep_state_and_revisions(self):
        store = ApplicationStateStore()
        with patch.object(store, '_snapshot_locked', wraps=store._snapshot_locked) as snapshot:
            self.assertEqual(store.publish({}), 1)
            self.assertEqual(store.publish({}), 2)
            snapshot.assert_not_called()
        self.assertEqual(store.snapshot()['revision'], 2)

    def test_subscribers_order_detachment_and_payload_revision_parity(self):
        subscribed, bare = ApplicationStateStore(), ApplicationStateStore()
        events = []
        def first(payload):
            events.append(('first', payload['revision']))
            payload['state'].clear()
            raise ValueError('subscriber failure')
        def second(payload):
            events.append(('second', payload['revision']))
            self.assertEqual(payload, bare.snapshot())
            payload['state'].clear()
        subscribed.subscribe(first)
        subscribed.subscribe(second)
        for revision in (1, 2):
            source = {}
            bare.publish(source)
            self.assertEqual(subscribed.publish(source), revision)
            source['unexpected'] = 'mutation'
            self.assertEqual(subscribed.snapshot(), bare.snapshot())
        self.assertEqual(events, [('first', 1), ('second', 1), ('first', 2), ('second', 2)])


if __name__ == '__main__':
    unittest.main()
