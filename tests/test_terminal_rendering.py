import io
import unittest

import transit_warning as transit


def aircraft(distance=10, sun_time="", moon_time=""):
    entry = [""] * 32
    entry[5] = distance
    entry[22] = sun_time
    entry[26] = moon_time
    return entry


class TerminalRenderingTests(unittest.TestCase):
    def test_clear_screen_homes_cursor_and_clears_to_end(self):
        output = io.StringIO()

        transit.clear_screen(output)

        self.assertEqual(output.getvalue(), "\x1b[H\x1b[J")

    def test_aircraft_rows_leave_fixed_frame_and_scroll_guard_visible(self):
        limit = transit.terminal_aircraft_row_limit(39)

        self.assertEqual(limit, 29)
        self.assertLessEqual(
            limit + transit.TABLE_FIXED_OUTPUT_LINES,
            38,
        )

    def test_small_terminal_never_produces_negative_row_limit(self):
        self.assertEqual(transit.terminal_aircraft_row_limit(5), 0)

    def test_candidates_are_sorted_together_by_nearest_time(self):
        planes = {
            "MOON_LATER": aircraft(moon_time=8),
            "SUN_MIDDLE": aircraft(sun_time=5),
            "MOON_FIRST": aircraft(moon_time=2),
        }

        plan = transit.build_terminal_render_plan(planes, 10, 200)

        self.assertEqual(
            plan.aircraft_ids,
            ("MOON_FIRST", "SUN_MIDDLE", "MOON_LATER"),
        )

    def test_practical_time_tie_prefers_sun(self):
        planes = {
            "MOON": aircraft(moon_time=10.0004),
            "SUN": aircraft(sun_time=10.0),
        }

        plan = transit.build_terminal_render_plan(planes, 10, 200)

        self.assertEqual(plan.aircraft_ids, ("SUN", "MOON"))

    def test_dual_candidate_is_not_duplicated_and_uses_nearest_transit(self):
        planes = {
            "SUN_ONLY": aircraft(sun_time=4),
            "DUAL": aircraft(sun_time=9, moon_time=3),
        }

        plan = transit.build_terminal_render_plan(planes, 10, 200)

        self.assertEqual(plan.aircraft_ids, ("DUAL", "SUN_ONLY"))
        self.assertEqual(plan.aircraft_ids.count("DUAL"), 1)
        self.assertEqual(plan.sun_candidate_count, 2)
        self.assertEqual(plan.moon_candidate_count, 1)

    def test_non_candidates_keep_existing_order_after_candidates(self):
        planes = {
            "FIRST": aircraft(),
            "CANDIDATE": aircraft(sun_time=20),
            "SECOND": aircraft(),
            "THIRD": aircraft(),
        }

        plan = transit.build_terminal_render_plan(planes, 10, 200)

        self.assertEqual(
            plan.aircraft_ids,
            ("CANDIDATE", "FIRST", "SECOND", "THIRD"),
        )

    def test_counts_shown_tracked_and_candidates_when_clipped(self):
        planes = {
            "ORDINARY": aircraft(),
            "SUN": aircraft(sun_time=10),
            "MOON": aircraft(moon_time=12),
            "OUTSIDE": aircraft(distance=250, sun_time=30),
        }

        plan = transit.build_terminal_render_plan(planes, 2, 200)

        self.assertEqual(plan.aircraft_ids, ("SUN", "MOON"))
        self.assertEqual(plan.shown_count, 2)
        self.assertEqual(plan.total_count, 4)
        self.assertEqual(plan.sun_candidate_count, 2)
        self.assertEqual(plan.moon_candidate_count, 1)

    def test_counts_when_not_clipped(self):
        planes = {
            "FIRST": aircraft(),
            "MOON": aircraft(moon_time=7),
        }

        plan = transit.build_terminal_render_plan(planes, 20, 200)

        self.assertEqual(plan.shown_count, 2)
        self.assertEqual(plan.total_count, 2)
        self.assertEqual(plan.sun_candidate_count, 0)
        self.assertEqual(plan.moon_candidate_count, 1)
        self.assertEqual(
            transit.terminal_tracking_summary(51.39309, 21.18876, plan),
            "LAT: 51.39309 LON: 21.18876 | Aircraft: 2/2 shown | "
            "Transit candidates: Sun 0, Moon 1",
        )

    def test_low_terminal_limit_hides_only_lower_priority_aircraft(self):
        planes = {
            "FIRST": aircraft(),
            "MOON": aircraft(moon_time=5),
            "SUN": aircraft(sun_time=2),
        }

        plan = transit.build_terminal_render_plan(planes, 1, 200)

        self.assertEqual(plan.aircraft_ids, ("SUN",))
        self.assertEqual(plan.shown_count, 1)
        self.assertEqual(plan.total_count, 3)

    def test_transit_columns_are_sun_first_and_moon_second(self):
        entry = aircraft()
        entry[18:28] = [37.9, 32.29, 17.9, 33.7, 135,
                        -35.3, -30.0, 44.4, 240, 55.5]

        sun, moon = transit.terminal_transit_values(entry)

        self.assertAlmostEqual(sun[0], 5.61)
        self.assertEqual(sun[1:], (33.7, 17.9, 135))
        self.assertAlmostEqual(moon[0], 5.3)
        self.assertEqual(moon[1:], (55.5, 44.4, 240))

    def test_vertical_separation_is_never_negative(self):
        self.assertAlmostEqual(
            transit.vertical_transit_separation(32.29, 37.9), 5.61)

    def test_cleared_prediction_is_not_a_render_candidate(self):
        entry = aircraft(sun_time=135, moon_time=240)
        transit.clear_transit_prediction(entry, 18)
        transit.clear_transit_prediction(entry, 23)

        plan = transit.build_terminal_render_plan(
            {"CLEARED": entry}, 10, 200)

        self.assertEqual(plan.aircraft_ids, ("CLEARED",))
        self.assertEqual(plan.sun_candidate_count, 0)
        self.assertEqual(plan.moon_candidate_count, 0)


if __name__ == "__main__":
    unittest.main()
