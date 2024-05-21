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
import os
import subprocess
import sys
import datetime
import time
import math
import ephem
import re
import requests
import socket
import threading
from math import atan2, sin, cos, acos, radians, degrees, atan, asin, sqrt, isnan
import pytz  # Import pytz for timezone handling

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

# Global settings / Globalne ustawienia
MAX_AGE_SECONDS = 60  # Maksymalny czas życia wpisu po ostatnim odbiorze sygnału (w sekundach) / Maximum entry lifetime after the last received signal (in seconds)

# Deklaracja globalnych zmiennych / Declaration of global variables
global metar_t
global pressure
metar_t = datetime.datetime.now(pytz.utc) - datetime.timedelta(seconds=900)  # Ustawienie początkowego czasu / Initial setting of time
pressure = 1013  # Domyślne ciśnienie / Default pressure
metar_url = 'https://awiacja.imgw.pl/metar00.php?airport=EPRA'  # Adres URL z danymi METAR / URL for METAR data

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
plane_deque = deque()

# Ustawienie jednostek metrycznych / Set desired units
metric_units = True

# Inicjalizacja czasu z uwzględnieniem strefy czasowej / Initialize time with timezone
aktual_t = datetime.datetime.now(pytz.utc)
last_t = datetime.datetime.now(pytz.utc) - datetime.timedelta(seconds=10)
gong_t = datetime.datetime.now(pytz.utc)

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
my_lat = 51.1111  # Szerokość geograficzna / Latitude
my_lon = 21.1111  # Długość geograficzna / Longitude
my_elevation_const = 111  # Wysokość anteny = wysokość miejsca + 3 metry / Your antenna elevation = site elevation + for example 3 metres
near_airport_elevation = 100  # Wysokość najbliższego lotniska / Nearest airport elevation

# Ustawienia efemeryd / Ephemeris settings
gatech = ephem.Observer()
gatech.lat, gatech.lon = str(my_lat), str(my_lon)
gatech.elevation = my_elevation_const

# Obliczanie strefy czasowej dla daty/godziny ISO / Calculate time zone for ISO date/timestamp
timezone_hours = time.altzone / 60 / 60
last_update_time = datetime.datetime.now(pytz.utc)  # Inicjalizacja zmiennej na początku skryptu / Initialize variable at the beginning of the script

port_status = {30003: False, 30106: False}  # Inicjalizacja statusów portów / Initialize port statuses

# Funkcja do czyszczenia ekranu / Function to clear the screen
def clear_screen():
    if os.name == 'nt':
        subprocess.call('cls', shell=True)
    else:
        subprocess.call('clear', shell=True)

# Funkcja do czyszczenia słownika samolotów / Function to clean the plane dictionary
def clean_dict():
    current_time = datetime.datetime.now(pytz.utc)
    to_delete = [icao for icao, entry in plane_dict.items() if (current_time - entry[0]).total_seconds() > MAX_AGE_SECONDS]
    for icao in to_delete:
        del plane_dict[icao]

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
        date_time = datetime.datetime.now(pytz.utc).strftime("%Y-%m-%d %H:%M:%S")
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
    delta_time = (dst_p2x / velocity) * 3600
    moon_alt_B = 90.00 - moon_alt
    ideal_dist = (sin(radians(moon_alt_B)) * elevation) / sin(radians(moon_alt)) / 1000
    ideal_lat = asin(sin(radians(my_lat)) * cos(ideal_dist / earth_R) + cos(radians(my_lat)) * sin(ideal_dist / earth_R) * cos(radians(moon_az)))
    ideal_lon = radians(my_lon) + atan2(sin(radians(moon_az)) * sin(ideal_dist / earth_R) * cos(radians(my_lat)), cos(ideal_dist / earth_R) - sin(radians(my_lat)) * sin(ideal_lat))
    ideal_lat, ideal_lon = degrees(ideal_lat), degrees(ideal_lon)
    ideal_lon = (ideal_lon + 540) % 360 - 180
    return lat3, lon3, azimuth1, altitude1, dst_h2x, dst_p2x, delta_time, 0, moon_az, moon_alt, datetime.datetime.now(pytz.utc)

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
    aktual_gong_t = datetime.datetime.now(pytz.utc)
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
    global pressure
    aktual_metar_t = datetime.datetime.now(pytz.utc)
    diff_metar_t = (aktual_metar_t - metar_t).total_seconds()
    if diff_metar_t > 900:
        metar_t = aktual_metar_t
        try:
            response = requests.get(metar_url)
            if response.status_code == 200:
                metar_data = response.text
                pressure_match = re.search(r'Q(\d{4})', metar_data)
                if pressure_match:
                    pressure = int(pressure_match.group(1))
                    if 800 < pressure < 1100:
                        return pressure
                    else:
                        return 1013  # Wartość domyślna w przypadku nierealistycznego odczytu / Default value in case of unrealistic reading
                else:
                    return 1013  # Wartość domyślna, jeśli brak ciśnienia w danych / Default value if no pressure in data
            else:
                return 1013  # Wartość domyślna, jeśli odpowiedź serwera nie jest 200 OK / Default value if server response is not 200 OK
        except requests.exceptions.RequestException as e:
            print("Error retrieving METAR data: ", e)
            return pressure  # Zwraca ostatnio znaną wartość ciśnienia, jeśli wystąpi błąd / Returns the last known pressure value if an error occurs
    else:
        return pressure  # Zwraca ostatnio znaną wartość ciśnienia, jeśli nie jest czas na aktualizację / Returns the last known pressure value if it's not time for an update

# Funkcja do generowania tabeli wyjściowej / Function to generate output table
def tabela():
    global last_t
    gatech.date = ephem.now()  # Aktualizuj datę w ephemeris / Update date in ephemeris
    vm, vs = ephem.Moon(gatech), ephem.Sun(gatech)  # Pobierz dane o Księżycu i Słońcu / Get data about the Moon and the Sun
    vm.compute(gatech)  # Oblicz pozycję Księżyca / Compute Moon position
    vs.compute(gatech)  # Oblicz pozycję Słońca / Compute Sun position
    moon_alt, moon_az = round(math.degrees(vm.alt), 1), round(math.degrees(vm.az), 1)  # Wysokość i azymut Księżyca / Moon altitude and azimuth
    sun_alt, sun_az = round(math.degrees(vs.alt), 1), round(math.degrees(vs.az), 1)  # Wysokość i azymut Słońca / Sun altitude and azimuth
    aktual_t = datetime.datetime.now(pytz.utc)  # Aktualny czas w UTC / Current time in UTC
    diff_t = (aktual_t - last_t).total_seconds()  # Różnica czasu od ostatniego odświeżenia / Time difference from last refresh
    if diff_t > 1:
        last_t = aktual_t  # Ustaw ostatni czas odświeżenia / Set last refresh time
        clear_screen()  # Wyczyść ekran / Clear the screen
        print("Flight info |  Actual parameters  |-- Pred. closest  --|--- Current Az/Alt ---|----- Transits: Sun", sun_az, sun_alt,'  & Moon', moon_az, moon_alt )
        print('{:9} {:>6} {:>7} {} {:>6} {} {:>8} {} {:>7} {} {:>6} {:>6} {:>5} {} {:>7} {:>7} {:>7} {:>8} {} {:>7} {:>7} {:>7} {:>7} {} {:>5}'.format(\
        ' icao or', ' (m)', '(d)', '|', '(km)', '|', '(km)', '|', '(d)', '|', '(d)', '(d)', '(l)', ' |', '(d)', '(km)', '(km)', '   (s)', '|', '(d)', '(km)', '(km)', '   (s)', ' |', '(s)'))
        print('{:9} {:>6} {:>7} {} {:>6} {} {:>8} {} {:>7} {} {:>6} {:>6} {:>5} {} {:>7} {:>7} {:>7} {:>8} {} {:>7} {:>7} {:>7} {:>7} {} {:>5}'.format(\
        ' flight', 'elev', 'trck', '|', 'dist', '|', '[warn]','|', '[Alt]', '|', 'Alt', 'Azim', 'Azim', ' |', 'Sep', 'p2x', 'h2x', 'time2X', '|', 'Sep', 'p2x', 'h2x', 'time2X', ' |', 'age'))
        print("-------------------------|--------|--------- |---------|----------------------|----------------------------------|----------------------------------|------------------|")

        for pentry in plane_dict:
            try:
                distance = float(plane_dict[pentry][5])
            except ValueError:
                continue

            if distance <= warning_distance:
                then = plane_dict[pentry][17] if plane_dict[pentry][17] else datetime.datetime.now(pytz.utc)
                diff_seconds = (datetime.datetime.now(pytz.utc) - then).total_seconds()
                diff_minutes = (datetime.datetime.now(pytz.utc) - plane_dict[pentry][0]).total_seconds() / 60

                if plane_dict[pentry][1]:
                    wiersz = '{}{:<9}{}'.format(YELLOW, plane_dict[pentry][1], RESET)
                else:
                    wiersz = '{}{:<9}{}'.format(RESET, pentry, RESET)

                elevation = int(plane_dict[pentry][4]) if is_float_try(plane_dict[pentry][4]) else 9999
                wiersz += '{}{:>7}{} '.format(elev_col(elevation), elevation, RESET)
                wiersz += '{:>7} | '.format(plane_dict[pentry][11])

                wiersz += '{}{:>6.1f}{} | '.format(dist_col(plane_dict[pentry][5]), distance, RESET)

                try:
                    warn_val = float(plane_dict[pentry][13])
                except ValueError:
                    warn_val = 0.0  # Default value if conversion fails

                if plane_dict[pentry][12] == 'WARNING' and plane_dict[pentry][9] != "RECEDING":
                    wiersz += '[{}{:>7.1f}{}]'.format(REDALERT, warn_val, RESET)
                elif plane_dict[pentry][12] == 'WARNING' and plane_dict[pentry][9] == "RECEDING":
                    wiersz += '[{}{:>7.1f}{}]'.format(RED, warn_val, RESET)
                elif plane_dict[pentry][12] != 'WARNING' and plane_dict[pentry][9] == "RECEDING":
                    wiersz += '[{}{:>7.1f}{}]'.format(PURPLEDARK, warn_val, RESET)
                else:
                    wiersz += '[{}{:>7.1f}{}]'.format(PURPLE, warn_val, RESET)

                if is_float_try(plane_dict[pentry][13]):
                    altitudeX = round(degrees(atan((elevation - my_elevation_const) / (float(plane_dict[pentry][13]) * 1000))), 1) if plane_dict[pentry][13] else 0
                else:
                    altitudeX = 0.0

                wiersz += '[{}{:>7.1f}{}] | '.format(alt_col(altitudeX), altitudeX, RESET)
                wiersz += '{}{:>6.1f}{}'.format(alt_col(plane_dict[pentry][7]), plane_dict[pentry][7], RESET)

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

                wiersz += '{:>6.1f} '.format(plane_dict[pentry][6])
                wiersz += '{:>6} | '.format(wind_deg_to_str1(plane_dict[pentry][6]))

                diff_secx = (datetime.datetime.now(pytz.utc) - plane_dict[pentry][0]).total_seconds()
                separation_deg = float(plane_dict[pentry][24] - plane_dict[pentry][23]) if is_float_try(plane_dict[pentry][24]) and is_float_try(plane_dict[pentry][23]) else 90.0

                if -transit_separation_GREENALERT_FG < separation_deg < transit_separation_GREENALERT_FG:
                    wiersz += '{}{:>7.2f}{} {:>7.1f} {:>7.1f} {:>8.1f}'.format(GREENALERT, separation_deg, RESET, plane_dict[pentry][27], plane_dict[pentry][25], plane_dict[pentry][26])
                elif -transit_separation_REDALERT_FG < separation_deg < transit_separation_REDALERT_FG:
                    wiersz += '{}{:>7.2f}{} {:>7.1f} {:>7.1f} {:>8.1f}'.format(REDALERT, separation_deg, RESET, plane_dict[pentry][27], plane_dict[pentry][25], plane_dict[pentry][26])
                elif -transit_separation_notignored < separation_deg < transit_separation_notignored:
                    wiersz += '{}{:>7.2f}{} {:>7.1f} {:>7.1f} {:>8.1f}'.format(RED, separation_deg, RESET, plane_dict[pentry][27], plane_dict[pentry][25], plane_dict[pentry][26])
                else:
                    wiersz += '{:>7} {:>7} {:>7} {:>8}'.format('---', '---', '---', '---')

                wiersz += ' | '

                separation_deg2 = float(plane_dict[pentry][19] - plane_dict[pentry][18]) if is_float_try(plane_dict[pentry][19]) and is_float_try(plane_dict[pentry][18]) else 90.0

                if -transit_separation_GREENALERT_FG < separation_deg2 < transit_separation_GREENALERT_FG:
                    wiersz += '{}{:>7.2f}{} {:>7.1f} {:>7.1f} {:>8.1f}'.format(GREENALERT, separation_deg2, RESET, plane_dict[pentry][21], plane_dict[pentry][20], plane_dict[pentry][22])
                elif -transit_separation_REDALERT_FG < separation_deg2 < transit_separation_REDALERT_FG:
                    wiersz += '{}{:>7.2f}{} {:>7.1f} {:>7.1f} {:>8.1f}'.format(REDALERT, separation_deg2, RESET, plane_dict[pentry][21], plane_dict[pentry][20], plane_dict[pentry][22])
                elif -transit_separation_notignored < separation_deg2 < transit_separation_notignored:
                    wiersz += '{}{:>7.2f}{} {:>7.1f} {:>7.1f} {:>8.1f}'.format(RED, separation_deg2, RESET, plane_dict[pentry][21], plane_dict[pentry][20], plane_dict[pentry][22])
                else:
                    wiersz += '{:>7} {:>7} {:>7} {:>8}'.format('---', '---', '---', '---')

                wiersz += ' | '
                wiersz += '{:>5.1f}'.format(diff_secx)
                wiersz += ' {} {} '.format(len(plane_dict[pentry][15]), len(plane_dict[pentry][16]))
                wiersz += '{:>5.1f}'.format(diff_seconds)
                print(wiersz)

        print(" ")
        print("{} (UTC) --- delay < {:.1f}s --- QNH {}hPa".format(datetime.datetime.now(pytz.utc).time(), diff_t, pressure))
        # Print port statuses
        for port, status in port_status.items():
            status_str = "Listening" if status else "Not listening"
            print("Port {}: {}".format(port, status_str))

    return moon_alt, moon_az, sun_alt, sun_az


# Funkcja do czyszczenia słownika tranzytów / Function to clean the transit dictionary
def clean_transit_dict():
    current_time = datetime.datetime.now(pytz.utc)
    to_delete = [icao for icao, entry in plane_dict.items() if len(entry) > 31 and entry[31] and isinstance(entry[30], datetime.datetime) and (current_time - entry[30]).total_seconds() > 120]
    for icao in to_delete:
        del plane_dict[icao]

# Funkcja do czytania danych z portu / Function to read data from port
def read_from_port(port, process_line):
    global port_status
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(('127.0.0.1', port))
            port_status[port] = True
            file = sock.makefile()
            while True:
                line = file.readline()
                if not line:
                    break
                process_line(line.strip(), port)
        except Exception as e:
            print("Error on port {}: {}".format(port, e))
            port_status[port] = False
            time.sleep(5)  # Retry after a short delay


# Funkcja do przetwarzania linii danych / Function to process a line of data
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

    # Konwersja daty i czasu na UTC / Convert date and time to UTC
    try:
        date_time = datetime.datetime.strptime(date + " " + time, '%Y/%m/%d %H:%M:%S.%f').replace(tzinfo=pytz.utc)
    except ValueError:
        print("Error parsing date and time: {} {}".format(date, time))
        return

    if port == 30003:
        # Dane ADS-B / ADS-B data
        date_time_utc = date_time + datetime.timedelta(hours=timezone_hours)
    else:
        # Dane MLAT, już w UTC / MLAT data, already in UTC
        date_time_utc = date_time

    if mtype == "1":
        flight = parts[10].strip()
        if icao not in plane_dict:
            plane_dict[icao] = [date_time_utc, flight, "", "", "", "", "", "", "", "", "", "", "", "", "", [], [], "", "", "", "", "", "", "", "", "", "", "", "", "", None, False]
        else:
            plane_dict[icao][0] = date_time_utc
            plane_dict[icao][1] = flight
            last_update_time = datetime.datetime.now(pytz.utc)

    if mtype == "5":
        flight = parts[10].strip()
        elevation = parts[11].strip()
        if is_int_try(elevation):
            elevation = int(elevation)
            if elevation > 6500:
                pressure = int(get_metar_press())
                elevation += (1013 - pressure) * 26
                my_elevation = my_elevation_const
            else:
                my_elevation = near_airport_elevation
            if metric_units:
                elevation = float((elevation * 0.3048))
            else:
                elevation = ""
        if icao not in plane_dict:
            plane_dict[icao] = [date_time_utc, flight, "", "", elevation, "", "", "", "", "", "", "", "", "", "", [], [], "", "", "", "", "", "", "", "", "", "", "", "", "", None, False]
        else:
            plane_dict[icao][4] = elevation
            plane_dict[icao][0] = date_time_utc
            last_update_time = datetime.datetime.now(pytz.utc)
            if flight != '':
                plane_dict[icao][1] = flight

    if mtype == "4" or (mtype == "3" and a_m_type == "MLAT"):
        velocity = parts[12].strip()
        track = parts[13].strip() if len(parts) > 13 else ''
        if is_int_try(velocity):
            velocity = round(int(velocity) * 1.852)
        else:
            velocity = 900
        if icao not in plane_dict:
            plane_dict[icao] = [date_time_utc, "", "", "", "", "", "", "", "", "", "", track, "", "", velocity, [], [], "", "", "", "", "", "", "", "", "", "", "", "", "", None, False]
        else:
            plane_dict[icao][0] = date_time_utc
            if track:  # Aktualizuj track tylko, jeśli nie jest pusty / Update track only if not empty
                plane_dict[icao][11] = track
            plane_dict[icao][14] = velocity
            last_update_time = datetime.datetime.now(pytz.utc)

    if mtype == "3":
        elevation = parts[11].strip()
        track = parts[12].strip() if len(parts) > 12 else ''
        if is_int_try(elevation):
            elevation = int(elevation)
            if elevation > 6500:
                pressure = int(get_metar_press())
                elevation += (1013 - pressure) * 26
                my_elevation = my_elevation_const
            else:
                my_elevation = near_airport_elevation
            if metric_units:
                elevation = float((elevation * 0.3048))
            else:
                elevation = ""
        try:
            plane_lat = float(parts[14])
        except ValueError:
            plane_lat = 0.0
        try:
            plane_lon = float(parts[15])
        except ValueError:
            plane_lon = 0.0
        if plane_lat and plane_lon:
            distance = round(haversine((my_lat, my_lon), (plane_lat, plane_lon)), 1)
            azimuth = atan2(sin(radians(plane_lon - my_lon)) * cos(radians(plane_lat)), cos(radians(my_lat)) * sin(radians(plane_lat)) - sin(radians(my_lat)) * cos(radians(plane_lat)) * cos(radians(plane_lon - my_lon)))
            azimuth = round(((degrees(azimuth) + 360) % 360), 1)
            if distance == 0:
                distance = 0.01
            altitude = degrees(atan((elevation - my_elevation_const) / (distance * 1000)))
            altitude = round(altitude, 1)
            if icao not in plane_dict:
                plane_dict[icao] = [date_time_utc, "", plane_lat, plane_lon, elevation, distance, azimuth, altitude, "", "", distance, track, "", "", "", [], [], "", "", "", "", "", "", "", "", "", "", "", "", "", None, False]
                plane_dict[icao][15] = []
                plane_dict[icao][16] = []
                plane_dict[icao][15].append(azimuth)
                plane_dict[icao][16].append(altitude)
                last_update_time = datetime.datetime.now(pytz.utc)
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
                plane_dict[icao][4] = elevation
                plane_dict[icao][5] = distance
                plane_dict[icao][6] = azimuth
                plane_dict[icao][7] = altitude
                if track:  # Aktualizuj track tylko, jeśli nie jest pusty / Update track only if not empty
                    plane_dict[icao][11] = track
                last_update_time = datetime.datetime.now(pytz.utc)
                if not plane_dict[icao][17]:
                    plane_dict[icao][17] = date_time_utc
                then = plane_dict[icao][17]
                now = datetime.datetime.now(pytz.utc)
                diff_seconds = (now - then).total_seconds()
                if diff_seconds > 6:
                    plane_dict[icao][17] = date_time_utc
                    poz_az = str(plane_dict[icao][6])
                    poz_alt = str(plane_dict[icao][7])
                    plane_dict[icao][15].append(poz_az)
                    plane_dict[icao][16].append(poz_alt)

    if (mtype in ["1", "3", "4"]) and (icao in plane_dict and plane_dict[icao][2] and plane_dict[icao][11]):
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
        if tst_int1:
            alt_a = round(tst_int1[3], 2)
            dst_h2x = round(tst_int1[4], 2)
            dst_p2x = round(tst_int1[5], 2)
            delta_time = int(tst_int1[6])
            if delta_time <= 900:  # Ignore transits with time to transit greater than 900 seconds
                plane_dict[icao][25] = dst_h2x
                plane_dict[icao][23] = moon_alt
                plane_dict[icao][24] = alt_a
                plane_dict[icao][26] = delta_time
                plane_dict[icao][27] = dst_p2x
                separation_deg = float(plane_dict[icao][24] - plane_dict[icao][23]) if is_float_try(plane_dict[icao][24]) and is_float_try(plane_dict[icao][23]) else 90.0
                if -transit_separation_sound_alert < separation_deg < transit_separation_sound_alert:
                    gong()
                if delta_time <= 2:  # Ustaw flagę tranzytu jeśli czas do tranzytu jest mniejszy lub równy 2 sekundy / Set transit flag if time to transit is less than or equal to 2 second
                    plane_dict[icao][31] = True
                    plane_dict[icao][30] = datetime.datetime.now(pytz.utc)  # Ustaw czas rozpoczęcia tranzytu / Set transit start time
                plane_dict[icao][29] = datetime.datetime.now(pytz.utc)
        if tst_int2:
            alt_a = round(tst_int2[3], 2)
            dst_h2x = round(tst_int2[4], 2)
            dst_p2x = round(tst_int2[5], 2)
            delta_time = int(tst_int2[6])
            if delta_time <= 900:  # Ignore transits with time to transit greater than 900 seconds
                plane_dict[icao][20] = dst_h2x
                plane_dict[icao][18] = sun_alt
                plane_dict[icao][19] = alt_a
                plane_dict[icao][22] = delta_time
                plane_dict[icao][21] = dst_p2x
                separation_deg2 = float(plane_dict[icao][19] - plane_dict[icao][18]) if is_float_try(plane_dict[icao][19]) and is_float_try(plane_dict[icao][18]) else 90.0
                if -transit_separation_sound_alert < separation_deg2 < transit_separation_sound_alert:
                    gong()
                if delta_time <= 2:  # Ustaw flagę tranzytu jeśli czas do tranzytu jest mniejszy lub równy 2 sekundy / Set transit flag if time to transit is less than or equal to 2 second
                    plane_dict[icao][31] = True
                    plane_dict[icao][30] = datetime.datetime.now(pytz.utc)  # Ustaw czas rozpoczęcia tranzytu / Set transit start time
                plane_dict[icao][30] = datetime.datetime.now(pytz.utc)
    sun_alt, sun_az, moon_alt, moon_az = tabela()
    clean_dict()
    clean_transit_dict()


# Uruchomienie wątków do czytania z portów / Start threads to read from ports
threading.Thread(target=read_from_port, args=(30003, process_line)).start()
threading.Thread(target=read_from_port, args=(30106, process_line)).start()

# Pętla główna / Main loop
while True:
    time.sleep(1)
    sun_alt, sun_az, moon_alt, moon_az = tabela()
    clean_dict()
    clean_transit_dict()

