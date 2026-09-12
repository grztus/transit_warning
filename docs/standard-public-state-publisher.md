# STANDARD public-state publisher

DashboardRuntime owns one publication worker with a 0.2-second minimum interval
between attempts. Runtime mutations mark a dirty generation; no snapshot/event
FIFO is created. Repeated marks coalesce. A successful attempt acknowledges only
the generation captured before building, so concurrent mutations schedule another
attempt. Failures retain dirty state and retry at the same cadence.

Candidate lifecycle, withdrawal, finalization, history, notifications and recorder
actions remain synchronous and authoritative. Only presentation snapshots coalesce.
No-op withdrawals, empty invalidations/body resets and identical timestamp ticks
do not mark dirty. Source resets and owner replacements retain their existing
guards; the next acknowledged snapshot reflects current authoritative state.

The worker builds a consistent snapshot under the dashboard lock. Contract
serialization, copying, privacy validation, SSE encoding validation and fan-out
run on the worker after that lock is released. It takes no source, aircraft or
scheduler lock. Dashboard-lock contention and Python GIL cost still exist.

Startup publishes the initial snapshot synchronously before HTTP serving.
Bootstrap waits up to 5 seconds for the dirty generation known at request entry,
returning HTTP 503 on failure. This is the only normal HTTP publication barrier.
Source/observer settings responses remain authoritative settings responses;
they do not wait for public publication. Source reset and finalization mark dirty
without blocking the producer. Bootstrap or an explicit internal flush can wait
for their visibility. No hot-path mutation calls a barrier.

The production live SSE subscriber is required. Envelope validation, JSON encoding
or fan-out failure prevents generation acknowledgement. Optional subscribers remain
fail-open. Required delivery means successful local publication/fan-out, not client
socket acknowledgement. A failed attempt may already have updated the store or
some clients; retry uses a newer store revision. One worker preserves publication
order. A previously published snapshot can remain visible until the new generation
is acknowledged; snapshots are not a lossless lifecycle event stream.

Shutdown closes input sockets, stops HTTP serving and drains accepted POST/PATCH/
DELETE mutations while the publisher remains alive. Incomplete request transport
is interrupted, with a 5-second I/O timeout ceiling. The provider and input readers
are stopped/joined outside their runtime locks; then Stage 2C closes the scheduler
before snapshot/recorder/notification consumers close. Dashboard close joins its
HTTP thread, drains/joins the publisher, then closes history and finalization sinks.
Dirty publisher shutdown makes one final cadence-respecting attempt and does not
retry indefinitely on failure. Join assumes local serializer/subscriber work returns;
the publisher is not abandoned while it could access consumers.

Private diagnostics include dirty/published generations, attempts/publications,
coalesced marks, failures, pending state, duration, barrier waits/failures, worker
status and shutdown drain result. Existing terminal snapshots include these scalar
counters. No public contract changes or coordinate-bearing diagnostics are added.
