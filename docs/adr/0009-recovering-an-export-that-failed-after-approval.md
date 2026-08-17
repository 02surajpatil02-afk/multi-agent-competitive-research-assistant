# ADR 0009 — Recovering an export that failed after approval

- **Status:** **Accepted, 2026-08-17.** Not built. Blocks the S3 write, which is the first thing that
  makes `export_write_failed` reachable. Accepted unchanged — the reviewer-gate work of 2026-08-17
  ([ADR 0013](0013-reviewer-gate-payload-view.md),
  [ADR 0014](0014-gate-review-history-is-not-snapshotted.md)) touches nothing this record decides
- **Date:** 2026-08-16
- **Affects:** `graph/build.py` (`export_node`) · `database/queries.py` · `database/schema.py`
  (`AuditAction`) · `routes/api.py` (`GET /jobs/{id}/report`) · `scripts/` ·
  `docs/ARCHITECTURE.md` §3, §8, §10, §22 item 1 · `docs/engineering-guidelines.md` §9, §12
- **Found by:** The Phase 3 readiness audit of 2026-08-16. It is the one item
  `docs/ARCHITECTURE.md` §22 carries forward as open, and §22 says why it matters now: *"Needed
  before Phase 3 ships S3, which is also when `export_write_failed` first becomes reachable."*
- **Resolves:** ARCHITECTURE.md §22 item 1
- **Relates to:** [ADR 0008](0008-a-failed-jobs-reason-lives-in-the-checkpoint-for-phase-2.md)
  decision 4 assigns the durable `failure_reason` representation to Phase 3 and asks that it be
  decided *together with* this question. This record does that

---

## Context

§20 row 30 and §15 already fix the failure itself: the S3 artifact write gets 10s, 2 retries at 2s
and 8s (gl §17), and on exhaustion the job is `status=failed`, `failure_reason="export_write_failed"`,
with the report, claims, `claim_sources`, and the audit trail all preserved. Research and synthesis
are **never** re-run, because the report was already correct when the gate passed.

What is not decided is what happens next. §22 states the shape of the problem precisely:

> The job ends with a finished, approved, gate-passed report and no artifact. Re-running the graph
> would re-bill research and synthesis for a storage error, which the decision explicitly rules out —
> so recovery has to be a **re-export of existing work**. gl §12's API surface has no route for that,
> and none has been invented here. The options are an operational script, a new authenticated route,
> or accepting that a failed export is re-submitted as a new job. Each is a different answer about
> who is allowed to trigger a re-export, which makes it an authorization question as much as an
> operational one.

### What the current code already gives us, measured against the repository

| Fact | Where |
|---|---|
| The approved body is written to `jobs.report_json` and `exported_at` is stamped, in the **same transaction** as the `export_result` audit row | `database/queries.py::record_export_result`, called from `graph/build.py::export_node` |
| §8 keeps that write in Phase 3: *"`report_json` stays as the durable body the artifact is rendered from"* | ARCHITECTURE.md §8 |
| `GET /jobs/{id}` already carries `report?`, served from `jobs.report_json` | `routes/api.py::read_job` |
| §3 already says downloadability is answered by the artifact existing, not by the status | ARCHITECTURE.md §3, terminal states |
| `finish_job` and `set_job_status` both refuse to touch a row that has `completed_at` | `database/queries.py` |
| `audit_events.actor` has a CHECK refusing `unknown` and blank | `database/schema.py` |

Two things follow that shrink this problem to something small.

**The report is never lost — only the artifact is.** The body is in Postgres before the artifact
write is attempted, and `GET /jobs/{id}` serves it. A reviewer who approved a report can still read
it after the artifact write failed. What is missing is the S3 object and the presigned-URL route.

**Recovery is therefore a pure re-projection of a row that already exists.** It touches no LLM, no
tool, no agent, and no graph node other than the artifact write itself.

### How often can this happen

One `PutObject` of a small JSON body, with a 10s timeout and two retries. To exhaust it, S3 must be
unavailable for roughly twenty seconds at the moment a reviewer approves, in a system gl §19 sizes at
"a handful of jobs a day". This is a rare event, and the recovery mechanism should be proportional to
that rather than to how alarming the failure sounds.

---

## Decision

### 1. The export node's write order changes, and `exported_at` comes to mean "the artifact exists"

```text
export gate passes
  ↓
write jobs.report_json + the export_result audit row      (Postgres, 0 retries, gl §17)
  ↓
PutObject to S3                                            (10s, 2 retries at 2s and 8s, gl §17)
  ↓  written                        ↓  exhausted
stamp jobs.exported_at             status=failed, failure_reason="export_write_failed"
+ audit row                        report_json preserved, exported_at left NULL
```

**No schema change.** Both columns already exist; what moves is which transaction stamps
`exported_at`. Today `record_export_result` writes `report_json` and `exported_at` together, which
was correct in Phase 2 because there was no artifact and "exported" could only mean "the body was
stored". Once an artifact exists, that reading is wrong: a job whose PutObject failed would claim an
export date for an object nobody can fetch.

**This gives the recoverable set a SQL predicate rather than an investigation:**

```sql
SELECT job_id FROM jobs
 WHERE status = 'failed' AND report_json IS NOT NULL AND exported_at IS NULL;
```

`ix_jobs_status` already indexes the leading column. This is the same property `claim_sources` was
built for, applied one level up: *"which URL supports this sentence?"* became a query, and so does
*"which approved reports have no artifact?"*

### 2. The failure stays terminal. §15 and §20 row 30 are not reversed

The job ends `failed` with `failure_reason="export_write_failed"`. The bounded retry is not widened,
the message is not left for redelivery, and the graph is not re-entered. §20 row 30 was decided at
architecture review and nothing found since contradicts it.

### 3. `GET /jobs/{id}/report` keys on the artifact, not on the status

```text
exported_at IS NOT NULL  ->  200, a 15-minute presigned URL
otherwise                ->  404 not_exported
```

This is ARCHITECTURE.md §3's sentence made executable — *"whether a report is downloadable is
answered by the artifact existing"* — and it is what makes a recovered job's artifact reachable
without rewriting the job's history.

### 4. Recovery is an operator-run re-export, not a route and not a new job

`scripts/reexport_job.py`, run by a person with the worker's credentials.

| Question §22 asks | Answer |
|---|---|
| **Retry route?** | No. `POST /jobs/{id}/export` would widen gl §12's fixed five-endpoint surface for an event that requires S3 to be down for twenty seconds during an approval |
| **Operator route?** | **Yes, as a script.** It reads the predicate in decision 1, re-runs only the artifact write, and stamps `exported_at` on success |
| **New-job policy?** | No. §8 explicitly rules out re-billing research and synthesis for a storage error, and a resubmission would do exactly that |
| **Who is allowed?** | Whoever holds the worker's IAM role — database write plus the bucket prefix. Strictly smaller than `reviewer`, and it is the honest answer: re-export makes **no new judgement**. The reviewer's decision is already recorded; recovery finishes carrying it out |

**The script takes `--actor` and refuses to run without it.** `audit_events.actor` has a CHECK that
rejects `unknown` and blank, and it exists precisely so that a row cannot say a machine did something
a person did. Recovery writes `export_attempted` and `export_result` rows under the operator's
identity, so a recovered export is exactly as auditable as an original one.

**The job's status is not rewritten.** It stays `failed`. `finish_job` and `set_job_status` both
refuse a row with `completed_at` set, and that guard stays. The history is honest: the job failed at
export, and the artifact was recovered afterwards. Decision 3 is what makes the artifact reachable
anyway.

### 5. `job_finished` is built in Phase 3, and this decision is why it is needed

[ADR 0008](0008-a-failed-jobs-reason-lives-in-the-checkpoint-for-phase-2.md) decision 5 already
determined the shape and deliberately did not build it: *"the natural home is a `job_finished` audit
action carrying `{status, failure_reason}`, not a column on `jobs`."* That shape is adopted
unchanged. What this record adds is the trigger ADR 0008 asked for.

**Decision 1's predicate finds the candidates; `job_finished` is what tells you they are candidates.**
`status = 'failed' AND report_json IS NOT NULL AND exported_at IS NULL` is a strong signal, but it is
an inference. The reason itself lives only in the checkpoint, and ADR 0008 states the accepted risk
plainly: *"If a job's checkpoint is ever pruned, its `jobs` row still says `failed` for the remaining
retention window with nothing left to say why."* Recovery is the first operation that has to read
that reason from a durable place, so this is the phase where the row stops being optional — which is
what ADR 0008 decision 4 predicted.

**Cost: one Alembic revision**, extending `AuditAction` with `job_finished`; the
`ck_audit_events_action` CHECK is built from `get_args(AuditAction)`. `finalize_node` writes the row
in the same transaction as `finish_job`, which restores the sentence §3 had to withdraw.

**Still no `jobs.failure_reason` column.** ADR 0008's rejection of it stands: it would duplicate into
`jobs` a fact §5 assigns to the trail. And still no reason field on `GET /jobs/{id}` — decision 3 is
what a caller needs, and it is a `404` or a URL.

### 6. The escalation trigger, recorded so the next decision starts from here

Promote the script to an authenticated route if either happens:

- `export_write_failed` occurs more than once in production, or
- somebody who is not an operator legitimately needs to trigger a re-export.

Both would mean the event is no longer rare or the authorization answer above is no longer the right
one, and either is a real reason to widen gl §12's surface. Neither is true today.

---

## Consequences

- **The recoverable set is a query, and the recovery is a script.** No new route, no new role, no new
  status, no new column beyond the `job_finished` action ADR 0008 already designed.
- **A recovered job reads `status: "failed"` forever, with a downloadable artifact.** That is
  deliberate, and decision 3 is what stops it being contradictory. A client branches on the `report`
  route's answer, not on the status, when it wants the artifact.
- **`exported_at` changes meaning between Phase 2 and Phase 3.** In Phase 2 it meant "the body was
  stored"; from Phase 3 it means "the artifact exists". No Phase 2 job carries a `report_json` without
  an `exported_at`, so no historical row is reinterpreted — the two meanings coincide on every row
  that exists today.
- **The bounded-retry window is the only automatic protection.** Twenty seconds of S3 unavailability
  costs a job its artifact until an operator runs the script. Accepted, because the report is not lost
  and the reviewer can still read it.
- **`scripts/` gains a file that talks to production.** It is the second one — `measure_jobs.py` is
  the first — and like that one it is explicit about what it touches. It must never construct a graph,
  an `LLMClient`, or a tool.

## Alternatives rejected

| Option | Why not |
|---|---|
| **`POST /jobs/{id}/export` as a sixth route** | Widens a surface two documents specify exactly, and adds an authorization question (`reviewer`? `submitter`? a third role?) for an event measured in years. Decision 6 is the trigger that would change this answer |
| **Automatic re-export by leaving the SQS message undeleted** | Turns 2 retries into 3 deliveries × 3 attempts = 9 PutObjects, re-runs the export node's gate check each time, and reverses §20 row 30's "bounded, then terminal" without new evidence |
| **Re-submit as a new job** | §8 rules it out in terms: it re-bills research and synthesis for a storage error, and it produces a second job for one question that a reviewer then has to approve twice |
| **Flip the job to `approved` after a successful re-export** | Rewrites history, and both `finish_job` and `set_job_status` are built to refuse it. The job did fail; the artifact was recovered |
| **Add `jobs.failure_reason` so the predicate is exact** | ADR 0008 rejected the column with reasons that have not changed. `job_finished` puts the same fact in the table gl §9 already gives a retention rule |
| **Keep stamping `exported_at` when the gate passes** | Then `exported_at` claims an artifact that does not exist, and decision 1's predicate cannot be written at all |

## What a test has to prove before this ships

1. A PutObject that fails all three attempts leaves `status=failed`, `failure_reason="export_write_failed"`, `report_json` non-null, `exported_at` null — and leaves claims, `claim_sources` and the audit trail intact.
2. The retry schedule is exactly 10s / 2s / 8s (gl §17), asserted on attempt count and delays, as every other §17 row already is.
3. `GET /jobs/{id}/report` answers `404 not_exported` for that job, and `200` with a presigned URL once `exported_at` is stamped.
4. The re-export script writes `export_attempted` and `export_result` under the supplied actor, stamps `exported_at`, and refuses to run without `--actor`.
5. The re-export script never constructs a graph or an `LLMClient` — asserted by import, not by inspection.
6. `finalize` writes one `job_finished` row carrying `{status, failure_reason}`, in the same transaction as `finish_job`, and the migration widens `ck_audit_events_action` before any code writes it (gl §19's backward-compatible-for-one-release rule).
