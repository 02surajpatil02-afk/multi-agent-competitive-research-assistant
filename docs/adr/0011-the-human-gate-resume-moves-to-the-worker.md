# ADR 0011 — The human-gate resume moves from the API process to the worker

- **Status:** **Accepted, 2026-08-17.** Not built. Blocks the second half of implementation step 20.
  **Corrected on acceptance** — one Consequences bullet claimed the reviewer payload was unreachable,
  which [ADR 0013](0013-reviewer-gate-payload-view.md) made untrue the following day. The
  async-resume decision itself is unchanged
- **Date:** 2026-08-16
- **Affects:** `routes/api.py` (`decide`, `_resume`, `_reconcile_status`) · `database/queries.py`
  (`read_gate_decision`) · a new `worker.py` · `docs/ARCHITECTURE.md` §12, §19 ·
  `docs/engineering-guidelines.md` §12
- **Found by:** The Phase 3 readiness audit of 2026-08-16 (decision D4). `routes/api.py` marks the
  seam at its own call site: *"Phase 3 replaces the invoke with an enqueue and lets the worker resume
  (ARCHITECTURE.md §12). Until the queue exists, the decision runs here rather than going nowhere."*
- **Relates to:** [ADR 0006](0006-reviewer-edit-returns-to-the-human-gate.md) and
  [ADR 0007](0007-reviewer-decision-idempotency-and-gate-resume-failure.md). **Neither is reopened.**
  This record moves *where* a resume executes and keeps every semantic both records fixed.
  [ADR 0010](0010-job-dispatch-and-status-across-api-queue-and-worker.md) supplies the queue,
  the FIFO group that makes a resume single-writer, and the start/resume discrimination.
  [ADR 0012](0012-the-api-stops-holding-a-compiled-graph.md) removes the graph object this endpoint
  currently uses

---

## Context

§20 row 24 decided this at architecture review and marked it `[derived]`:

> **The approval endpoint records the decision and enqueues a resume message; the worker resumes.**
> Keeps the API a control plane, and `edit` is a Synthesizer pass on the main-tier timeout (180s in
> development) that must not run in an HTTP request. The alternative — resume inline for `approve`,
> enqueue only for `edit` — is faster for the common case but gives the system two resume paths to
> test.

Phase 2 shipped the endpoint without the queue, so today `decide` calls
`deps.graph.invoke(Command(resume=...), run_config(job_id))` **inside the request**. On an `edit` that
is a Synthesizer pass, a Fact-Checker pass, and a reflection pass — three main-tier calls at up to
180 seconds each — held open on an HTTP connection.

### What the built system already gives us

| Fact | Where |
|---|---|
| The **full** decision is already durable: `{decision, note, edits, calls_used}` in `audit_events.detail` | `database/queries.py::record_reviewer_decision` |
| `read_gate_decision` projects only `detail["decision"]` and returns `str \| None` | `database/queries.py` |
| The gate-visit key `(job_id, calls_used)` is written on both the `gate_opened` and `reviewer_decision` rows and found by one predicate | ADR 0007 |
| `refuse_edit()` is a pure function of `(config, llm_calls_used, edits_made)` and is already called before anything is claimed or written | `graph/build.py`, `routes/api.py::_refuse_unaffordable_edit` |
| `claim_gate` is a conditional update with exactly one winner | `database/queries.py` |
| The graph is retry-safe across a failed resume — probed, not assumed | ADR 0007, "One thing that is already right" |

**So nothing has to be persisted that is not already persisted.** The gap is a projection: no query
returns `note` and `edits`, and a worker resuming an `edit` needs the reviewer's instruction to reach
`GateUpdate(reviewer_edit_text=decision.edits)`.

### The three things that genuinely change

1. `POST /jobs/{id}/approve` stops executing the graph, so its response no longer reports the outcome
   of the resume.
2. ADR 0007's status reconciliation runs in a `finally` around that resume. With no resume in the
   endpoint, there is nothing there to bracket.
3. ADR 0007 invariant 2 says a retried decision **continues the graph**. "Continue" has to mean
   something in a process that no longer runs one.

---

## Decision

### 1. The endpoint records and enqueues. It never invokes

```text
POST /jobs/{id}/approve
  clean the reviewer's text                       (ADR 0006 decision 8 — unchanged)
  load the job row, refuse a terminal job         (409 job_not_awaiting_approval — unchanged)
  read calls_used from the checkpoint             (ADR 0012's read path)
  read the decision already on record for that visit
     none      -> require awaiting_approval, refuse_edit(), claim_gate(), record the decision
     same      -> retry: write nothing, count nothing
     different -> 409 gate_already_decided        (unchanged)
  enqueue a resume message                        <- the only new step
  return {job_id, status}
```

Every line except the last two is what the endpoint does today, in the same order and for the same
reasons. **`refuse_edit()` stays in the API**, before the claim: ADR 0006's whole point is that a
refused edit spends nothing, and an edit refused after an enqueue would have spent a worker.

### 2. The worker reads the full decision from the audit trail, keyed by the visit

`read_gate_decision` is **replaced** by one query that returns the whole detail:

```python
def read_gate_decision(engine, job_id, *, calls_used) -> Mapping[str, Any] | None:
    """The decision on record for one gate visit — {decision, note, edits, calls_used} — or None."""
```

One row, one query, two readers: the endpoint compares `detail["decision"]` for ADR 0007's
four-case table, and the worker rebuilds `GateDecision` from the same mapping to resume with. Adding
a second near-identical query beside the existing one would be two statements answering one question,
and they would drift.

**Which visit the worker resumes is not carried in the message.** The worker already loads the
checkpoint to discriminate start from resume
([ADR 0010](0010-job-dispatch-and-status-across-api-queue-and-worker.md) decision 5), so it reads
`llm_calls_used` from that same snapshot — the identical computation `routes/api.py::_gate_visit`
performs today, against the identical durable value. The key is derived on both sides from the
checkpoint, never passed between them.

**A resume message whose visit has no decision row is a bug, and is loud about it.** The worker logs
it and leaves the message for redelivery rather than guessing; guessing which of the three decisions
was meant is exactly the silent wrong answer `human_gate_node` already refuses when a resume payload
does not validate.

### 3. ADR 0007 invariant 2 — "continues the graph" — becomes "enqueues a resume"

A retried decision writes nothing, counts nothing, and **enqueues**. What the reviewer gets is
unchanged: a `200`, and a job that carries on from where the failure left it.

**The retry is now safer than it was, for a reason worth stating.** ADR 0010 decision 4 puts the
queue in FIFO mode with `MessageGroupId = job_id`, so a retry's message cannot be processed while the
original is still in flight — the single-writer property ADR 0005 depends on becomes a queue
guarantee instead of a timing assumption. And `MessageDeduplicationId = f"{job_id}:{calls_used}"`
means a retry inside SQS's five-minute window collapses into the message already queued. **The
gate-visit key that makes the endpoint idempotent makes the queue idempotent too.**

If two resume messages for one visit do get processed in sequence, the second is harmless: the first
consumed the interrupt, so the second falls into ADR 0010 decision 5's `continue` branch —
`invoke(None)` — which either finds a terminal job and deletes the message, or carries a mid-run job
forward. Neither re-applies the decision.

### 4. Status reconciliation moves to the worker, unchanged in rule

ADR 0007 invariant 4 — *"`jobs.status = 'awaiting_approval'` if and only if the checkpoint holds a
pending interrupt at `human_gate`"* — is preserved exactly. What moves is the `finally`.

| | Phase 2 | From Phase 3 |
|---|---|---|
| Who reconciles | `routes/api.py::_reconcile_status`, in a `finally` around `graph.invoke` | The worker, in a `finally` around its invocation |
| The rule | terminal → leave it; pending interrupt → `awaiting_approval`; otherwise → `running` | **Identical** |
| The predicate | the pending interrupt, not `next` | **Identical** |

**`_reconcile_status` is deleted from the API, not kept.** After decision 1 there is no resume for it
to bracket, and a second writer of that column with nothing to bracket is a hazard rather than a
safety net.

**What the endpoint leaves behind is already correct.** `claim_gate` writes `running`, and after
decision 1 that is true: the gate is answered and a resume is queued. The row and the checkpoint agree
at every point in the sequence.

**And the divergence ADR 0007 was written against gets a better recovery.** In Phase 2, a process that
died before the `finally` left the row wrong until the next request arrived. Now the message was never
deleted, so SQS redelivers it and the worker reconciles without anyone asking — ADR 0007's own
"process died before the `finally` ran" row, answered by the queue instead of by a reviewer.

### 5. What the endpoint returns

`{job_id, status}` — the documented shape, unchanged. **What changes is the value.**

| Decision | Phase 2 returned | From Phase 3 |
|---|---|---|
| `approve` | `approved`, or `failed` if the export gate blocked | `running` |
| `reject` | `rejected` | `running` |
| `edit` | `awaiting_approval` — the next gate visit | `running` |
| retry of any of the three | whatever the resumed graph reached | `running` |

The value is read from the row after `claim_gate`, and `running` is honest in all four cases: the
gate is answered and the work is queued. **A caller that needs the outcome polls `GET /jobs/{id}`**,
which is what gl §12 already tells them to do for a job that takes minutes — *"polling a 20-minute
job every few seconds is cheap"*. The status code stays `200`.

This is a real contract change in meaning, and it is the price of the decision §20 row 24 already
took. It is recorded here so a client is not written against the synchronous behaviour and then
broken by the phase that was always going to remove it.

### 6. ADR 0006 is preserved in full, and here is each piece

| ADR 0006 decision | Where it lives after this record |
|---|---|
| An edit is one Synthesizer pass over existing evidence, never research | `human_gate_node` and the reflection node. **Untouched** — this record changes no graph node |
| Bounded at `MAX_REVIEWER_EDITS` = 3, counted from `audit_events` | `count_reviewer_edits`, called by the endpoint. **Untouched** |
| Refused when the live call budget cannot fund it, **before** the graph runs | `refuse_edit()` in the endpoint, before `claim_gate`. **Untouched, and now refused before a worker is spent as well** |
| The live count is the checkpoint's, never `jobs.llm_calls_used` | Still the checkpoint's, read through ADR 0012's path |
| Decision 8's edge cleaning of `edits` and `note` | `_clean_decision` in the endpoint, before anything is written. **Untouched** — and it now matters more, because the cleaned text travels through the audit trail to a different process |
| The gate is the only writer of `reviewer_edit_text` | `human_gate_node`. **Untouched** |

### 7. No graph execution inside the API, stated as a boundary

The API does not invoke, stream, or resume a graph, and does not construct one.
[ADR 0012](0012-the-api-stops-holding-a-compiled-graph.md) removes the object and the LLM client
behind it. Together the two records turn §19's *"the API container **never** runs the graph, calls the
LLM, calls a tool, or fetches a web page"* from a description of an intended container into a
property of the code.

---

## Consequences

- **`POST /jobs/{id}/approve` returns in milliseconds** for all three decisions, instead of holding an
  HTTP connection open for up to three 180-second main-tier calls on an `edit`.
- **One resume path for all three decisions**, which is the property §20 row 24 chose this design for.
- **`RouteDeps.graph` disappears**, and with it the last reason the API process needs an
  `LLMClient` (ADR 0012).
- **A client that read the outcome from the approve response must poll instead.** Single-tenant, no
  external consumers, and `GET /jobs/{id}` already carries `status`, `phase`, `quality_flag` and
  `report`.
- **`read_gate_decision`'s return type changes** from `str | None` to a mapping. One call site today,
  two after this record.
- **The interrupt's return value is discarded by the worker, and that is now harmless.**
  `interrupt()` hands `reviewer_payload(state)` to whichever process is invoking, which from Phase 3
  is the worker — which has nowhere to put it. When this record was written no route surfaced the
  payload either, so it flagged a gap it did not fix. **[Corrected 2026-08-17]
  [ADR 0013](0013-reviewer-gate-payload-view.md) closed it**: `GET /jobs/{job_id}/gate` rebuilds the
  same payload from the checkpoint on demand, so nothing depends on catching the value `interrupt()`
  returns. Moving the resume off the API process therefore costs a reviewer no visibility at all.

## Alternatives rejected

| Option | Why not |
|---|---|
| **Resume inline for `approve`, enqueue only for `edit`** | §20 row 24 rejected it at architecture review: faster for the common case, two resume paths to test. `approve` also runs the export node and, from Phase 3, an S3 write with its own 10s + 2 retries |
| **Put the decision in the resume message** | Breaks §20 row 8's "identifiers only, never state", and duplicates a fact `audit_events` already holds as the authenticated record of who decided what |
| **Add a second query beside `read_gate_decision`** | Two statements answering one question over one row. They drift, and ADR 0007's four-case table would then depend on which one a reader happened to call |
| **Keep `_reconcile_status` in the API as a safety net** | With no resume to bracket it can only assert a value it did not derive, which is precisely what ADR 0007 invariant 4 forbids |
| **Return `202` from the approve endpoint** | The decision *was* processed synchronously and durably — recorded, counted, and the gate claimed. Only the graph's continuation is asynchronous, and `200` with a `running` status says that accurately |
| **Have the worker re-derive the visit key from the message** | It would be a second copy of `_gate_visit`'s rule living somewhere the checkpoint is already open |

## What a test has to prove before this ships

1. **ADR 0007's four cases, unchanged, with the resume enqueued rather than invoked:** an undecided visit records once and enqueues once; the same decision again writes no row, counts no edit, and enqueues; a different decision is `409 gate_already_decided`; a terminal job is `409 job_not_awaiting_approval`.
2. **A retried `edit` still costs one edit, not two** — ADR 0006 decision 6 and ADR 0007 invariant 7, re-asserted across the new boundary.
3. **The worker rebuilds the exact `GateDecision` the reviewer sent**, including the cleaned `edits` text, and an `edit` reaches the Synthesizer with `reviewer_edit_text` set.
4. **A resume message for a visit with no decision row leaves the message undeleted and logs**, rather than resuming with a guess.
5. **The reconcile runs in the worker on both paths** — invocation returned, and invocation raised — and produces ADR 0007's three outcomes against the same checkpoint states its probe recorded.
6. **The API never invokes a graph:** `routes/api.py` and `app.py` import no graph builder and no `LLMClient` — asserted by import, not by review.
7. **A refused edit enqueues nothing**, spends no call, and leaves the gate open.
