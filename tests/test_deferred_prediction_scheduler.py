"""Deterministic bounded scheduler tests, independent of runtime geometry."""
import threading
import unittest

from deferred_prediction import LatestPredictionScheduler, PredictionInput


def job(version, icao='ABC123'):
    return PredictionInput(icao, 1, 1, 1, 'LOCAL', version, 0., None, ())


class SchedulerTests(unittest.TestCase):
    def scheduler(self, solve, commit, capacity=256):
        scheduler = LatestPredictionScheduler(solve, commit, capacity)
        self.addCleanup(scheduler.close)
        return scheduler

    def gate(self):
        event = threading.Event()
        self.addCleanup(event.set)  # Release worker before scheduler.close cleanup.
        return event

    def idle(self, scheduler):
        with scheduler._condition:
            self.assertTrue(scheduler._condition.wait_for(
                lambda: scheduler._running is None and not scheduler._pending, 3))

    def test_interim_commit_keeps_latest_pending_and_skips_transient_events(self):
        started = threading.Event()
        executed, committed = [], []
        def solve(value):
            executed.append(value.version)
            if value.version == 1:
                started.set()
                self.assertTrue(release.wait(3))
            return value.version
        scheduler = self.scheduler(solve, lambda value, result: committed.append(result) or True)
        release = self.gate()
        scheduler.submit(job(1))
        self.assertTrue(started.wait(3))
        scheduler.submit(job(2))
        scheduler.submit(job(3))
        self.assertEqual(1, scheduler.snapshot()['pending_aircraft'])
        release.set()
        self.idle(scheduler)
        self.assertEqual([1, 3], executed)
        # Version 2 may represent a transient candidate; it legitimately never
        # reaches the lifecycle commit callback under latest-state semantics.
        self.assertEqual([1, 3], committed)
        self.assertEqual(1, scheduler.snapshot()['replacements'])

    def test_capacity_and_fairness_preserve_other_aircraft(self):
        started = threading.Event()
        executed = []
        def solve(value):
            executed.append((value.icao, value.version))
            if value.icao == 'RUN':
                started.set()
                self.assertTrue(release.wait(3))
        scheduler = self.scheduler(solve, lambda *_: True, capacity=2)
        release = self.gate()
        scheduler.submit(job(1, 'RUN'))
        self.assertTrue(started.wait(3))
        self.assertTrue(scheduler.submit(job(1, 'A')))
        self.assertTrue(scheduler.submit(job(1, 'B')))
        self.assertFalse(scheduler.submit(job(1, 'C')))
        self.assertTrue(scheduler.submit(job(2, 'A')))
        release.set()
        self.idle(scheduler)
        self.assertEqual([('RUN', 1), ('A', 2), ('B', 1)], executed)
        self.assertEqual(1, scheduler.snapshot()['rejected_capacity'])

    def test_duplicate_or_older_pending_running_generations_rejected(self):
        started = threading.Event()
        def solve(value):
            started.set()
            self.assertTrue(release.wait(3))
        scheduler = self.scheduler(solve, lambda *_: True)
        release = self.gate()
        scheduler.submit(job(2))
        self.assertTrue(started.wait(3))
        self.assertFalse(scheduler.submit(job(2)))
        self.assertFalse(scheduler.submit(job(1)))
        self.assertTrue(scheduler.submit(job(4)))
        self.assertFalse(scheduler.submit(job(3)))
        release.set()
        self.idle(scheduler)
        self.assertEqual(2, scheduler.snapshot()['executed'])

    def test_cancel_pending_does_not_coalesce_committed_events(self):
        started = threading.Event()
        committed = []
        def solve(value):
            started.set()
            self.assertTrue(release.wait(3))
        scheduler = self.scheduler(solve, lambda value, _: committed.append(value.version) or True)
        release = self.gate()
        scheduler.submit(job(1))
        self.assertTrue(started.wait(3))
        scheduler.submit(job(2))
        scheduler.cancel('ABC123')
        release.set()
        self.idle(scheduler)
        self.assertEqual([1], committed)
        self.assertEqual(1, scheduler.snapshot()['cancelled'])

    def test_worker_exception_isolated_and_next_aircraft_runs(self):
        committed = threading.Event()
        def solve(value):
            if value.icao == 'BAD':
                raise ValueError('private diagnostic must not be logged')
        scheduler = self.scheduler(solve, lambda *_: committed.set() or True)
        with self.assertLogs('deferred_prediction', level='WARNING') as logs:
            scheduler.submit(job(1, 'BAD'))
            scheduler.submit(job(1, 'GOOD'))
            self.assertTrue(committed.wait(3))
            self.idle(scheduler)
        self.assertNotIn('private diagnostic', str(logs.output))
        self.assertEqual(1, scheduler.snapshot()['failures'])
        self.assertEqual(1, scheduler.snapshot()['committed'])

    def test_rejected_result_retains_pending_latest_and_counts_discard(self):
        started = threading.Event()
        committed = []
        def solve(value):
            if value.version == 1:
                started.set()
                self.assertTrue(release.wait(3))
        def commit(value, _):
            if value.version == 1:
                return False
            committed.append(value.version)
            return True
        scheduler = self.scheduler(solve, commit)
        release = self.gate()
        scheduler.submit(job(1))
        self.assertTrue(started.wait(3))
        scheduler.submit(job(2))
        release.set()
        self.idle(scheduler)
        self.assertEqual([2], committed)
        self.assertEqual(1, scheduler.snapshot()['stale_discarded'])

    def test_shutdown_joins_running_work_cancels_pending_and_rejects_submission(self):
        started = threading.Event()
        executed = []
        def solve(value):
            executed.append(value.version)
            started.set()
            self.assertTrue(release.wait(3))
        scheduler = self.scheduler(solve, lambda *_: True)
        release = self.gate()
        scheduler.submit(job(1))
        self.assertTrue(started.wait(3))
        scheduler.submit(job(2))
        closer = threading.Thread(target=scheduler.close)
        closer.start()
        with scheduler._condition:
            self.assertTrue(scheduler._condition.wait_for(lambda: scheduler._closed, 3))
        self.assertFalse(scheduler.submit(job(3)))
        self.assertTrue(closer.is_alive())
        release.set()
        closer.join(3)
        self.assertFalse(closer.is_alive())
        self.assertFalse(scheduler._thread.is_alive())
        self.assertEqual([1], executed)
        scheduler.close()  # Idempotent.

    def test_exactly_one_worker_runs_all_aircraft_and_default_capacity_is_32(self):
        identifiers = []
        scheduler = LatestPredictionScheduler(
            lambda value: identifiers.append(threading.get_ident()), lambda *_: True)
        self.addCleanup(scheduler.close)
        for number in range(32):
            self.assertTrue(scheduler.submit(job(1, str(number))))
        self.idle(scheduler)
        self.assertEqual({scheduler._thread.ident}, set(identifiers))
        self.assertEqual(32, scheduler.snapshot()['capacity'])
        self.assertTrue(scheduler.close())
        self.assertFalse(scheduler.snapshot()['worker_alive'])

    def test_shutdown_timeout_is_bounded_and_late_commit_is_suppressed(self):
        from unittest.mock import patch
        started, release = threading.Event(), threading.Event()
        committed = []
        def solve(value):
            started.set()
            release.wait(3)
        scheduler = self.scheduler(solve, lambda *_: committed.append(True))
        self.addCleanup(release.set)
        scheduler.submit(job(1))
        self.assertTrue(started.wait(3))
        with patch('deferred_prediction.WORKER_JOIN_SECONDS', .01):
            self.assertFalse(scheduler.close())
        self.assertEqual(1, scheduler.snapshot()['shutdown_timeouts'])
        release.set()
        self.assertTrue(scheduler.close())
        self.assertEqual([], committed)
