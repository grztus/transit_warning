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
  `candidate_recorder.py`. Phase 3A connects their in-memory machinery to live
  production input and authoritative lifecycle paths. It is observational,
  fail-open, and diskless rather than completely runtime-dormant.
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
- Phase 3A observes existing production paths without duplicating their
  ownership:
  - ADS-B SBS and MLAT SBS input feed `CandidatePreBuffer`;
  - RAW ADS-B input is observed from the existing reader path;
  - parsed MLAT Beast frames are observed from the existing parser path;
  - existing authoritative lifecycle transitions feed
    `CandidateEncounterManager`, including applicable observer invalidation and
    aircraft-removal transitions;
  - the main loop advances completion of in-memory forensic windows.
- No duplicate sockets, readers, or parsers were introduced. Candidate runtime
  hooks are fail-open so their exceptions cannot interrupt normal stream
  processing, prediction, authoritative lifecycle handling, or FULL recording.
  Phase 3A changed no prediction geometry, dashboard, Telegram, HISTORY,
  snapshot, gong, or FULL-recorder behavior.
- Phase 3A remains diskless: it creates no candidate files, directories,
  bundles, or writers. It emits no observer coordinates or candidate forensic
  data through ordinary logs, dashboard, Telegram, HISTORY, public CSV, or
  other normal diagnostics/output.
- The manually started FULL recorder has priority and Candidate Recorder logic
  must never stop or interfere with it. Future integration must avoid duplicate
  physical capture while FULL recording is active; covered candidate encounters
  should create only a lightweight marker/index referencing the FULL session
  and relevant time window.
- A private forensic candidate bundle may retain its frozen observer context,
  including MOBILE coordinates, solely because later photo reconstruction
  requires it. Such coordinates are private forensic data and must stay out of
  ordinary logs, dashboard, Telegram, HISTORY, public CSV, and other normal
  diagnostics/output. Phase 3A does not persist this context because it writes
  no bundles.

### Candidate Auto-Recorder Phase 3A validation

- Focused Candidate Recorder tests: 43 passed.
- Authoritative/recorder regression tests: 133 passed.
- Full suite: 766 run, 765 passed, with one known Windows dashboard HTTP
  teardown error; that test passed independently.
- `py_compile` passed.
- `git diff --check` passed.

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

The next checkpoint is **Candidate Auto-Recorder Phase 3B — physical
candidate-bundle writing/finalization and FULL-recorder-aware marker/index
handling**. It must preserve FULL recorder priority, fail-open isolation,
authoritative encounter identity, all Phase 1/2/3A semantics, and private
frozen observer-context rules. It must not leak private forensic data into
normal outputs. Phase 3B is not designed or implemented yet.
