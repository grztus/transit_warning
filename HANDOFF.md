# Current task

Candidate Auto-Recorder Phase 3A is complete. The next checkpoint is Phase 3B:
physical candidate-bundle writing/finalization and FULL-recorder-aware
marker/index handling.

# Current baseline

- Branch: `main`
- Baseline: `4f1ed23`
- Working tree was clean before this documentation update.
- Authoritative TRUE_2D is deployed; the repository configuration default
  remains LEGACY for compatibility.

# Completed immediately before handoff

- Phase 1 provides a bounded, thread-safe, approximately 60-second per-ICAO
  pre-buffer for attributable ADS-B SBS, MLAT SBS, RAW ADS-B, and MLAT Beast
  input.
- Phase 2 provides authoritative gating and in-memory state:
  - identity is the existing `prediction.encounter_id`;
  - forensic state is per encounter and conceptual capture state is per ICAO;
  - only future TRUE_2D `INTERIOR` predictions trigger;
  - defaults are T0 <= 300 seconds, SEP <= 2.0 degrees, and end at T0 + 180
    seconds;
  - later T0 extends the window; earlier T0 never shortens it;
  - `WITHDRAWN` updates outcome without cancelling coverage;
  - new same-ICAO generations have no cooldown;
  - SUN/MOON encounters are distinct but share per-ICAO coverage;
  - `capture_until` is the latest required end among unfinished encounters.
- Phase 3A wires this in-memory machinery into production runtime paths:
  - existing ADS-B and MLAT SBS inputs feed `CandidatePreBuffer`;
  - RAW ADS-B input is observed from the existing RAW reader;
  - parsed MLAT Beast frames are observed from the existing parser;
  - existing authoritative lifecycle transitions feed
    `CandidateEncounterManager`, including applicable invalidation and
    aircraft-removal transitions;
  - the main loop advances completion of in-memory forensic windows.
- No duplicate sockets, readers, or parsers were introduced. All Candidate
  Recorder hooks are fail-open and must not interrupt normal stream processing,
  prediction, authoritative lifecycle handling, or FULL recording.
- Phase 3A is observational and diskless: it creates no candidate files,
  directories, bundles, or writers, and emits no observer coordinates or
  candidate forensic data through normal outputs.
- Prediction geometry, dashboard, Telegram, HISTORY, snapshots, gong, and FULL
  recorder behavior are unchanged.

# Next implementation

Implement **Candidate Auto-Recorder Phase 3B — physical candidate-bundle
writing/finalization and FULL-recorder-aware marker/index handling**.

Phase 3B must preserve authoritative encounter identity and all Phase 1/2/3A
gating, window, overlap, and fail-open semantics. It must not leak private
forensic data into normal outputs or alter prediction, dashboard, Telegram,
HISTORY, snapshots, or gong behavior.

Future Phase 3+ integration constraints:

- The manually started FULL recorder has priority. Candidate logic must never
  stop or interfere with it, and future physical capture must avoid unnecessary
  duplicate broad capture while FULL already covers an event. A covered
  candidate encounter may instead create a lightweight marker/index referencing
  the FULL session and time window. This coordination is not implemented in
  Phase 3A.
- A private forensic candidate bundle may retain its frozen observer context,
  including applicable MOBILE coordinates. These are private forensic data only
  and must never appear in ordinary logs, dashboard, Telegram, HISTORY, public
  CSV, or other normal diagnostics/output. Phase 3A persists no such context
  because it writes no bundles.

# Phase 3A validation

- Focused Candidate Recorder tests: 43 passed.
- Authoritative/recorder regression tests: 133 passed.
- Full suite: 766 run, 765 passed, with one known Windows dashboard HTTP
  teardown error; that dashboard test passed independently.
- `py_compile` passed.
- `git diff --check` passed.

# Working tree state

Only `PROJECT_STATE.md` and `HANDOFF.md` should be modified by this handoff
update. Phase 3A is committed; no Candidate Auto-Recorder implementation is
pending in the working tree.
