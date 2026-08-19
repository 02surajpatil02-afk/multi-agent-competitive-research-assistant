# Architecture Decision Records

One file per decision, named `NNNN-kebab-case-title.md` (`CLAUDE.md`). A record is written when a
decision changes something the guidance already states — not for every choice. Decisions that were
settled before implementation began live in `docs/ARCHITECTURE.md` §20 and are not restated here.

A record is never edited to reflect a later change of mind. It is superseded by a new one, and the
old record's status says so, because the reasoning that was current at the time is the useful part.

**A factual error in a measurement a record reports is the one exception**, because other documents
quote those numbers from the record and leaving a known-wrong figure in the source spreads the error.
It is corrected in place, with a dated audit note that keeps the original value and says what was
wrong. [ADR 0002](0002-concurrent-page-extraction-in-the-researcher.md) carries one.

**A record is indexed once it is accepted.** Every row below has been; a record still under
discussion carries `Status: Proposed` and stays out of the table until it is not.

**Accepted is not the same as built.** `0009`–`0012` were Phase 3 decisions taken before the code that
implements them, which is the point of taking them. **`0010`, `0011` and `0012` were built on
2026-08-17** (Phase 3 stage 2); `0009` was built by step 22a on **2026-08-18**. The `Status` line on each
record says which it is, and the Status column here repeats it.

**Two of ADR 0010's mechanisms were corrected by building them, and the corrections live in the code
rather than in an edit to the record.** `worker.py::_finalise` explains both: `update_state` followed
by `invoke(None)` runs one further node on the timeout path and reaches `finalize` on neither, so the
worker writes the terminal row itself. The requirement each decision states — a terminal job with a
`failure_reason`, and a message left for the DLQ — is met exactly.

| # | Decision | Status | Date |
|---|---|---|---|
| [0001](0001-supervisor-llm-routing-is-advisory.md) | The Supervisor's LLM routing call is advisory; `allowed_target(state)` is authoritative | Accepted | 2026-08-12 |
| [0002](0002-concurrent-page-extraction-in-the-researcher.md) | A subtopic's page extractions run concurrently; choosing and fetching sources stays sequential | Accepted | 2026-08-13 |
| [0003](0003-finding-ids-are-a-per-job-sequence.md) | Finding ids are a short per-job sequence (`f1`, `f2`, …), assigned after the extraction pool joins | Accepted | 2026-08-14 |
| [0004](0004-no-op-researcher-retries-after-evidence-exhaustion.md) | Reflection does not retry an `unresearched` subtopic; it retries an eligible one instead. Only when every subtopic is `unresearched` is the route dropped for another failing dimension, or the gate `below_threshold` | Accepted, corrected 2026-08-15 | 2026-08-14 |
| [0005](0005-graph-time-persistence-semantics.md) | Graph-time writes are one transaction per node event, keyed so a replayed node converges, append-only for the audit trail, and loud on failure | Accepted | 2026-08-15 |
| [0006](0006-reviewer-edit-returns-to-the-human-gate.md) | A reviewer `edit` is one Synthesizer pass **over existing evidence** and returns to the gate: scored, never a revision, never research. Bounded at 3 edits, refused when the live budget cannot fund it, and grounding is not relaxed for a reviewer | Accepted, built in step 17 | 2026-08-16 |
| [0007](0007-reviewer-decision-idempotency-and-gate-resume-failure.md) | One gate visit takes one decision, keyed on `(job_id, calls_used)`. Re-sending it is a retry that continues the graph and writes nothing; a different decision is `409 gate_already_decided`. `jobs.status` is derived from the checkpoint on both paths, and an unexpected failure leaves in the documented error envelope | Accepted, built | 2026-08-16 |
| [0008](0008-a-failed-jobs-reason-lives-in-the-checkpoint-for-phase-2.md) | A failed job's `failure_reason` gets no column and no API field in Phase 2: it lives in the durable checkpoint, and `GET /jobs/{id}` answers `status=failed`, `phase=failed`, `report=null`. The durable `job_finished` audit row is Phase 3's, with the two reasons that need it. Closes ADR 0005's recorded gap | Accepted | 2026-08-16 |
| [0009](0009-recovering-an-export-that-failed-after-approval.md) | An S3 write that fails after approval stays terminal. `exported_at` comes to mean "the artifact exists", which makes the recoverable set a query; recovery is an operator-run re-export of the durable body, not a route and not a new job. `job_finished` is built in Phase 3, which is what makes the reason durable | Accepted; **built 2026-08-18** (step 22a) | 2026-08-17 |
| [0010](0010-job-dispatch-and-status-across-api-queue-and-worker.md) | `JobStatus` gains `queued`; the worker moves it to `running` on receipt. The message is three identifiers and `attempt` is dropped, because a body cannot count redeliveries. The queue is **FIFO on `MessageGroupId = job_id`**, which is what keeps ADR 0005's single-writer rule true. `MAX_JOB_RUNTIME` is per invocation, checked between nodes. A DLQ'd job is finalized and its message still reaches the DLQ. Decision 8's static visibility proof is superseded by ADR 0015 | Accepted, **built**; decision 8 superseded | 2026-08-17 |
| [0011](0011-the-human-gate-resume-moves-to-the-worker.md) | `POST /jobs/{id}/approve` records the decision and enqueues; the worker resumes. The full decision is read back from `audit_events`, status reconciliation moves to the worker, and the endpoint returns `running`. ADR 0006's and ADR 0007's semantics are preserved exactly — only where the resume executes changes | Accepted, **built** — `routes/api.py`, `worker.py` | 2026-08-17 |
| [0012](0012-the-api-stops-holding-a-compiled-graph.md) | The API constructs no graph and no LLM client, and `LLM_*` leaves its environment. `phase` is derived from `jobs.status`; two checkpoint reads remain — the gate-visit key and the gate view — and reading durable state is not executing a graph | Accepted, **built** — `app.py`, `routes/api.py`, `config.py`, `worker.required_credentials` | 2026-08-17 |
| [0013](0013-reviewer-gate-payload-view.md) | The reviewer's gate view is its own route, `GET /jobs/{job_id}/gate`, returning `reviewer_payload()` verbatim from the checkpoint. No new schema, no new gate-visit identifier, no graph execution. The projection moves to `graph/state.py` so the API can reach it without the agent stack | Accepted, built | 2026-08-17 |
| [0014](0014-gate-review-history-is-not-snapshotted.md) | No `gate_snapshots` table and no full payload in `audit_events`. Historical gate-payload preservation is **not required** — four fields are lost when a checkpoint is pruned, and nothing prunes yet. Phase 5's retention design owns the trade; the fallback, if one is ever needed, is a snapshot at sweep time rather than at every gate visit | Accepted | 2026-08-17 |
| [0015](0015-visibility-leases-replace-static-duration-ownership.md) | Active SQS visibility renewal replaces ADR 0010 decision 8's static duration proof. Renew every one third of the queue lease; repeated failure or an invalid receipt relinquishes at the next checkpoint. Numeric LLM `Retry-After` is capped at 30 seconds. At-least-once delivery and checkpoint/idempotency recovery remain load-bearing | Accepted, **built** | 2026-08-19 |
| [0016](0016-postgresql-fences-per-job-execution.md) | A session-scoped PostgreSQL advisory lock enforces ADR 0005's one-writer precondition when SQS ownership becomes unsafe. A redelivery heartbeats while waiting, rereads durable state only after acquisition, and one synchronously checkpointed node is atomically admitted at a time | Accepted, **built** | 2026-08-19 |
| [0017](0017-deterministic-evaluators-and-a-custom-structured-judge.md) | Offline evaluation is twelve deterministic metrics plus one optional structured judge, run over **already-produced outputs** rather than by re-running the graph. Neither RAGAS nor DeepEval is added. No blended score, no threshold and no HOLDOUT in this block — Block C calibrates against the baseline this produces. The DEV benchmark is fixture-backed and asserts no external company fact | Accepted, **built** (Phase 4 block A+B) | 2026-08-19 |
