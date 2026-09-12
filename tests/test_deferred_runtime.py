"""Frozen input/worker/consumer integration without network or private recordings."""
import datetime
from dataclasses import replace
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock, patch

import transit_warning as r
from authoritative_transit import AuthoritativeTransitLifecycle
from live_dashboard import DashboardRuntime, DashboardState
from observer_position import ObserverContext, ObserverPosition
from runtime_deferred_prediction import RuntimeDeferredPrediction
from shadow_2d_prediction import Shadow2DConfig
from test_motion_freshness import state as motion_state, NOW, sbs
from test_plane_dict_synchronization import plane_entry


class DeferredRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.observer = ObserverContext(ObserverPosition(51., 21., 100.), 'STATIC', 'STATIC', epoch=7)
        self.enterContext(patch.object(r, 'clock', SimpleNamespace(now_utc=lambda: NOW)))
        for name in ('sun_alt', 'sun_az', 'moon_alt', 'moon_az'):
            self.enterContext(patch.object(r, name, 0., create=True))
        self.enterContext(patch.object(r, 'current_observer_context', lambda: self.observer))
        self.enterContext(patch.object(r, 'aircraft_source_mode', 'LOCAL'))
        self.enterContext(patch.object(r, 'shadow_2d_config', Shadow2DConfig(enabled=True)))
        self.enterContext(patch.object(r, 'authoritative_transit_lifecycle', AuthoritativeTransitLifecycle('TRUE_2D')))
        for name in ('plane_dict', 'aircraft_motion_states', 'aircraft_intent_states',
                     'altitude_sources', 'raw_adsb_tracks', 'raw_adsb_versions',
                     'gnss_altitude_states', 'mlat_beast_tracks', 'mlat_coarse_tracks',
                     'aircraft_motion_freshness_status', 'sun_prediction_last_valid',
                     'moon_prediction_last_valid', 'sun_predicted_transit_utc',
                     'moon_predicted_transit_utc', 'transit_solver_diagnostics',
                     'vertical_transit_diagnostics', 'geometric_altitude_selections',
                     'authoritative_terminal_predictions', 'fused_aircraft_states'):
            self.enterContext(patch.object(r, name, {}))
        r.plane_dict['ABC123'] = plane_entry(NOW, distance=10.)
        r.aircraft_motion_states['ABC123'] = motion_state()
        self.enterContext(patch.object(r, 'aircraft_los_geoid_provider', SimpleNamespace(undulation_m=lambda *_: 0.)))
        self.enterContext(patch.object(r, 'select_geometric_altitude_for_prediction',
            lambda icao, vertical, now: SimpleNamespace(altitude_m=vertical.predicted_altitude_m,
                                                       source=SimpleNamespace(value='BARO'))))
        self.enterContext(patch.object(r, 'shadow_2d_diagnostics', None))
        self.enterContext(patch.object(r, 'transit_snapshot_manager', None))
        self.enterContext(patch.object(r, 'cancel_pending_transit_notification'))
        self.enterContext(patch.object(r, 'drop_transit_snapshot_buffer'))
        self.enterContext(patch.object(r, 'gong'))
        self.recorder = self.enterContext(patch.object(r, 'observe_candidate_authoritative_transition'))
        self.capture = self.enterContext(patch.object(r, 'capture_authoritative_transit_prediction'))
        self.telegram = self.enterContext(patch.object(r, 'emit_authoritative_transit_notification', return_value=False))
        self.dashboard = DashboardRuntime(DashboardState())
        self.enterContext(patch.object(r, 'dashboard_runtime', self.dashboard))
        self.coarse = self.enterContext(patch.object(r, 'shadow_coarse_screen',
            return_value=SimpleNamespace(passed=True)))
        self.exact = self.enterContext(patch.object(r, 'shadow_exact_refine', return_value=SimpleNamespace(
            succeeded=True, boundary_status='INTERIOR', tca_seconds=60., separation_deg=1.,
            body_radius_deg=.25, aircraft_azimuth_deg=120., aircraft_altitude_deg=30.,
            body_azimuth_deg=120., body_altitude_deg=31., aircraft_altitude_m=1000.,
            aircraft_latitude_deg=51.2, aircraft_longitude_deg=21.2, vertical_state=None,
            slant_range_km=10.)))
        self.service = RuntimeDeferredPrediction(r)
        self.enterContext(patch.object(r, 'deferred_true2d', self.service))
        self.addCleanup(self.service.close)

    def idle(self):
        scheduler = self.service.scheduler
        with scheduler._condition:
            self.assertTrue(scheduler._condition.wait_for(
                lambda: scheduler._running is None and not scheduler._pending, 3))

    def submit(self):
        with r.plane_dict_lock:
            self.assertTrue(self.service.submit('ABC123', 'TEST', self.observer,
                r.aircraft_motion_states['ABC123'].track, 800., 1000., NOW))

    def pause_worker(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def coarse(context, config):
            self.assertFalse(r.plane_dict_lock._is_owned())
            started.set()
            self.assertTrue(release.wait(3))
            return SimpleNamespace(passed=True)
        self.coarse.side_effect = coarse
        return started, release

    def test_valid_pair_commits_once_through_existing_consumers(self):
        self.submit()
        self.idle()
        self.assertEqual(2, self.coarse.call_count)
        self.assertEqual(2, self.exact.call_count)
        self.assertEqual(2, self.recorder.call_count)
        self.assertEqual(2, self.telegram.call_count)
        self.assertEqual(2, self.capture.call_count)
        self.assertEqual(1, self.service.scheduler.snapshot()['committed'])
        snapshot = self.dashboard.state.snapshot(NOW)
        self.assertEqual(1, len(snapshot['sun']['candidates']))
        self.assertEqual(1, len(snapshot['moon']['candidates']))
        # Unchanged per-body history policy for committed candidates.
        self.dashboard.state.mark_history_worthy('ABC123', 'SUN')
        self.dashboard.withdraw_aircraft('ABC123', NOW)
        self.assertEqual(1, len(self.dashboard.state.query_history()['records']))

    def test_running_input_freezes_geometry_latest_pending_survives(self):
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        original = self.coarse.call_args.args[0]
        with r.plane_dict_lock:
            previous_motion = r.aircraft_motion_states['ABC123']
            r.aircraft_motion_states['ABC123'] = replace(previous_motion,
                position=replace(previous_motion.position, latitude=51.23456789))
        self.submit()
        self.submit()
        self.assertEqual(1, self.service.scheduler.snapshot()['replacements'])
        release.set()
        self.idle()
        self.assertEqual(2, self.service.scheduler.snapshot()['committed'])
        self.assertEqual(4, self.coarse.call_count)  # two pairs, not six jobs
        self.assertEqual(original.latitude_deg, self.coarse.call_args_list[1].args[0].latitude_deg)
        self.assertEqual(51.23456789, self.coarse.call_args_list[2].args[0].latitude_deg)

    def test_invalidation_discards_inflight_results_without_resurrection(self):
        for kind in ('observer', 'source', 'expired', 'motion_stale', 'explicit_clear', 'incarnation'):
            with self.subTest(kind=kind):
                r.plane_dict['ABC123'] = plane_entry(NOW, distance=10.)
                r.aircraft_motion_states['ABC123'] = motion_state()
                started, release = self.pause_worker()
                self.submit()
                self.assertTrue(started.wait(3))
                with r.plane_dict_lock:
                    if kind == 'observer':
                        self.observer = replace(self.observer, epoch=self.observer.epoch + 1)
                        r.invalidate_observer_dependent_state(self.observer)
                    elif kind == 'source':
                        r._clear_aircraft_source_state()
                    elif kind == 'expired':
                        r.plane_dict['ABC123'][0] = NOW - datetime.timedelta(seconds=61)
                        r.clean_dict()
                    elif kind == 'motion_stale':
                        r.discard_authoritative_aircraft('ABC123', NOW)
                    elif kind == 'explicit_clear':
                        r.clear_transit_prediction_state('ABC123', r.plane_dict['ABC123'], 'sun', 18)
                    else:
                        r.plane_dict['ABC123'] = plane_entry(NOW, distance=10.)
                release.set()
                self.idle()
                self.assertEqual(0, self.service.scheduler.snapshot()['committed'])
        self.assertEqual(6, self.service.scheduler.snapshot()['stale_discarded'])
        self.telegram.assert_not_called()

    def test_sbs_submits_without_solving_on_reader_and_records_each_observation(self):
        started, release = self.pause_worker()
        observation = self.enterContext(patch.object(r, 'capture_transit_observation'))
        self.enterContext(patch.object(r, 'port_timestamp_to_utc', return_value=NOW))
        self.enterContext(patch.object(r, 'tabela_for_observer', return_value=(0., 0., 0., 0.)))
        self.enterContext(patch.object(r, 'get_metar_press', return_value=1013.25))
        self.enterContext(patch.object(r, 'moving_body_transit_pred', return_value=0))
        self.enterContext(patch.object(r, 'aircraft_angular_position_from_observer',
            return_value=SimpleNamespace(azimuth_deg=120., altitude_angle_deg=30.)))
        self.enterContext(patch.object(r, 'last_update_time', None))
        self.enterContext(patch.object(r, 'capture_transit_prediction'))
        message = sbs('MLAT', 3, '2026/08/19 12:00:00.000', altitude=10000,
                      groundspeed=450, track=180, latitude=51.2, longitude=21.2, vertical_rate=0)
        r.process_line(message, r.mlat_port)
        self.assertTrue(started.wait(3))
        for _ in range(5):
            r.process_line(message, r.mlat_port)
        self.assertEqual(6, observation.call_count)
        self.assertEqual(1, self.service.scheduler.snapshot()['pending_aircraft'])
        self.assertEqual(4, self.service.scheduler.snapshot()['replacements'])
        release.set()
        self.idle()

    def test_expired_pending_inputs_never_solve_and_have_separate_counter(self):
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        self.submit()
        scheduler = self.service.scheduler
        with scheduler._condition:
            pending = scheduler._pending['ABC123']
            scheduler._pending['ABC123'] = replace(pending, captured_monotonic=time.monotonic() - 3.)
        release.set()
        self.idle()
        counters = scheduler.snapshot()
        self.assertEqual(1, counters['expired_before_solve'])
        self.assertEqual(1, counters['executed'])
        self.assertEqual(0, counters['stale_discarded'])
        self.assertEqual(32, counters['capacity'])

    def test_age_after_solve_is_separate_from_pre_solve_expiration(self):
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        # Deterministic commit-age injection, without sleeping for two seconds.
        real_commit = self.service.scheduler.commit
        self.service.scheduler.commit = lambda job, result: real_commit(
            replace(job, captured_monotonic=time.monotonic() - 3.), result)
        release.set()
        self.idle()
        self.assertEqual(1, self.service.scheduler.snapshot()['stale_discarded'])
        self.assertEqual(0, self.service.scheduler.snapshot()['expired_before_solve'])

    def test_passed_tick_cancels_inflight_prediction_before_it_can_resurrect(self):
        self.submit()
        self.idle()
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        self.service.tick(NOW + datetime.timedelta(seconds=60))
        release.set()
        self.idle()
        self.assertEqual(1, self.service.scheduler.snapshot()['committed'])
        self.assertEqual(1, self.service.scheduler.snapshot()['stale_discarded'])
        self.assertEqual([], self.dashboard.state.snapshot(NOW)['sun']['candidates'])
        self.assertEqual(2, len(self.dashboard.state.query_history()['records']))

    def test_compute_failure_does_not_kill_ingestion_or_worker(self):
        self.coarse.side_effect = ValueError('failed computation')
        with self.assertLogs('deferred_prediction', level='WARNING'):
            self.submit()
            self.idle()
        self.telegram.assert_not_called()
        self.assertEqual(['NONE', 'NONE'],
            [call.args[0].kind.value for call in self.recorder.call_args_list])
        self.coarse.side_effect = None
        self.submit()
        self.idle()
        self.assertEqual(1, self.service.scheduler.snapshot()['failures'])
        self.assertEqual(2, self.service.scheduler.snapshot()['committed'])

    def test_one_body_failure_does_not_suppress_other_body(self):
        self.coarse.side_effect = lambda context, config: (
            (_ for _ in ()).throw(ValueError('moon failure'))
            if context.body == 'MOON' else SimpleNamespace(passed=True))
        with self.assertLogs('deferred_prediction', level='WARNING'):
            self.submit()
            self.idle()
        snapshot = self.dashboard.state.snapshot(NOW)
        self.assertEqual([], snapshot['moon']['candidates'])
        self.assertEqual(1, len(snapshot['sun']['candidates']))
        self.telegram.assert_called_once()
        self.capture.assert_called_once()
        self.assertEqual(['NONE', 'OPENED'],
            [call.args[0].kind.value for call in self.recorder.call_args_list])

    def test_past_body_does_not_suppress_future_body(self):
        normal = self.exact.return_value
        self.exact.side_effect = lambda context, *args, **kwargs: (
            SimpleNamespace(**{**vars(normal), 'tca_seconds': 0.})
            if context.body == 'MOON' else normal)
        self.submit()
        self.idle()
        snapshot = self.dashboard.state.snapshot(NOW)
        self.assertEqual([], snapshot['moon']['candidates'])
        self.assertEqual(1, len(snapshot['sun']['candidates']))
        self.telegram.assert_called_once()

    def test_direct_dashboard_tick_also_cancels_inflight_job(self):
        self.submit()
        self.idle()
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        generation = self.service.sequence
        # This is also the entry point used by StandaloneBridge.step in AUTO.
        self.dashboard.tick(NOW + datetime.timedelta(seconds=60))
        self.assertGreater(self.service.sequence, generation)
        release.set()
        self.idle()
        self.assertEqual(1, self.service.scheduler.snapshot()['stale_discarded'])
        self.assertEqual(1, self.service.scheduler.snapshot()['committed'])

    def test_auto_bridge_clear_remove_publish_cancel_inflight_local_results(self):
        for operation in ('_clear', '_remove', '_publish'):
            with self.subTest(operation=operation):
                started, release = self.pause_worker()
                self.submit()
                self.assertTrue(started.wait(3))
                bridge = object.__new__(r.AutoFusionBridge)
                bridge.aircraft = {'ABC123': {}}
                bridge.monotonic = lambda: 0.
                bridge.geoid = object()
                with patch.object(r, 'fuse_aircraft'), \
                        patch.object(r, 'get_metar_press', return_value=1013.25), \
                        patch.object(r, 'predictor_view', return_value={
                            'classification': 'DEGRADED_UNKNOWN_FIELD_AGE'}), \
                        patch.object(r.StandaloneBridge, operation) as parent:
                    with r.aircraft_source_lock:
                        if operation == '_clear':
                            bridge._clear(NOW)
                        elif operation == '_remove':
                            bridge._remove('ABC123', NOW)
                        else:
                            bridge._publish(SimpleNamespace(icao='ABC123'), None, NOW)
                    parent.assert_called_once()
                release.set()
                self.idle()
                self.assertEqual(0, self.service.scheduler.snapshot()['committed'])
        self.assertEqual(3, self.service.scheduler.snapshot()['stale_discarded'])

    def test_one_body_failure_preserves_existing_withdrawal_grace_for_that_body(self):
        self.submit()
        self.idle()
        self.enterContext(patch.object(r, 'clock', SimpleNamespace(
            now_utc=lambda: NOW + datetime.timedelta(seconds=4))))
        def coarse(context, config):
            if context.body == 'MOON':
                raise ValueError('moon failure')
            return SimpleNamespace(passed=True)
        self.coarse.side_effect = coarse
        self.recorder.reset_mock()
        with self.assertLogs('deferred_prediction', level='WARNING'):
            self.submit()
            self.idle()
        self.assertEqual(['WITHDRAWN', 'UPDATED'],
            [call.args[0].kind.value for call in self.recorder.call_args_list])
        self.assertEqual([], self.dashboard.state.snapshot(NOW)['moon']['candidates'])

    def test_age_is_rechecked_after_contended_dashboard_guard(self):
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        scheduler = self.service.scheduler
        captured = scheduler._running.captured_monotonic
        original = self.dashboard.state.prediction_encounters
        age = [captured + .5]
        def encounters(icao):
            age[0] = captured + 2.01
            return original(icao)
        with patch('runtime_deferred_prediction.time.monotonic', side_effect=lambda: age[0]), \
                patch.object(self.dashboard.state, 'prediction_encounters', side_effect=encounters):
            release.set()
            self.idle()
        self.assertEqual(0, scheduler.snapshot()['committed'])
        self.assertEqual(1, scheduler.snapshot()['stale_discarded'])

    def test_close_waits_for_worker_and_prevents_late_consumer_calls(self):
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        closer = threading.Thread(target=self.service.close)
        closer.start()
        with self.service.scheduler._condition:
            self.assertTrue(self.service.scheduler._condition.wait_for(
                lambda: self.service.scheduler._closed, 3))
        release.set()
        closer.join(3)
        self.assertFalse(closer.is_alive())
        self.telegram.assert_not_called()
        self.recorder.assert_not_called()

    def test_real_capacity_rejects_new_key_without_eviction_or_blocking(self):
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        for i in range(32):
            icao = f'{i:06X}'
            r.plane_dict[icao] = plane_entry(NOW, distance=10.)
            r.aircraft_motion_states[icao] = motion_state()
            with r.plane_dict_lock:
                self.assertTrue(self.service.submit(icao, 'TEST', self.observer,
                    r.aircraft_motion_states[icao].track, 800., 1000., NOW))
        r.plane_dict['NEW'] = plane_entry(NOW, distance=10.)
        r.aircraft_motion_states['NEW'] = motion_state()
        with r.plane_dict_lock:
            self.assertFalse(self.service.submit('NEW', 'TEST', self.observer,
                r.aircraft_motion_states['NEW'].track, 800., 1000., NOW))
            self.assertTrue(self.service.submit('000000', 'TEST', self.observer,
                r.aircraft_motion_states['000000'].track, 800., 1000., NOW))
        stats = self.service.scheduler.snapshot()
        self.assertEqual(32, stats['pending_aircraft'])
        self.assertEqual(1, stats['rejected_capacity'])
        self.assertEqual(1, stats['replacements'])
        self.service.scheduler.cancel()  # Do not compute unrelated test fixtures.
        release.set()
        self.idle()

    def test_same_encounter_remote_owner_replacement_rejects_inflight_local_result(self):
        self.submit()
        self.idle()
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        candidate = self.dashboard.state._live['SUN']['ABC123']['candidate']
        self.dashboard.publish_authoritative(candidate, SimpleNamespace(), source_owner=object())
        self.recorder.reset_mock()
        release.set()
        self.idle()
        self.recorder.assert_not_called()
        self.assertEqual(1, self.service.scheduler.snapshot()['stale_discarded'])
        self.assertIsNotNone(self.dashboard.state._live['SUN']['ABC123']['source_owner'])

    def test_withdraw_and_republish_same_encounter_cannot_resurrect_old_input(self):
        self.submit()
        self.idle()
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        candidate = self.dashboard.state._live['SUN']['ABC123']['candidate']
        self.dashboard.withdraw_aircraft('ABC123', NOW)
        self.dashboard.publish(candidate)
        self.recorder.reset_mock()
        release.set()
        self.idle()
        self.recorder.assert_not_called()
        self.assertEqual(1, self.service.scheduler.snapshot()['stale_discarded'])
        self.assertEqual([], self.dashboard.state.snapshot(NOW)['moon']['candidates'])

    def test_direct_state_finalization_cancels_work_without_runtime_tick_wrapper(self):
        self.submit()
        self.idle()
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        self.dashboard.state.tick(NOW + datetime.timedelta(seconds=60))
        release.set()
        self.idle()
        self.assertEqual(1, self.service.scheduler.snapshot()['stale_discarded'])
        self.assertEqual([], self.dashboard.state.snapshot(NOW)['sun']['candidates'])

    def test_noop_withdrawal_does_not_cancel_valid_pending_input(self):
        started, release = self.pause_worker()
        self.submit()
        self.assertTrue(started.wait(3))
        self.assertFalse(self.dashboard.withdraw_aircraft('ABC123', NOW))
        release.set()
        self.idle()
        self.assertEqual(1, self.service.scheduler.snapshot()['committed'])

    def test_valid_mobile_jitter_commits_but_excess_displacement_does_not(self):
        from test_deferred_observer_compatibility import observer
        for distance, expected in ((15., 1), (70., 0)):
            with self.subTest(distance=distance):
                self.observer = observer(accuracy=15.)
                started, release = self.pause_worker()
                before = self.service.scheduler.snapshot()['committed']
                self.submit()
                self.assertTrue(started.wait(3))
                self.observer = observer(distance, 15.)
                release.set()
                self.idle()
                self.assertEqual(before + expected, self.service.scheduler.snapshot()['committed'])

    def test_legacy_comparison_and_provenance_are_detached_at_capture(self):
        started, release = self.pause_worker()
        legacy = [0., 0., 0., 0., 0., 0., 60.]
        with r.plane_dict_lock:
            self.assertTrue(self.service.submit('ABC123', 'TEST', self.observer,
                r.aircraft_motion_states['ABC123'].track, 800., 1000., NOW,
                (legacy, legacy)))
        self.assertTrue(started.wait(3))
        job = self.service.scheduler._running
        frozen_json = job.payload.provenance_json
        legacy[6] = 999.
        r.aircraft_source_mode = 'AUTO'
        self.assertEqual(60., job.payload.legacy_results[0][6])
        self.assertEqual(frozen_json, job.payload.provenance_json)
        release.set()
        self.idle()
        self.assertEqual(1, self.service.scheduler.snapshot()['stale_discarded'])

    def test_callbacks_and_computation_run_without_scheduler_lock(self):
        acquired = []
        def probe(*args):
            def other_thread():
                with self.service.scheduler._condition:
                    acquired.append(True)
                with r.plane_dict_lock:
                    acquired.append(True)
                with r.aircraft_source_lock:
                    acquired.append(True)
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join(1.)
            self.assertFalse(thread.is_alive())
            return SimpleNamespace(passed=True)
        self.coarse.side_effect = probe
        self.submit()
        self.idle()
        self.assertEqual(6, len(acquired))
        self.assertEqual(1, self.service.scheduler.snapshot()['committed'])

    def test_shutdown_interrupts_numerical_samples_and_joins_outside_locks(self):
        started = threading.Event()
        def coarse(context, config):
            started.set()
            while True:
                context.aircraft_los_resolver(context.observer_context.position, (51.2, 21.2), 1000.)
        self.coarse.side_effect = coarse
        self.submit()
        self.assertTrue(started.wait(3))
        self.assertTrue(self.service.close())
        self.assertFalse(self.service.scheduler._thread.is_alive())
        self.recorder.assert_not_called()
        self.assertEqual(0, self.service.scheduler.snapshot()['shutdown_timeouts'])

    def test_real_solver_matches_synchronous_geometry_and_reuses_paired_samples(self):
        import shadow_2d_prediction as shadow
        from source_geometry_fixture import canonical_context, ConstantGeoid
        ctx = replace(canonical_context(), prediction_base_utc=NOW)
        self.observer = ctx.observer_context
        self.coarse.side_effect = shadow.coarse_screen
        self.exact.side_effect = shadow.exact_refine
        outputs = []
        self.service.scheduler.commit = lambda job, result: outputs.extend(result) or True
        with patch.object(r, 'build_shadow_2d_context', return_value=ctx), \
                patch.object(r, 'aircraft_los_geoid_provider', ConstantGeoid()):
            self.submit()
            self.idle()
        self.assertEqual(2, len(outputs))
        for actual_context, actual, _ in outputs:
            expected = shadow.run_shadow_pipeline(
                replace(ctx, body=actual_context.body), r.shadow_2d_config)
            self.assertEqual(replace(expected.coarse, duration_ms=0),
                             replace(actual.coarse, duration_ms=0))
            self.assertTrue(actual.exact.succeeded)
            self.assertEqual(replace(expected.exact, duration_ms=0),
                             replace(actual.exact, duration_ms=0))
        stats = self.service.scheduler.snapshot()
        print("Deferred real paired solve: {:.3f} ms".format(
            stats['mean_execution_seconds'] * 1000))
        # The same frozen resolver identity allows one aircraft sample cache
        # to be reused for both bodies, preserving the existing optimization.
        self.assertEqual(outputs[0][0].aircraft_los_resolver,
                         outputs[1][0].aircraft_los_resolver)

    def test_diagnostic_failure_does_not_suppress_committed_consumers(self):
        writer = SimpleNamespace(counters={}, record=Mock(side_effect=OSError('diagnostics')))
        with patch.object(r, 'shadow_2d_diagnostics', writer):
            self.submit()
            self.idle()
        self.assertEqual(2, self.recorder.call_count)
        self.assertEqual(2, self.capture.call_count)
        self.assertEqual(1, self.service.scheduler.snapshot()['committed'])

    def test_publication_callbacks_are_outside_scheduler_and_dashboard_locks(self):
        def publish():
            acquired = []
            def probe():
                with self.service.scheduler._condition:
                    acquired.append('scheduler')
                with self.dashboard.state._lock:
                    acquired.append('dashboard')
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(['scheduler', 'dashboard'], acquired)
        with patch.object(self.dashboard, '_publish_application_state', side_effect=publish):
            self.submit()
            self.idle()
        self.assertEqual(1, self.service.scheduler.snapshot()['committed'])

    def test_fresh_local_input_can_recover_from_already_captured_remote_owner(self):
        self.submit()
        self.idle()
        candidate = self.dashboard.state._live['SUN']['ABC123']['candidate']
        self.dashboard.publish_authoritative(candidate, SimpleNamespace(), source_owner=object())
        self.submit()
        self.idle()
        self.assertEqual(2, self.service.scheduler.snapshot()['committed'])
        self.assertIsNone(self.dashboard.state._live['SUN']['ABC123']['source_owner'])

    def test_prediction_commit_does_not_wait_for_public_snapshot_build(self):
        from app_backend.publisher import PublicStatePublisher
        from app_backend.state import ApplicationStateStore
        store = ApplicationStateStore()
        started, release = threading.Event(), threading.Event()
        def build():
            self.assertEqual(threading.current_thread().name, 'public-state-publisher')
            self.assertFalse(r.plane_dict_lock._is_owned())
            started.set()
            self.assertTrue(release.wait(3))
            store.publish(self.dashboard.state.snapshot())
        publisher = PublicStatePublisher(build, .02)
        self.addCleanup(publisher.close)
        self.addCleanup(release.set)
        self.dashboard.publisher = publisher
        publisher.mark_dirty()
        self.assertTrue(started.wait(2))
        self.submit()
        self.idle()
        self.assertEqual(1, self.service.scheduler.snapshot()['committed'])
        self.assertEqual(2, self.recorder.call_count)
        self.assertEqual(2, self.telegram.call_count)
        self.assertEqual(2, self.capture.call_count)
        self.assertEqual(0, store.snapshot()['revision'])
        release.set()
        self.assertTrue(publisher.flush())
        self.assertEqual(1, len(store.snapshot()['state']['bodies']['sun']['candidates']))

    def test_sbs_ingest_continues_while_publication_is_blocked(self):
        from app_backend.publisher import PublicStatePublisher
        started, release = threading.Event(), threading.Event()
        threads = []
        def build():
            threads.append(threading.current_thread().name)
            self.assertFalse(r.plane_dict_lock._is_owned())
            self.assertFalse(r.aircraft_source_lock._is_owned())
            started.set()
            self.assertTrue(release.wait(3))
            self.dashboard.state.snapshot()
        publisher = PublicStatePublisher(build, .02)
        self.addCleanup(publisher.close)
        self.addCleanup(release.set)
        self.dashboard.publisher = publisher
        publisher.mark_dirty()
        self.assertTrue(started.wait(2))
        self.test_sbs_submits_without_solving_on_reader_and_records_each_observation()
        self.assertEqual(0, publisher.snapshot()['published'])
        release.set()
        self.assertTrue(publisher.flush())
        self.assertEqual({'public-state-publisher'}, set(threads))
        self.assertLessEqual(publisher.snapshot()['published'], 2)
