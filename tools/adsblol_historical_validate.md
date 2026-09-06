# Offline ADSB.lol historical validation

No server, live API, recorder, or prediction engine is started. Requires the
project Python dependencies (including matplotlib); Windows also needs IANA
timezone data (`tzdata`) for local SBS timestamps.

```powershell
python tools/adsblol_historical_validate.py --capture "<encounter manifest or capture directory>" --adsblol-trace "<trace_full_48ae25.json>" --sbs-timezone Europe/Warsaw --geoid-pgm "<egm96-15.pgm>"
```

For a physical capture containing multiple encounters, add
`--encounter-id "0:48AE25:SUN:3"`. Alternatively omit `--capture` and supply
`--recordings-dir <recordings root> --date 20260904 --icao 48AE25`.
Discovery requires exactly one matching candidate capture and never guesses
between logical encounters. Defaults select the benchmark date/aircraft, not a
machine-specific recording path. `--prediction latest|trigger` selects a stored
prediction, default latest. No prediction is reconstructed.

Generated output defaults to
`diagnostics/validation/adsblol/events/<date>_<icao>/`. Supply
`--output-dir <new directory>` to override it; explicit output directories may
be located anywhere.

Outputs in a **new** directory: `report.json` (schema version 1),
`comparison.csv`, and `trajectories.png`. Existing directories are refused.
These aircraft trajectories are offline artifacts; do not publish recordings
without reviewing their sensitivity. Observer context is never exported.

## Supported source semantics

- Candidate encounter manifests with standalone `adsb_sbs.log` and optional
  `raw_adsb.log` plus its `.timing.jsonl` sidecar. FULL-session reference-only
  captures are explicitly rejected, not silently misread.
- ADSB.lol/readsb trace arrays, including gzip detected by magic bytes even with
  a `.json` extension. The timestamp base plus row offset is UTC. Flags distinguish
  geometric from pressure altitude/rate; ground heading is not treated as track.
  See [readsb trace documentation](https://github.com/wiedehopf/readsb/blob/dev/README-json.md).
- SBS generated timestamps require an explicit timezone; ambiguous DST times
  fail. RAW fields use recorder receipt timestamps. Source clock/transport lag
  can therefore affect measured position differences; this is not automatically
  corrected or attributed to position accuracy.
- Local pressure altitude and GS come from SBS. RAW TC19 track and SBS track
  are reported separately; neither is a new runtime source-priority decision.
  TC19 rate source is retained; unspecified SBS rate stays separately labelled.
- Local geometric HAE is derived from TC19 GNSS-minus-baro plus interpolated SBS
  pressure altitude only after recorded ADS-B version 2 establishes its datum.
- Stored TRUE_2D final altitude is orthometric EGM96. Comparing it to ADSB.lol
  geometric HAE requires `--geoid-pgm`: HAE = final altitude + geoid undulation
  at the predicted **aircraft** point. Without it, this difference is unavailable.
  Predicted track/GS/rates are unavailable when the manifest lacks those fields.
- Linear interpolation uses actual numeric values; track/longitude use shortest
  circular arcs. No extrapolation, missing-value bridging, or gaps exceeding
  `--max-gap-seconds` (default 15). Stale/new-leg trace boundaries break bridging.
- `--window-seconds` defaults to 60 on either side of T0. Metrics use local
  position timestamps within that window with per-field valid sample counts.
  Median/P95/max are absolute differences; row deltas are signed local minus
  reference. Horizontal distance is diagnostic spherical great-circle distance,
  not a change to canonical prediction geometry. T0 observed and predicted rows
  are separate from overlap statistics. JSON null / CSV blank means unavailable.

Plot latitude/longitude axes show angular coordinates (not equal-distance map
axes). Altitude curves distinguish pressure and geometric HAE, with a converted
final-altitude marker when available; track is displayed wrapped 0–360.
