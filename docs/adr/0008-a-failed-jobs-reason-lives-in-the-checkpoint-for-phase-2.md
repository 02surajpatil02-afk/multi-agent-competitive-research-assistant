# ADR 0008 — A failed job's reason lives in the checkpoint for Phase 2; the durable column is Phase 3's

- **Status:** **Accepted, 2026-08-16.** No schema change, no API change, one documentation correction
- **Date:** 2026-08-16
- **Affects:** `docs/ARCHITECTURE.md` §3 · `docs/ARCHITECTURE.md` §10
- **Found by:** The Phase 2 completion audit of 2026-08-16, closing the gap
  [ADR 0005](0005-graph-time-persistence-semantics.md) recorded and assigned to step 18
- **Resolves:** ADR 0005's "A failed job's `failure_reason` has nowhere durable to go"

---

## Context

ADR 0005 ended with two recorded specification gaps rather than decisions. The first —
`reflection_failed` missing from §9's action list — was closed by adding the action. The second was
left open with an explicit owner:

> **A failed job's `failure_reason` has nowhere durable to go.** §9's `jobs` has no such column and
> §9's action list has no job-finished action, so `finish_job` persists the status, the counters,
> `quality_flag`, and `completed_at` — and the reason stays in state and in the logs. Nothing has been
> invented to hold it. It is a real gap in the Phase 2 schema and belongs with the routes that would
> expose it (step 18), where `GET /jobs/{id}`'s documented response shape also has no field for it.

Step 18 shipped on 2026-08-16 without addressing it and **without recording a decision to defer it**,
which is what the completion audit found. This record is that decision.

### What the specification already fixes

- **The state contract.** gl §4: `failure_reason` is `str | None`, "Set whenever `status=failed`;
  never left `None` on a failure". `finalize_node` enforces it — a failed job arriving with no reason
  is rewritten to `unrecorded_failure` and logged as an error.
- **Where it was always meant to live.** ARCHITECTURE.md §5's mapping table, twice:
  `errors` → "`failure_reason` + `audit_events` — **one reason on state; the full history in
  Postgres**". The reason was specified as a state field from the start. The audit trail was specified
  as the history, not as a second copy of the reason.
- **The API contract.** gl §12 and §10 both give `GET /jobs/{id}` exactly
  `{job_id, status, phase, revision_count, quality_flag, report?}`. **There is no reason field, by
  specification.**
- **What "on state" now means.** Since step 14, `ResearchState` lives in the **Postgres checkpointer**
  keyed on `thread_id = job_id`. ADR 0005 was written the same day, when "state and the logs" still
  meant something that died with the process. It does not any more.

### What is actually missing, measured against the code

| Claim | Status |
|---|---|
| `jobs.failure_reason` column | Absent. Specified nowhere |
| A reason field on `GET /jobs/{id}` | Absent. Specified nowhere |
| `failure_reason` durable across a restart | **Present** — in the checkpoint, since step 14 |
| ARCHITECTURE.md §3: "finalize … **emits the audit event**" | **Absent, and it is a documentation error.** `finish_job` writes only the `UPDATE`, and `AuditAction` has no job-finished action — which is precisely the second half of ADR 0005's gap |

So exactly one documented statement is untrue, and it is a sentence rather than a schema.

### What the trail can already explain, and what it cannot

| `failure_reason` | Reconstructable from the `jobs` row and `audit_events`? |
|---|---|
| `uncited_claims` | **Yes** — `export_result` carries `{"result": "blocked", "uncited_claims": [...]}` |
| unscored quality | **Yes** — `reflection_failed` carries `{"quality_flag": "unscored"}`, and `jobs.quality_flag` agrees |
| `budget_exceeded`, `hop_limit` | **Partly** — `jobs.llm_calls_used` and `jobs.revision_count` are persisted at finalize for exactly this reason, and the trail's length shows where it stopped |
| `empty_plan`, `no_findings`, `unsourced_report`, `report_cites_unknown_findings` | **Partly** — the trail stops at the node that failed, so *where* is visible and *which* rule is not |
| `rate_limited`, `invalid_output` | **No** — transport-shaped, and they belong in the logs and the trace |
| `job_timeout`, `export_write_failed` | **Not reachable yet.** Nothing enforces `MAX_JOB_RUNTIME` until the Phase 3 worker, and S3 arrives in Phase 3 |

---

## Decision

**1. No `jobs.failure_reason` column in Phase 2, and no reason field on `GET /jobs/{id}`.** Neither is
specified, and adding either would widen a documented contract that nothing has asked to widen.

**2. For Phase 2, a failed job's reason lives in the durable checkpoint**, at
`ResearchState["failure_reason"]` for `thread_id = job_id`, alongside the logs and — for the two most
common cases — the audit trail rows above. This is ARCHITECTURE.md §5's "one reason on state" with
step 14's durability behind it, not a new arrangement.

**3. `GET /jobs/{id}` represents a failed job as `status: "failed"`, `phase: "failed"`,
`report: null`**, with `revision_count` and `quality_flag` carrying whatever the job reached. A caller
branches on `status`. This is the documented shape, unchanged.

**4. Phase 3 owns the final durable representation**, and it is the phase where it stops being
optional:

- the **worker** is the first component that owns a job's lifetime, which is what makes `job_timeout`
  reachable at all;
- **S3** arrives with it, which is what makes `export_write_failed` reachable — the one reason
  ARCHITECTURE.md §3 already singles out as needing to be "unambiguous to a caller";
- the recovery question for `export_write_failed` (ARCHITECTURE.md §22 item 1) is open and *also*
  Phase 3's, and it needs to know why a job failed. **The two should be decided together rather than
  half-answered now.**

**5. ARCHITECTURE.md §3's finalize sentence is corrected**, because it claims an audit row that is not
written. This is the one edit this record makes.

### If and when it is built, the shape is already determined

Not built now, and written down so the next decision starts from here rather than from scratch: the
natural home is a **`job_finished` audit action** carrying `{status, failure_reason}`, not a column on
`jobs`. It restores the sentence §3 wants to make true, it puts the reason in the table gl §9 gives a
365-day retention rule, and it costs one Alembic revision — the `ck_audit_events_action` CHECK is
built from `get_args(AuditAction)`, so a new literal needs a migration. A column would additionally
duplicate a fact the trail should own.

---

## Alternatives rejected

| Option | Why not |
|---|---|
| **Add `jobs.failure_reason` now** | A migration and a schema change for two reasons that cannot occur yet (`job_timeout`, `export_write_failed`) and several that the trail plus the counters already explain. It also duplicates into `jobs` a fact ARCHITECTURE.md §5 assigns to the trail |
| **Add the `job_finished` audit action now** | The right shape (see above) and the wrong time. It needs an Alembic revision to change the action CHECK constraint, and its most valuable payload is the two Phase-3 reasons. Building it now means migrating a constraint twice or guessing the payload |
| **Expose the reason on `GET /jobs/{id}` now** | Widens a response shape two documents specify exactly, for a Phase-2 API with one tenant, no worker, and no caller asking for it. gl §12 keeps this surface deliberately small |
| **Leave it undecided** | What ADR 0005 did, and the completion audit found it still open a phase later. An open gap that nobody re-reads becomes a silent omission |
| **Rely on the logs alone** | Logs are not queryable per job, are not retained under gl §9's rules, and structured logging is Phase 4. The checkpoint is the durable artefact that exists today |

---

## Consequences

- **A failed job's reason survives a restart**, because the checkpoint does. It is **not** answerable
  by a SQL query over `jobs`, and no report or dashboard may assume it is.
- **The accepted risk, stated plainly.** The checkpointer's tables are LangGraph's — Alembic never
  touches them (gl §19) and gl §9's retention rules do not cover them. If a job's checkpoint is ever
  pruned, its `jobs` row still says `failed` for the remaining retention window with nothing left to
  say why. **That is the trigger for Phase 3 to build decision 5's `job_finished` row**, and it is why
  this record fixes the phase rather than leaving the question open again.
- **ADR 0005's second gap is closed** — as a decision, not as code.
- **No migration, no API change, no new test.** The behaviour under test does not move; what moves is
  a sentence in §3 and the fact that the question now has an answer.
