# Current task

Candidate Auto-Recorder Phase 3 is complete. Future work may add forensic
tooling that consumes candidate bundles and `FULL_REFERENCE` markers, but this
is not unfinished Phase 3 implementation.

# Current baseline

- Branch: `main`
- Baseline: `97e888c`
- Working tree was clean before this documentation update.
- Authoritative TRUE_2D is deployed; the repository configuration default
  remains LEGACY for compatibility.

# Candidate Auto-Recorder Phase 3 completion

- Phase 1 provides a bounded, thread-safe, approximately 60-second per-ICAO
  pre-buffer for attributable ADS-B SBS, MLAT SBS, RAW ADS-B, and parsed MLAT
  Beast input. RAW/Beast ICAO attribution is deliberately conservative and
  Phase 1 itself writes nothing to disk.
- Phase 2 uses the existing authoritative `prediction.encounter_id` as the
  forensic identity. State is per encounter while physical coverage is shared
  per ICAO. Only future TRUE_2D `INTERIOR` predictions trigger; defaults are
  T0 <= 300 seconds, SEP <= 2.0 degrees, and capture through T0 + 180 seconds.
  Later T0 may extend coverage, earlier T0 cannot shorten it, and `WITHDRAWN`
  does not cancel an established window. There is no ICAO cooldown; generations
  and SUN/MOON encounters remain distinct while sharing overlapping coverage.
- Phase 3A connects the pre-buffer to existing production stream-reader/parser
  paths and forwards authoritative lifecycle transitions to
  `CandidateEncounterManager`. Runtime advances window completion. No duplicate
  sockets, readers, or parsers were introduced, and every integration boundary
  is fail-open.
- Phase 3B.1 implements private candidate bundles: one physical capture per
  active ICAO plus distinct per-encounter manifests. Pre-buffer and live records
  are written by a bounded asynchronous storage worker, using stable immutable
  sequence IDs for deduplication. Cross-midnight references, directory
  collisions, retryable finalization, timing/data reconciliation, crash tails,
  explicit hard-ceiling metadata, and inspectable incomplete shutdown bundles
  are handled.
- Phase 3B.2 gives the manually started FULL recorder absolute priority. FULL
  state and stream coverage come from the live `SessionRecorder`, never from
  filesystem presence. `FULL_REFERENCE` is selected only when every
  production-enabled Candidate stream is actively covered; the decision is
  all-or-nothing. Otherwise Phase 3B.1 physical capture remains active.
- In `FULL_REFERENCE` mode Candidate Recorder creates no duplicate stream
  capture, does not drain the pre-buffer to candidate storage, and does not
  enqueue live candidate stream writes. It creates atomic private per-encounter
  markers referencing the actual FULL session and retaining the authoritative
  encounter, logical window, lifecycle, prediction, and frozen observer
  context. Candidate Recorder never controls or mutates `SessionRecorder`.

# Isolation and privacy

- Candidate failures must not interrupt normal stream processing, prediction,
  authoritative lifecycle handling, or FULL recording.
- Candidate Auto-Recorder does not change prediction geometry, dashboard,
  Telegram, HISTORY, snapshots, gong, or FULL-recorder semantics.
- Private candidate metadata may contain frozen observer context, including
  applicable MOBILE coordinates. Those coordinates must never appear in
  ordinary logs, dashboard, Telegram, HISTORY, public CSV, snapshots, or other
  normal/public diagnostics.

# Accepted Phase 3B.2 validation

- Focused Candidate Recorder tests: 76 passed.
- Authoritative/recorder regression tests: 108 passed.
- Full suite: 799 run, 798 passed, with one known Windows dashboard HTTP
  teardown error; that dashboard test passed independently.
- `py_compile` passed.
- `git diff --check` passed.

# Future forensic tooling

Optional later work may:

- consume `FULL_REFERENCE` markers and extract or reconstruct encounter windows
  from canonical FULL sessions;
- add replay, visualization, or analysis tools for private candidate bundles;
- add a broader private forensic review UI.

These are downstream tools, not requirements for closing Candidate
Auto-Recorder Phase 3. Existing unrelated project roadmap items and security,
privacy, geometry, and recorder constraints remain in force.

# Working tree state

Only `PROJECT_STATE.md` and `HANDOFF.md` should be modified by this documentation
checkpoint. Candidate Auto-Recorder Phase 3 is committed and pushed; no Phase 3
implementation is pending in the working tree.
