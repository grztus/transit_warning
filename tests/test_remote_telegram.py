"""Production remote transition routing with fake provider/solver/transport."""
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import transit_warning as runtime
from authoritative_transit import AuthoritativeTransition, AuthoritativeTransitionKind
from live_dashboard import DashboardRuntime, DashboardState
from observer_position import ObserverPosition, RuntimeObserverPositionProvider
from telegram_notifications import TelegramNotifier
from tools.adsblol_live import normalize_response
from tools.adsblol_standalone_runtime import SnapshotPoller
from tests.test_adsblol_live import NOW, fixture
from tests.test_authoritative_transit import result
from tests.test_authoritative_consumers import prediction
from source_geometry_fixture import ConstantGeoid


class RemoteTelegramTests(unittest.TestCase):
    def setUp(self):
        self.now, self.mono = NOW, 100.
        self.observer = RuntimeObserverPositionProvider(ObserverPosition(1, 2, 3))
        self.provider = Mock()
        self.poller = SnapshotPoller(self.observer, provider=self.provider,
            now=lambda: self.now, monotonic=lambda: self.mono)
        self.dashboard = DashboardRuntime(DashboardState())
        self.dashboard.state.notification_finalized = runtime.finalize_authoritative_notification
        self.transport = Mock()
        self.transport.send.return_value = (True, None)
        self.notifier = TelegramNotifier(self.transport, stability_seconds=0)
        self.addCleanup(self.notifier.close)
        self.enterContext(patch.object(runtime, "telegram_notifier", self.notifier))
        self.enterContext(patch.object(runtime, "dashboard_runtime", self.dashboard))
        self.enterContext(patch.object(runtime, "clock", SimpleNamespace(now_utc=lambda: self.now)))
        self.enterContext(patch.object(runtime, "deferred_true2d", None))
        self.enterContext(patch.object(runtime, "telegram_alert_horizon_seconds", 300.))
        self.enterContext(patch.object(runtime, "telegram_alert_separation_deg", 2.))
        self.bridge = self.make_bridge()

    def make_bridge(self):
        return runtime.ProductionInternetBridge(self.poller, self.dashboard,
            ConstantGeoid(35), solver=Mock(return_value=result(seconds=60)),
            source="ADSBLOL", source_mode="INTERNET")

    def acquire(self, payload=None):
        report = dict(normalize_response(payload or fixture(), self.now, self.mono),
            status="OK", retry_after_seconds=None, response_latency_seconds=.1,
            request_finished_monotonic=self.mono)
        self.provider.acquire.return_value = report
        self.poller.fetch_once()
        self.bridge.step()
        return report

    def advance(self, seconds):
        self.now += dt.timedelta(seconds=seconds)
        self.mono += seconds

    def count(self):
        return self.notifier.diagnostics().get("ENQUEUED", 0)

    def test_internet_cold_start_inside_range_sun_and_moon(self):
        self.acquire()
        self.assertEqual(2, self.count())
        for body in ("sun", "moon"):
            candidate = self.dashboard.state.snapshot(self.now)[body]["candidates"][0]
            self.assertEqual("TELEGRAM RANGE", candidate["state"])
        self.advance(1)
        self.acquire()
        self.assertEqual(2, self.count())

    def test_disabled_bodies_still_block(self):
        for body in ("SUN", "MOON"):
            self.dashboard.set_telegram_enabled(body, False)
        self.acquire()
        self.assertEqual(0, self.count())
        self.assertEqual(2, self.notifier.diagnostics()["SKIPPED_BODY_DISABLED"])

    def test_internet_cold_start_obeys_existing_five_second_stability(self):
        self.notifier._stability_seconds = 5
        self.acquire()
        self.assertEqual(0, self.count())
        self.advance(5)
        self.acquire()
        self.assertEqual(2, self.count())

    def test_range_and_horizon_boundaries_still_block(self):
        for seconds, sep in ((60, 2.), (301, .1)):
            self.bridge.solver.return_value = result(seconds=seconds, separation=sep)
            self.acquire()
        self.assertEqual(0, self.count())

    def test_range_rejected_predecessor_does_not_suppress_source_replacement(self):
        self.bridge.solver.return_value = result(seconds=60, separation=2.)
        self.acquire()
        runtime.invalidate_observer_dependent_state(reason="SOURCE_RESET")
        self.bridge = self.make_bridge()
        self.acquire()
        self.assertEqual(2, self.count())
        self.assertNotIn("SKIPPED_SOURCE_HANDOFF", self.notifier.diagnostics())

    def test_stale_unknown_position_age_and_invalid_solution_do_not_notify(self):
        for age in (21, None):
            payload = fixture()
            if age is None:
                payload["ac"][0].pop("seen_pos")
            else:
                payload["ac"][0]["seen_pos"] = age
            self.acquire(payload)
        self.bridge.solver.return_value = result(succeeded=False)
        self.acquire()
        self.assertEqual(0, self.count())

    def test_unknown_individual_field_age_keeps_authoritative_eligibility(self):
        report = self.acquire()
        self.assertIsNone(report["aircraft"][0]["fields"]["groundspeed"]["age_seconds"])
        self.assertEqual(2, self.count())

    def test_expiry_and_withdrawal_cancel_pending_stabilization(self):
        self.notifier._stability_seconds = 5
        self.acquire()
        self.assertEqual(2, len(self.notifier._pending))
        self.advance(21)
        self.bridge.step()
        self.assertEqual({}, self.notifier._pending)
        self.assertEqual(0, self.count())

    def test_passed_and_held_do_not_send(self):
        item = replace(prediction(), observer_epoch=self.observer.resolve(self.now).epoch,
            predicted_transit_utc=self.now-dt.timedelta(seconds=1))
        transition = AuthoritativeTransition(AuthoritativeTransitionKind.UPDATED, item)
        self.bridge._on_transition(transition, None, self.now)
        self.assertEqual(1, self.notifier.diagnostics()["SKIPPED_SOURCE_POLICY"])
        held = AuthoritativeTransition(AuthoritativeTransitionKind.HELD,
            replace(item, predicted_transit_utc=self.now+dt.timedelta(seconds=60)))
        self.bridge._on_transition(held, None, self.now)
        finalized = replace(held.prediction, lifecycle_state="PASSED")
        self.assertFalse(runtime.emit_authoritative_transit_notification(finalized, self.now))
        self.assertEqual(0, self.count())

    def test_actual_passed_cancels_unsent_and_blocks_same_id_rearm(self):
        self.notifier._stability_seconds = 5
        self.bridge.solver.return_value = result(seconds=1)
        self.acquire()
        item = self.bridge.lifecycle.active_prediction(
            self.observer.resolve(self.now).epoch, "ABC123", "SUN")
        self.advance(1)
        self.dashboard.tick(self.now)
        self.assertEqual({}, self.notifier._pending)
        for seconds in (0, 6):
            self.advance(seconds)
            attempted = replace(item, predicted_transit_utc=self.now+dt.timedelta(seconds=60))
            self.bridge._on_transition(AuthoritativeTransition(
                AuthoritativeTransitionKind.UPDATED, attempted), None, self.now)
        self.assertEqual(0, self.count())

    def test_passed_notified_encounter_cannot_seed_source_handoff(self):
        self.bridge.solver.return_value = result(seconds=1)
        self.acquire()
        self.assertEqual(2, self.count())
        self.advance(1)
        self.dashboard.tick(self.now)
        runtime.invalidate_observer_dependent_state(reason="SOURCE_RESET")
        self.bridge = self.make_bridge()
        self.acquire()
        self.assertEqual(4, self.count())
        self.assertNotIn("SKIPPED_SOURCE_HANDOFF", self.notifier.diagnostics())

    def test_actual_withdrawal_blocks_old_id_but_new_generation_can_notify(self):
        self.notifier._stability_seconds = 5
        self.acquire()
        item = self.bridge.lifecycle.active_prediction(
            self.observer.resolve(self.now).epoch, "ABC123", "SUN")
        self.dashboard.withdraw_aircraft("ABC123", self.now)
        self.assertEqual({}, self.notifier._pending)
        for seconds in (0, 5):
            self.advance(seconds)
            self.bridge._on_transition(AuthoritativeTransition(
                AuthoritativeTransitionKind.UPDATED, item), None, self.now)
        self.assertEqual(0, self.count())
        self.bridge.lifecycle.discard_aircraft_transitions("ABC123")
        self.acquire()
        self.advance(5)
        self.acquire()
        self.assertEqual(2, self.count())

    def test_notification_callback_failure_does_not_interrupt_finalization(self):
        self.acquire()
        self.dashboard.state.notification_finalized = Mock(side_effect=RuntimeError("test"))
        self.dashboard.withdraw_aircraft("ABC123", self.now)
        self.assertEqual([], self.dashboard.state.snapshot(self.now)["sun"]["candidates"])
        self.assertEqual(2, len(self.dashboard.state.query_history()["records"]))

    def test_actual_source_reset_clears_table_then_repopulates_without_duplicate(self):
        # A LOCAL authoritative candidate and notification precede provider handoff.
        observer = self.observer.resolve(self.now)
        ctx = SimpleNamespace(observer_context=observer)
        item = replace(prediction(), observer_epoch=observer.epoch,
            predicted_transit_utc=self.now+dt.timedelta(seconds=60))
        self.assertTrue(runtime.emit_authoritative_transit_notification(item, self.now))
        self.bridge._publish(item, ctx, self.now)
        self.assertTrue(self.dashboard.state.snapshot(self.now)["sun"]["candidates"])
        with (patch.object(runtime, "aircraft_source_mode", "LOCAL"),
              patch.object(runtime, "internet_source_poller", None),
              patch.object(runtime, "internet_source_bridge", None),
              patch.object(runtime, "aircraft_los_geoid_provider", ConstantGeoid(35)),
              patch.object(runtime, "SnapshotPoller", return_value=self.poller),
              patch.object(self.poller, "start")):
            runtime.set_aircraft_source_mode("INTERNET")
            self.bridge = runtime.internet_source_bridge
            self.bridge.solver = Mock(return_value=result(seconds=60))
            self.assertEqual([], self.dashboard.state.snapshot(self.now)["sun"]["candidates"])
            self.acquire()
        self.assertEqual(2, self.count())  # LOCAL SUN + previously unnotified MOON.
        self.assertTrue(self.dashboard.state.snapshot(self.now)["sun"]["candidates"])
        self.assertEqual(1, self.notifier.diagnostics()["SKIPPED_SOURCE_HANDOFF"])

    def test_recreated_bridge_has_distinct_encounter_identity(self):
        self.acquire()
        old = self.dashboard.state.snapshot(self.now)["sun"]["candidates"][0]["encounter_id"]
        runtime.invalidate_observer_dependent_state(reason="SOURCE_RESET")
        self.bridge = self.make_bridge()
        self.acquire()
        new = self.dashboard.state.snapshot(self.now)["sun"]["candidates"][0]["encounter_id"]
        self.assertNotEqual(old, new)
        self.assertEqual(2, self.count())  # First authority replacement inherits.

    def test_observer_invalidation_before_notification_blocks_old_result(self):
        observer = self.observer.resolve(self.now)
        item = replace(prediction(), observer_epoch=observer.epoch,
            predicted_transit_utc=self.now+dt.timedelta(seconds=60))
        self.observer.set_mode("MOBILE", self.now)
        self.bridge._on_transition(AuthoritativeTransition(
            AuthoritativeTransitionKind.OPENED, item), None, self.now)
        self.assertEqual(0, self.count())

    def test_internet_local_internet_does_not_chain_inherited_suppression(self):
        self.acquire()  # Both remote bodies actually notified.
        observer = self.observer.resolve(self.now)
        with (patch.object(runtime, "aircraft_source_mode", "INTERNET"),
              patch.object(runtime, "internet_source_poller", self.poller),
              patch.object(runtime, "internet_source_bridge", self.bridge),
              patch.object(runtime, "aircraft_los_geoid_provider", ConstantGeoid(35)),
              patch.object(runtime, "SnapshotPoller", return_value=self.poller),
              patch.object(self.poller, "start"), patch.object(self.poller, "close")):
            runtime.set_aircraft_source_mode("LOCAL")
            for body in ("SUN", "MOON"):
                local = replace(prediction(), body=body, observer_epoch=observer.epoch,
                    predicted_transit_utc=self.now+dt.timedelta(seconds=60))
                self.assertFalse(runtime.emit_authoritative_transit_notification(local, self.now))
            self.assertEqual(2, self.count())
            runtime.set_aircraft_source_mode("INTERNET")
            self.bridge = runtime.internet_source_bridge
            self.bridge.solver = Mock(return_value=result(seconds=60))
            self.poller.stop.clear()  # This fixture reuses a poller; production constructs a fresh one.
            self.acquire()
        # The second replacement does not inherit from a suppressed predecessor.
        self.assertEqual(4, self.count())

    def test_auto_remote_candidate_and_local_authority_handoff(self):
        self.bridge = runtime.AutoFusionBridge(self.poller, self.dashboard, ConstantGeoid(35))
        self.bridge.solver = Mock(return_value=result(seconds=60))
        with (patch.object(runtime, "adsblol_auto_cache") as cache,
              patch.object(runtime, "local_fusion_fields", return_value={}),
              patch.object(runtime, "capture_authoritative_transit_prediction")):
            cache.receipt_monotonic = self.mono
            cache.health = "OK"
            self.acquire()
        self.assertEqual(2, self.count())
        observer = self.observer.resolve(self.now)
        item = replace(prediction(), observer_epoch=observer.epoch,
            predicted_transit_utc=self.now+dt.timedelta(seconds=70))
        self.assertFalse(runtime.emit_authoritative_transit_notification(item, self.now))
        self.assertEqual(1, self.notifier.diagnostics()["SKIPPED_SOURCE_HANDOFF"])

    def test_each_body_toggle_is_independent_and_disabled_body_cannot_seed_handoff(self):
        for disabled in ('SUN', 'MOON'):
            with self.subTest(disabled=disabled):
                self.dashboard.set_telegram_enabled('SUN', True)
                self.dashboard.set_telegram_enabled('MOON', True)
                self.dashboard.state.invalidate_live(self.now)
                runtime.invalidate_observer_dependent_state(reason='OBSERVER_INVALIDATED')
                self.bridge = self.make_bridge()
                before = self.count()
                self.dashboard.set_telegram_enabled(disabled, False)
                self.acquire()
                self.assertEqual(before + 1, self.count())
                runtime.invalidate_observer_dependent_state(reason='SOURCE_RESET')
                self.dashboard.set_telegram_enabled(disabled, True)
                self.bridge = self.make_bridge()
                self.acquire()
                # Only the body that actually enqueued can seed inheritance.
                self.assertEqual(before + 2, self.count())

    def test_all_six_source_direction_handoffs_use_the_same_identity(self):
        directions = [('LOCAL', 'INTERNET'), ('INTERNET', 'LOCAL'),
                      ('LOCAL', 'AUTO'), ('AUTO', 'LOCAL'),
                      ('INTERNET', 'AUTO'), ('AUTO', 'INTERNET')]
        for origin, destination in directions:
            with self.subTest(origin=origin, destination=destination):
                notifier = TelegramNotifier(self.transport, stability_seconds=0)
                self.addCleanup(notifier.close)
                self.dashboard.state.invalidate_live(self.now)
                def poller_factory(*args):
                    self.poller = SnapshotPoller(self.observer, provider=self.provider,
                        now=lambda: self.now, monotonic=lambda: self.mono)
                    self.poller.start = Mock()
                    return self.poller
                def notify_source(mode):
                    runtime.set_aircraft_source_mode(mode)
                    if mode == 'LOCAL':
                        observer = self.observer.resolve(self.now)
                        for body in ('SUN', 'MOON'):
                            item = replace(prediction(), observer_epoch=observer.epoch, body=body,
                                predicted_transit_utc=self.now+dt.timedelta(seconds=60))
                            runtime.emit_authoritative_transit_notification(item, self.now)
                    else:
                        self.bridge = runtime.internet_source_bridge
                        self.bridge.solver = Mock(return_value=result(seconds=60))
                        self.acquire()
                with (patch.object(runtime, 'telegram_notifier', notifier),
                      patch.object(runtime, 'aircraft_source_mode', 'LOCAL'),
                      patch.object(runtime, 'internet_source_poller', None),
                      patch.object(runtime, 'internet_source_bridge', None),
                      patch.object(runtime, 'aircraft_los_geoid_provider', ConstantGeoid(35)),
                      patch.object(runtime, 'SnapshotPoller', side_effect=poller_factory),
                      patch.object(runtime, 'local_fusion_fields', return_value={}),
                      patch.object(runtime, 'capture_authoritative_transit_prediction'),
                      patch.object(runtime, 'adsblol_auto_cache') as cache):
                    cache.receipt_monotonic = self.mono
                    cache.health = 'OK'
                    notify_source(origin)
                    self.assertEqual(2, notifier.diagnostics().get('ENQUEUED', 0))
                    notify_source(destination)
                    self.assertEqual(2, notifier.diagnostics().get('ENQUEUED', 0))
                    self.assertEqual(2, notifier.diagnostics().get('SKIPPED_SOURCE_HANDOFF', 0))

    def test_local_recovery_rejection_does_not_notify_from_remote_transition(self):
        self.bridge = runtime.AutoFusionBridge(self.poller, self.dashboard, ConstantGeoid(35))
        self.bridge.solver = Mock(return_value=result(seconds=60))
        # Freeze a valid remote context, then make LOCAL recover before publication.
        with (patch.object(runtime, 'adsblol_auto_cache') as cache,
              patch.object(runtime, 'local_fusion_fields', return_value={}),
              patch.object(runtime, 'capture_authoritative_transit_prediction')):
            cache.receipt_monotonic = self.mono
            cache.health = 'OK'
            def rejected_publication(*args):
                with patch.object(runtime, 'predictor_view', return_value=None):
                    return runtime.AutoFusionBridge._publish(self.bridge, *args)
            with patch.object(self.bridge, '_publish', side_effect=rejected_publication):
                self.acquire()
        self.assertEqual(0, self.count())
        self.assertEqual(2, self.notifier.diagnostics()['SKIPPED_SOURCE_POLICY'])

    def test_late_remote_transition_cannot_notify_after_local_owned_replacement(self):
        self.notifier._stability_seconds = 5
        self.acquire()
        observer = self.observer.resolve(self.now)
        item = self.bridge.lifecycle.active_prediction(observer.epoch, 'ABC123', 'SUN')
        candidate = self.dashboard.state._live['SUN']['ABC123']['candidate']
        self.dashboard.publish(candidate)
        self.advance(5)
        self.bridge._on_transition(AuthoritativeTransition(
            AuthoritativeTransitionKind.UPDATED, item), None, self.now)
        self.assertEqual(0, self.count())
        self.assertEqual(1, self.notifier.diagnostics()['SKIPPED_SOURCE_POLICY'])

    def test_journal_failure_does_not_prevent_notification_finalization(self):
        self.notifier._stability_seconds = 5
        self.acquire()
        self.dashboard.state.finalization_journal = Mock()
        self.dashboard.state.finalization_journal.append.side_effect = OSError('diagnostics')
        self.dashboard.withdraw_aircraft('ABC123', self.now)
        self.assertEqual({}, self.notifier._pending)
        self.assertEqual({}, self.notifier._authoritative)
        self.assertEqual([], self.dashboard.state.snapshot(self.now)['sun']['candidates'])

    def test_remote_notifications_do_not_wait_for_coalesced_publication(self):
        import threading
        from app_backend.publisher import PublicStatePublisher
        from app_backend.state import ApplicationStateStore
        store = ApplicationStateStore()
        entered, release = threading.Event(), threading.Event()
        def publish():
            entered.set()
            self.assertTrue(release.wait(2))
            store.publish(self.dashboard.state.snapshot())
        publisher = PublicStatePublisher(publish, .02)
        self.addCleanup(publisher.close)
        self.addCleanup(release.set)
        self.dashboard.publisher = publisher
        publisher.mark_dirty()
        self.assertTrue(entered.wait(2))
        self.acquire()
        self.advance(1)
        self.acquire()
        self.assertEqual(2, self.count())
        self.assertEqual(0, store.snapshot()['revision'])
        release.set()
        self.assertTrue(publisher.flush())
        self.assertGreater(publisher.snapshot()['coalesced_marks'], 0)

    def test_publication_failure_does_not_suppress_authoritative_telegram(self):
        from app_backend.publisher import PublicStatePublisher
        publisher = PublicStatePublisher(Mock(side_effect=ValueError('publication')), .02)
        self.addCleanup(publisher.close)
        self.dashboard.publisher = publisher
        with self.assertLogs('app_backend.publisher', level='ERROR'):
            self.acquire()
            with publisher._condition:
                self.assertTrue(publisher._condition.wait_for(lambda: publisher._failures > 0, 2))
            self.advance(1)
            self.acquire()
            self.assertEqual(2, self.count())
            self.assertEqual(0, publisher.snapshot()['published'])
            publisher._publish = lambda: None
            self.assertTrue(publisher.flush())


if __name__ == "__main__":
    unittest.main()
