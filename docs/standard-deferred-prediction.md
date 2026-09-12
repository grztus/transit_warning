# STANDARD deferred TRUE_2D execution

Live authoritative TRUE_2D LOCAL ingest uses one backend-owned worker for each
frozen MOON/SUN pair. Replay and LEGACY/shadow-only evaluation remain synchronous.
INTERNET bridge computation remains on its existing path; AUTO provider lifecycle
changes invalidate incompatible deferred LOCAL work.

The scheduler holds at most 32 distinct pending aircraft plus one running job.
A newer pending input for the same aircraft replaces its predecessor without
moving its queue position. New aircraft keys at capacity are rejected without
eviction. An accepted running result does not erase newer pending input.
Superseded unevaluated inputs may never create a candidate or alert. Physical
message recording and observation capture are unchanged.

Observer, motion/intent/policy, QNH, altitude correction, configuration, geoid
provider identity, source provenance and legacy comparison inputs are frozen
before submission. Coarse screening and exact refinement run outside source
and aircraft locks with the existing paired-body sample cache. Numerical
algorithms and datum handling are unchanged.

Work older than 2 seconds from monotonic capture is discarded before solving.
Commit accepts ages from 0 through 2 seconds inclusive, rechecking the deadline
after contended guards. Aircraft and motion freshness use the existing thresholds.
A body's already-past event follows its existing unavailable/grace path without
suppressing the other body.

Commit requires compatible incarnation/cancellation/source generations, source
mode, observer epoch/mode/effective source, and a newer version than the last
committed version. Encounter and source-owner guards reject replacement or
finalization; withdrawals and PASSED ticks invalidate outstanding work even
when the same encounter ID is later reused. No-op withdrawals do not cancel work.
Source resets retain the Stage 2B finalization policy.

STATIC and MANUAL require exact position and elevation equality. MOBILE requires
exact configured elevation and displacement no greater than
min(50 m, max(5 m, frozen accuracy + current accuracy)). Missing, negative,
non-finite or nonnumeric accuracy uses a 5 m allowance. Equality is accepted.
Accuracy affects compatibility only; frozen geometry is not rounded or smoothed.
Phone altitude does not replace configured calculation elevation.

Lock order is source, aircraft, then scheduler condition or dashboard state.
Solve and commit callbacks never run under the scheduler condition. Dashboard
mutation guards run before its state lock; existing publication callbacks run
after that lock is released. Commit retains synchronous authoritative lifecycle,
history, notification and recorder consumers. Presentation changes only mark a
dirty generation: the bounded/coalescing publisher builds and serializes the
latest public state on its worker, at a minimum 0.2-second attempt interval.
There is no snapshot FIFO and no publication barrier on ingest/solve paths.
Bootstrap can wait for publication; settings responses do not. See
[publication semantics](standard-public-state-publisher.md).

Shutdown rejects new work, cancels pending jobs and invalidates running results.
Numerical sampling checks cooperative cancellation. The worker is joined outside
runtime locks for at most 2 seconds, matching the runtime's existing worker-join
interval. A nonreturning external callback cannot be forcibly killed by Python:
on timeout the daemon handle remains owned, a scalar counter/warning records the
timeout, and late results cannot invoke consumers. Normal shutdown joins the
worker before closing downstream consumers.

Private scheduler snapshots contain only counters: pending/running/capacity,
submissions/replacements, capacity rejects, pre-solve expiry, stale results,
executed/committed/cancelled/failed jobs, solve timing and shutdown status.
Existing terminal diagnostic snapshots include these counters. No public contract
or persistent scheduler storage is added.
