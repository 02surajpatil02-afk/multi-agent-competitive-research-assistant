# ADR 0005 — Graph-time persistence: one transaction per node event, keyed writes, loud failures

- **Status:** Accepted
- **Date:** 2026-08-15
- **Affects:** `database/queries.py` · `graph/build.py` · `docs/ARCHITECTURE.md` §8, §9, §11, §15 ·
  `docs/engineering-guidelines.md` §9, §17
- **Found by:** Implementation steps 15–16. The schema, the audit vocabulary, and the failure policy
  are all specified; what a *replayed* node does to rows it already wrote is not

---

## Context

Step 15 requires the audit trail to be written **while the job runs**, not reconstructed after it
(§21). That turns each node into a writer, and a writer inside a graph that can replay a node raises
three questions the specification does not answer directly:

1. What is one transaction?
2. What happens when a node runs twice?
3. What happens when a write fails?

The specification does fix everything around those questions, and the answers below are derived from
it rather than invented:

- **The tables and their keys** — §9. `findings` is keyed `(job_id, finding_id)` ([ADR 0003](0003-finding-ids-are-a-per-job-sequence.md)),
  `claims` on `claim_id`, `claim_sources` on `(claim_id, finding_id)`, `audit_events` on a sequence.
- **`audit_events` is append-only** — §9. No row is updated or deleted except by the retention sweep.
- **A replay is expected, not an incident** — §11. Checkpoints are written per node, a crash loses at
  most the in-flight node, and the redelivered message resumes rather than restarts.
- **A database failure is loud** — §15 and gl §17: 5s statement timeout, **0 retries**, fail, and
  `/health` reports `db` unhealthy.
- **One job has one writer** — §11. SQS's 25-minute visibility timeout deliberately exceeds the
  20-minute job bound so two workers never process one job at once.

## Decision

**1. One transaction per node event, owned by the function that writes it.** Each function in
`database/queries.py` takes the `Engine` and opens its own transaction, so a caller cannot split one
in half. `record_research` writes a visit's findings *and* its `subtopic_researched` row together;
`record_claims` replaces the claim set *and* its `claim_sources` together; `record_export_result`
writes `report_json`, `exported_at`, *and* the `export_result` row together.

The rule this enforces: **an audit row never commits without the write it describes.** A row saying a
subtopic was researched, sitting next to no findings, is a record of something that did not happen.

**2. The write happens inside the node, before its state update.** LangGraph applies a node's update
and writes the checkpoint after the node returns, so there is no ordering in which the checkpoint
comes first. The database is therefore briefly **ahead** of the checkpoint, and a crash in that window
replays the node — which is what decision 3 exists for.

**3. Writes are keyed so a replayed node converges rather than duplicating.**

| Table | On replay | Why |
|---|---|---|
| `findings` | Insert what is new, refresh what is already there | Ids are numbered from what state already holds (ADR 0003), so a replayed node mints the same ids. `DO NOTHING` would keep the abandoned attempt's text under an id the checkpoint now uses for different evidence — an audit trail that quietly disagrees with the report |
| `claims`, `claim_sources` | Replaced wholesale for the current draft | One draft exists at a time (`schemas.py`). A revision writes a whole new draft with new claim ids, so "this job's claims" must mean the ones in the report, not two passes' worth |
| `audit_events` | Appended | The node **did** run twice. Two rows is the truth, and the table is append-only by specification |

**4. A failed write propagates out of the node.** No retry, no catch, no new `failure_reason`. §15
gives the database 0 retries and says a PostgreSQL failure fails loudly and takes the task out of
service. Translating it into `status=failed` here would invent a vocabulary word the specification
does not have; `export_write_failed` is explicitly the **S3** write's reason and begins in Phase 3
(§8).

**5. `actor` is `system` for what the graph did on its own, and the caller for what a caller did.**
`create_job` takes the authenticated submitter's identity; every graph-time row is `system`. The gate's
own rows — `gate_opened` and `reviewer_decision`, which carry the reviewer's identity — are **not**
written yet: they arrive with the authenticated endpoint that produces them, in step 17. A CHECK
constraint refuses `actor = 'unknown'` outright (gl §9).

## Consequences

- A replayed node leaves two `subtopic_researched` rows for one subtopic. That is intended, and a
  reader of the trail can tell replay from research because the findings count is the same.
- A database error surfaces as an exception from `graph.invoke()` rather than as a finalized job.
  Until the Phase 3 worker owns a job's lifetime, that is the only "loud" available — and it is what
  §15 asks for.
- The read-then-write in `_write_findings` is not concurrency-safe on its own. It does not need to be
  while §11's single-writer rule holds; if that ever changes, this is the line that has to change with
  it.

## Alternatives rejected

- **`ON CONFLICT DO NOTHING` on findings.** Cheaper and atomic, but it preserves the losing attempt's
  row under a live id. The database would then disagree with the checkpoint about what `f4` says.
- **Write everything at the end of the job.** Simpler code, and it destroys the property step 15 is
  for: a crashed job would have no trail at all, which is exactly when one is wanted.
- **Catch database errors and finalize the job.** It reads as robustness and behaves as a silent wrong
  answer: the job would end `approved` with an audit trail missing the rows that were supposed to
  prove it.

## Two specification gaps this surfaced, recorded rather than papered over

- **`reflection_failed` is required by §15 and missing from §9's action list.** §15 says the unscored
  path records an `audit_events` row; §9 enumerates the actions and does not include it. The
  vocabulary here follows the requirement, and §9's list is the one that should be corrected.
- **A failed job's `failure_reason` has nowhere durable to go.** §9's `jobs` has no such column and
  §9's action list has no job-finished action, so `finish_job` persists the status, the counters,
  `quality_flag`, and `completed_at` — and the reason stays in state and in the logs. Nothing has been
  invented to hold it. It is a real gap in the Phase 2 schema and belongs with the routes that would
  expose it (step 18), where `GET /jobs/{id}`'s documented response shape also has no field for it.
