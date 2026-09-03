"""Batch replay explicitly selected session archives through shadow 2-D.

The tool intentionally uses Transit Warning's existing SBS replay parser and
shadow pipeline.  It does not discover sessions and does not replay the RAW or
MLAT Beast precision-track streams, whose deterministic replay is not currently
part of the production replay contract.
"""

from __future__ import annotations

import argparse
import csv
import datetime
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import time
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_installation_config  # noqa: E402
from environment import (  # noqa: E402
    EnvironmentReplay,
    iter_environment_events,
)
from raw_adsb_diagnostic_replay import (  # noqa: E402
    RawDiagnosticReplay,
    parse_raw_diagnostic_event,
)
from replay_server import merge_logged_streams  # noqa: E402
from shadow_2d_prediction import Shadow2DDiagnosticWriter  # noqa: E402
from transit_clock import ReplayClock  # noqa: E402
import transit_warning as transit  # noqa: E402


CSV_FIELDS = (
    "session", "icao", "callsign", "body",
    "legacy_available", "legacy_sep_deg", "legacy_t0_utc",
    "coarse_sep_deg", "exact_sep_2d_deg", "exact_t0_2d_utc",
    "sep_body_radii", "slant_range_km", "delta_sep_deg",
    "delta_t_seconds", "shadow_only", "boundary_status",
    "solver_status", "altitude_source", "track_source",
)
PROGRESS_RECORD_INTERVAL = 10000


def is_interesting(record):
    """Return whether one existing shadow diagnostic belongs in the CSV."""
    exact_sep = record.get("exact_sep_2d_deg")
    delta_sep = record.get("delta_sep_deg")
    boundary = record.get("boundary_status")
    status = record.get("solver_status")
    return any((
        exact_sep is not None and float(exact_sep) <= 2.0,
        bool(record.get("shadow_only")),
        status == "FAILED",
        boundary not in (None, "INTERIOR"),
        delta_sep is not None and abs(float(delta_sep)) >= 0.10,
    ))


def csv_row(session_name, record):
    row = {field: record.get(field) for field in CSV_FIELDS}
    row["session"] = session_name
    return row


class MemoryDiagnosticWriter:
    """Apply production diagnostic cadence while retaining interesting rows."""

    def __init__(self, minimum_interval=30.0):
        self.minimum_interval = float(minimum_interval)
        self._last = {}
        self.records = []
        self.counters = {
            "screened": 0, "passed": 0, "coarse_rejected": 0,
            "exact_success": 0, "exact_failure": 0, "shadow_only": 0,
        }

    def record(self, record):
        if not is_interesting(record):
            return True
        now = datetime.datetime.fromisoformat(
            record["utc"].replace("Z", "+00:00"))
        key = (record.get("icao"), record.get("body"))
        signature = Shadow2DDiagnosticWriter._signature(record)
        previous = self._last.get(key)
        if previous is not None:
            age = (now - previous[1]).total_seconds()
            if age < 1.0:
                return False
            if previous[0] == signature and age < self.minimum_interval:
                return False
        self._last[key] = (signature, now)
        self.records.append(dict(record))
        return True

    def withdraw(self, icao, callsign, body, now_utc, reason):
        self._last.pop((str(icao), str(body).upper()), None)
        return False


def resolve_session(value):
    """Resolve one explicit directory or streams.zip path; never discover."""
    supplied = Path(value).expanduser()
    archive = supplied / "streams.zip" if supplied.is_dir() else supplied
    if not archive.is_file() or archive.name.lower() != "streams.zip":
        raise ValueError("session must be a directory containing streams.zip or streams.zip itself: {}".format(value))
    manifest_path = archive.parent / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("session manifest is missing: {}".format(manifest_path))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return archive.parent.name, archive, manifest


def _member_name(manifest, section, fallback):
    details = manifest.get(section) or {}
    return details.get("file") or fallback


def _nonblank_text_lines(archive, member):
    with archive.open(member, "r") as raw:
        with io.TextIOWrapper(raw, encoding="utf-8", newline="") as text:
            for line in text:
                if line.strip():
                    yield line


def _raw_diagnostic_replay(archive, manifest):
    member = (manifest.get("raw") or {}).get("events_file")
    if not member or member not in archive.namelist():
        return None

    def events():
        for line in _nonblank_text_lines(archive, member):
            yield parse_raw_diagnostic_event(json.loads(line))

    return RawDiagnosticReplay(events())


def _quiet_body_positions(observer_position):
    now = transit.clock.now_utc()
    sun = transit.body_position_at_utc("sun", now, observer_position)
    moon = transit.body_position_at_utc("moon", now, observer_position)
    transit.sun_body_angular_diameter_arcsec = sun.angular_diameter_arcsec
    transit.moon_body_angular_diameter_arcsec = moon.angular_diameter_arcsec
    transit.sun_body_evaluated_at_utc = sun.evaluated_at_utc
    transit.moon_body_evaluated_at_utc = moon.evaluated_at_utc
    return (round(sun.altitude_deg, 1), round(sun.azimuth_deg, 1),
            round(moon.altitude_deg, 1), round(moon.azimuth_deg, 1))


def _offline_table(*args, **kwargs):
    """Update ephemeris state without rendering or touching the terminal."""
    observer_position = kwargs.get("observer_position")
    if observer_position is None and args:
        observer_position = args[0]
    if observer_position is None:
        observer_position = transit.current_observer_position()
    return _quiet_body_positions(observer_position)


def _disable_live_output_paths():
    # advance_replay_time() calls tabela() directly for the first message;
    # process_line() subsequently calls tabela_for_observer().  Replace both.
    transit.tabela = _offline_table
    transit.tabela_for_observer = _offline_table
    transit.gong = lambda: None


def _reset_runtime(configuration, collector, environment_path, qnh_hpa):
    transit.clock = ReplayClock()
    transit.replay_time_initialized = False
    transit.apply_installation_config(configuration)
    transit.shadow_2d_diagnostics = collector
    transit.pressure = float(qnh_hpa)
    transit.environment_replay = (
        EnvironmentReplay(iter_environment_events(environment_path))
        if environment_path else None)
    transit.raw_diagnostic_replay = None
    transit.transit_snapshot_manager = None
    transit.telegram_notifier = None
    transit.dashboard_runtime = transit.DisabledDashboard()
    transit.session_recorder = None
    transit.session_recording_requested = False
    _disable_live_output_paths()
    for name in (
            "plane_dict", "altitude_sources", "aircraft_motion_states",
            "raw_adsb_tracks", "raw_adsb_versions", "gnss_altitude_states",
            "mlat_beast_tracks", "mlat_coarse_tracks",
            "aircraft_intent_states", "aircraft_motion_freshness_status",
            "sun_prediction_last_valid", "moon_prediction_last_valid",
            "sun_predicted_transit_utc", "moon_predicted_transit_utc",
            "transit_solver_diagnostics", "vertical_transit_diagnostics",
            "geometric_altitude_selections"):
        getattr(transit, name).clear()
    transit.plane_deque.clear()


def replay_session(session_value, configuration, environment_path=None,
                   qnh_hpa=1013.25, progress=None,
                   progress_interval=PROGRESS_RECORD_INTERVAL):
    session_name, archive_path, manifest = resolve_session(session_value)
    collector = MemoryDiagnosticWriter()
    _reset_runtime(configuration, collector, environment_path, qnh_hpa)
    with zipfile.ZipFile(archive_path) as archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            raise ValueError("archive CRC failed for {}".format(bad_member))
        adsb_name = _member_name(manifest, "adsb", "adsb_30003.log")
        mlat_name = _member_name(manifest, "mlat", "mlat_30106.log")
        names = set(archive.namelist())
        missing = [name for name in (adsb_name, mlat_name) if name not in names]
        if missing:
            raise ValueError("archive is missing: {}".format(", ".join(missing)))
        transit.raw_diagnostic_replay = _raw_diagnostic_replay(archive, manifest)
        adsb_lines = _nonblank_text_lines(archive, adsb_name)
        mlat_lines = _nonblank_text_lines(archive, mlat_name)
        processed = 0
        for _timestamp, port, line in merge_logged_streams(
                adsb_lines, mlat_lines,
                configuration.adsb_timestamp_timezone):
            production_port = (configuration.adsb_port
                               if port == 30003 else configuration.mlat_port)
            transit.process_line(line, production_port)
            processed += 1
            if (progress is not None and progress_interval > 0
                    and processed % progress_interval == 0):
                progress(processed)
    return session_name, collector


def session_progress_prefix(index, total, session_name):
    return "[{}/{}] {}".format(index, total, session_name)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Replay explicit streams.zip sessions and retain interesting shadow 2-D diagnostics.")
    parser.add_argument("sessions", nargs="+",
                        help="explicit session directory or streams.zip path")
    parser.add_argument("--output", required=True,
                        help="destination CSV path")
    parser.add_argument("--environment",
                        help="optional environment JSONL used for replay QNH")
    parser.add_argument("--qnh", type=float, default=1013.25,
                        help="constant fallback QNH when no environment event applies (default: 1013.25)")
    return parser


def main(arguments=None):
    args = build_parser().parse_args(arguments)
    configuration = replace(
        load_installation_config(), shadow_2d_enabled=True,
        observer_mode="STATIC", dashboard_enabled=False,
        dashboard_mobile_gps_enabled=False,
        telegram_notifications_enabled=False)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    totals = {
        "sessions": 0, "screened": 0, "exact_success": 0,
        "shadow_only": 0, "failures": 0, "retained": 0,
    }
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        session_total = len(args.sessions)
        for session_index, session in enumerate(args.sessions, 1):
            session_name = resolve_session(session)[0]
            prefix = session_progress_prefix(
                session_index, session_total, session_name)
            print("{} ...".format(prefix), flush=True)
            started = time.perf_counter()
            session_name, collector = replay_session(
                session, configuration, args.environment, args.qnh,
                progress=lambda count, label=prefix: print(
                    "{}: {} records processed".format(label, count),
                    flush=True))
            elapsed = time.perf_counter() - started
            totals["sessions"] += 1
            totals["screened"] += collector.counters["screened"]
            totals["exact_success"] += collector.counters["exact_success"]
            totals["shadow_only"] += collector.counters["shadow_only"]
            totals["failures"] += collector.counters["exact_failure"]
            for record in collector.records:
                writer.writerow(csv_row(session_name, record))
                totals["retained"] += 1
            print("{}: finished in {:.1f}s, {} rows retained".format(
                prefix, elapsed, len(collector.records)), flush=True)
    print("sessions processed: {}".format(totals["sessions"]))
    print("encounters screened: {}".format(totals["screened"]))
    print("exact successes: {}".format(totals["exact_success"]))
    print("shadow_only count: {}".format(totals["shadow_only"]))
    print("failures: {}".format(totals["failures"]))
    print("retained rows: {}".format(totals["retained"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
