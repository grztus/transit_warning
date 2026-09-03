# Current task

Candidate Auto-Recorder Phase 2 is complete. The next checkpoint is Phase 3A:
dormant runtime wiring without disk output.

# Current baseline

- Branch: `main`
- Baseline: `01fb9f3`
- Working tree was clean before this documentation update.
- Authoritative TRUE_2D is deployed; the repository configuration default
  remains LEGACY for compatibility.

# Completed immediately before handoff

- Phase 1 provides a bounded, thread-safe, approximately 60-second per-ICAO
  pre-buffer for attributable ADS-B SBS, MLAT SBS, RAW ADS-B, and MLAT Beast
  input. It remains disconnected from production readers.
- Phase 2 provides dormant authoritative gating and in-memory state:
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
- Phase 2 creates no files/directories, opens no streams, and does not interact
  with the FULL recorder or production runtime.

# Next implementation

Implement **Candidate Auto-Recorder Phase 3A — dormant runtime wiring without
disk output**:

1. Feed production SBS/RAW/MLAT inputs into `CandidatePreBuffer` using the exact
   data already received by production readers.
2. Feed authoritative lifecycle transitions into `CandidateEncounterManager`.

Phase 3A must remain observational:

- no candidate bundles, directories, or physical candidate writers;
- no FULL-recorder suppression or marker/index implementation yet;
- no changes to prediction, dashboard, Telegram, history, snapshots, gong, or
  full-recorder behavior;
- every Candidate Recorder failure must remain isolated from normal Transit
  Warning operation.

Future Phase 3+ integration constraints:

- The manually started FULL recorder has priority. Candidate logic must never
  stop or interfere with it, and must not duplicate physical stream capture
  while FULL already covers the data. A covered candidate encounter should
  later create only a lightweight marker/index referencing the FULL session and
  time window.
- A private forensic candidate bundle may retain its frozen observer context,
  including applicable MOBILE coordinates. These are private forensic data only
  and must never appear in ordinary logs, dashboard, Telegram, HISTORY, public
  CSV, or other normal diagnostics/output.

# Constraints and validation

Follow `AGENTS.md`. First inspect the exact production reader and authoritative
transition call sites. Preserve stream ownership and avoid duplicate socket
connections. Run focused candidate/runtime tests, relevant recorder and
authoritative suites, the established full unittest suite, useful `py_compile`
checks, `git diff --check`, and a privacy/scope review.

# Working tree state

Only `PROJECT_STATE.md` and `HANDOFF.md` should be modified by this handoff
update. No Candidate Auto-Recorder implementation is pending in the working
tree.
