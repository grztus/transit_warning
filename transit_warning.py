#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
=======================================================================
Original idea: https://github.com/darethehair/flight-warning
=======================================================================
flight_warning.py
version 1.06
Copyright (C) 2015 Darren Enns <darethehair@gmail.com>

=======================================================================
Changes:
=======================================================================
transit_warning_v0.4 Grzegorz Tuszyński <grztus@wp.pl> (May 2024)
- added MLAT messages handling
- added automatic port connection to ports 30003 (ADS-B) and 30106 (MLAT) (nc or ncat is no longer necessary)
- added a 120-second display of transit information to allow time for taking a photo, and then returning to verify the separation from the Sun/Moon.
- code optimization and improved robustness

TO DO LIST:
1. add vertical speed and pilot selected altitude to transit calculations
2. add some logging functions


v0.3 Grzegorz Tuszyński <grztus@wp.pl> (May 2024)
- auto checking the pressure from metar (for Poland region. You need to find some metar url site for Your country and change proper lines below in the script)
- minor bug fixes (including some calculations)
- added support of python 2 and python 3 in one script
- fitted to work both on Windows (10 Pro) and linux debian (10) systems
- more comments as user instructions added for better understanding the script

v0.2 
- try/except for plane lat/lon in MSG 3
v0.1
- Color console realtime display Az/Alt
- Sun/Moon transits prediction

<aleksander5416@gmail.com>

=======================================================================
=======================================================================

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or (at
your option) any later version.

This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307
USA.
"""

# Importowanie niezbędnych bibliotek / Importing necessary libraries
from __future__ import print_function
import argparse
import io
import os
from pathlib import Path
import shutil
import signal
import sys
import datetime
import time
import math
import ephem
import re
import socket
import threading
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from math import atan2, sin, cos, acos, radians, degrees, atan, asin, sqrt, isnan
import pytz  # Import pytz for timezone handling
from config import ConfigurationError, InstallationConfig, load_installation_config
from beast_intent import BeastFrameParser, decode_tc29, modes_crc
from environment import (
    DailyEnvironmentRecorder,
    EnvironmentEvent,
    EnvironmentFormatError,
    EnvironmentRecorder,
    EnvironmentRecordError,
    EnvironmentReplay,
    iter_environment_events,
)
from metar import fetch_awc_metar
from mlat_beast_track import decode_mlat_beast_tc19, truncation_bin_consistent
from recording import RecordingStatus, SessionRecorder, archive_session
from raw_adsb_track import decode_raw_tc19_track
from transit_clock import ReplayClock, clock_from_args
from transit_time import AdsBTimestampOffsetValidator, port_timestamp_to_utc
from transit_snapshot import TransitSnapshotManager, runtime_git_commit
from transit_prediction_model import (
    AngularPosition,
    INTENT_FRESHNESS_SECONDS,
    IntentParameter,
    GreatCircleIntersection,
    MotionParameter,
    QNH_CORRECTION_FT_PER_HPA,
    VERTICAL_ALTITUDE_MAX_AGE_SECONDS,
    VERTICAL_PREDICTION_MAX_SECONDS,
    VERTICAL_RATE_IGNORE_AGE_SECONDS,
    VERTICAL_RATE_LEVEL_THRESHOLD_FPM,
    VERTICAL_RATE_MAX_SPREAD_FPM,
    VERTICAL_RATE_STABILITY_SAMPLES,
    VERTICAL_RATE_VALID_AGE_SECONDS,
    VerticalIntentState,
    VerticalMotionState,
    VerticalPredictionMode,
    VerticalPredictionPolicy,
    VerticalPredictionResult,
    VerticalStateAtTime,
    angular_position_from_observer,
    clamp_vertical_prediction_to_intent_state as shared_clamp_vertical,
    current_vertical_prediction_policy,
    great_circle_forward_bearing_at_point,
    predict_transit_altitude as shared_predict_transit_altitude,
    predict_vertical_state_at_time as shared_predict_vertical_state,
    solve_great_circle_intersection,
)

# Ustawienia GUI / GUI settings
try:
    import tkinter as tk
    from tkinter import simpledialog
except ImportError:
    import Tkinter as tk
    import tkSimpleDialog as simpledialog

# Kompatybilność z Python 2 i 3 / Compatibility with Python 2 and 3
try:
    input = raw_input
except NameError:
    pass

from collections import deque


DIAGNOSTICS_DIRECTORY = Path("diagnostics")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
table_snapshot_requested = threading.Event()


def parse_runtime_args(arguments):
    parser = argparse.ArgumentParser()
    parser.add_argument("--clock", choices=("real", "replay"), default="real")
    parser.add_argument("--environment-replay")
    parser.add_argument("--environment-record")
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args(arguments)
    if args.environment_replay is not None and args.environment_record is not None:
        parser.error("--environment-replay and --environment-record cannot be used together")
    if args.environment_replay is not None and args.clock != "replay":
        parser.error("--environment-replay requires --clock replay")
    if args.environment_record is not None and args.clock != "real":
        parser.error("--environment-record requires --clock real")
    if args.record and args.clock != "real":
        parser.error("--record requires --clock real")
    return args


runtime_args = parse_runtime_args(sys.argv[1:] if __name__ == "__main__" else [])
clock = clock_from_args(["--clock", runtime_args.clock])
replay_time_lock = threading.Lock()
replay_time_initialized = not isinstance(clock, ReplayClock)
environment_replay = None
environment_recorder = None
daily_environment_recorder = None
adsb_timestamp_validator = None
session_recorder = None
session_recording_requested = False
transit_snapshot_manager = None
transit_warning_git_commit = runtime_git_commit(Path(__file__).resolve().parent)
stop_event = threading.Event()
active_sockets = {}
active_sockets_lock = threading.Lock()
shutdown_lock = threading.Lock()
shutdown_complete = False

# Global settings / Globalne ustawienia
MAX_AGE_SECONDS = 60  # Maksymalny czas życia wpisu po ostatnim odbiorze sygnału (w sekundach) / Maximum entry lifetime after the last received signal (in seconds)
TRANSIT_PREDICTION_GRACE_SECONDS = 3.0
MOVING_BODY_CONVERGENCE_SECONDS = 0.5
MOVING_BODY_MAX_CORRECTIONS = 6
MOVING_BODY_CYCLE_TOLERANCE_SECONDS = 0.1
MOTION_FRESH_POSITION_SECONDS = 3.0
MOTION_FRESH_PARAMETER_SECONDS = 5.0
MOTION_FRESH_DELTA_SECONDS = 3.0
MOTION_STALE_SECONDS = 10.0
MOTION_STALE_DELTA_SECONDS = 10.0
TRANSIT_SNAPSHOT_SEP_THRESHOLD_DEG = 0.5
TRANSIT_SNAPSHOT_ARM_SECONDS = 15.0
TRANSIT_SNAPSHOT_FINALIZE_GRACE_SECONDS = 2.0

# Deklaracja globalnych zmiennych / Declaration of global variables
global metar_t
global metar_attempt_t
global pressure
metar_t = None
metar_attempt_t = None
pressure = 1013  # Domyślne ciśnienie / Default pressure
metar_station = None

# Kolory terminala / Terminal Colors
REDALERT = '\x1b[1;37;41m'
PURPLE = '\x1b[1;35;40m'
PURPLEDARK = '\x1b[0;35;40m'
RED = '\x1b[0;31;40m'
GREEN = '\x1b[0;30;42m'
GREENALERT = '\x1b[0;30;42m'
GREENFG = '\x1b[1;32;40m'
BLUE = '\x1b[1;34;40m'
YELLOW = '\x1b[1;33;40m'
CYAN = '\x1b[1;36;40m'
RESET = '\x1b[0m'

# Global Settings
earth_R = 6371  # Promień Ziemi w km / Radius of the earth in km

# Inicjalizacja pustych słowników i kolejek / Initialize empty dictionaries and deques
plane_dict = {}
altitude_sources = {}
aircraft_motion_states = {}
raw_adsb_tracks = {}
mlat_beast_tracks = {}
mlat_coarse_tracks = {}
aircraft_intent_states = {}
aircraft_motion_freshness_status = {}
sun_prediction_last_valid = {}
moon_prediction_last_valid = {}
sun_predicted_transit_utc = {}
moon_predicted_transit_utc = {}
transit_solver_diagnostics = {}
vertical_transit_diagnostics = {}
plane_dict_lock = threading.RLock()
plane_deque = deque()

VERTICAL_RATE_HISTORY_MAXLEN = 10
INTENT_HISTORY_MAXLEN = 10


@dataclass(frozen=True)
class AltitudeMeasurement:
    source: str
    altitude_kind: str
    altitude_baro_ft: int
    altitude_corrected_m: float
    timestamp_utc: datetime.datetime
    message_type: str


@dataclass(frozen=True)
class AltitudeDiagnostics:
    current_geometry_altitude_m: float | str | None
    latest_adsb: AltitudeMeasurement | None
    latest_mlat: AltitudeMeasurement | None
    delta_adsb_mlat_ft: int | None
    adsb_age_seconds: float | None
    mlat_age_seconds: float | None


@dataclass(frozen=True)
class PositionParameter:
    latitude: float
    longitude: float
    updated_at_utc: datetime.datetime
    source: str


@dataclass
class AircraftMotionState:
    position: PositionParameter | None = None
    altitude: MotionParameter | None = None
    track: MotionParameter | None = None
    groundspeed: MotionParameter | None = None
    vertical_rate: MotionParameter | None = None
    vertical_rate_history: deque = field(default_factory=lambda: deque(
        maxlen=VERTICAL_RATE_HISTORY_MAXLEN))


@dataclass
class RawAdsbTrackState:
    precise_value_deg: float
    raw_updated_at_utc: datetime.datetime
    coarse_anchor_deg: float | None
    hold_valid: bool = True


@dataclass
class MlatBeastTrackState:
    precise_value_deg: float
    received_at_utc: datetime.datetime
    east_west_velocity_knots: float
    north_south_velocity_knots: float
    derived_groundspeed_knots: float
    angular_interval_low_deg: float
    angular_interval_high_deg: float
    coarse_anchor_deg: float | None = None
    coarse_anchor_timestamp_utc: datetime.datetime | None = None
    confirmed: bool = False
    hold_valid: bool = True


@dataclass(frozen=True)
class SelectedAltitudeHistoryEntry:
    selected_altitude_ft: float
    nav_qnh_hpa: float | None
    updated_at_utc: datetime.datetime
    source: str


@dataclass
class AircraftIntentState:
    selected_altitude: IntentParameter | None = None
    nav_qnh: IntentParameter | None = None
    selected_altitude_history: deque = field(default_factory=lambda: deque(
        maxlen=INTENT_HISTORY_MAXLEN))


@dataclass(frozen=True)
class AircraftMotionFreshness:
    position_age: float | None
    altitude_age: float | None
    track_age: float | None
    groundspeed_age: float | None
    vertical_rate_age: float | None


class MotionFreshnessStatus(str, Enum):
    FRESH = "FRESH"
    DEGRADED = "DEGRADED"
    STALE = "STALE"


@dataclass(frozen=True)
class VerticalTransitDiagnostic:
    body: str
    prediction: VerticalPredictionResult
    separation_before: float
    separation_after: float
    selected_altitude_ft: float | None = None
    selected_altitude_source: str | None = None
    selected_altitude_age_seconds: float | None = None
    nav_qnh_hpa: float | None = None
    nav_qnh_age_seconds: float | None = None
    target_altitude_m: float | None = None
    target_direction_valid: bool | None = None
    intent_clamped: bool = False
    intent_reason: str = "TC29_NOT_EVALUATED"
    predicted_altitude_before_clamp_m: float | None = None
    predicted_altitude_after_clamp_m: float | None = None
    separation_before_clamp: float | None = None


@dataclass
class BeastIntentDiagnostics:
    frames_received: int = 0
    invalid_crc_frames: int = 0
    tc29_updates: int = 0
    reconnects: int = 0
    resync_count: int = 0
    last_error: str | None = None
    last_error_utc: datetime.datetime | None = None


beast_intent_diagnostics = BeastIntentDiagnostics()


@dataclass
class RawAdsbTrackDiagnostics:
    frames_received: int = 0
    valid_track_updates: int = 0
    rejected_frames: int = 0
    reconnects: int = 0
    last_error: str | None = None
    last_error_utc: datetime.datetime | None = None


raw_adsb_track_diagnostics = RawAdsbTrackDiagnostics()


@dataclass
class MlatBeastTrackDiagnostics:
    frames_received: int = 0
    valid_track_updates: int = 0
    rejected_frames: int = 0
    reconnects: int = 0
    resync_count: int = 0
    last_error: str | None = None
    last_error_utc: datetime.datetime | None = None


mlat_beast_track_diagnostics = MlatBeastTrackDiagnostics()


class TransitSolverOutcome(str, Enum):
    CONVERGED = "CONVERGED"
    TWO_POINT_CYCLE = "TWO_POINT_CYCLE"
    MAX_ITERATIONS = "MAX_ITERATIONS"
    NO_INTERSECTION = "NO_INTERSECTION"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    TECHNICAL_FALLBACK = "TECHNICAL_FALLBACK"


@dataclass(frozen=True)
class MovingBodyTransitDiagnostic:
    body: str
    prediction_base_utc: datetime.datetime
    initial_time2x: float | None
    final_time2x: float | None
    correction_count: int
    convergence_residual: float | None
    outcome: TransitSolverOutcome
    final_separation: float | None
    body_angular_diameter_arcsec: float | None = None
    body_ephemeris_evaluated_at_utc: datetime.datetime | None = None


@dataclass(frozen=True)
class BodyPosition:
    altitude_deg: float
    azimuth_deg: float
    angular_diameter_arcsec: float | None
    evaluated_at_utc: datetime.datetime | None = None

    def __iter__(self):
        yield self.altitude_deg
        yield self.azimuth_deg

    def __getitem__(self, index):
        return (self.altitude_deg, self.azimuth_deg)[index]


@dataclass(frozen=True)
class ProductionAircraftState:
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    azimuth_from_observer_deg: float
    altitude_angle_deg: float


@dataclass(frozen=True)
class MovingBodyTransitSolution:
    result: tuple | None
    diagnostic: MovingBodyTransitDiagnostic


@dataclass(frozen=True)
class MotionFreshnessResult:
    status: MotionFreshnessStatus
    assessed_at_utc: datetime.datetime
    position_age: float | None
    altitude_age: float | None
    track_age: float | None
    groundspeed_age: float | None
    vertical_rate_age: float | None
    position_track_delta: float | None
    position_groundspeed_delta: float | None
    reason_codes: tuple[str, ...]
    horizontal_source_coherent: bool


def synchronized_plane_dict(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with plane_dict_lock:
            return function(*args, **kwargs)
    return locked

# Ustawienie jednostek metrycznych / Set desired units
metric_units = True

# Inicjalizacja czasu z uwzględnieniem strefy czasowej / Initialize time with timezone
aktual_t = clock.now_utc() if clock.is_ready() else None
last_t = clock.now_utc() - datetime.timedelta(seconds=10) if clock.is_ready() else None
gong_t = clock.now_utc() if clock.is_ready() else None

# Ustawienie pożądanych limitów odległości i czasu / Set desired distance and time limits
warning_distance = 200  # Odległość ostrzegawcza / Warning distance
alert_distance = 15  # Odległość alarmowa / Alert distance
xtd_tst = 20  # Odchylenie boczne / Cross-track deviation

# Ustawienia ostrzeżeń dla tranzytów / Transit warning settings
transit_separation_sound_alert = 3
transit_separation_REDALERT_FG = 7
transit_separation_GREENALERT_FG = 3
transit_separation_notignored = 15

# Ustawienia lokalizacji geograficznej i wysokości / Set geographic location and elevation
my_lat = None
my_lon = None
my_elevation_const = None
transition_altitude_ft = None
near_airport_elevation = 100  # Wysokość najbliższego lotniska / Nearest airport elevation

# Ustawienia efemeryd / Ephemeris settings
gatech = None
sun_body_angular_diameter_arcsec = None
moon_body_angular_diameter_arcsec = None
sun_body_evaluated_at_utc = None
moon_body_evaluated_at_utc = None

adsb_host = None
adsb_port = None
adsb_timestamp_timezone = None
mlat_host = None
mlat_port = None
beast_host = None
beast_port = None
raw_adsb_host = None
raw_adsb_port = None
mlat_beast_enabled = False
mlat_beast_host = None
mlat_beast_port = None


def apply_installation_config(configuration: InstallationConfig):
    global my_lat, my_lon, my_elevation_const, transition_altitude_ft
    global metar_station, gatech
    global adsb_host, adsb_port, adsb_timestamp_timezone, adsb_timestamp_validator
    global mlat_host, mlat_port, beast_host, beast_port
    global raw_adsb_host, raw_adsb_port, mlat_beast_enabled
    global mlat_beast_host, mlat_beast_port, port_status
    my_lat = configuration.observer_lat
    my_lon = configuration.observer_lon
    my_elevation_const = configuration.observer_elevation_m
    transition_altitude_ft = configuration.transition_altitude_ft
    metar_station = configuration.metar_station
    adsb_host = configuration.adsb_host
    adsb_port = configuration.adsb_port
    adsb_timestamp_timezone = configuration.adsb_timestamp_timezone
    adsb_timestamp_validator = (
        AdsBTimestampOffsetValidator(adsb_timestamp_timezone)
        if not isinstance(clock, ReplayClock) else None
    )
    mlat_host = configuration.mlat_host
    mlat_port = configuration.mlat_port
    beast_host = configuration.beast_host
    beast_port = configuration.beast_port
    raw_adsb_host = configuration.raw_adsb_host
    raw_adsb_port = configuration.raw_adsb_port
    mlat_beast_enabled = configuration.mlat_beast_enabled
    mlat_beast_host = configuration.mlat_beast_host
    mlat_beast_port = configuration.mlat_beast_port
    gatech = ephem.Observer()
    gatech.lat, gatech.lon = str(my_lat), str(my_lon)
    gatech.elevation = my_elevation_const
    port_status = {adsb_port: False, mlat_port: False}


def correct_pressure_altitude(pressure_altitude_ft, qnh_hpa):
    """Apply the existing linear QNH approximation to pressure altitude."""
    return (pressure_altitude_ft
            + (qnh_hpa - 1013.25) * QNH_CORRECTION_FT_PER_HPA)


def _record_altitude_measurement(
        icao, port, altitude_baro_ft, altitude_corrected_m,
        timestamp_utc, message_type):
    if port == adsb_port:
        source = "adsb"
    elif port == mlat_port:
        source = "mlat"
    else:
        return
    altitude_sources.setdefault(icao, {})[source] = AltitudeMeasurement(
        source=source,
        altitude_kind="barometric",
        altitude_baro_ft=altitude_baro_ft,
        altitude_corrected_m=altitude_corrected_m,
        timestamp_utc=timestamp_utc,
        message_type="MSG,{}".format(message_type),
    )


def get_altitude_diagnostics(icao, now_utc=None):
    """Return current geometry altitude and latest per-source measurements."""
    with plane_dict_lock:
        measurements = altitude_sources.get(icao, {})
        adsb = measurements.get("adsb")
        mlat = measurements.get("mlat")
        current = plane_dict.get(icao, [None, None, None, None, None])[4]
        now = (
            clock.now_utc() if now_utc is None else now_utc
        ) if adsb is not None or mlat is not None else None
        delta = (
            adsb.altitude_baro_ft - mlat.altitude_baro_ft
            if adsb is not None and mlat is not None else None
        )
        return AltitudeDiagnostics(
            current_geometry_altitude_m=current,
            latest_adsb=adsb,
            latest_mlat=mlat,
            delta_adsb_mlat_ft=delta,
            adsb_age_seconds=(now - adsb.timestamp_utc).total_seconds()
            if adsb is not None else None,
            mlat_age_seconds=(now - mlat.timestamp_utc).total_seconds()
            if mlat is not None else None,
        )


def _motion_source_for_port(port):
    if port == adsb_port:
        return "adsb"
    if port == mlat_port:
        return "mlat"
    return None


def _motion_state_for_update(icao):
    return aircraft_motion_states.setdefault(icao, AircraftMotionState())


def _update_motion_parameter(icao, name, value, updated_at_utc, port):
    source = _motion_source_for_port(port)
    if source is None:
        return
    state = _motion_state_for_update(icao)
    parameter = MotionParameter(float(value), updated_at_utc, source)
    setattr(state, name, parameter)
    if name == "vertical_rate":
        state.vertical_rate_history.append(parameter)
    if mlat_beast_enabled and name == "track" and port == mlat_port:
        mlat_coarse_tracks[icao] = parameter
        _reconcile_mlat_beast_track(icao, parameter, updated_at_utc)


def _update_motion_position(
        icao, latitude, longitude, updated_at_utc, port):
    source = _motion_source_for_port(port)
    if source is None:
        return
    _motion_state_for_update(icao).position = PositionParameter(
        float(latitude), float(longitude), updated_at_utc, source)


def update_raw_adsb_track(decoded, updated_at_utc):
    """Store a separate high-precision RAW track without replacing SBS/MLAT."""
    with plane_dict_lock:
        coarse = aircraft_motion_states.get(decoded.icao)
        coarse = coarse.track if coarse is not None else None
        coarse_age = (
            max(0.0, (updated_at_utc - coarse.updated_at_utc).total_seconds())
            if coarse is not None else None)
        anchor = (
            coarse.value
            if coarse_age is not None
            and coarse_age <= MOTION_FRESH_PARAMETER_SECONDS else None)
        raw_adsb_tracks[decoded.icao] = RawAdsbTrackState(
            precise_value_deg=decoded.track_deg,
            raw_updated_at_utc=updated_at_utc,
            coarse_anchor_deg=anchor,
        )


def _confirm_mlat_beast_track(state, coarse, now_utc):
    if (coarse is None or coarse.source != "mlat"
            or max(0.0, (now_utc - coarse.updated_at_utc).total_seconds())
            > MOTION_FRESH_PARAMETER_SECONDS
            or abs((state.received_at_utc
                    - coarse.updated_at_utc).total_seconds())
            > MOTION_FRESH_DELTA_SECONDS
            or not truncation_bin_consistent(state, coarse.value)):
        return False
    state.coarse_anchor_deg = float(coarse.value)
    state.coarse_anchor_timestamp_utc = coarse.updated_at_utc
    state.confirmed = True
    return True


def _reconcile_mlat_beast_track(icao, coarse, now_utc):
    state = mlat_beast_tracks.get(icao)
    if state is None or not state.hold_valid:
        return
    if not state.confirmed:
        _confirm_mlat_beast_track(state, coarse, now_utc)
        return
    anchor_time = state.coarse_anchor_timestamp_utc
    if (anchor_time is not None and coarse.updated_at_utc > anchor_time
            and float(coarse.value) != state.coarse_anchor_deg):
        state.hold_valid = False


def update_mlat_beast_track(decoded, updated_at_utc):
    """Store a pending precision track and confirm it against fresh 30106."""
    with plane_dict_lock:
        state = MlatBeastTrackState(
            precise_value_deg=decoded.track_deg,
            received_at_utc=updated_at_utc,
            east_west_velocity_knots=decoded.east_west_velocity_knots,
            north_south_velocity_knots=decoded.north_south_velocity_knots,
            derived_groundspeed_knots=decoded.groundspeed_knots,
            angular_interval_low_deg=decoded.angular_interval_low_deg,
            angular_interval_high_deg=decoded.angular_interval_high_deg,
        )
        mlat_beast_tracks[decoded.icao] = state
        _confirm_mlat_beast_track(
            state, mlat_coarse_tracks.get(decoded.icao), updated_at_utc)


def _effective_mlat_beast_track_locked(icao, now_utc):
    state = mlat_beast_tracks.get(icao)
    if state is None or not state.confirmed or not state.hold_valid:
        return None
    coarse = mlat_coarse_tracks.get(icao)
    if (coarse is not None
            and state.coarse_anchor_timestamp_utc is not None
            and coarse.updated_at_utc > state.coarse_anchor_timestamp_utc
            and float(coarse.value) != state.coarse_anchor_deg):
        state.hold_valid = False
        return None
    precise_age = max(
        0.0, (now_utc - state.received_at_utc).total_seconds())
    if precise_age <= MOTION_FRESH_PARAMETER_SECONDS:
        return MotionParameter(
            state.precise_value_deg, state.received_at_utc,
            "MLAT_BEAST_TC19_FRESH")
    coarse_age = (
        max(0.0, (now_utc - coarse.updated_at_utc).total_seconds())
        if coarse is not None else None)
    if (coarse is not None and coarse.source == "mlat"
            and coarse_age <= MOTION_FRESH_PARAMETER_SECONDS
            and float(coarse.value) == state.coarse_anchor_deg):
        return MotionParameter(
            state.precise_value_deg, coarse.updated_at_utc,
            "MLAT_BEAST_TC19_HELD")
    return None


def effective_track_parameter(icao, fallback_track=None, now_utc=None):
    """Return the approved precision source, otherwise the coarse track."""
    now = clock.now_utc() if now_utc is None else now_utc
    with plane_dict_lock:
        state = aircraft_motion_states.get(icao)
        coarse = state.track if state is not None else None
        coarse_age = (
            max(0.0, (now - coarse.updated_at_utc).total_seconds())
            if coarse is not None else None)
        coarse_fresh = (
            coarse_age is not None
            and coarse_age <= MOTION_FRESH_PARAMETER_SECONDS)
        raw_track = raw_adsb_tracks.get(icao)
        if raw_track is not None and raw_track.hold_valid:
            anchor = raw_track.coarse_anchor_deg
            if (anchor is not None and coarse_fresh
                    and coarse.value != anchor):
                raw_track.hold_valid = False
            else:
                raw_age = max(
                    0.0, (now - raw_track.raw_updated_at_utc).total_seconds())
                if raw_age <= MOTION_FRESH_PARAMETER_SECONDS:
                    return MotionParameter(
                        raw_track.precise_value_deg,
                        raw_track.raw_updated_at_utc,
                        "RAW_ADSB_TC19_FRESH")
                if anchor is not None and coarse_fresh and coarse.value == anchor:
                    return MotionParameter(
                        raw_track.precise_value_deg,
                        coarse.updated_at_utc,
                        "RAW_ADSB_TC19_HELD")
        position = state.position if state is not None else None
        if (mlat_beast_enabled and position is not None
                and position.source == "mlat"):
            mlat_precise = _effective_mlat_beast_track_locked(icao, now)
            if mlat_precise is not None:
                return mlat_precise
        if coarse is not None:
            return coarse
    if fallback_track is not None and is_float_try(fallback_track):
        return MotionParameter(float(fallback_track), now, "fallback")
    return None


def effective_motion_state(icao, now_utc):
    """Copy motion state with only its prediction track precedence applied."""
    state = get_aircraft_motion_state(icao)
    if state is None:
        return None
    fallback_track = state.track.value if state.track is not None else None
    effective_track = effective_track_parameter(icao, fallback_track, now_utc)
    state.track = effective_track
    return state


def format_track_for_display(icao, fallback_track, now_utc=None):
    """Show one decimal while fresh or coarse-confirmed RAW is effective."""
    parameter = effective_track_parameter(icao, fallback_track, now_utc)
    if (parameter is not None
            and parameter.source in (
                "RAW_ADSB_TC19_FRESH", "RAW_ADSB_TC19_HELD",
                "MLAT_BEAST_TC19_FRESH", "MLAT_BEAST_TC19_HELD")):
        return "{:.1f}".format(parameter.value)
    return fallback_track


def get_aircraft_motion_state(icao):
    """Return a stable diagnostic snapshot of one aircraft's motion state."""
    with plane_dict_lock:
        state = aircraft_motion_states.get(icao)
        if state is None:
            return None
        return AircraftMotionState(
            position=state.position,
            altitude=state.altitude,
            track=state.track,
            groundspeed=state.groundspeed,
            vertical_rate=state.vertical_rate,
            vertical_rate_history=deque(
                state.vertical_rate_history,
                maxlen=VERTICAL_RATE_HISTORY_MAXLEN),
        )


def get_aircraft_motion_freshness(icao, now_utc=None):
    """Return per-parameter ages without applying a freshness policy."""
    state = get_aircraft_motion_state(icao)
    if state is None:
        return None
    now = clock.now_utc() if now_utc is None else now_utc

    def age(parameter):
        return ((now - parameter.updated_at_utc).total_seconds()
                if parameter is not None else None)

    return AircraftMotionFreshness(
        position_age=age(state.position),
        altitude_age=age(state.altitude),
        track_age=age(state.track),
        groundspeed_age=age(state.groundspeed),
        vertical_rate_age=age(state.vertical_rate),
    )


def assess_motion_freshness(motion_state, now_utc):
    """Classify one timestamped motion state without consulting wall time."""
    def age(parameter):
        if parameter is None:
            return None
        return max(0.0, (now_utc - parameter.updated_at_utc).total_seconds())

    position_age = age(motion_state.position) if motion_state else None
    altitude_age = age(motion_state.altitude) if motion_state else None
    track_age = age(motion_state.track) if motion_state else None
    groundspeed_age = age(motion_state.groundspeed) if motion_state else None
    vertical_rate_age = age(motion_state.vertical_rate) if motion_state else None
    position_track_delta = (
        abs(position_age - track_age)
        if position_age is not None and track_age is not None else None)
    position_groundspeed_delta = (
        abs(position_age - groundspeed_age)
        if position_age is not None and groundspeed_age is not None else None)

    parameters = {
        "POSITION": position_age,
        "ALTITUDE": altitude_age,
        "TRACK": track_age,
        "GROUNDSPEED": groundspeed_age,
    }
    reasons = [
        "MISSING_{}".format(name)
        for name, parameter_age in parameters.items()
        if parameter_age is None
    ]
    stale_age = False
    for name, parameter_age in parameters.items():
        if parameter_age is not None and parameter_age > MOTION_STALE_SECONDS:
            stale_age = True
            reasons.append("{}_AGE_GT_10".format(name))
    stale_delta = False
    for name, delta in (
            ("POSITION_TRACK", position_track_delta),
            ("POSITION_GROUNDSPEED", position_groundspeed_delta)):
        if delta is not None and delta > MOTION_STALE_DELTA_SECONDS:
            stale_delta = True
            reasons.append("{}_DELTA_GT_10".format(name))

    complete = all(value is not None for value in parameters.values())
    non_stale_ages = complete and not stale_age and not stale_delta
    fresh = (
        non_stale_ages
        and position_age <= MOTION_FRESH_POSITION_SECONDS
        and track_age <= MOTION_FRESH_PARAMETER_SECONDS
        and groundspeed_age <= MOTION_FRESH_PARAMETER_SECONDS
        and altitude_age <= MOTION_FRESH_PARAMETER_SECONDS
        and position_track_delta <= MOTION_FRESH_DELTA_SECONDS
        and position_groundspeed_delta <= MOTION_FRESH_DELTA_SECONDS
    )
    if not complete or stale_age or stale_delta:
        status = MotionFreshnessStatus.STALE
    elif fresh:
        status = MotionFreshnessStatus.FRESH
    else:
        status = MotionFreshnessStatus.DEGRADED
        if position_age > MOTION_FRESH_POSITION_SECONDS:
            reasons.append("POSITION_AGE_GT_3")
        for name, parameter_age in (
                ("TRACK", track_age),
                ("GROUNDSPEED", groundspeed_age),
                ("ALTITUDE", altitude_age)):
            if parameter_age > MOTION_FRESH_PARAMETER_SECONDS:
                reasons.append("{}_AGE_GT_5".format(name))
        if position_track_delta > MOTION_FRESH_DELTA_SECONDS:
            reasons.append("POSITION_TRACK_DELTA_GT_3")
        if position_groundspeed_delta > MOTION_FRESH_DELTA_SECONDS:
            reasons.append("POSITION_GROUNDSPEED_DELTA_GT_3")

    horizontal = (
        motion_state is not None
        and motion_state.position is not None
        and motion_state.track is not None
        and motion_state.groundspeed is not None
    )
    if horizontal:
        sources = {
            motion_state.position.source,
            motion_state.track.source,
            motion_state.groundspeed.source,
        }
        horizontal = len(sources) == 1 or non_stale_ages

    return MotionFreshnessResult(
        status=status,
        assessed_at_utc=now_utc,
        position_age=position_age,
        altitude_age=altitude_age,
        track_age=track_age,
        groundspeed_age=groundspeed_age,
        vertical_rate_age=vertical_rate_age,
        position_track_delta=position_track_delta,
        position_groundspeed_delta=position_groundspeed_delta,
        reason_codes=tuple(reasons),
        horizontal_source_coherent=horizontal,
    )


def get_aircraft_motion_freshness_status(icao):
    """Return the latest diagnostic freshness assessment for one aircraft."""
    with plane_dict_lock:
        return aircraft_motion_freshness_status.get(icao)

last_update_time = clock.now_utc() if clock.is_ready() else None  # Inicjalizacja zmiennej na początku skryptu / Initialize variable at the beginning of the script

port_status = {}


def configure_environment_replay(path):
    global environment_replay
    environment_replay = (
        EnvironmentReplay(iter_environment_events(path)) if path is not None else None
    )


def configure_environment_recording(path):
    global environment_recorder
    if path is None:
        environment_recorder = None
        return
    initial_event = EnvironmentEvent(
        version=1,
        time=clock.now_utc(),
        type="qnh",
        value_hpa=pressure,
        source="fallback",
        station=metar_station,
    )
    environment_recorder = EnvironmentRecorder(path, initial_event)


def session_recorder_statuses():
    if not session_recording_requested:
        return (RecordingStatus.OFF.value, RecordingStatus.OFF.value)
    if session_recorder is None:
        return (RecordingStatus.FAILED.value, RecordingStatus.FAILED.value)
    adsb_writer = session_recorder.adsb_writer
    mlat_writer = session_recorder.mlat_writer
    return (
        adsb_writer.status.value if adsb_writer is not None else RecordingStatus.FAILED.value,
        mlat_writer.status.value if mlat_writer is not None else RecordingStatus.FAILED.value,
    )


def source_status_lines():
    adsb_recorder_status, mlat_recorder_status = session_recorder_statuses()
    adsb_port_status = "Listening" if port_status.get(adsb_port, False) else "Not listening"
    mlat_port_status = "Listening" if port_status.get(mlat_port, False) else "Not listening"
    return (
        "ADS-B  Port {}: {}  |  Recorder: {}".format(
            adsb_port, adsb_port_status, adsb_recorder_status),
        "MLAT   Port {}: {}  |  Recorder: {}".format(
            mlat_port, mlat_port_status, mlat_recorder_status),
    )


def initialize_daily_environment(base_dir=None):
    global daily_environment_recorder, pressure, metar_t, metar_attempt_t
    if isinstance(clock, ReplayClock):
        daily_environment_recorder = None
        return None

    now_utc = clock.now_utc()
    recorder_options = {
        "fallback_station": metar_station,
        "error_handler": lambda message: print(message),
    }
    if base_dir is not None:
        recorder_options["base_dir"] = base_dir
    daily_environment_recorder = DailyEnvironmentRecorder(now_utc, **recorder_options)
    recovered = daily_environment_recorder.recover_recent_qnh(now_utc)
    pressure = recovered.value_hpa if recovered is not None else 1013
    metar_t = None
    metar_attempt_t = None
    return recovered


def apply_replay_environment(timestamp_utc):
    global pressure
    if environment_replay is None:
        return
    for event in environment_replay.pop_through(timestamp_utc):
        if event.type == "qnh":
            pressure = event.value_hpa


def advance_replay_time(timestamp_utc):
    global replay_time_initialized, metar_t, metar_attempt_t, aktual_t, last_t, gong_t, last_update_time
    global sun_alt, sun_az, moon_alt, moon_az
    if not isinstance(clock, ReplayClock):
        return
    with replay_time_lock:
        clock.advance_to(timestamp_utc)
        if not replay_time_initialized:
            current_time = clock.now_utc()
            metar_t = None
            metar_attempt_t = None
            aktual_t = current_time
            last_t = current_time - datetime.timedelta(seconds=10)
            gong_t = current_time
            last_update_time = current_time
            sun_alt, sun_az, moon_alt, moon_az = tabela()
            replay_time_initialized = True

# Funkcja do czyszczenia ekranu / Function to clear the screen
TERMINAL_HOME_CLEAR = "\x1b[H\x1b[J"
TABLE_FIXED_OUTPUT_LINES = 9
TERMINAL_SCROLL_GUARD_LINES = 1
TRANSIT_TIME_DISPLAY_PRECISION = 3


@dataclass(frozen=True)
class TerminalRenderPlan:
    aircraft_ids: tuple
    shown_count: int
    total_count: int


def clear_screen(output=None):
    """Start a terminal frame at the visible top-left corner."""
    output = sys.stdout if output is None else output
    output.write(TERMINAL_HOME_CLEAR)
    output.flush()


def terminal_aircraft_row_limit(terminal_lines=None):
    """Return the aircraft rows that fit without scrolling the frame."""
    if terminal_lines is None:
        terminal_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
    return max(
        0,
        terminal_lines - TABLE_FIXED_OUTPUT_LINES - TERMINAL_SCROLL_GUARD_LINES,
    )


def _prediction_timestamps(celestial_body):
    if celestial_body == "sun":
        return sun_prediction_last_valid, sun_predicted_transit_utc
    return moon_prediction_last_valid, moon_predicted_transit_utc


def predicted_transit_remaining_seconds(icao, celestial_body, now_utc=None):
    """Return whole future seconds remaining on the configured clock."""
    predicted_times = _prediction_timestamps(celestial_body)[1]
    predicted_utc = predicted_times.get(icao)
    if predicted_utc is None:
        return None
    now_utc = clock.now_utc() if now_utc is None else now_utc
    return max(0, int((predicted_utc - now_utc).total_seconds()))


def visible_transit_candidate(entry, celestial_body, icao=None, now_utc=None):
    """Return a numeric display block only for a visible transit candidate."""
    indices = (
        (18, 19, 21, 20, 22)
        if celestial_body == "sun" else (23, 24, 27, 25, 26))
    try:
        body_alt, predicted_alt, p2x, h2x, stored_time2x = (
            float(entry[index]) for index in indices)
    except (IndexError, TypeError, ValueError):
        return None
    dynamic_time2x = (
        predicted_transit_remaining_seconds(icao, celestial_body, now_utc)
        if icao is not None else None)
    time2x = stored_time2x if dynamic_time2x is None else dynamic_time2x
    separation = vertical_transit_separation(predicted_alt, body_alt)
    if time2x <= 0 or separation >= transit_separation_notignored:
        return None
    return separation, p2x, h2x, time2x


def build_terminal_render_plan(planes, row_limit, maximum_distance,
                               now_utc=None):
    """Prioritize a display-only copy without changing tracked aircraft order."""
    candidates = []
    remaining = []

    for original_index, icao in enumerate(planes):
        entry = planes[icao]
        sun_candidate = visible_transit_candidate(
            entry, "sun", icao, now_utc)
        moon_candidate = visible_transit_candidate(
            entry, "moon", icao, now_utc)
        sun_time = sun_candidate[3] if sun_candidate is not None else None
        moon_time = moon_candidate[3] if moon_candidate is not None else None

        try:
            is_renderable = (
                maximum_distance is None
                or float(entry[5]) <= maximum_distance
            )
        except (IndexError, TypeError, ValueError):
            is_renderable = maximum_distance is None
        if not is_renderable:
            continue

        if sun_time is None and moon_time is None:
            remaining.append(icao)
            continue

        transit_times = [
            (sun_time, 0),
            (moon_time, 1),
        ]
        nearest_time, body_priority = min(
            (item for item in transit_times if item[0] is not None),
            key=lambda item: (round(item[0], TRANSIT_TIME_DISPLAY_PRECISION),
                              item[1]),
        )
        candidates.append((
            round(nearest_time, TRANSIT_TIME_DISPLAY_PRECISION),
            body_priority,
            original_index,
            icao,
        ))

    candidates.sort()
    ordered = [candidate[3] for candidate in candidates] + remaining
    shown = tuple(ordered[:max(0, row_limit)])
    return TerminalRenderPlan(
        aircraft_ids=shown,
        shown_count=len(shown),
        total_count=len(planes),
    )


def terminal_tracking_summary(observer_lat, observer_lon, render_plan):
    return (
        "LAT: {} LON: {} | Aircraft: {}/{} shown".format(
            observer_lat,
            observer_lon,
            render_plan.shown_count,
            render_plan.total_count,
        )
    )


def vertical_transit_separation(predicted_alt, body_alt):
    """Return the existing vertical-only transit separation as a magnitude."""
    if not is_float_try(predicted_alt) or not is_float_try(body_alt):
        return 90.0
    return abs(float(predicted_alt) - float(body_alt))


def vertical_motion_input_from_runtime(motion_state):
    """Freeze only altitude and vertical-rate inputs required by 2E."""
    if motion_state is None:
        return None
    return VerticalMotionState(
        altitude=motion_state.altitude,
        vertical_rate=motion_state.vertical_rate,
        vertical_rate_history=tuple(motion_state.vertical_rate_history),
    )


def vertical_intent_input_from_runtime(intent_state):
    """Freeze only selected-altitude and QNH inputs required by 2F."""
    if intent_state is None:
        return None
    return VerticalIntentState(
        selected_altitude=intent_state.selected_altitude,
        nav_qnh=intent_state.nav_qnh,
    )


def predict_transit_altitude(current_altitude_m, motion_state, now_utc,
                             final_time2x_seconds, policy=None):
    """Compatibility adapter to the shared frozen vertical model."""
    return shared_predict_transit_altitude(
        current_altitude_m, vertical_motion_input_from_runtime(motion_state),
        now_utc, final_time2x_seconds, policy)


def update_aircraft_intent(intent, received_at_utc):
    """Store one valid TC29 intent sample independently from motion state."""
    with plane_dict_lock:
        state = aircraft_intent_states.setdefault(
            intent.icao, AircraftIntentState())
        selected = IntentParameter(
            intent.selected_altitude_ft, received_at_utc,
            intent.selected_altitude_source)
        state.selected_altitude = selected
        history_entry = SelectedAltitudeHistoryEntry(
            intent.selected_altitude_ft, intent.nav_qnh_hpa,
            received_at_utc, intent.selected_altitude_source)
        if (not state.selected_altitude_history
                or (state.selected_altitude_history[-1].selected_altitude_ft,
                    state.selected_altitude_history[-1].nav_qnh_hpa,
                    state.selected_altitude_history[-1].source)
                != (history_entry.selected_altitude_ft,
                    history_entry.nav_qnh_hpa, history_entry.source)):
            state.selected_altitude_history.append(history_entry)
        if intent.nav_qnh_hpa is not None:
            state.nav_qnh = IntentParameter(
                intent.nav_qnh_hpa, received_at_utc, "ADS-B TC29")
        capture_transit_observation(
            intent.icao, received_at_utc, "BEAST", "DF17,TC29")


def clamp_vertical_prediction_to_intent_state(
        prediction, intent_state, now_utc, qnh_hpa, policy=None):
    """Compatibility adapter to the shared frozen TC29 clamp."""
    return shared_clamp_vertical(
        prediction, vertical_intent_input_from_runtime(intent_state),
        now_utc, qnh_hpa, policy)


def clamp_vertical_prediction_to_selected_altitude(
        icao, prediction, now_utc, qnh_hpa):
    """Compatibility wrapper using the current stored TC29 intent."""
    return clamp_vertical_prediction_to_intent_state(
        prediction, aircraft_intent_states.get(icao), now_utc, qnh_hpa)


def predict_vertical_state_at_time(
        current_altitude_m, motion_state, intent_state, now_utc,
        dt_seconds, qnh_hpa, policy=None):
    """Compatibility adapter composing the shared frozen 2E/2F model."""
    return shared_predict_vertical_state(
        current_altitude_m,
        vertical_motion_input_from_runtime(motion_state),
        vertical_intent_input_from_runtime(intent_state),
        now_utc, dt_seconds, qnh_hpa, policy)


def apply_vertical_prediction_to_transit_result(
        icao, celestial_body, transit_result, current_altitude_m, now_utc):
    """Update only the final vertical angle of a solved 2D transit."""
    if not transit_result:
        return transit_result
    vertical_state = predict_vertical_state_at_time(
        current_altitude_m,
        aircraft_motion_states.get(icao),
        aircraft_intent_states.get(icao),
        now_utc,
        transit_result[6],
        pressure,
    )
    prediction_2e = vertical_state.prediction_before_clamp
    prediction = vertical_state.prediction
    intent_details = vertical_state.intent_details
    before = vertical_transit_separation(
        transit_result[3], transit_result[9])
    updated = list(transit_result)
    if prediction.mode == VerticalPredictionMode.DYNAMIC_VALID:
        h2x_km = float(transit_result[4])
        if h2x_km == 0:
            h2x_km = 0.001
        updated[3] = degrees(atan(
            (prediction.predicted_altitude_m - my_elevation_const)
            / (h2x_km * 1000)))
    after = vertical_transit_separation(updated[3], updated[9])
    altitude_before_clamp = float(transit_result[3])
    if prediction_2e.mode == VerticalPredictionMode.DYNAMIC_VALID:
        h2x_km = float(transit_result[4]) or 0.001
        altitude_before_clamp = degrees(atan(
            (prediction_2e.predicted_altitude_m - my_elevation_const)
            / (h2x_km * 1000)))
    intent_details["separation_before_clamp"] = vertical_transit_separation(
        altitude_before_clamp, transit_result[9])
    vertical_transit_diagnostics[(icao, celestial_body)] = (
        VerticalTransitDiagnostic(
            body=celestial_body,
            prediction=prediction,
            separation_before=before,
            separation_after=after,
            **intent_details,
        ))
    return tuple(updated)


def production_aircraft_state_at_transit(
        transit_result, predicted_altitude_m):
    """Represent production T0 without re-propagating rounded time2X."""
    return ProductionAircraftState(
        latitude_deg=transit_result[0],
        longitude_deg=transit_result[1],
        altitude_m=predicted_altitude_m,
        azimuth_from_observer_deg=transit_result[2],
        altitude_angle_deg=transit_result[3],
    )


def get_vertical_transit_diagnostic(icao, celestial_body):
    """Return the latest immutable post-solver vertical diagnostic."""
    with plane_dict_lock:
        return vertical_transit_diagnostics.get((icao, celestial_body))


def _capture_transit_prediction(icao, callsign, celestial_body,
                                transit_result, now_utc, solver_input):
    """Pass an already solved prediction to the optional validation layer."""
    if transit_snapshot_manager is None or not transit_result:
        return
    diagnostic = vertical_transit_diagnostics.get((icao, celestial_body))
    vertical = None
    if diagnostic is not None:
        prediction = diagnostic.prediction
        vertical = {
            "mode": prediction.mode.value,
            "reason": prediction.reason,
            "vertical_rate_fpm": prediction.last_vertical_rate_fpm,
            "vertical_rate_age_seconds": prediction.vertical_rate_age_seconds,
            "applied_seconds": prediction.applied_seconds,
            "predicted_altitude_m": prediction.predicted_altitude_m,
            "intent_reason": diagnostic.intent_reason,
            "intent_clamped": diagnostic.intent_clamped,
            "selected_altitude_ft": diagnostic.selected_altitude_ft,
            "nav_qnh_hpa": diagnostic.nav_qnh_hpa,
            "target_altitude_m": diagnostic.target_altitude_m,
        }
    solver_diagnostic = transit_solver_diagnostics.get(
        (icao, celestial_body))
    body_size = (
        solver_diagnostic.body_angular_diameter_arcsec
        if solver_diagnostic is not None else None)
    predicted_utc = _prediction_timestamps(celestial_body)[1].get(icao)
    if predicted_utc is None:
        return
    aircraft_altitude_m = (
        diagnostic.prediction.predicted_altitude_m
        if diagnostic is not None else solver_input["aircraft_altitude_m"])
    frozen_prediction_state = build_frozen_prediction_state(
        icao, celestial_body, transit_result, now_utc, solver_input,
        diagnostic, solver_diagnostic, pressure)
    transit_snapshot_manager.consider_prediction({
        "recorded_at_utc": now_utc,
        "prediction_base_utc": _snapshot_utc_text(now_utc),
        "predicted_transit_utc": predicted_utc,
        "icao": icao,
        "callsign": callsign or None,
        "body": celestial_body.upper(),
        "observer": {
            "lat": my_lat, "lon": my_lon,
            "elevation_m": my_elevation_const,
        },
        "time2x_seconds": float(transit_result[6]),
        "aircraft_altitude_m": aircraft_altitude_m,
        "separation_deg": vertical_transit_separation(
            transit_result[3], transit_result[9]),
        "predicted_aircraft_elevation_deg": float(transit_result[3]),
        "body_altitude_deg": float(transit_result[9]),
        "body_azimuth_deg": float(transit_result[8]),
        "h2x_km": float(transit_result[4]),
        "p2x_km": float(transit_result[5]),
        "groundspeed": solver_input["groundspeed"],
        "track": solver_input["track"],
        "vertical_prediction": vertical,
        "solver_input": dict(solver_input),
        "intersection": {
            "lat": float(transit_result[0]),
            "lon": float(transit_result[1]),
            "azimuth_from_observer_deg": float(transit_result[2]),
            "aircraft_altitude_deg": float(transit_result[3]),
            "body_azimuth_deg": float(transit_result[8]),
            "body_altitude_deg": float(transit_result[9]),
            "signed_vertical_offset_deg": (
                float(transit_result[3]) - float(transit_result[9])),
        },
        "body_angular_diameter_arcsec": body_size,
        "frozen_prediction_state": frozen_prediction_state,
    })


def capture_transit_prediction(icao, callsign, celestial_body,
                               transit_result, now_utc, solver_input):
    """Keep every TC29G failure outside the aircraft input path."""
    try:
        _capture_transit_prediction(
            icao, callsign, celestial_body, transit_result, now_utc,
            solver_input)
    except Exception:
        pass


def initialize_transit_snapshots():
    """Initialize the optional validation layer without affecting startup."""
    global transit_snapshot_manager
    try:
        transit_snapshot_manager = TransitSnapshotManager(
            sep_threshold_deg=TRANSIT_SNAPSHOT_SEP_THRESHOLD_DEG,
            arm_seconds=TRANSIT_SNAPSHOT_ARM_SECONDS,
            finalize_grace_seconds=(
                TRANSIT_SNAPSHOT_FINALIZE_GRACE_SECONDS),
            git_commit=transit_warning_git_commit)
    except Exception:
        transit_snapshot_manager = None
    return transit_snapshot_manager


def finalize_transit_snapshots(now_utc):
    if transit_snapshot_manager is None:
        return []
    try:
        return transit_snapshot_manager.finalize_due(now_utc)
    except Exception:
        return []


def close_transit_snapshots(now_utc):
    if transit_snapshot_manager is None:
        return []
    try:
        return transit_snapshot_manager.close(now_utc)
    except Exception:
        return []


def drop_transit_snapshot_buffer(icao):
    if transit_snapshot_manager is None:
        return False
    try:
        return transit_snapshot_manager.drop_aircraft_buffer(icao)
    except Exception:
        return False


def terminal_transit_values(entry):
    """Return display blocks in header order: Sun first, then Moon."""
    return (
        (vertical_transit_separation(entry[19], entry[18]),
         entry[21], entry[20], entry[22]),
        (vertical_transit_separation(entry[24], entry[23]),
         entry[27], entry[25], entry[26]),
    )


def clear_transit_prediction(entry, start_index):
    entry[start_index:start_index + 5] = [""] * 5


def update_transit_prediction_timestamp(icao, celestial_body, now_utc,
                                        time2x_seconds):
    last_valid, predicted_times = _prediction_timestamps(celestial_body)
    last_valid[icao] = now_utc
    predicted_times[icao] = now_utc + datetime.timedelta(
        seconds=time2x_seconds)


def clear_transit_prediction_state(icao, entry, celestial_body,
                                   start_index):
    clear_transit_prediction(entry, start_index)
    last_valid, predicted_times = _prediction_timestamps(celestial_body)
    last_valid.pop(icao, None)
    predicted_times.pop(icao, None)
    vertical_transit_diagnostics.pop((icao, celestial_body), None)


def _store_transit_solver_solution(icao, celestial_body, solution):
    """Store production diagnostics while accepting simple test doubles."""
    if isinstance(solution, MovingBodyTransitSolution):
        transit_solver_diagnostics[(icao, celestial_body)] = (
            solution.diagnostic)
        return solution.result
    return solution


def expire_transit_prediction_after_grace(icao, entry, celestial_body,
                                          start_index, now_utc):
    """Keep one missing prediction briefly to absorb input-stream jitter."""
    timestamps = (
        sun_prediction_last_valid
        if celestial_body == "sun" else moon_prediction_last_valid)
    last_valid = timestamps.get(icao)
    has_active_prediction = is_float_try(entry[start_index + 4])
    if (not has_active_prediction or last_valid is None
            or (now_utc - last_valid).total_seconds()
            >= TRANSIT_PREDICTION_GRACE_SECONDS):
        clear_transit_prediction_state(
            icao, entry, celestial_body, start_index)


# Funkcja do czyszczenia słownika samolotów / Function to clean the plane dictionary
@synchronized_plane_dict
def clean_dict():
    current_time = clock.now_utc()
    to_delete = [icao for icao, entry in plane_dict.items() if (current_time - entry[0]).total_seconds() > MAX_AGE_SECONDS]
    for icao in to_delete:
        del plane_dict[icao]
        altitude_sources.pop(icao, None)
        aircraft_motion_states.pop(icao, None)
        raw_adsb_tracks.pop(icao, None)
        mlat_beast_tracks.pop(icao, None)
        mlat_coarse_tracks.pop(icao, None)
        aircraft_intent_states.pop(icao, None)
        aircraft_motion_freshness_status.pop(icao, None)
        sun_prediction_last_valid.pop(icao, None)
        moon_prediction_last_valid.pop(icao, None)
        sun_predicted_transit_utc.pop(icao, None)
        moon_predicted_transit_utc.pop(icao, None)
        transit_solver_diagnostics.pop((icao, "sun"), None)
        transit_solver_diagnostics.pop((icao, "moon"), None)
        vertical_transit_diagnostics.pop((icao, "sun"), None)
        vertical_transit_diagnostics.pop((icao, "moon"), None)
        drop_transit_snapshot_buffer(icao)

# Funkcja do obliczania odległości między punktami (haversine) / Function to calculate distance between points (haversine)
def haversine(origin, destination):
    lat1, lon1 = origin
    lat2, lon2 = destination
    radius = 6371 if metric_units else 3959
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return radius * c

# Funkcja do obliczania odchylenia bocznego / Function to calculate cross-track deviation
def crosstrack(distance, azimuth, track):
    radius = 6371 if metric_units else 3959
    azimuth = float(azimuth)
    track = float(track)
    return round(abs(asin(sin(distance / radius) * sin(radians(azimuth - track))) * radius), 1)

# Funkcja do logowania tranzytów / Function to log transits
def log_transits(icao, flight, transit_info, celestial_body):
    filename = "transits_log.txt"
    with open(filename, "a") as file:
        date_time = clock.now_utc().strftime("%Y-%m-%d %H:%M:%S")
        line = "{},{},{},{},{},{},{},{},{}\n".format(
            date_time, icao, flight, transit_info['min_distance'],
            transit_info['plane_az'], transit_info['plane_alt'],
            transit_info['celestial_az'], transit_info['celestial_alt'],
            celestial_body
        )
        file.write(line)

# Funkcja do przewidywania tranzytów / Function to predict transits
def transit_pred(obs2moon, plane_pos, track, velocity, elevation, moon_alt, moon_az):
    if moon_alt < 0.1:
        return 0
    moon_az = float(moon_az)
    intersection = solve_great_circle_intersection(
        obs2moon, plane_pos, track, velocity, elevation, moon_az,
        my_elevation_const)
    if intersection is None:
        return 0
    moon_alt_B = 90.00 - moon_alt
    ideal_dist = (sin(radians(moon_alt_B)) * elevation) / sin(radians(moon_alt)) / 1000
    ideal_lat = asin(sin(radians(my_lat)) * cos(ideal_dist / earth_R) + cos(radians(my_lat)) * sin(ideal_dist / earth_R) * cos(radians(moon_az)))
    ideal_lon = radians(my_lon) + atan2(sin(radians(moon_az)) * sin(ideal_dist / earth_R) * cos(radians(my_lat)), cos(ideal_dist / earth_R) - sin(radians(my_lat)) * sin(ideal_lat))
    ideal_lat, ideal_lon = degrees(ideal_lat), degrees(ideal_lon)
    ideal_lon = (ideal_lon + 540) % 360 - 180
    return (intersection.latitude_deg, intersection.longitude_deg,
            intersection.azimuth_from_observer_deg,
            intersection.aircraft_altitude_angle_deg,
            intersection.observer_distance_km,
            intersection.aircraft_distance_km,
            intersection.time_seconds, 0, moon_az, moon_alt,
            clock.now_utc())


def body_position_at_utc(body_name, when_utc):
    """Return one shared PyEphem body state at an explicit UTC time."""
    if (when_utc.tzinfo is None
            or when_utc.utcoffset() != datetime.timedelta(0)):
        raise ValueError("body ephemeris requires timezone-aware UTC")
    observer = ephem.Observer()
    observer.lat = str(my_lat)
    observer.lon = str(my_lon)
    observer.elevation = float(my_elevation_const)
    observer.date = ephem.Date(when_utc.astimezone(pytz.utc))
    if body_name == "sun":
        body = ephem.Sun(observer)
    elif body_name == "moon":
        body = ephem.Moon(observer)
    else:
        raise ValueError("unsupported celestial body: {}".format(body_name))
    body.compute(observer)
    return BodyPosition(
        altitude_deg=math.degrees(body.alt),
        azimuth_deg=math.degrees(body.az),
        angular_diameter_arcsec=float(body.size),
        evaluated_at_utc=when_utc,
    )


def _moving_body_result_time(result):
    try:
        return float(result[6])
    except (IndexError, TypeError, ValueError):
        return None


def _moving_body_result_separation(result):
    if not result:
        return None
    try:
        return vertical_transit_separation(result[3], result[9])
    except (IndexError, TypeError, ValueError):
        return None


def _moving_body_solution(body_name, prediction_base_utc, initial_time,
                          result, correction_count, residual, outcome,
                          body_angular_diameter_arcsec=None,
                          body_ephemeris_evaluated_at_utc=None):
    return MovingBodyTransitSolution(
        result=result,
        diagnostic=MovingBodyTransitDiagnostic(
            body=body_name,
            prediction_base_utc=prediction_base_utc,
            initial_time2x=initial_time,
            final_time2x=_moving_body_result_time(result),
            correction_count=correction_count,
            convergence_residual=residual,
            outcome=outcome,
            final_separation=_moving_body_result_separation(result),
            body_angular_diameter_arcsec=body_angular_diameter_arcsec,
            body_ephemeris_evaluated_at_utc=(
                body_ephemeris_evaluated_at_utc),
        ),
    )


def _body_angular_diameter(body_position):
    return getattr(body_position, "angular_diameter_arcsec", None)


def _ephem_angular_diameter(body):
    try:
        value = float(body.size)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _body_evaluated_at_utc(body_position, fallback=None):
    return getattr(body_position, "evaluated_at_utc", None) or fallback


def _snapshot_parameter(state, name):
    parameter = getattr(state, name, None) if state is not None else None
    return (
        parameter.value if parameter is not None else None,
        parameter.source if parameter is not None else None,
        parameter.updated_at_utc if parameter is not None else None,
    )


def _snapshot_utc_text(value):
    if value is None:
        return None
    return value.astimezone(pytz.utc).isoformat().replace("+00:00", "Z")


def _snapshot_mlat_beast_track(
        icao, effective_track, position, now_utc):
    """Return additive MLAT Beast selection diagnostics without mutation."""
    state = mlat_beast_tracks.get(icao)
    if state is None:
        return None
    coarse = mlat_coarse_tracks.get(icao)
    precise_age = max(
        0.0, (now_utc - state.received_at_utc).total_seconds())
    coarse_age = (
        max(0.0, (now_utc - coarse.updated_at_utc).total_seconds())
        if coarse is not None else None)
    confirmation_delta = (
        abs((state.received_at_utc
             - coarse.updated_at_utc).total_seconds())
        if coarse is not None else None)
    bin_consistent = (
        truncation_bin_consistent(state, coarse.value)
        if coarse is not None else None)
    selected_source = (
        effective_track.source if effective_track is not None else None)

    freshness = "UNAVAILABLE"
    reason = "NOT_CONFIRMED"
    if not state.confirmed:
        if coarse is None:
            reason = "NO_MLAT_COARSE_TRACK"
        elif coarse.source != "mlat":
            reason = "COARSE_SOURCE_NOT_MLAT"
        elif coarse_age > MOTION_FRESH_PARAMETER_SECONDS:
            reason = "COARSE_TRACK_STALE"
        elif confirmation_delta > MOTION_FRESH_DELTA_SECONDS:
            reason = "CONFIRMATION_TIME_DELTA"
        elif not bin_consistent:
            reason = "COARSE_BIN_MISMATCH"
    elif not state.hold_valid:
        reason = "COARSE_ANCHOR_CHANGED"
    elif position is None or position.source != "mlat":
        reason = "POSITION_SOURCE_NOT_MLAT"
    elif precise_age <= MOTION_FRESH_PARAMETER_SECONDS:
        freshness = "FRESH"
        reason = (
            "SELECTED" if selected_source == "MLAT_BEAST_TC19_FRESH"
            else "RAW_ADSB_PRIORITY"
            if selected_source in (
                "RAW_ADSB_TC19_FRESH", "RAW_ADSB_TC19_HELD")
            else "NOT_SELECTED")
    elif (coarse is not None and coarse.source == "mlat"
          and coarse_age <= MOTION_FRESH_PARAMETER_SECONDS
          and float(coarse.value) == state.coarse_anchor_deg):
        freshness = "HELD"
        reason = (
            "SELECTED" if selected_source == "MLAT_BEAST_TC19_HELD"
            else "RAW_ADSB_PRIORITY"
            if selected_source in (
                "RAW_ADSB_TC19_FRESH", "RAW_ADSB_TC19_HELD")
            else "NOT_SELECTED")
    elif coarse is None:
        reason = "NO_MLAT_COARSE_TRACK"
    elif coarse.source != "mlat":
        reason = "COARSE_SOURCE_NOT_MLAT"
    elif coarse_age > MOTION_FRESH_PARAMETER_SECONDS:
        reason = "COARSE_TRACK_STALE"
    elif float(coarse.value) != state.coarse_anchor_deg:
        reason = "COARSE_ANCHOR_CHANGED"
    else:
        reason = "PRECISION_TRACK_EXPIRED"

    return {
        "effective_track_value_deg": (
            effective_track.value if effective_track is not None else None),
        "effective_track_source": selected_source,
        "effective_track_timestamp_utc": _snapshot_utc_text(
            effective_track.updated_at_utc
            if effective_track is not None else None),
        "coarse_track_value_deg": coarse.value if coarse is not None else None,
        "coarse_track_source": coarse.source if coarse is not None else None,
        "coarse_track_timestamp_utc": _snapshot_utc_text(
            coarse.updated_at_utc if coarse is not None else None),
        "precise_track_deg": state.precise_value_deg,
        "received_at_utc": _snapshot_utc_text(state.received_at_utc),
        "east_west_velocity_knots": state.east_west_velocity_knots,
        "north_south_velocity_knots": state.north_south_velocity_knots,
        "derived_groundspeed_knots": state.derived_groundspeed_knots,
        "angular_interval_low_deg": state.angular_interval_low_deg,
        "angular_interval_high_deg": state.angular_interval_high_deg,
        "coarse_anchor_deg": state.coarse_anchor_deg,
        "coarse_anchor_timestamp_utc": _snapshot_utc_text(
            state.coarse_anchor_timestamp_utc),
        "confirmed": state.confirmed,
        "hold_valid": state.hold_valid,
        "freshness_classification": freshness,
        "quality_reason": reason,
        "precise_age_seconds": precise_age,
        "coarse_age_seconds": coarse_age,
        "confirmation_delta_seconds": confirmation_delta,
        "truncation_bin_consistent": bin_consistent,
    }


def _frozen_parameter(parameter, value_key):
    if parameter is None:
        return {value_key: None, "timestamp_utc": None, "source": None}
    return {
        value_key: parameter.value,
        "timestamp_utc": _snapshot_utc_text(parameter.updated_at_utc),
        "source": parameter.source,
    }


def _frozen_vertical_policy(policy):
    return {
        "level_threshold_fpm": policy.level_threshold_fpm,
        "valid_vr_age_seconds": policy.valid_vr_age_seconds,
        "ignore_vr_age_seconds": policy.ignore_vr_age_seconds,
        "altitude_max_age_seconds": policy.altitude_max_age_seconds,
        "stability_sample_count": policy.stability_sample_count,
        "max_spread_fpm": policy.max_spread_fpm,
        "prediction_limit_seconds": policy.prediction_limit_seconds,
        "selected_altitude_freshness_seconds": (
            policy.selected_altitude_freshness_seconds),
        "nav_qnh_freshness_seconds": policy.nav_qnh_freshness_seconds,
        "qnh_correction_ft_per_hpa": policy.qnh_correction_ft_per_hpa,
    }


def _ephem_provider_version():
    return str(getattr(ephem, "__version__", getattr(ephem, "version", "unknown")))


def build_frozen_prediction_state(
        icao, celestial_body, transit_result, prediction_base_utc,
        solver_input, vertical_diagnostic, solver_diagnostic,
        application_qnh_hpa):
    """Serialize the exact model inputs used by one completed prediction."""
    motion_state = aircraft_motion_states.get(icao)
    intent_state = aircraft_intent_states.get(icao)
    policy = current_vertical_prediction_policy()
    history = tuple(
        list(motion_state.vertical_rate_history)[
            -policy.stability_sample_count:]
        if motion_state is not None else ())
    altitude_parameter = (
        motion_state.altitude if motion_state is not None else None)
    vertical_rate_parameter = (
        motion_state.vertical_rate if motion_state is not None else None)
    selected_altitude = (
        intent_state.selected_altitude if intent_state is not None else None)
    nav_qnh = intent_state.nav_qnh if intent_state is not None else None
    prediction = (
        vertical_diagnostic.prediction
        if vertical_diagnostic is not None else None)
    forward_bearing = great_circle_forward_bearing_at_point(
        (solver_input["aircraft_lat"], solver_input["aircraft_lon"]),
        solver_input["track"],
        (transit_result[0], transit_result[1]))
    return {
        "horizontal": {
            "origin_lat": solver_input["aircraft_lat"],
            "origin_lon": solver_input["aircraft_lon"],
            "initial_track_deg": solver_input["track"],
            "groundspeed_kmh": solver_input["groundspeed"],
            "effective_groundspeed_kmh": int(solver_input["groundspeed"]),
            "earth_model": "sphere",
            "earth_radius_km": float(earth_R),
            "distance_rounding_km": 0.1,
            "forward_bearing_at_t0_deg": forward_bearing,
        },
        "vertical": {
            "evaluated_at_utc": _snapshot_utc_text(prediction_base_utc),
            "current_altitude": {
                "value_m": solver_input["aircraft_altitude_m"],
                "timestamp_utc": _snapshot_utc_text(
                    altitude_parameter.updated_at_utc
                    if altitude_parameter is not None else None),
                "source": (
                    altitude_parameter.source
                    if altitude_parameter is not None else None),
            },
            "latest_vertical_rate": _frozen_parameter(
                vertical_rate_parameter, "value_fpm"),
            "vertical_rate_history": [
                _frozen_parameter(sample, "value_fpm") for sample in history
            ],
            "selected_altitude": _frozen_parameter(
                selected_altitude, "value_ft"),
            "nav_qnh": _frozen_parameter(nav_qnh, "value_hpa"),
            "application_qnh_hpa": float(application_qnh_hpa),
            "decision": {
                "mode": prediction.mode.value if prediction is not None else None,
                "reason": prediction.reason if prediction is not None else None,
                "spread_fpm": (
                    prediction.spread_fpm if prediction is not None else None),
                "applied_seconds_at_t0": (
                    prediction.applied_seconds
                    if prediction is not None else None),
                "predicted_altitude_before_clamp_m": (
                    vertical_diagnostic.predicted_altitude_before_clamp_m
                    if vertical_diagnostic is not None else None),
                "predicted_altitude_m": (
                    prediction.predicted_altitude_m
                    if prediction is not None else None),
                "target_altitude_m": (
                    vertical_diagnostic.target_altitude_m
                    if vertical_diagnostic is not None else None),
                "intent_clamped": (
                    vertical_diagnostic.intent_clamped
                    if vertical_diagnostic is not None else False),
                "intent_reason": (
                    vertical_diagnostic.intent_reason
                    if vertical_diagnostic is not None else None),
            },
            "policy": _frozen_vertical_policy(policy),
        },
        "astronomy": {
            "body": celestial_body.upper(),
            "provider": "PyEphem",
            "provider_version": _ephem_provider_version(),
            "ephemeris_evaluated_at_utc": _snapshot_utc_text(
                solver_diagnostic.body_ephemeris_evaluated_at_utc
                if solver_diagnostic is not None else None),
            "altitude_deg": float(transit_result[9]),
            "azimuth_deg": float(transit_result[8]),
            "angular_diameter_arcsec": (
                solver_diagnostic.body_angular_diameter_arcsec
                if solver_diagnostic is not None else None),
        },
    }


def build_snapshot_solver_input(icao, plane_lat, plane_lon, elevation,
                                distance, azimuth, altitude_angle,
                                groundspeed, track):
    """Copy the exact horizontal inputs and corresponding motion metadata."""
    state = aircraft_motion_states.get(icao)
    position = state.position if state is not None else None
    altitude = state.altitude if state is not None else None
    snapshot_now_utc = clock.now_utc()
    track_parameter = effective_track_parameter(
        icao, track, snapshot_now_utc)
    groundspeed_parameter = state.groundspeed if state is not None else None
    vertical_rate = state.vertical_rate if state is not None else None
    intent = aircraft_intent_states.get(icao)
    selected_altitude = intent.selected_altitude if intent is not None else None
    raw_track = raw_adsb_tracks.get(icao)
    solver_input = {
        "aircraft_lat": float(plane_lat),
        "aircraft_lon": float(plane_lon),
        "aircraft_altitude_m": float(elevation),
        "aircraft_distance_km": float(distance),
        "aircraft_azimuth_deg": float(azimuth),
        "aircraft_altitude_angle_deg": (
            float(altitude_angle) if is_float_try(altitude_angle) else None),
        "groundspeed": float(groundspeed),
        "track": float(track),
        "track_source": (
            track_parameter.source if track_parameter is not None else None),
        "raw_track_timestamp_utc": _snapshot_utc_text(
            raw_track.raw_updated_at_utc if raw_track is not None else None),
        "track_coarse_anchor_deg": (
            raw_track.coarse_anchor_deg if raw_track is not None else None),
        "vertical_rate": (
            vertical_rate.value if vertical_rate is not None else None),
        "selected_altitude": (
            selected_altitude.value if selected_altitude is not None else None),
        "position_source": position.source if position is not None else None,
        "altitude_source": altitude.source if altitude is not None else None,
        "position_timestamp_utc": _snapshot_utc_text(
            position.updated_at_utc if position is not None else None),
        "altitude_timestamp_utc": _snapshot_utc_text(
            altitude.updated_at_utc if altitude is not None else None),
        "track_timestamp_utc": _snapshot_utc_text(
            track_parameter.updated_at_utc
            if track_parameter is not None else None),
        "groundspeed_timestamp_utc": _snapshot_utc_text(
            groundspeed_parameter.updated_at_utc
            if groundspeed_parameter is not None else None),
    }
    mlat_beast_snapshot = _snapshot_mlat_beast_track(
        icao, track_parameter, position, snapshot_now_utc)
    if mlat_beast_snapshot is not None:
        solver_input["mlat_beast_track"] = mlat_beast_snapshot
    return solver_input


def _capture_transit_observation(icao, timestamp_utc, message_source,
                                 message_type):
    """Copy the earliest accepted per-message state into the small ring buffer."""
    if transit_snapshot_manager is None:
        return
    state = aircraft_motion_states.get(icao)
    position = state.position if state is not None else None
    altitude, altitude_source, altitude_time = _snapshot_parameter(state, "altitude")
    groundspeed, groundspeed_source, groundspeed_time = _snapshot_parameter(state, "groundspeed")
    track, track_source, track_time = _snapshot_parameter(state, "track")
    vertical_rate, vertical_rate_source, vertical_rate_time = _snapshot_parameter(
        state, "vertical_rate")
    intent = aircraft_intent_states.get(icao)
    selected = intent.selected_altitude if intent is not None else None
    source_timestamps = {
        "position": position.updated_at_utc if position is not None else None,
        "altitude": altitude_time,
        "groundspeed": groundspeed_time,
        "track": track_time,
        "vertical_rate": vertical_rate_time,
        "selected_altitude": selected.updated_at_utc if selected is not None else None,
    }
    parameter_ages = {
        name: ((timestamp_utc - updated_at).total_seconds()
               if updated_at is not None else None)
        for name, updated_at in source_timestamps.items()
    }
    transit_snapshot_manager.record_observation({
        "timestamp_utc": timestamp_utc,
        "icao": icao,
        "message_source": message_source,
        "message_type": message_type,
        "lat": position.latitude if position is not None else None,
        "lon": position.longitude if position is not None else None,
        "altitude_m": altitude,
        "groundspeed": groundspeed,
        "track": track,
        "vertical_rate_fpm": vertical_rate,
        "selected_altitude_ft": selected.value if selected is not None else None,
        "parameter_sources": {
            "position": position.source if position is not None else None,
            "altitude": altitude_source,
            "groundspeed": groundspeed_source,
            "track": track_source,
            "vertical_rate": vertical_rate_source,
            "selected_altitude": selected.source if selected is not None else None,
        },
        "source_timestamps_utc": source_timestamps,
        "parameter_ages_seconds": parameter_ages,
    })


def capture_transit_observation(icao, timestamp_utc, message_source,
                                message_type):
    """Keep every TC29G failure outside ADS-B, MLAT and Beast paths."""
    try:
        _capture_transit_observation(
            icao, timestamp_utc, message_source, message_type)
    except Exception:
        pass


def moving_body_transit_pred(body_name, obs2body, plane_pos, track,
                             velocity, elevation, prediction_base_utc,
                             fallback_body_position=None):
    """Iteratively solve the existing geometry against a moving Sun/Moon."""
    geometry_args = (obs2body, plane_pos, track, velocity, elevation)
    try:
        body_position = body_position_at_utc(body_name, prediction_base_utc)
        body_alt, body_az = body_position
        body_size = _body_angular_diameter(body_position)
    except Exception:
        fallback_result = None
        if fallback_body_position is not None:
            fallback_alt, fallback_az = fallback_body_position
            fallback_result = transit_pred(
                *geometry_args, fallback_alt, fallback_az)
            fallback_time = _moving_body_result_time(fallback_result)
            if (fallback_time is None or fallback_time <= 0
                    or fallback_time > 900):
                fallback_result = None
        return _moving_body_solution(
            body_name, prediction_base_utc,
            _moving_body_result_time(fallback_result), fallback_result,
            0, None, TransitSolverOutcome.TECHNICAL_FALLBACK,
            _body_angular_diameter(fallback_body_position),
            _body_evaluated_at_utc(fallback_body_position))

    initial_result = transit_pred(*geometry_args, body_alt, body_az)
    if not initial_result:
        return _moving_body_solution(
            body_name, prediction_base_utc, None, None, 0, None,
            TransitSolverOutcome.NO_INTERSECTION)
    initial_time = _moving_body_result_time(initial_result)
    if initial_time is None or initial_time <= 0 or initial_time > 900:
        return _moving_body_solution(
            body_name, prediction_base_utc, initial_time, None, 0, None,
            TransitSolverOutcome.OUT_OF_RANGE)

    results = [(
        initial_result,
        body_size,
        _body_evaluated_at_utc(body_position, prediction_base_utc),
    )]
    current_time = initial_time
    for correction_count in range(1, MOVING_BODY_MAX_CORRECTIONS + 1):
        body_time = prediction_base_utc + datetime.timedelta(
            seconds=current_time)
        try:
            body_position = body_position_at_utc(body_name, body_time)
            body_alt, body_az = body_position
            body_size = _body_angular_diameter(body_position)
        except Exception:
            return _moving_body_solution(
                body_name, prediction_base_utc, initial_time,
                initial_result, correction_count - 1, None,
                TransitSolverOutcome.TECHNICAL_FALLBACK,
                results[0][1], results[0][2])

        next_result = transit_pred(*geometry_args, body_alt, body_az)
        if not next_result:
            return _moving_body_solution(
                body_name, prediction_base_utc, initial_time, None,
                correction_count, None,
                TransitSolverOutcome.NO_INTERSECTION)
        next_time = _moving_body_result_time(next_result)
        if next_time is None or next_time <= 0 or next_time > 900:
            return _moving_body_solution(
                body_name, prediction_base_utc, initial_time, None,
                correction_count, None,
                TransitSolverOutcome.OUT_OF_RANGE)

        residual = abs(next_time - current_time)
        if residual < MOVING_BODY_CONVERGENCE_SECONDS:
            return _moving_body_solution(
                body_name, prediction_base_utc, initial_time, next_result,
                correction_count, residual, TransitSolverOutcome.CONVERGED,
                body_size, _body_evaluated_at_utc(body_position, body_time))

        if (len(results) >= 2
                and abs(next_time - _moving_body_result_time(results[-2][0]))
                <= MOVING_BODY_CYCLE_TOLERANCE_SECONDS):
            cycle_results = (
                results[-1], (
                    next_result,
                    body_size,
                    _body_evaluated_at_utc(body_position, body_time),
                ))
            final_result, final_body_size, final_body_time = max(
                cycle_results,
                key=lambda pair: _moving_body_result_separation(pair[0]))
            return _moving_body_solution(
                body_name, prediction_base_utc, initial_time, final_result,
                correction_count, residual,
                TransitSolverOutcome.TWO_POINT_CYCLE, final_body_size,
                final_body_time)

        results.append((
            next_result,
            body_size,
            _body_evaluated_at_utc(body_position, body_time),
        ))
        current_time = next_time

    final_result, final_body_size, final_body_time = max(
        results[-2:],
        key=lambda pair: _moving_body_result_separation(pair[0]))
    return _moving_body_solution(
        body_name, prediction_base_utc, initial_time, final_result,
        MOVING_BODY_MAX_CORRECTIONS,
        abs(_moving_body_result_time(results[-1][0])
            - _moving_body_result_time(results[-2][0])),
        TransitSolverOutcome.MAX_ITERATIONS, final_body_size,
        final_body_time)

# Funkcje kolorowania odległości, wysokości, azymutu / Functions for coloring distance, altitude, azimuth
def dist_col(distance):
    if distance <= 300 and distance > 100:
        return PURPLE
    elif distance <= 100 and distance > 50:
        return CYAN
    elif distance <= 50 and distance > 30:
        return YELLOW
    elif distance <= 30 and distance > 15:
        return REDALERT
    elif distance <= 15 and distance > 0:
        return GREENALERT
    else:
        return PURPLEDARK

def alt_col(altitude):
    if altitude >= 5 and altitude < 15:
        return PURPLE
    elif altitude >= 15 and altitude < 25:
        return CYAN
    elif altitude >= 25 and altitude < 30:
        return YELLOW
    elif altitude >= 30 and altitude < 45:
        return REDALERT
    elif altitude >= 45 and altitude <= 90:
        return GREEN
    else:
        return PURPLEDARK

def elev_col(elevation):
    if elevation >= 4000 and elevation <= 8000:
        return PURPLE
    elif elevation >= 2000 and elevation < 4000:
        return GREEN
    elif elevation > 0 and elevation < 2000:
        return YELLOW
    else:
        return RESET

# Konwersja kierunku wiatru na string / Convert wind direction to string
def wind_deg_to_str1(deg):
    if deg >= 11.25 and deg < 33.75:
        return 'NNE'
    elif deg >= 33.75 and deg < 56.25:
        return 'NE'
    elif deg >= 56.25 and deg < 78.75:
        return 'ENE'
    elif deg >= 78.75 and deg < 101.25:
        return 'E'
    elif deg >= 101.25 and deg < 123.75:
        return 'ESE'
    elif deg >= 123.75 and deg < 146.25:
        return 'SE'
    elif deg >= 146.25 and deg < 168.75:
        return 'SSE'
    elif deg >= 168.75 and deg < 191.25:
        return 'S'
    elif deg >= 191.25 and deg < 213.75:
        return 'SSW'
    elif deg >= 213.75 and deg < 236.25:
        return 'SW'
    elif deg >= 236.25 and deg < 258.75:
        return 'WSW'
    elif deg >= 258.75 and deg < 281.25:
        return 'W'
    elif deg >= 281.25 and deg < 303.75:
        return 'WNW'
    elif deg >= 303.75 and deg < 326.25:
        return 'NW'
    elif deg >= 326.25 and deg < 348.75:
        return 'NNW'
    else:
        return 'N'

# Funkcja do generowania dźwięku ostrzegawczego / Function to generate a warning sound
def gong():
    global gong_t
    aktual_gong_t = clock.now_utc()
    diff_gong_t = (aktual_gong_t - gong_t).total_seconds()
    if diff_gong_t > 2:
        gong_t = aktual_gong_t
        sys.stdout.write('\a')  # TERMINAL GONG!
        sys.stdout.flush()

# Funkcje sprawdzające, czy wartość jest floatem lub intem / Functions to check if a value is float or int
def is_float_try(value):
    try:
        float(value)
        return True
    except ValueError:
        return False

def is_int_try(value):
    try:
        int(value)
        return True
    except ValueError:
        return False

# Funkcja do pobierania danych METAR / Function to retrieve METAR data
def get_metar_press():
    global metar_t
    global metar_attempt_t
    global pressure

    if isinstance(clock, ReplayClock):
        return pressure

    aktual_metar_t = clock.now_utc()
    if metar_t is not None and (aktual_metar_t - metar_t).total_seconds() < 900:
        return pressure
    if metar_attempt_t is not None and (aktual_metar_t - metar_attempt_t).total_seconds() < 60:
        return pressure
    metar_attempt_t = aktual_metar_t
    metar_data = fetch_awc_metar(metar_station)
    if metar_data is None:
        return pressure
    metar_age = (aktual_metar_t - metar_data.obs_time).total_seconds()
    if metar_age < 0 or metar_age > 90 * 60:
        return pressure
    pressure = metar_data.altim
    metar_t = aktual_metar_t
    if environment_recorder is not None:
        environment_recorder.record(EnvironmentEvent(
            version=1,
            time=aktual_metar_t,
            type="qnh",
            value_hpa=metar_data.altim,
            source="awc",
            station=metar_station,
            obs_time=metar_data.obs_time,
        ))
    if daily_environment_recorder is not None:
        daily_environment_recorder.record_qnh(EnvironmentEvent(
            version=1,
            time=aktual_metar_t,
            type="qnh",
            value_hpa=metar_data.altim,
            source="awc",
            station=metar_station,
            obs_time=metar_data.obs_time,
        ))
    return pressure

# Funkcja do generowania tabeli wyjściowej / Function to generate output table
@synchronized_plane_dict
def tabela(output=None, full=False, force=False):
    global last_t, sun_body_angular_diameter_arcsec
    global moon_body_angular_diameter_arcsec
    global sun_body_evaluated_at_utc, moon_body_evaluated_at_utc
    output = sys.stdout if output is None else output
    emit = lambda *args: print(*args, file=output)
    gatech.date = clock.ephem_now()  # Aktualizuj datę w ephemeris / Update date in ephemeris
    vm, vs = ephem.Moon(gatech), ephem.Sun(gatech)  # Pobierz dane o Księżycu i Słońcu / Get data about the Moon and the Sun
    vm.compute(gatech)  # Oblicz pozycję Księżyca / Compute Moon position
    vs.compute(gatech)  # Oblicz pozycję Słońca / Compute Sun position
    try:
        body_evaluated_at_utc = ephem.Date(gatech.date).datetime().replace(
            tzinfo=pytz.utc)
    except (TypeError, ValueError):
        body_evaluated_at_utc = clock.now_utc()
    moon_body_evaluated_at_utc = body_evaluated_at_utc
    sun_body_evaluated_at_utc = body_evaluated_at_utc
    moon_body_angular_diameter_arcsec = _ephem_angular_diameter(vm)
    sun_body_angular_diameter_arcsec = _ephem_angular_diameter(vs)
    moon_alt, moon_az = round(math.degrees(vm.alt), 1), round(math.degrees(vm.az), 1)  # Wysokość i azymut Księżyca / Moon altitude and azimuth
    sun_alt, sun_az = round(math.degrees(vs.alt), 1), round(math.degrees(vs.az), 1)  # Wysokość i azymut Słońca / Sun altitude and azimuth
    aktual_t = clock.now_utc()  # Aktualny czas w UTC / Current time in UTC
    diff_t = (aktual_t - last_t).total_seconds()  # Różnica czasu od ostatniego odświeżenia / Time difference from last refresh
    if force or diff_t > 1:
        if not force:
            last_t = aktual_t  # Ustaw ostatni czas odświeżenia / Set last refresh time
            clear_screen(output)  # Wyczyść ekran / Clear the screen
        emit("Flight info |  Actual parameters  |-- Pred. closest  --|--- Current Az/Alt ---|----- Transits: Sun", sun_az, sun_alt,'  & Moon', moon_az, moon_alt )
        emit('{:9} {:>6} {:>7} {} {:>6} {} {:>8} {} {:>7} {} {:>6} {:>6} {:>5} {} {:>7} {:>7} {:>7} {:>8} {} {:>7} {:>7} {:>7} {:>7} {} {:>5}'.format(\
        ' icao or', ' (m)', '(d)', '|', '(km)', '|', '(km)', '|', '(d)', '|', '(d)', '(d)', '(l)', ' |', '(d)', '(km)', '(km)', '   (s)', '|', '(d)', '(km)', '(km)', '   (s)', ' |', '(s)'))
        emit('{:9} {:>6} {:>7} {} {:>6} {} {:>8} {} {:>7} {} {:>6} {:>6} {:>5} {} {:>7} {:>7} {:>7} {:>8} {} {:>7} {:>7} {:>7} {:>7} {} {:>5}'.format(\
        ' flight', 'elev', 'trck', '|', 'dist', '|', '[warn]','|', '[Alt]', '|', 'Alt', 'Azim', 'Azim', ' |', 'Sep', 'p2x', 'h2x', 'time2X', '|', 'Sep', 'p2x', 'h2x', 'time2X', ' |', 'age'))
        emit("-------------------------|--------|--------- |---------|----------------------|----------------------------------|----------------------------------|------------------|")

        render_plan = build_terminal_render_plan(
            plane_dict,
            len(plane_dict) if full else terminal_aircraft_row_limit(),
            None if full else warning_distance,
            aktual_t)
        for pentry in render_plan.aircraft_ids:
            try:
                distance = float(plane_dict[pentry][5])
            except (TypeError, ValueError):
                distance = None

            if full or (distance is not None and distance <= warning_distance):
                then = plane_dict[pentry][17] if plane_dict[pentry][17] else clock.now_utc()
                diff_seconds = (clock.now_utc() - then).total_seconds()
                diff_minutes = (clock.now_utc() - plane_dict[pentry][0]).total_seconds() / 60

                if plane_dict[pentry][1]:
                    wiersz = '{}{:<9}{}'.format(YELLOW, plane_dict[pentry][1], RESET)
                else:
                    wiersz = '{}{:<9}{}'.format(RESET, pentry, RESET)

                has_elevation = is_float_try(plane_dict[pentry][4])
                elevation = int(plane_dict[pentry][4]) if has_elevation else None
                if has_elevation:
                    wiersz += '{}{:>7}{} '.format(
                        elev_col(elevation), elevation, RESET)
                else:
                    wiersz += '{:>7} '.format('---')
                displayed_track = format_track_for_display(
                    pentry, plane_dict[pentry][11], aktual_t)
                wiersz += '{:>7} | '.format(displayed_track)

                if distance is not None:
                    wiersz += '{}{:>6.1f}{} | '.format(
                        dist_col(distance), distance, RESET)
                else:
                    wiersz += '{:>6} | '.format('---')

                try:
                    warn_val = float(plane_dict[pentry][13])
                except (TypeError, ValueError):
                    warn_val = 0.0  # Default value if conversion fails

                if plane_dict[pentry][12] == 'WARNING' and plane_dict[pentry][9] != "RECEDING":
                    wiersz += '[{}{:>7.1f}{}]'.format(REDALERT, warn_val, RESET)
                elif plane_dict[pentry][12] == 'WARNING' and plane_dict[pentry][9] == "RECEDING":
                    wiersz += '[{}{:>7.1f}{}]'.format(RED, warn_val, RESET)
                elif plane_dict[pentry][12] != 'WARNING' and plane_dict[pentry][9] == "RECEDING":
                    wiersz += '[{}{:>7.1f}{}]'.format(PURPLEDARK, warn_val, RESET)
                else:
                    wiersz += '[{}{:>7.1f}{}]'.format(PURPLE, warn_val, RESET)

                if has_elevation and is_float_try(plane_dict[pentry][13]):
                    altitudeX = round(degrees(atan((elevation - my_elevation_const) / (float(plane_dict[pentry][13]) * 1000))), 1) if plane_dict[pentry][13] else 0
                else:
                    altitudeX = None

                if altitudeX is not None:
                    wiersz += '[{}{:>7.1f}{}] | '.format(
                        alt_col(altitudeX), altitudeX, RESET)
                else:
                    wiersz += '[{:>7}] | '.format('---')
                if is_float_try(plane_dict[pentry][7]):
                    current_altitude = float(plane_dict[pentry][7])
                    wiersz += '{}{:>6.1f}{}'.format(
                        alt_col(current_altitude), current_altitude, RESET)
                else:
                    wiersz += '{:>6}'.format('---')

                if diff_seconds >= 999:
                    wiersz += '{}x{}'.format(RED, RESET)
                elif diff_seconds > 30:
                    wiersz += '{}!{}'.format(RED, RESET)
                elif diff_seconds > 15:
                    wiersz += '{}!{}'.format(YELLOW, RESET)
                elif diff_seconds > 10:
                    wiersz += '{}!{}'.format(GREENFG, RESET)
                else:
                    wiersz += '{}o{}'.format(GREENFG, RESET)

                if is_float_try(plane_dict[pentry][6]):
                    current_azimuth = float(plane_dict[pentry][6])
                    wiersz += '{:>6.1f} '.format(current_azimuth)
                    wiersz += '{:>6} | '.format(
                        wind_deg_to_str1(current_azimuth))
                else:
                    wiersz += '{:>6} {:>6} | '.format('---', '---')

                diff_secx = (clock.now_utc() - plane_dict[pentry][0]).total_seconds()
                sun_values = visible_transit_candidate(
                    plane_dict[pentry], "sun", pentry, aktual_t)

                if (sun_values is not None
                        and sun_values[0] < transit_separation_GREENALERT_FG):
                    separation_deg = sun_values[0]
                    wiersz += '{}{:>7.2f}{} {:>7.1f} {:>7.1f} {:>8.1f}'.format(GREENALERT, separation_deg, RESET, *sun_values[1:])
                elif (sun_values is not None
                      and sun_values[0] < transit_separation_REDALERT_FG):
                    separation_deg = sun_values[0]
                    wiersz += '{}{:>7.2f}{} {:>7.1f} {:>7.1f} {:>8.1f}'.format(REDALERT, separation_deg, RESET, *sun_values[1:])
                elif sun_values is not None:
                    separation_deg = sun_values[0]
                    wiersz += '{}{:>7.2f}{} {:>7.1f} {:>7.1f} {:>8.1f}'.format(RED, separation_deg, RESET, *sun_values[1:])
                else:
                    wiersz += '{:>7} {:>7} {:>7} {:>8}'.format('---', '---', '---', '---')

                wiersz += ' | '

                moon_values = visible_transit_candidate(
                    plane_dict[pentry], "moon", pentry, aktual_t)

                if (moon_values is not None
                        and moon_values[0] < transit_separation_GREENALERT_FG):
                    separation_deg2 = moon_values[0]
                    wiersz += '{}{:>7.2f}{} {:>7.1f} {:>7.1f} {:>8.1f}'.format(GREENALERT, separation_deg2, RESET, *moon_values[1:])
                elif (moon_values is not None
                      and moon_values[0] < transit_separation_REDALERT_FG):
                    separation_deg2 = moon_values[0]
                    wiersz += '{}{:>7.2f}{} {:>7.1f} {:>7.1f} {:>8.1f}'.format(REDALERT, separation_deg2, RESET, *moon_values[1:])
                elif moon_values is not None:
                    separation_deg2 = moon_values[0]
                    wiersz += '{}{:>7.2f}{} {:>7.1f} {:>7.1f} {:>8.1f}'.format(RED, separation_deg2, RESET, *moon_values[1:])
                else:
                    wiersz += '{:>7} {:>7} {:>7} {:>8}'.format('---', '---', '---', '---')

                wiersz += ' | '
                wiersz += '{:>5.1f}'.format(diff_secx)
                wiersz += ' {} {} '.format(len(plane_dict[pentry][15]), len(plane_dict[pentry][16]))
                wiersz += '{:>5.1f}'.format(diff_seconds)
                emit(wiersz)

        emit(" ")
        emit("{} (UTC) --- delay < {:.1f}s --- QNH {}hPa".format(clock.now_utc().time(), diff_t, pressure))
        emit(terminal_tracking_summary(my_lat, my_lon, render_plan))
        # Print combined port and recorder statuses.
        for status_line in source_status_lines():
            emit(status_line)

    return sun_alt, sun_az, moon_alt, moon_az


def request_table_snapshot(signum=None, frame=None):
    """Signal handler: defer all rendering and I/O to the main loop."""
    table_snapshot_requested.set()


def install_table_snapshot_signal_handler():
    if not hasattr(signal, "SIGUSR1"):
        return False
    signal.signal(signal.SIGUSR1, request_table_snapshot)
    return True


def render_full_table_snapshot():
    output = io.StringIO()
    tabela(output=output, full=True, force=True)
    return ANSI_ESCAPE_RE.sub("", output.getvalue())


def write_table_snapshot(directory=DIAGNOSTICS_DIRECTORY):
    now = clock.now_utc()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / now.strftime(
        "table_snapshot_%Y%m%d_%H%M%S_UTC.txt")
    path.write_text(render_full_table_snapshot(), encoding="utf-8")
    return path


def process_table_snapshot_request(directory=DIAGNOSTICS_DIRECTORY):
    if not table_snapshot_requested.is_set():
        return None
    table_snapshot_requested.clear()
    try:
        path = write_table_snapshot(directory)
    except Exception as error:
        print("Table snapshot: FAILED ({})".format(error))
        return None
    print("Table snapshot: {}".format(path))
    return path


# Funkcja do czyszczenia słownika tranzytów / Function to clean the transit dictionary
@synchronized_plane_dict
def clean_transit_dict():
    current_time = clock.now_utc()
    to_delete = [icao for icao, entry in plane_dict.items() if len(entry) > 31 and entry[31] and isinstance(entry[30], datetime.datetime) and (current_time - entry[30]).total_seconds() > 120]
    for icao in to_delete:
        del plane_dict[icao]
        altitude_sources.pop(icao, None)
        aircraft_motion_states.pop(icao, None)
        raw_adsb_tracks.pop(icao, None)
        mlat_beast_tracks.pop(icao, None)
        mlat_coarse_tracks.pop(icao, None)
        aircraft_intent_states.pop(icao, None)
        aircraft_motion_freshness_status.pop(icao, None)
        sun_prediction_last_valid.pop(icao, None)
        moon_prediction_last_valid.pop(icao, None)
        sun_predicted_transit_utc.pop(icao, None)
        moon_predicted_transit_utc.pop(icao, None)
        transit_solver_diagnostics.pop((icao, "sun"), None)
        transit_solver_diagnostics.pop((icao, "moon"), None)
        vertical_transit_diagnostics.pop((icao, "sun"), None)
        vertical_transit_diagnostics.pop((icao, "moon"), None)
        drop_transit_snapshot_buffer(icao)

# Function to manage sockets blocked in readline() during controlled shutdown.
def _register_active_socket(port, sock):
    with active_sockets_lock:
        if stop_event.is_set():
            return False
        active_sockets[port] = sock
        return True


def _unregister_active_socket(port, sock):
    with active_sockets_lock:
        if active_sockets.get(port) is sock:
            del active_sockets[port]


def close_active_sockets():
    with active_sockets_lock:
        sockets = list(active_sockets.values())
        active_sockets.clear()
    for sock in sockets:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


def shutdown_runtime(threads, recorder):
    global shutdown_complete
    with shutdown_lock:
        if shutdown_complete:
            return
        shutdown_complete = True
        stop_event.set()
    close_active_sockets()
    for thread in threads:
        try:
            thread.join(timeout=2.0)
        except Exception:
            pass
    close_transit_snapshots(clock.now_utc())
    if recorder is not None:
        try:
            recorder.close(clock.now_utc())
        except Exception as error:
            print("Session recorder shutdown failed: {}".format(error))
            return
        try:
            delete_raw = (
                recorder.manifest_data().get("recording_status") == "complete")
        except Exception:
            delete_raw = False
        archive_errors = []
        try:
            archived = archive_session(
                recorder.session_dir,
                delete_raw=delete_raw,
                error_handler=archive_errors.append,
            )
        except Exception as error:
            archived = False
            archive_errors.append(str(error))
        if archived:
            print("Session archive: OK")
        else:
            detail = archive_errors[0] if archive_errors else "unknown error"
            print("Session archive: FAILED ({})".format(detail))


# Funkcja do czytania danych z portu / Function to read data from port
def read_from_port(host, port, process_line, session_recorder=None):
    global port_status
    while not stop_event.is_set():
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if not _register_active_socket(port, sock):
                sock.close()
                break
            sock.connect((host, port))
            port_status[port] = True
            file = sock.makefile()
            while not stop_event.is_set():
                line = file.readline()
                if not line:
                    break
                if session_recorder is not None:
                    try:
                        session_recorder.record_line(port, line)
                    except Exception as error:
                        print("Session recorder error on port {}: {}".format(port, error))
                process_line(line.strip(), port)
        except Exception as e:
            if stop_event.is_set():
                break
            print("Error on port {}: {}".format(port, e))
            port_status[port] = False
            if stop_event.wait(5):
                break
        finally:
            if sock is not None:
                _unregister_active_socket(port, sock)
                try:
                    sock.close()
                except Exception:
                    pass
    port_status[port] = False


def read_beast_intent(host, port):
    """Consume live Beast data for TC29 enrichment; failures are fail-open."""
    while not stop_event.is_set():
        sock = None
        try:
            parser = BeastFrameParser()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if not _register_active_socket(port, sock):
                sock.close()
                break
            sock.connect((host, port))
            beast_intent_diagnostics.reconnects += 1
            while not stop_event.is_set():
                chunk = sock.recv(65536)
                if not chunk:
                    break
                for frame in parser.feed(chunk):
                    beast_intent_diagnostics.frames_received += 1
                    if len(frame.modes) == 14 and modes_crc(frame.modes) != 0:
                        beast_intent_diagnostics.invalid_crc_frames += 1
                        continue
                    intent = decode_tc29(frame)
                    if intent is not None:
                        update_aircraft_intent(intent, clock.now_utc())
                        beast_intent_diagnostics.tc29_updates += 1
                beast_intent_diagnostics.resync_count = parser.resync_count
        except Exception as error:
            if stop_event.is_set():
                break
            beast_intent_diagnostics.last_error = str(error)
            beast_intent_diagnostics.last_error_utc = clock.now_utc()
            if stop_event.wait(5):
                break
        finally:
            if sock is not None:
                _unregister_active_socket(port, sock)
                try:
                    sock.close()
                except Exception:
                    pass


def read_raw_adsb_track(host, port, session_recorder=None):
    """Consume optional RAW port 30002 TC19 tracks; failures are fail-open."""
    while not stop_event.is_set():
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if not _register_active_socket(port, sock):
                sock.close()
                break
            sock.connect((host, port))
            raw_adsb_track_diagnostics.reconnects += 1
            file = sock.makefile()
            while not stop_event.is_set():
                line = file.readline()
                if not line:
                    break
                if session_recorder is not None:
                    try:
                        session_recorder.record_line(port, line)
                    except Exception:
                        pass
                raw_adsb_track_diagnostics.frames_received += 1
                decoded = decode_raw_tc19_track(line)
                if decoded is None:
                    raw_adsb_track_diagnostics.rejected_frames += 1
                    continue
                update_raw_adsb_track(decoded, clock.now_utc())
                raw_adsb_track_diagnostics.valid_track_updates += 1
            if not stop_event.is_set() and stop_event.wait(5):
                break
        except Exception as error:
            if stop_event.is_set():
                break
            raw_adsb_track_diagnostics.last_error = str(error)
            raw_adsb_track_diagnostics.last_error_utc = clock.now_utc()
            if stop_event.wait(5):
                break
        finally:
            if sock is not None:
                _unregister_active_socket(port, sock)
                try:
                    sock.close()
                except Exception:
                    pass


def read_mlat_beast_track(host, port):
    """Consume optional synthetic MLAT Beast TC19; failures are fail-open."""
    while not stop_event.is_set():
        sock = None
        try:
            parser = BeastFrameParser()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if not _register_active_socket(port, sock):
                sock.close()
                break
            sock.connect((host, port))
            mlat_beast_track_diagnostics.reconnects += 1
            while not stop_event.is_set():
                chunk = sock.recv(65536)
                if not chunk:
                    break
                for frame in parser.feed(chunk):
                    mlat_beast_track_diagnostics.frames_received += 1
                    decoded = decode_mlat_beast_tc19(frame)
                    if decoded is None:
                        mlat_beast_track_diagnostics.rejected_frames += 1
                        continue
                    update_mlat_beast_track(decoded, clock.now_utc())
                    mlat_beast_track_diagnostics.valid_track_updates += 1
                mlat_beast_track_diagnostics.resync_count = parser.resync_count
            if not stop_event.is_set() and stop_event.wait(5):
                break
        except Exception as error:
            if stop_event.is_set():
                break
            mlat_beast_track_diagnostics.last_error = str(error)
            mlat_beast_track_diagnostics.last_error_utc = clock.now_utc()
            if stop_event.wait(5):
                break
        finally:
            if sock is not None:
                _unregister_active_socket(port, sock)
                try:
                    sock.close()
                except Exception:
                    pass


# Funkcja do przetwarzania linii danych / Function to process a line of data
@synchronized_plane_dict
def process_line(line, port):
    global last_update_time, moon_alt, moon_az, sun_alt, sun_az, gatech
    global moon_body_angular_diameter_arcsec, sun_body_angular_diameter_arcsec
    global moon_body_evaluated_at_utc, sun_body_evaluated_at_utc

    if not line:
        return

    parts = line.split(",")
    if len(parts) < 2:
        return
    a_m_type = parts[0].strip()
    mtype = parts[1].strip()
    icao = re.sub(r'\W+', '', parts[4].strip())  # Usunięcie znaków specjalnych z kodu icao / Remove special characters from icao code
    date = parts[6].strip()
    time = parts[7].strip()
    logged_date = parts[8].strip()
    logged_time = parts[9].strip()

    # Konwersja daty i czasu na UTC / Convert date and time to UTC
    try:
        date_time = datetime.datetime.strptime(date + " " + time, '%Y/%m/%d %H:%M:%S.%f')
    except ValueError:
        print("Error parsing date and time: {} {}".format(date, time))
        return

    try:
        logged_date_time = datetime.datetime.strptime(logged_date + " " + logged_time, '%Y/%m/%d %H:%M:%S.%f')
    except ValueError:
        if isinstance(clock, ReplayClock):
            print("Error parsing logged date and time: {} {}".format(logged_date, logged_time))
            return
        logged_date_time = None

    date_time_utc = port_timestamp_to_utc(
        date_time, port, adsb_timestamp_timezone, adsb_port)
    logged_date_time_utc = (
        port_timestamp_to_utc(logged_date_time, port, adsb_timestamp_timezone, adsb_port)
        if logged_date_time else None
    )
    if (port == adsb_port and adsb_timestamp_validator is not None
            and not isinstance(clock, ReplayClock)):
        adsb_timestamp_validator.observe(date_time, clock.now_utc())

    if logged_date_time_utc is not None:
        advance_replay_time(logged_date_time_utc)
        if isinstance(clock, ReplayClock):
            apply_replay_environment(clock.now_utc())

    if mtype == "1":
        flight = parts[10].strip()
        if icao not in plane_dict:
            plane_dict[icao] = [date_time_utc, flight, "", "", "", "", "", "", "", "", "", "", "", "", "", [], [], "", "", "", "", "", "", "", "", "", "", "", "", "", None, False]
        else:
            plane_dict[icao][0] = date_time_utc
            plane_dict[icao][1] = flight
            last_update_time = clock.now_utc()

    if mtype == "5":
        flight = parts[10].strip()
        elevation = parts[11].strip()
        if is_int_try(elevation):
            altitude_baro_ft = int(elevation)
            pressure = get_metar_press()
            corrected_altitude_ft = correct_pressure_altitude(
                altitude_baro_ft, pressure)
            corrected_altitude_m = float(corrected_altitude_ft * 0.3048)
            _record_altitude_measurement(
                icao, port, altitude_baro_ft, corrected_altitude_m,
                date_time_utc, mtype)
            _update_motion_parameter(
                icao, "altitude", corrected_altitude_m,
                date_time_utc, port)
            if metric_units:
                elevation = corrected_altitude_m
            else:
                elevation = ""
        if icao not in plane_dict:
            plane_dict[icao] = [date_time_utc, flight, "", "", elevation, "", "", "", "", "", "", "", "", "", "", [], [], "", "", "", "", "", "", "", "", "", "", "", "", "", None, False]
        else:
            plane_dict[icao][4] = elevation
            plane_dict[icao][0] = date_time_utc
            last_update_time = clock.now_utc()
            if flight != '':
                plane_dict[icao][1] = flight

    if mtype == "4" or (mtype == "3" and a_m_type == "MLAT"):
        reported_velocity = parts[12].strip()
        track = parts[13].strip() if len(parts) > 13 else ''
        reported_vertical_rate = parts[16].strip() if len(parts) > 16 else ''
        if is_int_try(reported_velocity):
            velocity = round(int(reported_velocity) * 1.852)
            _update_motion_parameter(
                icao, "groundspeed", velocity, date_time_utc, port)
        else:
            velocity = 900
        if is_float_try(track):
            _update_motion_parameter(
                icao, "track", track, date_time_utc, port)
        if is_float_try(reported_vertical_rate):
            _update_motion_parameter(
                icao, "vertical_rate", reported_vertical_rate,
                date_time_utc, port)
        if icao not in plane_dict:
            plane_dict[icao] = [date_time_utc, "", "", "", "", "", "", "", "", "", "", track, "", "", velocity, [], [], "", "", "", "", "", "", "", "", "", "", "", "", "", None, False]
        else:
            plane_dict[icao][0] = date_time_utc
            if track:  # Aktualizuj track tylko, jeśli nie jest pusty / Update track only if not empty
                plane_dict[icao][11] = track
            plane_dict[icao][14] = velocity
            last_update_time = clock.now_utc()

    if mtype == "3":
        reported_elevation = parts[11].strip()
        track_index = 13 if a_m_type == "MLAT" else 12
        track = parts[track_index].strip() if len(parts) > track_index else ''
        elevation = None
        if is_int_try(reported_elevation):
            altitude_baro_ft = int(reported_elevation)
            pressure = get_metar_press()
            corrected_altitude_ft = correct_pressure_altitude(
                altitude_baro_ft, pressure)
            corrected_altitude_m = float(corrected_altitude_ft * 0.3048)
            _record_altitude_measurement(
                icao, port, altitude_baro_ft, corrected_altitude_m,
                date_time_utc, mtype)
            _update_motion_parameter(
                icao, "altitude", corrected_altitude_m,
                date_time_utc, port)
            if metric_units:
                elevation = corrected_altitude_m
        elif icao in plane_dict and is_float_try(plane_dict[icao][4]):
            elevation = float(plane_dict[icao][4])
        try:
            plane_lat = float(parts[14])
        except ValueError:
            plane_lat = 0.0
        try:
            plane_lon = float(parts[15])
        except ValueError:
            plane_lon = 0.0
        if plane_lat and plane_lon:
            _update_motion_position(
                icao, plane_lat, plane_lon, date_time_utc, port)
            distance = round(haversine((my_lat, my_lon), (plane_lat, plane_lon)), 1)
            if distance == 0:
                distance = 0.01
            angular_position = (
                angular_position_from_observer(
                    (my_lat, my_lon), my_elevation_const,
                    (plane_lat, plane_lon), elevation,
                    distance_km=distance)
                if elevation is not None else None)
            if angular_position is None:
                azimuth = angular_position_from_observer(
                    (my_lat, my_lon), my_elevation_const,
                    (plane_lat, plane_lon), my_elevation_const,
                    distance_km=distance).azimuth_deg
                altitude = ""
            else:
                azimuth = angular_position.azimuth_deg
                altitude = round(angular_position.altitude_angle_deg, 1)
            if icao not in plane_dict:
                plane_dict[icao] = [date_time_utc, "", plane_lat, plane_lon, elevation if elevation is not None else "", distance, azimuth, altitude, "", "", distance, track, "", "", "", [], [], "", "", "", "", "", "", "", "", "", "", "", "", "", None, False]
                plane_dict[icao][15] = []
                plane_dict[icao][16] = []
                if altitude != "":
                    plane_dict[icao][15].append(azimuth)
                    plane_dict[icao][16].append(altitude)
                last_update_time = clock.now_utc()
            else:
                min_distance = plane_dict[icao][10]
                try:
                    min_distance = float(min_distance)
                except ValueError:
                    min_distance = float('inf')
                if distance < min_distance:
                    plane_dict[icao][9] = "APPROACHING"
                    plane_dict[icao][10] = distance
                elif distance > min_distance:
                    plane_dict[icao][9] = "RECEDING"
                else:
                    plane_dict[icao][9] = "HOLDING"
                plane_dict[icao][0] = date_time_utc
                plane_dict[icao][2] = plane_lat
                plane_dict[icao][3] = plane_lon
                if elevation is not None:
                    plane_dict[icao][4] = elevation
                plane_dict[icao][5] = distance
                plane_dict[icao][6] = azimuth
                if altitude != "":
                    plane_dict[icao][7] = altitude
                if track:  # Aktualizuj track tylko, jeśli nie jest pusty / Update track only if not empty
                    plane_dict[icao][11] = track
                last_update_time = clock.now_utc()
                if not plane_dict[icao][17]:
                    plane_dict[icao][17] = date_time_utc
                then = plane_dict[icao][17]
                now = clock.now_utc()
                diff_seconds = (now - then).total_seconds()
                if diff_seconds > 6:
                    plane_dict[icao][17] = date_time_utc
                    poz_az = str(plane_dict[icao][6])
                    poz_alt = str(plane_dict[icao][7])
                    if altitude != "":
                        plane_dict[icao][15].append(poz_az)
                        plane_dict[icao][16].append(poz_alt)

    if icao:
        capture_transit_observation(
            icao, date_time_utc, a_m_type,
            "{},{}".format(a_m_type, mtype))

    motion_freshness = None
    if mtype in ["3", "4"]:
        motion_now = clock.now_utc()
        motion_freshness = assess_motion_freshness(
            effective_motion_state(icao, motion_now), motion_now)
        aircraft_motion_freshness_status[icao] = motion_freshness

    if (mtype in ["3", "4"] and (
            icao in plane_dict and plane_dict[icao][2]
            and plane_dict[icao][11] and is_float_try(plane_dict[icao][4]))):
        flight = plane_dict[icao][1]
        plane_lat = plane_dict[icao][2]
        plane_lon = plane_dict[icao][3]
        elevation = plane_dict[icao][4]
        distance = plane_dict[icao][5]
        azimuth = plane_dict[icao][6]
        altitude = plane_dict[icao][7]
        effective_track = effective_track_parameter(
            icao, plane_dict[icao][11], clock.now_utc())
        track = effective_track.value if effective_track is not None else 0.0
        warning = plane_dict[icao][12]
        direction = plane_dict[icao][9]
        velocity = plane_dict[icao][14]
        xtd = crosstrack(distance, (180 + float(azimuth)) % 360, track)
        plane_dict[icao][13] = xtd
        if xtd <= xtd_tst and distance < warning_distance and warning == "" and direction != "RECEDING":
            plane_dict[icao][12] = "WARNING"
            plane_dict[icao][13] = xtd
            gong()
        if xtd > xtd_tst and distance < warning_distance and warning == "WARNING" and direction != "RECEDING":
            plane_dict[icao][12] = ""
            plane_dict[icao][13] = xtd
            gong()
        if not plane_dict[icao][8]:
            plane_dict[icao][8] = "LINKED!"
        if distance <= alert_distance and plane_dict[icao][8] != "ENTERING":
            plane_dict[icao][8] = "ENTERING"
            gong()
        if distance > alert_distance and plane_dict[icao][8] == "ENTERING":
            plane_dict[icao][8] = "LEAVING"
        if motion_freshness.status == MotionFreshnessStatus.STALE:
            sun_alt, sun_az, moon_alt, moon_az = tabela()
            clean_dict()
            clean_transit_dict()
            return
        snapshot_solver_input = None
        if transit_snapshot_manager is not None:
            try:
                snapshot_solver_input = build_snapshot_solver_input(
                    icao, plane_lat, plane_lon, elevation, distance, azimuth,
                    altitude, velocity, track)
            except Exception:
                pass
        prediction_base_utc = clock.now_utc()
        moon_solution = moving_body_transit_pred(
            "moon", (my_lat, my_lon), (plane_lat, plane_lon), track,
            velocity, elevation, prediction_base_utc,
            fallback_body_position=BodyPosition(
                moon_alt, moon_az, moon_body_angular_diameter_arcsec,
                moon_body_evaluated_at_utc))
        sun_solution = moving_body_transit_pred(
            "sun", (my_lat, my_lon), (plane_lat, plane_lon), track,
            velocity, elevation, prediction_base_utc,
            fallback_body_position=BodyPosition(
                sun_alt, sun_az, sun_body_angular_diameter_arcsec,
                sun_body_evaluated_at_utc))
        tst_int1 = _store_transit_solver_solution(
            icao, "moon", moon_solution)
        tst_int2 = _store_transit_solver_solution(
            icao, "sun", sun_solution)
        prediction_now = prediction_base_utc
        tst_int1 = apply_vertical_prediction_to_transit_result(
            icao, "moon", tst_int1, elevation, prediction_now)
        tst_int2 = apply_vertical_prediction_to_transit_result(
            icao, "sun", tst_int2, elevation, prediction_now)
        if tst_int1:
            alt_a = round(tst_int1[3], 2)
            dst_h2x = round(tst_int1[4], 2)
            dst_p2x = round(tst_int1[5], 2)
            final_time2x = float(tst_int1[6])
            delta_time = int(final_time2x)
            if 0 <= delta_time <= 900:  # Ignore past or excessively distant transits
                plane_dict[icao][25] = dst_h2x
                plane_dict[icao][23] = float(tst_int1[9])
                plane_dict[icao][24] = alt_a
                plane_dict[icao][26] = delta_time
                plane_dict[icao][27] = dst_p2x
                separation_deg = vertical_transit_separation(
                    plane_dict[icao][24], plane_dict[icao][23])
                if -transit_separation_sound_alert < separation_deg < transit_separation_sound_alert:
                    gong()
                if delta_time <= 2:  # Ustaw flagę tranzytu jeśli czas do tranzytu jest mniejszy lub równy 2 sekundy / Set transit flag if time to transit is less than or equal to 2 second
                    plane_dict[icao][31] = True
                    plane_dict[icao][30] = clock.now_utc()  # Ustaw czas rozpoczęcia tranzytu / Set transit start time
                plane_dict[icao][29] = clock.now_utc()
                update_transit_prediction_timestamp(
                    icao, "moon", prediction_now, final_time2x)
                capture_transit_prediction(
                    icao, flight, "moon", tst_int1, prediction_now,
                    snapshot_solver_input)
            else:
                clear_transit_prediction_state(
                    icao, plane_dict[icao], "moon", 23)
        else:
            if moon_alt < 0.1:
                clear_transit_prediction_state(
                    icao, plane_dict[icao], "moon", 23)
            else:
                expire_transit_prediction_after_grace(
                    icao, plane_dict[icao], "moon", 23, prediction_now)
        if tst_int2:
            alt_a = round(tst_int2[3], 2)
            dst_h2x = round(tst_int2[4], 2)
            dst_p2x = round(tst_int2[5], 2)
            final_time2x = float(tst_int2[6])
            delta_time = int(final_time2x)
            if 0 <= delta_time <= 900:  # Ignore past or excessively distant transits
                plane_dict[icao][20] = dst_h2x
                plane_dict[icao][18] = float(tst_int2[9])
                plane_dict[icao][19] = alt_a
                plane_dict[icao][22] = delta_time
                plane_dict[icao][21] = dst_p2x
                separation_deg2 = vertical_transit_separation(
                    plane_dict[icao][19], plane_dict[icao][18])
                if -transit_separation_sound_alert < separation_deg2 < transit_separation_sound_alert:
                    gong()
                if delta_time <= 2:  # Ustaw flagę tranzytu jeśli czas do tranzytu jest mniejszy lub równy 2 sekundy / Set transit flag if time to transit is less than or equal to 2 second
                    plane_dict[icao][31] = True
                    plane_dict[icao][30] = clock.now_utc()  # Ustaw czas rozpoczęcia tranzytu / Set transit start time
                plane_dict[icao][30] = clock.now_utc()
                update_transit_prediction_timestamp(
                    icao, "sun", prediction_now, final_time2x)
                capture_transit_prediction(
                    icao, flight, "sun", tst_int2, prediction_now,
                    snapshot_solver_input)
            else:
                clear_transit_prediction_state(
                    icao, plane_dict[icao], "sun", 18)
        else:
            if sun_alt < 0.1:
                clear_transit_prediction_state(
                    icao, plane_dict[icao], "sun", 18)
            else:
                expire_transit_prediction_after_grace(
                    icao, plane_dict[icao], "sun", 18, prediction_now)
    sun_alt, sun_az, moon_alt, moon_az = tabela()
    clean_dict()
    clean_transit_dict()


def main():
    global daily_environment_recorder, session_recorder, session_recording_requested
    global transit_snapshot_manager
    global shutdown_complete
    try:
        configuration = load_installation_config()
    except ConfigurationError as error:
        raise SystemExit(str(error))
    apply_installation_config(configuration)
    install_table_snapshot_signal_handler()
    stop_event.clear()
    with shutdown_lock:
        shutdown_complete = False
    session_recorder = None
    session_recording_requested = runtime_args.record
    try:
        configure_environment_replay(runtime_args.environment_replay)
        if isinstance(clock, ReplayClock):
            daily_environment_recorder = None
            transit_snapshot_manager = None
            configure_environment_recording(None)
        else:
            initialize_transit_snapshots()
            initialize_daily_environment()
            configure_environment_recording(runtime_args.environment_record)
            get_metar_press()
    except (OSError, EnvironmentFormatError, EnvironmentRecordError) as error:
        raise SystemExit("Invalid environment file: {}".format(error))

    if session_recording_requested:
        try:
            session_recorder = SessionRecorder(
                clock.now_utc(), adsb_port, mlat_port, adsb_timestamp_timezone,
                error_handler=lambda message: print(message),
                raw_port=raw_adsb_port,
            )
        except Exception as error:
            print("Session recorder initialization failed: {}".format(error))
            session_recorder = None

    # Uruchomienie wątków do czytania z portów / Start threads to read from ports
    threads = [threading.Thread(
        target=read_from_port,
        args=(adsb_host, adsb_port, process_line, session_recorder),
    ), threading.Thread(
        target=read_from_port,
        args=(mlat_host, mlat_port, process_line, session_recorder),
    )]
    if not isinstance(clock, ReplayClock):
        threads.append(threading.Thread(
            target=read_beast_intent,
            args=(beast_host, beast_port),
        ))
        threads.append(threading.Thread(
            target=read_raw_adsb_track,
            args=(raw_adsb_host, raw_adsb_port, session_recorder),
        ))
        if mlat_beast_enabled:
            threads.append(threading.Thread(
                target=read_mlat_beast_track,
                args=(mlat_beast_host, mlat_beast_port),
            ))
    for thread in threads:
        thread.start()

    # Pętla główna / Main loop
    try:
        while True:
            time.sleep(1)
            process_table_snapshot_request()
            if daily_environment_recorder is not None:
                daily_environment_recorder.rotate_if_needed(clock.now_utc())
            if session_recorder is not None:
                session_recorder.flush_if_due()
            finalize_transit_snapshots(clock.now_utc())
            if replay_time_initialized:
                sun_alt, sun_az, moon_alt, moon_az = tabela()
                clean_dict()
                clean_transit_dict()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_runtime(threads, session_recorder)


if __name__ == "__main__":
    main()

