# ADR 0016 — PostgreSQL fences per-job execution

- **Status:** **Accepted and built, 2026-08-19.** Refines ADR 0005's single-writer
  precondition, ADR 0010 decision 4, and ADR 0015's ownership-loss boundary; no API, FIFO,
  redrive or provider semantics change
- **Date:** 2026-08-19
- **Affects:** `database/locks.py` · `database/queries.py` · `jobqueue.py` · `worker.py` ·
  `docs/ARCHITECTURE.md` §11, §15, §19 · `docs/engineering-guidelines.md` §12, §17
- **Found by:** Final Step 22 worker-reliability review

---

## Context

ADR 0005 explicitly permits `_write_findings` to read then insert only while one execution writer
exists for a job. The same assumption protects wholesale replacement of `claims` and
`claim_sources`, query-then-insert audit guards, LangGraph checkpoint progression for
`thread_id = job_id`, and export's separate PostgreSQL, checkpoint and deterministic S3 writes.
Those operations converge under sequential replay; they do not form one transaction and are not a
concurrent same-job protocol.

ADR 0010 assigned serialization to FIFO `MessageGroupId = job_id`, and ADR 0015 actively renews the
current delivery. Both remain necessary but neither covers this interval:

1. a renewal becomes unsafe or its visibility expires while Worker A is inside a long node;
2. SQS redelivers the same FIFO message to Worker B;
3. ADR 0015 deliberately lets A finish and checkpoint the node already in flight.

FIFO prevents another message in the group from running while SQS still recognizes the first as
in flight. It does not fence A after that delivery expires. Heartbeat alone therefore cannot prove
ADR 0005's single-writer precondition during lease-loss or network-partition conditions.

There is a separate check/start race. Advancing `graph.stream()` runs the next node before its
update reaches the Python loop body, so a stop check after each yielded update cannot by itself
prove that no new node starts after ownership is declared unsafe.

---

## Decision

### 1. A session-scoped PostgreSQL advisory lock fences one job

Before reading durable state or executing a graph, the worker acquires one
`pg_try_advisory_lock(bigint)` on a dedicated SQLAlchemy connection. The key is a domain-separated
64-bit BLAKE2 digest of the complete `job_id`. Equal job ids always map to the same key. A
theoretical hash collision only serializes two unrelated jobs conservatively; it cannot permit two
owners of one job.

The lock is session-scoped because graph execution spans many application transactions and
LangGraph checkpoint transactions. A transaction-scoped advisory lock ends too early. A lock-table
row would need a second lease, stale-owner recovery and cleanup—the failure mode this decision is
trying to remove. A process-local mutex cannot coordinate worker containers.

The lock connection is held for the complete durable handling interval and explicitly unlocked
before it is returned to SQLAlchemy's pool. If normal release fails, the connection is invalidated
instead of pooled with unknown lock state. PostgreSQL automatically releases the lock when its
backend connection or worker process dies; no ownership row or cleanup sweep exists.

### 2. Receipt ownership remains active while a redelivery waits

The worker uses non-blocking try-lock attempts rather than blocking a PostgreSQL backend. It starts
the new receipt's visibility heartbeat before waiting and runs zero graph nodes while the lock is
busy. Retry sleep reuses the existing five-second database query-timeout policy so shutdown and
lease-loss response are bounded without introducing another arbitrary interval.

A healthy waiter is not abandoned merely because Worker A is still finishing. Doing so would burn
one of only three receive attempts without advancing the job. Lock waiting is delivery-ownership
time, not graph runtime, so `MAX_JOB_RUNTIME` starts only after acquisition when graph execution
begins. SIGTERM or unsafe ownership stops the wait, releases its connection, leaves the message
unacknowledged, and starts no work.

### 3. Every execution decision is made from fresh durable state

The order is:

```text
SQS receive
  -> post-receive shutdown refusal
  -> start visibility heartbeat for this receipt
  -> acquire the per-job PostgreSQL lock
  -> reread jobs row, checkpoint, and reviewer decision if applicable
  -> decide start / resume / continue / already finished
  -> execute and complete durable checkpoint/status handling
  -> release PostgreSQL lock
  -> stop heartbeat
  -> acknowledge only an established ADR 0010 terminal/gate outcome
```

No checkpoint or reviewer decision used for execution is read before lock acquisition. A Worker B
that waited behind A therefore cannot act on a stale checkpoint branch.

The two final-delivery fallback paths outside the normal lifecycle try the same lock once and do
not wait without a healthy heartbeat. They mutate only if no execution owner exists.

### 4. One node/superstep is admitted at a time

Each graph call uses `interrupt_after="*"` and synchronous durability. It admits one sequential
node/superstep, waits for its checkpoint, and returns control before another graph call. An atomic
admission gate linearizes lease loss or SIGTERM against starting that call: a node admitted first
may finish; a stop recorded first prevents it from starting.

Worker A retains its PostgreSQL lock while that admitted node, checkpoint, status reconciliation
and any terminal handling finish. Worker B can maintain its own receipt but cannot enter durable
execution until A releases or its PostgreSQL session dies. After acquisition B rereads the new
checkpoint and does not replay A's completed node.

### 5. Delivery remains at-least-once

This is an execution-serialization fence, not exactly-once provider execution. A process can die
after a provider side effect and before its checkpoint. A replacement may repeat that node and its
provider call, while ADR 0005's replay keys still make durable application writes converge.
`MessageGroupId = job_id`, `maxReceiveCount = 3`, the DLQ, reviewer idempotency, the ADR 0012 API
boundary, and Phase 5's final-delivery orphan sweep are unchanged.

---

## Consequences

- Healthy SQS visibility renewal remains the normal defense against redelivery.
- If queue ownership is lost while a live node finishes, PostgreSQL prevents a replacement from
  concurrently mutating that job.
- If the old process or lock session dies, PostgreSQL releases ownership automatically and a
  redelivery resumes from fresh durable state.
- Unrelated job ids use unrelated advisory keys and remain concurrent.
- One waiting delivery consumes one pool connection. A worker handles one delivery at a time and
  the deployed worker still needs separate connections for graph/checkpoint work; pool sizing is
  therefore an observable deployment constraint rather than an unbounded waiter fan-out.
- Advisory locks are visible in PostgreSQL's lock/backend views and worker logs include the job,
  derived key and backend pid without exposing a receipt handle.

## Alternatives rejected

| Option | Why not |
|---|---|
| Transaction-scoped advisory lock | The graph, application writes and checkpoints span multiple transactions; ownership would disappear between them |
| Lock-table or row ownership | Needs its own expiry, stale-row cleanup and takeover protocol; process death is no longer automatic |
| Blocking advisory-lock call | A PostgreSQL backend cannot promptly observe SIGTERM or loss of the waiting receipt |
| Give up immediately when the lock is busy | Burns receive/DLQ attempts only because the prior owner is completing safely |
| Read checkpoint before waiting, then execute | The checkpoint can advance while waiting and the decision becomes stale |
| Process-local mutex | Does not coordinate independent worker processes or containers |
| Advisory lock without one-node admission | Serializes Worker B but does not stop already-owning Worker A from starting a new node after lease loss |

## Verification required

1. Real PostgreSQL excludes the same job, permits a different job, releases normally, and releases
   automatically when the owning backend disappears.
2. A redelivered Worker B renews its receipt while waiting, executes no node, then rereads A's
   checkpoint and does not replay its completed node.
3. Findings, claims, checkpoint progression and export observe only one same-job execution owner;
   unrelated jobs remain concurrent.
4. SIGTERM and lease loss while waiting begin no graph work and acknowledge nothing.
5. SIGTERM or lease loss racing node admission permits at most the already-admitted node to finish.
6. FIFO grouping, three-delivery DLQ behavior, reviewer idempotency and final-delivery Phase 5 scope
   remain unchanged.
