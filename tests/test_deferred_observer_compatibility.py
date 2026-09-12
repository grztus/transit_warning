import datetime
from dataclasses import replace
import math
import unittest

from deferred_prediction import (PredictionInput, mobile_displacement_allowance,
                                 observer_compatible, result_compatible)
from live_dashboard import MobileGpsState
from observer_position import ObserverContext, ObserverPosition, RuntimeObserverPositionProvider


def observer(displacement=0., accuracy=15., mode="MOBILE"):
    return ObserverContext(
        ObserverPosition(math.degrees(displacement / 6371008.8), 0., 100.),
        mode, "MOBILE_FRESH" if mode == "MOBILE" else mode,
        mobile_accuracy_m=accuracy, epoch=1)


class DeferredObserverCompatibilityTests(unittest.TestCase):
    def test_result_generation_guards_are_independent_of_accuracy(self):
        frozen = observer(accuracy=50.)
        job = PredictionInput('ABC123', 1, 2, 3, 'LOCAL', 4, 10., frozen, ())
        valid = dict(incarnation=1, cancellation=2, source_generation=3,
                     source_mode='LOCAL', observer=observer(30., 50.),
                     committed_version=3, now_monotonic=11.)
        self.assertTrue(result_compatible(job, **valid))
        for key, value in (('incarnation', 2), ('cancellation', 3),
                           ('source_generation', 4), ('source_mode', 'AUTO'),
                           ('committed_version', 4), ('now_monotonic', 9.),
                           ('observer', replace(frozen, epoch=2))):
            with self.subTest(key=key):
                self.assertFalse(result_compatible(job, **{**valid, key: value}))

    def test_approved_examples(self):
        for accuracy, cases in (
                (5., ((4., True), (10., True))),
                (15., ((5., True), (15., True), (25., True))),
                (30., ((10., True), (30., True), (45., True))),
                (50., ((10., True), (30., True), (50., True), (70., False)))):
            for distance, expected in cases:
                with self.subTest(accuracy=accuracy, distance=distance):
                    self.assertEqual(expected, observer_compatible(
                        observer(accuracy=accuracy), observer(distance, accuracy)))

    def test_allowance_uses_both_accuracies_and_cap(self):
        self.assertEqual(35., mobile_displacement_allowance(5., 30.))
        self.assertEqual(5., mobile_displacement_allowance(0., 0.))
        self.assertEqual(50., mobile_displacement_allowance(1000., 1000.))
        self.assertFalse(observer_compatible(observer(accuracy=1000.), observer(51., 1000.)))

    def test_invalid_either_accuracy_uses_five_metres(self):
        for invalid in (None, -1., float('nan'), float('inf'), -float('inf')):
            for a, b in ((invalid, 50.), (50., invalid)):
                with self.subTest(a=a, b=b):
                    self.assertEqual(5., mobile_displacement_allowance(a, b))
                    self.assertTrue(observer_compatible(observer(accuracy=a), observer(4., b)))
                    self.assertFalse(observer_compatible(observer(accuracy=a), observer(6., b)))

    def test_static_manual_require_exact_position_ignore_accuracy(self):
        for mode in ('STATIC', 'MANUAL'):
            a = observer(mode=mode)
            self.assertTrue(observer_compatible(a, replace(a, mobile_accuracy_m=float('nan'))))
            self.assertFalse(observer_compatible(a, observer(0.001, 1000., mode)))
            self.assertFalse(observer_compatible(a, replace(a,
                position=replace(a.position, elevation_m=100.001))))

    def test_mobile_epoch_source_and_elevation_changes_rejected(self):
        a = observer()
        for b in (replace(a, epoch=2), replace(a, effective_source='MOBILE_LAST_KNOWN'),
                  replace(a, position=replace(a.position, elevation_m=100.001)),
                  replace(a, position=None)):
            self.assertFalse(observer_compatible(a, b))

    def test_real_provider_ignores_changing_gps_altitude_for_geometry(self):
        now = datetime.datetime(2026, 9, 7, tzinfo=datetime.timezone.utc)
        gps = MobileGpsState(enabled=True)
        provider = RuntimeObserverPositionProvider(ObserverPosition(0., 0., 100.), mode='MOBILE')
        provider.attach_mobile_state(gps)
        contexts = []
        for altitude in (None, 120., 175., -10.):
            gps.update(dict(latitude=0., longitude=0., accuracy=15.,
                            altitude=altitude, timestamp=1.), now)
            contexts.append(provider.resolve(now))
            self.assertEqual(altitude, gps.latest_position().altitude_m)
        self.assertTrue(all(c.position.elevation_m == 100. for c in contexts))
        self.assertTrue(all(observer_compatible(contexts[0], c) for c in contexts))
        self.assertEqual(100., provider.resolve(now + datetime.timedelta(seconds=16)).position.elevation_m)

    def test_stationary_jitter_accepts_interim_results_without_modifying_geometry(self):
        committed = 0
        for version, displacement in enumerate((15., -10., 20., -5.), 1):
            frozen = observer(accuracy=15.)
            current = observer(displacement, 15.)
            job = PredictionInput('ABC123', 1, 1, 1, 'LOCAL', version, 10., frozen, ())
            self.assertTrue(result_compatible(job, incarnation=1, cancellation=1,
                source_generation=1, source_mode='LOCAL', observer=current,
                committed_version=committed, now_monotonic=12.))
            self.assertFalse(result_compatible(job, incarnation=1, cancellation=1,
                source_generation=1, source_mode='LOCAL', observer=current,
                committed_version=committed, now_monotonic=12.001))
            self.assertEqual(0., job.observer.position.latitude_deg)
            self.assertEqual(math.degrees(displacement / 6371008.8), current.position.latitude_deg)
            committed = version
