# STANDARD technical reference

See the [public quick start](../README.md) for installation and the current UI.
This reference retains the detailed geometry, receiver, recorder and configuration
material separately from the landing page. Run commands from the repository
root; `python` below means the configured deployment/venv interpreter with
`requirements.txt` installed.

## Motion freshness and prediction

Fully fresh motion currently requires position age at most 3 seconds, parameter
age at most 5 seconds, and position-to-track/groundspeed timestamp deltas at most
3 seconds. Older or incoherent input is treated conservatively.

The horizontal model propagates constant track and groundspeed on a spherical
great-circle path. The solved finite-distance aircraft line of sight is then
computed with WGS84 geodetic-to-ECEF and observer-local ENU geometry. MSL-like
observer and aircraft heights are converted to WGS84 ellipsoidal height with
the local EGM96 undulation before that calculation. LEGACY can report missing
geoid data and use its explicit flat LOS fallback. Authoritative TRUE_2D requires
datum-consistent geometry: missing geoid data or an exact-solve failure withholds
the affected prediction, without a per-event fallback to LEGACY. The
moving-body solver iterates against the changing Sun or Moon. Vertical
prediction uses vertical-rate history and the existing LEVEL, DYNAMIC,
DEGRADED, and ignored/stale policies. Fresh TC29 selected altitude may clamp
extrapolation; it is intent, not measured altitude.

Internal solutions extend to 15 degrees vertical separation and approximately
900 seconds. A three-second prediction grace absorbs brief input jitter.
In authoritative `TRUE_2D` mode, production T0 and SEP are the spherical
closest-approach time and angular separation. `LEGACY` mode remains available
for compatibility and uses the older azimuth-intersection event semantics.

Sun/Moon prediction uses topocentric geometric (airless) PyEphem coordinates
with observer pressure explicitly zero. This preserves lunar parallax and keeps
celestial direction consistent with geometric aircraft line of sight.
Atmospheric refraction is not part of deterministic production geometry.

## Precision track selection

Effective track priority is:

1. eligible RAW ADS-B TC19 precision;
2. for an MLAT-positioned aircraft, eligible confirmed MLAT Beast TC19 precision;
3. timestamped SBS/MLAT coarse track;
4. legacy fallback only when no timestamped track exists.

Precision is fresh for 5 seconds. It can be held for at most 20 seconds while a
fresh coarse track remains compatible. Circular difference up to 1.5 degrees is
compatible; at least 3 degrees rejects immediately; intermediate disagreement
invalidates after two distinct coarse updates.

### RAW ADS-B 30002

The RAW reader decodes fractional TC19 ground track for the matching ICAO.
Schema-v3 snapshots retain effective/coarse provenance, timestamps, fresh/held
state, and rejection diagnostics. Recording stores exact RAW lines plus a JSONL
TC19/TC31 diagnostic journal.

`--raw-diagnostics-replay` deterministically restores recorded TC19
GNSS-minus-baro and TC31 version/datum events. It does **not** replay RAW
fractional track from the raw stream.

### MLAT Beast 30105

Port 30105 supplies synthetic Beast precision; port 30106 remains the MLAT
position/coarse-motion source. A 30105 track is eligible only for an
MLAT-positioned aircraft after timing/truncation compatibility with fresh 30106.
RAW ADS-B precision retains priority.

Recording stores exact 30105 binary bytes and a receipt-time JSONL journal,
declared in the manifest and verified in `streams.zip`. MLAT Beast deterministic
replay is not implemented.

## Geometric altitude selection

SBS altitude is treated as pressure/barometric altitude, QNH-corrected in feet,
then converted to metres. Vertical-rate prediction and TC29 intent remain in
their barometric domain.

When `GEOMETRIC_ALTITUDE_SELECTION_ENABLED=true`, line-of-sight geometry uses:

1. `OWN_GNSS_GEOMETRIC`: fresh aligned TC19 plus TC31-qualified WGS84 HAE,
   converted with EGM96;
2. `FLEET_GEOMETRIC`: estimate from other qualified aircraft, accepted only at
   `HIGH` or `MEDIUM` confidence;
3. `BARO_QNH`: safe fallback.

TC19 difference is applied to predicted pressure altitude, never to an already
QNH-corrected altitude. The fleet estimator excludes the target and weights
contributors by age, horizontal distance, and altitude difference. Missing
geoid data, stale/ambiguous input, insufficient contributors, or low confidence
falls safely to `BARO_QNH`. `FLEET_GEOMETRIC_ALTITUDE_ENABLED` enables additive
fleet diagnostics; `FLEET_GEOID_PGM_PATH` may identify a GeographicLib EGM96
PGM, otherwise supported standard locations are searched. The same geoid grid
also supplies the observer and target datum conversion for WGS84 ECEF/ENU LOS;
phone altitude remains diagnostic-only.


## Recording sessions

```console
python transit_warning.py --record
```

Optional session members are manifest-driven:

```text
recordings/sessions/YYYYMMDD_HHMMSS/
├── manifest.json
├── streams.zip
├── adsb_<port>.log
├── mlat_<port>.log
├── raw_<port>.log
├── raw_<port>_events.jsonl
├── mlat_beast_<port>.bin
└── mlat_beast_<port>_events.jsonl
```

The production readers pass the exact same SBS/RAW line or Beast bytes to the
recorder and decoder; no duplicate source connection is opened. Writers fail
independently. Controlled shutdown closes writers, writes final manifest counts,
builds a temporary ZIP, verifies members/counts/CRC, then atomically renames it.
Complete sessions remove loose streams only after verification; partial/failed
sessions preserve them.

**`streams.zip` is the authoritative completed artifact.** Loose files can be
incomplete or prefix-only after interruption. For replay/forensics: validate and
prefer ZIP members; use loose files only if ZIP is absent/corrupt/incomplete;
report that fallback explicitly; never silently combine archive and loose data.

Accepted QNH is always recorded separately in daily UTC files under
`recordings/environment/`; midnight rotation carries state forward. Environment
files are not session ZIP members.

### Candidate Auto-Recorder

The Candidate Auto-Recorder maintains bounded per-aircraft pre-buffers and can
retain focused private forensic windows for qualifying authoritative TRUE_2D
encounters. Distinct Sun/Moon encounters can share one physical per-aircraft
capture without duplicating stream bytes. Storage is asynchronous and fail-open
so candidate filesystem failures do not interrupt surveillance or prediction.

When a manually started FULL recorder already covers every required candidate
stream, it has priority. Candidate recording creates a lightweight
`FULL_REFERENCE` encounter marker referring to that full session instead of
duplicating broad physical capture. If complete FULL coverage is unavailable,
the normal candidate bundle path remains active. Candidate logic never starts,
stops, or controls the FULL recorder.

## Replay

```console
python transit_warning.py --clock replay \
  --environment-replay path/to/environment.jsonl
```

`replay_server.py` supports ADS-B, MLAT, and dual SBS scenarios at `1`, `10`,
`100`, or `max` speed. Environment is optional; without historical data the
1013 fallback remains. Existing log formats and ReplayClock stay unchanged.

```console
python replay_server.py adsb-2026 --file path/to/adsb.log --speed 100
python replay_server.py mlat-2024 --file path/to/mlat.log --speed 100
```

`--raw-diagnostics-replay` restores RAW altitude/version diagnostic events, not
precision track. Scenario defaults refer to local test recordings, not bundled
deployment inputs.
Supply your own recording with `--file` for single-source replay.
Automatic manifest/ZIP session replay and MLAT Beast replay are
not implemented.

## Snapshots and diagnostics

Schema-v3 snapshots capture trigger/update/final history, frozen solver input,
intersection geometry, angular body size, vertical/intent state, effective track
and precision provenance, GNSS/fleet altitude diagnostics, and privacy-safe
observer source metadata. Mobile coordinates are omitted. Older schemas remain
accepted where supported by the offline visualizer.

SIGUSR1 can request a full terminal-table text snapshot without the normal
screen-height/range limit. Render transit JSON offline with:

```console
python tools/transit_snapshot_visualizer.py snapshot.json \
  --zoom 3 --show-production-path --output transit.png
```

The default plot uses unrounded diagnostic observer geometry while preserving
stored production values. Offline HIT/EDGE/MISS does not affect live behavior.

An optional true-2D shadow pipeline can independently screen and refine
spherical Sun/Moon closest approaches without requiring the legacy azimuth
intersection. Enable it with `SHADOW_2D_ENABLED=true`. Defaults are a 900-second
horizon, 60-second coarse segments, local 15-second subdivision, a 7-degree
refinement target, and a 0.052-degree conservative screening margin.

Coordinate-free comparisons are rate-limited below
`diagnostics/shadow_2d/YYYY-MM-DD/`. Shadow results remain diagnostic and do
not replace the configured authoritative prediction.

`AUTHORITATIVE_PREDICTION_GEOMETRY` selects `LEGACY` or `TRUE_2D` and defaults
to `LEGACY` for compatibility. In `TRUE_2D` mode the authoritative lifecycle
and its terminal, dashboard, Telegram, history, snapshot, and candidate-recorder
consumers use exact 2-D T0/SEP semantics; an exact failure does not silently
fall back to LEGACY for that event.


## Configuration reference

Required/static:

| Variable | Default | Purpose |
|---|---|---|
| `OBSERVER_LAT`, `OBSERVER_LON`, `OBSERVER_ELEVATION_M` | required | Static observer coordinates/elevation |
| `TRANSITION_ALTITUDE_FT` | required | Positive reserved operational value |
| `ADSB_TIMESTAMP_TIMEZONE` | required | IANA timezone of naive 30003 timestamps |
| `METAR_STATION` | required | Four-letter AWC station |

Inputs and altitude:

| Variable | Default | Purpose |
|---|---|---|
| `ADSB_HOST`, `ADSB_PORT` | `127.0.0.1`, `30003` | ADS-B SBS |
| `MLAT_HOST`, `MLAT_PORT` | `127.0.0.1`, `30106` | MLAT SBS-compatible |
| `RAW_ADSB_HOST`, `RAW_ADSB_PORT` | `127.0.0.1`, `30002` | RAW precision/diagnostics |
| `BEAST_HOST`, `BEAST_PORT` | installation-specific, `30005` | TC29 intent |
| `MLAT_BEAST_ENABLED` | `false` | Enable MLAT precision input/recording |
| `MLAT_BEAST_HOST`, `MLAT_BEAST_PORT` | `MLAT_HOST`, `30105` | MLAT Beast endpoint |
| `GEOMETRIC_ALTITUDE_SELECTION_ENABLED` | `false` | Enable OWN → FLEET → BARO selection |
| `FLEET_GEOMETRIC_ALTITUDE_ENABLED` | `false` | Enable fleet diagnostics |
| `FLEET_GEOID_PGM_PATH` | empty | Optional EGM96 PGM path |

Telegram/presentation:

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_NOTIFICATIONS_ENABLED` | `false` | Outgoing alerts |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty/private | Credentials |
| `TELEGRAM_ALERT_SEPARATION_DEG` | `2.0` | Alert threshold |
| `TELEGRAM_ALERT_HORIZON_SECONDS` | `300` | Alert horizon |
| `TELEGRAM_ALERT_STABILITY_SECONDS` | `5` | Stable eligibility |
| `TELEGRAM_SUN_ENABLED`, `TELEGRAM_MOON_ENABLED` | `true` | Startup body-specific alert switches |
| `TMUX_SEP_GREEN_MAX_DEG`, `TMUX_SEP_YELLOW_MAX_DEG`, `TMUX_SEP_VISIBLE_MAX_DEG` | `3`, `5`, `7` | Terminal colors/visibility |

Dashboard/observer:

| Variable | Default | Purpose |
|---|---|---|
| `DASHBOARD_ENABLED` | `false` | Start dashboard |
| `DASHBOARD_HOST`, `DASHBOARD_PORT` | `127.0.0.1`, `8765` | Bind endpoint |
| `DASHBOARD_HISTORY_ENABLED` | `true` | Persistent history |
| `DASHBOARD_HISTORY_DIR` | `recordings/dashboard_history` | History path |
| `DASHBOARD_SETTINGS_PATH` | `recordings/dashboard_settings.json` | Last valid MANUAL observer position |
| `DASHBOARD_MOBILE_GPS_ENABLED` | `false` | Accept browser fixes |
| `DASHBOARD_MOBILE_GPS_FRESH_SECONDS` | `15` | Fresh/fallback boundary |
| `OBSERVER_MODE` | `STATIC` | Startup mode |
| `MOBILE_GPS_STATIC_FALLBACK_ENABLED` | `false` | Static fallback |
| `MOBILE_GPS_STALE_WARNING_SECONDS` | `30` | Orange age warning |
| `MOBILE_GPS_CRITICAL_WARNING_SECONDS` | `300` | Red age warning |
| `DASHBOARD_SEP_GREEN_MAX_DEG`, `DASHBOARD_SEP_YELLOW_MAX_DEG`, `DASHBOARD_SEP_VISIBLE_MAX_DEG` | `3`, `5`, `7` | Dashboard colors/visibility |
| `NEW_TRANSIT_INDICATOR_ENABLED` | `true` | Mark late-discovered LIVE events |
| `NEW_TRANSIT_THRESHOLD_SECONDS` | `60` | Maximum time before T0 at first LIVE appearance |

Shadow diagnostics:

| Variable | Default | Purpose |
|---|---|---|
| `SHADOW_2D_ENABLED` | `false` | Run independent true-2D diagnostics |
| `SHADOW_2D_HORIZON_SECONDS` | `900` | Shadow search horizon |
| `SHADOW_2D_SEGMENT_SECONDS` | `60` | Coarse segment spacing |
| `SHADOW_2D_LOCAL_SEGMENT_SECONDS` | `15` | Local subdivision spacing |
| `SHADOW_2D_SAFETY_MARGIN_DEG` | `0.052` | Conservative coarse-pass margin |
| `SHADOW_2D_REFINEMENT_TARGET_DEG` | `7` | Independent exact-refinement target |
| `AUTHORITATIVE_PREDICTION_GEOMETRY` | `LEGACY` | Select `LEGACY` or authoritative `TRUE_2D` |

System environment overrides `.env`. MOBILE startup requires dashboard and GPS
enabled. Critical age must exceed warning age; presentation thresholds require
green < yellow < visible.



## Running and terminal presentation

```console
python transit_warning.py
```

The terminal prioritizes visible Sun/Moon candidates. Its independent defaults
are green below 3 degrees, yellow below 5, red below 7, and hidden from candidate
presentation at 7 or above. Solver, Telegram, gong, and snapshot thresholds are
separate. Ctrl+C performs controlled reader/recorder shutdown.

## Telegram notifications

Telegram is an optional attention channel, not the live UI.

```dotenv
TELEGRAM_NOTIFICATIONS_ENABLED=true
TELEGRAM_BOT_TOKEN=<private-bot-token>
TELEGRAM_CHAT_ID=<private-chat-id>
TELEGRAM_ALERT_SEPARATION_DEG=2.0
TELEGRAM_ALERT_HORIZON_SECONDS=300
TELEGRAM_ALERT_STABILITY_SECONDS=5
TELEGRAM_SUN_ENABLED=true
TELEGRAM_MOON_ENABLED=true
```

An accepted event must have `0 < time2X <= horizon` and remain below the
separation threshold for the stability interval. One notification is queued per
active ICAO/body event; countdown/improvement updates are not sent. Leaving
eligibility resets pending stabilization. Deduplication lasts through predicted
transit plus 60 seconds. Network delivery runs in a bounded background queue and
fails open.

SUN and MOON delivery can be switched independently from the dashboard. These
switches are runtime-only; restart restores `TELEGRAM_SUN_ENABLED` and
`TELEGRAM_MOON_ENABLED`. Suppression does not affect prediction or event history,
and it preserves notification deduplication when a body is turned back on.

```console
python transit_warning.py --test-notification
```

Credentials belong only in `.env`; observer coordinates are not sent.


## Versioned application API

The public App/Web foundation provides:

- `GET /api/v1/bootstrap`
- `GET /api/v1/stream` (SSE)
- `GET /api/v1/settings`
- `PATCH /api/v1/settings`

Schema-v1 bootstrap responses use explicit privacy-safe contracts and include
monotonic live and settings revisions. Runtime mutation uses one authoritative
settings store with expected-revision conflict handling and idempotent command
identifiers. The React frontend uses these settings controls and synchronized
updates. Ordinary API/frontend payloads expose explicitly configured custom STATIC
(internally MANUAL) observer values only; they do not expose MOBILE coordinates, Telegram
credentials, private recorder paths, or private forensic context.


## Custom STATIC persistence (internal MANUAL compatibility)

The internal MANUAL path uses runtime settings for latitude/longitude in decimal degrees and
observer elevation AMSL in metres. The saved MANUAL position remains available
when another mode is selected and becomes effective when the custom location
is applied again. The last complete, valid MANUAL position is stored in
`recordings/dashboard_settings.json` by default and restored after process
restart. Startup mode still comes from `OBSERVER_MODE`; when no saved MANUAL
position exists, Change location opens an empty editor and leaves the current
observer effective until a complete valid position is saved and activated.
Frontend reload/restart does not erase the saved custom location. The normal
React UI has no third MANUAL mode button.
Only these explicit MANUAL coordinates are exposed for dashboard editing;
MOBILE coordinates remain private. Raw GNSS altitude is not used as AMSL.

Other dashboard mode/fallback controls remain runtime-only. After restart,
mobile coordinates are gone, mode returns to configured `OBSERVER_MODE`,
fallback to its configured default, and no previous fix is restored.

### Mobile privacy

Mobile coordinates exist only in dedicated locked memory. They are not returned
by normal dashboard state or observer diagnostics, displayed, written to
history/CSV, sent to Telegram, recorded in stream/log files, or included in
mobile snapshots. Mobile snapshots contain only privacy-safe source, freshness,
accuracy, fallback, epoch, and configured-elevation metadata.


## Compatibility dashboard and event presentation

The optional dashboard reuses production predictions and does not run another
solver. It includes chronological LIVE Sun/Moon queues, responsive candidate
cards, local absolute-UTC countdowns, HISTORY with filters/bounded pagination,
CSV export, optional daily JSONL persistence, and ACTIVE/STALE/DISCONNECTED
health based on polling and the advancing main-loop heartbeat—not candidate
presence.

Dashboard presentation also defaults independently to 3/5/7 degrees. Passed,
near-event-withdrawn, and Telegram-triggered events are history-worthy; early
insignificant transients are discarded. Persistent-history failure is fail-open,
and `/api/state` never scans history files.

With `NEW_TRANSIT_INDICATOR_ENABLED=true`, a compact pulsing `NEW` badge marks
an event only when it first enters LIVE within
`NEW_TRANSIT_THRESHOLD_SECONDS` of its predicted T0 (60 seconds by default).
An event discovered earlier does not become NEW merely because its countdown
later crosses the threshold. The backend owns this state across browser reloads;
the badge ends at T0 and is never written to HISTORY.

The server defaults to disabled and localhost. Remote access should use a
trusted private transport such as Tailscale Serve/tailnet HTTPS. Do not use
public Funnel exposure without an appropriate security model. The dashboard
itself has no authentication.


## Repository layout

| Path | Purpose |
|---|---|
| `transit_warning.py` | Main ingestion, prediction, and runtime integration |
| `authoritative_transit.py` | LEGACY/TRUE_2D authoritative encounter lifecycle |
| `shadow_2d_prediction.py` | Independent coarse screening and exact 2-D solver |
| `live_dashboard.py` | Operational legacy dashboard and HTTP API host |
| `app_backend/` | Versioned public contracts, live-state, and runtime settings stores |
| `web/` | React + TypeScript LIVE/HISTORY interface and operational controls |
| `recording.py`, `replay_server.py` | Full-session recording and SBS replay |
| `candidate_recorder.py` | Candidate pre-buffer, bundles, and FULL_REFERENCE markers |
| `transit_snapshot.py`, `tools/` | Snapshot and offline diagnostic tooling |
| `tests/` | Python and integration regression tests |
| `web/src/test/` | React frontend tests |
