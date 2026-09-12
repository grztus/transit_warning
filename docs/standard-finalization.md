# STANDARD finalization

Candidate termination records its encounter ID, frozen last prediction, final
state, trigger reason, and history result in a bounded private diagnostic journal.
Reasons include prediction replacement/unavailability, stale motion, aircraft
expiry, observer invalidation, source reset, and passage. Ordinary withdrawal
retains its existing generic reason where no more specific trigger is supplied.

History eligibility and encounter outcomes are unchanged. The journal distinguishes
persisted history, failed writes, and skipped history (including early withdrawal,
duplicates, disabled persistence, and invalidation). A failed diagnostic enqueue,
serialization, or write does not prevent withdrawal, history transitions, or cleanup.
The journal is enabled with the dashboard and drains on dashboard shutdown.
Its queue holds at most 4096 records; overflow drops diagnostics without blocking ingest.

Records contain no observer coordinates or prediction geometry context and are
not part of public dashboard contracts. The journal does not start either recorder
or change candidate observation windows or final snapshot ownership.

LOCAL cleanup only withdraws LOCAL-owned rows. Provider cleanup only withdraws
rows still owned by that bridge. Source reset invalidates all old-generation rows
without adding history. Observer-scope invalidation preserves other source owners.

No-op withdrawals do not publish. Aircraft-expiry and post-transit cleanup finalize
each mutation immediately and publish once per changed batch, including from a
finally block when later cleanup fails. Authoritative finalization remains
synchronous; presentation publication is marked dirty and coalesced by the
[public-state worker](standard-public-state-publisher.md).
