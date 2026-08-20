# ADR 0021 — Stale-job reconciliation and dead-letter recovery are operator-run, fenced, and evidence-led

- **Status:** **Accepted and built, 2026-08-20** (Phase 5 block C); **never applied to AWS**.
  Closes [ADR 0010](0010-job-dispatch-and-status-across-api-queue-and-worker.md) decision 9's
  deferred orphan sweep. No API, worker, FIFO, visibility, fencing or human-gate semantics change
- **Date:** 2026-08-20
- **Affects:** `operations.py` · `scripts/reconcile_jobs.py` · `scripts/inspect_dlq.py` ·
  `scripts/replay_dlq.py` · `jobqueue.py` · `database/queries.py` · `database/schema.py` ·
  `database/migrations/versions/rev_0004_*.py` · `config.py` · `infra/monitoring.tf` ·
  `docs/runbook.md` · `docs/deployment.md`
- **Supersedes nothing.** ADR 0005, 0007, 0010, 0011, 0015 and 0016 are all load-bearing here and
  all unchanged

---

## Context

The runtime already recovers almost everything on its own. A visibility heartbeat keeps a delivery
alive while a node runs ([ADR 0015](0015-visibility-leases-replace-static-duration-ownership.md)),
a PostgreSQL advisory lock fences one job to one executing writer when that heartbeat becomes unsafe
([ADR 0016](0016-postgresql-fences-per-job-execution.md)), every graph-time write is keyed so a
replayed node converges ([ADR 0005](0005-graph-time-persistence-semantics.md)), and a message is
acknowledged only on an established terminal or gate outcome
([ADR 0010](0010-job-dispatch-and-status-across-api-queue-and-worker.md) decision 6). A worker that
dies is a redelivery; a redelivery is a resume.

**One condition survives all of that, and it was recorded rather than fixed.** ADR 0010 decision 9
assigned it to "Phase 5's retention-and-expiry sweep":

> A hard kill leaves a job `running` forever. SIGTERM is handled; SIGKILL and OOM are not, and
> cannot be — nothing gets to write.

The message is redelivered and normally the worker recovers the job. But if the failure is the job
itself, the third delivery dead-letters it, and the worker's final-delivery handler is exactly the
code a SIGKILL prevents from running. What is left is a `jobs` row saying `queued` or `running`, a
message in the dead-letter queue, and nothing that will ever reconcile the two. `CLAUDE.md` carries
three sibling gaps of the same shape: a job whose enqueue failed sits `queued` with nothing to pick
it up; a dead-lettered message sits there with no alarm on it; and an exhausted artifact write needs
a person nothing tells.

There is a second, sharper variant that redelivery *can* reach and that the code already handles
badly enough to be worth naming: a gate node that dies after its keyed `record_gate_opened` write
but before its reconcile leaves a pending interrupt beside a row saying `running`. `worker.py`
repairs that on its own branch — but only if a delivery arrives. If the message dead-lettered first,
nobody is coming.

**And there is no operational signal at all.** Block A and block B built the deployment and its
credentials; neither built a single alarm, so every condition above is discovered by a person
looking. `docs/deployment.md` §8 lists alarms, the sweep and retention as block C's, which is this.

---

## Decision

### 1. Operational signals are native CloudWatch metrics and six alarms, and nothing else

No custom metric, no `PutMetricData`, no metric filter, no agent, no dashboard. Every alarm reads a
metric AWS publishes for the queue, the load balancer, the database or the cache. **An operational
signal the application has to remember to emit is a signal that goes quiet exactly when the
application is broken** — and it would need a permission on a task role that has none today.

Six alarms, each with a threshold derived from something the system already fixed:

| Alarm | Threshold | Why that number |
|---|---|---|
| `dlq-not-empty` | > 0 visible | Any message here means three deliveries failed and nobody has looked |
| `jobs-queue-backlog-age` | > 3600s | Longer than one invocation's `MAX_JOB_RUNTIME` (1200s), shorter than three 1800s deliveries (5400s) |
| `api-unhealthy-targets` | > 0 for 10 × 60s | Longer than the documented startup window in which `/health` is legitimately 503 |
| `api-target-5xx` | > 0 in 300s | This deployment serves requests by hand; one is an event |
| `rds-free-storage-low` | < 2 GiB of 20 | ≈10% remaining. A full disk stops every checkpoint, audit row and terminal status |
| `redis-memory-pressure` | ≥ 80% | An evicted `ratelimit:llm` key silently widens the shared window rather than breaking |

**Two alarms that would have been obvious are deliberately absent.** ECS `RunningTaskCount` lives in
the `ECS/ContainerInsights` namespace, which is a per-metric charge; the plain `AWS/ECS` namespace
has no task count at all. So API liveness is the ALB's unhealthy-target count and **worker liveness
is the queue's oldest-message age** — a worker that is not consuming is a queue that is ageing,
which is the symptom that actually matters. A long-running deployment should enable Container
Insights and alarm on the counts directly; `docs/deployment.md` §9 says so.

**None of these thresholds is claimed to be production-optimal.** Four are variables.

### 2. Age selects a candidate. It never authorises a change

`STALE_JOB_MIN_AGE_SECONDS` (default 7200) decides which `queued` and `running` rows are worth
*inspecting*, measured from the job's last durable activity — the newest `audit_events` row, not its
submission — so a job that has been checkpointing for ten minutes is not a candidate.

The default is derived: a message can spend three deliveries of the 1800-second visibility window in
flight before it is dead-lettered (5400s), and 7200 is that plus half an hour. A job still working
through legitimate redelivery is therefore not selected at all.

**Every mutation additionally requires all three of:** the per-job execution fence acquired, durable
state reread *after* acquiring it, and evidence specific to the outcome. **No job is ever failed
because it is old** — the closest thing to it needs a message sitting in the dead-letter queue to say
that no delivery is coming back.

`awaiting_approval` is not a candidate status. A job may legitimately wait at the gate for days; an
age-based sweep that could touch it would be the one design able to close a review nobody answered.
Gate expiry stays a separate, still-deferred decision, and keeping it separate means this tool
cannot perform it by accident.

### 3. The reconciler reuses ADR 0016's fence and never waits for it

The sequence is ADR 0016 decision 3's, with a reconciler in the worker's place:

```text
try pg_try_advisory_lock(job)  ->  busy: report `owned`, change nothing
                               ->  acquired: reread the row and the checkpoint
                                            -> decide -> apply -> release
```

**No second ownership mechanism is introduced.** A reconciler with its own notion of who owns a job
would be a second answer to a question that has one, and the first thing two ownership mechanisms do
is disagree. A timestamp is never ownership proof: it cannot distinguish a dead process from a slow
one, and PostgreSQL releasing the lock when its backend dies is precisely what makes a hard-killed
worker recoverable.

Unlike the worker it does not wait on a busy lock. A worker that gives up its turn burns one of
three deliveries; a sweep that gives up simply runs again later.

### 4. Repair from durable state before failing, and skip rather than guess

Seven outcomes, in this order, each with its own evidence:

| Outcome | Evidence | Write |
|---|---|---|
| `owned` | The fence is busy | none |
| `no_change` | `completed_at` is set, or the row already matches durable state | none |
| `repaired_gate` | The checkpoint holds a pending interrupt, the row does not say `awaiting_approval` | `set_job_status` |
| `repaired_terminal` | The checkpoint reached `approved`/`rejected`/`failed` | `finish_job` from the checkpoint's own values |
| `requeued` | `queued`, no checkpoint at all, and not dead-lettered | `send_start` with the job's original identifiers |
| `failed` | A message for this job is in the dead-letter queue and none of the above holds | `finish_job(failed, job_dead_lettered)` |
| `skipped` | Anything else | none |

`repaired_gate` comes first because it is the one state nothing else recovers from: both
`GET /jobs/{id}/gate` and `POST /jobs/{id}/approve` refuse a job that is not `awaiting_approval`, so
a pending interrupt beside a `running` row is a gate no person can ever answer.

`failed` reuses `job_dead_lettered` rather than inventing `job_orphaned`: it is the reason
`worker._finalise` already writes on a final delivery, and this sweep exists for the case where the
worker never got to write it.

`requeued` closes the sibling gap: `POST /jobs` answers `503 enqueue_failed` and **keeps the row on
purpose**, because it holds the `idempotency_key` a re-enqueue targets. Sending it is safe even if
the original message does exist — FIFO grouping and the fence still permit one worker, and the
second delivery finds the job terminal or continues it from the checkpoint.

**No new status and no new column.** Every outcome is expressed in the vocabulary that already
exists.

### 5. Every mutation is idempotent, attributed, and audited exactly once

Running the sweep twice must change nothing the first run did not, and the mutations are
self-limiting by construction: a repaired gate is already `awaiting_approval` the second time, a
finished job is not a candidate, `set_job_status` cannot talk over `completed_at`, `finish_job`
guards its own `job_finished` row, and a re-enqueue carries the job's original deduplication id.

Two of the four mutations finish no job and so write no `job_finished` row, which would leave a
durable row changed by a person with no record of who or why. So `audit_events` gains one action —
`job_reconciled`, migration `rev_0004` — carrying `{outcome, previous_status, reason}` and guarded
on `(job_id, outcome)` so a repeated sweep cannot inflate the trail. `--actor` is **required** to
apply, for `scripts/reexport_job.py`'s reason: `ck_audit_events_actor` refuses `unknown`, because a
repair with no identity behind it is not accountability.

**Dry run is the default** for both mutating tools. The report names every candidate, its evidence,
the proposed action and the reason; `--apply` is a word an operator types after reading it.

### 6. Dead-letter replay is per-message, state-checked, and preserves the message exactly

`StartMessageMoveTask` — AWS's own redrive — was evaluated and rejected. **It cannot inspect a job's
durable state before it moves a message**, and it moves all of them: a message reached that queue
because three deliveries could not make it work, so a blind redrive is the same outage repeated a
fourth time. Four of the states below are ones where moving a message would do nothing or do harm.

`scripts/replay_dlq.py` therefore requires `--job-id`, has no `--all`, defaults to a dry run, and
refuses:

| Refused when | Because |
|---|---|
| There is no `jobs` row | Nothing can run it |
| The job is terminal | The worker would delete it on its first branch; the only effect is another delivery |
| The checkpoint holds a pending interrupt, or the row says `awaiting_approval` | A reviewer's decision moves this job, not a message |
| The checkpoint is terminal beside a stale row | That is the reconciler's job, and re-running a graph is a very expensive way to write one row |
| The execution fence is busy | A fourth delivery pushed at a live job is the one thing a recovery tool must never do |

**The message that goes back is the message that came out**: the same three identifiers, the same
`MessageGroupId`, and the same `MessageDeduplicationId`. Minting a new deduplication id would break
[ADR 0007](0007-reviewer-decision-idempotency-and-gate-resume-failure.md)'s gate-visit key, under
which one visit is one message however many times it is sent. SQS's five-minute deduplication window
is not a hazard: a dead-lettered message is at least three failed deliveries old.

It sends first and deletes from the dead-letter queue only after the send returns — the reverse
order is a recovery that can lose a job. Deleting it afterwards is deliberate: a replayed message
left in place would keep the alarm on forever and be replayed again by the next operator. The body
is three identifiers and is logged before the delete.

`scripts/inspect_dlq.py` changes nothing at all. SQS has no peek, so it receives each message and
releases it with a zero visibility timeout — the queue an alarm is watching is not quietly emptied
by looking at it.

### 7. The tooling is three scripts run by a person, not a service

No Lambda, no EventBridge schedule, no Step Function, no always-on reconciler. Each would be an
operational service running full time for an environment that lives an hour, to do work three
deterministic scripts do on demand — and each would need its own IAM role, its own failure mode and
its own alarm.

**No IAM role or task permission was added.** The scripts run with the operator's own credentials —
the same ones that ran `terraform apply` — and `docs/runbook.md` names the actions they need. In
particular the worker still cannot receive from the dead-letter queue and the API still cannot
receive from anything: a recovery tool's reach must not become a running service's reach.

The decision logic lives in `operations.py` rather than in the scripts, so `decide` and
`replay_verdict` are pure functions of one `JobEvidence` value and every interesting case — a
terminal checkpoint beside a `running` row, a pending interrupt beside one — is a dataclass literal
in a test rather than three services arranged to produce it. **Nothing in the request or job path
imports it.**

### 8. Retention stays what it already is, explicitly, and is documented rather than extended

| Store | Retention | Decision |
|---|---|---|
| CloudWatch Logs | **1 day, explicit**, on all three Terraform-managed groups | A group ECS creates itself never expires, and that is storage that charges after every task has stopped |
| Dead-letter queue | 14 days, the maximum | The evidence a job failed must outlive the demo — deliberately outliving the logs that explain it |
| Jobs queue | 4 days | Unchanged |
| S3 reports | **No expiry rule** | The reports are the evidence this deployment exists to produce; a schedule could delete them before the screenshots |
| ECR | Keep the last 5 images | Unchanged |
| `jobs`, `findings`, `claims`, `audit_events` | **Nothing deletes anything** | `RETENTION_DAYS` still has no sweep. Deleting audit history during a demo destroys the only durable record of who approved what |

**No destructive database cleanup was added for checklist value.** What a long-running deployment
should do differently is written down in `docs/deployment.md` §9 rather than built here.

---

## Consequences

- The condition ADR 0010 decision 9 accepted now has a diagnosis, a repair and a record.
- An operator who does nothing still gets alarms; an operator who runs the tools cannot mutate a job
  a worker is holding, and cannot mutate anything at all without typing `--apply`.
- **A job can still be `skipped` indefinitely.** Ambiguous evidence produces no action by design, and
  the honest answer is that redelivery is still the recovery path for it.
- `INTERRUPT_CHANNEL` reads a private LangGraph constant, because building a compiled graph in an
  operator tool would drag five agents and an LLM client into a process that must not reach a model.
  A test drives the real graph to the real gate and asserts this reader agrees with
  `graph.get_state().interrupts`, so an upgrade that moves the channel fails a test rather than
  quietly reporting that no reviewer is waiting.
- Reading the dead-letter queue makes its messages briefly invisible. A release that fails costs 30
  seconds of a hidden message, which is logged.
- Six alarms is a set someone will actually read. It is also six things that can be wrong about a
  threshold, and every one of them is a variable for that reason.

## Alternatives rejected

| Option | Why not |
|---|---|
| Fail every non-terminal row older than N | Age cannot distinguish a dead process from a slow one. It would fail live jobs, and it is the single most damaging thing this tool could do |
| A lock table or an ownership row for the reconciler | A second answer to "who owns this job", with its own expiry and stale-row cleanup — the failure mode ADR 0016 removed |
| Add `jobs.updated_at` for staleness | `audit_events` already records every durable node event; a second home for the same fact is a second thing to keep true (ADR 0006 made the same call for edit counts) |
| AWS `StartMessageMoveTask` for DLQ redrive | Cannot inspect durable state per message, and moves all of them. Four of this ADR's five refusals become impossible to express |
| Automatic replay of every dead-lettered message | Repeats the outage that produced them |
| A Lambda or EventBridge-scheduled reconciler | An always-on service, its own role and its own failure mode, for an hour-long environment |
| Custom application metrics for queue depth and job age | Duplicates what SQS publishes free, and goes quiet when the application does |
| Container Insights for ECS task-count alarms | A per-metric charge; the ALB and queue-age alarms answer the same two questions from metrics already published |
| A CloudWatch dashboard | A second place to keep true, for six alarms already visible in the console |
| An S3 lifecycle expiry on reports, and a `RETENTION_DAYS` database sweep | Would delete the evidence the deployment exists to produce, on a schedule, during the demo |
| A new operational IAM role | The operator already holds the credentials that created the deployment |

## Verification required

1. A candidate whose execution fence is held by another process is `owned` and nothing is written.
2. Durable state is reread **after** the fence is acquired, never before.
3. A terminal checkpoint beside a `running` row produces the terminal row the worker would have.
4. A pending interrupt beside a `running` row produces `awaiting_approval`.
5. Ambiguous evidence produces `skipped` and no write.
6. A stale row is failed **only** with a dead-lettered message as evidence, never on age alone.
7. Running the sweep twice writes nothing the second time, and produces one audit row per outcome.
8. A dry run performs no database write and sends no message.
9. Replay is refused for a terminal job, a job at the gate, and a job whose fence is held.
10. A replayed message carries the original `MessageGroupId` and `MessageDeduplicationId`.
11. `awaiting_approval` is never a sweep candidate.
12. Terraform declares six alarms, the optional topic, and no dashboard, Lambda, schedule or new
    IAM role; every log group has explicit retention.
13. The offline suite proves all of the above with no AWS account, no network and no credential.
