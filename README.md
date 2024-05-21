Transit Warning is a flight tracker script designed to track aircraft and calculate potential transits across the Sun and Moon from a specified location.</br>
It uses ADS-B and MLAT data to determine the positions and velocities of aircraft and predicts their paths to identify possible transit events.

Key Features</br>
Tracks aircraft in real-time using ADS-B and MLAT data.</br>
Predicts aircraft transits across the Sun and Moon.</br>
Provides visual and audible alerts for close approaches and transits.</br></br>
# Prerequisites:</br>
Before running the script, ensure you have the following Python modules installed:</br>
ephem</br>
requests</br>
pytz</br>
socket</br>
tkinter (if using a GUI)</br>
To install these modules, you can use pip: pip install ephem requests pytz</br>
Socket should be preinstalled with python.

Configuration</br>
Before running the script, you need to configure a few settings at the beginning of the script. These include your location, elevation, and the METAR URL for weather data.</br>
You also need to change 127.0.0.1 in sock.connect(('127.0.0.1', port)) to your machine IP address if You are not working on the localhost (line 516 in v5 sript), for example sock.connect(('192.168.1.197', port)).</br>

# Set geographic location and elevation
my_lat = 51.1111  # Latitude

my_lon = 21.1111  # Longitude

my_elevation_const = 114  # Your antenna elevation = site elevation + 3 metres (for example) - this elevation is taken into calculations

near_airport_elevation = 111  # Nearest airport elevation

# METAR URL for weather data
metar_url = 'https://awiacja.imgw.pl/metar00.php?airport=EPRA'  # Change to your local METAR URL (if there is some provided)

# Set desired distance and time limits
warning_distance = 200  # Warning radius in km

alert_distance = 15  # Alert radius in km

xtd_tst = 20  # Cross-track distance threshold


# Running the Script
To run the script, execute it with Python:

python transit_warning_v5.py (or python3 transit_warning_v5.py)

Notes
Ensure that your system has access to the ADS-B and MLAT data streams. The script is designed to connect to local ports (30003 for ADS-B and 30106 for MLAT).</br>
The script clears the terminal screen periodically to display updated tracking information. If running on a system without a terminal, you might need to adjust or remove the clear_screen function calls.</br>
By following these instructions and configurations, you should be able to track aircraft and predict potential transits effectively.</br>

This script is also running on Anroid (tested with Pydroid3 on Samsung S23) - but to make this happen you need to comment the line (add "#") with clear_screen() function (line 396 in v5 script).

Grzegorz Tuszynski
