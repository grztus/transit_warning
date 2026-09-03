"""Candidate Auto-Recorder Phase 2 encounter gating and state tests."""

import datetime
from pathlib import Path
import tempfile
import unittest

from authoritative_transit import (
    AuthoritativeTransitPrediction,
    AuthoritativeTransition,
    AuthoritativeTransitionKind,
)
from candidate_recorder import (
    CandidateEncounterManager,
    CandidateEncounterOutcome,
    CandidatePreBuffer,
)


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def prediction(*, icao="ABC123", body="SUN", epoch=4, generation=1,
               seconds=120.0, separation=0.5, model="TRUE_2D",
               boundary="INTERIOR"):
    return AuthoritativeTransitPrediction(
        observer_epoch=epoch,
        observer_source="STATIC",
        icao=icao,
        callsign="TEST1",
        body=body,
        encounter_generation=generation,
        encounter_id="{}:{}:{}:{}".format(
            epoch, icao, body, generation),
        predicted_transit_utc=NOW + datetime.timedelta(seconds=seconds),
        separation_deg=separation,
        body_radius_deg=0.25,
        aircraft_azimuth_deg=120.1,
        aircraft_altitude_deg=10.2,
        body_azimuth_deg=120.0,
        body_altitude_deg=10.1,
        aircraft_altitude_m=9000.0,
        aircraft_latitude_deg=11.1,
        aircraft_longitude_deg=22.2,
        frozen_vertical_state=("frozen", seconds),
        slant_range_km=40.0,
        model=model,
        boundary_status=boundary,
        lifecycle_state="ACTIVE",
        updated_at_utc=NOW)


def transition(kind=AuthoritativeTransitionKind.OPENED, **kwargs):
    return AuthoritativeTransition(kind, prediction(**kwargs))


class CandidateEncounterManagerTests(unittest.TestCase):
    def manager(self, **kwargs):
        return CandidateEncounterManager(
            pre_buffer=CandidatePreBuffer(buffer_duration_seconds=60),
            **kwargs)

    def test_valid_true_2d_prediction_triggers_with_forensic_window(self):
        manager = self.manager()
        state = manager.process_transition(transition(), NOW)

        self.assertEqual("4:ABC123:SUN:1", state.encounter_id)
        self.assertEqual(NOW - datetime.timedelta(seconds=60),
                         state.prebuffer_start_utc)
        self.assertEqual(NOW + datetime.timedelta(seconds=300),
                         state.required_end_time_utc)
        self.assertIs(state.trigger_prediction, state.latest_prediction)
        self.assertEqual(state.required_end_time_utc,
                         manager.capture_state("ABC123").capture_until_utc)

    def test_sep_and_horizon_gates_are_configurable_and_future_only(self):
        manager = self.manager(
            trigger_horizon_seconds=200, trigger_separation_deg=1.0)
        for seconds, separation in (
                (201, 0.5), (120, 1.001), (0, 0.5), (-1, 0.5)):
            with self.subTest(seconds=seconds, separation=separation):
                self.assertIsNone(manager.process_transition(
                    transition(seconds=seconds, separation=separation), NOW))
        self.assertIsNotNone(manager.process_transition(
            transition(seconds=200, separation=1.0), NOW))

    def test_legacy_boundary_and_invalid_transitions_cannot_trigger(self):
        manager = self.manager()
        rejected = (
            transition(model="LEGACY"),
            transition(boundary="START_BOUNDARY"),
            transition(boundary="END_BOUNDARY_CONTINUING"),
            transition(AuthoritativeTransitionKind.HELD),
            transition(AuthoritativeTransitionKind.WITHDRAWN),
            AuthoritativeTransition(AuthoritativeTransitionKind.NONE, None),
        )
        for item in rejected:
            self.assertIsNone(manager.process_transition(item, NOW))
        self.assertIsNone(manager.capture_state("ABC123"))

    def test_same_encounter_drift_does_not_duplicate_and_only_extends_end(self):
        manager = self.manager()
        first = manager.process_transition(transition(seconds=120), NOW)
        later_prediction = prediction(seconds=180, separation=0.4)
        later = manager.process_transition(AuthoritativeTransition(
            AuthoritativeTransitionKind.UPDATED, later_prediction),
            NOW + datetime.timedelta(seconds=1))

        self.assertEqual(first.encounter_id, later.encounter_id)
        self.assertEqual(1, len(manager.encounters_for_icao("ABC123")))
        self.assertIs(first.trigger_prediction, later.trigger_prediction)
        self.assertIs(later_prediction, later.latest_prediction)
        extended_end = NOW + datetime.timedelta(seconds=360)
        self.assertEqual(extended_end, later.required_end_time_utc)

        earlier_prediction = prediction(seconds=30, separation=0.2)
        earlier = manager.process_transition(AuthoritativeTransition(
            AuthoritativeTransitionKind.UPDATED, earlier_prediction),
            NOW + datetime.timedelta(seconds=2))
        self.assertEqual(extended_end, earlier.required_end_time_utc)

    def test_withdrawal_records_outcome_without_cancelling_window(self):
        manager = self.manager()
        active = manager.process_transition(transition(), NOW)
        withdrawn = manager.process_transition(AuthoritativeTransition(
            AuthoritativeTransitionKind.WITHDRAWN,
            active.latest_prediction), NOW + datetime.timedelta(seconds=3))

        self.assertEqual(CandidateEncounterOutcome.WITHDRAWN,
                         withdrawn.outcome)
        self.assertEqual(active.required_end_time_utc,
                         withdrawn.required_end_time_utc)
        self.assertEqual(active.required_end_time_utc,
                         manager.capture_state("ABC123").capture_until_utc)
        self.assertTrue(withdrawn.unfinished)

    def test_rapid_new_generation_has_no_icao_cooldown(self):
        manager = self.manager()
        first = manager.process_transition(transition(generation=1), NOW)
        second = manager.process_transition(
            transition(generation=2, seconds=121),
            NOW + datetime.timedelta(milliseconds=1))

        self.assertNotEqual(first.encounter_id, second.encounter_id)
        self.assertEqual(2, len(manager.encounters_for_icao("ABC123")))
        self.assertEqual(
            (first.encounter_id, second.encounter_id),
            manager.capture_state("ABC123").encounter_ids)

    def test_overlapping_sun_and_moon_share_latest_icao_capture_end(self):
        manager = self.manager()
        sun = manager.process_transition(
            transition(body="SUN", seconds=100), NOW)
        moon = manager.process_transition(
            transition(body="MOON", seconds=200), NOW)

        capture = manager.capture_state("ABC123")
        self.assertEqual(2, len(capture.encounter_ids))
        self.assertEqual(moon.required_end_time_utc,
                         capture.capture_until_utc)

        completed = manager.complete_due(sun.required_end_time_utc)
        self.assertEqual((sun.encounter_id,),
                         tuple(item.encounter_id for item in completed))
        remaining = manager.capture_state("ABC123")
        self.assertEqual((moon.encounter_id,), remaining.encounter_ids)
        self.assertEqual(moon.required_end_time_utc,
                         remaining.capture_until_utc)

    def test_observer_epoch_is_part_of_authoritative_encounter_identity(self):
        manager = self.manager()
        first = manager.process_transition(transition(epoch=4), NOW)
        next_epoch = manager.process_transition(
            transition(epoch=5), NOW + datetime.timedelta(seconds=1))

        self.assertNotEqual(first.encounter_id, next_epoch.encounter_id)
        self.assertEqual(2, len(manager.encounters_for_icao("ABC123")))

    def test_completed_encounter_remains_known_and_cannot_retrigger(self):
        manager = self.manager()
        state = manager.process_transition(transition(seconds=10), NOW)
        manager.complete_due(state.required_end_time_utc)
        retrigger = manager.process_transition(
            transition(AuthoritativeTransitionKind.UPDATED, seconds=10),
            state.required_end_time_utc + datetime.timedelta(seconds=1))

        self.assertFalse(retrigger.unfinished)
        self.assertEqual(CandidateEncounterOutcome.COMPLETED,
                         retrigger.outcome)
        self.assertEqual(1, len(manager.encounters_for_icao("ABC123")))
        self.assertIsNone(manager.capture_state("ABC123"))

    def test_processing_is_fail_open_and_performs_zero_disk_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            before = tuple(Path(directory).iterdir())
            manager = self.manager()
            self.assertIsNone(manager.process_transition(object(), NOW))
            self.assertIsNone(manager.process_transition(
                transition(), datetime.datetime(2026, 9, 3, 12, 0)))
            after = tuple(Path(directory).iterdir())

        self.assertEqual(before, after)
        self.assertIsNotNone(manager.last_error)


if __name__ == "__main__":
    unittest.main()
