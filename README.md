Transit Warning is a flight tracker script designed to track aircraft and calculate potential transits across the Sun and Moon from a specified location.
It uses ADS-B and MLAT data to determine the positions and velocities of aircraft and predicts their paths to identify possible transit events.

Key Features
Tracks aircraft in real-time using ADS-B and MLAT data.
Predicts aircraft transits across the Sun and Moon.
Provides visual and audible alerts for close approaches and transits.
Prerequisites
Before running the script, ensure you have the following Python modules installed:

ephem
requests
pytz
socket
tkinter (if using a GUI)
To install these modules, you can use pip: pip install ephem requests pytz
Socket and tkinter should be preinstalled with python.

Configuration
Before running the script, you need to configure a few settings at the beginning of the script. These include your location, elevation, and the METAR URL for weather data.

Location and Elevation:
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


Running the Script
To run the script, execute it with Python:

python transit_warning_v4.py (or python3 transit_warning_v4.py)

Notes
Ensure that your system has access to the ADS-B and MLAT data streams. The script is designed to connect to local ports (30003 for ADS-B and 30106 for MLAT).
The script clears the terminal screen periodically to display updated tracking information. If running on a system without a terminal, you might need to adjust or remove the clear_screen function calls.
By following these instructions and configurations, you should be able to track aircraft and predict potential transits effectively.

Grzegorz Tuszynski
