# STANDARD ADSB.lol diagnostic field contract

This document describes the contract implemented by [adsblol_live.py](adsblol_live.py)
and consumed by the [isolated probe](adsblol_live_probe.md). It documents local
normalization, not a provider availability guarantee or permission to redistribute
provider data. Review applicable provider terms before distributing acquired data.
No remote acquisition was required for the STANDARD release checks.

## Envelope and identity

The provider uses the v2 envelope; `now` and `ctime` are millisecond timestamps.
The adapter validates success/error state, aircraft count and identities before
accepting a snapshot. An empty valid `ac` list is successful; malformed replies
are errors. Aircraft `version` describes ADS-B version, not API/schema version.
Non-ICAO addresses retain their tilde and namespace with `icao=null`.
Unknown additive fields remain diagnostic extensions.

## Field meaning

| Provider evidence | Normalized meaning | Constraint |
|---|---|---|
| `lat`, `lon` | Atomic geographic position | Both coordinates must be valid |
| `seen_pos` | Position age at provider snapshot | KNOWN age does not imply freshness |
| `seen` | Latest aircraft message age | Never copied to individual field ages |
| `alt_baro` | Pressure altitude in feet | Separate from geometric altitude; ground is not zero |
| `alt_geom` | Provider-declared WGS84 HAE in feet | Not independently qualified AMSL or direct GNSS |
| `gs`, `track` | Knots and true-north ground track degrees | Individual observation ages remain UNKNOWN |
| `baro_rate`, `geom_rate` | Pressure/geometric vertical rates, ft/min | Distinct semantics; age remains UNKNOWN |
| MCP/FMS selected altitude and `nav_qnh` | Intent and selected altimeter setting | Not measured altitude or an assumed local QNH |
| Heading fields | Magnetic, true or unspecified reference | Kept separate from ground track |
| NIC/NAC/SIL/GVA and related quality | Independent quality observations | No invented confidence or observation timestamp |

Each normalized observation carries availability (PRESENT/MISSING/INVALID/
NOT_APPLICABLE), missing reason (including absent versus null), freshness
(KNOWN/UNKNOWN/UNAVAILABLE), unit/reference, provider/local timestamps and
provenance. Provenance retains raw field, aircraft type and per-field MLAT/TIS-B
flags. Position observation time is estimated from provider time minus `seen_pos`.
Repeated receipt does not turn old evidence into a fresh measurement. Cross-clock
offsets are diagnostic, with uncertainty preserved. The probe does not select
predictor inputs, convert pressure/geoid datums, or generate synthetic SBS.

## Acquisition and privacy

Only explicit STATIC/internal MANUAL geographic queries or ICAO queries are
accepted. Requested MOBILE is blocked before coordinate inspection, URL creation
or HTTP, even if its effective position is a static fallback. Geographic and ICAO
queries are exclusive. Radius is an integer in nautical miles, capped at 250.
TLS verification is required; redirects are disabled. Retries and polling are
bounded as described in the probe documentation. No credentials or runtime
observer configuration are read.

The console summary omits the geographic centre. Optional private JSON includes
an explicitly authorized query centre and must be reviewed before sharing. Error
text is sanitized; raw request exceptions can reveal query URLs. Output files are
never overwritten. Runtime eligibility, leases, AUTO fusion and Telegram behavior
are separate policies documented in [STANDARD aircraft sources](../docs/standard-aircraft-sources.md).
