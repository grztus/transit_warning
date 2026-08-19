import copy
import datetime
import io
import math
import tempfile
import unittest
from unittest.mock import Mock, patch

import transit_warning as transit


def aircraft(distance=10, sun_time="", moon_time=""):
    entry = [""] * 32
    entry[5] = distance
    entry[22] = sun_time
    entry[26] = moon_time
    return entry


def render_aircraft(now, distance=10, sun_time="", moon_time=""):
    entry = aircraft(distance, sun_time, moon_time)
    entry[0] = now
    entry[4] = 1000.0
    entry[6] = 180.0
    entry[7] = 10.0
    entry[11] = 180.0
    entry[13] = 0.0
    entry[15] = []
    entry[16] = []
    entry[17] = now
    if sun_time:
        entry[18:22] = [30.0, 31.0, 10.0, 20.0]
    if moon_time:
        entry[23:26] = [20.0, 21.0, 11.0]
        entry[27] = 21.0
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

    def test_counts_shown_and_tracked_when_clipped(self):
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

    def test_counts_when_not_clipped(self):
        planes = {
            "FIRST": aircraft(),
            "MOON": aircraft(moon_time=7),
        }

        plan = transit.build_terminal_render_plan(planes, 20, 200)

        self.assertEqual(plan.shown_count, 2)
        self.assertEqual(plan.total_count, 2)
        self.assertEqual(
            transit.terminal_tracking_summary(51.39309, 21.18876, plan),
            "LAT: 51.39309 LON: 21.18876 | Aircraft: 2/2 shown",
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

    def test_full_snapshot_ignores_terminal_limit_and_preserves_state(self):
        now = datetime.datetime(
            2026, 8, 19, 12, 0, tzinfo=datetime.timezone.utc)
        planes = {
            "P{:03d}".format(index): render_aircraft(now)
            for index in range(55)
        }
        planes["P054"][4:8] = ["", "", "", ""]
        planes["SUNFIRST"] = render_aircraft(now, sun_time=5)
        original = copy.deepcopy(planes)
        clock = Mock()
        clock.now_utc.return_value = now
        clock.ephem_now.return_value = "controlled ephem date"
        sun = Mock(alt=math.radians(30), az=math.radians(120))
        moon = Mock(alt=math.radians(20), az=math.radians(80))

        with patch.object(transit, "plane_dict", planes), \
                patch.object(transit, "clock", clock), \
                patch.object(transit, "gatech", Mock()), \
                patch.object(transit, "my_lat", 51.39309), \
                patch.object(transit, "my_lon", 21.18876), \
                patch.object(transit, "my_elevation_const", 100), \
                patch.object(transit.ephem, "Sun", return_value=sun), \
                patch.object(transit.ephem, "Moon", return_value=moon), \
                patch.object(transit, "terminal_aircraft_row_limit",
                             return_value=29) as row_limit:
            snapshot = transit.render_full_table_snapshot()

        self.assertIn("Aircraft: 56/56 shown", snapshot)
        self.assertIn("P054", snapshot)
        self.assertEqual(snapshot.count("\nP"), 55)
        self.assertLess(snapshot.index("SUNFIRST"), snapshot.index("P000"))
        self.assertEqual(planes, original)
        row_limit.assert_not_called()

    def test_normal_39_line_renderer_still_limits_56_aircraft(self):
        planes = {
            "P{:03d}".format(index): aircraft()
            for index in range(55)
        }
        planes["SUNFIRST"] = aircraft(sun_time=5)

        plan = transit.build_terminal_render_plan(
            planes, transit.terminal_aircraft_row_limit(39), 200)

        self.assertEqual(plan.shown_count, 29)
        self.assertEqual(plan.total_count, 56)
        self.assertEqual(plan.aircraft_ids[0], "SUNFIRST")

    def test_full_plan_includes_aircraft_outside_normal_distance(self):
        planes = {
            "NEAR{:02d}".format(index): aircraft(distance=10)
            for index in range(32)
        }
        planes.update({
            "FAR{:02d}".format(index): aircraft(distance=250)
            for index in range(14)
        })

        normal = transit.build_terminal_render_plan(planes, 46, 200)
        full = transit.build_terminal_render_plan(planes, 46, None)

        self.assertEqual((normal.shown_count, normal.total_count), (32, 46))
        self.assertEqual((full.shown_count, full.total_count), (46, 46))
        self.assertEqual(
            transit.terminal_tracking_summary(51.39309, 21.18876, full),
            "LAT: 51.39309 LON: 21.18876 | Aircraft: 46/46 shown",
        )

    def test_snapshot_signal_handler_only_sets_request_event(self):
        transit.table_snapshot_requested.clear()
        with patch.object(transit, "write_table_snapshot") as writer, \
                patch.object(transit, "tabela") as table:
            transit.request_table_snapshot()

        self.assertTrue(transit.table_snapshot_requested.is_set())
        writer.assert_not_called()
        table.assert_not_called()
        transit.table_snapshot_requested.clear()

    def test_snapshot_writer_creates_diagnostics_file(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(transit, "render_full_table_snapshot",
                             return_value="complete table\n"):
            path = transit.write_table_snapshot(directory)

            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"),
                             "complete table\n")
            self.assertRegex(
                path.name,
                r"^table_snapshot_\d{8}_\d{6}_UTC\.txt$")


if __name__ == "__main__":
    unittest.main()
