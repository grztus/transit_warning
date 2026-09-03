import importlib.util
import contextlib
import datetime
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


TOOL_PATH = (Path(__file__).resolve().parents[1] / "tools"
             / "shadow_2d_batch_replay.py")
SPEC = importlib.util.spec_from_file_location("shadow_2d_batch_replay", TOOL_PATH)
batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch)


class Shadow2DBatchReplayTests(unittest.TestCase):
    def base_record(self, **changes):
        record = {
            "utc": "2026-09-03T10:00:00Z",
            "icao": "ABC123", "callsign": "TEST1", "body": "SUN",
            "legacy_available": True, "legacy_sep_deg": 0.5,
            "legacy_t0_utc": "2026-09-03T10:01:00Z",
            "coarse_sep_deg": 0.4, "exact_sep_2d_deg": 0.45,
            "exact_t0_2d_utc": "2026-09-03T10:01:01Z",
            "sep_body_radii": 1.8, "slant_range_km": 42.0,
            "delta_sep_deg": -0.05, "delta_t_seconds": 1.0,
            "shadow_only": False, "boundary_status": "INTERIOR",
            "solver_status": "SUCCESS", "altitude_source": "BARO_QNH",
            "track_source": "adsb",
        }
        record.update(changes)
        return record

    def test_interesting_filter_covers_each_requested_reason(self):
        self.assertTrue(batch.is_interesting(self.base_record()))
        self.assertTrue(batch.is_interesting(self.base_record(
            exact_sep_2d_deg=4.0, shadow_only=True)))
        self.assertTrue(batch.is_interesting(self.base_record(
            exact_sep_2d_deg=None, solver_status="FAILED")))
        self.assertTrue(batch.is_interesting(self.base_record(
            exact_sep_2d_deg=4.0, boundary_status="START_BOUNDARY")))
        self.assertTrue(batch.is_interesting(self.base_record(
            exact_sep_2d_deg=4.0, delta_sep_deg=0.10)))
        self.assertFalse(batch.is_interesting(self.base_record(
            exact_sep_2d_deg=4.0, delta_sep_deg=0.099,
            legacy_sep_deg=4.05)))

    def test_csv_row_has_exact_requested_schema(self):
        row = batch.csv_row("session-a", self.base_record())
        self.assertEqual(tuple(row), batch.CSV_FIELDS)
        self.assertEqual(row["session"], "session-a")

    def test_compact_session_progress_prefix(self):
        self.assertEqual(
            batch.session_progress_prefix(1, 5, "20260902_212419"),
            "[1/5] 20260902_212419")

    def test_memory_writer_applies_production_cadence_and_filters(self):
        writer = batch.MemoryDiagnosticWriter()
        interesting = self.base_record()
        self.assertTrue(writer.record(interesting))
        self.assertEqual(len(writer.records), 1)
        duplicate = self.base_record(utc="2026-09-03T10:00:00.500000Z")
        self.assertFalse(writer.record(duplicate))
        ordinary = self.base_record(
            utc="2026-09-03T10:00:31Z", exact_sep_2d_deg=4.0,
            legacy_sep_deg=4.05, delta_sep_deg=-0.05)
        self.assertTrue(writer.record(ordinary))
        self.assertEqual(len(writer.records), 1)
        later_interesting = self.base_record(
            utc="2026-09-03T10:00:32Z", exact_sep_2d_deg=1.9)
        self.assertTrue(writer.record(later_interesting))
        self.assertEqual(len(writer.records), 2)

    def test_resolve_session_accepts_only_explicit_archive_or_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "session-a"
            session.mkdir()
            (session / "manifest.json").write_text(
                json.dumps({"adsb": {}, "mlat": {}}), encoding="utf-8")
            with zipfile.ZipFile(session / "streams.zip", "w") as archive:
                archive.writestr("adsb_30003.log", "")
                archive.writestr("mlat_30106.log", "")
            name, archive_path, _manifest = batch.resolve_session(session)
            self.assertEqual(name, "session-a")
            self.assertEqual(archive_path, session / "streams.zip")
            self.assertEqual(
                batch.resolve_session(session / "streams.zip")[0],
                "session-a")
            with self.assertRaisesRegex(ValueError, "streams.zip"):
                batch.resolve_session(Path(directory))

    def test_import_does_not_start_production_runtime(self):
        command = (
            "import importlib.util; "
            "s=importlib.util.spec_from_file_location('batch_import', r'{}'); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)"
            .format(TOOL_PATH))
        result = subprocess.run(
            [sys.executable, "-c", command], capture_output=True, text=True,
            timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Flight info", result.stdout)

    def test_first_replay_timestamp_uses_silent_table_path(self):
        old_clock = batch.transit.clock
        old_initialized = batch.transit.replay_time_initialized
        old_table = batch.transit.tabela
        old_table_for_observer = batch.transit.tabela_for_observer
        old_current_observer = batch.transit.current_observer_position
        old_quiet = batch._quiet_body_positions
        missing = object()
        old_body_values = tuple(
            getattr(batch.transit, name, missing)
            for name in ("sun_alt", "sun_az", "moon_alt", "moon_az"))
        try:
            batch.transit.clock = batch.ReplayClock()
            batch.transit.replay_time_initialized = False
            batch._quiet_body_positions = lambda observer: (1.0, 2.0, 3.0, 4.0)
            batch.transit.current_observer_position = lambda: object()
            batch._disable_live_output_paths()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                batch.transit.advance_replay_time(datetime.datetime(
                    2026, 9, 3, 10, 0, tzinfo=datetime.timezone.utc))
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(batch.transit.sun_alt, 1.0)
            self.assertEqual(batch.transit.moon_alt, 3.0)
        finally:
            batch.transit.clock = old_clock
            batch.transit.replay_time_initialized = old_initialized
            batch.transit.tabela = old_table
            batch.transit.tabela_for_observer = old_table_for_observer
            batch.transit.current_observer_position = old_current_observer
            batch._quiet_body_positions = old_quiet
            for name, value in zip(
                    ("sun_alt", "sun_az", "moon_alt", "moon_az"),
                    old_body_values):
                if value is missing:
                    delattr(batch.transit, name)
                else:
                    setattr(batch.transit, name, value)


if __name__ == "__main__":
    unittest.main()
