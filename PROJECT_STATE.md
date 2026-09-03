# Transit Warning project state

This is a compact statement of current architecture. Code and tests remain
authoritative if this document becomes stale.

## Production geometry

- `AUTHORITATIVE_PREDICTION_GEOMETRY` accepts `LEGACY` or `TRUE_2D`; its
  repository default remains `LEGACY` for configuration compatibility.
- Production deployment is currently configured for `TRUE_2D`.
- TRUE_2D uses independent coarse screening followed by an exact spherical
  closest-approach solver. It does not require a legacy azimuth intersection.
- The winning exact result freezes propagated aircraft position, the vertical
  decision, final altitude, body geometry, separation, T0, and slant range.
- Aircraft finite-distance geometry uses datum-consistent WGS84 ECEF/ENU with
  EGM96 geoid conversion. Sun/Moon geometry is deterministic, topocentric,
  geometric, and pressure-zero (airless).
- LEGACY remains selectable. TRUE_2D does not silently fall back per event to
  LEGACY when exact refinement fails.

## Authoritative prediction and lifecycle

- Encounter identity is observer epoch + ICAO + body + generation; predicted
  T0 drift does not change identity.
- Explicit transitions are `OPENED`, `UPDATED`, `HELD`, `WITHDRAWN`, and
  `NONE`.
- Only a future exact `INTERIOR` result can open an encounter. Boundary-only
  results cannot open one. `HELD` preserves the last success during grace but
  is not a fresh prediction and does not refresh grace.
- TRUE_2D consumers have been migrated end-to-end: terminal T0/countdown/SEP,
  ordering and colors; dashboard LIVE and HISTORY; Telegram stabilization and
  qualification; gong; snapshots; history CSV geometry marker; and slant range
  at T0_2D. Legacy-only `p2x`/`h2x` display as `---` in TRUE_2D.

## Observer modes

- STATIC and browser-supplied MOBILE latitude/longitude are implemented.
- The observer context is immutable and frozen for each prediction operation.
- MOBILE state is runtime-only. Phone altitude remains diagnostic-only; the
  configured static elevation is used by the solver.
- Normal persistent outputs do not expose mobile latitude/longitude. Replay is
  forced to STATIC semantics.
- Process restart discards mobile position and runtime observer/Telegram
  switches, restoring configured defaults.

## Dashboard and Telegram

- The dashboard provides LIVE and HISTORY, bounded history queries, CSV export,
  health state, body positions, observer status, and runtime body-specific
  Telegram controls.
- Terminal and dashboard presentation thresholds are independently configured.
- Telegram is an early attention channel with its own threshold, horizon,
  stabilization, deduplication, and SUN/MOON switches. It is not the live UI.
- Observer coordinates are not exposed by normal dashboard or Telegram paths.

## Snapshots and history

- Snapshot schema v3 remains current and older supported records remain
  readable.
- TRUE_2D snapshots use the exact propagated position and frozen vertical
  state, not reconstructed legacy-T0 state.
- Geometry provenance is persisted as `LEGACY` or `TRUE_2D`.
- Dashboard history may be persisted as daily JSONL and exported as CSV; mobile
  coordinates are excluded.

## Recorder

- The existing manually controlled full recorder captures broad ADS-B SBS,
  MLAT SBS, RAW ADS-B, and optional MLAT Beast streams using production-owned
  connections. Manifests drive verified `streams.zip` membership.
- `streams.zip` is the canonical completed archive. Failed finalization
  preserves loose inputs.
- Candidate Auto-Recorder Phases 1 and 2 are implemented in
  `candidate_recorder.py` and remain dormant: they are not connected to
  production stream readers or disk output.
- Phase 1 provides:
  - per-ICAO bounded in-memory pre-buffering;
  - ADS-B SBS and MLAT SBS demultiplexing;
  - RAW AVR attribution using existing repository decoding helpers;
  - MLAT Beast framing across arbitrary TCP chunk boundaries;
  - filtering before retention;
  - ~60 s configurable pre-buffer;
  - bounded per-ICAO record count and total ICAO count;
  - stale ICAO pruning;
  - thread safety;
  - no disk writes.
- RAW/Beast attribution is intentionally conservative:
  - accept only repository-supported attributable DF17 and DF18 CF=2 frames with
    valid CRC;
  - reject DF11, DF19, anonymous DF18, AP-overlaid frames, Mode A/C, and
    corrupt/unattributable frames.
- Phase 2 adds authoritative encounter gating and in-memory state management:
  - forensic identity comes directly from the existing
    `prediction.encounter_id` and state is per authoritative encounter;
  - conceptual physical capture state is per ICAO;
  - only a future TRUE_2D `INTERIOR` prediction can trigger;
  - default gates are T0 <= 300 seconds and SEP <= 2.0 degrees;
  - the Phase 1 pre-buffer provides approximately 60 seconds before trigger;
  - required end is authoritative T0 + 180 seconds;
  - a later T0 can extend the window and an earlier T0 cannot shorten it;
  - `WITHDRAWN` records the outcome without cancelling a triggered window;
  - there is no ICAO cooldown: a new generation is a new forensic encounter;
  - overlapping SUN/MOON encounters remain distinct while sharing ICAO capture;
  - `capture_until` is the maximum required end of unfinished encounters.
- Adversarial review found and resolved:
  - future timestamp poisoning;
  - out-of-order pruning;
  - overly broad DF18/DF11/DF19 attribution;
  - stale-before-active eviction;
  - mutable metadata;
  - naive/non-UTC timestamps.
- Phase 2 is in-memory only. It performs no candidate socket-reader integration,
  physical writes, directory creation, production-runtime integration, or
  FULL-recorder suppression/marker handling. Existing FULL recording and all
  prediction consumers remain unchanged.
- The manually started FULL recorder has priority and Candidate Recorder logic
  must never stop or interfere with it. Future integration must avoid duplicate
  physical capture while FULL recording is active; covered candidate encounters
  should create only a lightweight marker/index referencing the FULL session
  and relevant time window.
- A private forensic candidate bundle may retain its frozen observer context,
  including MOBILE coordinates, solely because later photo reconstruction
  requires it. Such coordinates are private forensic data and must stay out of
  ordinary logs, dashboard, Telegram, HISTORY, public CSV, and other normal
  diagnostics/output.

## Testing and runtime

- Production Debian uses `/usr/local/bin/python3.11`; Windows development may
  use a different interpreter and dependency/data installation.
- Where pytest is unavailable, the established full runner is
  `python -m unittest discover -s tests`.
- A known flaky Windows dashboard HTTP teardown can raise
  `ConnectionResetError` during the full suite while passing in isolation; do
  not attribute it to unrelated work without evidence.
- Production auto-discovers the installed GeographicLib `egm96-15.pgm` grid.
  Do not hardcode machine-specific geoid paths.

## Current rollout status

- Authoritative TRUE_2D is enabled on production and is in observation/soak.
- Consumer migration is complete.
- The TMUX table cleanup is complete; the table ends at `age` and uses a
  privacy-safe observer footer.

## Planned next work

The next checkpoint is **Candidate Auto-Recorder Phase 3A — dormant runtime
wiring without disk output**. It should connect production SBS/RAW/MLAT inputs
to `CandidatePreBuffer` and authoritative lifecycle transitions to
`CandidateEncounterManager`, while remaining observational and fail-open. It
must not yet create candidate bundles/writers or implement FULL-recorder
suppression/markers, and must not change prediction, dashboard, Telegram,
history, snapshots, gong, or full-recorder behavior.
