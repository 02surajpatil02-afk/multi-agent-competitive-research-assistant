# ADR 0002 — A subtopic's page extractions run concurrently

- **Status:** Accepted
- **Date:** 2026-08-13
- **Affects:** `agents/researcher.py` · `llm_client.py` · `config.py` · `CLAUDE.md` ·
  `docs/engineering-guidelines.md` §2.3, §13, §14, §17 · `docs/ARCHITECTURE.md` §4.3, §16, §20
- **Found by:** The latency investigation over step 12's n=20 baseline (`ARCHITECTURE.md` §21 step 12)

---

## Correction — 2026-08-13, request counts only

**Every request count in this record was 12 too high and has been corrected below. No time figure, no
share, no projection, and no part of the decision changes.**

The reconstruction this record quotes counted three kinds of log event as one request each: every
httpx `POST` line, every `llm_client … call failed (…), retrying in Ns` line, and every
`… unreachable after N retries` line. **A 5xx is an HTTP response**, so httpx logs it *and*
`llm_client` logs the failure that followed it 0.000–0.002s later — and both were counted. A timeout
or a connection error produces no response, so it appears once and was counted once. The error is
therefore exactly the number of 5xx responses in the sample: **12** — nine 500s, two 503s and one
502, all on the fast tier.

Removing those 12 yields the identical set of log events as an independent reconstruction, and
**542** — which is `sum(llm_calls_used)` over `measurements/jobs.jsonl`, matching per job in 20 of 20
jobs. 542 is what `CallBudget` counts ("every request counts, retries included"), so it is the figure
this record should have carried.

| Figure | As published | Corrected |
|---|---:|---:|
| Requests sent, 20 jobs | 554 | **542** |
| Observed rate | 1.80 req/min | **1.76 req/min** |
| Utilisation of `LLM_RPM_LIMIT` | 4.5% | **4.4%** |
| Extraction requests | 254 | **252** |
| Passes sending all three extraction requests | 73 of 89 | **75 of 89** |
| Projected rate once extraction overlaps | ~5.4 req/min | **~5.3 req/min** |

**Unchanged, because the 12 duplicates carry about 0.001s each:** 17,404s inside an LLM request
(94.3%), 7,504s extraction (40.7%), 743s at the tool boundary (4.0%), extraction p50 26.7s and p90
54.3s, 89 subtopic passes, the entire projection table below, and every figure in the published n=20
baseline — p50 13m48s, p95 22m01s, 16 of 20 approved, and the token and cost rows, none of which are
derived from the log at all.

This was a measurement-method error in a throwaway analysis script, **not an implementation defect**:
`llm_client._send` spends exactly one call per request sent, and `measurements/jobs.jsonl` has
carried 542 since the run finished. The per-request telemetry added after this ADR removes the whole
class of error — one `CallRecord` is emitted by the code that sends the request, so a count no longer
has to be pattern-matched out of a log (`gl §14`).

**Why this record was edited rather than superseded.** `adr/README.md` says a record is never edited
to reflect a later change of mind. This is not one: the decision, the reasoning, the alternatives and
every conclusion stand exactly as accepted. It is arithmetic in a measurement the record *reports*,
and four other documents quote those numbers from here — leaving a figure known to be wrong in the
source would spread the error rather than record it. The original values are preserved in the table
above, so nothing is lost.

---

## Context

Step 12's baseline missed both latency targets: p50 **13m48s** against a 6-minute target, p95
**22m01s** against 15 minutes. The investigation that followed reconstructed per-request timing for
all **542 requests** the 20 jobs made, from `measurements/run.log`, and attributed each to its
calling node. The reconstruction accounts for every request inside the 20 job segments, no fast-tier
call exceeds its 30s bound, and the resulting node shares match the harness's own `node_seconds` to
within 2 points — so it is describing the same run the published baseline describes.

### Where the time actually goes

| | Seconds | Share of job wall clock |
|---|---|---|
| Total job wall clock, 20 jobs | 18,460 | 100% |
| **Inside an LLM request** | **17,404** | **94.3%** |
| Researcher node total | 8,349 | 45.2% |
| — of which, extraction requests | 7,504 | 40.7% |
| — of which, search + robots.txt + page headers + fetch backoff | 743 | 4.0% |

**The tool boundary is not the bottleneck.** Tavily search is 360s (1.9%), robots.txt 192s (1.0%),
page headers and redirects 179s (1.0%). Body download and HTML/PDF parsing are not measurable at
all: extraction from a page served out of the cache — no network, no parse — has p50 31.5s against
26.7s for a freshly fetched one, a difference inside the spread.

So the Researcher's 45% is **90% model time**, and that model time is spent one request at a time.

### The serialisation

`research_subtopic` walked its candidate list in one loop: choose a URL, fetch it, extract from it,
repeat. 252 extraction requests over 89 reconstructed subtopic passes, and **75 of those 89 sent all
three extraction requests** — a 76th read three pages too, but one of its extractions needed a
validation retry, so it sent four — at p50 26.7s and p90 54.3s each.

Meanwhile the job was using almost none of the rate budget it is allowed:

| | Measured |
|---|---|
| Requests sent, 20 jobs | 542 |
| Elapsed | 18,460s |
| Observed rate | **1.76 requests/minute** |
| `LLM_RPM_LIMIT` | 40 |
| Utilisation | **4.4%** |
| Requests in flight at any moment | **1** |

`gl §13` says "a single job can consume a full minute of the entire rate budget, so two concurrent
jobs saturate the development tier." Measured, one job consumes 4.4% of it. The system was not rate
bound, or tool bound, or context bound. It was bound by doing independent work in sequence.

### Why these calls are independent

One extraction call takes one `FetchedPage` and returns `_Extraction` — a list of claim/quote pairs.
It reads no shared state, and everything on the resulting `Finding` that matters for the audit trail
(`url`, `title`, `retrieved_at`, `content_hash`, `truncated`) is copied from the tool result in
Python afterwards, not produced by the model. Two extractions in the same subtopic cannot affect each
other's output.

Choosing and fetching sources is **not** independent: the loop reads and writes the `seen` set that
stops one page being read twice, and that set is what makes the per-job URL dedupe true.

---

## Decision

**The pages of one subtopic are chosen and fetched sequentially, and then extracted from
concurrently, on a pool of `RESEARCHER_CONCURRENCY` (default 3, hard ceiling
`MAX_LLM_CALLS_PER_SUBTOPIC`). Findings are collected in page order, never completion order.**

`research_subtopic` is now two phases:

```text
_pages_to_read(...)   sequential   search -> dedupe -> fetch, deadline checked between sources
_extract(...)         concurrent   one call per page, <= RESEARCHER_CONCURRENCY at once
```

**What stays sequential, stated in full, because "the system is concurrent now" is the wrong summary.**
The graph topology is unchanged; nodes still run one at a time; the Supervisor still visits between
agents; a Researcher visit still handles exactly one subtopic; and jobs still run one at a time.
Inside the Researcher, choosing sources, deduplicating them, and fetching them are still sequential.
**The only thing that overlaps is the set of extraction calls for the pages of a single subtopic** —
at most `MAX_LLM_CALLS_PER_SUBTOPIC` of them, at most `RESEARCHER_CONCURRENCY` at a time.

Three supporting changes:

- **`CallBudget.spend()` is guarded.** One budget is now spent from several threads, and the check
  and the increment have to be one critical section or call 61 gets sent — `MAX_LLM_CALLS_PER_JOB`
  is a hard ceiling (CLAUDE.md invariant 3).
- **`RESEARCHER_CONCURRENCY` is refused outside 1..3 at startup**, not clamped. Above the ceiling
  means whoever set it expected parallelism a subtopic has no work for; below 1 means no extraction
  runs. `config.MAX_RESEARCHER_CONCURRENCY` restates `agents.researcher.MAX_LLM_CALLS_PER_SUBTOPIC`
  because config must not import an agent, and a test asserts the two agree.
- **The test doubles that concurrency reaches are guarded** — `FakeCompletions` and `FakeLLM`. A
  positional script can no longer say "this page fails and that one succeeds", so the tests that
  needed to say it answer from the page in the request instead.

### Verified: the path works

Two real checks were run against the real endpoint and the real web before this record was accepted.
**Neither is a re-baseline** — they establish that the mechanism does what it says, not what the
system's latency now is.

**A controlled A/B/A on one subtopic.** Three runs sharing one `ToolCache`, so all three read the
same three pages and issue the same prompts, compared on the LLM request timestamps so that fetch
time is excluded by construction:

| Run | `RESEARCHER_CONCURRENCY` | Per-request | Sum of requests | Elapsed window | Peak in flight |
|---|---|---|---|---|---|
| 1 | 1 | 48.4 / 47.8 / 45.7s | 141.9s | **141.9s** | **1** |
| 2 | **3** | 19.6 / 42.6 / 43.7s | 106.0s | **43.7s** | **3** |
| 3 | 1 | 33.9 / 42.5 / 34.8s | 111.1s | **111.1s** | **1** |

Run 2's elapsed window equals its longest single request exactly. Its within-run ratio of
`sum ÷ window` is **2.42×**, which is the figure that does not depend on how fast the endpoint
happened to be that minute. Run 3 returning to 1.00× is what rules out endpoint drift as the
explanation.

**One full job**, on a question outside the 20-job set: reached `approved`, 28 requests, **peak 3 in
flight**, five subtopics of three extractions each, Researcher node 234.1s — 46.8s per subtopic pass
against the baseline's 93.8s. The export gate passed with 17 claims, every one cited, and 17 of 17
verdicts supported.

### Expected effect — a projection, not a result

Simulated against the measured per-request latencies, replacing each pass's `sum()` with its `max()`:

| | Baseline | Projected | Change |
|---|---|---|---|
| Extraction time, 20 jobs | 7,504s | 3,908s | **−48%** |
| Total wall clock | 18,460s | 14,865s | **−19%** |
| **p50 job latency** | **829s** | **712s** | **−14%** |
| **p95 job latency** | **1,322s** | **1,082s** | **−18%** |

Every one of the 20 jobs improves, between −6% and −30%. Rate use rises from 1.76 to about 5.3
requests/minute — 13% of the development tier.

**None of that column is a measured system property, and it must not be quoted as one.** It is
arithmetic over the baseline's own per-request latencies, and it becomes a result only when a
re-baseline runs. The two checks above verify the mechanism on one subtopic and one job; they say
nothing about a p50 over twenty.

> **Outcome — the re-baseline ran on 2026-08-14** (n=20, `measurements/jobs.jsonl`, `run.log`): the
> same 20 questions, the same two models, and the same NIM development overrides, with concurrency in
> place.
>
> | | Baseline (08-13) | Projected | Measured (08-14) |
> |---|---|---|---|
> | Total wall clock | 18,460s | 14,865s (−19%) | **14,291s (−23%)** |
> | p50 job latency | 829s | 712s (−14%) | **650s (−22%)** |
> | Slowest approved job | 1,685s | — | **924s (−45%)** |
> | Researcher share of wall clock | 45.2% | — | **33.1%** |
>
> **The projection was conservative on both figures it predicted.** The all-jobs p95 is deliberately
> absent: 1,322s on 08-13 is an approved job and 1,359s on 08-14 is a job that failed after 30 of its
> 60 calls, so the two rank-19 observations are not the same kind of event and the pair must not be
> quoted as a trend. The approved-only row is used instead, and at n=16 nearest-rank that row *is* the
> slowest approved job, which is why it is labelled as one rather than as a p95.
>
> **Two caveats travel with these numbers.** The 08-14 run **recorded no token counts** — they moved
> to LangSmith — so it restates nothing about tokens or cost. And it ran through a **local DNS outage
> that cost three of its twenty jobs**, which is disclosed wherever its figures are cited.
>
> **Neither the decision nor the projection method changes.** This records the result that the
> paragraph above said would settle them.

Neither baseline is a production-default benchmark: both ran under the NIM development overrides,
which [`gl §14 "Measurement context"`](../engineering-guidelines.md#baseline-measurement-context)
sets out in full.

The 6-minute p50 target is still not met: the measured 650s is 10.8 minutes. The remaining gap is
generation speed and the two report-producing calls, which this ADR does not touch.

---

## Alternatives considered

| Option | Why not |
|---|---|
| **Raise `LLM_MAIN_TIMEOUT_S`** | Turns a 550s failure into a ~250s success and leaves p50 where it is. `gl §17` already records the decision not to raise it again on this evidence |
| **Streaming** | The correct fix for the *timeout* failures — it changes the bound from total request duration to inter-token gap, so the 3,061s of discarded generation stops being discarded. It is a larger change touching `_send`, JSON accumulation, and every structured-output caller, and it is deferred to its own ADR |
| **Reduce prompt context** | Input is not the driver. For the Synthesizer, `corr(latency, claims)` = +0.62 against `corr(latency, findings)` = +0.17. And the Fact-Checker's source pages are what a verdict is checked against — removing them makes verification circular |
| **Change Fact-Checker batching** | Trades one 104s call for several shorter ones, against a component that is not the top bottleneck, reversing ARCHITECTURE.md §20 row 10 |
| **Concurrency across subtopics, or across jobs** | A subtopic is a graph node; overlapping nodes is a graph-topology change. Overlapping jobs is what `gl §12`'s worker bound already refuses, and both would need the shared limiter that does not exist yet |
| **Parallelise fetching too** | 4.0% of wall clock, against a loop whose sequential `seen` set is what makes per-job URL dedupe true. The complexity buys nothing measurable |

---

## Consequences

### What gets better

- **Verified:** a subtopic's extraction stops costing the sum of its calls and starts costing the
  longest of them — 2.42× within-run on the A/B/A, 46.8s per subtopic pass against the baseline's
  93.8s on one full job. **Projected, and since measured:** the projection was −19% total wall clock
  and −14% p50; the 2026-08-14 re-baseline returned **−23% and −22%**, beating both. The projected
  −18% p95 is the one figure that cannot be checked this way — the Outcome block above records why
  the all-jobs p95 is not comparable across the two runs.
- Fewer subtopics cut short by `SUBTOPIC_TIMEOUT_S`. Four hit it in the baseline; three sequential
  calls at p50 26.7s already spend 80s of the 120s, and the deadline now stops a subtopic choosing
  more sources rather than stopping it mid-extraction.

### What we accept

- **A job holds up to 3 requests open at once.** Nothing but this number bounds that until the
  shared Redis limiter arrives in Phase 3. At 5.3 requests/minute against a 40 RPM tier the margin
  is large, but it is a margin rather than an enforced limit, and **two concurrent jobs plus this
  change is the combination to avoid** until the limiter exists.
- **More findings reach the Synthesizer**, because fewer subtopics are cut short. Claims are the
  strongest predictor of Synthesizer latency (+0.62), so this change can slightly *increase*
  Synthesizer timeout risk. It is the first thing to read in the re-baseline.
- **Siblings already in flight when one call fails fatally are allowed to finish.** At most
  `MAX_LLM_CALLS_PER_SUBTOPIC` of them, and `budget_exceeded` stops each one before it sends
  anything. A `rate_limited` failure can waste up to two extra calls that the old sequential loop
  would have skipped.
- **The `SUBTOPIC_TIMEOUT_S` bound changed meaning.** It bounded "how long may this subtopic spend
  choosing and reading sources" and still does — but extraction now happens after the loop, so the
  deadline no longer sits between two extraction calls.
- **The lock on `CallBudget` cannot be shown to be necessary by a test on a GIL build.** Measured:
  an unguarded version over-granted in 0 of 20 trials at every switch interval from 5ms to 100ns.
  It is there because CPython does not promise not to switch between the check and the increment,
  and because a free-threaded build removes the thing currently hiding it. Both the code and the
  test say so rather than implying a proof.

### Unchanged

Grounding, claim-to-source traceability, fact-check coverage, the audit trail, the injection
boundary, revision semantics, and the export invariants. Each extraction still carries exactly one
page inside one `as_untrusted_block()`; provenance is still attached in Python from the tool result;
the Supervisor still sees no fetched text; queries still come from the plan and URLs still come from
a search result. The graph topology, `MAX_LLM_CALLS_PER_JOB`, `MAX_LLM_CALLS_PER_SUBTOPIC`,
`MAX_REVISIONS`, `MAX_SUPERVISOR_HOPS`, and every timeout are untouched.

### Revisit when

- ~~The re-baseline lands.~~ **It landed on 2026-08-14 and beat the projection on both figures it
  could check** (Outcome, above), so the model of where the time goes holds. The version of this that
  is still open is a **production-endpoint** run, which would test the same model against hardware
  that is not the NIM development tier.
- The shared Redis limiter arrives (Phase 3). At that point in-job concurrency stops being bounded
  only by this setting, and `LLM_RPM_LIMIT` becomes the real bound.
- More than one worker runs. `RESEARCHER_CONCURRENCY` × workers is the number that matters then, and
  `gl §12`'s worker table is written against a job that held one request open.
