# ADR 0004 — Reflection does not retry a Researcher target that is already exhausted

- **Status:** Accepted
- **Date:** 2026-08-14
- **Affects:** `graph/reflection.py` · `docs/ARCHITECTURE.md` §3, §6, §22 ·
  `docs/engineering-guidelines.md` §6 · `CLAUDE.md`
- **Found by:** The 6-job diagnostic run of 2026-08-13/14 — `measure-06` spent three revision
  cycles re-researching the same subtopic and produced nothing each time

---

## Context

Reflection routes a failing **research completeness** or **source correctness** score back to the
Researcher. It does that by invalidating the draft and returning the thin subtopics to `pending`
(`gl §6`), and the transition table carries the job through the Researcher, the Synthesizer and the
Fact-Checker back to reflection.

The retry's **inputs** are identical to the visit that came before it:

- The query is **`subtopic.search_query`, taken from the plan**, and the plan does not change. The
  Researcher deliberately never writes a query of its own — queries come from the plan and URLs come
  from a search result, which is what keeps fetched text out of a tool argument (`gl §8`).
- That search is a **cache hit**. `cache:search:{hash}` holds for 24h, so the retry gets the
  identical result list.
- A source that failed is **not cached**. `tools/fetch.py` writes only a `FetchedPage`, so an
  `Unreachable` is attempted again — and fails again for the same reason.
- The per-job `seen` set that makes the Researcher reach for *new* sources is built from
  `Finding.url`. **A visit that produced no findings adds nothing to it**, so the next visit starts
  from the same position in the same list.

So a subtopic whose last visit produced nothing is re-researched from identical inputs: the same
query, the same result list, the same reachable and unreachable sources, and no new URL to reach for.

**The outcome is not identical, and this record does not claim it is.** Extraction is a fresh LLM
call over those pages, so a retry can find evidence the previous visit missed — measured, twice it
did (below). What the retry reliably *is* is expensive: a full cycle of Researcher, Synthesizer,
Fact-Checker and reflection, aimed at the one subtopic with no unread source left to reach.

### What was measured

**`measure-06`** — *"Compare Datadog and New Relic on observability products"*, from
`measurements/console.diagnostic-6job.log` and `measurements/run.log`:

```text
researcher   s5 produced no findings; marking it unresearched
reflection   scored 3.00 and re-researches ['s5']
             search cache hit for '<the same planned query>'
             source unusable: <A> (http_error)      <B> (robots_denied)
                              <C> (http_error)      <D> (http_error)
researcher   s5 produced no findings; marking it unresearched
reflection   scored 3.80 and re-researches ['s5']
             search cache hit for '<the same planned query>'
             ... the same four sources, the same four reasons ...
researcher   s5 produced no findings; marking it unresearched
reflection   scored 2.50 and re-researches ['s5']
             ... the same four sources, the same four reasons ...
researcher   s5 produced no findings; marking it unresearched
reflection   scored 3.50 and routed to the human gate
```

Four visits to `s5`, four times zero findings, and the same four URLs failing with the same four
reasons on every one of them. The job's row in `measurements/jobs.jsonl`:

| | |
|---|---:|
| Status | `approved`, `quality_flag` `None` |
| Wall clock | **1,675.0s** |
| LLM calls | **46** of 60 |
| Revision cycles | **3** |
| Findings | 45 |
| Subtopics left `unresearched` | 1 |

**The three cycles cost 19 of the 46 LLM calls and 1,059s of the 1,675s — 63% of the job's wall
clock** — measured from the reflection-to-reflection spans in the console log (calls 27→34→40→46,
615.5s→1,134.7s→1,349.8s→1,674.9s).

They bought nothing. `s5` contributed no findings at any point, so **all four reflection passes
scored a report built from the same 45 findings**; only the redraft differed. The scores 3.00, 3.80,
2.50, 3.50 are the scorer moving on unchanged evidence, and the pass on the fourth is that movement
crossing 3.5 rather than an improvement. `measure-06` ran under the NIM development override
`MAX_REVISIONS=3`; at the production default of 2 the same loop spends two cycles instead of three.

### How often a retry produces findings, and how often it does not

Across the n=20 baseline (`measurements/run.historical-step12-n20.log`) and the 6-job diagnostic,
reflection made **10 routing decisions** naming **11 Researcher targets** — one decision named two
subtopics:

| The retry targeted | Targets | Produced findings |
|---|---:|---:|
| A subtopic already marked `unresearched` | 6 | **2 of 6** |
| A subtopic that had findings (`done`) | 5 | 3 of 5 |

An `unresearched` retry therefore comes back empty **4 times in 6** — often, not always. A `done`
subtopic has its own URLs in `seen`, so its retry reaches **past** them for a source it has not read;
that is why the `Notion` job's `s5`, the `Wix` job's `s3` and one subtopic of an `OpenAI` job's retry
all came back with new evidence.

`unresearched` says where the remaining opportunity is thinnest. It does not say what a retry would
find.

### The counterexample, in full

The n=20 run's TCS job settles the point. `s1` produced nothing on its first visit and was marked
`unresearched`; reflection sent it back anyway, and it came back with evidence:

```text
23:46:35  subtopic s1 produced no findings; marking it unresearched
23:54:41  synthesizer drafted 20 claims across 8 sources
23:56:19  reflection scored 3.00 and re-researches ['s1']
23:56:19  search cache hit for 'TCS cloud strategy goals priorities annual report'
23:56:20  source unusable: <url> (http_error)     <- the same failure as the first visit
23:59:46  synthesizer drafted 22 claims across 9 sources
```

**Eight sources became nine.** The query, the cached result list and the failing URL were identical.
The first visit had fetched pages and spent three extraction calls that returned nothing usable; the
retry re-extracted from those same pages and found something.

### The one case that genuinely is deterministic

There is a shape where a retry provably cannot produce findings: **when every candidate source for a
subtopic is unreachable, no page is fetched, no extraction call is made, and zero findings follows by
construction.** That is the only place where "the inputs are identical" carries all the way through
to "the outcome is identical", and it is narrow — it needs *every* candidate to fail, not most.

**`measure-06` was not that case.** Four of its five candidates were unusable, but one page was
fetched and extracted on each of the three retry visits — one LLM call each, calls 27→28, 34→35 and
40→41 — and every one of them returned nothing, as the first visit had. That is an **observed
repeated-empty outcome, not a proof**: four empty samples from the same page is what happened, not
what had to happen.

The distinction matters because the guard keys on `unresearched`, which covers both shapes without
telling them apart. Separating them would mean recording whether a visit made any extraction call at
all — a new state field, which this record deliberately does not add (`ARCHITECTURE.md` §5 refused a
retry-scope field for the same reason).

### The code already contradicted itself here

`unresearched` is documented as terminal everywhere except the one place that reactivated it. The
Supervisor's own prompt says so in as many words — *"`unresearched` means it was already attempted
and yielded nothing, so it is finished and must never be sent back to the researcher"* — and
[ADR 0001](0001-supervisor-llm-routing-is-advisory.md) point 6 records `done` and `unresearched` as
both terminal, after a real job routed on the other reading. `ARCHITECTURE.md` §6.2 then carved out
"only reflection can return it to `pending`", and that carve-out is the loop.

---

## Decision

**A subtopic marked `unresearched` is not a Researcher target. When no target is left, reflection acts
on the next failing dimension instead — and if none is left, the job goes to the human gate carrying
`quality_flag="below_threshold"`.**

Two changes in `graph/reflection.py`, and nothing else:

- **`_thin_subtopics` skips `unresearched`.** They are excluded before thinness is computed, so the
  "fewest sources" fallback cannot reach for one either — an exhausted subtopic is thinner than every
  eligible one and would otherwise always win.
- **`_route_for` replaces the direct table lookup.** When the lowest failing dimension routes to the
  Researcher and there is no target, **both** research dimensions are dropped — whether a target
  exists is a property of the subtopics, not of which dimension scored lowest — and the lowest
  remaining failing dimension is acted on. With none left, the route is `human_gate`.

**Nothing changes for an eligible target**, and that is as deliberate as the exclusion. A `done`
subtopic is still selected, still returned to `pending`, and still re-researched — including through
the "fewest sources" fallback when every eligible subtopic already clears the two-source bar. 3 of
the 5 measured `done` retries produced findings, and that path is untouched.

**This is a revision-budget decision, not a deterministic proof that an `unresearched` retry cannot
produce evidence.** 2 of 6 measured ones did. What such a retry reliably *costs* is:

- the same planned query against the same cached result list — no new search space to explore;
- the same failed fetches attempted again, because an `Unreachable` is not cached;
- up to `MAX_LLM_CALLS_PER_SUBTOPIC` extraction calls over pages a previous visit already read;
- and then the Synthesizer, the Fact-Checker and a reflection pass on top, because a Researcher route
  invalidates the draft — on `measure-06` the three cycles averaged about 6 LLM calls and 350s each.

`MAX_REVISIONS` is **2**. The system spends those two cycles on subtopics that still have an eligible
unread source rather than on re-rolling extraction over pages that already came back empty. **That
trade is the whole of this decision, and it is a heuristic about where the remaining research
opportunity lies** — not a claim about what a retry would have found.

**The gap stays visible.** Reaching the gate this way is the `below_threshold` path, identical to the
revision cap: the score, the `failed_dimensions` and the `unresearched` subtopics all reach the
reviewer, and `gl §10` shows unresearched subtopics **first**. It is never converted into a pass, and
it bypasses nothing — the Fact-Checker has already run, the human gate still holds the job, and the
export gate still refuses an uncited claim.

**No revision is counted for a cycle that is not started.** A job that reaches the gate this way
arrives with its remaining cycles unspent rather than burned.

### What is deliberately unchanged

The scoring weights, `REFLECTION_PASS_THRESHOLD`, `MAX_REVISIONS`, `MAX_SUPERVISOR_HOPS`,
`MAX_LLM_CALLS_PER_JOB`, the graph topology, the Supervisor's authority, `RESEARCHER_CONCURRENCY`,
the search and fetch behaviour, and every prompt. The Researcher is untouched.

---

## Alternatives considered

| Option | Why not |
|---|---|
| **Adaptive query rewriting** | The right long-term answer, and **deferred to its own ADR** — see below. It is a new LLM call, a new bound, and a new prompt-injection surface, and the rubric that would judge whether the new evidence is better is uncalibrated until Phase 4 (`gl §6`) |
| **Bypass the search cache on a retry** | The cache is not the cause. It returns what the provider returned minutes earlier, so bypassing it buys a paid search call for the same list — and it would break the per-job cache-hit rate `gl §14` publishes |
| **Exclude permanently failed URLs** | Already true of URLs that *worked* — `seen` is exactly that. Adding an exclusion list for failures does not help in the measured case: `measure-06`'s visit fetched **one** page from the whole result list, well under `MAX_LLM_CALLS_PER_SUBTOPIC`, which means it walked the candidates to the end. There was nothing further down to reach. It would also need the retry-scope state field `ARCHITECTURE.md` §5 deliberately refused to add |
| **Cache unreachable results** | Saves the repeated fetch attempts, not the cycle. The extraction calls, the redraft, the re-verification and the reflection pass are the expensive part; this makes an unproductive retry cheaper rather than spending the cycle somewhere better |
| **Let `MAX_REVISIONS` absorb it** | It already does — the loop is bounded, which is why this was wasted work rather than a hang. But "bounded" is not "well spent": two cycles aimed at the least promising target are two cycles a job with a real fixable weakness could have used |
| **Do nothing until query rewriting lands** | It is 63% of one measured job's wall clock, and the fix is an eligibility check on a list that is already being built |

---

## Consequences

### What gets better

- A cycle is no longer spent on the subtopic with the least left to find. On `measure-06`'s shape
  that is three cycles, 19 LLM calls, ~1,059s, and the associated search and fetch traffic, none of
  which produced evidence.
- Revision behaviour becomes predictable: a cycle is spent where a specialist has something to act
  on.
- The code now agrees with `ADR 0001` point 6 and with the Supervisor's own prompt about what
  `unresearched` means.

### What we accept

- **Reflection can no longer reactivate an exhausted subtopic.** Until query rewriting exists, an
  `unresearched` subtopic is final for the job — its gap is reported to the reviewer rather than
  retried. That is the outcome the system already calls legitimate (`gl §2.3`: an unresearched
  subtopic is a reportable outcome, not a silent omission).
- **Evidence this guard would have suppressed.** `unresearched` says the last visit produced
  nothing; it does not say the next one would. **Extraction is a fresh LLM call over the same
  pages**, and that is the mechanism — not, as this record first claimed, another subtopic's findings
  shifting the `seen` set. Measured, 2 of 6 such retries produced findings, and one of them took the
  TCS job's report from 8 sources to 9. That evidence is the price of the guard, paid knowingly.
  What is not paid is correctness: the cost of being wrong is a reported gap, never a wrong claim.
- **It does not stop every empty retry.** A `done` subtopic that comes back with nothing is still
  attempted, and 2 of the 5 measured `done` retries did exactly that. Those are legitimate attempts —
  and their outcome is what produces the `unresearched` status this guard then respects.
- **`quality_flag="below_threshold"` now has two causes**, the revision cap and an exhausted research
  target. A reader who needs to tell them apart reads `failed_dimensions` and the subtopic statuses,
  both of which are already in the gate payload.

### Unchanged

Grounding, claim-to-source traceability, fact-check coverage, the export invariant, the injection
boundary, the human gate, and every loop guard. No finding, verdict or report is discarded by this
change: reflection writes none of them, and the guard's whole effect is to *not* write
`subtopic_status` and *not* invalidate the draft.

---

## Future enhancement: adaptive Researcher query rewriting after evidence exhaustion

**Not implemented in the current production-hardening change.** This section exists so the idea is
not lost; the design is open, not settled.

The shape a future implementation might take:

```text
Researcher  ->  no new evidence  ->  generate a bounded alternative query
            ->  new search       ->  is the evidence genuinely new and better?
```

What has to be answered before it is built:

- **How is the query rewritten, and by what?** An LLM call is the obvious answer and is not free —
  it is one more call against the per-job budget and the 40 RPM tier.
- **How many rewrites are allowed per subtopic, and per job?** Unbounded rewriting is the same loop
  with a larger period. `gl §7` requires a bound and a stated give-up.
- **How different must a rewrite be to count as one?** A query that differs by a word returns the
  same results and burns a call proving it.
- **Does it bypass the search cache?** If it does not, a near-identical rewrite is a cache hit and
  changes nothing; if it does, the per-job cache-hit measurement needs re-reading.
- **What does the extra Tavily traffic cost?** One search per rewrite, per subtopic, per cycle.
- **How do we tell the new evidence is better?** The reflection rubric is the only judge available
  and it is uncalibrated until the Phase 4 hand-scoring pass (`gl §6`). "More findings" is not the
  same as "better evidence".
- **What is the injection risk?** This is the serious one. **A query rewritten from anything the
  fetched pages said would let a third party influence a tool argument**, which `CLAUDE.md`
  invariant 4 forbids outright — queries come from the plan today, and that is not an accident. A
  rewrite would have to be derived from the plan and the subtopic question only, and the boundary
  test would have to prove it.

Recorded in `ARCHITECTURE.md` §22 as a deferred question so it is found from the open-questions list
as well as from here.

---

## Revisit when

- **Query rewriting is designed.** This record's guard is what makes the loop safe *without* it; a
  rewrite path would change when a subtopic is genuinely exhausted, and this decision should be
  re-read then rather than assumed still true.
- **The rubric is calibrated (Phase 4).** If a calibrated scorer stops marking completeness down on
  jobs whose gap is genuinely unfillable, the Researcher route is taken less often and this guard
  matters less.
- **A retry mechanism that changes the Researcher's inputs is added** — a different provider, a
  deeper result list, or a shared cross-job URL set. Any of those makes "the inputs are identical"
  false, and the eligibility check has to be re-derived from whatever is true then.
