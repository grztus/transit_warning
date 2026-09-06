"""Offline historical validation CLI; never starts Transit Warning."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.adsblol_validation import validate, write_report
from tools.adsblol_validation_plots import plot_report


DEFAULT_OUTPUT_ROOT = Path('diagnostics/validation/adsblol/events')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', help='Candidate capture directory or encounter manifest')
    parser.add_argument('--recordings-dir', type=Path, default=Path('recordings'))
    parser.add_argument('--icao', default='48AE25')
    parser.add_argument('--date', default='20260904', help='Discovery date YYYYMMDD')
    parser.add_argument('--encounter-id')
    parser.add_argument('--adsblol-trace', required=True, type=Path)
    parser.add_argument('--output-dir', type=Path,
                        help='New output directory (default: diagnostics/validation/adsblol/events/<date>_<icao>)')
    parser.add_argument('--sbs-timezone', required=True, help='Explicit IANA zone for naive SBS generated timestamps')
    parser.add_argument('--window-seconds', type=float, default=60)
    parser.add_argument('--max-gap-seconds', type=float, default=15)
    parser.add_argument('--prediction', choices=['latest', 'trigger'], default='latest')
    parser.add_argument('--geoid-pgm', type=Path, help='EGM96 PGM for orthometric to HAE conversion')
    return parser


def output_directory(args):
    if args.output_dir is not None:
        return args.output_dir
    return DEFAULT_OUTPUT_ROOT / f'{args.date}_{args.icao.lower()}'


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    output_dir = output_directory(args)
    try:
        capture = args.capture
        if capture is None:
            matches = list(args.recordings_dir.glob(f'**/{args.date}/{args.icao.upper()}/captures/*/capture_manifest.json'))
            if len(matches) != 1:
                raise ValueError('Select --capture; discovered: ' + ', '.join(map(str, matches)))
            capture = matches[0]
        geoid = None
        if args.geoid_pgm:
            from fleet_geometric_altitude import PgmGeoidProvider
            geoid = PgmGeoidProvider(args.geoid_pgm)
        report, local, reference = validate(capture, args.adsblol_trace, args.encounter_id,
            args.sbs_timezone, args.window_seconds, args.max_gap_seconds, args.prediction + '_prediction', geoid)
        write_report(report, output_dir)
        plot_report(report, local, reference, output_dir)
    except (OSError, ValueError, KeyError, ImportError) as exc:
        parser.exit(2, f'Validation failed: {exc}\n')
    print(f"{report['callsign']} / {report['icao']} / {report['encounter_id']}")
    print(f"Predicted T0: {report['predicted_t0_utc']} ({args.prediction})")
    for name, stats in report['metrics'].items():
        print(f'{name}: {stats}')
    for kind in ('observed_at_t0', 'predicted_at_t0'):
        print(kind + ':')
        for name in ('horizontal_m', 'delta_baro_ft', 'delta_geom_ft', 'delta_gs_kt', 'delta_track_deg', 'delta_baro_rate_fpm', 'delta_geom_rate_fpm'):
            value = report[kind][name]
            print(f'  {name}: {value:.3f}' if value is not None else f'  {name}: unavailable')
    print(f"Artifacts: {output_dir.resolve()}")


if __name__ == '__main__':
    main()
