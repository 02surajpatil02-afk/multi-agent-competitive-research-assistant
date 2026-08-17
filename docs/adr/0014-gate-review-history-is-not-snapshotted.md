# ADR 0014 — Gate-review history is not snapshotted; retention owns the trade

- **Status:** **Accepted, 2026-08-17.** No code, no schema, no migration. It records a decision not
  to build something, and hands a constraint to the phase that can act on it
- **Date:** 2026-08-17
- **Affects:** `docs/engineering-guidelines.md` §9 (retention) · `docs/ARCHITECTURE.md` §15 (the
  human-rejection row) · implementation step 32, Phase 5
- **Found by:** The gate-history inspection of 2026-08-17, run before accepting ADRs 0009–0012
- **Relates to:** [ADR 0013](0013-reviewer-gate-payload-view.md) built the route this record decides
  not to extend backwards in time. [ADR 0008](0008-a-failed-jobs-reason-lives-in-the-checkpoint-for-phase-2.md)
  is the precedent and the sibling: it recorded the same shape of risk — a fact that lives in the
  checkpoint while the `jobs` row outlives it — and this record does the same for the gate payload

---

## Context

[ADR 0013](0013-reviewer-gate-payload-view.md) added `GET /jobs/{job_id}/gate`, which rebuilds
`reviewer_payload(state)` from the checkpoint **while the job is `awaiting_approval`**. After a
decision the route answers `409`, because it is a decision surface and not a history API.

That raises a question worth answering deliberately rather than discovering later:

> *"What exactly did the reviewer see when they approved or rejected this job?"*

### The retention asymmetry that creates the question

| Data | Retention (gl §9) |
|---|---|
| `jobs`, `claims`, `claim_sources`, `audit_events` | 12 months |
| `findings.evidence` | 12 months |
| **Checkpoints for closed jobs** | **30 days after close** |

So a closed job's row and trail outlive its checkpoint by eleven months.

**No sweep implements any of this.** `retention_delete` is in `AuditAction` and in the migration,
`RETENTION_DAYS` is in `config.py`, and nothing in the repository deletes anything. The sweep is
Phase 5 step 32. **Nothing has ever been pruned, and nothing can be until that job is written.**

### What actually survives, traced field by field

Most of the payload is already durable on the 12-month rule:

| Payload field | Durable after pruning? | Where |
|---|---|---|
| `job_id`, `revision_count`, `quality_flag` | ✅ | `jobs`, written at `finalize` |
| `llm_calls_used` | ✅ | `gate_opened.detail.calls_used` — ADR 0007's visit key |
| `unsupported_claims`, `unresearched_subtopics` | ✅ counts as seen at the gate; ids from `claims.supported` and the `subtopic_researched` rows | `audit_events`, `claims` |
| `claims[].claim_id / text / sources / supported / note` | ✅ | `claims`, `claim_sources`, `findings.url` |
| `report` — for an **exported** job | ✅ | `jobs.report_json` |
| decision, note, edits, actor, timestamp | ✅ | `reviewer_decision` |

### The four things that do not survive

1. **The reflection score** — the five dimensions, the weighted score, and the rationale.
   `record_revision` is the only writer of a score, so a job that passes first time records none.
2. **`failed_dimensions`** — same writer, same gap.
3. **Every `Verdict.quote`.** `claims` carries `supported` and `verdict_note` and **no quote column**;
   gl §9's own schema sketch omits it too.
4. **`report.sections[].body`** for a **rejected or failed** job. `record_export_result` never ran,
   so `jobs.report_json` is `NULL`, and no table holds section prose.

**The grounding chain is not among them.** `claim → finding → evidence quote → URL → content_hash`
is intact for twelve months, which is the reproducibility gl §9 actually argues for.

### Whether any requirement asks for the rest

| Requirement | What it asks | Met without a snapshot |
|---|---|---|
| CLAUDE.md invariant 7 | Every decision records **who** made it | ✅ `reviewer_decision.actor`, with a CHECK refusing `unknown` |
| CLAUDE.md invariant 1 | Every claim in an exported report traces to a source URL | ✅ `claims` + `claim_sources` + `findings` |
| gl §9, audit trail | Records every **transition** worth reconstructing, `gate_opened` and `reviewer_decision` among them | ✅ both rows are written |
| gl §9, reproducibility | `retrieved_at` + `content_hash`, so a March claim is explicable in June | ✅ `findings.evidence` is on the 12-month rule |
| gl §16 | Every authenticated identity reaches `audit_events.actor` | ✅ |
| gl §15, evaluation | 30–50 curated questions **with expected evidence** | ✅ — it needs a dataset, not an archive of past jobs |

The requirements ask **who decided**, **what they decided**, and **what the claims rest on**. None
asks what the screen showed.

---

## Decision

**1. No `gate_snapshots` table.**

**2. No full reviewer payload in `audit_events.detail`.** `gate_opened` keeps carrying counts, as
`_gate_summary` already writes them, "without the report in it".

**3. No evaluation-specific fields, columns, or tables.** Not now, and not as a side effect of
anything in this record.

**4. The current design is accepted as sound.** Not tolerated — accepted. Nothing prunes today; what
would eventually be lost is the four items above rather than "the record of the decision"; and the
decision, its author, its context counts and the whole grounding chain live in Postgres for twelve
months either way.

**5. Historical gate-payload preservation is recorded as *not required*.** It is a **desirable future
capability, not a Phase 2 or Phase 3 requirement.** Stating that plainly is most of the value of this
record: without it, the next reader finds a checkpoint-shaped hole and re-litigates it.

**6. Phase 5's retention design owns the trade.** Step 32 is the first code that can destroy
anything, so it is the first place the question is real. What this record hands it is the loss list
above, so the sweep's retention rule is chosen **knowing** what it destroys rather than discovering
it afterwards.

### The options that were considered

| | Option | Verdict |
|---|---|---|
| **A** | Full payload in `audit_events.detail` at gate-open | Rejected **now**, preferred **if ever needed**. Zero schema change, already keyed `(job_id, calls_used)`, already append-only by specification, already on the 12-month rule, already cascading. Its cost is that it makes the audit trail's size track report size — and gl §9 calls that trail "the product" |
| **B** | A dedicated immutable `gate_snapshots` table on `(job_id, calls_used)` | Rejected. It buys exactly one thing over A — keeping the timeline table small — and pays a sixth table, a migration, a drift test, a queries module and a retention rule for it. gl §9's five-table schema is deliberately small, and CLAUDE.md §3 asks what a new structure is for |
| **C** | **Keep the current design** | **Selected** |

### Why C

- **No requirement asks for it** (the table above).
- **Nothing can be lost yet.** The pruning is Phase 5's, so building storage now pays for a loss no
  code can currently cause.
- **What would be lost is four specific fields**, not the audit trail, not the decision, and not the
  claim-to-URL chain.
- **It keeps one source of truth for the payload.** A and B both give
  `GET /jobs/{job_id}/gate` two backing stores — the checkpoint while open, a snapshot once closed —
  and two paths that can disagree about one contract.
- **It keeps one copy of every third-party quote.** A and B duplicate fetched page text into a second
  table; gl §9's PII note covers one copy today, and a second is a second thing to redact if any of
  its three stated triggers ever fires.

### The preferred fallback, if a preservation requirement appears

**Snapshot at retention/sweep time, immediately before the checkpoint is deleted — not at every gate
visit by default.**

It is strictly cheaper than snapshotting on the way in: one row per *closed, aged* job instead of one
row per gate visit for every job forever, and only for jobs that survive to the pruning boundary. It
is also available precisely because ADR 0013 moved `reviewer_payload()` into `graph/state.py`, so a
sweep can call it with no graph, no agent, and no LLM client. If it is ever built, it should be
**Option A's shape** — an `audit_events` row keyed on the `(job_id, calls_used)` the trail already
uses — rather than Option B's table.

### Revisit triggers

Any one of these makes the question live again, and the answer starts from the fallback above:

- an **external audit obligation** that requires showing what a reviewer was shown;
- a **second reviewer identity** or a multi-reviewer workflow, where "which of them saw what" stops
  being answerable by inspection;
- **Phase 4 evaluation** genuinely needing historical reviewer context — see below, and note that
  gl §15's dataset is curated, not harvested;
- a **concrete product requirement for historical gate replay**.

---

## Future scope — Phase 4, not implemented and not a Phase 3 requirement

```text
gate snapshot
    +
reviewer decision
    +
final outcome
    ↓
evaluation dataset
```

Potential uses: rubric calibration · reflection evaluation · retrieval evaluation · synthesizer
evaluation · reviewer-disagreement analysis.

**Recorded as an idea only.** It is not implemented, it is not a Phase 3 requirement, and **no
evaluation-specific schema is created by this record.** The inspection specifically looked for fields
that would be independently required for *auditability* — which would have justified them regardless
of Phase 4 — and found none. The two candidates it surfaced, `Verdict.quote` and a reflection score
for a passing pass, are both absent from gl §9's own schema sketch, so adding either would be
inventing a requirement rather than meeting one.

---

## Consequences

- **A closed job's gate payload is not recoverable once its checkpoint is pruned**, and the four
  fields that go with it are named rather than discovered.
- **`GET /jobs/{job_id}/gate` stays "open gates only"**, with one backing store and one code path.
- **Phase 5 step 32 inherits a constraint, not a free choice.** Its retention rule has to be selected
  against the loss list, and this record is what makes that possible.
- **gl §9's retention table and ARCHITECTURE.md §15's rejection row are corrected** so "state
  retained" no longer reads as an unlimited promise that the 30-day checkpoint rule contradicts.
- **No migration, no schema change, no new test.** Nothing under test moves.

## Alternatives rejected

| Option | Why not |
|---|---|
| **Build Option A now** | Pays storage and audit-table bloat for a loss that no code can currently cause, since nothing prunes |
| **Build Option B now** | All of the above, plus a table, a migration, a drift test and a retention rule |
| **Extend `GET /jobs/{id}/gate` to closed jobs from the trail** | Two backing stores for one contract, and the trail cannot supply four of the payload's fields anyway — so the closed-job answer would silently differ from the open-gate one |
| **Add `score` and `failed_dimensions` to `gate_opened.detail`** | A few hundred bytes, and genuinely tempting — but unrequested. `jobs.quality_flag` durably records the outcome the decision actually turned on. This is the named smallest increment if a revisit trigger fires, not something to do pre-emptively |
| **Add a `Verdict.quote` column to `claims`** | gl §9's schema sketch omits it, so the schema matches the specification. `findings.evidence` — the quote gl §9 does argue for — is already kept for twelve months |
| **Align checkpoint retention with the 12-month job retention** | A real option, and it is Phase 5's to weigh: it trades storage for reach and needs no snapshot at all. Deciding it here would be deciding it without the sweep whose design determines the cost |
| **Leave it undecided** | What ADR 0005 did with `failure_reason`, and the completion audit found it still open a phase later. An open gap nobody re-reads becomes a silent omission |
