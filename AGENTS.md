# Repository agent rules

- Repository code and tests are authoritative when documentation conflicts.
- Inspect `git status --short` and the relevant diff before editing. Preserve
  unrelated working-tree changes.
- Preserve existing behavior unless the task explicitly changes it. Prefer
  small, auditable changes over broad refactors.
- Do not invent lifecycle, alert, snapshot, or history policy when semantics
  are ambiguous. Stop and report the ambiguity.
- Run focused tests first, then the established full suite
  (`python -m unittest discover -s tests`). Run `py_compile` where useful and
  always run `git diff --check`.
- Never commit or push unless the user explicitly requests it.
- Preserve LEGACY behavior unless a task explicitly changes it.
- TRUE_2D geometry must remain datum-consistent WGS84 ECEF/ENU with EGM96
  conversion and pressure-zero topocentric Sun/Moon geometry.
- In TRUE_2D mode, never silently fall back per event to LEGACY geometry.
- Do not invent TRUE_2D meanings for legacy-only `p2x`, `h2x`, intersection,
  or vertical-SEP diagnostics.
- Mobile observer coordinates are private. Do not expose them in normal logs,
  Telegram, dashboard history, CSV, snapshots, or public diagnostics unless a
  task explicitly defines a private forensic recording format.
- Never place secrets, private hostnames/IP addresses, tokens, sharing keys,
  observer locations, or recorder-sensitive data in documentation.
- Production runs separately from development. Do not assume Windows and
  Debian use the same Python interpreter or installed data paths.
- The existing full-session recorder and the planned Candidate Auto-Recorder
  are distinct features; do not duplicate or conflate their capture paths.
- Write prompts and documentation intended for coding agents in English.
