# ADR 0006 — A reviewer edit is one Synthesizer pass over existing evidence, and returns to the gate

- **Status:** **Accepted and fully implemented** — proposed 2026-08-15, accepted 2026-08-16, built in
  implementation step 17 on 2026-08-16, with the two `409` refusals `refuse_edit()` decides returned
  by the authenticated endpoint in step 18 the same day. **Decision 8's edge cleaning was the one
  part that shipped late**: the Phase 2 completion audit of 2026-08-16 found the reviewer's `edits`
  and `note` neither length-capped nor stripped of control characters, and it was implemented that
  day at `POST /jobs/{id}/approve` — the earlier "everything else below is code" claim was inaccurate
  while that was outstanding
- **Date:** 2026-08-16
- **Affects:** `graph/reflection.py` · `graph/build.py` · `agents/synthesizer.py` · `config.py` ·
  `docs/ARCHITECTURE.md` §3, §5, §9, §12, §22 · `docs/engineering-guidelines.md` §5, §6, §10, §13
- **Resolves:** `docs/ARCHITECTURE.md` §22 question 2, and — as one inseparable change — question 3
- **Blocks:** implementation step 17

---

## Context

Step 17 cannot start while it is undecided whether an edited draft may trigger another automatic
improvement cycle. The question looks small and is not: it decides who is in control of a report after
a human has touched it, what a reviewer's instruction costs, and whether that instruction can quietly
turn into more research.

### Current behaviour

| # | Step | Where | What happens |
|---|---|---|---|
| 1 | Reviewer resumes with `{"decision":"edit","edits":"…"}` | `graph/build.py` `human_gate_node` | `GateDecision` validates it; an edit with no text is refused |
| 2 | The gate routes | same | `Command(goto="synthesizer", update={"reviewer_edit_text": …})`. `revision_count` untouched, no audit row |
| 3 | Synthesizer pass | `agents/synthesizer.py` `write_report` | **Does not read `reviewer_edit_text`.** The prompt is question + subtopics + criteria + findings. `SynthesizerUpdate` has no such key, so the field is neither applied nor cleared. A fresh draft with fresh `uuid4` claim ids is produced |
| 4 | Fixed edge → Supervisor | `agents/supervisor.py` `allowed_target` | The new claim ids have no verdicts → `fact_checker`. Costs 1 hop |
| 5 | Fixed edge → reflection | `graph/build.py` | Re-verified, then scored |
| 6 | Reflection | `graph/reflection.py` `_scored` | **Applies its normal rules.** `route = "human_gate" if passed or capped else _route_for(...)`. Nothing distinguishes the edit path |

So today an edited draft that scores below the threshold **with revisions left starts an automatic
cycle** — and if that cycle's lowest failing dimension is a research dimension, **the reviewer's edit
silently launches the Researcher.** `reviewer_edit_text` also stays set for the rest of the job.

### Intended behaviour, as already documented

| Source | Statement |
|---|---|
| gl §10 | `edit` → "routed to the Synthesizer for **one pass, then back to the gate**" |
| ARCHITECTURE §3 "Approval path" | "Edit resumes into `synthesizer` for one pass and then returns to the gate" |
| ARCHITECTURE §12 `[derived]` | "The edit pass **is scored but not re-routed** … reflection records its score and routes to `human_gate` regardless of the result" |
| gl §6, ARCHITECTURE §3 | An edit is human-triggered, **not** a revision; it does not increment `revision_count` |
| ARCHITECTURE §5, §12 | `reviewer_edit_text` is "consumed by the next Synthesizer pass, then cleared" — applied exactly once; the text survives in `audit_events` |
| gl §2.4 | The Synthesizer "never introduces a fact that is not in a finding", and has no tools |
| ARCHITECTURE §22 Q2 | Records the "one pass" reading as the one this document implements, and that an implementer could read it the other way |

The documents agree with each other. Only the code has no rule.

### Three gaps, and why they cannot be fixed separately

- **(a)** Reflection has no edit-path rule, so it can start a cycle — including a research cycle. This
  is Q2 proper.
- **(b)** The Synthesizer never reads `reviewer_edit_text`, so the instruction reaches state and not
  the prompt. This is Q3, whose design is already settled.
- **(c)** Nothing ever clears the field, so "applied exactly once" is unenforced. Harmless today
  because nobody reads it — **and actively harmful the moment (b) is fixed without (a)**: an automatic
  cycle after an edit would re-apply the reviewer's instruction to drafts they never asked for.

---

## Decision

### 1. A reviewer edit performs exactly one Synthesizer pass and returns to the gate

`human_gate → synthesizer → supervisor → fact_checker → reflection → human_gate`, and no other shape.
The reviewer sees the result of what they asked for, scored, and decides again.

### 2. The edit pass is scored, and never starts an automatic cycle

Reflection gains one rule, applied before its routing logic: **if this pass is a reviewer-edit pass,
the route is `human_gate`.** The score is still computed and appended to `reflection_scores`;
`revision_count` is not incremented; the draft is not invalidated; no subtopic returns to `pending`.

An edit therefore never consumes an automatic revision, and a reviewer can still edit a job that has
already spent both cycles — which is what gl §6 already says about the counter.

### 3. **Scope boundary — an edit is a synthesis operation over existing evidence, never a research request**

**This is the load-bearing sentence of this record.**

> A reviewer `edit` instructs the Synthesizer to rewrite the report **from the evidence the job has
> already gathered**. It is not a request for new research, and it must never become one implicitly.

The required flow, and why each step is in it:

```text
reviewer edit
    ↓  the instruction reaches the Synthesizer prompt, and nothing else
Synthesizer   applies it using the findings already in state - no new evidence exists to use
    ↓
Fact-Checker  verifies the resulting claims against their cited sources, exactly as for any draft
    ↓         (this is what catches an instruction satisfied by mis-attributing an existing finding)
Reflection    scores the result and records it - and routes to the gate, by decision 2
    ↓
Human gate    the reviewer sees the new draft, the new score, and any gap
```

**When the reviewer asks for something the evidence does not support**, four things are required, and
they are listed in the order of how strongly each is guaranteed:

| Requirement | How it holds |
|---|---|
| **Do not invent it** | **Structural.** `Claim.finding_ids` has `min_length=1`, a claim citing an id that is not in state fails the job with `report_cites_unknown_findings`, and `Report.sources` is derived in Python from the findings actually cited — the model gets no opportunity to name a URL |
| **Do not silently launch the Researcher** | **Structural.** The Synthesizer has no tools, and decision 2 removes the only remaining path — reflection's research route — from the edit pass |
| **Do not start an automatic research/revision cycle** | **Structural**, by decision 2 |
| **Surface the evidence gap to the reviewer** | **Prompt-level, plus the gate.** The Synthesizer already carries the rule "where a subtopic has no findings, say so in the report as a gap. Do not fill it in"; step 17 extends the same sentence to the reviewer's instruction. The reviewer payload independently shows unresearched subtopics and unsupported claims first, and reflection's completeness score moves |

**Worked example — "Add missing information about Product B":**

- **Product B evidence exists in `findings`** → the Synthesizer may incorporate it, cite it, and the
  Fact-Checker verifies it like any other claim.
- **Product B evidence does not exist** → the report says so as a gap and returns to the reviewer. No
  search is issued, no finding is invented, no cycle starts. The reviewer's route to that evidence is
  a new job, not this one.

**Be precise about what "surface the gap" is and is not.** Invention is prevented by validation;
research is prevented by topology. The *statement* of the gap is a prompt instruction, so it is a
behaviour rather than a guarantee — the honest claim is "bounded and reported, with a human as the
backstop", the same shape gl §8 uses for reflection's injection exposure. What a test can assert is
the structural half: no research ran, no unsourced claim exists, and the export gate still holds.

#### Grounding is not relaxed for a reviewer

> **The `report_cites_unknown_findings` guard is unchanged on the edit path.** A draft that cites a
> finding the job does not hold fails the job, whether the model wrote it unprompted or wrote it
> because a reviewer asked for something the evidence cannot support.

This was the one item this record left undecided at review, and it is now decided: **the guard
stays.** The cost is real and is accepted — an edit can end a job that was one approval away from
export, and the reviewer must resubmit. Softening it was rejected for three reasons:

- **A per-path exception weakens the invariant everywhere.** "Every claim traces to a finding" is
  either true of the Synthesizer's output or it is a default the code sometimes applies. gl §3 is
  explicit that a wrong value is worse than a visible failure, precisely because it survives into the
  report and looks deliberate.
- **The failure is loud, and the alternative is quiet.** Dropping the offending claim instead would
  hand the reviewer a report that silently omits the thing they asked for — the failure mode this
  system exists to prevent.
- **The mitigation belongs upstream.** The prompt rule above tells the model to state the gap rather
  than reach for a citation, so the guard should be the backstop it already is, not the ordinary
  outcome of an edit. If measurement later shows reviewers hitting it often, that is evidence about
  the prompt, and it gets its own record.

`agents/synthesizer.py` therefore needs **no change** for this decision, and
`tests/test_agents_synthesizer.py::test_a_claim_citing_a_finding_that_does_not_exist_fails_the_job`
is the regression test that already covers it.

**Explicitly future work, not step 17:** reviewer-triggered research, adaptive re-planning from an
edit, or any path where reviewer text reaches a search query. That last one is why it is deferred
rather than merely unbuilt — it inherits every open question in §22 Q4 (how a query is generated, how
many are allowed, what the extra Tavily traffic costs, how "better evidence" is judged against an
uncalibrated rubric) **plus** two of its own: which role may spend more research budget on someone
else's job (gl §16), and the fact that queries today come from the plan alone, which §7 and §13 treat
as a property worth keeping.

### 4. `reviewer_edit_text` — one writer, two readers

| Role | Component | Rule |
|---|---|---|
| **Writer** | `human_gate` only | Every decision writes the field: the reviewer's text on `edit`, `None` on `approve` and `reject` |
| **Reader** | Synthesizer | Reads it into the prompt as a distinct instruction section |
| **Reader** | Reflection | `reviewer_edit_text is not None` **is** the edit-pass marker |

**This amends the documented lifecycle** (ARCHITECTURE §5's ownership row and §12's sentence), which
says the Synthesizer clears it at the end of the pass that applies it. It cannot: reflection runs two
nodes later and would have nothing left to read, which would force a second state field for one
concept. The gate sets it and the gate clears it — symmetric ownership, one field.

**"Applied exactly once" still holds, and decision 2 is what makes it hold.** Under this ADR exactly
one Synthesizer pass sits between the gate and the gate, so there is no second pass that could
re-apply the text. A test pins that, because the property is a consequence of the routing rule rather
than of the field.

**Mechanical note for the implementer.** `interrupt()` aborts the node and re-runs it from the top on
resume, so nothing written before that line survives. The clear is therefore part of the `Command`
returned *after* the interrupt returns a decision — not something written on entry.

### 5. `quality_flag = "below_threshold"` gains a third reason

Its documented meaning — "a failing score **no automatic cycle can fix**: the revision cap, or every
subtopic already `unresearched`" — becomes:

> **A failing score the graph will not act on automatically.** Three ways to get here: both
> improvement cycles are spent; every subtopic is already `unresearched` (ADR 0004); or the pass was a
> reviewer edit, which returns to the gate by design.

The value, the `QualityFlag` literal, the `ck_jobs_quality_flag` CHECK constraint and the API contract
are all unchanged — only the sentence that explains it. Writing `None` on a failing edit pass was
rejected: it would show a reviewer a failing report with no flag on it.

### 6. `MAX_REVIEWER_EDITS` = 3, and **`MAX_SUPERVISOR_HOPS` stays 24**

**Add the bound now.** gl §7 requires every retry loop to be bounded with a stated give-up, and
decisions 1–3 are what make the bound derivable: with a constant per-edit cost, three is a number
rather than a guess. CLAUDE.md and gl §13 already name the variable and the value 3 as planned, so
this adopts a documented plan rather than inventing one.

- **Default 3.** `MAX_REVIEWER_EDITS=3`, in CLAUDE.md's environment table and `config.py`.
- **Enforced at the approval endpoint (step 18), before the graph is resumed** — the same place and
  the same shape as decision 7.
- **Counted from `audit_events`:** rows with `action = 'reviewer_decision'` and
  `detail->>'decision' = 'edit'` for that job. **No new column and no migration** — the audit trail
  step 15 built is already the record.
- **At the bound:** `409` with a stable error code (`reviewer_edit_limit_reached`). The job stays
  `awaiting_approval` with its draft intact; approve and reject remain available. A refused edit
  spends nothing.

#### The hop ceiling — locked

> **`MAX_SUPERVISOR_HOPS` remains 24. It is not reduced to 20.**

| Contribution | Hops |
|---|---|
| Automatic workflow worst case (`N`=5 subtopics, `MAX_REVISIONS`=2) — gl §5's derivation | 20 |
| Reviewer edits at `MAX_REVIEWER_EDITS` = 3, **+1 hop each** (`synthesizer → supervisor → fact_checker`; the gate's own `Command` and reflection's route cost none) | +3 |
| **Derived legitimate maximum with the bound in place** | **23** |
| Default | **24** — the ceiling plus one |

**This corrects gl §5**, which says the `+4` is "temporary margin for the unbounded reviewer-edit
path" and that "20 becomes the correct value" once the path is bounded. That is wrong under the bound
the same documents plan: bounding edits at 3 makes three edit hops **legitimate**, so the ceiling
rises from 20 to 23 rather than falling to 20. A guard at 20 would kill a job that used its three
permitted edits — a legitimate job, stopped by the guard that exists to catch oscillation. The `+4`
therefore stops being temporary margin and becomes the ordinary one-hop headroom above a derived
ceiling, which is the same reasoning gl §5 already gives for not setting a guard exactly at its
maximum.

**The call budget is unaffected.** gl §13's `total = 1 + 24 + 45 + 3 × (3 + E)` gives **88 at E = 3**,
and `MAX_LLM_CALLS_PER_JOB` = 60 remains the binding guard in every case. Bounding edits does not
require revisiting 60.

### 7. An edit that cannot fit the remaining call budget is refused **before graph execution**

**Today's behaviour is the harm to avoid.** An edit on a job near the budget spends calls until the
Supervisor's guard trips, then ends the job at `status=failed`, `failure_reason=budget_exceeded`.
`export` never runs, so `jobs.report_json` is never written: **a reviewer who had an approvable report
in hand loses it by asking for a change.**

- **The rule:** if `MAX_LLM_CALLS_PER_JOB − llm_calls_used < 3` — the three logical calls an edit
  needs at minimum (Synthesizer, Fact-Checker, reflection; the Supervisor hop is already inside the
  24) — the endpoint refuses the edit with `409` and the stable code
  `insufficient_call_budget_for_edit`, **without resuming the graph**. The job stays
  `awaiting_approval`; approve and reject remain available and cost nothing.
- **The residual risk is stated rather than engineered away.** A logical call costs 1 request when it
  works and up to 2 when validation retries, plus transport retries (gl §13), so an edit that clears
  the pre-check can still trip the guard mid-pass. That backstop is unchanged and already loud.
  Inflating the pre-check to a worst case would invent a number and refuse edits that would have been
  fine.

#### Source of truth for the live call count — locked

> **The live `llm_calls_used` comes from the checkpoint, surfaced in the gate payload. It is
> never read from `jobs.llm_calls_used`.**

| Source | Value while the job waits at the gate | Verdict |
|---|---|---|
| Checkpoint for `thread_id = job_id` — `ResearchState["llm_calls_used"]`, written by every node that spends a call | Current | **Authoritative.** The gate payload carries it, so the reviewer can also see what an edit has room for |
| `jobs.llm_calls_used` | **`0`** | **Never use it here** |

`jobs.llm_calls_used` is written **only by `finish_job` at finalize** (ADR 0005, step 15), and
`create_job` inserts the row with the column's `0` default. A job sitting at the gate therefore has
`0` in that column no matter how many calls it has spent — so a pre-check reading it would compute a
full budget every time and **allow every edit, silently**, which is precisely the failure mode this
rule exists to prevent. The column is for auditing a job after it ends, and this decision does not
change that.

### 8. Reviewer text is authenticated input, and is handled as such

- **It is not wrapped in `as_untrusted_block()`.** That wrapper tells the model to treat text as data
  and never follow an instruction inside it, which is exactly the opposite of what a reviewer's edit
  is for. Invariant 4 governs **fetched third-party content**; a reviewer is an authenticated,
  authorized human (invariant 7), and conflating the two would either neuter the feature or dilute
  what the wrapper means everywhere else.
- **It is validated at the edge, like the question.** Length-capped and stripped of control characters
  at `POST /jobs/{id}/approve`, the same treatment `POST /jobs` already gives `question`
  (ARCHITECTURE §10).
- **It cannot reach a tool argument.** The Synthesizer has no tools, so this holds structurally rather
  than by rule; search queries still come from the plan (invariant 4, §7). Decision 3 is what keeps
  that true — the moment an edit could trigger research, reviewer text would be reaching a query.
- **Its blast radius is one draft's prose.** It cannot name a route (reflection's route on that pass
  is fixed in code by decision 2), cannot add a source (`Report.sources` is derived from the findings
  actually cited), cannot invent a finding, cannot cause a fetch, and cannot bypass the export gate.
- **It is stored and it travels.** The text lands in `audit_events.detail` and is kept for the
  retention window (gl §9), and it reaches LangSmith when tracing is on — the same PII position the
  question already has, recorded here so it is a decision rather than an oversight.

### 9. Audit and checkpoint implications

**Audit — no new action, no migration.**

| Row | Written by | Actor | Detail |
|---|---|---|---|
| `gate_opened` | the gate node, on every gate visit | `system` | the payload's summary counts |
| `reviewer_decision` | **the endpoint**, before it resumes the graph (ARCHITECTURE §12) | the reviewer's identity | `{decision, note, edits}` |

Both actions are already in `AuditAction` and in the `ck_audit_events_action` CHECK constraint, so
step 17 adds **no Alembic revision**. One edit therefore produces a second `gate_opened`, which is
what makes "how many times did this job go back to a human?" a query — and it is the same row
decision 6 counts the edits from. The edit pass itself writes no row: a pass that reaches the gate is
not an event, consistent with ADR 0005.

**Checkpoint — nothing changes.** No new state key, so `ResearchState`, `CHECKPOINTED_TYPES`, the
serde, the Postgres checkpointer, and the "no state field the contract does not name" test are all
untouched. `reviewer_edit_text` is a `str | None` that already travels in the checkpoint — and by
decision 7 that checkpoint is also where the live call count is read from.

---

## Alternatives rejected

| Option | Why not |
|---|---|
| **Treat an edit like any other draft** (what the code does now) | Contradicts gl §10 in three documents; lets one edit consume the remaining automatic cycles; lets a reviewer's wording silently launch the Researcher, which decision 3 forbids; and it is the option that makes gap (c) dangerous — the reviewer's instruction would be re-applied to drafts they never saw |
| **Let an edit request research when the evidence is missing** | The honest version of what a reviewer often means, and out of scope for step 17: it needs §22 Q4's unanswered questions plus an authorization rule, and it would put reviewer text upstream of a search query. Recorded as future work rather than half-built |
| **Hybrid: return to the gate unless `citation_coverage` fails** | Two rules where one will do, for a case already covered twice: `failed_dimensions` is shown at the gate *before* approval, and the export gate blocks an uncited claim regardless of what the reviewer decides (invariant 1) |
| **Skip reflection on the edit path** | `fact_checker → reflection` is a fixed edge and reflection is not a Supervisor target (invariant 8); routing around it is a topology change. It would also hand the reviewer a stale score for a draft they just changed |
| **Clear `reviewer_edit_text` in the Synthesizer, add a separate edit-pass flag** | Two state fields for one concept, plus a `ResearchState` change with its documentation, contract test, and checkpoint implications — to avoid amending one sentence |
| **Soften `report_cites_unknown_findings` on the edit path** — drop the offending claim instead of failing | A per-path exception makes "every claim traces to a finding" a default the code sometimes applies rather than a property of the Synthesizer's output, and it swaps a loud failure for a report that silently omits what the reviewer asked for (gl §3) |
| **Reduce `MAX_SUPERVISOR_HOPS` to 20 once edits are bounded** (what gl §5 currently says) | Wrong arithmetic: the bound makes three edit hops legitimate, so the ceiling is 23. A guard at 20 stops a legitimate three-edit job |
| **Read the live call count from `jobs.llm_calls_used`** | That column is `0` until finalize, so the check would pass every time — a silent wrong answer rather than a bound |
| **Enforce both caps inside the gate node** (`Command(goto="human_gate")` back to itself) | Workable, and it would make the graph self-protecting rather than relying on its caller. Rejected as the primary because validation belongs at the edge — §12 already has the endpoint record the decision and then resume — and a self-looping interrupt is a subtler resume path to test. Worth revisiting if a second caller ever resumes the graph |

---

## Consequences

- **A reviewer whose edit makes the report worse gets it back with a fresh score, and must edit again
  or reject.** That is the intended loop and it costs a round trip. It is the price of the reviewer
  being in charge after they intervene.
- **A reviewer cannot grow the evidence base from the gate.** An edit rewrites what exists; the route
  to new evidence is a new job. This is a real product limitation, deliberately taken, and it is the
  thing reviewer-triggered research would later remove.
- **An edit's cost becomes a constant** — 3 logical calls and 1 hop — which is what makes both caps in
  decisions 6 and 7 arithmetic rather than judgement.
- **`MAX_SUPERVISOR_HOPS` = 24 gains a derivation** (20 + 3 + 1) and gl §5's instruction to reduce it
  to 20 is withdrawn.
- **Two documented contracts are amended**: the `reviewer_edit_text` lifecycle and the meaning of
  `below_threshold`. Both are sentences, not schemas — no migration, no API change.
- **A latent trap is created and pinned by a test.** If a future change ever lets the Synthesizer run
  twice while `reviewer_edit_text` is set, the text is applied twice. Decision 2 is what prevents it,
  so the test that proves the field is applied once is really a test of the routing rule.
- **An edit can still fail a job, and that is the accepted trade.** If an instruction the evidence
  cannot support provokes the model into citing a finding id that does not exist, the existing
  Synthesizer guard fails the job with `report_cites_unknown_findings` — unchanged on this path by
  decision 3, so a reviewer edit can end a job that was one approval away from export. The prompt rule
  is the mitigation, the guard is the backstop, and a reviewer who hits it resubmits. **Watch for it:**
  if it turns out to be a common outcome rather than a rare one, that is evidence about the prompt and
  earns its own record.
- **§22 questions 2 and 3 both close.** Question 3's "deferred, design settled" becomes implemented,
  because the two halves cannot ship apart.

---

## What step 17 would change, exactly

| File | Change |
|---|---|
| `graph/reflection.py` | The edit-pass rule in `_scored`: when `state["reviewer_edit_text"] is not None`, route `human_gate`, write no `revision_count`, no draft invalidation; `quality_flag` per decision 5 |
| `graph/build.py` | `human_gate_node`: write `reviewer_edit_text` on **every** decision (text on `edit`, `None` on `approve`/`reject`); build the reviewer payload (problems first, and the live `llm_calls_used` per decision 7); write `gate_opened`; set `status="awaiting_approval"` where the interrupt is observed |
| `agents/synthesizer.py` | Read `reviewer_edit_text` into the prompt as a distinct instruction section, outside `as_untrusted_block()` (decision 8), and extend the existing "say so as a gap, do not fill it in" rule to cover an instruction the findings cannot support (decision 3) |
| `config.py` | `MAX_REVIEWER_EDITS`, default 3 |
| `.env.example`, `CLAUDE.md` | The new variable and its purpose in the environment table |
| `docs/ARCHITECTURE.md` | §3 approval path, §5 ownership row, §12 (the `[derived]` note becomes enforced; the `quality_flag` table gains its third reason; the "not built yet" block goes), §22 Q2/Q3 close |
| `docs/engineering-guidelines.md` | §6 `below_threshold`, §10 the gate's decisions and the evidence-scope boundary, §13 the E = 3 row as built rather than planned |
| `docs/adr/README.md` | This record, once accepted |

**Not in step 17:** the endpoint itself, API-key auth, the `reviewer_decision` audit row and both
`409` refusals from decisions 6 and 7 (all step 18, because they need the authenticated caller), the
7-day expiry sweep (Phase 5, step 32), and reviewer-triggered research (future work, decision 3).

## Tests required

Offline, on the existing harness, no new fixtures:

1. An edited draft that scores **below** the threshold **with revisions left** routes to `human_gate`,
   and `revision_count` is unchanged. *(The rule itself — this test fails against today's code.)*
2. The same pass records its score in `reflection_scores` and sets `quality_flag="below_threshold"`.
3. **No research runs on the edit path**: after an edit, `RecordedWeb` logs no new query and no new
   fetch, and the fake LLM receives no `researcher` request — asserted over a whole job, with a
   reflection score whose lowest failing dimension is a research dimension, which is the case that
   would route to the Researcher without decision 2.
4. **An edit that asks for unsupported information produces no unsourced claim**: every claim in the
   resulting draft still reaches a source URL, and a draft citing an unknown finding id still fails
   the job rather than exporting. *(The second half is already covered by
   `test_a_claim_citing_a_finding_that_does_not_exist_fails_the_job`, which decision 3 keeps as the
   backstop for this path — step 17 adds the edit-path case, not a new guard.)*
5. The reviewer's text reaches the Synthesizer prompt **once**; a later pass in the same job does not
   contain it.
6. `approve` and `reject` clear `reviewer_edit_text`; a second `edit` replaces it.
7. An edit on a job that has spent both revisions still runs (the cap does not starve a reviewer).
8. The export gate still blocks an edited draft whose claims lost their citations.
9. `gate_opened` appears once per gate visit — twice after one edit — and no `revision` row is written
   for the edit pass.
10. A whole-job integration run of the full shape: gate → edit → synthesizer → fact_checker →
    reflection → gate → approve → export, asserting the documented cost of exactly **3 logical calls
    and 1 hop**.
11. Three edits on one job stay inside `MAX_SUPERVISOR_HOPS` = 24 — the 23-hop derivation in decision
    6, asserted rather than believed.

**One of these could be written before step 17, and was**, because it pins behaviour that exists
today rather than behaviour the step adds:
`tests/test_graph_persistence.py::test_the_jobs_row_call_count_is_stale_while_a_job_waits_at_the_gate`
asserts that `jobs.llm_calls_used` reads `0` at the gate while the checkpoint carries the real number.
That is decision 7's trap, and it is now a failing test the day anyone wires a budget check to the
wrong column.
