# Current task

Prepare for Candidate Auto-Recorder Phase 2 design/implementation of candidate
encounter gating and state management, still without broad production rollout.

# Current baseline

- Branch: `main`
- Baseline at handoff: `e552a9b` (`Trim terminal table separator`)
- TRUE_2D consumer promotion is complete and deployed.
- Candidate Auto-Recorder Phase 1 in-memory pre-buffer is implemented and dormant.

# Completed immediately before handoff

- Candidate Auto-Recorder Phase 1 implemented in `candidate_recorder.py` and
  tested in `tests/test_candidate_recorder.py`.
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
  - accepts only repository-supported attributable DF17 and DF18 CF=2 frames
    with valid CRC;
  - rejects DF11, DF19, anonymous DF18 (CF!=2), AP-overlaid frames, Mode A/C,
    and corrupt/unattributable frames.
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
- Validation:
  - Candidate Recorder tests: 23 passed
  - recorder suites: 61 passed
  - full repository suite: 746 passed
  - py_compile passed
  - git diff --check passed

# Current production state

Production is configured for TRUE_2D and is in observation/soak. The repository
configuration default remains LEGACY for compatibility. Candidate Auto-Recorder
Phase 1 is dormant and is not connected to production runtime.

# Next implementation

The next checkpoint is Candidate Auto-Recorder Phase 2: candidate encounter
gating and state management, still without broad production rollout.

Preserve the agreed two-layer model:
1. Physical stream capture is per ICAO:
   - SBS / RAW / MLAT data for one aircraft must be physically captured only once.
   - Multiple candidate encounters for the same ICAO share that physical capture
     rather than creating duplicate stream files/writers.
   - Physical capture remains active until the latest required end time of all
     encounters currently using it:
     `capture_until = max(required_end_time for active encounters)`.
   - If another encounter extends beyond the current physical capture end,
     extend `capture_until` rather than opening a duplicate stream capture.

2. Forensic event identity remains per authoritative encounter:
   `(observer epoch, ICAO, celestial body, encounter generation)`.
   - Every authoritative encounter crossing the Candidate Recorder trigger gets
     its own forensic encounter metadata/manifest.
   - A later authoritative WITHDRAWN must NOT cancel the forensic capture window
     once triggered; the encounter outcome is recorded as WITHDRAWN, PASSED, or
     another final lifecycle outcome (supersedes any early-exit suggestion).
   - A new encounter generation for the same ICAO is a new candidate even if it
     appears seconds or minutes after the previous encounter; there must be no
     cooldown that could suppress a legitimate new encounter.
   - Concurrent or overlapping SUN/MOON encounters for the same ICAO are also
     distinct forensic encounters sharing the single physical ICAO capture.

Window semantics:
- Candidate trigger is currently expected around T0 <= 300 s and SEP <= 2 deg,
  configurable and confirmed against config/constants.
- Each triggered encounter requires data beginning from approximately 60 s
  before its trigger time.
- Required end time is approximately authoritative T0 + 180 s.
- If triggered with T0 3 minutes away, the resulting forensic coverage is
  approximately 7 minutes including the 60 s pre-buffer.

Operational & privacy constraints:
- FULL recorder still has priority: when full recording is active, avoid
  duplicate broad capture and create a small marker/index referencing the full
  session instead.
- Retain the frozen observer context inside the private forensic bundle,
  including MOBILE coordinates, but never expose it through normal logs,
  dashboard, Telegram, history, snapshots, or public CSV.

# Constraints for the next agent

Follow `AGENTS.md`. Do not alter prediction semantics as part of recorder work,
and do not confuse the full recorder with candidate capture.

# Validation expectations

Run focused tests, the established full unittest suite, useful `py_compile`
checks, `git diff --check`, and a privacy/scope review.

# Working tree state

Working tree contains uncommitted `candidate_recorder.py` and
`tests/test_candidate_recorder.py`, with updated `PROJECT_STATE.md` and
`HANDOFF.md`.
