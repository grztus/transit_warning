"""Retention equivalence against the exact previous algorithm, not timing gates."""
import datetime as dt
import random
import threading
import unittest
from unittest.mock import patch

from transit_snapshot import TransitSnapshotManager, BUFFER_MAXLEN, _ObservationBuffer
from tools.benchmark_observation_buffer import LegacyObservationManager, observation, prediction, BASE


class ObservationBufferTests(unittest.TestCase):
    def setUp(self):
        self.old = LegacyObservationManager(git_commit='test')
        self.new = TransitSnapshotManager(git_commit='test')

    def equal(self):
        self.assertEqual({key: list(value) for key, value in self.old._buffers.items()},
                         {key: list(value) for key, value in self.new._buffers.items()})
        self.assertEqual(self.old._buffer_last_seen, self.new._buffer_last_seen)
        self.assertEqual(self.old.active_events, self.new.active_events)
        self.assertEqual(self.old._recent_events, self.new._recent_events)

    def record(self, item):
        self.assertEqual(self.old.record_observation(item), self.new.record_observation(item))
        self.equal()

    def test_random_replay_out_of_order_bursts_and_aircraft_isolation(self):
        rng = random.Random(2026)
        for index in range(1500):
            stamp = index / 10 + rng.choice((0, 0, 0, -2, -30, -31, 65, -150))
            self.record(observation(str(rng.randrange(4)), stamp, index))
            if index % 100 == 0:
                now = BASE + dt.timedelta(seconds=stamp + 61)
                self.old.cleanup(now)
                self.new.cleanup(now)
                self.equal()

    def test_arrival_order_equal_timestamps_cutoff_and_maxlen(self):
        for index, seconds in enumerate((0, 30, 15, 0, -.000001, 30.000001, 1, 31)):
            self.record(observation('A', seconds, index))
        for index in range(BUFFER_MAXLEN + 50):
            self.record(observation('A', 31, index))
        self.assertEqual(len(self.new._buffers['A']), BUFFER_MAXLEN)
        self.assertEqual(self.new._buffers['A'][-1]['sequence'], BUFFER_MAXLEN + 49)

    def test_exact_retention_and_stale_boundaries(self):
        for seconds, retained in ((29.999999, True), (30, True), (30.000001, False)):
            with self.subTest(retention_seconds=seconds):
                for factory in (LegacyObservationManager, TransitSnapshotManager):
                    manager = factory(git_commit='test')
                    self.assertTrue(manager.record_observation(observation('A', 0, 1)))
                    self.assertTrue(manager.record_observation(observation('A', seconds, 2)))
                    self.assertEqual(any(row['sequence'] == 1 for row in manager._buffers['A']), retained)
        for seconds, retained in ((59.999999, True), (60, True), (60.000001, False)):
            with self.subTest(stale_seconds=seconds):
                for factory in (LegacyObservationManager, TransitSnapshotManager):
                    manager = factory(git_commit='test')
                    manager.record_observation(observation('A', 0))
                    self.assertTrue(manager.cleanup(BASE + dt.timedelta(seconds=seconds)))
                    self.assertEqual('A' in manager._buffers, retained)

    def test_out_of_order_burst_cap_and_expired_late_input(self):
        offsets = (30, 0, 29, 5, 15, 30)
        for index in range(BUFFER_MAXLEN * 2):
            self.record(observation('A', offsets[index % len(offsets)], index))
            self.assertLessEqual(len(self.new._buffers['A']), BUFFER_MAXLEN)
        self.assertEqual([row['sequence'] for row in self.new._buffers['A']],
                         list(range(BUFFER_MAXLEN, BUFFER_MAXLEN * 2)))
        for seconds in (31, -1000, 61, 32, 62):
            self.record(observation('A', seconds))

    def test_cached_cleanup_preserves_invalid_timestamp_failures(self):
        for bad in (None, 'invalid', dt.datetime.min.replace(tzinfo=dt.timezone.utc)):
            with self.subTest(timestamp=bad):
                for manager in (self.old, self.new):
                    self.assertTrue(manager.cleanup(BASE))
                    self.assertFalse(manager.cleanup(bad))
                    self.assertIsNotNone(manager.last_error)

    def test_removal_recreation_updates_deadline_even_after_clock_rewinds(self):
        self.record(observation('A', 100))
        for manager in (self.old, self.new):
            manager.drop_aircraft_buffer('A')
            manager.cleanup(BASE + dt.timedelta(seconds=160))
        self.equal()
        self.record(observation('A', -100))
        for manager in (self.old, self.new):
            manager.cleanup(BASE - dt.timedelta(seconds=39))
        self.equal()
        self.assertNotIn('A', self.new._buffers)

    def test_finalization_and_shutdown_remove_active_protection_on_next_cleanup(self):
        for finalize in (True, False):
            with self.subTest(finalize=finalize):
                managers = [factory(git_commit='test') for factory in
                            (LegacyObservationManager, TransitSnapshotManager)]
                documents = [[], []]
                for manager, captured in zip(managers, documents):
                    manager._write_document = lambda document, now, captured=captured: captured.append(document)
                    manager.record_observation(observation('000000', 30))
                    manager.consider_prediction(prediction())
                    manager.cleanup(BASE + dt.timedelta(seconds=200))
                    self.assertIn('000000', manager._buffers)
                    if finalize:
                        manager.finalize_due(BASE + dt.timedelta(seconds=200))
                    else:
                        manager.close(BASE + dt.timedelta(seconds=200))
                    manager.cleanup(BASE + dt.timedelta(seconds=200))
                    self.assertNotIn('000000', manager._buffers)
                self.assertEqual(documents[0], documents[1])

    def test_ordered_capture_never_scans_surviving_samples_or_all_aircraft(self):
        class NoScan(dict):
            def items(self):
                raise AssertionError('unexpected full cleanup scan')
        for count in (30, 100, 300):
            manager = TransitSnapshotManager(git_commit='test')
            for index in range(count):
                manager.record_observation(observation(str(index), 0))
            buffer = manager._buffers['0']
            manager._buffer_last_seen = NoScan(manager._buffer_last_seen)
            manager._recent_events = NoScan(manager._recent_events)
            with patch.object(_ObservationBuffer, '__iter__', side_effect=AssertionError('sample scan')):
                self.assertTrue(manager.record_observation(observation('0', 30)))
                self.assertTrue(manager.record_observation(observation('0', 31)))
            self.assertIs(manager._buffers['0'], buffer)
            self.assertEqual([sample['timestamp_utc'] for sample in buffer],
                             [BASE + dt.timedelta(seconds=value) for value in (30, 31)])

    def test_equivalent_aware_timezones_do_not_change_retention(self):
        for index, seconds in enumerate((0, 30, 15, 31, 61)):
            item = observation('A', seconds, index)
            item['timestamp_utc'] = item['timestamp_utc'].astimezone(
                dt.timezone(dt.timedelta(hours=(index % 3)-1)))
            self.record(item)

    def test_expiry_index_does_not_reject_maximum_aware_timestamp(self):
        item = observation('A', 0)
        item['timestamp_utc'] = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        self.record(item)
        self.assertIn('A', self.new._buffers)
        self.old.cleanup(item['timestamp_utc'])
        self.new.cleanup(item['timestamp_utc'])
        self.equal()

    def test_strict_stale_boundary_idle_and_backward_replay(self):
        self.record(observation('A', 0))
        for seconds in (60, 59, 60.000001, -50):
            now = BASE + dt.timedelta(seconds=seconds)
            self.old.cleanup(now)
            self.new.cleanup(now)
            self.equal()
        self.assertNotIn('A', self.new._buffers)
        self.record(observation('A', -10))
        self.record(observation('B', -100))

    def test_owned_nested_values_and_snapshot_output_equivalence(self):
        for seconds in (26, 29, 28, 29, 30):
            item = observation('000000', seconds)
            self.record(item)
            item['parameter_sources']['position'] = 'changed'
        for manager in (self.old, self.new):
            manager.consider_prediction(prediction())
        old_event = next(iter(self.old._active.values()))
        new_event = next(iter(self.new._active.values()))
        old = self.old._document(old_event, BASE, True, 'test')
        new = self.new._document(new_event, BASE, True, 'test')
        self.assertEqual(old, new)
        self.assertNotIn('changed', str(new))
        new['observations'][0]['parameter_sources']['position'] = 'external'
        self.equal()

    def test_active_protection_invalidation_and_recent_expiry(self):
        self.record(observation('000000', 30))
        for manager in (self.old, self.new):
            manager.consider_prediction(prediction())
            manager.cleanup(BASE + dt.timedelta(seconds=200))
        self.equal()
        self.assertIn('000000', self.new._buffers)
        for manager in (self.old, self.new):
            manager.invalidate_active_predictions()
            manager.cleanup(BASE + dt.timedelta(seconds=200))
        self.equal()
        self.assertNotIn('000000', self.new._buffers)
        for manager in (self.old, self.new):
            manager.consider_prediction(prediction(seconds=301))
            manager._write_document = lambda *_: None
            manager.finalize_due(BASE + dt.timedelta(seconds=308))
        for seconds in (368, 368.000001, 10):
            self.old.cleanup(BASE + dt.timedelta(seconds=seconds))
            self.new.cleanup(BASE + dt.timedelta(seconds=seconds))
            self.equal()

    def test_concurrent_capture_and_extraction_keep_isolated_records(self):
        manager = self.new
        manager.consider_prediction(prediction())
        event = next(iter(manager._active.values()))
        errors = []
        def read():
            try:
                for _ in range(100):
                    with manager._lock:
                        document = manager._document(event, BASE, True, 'test')
                    for row in document['observations']:
                        row['parameter_sources'].clear()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=read)
        thread.start()
        for index in range(600):
            self.assertTrue(manager.record_observation(observation('000000', index / 10, index)))
            manager.cleanup(BASE + dt.timedelta(seconds=index / 10))
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(all(sample['parameter_sources'] for sample in manager._buffers['000000']))
