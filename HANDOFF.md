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

First inspect the existing recorder architecture before proposing changes.
Current Candidate Auto-Recorder requirements are:

- authoritative TRUE_2D trigger;
- likely gate near SEP <= 2 degrees and T0 <= 5 minutes, but thresholds must be
  configurable and confirmed against current config/constants;
- approximately 60 seconds of pre-buffer and 180 seconds after T0;
- record only the relevant candidate ICAO;
- include SBS plus RAW/TC19/TC31 and MLAT data required for reconstruction;
- one private forensic bundle per stable encounter identity;
- when full recording is active, avoid duplicate capture and create a small
  marker/index referencing the full session instead;
- retain the frozen observer context inside the private forensic bundle,
  including MOBILE coordinates, but never expose it through normal logs,
  dashboard, Telegram, history, snapshots, or public CSV;
- do not implement until stream ownership, filtering, session format, and
  full-recorder detection have been audited.

# Constraints for the next agent

Follow `AGENTS.md`. Do not alter prediction semantics as part of recorder work,
and do not confuse the full recorder with candidate capture.

# Validation expectations

Run focused tests, the established full unittest suite, useful `py_compile`
checks, `git diff --check`, and a privacy/scope review.

# Working tree state

The working tree was clean before these documentation-only handoff files were
created. No Candidate Auto-Recorder code is pending.
