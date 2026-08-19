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
from dataclasses import dataclass
from functools import wraps
from math import atan2, sin, cos, acos, radians, degrees, atan, asin, sqrt, isnan
import pytz  # Import pytz for timezone handling
from config import ConfigurationError, InstallationConfig, load_installation_config
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
from recording import RecordingStatus, SessionRecorder, archive_session
from transit_clock import ReplayClock, clock_from_args
from transit_time import AdsBTimestampOffsetValidator, port_timestamp_to_utc

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
stop_event = threading.Event()
active_sockets = {}
active_sockets_lock = threading.Lock()
shutdown_lock = threading.Lock()
shutdown_complete = False

# Global settings / Globalne ustawienia
MAX_AGE_SECONDS = 60  # Maksymalny czas życia wpisu po ostatnim odbiorze sygnału (w sekundach) / Maximum entry lifetime after the last received signal (in seconds)
TRANSIT_PREDICTION_GRACE_SECONDS = 3.0

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
sun_prediction_last_valid = {}
moon_prediction_last_valid = {}
sun_predicted_transit_utc = {}
moon_predicted_transit_utc = {}
plane_dict_lock = threading.RLock()
plane_deque = deque()


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
class MotionParameter:
    value: float
    updated_at_utc: datetime.datetime
    source: str


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


@dataclass(frozen=True)
class AircraftMotionFreshness:
    position_age: float | None
    altitude_age: float | None
    track_age: float | None
    groundspeed_age: float | None
    vertical_rate_age: float | None


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

adsb_host = None
adsb_port = None
adsb_timestamp_timezone = None
mlat_host = None
mlat_port = None


def apply_installation_config(configuration: InstallationConfig):
    global my_lat, my_lon, my_elevation_const, transition_altitude_ft
    global metar_station, gatech
    global adsb_host, adsb_port, adsb_timestamp_timezone, adsb_timestamp_validator
    global mlat_host, mlat_port, port_status
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
    gatech = ephem.Observer()
    gatech.lat, gatech.lon = str(my_lat), str(my_lon)
    gatech.elevation = my_elevation_const
    port_status = {adsb_port: False, mlat_port: False}


def correct_pressure_altitude(pressure_altitude_ft, qnh_hpa):
    """Apply the existing linear QNH approximation to pressure altitude."""
    return pressure_altitude_ft + (qnh_hpa - 1013.25) * 26


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
    setattr(
        _motion_state_for_update(icao),
        name,
        MotionParameter(float(value), updated_at_utc, source),
    )


def _update_motion_position(
        icao, latitude, longitude, updated_at_utc, port):
    source = _motion_source_for_port(port)
    if source is None:
        return
    _motion_state_for_update(icao).position = PositionParameter(
        float(latitude), float(longitude), updated_at_utc, source)


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
        sun_prediction_last_valid.pop(icao, None)
        moon_prediction_last_valid.pop(icao, None)
        sun_predicted_transit_utc.pop(icao, None)
        moon_predicted_transit_utc.pop(icao, None)

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
    lat1, lon1 = obs2moon
    lat2, lon2 = plane_pos
    lat1, lat2, lon1, lon2 = map(radians, [lat1, lat2, lon1, lon2])
    moon_az = float(moon_az)
    track = float(track)
    theta_13, theta_23 = radians(moon_az), radians(track)
    delta_12 = 2 * asin(sqrt(sin((lat1 - lat2) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon1 - lon2) / 2) ** 2))
    if delta_12 == 0:
        return 0
    x = (sin(lat2) - sin(lat1) * cos(delta_12)) / (sin(delta_12) * cos(lat1))
    x = min(1, max(-1, x))
    theta_a = acos(x)
    y = (sin(lat1) - sin(lat2) * cos(delta_12)) / (sin(delta_12) * cos(lat2))
    y = min(1, max(-1, y))
    theta_b = acos(y)
    theta_12 = theta_a if sin(lon2 - lon1) > 0 else 2 * math.pi - theta_a
    theta_21 = 2 * math.pi - theta_b if sin(lon2 - lon1) > 0 else theta_b
    alfa_1, alfa_2 = theta_13 - theta_12, theta_21 - theta_23
    if sin(alfa_1) == 0 and sin(alfa_2) == 0:
        return 0
    if (sin(alfa_1) * sin(alfa_2)) < 0:
        return 0
    alfa_3 = acos(-cos(alfa_1) * cos(alfa_2) + sin(alfa_1) * sin(alfa_2) * cos(delta_12))
    delta_13 = atan2(sin(delta_12) * sin(alfa_1) * sin(alfa_2), cos(alfa_2) + cos(alfa_1) * cos(alfa_3))
    lat3 = asin(sin(lat1) * cos(delta_13) + cos(lat1) * sin(delta_13) * cos(theta_13))
    Dlon_13 = atan2(sin(theta_13) * sin(delta_13) * cos(lat1), cos(delta_13) - sin(lat1) * sin(lat3))
    lon3 = lon1 + Dlon_13
    lat3, lon3 = degrees(lat3), (degrees(lon3) + 540) % 360 - 180
    dst_h2x = round(haversine((my_lat, my_lon), (lat3, lon3)), 1)
    if dst_h2x > 500:
        return 0
    if dst_h2x == 0:
        dst_h2x = 0.001
    if not is_int_try(elevation):
        return 0
    altitude1 = degrees(atan((elevation - my_elevation_const) / (dst_h2x * 1000)))
    azimuth1 = atan2(sin(radians(lon3 - my_lon)) * cos(radians(lat3)), cos(radians(my_lat)) * sin(radians(lat3)) - sin(radians(my_lat)) * cos(radians(lat3)) * cos(radians(lon3 - my_lon)))
    azimuth1 = round(((degrees(azimuth1) + 360) % 360), 1)
    dst_p2x = round(haversine((plane_pos[0], plane_pos[1]), (lat3, lon3)), 1)
    velocity = int(velocity)
    if velocity <= 0:
        return 0
    delta_time = (dst_p2x / velocity) * 3600
    moon_alt_B = 90.00 - moon_alt
    ideal_dist = (sin(radians(moon_alt_B)) * elevation) / sin(radians(moon_alt)) / 1000
    ideal_lat = asin(sin(radians(my_lat)) * cos(ideal_dist / earth_R) + cos(radians(my_lat)) * sin(ideal_dist / earth_R) * cos(radians(moon_az)))
    ideal_lon = radians(my_lon) + atan2(sin(radians(moon_az)) * sin(ideal_dist / earth_R) * cos(radians(my_lat)), cos(ideal_dist / earth_R) - sin(radians(my_lat)) * sin(ideal_lat))
    ideal_lat, ideal_lon = degrees(ideal_lat), degrees(ideal_lon)
    ideal_lon = (ideal_lon + 540) % 360 - 180
    return lat3, lon3, azimuth1, altitude1, dst_h2x, dst_p2x, delta_time, 0, moon_az, moon_alt, clock.now_utc()

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
        print('\a')  # TERMINAL GONG!

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
    global last_t
    output = sys.stdout if output is None else output
    emit = lambda *args: print(*args, file=output)
    gatech.date = clock.ephem_now()  # Aktualizuj datę w ephemeris / Update date in ephemeris
    vm, vs = ephem.Moon(gatech), ephem.Sun(gatech)  # Pobierz dane o Księżycu i Słońcu / Get data about the Moon and the Sun
    vm.compute(gatech)  # Oblicz pozycję Księżyca / Compute Moon position
    vs.compute(gatech)  # Oblicz pozycję Słońca / Compute Sun position
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
                wiersz += '{:>7} | '.format(plane_dict[pentry][11])

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
        sun_prediction_last_valid.pop(icao, None)
        moon_prediction_last_valid.pop(icao, None)
        sun_predicted_transit_utc.pop(icao, None)
        moon_predicted_transit_utc.pop(icao, None)

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


# Funkcja do przetwarzania linii danych / Function to process a line of data
@synchronized_plane_dict
def process_line(line, port):
    global last_update_time, moon_alt, moon_az, sun_alt, sun_az, gatech

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
            azimuth = atan2(sin(radians(plane_lon - my_lon)) * cos(radians(plane_lat)), cos(radians(my_lat)) * sin(radians(plane_lat)) - sin(radians(my_lat)) * cos(radians(plane_lat)) * cos(radians(plane_lon - my_lon)))
            azimuth = round(((degrees(azimuth) + 360) % 360), 1)
            if distance == 0:
                distance = 0.01
            altitude = (
                round(degrees(atan(
                    (elevation - my_elevation_const) / (distance * 1000))), 1)
                if elevation is not None else ""
            )
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

    if (mtype in ["1", "3", "4"]) and (
            icao in plane_dict and plane_dict[icao][2]
            and plane_dict[icao][11] and is_float_try(plane_dict[icao][4])):
        flight = plane_dict[icao][1]
        plane_lat = plane_dict[icao][2]
        plane_lon = plane_dict[icao][3]
        elevation = plane_dict[icao][4]
        distance = plane_dict[icao][5]
        azimuth = plane_dict[icao][6]
        altitude = plane_dict[icao][7]
        track = float(plane_dict[icao][11]) if is_float_try(plane_dict[icao][11]) else 0.0
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
        tst_int1 = transit_pred((my_lat, my_lon), (plane_lat, plane_lon), track, velocity, elevation, moon_alt, moon_az)
        tst_int2 = transit_pred((my_lat, my_lon), (plane_lat, plane_lon), track, velocity, elevation, sun_alt, sun_az)
        prediction_now = clock.now_utc()
        if tst_int1:
            alt_a = round(tst_int1[3], 2)
            dst_h2x = round(tst_int1[4], 2)
            dst_p2x = round(tst_int1[5], 2)
            delta_time = int(tst_int1[6])
            if 0 <= delta_time <= 900:  # Ignore past or excessively distant transits
                plane_dict[icao][25] = dst_h2x
                plane_dict[icao][23] = moon_alt
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
                    icao, "moon", prediction_now, delta_time)
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
            delta_time = int(tst_int2[6])
            if 0 <= delta_time <= 900:  # Ignore past or excessively distant transits
                plane_dict[icao][20] = dst_h2x
                plane_dict[icao][18] = sun_alt
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
                    icao, "sun", prediction_now, delta_time)
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
            configure_environment_recording(None)
        else:
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

