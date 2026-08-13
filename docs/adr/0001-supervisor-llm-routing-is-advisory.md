# ADR 0001 — The Supervisor's LLM routing call is advisory

- **Status:** Accepted
- **Date:** 2026-08-12
- **Affects:** `agents/supervisor.py` · `CLAUDE.md` · `docs/engineering-guidelines.md` §2.1, §5, §18, §22 ·
  `docs/ARCHITECTURE.md` §4.1, §6.1, §20
- **Found by:** Step 12, gate 5 — the first two real smoke jobs (`ARCHITECTURE.md` §21)

---

## Context

The Supervisor computed the route twice: once in Python, once in the model. Reading `decide_next()`
as it executed before this ADR:

```python
allowed = allowed_target(state)       # deterministic, from structured state alone
decision = llm.call_structured(...)   # the fast-tier routing call
if decision.next != allowed:          # disagreement
    ...                               # -> finalize, status=failed, invalid_route
return decision                       # decision.next == allowed, on every surviving path
```

The value returned was **always** `allowed_target(state)`. The model's answer reached exactly two
places: an equality check, and a `reason` string in one log line. It could not select a route. It
could only agree — which is indistinguishable from not calling it — or disagree, which killed the
job.

### What the first two real jobs showed

| Run | Fast model | Supervisor calls | Wrong at | Proposed / required | Outcome |
|---|---|---|---|---|---|
| 1 | `meta/llama-3.1-8b-instruct` | 4 | hop 4 | `planner` / `researcher` | `invalid_route` at 275.6s, 11 calls |
| 2 | `nvidia/nemotron-3-nano-30b-a3b` | 6 | hop 6 | `researcher` / `synthesizer` | `invalid_route` at 426.5s, 18 calls |

**2 of 10 Supervisor calls disagreed (20%).** At that rate a job with ~7 hops survives with
probability `0.8⁷ ≈ 21%`; two of two jobs died, which is consistent. `n=10` is small, so 20% is an
order of magnitude rather than a measured rate — but the argument does not depend on the rate. Any
non-zero disagreement rate is job mortality bought with no routing benefit.

Run 2 reached the Planner and four Researcher passes — 23 findings, zero malformed JSON, zero
validation retries, 18 of 60 calls — and was killed by a routing call that could not change the
route it was killing.

### Why validation did not catch it

`SupervisorDecision.next` is a `Literal` of the five valid node names, so `researcher` is
**schema-valid and semantically wrong**. Pydantic validates shape, not appropriateness, and the
bounded validation retry in `llm_client.py` never fired because nothing was malformed. Wrong-but-valid
structured output passes every validation layer this system has. That is the defect class, and it is
not fixable by strengthening validation — which is why this ADR does not touch it.

### Why this is structural, not a model-selection problem

Two different models, on the fast tier, at two different graph states, produced the same failure. A
third model was rejected before it ran: `meta/llama-3.3-70b-instruct` answers in 37–85s against a 30s
fast-tier timeout (`gl §17`), so every routing call would time out. Model selection cannot remove a
failure mode created by asking a model a question whose answer is already known.

---

## Decision

**`allowed_target(state)` is the sole authority for the Supervisor's route. The LLM call continues to
run, on the fast tier, and its proposal is advisory: it is logged and it never controls or blocks
routing.**

Seven points, stated so they are decided rather than discovered:

1. **`allowed_target(state)` is the only source of the actual route.** It is unchanged by this ADR,
   and it is not weakened: a state the table does not cover still routes to `finalize` with
   `no_valid_transition`.
2. **The advisory call stays on `LLM_FAST_MODEL`, one call per hop, bounded by
   `MAX_SUPERVISOR_HOPS` = 12.** Call counts, budget arithmetic, and latency are unchanged.
3. **A wrong-but-valid proposal does not fail the job.** It is logged as a disagreement and the graph
   proceeds on `allowed_target(state)`. `invalid_route` ceases to exist as a job-fatal reason.
4. **`invalid_output` and `llm_call_failed` from the advisory call do not block routing.** They are
   logged and the graph proceeds on `allowed_target(state)`. Routing does not depend on a call whose
   answer it does not use.
5. **`rate_limited` and `budget_exceeded` remain job-fatal, unchanged.** No change is necessary and
   none is made. `budget_exceeded` is `CLAUDE.md` invariant 3 and is not negotiable. `rate_limited`
   stays fatal because the fast and main tiers share one 40 RPM account limit (`gl §13`) — a
   rate-limited Supervisor means the next Researcher or Synthesizer call is rate-limited too, so
   failing here fails early with an accurate reason instead of deeper with the same one. `gl §13`'s
   "a rate-limited job fails visibly" continues to hold exactly as written.
6. **`pending` is the only unresolved subtopic state.** `done` and `unresearched` are both terminal
   for routing. This was already true of `allowed_target()`, which tests only for `pending`, but it
   was never stated in the routing prompt or at the point of use in the documentation — see below.
7. **The Supervisor remains one of the five agents.** It keeps its module, its contract, its input,
   its model tier, and its budget. Its job changes from *deciding* the route to *explaining* it.

### The `unresearched` ambiguity, fixed

Run 2's failing hop sent these counters against these prompt rules:

```text
subtopics_pending: 0        │  a subtopic is still pending              -> researcher
subtopics_done: 3           │  every subtopic resolved and no draft yet -> synthesizer
subtopics_unresearched: 1   │
```

`allowed_target()` treats only `pending` as unresolved, so the answer was `synthesizer`. But the
prompt never defined "resolved", and the counter is named `subtopics_unresearched` — which reads as
"not researched yet, so research it". The model's reading was defensible; the prompt was ambiguous.
The same undefined word appears in `gl §5` and `ARCHITECTURE.md` §6.1.

Point 6 fixes the prompt and the two documents. It is **not** load-bearing under this ADR — a wrong
proposal is now harmless — so it is included only because a clearer prompt makes the disagreement
rate a cleaner signal. It would not have been sufficient on its own: run 1 failed on
`plan_exists: True, subtopics_pending: 3`, which was never ambiguous.

---

## Alternatives considered

| | Why not |
|---|---|
| **Keep strict agree-or-fail** | Falsified by evidence: two models, two states, 2/2 jobs dead. It also collapses under the question "what does the model contribute?" — the answer is one log line |
| **Retry once on mismatch** | Treats a structural defect as flakiness. At a 20% per-call rate, one retry still leaves ~25% of 7-hop jobs dead, and it adds a retry policy `gl §17` does not have |
| **Remove the routing call entirely** | The honest end state **if** the call proves worthless, and it would save 6–12 calls per job — roughly a third of typical calls. Rejected *for now* because it is an identity change: the Supervisor would stop being an LLM agent, contradicting `CLAUDE.md` invariant 8 and the five-agent framing throughout both documents, and it would rewrite `harness.ROLE_SCHEMAS` and every graph-level `FakeLLM` supervisor script. Deciding that on `n=10` would be exactly the assumption-over-measurement this project's rules forbid |

---

## Consequences

### What gets better

- **The failure class is gone.** No proposal, however wrong, can end a job.
- **The injection boundary is strengthened, not weakened.** `ARCHITECTURE.md` §20 decision 2 justifies
  the Supervisor as the structural defense against injection reaching control flow. That property
  belongs entirely to `allowed_target()` reading structured state. The advisory call was the only
  component in the routing path an injected page could theoretically influence, and it now has no
  authority to influence.
- **The waste becomes measurable.** Disagreements are logged, so the 20-job run yields a real
  disagreement rate — the evidence needed to decide later whether to remove the call.

### What we accept

- **6–12 fast-tier calls per job that cannot change behaviour** — about a third of a typical job's
  calls, and 12 of the 60-call ceiling in the worst case. This is real waste, kept deliberately and
  temporarily so the decision to remove it can be made on measurement instead of on two data points.
- **`reason` is no longer the model's sole account of the route.** On agreement it carries the model's
  rationale; on disagreement or advisory failure it is written in code and names what happened.

### Unchanged

`ResearchState` (no new fields) · the graph topology · `MAX_LLM_CALLS_PER_JOB` = 60 and the 41/44
worst-case arithmetic in `ARCHITECTURE.md` §16 · `MAX_SUPERVISOR_HOPS` = 12 and the hop guard · the
budget guard · `SupervisorDecision` · `allowed_target()` · `CLAUDE.md` invariants 3, 4, 5, and 8.

### Revisit when

The 20-job measurement reports the disagreement rate. If the advisory call agrees essentially always,
it is buying nothing and removing it becomes a small, evidence-backed follow-up. If it disagrees at a
material rate, that rate is itself the argument for removal — and either way the decision is then made
on data.
