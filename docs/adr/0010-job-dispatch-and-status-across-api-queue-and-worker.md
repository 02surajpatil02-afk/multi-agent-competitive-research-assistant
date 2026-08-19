# ADR 0010 — Job dispatch and status across API → SQS → worker

- **Status:** **Accepted, 2026-08-17; decision 8 superseded by
  [ADR 0015](0015-visibility-leases-replace-static-duration-ownership.md), 2026-08-19.** Not built at
  acceptance. Blocks implementation step 20. Accepted unchanged
  — the reviewer-gate work of 2026-08-17 ([ADR 0013](0013-reviewer-gate-payload-view.md),
  [ADR 0014](0014-gate-review-history-is-not-snapshotted.md)) touches nothing this record decides
- **Date:** 2026-08-16
- **Affects:** `schemas.py` (`JobStatus`) · `database/schema.py` · a new Alembic revision ·
  `database/queries.py` · `routes/api.py` (`POST /jobs`) · a new `worker.py` and queue boundary ·
  `.env` / `.env.example` · `docs/ARCHITECTURE.md` §3, §10, §11, §15 ·
  `docs/engineering-guidelines.md` §12, §17
- **Found by:** The Phase 3 readiness audit of 2026-08-16 (decisions D2, D3, D5, D6, and risk G.1)
- **Relates to:** [ADR 0005](0005-graph-time-persistence-semantics.md), whose keyed writes assume one
  writer per job — decision 4 below is what keeps that assumption true.
  [ADR 0008](0008-a-failed-jobs-reason-lives-in-the-checkpoint-for-phase-2.md) predicted that the
  worker is what makes `job_timeout` reachable; decision 7 is where that happens.
  [ADR 0009](0009-recovering-an-export-that-failed-after-approval.md) builds the `job_finished` audit
  row this record's terminal transitions write

---

## Context

§11 and gl §12 already fix most of this: the message is a pointer and never state, `idempotency_key`
is `UNIQUE NOT NULL` on `jobs` and stops a duplicate *request*, the checkpoint stops a duplicate
*delivery*, visibility is 25 minutes against a 20-minute job bound, three deliveries then the DLQ,
and the worker count is bounded by the LLM rate limit rather than by queue depth.

Six things are not fixed anywhere, and each of them blocks writing `worker.py`.

**1. A submitted job says it is running while nothing runs it.** `create_job` writes
`status="running"` (`database/queries.py`), and `_phase` computes `"queued"`
(`routes/api.py::_phase`). `JobStatus` is `running | awaiting_approval | approved | rejected |
failed` — there is no `queued`. Phase 2 documented this as a seam; once a queue exists, `running`
for a job nothing has dequeued is simply false.

**2. `attempt` cannot do the job the documented message shape gives it.** §11 and gl §12 both put
`"attempt": 1` in the body. An SQS message body is immutable once sent, so a field inside it cannot
count redeliveries. The number that can is `ApproximateReceiveCount`, a receive-time attribute.

**3. Nothing says how the worker tells a start from a resume.** §11's diagram has the API enqueue a
"pointer message" for a new job and a "resume message" after approval, and gives both the same shape.

**4. Two messages for one job can be in flight at once.** A reviewer retrying a decision (ADR 0007
invariant 2) enqueues a second resume while the first may still be being processed. The 25-minute
visibility timeout does not help — it protects one message from being redelivered, not one job from
having two messages. ADR 0005 states the consequence explicitly: *"The read-then-write in
`_write_findings` is not concurrency-safe on its own. It does not need to be while §11's
single-writer rule holds; **if that ever changes, this is the line that has to change with it.**"*

**5. `MAX_JOB_RUNTIME` has no clock and no enforcement.** `config.py` says so in the field's own
docstring, and gl §17 records it: *"A value the code claims to honour and does not is worse than no
value."* The worker is the component that owns a job's lifetime.

**6. A DLQ'd message leaves its job non-terminal forever.** §15's row ends at "DLQ + CloudWatch
alarm". Nothing writes the `jobs` row, so the job sits at `running` — invisible to gate expiry, which
only sweeps `awaiting_approval`, and to any status query a person would think to run.

### One measurement that changes a documented number

§17's visibility-vs-runtime argument is *visibility (1500s) > job limit (1200s)*, with the 5-minute
margin covering "worker startup and checkpoint writes". The runtime bound can only be checked
**between nodes** — a node in flight is inside a blocking LLM request. So the real bound is
`MAX_JOB_RUNTIME + the longest a single node can take`, and §22 item 6 measures that longest node:
three consecutive `LLM_MAIN_TIMEOUT_S` attempts plus the 2s/8s backoff.

| | `LLM_MAIN_TIMEOUT_S` | Worst node | Runtime + node | vs 1500s visibility |
|---|---:|---:|---:|---|
| Production defaults | 60s | 190s | **1390s** | safe |
| `.env` development override | 180s | 550s | **1750s** | **exceeds it** |

And `.env` currently sets `MAX_JOB_RUNTIME=1800`, which exceeds 1500s on its own. The audit's first
reading of this was "bring the override under 1500s"; the table above shows that is not sufficient,
because 1200 + 550 is already over.

---

## Decision

### 1. `JobStatus` gains `queued`, as its first value

```python
JobStatus = Literal["queued", "running", "awaiting_approval", "approved", "rejected", "failed"]
```

`create_job` writes `queued`. `POST /jobs` returns `{job_id, status: "queued"}`. The `_phase`
vocabulary is unchanged — gl §12 already lists `queued` as its first value, so the field and the
status finally agree rather than contradicting each other.

**Migration ordering follows gl §19's backward-compatibility rule, and the order is not optional.**
`ck_jobs_status` is built from `get_args(JobStatus)`, so the revision that widens the CHECK ships
**before** any code writes the value, and a `JobResponse` validated against the old Literal would
reject a `queued` row. One release apart, as §19 requires.

### 2. The worker moves `queued → running`, and nothing else does

On receipt, before the first invocation, through the existing `set_job_status` — whose
`completed_at IS NULL` guard already means a finished job cannot be dragged backwards.

**No `job_started` audit action.** `jobs.created_at` and the first graph-time row already bracket the
queue wait, and the audit vocabulary stays as small as gl §9 keeps it. The one action added in Phase 3
is `job_finished`, and it belongs to [ADR 0009](0009-recovering-an-export-that-failed-after-approval.md).

### 3. The message shape, exactly

```json
{
  "job_id": "uuid",
  "user_id": "uuid",
  "idempotency_key": "sha256(user_id + question + date)"
}
```

**`attempt` is removed from the body**, and the delivery count is read from SQS's
`ApproximateReceiveCount` at receive time. This corrects §11 and gl §12 rather than following them: a
field inside an immutable body cannot count redeliveries, so keeping it would mean shipping a
number the code has to ignore.

`user_id` and `idempotency_key` stay. They are redundant against the `jobs` row the worker reads
anyway, and they are kept because they let a log line and a trace identify a message without a
database read — which is the state you are in when the database is the thing that is broken.

**Still identifiers only.** No question text, no decision, no state. §20 row 8 is unchanged, and it is
what keeps untrusted user text out of the queue and makes a redelivery a resume rather than a restart.

### 4. The queue is FIFO, with `MessageGroupId = job_id`

This is the decision that keeps ADR 0005's single-writer precondition true, and it is a queue
attribute rather than application code.

A FIFO queue delivers at most one message per message group at a time. With the group set to the job
id, **two messages for one job can never be processed concurrently**, whatever mix of starts,
resumes, retries and redeliveries produced them. That is exactly the guarantee `_write_findings`
needs, provided by the thing that already knows about in-flight messages.

`MessageDeduplicationId` is set explicitly rather than left to content-based dedup:

| Message | `MessageDeduplicationId` | Effect |
|---|---|---|
| Start | the job's `idempotency_key` | Belt-and-braces behind the `UNIQUE` constraint, which refuses the duplicate first |
| Resume | `f"{job_id}:{calls_used}"` — the ADR 0007 gate-visit key | A reviewer retrying the same decision inside SQS's 5-minute dedup window produces **one** message, which is the retry semantics ADR 0007 invariant 2 already asks for |

The second row is worth reading twice: the gate-visit key that makes the *endpoint* idempotent is the
same value that makes the *queue* idempotent. One key, three places — the `gate_opened` row, the
`reviewer_decision` row, and now the message.

### 5. Start, resume, and continue are discriminated from the checkpoint

The message says which job. The checkpoint says what to do with it.

```text
jobs.completed_at IS NOT NULL   -> terminal: delete the message, do nothing
no checkpoint for thread_id     -> start:    invoke(new_state(job_id, user_id, question))
checkpoint, pending interrupt   -> resume:   invoke(Command(resume=<the visit's decision>))
checkpoint, no interrupt        -> continue: invoke(None)   # a previous delivery died mid-run
```

This keeps decision 3's "identifiers only" intact and needs no message type. It also makes the
crash-recovery case explicit, which §11 describes in prose and no shape captured: a worker that dies
mid-run leaves a checkpoint with no interrupt, and `invoke(None)` continues it from the last completed
node. Where the resume decision comes from is
[ADR 0011](0011-the-human-gate-resume-moves-to-the-worker.md)'s.

### 6. Attempt and retry semantics

- `maxReceiveCount = 3` on the redrive policy — §11's "three deliveries, then the dead-letter queue",
  expressed where SQS can enforce it.
- **The worker deletes the message on exactly three outcomes:** the graph interrupted at the gate, the
  job reached a terminal status, or the job was already terminal when the message arrived.
- **It deletes on nothing else.** An unhandled failure leaves the message, and redelivery is the
  retry. This is §11's worker-crash paragraph made executable.
- One message at a time: `MaxNumberOfMessages=1`, long poll at 20s. One worker, one job — §11's
  worker table, unchanged.

### 7. `MAX_JOB_RUNTIME` is per invocation, wall-clock, checked between nodes

**Meaning.** The bound applies to one worker invocation — from the moment the worker begins invoking
the graph for this message — **not** to the job's lifetime. It is the only reading that works: a job
that waits three days at the gate must not fail on resume, and the reason the bound exists is to
protect a *per-delivery* visibility timeout.

**Consequence, stated rather than discovered.** Three deliveries can therefore spend up to
`3 × MAX_JOB_RUNTIME` in total. Each resumes from a checkpoint rather than repeating work, and
`MAX_LLM_CALLS_PER_JOB` still bounds the spend — which is the guard §16 already calls the binding one.

**Enforcement, with no graph change.** The worker consumes `graph.stream(..., stream_mode="updates")`
and checks elapsed time after each node — structurally what `scripts/measure_jobs.py::_drain` already
does. On exceeding the bound it stops consuming and then:

```python
graph.update_state(run_config(job_id), {"status": "failed", "failure_reason": "job_timeout"})
graph.invoke(None, run_config(job_id))   # the next router sees status=failed and goes to finalize
```

Both `supervisor_node` and `reflection_node` already answer `_job_already_failed()` by routing
straight to `finalize` without spending a call. The timeout therefore reuses the failure path the
graph has had since Phase 1, and adds no node, no edge, and no state field.

**The granularity is admitted, not hidden.** A node already in flight is not interrupted, so the
effective bound is `MAX_JOB_RUNTIME + the longest a single node can take`. That is what decision 8
sizes.

**The variable keeps its name.** gl §17's row wording changes from "Whole job" to "one worker
invocation"; renaming the environment variable would break every existing `.env` for a clarification.

### 8. The visibility timeout is derived and checked at startup, not asserted in a document

**The invariant is not `visibility > MAX_JOB_RUNTIME`.** It is:

```text
visibility_timeout  >  MAX_JOB_RUNTIME + worst_case_node_seconds

where  worst_case_node_seconds = 3 × LLM_MAIN_TIMEOUT_S + 10       (gl §17: 3 attempts, 2s + 8s backoff)
```

The worker reads its queue's visibility timeout with `GetQueueAttributes` at startup and **refuses to
start** if the inequality fails — the same "refuse loudly rather than clamp quietly" that
`config._researcher_concurrency` already uses, and for the same reason: a value that behaves
differently from the one that was configured is expensive to read back off a run.

| Environment | `LLM_MAIN_TIMEOUT_S` | `MAX_JOB_RUNTIME` | Required | Set to |
|---|---:|---:|---:|---:|
| Production defaults | 60s | 1200s | > 1390s | **1500s** (§11's 25 minutes, unchanged) |
| Local Compose, development overrides | 180s | 1200s | > 1750s | **1800s** |

**`.env` drops its `MAX_JOB_RUNTIME=1800` override and returns to the 1200 default.** The right place
for the development slack is the *local queue's* visibility timeout, not the job bound — raising the
job bound raises the left side of the inequality and the right side with it.

### 9. A job that reaches the DLQ becomes terminal, and the alarm still fires

On receive the worker reads `ApproximateReceiveCount`. When it equals `maxReceiveCount`, this is the
final delivery, and the worker wraps the invocation so that **any** unhandled failure ends with:

```python
graph.update_state(run_config(job_id), {"status": "failed", "failure_reason": "job_dead_lettered"})
graph.invoke(None, run_config(job_id))   # -> finalize
```

— the same mechanism as decision 7 — and then **does not delete the message**, so it goes to the DLQ
and the CloudWatch alarm on DLQ depth fires as §11 and §14 intend. The job is terminal *and* the
failure is visible: those are two requirements, not one.

**`job_dead_lettered` is a required vocabulary word, not an invented one.** gl §4 says
`failure_reason` is never left `None` on a failure, and §15's DLQ row supplies no reason. A job that
ends `failed` with nothing to say why is the outcome that rule exists to prevent. `failure_reason` is
`str | None` on state, so this costs no schema change; ADR 0009's `job_finished` row is what makes it
queryable.

**The residual, stated so it is not rediscovered as a bug.** A hard kill — SIGKILL, OOM — leaves no
opportunity to write anything, so the job stays `running` while its message reaches the DLQ. The
recovery is the Phase 5 retention-and-expiry sweep (step 32), which already walks jobs by status;
a non-terminal job with no in-flight message and no recent audit row is exactly what it should close.
Recorded here, and assigned, rather than left as a silent hole.

### 10. `POST /jobs` inserts, commits, then enqueues — and says so when the enqueue fails

The insert and the send cannot share a transaction. If the send fails after the row is committed, the
job is `queued` with no message.

**The endpoint answers `503 enqueue_failed`, carrying the `job_id`, in the documented error
envelope.** The row is left in place: it holds the `idempotency_key` that makes a resubmission
converge on the same job rather than creating a second one, and it is what a re-enqueue would target.

`503` is added to `POST /jobs`'s code list in gl §12. The alternative — returning `202` — would tell a
caller their job was accepted for processing when nothing will process it, and `202` is precisely that
claim.

**The orphan is queryable and its recovery is Phase 5's**, alongside decision 9's residual, because
the two are the same shape of problem: `status = 'queued' AND created_at < now() - interval`.

---

## Consequences

- **Three transitions have three owners, and no two write the same value.** The API writes `queued` on
  insert and `running` at `claim_gate`; the worker writes `running` on receipt and reconciles on exit
  ([ADR 0011](0011-the-human-gate-resume-moves-to-the-worker.md)); the gate node writes
  `awaiting_approval`; `finalize` writes the terminal status. ADR 0007 invariant 4 survives intact.
- **The FIFO group is now load-bearing.** ADR 0005's `_write_findings` stays as it is, and the reason
  it may stay is written down in one place. A change to a standard queue would silently break it, so
  the group id belongs in the same test that asserts redelivery converges.
- **A queue with an ordering guarantee has a throughput ceiling.** 300 messages/second per group is
  four orders of magnitude above "a handful of jobs a day". If that ever stops being true, this is the
  paragraph to re-read.
- **Two documented numbers change:** the message loses `attempt`, and the local visibility timeout is
  1800s rather than 1500s. Both corrections carry their derivation above.
- **`MAX_JOB_RUNTIME` stops being a number the code claims to honour and does not** — the sentence gl
  §17 has carried since step 12.
- **Two non-terminal residuals are assigned to Phase 5 rather than closed** (decisions 9 and 10), and
  both have a SQL predicate that finds them.

## Alternatives rejected

| Option | Why not |
|---|---|
| **Keep `status="running"` for a queued job** | It is false, and it makes "how many jobs are actually executing?" unanswerable — which is the first question during an incident |
| **Standard SQS queue + a Postgres advisory lock for single-writer** | More application code, a lock to hold across a 20-minute job, and a new failure mode when it is not released. FIFO gives the same guarantee as a queue attribute |
| **Put the decision in the resume message** | Breaks §20 row 8's "identifiers only, never state" for a value the worker can read from the audit trail it must read anyway ([ADR 0011](0011-the-human-gate-resume-moves-to-the-worker.md) decision 2) |
| **A `type: "start" \| "resume"` field on the message** | The checkpoint already knows, authoritatively. A field that can disagree with the checkpoint is a field that eventually will |
| **`MAX_JOB_RUNTIME` as a whole-job-lifetime bound** | It would fail a job on resume because a human took three days to approve, which is the exact behaviour the durable checkpointer was built to make cheap |
| **A hard timeout that interrupts the node in flight** | A thread killed mid-request leaves the checkpoint one node behind and the database possibly ahead of it (ADR 0005 decision 2). Between-node checking loses granularity and keeps every write consistent |
| **Delete the message on the final delivery after finalizing** | The job would be terminal and the DLQ empty, so the alarm that says something is broken would never fire |
| **Let a DLQ'd job stay non-terminal and rely on the alarm** | An alarm tells an operator; it does not tell `GET /jobs/{id}`. The submitter would poll a job that will never move |
| **Return `202` when the enqueue fails** | "Accepted for processing" is the one claim that is false in exactly that case |

## What a test has to prove before this ships

1. The migration widens `ck_jobs_status` before any code writes `queued`, and an old `JobStatus` cannot validate a `queued` row — the ordering gl §19 requires.
2. A duplicate delivery of a start message resumes from the checkpoint and does not restart: no second plan, no second set of findings.
3. Each of decision 5's four branches is taken for the state that should produce it, including `continue` after a simulated mid-run death.
4. The runtime bound fires between nodes, the job reaches `finalize` with `failure_reason="job_timeout"`, and no further LLM call is spent after it trips.
5. The worker refuses to start when `visibility <= MAX_JOB_RUNTIME + 3 × LLM_MAIN_TIMEOUT_S + 10`, and starts when it does not — asserted at both edges.
6. The final delivery finalizes the job with `job_dead_lettered` **and** leaves the message undeleted.
7. A failed enqueue leaves the row `queued` and answers `503 enqueue_failed` in the documented envelope.
8. Two gate visits produce different `MessageDeduplicationId` values — the same property ADR 0007 asks a test to pin for its visit key.

All of the above run offline against a fake queue. The SQS attributes themselves — FIFO group
behaviour, redrive, `ApproximateReceiveCount` — are verified against LocalStack in the
service-container suite, not in `pytest`.
