# Operations runbook — Phase 5 block C

**What this is.** One page an operator can work from when something is wrong, with the thirteen
conditions this system can actually produce. Each entry says what the symptom looks like, where to
look, what is safe to do first, what not to do, and which command recovers it.

**What this is not.** An SRE manual. Every entry below corresponds to a failure the runtime already
has a defined behaviour for — `docs/ARCHITECTURE.md` §15 is the authority on what the *system* does,
and this page is what a *person* does about it.

**Three rules run through the whole page.**

1. **Read before you write.** All three block C tools default to a dry run and print their evidence.
   `--apply` is a word you type after reading it. (`reexport_job.py` is the exception and is not a
   block C tool: it acts immediately, because re-writing a stored report body to its own key is
   idempotent and there is nothing for a dry run to reveal.)
2. **Nothing here is failed because it is old.** The reconciler uses age only to decide which rows
   are worth inspecting; every change needs the per-job PostgreSQL execution fence, a fresh read of
   durable state, and evidence specific to the outcome
   ([ADR 0021](adr/0021-stale-job-reconciliation-and-dlq-recovery.md)).
3. **A busy execution fence means stop.** If a tool reports `owned`, a worker is running that job.
   That is the healthy answer, not an obstacle.

**The tools, and what each may change.** The first three are block C's; the fourth is ADR 0009's
and predates them, and it runs from the same place for the same reason.

| Tool | Changes | Default |
|---|---|---|
| `scripts/inspect_dlq.py` | **Nothing.** Reads the dead-letter queue and releases every message | read-only |
| `scripts/reconcile_jobs.py` | One `jobs` row per candidate, and one audit row | dry run |
| `scripts/replay_dlq.py` | Sends one named message back and deletes it from the DLQ | dry run |
| `scripts/reexport_job.py` | Writes one `reports/{job_id}.json` and stamps `exported_at` | **acts immediately** — there is no dry run, and `--actor` is required |

**Setting up.** Every command below assumes the deployment's outputs are to hand:

```bash
cd /path/to/repo && export CLUSTER=$(terraform -chdir=infra output -raw ecs_cluster_name)
```

```bash
export API=$(terraform -chdir=infra output -raw api_url) QUEUE=$(terraform -chdir=infra output -raw jobs_queue_url)
```

The Python tools read `DATABASE_URL` and `SQS_QUEUE_URL` from the environment, exactly as the
worker does; `reexport_job.py` additionally reads `S3_BUCKET`. **They need no LLM key, no Tavily
key and no Redis** — none of them can reach a model. The `ops` task definition supplies all
three, and the `ops` task role may send to the jobs queue, read and release the dead-letter
queue, and `s3:PutObject` under `reports/*` — nothing wider.

**In the AWS deployment they do not run on your laptop, and they cannot.** RDS is
`publicly_accessible = false` in subnets with no route off the VPC, so the only place any of them
can reach the database is inside it — as a one-off task, from the same image, exactly as the
migration runs. That is what the `ops` task definition is for. Define this once per session:

```bash
ops() { aws ecs run-task --cluster "$CLUSTER" --task-definition "$(terraform -chdir=infra output -raw ops_task_definition)" --launch-type FARGATE --network-configuration "awsvpcConfiguration={subnets=[$(terraform -chdir=infra output -json task_subnet_ids | tr -d '[]\"')],securityGroups=[$(terraform -chdir=infra output -raw worker_security_group_id)],assignPublicIp=ENABLED}" --overrides "{\"containerOverrides\":[{\"name\":\"ops\",\"command\":$1}]}"; }
```

Then `ops '["python","scripts/inspect_dlq.py"]'`, and read the output where it lands:

```bash
aws logs tail /ecs/competitive-research/ops --since 10m
```

Every `python scripts/...` command below is the command to put inside that override. The task's
**default** command is the reconciler's dry run, so starting it with no override writes nothing.

**Locally**, against `docker compose up`, run them directly or through the worker image:

```bash
docker compose --profile app run --rm --no-deps worker python scripts/inspect_dlq.py
```

---

## The alarms, and which entry each one sends you to

| Alarm | Means | Go to |
|---|---|---|
| `dlq-not-empty` | A message failed three deliveries | [DLQ contains messages](#dlq-contains-messages) |
| `jobs-queue-backlog-age` | The oldest message is older than an hour | [Worker not running](#worker-not-running), then [Jobs queue backing up](#jobs-queue-backing-up) |
| `api-unhealthy-targets` | The ALB cannot reach a healthy API for ten minutes | [API unhealthy](#api-unhealthy) |
| `api-target-5xx` | The application returned a 5xx | [API unhealthy](#api-unhealthy) |
| `rds-free-storage-low` | Under 2 GiB free | [RDS unavailable](#rds-unavailable) |
| `redis-memory-pressure` | Cache memory at 80% | [Redis unavailable](#redis-unavailable) |

There is deliberately **no alarm for a job waiting at the human gate**: its message was deleted when
the gate interrupted, so a three-day review is invisible to the queue and correct. See
[Job stuck awaiting approval](#job-stuck-awaiting-approval).

---

## API unhealthy

**Symptom.** `api-unhealthy-targets` or `api-target-5xx` is in ALARM; `curl $API/health` answers
`503 {"status":"degraded", ...}` or nothing at all.

**Where to look.** The body of `/health` names the failing dependency, and that is the whole
diagnosis — one boolean each for `db`, `redis` and `checkpoints`.

```bash
curl -s "$API/health"
```

```bash
aws logs tail /ecs/competitive-research/api --since 15m
```

**Safe first action.** Read which check is false and go to that entry: `db` →
[RDS unavailable](#rds-unavailable), `redis` → [Redis unavailable](#redis-unavailable),
`checkpoints` → **the first worker has not started yet**, which is expected for a few minutes after
a fresh deploy and is not a fault. `api-target-5xx` with `/health` at 200 is an application error:
the response body carries a stable error code and the job id, and the log line carries the reason.

**What NOT to do.** Do not weaken the ALB health-check matcher to accept 503. It answers 503
precisely when no job can run, and a deployment that reports healthy in that state is the one thing
`/health` exists to prevent. Do not restart the API to "fix" a false `checkpoints` — the worker
creates those tables, not the API.

---

## Worker not running

**Symptom.** `jobs-queue-backlog-age` is in ALARM. Jobs stay `queued`; `GET /jobs/{id}` never moves
to `running`.

**Where to look.** There is no task-count alarm — Container Insights is off for cost — so ask ECS
directly, then read the worker's own log.

```bash
aws ecs describe-services --cluster "$CLUSTER" --services "$(terraform -chdir=infra output -raw worker_service_name)" --query 'services[0].{desired:desiredCount,running:runningCount,events:events[:3]}'
```

```bash
aws logs tail /ecs/competitive-research/worker --since 30m
```

**Safe first action.** The worker **refuses to start** for three named reasons and says which in its
first log line: a missing provider credential, a Redis that does not answer, or a queue that is not
FIFO. Fix the named one. If the task is running and idle, check that `SQS_QUEUE_URL` in the task
definition is the queue you are watching.

**What NOT to do.** Do not raise `worker_desired_count` to clear a backlog. Worker count is bounded
by the LLM rate limit, not by queue depth — more workers on the same tier produce 429s and slower
jobs, which is why there is no autoscaling here at all.

**Recovery.** Restart the service once the cause is fixed; unacknowledged messages redeliver on
their own and resume from the checkpoint:

```bash
aws ecs update-service --cluster "$CLUSTER" --service "$(terraform -chdir=infra output -raw worker_service_name)" --force-new-deployment
```

---

## Redis unavailable

**Symptom.** `/health` reports `redis: false`; nodes fail with `rate_limiter_unavailable`;
`redis-memory-pressure` may be in ALARM instead.

**Where to look.** ElastiCache's own status, and the worker log for the failure reason.

```bash
aws elasticache describe-cache-clusters --cache-cluster-id competitive-research-redis --query 'CacheClusters[0].CacheClusterStatus'
```

**Safe first action.** Understand which half is broken before acting. **The caches and the per-job
URL set fail open** — they cost a call each and nothing stops. **The shared rate limiter fails
closed** — no token, no LLM call — because a limiter that fails open is not a limiter
(`ARCHITECTURE.md` §20 row 29). So a Redis outage stops LLM work deployment-wide, loudly and by
design, and the API reporting `degraded` is what stops new jobs arriving in the first place.

Memory pressure without an outage is worth acting on for one specific reason: an evicted
`ratelimit:llm` key does not break anything, it silently widens the window every worker shares.

**What NOT to do.** Do not make the limiter fail open to get moving again. Do not add a second
Redis: one window shared by every worker is the entire point.

**Recovery.** Restore the cluster. Nothing in Redis needs recovering — it is two caches, a 6-hour
URL set and a 60-second window. Jobs whose nodes failed with `rate_limiter_unavailable` are
terminal and are re-submitted, not resumed.

---

## RDS unavailable

**Symptom.** `/health` reports `db: false`, or `rds-free-storage-low` is in ALARM. Nodes fail
loudly with a database error rather than degrading.

**Where to look.**

```bash
aws rds describe-db-instances --db-instance-identifier competitive-research-postgres --query 'DBInstances[0].{status:DBInstanceStatus,storage:AllocatedStorage}'
```

**Safe first action.** For an outage: nothing application-side. Database failures get **0 retries**
and fail the node loudly (`guidelines` §17), the task leaves the target group, and unacknowledged
messages redeliver once the database is back. For low storage: increase `db_allocated_storage` and
apply — the alarm fires at roughly 10% remaining, which is a day of headroom at any rate this
deployment can produce.

**What NOT to do.** Do not delete rows to free space. `audit_events` is append-only and is the only
durable record of who approved what; `findings` carry the verbatim evidence that makes a claim
explicable. Do not restart tasks in a loop — they will fail identically and burn deliveries.

**Recovery.** Once the database answers, `/health` returns 200 within two check intervals and the
queue drains. Any job whose row was left non-terminal is a
[stale row](#stale-queued-or-running-row).

---

## Jobs queue backing up

**Symptom.** `jobs-queue-backlog-age` in ALARM with the worker healthy and consuming.

**Where to look.**

```bash
aws sqs get-queue-attributes --queue-url "$QUEUE" --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible ApproximateAgeOfOldestMessage
```

**Safe first action.** Read the two counts together. `NotVisible = 1` with a rising age is **one job
running for a long time**, which is ordinary — research takes minutes and the metric counts
in-flight messages. `Messages > 0` with `NotVisible = 0` and a healthy worker means the worker is
not receiving: check its `SQS_QUEUE_URL`. A large `Messages` with a working worker is simply
throughput, and the bound on throughput here is the LLM tier.

**What NOT to do.** Do not purge the queue. Do not add workers (see
[Worker not running](#worker-not-running)). Do not lower the queue's visibility timeout — the
worker derives its heartbeat cadence from it and refuses a queue whose window it cannot renew
inside.

---

## DLQ contains messages

**Symptom.** `dlq-not-empty` in ALARM.

**Where to look.** Start here, always. It reads the dead-letter queue **without consuming it** and
puts every message beside its job's durable state:

```bash
python scripts/inspect_dlq.py
```

Each line gives the job id, whether the message was a `start` or a gate `resume`, how many
deliveries it took, the `jobs` row's status, and one word for what the checkpoint holds.

**Safe first action.** The checkpoint column decides which tool comes next:

| Checkpoint | What happened | Next |
|---|---|---|
| `terminal:*` and the row is unfinished | The job ended; only its row is stale | `reconcile_jobs.py` |
| `awaiting-reviewer` | A reviewer is owed a gate the API cannot show them | `reconcile_jobs.py` |
| `unfinished:*` or `none`, row `queued`/`running` | Three deliveries failed at something | Fix the cause, then `replay_dlq.py` |
| Row already `approved`/`rejected`/`failed` | Nothing to do; the message is history | Delete it by hand once you have read it |

**What NOT to do.** **Do not redrive the whole queue.** AWS's `StartMessageMoveTask` moves every
message without looking at any job's state, and a message reached this queue because three
deliveries could not make it work — moving them all is the same outage repeated a fourth time. Do
not delete a message before you have read what it names.

**Recovery.** Repair the rows, then replay only what is genuinely recoverable, one job at a time:

```bash
python scripts/reconcile_jobs.py
```

```bash
python scripts/replay_dlq.py --job-id <id>
```

```bash
python scripts/replay_dlq.py --job-id <id> --apply
```

A replay puts back **the message that came out** — same `MessageGroupId`, same
`MessageDeduplicationId` — so FIFO ordering and the gate-visit key survive it, and the worker
handles it exactly as it would have handled the original delivery. It is refused for a terminal
job, for a job waiting at the gate, for a job with a terminal checkpoint, and for a job another
process is running.

---

## Stale queued or running row

**Symptom.** `GET /jobs/{id}` says `queued` or `running` and has not changed for hours. No message
is in flight for it. This is the residual condition ADR 0010 decision 9 accepted and deferred: a
process killed hard — SIGKILL, OOM, a task the platform reclaimed — writes nothing on the way out.

**Where to look.** The sweep is the diagnosis. It defaults to a dry run and prints, per candidate,
the evidence and the action it would take:

```bash
python scripts/reconcile_jobs.py
```

```sql
SELECT job_id, status, created_at FROM jobs WHERE completed_at IS NULL AND status IN ('queued','running') ORDER BY created_at;
```

**Safe first action.** Read the dry run. The outcomes mean:

| Outcome | Meaning |
|---|---|
| `owned` | A worker holds the execution fence. **Leave it alone** — this is healthy |
| `no_change` | The row and durable state already agree |
| `repaired_gate` | The checkpoint holds a pending interrupt; the row will be set to `awaiting_approval` so a reviewer can answer it |
| `repaired_terminal` | The checkpoint reached a terminal status; the row will be finished from the checkpoint's own values |
| `requeued` | Nothing ever ran this job and no message holds it; a start message will be sent |
| `failed` | Its message is in the dead-letter queue and no recoverable state remains |
| `skipped` | The evidence is ambiguous. Redelivery is still the recovery path — run it again later |

**What NOT to do.** Do not update `jobs.status` by hand in SQL. `finish_job` is the only writer of
`completed_at` and it writes the `job_finished` audit row in the same transaction; a hand-edited row
is a status with no explanation behind it. Do not lower `--min-age-seconds` to reach a job that is
probably still running — the fence will refuse it anyway, and the default is derived from the
redelivery window on purpose.

**Recovery.**

```bash
python scripts/reconcile_jobs.py --apply --actor you@example.com
```

`--actor` is required and is written to `audit_events.actor`. Running it twice is safe: the
mutations are self-limiting and the audit row is keyed on `(job_id, outcome)`.

---

## Job stuck awaiting approval

**Symptom.** `GET /jobs/{id}` says `awaiting_approval` and has for days. **No alarm covers this and
none should**: the message was deleted when the gate interrupted, so nothing is queued, nothing is
ageing, and a slow reviewer is not an incident.

**Where to look.** Read what the reviewer is being asked to approve:

```bash
curl -s "$API/jobs/$JOB_ID/gate" -H "Authorization: Bearer $TOKEN"
```

**Safe first action.** Ask the reviewer. If `GET /jobs/{id}/gate` answers `409` for a job the
checkpoint really has paused, the row is stale rather than the gate — that is
[a stale row](#stale-queued-or-running-row) and `reconcile_jobs.py` reports `repaired_gate`.

**What NOT to do.** **The reconciler will never close a gate**, and that is deliberate:
`awaiting_approval` is not a candidate status at all, so no sweep can decide a review on a
reviewer's behalf. Do not add it. **Gate expiry is still not built** — the 7-day sweep is a
separate, still-deferred decision, so today a job waits indefinitely and a person is the only thing
that ends it.

---

## S3 export failed

**Symptom.** A job is `failed` with `failure_reason = "export_write_failed"`, holds a complete
approved report, and has no downloadable artifact. `GET /jobs/{id}/report` answers `404
not_exported`.

**Where to look.** Nothing polls for this and no alarm fires — the recoverable set is a query:

```sql
SELECT job_id FROM jobs WHERE status='failed' AND report_json IS NOT NULL AND exported_at IS NULL;
```

**Safe first action.** Confirm the bucket is reachable, then re-export. The report was already
correct when the gate passed; this is a storage error and the recovery re-projects a stored row.

**What NOT to do.** **Do not re-submit the question.** Research and synthesis are never re-run for a
storage failure — that would re-bill the whole pipeline. Do not edit the job's status: it stays
`failed` forever *and* gains a downloadable artifact, which reads like a contradiction and is not
(`GET /jobs/{id}/report` keys on `exported_at`, never on the status).

**Recovery.** In AWS this runs as an `ops` task override like the other three — the `ops` task
definition carries `S3_BUCKET` and its task role may `s3:PutObject` under `reports/*` for exactly
this command:

```bash
ops '["python","scripts/reexport_job.py","<job_id>","--actor","you@example.com"]'
```

Locally, or wherever the database is directly reachable:

```bash
python scripts/reexport_job.py <job_id> --actor you@example.com
```

**It has no dry run and `--actor` is not optional.** Running it twice is harmless — the key is
derived from the job id, so a re-export overwrites rather than accumulating a second copy.

---

## Provider outage

**Symptom.** Jobs fail with `rate_limited`, or nodes fail after their retries with a timeout. The
worker log shows the LLM or Tavily endpoint failing.

**Where to look.**

```bash
aws logs tail /ecs/competitive-research/worker --since 30m --filter-pattern "rate_limited"
```

**Safe first action.** Wait, then resubmit. The retry schedules are already exhausted by the time
you see this — every one of them is bounded and every one has a defined give-up (`ARCHITECTURE.md`
§15), which is why a provider outage produces failed jobs rather than a hung worker. **A rate-limited
job fails visibly; it never silently produces a shorter report.**

**What NOT to do.** Do not raise `LLM_RPM_LIMIT` to push through a 429 — that is the shared window
every worker takes a token from, and widening it makes the 429s worse. Do not replay the
dead-lettered messages until the provider is back; they will fail three more times.

**Recovery.** Once the provider is healthy, jobs that failed are re-submitted as new jobs. Jobs whose
messages are still in flight resume from their checkpoints without replaying completed nodes.

---

## Lease ownership lost

**Symptom.** The worker log carries `stopping at a checkpoint because the visibility lease is
unsafe`, or `another worker owns execution; maintaining visibility while waiting`. Jobs make
progress in bursts.

**Where to look.**

```bash
aws logs tail /ecs/competitive-research/worker --since 30m --filter-pattern "lease"
```

**Safe first action.** Usually nothing. This is the design working: the worker renews its SQS
visibility every third of the queue's window, and when renewal becomes unsafe it finishes the node
in flight, checkpoints, and **does not acknowledge** — so redelivery resumes from the checkpoint.
The PostgreSQL execution fence is what stops the replacement running beside the old owner. Repeated
occurrences mean SQS is being slow or the task is starved; check task CPU and memory.

**What NOT to do.** Do not raise the queue's visibility timeout to "give it more room" — the worker
derives its heartbeat from that number, and the two must stay in step with what
`docker-compose.yml` and `infra/data_stores.tf` already agree on. Do not run two workers to speed
things up; that is what the fence exists to survive, not an optimisation.

---

## Migration failure

**Symptom.** The one-off migration task exits non-zero, or the API and worker start against a
schema that is not current.

**Where to look.**

```bash
aws logs tail /ecs/competitive-research/migrate --since 30m
```

**Safe first action.** Read the Alembic error, fix the revision, and run the task again. **Nothing
starts a migration automatically**, and neither long-running process runs one — that ordering is
what makes this recoverable at all: a failed migration means the schema did not change, not that it
changed halfway.

**What NOT to do.** Do not run `alembic upgrade head` from inside a running API or worker task. Do
not apply migrations from two places at once. Do not touch LangGraph's four checkpoint tables —
they are created by the worker's `setup()` and Alembic deliberately ignores them.

**Recovery.** Re-run the migration task, confirm exit code 0, then deploy the service revision.

---

## Authentication failure

**Symptom.** Every authenticated call answers `401`, or a caller who should be allowed gets `403`.

**Where to look.** The two answers mean different things. `401` is "this credential is not
accepted"; `403` is "it is, and this role may not do that".

```bash
curl -s -o /dev/null -w '%{http_code}\n' "$API/jobs/00000000-0000-4000-8000-000000000000" -H "Authorization: Bearer $TOKEN"
```

```bash
aws logs tail /ecs/competitive-research/api --since 15m
```

**Safe first action.** Check which mode the deployment is in — `terraform -chdir=infra output -raw
auth_mode` — because only one is ever live. Under `cognito`, a `401` is an expired token far more
often than anything else: access tokens last one hour, which is about the life of the whole
deployment. Get a new one. Under `api_key`, confirm the key's sha256 is in the table the API was
given. A `403` on the approve route means the user is a `submitter`, not a `reviewer`.

**What NOT to do.** **Do not set `AUTH_MODE` to accept both.** An API that took either credential
would be exactly as strong as the weaker one, and the weaker one is a shared secret with no expiry.
Do not disable authentication to "unblock" a demo: the gate is an authorization decision, and an
approval with no identity behind it is not a backstop.

---

## What this block still does not cover

| Not covered | Where it belongs |
|---|---|
| **Gate expiry** — a job waits at the human gate indefinitely | Still deferred. It is a policy decision about closing someone else's review, not an engineering gap |
| **Retention** — nothing deletes a job, a finding, an audit row or an S3 object on a schedule | Deliberate for a temporary deployment; `RETENTION_DAYS` has no sweep behind it yet |
| **Anything automatic** — every tool here is run by a person | An always-on reconciler for an hour-long environment would cost more than the condition it watches for |
