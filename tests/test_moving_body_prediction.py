import datetime
import unittest
from unittest.mock import Mock, patch

import pytz

import transit_warning as transit
from config import InstallationConfig
from transit_clock import ReplayClock


UTC_BASE = datetime.datetime(
    2026, 8, 19, 12, 0, 0, tzinfo=pytz.utc)

TEST_CONFIG = InstallationConfig(
    observer_lat=51.0,
    observer_lon=21.0,
    observer_elevation_m=200.0,
    transition_altitude_ft=6500,
    adsb_host="127.0.0.1",
    adsb_port=30003,
    adsb_timestamp_timezone="Europe/Warsaw",
    mlat_host="127.0.0.1",
    mlat_port=30106,
    metar_station="EPRA",
)


def prediction(time2x, separation=1.0, body_alt=20.0):
    return (
        51.2, 21.2, 120.0, body_alt + separation,
        17.9, 33.7, time2x, 0, 120.0, body_alt, UTC_BASE,
    )


def solve(body="sun", fallback=None):
    return transit.moving_body_transit_pred(
        body, (51.0, 21.0), (51.2, 21.2), 180.0, 800.0,
        10000.0, UTC_BASE, fallback_body_position=fallback)


class MovingBodyTransitSolverTests(unittest.TestCase):
    def run_sequence(self, values, body="sun"):
        with patch.object(
                transit, "body_position_at_utc", return_value=(20.0, 120.0)), \
                patch.object(transit, "transit_pred", side_effect=values):
            return solve(body)

    def test_converges_after_one_correction(self):
        solution = self.run_sequence([
            prediction(100.0), prediction(100.4)])
        self.assertEqual(
            solution.diagnostic.outcome,
            transit.TransitSolverOutcome.CONVERGED)
        self.assertEqual(solution.diagnostic.correction_count, 1)
        self.assertAlmostEqual(solution.diagnostic.final_time2x, 100.4)

    def test_converges_after_two_corrections(self):
        solution = self.run_sequence([
            prediction(100.0), prediction(101.0), prediction(101.4)])
        self.assertEqual(solution.diagnostic.correction_count, 2)
        self.assertAlmostEqual(solution.diagnostic.convergence_residual, 0.4)

    def test_converges_after_three_corrections(self):
        solution = self.run_sequence([
            prediction(100.0), prediction(102.0), prediction(101.0),
            prediction(101.4)])
        self.assertEqual(solution.diagnostic.correction_count, 3)

    def test_residual_0_499_converges(self):
        solution = self.run_sequence([
            prediction(100.0), prediction(100.499)])
        self.assertEqual(solution.diagnostic.correction_count, 1)
        self.assertEqual(
            solution.diagnostic.outcome,
            transit.TransitSolverOutcome.CONVERGED)

    def test_residual_0_500_requires_another_correction(self):
        solution = self.run_sequence([
            prediction(100.0), prediction(100.5), prediction(100.9)])
        self.assertEqual(solution.diagnostic.correction_count, 2)
        self.assertAlmostEqual(solution.diagnostic.final_time2x, 100.9)

    def test_two_point_cycle_chooses_larger_separation(self):
        solution = self.run_sequence([
            prediction(10.0, 0.1), prediction(12.0, 0.2),
            prediction(10.05, 0.8)])
        self.assertEqual(
            solution.diagnostic.outcome,
            transit.TransitSolverOutcome.TWO_POINT_CYCLE)
        self.assertAlmostEqual(solution.diagnostic.final_time2x, 10.05)
        self.assertAlmostEqual(solution.diagnostic.final_separation, 0.8)

    def test_max_six_corrections_chooses_larger_of_last_two_separations(self):
        values = [
            prediction(10.0, 0.1), prediction(11.0, 0.1),
            prediction(12.0, 0.1), prediction(13.0, 0.1),
            prediction(14.0, 0.1), prediction(15.0, 2.0),
            prediction(16.0, 1.0),
        ]
        with patch.object(
                transit, "body_position_at_utc", return_value=(20.0, 120.0)), \
                patch.object(
                    transit, "transit_pred", side_effect=values) as geometry:
            solution = solve()
        self.assertEqual(geometry.call_count, 7)
        self.assertEqual(
            solution.diagnostic.outcome,
            transit.TransitSolverOutcome.MAX_ITERATIONS)
        self.assertAlmostEqual(solution.diagnostic.final_time2x, 15.0)
        self.assertAlmostEqual(solution.diagnostic.final_separation, 2.0)

    def test_intermediate_no_intersection_rejects_prediction(self):
        solution = self.run_sequence([prediction(100.0), 0])
        self.assertIsNone(solution.result)
        self.assertEqual(
            solution.diagnostic.outcome,
            transit.TransitSolverOutcome.NO_INTERSECTION)

    def test_nonpositive_time_rejects_prediction(self):
        solution = self.run_sequence([prediction(100.0), prediction(0.0)])
        self.assertIsNone(solution.result)
        self.assertEqual(
            solution.diagnostic.outcome,
            transit.TransitSolverOutcome.OUT_OF_RANGE)

    def test_time_over_900_rejects_prediction(self):
        solution = self.run_sequence([
            prediction(899.0), prediction(900.001)])
        self.assertIsNone(solution.result)
        self.assertEqual(
            solution.diagnostic.outcome,
            transit.TransitSolverOutcome.OUT_OF_RANGE)

    def test_ephemeris_exception_uses_controlled_current_body_fallback(self):
        ephemeris = Mock(side_effect=[(20.0, 120.0), RuntimeError("boom")])
        with patch.object(transit, "body_position_at_utc", ephemeris), \
                patch.object(
                    transit, "transit_pred",
                    return_value=prediction(100.0)) as geometry:
            solution = solve(fallback=(19.0, 119.0))
        self.assertEqual(geometry.call_count, 1)
        self.assertEqual(solution.result, prediction(100.0))
        self.assertEqual(
            solution.diagnostic.outcome,
            transit.TransitSolverOutcome.TECHNICAL_FALLBACK)

    def test_same_solver_supports_sun_and_moon(self):
        helper = Mock(return_value=(20.0, 120.0))
        with patch.object(transit, "body_position_at_utc", helper), \
                patch.object(
                    transit, "transit_pred",
                    side_effect=[prediction(10), prediction(10.1),
                                 prediction(20), prediction(20.1)]):
            sun = solve("sun")
            moon = solve("moon")
        self.assertEqual(sun.diagnostic.body, "sun")
        self.assertEqual(moon.diagnostic.body, "moon")
        self.assertEqual(helper.call_args_list[0].args[0], "sun")
        self.assertEqual(helper.call_args_list[2].args[0], "moon")

    def test_lot3pw_reference_sequence_converges_to_135_9_seconds(self):
        sequence = [
            prediction(119.59184, 2.61813),
            prediction(133.87755, 2.92326),
            prediction(135.51020, 2.96019),
            prediction(135.91837, 2.96616),
        ]
        solution = self.run_sequence(sequence, "moon")
        self.assertEqual(solution.diagnostic.correction_count, 3)
        self.assertAlmostEqual(solution.diagnostic.final_time2x, 135.91837)
        self.assertEqual(
            UTC_BASE + datetime.timedelta(
                seconds=solution.diagnostic.final_time2x),
            datetime.datetime(
                2026, 8, 19, 12, 2, 15, 918370, tzinfo=pytz.utc))

    def test_ephemeris_is_deterministic_for_explicit_utc(self):
        transit.apply_installation_config(TEST_CONFIG)
        first = transit.body_position_at_utc("sun", UTC_BASE)
        second = transit.body_position_at_utc("sun", UTC_BASE)
        moon = transit.body_position_at_utc("moon", UTC_BASE)
        self.assertEqual(first, second)
        self.assertNotEqual(first, moon)
        self.assertGreater(first.angular_diameter_arcsec, 1000)
        self.assertGreater(moon.angular_diameter_arcsec, 1000)

    def test_final_diagnostic_keeps_size_from_selected_ephemeris_state(self):
        positions = [
            transit.BodyPosition(20.0, 120.0, 1900.0),
            transit.BodyPosition(20.1, 120.1, 1899.5),
        ]
        with patch.object(
                transit, "body_position_at_utc", side_effect=positions), \
                patch.object(
                    transit, "transit_pred",
                    side_effect=[prediction(100.0), prediction(100.4)]):
            solution = solve("sun")
        self.assertEqual(
            solution.diagnostic.body_angular_diameter_arcsec, 1899.5)


class MovingBodyTransitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.originals = {
            name: getattr(transit, name) for name in (
                "clock", "plane_dict", "altitude_sources",
                "aircraft_motion_states", "aircraft_motion_freshness_status",
                "pressure", "tabela", "moving_body_transit_pred", "gong")
        }
        transit.clock = ReplayClock()
        transit.apply_installation_config(TEST_CONFIG)
        transit.replay_time_initialized = False
        transit.plane_dict = {}
        transit.altitude_sources = {}
        transit.aircraft_motion_states = {}
        transit.aircraft_motion_freshness_status = {}
        transit.pressure = 1013.25
        transit.sun_alt = 30.0
        transit.sun_az = 120.0
        transit.moon_alt = 20.0
        transit.moon_az = 90.0
        transit.tabela = lambda: (30.0, 120.0, 20.0, 90.0)
        transit.gong = lambda: None
        transit.sun_prediction_last_valid.clear()
        transit.moon_prediction_last_valid.clear()
        transit.sun_predicted_transit_utc.clear()
        transit.moon_predicted_transit_utc.clear()
        transit.transit_solver_diagnostics.clear()

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(transit, name, value)
        transit.sun_prediction_last_valid.clear()
        transit.moon_prediction_last_valid.clear()
        transit.sun_predicted_transit_utc.clear()
        transit.moon_predicted_transit_utc.clear()
        transit.transit_solver_diagnostics.clear()

    @staticmethod
    def mlat3(timestamp, icao="ABC123"):
        date, value_time = timestamp.split()
        return (
            "MLAT,3,1,1,{icao},1,{date},{time},{date},{time},,10000,"
            "450,180,51.2,21.2,0".format(
                icao=icao, date=date, time=value_time))

    @staticmethod
    def solution(body, base, seconds, result=None, outcome=None):
        result = prediction(seconds) if result is None else result
        outcome = outcome or transit.TransitSolverOutcome.CONVERGED
        return transit.MovingBodyTransitSolution(
            result=result,
            diagnostic=transit.MovingBodyTransitDiagnostic(
                body=body,
                prediction_base_utc=base,
                initial_time2x=seconds - 1,
                final_time2x=seconds if result else None,
                correction_count=2,
                convergence_residual=0.2,
                outcome=outcome,
                final_separation=(
                    transit.vertical_transit_separation(result[3], result[9])
                    if result else None),
            ),
        )

    def test_one_prediction_base_is_shared_and_final_target_uses_exact_time(self):
        base = UTC_BASE

        def solver(body, *args, **kwargs):
            return self.solution(body, args[5], 135.91837)

        transit.moving_body_transit_pred = Mock(side_effect=solver)
        transit.process_line(
            self.mlat3("2026/08/19 12:00:00.000"), 30106)

        calls = transit.moving_body_transit_pred.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0].args[6], calls[1].args[6])
        self.assertEqual(calls[0].args[6], base)
        expected = base + datetime.timedelta(seconds=135.91837)
        self.assertEqual(
            transit.moon_predicted_transit_utc["ABC123"], expected)
        self.assertEqual(
            transit.sun_predicted_transit_utc["ABC123"], expected)

    def test_invalid_next_solution_does_not_shift_target_during_grace(self):
        base = UTC_BASE
        valid = [
            self.solution("moon", base, 120.25),
            self.solution("sun", base, 130.25),
        ]
        transit.moving_body_transit_pred = Mock(side_effect=valid)
        transit.process_line(
            self.mlat3("2026/08/19 12:00:00.000"), 30106)
        moon_target = transit.moon_predicted_transit_utc["ABC123"]

        def invalid(body, *args, **kwargs):
            return self.solution(
                body, args[5], 0, result=0,
                outcome=transit.TransitSolverOutcome.NO_INTERSECTION)

        transit.moving_body_transit_pred = Mock(side_effect=invalid)
        transit.process_line(
            self.mlat3("2026/08/19 12:00:01.000"), 30106)
        self.assertEqual(
            transit.moon_predicted_transit_utc["ABC123"], moon_target)

    def test_cleanup_removes_solver_diagnostics(self):
        transit.moving_body_transit_pred = Mock(side_effect=lambda body, *args,
            **kwargs: self.solution(body, args[5], 120.0))
        transit.process_line(
            self.mlat3("2026/08/19 12:00:00.000"), 30106)
        self.assertIn(("ABC123", "sun"), transit.transit_solver_diagnostics)
        self.assertIn(("ABC123", "moon"), transit.transit_solver_diagnostics)
        transit.clock.advance_to(
            UTC_BASE + datetime.timedelta(seconds=61))
        transit.clean_dict()
        self.assertNotIn(("ABC123", "sun"), transit.transit_solver_diagnostics)
        self.assertNotIn(("ABC123", "moon"), transit.transit_solver_diagnostics)


if __name__ == "__main__":
    unittest.main()
