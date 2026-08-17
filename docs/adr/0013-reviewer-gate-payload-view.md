# ADR 0013 — The reviewer's gate view is a route of its own

- **Status:** **Accepted, built on 2026-08-17.** `GET /jobs/{job_id}/gate`, the projection moved
  to `graph/state.py`, and twenty tests in `tests/test_api.py`
- **Date:** 2026-08-17
- **Affects:** `graph/state.py` · `graph/build.py` · `routes/api.py` · `tests/test_api.py` ·
  `docs/ARCHITECTURE.md` §10, §12 · `docs/engineering-guidelines.md` §12 · `CLAUDE.md`
- **Found by:** The Phase 3 readiness audit of 2026-08-16, and the design review of 2026-08-17. It
  is a **Phase 2 gap**, not a Phase 3 one: `reviewer_payload()` shipped with the gate node in step
  17, and step 18's five routes never exposed it
- **Relates to:** [ADR 0006](0006-reviewer-edit-returns-to-the-human-gate.md) and
  [ADR 0007](0007-reviewer-decision-idempotency-and-gate-resume-failure.md) — **neither is
  reopened.** This adds a read; it changes nothing about what a decision means or how a gate visit
  is keyed. Proposed [ADR 0012](0012-the-api-stops-holding-a-compiled-graph.md) is what the
  projection move below is for

---

## Context

At `human_gate` the graph builds `reviewer_payload(state)` and passes it to `interrupt()`. That
payload is ARCHITECTURE.md §12's "what the reviewer sees", in §12's order, and it exists because a
reviewer who has to hunt for the problems will approve past them.

**No route returns it.** `interrupt()` hands its value to whichever process is invoking the graph,
and nothing in the API surface asks for it afterwards. So a reviewer holding an authenticated
endpoint that decides whether a report is exported has had no way to read the report.

`GET /jobs/{id}` does not fill the gap, and cannot:

| Field | At the gate | Why |
|---|---|---|
| `report` | `null` | It is the **exported** body. `record_export_result` writes `jobs.report_json` only after the export gate passes, which is after the approval |
| `revision_count` | `0` | Written by `finish_job`, at finalize |
| `quality_flag` | `null` | Same |
| `status`, `phase` | `awaiting_approval`, `human_gate` | True, and not a report |

That route is deliberately a lightweight status read: gl §12 designs it to be polled every few
seconds for twenty minutes.

### What the durable stores actually hold, measured against the code

Five of the payload's values exist **only** in the checkpoint, and this is the fact the decision
turns on:

| Payload value | Where it is not |
|---|---|
| `score` | `record_revision` writes a weighted score only when a revision *starts*. A passing pass writes no row at all |
| `failed_dimensions` | Same — revision rows only |
| `revision_count` | `jobs.revision_count` is written by `finish_job` |
| `report.sections[].body` | **No table holds a section body.** `claims.section` is the section *id* |
| `claims[].quote` | **`claims` has `supported` and `verdict_note` and no quote column** |

The rest is reconstructable — `unsupported_claims` from `claims.supported`, `unresearched_subtopics`
from the `subtopic_researched` audit rows, `quality_flag` and `llm_calls_used` from `gate_opened`'s
detail — but a route assembled that way would answer with a report the reviewer cannot read.

### One property that makes this cheap

`reviewer_payload()` is a **pure function of `ResearchState`**: no LLM, no tool, no node, no
database. And `interrupt()` discards the interrupted node's writes, so LangGraph re-runs the node
from the top on resume and the checkpoint holds exactly the state the gate node was invoked with —
the same property ADR 0007 relies on for its visit key. **Rebuilding the payload from the checkpoint
reproduces it rather than resembling it**, so nothing has to be persisted for this to work.

---

## Decision

### 1. A route of its own

```
GET /jobs/{job_id}/gate
```

**Not an extension of `GET /jobs/{id}`.** Three reasons, in order of weight:

1. **It would contradict proposed ADR 0012 decision 2**, which makes `GET /jobs/{id}` a single-row
   read. A conditional checkpoint read on the polling route reverses that before it ships.
2. **The two reads have different cadences.** Status is polled for twenty minutes; the gate view is
   read once, by one person, when a job stops. Attaching a full report to the polling route makes
   the common case pay for the rare one.
3. **`report` would mean two things** — the exported artifact, and a draft that is neither exported
   nor guaranteed ever to be.

It is also additive: no existing response changes, and no existing test had to be rewritten.

**Why a sixth endpoint is justified where [ADR 0009](0009-recovering-an-export-that-failed-after-approval.md)
declined one.** ADR 0009 refused a route for an event needing S3 down for twenty seconds during an
approval, where the report is not lost. This is on the **normal path of every job**, and without it
CLAUDE.md invariant 6's backstop and gl §16's authentication argument are weaker than three
documents describe.

### 2. The body is `reviewer_payload()` verbatim

Same keys, same order, no new schema. It is returned as a `JSONResponse` rather than through a
Pydantic model precisely so that no second definition of that shape exists: §12's ordering is the
contract, and a model would be a second place to keep it true.

```text
job_id · unsupported_claims · unresearched_subtopics · quality_flag · score
       · failed_dimensions · revision_count · llm_calls_used · report · claims
```

**No new payload is invented, and `edits` and `note` are not exposed.** `reviewer_edit_text` is
written and cleared by the gate (ADR 0006) and a reviewer's note lives in `audit_events`; neither is
in the payload today and neither is added.

### 3. It is rebuilt from the checkpoint, and executes nothing

State comes from the durable checkpoint for `thread_id = job_id`. **No LLM call, no tool call, no
graph invocation, no node execution, no agent re-run.** `get_state` loads a checkpoint; it does not
run a graph.

### 4. The projection moves to `graph/state.py`

`reviewer_payload`, `unsupported_claims`, `unresearched_subtopics` and `_claims_with_sources` move
out of `graph/build.py`, which imports all five agents, the LLM client, the tool boundary and the
database layer. `graph/state.py` imports `schemas` and nothing else, so the API reaches the payload
without dragging any of that into a process ARCHITECTURE.md §19 says must never run the graph or
call the LLM. A test asserts `graph/state.py`'s import set statically, because what the module
imports *is* the property.

**Graph behaviour is unchanged.** `human_gate_node` calls the same function and interrupts with the
same value; `graph.build` re-exports it, so its existing tests import it from where they always did.

### 5. Authorization is `GET /jobs/{id}`'s, unchanged

| Caller | Answer |
|---|---|
| Unauthenticated | `401 unauthenticated` |
| Submitter, own job | `200` |
| Submitter, another caller's job | `403 not_the_owner` |
| Reviewer, any job | `200` |

Not reviewer-only. gl §16 grants a submitter "read its own jobs and their reports", and this is its
own job's draft; denying it while `GET /jobs/{id}` hands the same caller the exported body would be
incoherent. And a reviewer's read is deliberately not limited to its own jobs, because deciding
without reading is approving unseen.

**Nothing in the payload is a new disclosure class**: claim ids, subtopic ids, model-produced scores
and rationale, third-party page quotes, and public source URLs — all of which already reach the same
caller in an exported report. No secret, no connection string, no internal path.

### 6. Preconditions

| Situation | Answer |
|---|---|
| No such job | `404 job_not_found` |
| `status != "awaiting_approval"` | `409 job_not_awaiting_approval` |
| Row says `awaiting_approval`, checkpoint has nothing | `409 job_not_awaiting_approval`, and an error log |

`409` reuses the code `POST /jobs/{id}/approve` already returns for the same condition, so a client
branches on one string for one situation across both gate routes. Not `404` — the job exists.

**Approved, rejected, failed, and never-run jobs get no payload.** It is a decision surface, not a
history record: after approval the artifact and `GET /jobs/{id}` serve the outcome, after rejection
there is nothing to approve, and [ADR 0008](0008-a-failed-jobs-reason-lives-in-the-checkpoint-for-phase-2.md)
records that a checkpoint may be pruned while the `jobs` row lives out its retention window — so a
historical gate view would be a promise the storage cannot keep.

The last row is a contradiction of ADR 0007 invariant 4. There is genuinely no gate to show, so the
caller gets the code that says so and the contradiction goes to the log, where an explanation an
anonymous caller must not see belongs (gl §16).

### 7. Side effects: none

No database write, no audit row, no gate claim, no status change, no LLM budget spent, no graph node
executed. **Reading is not deciding**, and a test asserts each of those separately.

### 8. The gate visit keeps ADR 0007's key, and no identifier is added

**`llm_calls_used` is already field 8 of the payload**, and `graph/state.py` says why: it is the live
number `refuse_edit()` needs. It is also ADR 0007's gate-visit key. So the payload and the decision
address the same visit by construction, both reading `llm_calls_used` from the checkpoint for
`thread_id = job_id` — the route through `reviewer_payload`, the endpoint through `_gate_visit`.

**No `gate_visit` field is added**, because none is required: the value is already exposed, under the
name it already has.

### 9. The stale-read window, recorded and deliberately not closed

Reviewer A reads visit *N*; reviewer B sends an `edit`; the edit pass completes and the job returns
to the gate at visit *N+1*; A then posts without re-reading. The status is `awaiting_approval` again
and *N+1* carries no decision, so A's decision is accepted **against a payload they never read**.

**Not closed now.** It needs two reviewer identities *and* a full Synthesizer, Fact-Checker and
reflection pass to elapse between one caller's two requests. The system is single-tenant with one
reviewer role, so it is not currently reachable. Closing it means an optional `gate_visit` on
`GateDecision`, which changes ADR 0006's request body and adds a fifth case to ADR 0007's four-case
table, for a race nobody can produce.

> **The trigger for revisiting: a second reviewer identity, or a client that caches the payload
> between reading and deciding.** Either makes it reachable. The answer when it is reachable is an
> optional `gate_visit` echoed back on `POST /jobs/{id}/approve` and refused with
> `409 gate_moved_on` when it does not match the current visit — not a new key, since
> `(job_id, calls_used)` already identifies the visit.

### 10. Forward compatibility with ADR 0012

The route reads the checkpoint through the graph object the API holds today. When ADR 0012 lands,
that becomes `PostgresSaver.get_tuple(run_config(job_id))` and the route, the payload, the
authorization and the preconditions are unchanged — **two lines move, and nothing else.** The route
is already free of graph execution and of any LLM dependency, which is the property ADR 0012 needs
rather than something it has to add.

---

## Consequences

- **The gate is a judgement again.** A reviewer can read the draft, the unsupported claims, the
  unresearched subtopics, the score breakdown, and the source URL and quote behind every sentence —
  which is what CLAUDE.md invariant 6 and gl §16 have always assumed.
- **gl §12's surface is six endpoints, not five.** Deliberate, and the reason is written above.
- **`graph/state.py` gains a projection and stays free of routing and guard logic.** It imports
  `schemas` and nothing else, and a test keeps it that way.
- **The payload has two callers and one definition.** The gate node interrupts with it and the route
  returns it, so §12's ordering cannot drift between what a reviewer is shown and what the graph
  built.
- **`GET /jobs/{id}` is untouched** — same six fields, same meanings, and still safe to poll.
- **One narrow stale-read window is open and recorded**, with a stated trigger.

## Alternatives rejected

| Option | Why not |
|---|---|
| **Extend `GET /jobs/{id}` with the payload when awaiting approval** | Contradicts ADR 0012 decision 2; puts a full report on a route designed to be polled every few seconds; makes `report` mean two things; and breaks a response shape gl §12 and ARCHITECTURE.md §10 both specify exactly |
| **Persist the interrupt value so the route can serve it from a table** | It is already durable — it is a pure function of a state that is already checkpointed. A second copy would be a second thing that can disagree with the first |
| **Reconstruct the payload from `jobs`, `claims`, `claim_sources` and `audit_events`** | Loses the five values in the Context table, including the report body and every quote. A reviewer would get a citation list and no report |
| **Return the payload only to `reviewer` keys** | Denies a submitter its own job's draft while `GET /jobs/{id}` hands the same caller the exported body. gl §16 gives the submitter that read |
| **Serve the payload for approved and rejected jobs too** | Turns a decision surface into a history API whose backing store may be pruned (ADR 0008) |
| **Define a Pydantic response model for the payload** | A second definition of an ordering that is the contract. The point of returning it verbatim is that there is exactly one |
| **Add a `gate_visit` identifier now** | `llm_calls_used` already is one, and ADR 0007 already keys on it. A second name for one value is the drift this project keeps writing records to avoid |

## What the tests prove

Twenty tests in `tests/test_api.py`, plus the route's row in `PROTECTED`. Three deliberately wrong
implementations were run against them before this record was written, and each was caught:

| Wrong implementation | Tests that failed |
|---|---|
| `report` served from `jobs.report_json` | 3 |
| Checkpoint-only fields reconstructed from Postgres | 5 |
| The read claims the gate | 3 |

The ordering assertion now exists twice on purpose — at the gate node in `test_graph_build.py`, and
on the HTTP body — because §12's order is a promise to a reviewer, not to a function.
