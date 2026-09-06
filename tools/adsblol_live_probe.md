# A4.2 isolated live acquisition diagnostics

This opt-in tool implements the [A4.1 field contract](adsblol_live_contract.md).
It does not read installation/observer settings, import the runtime, populate
aircraft state, run geometry, create candidates, or update the map/dashboard.
It cannot yet be used to test live Transit Warning predictions in another region.
It acquires and inspects potential inputs only.

## Usage

From the repository root, with the existing Python requirements installed:

```powershell
python tools/adsblol_live_probe.py --observer-mode STATIC --lat <authorized-latitude> --lon <authorized-longitude> --radius-nm 100
python tools/adsblol_live_probe.py --observer-mode MANUAL --lat <authorized-latitude> --lon <authorized-longitude> --radius-nm 100 --json-output <new-private-report.json>
python tools/adsblol_live_probe.py --icao 48AE25 --retries 0
python tools/adsblol_live_probe.py --icao 48AE25 --count 3 --poll-seconds 10
```

Supplying a location explicitly authorizes sending that query centre to ADSB.lol;
no location is inferred from the running application. Default observer mode is
STATIC. Callers must pass the **requested** mode, never an effective STATIC
fallback in place of requested MOBILE. MOBILE returns PRIVACY_BLOCKED before
coordinate inspection, URL construction or HTTP; its report contains no query
centre. Unknown modes are rejected. ICAO and geographic queries are exclusive.

The radius is an integer in nautical miles, capped at 250; negatives/fractional
radii and invalid coordinates are rejected. The JSON records the actual radius
and whether it was capped. The geographic centre is included in the optional
private JSON but not echoed by the console summary. Do not publish that file
without reviewing location/aircraft sensitivity and A4.1's licensing constraints.
Existing output files are never overwritten.

Default is one acquisition (which can include one transient retry), not a
continuous service. Use `--retries 0` for exactly one HTTP attempt.
Exit codes: 0 successful, 1 acquisition/privacy failure, 2 invalid CLI/output
error, 130 interruption.

## Acquisition and failure behavior

- TLS certificate verification is always enabled. Redirects are not followed.
  No credentials, runtime settings or recorder data are sent.
- `--timeout-seconds` defaults to 10. It is Requests' connection/read-inactivity
  timeout, not a total wall-clock deadline against a continuously trickling body.
- `--retries` accepts 0..3. Timeouts, connection failures, HTTP 429 and 5xx
  receive bounded backoff with jitter; TLS/authentication/malformed data and
  other HTTP errors are not automatically retried within acquisition.
- Retry-After supports seconds and HTTP-date. Retry delays start at 10 seconds
  and grow exponentially; a delay over 60 seconds is returned as diagnostic
  metadata rather than waited on inside the provider.
- Optional finite CLI polling waits at least `--poll-seconds` (minimum 10)
  after completion, and respects any longer retry delay. It stops on permanent
  failures. Only one request runs at a time, with no catch-up queue.
  These are diagnostic pacing rules, **not stale thresholds or promised API quotas**.
- Ctrl+C stops the CLI; the provider also accepts a cancellation event for
  interruptible retry waits and discards a response if cancelled while in flight.
  The synchronous HTTP call itself finishes or times out before event cancellation
  is observed.
- HTTP 200 is insufficient: successful v2 envelope, matching aircraft count,
  valid identities and millisecond timestamps are required. Malformed/error
  bodies never become an empty successful fleet. A valid empty `ac` is OK.
- Error messages are sanitized; raw transport exceptions can contain query URLs
  and are not printed or serialized.

Metadata records query, actual radius, observer mode, status/error, each attempt's
HTTP status and start/finish UTC, monotonic elapsed latency, aircraft count and
retry delay. The top-level request times describe the final attempt, not the
whole retry interval. Successful replies retain provider now/ctime (ms),
processing duration, snapshot UTC and a content hash. Local report schema is 1;
API contract is v2. Provider deployment/schema versions stay null because the
aircraft `version` field is ADS-B version, not API version.

## Normalized contract

`aircraft[].fields` contains observations, not selected/fused predictor inputs.
Each has:

- value; source ADSBLOL; native unit and datum/semantic reference;
- PRESENT/MISSING/INVALID/NOT_APPLICABLE availability, with ABSENT versus NULL;
- KNOWN/UNKNOWN/UNAVAILABLE freshness (KNOWN does **not** mean fresh);
- field age, observation estimate/basis, provider snapshot and local receipt;
- provenance with raw field, aircraft-level type, per-field MLAT/TIS-B flags,
  UNSPECIFIED derivation, and no invented confidence;
- quality references to independently normalized NIC/NAC/SIL/GVA/etc. observations.

Latitude/longitude are the atomic `position.value.lat/lon` pair. An incomplete
pair is unavailable; historical/rough positions remain in diagnostic extensions.
Non-ICAO addresses retain the tilde and separate namespace, with `icao=null`.
Unknown additive fields remain extensions, not predictor inputs.

Only position obtains `age_seconds=seen_pos` at PROVIDER_SNAPSHOT and the
position-update estimate `now_ms/1000 - seen_pos`. Track, altitude, GS, rates,
intent and headings retain UNKNOWN age. `aircraft_message_age.value` retains
`seen` and a separate latest-message estimate; it is not copied into field ages.
Quality observation times remain unknown; NIC/RC can reference position context.

Local receipt times differ from provider snapshot time. Apparent position age
at receipt is reported with unknown clock uncertainty; future-provider-clock
warnings and negative apparent ages are preserved, not silently clamped.
Repeated snapshots retain observation times even when receipt times change.
The epoch validation rejects seconds accidentally supplied as v2 milliseconds;
it does not classify data as fresh or stale.

Pressure altitude and geometric WGS84 HAE remain separate, in feet. Geometric
height is provider-declared, not independently qualified AMSL or direct GNSS.
No geoid/pressure conversion is performed. Ground is NOT_APPLICABLE altitude,
not numeric zero. MCP/FMS selected altitudes are intent with unspecified
reference; altimeter setting is not assumed local QNH. Magnetic/true/selected
headings and calculated/reported tracks remain distinct and unrounded.

## Validation and next boundary

Focused tests use synthetic fixtures and mocked HTTP only:

```powershell
python -m unittest discover -s tests -p test_adsblol_live.py
python -m unittest discover -s tests
python -m py_compile tools/adsblol_live.py tools/adsblol_live_probe.py tests/test_adsblol_live.py
```

The A4.2 manual probe failed certificate verification in the development
environment before receiving HTTP. No successful latency or live field
availability is claimed; TLS validation was not weakened.

Recommended A4.3: opt-in, offline/shadow comparison of normalized observations
against local recordings, with explicit clocks/datums and measured field
availability. Decide unknown-age eligibility and privacy separately before any
runtime provider adapter. Do not automatically turn A4.2 output into synthetic
SBS messages or wire it to the predictor/maps.
