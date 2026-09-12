"""Notification-only handoff policy; synthetic identities, no network."""
import datetime as dt
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from telegram_notifications import TelegramNotifier, TransitNotification

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 7, 12, tzinfo=UTC)


def event(seconds=0, t0=60, encounter="one", owner="LOCAL", epoch=7, body="SUN", icao="ABC123"):
    return TransitNotification(
        NOW + dt.timedelta(seconds=seconds), body, icao, "TEST",
        NOW + dt.timedelta(seconds=t0), t0-seconds, .4, 120., 20., 20.1,
        40., encounter, "TRUE_2D", epoch, owner)


class SourceHandoffTests(unittest.TestCase):
    def setUp(self):
        self.transport = Mock()
        self.transport.send.return_value = (True, None)
        self.notifier = TelegramNotifier(self.transport, stability_seconds=0)
        self.addCleanup(self.notifier.close)

    def consider(self, item):
        self.notifier.observe_authoritative(item)
        return self.notifier.consider(item)

    def switch(self, seconds=1):
        self.notifier.invalidate_authoritative(
            source_handoff=True, now=NOW + dt.timedelta(seconds=seconds))

    def test_first_replacement_inherits_even_with_different_t0(self):
        for t0 in (61, 10000):
            with self.subTest(t0=t0):
                # Independent epoch for each case.
                epoch = t0
                self.assertTrue(self.consider(event(epoch=epoch)))
                self.switch()
                self.assertFalse(self.consider(event(2, t0, owner="REMOTE", epoch=epoch)))
                self.assertFalse(self.consider(event(119, t0, owner="REMOTE", epoch=epoch)))
                self.assertFalse(self.consider(event(120, t0, owner="REMOTE", epoch=epoch)))
                self.assertTrue(self.consider(event(120.001, t0 + 200, owner="REMOTE", epoch=epoch)))

    def test_later_encounter_after_expiry_not_suppressed(self):
        self.assertTrue(self.consider(event()))
        self.switch()
        self.assertFalse(self.consider(event(2, owner="REMOTE")))
        self.assertTrue(self.consider(event(121, 180, "later", "REMOTE")))

    def test_second_encounter_does_not_inherit_before_old_expiry(self):
        self.assertTrue(self.consider(event()))
        self.switch()
        self.assertFalse(self.consider(event(2, owner="REMOTE")))
        self.assertTrue(self.consider(event(3, 90, "second", "REMOTE")))

    def test_inherited_suppression_does_not_chain_through_another_switch(self):
        self.assertTrue(self.consider(event()))
        self.switch()
        self.assertFalse(self.consider(event(2, owner="REMOTE")))
        self.switch(3)
        self.assertTrue(self.consider(event(4, 90, "second", "LOCAL")))

    def test_epoch_icao_and_body_must_match(self):
        for changes in ({"epoch": 8}, {"icao": "DEF456"}, {"body": "MOON"}):
            with self.subTest(changes=changes):
                other = TelegramNotifier(self.transport, stability_seconds=0)
                self.addCleanup(other.close)
                original = event()
                other.observe_authoritative(original)
                self.assertTrue(other.consider(original))
                other.invalidate_authoritative(source_handoff=True, now=NOW)
                successor = event(2, owner="REMOTE", **changes)
                other.observe_authoritative(successor)
                self.assertTrue(other.consider(successor))

    def test_observer_invalidation_clears_unclaimed_handoff(self):
        self.assertTrue(self.consider(event()))
        self.switch()
        self.notifier.invalidate_authoritative(source_handoff=False, now=NOW)
        self.assertTrue(self.consider(event(2, owner="REMOTE")))

    def test_cold_start_and_switch_without_notified_predecessor(self):
        self.switch()
        self.assertTrue(self.consider(event(owner="REMOTE")))
        pending = event(1, encounter="disabled", icao="DEF456")
        self.notifier.observe_authoritative(pending)
        self.notifier.suppress(pending)
        self.switch(2)
        self.assertTrue(self.consider(event(3, owner="REMOTE", icao="DEF456")))

    def test_pending_but_unsent_predecessor_does_not_seed_handoff(self):
        self.notifier._stability_seconds = 5
        self.assertFalse(self.consider(event()))
        self.switch()
        self.assertFalse(self.consider(event(2, owner="REMOTE")))
        self.assertTrue(self.consider(event(7, owner="REMOTE")))
        self.assertNotIn("SKIPPED_SOURCE_HANDOFF", self.notifier.diagnostics())

    def test_auto_owner_change_uses_same_first_replacement_rule(self):
        self.assertTrue(self.consider(event()))
        self.assertFalse(self.consider(event(1, 200, "remote", "AUTO_REMOTE")))
        self.assertFalse(self.consider(event(2, 250, "remote", "AUTO_REMOTE")))
        self.assertTrue(self.consider(event(3, 150, "later", "LOCAL")))

    def test_auto_revisiting_original_id_cannot_reseed_its_old_notification(self):
        self.assertTrue(self.consider(event()))
        self.assertFalse(self.consider(event(1, 200, "remote", "AUTO_REMOTE")))
        self.assertFalse(self.consider(event(2)))  # Original LOCAL ID is already seen.
        self.assertTrue(self.consider(event(3, 90, "second_remote", "AUTO_REMOTE")))

    def test_revisiting_recipient_cannot_extend_or_renew_inheritance(self):
        self.assertTrue(self.consider(event()))
        self.assertFalse(self.consider(event(1, 200, "remote", "AUTO_REMOTE")))
        self.assertTrue(self.consider(event(2, 300, "second_local")))
        self.assertFalse(self.consider(event(3, 200, "remote", "AUTO_REMOTE")))
        self.assertTrue(self.consider(event(121, 200, "remote", "AUTO_REMOTE")))

    def test_first_previously_observed_encounter_cannot_gain_handoff_later(self):
        first = event(owner="AUTO_REMOTE")
        self.notifier.observe_authoritative(first)  # Outside range; never notified.
        self.assertTrue(self.consider(event(1, 90, "local")))
        self.assertTrue(self.consider(event(2, 90, owner="AUTO_REMOTE")))

    def test_withdrawn_unsent_encounter_cannot_rearm_or_seed_handoff(self):
        self.notifier.observe_authoritative(event())
        pred = SimpleNamespace(observer_epoch=7, icao="ABC123", body="SUN", encounter_id="one")
        self.notifier.withdraw_authoritative(pred, "LOCAL")
        self.assertFalse(self.consider(event(2)))
        self.switch(3)
        self.assertTrue(self.consider(event(4, 90, "new", "REMOTE")))

    def test_ordinary_new_encounter_is_not_general_time_window_matching(self):
        self.assertTrue(self.consider(event()))
        self.assertTrue(self.consider(event(1, 61, "different")))

    def test_first_out_of_range_replacement_consumes_token(self):
        self.assertTrue(self.consider(event()))
        self.switch()
        self.notifier.observe_authoritative(event(1, 10000, "outside", "REMOTE"))
        self.assertTrue(self.consider(event(2, 90, "second", "REMOTE")))

    def test_revisions_do_not_resend_or_restart_stability(self):
        self.notifier._stability_seconds = 5
        self.assertFalse(self.consider(event(owner="REMOTE")))
        self.assertFalse(self.consider(event(4, 80, owner="REMOTE")))
        self.assertTrue(self.consider(event(5, 90, owner="REMOTE")))
        self.assertFalse(self.consider(event(6, 100, owner="REMOTE")))
        self.assertEqual(1, self.notifier.diagnostics()["ENQUEUED"])

    def test_withdrawal_cancels_pending_and_old_owner_cannot_cancel_successor(self):
        self.notifier._stability_seconds = 5
        self.assertFalse(self.consider(event()))
        self.assertFalse(self.consider(event(1, owner="REMOTE")))
        pred = SimpleNamespace(observer_epoch=7, icao="ABC123", body="SUN", encounter_id="one")
        self.notifier.withdraw_authoritative(pred, "LOCAL")
        self.assertTrue(self.consider(event(6, owner="REMOTE")))
        self.notifier.withdraw_authoritative(pred, "REMOTE")
        # A repeat of the same closed encounter remains deduplicated.
        self.assertFalse(self.consider(event(7, owner="REMOTE")))

    def test_queue_failure_cannot_seed_handoff(self):
        import queue
        with patch.object(self.notifier._queue, "put_nowait", side_effect=queue.Full):
            self.assertFalse(self.consider(event()))
        self.switch()
        self.assertTrue(self.consider(event(2, owner="REMOTE")))

    def test_diagnostics_are_counts_not_identifiers(self):
        self.consider(event())
        self.switch()
        self.consider(event(2, owner="REMOTE"))
        self.assertEqual({"ENQUEUED": 1, "SKIPPED_SOURCE_HANDOFF": 1}, self.notifier.diagnostics())

    def test_normalized_identity_and_inherited_expiry_are_exact(self):
        predecessor = event(icao='abc123', body='sun')
        self.assertTrue(self.consider(predecessor))
        expiry = self.notifier._events[self.notifier._event_key(predecessor)]
        self.switch()
        successor = event(2, 10000, owner='REMOTE')
        self.assertFalse(self.consider(successor))
        key = self.notifier._event_key(successor)
        self.assertEqual(NOW + dt.timedelta(seconds=120), expiry)
        self.assertEqual(expiry, self.notifier._inherited[key])
        self.assertFalse(self.consider(event(100, 20000, owner='REMOTE')))
        self.assertEqual(expiry, self.notifier._inherited[key])
        self.assertEqual(100, self.notifier._queue.maxsize)



if __name__ == "__main__":
    unittest.main()
