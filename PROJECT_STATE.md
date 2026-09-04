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

## App/Web architecture

- Architecture Phase A0 accepted a gradual shared architecture for the current
  dashboard and future web/mobile clients. React + TypeScript is planned for
  the shared frontend, with PWA delivery and optional native packaging. Transit
  Warning core and server/runtime state remain authoritative.
- Phase A1 is complete at commit `355be81`. The `app_backend` package provides
  privacy-safe schema-v1 contracts, a thread-safe immutable
  `ApplicationStateStore`, monotonic live revisions, and one authoritative
  `RuntimeSettingsStore` with monotonic settings revisions.
- Runtime mutations use `expected_revision` compare-and-set. Stale writes are
  rejected with HTTP 409 and the current authoritative state. Accepted
  `command_id` retries are idempotent, validation precedes mutation, and a
  fail-open accepted-state subscriber hook supports a future realtime channel.
- Versioned endpoints are:
  - `GET /api/v1/bootstrap`;
  - `GET /api/v1/settings`;
  - `PATCH /api/v1/settings`.
- The legacy dashboard and endpoints remain operational and visually unchanged.
  Legacy Telegram and observer writes use the same `RuntimeSettingsStore` as
  `/api/v1`; there is no second operational settings authority.
- Ordinary v1 responses exclude observer/MOBILE coordinates, private Candidate
  Recorder context, filesystem paths, Telegram secrets, and other private
  forensic information.

### Phase A1 validation

- Focused A1 tests: 18 passed.
- Authoritative/recorder regressions: 102 passed.
- Full suite: 820 run, 819 passed, with one known Windows dashboard HTTP
  teardown error; the same test passed independently.
- `py_compile`, `git diff --check`, and privacy/scope review passed.

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
- Candidate Auto-Recorder Phases 1, 2, and 3 are complete. Phase 1 provides
  bounded pre-buffering and attribution, Phase 2 provides authoritative
  encounter/capture state, and Phase 3 connects production runtime and private
  storage while remaining fail-open.
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
- Phase 3B.1 implements private physical candidate bundles:
  - one physical capture is shared per active ICAO while each authoritative
    encounter retains a distinct private manifest;
  - both the approximately 60-second pre-buffer and subsequent live records are
    retained;
  - a bounded asynchronous queue and one storage worker keep candidate
    filesystem I/O off production reader and prediction paths;
  - stable immutable monotonically increasing sequence IDs deduplicate the
    pre-buffer/live boundary without collapsing legitimate identical records;
  - cross-midnight references and capture-directory collisions are handled;
  - finalization is retryable, stream data and timing sidecars are reconciled,
    and crash-tail bytes cannot be silently claimed by timing metadata;
  - configured/requested/applied hard-ceiling state and truncation reason are
    explicit in private metadata;
  - graceful shutdown preserves inspectable incomplete bundles when required.
- Phase 3B.2 gives the manually started FULL recorder absolute priority:
  - FULL state, session identity, start time, and stream coverage are read from
    the live `SessionRecorder`, not inferred from filesystem presence;
  - `FULL_REFERENCE` applies only when every production-enabled Candidate stream
    is actively covered by FULL; this is an all-or-nothing coverage decision;
  - incomplete FULL coverage retains normal Phase 3B.1 physical candidate
    capture;
  - in `FULL_REFERENCE` mode no duplicate candidate stream capture is opened,
    the pre-buffer is not drained to candidate storage, and live candidate
    stream writes are not enqueued;
  - atomic private per-encounter markers reference the actual FULL session and
    retain authoritative identity, logical window, lifecycle/prediction data,
    stream membership, and frozen observer context;
  - Candidate Recorder never stops, alters, reconfigures, or otherwise controls
    `SessionRecorder`.
- A private forensic candidate bundle may retain its frozen observer context,
  including MOBILE coordinates, solely because later photo reconstruction
  requires it. Such coordinates are private forensic data and must stay out of
  ordinary logs, dashboard, Telegram, HISTORY, public CSV, and other normal
  diagnostics/output. Phase 3A persists nothing; Phase 3B may store this context
  only in private bundle or `FULL_REFERENCE` marker metadata.

### Candidate Auto-Recorder Phase 3B.2 validation

- Focused Candidate Recorder tests: 76 passed.
- Authoritative/recorder regression tests: 108 passed.
- Full suite: 799 run, 798 passed, with one known Windows dashboard HTTP
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
- Candidate Auto-Recorder Phase 3 and its completed-capture reopen production
  hotfix are complete.
- App/Web Phase A1 is complete; the shared frontend is not yet implemented.

## Planned next work

The next public technical checkpoint is App/Web Phase A2: a shared
React/TypeScript frontend shell with a read-only LIVE display consuming
`/api/v1/bootstrap`, initially coexisting with the legacy dashboard. A broader
shared web/mobile client remains planned at a high level.

Candidate Auto-Recorder Phase 3 remains complete. Optional downstream forensic
tools may consume `FULL_REFERENCE` markers or candidate bundles, but are not
unfinished Phase 3 work. They must preserve FULL-recorder priority,
authoritative encounter identity, fail-open isolation, and private
observer-context rules.
