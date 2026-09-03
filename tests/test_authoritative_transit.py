import datetime
from types import SimpleNamespace
import unittest

from authoritative_transit import (
    AuthoritativeTransitLifecycle,
    PredictionGeometry,
)
from observer_position import ObserverContext, ObserverPosition


UTC = datetime.timezone.utc
BASE = datetime.datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def context(epoch=4):
    return SimpleNamespace(
        observer_context=ObserverContext(
            ObserverPosition(10.0, 20.0, 100.0), "STATIC", "STATIC",
            epoch=epoch),
        icao="ABC123", callsign="TEST1", body="MOON",
        prediction_base_utc=BASE,
    )


def result(boundary="INTERIOR", seconds=120.0, separation=0.1,
           succeeded=True):
    exact = SimpleNamespace(
        succeeded=succeeded, boundary_status=boundary,
        tca_seconds=seconds, separation_deg=separation,
        body_radius_deg=0.25, aircraft_azimuth_deg=120.1,
        aircraft_altitude_deg=10.2, body_azimuth_deg=120.0,
        body_altitude_deg=10.1, aircraft_altitude_m=9000.0,
        slant_range_km=40.0)
    return SimpleNamespace(exact=exact)


class AuthoritativeTransitLifecycleTests(unittest.TestCase):
    def manager(self):
        return AuthoritativeTransitLifecycle(
            PredictionGeometry.TRUE_2D, grace_seconds=3.0,
            horizon_seconds=900.0)

    def test_offset_true_2d_without_legacy_opens_one_encounter(self):
        manager = self.manager()
        prediction = manager.consider(context(), result(), BASE)
        self.assertEqual("4:ABC123:MOON:1", prediction.encounter_id)
        self.assertEqual("TRUE_2D", prediction.model)
        self.assertAlmostEqual(0.1, prediction.separation_deg)

    def test_start_boundary_cannot_open_an_encounter(self):
        manager = self.manager()
        self.assertIsNone(manager.consider(
            context(), result("START_BOUNDARY", 0.0), BASE))

    def test_start_boundary_after_active_does_not_create_generation(self):
        manager = self.manager()
        first = manager.consider(context(), result(), BASE)
        held = manager.consider(
            context(), result("START_BOUNDARY", 0.0),
            BASE + datetime.timedelta(seconds=1))
        self.assertEqual(first.encounter_id, held.encounter_id)
        self.assertIsNone(manager.consider(
            context(), result("START_BOUNDARY", 0.0),
            BASE + datetime.timedelta(seconds=3)))
        self.assertIsNone(manager.consider(
            context(), result("START_BOUNDARY", 0.0),
            BASE + datetime.timedelta(seconds=4)))

    def test_end_boundary_does_not_publish_event_or_t0(self):
        manager = self.manager()
        self.assertIsNone(manager.consider(
            context(), result("END_BOUNDARY_CONTINUING", 900.0), BASE))

    def test_t0_and_sep_drift_preserve_identity(self):
        manager = self.manager()
        first = manager.consider(context(), result(), BASE)
        changed = manager.consider(
            context(), result(seconds=125.0, separation=0.2),
            BASE + datetime.timedelta(seconds=1))
        self.assertEqual(first.encounter_id, changed.encounter_id)
        self.assertNotEqual(first.predicted_transit_utc,
                            changed.predicted_transit_utc)
        self.assertEqual(0.2, changed.separation_deg)

    def test_exact_failure_uses_three_second_grace(self):
        manager = self.manager()
        first = manager.consider(context(), result(), BASE)
        failed = result(succeeded=False)
        self.assertEqual(first, manager.consider(
            context(), failed, BASE + datetime.timedelta(seconds=2.999)))
        self.assertIsNone(manager.consider(
            context(), failed, BASE + datetime.timedelta(seconds=3.0)))

    def test_exact_failure_without_active_event_publishes_nothing(self):
        self.assertIsNone(self.manager().consider(
            context(), result(succeeded=False), BASE))

    def test_observer_invalidation_discards_active_state(self):
        manager = self.manager()
        manager.consider(context(), result(), BASE)
        manager.invalidate()
        self.assertIsNone(manager.active_prediction(4, "ABC123", "MOON"))

    def test_future_interior_after_closure_creates_new_generation(self):
        manager = self.manager()
        first = manager.consider(context(), result(), BASE)
        manager.consider(context(), result(succeeded=False),
                         BASE + datetime.timedelta(seconds=3))
        second = manager.consider(
            context(), result(seconds=60), BASE + datetime.timedelta(seconds=4))
        self.assertNotEqual(first.encounter_id, second.encounter_id)
        self.assertEqual(2, second.encounter_generation)

    def test_default_legacy_mode_is_a_no_op(self):
        manager = AuthoritativeTransitLifecycle()
        self.assertFalse(manager.enabled)
        self.assertIsNone(manager.consider(context(), result(), BASE))
        self.assertIsNone(manager.active_prediction(4, "ABC123", "MOON"))


if __name__ == "__main__":
    unittest.main()
