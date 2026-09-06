import gzip
import json
from pathlib import Path
import tempfile
import unittest

from tools.adsblol_validation import (Series, circular_difference, load_trace,
    timestamp, validate, write_report, resolve_capture, compare, metrics)
from tools.adsblol_validation_plots import plot_report
from tools.adsblol_historical_validate import build_parser, output_directory


class HistoricalValidationTests(unittest.TestCase):
    def test_cli_default_output_is_diagnostic_and_recordings_input_is_unchanged(self):
        args = build_parser().parse_args([
            '--adsblol-trace', 'trace.json', '--sbs-timezone', 'UTC'])
        self.assertEqual(args.recordings_dir, Path('recordings'))
        self.assertEqual(output_directory(args), Path(
            'diagnostics/validation/adsblol/events/20260904_48ae25'))

    def test_cli_custom_output_overrides_default(self):
        custom = Path('elsewhere/custom-report')
        args = build_parser().parse_args([
            '--adsblol-trace', 'trace.json', '--sbs-timezone', 'UTC',
            '--output-dir', str(custom)])
        self.assertEqual(output_directory(args), custom)

    def assert_full_reference_rejected(self, directory_input):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'manifest.json'
            marker.write_text(json.dumps({
                'schema_version': 1, 'storage_mode': 'FULL_REFERENCE',
                'encounter_id': 'one', 'latest_prediction': {},
                'full_session': {'relative_path': '../missing-session', 'streams': {}}}))
            with self.assertRaises(ValueError) as error:
                resolve_capture(root if directory_input else marker)
            self.assertEqual(str(error.exception),
                'FULL-reference-only captures are not supported by A4.0. '
                'Supply a self-contained candidate capture or encounter manifest. '
                'Shared FULL RAW streams require a separate UTC timing adapter.')

    def test_full_marker_file_rejected_explicitly(self):
        self.assert_full_reference_rejected(False)

    def test_full_marker_directory_rejected_explicitly(self):
        self.assert_full_reference_rejected(True)

    def test_self_contained_capture_file_and_directory_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / 'captures' / 'capture-one'
            encounter = root / 'encounters' / 'one'
            capture.mkdir(parents=True)
            encounter.mkdir(parents=True)
            (capture / 'adsb_sbs.log').write_text('')
            physical = capture / 'capture_manifest.json'
            physical.write_text(json.dumps({'encounter_ids': ['one']}))
            event = {'encounter_id': 'one', 'latest_prediction': {},
                     'physical_capture': {'relative_path': '../../captures/capture-one'}}
            marker = encounter / 'manifest.json'
            marker.write_text(json.dumps(event))
            for source in (capture, physical, marker):
                with self.subTest(source=source):
                    self.assertEqual(resolve_capture(source), (capture.resolve(), event))

    def test_capture_requires_explicit_encounter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture_manifest.json'
            path.write_text(json.dumps({'encounter_ids': ['one', 'two']}))
            with self.assertRaisesRegex(ValueError, 'Choose --encounter-id'):
                resolve_capture(path)

    def test_source_separation_and_metric_counts(self):
        row = compare(0, {'track_deg': 359, 'sbs_track_deg': 358}, {'track_deg': 1})
        self.assertEqual(row['delta_track_deg'], -2)
        self.assertEqual(row['delta_sbs_track_deg'], -3)
        stats = metrics([row])
        self.assertEqual(stats['delta_track_deg']['median_abs'], 2)
        self.assertEqual(stats['delta_baro_ft']['sample_count'], 0)
        self.assertIsNone(stats['delta_baro_ft']['p95_abs'])

    def test_timestamp_utc_and_explicit_local_zone(self):
        self.assertEqual(timestamp('2026-09-04T15:00:00Z'), timestamp('2026/09/04 17:00:00', 'Europe/Warsaw'))
        self.assertEqual(timestamp(100), 100)
        with self.assertRaises(ValueError): timestamp('2026-09-04 17:00:00')
        with self.assertRaises(ValueError): timestamp('2026-10-25 02:30:00', 'Europe/Warsaw')

    def test_interpolation_missing_gap_and_angles(self):
        self.assertEqual(Series([(0, 10), (10, 20)]).at(5, 10), 15)
        self.assertIsNone(Series([(0, 10), (10, 20)]).at(5, 9))
        self.assertIsNone(Series([(0, None), (10, 20)]).at(5, 10))
        self.assertIsNone(Series([(0, 10)]).at(1, 10))
        self.assertEqual(Series([(0, 359), (10, 1)]).at(5, 10, True), 0)
        self.assertEqual(circular_difference(1, 359), 2)
        with self.assertRaises(ValueError): Series([(0, 1), (0, 2)])

    def test_trace_gzip_flags_and_missing_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.json'
            path.write_bytes(gzip.compress(json.dumps({'icao': 'abc123', 'timestamp': 100,
                'trace': [[0, 50, 20, 1000, 100, 359, 12, 64], [10, 50, 20, 'ground', None, 1, 0, None]]}).encode()))
            trace = load_trace(path, 'ABC123')
            self.assertEqual(trace['geom_ft'].at(100, 15), 1000)
            self.assertIsNone(trace['baro_ft'].at(100, 15))
            self.assertEqual(trace['geom_rate_fpm'].at(100, 15), 64)
            self.assertIsNone(trace['track_deg'].at(110, 15))
            with self.assertRaises(ValueError): load_trace(path, 'OTHER')

    def test_t0_schema_outputs_and_missing_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / 'capture'
            capture.mkdir()
            base = timestamp('2026-09-04T15:00:00Z')
            lines = []
            for second, lat in [(0, 50), (10, 50.01)]:
                p = [''] * 22
                p[0], p[1], p[4] = 'MSG', '3', 'ABC123'
                p[6], p[7] = '2026/09/04', f'15:00:{second:02d}.000'
                p[11], p[14], p[15] = '1000', str(lat), '20'
                lines.append(','.join(p))
            (capture / 'adsb_sbs.log').write_text('\n'.join(lines))
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'icao': 'ABC123', 'callsign': 'TEST', 'body': 'SUN', 'encounter_id': 'one',
                'physical_capture': {'relative_path': 'capture'}, 'observer_context': {'private': 'DO_NOT_EXPORT'},
                'latest_prediction': {'prediction_geometry': 'TRUE_2D', 'predicted_transit_utc': base+5,
                    'aircraft': {'latitude_deg': 50.005, 'longitude_deg': 20},
                    'frozen_vertical_state': {'final_altitude_m': 300}}}))
            trace = root / 'trace.json'
            trace.write_text(json.dumps({'icao': 'abc123', 'timestamp': base, 'trace': [
                [0, 50, 20, 1000, 100, 359, 0, 0, None, 'adsb_icao', 1100, 64],
                [10, 50.01, 20, 1000, 100, 1, 0, 0, None, 'adsb_icao', 1100, 64]]}))
            class Geoid:
                def undulation_m(self, lat, lon): return 35.28
            report, local, reference = validate(manifest, trace, timezone='UTC', geoid=Geoid())
            self.assertAlmostEqual(report['predicted_at_t0']['delta_geom_ft'], 0)
            self.assertAlmostEqual(report['predicted_at_t0']['horizontal_m'], 0)
            self.assertEqual(report['observed_at_t0']['delta_baro_ft'], 0)
            self.assertIsNone(report['observed_at_t0']['local_track_deg'])
            self.assertEqual(report['metrics']['horizontal_m']['sample_count'], 2)
            self.assertEqual(report['schema_version'], 1)
            self.assertNotIn('DO_NOT_EXPORT', json.dumps(report))
            without, _, _ = validate(manifest, trace, timezone='UTC')
            self.assertIsNone(without['predicted_at_t0']['delta_geom_ft'])
            output = root / 'output'
            write_report(report, output)
            plot_report(report, local, reference, output)
            self.assertEqual(json.loads((output / 'report.json').read_text())['schema_version'], 1)
            self.assertIn('predicted_at_t0', (output / 'comparison.csv').read_text())
            self.assertTrue((output / 'trajectories.png').stat().st_size > 100)
            with self.assertRaises(FileExistsError): write_report(report, output)


if __name__ == '__main__':
    unittest.main()
