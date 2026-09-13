"""Correlate input, numerical completion, acceptance and public finalization."""
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import Mock, patch

import transit_warning as r
import test_authoritative_continuity as continuity


class SuccessCommitTests(unittest.TestCase):
    setUp = continuity.ContinuityRuntimeTests.setUp
    advance = continuity.ContinuityRuntimeTests.advance
    submit = continuity.ContinuityRuntimeTests.submit
    idle = continuity.ContinuityRuntimeTests.idle
    establish = continuity.ContinuityRuntimeTests.establish
    candidate = continuity.ContinuityRuntimeTests.candidate

    def capture_job(self, seconds):
        self.advance(seconds)
        jobs = []
        with patch.object(self.service.scheduler, 'submit', side_effect=lambda job: jobs.append(job) or True):
            self.submit()
        return jobs[0]

    def diagnostics(self, trace):
        self.coarse.return_value = SimpleNamespace(passed=True, estimated_tca_seconds=60,
            estimated_separation_deg=1, evaluation_count=1, duration_ms=0, reason='PASS')
        for name, value in dict(solver_status='SUCCESS', separation_body_radii=4,
                                evaluation_count=1, duration_ms=0, reason=None).items():
            setattr(self.exact.return_value, name, value)
        writer = SimpleNamespace(counters=dict(screened=0, passed=0, coarse_rejected=0,
            exact_success=0, exact_failure=0, shadow_only=0),
            record=lambda record: trace.append(('diagnostic', dict(record), self.now)))
        self.enterContext(patch.object(r, 'shadow_2d_diagnostics', writer))

    def test_success_timestamp_refers_to_new_generation_after_expiry(self):
        first = self.establish()
        trace = []
        self.dashboard.state.finalization_journal = SimpleNamespace(
            append=lambda record: trace.append(('finalization', record, self.now)))
        self.diagnostics(trace)
        # These offsets reproduce the supplied 08:19:47.398429 input,
        # .401516 finalization, .452260 new-generation success timeline.
        input_offset, expiry_offset, commit_offset = 18.529876, 18.532963, 18.583707
        self.advance(input_offset)
        prediction_base = self.now
        self.advance(expiry_offset)
        jobs = []
        with r.plane_dict_lock, patch.object(self.service.scheduler, 'submit',
                side_effect=lambda job: jobs.append(job) or True):
            self.assertTrue(self.service.submit('ABC123', 'TEST', self.observer,
                r.aircraft_motion_states['ABC123'].track, 800, 1000, prediction_base))
        job = jobs[0]
        outputs = self.service.solve(job)
        completed_at = self.now
        self.advance(commit_offset)
        self.assertTrue(self.service.commit(job, outputs))
        self.assertEqual(['finalization', 'finalization', 'diagnostic', 'diagnostic'],
                         [entry[0] for entry in trace])
        record = trace[2][1]
        self.assertEqual('SUCCESS', record['solver_status'])
        self.assertEqual('INTERIOR', record['boundary_status'])
        self.assertNotEqual(first.encounter_id, self.candidate().encounter_id)
        self.assertEqual(self.now, self.candidate().last_prediction_update_utc)
        self.assertEqual(self.now, dt.datetime.fromisoformat(record['utc'].replace('Z', '+00:00')))
        self.assertEqual(self.candidate().encounter_id, record['encounter_id'])
        self.assertEqual('ACCEPTED', record['commit_status'])
        self.assertEqual(job.version, record['job_version'])
        self.assertLess(completed_at, self.now)
        self.assertEqual(job.observer.epoch, record['observer_epoch'])
        self.assertEqual(prediction_base, dt.datetime.fromisoformat(
            record['prediction_base_utc'].replace('Z', '+00:00')))

    def test_rejected_numerical_success_is_diagnosed_without_public_mutation(self):
        self.establish()
        trace = []
        self.diagnostics(trace)
        old = self.capture_job(1)
        outputs = self.service.solve(old)
        newer = self.capture_job(2)
        self.assertTrue(self.service.commit(newer, self.service.solve(newer)))
        candidate = self.candidate()
        trace.clear()
        self.assertFalse(self.service.commit(old, outputs))
        self.assertEqual(candidate, self.candidate())
        self.assertEqual(2, len(trace))
        for _, record, _ in trace:
            self.assertEqual('SUCCESS', record['solver_status'])
            self.assertEqual('REJECTED', record['commit_status'])
            self.assertEqual('SUPERSEDED', record['commit_reason'])
            self.assertEqual(old.version, record['job_version'])
            self.assertFalse(any(key in record for key in
                ('observer_latitude', 'observer_longitude', 'coordinates', 'payload')))

    def test_commit_diagnostics_keep_existing_rate_limit_and_privacy_guard(self):
        from shadow_2d_prediction import Shadow2DDiagnosticWriter
        with tempfile.TemporaryDirectory() as root:
            writer = Shadow2DDiagnosticWriter(root)
            record = dict(utc=self.now.isoformat(), icao='ABC123', body='MOON',
                solver_status='SUCCESS', commit_status='ACCEPTED', commit_reason='UPDATED')
            self.assertTrue(writer.record(record))
            record.update(commit_status='REJECTED', commit_reason='SUPERSEDED')
            record['utc'] = (self.now + dt.timedelta(seconds=.5)).isoformat()
            self.assertFalse(writer.record(record))
            record['utc'] = (self.now + dt.timedelta(seconds=1)).isoformat()
            self.assertTrue(writer.record(record))
            record['utc'] = (self.now + dt.timedelta(seconds=2)).isoformat()
            self.assertFalse(writer.record(record))
            with self.assertRaises(ValueError):
                writer.record(dict(record, observer_latitude=0))

    def test_fresh_without_unavailable_transition_expires_on_maintenance(self):
        first = self.establish()
        self.advance(9.999)
        r.clean_dict()
        self.assertEqual(first.encounter_id, self.candidate().encounter_id)
        self.advance(10)
        r.clean_dict()
        self.assertFalse('ABC123' in self.dashboard.state._live['MOON'])
        self.assertEqual(['PREDICTION_UNAVAILABLE'] * 2, [event['reason'] for event in self.events])
        r.clean_dict()
        self.assertEqual(2, len(self.events))

    def test_old_unavailable_after_newer_success_is_ignored(self):
        self.establish()
        old = self.capture_job(1)
        unavailable = tuple((ctx, None, legacy) for ctx, _, legacy in self.service.solve(old))
        newer = self.capture_job(2)
        self.assertTrue(self.service.commit(newer, self.service.solve(newer)))
        candidate = self.candidate()
        ownership = self.dashboard.state.prediction_encounters('ABC123')
        calls = self.telegram.call_count
        self.assertFalse(self.service.commit(old, unavailable))
        self.assertEqual(candidate, self.candidate())
        self.assertEqual(ownership, self.dashboard.state.prediction_encounters('ABC123'))
        self.assertEqual(calls, self.telegram.call_count)
        self.assertEqual([], self.events)

    def test_newer_unavailable_rejects_old_success(self):
        first = self.establish()
        old = self.capture_job(1)
        success = self.service.solve(old)
        newer = self.capture_job(2)
        unavailable = tuple((ctx, None, legacy) for ctx, _, legacy in self.service.solve(newer))
        self.assertTrue(self.service.commit(newer, unavailable))
        self.assertFalse(self.service.commit(old, success))
        self.assertEqual(first.encounter_id, self.candidate().encounter_id)
        self.assertEqual('SOLVE_UNAVAILABLE', self.candidate().prediction_quality_reason)
        self.assertEqual(first.last_prediction_update_utc, self.candidate().last_prediction_update_utc)

    def test_duplicate_delivery_of_same_job_cannot_change_accepted_success(self):
        job = self.capture_job(0)
        outputs = self.service.solve(job)
        self.assertTrue(self.service.commit(job, outputs))
        first = self.candidate()
        unavailable = tuple((ctx, None, legacy) for ctx, _, legacy in outputs)
        self.assertFalse(self.service.commit(job, unavailable))
        self.assertEqual(first, self.candidate())

    def test_rejected_superseded_job_has_no_expiry_side_effects(self):
        old = self.capture_job(0)
        outputs = self.service.solve(old)
        newer = self.capture_job(1)
        self.assertTrue(self.service.commit(newer, self.service.solve(newer)))
        candidate = self.candidate()
        self.advance(12)
        self.assertFalse(self.service.commit(old, outputs))
        # Independent maintenance must retire the expired prediction, not an old job.
        self.assertEqual(candidate, self.candidate())
        self.assertEqual([], self.events)
        r.clean_dict()
        self.assertFalse('ABC123' in self.dashboard.state._live['MOON'])

    def test_obsolete_body_result_is_ignored_without_degrading_existing_future_event(self):
        first = self.establish()
        job = self.capture_job(1)
        outputs = list(self.service.solve(job))
        ctx, solved, legacy = outputs[0]
        outputs[0] = (replace(ctx, prediction_base_utc=self.now-dt.timedelta(seconds=61)), solved, legacy)
        self.assertTrue(self.service.commit(job, tuple(outputs)))
        self.assertEqual(first, self.candidate())
        self.assertEqual(self.now, self.candidate('SUN').last_prediction_update_utc)
        self.assertEqual([], self.events)

    def test_fresh_success_then_current_unavailable_holds_without_finalization(self):
        self.establish()
        success = self.capture_job(4)
        self.assertTrue(self.service.commit(success, self.service.solve(success)))
        first = self.candidate()
        job = self.capture_job(4.003)
        outputs = tuple((ctx, None, legacy) for ctx, _, legacy in self.service.solve(job))
        self.assertTrue(self.service.commit(job, outputs))
        self.assertEqual(first.encounter_id, self.candidate().encounter_id)
        self.assertEqual(first.last_prediction_update_utc, self.candidate().last_prediction_update_utc)
        self.assertEqual('SOLVE_UNAVAILABLE', self.candidate().prediction_quality_reason)
        self.assertEqual([], self.events)


if __name__ == '__main__':
    unittest.main()
