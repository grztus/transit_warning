# Transit Warning

Transit Warning receives SBS/BaseStation aircraft data from ADS-B and MLAT TCP
sources and predicts possible transits across the Sun and Moon for a configured
observer location.

## Requirements

- Python 3.10 or newer
- Access to ADS-B and MLAT TCP sources
- Tkinter/Tk support in the Python installation
- The packages listed in `requirements.txt`: `ephem`, `pytz`, `requests`,
  `python-dotenv`, and `tzdata`

`tzdata` provides IANA timezone data on systems such as Windows where it may not
be available from the operating system. On Linux, Tkinter may need to be
installed separately, for example with the `python3-tk` system package on
Debian-based distributions.

## Installation

Clone the repository, enter its directory, and install the dependencies:

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

Configure these fields:

- `OBSERVER_LAT` — observer latitude in decimal degrees
- `OBSERVER_LON` — observer longitude in decimal degrees
- `OBSERVER_ELEVATION_M` — observer/antenna elevation in metres above sea level
- `TRANSITION_ALTITUDE_FT` — local transition altitude as a positive integer in feet
- `ADSB_HOST` — ADS-B source IP address or hostname
- `ADSB_PORT` — ADS-B TCP port; normally `30003`
- `ADSB_TIMESTAMP_TIMEZONE` — IANA timezone of the SBS/dump1090-fa source,
  for example `Europe/Warsaw`
- `RAW_ADSB_HOST` — optional RAW Mode-S/ADS-B source hostname; normally the
  same receiver as `ADSB_HOST`
- `RAW_ADSB_PORT` — optional RAW source port; normally `30002`
- `BEAST_HOST` — Beast/Mode-S source IP address or hostname (default `192.168.56.1`)
- `BEAST_PORT` — Beast TCP port used for TC29 intent; normally `30005`
- `MLAT_HOST` — MLAT source IP address or hostname
- `MLAT_PORT` — MLAT TCP port; normally `30106`
- `METAR_STATION` — four-letter ICAO station used to retrieve METAR data from
  Aviation Weather Center, for example `EPRA`

Optional FlightAware MLAT Beast precision-track settings are
`MLAT_BEAST_ENABLED` (disabled by default), `MLAT_BEAST_HOST` (defaults to
`MLAT_HOST`), and `MLAT_BEAST_PORT` (normally `30105`).

Optional outgoing Telegram alerts use `TELEGRAM_NOTIFICATIONS_ENABLED`
(disabled by default), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and
`TELEGRAM_ALERT_SEPARATION_DEG` (default `2.0`). Credentials belong only in
the private `.env` file.

`ADSB_TIMESTAMP_TIMEZONE` describes the timezone used by the host producing the
naive SBS timestamps. It is not the timezone of the computer running Transit
Warning. Conversion uses the date of each record and the applicable IANA
daylight-saving rules.

The RAW source is optional and fail-open. Fresh DF17 TC19 ground-velocity
messages provide a fractional track for the matching aircraft only. After a
RAW update becomes stale, its fractional track may be held while fresh SBS or
MLAT updates continue to report exactly the same coarse track; any coarse-track
change invalidates the hold until another RAW update arrives. The terminal
shows one decimal place while fresh or coarse-confirmed RAW precision is
active; prediction retains the full decoded precision.

System environment variables override `.env`. The private `.env` file and the
`recordings/` directory are ignored by Git.

## Running

Start normal RealClock operation with:

```console
python transit_warning.py
```

### Telegram notifications

Create a bot with Telegram's `@BotFather`, place its token in
`TELEGRAM_BOT_TOKEN`, and send a message to the new bot. Obtain the target chat
ID from the Bot API `getUpdates` response, then set:

```dotenv
TELEGRAM_NOTIFICATIONS_ENABLED=true
TELEGRAM_BOT_TOKEN=123456:replace-with-your-token
TELEGRAM_CHAT_ID=replace-with-your-chat-id
TELEGRAM_ALERT_SEPARATION_DEG=2.0
TELEGRAM_ALERT_STABILITY_SECONDS=5
```

Test the outgoing connection without starting aircraft receivers or the normal
prediction loop:

```console
python transit_warning.py --test-notification
```

Telegram is an early attention/wake-up channel, not the live user interface.
It sends one potential-transit alert per active aircraft and Sun/Moon event
when the accepted prediction is within the existing approximately 15-minute
horizon and its separation remains continuously below the configured threshold
(2.0° by default) for the stability interval (five seconds by default). A brief
prediction that leaves the eligible range resets the timer. Set the stability
interval to `0` to retain immediate-send behavior. Subsequent countdown and
improving-separation updates are intentionally not sent. Alerts are predictions,
not guarantees of a photographic transit.

Checkpoint 1 supports outgoing alerts only; incoming commands, webhooks, and
observer location/GPS updates are intentionally not implemented.

#### Future dashboard graphics (design note only)

A later checkpoint may add a compact graphic that reuses the offline visualizer
concepts: the Sun/Moon disk, predicted aircraft trajectory, direction of motion,
and near-miss or crossing geometry. Checkpoint 2 does not implement trajectory
graphics, GPS, mobile observer, incoming commands, or location handling.

### Live dashboard

The optional read-only mobile dashboard shows independent chronological queues
of future Sun and Moon candidates plus a bounded in-memory recent-event history.
It uses already-computed production predictions and does not run another solver.
It is disabled and bound to localhost by default:

```dotenv
DASHBOARD_ENABLED=false
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8765
DASHBOARD_HISTORY_ENABLED=true
DASHBOARD_HISTORY_DIR=recordings/dashboard_history
TMUX_SEP_GREEN_MAX_DEG=3
TMUX_SEP_YELLOW_MAX_DEG=5
TMUX_SEP_VISIBLE_MAX_DEG=7
DASHBOARD_SEP_GREEN_MAX_DEG=3
DASHBOARD_SEP_YELLOW_MAX_DEG=5
DASHBOARD_SEP_VISIBLE_MAX_DEG=7
```

Terminal and dashboard separation presentation is configured independently.
With the defaults, separation below 3 degrees is green, 3 to below 5 degrees
is yellow, 5 to below 7 degrees is red, and values at or above 7 degrees are
not presented as transit candidates in that presentation layer. These settings
do not change the independent Telegram, audible gong, or snapshot thresholds.

For a local test, set `DASHBOARD_ENABLED=true`, start Transit Warning, and open
`http://127.0.0.1:8765/` on the same computer. The read-only JSON state is at
`http://127.0.0.1:8765/api/state`. To test from a phone on the same
trusted LAN, explicitly set `DASHBOARD_HOST` to the Debian host's LAN address,
restart Transit Warning, and open `http://<debian-lan-ip>:8765/` on the phone.
No real LAN address is stored in the repository. Return to localhost-only mode
by restoring `DASHBOARD_HOST=127.0.0.1`, or disable the server completely with
`DASHBOARD_ENABLED=false`.

The dashboard uses plain HTML/CSS/JavaScript with no CDN. The browser polls
state every three seconds and calculates visible countdowns locally from the
absolute predicted UTC timestamps. A compact ACTIVE/STALE/DISCONNECTED indicator
uses the main-loop heartbeat and polling results, independently of whether any
candidates are present. Events which pass normally, reach the near-event window,
or trigger a Telegram notification are retained in recent history; an early
insignificant transient is discarded. Retained history is appended to daily UTC
JSONL files under `DASHBOARD_HISTORY_DIR`; the 100-record in-memory list remains
only a recent hot cache. History can be filtered by UTC date, callsign, and body,
loaded in bounded pages, and exported as CSV. Checkpoint 2 intentionally has no
authentication, so LAN binding should only be used on a trusted private network.

The application validates the installation configuration before creating the
observer or starting the TCP threads. In RealClock mode it always records the
accepted QNH history in daily UTC files:

```text
recordings/
└── environment/
    └── environment_YYYYMMDD.jsonl
```

The daily environment recorder stores QNH changes and carries the latest known
state into a new UTC day. It operates independently of aircraft stream
recording.

### Recording a RAW ADS-B/SBS/MLAT session

Start session recording with:

```console
python transit_warning.py --record
```

One run creates one session directory with independent RAW ADS-B, SBS ADS-B,
and MLAT streams:

```text
recordings/
├── environment/
│   └── environment_YYYYMMDD.jsonl
└── sessions/
    └── YYYYMMDD_HHMMSS/
        ├── manifest.json
        └── streams.zip
```

Pressing Ctrl+C performs a controlled shutdown: it stops the TCP readers,
closes all stream writers, records final line counts and session status in the
manifest, and then archives the session. For a `complete` session, the raw
`raw_<port>.log`, `adsb_<port>.log`, and `mlat_<port>.log` files are removed
only after
`streams.zip` has been created and verified. For a `partial` or `failed`
session, raw logs are retained for diagnosis; a ZIP may also be present if
archiving succeeded.

`streams.zip` contains the RAW ADS-B, SBS ADS-B, and MLAT logs. The RAW stream
is captured for diagnostics but is not yet consumed by replay. Daily
environment/QNH files remain under `recordings/environment/` and are never
included in the session archive.

The verified ZIP members use the configured ports:

```text
raw_<RAW_ADSB_PORT>.log
adsb_<ADSB_PORT>.log
mlat_<MLAT_PORT>.log
```

RealClock also supports writing an additional explicit environment sidecar:

```console
python transit_warning.py --environment-record path/to/environment.jsonl
```

## Replay

Start Transit Warning with its replay clock:

```console
python transit_warning.py --clock replay
```

To apply an explicitly selected historical environment file during replay:

```console
python transit_warning.py --clock replay --environment-replay path/to/environment.jsonl
```

In another terminal, start one of the implemented replay scenarios:

```console
python replay_server.py adsb-2026 --speed 100
python replay_server.py mlat-2024 --speed 100
python replay_server.py dual-2026 --speed 100
```

Supported speeds are `1`, `10`, `100`, and `max`. `--host` changes the listen
address, while `--file` can override the input file for a single-stream
scenario. Replay scenarios require their corresponding local files under
`tests/data/`; these files are ignored by Git and may not exist after a fresh
clone.

## Offline transit snapshot visualizer

Render a schema-v3 transit snapshot with the smooth, high-precision diagnostic
trajectory:

```console
python tools/transit_snapshot_visualizer.py snapshot.json --zoom 3 --output transit.png
```

The visualizer preserves the stored production prediction and vertical SEP,
but uses unrounded observer-relative geometry for its default plotted path and
full two-dimensional closest-approach result. To compare it with the exact
legacy production-quantized geometry, add:

```console
--show-production-path
```

`--edge-tolerance-radii` controls the diagnostic limb band and defaults to
`0.05`: below `0.95 R` is `HIT`, `0.95-1.05 R` is `EDGE`, and above `1.05 R`
is `MISS`. This classification is offline diagnostics only and does not change
live alerts or production prediction behavior.
