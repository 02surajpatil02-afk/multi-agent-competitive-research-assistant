# ADR 0015 — Visibility leases replace static duration ownership

- **Status:** **Accepted and built, 2026-08-19.** Partially supersedes
  [ADR 0010](0010-job-dispatch-and-status-across-api-queue-and-worker.md) decision 8; every other
  ADR 0010 decision is unchanged
- **Date:** 2026-08-19
- **Affects:** `jobqueue.py` · `worker.py` · `llm_client.py` · `docker-compose.yml` ·
  `docs/ARCHITECTURE.md` §11, §15, §19 · `docs/engineering-guidelines.md` §12, §17
- **Found by:** Final Step 22 shutdown-invariant review

---

## Context

ADR 0010 decision 8 tried to prove exclusive ownership with one static inequality:

```text
visibility > MAX_JOB_RUNTIME + 3 * LLM_MAIN_TIMEOUT_S + 10
```

That was not the implementation's duration model. A structured call permits two validation
attempts, and each validation attempt owns a fresh three-attempt transport schedule. The 429 and
transport counters are independent, a Researcher node also performs search, fetch and concurrent
extractions, and the Fact-Checker fetches its distinct sources before its batched LLM call.
`MAX_JOB_RUNTIME` is checked only after a node returns and its checkpoint is durable. It is a
no-new-node deadline, not a hard wall around a Python thread.

The old formula therefore admitted a real failure: a healthy worker could still be processing a
legitimate delivery when its visibility expired, allowing another worker to receive the same FIFO
message. FIFO orders messages in a group; it does not preserve exclusivity after an in-flight
message's visibility expires.

Provider-directed sleeping had a second hole. Numeric `Retry-After` was converted to `float` and
served without a ceiling, so one 429 response could suspend an otherwise bounded retry policy for an
arbitrary duration.

---

## Decision

### 1. Active visibility renewal is the ownership mechanism

Once the worker commits to handling a received message, it starts a background visibility lease.
The thread calls `JobQueue.extend_visibility()`; boto3 and `ChangeMessageVisibility` remain entirely
inside `jobqueue.py`.

The renewal cadence is derived from the queue's actual `VisibilityTimeout`:

```text
renew every visibility_timeout / 3
```

The local queue remains at 1,800 seconds, so renewal is every 600 seconds. A renewal attempt has a
conservative 93-second call envelope: three total attempts of 5-second connect plus 25-second read,
with at most 1-second and 2-second SDK backoff sleeps. The latest safe attempt start is therefore
`estimated_expiry - visibility_timeout / 3 - call_envelope`. After one failure, the retry is placed
halfway through the remaining safe-start window. A full-envelope first failure at 600 seconds thus
retries at 900 seconds, rather than waiting until 1,200 seconds. If another bounded attempt cannot
finish while preserving the existing one-third margin, ownership becomes unsafe immediately. The
established 1,800-second value is not shortened in this change: crash-recovery tuning needs
production SQS latency and worker-replacement measurements, not another guessed constant.

The heartbeat runs independently of graph execution, so a blocking LLM, search, fetch, S3 or
database operation on the main thread does not prevent renewal.

The shared SQS client also makes its own waits explicit: 5-second connect timeout, 25-second read
timeout (the 20-second legitimate long poll plus network margin), and three total standard-mode SDK
attempts. Visibility renewal therefore cannot leave heartbeat shutdown waiting on an unbounded boto
operation.

### 2. Ownership loss stops new graph work at a durable boundary

One transient renewal failure is logged and tolerated. A second consecutive failure marks the lease
unsafe. An invalid, expired or no-longer-in-flight receipt handle marks it unsafe immediately. Once
unsafe, the worker:

1. lets the node already running return and lets LangGraph persist its checkpoint;
2. starts no additional graph node;
3. stops the heartbeat;
4. does not acknowledge the message.

If the graph reached a terminal state or human gate just as ownership became unsafe, that outcome
remains durable but this receipt handle is still not used to acknowledge it. A redelivery observes
the durable outcome and performs the existing terminal/gate acknowledgement path.

This strengthens the single-owner invariant; it does not claim exactly-once execution. A network
partition can make the worker uncertain before SQS makes the message visible. Delivery remains
at-least-once, and ADR 0005's idempotent writes plus LangGraph checkpoints remain required.

### 3. Heartbeat lifecycle follows delivery ownership

The lease starts only inside `handle()`, after `run()`'s post-long-poll SIGTERM check. It remains
active while the current node and its checkpoint legitimately complete. It is stopped and joined
before `DeleteMessage`, so no renewal can race after successful acknowledgement. It also stops when
unfinished work is relinquished after SIGTERM, on invocation failure, or on ownership loss.

SIGTERM still means: take no new message, allow the in-flight node to finish if possible, stop before
the next node, stop renewing, and leave unfinished work unacknowledged. The 120-second Compose grace
period and future Fargate `stopTimeout` remain best-effort maximum graceful-stop opportunities, not
proofs that every node completes in 120 seconds.

### 4. `MAX_JOB_RUNTIME` remains a no-new-node deadline

The 1,200-second default applies to one worker invocation. It is checked after each streamed graph
update, when the just-finished node is checkpointed. Once reached, the worker records `job_timeout`
and starts no further node. A node already running may have a finite overrun; Python thread
cancellation is not introduced.

The heartbeat may continue during that current node and the terminal checkpoint/row write, then it
stops before acknowledgement. It is not permission to renew an invocation forever.

### 5. Every provider-directed retry sleep is finite

The LLM client's existing retry structure remains:

- two structured-output validation attempts;
- per validation attempt, one request plus two transport retries (`2s`, `8s` main; `1s`, `4s` fast);
- an independent 429 budget of one request plus three retries;
- main request timeout configurable, 60 seconds by default and 180 seconds locally; fast timeout
  fixed at 30 seconds;
- SDK retries disabled underneath this policy; every real request still consumes `CallBudget` and a
  Redis rate-limit token.

Numeric `Retry-After` is honoured through 30 seconds, the largest existing fallback delay and the
fast tier's complete request timeout. Larger values are clipped to 30. Missing, malformed, negative
and non-finite values use the normal `2s`, `8s`, `30s` schedule.

### 6. FIFO, DLQ and checkpoint recovery do not change

`MessageGroupId = job_id`, `maxReceiveCount = 3`, the existing DLQ, reviewer-decision idempotency,
checkpoint discrimination, status reconciliation and export semantics are unchanged. Renewal stops
on process death, so visibility eventually expires and another worker resumes from the last durable
checkpoint.

A hard kill on the final delivery can still leave a stale non-terminal row beside a DLQ message.
ADR 0010 decisions 9 and 10 continue to assign stale `running` and `queued` rows to the Phase 5
reconciliation sweep. This ADR does not claim to close that operational limitation.

---

## Consequences

- Exclusive ownership no longer depends on predicting the duration of the slowest possible node.
- A live but partitioned worker deliberately gives up at a checkpoint rather than continuing under a
  false claim of exclusivity.
- A crashed worker can still take up to the remaining visibility window to redeliver; 1,800 seconds
  is retained until production recovery measurements justify a smaller lease.
- Heartbeat logs expose renewal success, transient failure, ownership loss and shutdown
  relinquishment without logging receipt handles or credentials.
- The old `worst_case_node_seconds()` and startup duration inequality are removed rather than
  renamed into another false bound.

## Alternatives rejected

| Option | Why not |
|---|---|
| Compute a larger static worst-case node duration | Retry layers, tool work and checkpoint latency make it brittle; configuration drift recreates the race |
| Renew only between graph nodes | A long blocking node is the exact interval that needs independent renewal |
| Interrupt a Python thread at `MAX_JOB_RUNTIME` or SIGTERM | It can leave graph state, application rows and the checkpoint on different sides of one node |
| Keep renewing after repeated failures and hope ownership remains | It advertises exclusivity the process can no longer demonstrate |
| Set visibility to 120 seconds because shutdown is 120 seconds | Stop grace and queue recovery are different controls; the SQS operation/jitter margin needs production evidence |
| Claim exactly-once from FIFO plus heartbeat | SQS remains at-least-once under crashes and partitions; idempotency and checkpoints are still load-bearing |

## Verification required

1. Nested validation and transport retries are counted independently, and numeric Retry-After is
   tested below, at and above the cap plus missing, malformed, negative and non-finite inputs.
2. A blocked graph node does not prevent renewal.
3. One renewal failure recovers; repeated failure and invalid receipt handles mark ownership unsafe.
4. Ownership loss and SIGTERM stop at a checkpoint, acknowledge nothing unfinished, and replay no
   completed node on redelivery.
5. Gate and terminal outcomes stop and join the heartbeat before acknowledgement.
6. Real LocalStack `ChangeMessageVisibility` extends ownership, and stopping renewal eventually
   permits redelivery.
7. FIFO, delivery count, DLQ and reviewer-decision tests remain green.
