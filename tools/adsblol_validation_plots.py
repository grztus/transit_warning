"""Reusable aircraft-only plots for offline historical comparisons."""
from pathlib import Path
from tools.adsblol_validation import timestamp, sample


def plot_report(report, local, reference, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    t0 = timestamp(report['predicted_t0_utc'])
    window = report['window_seconds']
    gap = report['max_interpolation_gap_seconds']
    fig, axes = plt.subplots(3, 1, figsize=(10, 12), constrained_layout=True)
    for data, label, style in ((local, 'TW local', '.'), (reference, 'ADSB.lol', '-')):
        times = [t for t, _ in data['lat'].points if abs(t-t0) <= window]
        positions = [sample(data, t, gap) for t in times]
        axes[0].plot([p['lon'] for p in positions], [p['lat'] for p in positions], style, label=label)
        for key, suffix, line in [('baro_ft', 'pressure/barometric', '-'), ('geom_ft', 'geometric HAE', '--')]:
            points = [(t-t0, v) for t, v in data[key].points if abs(t-t0) <= window]
            if points:
                axes[1].plot(*zip(*points), line, label=label + ' ' + suffix)
        points = [(t-t0, v) for t, v in data['track_deg'].points if abs(t-t0) <= window]
        if points:
            axes[2].plot(*zip(*points), style, label=label)
    p = report['predicted_at_t0']
    if p['local_lon'] is not None and p['local_lat'] is not None:
        axes[0].scatter(p['local_lon'], p['local_lat'], marker='x', s=80, label='TW predicted T0')
    if p['local_geom_ft'] is not None:
        axes[1].scatter(0, p['local_geom_ft'], marker='x', s=80, label='TW final converted to HAE')
    axes[0].set(xlabel='Aircraft longitude (degrees)', ylabel='Aircraft latitude (degrees)', title='Aircraft trajectories (no observer position)')
    axes[1].set(xlabel='Seconds relative to predicted T0', ylabel='Altitude (ft)')
    axes[2].set(xlabel='Seconds relative to predicted T0', ylabel='Track (degrees, wrapped 0–360)')
    for axis in axes:
        axis.grid(alpha=.2)
        axis.legend()
    for axis in axes[1:]:
        axis.axvline(0, color='black', alpha=.5, label='Predicted T0')
    fig.suptitle(f"{report['callsign']} — {report['encounter_id']} — {report['predicted_t0_utc']}")
    fig.savefig(Path(output) / 'trajectories.png', dpi=150)
    plt.close(fig)
