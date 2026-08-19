import io
import unittest

import transit_warning as transit


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


if __name__ == "__main__":
    unittest.main()
