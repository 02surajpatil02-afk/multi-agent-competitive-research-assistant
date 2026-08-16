# ADR 0007 — One decision per gate visit: reviewer-decision idempotency and gate-resume failure

- **Status:** **Accepted, built on 2026-08-16.** All four files below carry the change, and the eight
  tests named at the end exist — each one verified to fail with its fix reverted
- **Date:** 2026-08-16
- **Affects:** `database/queries.py` · `routes/api.py` · `app.py` ·
  `docs/ARCHITECTURE.md` §12 · `docs/engineering-guidelines.md` §12
- **Found by:** The Phase 2 completion audit of 2026-08-16, with two read-only probes against the
  built system
- **Relates to:** [ADR 0005](0005-graph-time-persistence-semantics.md) decides what a *graph-time*
  write failure does; this decides what the *endpoint* does when the resume it triggered fails.
  [ADR 0006](0006-reviewer-edit-returns-to-the-human-gate.md) decides what a gate decision means and
  what bounds it; this keeps those bounds honest when the machine fails halfway

---

## Context

ADR 0005 settled that a graph-time write failure propagates loudly and that every graph-time write is
keyed so a replayed node converges. ADR 0006 settled what a reviewer's decision means and bounded the
edit path. Neither says what happens when the **API** records a decision, resumes the graph, and the
resume dies in the middle — and Phase 2 shipped an endpoint that does exactly that.

### What the built system does today, measured

A reviewer sends `{"decision": "edit"}`; the graph resumes; a database write inside the Synthesizer
node raises. Observed, with a read-only probe:

```text
at the gate     next=('human_gate',)  interrupts=1  jobs.status=awaiting_approval
claim_gate                                          jobs.status=running
gate node replays, interrupt consumed, gate_opened  jobs.status=awaiting_approval   <- (A)
reviewer_decision row written
synthesizer: agent succeeds, persistence raises  -> exception leaves the node
after failure   next=('synthesizer',)  interrupts=0  jobs.status=awaiting_approval  <- divergence
API returns     500 "Internal Server Error"  (not the documented error envelope)
retry           status check passes -> claim_gate -> a SECOND reviewer_decision row  <- (B)
                second attempt fails too, leaving jobs.status=running
```

The row says a human is holding the job. The checkpoint says the job is mid-edit-pass with no
interrupt to answer. They disagree, and the row is the one that is wrong.

### Three root causes, each small and each verified in the code

1. **`record_gate_opened` reopens the gate during the replay.** Its
   `UPDATE jobs SET status='awaiting_approval'` sits **outside** the `if not already:` guard that
   protects the audit row. Its docstring reasons that "setting the same value twice is the same
   value" — true on the first entry, false on the post-resume replay, where `claim_gate` has just
   moved the row to `running`. LangGraph re-runs an interrupted node from the top, so this executes
   again on the way *out* of the gate, not only on the way in.
2. **`reviewer_decision` is not keyed to a gate visit.** It is a plain append with no notion of which
   opening it answers, so a retry writes a second row for one decision.
3. **The endpoint leaves the row and the checkpoint unreconciled when the resume raises**, and the
   exception escapes as FastAPI's default `500 Internal Server Error` rather than the one documented
   error shape (guidelines §12).

### Two consequences that are worse than the divergence itself

- **`claim_gate`'s concurrency guarantee is defeated.** Root cause 1 returns the row to
  `awaiting_approval` milliseconds into the resume, so a second reviewer arriving during the edit
  pass also passes the status check, also claims, and also invokes the same thread. The conditional
  update is doing less than it appears to.
- **A retried edit spends a reviewer's budget.** `count_reviewer_edits` counts rows, so root cause 2
  means an infrastructure failure costs the reviewer one of their three permitted edits (ADR 0006
  decision 6) for an edit that never happened.

### One thing that is already right, and it shrinks this problem

**The graph is retry-safe.** Probed: after the failed resume, re-invoking the same thread with a stray
`Command(resume=…)` **completed without raising** — the run continued from `synthesizer`, finished the
edit pass, and returned to the gate with two legitimate `gate_opened` rows. ADR 0005's keyed writes
are what make that true. So nothing in the graph layer needs changing here; the damage is entirely in
the API and persistence bookkeeping.

---

## Decision

### The invariants

**1. One gate visit, one reviewer decision.** At most one `reviewer_decision` row may exist per gate
visit. The visit key already exists and needs no schema change: it is the `calls_used` value that
`gate_opened` is keyed on — the job's `llm_calls_used` at the pause, identical across the gate node's
replay and strictly greater at the next visit.

**2. A retry of the same decision is idempotent.** Re-sending the decision a visit already carries
writes no second row, counts no second edit, and **continues the graph** rather than refusing. The
graph tolerates this, measured; the endpoint is what must stop refusing.

**3. A different decision on an already-decided visit is refused** with `409` and the stable code
`gate_already_decided`. A reviewer who changes their mind waits for the job to come back to the gate,
which is the only point at which a new decision is a new decision.

**4. `jobs.status = 'awaiting_approval'` if and only if the checkpoint holds a pending interrupt at
`human_gate`.** The status is **derived from the checkpoint**, never asserted independently of it.
Concretely: the gate node writes `awaiting_approval` only when it is genuinely opening a visit — the
same `if not already:` branch that guards the audit row — and the endpoint reconciles the row from the
checkpoint on the way out, including on the error path.

**5. The decision is recorded before the resume.** ARCHITECTURE.md §12 says "record the decision and
the reviewer identity as an `audit_events` row, then resume the graph", and that ordering stays. A
decision that vanishes because the machine failed afterwards is worse than one recorded twice — and
invariant 1 removes the duplication without moving the write.

**6. A failed resume creates no second logical decision.** The row from the failed attempt stands: a
human did decide. What the failure changes is the job's progress, not the decision's existence.

**7. Retrying the same decision must not consume another reviewer-edit unit.** This follows from
invariant 1, and it is called out separately because it is the consequence a reviewer would actually
feel: three edits must mean three edits they asked for, not three the infrastructure counted.

**8. An unexpected API failure uses the documented error envelope.** `{"error": {"code", "message",
"job_id"}}`, with a stable code, and nothing else in the body — no stack trace, no internal path, no
SQL (guidelines §12, §16). "One shape, everywhere" is only true if the framework's default 500 cannot
leak through it.

### The gate-visit key, exactly

> **The key is `(job_id, calls_used)`, where `calls_used` is `ResearchState["llm_calls_used"]` as the
> gate node reads it at the pause.** It is stored as `detail.calls_used` on the `gate_opened` row and
> on the `reviewer_decision` row that answers it, and both are found by the same predicate
> `audit_events.detail["calls_used"].as_integer() == calls_used`, scoped by `job_id`.

No new column and no new table: `record_gate_opened` already writes and queries this exact key, and
invariant 1 gives the decision row the same one so that "which opening does this decision answer?" is
a join rather than an assumption.

**Why it is stable across a replay, and across a process.** The gate node's two executions per visit —
the one that raises `interrupt()` and the one that resumes — both run against the *same* checkpointed
state, because an interrupted node's writes are discarded and LangGraph re-runs it from the top with
the input it had. `llm_calls_used` is therefore identical in both, and identical again in any other
process that loads the checkpoint for `thread_id = job_id`, since the value comes from the durable
checkpoint rather than from anything in memory. The same property is what makes `gate_opened` write
once per visit today, measured rather than assumed.

**Why it is unique per visit.** `llm_calls_used` never decreases — `CallBudget.spend()` runs at the
top of every attempt — and the only route back to the human gate spends calls on the way:
`human_gate` is reachable **only** from `reflection` (it appears in no other node's `destinations`),
`reflection` is reachable only by the fixed edge from `fact_checker`, and a second draft to check
requires the Synthesizer. So a job returning for an (N+1)th visit has spent at least a Synthesizer, a
Supervisor hop, a Fact-Checker and a reflection pass since visit N — the 3 logical calls plus 1 hop
ADR 0006 already counts for an edit. The value at visit N+1 is therefore **strictly greater** than at
visit N, and the two keys cannot collide.

**What that argument depends on, stated so it is re-checked rather than assumed.** Uniqueness is a
property of the topology, not of the counter: it holds because no edge reaches `human_gate` without
spending a call. A future change that added one — a gate revisit that costs nothing, or a decision
that returns to the gate without a pass — would break it silently. Any change to the graph's
`destinations` or to ADR 0006's edit path has to re-read this paragraph, and a test that asserts two
gate visits carry different keys is the cheapest way to make that failure loud.

### What the endpoint does, in one table

| Situation | Behaviour |
|---|---|
| No decision for the current gate visit | Require `awaiting_approval`, claim the gate, record the decision, resume |
| The same decision already recorded for this visit, job not terminal | **Retry:** write nothing, count nothing, continue the graph |
| A different decision on a visit that already has one | `409 gate_already_decided` |
| Job already terminal | `409 job_not_awaiting_approval` — unchanged, documented, and already tested |

### Where `jobs.status` is reconciled, exactly

Invariant 4 says the status is derived from the checkpoint. This is where that derivation happens, and
it is the only place the endpoint writes the column.

**The rule**, evaluated from a checkpoint snapshot for `thread_id = job_id`:

```text
row already terminal (approved | rejected | failed)  -> leave it. finalize is authoritative
a task carries a pending interrupt                   -> awaiting_approval
otherwise                                            -> running
```

**The predicate is the pending interrupt, not `next`.** A job that has not yet entered the gate also
reports `next == ("human_gate",)` while no human is being waited on; only an interrupt recorded
against that task means the graph has actually stopped for a person. The probe behind this record
shows both states: at the gate, `next=('human_gate',) interrupts=1`; after the failed resume,
`next=('synthesizer',) interrupts=0`.

**When it runs — both paths, one call site.** The reconcile is a `finally` around the resume in
`routes/api.py::decide`, so it executes whether the resume returned or raised:

| Path | What the graph left behind | What the reconcile writes |
|---|---|---|
| **Approve, succeeded** | `finalize` wrote `approved` | Nothing — the row is terminal |
| **Reject, succeeded** | `finalize` wrote `rejected` | Nothing — terminal |
| **Edit, succeeded** | The gate node opened the *next* visit and wrote `awaiting_approval` | The same value, from the pending interrupt. Idempotent |
| **Failed after the gate node completed** | `claim_gate`'s `running`, and a checkpoint mid-pass | `running` — which is now true, and is what invariant 4 was violated by before |
| **Failed before the gate node completed** | `claim_gate`'s `running`, interrupt still pending | `awaiting_approval` — the gate really is still open |
| **Process died before the `finally` ran** | Whatever the last write left | Nothing now; the next request reconciles it, because a decision exists for the visit and the retry path continues the job |

The graph keeps the two transitions it owns and the endpoint does not touch them: the gate node writes
`awaiting_approval` when it **opens** a visit (invariant 4 moves that write inside the
already-guarded branch, so the replay no longer reopens an answered gate), and `finalize` writes the
terminal status. `claim_gate` still writes `running` when a decision is accepted; with the gate node's
replay no longer overwriting it, that value now survives the whole resume, which is what makes the
conditional update serialise two reviewers as it was meant to.

### A retry resumes from the checkpoint; it never restarts the job

**A retry of the same decision invokes the same thread — `thread_id = job_id` — so LangGraph replays
from the last checkpoint and completed nodes are not re-executed** (guidelines §10, and the resume
tests that already pin it). It is not a new job, not a new thread, and not a re-run of research.

Measured on the probe behind this record: after the failed resume the retry **continued from
`synthesizer`** and finished the edit pass. The Planner, the three Researcher visits and the first
Synthesizer pass did not run again, no search was re-issued, and no finding was re-gathered.

The cost of a retry is therefore bounded by **the single node that was in flight**, which re-executes
and converges because ADR 0005 keys every graph-time write. That is the same bound ARCHITECTURE.md §11
gives a redelivered SQS message, and it is why this record needs no compensating action and no
"unwind" step: there is nothing to undo, only something to finish.

### What this does not decide

- **Recovery after `export_write_failed`** stays open (ARCHITECTURE.md §22 item 1). That is a
  finished, approved job with no artifact; this record is about a decision whose resume died.
- **`failure_reason` still has no durable home** (ADR 0005's recorded gap). A failed resume is visible
  in the logs and in the job's status, not in a column.

---

## Alternatives rejected

| Option | Why not |
|---|---|
| **Record the decision after a successful resume** | Loses the record of what a human decided whenever the machine fails, and contradicts §12's stated ordering. It also just moves the hole: a crash between a successful resume and the write loses a decision the graph has already acted on, which is strictly worse than recording one twice |
| **Add a `gate_visit` column, or a `reviewer_decisions` table** | A migration for a key that already exists. `gate_opened.detail.calls_used` identifies the visit today, and ADR 0005's keyed-write pattern already reads it |
| **A new operator or "re-drive" route** | The API surface is five routes (guidelines §12), and the retry is the existing endpoint behaving correctly rather than a sixth. §22 item 1 is the standing example of what inventing a recovery route costs: it becomes an authorization question nobody has answered |
| **Let `claim_gate`'s `running` be authoritative and stop the gate node writing status at all** | Then a job that reaches the gate without the API — the Phase 3 worker, `scripts/measure_jobs.py`, any test — never becomes `awaiting_approval`. The node that makes the transition has to be the node that records it |
| **Retry the failed persistence inside the endpoint** | guidelines §17 gives the database 0 retries and ADR 0005 makes a failed write loud. A retry loop here would quietly re-run node side effects and contradict both |
| **Return `200` for a repeat decision on a finished job** | Tidier as pure idempotent-POST, and it contradicts the documented `409 job is not awaiting_approval` and the test that pins it. The retry rule is scoped to a visit that has not finished |
| **Leave it, and document the failure as operational** | It is not rare enough or harmless enough: the audit trail gains a decision nobody made, a reviewer silently loses edit budget, and the concurrency guard `claim_gate` exists for does not hold |

---

## Consequences

- **The retry path is the recovery path.** No new route, no operator script, no schema change. A
  reviewer who gets a `500` sends the same request again.
- **`claim_gate` starts doing what it was written to do.** With the status write moved inside the
  guard, the row stays `running` for the whole resume, so a second reviewer arriving mid-pass is
  refused rather than racing.
- **The audit trail can show a decision whose resume failed, with no second decision beside it.** That
  is intended: the trail records what a human decided, and the job's status records how far the
  machine got.
- **One extra checkpoint read per decision.** The edit path already does this to get the live call
  count; the other two decisions now do it as well, to compute the visit key.
- **It self-heals across a process crash.** If the API dies before reconciling, the row is left
  `running`; the next request finds a decision for the current visit and continues, which is the retry
  case rather than a special one.
- **A reviewer cannot change their mind during a failure.** A different decision on a decided visit is
  refused until the job returns to the gate. That is a real cost, accepted: the alternative is two
  decisions racing on one thread.
- **`MAX_REVIEWER_EDITS` becomes accurate under failure**, which is the one user-visible number this
  record protects.

---

## What implementing this changed

| File | Change |
|---|---|
| `database/queries.py` | `record_gate_opened`'s status `UPDATE` moved **inside** `if not already:` — one statement, and it fixes invariant 4 on the replay path. `record_reviewer_decision` gained the gate key and the same query-then-insert guard; `read_gate_decision` answers "what does this visit already carry?", and `set_job_status` is the reconcile's one write, guarded on `completed_at IS NULL` so `finalize` stays authoritative |
| `routes/api.py` | The live `llm_calls_used` is read once for all three decisions rather than only `edit` — it is both the visit key and the edit path's budget input; `decide` branches on the four-row table above; `_reconcile_status` derives `jobs.status` from the checkpoint's pending interrupt in a `finally` around the resume |
| `app.py` | A catch-all exception handler, so an unexpected failure leaves in the documented envelope with the stable code `internal_error` (invariant 8) |
| `docs/ARCHITECTURE.md` §12, `docs/engineering-guidelines.md` §12 | The retry semantics and `gate_already_decided` |

## Tests it needed

1. A failed resume leaves `jobs.status` and the checkpoint agreeing.
2. Retrying the same decision writes no second `reviewer_decision` and completes the job.
3. A different decision on a decided visit returns `409 gate_already_decided` and writes no row.
4. The gate node's replay does not reopen the gate: the status stays `running` for the whole resume.
5. A retried **edit** does not consume a second unit of `MAX_REVIEWER_EDITS`.
6. An unexpected failure returns the documented error envelope rather than `Internal Server Error`.
7. Two gate visits on one job carry **different** keys — the uniqueness the visit key rests on,
   asserted rather than argued, so a future topology change that breaks it fails here first.
8. A retried decision re-executes **only** the node that was in flight: the Planner, the Researcher
   and the earlier Synthesizer passes do not run again, and no search is re-issued.

All eight exist, in `tests/test_api.py` and `tests/test_database.py`. Test 1 is split in two,
because the reconcile table has two failure rows and only the second isolates the reconcile: a
resume that dies **after** the gate node completes is already left consistent by the
`record_gate_opened` fix alone, while one that dies **before** it completes leaves a pending
interrupt and needs the `finally` to put `awaiting_approval` back. Each test was run with its own
fix reverted and observed to fail.
