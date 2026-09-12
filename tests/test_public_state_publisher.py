import datetime
import threading
import socket
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app_backend.publisher import PublicStatePublisher
from app_backend.state import ApplicationStateStore
from app_backend.sse import SseBroker, live_envelope
from live_dashboard import DashboardRuntime, DashboardState, _handler_factory


class PublisherTests(unittest.TestCase):
    def publisher(self, callback, interval=0.02):
        worker = PublicStatePublisher(callback, interval)
        self.addCleanup(worker.close)
        return worker

    def test_burst_is_one_latest_snapshot_and_clean_ticks_do_nothing(self):
        calls = []
        worker = self.publisher(lambda: calls.append(threading.current_thread().name), .2)
        for _ in range(1000):
            worker.mark_dirty()
        self.assertTrue(worker.flush())
        time.sleep(.22)
        self.assertEqual(calls, ['public-state-publisher'])
        stats = worker.snapshot()
        self.assertEqual(stats['dirty_marks'], 1000)
        self.assertEqual(stats['coalesced_marks'], 999)
        self.assertEqual(stats['published_generation'], 1000)
        self.assertFalse(stats['pending_dirty'])

    def test_mutations_during_build_remain_dirty_without_queue(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def build():
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(2))
        worker = self.publisher(build)
        self.addCleanup(release.set)
        worker.mark_dirty()
        self.assertTrue(entered.wait(2))
        for _ in range(1000):
            worker.mark_dirty()
        self.assertFalse(worker.flush(.01))
        release.set()
        self.assertTrue(worker.flush())
        self.assertEqual(len(calls), 2)
        self.assertEqual(worker.snapshot()['published_generation'], 1001)

    def test_mutation_after_callback_before_acknowledgement_is_not_lost(self):
        returned, release = threading.Event(), threading.Event()
        calls = []
        pause = [False]
        real_monotonic = time.monotonic
        def monotonic():
            if threading.current_thread().name == 'public-state-publisher' and pause[0]:
                pause[0] = False
                returned.set()
                self.assertTrue(release.wait(2))
            return real_monotonic()
        def build():
            calls.append(1)
            pause[0] = len(calls) == 1
        with patch('app_backend.publisher.time', SimpleNamespace(monotonic=monotonic)):
            worker = self.publisher(build)
            self.addCleanup(release.set)
            worker.mark_dirty()
            self.assertTrue(returned.wait(2))
            worker.mark_dirty()
            self.assertEqual(worker.snapshot()['published_generation'], 0)
            self.assertFalse(worker.flush(.01))
            release.set()
            self.assertTrue(worker.flush())
            self.assertEqual(len(calls), 2)
            self.assertEqual(worker.snapshot()['published_generation'], 2)

    def test_required_serialization_privacy_copy_and_sse_failures_remain_dirty(self):
        import app_backend.state as state_module
        import app_backend.sse as sse_module
        for stage in ('serialize', 'copy', 'privacy', 'wire', 'sse'):
            with self.subTest(stage=stage):
                store, broker = ApplicationStateStore(), SseBroker()
                store.subscribe(lambda value: broker.publish(live_envelope(value)), required=True)
                healthy = threading.Event()
                owner, attribute = {
                    'serialize': (state_module, 'serialize_live_state'),
                    'copy': (state_module, 'deepcopy'),
                    'privacy': (sse_module, 'assert_public_payload'),
                    'wire': (sse_module, 'encode_sse'),
                    'sse': (broker, 'publish'),
                }[stage]
                original = getattr(owner, attribute)
                def checked(*args, **kwargs):
                    if not healthy.is_set():
                        raise ValueError('injected failure')
                    return original(*args, **kwargs)
                worker = self.publisher(lambda: store.publish({}))
                with patch.object(owner, attribute, side_effect=checked), self.assertLogs(
                        'app_backend.publisher', level='ERROR'):
                    worker.mark_dirty()
                    with worker._condition:
                        self.assertTrue(worker._condition.wait_for(lambda: worker._failures > 0, 2))
                        stats = worker.snapshot()
                        self.assertEqual(stats['published_generation'], 0)
                        self.assertEqual(stats['published'], 0)
                        self.assertTrue(stats['pending_dirty'])
                    self.assertFalse(worker.flush(.001))
                    healthy.set()
                    self.assertTrue(worker.flush())
                self.assertFalse(worker.snapshot()['pending_dirty'])
                worker.close()

    def test_optional_subscriber_failure_remains_fail_open(self):
        store = ApplicationStateStore()
        def fail(_):
            raise ValueError()
        store.subscribe(fail)
        self.assertEqual(store.publish({}), 1)

    def test_barrier_cannot_wait_for_itself(self):
        results = []
        worker = self.publisher(lambda: results.append(worker.flush(timeout=100)))
        worker.mark_dirty()
        self.assertTrue(worker.flush(timeout=2))
        self.assertEqual(results, [False])

    def test_barrier_does_not_accept_success_for_an_older_generation(self):
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        calls = []
        def build():
            index = len(calls)
            calls.append(1)
            entered[index].set()
            self.assertTrue(release[index].wait(2))
        worker = self.publisher(build)
        for event in release:
            self.addCleanup(event.set)
        worker.mark_dirty()
        self.assertTrue(entered[0].wait(2))
        worker.mark_dirty()
        release[0].set()
        self.assertTrue(entered[1].wait(2))
        self.assertEqual(worker.snapshot()['published_generation'], 1)
        self.assertTrue(worker.snapshot()['pending_dirty'])
        self.assertFalse(worker.flush(.01))
        release[1].set()
        self.assertTrue(worker.flush())
        self.assertEqual(worker.snapshot()['published_generation'], 2)

    def test_shutdown_drains_http_mutations_before_final_publication_and_consumers(self):
        state, store = DashboardState(), ApplicationStateStore()
        runtime = DashboardRuntime(state, application_state_store=store)
        self.addCleanup(runtime.publisher.close)
        handler = _handler_factory(state, lambda: None, None, None, store,
                                   publisher=runtime.publisher)
        entered, release, closing = threading.Event(), threading.Event(), threading.Event()
        order = []
        def mutate(request):
            entered.set()
            self.assertTrue(release.wait(2))
            runtime.tick(datetime.datetime.now(datetime.timezone.utc))
            self.assertTrue(runtime.publisher.flush())
            order.append('mutation')
        handler._do_POST = mutate
        request = object.__new__(handler)
        producer = threading.Thread(target=request.do_POST)
        producer.start()
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        runtime.server = SimpleNamespace(RequestHandlerClass=handler,
            shutdown=closing.set, server_close=lambda: order.append('server'))
        state.history_store = SimpleNamespace(close=lambda: order.append('history'))
        state.finalization_journal = SimpleNamespace(close=lambda: order.append('journal'))
        closer = threading.Thread(target=runtime.close)
        closer.start()
        self.assertTrue(closing.wait(2))
        self.assertTrue(closer.is_alive())
        release.set()
        producer.join(2)
        closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertFalse(runtime.publisher._thread.is_alive())
        self.assertEqual(order, ['mutation', 'server', 'history', 'journal'])
        rejected = []
        request._send_json = lambda value, status: rejected.append(status)
        request.do_POST()
        self.assertEqual(rejected, [503])

    def test_runtime_shutdown_drains_producers_before_lifecycle_consumers(self):
        import transit_warning as runtime
        order = []
        dashboard = SimpleNamespace(stop_mutations=lambda: order.append('http'),
                                    close=lambda: order.append('dashboard'))
        reader = SimpleNamespace(join=lambda: order.append('reader'))
        with patch.multiple(runtime, shutdown_complete=False, stop_event=threading.Event(),
                dashboard_runtime=dashboard, internet_source_poller=None,
                internet_source_bridge=None,
                deferred_true2d=SimpleNamespace(close=lambda: order.append('prediction')),
                candidate_storage_worker=SimpleNamespace(close=lambda: order.append('recorder')),
                telegram_notifier=SimpleNamespace(close=lambda: order.append('telegram'))), patch.object(
                runtime, 'close_active_sockets', side_effect=lambda: order.append('sockets')), patch.object(
                runtime, 'close_transit_snapshots', side_effect=lambda _: order.append('snapshots')):
            runtime.shutdown_runtime([reader], None)
        self.assertEqual(order, ['sockets', 'http', 'reader', 'prediction', 'snapshots',
                                 'recorder', 'telegram', 'dashboard'])

    def test_shutdown_interrupts_incomplete_http_body_without_waiting_for_peer(self):
        handler = _handler_factory(DashboardState(), lambda: None, None, None)
        reading = threading.Event()
        request = object.__new__(handler)
        request.connection, peer = socket.socketpair()
        request.connection.settimeout(.05)
        self.addCleanup(request.connection.close)
        self.addCleanup(peer.close)
        received = []
        def read_body(request):
            reading.set()
            try:
                received.append(request.connection.recv(1))
            except OSError:
                received.append(b'')  # Windows may finish via the bounded socket timeout.
        handler._do_POST = read_body
        producer = threading.Thread(target=request.do_POST)
        producer.start()
        self.assertTrue(reading.wait(2))
        started = time.monotonic()
        handler.stop_mutations()
        self.assertLess(time.monotonic() - started, 1)
        producer.join(2)
        self.assertFalse(producer.is_alive())
        self.assertEqual(received, [b''])

    def test_failure_retries_at_bounded_cadence(self):
        times = []
        def build():
            times.append(time.monotonic())
            if len(times) == 1:
                raise ValueError('private payload must not be logged')
        worker = self.publisher(build)
        with self.assertLogs('app_backend.publisher', level='ERROR') as logs:
            worker.mark_dirty()
            self.assertTrue(worker.flush())
        self.assertNotIn('private payload', str(logs.output))
        self.assertGreaterEqual(times[1] - times[0], .019)
        self.assertEqual(worker.snapshot()['publish_failures'], 1)
        self.assertEqual(worker.snapshot()['published'], 1)

    def test_close_final_attempt_is_joined_and_no_late_marks(self):
        calls = []
        worker = self.publisher(lambda: calls.append(1))
        worker.mark_dirty()
        worker.close()
        worker.mark_dirty()
        worker.close()
        self.assertEqual(calls, [1])
        self.assertEqual(worker.snapshot()['dirty_marks'], 1)
        self.assertFalse(worker._thread.is_alive())

    def test_failed_final_attempt_does_not_retry_forever(self):
        def fail():
            raise ValueError()
        worker = self.publisher(fail)
        worker.mark_dirty()
        with self.assertLogs('app_backend.publisher', level='ERROR'):
            worker.close()
        self.assertFalse(worker.flush(.01))
        self.assertEqual(worker.snapshot()['publish_attempts'], 1)

    def test_shutdown_retry_also_obeys_cadence(self):
        entered, release = threading.Event(), threading.Event()
        times = []
        def fail():
            times.append(time.monotonic())
            if len(times) == 1:
                entered.set()
                self.assertTrue(release.wait(2))
            raise ValueError()
        worker = self.publisher(fail, .2)
        self.addCleanup(release.set)
        with self.assertLogs('app_backend.publisher', level='ERROR'):
            worker.mark_dirty()
            self.assertTrue(entered.wait(2))
            closer = threading.Thread(target=worker.close)
            closer.start()
            with worker._condition:
                self.assertTrue(worker._condition.wait_for(lambda: worker._closing, 2))
            release.set()
            closer.join(2)
            self.assertFalse(closer.is_alive())
        self.assertEqual(len(times), 2)
        self.assertGreaterEqual(times[1] - times[0], .199)

    def test_runtime_mutations_build_and_validate_only_on_publisher(self):
        state, store, broker = DashboardState(), ApplicationStateStore(), SseBroker()
        store.subscribe(lambda snapshot: broker.publish(live_envelope(snapshot)))
        runtime = DashboardRuntime(state, application_state_store=store)
        self.addCleanup(runtime.close)
        caller = threading.current_thread().name
        threads = []
        original = state.snapshot
        def build():
            threads.append(threading.current_thread().name)
            return original()
        from app_backend.privacy import assert_public_payload
        with patch.object(state, 'snapshot', side_effect=build), patch(
                'app_backend.contracts.assert_public_payload', wraps=assert_public_payload) as privacy:
            for index in range(100):
                runtime.update_body_position("SUN", index, 1, datetime.datetime.now(datetime.timezone.utc))
            self.assertEqual(threads, [])
            self.assertTrue(runtime.publisher.flush())
            self.assertEqual(threads, ['public-state-publisher'])
            self.assertNotIn(caller, threads)
            self.assertGreater(privacy.call_count, 0)
            self.assertEqual(store.snapshot()['revision'], 1)
            self.assertEqual(broker.last_revisions['live_state'], 1)

    def test_default_cadence_and_noop_mutations_leave_generation_clean(self):
        runtime = DashboardRuntime(DashboardState(), application_state_store=ApplicationStateStore())
        self.addCleanup(runtime.close)
        now = datetime.datetime(2026, 9, 12, tzinfo=datetime.timezone.utc)
        self.assertEqual(.2, runtime.publisher.snapshot()['interval_seconds'])
        runtime.clear_body_positions()
        runtime.invalidate_live(now)
        runtime.withdraw_aircraft('ABC123', now)
        runtime.withdraw_source('ABC123', 'SUN', object(), now)
        runtime.invalidate_source(object(), now)
        runtime.mark_history_worthy('ABC123', 'SUN')
        self.assertEqual(0, runtime.publisher.snapshot()['current_generation'])
        runtime.tick(now)
        self.assertTrue(runtime.publisher.flush())
        generation = runtime.publisher.snapshot()['current_generation']
        runtime.tick(now)
        self.assertEqual(generation, runtime.publisher.snapshot()['current_generation'])
        self.assertTrue(runtime.update_body_position('SUN', 1., 2., now))
        generation = runtime.publisher.snapshot()['current_generation']
        self.assertFalse(runtime.update_body_position('SUN', 1., 2., now))
        self.assertEqual(generation, runtime.publisher.snapshot()['current_generation'])

    def test_bootstrap_barrier_waits_and_returns_coherent_latest_state(self):
        from app_backend.settings import RuntimeSettingsStore
        state, store = DashboardState(), ApplicationStateStore()
        entered, release = threading.Event(), threading.Event()
        def build():
            entered.set()
            self.assertTrue(release.wait(2))
            store.publish(state.snapshot())
        publisher = self.publisher(build)
        self.addCleanup(release.set)
        now = datetime.datetime(2026, 9, 12, tzinfo=datetime.timezone.utc)
        state.tick(now)
        publisher.mark_dirty()
        self.assertTrue(entered.wait(2))
        handler = _handler_factory(state, lambda: now, None, None, store,
                                   RuntimeSettingsStore(), publisher=publisher)
        request = object.__new__(handler)
        request.path = '/api/v1/bootstrap'
        responses = []
        request._send_json = lambda value, status=200: responses.append((status, value))
        reader = threading.Thread(target=request.do_GET)
        reader.start()
        with publisher._condition:
            self.assertTrue(publisher._condition.wait_for(
                lambda: publisher._barrier_waits > 0, .1))
        self.assertEqual([], responses)
        release.set()
        reader.join(2)
        self.assertFalse(reader.is_alive())
        self.assertEqual(200, responses[0][0])
        self.assertEqual(1, store.snapshot()['revision'])
        self.assertEqual(1, publisher.snapshot()['published_generation'])

    def test_bootstrap_barrier_failure_returns_503(self):
        from unittest.mock import Mock
        publisher = Mock()
        publisher.flush.return_value = False
        handler = _handler_factory(DashboardState(), lambda: None, None, None,
                                   ApplicationStateStore(), publisher=publisher)
        request = object.__new__(handler)
        request.path = '/api/v1/bootstrap'
        responses = []
        request._send_json = lambda value, status=200: responses.append(status)
        request.do_GET()
        self.assertEqual([503], responses)

    def test_source_reset_and_new_owner_are_visible_after_inflight_snapshot(self):
        from live_dashboard import DashboardCandidate
        from dataclasses import replace
        now = datetime.datetime(2026, 9, 12, tzinfo=datetime.timezone.utc)
        candidate = DashboardCandidate(body='SUN', icao='ABC123', callsign='TEST',
            predicted_event_utc=now + datetime.timedelta(seconds=60),
            separation_deg=1., body_azimuth_deg=1., body_elevation_deg=2.,
            aircraft_elevation_deg=3., distance_km=4., telegram_range=False,
            last_prediction_update_utc=now, encounter_id='same')
        state, store = DashboardState(), ApplicationStateStore()
        entered, release = threading.Event(), threading.Event()
        snapshots = []
        def build():
            snapshot = state.snapshot()
            snapshots.append(snapshot)
            if len(snapshots) == 1:
                entered.set()
                self.assertTrue(release.wait(2))
            store.publish(snapshot)
        publisher = self.publisher(build)
        self.addCleanup(release.set)
        runtime = DashboardRuntime(state, application_state_store=store, publisher=publisher)
        old_owner = object()
        runtime.publish_authoritative(candidate, SimpleNamespace(), source_owner=old_owner)
        self.assertTrue(entered.wait(2))
        runtime.invalidate_live(now, reason='SOURCE_RESET')
        state.set_aircraft_source({'requested_mode': 'LOCAL', 'effective_mode': 'LOCAL'})
        runtime.publish(replace(candidate, callsign='LOCAL'))
        self.assertFalse(runtime.withdraw_source('ABC123', 'SUN', old_owner, now))
        release.set()
        self.assertTrue(publisher.flush())
        public = store.snapshot()['state']
        self.assertEqual('LOCAL', public['bodies']['sun']['candidates'][0]['callsign'])
        self.assertEqual('LOCAL', public['aircraft_source']['requested_mode'])
        self.assertEqual(2, len(snapshots))
        self.assertEqual(publisher.snapshot()['current_generation'],
                         publisher.snapshot()['published_generation'])

    def test_patch_and_delete_producers_are_gated_during_shutdown(self):
        for verb in ('PATCH', 'DELETE'):
            with self.subTest(verb=verb):
                handler = _handler_factory(DashboardState(), lambda: None, None, None)
                calls = []
                setattr(handler, '_do_' + verb, lambda request: calls.append('accepted'))
                request = object.__new__(handler)
                request._send_json = lambda value, status: calls.append(status)
                getattr(request, 'do_' + verb)()
                handler.stop_mutations()
                getattr(request, 'do_' + verb)()
                self.assertEqual(['accepted', 503], calls)

    def test_shutdown_reports_drain_success_and_failure(self):
        success = self.publisher(lambda: None)
        success.mark_dirty()
        success.close()
        self.assertTrue(success.snapshot()['shutdown_drained'])
        def fail():
            raise ValueError('private failure')
        failed = self.publisher(fail)
        failed.mark_dirty()
        with self.assertLogs('app_backend.publisher', level='ERROR'):
            failed.close()
        self.assertFalse(failed.snapshot()['shutdown_drained'])
        self.assertFalse(failed.snapshot()['worker_alive'])
