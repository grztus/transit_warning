"""Offline SBS replay comparison, separate process per variant; no live inputs.

Uses the established batch replay adapter: no RAW precision-track replay,
dashboard clients, recorder writes, Telegram, or terminal rendering. Results
are a parser/solver/consumer benchmark, not a production capacity guarantee.
"""
import argparse
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def worker(args):
    import shadow_2d_prediction as shadow
    import transit_warning as transit
    from config import load_installation_config
    from tools.shadow_2d_batch_replay import replay_session
    from tools import shadow_2d_batch_replay as batch
    from observer_position import ObserverContext
    original_body_positions = batch._quiet_body_positions
    def body_positions(observer):
        # The older batch adapter predates the frozen ObserverContext callsite.
        return original_body_positions(observer.position if isinstance(observer, ObserverContext) else observer)
    counts = dict(sbs_lines=0, evaluate_shadow_geometry=0,
                  aircraft_computations=0, coarse_screens=0, exact_refinements=0)
    digest = hashlib.sha256()
    configuration = replace(load_installation_config(), shadow_2d_enabled=True,
        authoritative_prediction_geometry='TRUE_2D', observer_mode='STATIC',
        dashboard_enabled=False, dashboard_mobile_gps_enabled=False,
        telegram_notifications_enabled=False)
    if args.geoid_pgm:
        configuration = replace(configuration, fleet_geoid_pgm_path=args.geoid_pgm)
    class Finished(Exception):
        pass
    original_line = transit.process_line
    original_eval = shadow.evaluate_shadow_geometry
    original_aircraft = shadow.evaluate_aircraft_geometry
    original_coarse = transit.shadow_coarse_screen
    original_exact = transit.shadow_exact_refine
    original_consume = transit.consume_authoritative_transition
    original_scope = shadow.message_aircraft_cache
    def line(*values):
        if counts['sbs_lines'] >= args.lines:
            raise Finished()
        counts['sbs_lines'] += 1
        return original_line(*values)
    def evaluate(*values):
        counts['evaluate_shadow_geometry'] += 1
        return original_eval(*values)
    def aircraft(*values):
        counts['aircraft_computations'] += 1
        return original_aircraft(*values)
    def coarse(*values):
        counts['coarse_screens'] += 1
        result = original_coarse(*values)
        digest.update(repr(replace(result, duration_ms=0)).encode())
        return result
    def exact(*values, **kwargs):
        counts['exact_refinements'] += 1
        result = original_exact(*values, evaluator=evaluate, **kwargs)
        digest.update(repr(replace(result, duration_ms=0)).encode())
        return result
    def consume(transition, *values, **kwargs):
        digest.update(repr(transition).encode())
        return original_consume(transition, *values, **kwargs)
    started = time.perf_counter()
    with ExitStack() as stack:
        for module, name, value in [
            (batch, '_quiet_body_positions', body_positions),
            (transit, 'process_line', line),
            (shadow, 'evaluate_shadow_geometry', evaluate),
            (shadow, 'evaluate_aircraft_geometry', aircraft),
            (transit, 'shadow_coarse_screen', coarse),
            (transit, 'shadow_exact_refine', exact),
            (transit, 'consume_authoritative_transition', consume),
        ]:
            stack.enter_context(patch.object(module, name, value))
        if args.variant == 'baseline':
            stack.enter_context(patch.object(shadow, 'message_aircraft_cache', lambda: original_scope(False)))
        stack.enter_context(redirect_stdout(io.StringIO()))
        try:
            replay_session(args.session, configuration, args.environment, args.qnh)
        except Finished:
            pass
    elapsed = time.perf_counter() - started
    counts.update(variant=args.variant, wall_seconds=elapsed,
        seconds_per_1000_sbs_lines=elapsed * 1000 / max(1, counts['sbs_lines']),
        cache_hits=counts['evaluate_shadow_geometry']-counts['aircraft_computations'],
        output_sha256=digest.hexdigest())
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True, help='Explicit FULL session directory or streams.zip')
    parser.add_argument('--lines', type=int, default=1000)
    parser.add_argument('--environment')
    parser.add_argument('--qnh', type=float, default=1013.25)
    parser.add_argument('--geoid-pgm')
    parser.add_argument('--variant', choices=['baseline', 'optimized'], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.lines <= 0:
        parser.error('--lines must be positive')
    if args.variant:
        print(json.dumps(worker(args)))
        return
    results = []
    for variant in ('baseline', 'optimized'):
        output = subprocess.check_output([sys.executable, '-B', __file__, *sys.argv[1:], '--variant', variant], text=True)
        results.append(json.loads(output))
    equal = results[0]['output_sha256'] == results[1]['output_sha256']
    print(json.dumps({'runs': results, 'outputs_equal': equal,
        'speedup': results[0]['wall_seconds']/results[1]['wall_seconds'],
        'scope': 'Deterministic batch SBS replay; dashboard/recording disabled; instrumented timings'}, indent=2))
    if not equal:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
