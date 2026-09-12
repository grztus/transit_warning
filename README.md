# Transit Warning STANDARD

Transit Warning is a real-time aircraft transit prediction application. It uses
aircraft position and motion data to predict upcoming close apparent passages
against the **Sun and Moon** for an observer, helping photographers and observers
prepare for possible transits.

**STANDARD is the public edition on `main`.** Its responsive desktop/phone web
interface provides **LIVE | HISTORY**. The Python backend owns prediction and
notification state; the browser displays it and changes supported settings.

## Features

- SUN and MOON predictions, including authoritative **TRUE_2D** closest approach.
- **LOCAL** receiver feeds, **INTERNET** data from ADSB.lol, and **AUTO** per-field fusion.
- **STATIC** configured or custom fixed location, and **MOBILE** browser geolocation.
- Independent Telegram **SUN/MOON** notification switches.
- Prediction lifecycle, event finalization, HISTORY filters, pagination and CSV export.
- Local ADS-B/SBS and MLAT support, optional precision/intent feeds, receiver
  recording, candidate forensic capture and diagnostic snapshots.
- Compact Backend state and Controls cards suited to phone use.

Maps, centerline/corridor, fullscreen maps and Pattern Planner/PATTERN navigation
are not part of STANDARD/main.

## Requirements

| Component | Requirement |
|---|---|
| Python | Source-level minimum **3.10**; use **3.11+** for a new deployment. Production validated with **3.11.15**. |
| Node.js | **22.22.2+ within 22.x** or **24.15.0+ within 24.x** for the locked frontend build/test stack. |
| Python packages | Install [requirements.txt](requirements.txt) using the interpreter that will run the application. |
| Geometry data | Readable GeographicLib **EGM96 PGM** geoid grid for authoritative TRUE_2D. |
| Aircraft input | Configured local feeds and/or internet access to ADSB.lol, according to source mode. |

The code uses evaluated union type annotations requiring Python 3.10, as well as
modern standard-library facilities. Python 2 and Debian 10's system Python 3.7
are unsupported. The requirements are not pinned: pip selects releases compatible
with its interpreter. The audited installed Matplotlib 3.11.1 requires Python
3.11; Python 3.10 would require an older compatible tooling release and is not the
validated Debian baseline. Do not replace Debian's system Python symlink; install
a modern interpreter separately and use a virtual environment.

Runtime packages are `ephem`, `pytz`, `requests`, `python-dotenv` and `tzdata`.
Matplotlib is intentionally included for plotting/validation tools and their tests;
it is not the live prediction engine. `tzdata` supplies IANA timezone rules where
system data are absent. No Python dependency restructuring is needed.
See [frontend toolchain evidence](web/README.md#toolchain) for the locked Node engines.

## Quick start

The following Linux/Debian example uses Python 3.11 with its `venv` and pip support
already installed, plus a supported Node release:

```sh
git clone https://github.com/grztus/transit_warning.git
cd transit_warning
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` before starting. Fill in observer coordinates/elevation,
`TRANSITION_ALTITUDE_FT`, `ADSB_TIMESTAMP_TIMEZONE` and `METAR_STATION`, and set the
feed endpoints for your installation. These fields are validated even when using
INTERNET. To use the public dashboard, set `DASHBOARD_ENABLED=true`; to accept
browser GPS also set `DASHBOARD_MOBILE_GPS_ENABLED=true`.

For authoritative TRUE_2D, install a GeographicLib EGM96 PGM grid readable by the
application (see [GeographicLib dataset installation](https://geographiclib.sourceforge.io/C++/doc/geoid.html)),
set `FLEET_GEOID_PGM_PATH` if it is outside the standard discovery
locations, and select `AUTHORITATIVE_PREDICTION_GEOMETRY=TRUE_2D`. Geoid data are
separate from pip packages. The shipped default remains LEGACY for compatibility;
TRUE_2D with missing datum data withholds affected predictions, rather than
silently using LEGACY geometry.

Build the production frontend:

```sh
cd web
npm ci
npm run build
cd ..
.venv/bin/python transit_warning.py
```

The backend serves the generated **`web/dist/`** at `/`. Open
`http://localhost:8765` on the same machine for local use. Without the build,
`/` falls back to the compatibility dashboard; `/legacy` remains available for
debugging. **Vite is not required in production.** Ctrl+C invokes graceful shutdown.

Use the same Python executable for installation and execution. If using a separate
non-venv installation, the equivalent dependency command is
`python3.11 -m pip install -r requirements.txt`, not an ambiguous `pip3` command.
On Windows, use the selected interpreter's venv executable, `Copy-Item` instead of
`cp`, and `npm.cmd` in PowerShell.

## Configuration

[.env.example](.env.example) is the detailed startup configuration template.
Environment variables override `.env`. Keep coordinates, credentials, recordings
and local endpoint details private; do not commit your populated `.env`.

| Settings | Purpose |
|---|---|
| `OBSERVER_LAT`, `OBSERVER_LON`, `OBSERVER_ELEVATION_M` | Default fixed observer; decimal degrees and metres AMSL. |
| `OBSERVER_MODE` | Startup STATIC or MOBILE; MOBILE requires dashboard and GPS enabled. |
| `AIRCRAFT_SOURCE_MODE` | Startup LOCAL, INTERNET or AUTO. |
| `ADSB_HOST/PORT`, `MLAT_HOST/PORT` | Local SBS feed endpoints. |
| `DASHBOARD_ENABLED`, `DASHBOARD_HOST/PORT` | Dashboard enable and bind address; defaults off and `127.0.0.1:8765`. |
| `DASHBOARD_MOBILE_GPS_ENABLED`, `MOBILE_GPS_STATIC_FALLBACK_ENABLED` | Browser GPS acceptance and optional fixed-observer fallback. |
| `TELEGRAM_NOTIFICATIONS_ENABLED`, token/chat settings, `TELEGRAM_SUN_ENABLED`, `TELEGRAM_MOON_ENABLED` | Optional notifications and independent startup body switches. |
| `AUTHORITATIVE_PREDICTION_GEOMETRY`, `FLEET_GEOID_PGM_PATH` | Geometry mode and geoid grid. |
| `GEOMETRIC_ALTITUDE_SELECTION_ENABLED`, `FLEET_GEOMETRIC_ALTITUDE_ENABLED` | Opt-in altitude selection and fleet diagnostics. |
| `DASHBOARD_HISTORY_ENABLED`, `DASHBOARD_HISTORY_DIR` | Persistent event history. |

The custom fixed location is saved via the existing internal MANUAL compatibility
storage at `DASHBOARD_SETTINGS_PATH` (default `recordings/dashboard_settings.json`).
The UI still calls it STATIC. Restart restores the saved coordinates but startup
mode, source, fallback and Telegram switches follow configuration. See the
[technical configuration reference](docs/standard-technical-reference.md#configuration-reference)
for defaults and additional options, and the [recording reference](docs/standard-technical-reference.md#recording-sessions)
for `--record` and Candidate Auto-Recorder behavior.

## Aircraft sources and local feeds

| Mode | Behavior |
|---|---|
| LOCAL | Uses configured local receiver/feed data. |
| INTERNET | Uses ADSB.lol data through the common authoritative prediction/lifecycle path. |
| AUTO | Fuses eligible LOCAL and ADSB.lol fields with freshness, datum and provenance checks; it is not merely a whole-source fallback. |

Typical local ports are ADS-B/SBS **30003** and MLAT SBS-compatible **30106**.
Hosts and ports are configurable; they refer to your receiver/feed installation.
Optional RAW text **30002**, Beast intent **30005**, and synthetic MLAT Beast
**30105** have distinct precision/intent roles. Beast is not the INTERNET provider.
Set `ADSB_TIMESTAMP_TIMEZONE` to the source's timezone for naive SBS timestamps;
30106 timestamps use UTC. See [detailed source policy](docs/standard-aircraft-sources.md)
and [precision inputs](docs/standard-technical-reference.md#precision-track-selection).

An INTERNET query uses the explicitly configured fixed observer as its query
centre. Requested MOBILE blocks provider HTTP, including when STATIC fallback is
active. This privacy rule also applies to AUTO's remote acquisition.

## Dashboard and observer modes

The top navigation is **LIVE | HISTORY**. Backend state and Controls each start as
one compact clickable row with muted typography and state accents. Expand Backend
state for transport, generated timestamp, last refresh and source diagnostics.
Expand Controls for:

- **Observer STATIC | MOBILE**, relevant GPS action and fallback setting.
- **STATIC** default configured location or **Change location** for a custom fixed
  location; **Use default location** restores the configured origin.
- **MOBILE** GPS freshness/accuracy and effective-observer diagnostics.
- **Aircraft source LOCAL | INTERNET | AUTO** and source diagnostics.
- Independent **Telegram SUN/MOON** switches and API/revision details.

The interface adapts to desktop and phone widths. Collapsing hides controls,
not their state: location drafts and an active GPS watch are retained. Generated
backend health is distinct from realtime/reconnecting transport state.

### MOBILE needs a secure browser context

A plain LAN HTTP address can display the dashboard while browser geolocation is
unavailable or denied. Use **HTTPS**, for example a properly configured
[Tailscale Serve secure origin](https://tailscale.com/docs/features/tailscale-serve).
Tailscale is optional; ordinary authenticated/private HTTPS deployment is also
possible. Localhost/loopback on the browser's own device is a development
exception, not a way to enable GPS on another phone over LAN HTTP. See
[secure-context requirements](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Secure_Contexts/features_restricted_to_secure_contexts).
Do not bypass certificate or browser security checks.

Select MOBILE and grant browser permission. A running watcher is not a fix:
**MOBILE_NO_FIX** means the backend has not accepted a valid current browser
position; **MOBILE_FRESH** confirms accepted fresh position state. Stop GPS clears
the mobile fix; selecting STATIC also stops that browser's watch. Reload requires
a new user action to start acquisition. Phone altitude is diagnostic-only;
configured observer elevation remains authoritative. With fallback disabled,
stale accepted positions may remain effective as MOBILE_LAST_KNOWN; no-fix
without fallback withholds new predictions. Optional fallback uses STATIC.

Normal dashboard payloads, history, Telegram and snapshots do not expose MOBILE
coordinates. The dashboard has no built-in authentication: keep it bound to
localhost or behind an appropriate private/authenticated access layer.

## Notifications, history and recording

Telegram is optional. Configure private token/chat values in `.env`, enable the
notification channel, then control SUN and MOON independently. LOCAL, INTERNET
and AUTO use the same eligibility, stabilization and notification lifecycle.
[Source handoff suppression](docs/standard-telegram-source-handoff.md) reduces
duplicate alerts. Queue acceptance is **not** proof of Telegram network delivery.
Turning notifications off does not turn prediction or history off.

HISTORY retains eligible completed/withdrawn/notification-triggered events with
filters, pagination and CSV export; not every brief prediction is history-worthy.
[Finalization](docs/standard-finalization.md) retains authoritative lifecycle
ownership. Full-session recording (`--record`) and Candidate Auto-Recorder are
separate capture mechanisms. They record local receiver evidence; ADSB.lol data
are not fabricated into SBS/Beast receiver recordings. See the
[recording and snapshot reference](docs/standard-technical-reference.md#recording-sessions).

### Replay

Replay uses deterministic replay/local input and is isolated from live
INTERNET/AUTO acquisition. The configured source preference remains available for
normal operation; replay does not mix recordings with live ADSB.lol. Replay uses
STATIC observer semantics. Supply your own SBS recordings and follow the
[replay instructions and limitations](docs/standard-technical-reference.md#replay)
in a separate test setup, not alongside production receivers.

## TRUE_2D and performance architecture

TRUE_2D solves the closest angular approach to the moving Sun/Moon using
WGS84 ECEF/ENU aircraft geometry, EGM96 height conversion and pressure-zero
(topocentric, airless) celestial geometry. Missing required geometry does not
silently fall back per event to LEGACY. See the
[geometry reference](docs/standard-technical-reference.md#motion-freshness-and-prediction).

Live LOCAL TRUE_2D solving is [deferred off the ingest hot path](docs/standard-deferred-prediction.md).
[Public-state serialization](docs/standard-public-state-publisher.md) coalesces
updates with bounded work. Source/lifecycle ownership guards prevent stale results
from resurrecting replaced or finalized candidates. These are architecture
properties, not guaranteed throughput or latency benchmarks.

## Deployment and development

For a generic optional systemd example, see [Linux deployment](docs/standard-installation.md).
Tmux is optional, not an application requirement. Never replace a production unit
without adapting its interpreter, paths, permissions and shutdown timeout.

Developer validation, from the repository root with dependencies installed:

```sh
.venv/bin/python -m unittest discover -s tests
cd web
npm test
npm run typecheck
npm run build
```

For a non-venv Python 3.11 installation, use
`python3.11 -m unittest discover -s tests`. Plotting/diagnostic tests need the
packages in `requirements.txt`. Development-only Vite/proxy instructions are in
[web/README.md](web/README.md); the production server remains Python.

## Documentation and validation status

- [Installation and optional systemd](docs/standard-installation.md)
- [Technical geometry, altitude, recorder, replay and configuration reference](docs/standard-technical-reference.md)
- [Aircraft source policy](docs/standard-aircraft-sources.md)
- [Deferred prediction](docs/standard-deferred-prediction.md) and [public-state publication](docs/standard-public-state-publisher.md)
- [Telegram handoff](docs/standard-telegram-source-handoff.md) and [finalization](docs/standard-finalization.md)
- [Release smoke-test results and checklist](docs/standard-release-smoke-test.md)
- [Offline ADSB.lol validation](tools/adsblol_historical_validate.md) and [isolated acquisition probe](tools/adsblol_live_probe.md)

The promoted release was operator-validated on Debian/Python 3.11.15 with real
geoid data, ADS-B/MLAT, all source modes, STATIC/custom persistence, Android MOBILE
through a secure Tailscale origin, independent Telegram controls, LIVE/HISTORY and
the Galaxy S23 UI. Candidate-dependent delivery/finalization scenarios are not
claimed unless separately observed; see the recorded scope in the checklist.

STANDARD remains under development. Some precision recordings do not yet have
full replay support, phone altitude remains diagnostic-only, and deterministic
production geometry excludes atmospheric refraction. No repository license file
is present at this checkpoint; no additional licensing grant is asserted here.
