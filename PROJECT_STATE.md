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
- Candidate Auto-Recorder Phase 1 is implemented (`candidate_recorder.py`,
  `tests/test_candidate_recorder.py`) but remains dormant and is not connected
  to production runtime.
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
- Phase 1 does not:
  - integrate with authoritative TRUE_2D lifecycle;
  - create candidate sessions/manifests;
  - write forensic bundles;
  - persist observer coordinates;
  - alter the existing FULL recorder;
  - change TRUE_2D or LEGACY behavior.
- Adversarial review found and resolved:
  - future timestamp poisoning;
  - out-of-order pruning;
  - overly broad DF18/DF11/DF19 attribution;
  - stale-before-active eviction;
  - mutable metadata;
  - naive/non-UTC timestamps.
- Validation: Candidate Recorder tests (23 passed), recorder suites (61 passed),
  full repository suite (746 passed), py_compile, and git diff --check clean.
- Preserved two-layer model:
  - Physical stream capture is per ICAO: SBS, RAW, and MLAT data for an aircraft
    is physically captured only once. Multiple candidate encounters for the
    same ICAO share that physical capture rather than creating duplicate stream
    files/writers. Capture remains active until the latest required end time:
    `capture_until = max(required_end_time for active encounters)`.
  - Forensic event identity is per authoritative encounter: `(observer_epoch,
    icao, body, encounter_generation)`. Every encounter crossing the trigger
    gets its own forensic metadata/manifest. A later authoritative WITHDRAWN
    does not cancel or abort the forensic capture window once triggered; the
    outcome is recorded in metadata (e.g. WITHDRAWN, PASSED). New generations
    for the same ICAO are new candidates without cooldown suppression, and
    overlapping SUN/MOON encounters remain distinct forensic encounters sharing
    the single physical ICAO capture.
- Candidate window semantics: trigger expected near T0 <= 300 s and SEP <= 2
  deg (configurable). Coverage requires ~60 s pre-buffer before trigger and
  extends to authoritative T0 + 180 s (e.g. ~7 minutes total coverage if
  triggered 3 minutes before T0). If another encounter extends beyond the
  current physical capture end, `capture_until` is extended rather than opening
  a duplicate capture.
- The full recorder has priority. A future candidate recorder must avoid
  duplicate raw capture while full recording is active.
- A private forensic candidate bundle may retain its frozen observer context,
  including MOBILE coordinates, solely because later photo reconstruction
  requires it. Such coordinates must stay confined to private bundles.

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

The next checkpoint is Candidate Auto-Recorder Phase 2 design/implementation of
candidate encounter gating and state management, still without broad production
rollout.
