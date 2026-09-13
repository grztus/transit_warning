# STANDARD authoritative continuity backport

## Scope and baseline

Target: STANDARD `main`, starting at
`bb409b463f0cea0ec27e02c2d7d05a82a86de2b0`
(`Backport degraded live candidate retention to STANDARD`). The target was clean.
The source worktree is reference-only; no source files or protected artifacts
were edited. Source commits were audited in this order:

1. `c83ce11c914305ac208a124be9372f0810461341` — Stabilize authoritative encounter continuity.
2. `343022121bc074d1ff6d7cb6fa93370a633265e3` — Fix authoritative success/unavailable ordering.

This is one semantic backport, not a cherry-pick. STANDARD's existing interfaces,
publication path, source policy and test fixtures remain authoritative.

## Source-hunk audit: c83ce11

Hunk identifiers below are the old-side start lines from the source commit's
unified diff. Each listed identifier is classified; new files are single
addition hunks. Context mentioning phase-b infrastructure was not imported.

| Source file / hunk | Classification | STANDARD application |
| --- | --- | --- |
| `authoritative_transit.py` 7 | APPLIED | Separate `SOLVE_CONTINUITY_SECONDS = 10.0`. |
| `authoritative_transit.py` 57 | APPLIED | Transition quality reason and active missing-solve marker. |
| `authoritative_transit.py` 74 | APPLIED | LOCAL opt-in continuity constructor argument; default grace preserved. |
| `authoritative_transit.py` 116 | APPLIED | Past-event guard and new generation after last-success expiry. |
| `authoritative_transit.py` 191 | APPLIED | HELD/SOLVE_UNAVAILABLE, fixed deadline, expiry helpers, exact-identity finalization. Maintenance is subsequently extended by 3430221. |
| `live_dashboard.py` 240 | APPLIED | Lifecycle finalization callback slot. |
| `live_dashboard.py` 534 | ADAPTED TO STANDARD | Reason/body/identity-aware degradation using existing quality contract; STANDARD has no retained-map state to update. |
| `live_dashboard.py` 702 | APPLIED | Numerical expiry reports PREDICTION_UNAVAILABLE; velocity expiry remains MOTION_STALE. |
| `live_dashboard.py` 719 | APPLIED | Callback closes matching lifecycle identity without acquiring aircraft/source locks under dashboard lock. |
| `live_dashboard.py` 1016 | APPLIED | Disabled dashboard accepts optional degradation arguments. |
| `live_dashboard.py` 1097 | APPLIED | Runtime forwards degradation arguments and publishes changed state. |
| `runtime_deferred_prediction.py` 74 | APPLIED | Register exact-identity finalization callback on STANDARD state. |
| `runtime_deferred_prediction.py` 94 | APPLIED | Expire authority before capturing a new job. |
| `runtime_deferred_prediction.py` 102 | APPLIED | Aircraft entry/incarnation replacement discards old lifecycle identities. |
| `runtime_deferred_prediction.py` 159 | ADAPTED TO STANDARD | Commit-time expiry integrated with STANDARD guards; final ordering is the post-guard ordering from 3430221, not this intermediate pre-guard position. |
| `transit_warning.py` 133 | APPLIED | Import the separate continuity constant. |
| `transit_warning.py` 905 | APPLIED | Enable continuity for the configured LOCAL lifecycle. |
| `transit_warning.py` 2298 | APPLIED | Expire before a validated result is considered. |
| `transit_warning.py` 2731 | APPLIED | Exact-identity finalization callback and existing recorder post-event behavior. |
| `transit_warning.py` 3143 | APPLIED | Callback registration plus aircraft-before-dashboard guard for synchronous/replay mode. |
| `transit_warning.py` 3516 | ADAPTED TO STANDARD | PASSED precedence and HELD quality updates; existing STANDARD plain publication remains, without map/context publication arguments. |
| `transit_warning.py` 3557 | APPLIED | Expiry-to-existing-consumer adapter. |
| `transit_warning.py` 3693 | APPLIED | Maintenance health checks and continuity expiry, subsequently extended to FRESH by 3430221. |
| `tests/test_authoritative_continuity.py` new file | ADAPTED TO STANDARD | Port lifecycle, runtime, source, Telegram, recorder, invalidation and synchronous tests using STANDARD imports/fixtures; omit two map-only tests and their helper, and remove map-specific assertions from the recovery test. |
| `tests/test_encounter_churn_investigation.py` new file | ADAPTED TO STANDARD | Port numerical-grace characterization, source and deferred tests; remove lineage assertions and rename that test to describe generation behavior. No source priority changes. |
| `tests/test_finalization_diagnostics.py` 216 | APPLIED | Mock finalization returns None after reset already removed identities. |
| `tools/authoritative_encounter_churn_investigation.md` new file | NOT APPLICABLE TO STANDARD | Phase-b investigation/research report is not imported. This focused report documents the shared semantics and adaptations. |
| `web/src/App.tsx` 180 | ADAPTED TO STANDARD | Apply the same reason-dependent wording inside STANDARD's existing candidate card; no Pattern/map controls imported. |
| `web/src/test/App.test.tsx` 19 | ADAPTED TO STANDARD | Extend STANDARD's existing retention/age test to both quality reasons. |
| `web/src/types.ts` 84 | APPLIED | Add SOLVE_UNAVAILABLE to the existing reason union. |

The omitted map-only tests are
`test_solve_hold_serves_only_accepted_frozen_map_until_deadline` and
`test_geometry_started_fresh_cannot_install_after_solve_hold`, together with
`request_map`. STANDARD has no centerline request endpoint, revision/lineage
store or retained-map computation. No replacement map infrastructure or tests
were added. Candidate equality still verifies frozen prediction fields.

## Source-hunk audit: 3430221

| Source file / hunk | Classification | STANDARD application |
| --- | --- | --- |
| `authoritative_transit.py` 235 | APPLIED | Optional include_fresh maintenance over the same last-success deadline. |
| `deferred_prediction.py` 62 | APPLIED | Existing compatibility policy expressed as stable rejection reasons; boolean compatibility API preserved. |
| `runtime_deferred_prediction.py` 12 | APPLIED | Import rejection-reason helper. |
| `runtime_deferred_prediction.py` 163 | ADAPTED TO STANDARD | Reject before expiry, with diagnostics. Keep STANDARD's `prediction_encounters` accessor, which returns encounter ID and source owner; do not introduce phase-b `prediction_ownership` or map retention. |
| `runtime_deferred_prediction.py` 200 | APPLIED | Ignore obsolete body; diagnose unavailable and accepted decisions; valid peer can still commit. |
| `shadow_2d_prediction.py` 569 | APPLIED | Commit status/reason participates in existing diagnostic deduplication only. |
| `transit_warning.py` 2298 | APPLIED | Explicit comparison-record timestamp arguments and private commit/reject diagnostic helper. |
| `transit_warning.py` 3747 | APPLIED | Reassess all continuity-enabled active LOCAL predictions, including FRESH. |
| `transit_warning.py` 3763 | APPLIED | Expire FRESH as well as missing-solve holds during maintenance. |
| `tests/test_authoritative_success_commit.py` new file | ADAPTED TO STANDARD | Port all ten tests; use STANDARD imports and assert unchanged candidate/source ownership instead of nonexistent centerline revision. |
| `tools/authoritative_success_commit_investigation.md` new file | NOT APPLICABLE TO STANDARD | Do not import phase-b research history. Diagnostic interpretation and remaining limits are summarized here. |

All 41 source hunks across both commits are accounted for above. Omitted map
assertions describe infrastructure absent from STANDARD, not omitted lifecycle
semantics. No Pattern functionality, tests, metadata or documentation is imported.

## Lifecycle, ownership and maintenance

A healthy, compatible LOCAL exact-solve gap preserves its encounter while
`now < last_successful_solve + 10 seconds` and before the predicted event.
The existing public fields report DEGRADED / SOLVE_UNAVAILABLE. A hold preserves
separation, event time, frozen authoritative prediction and last-success time.
It does not create an authoritative prediction, call fresh Telegram consumers,
refresh pending stability or extend a notification deadline. Invalid/missing
solves do not move the continuity deadline.

Recovery before expiry keeps the same ID/generation, restores FRESH, clears the
reason/expiry and updates normally. Expiry withdraws once; a subsequent valid
prediction opens N+1. If T0 comes first, normal PASSED finalization wins, including
history and recorder post-event behavior. Aircraft disappearance/incarnation
replacement, observer invalidation, source replacement and hard input invalidation
terminate authority through existing paths. Genuine PREDICTION_REPLACED remains
available; it is not globally suppressed.

VELOCITY_STALE remains the preceding bb409b4 policy with its existing freshness
thresholds and display interval. A reason change during retention stays anchored
to the existing accepted prediction timestamp. The internal numerical grace
remains 3 seconds, separate from the LOCAL 10-second logical continuity option.
LEGACY and lifecycles without continuity opt-in keep their original grace policy.

STANDARD's main loop calls `clean_dict` independently of incoming messages once
its clock is initialized; live clocks are initialized immediately and replay
uses its recorded timeline. Maintenance now scans continuity-enabled active
predictions, reassesses motion health and applies the same accepted-success
expiry even without an explicit unavailable callback. It also handles retained
state when the dashboard is disabled. Repeated maintenance and dashboard ticks
do not duplicate finalization. No frontend smoothing or additional UI timer is
used to make backend FRESH aging correct.

AUTO continues to use STANDARD's existing local field health and source-owner
policy. A numerical gap alone does not allow REMOTE takeover; genuine LOCAL
health loss still does. Tests cover LOCAL hold, recovery, REMOTE takeover after
health loss and LOCAL recovery with a genuinely new generation.

## Deferred ordering and diagnostics

STANDARD's deferred path remains capture -> bounded scheduler -> numerical
solve -> commit guards -> current lifecycle expiry -> per-body consideration or
unavailable decision -> existing dashboard/Telegram/recorder consumers.
Incarnation, cancellation, source generation/mode, result age, observer
compatibility, committed version, current motion freshness and ownership guards
all precede expiry mutation. Rejected old work does not represent a current
unavailable solve. A compatible job captured before expiry cannot resurrect or
extend an expired identity. An obsolete body is ignored as EVENT_OBSOLETE rather
than converted to missing; its valid paired body can still commit.

The private diagnostic `solver_status=SUCCESS` describes numerical success.
It is distinct from lifecycle `commit_status=ACCEPTED`. Decisions also include
REJECTED with a stable reason, UNAVAILABLE with its lifecycle decision, or
DIAGNOSTIC_ONLY when authority is disabled. ACCEPTED describes lifecycle
acceptance before dashboard/notification consumers, not a guaranteed send.

Previously the comparison-record positional arguments swapped input/base and
commit time. Explicit arguments now separate `prediction_base_utc` and
`commit_evaluated_at_utc`; `utc` uses the supplied decision time. Legacy comparison
base is also passed correctly. Deferred calls use commit evaluation time;
synchronous/replay calls retain their existing context-based evaluation clock.
There is no new claim of a separate wall-clock solver completion measurement.
Tests capture completion independently to establish ordering.

The phase-b THY1GH evidence motivated this: a diagnostic stamped with earlier
input time can appear milliseconds before old-generation finalization even when
its result commits afterward to the next generation. The SPAKS monitor confirmed
same-ID numerical-gap recovery. These observations do not justify extending the
10-second interval, and the backport does not claim to identify or eliminate
every production gap without accepted solves.

Diagnostics retain the existing private coordinate-free JSONL writer, fail-open
behavior, one-second per-pair floor and 30-second identical-record interval.
Commit status/reason changes affect deduplication; no public fields beyond the
already-established quality contract are added. Correlation uses existing job,
source, epoch and encounter identifiers. Rate limiting can suppress nearby
records; scheduler-dropped/pre-solve jobs have counters rather than result logs.
No unbounded job history or new queue is added.

Source -> aircraft -> dashboard/scheduler ordering is preserved. The dashboard
finalization callback takes only the lifecycle lock and releases it before
observational recorder work; it never acquires aircraft/source locks. Per-result
work remains constant per body; maintenance is linear in active encounters.
Existing scheduler capacity, age limits and cancellation behavior are unchanged.

## Preserved scope and frontend

No map/centerline infrastructure exists in this STANDARD publication path, so
none is introduced. Frozen authoritative prediction fields and existing source
ownership remain intact. No Pattern module, store, session, EARLY/R&D/UI,
metadata or test was changed or introduced.

TRUE_2D coarse, exact, moving-body, astronomical, geoid and separation mathematics
and refinement gates are unchanged. The sole `shadow_2d_prediction.py` edit is in
the diagnostic signature. AST comparisons against the starting STANDARD commit
confirm all other top-level functions/classes in that file and
`compute_shadow_2d_result` are unchanged.

The only frontend behavior change is reason-aware wording:
`DEGRADED · prediction unavailable · last prediction X s ago` for SOLVE_UNAVAILABLE,
with existing velocity-stale wording retained. Its existing expiry/age behavior
and fresh presentation are unchanged. No smoothing or unrelated UI change.

## Deterministic coverage and validation

The adapted tests cover short gaps crossing 3 seconds, recovery before 10,
exact expiry and next generation, event passage, observer and hard-motion
invalidation, disappearance/incarnation replacement, distinct degraded reasons,
late/duplicate/superseded results, obsolete-body handling, FRESH maintenance,
Telegram stability/deadlines, history/recorder finalization and real source fallback.
Non-opt-in 3-second behavior has separate characterization tests.

Actual validation on this STANDARD tree:

- Initial focused core: 201 tests passed in 4.259 s.
- Expanded focused backend: 470 tests passed in 9.026 s. Groups cover lifecycle,
  continuity/churn, deferred runtime/scheduler/observer, motion freshness and
  diagnostics, source/AUTO, cleanup/finalization, publisher, Telegram/remote
  handoff, recorder, observer, replay, history and aircraft-lock synchronization.
- Full frontend: 109 tests passed across 6 files (Vitest 5.0.0, 2.71 s).
- `npm run typecheck`: passed.
- `npm run build`: passed, including TypeScript and production Vite output.
- `py_compile`: passed for all 10 changed Python files.
- Numerical AST comparisons: passed.
- Staged and unstaged `git diff --check`: passed.

The first frontend launch hit filesystem EPERM while creating Vite's temporary
configuration under the target worktree. Rerunning with the required target
filesystem access passed; no dependency or runtime changes were needed.
Full Python and final repository validation results are recorded below.

Final full-suite result: `python -m unittest discover -s tests` passed all 1,187
STANDARD tests in 49.379 s. No WinError 10053/10054 occurred in the core, expanded
focused or full Python runs; no HTTP retry or unrelated HTTP change was needed.

Files changed (14):

- `authoritative_transit.py`
- `deferred_prediction.py`
- `live_dashboard.py`
- `runtime_deferred_prediction.py`
- `shadow_2d_prediction.py`
- `transit_warning.py`
- `tests/test_authoritative_continuity.py`
- `tests/test_authoritative_success_commit.py`
- `tests/test_encounter_churn_investigation.py`
- `tests/test_finalization_diagnostics.py`
- `web/src/App.tsx`
- `web/src/test/App.test.tsx`
- `web/src/types.ts`
- `tools/standard_authoritative_continuity_backport.md`

The source worktree remains on phase-b at 3430221, with no tracked differences
and only its three pre-existing protected untracked artifacts. They were not
edited, copied into STANDARD, or staged. No branch switch, merge, cherry-pick,
amend or push is part of this backport. The requested local commit is
`Backport authoritative continuity fixes to STANDARD` on main.
