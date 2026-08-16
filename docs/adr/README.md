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
