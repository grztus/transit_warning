# Transit Warning

Transit Warning tracks aircraft from ADS-B and MLAT data and predicts potential
transits across the Sun and Moon for a configured observer location.

## Requirements

- Python 3.10 or newer
- Access to ADS-B and MLAT TCP sources
- Tkinter/Tk support in the Python installation (the application currently imports it)

The project no longer supports Python 2.

## Fresh installation

Clone the repository, enter its directory, and install the Python dependencies:

```console
git clone <repository-url>
cd transit_warning
python -m pip install -r requirements.txt
```

Create a private `.env` file from the public template.

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Linux/macOS:

```console
cp .env.example .env
```

Edit `.env` and provide values for:

- `OBSERVER_LAT` — observer latitude in decimal degrees
- `OBSERVER_LON` — observer longitude in decimal degrees
- `OBSERVER_ELEVATION_M` — observer/antenna elevation in metres above sea level
- `ADSB_HOST` — IP address or hostname of the ADS-B source
- `ADSB_TIMESTAMP_TIMEZONE` — IANA timezone used by the ADS-B source for naive
  SBS timestamps (for example `Europe/Warsaw`)
- `MLAT_HOST` — IP address or hostname of the MLAT source
- `METAR_STATION` — four-letter ICAO station identifier used to retrieve METAR data from Aviation Weather Center (for example `EPRA`)

Keep `ADSB_PORT=30003` and `MLAT_PORT=30106` if the local data sources use the
default TCP ports. System environment variables override values from `.env`.
The private `.env` file is ignored by Git; do not commit it.

The ADS-B timezone describes the clock used by the host producing port 30003,
not the timezone of the computer running Transit Warning. It is applied using
the timestamp's own date, including the applicable daylight-saving rules.

## Running the application

Normal operation:

```console
python transit_warning.py
```

The application validates the installation configuration before it creates the
observer or starts the TCP input threads. Configuration errors are reported at
startup with the affected field names.

## Replay mode

Start the application with its replay clock:

```console
python transit_warning.py --clock replay
```

In another terminal, run one of the replay scenarios, for example:

```console
python replay_server.py adsb-2026 --speed 100
python replay_server.py mlat-2024 --speed 100
python replay_server.py dual-2026 --speed 100
```

Replay scenarios require their corresponding recording files under
`tests/data/`. These recordings are local files and are ignored by Git, so they
may not be present after a fresh clone.

## Notes

- The default source ports are 30003 for ADS-B and 30106 for MLAT.
- The terminal display is cleared periodically while the application runs.
- On Linux, Tkinter may need to be installed through the operating system's
  package manager (for example, the `python3-tk` package on Debian-based systems).
