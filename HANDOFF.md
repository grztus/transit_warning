# Current task

Prepare for the next feature: Candidate Auto-Recorder. Design or implementation
has not started.

# Current baseline

- Branch: `main`
- Baseline at handoff: `e552a9b` (`Trim terminal table separator`)
- TRUE_2D consumer promotion is complete and deployed.

# Completed immediately before handoff

- Authoritative TRUE_2D routes terminal, dashboard, Telegram, gong, snapshots,
  and history consumers.
- TMUX table cleanup is complete and its final column is `age`.

# Current production state

Production is configured for TRUE_2D and is in observation/soak. The repository
configuration default remains LEGACY for compatibility.

# Next implementation

The architecture audit is complete. Candidate Auto-Recorder design has two
distinct identities/layers:

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
- Do not yet implement; follow phased plan when instructed.

# Constraints for the next agent

Follow `AGENTS.md`. Do not alter prediction semantics as part of recorder work,
and do not confuse the full recorder with candidate capture.

# Validation expectations

Run focused tests, the established full unittest suite, useful `py_compile`
checks, `git diff --check`, and a privacy/scope review.

# Working tree state

The working tree was clean before these documentation-only handoff files were
created. No Candidate Auto-Recorder code is pending.
