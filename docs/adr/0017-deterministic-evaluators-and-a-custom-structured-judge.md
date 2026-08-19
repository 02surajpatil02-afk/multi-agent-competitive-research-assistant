# ADR 0017 — Deterministic evaluators and a custom structured judge, over produced outputs

- **Status:** **Accepted and built, 2026-08-19** (Phase 4 block A+B). No Phase 3 runtime semantics
  change; the only production file touched is `llm_client.py`, which gains one optional parameter
  that defaults to the request it already sent
- **Date:** 2026-08-19
- **Affects:** `eval/` (new) · `llm_client.py` · `docs/evaluation.md` (new) ·
  `docs/ARCHITECTURE.md` §14, §21 · `docs/engineering-guidelines.md` §15 · `CLAUDE.md`
- **Supersedes:** `docs/engineering-guidelines.md` §15's "LangSmith Evaluation. One tool." for the
  offline dataset and the judges. LangSmith stays what it already is — the tracing layer and the
  join from an eval row to a run tree

---

## Context

Everything this repository measures today is about whether a job *ran*: latency, LLM calls, node
shares, statuses, audit rows, cache hits. `scripts/measure_jobs.py` produces all of it from real
jobs. None of it says whether the research was any good, and a job can execute perfectly and
produce a report nobody should act on.

`docs/engineering-guidelines.md` §15 planned this as **"LangSmith Evaluation. One tool."** — a
dataset of 30–50 questions with expected evidence, five judged dimensions, and a release gate. That
plan was written in Phase 0, before four things were true:

1. **There is no committed corpus of real research outputs.** `measurements/` is gitignored, because
   its rows carry fetched third-party page text and report bodies. The two n=20 runs exist as
   LangSmith traces and local JSONL, and `CLAUDE.md` already records the risk that those traces age
   out. So a dataset cannot be built by labelling outputs this repository ships — it has none.
2. **Half of the five planned dimensions turned out to be countable.** Citation coverage is
   arithmetic and §15 says so; source *structure*, research breadth, duplicate sources, entity
   coverage and claim support are all reads over `Report`, `Finding` and `claims`. Sending those to
   a model buys variance in numbers that are exact.
3. **Running a job to score it costs minutes and real LLM calls.** An eval set that expensive is one
   that does not get run on the prompt change it exists to catch.
4. **CI is green and provider-free** (step 23). Anything that needs a credential to run cannot go in
   it, which is a hard constraint on where the judge can sit.

The question this record answers: *what does offline evaluation actually consist of, what does it
run on, and what does a run produce?*

---

## Decision

### 1. Evaluation reads already-produced outputs. It never runs the graph

`eval/outputs.py` defines one flattened `ResearchOutput` and two loaders: a committed JSON fixture,
and a real job read back through `database/queries.py`'s existing `read_*` statements. Neither
builds a graph, an `LLMClient`, or a tool.

**Why.** Cost decides how often an eval set is run, and how often it is run decides whether it
catches anything. A replay-free evaluation is seconds; a re-running one is twenty minutes per case
and a provider bill. The durable stores already hold everything a metric needs — `jobs.report_json`,
`findings`, `claims`, and the `plan_produced` / `subtopic_researched` audit rows — so this is a
projection of rows that exist, not a second copy of anything.

**Trade-off.** An evaluation can only score what was persisted. Two things are genuinely missing
from a database-loaded output: a verdict's quote (`claims` keeps `supported` and the note, not the
passage), and any report at all for a job that failed before the export gate, because `report_json`
is `NULL` until it passes. Both are recorded on the loader rather than worked around, and the
metrics report "not applicable" rather than zero.

### 2. Deterministic evaluators first, and they carry most of the load

Twelve pure functions in `eval/metrics.py`, each `(output, case) -> MetricResult`. No I/O, no
clock, no model, no configuration.

**Why.** guidelines §15 already states the principle — *"prefer a deterministic check to an LLM
judge wherever one exists"* — and it turned out to reach further than that section assumed. Twelve
metrics is more than the three §15 marks deterministic, because "is every claim traceable to a
source this job retrieved" and "does this report rest on one publisher" are set operations that
were being described as judgement calls.

**Trade-off, stated in the code and not only here.** Three of the twelve are lexical substring
matching — required facts, forbidden claims, entity coverage — and **a lexical match does not prove
semantic correctness.** A fact stated in words the case did not anticipate reads as absent; a
forbidden claim rephrased reads as absent too. They are cheap regression checks, and each metric's
docstring names the judge dimension that actually answers the question.

### 3. One custom structured judge, through the existing LLM client, optional and off by default

`eval/judge.py` scores five dimensions a count cannot reach — relevance, faithfulness,
completeness, synthesis quality, contradiction handling — on 1–5, through
`LLMClient.call_structured`, at temperature 0.0, behind `--judge`.

**Why through `LLMClient`.** It already owns the structured-output contract, the one validation
retry, the 429 and transport schedules from guidelines §17, and the call budget. A judge with its
own HTTP client would be a second place for retry policy to live, which is the thing
`llm_client.py` exists to prevent. The provider is a base URL and a model id, exactly as it is for
every other caller — there is no provider class here for the same reason there is none there
(ARCHITECTURE.md §20 row 4).

**Why off by default.** `pytest` and CI must stay provider-free. The default `python -m eval.run`
makes no network call and needs no credential; the judge is the only flag that costs money.

**Why its dimensions are not the reflection rubric's five names.** Sharing one vocabulary between
the inline gate and the offline measurement is a stated goal (guidelines §6, §15), and it is
satisfied where the two measure the same thing — but three of reflection's dimensions are now
measured *deterministically* here. Reusing those names for an LLM opinion would make one report
claim two different measurements of one thing.

**Trade-off.** The rubric is **uncalibrated**, exactly as the reflection rubric is until the Phase 4
hand-scoring pass (step 27). A judge score today is a relative signal between runs of the same
rubric version, and `JUDGE_RUBRIC_VERSION` travels with every score so two versions are never
silently compared.

### 4. Neither RAGAS nor DeepEval

Neither is a dependency of this project and neither is added.

**Why.** guidelines §20's rule is that a pin must name the requirement it serves. Between the twelve
deterministic metrics and the judge, there is no capability left that either library supplies: RAGAS
targets retrieval-augmented generation, where the unit is a retrieved chunk and a ground-truth
answer — this system's unit is a `Finding` with a URL and a verbatim quote, and its ground truth is
"which source says this", which the audit trail already answers exactly. DeepEval is a
pytest-shaped assertion layer over LLM judges, and guidelines §15 already deferred it explicitly:
*"add it only if a concrete need for offline pytest-based evaluation inside CI appears — and that
decision gets its own ADR."* No such need has appeared, because the judge is deliberately **not** in
CI. Adding either would also drag in a second prompt library, a second retry policy and a second
set of model calls a test could accidentally make.

**Trade-off.** Every metric here is one this repository has to maintain, and there is no external
benchmark to compare a number against. That is accepted: twelve small pure functions with named
limitations are cheaper to explain and to debug than a framework whose scores are computed
somewhere else.

### 5. No opaque overall score, and no threshold anywhere in block A+B

The report carries twelve deterministic metrics and five judge dimensions as seventeen separate
numbers. Nothing blends them. The runner exits `0` whatever the scores are.

**Why no blend.** "Quality fell 0.4" names nothing anyone can act on, and averaging a rate, a share
and a 1–5 opinion produces a number whose units do not exist. A regression has to name itself.

**Why no threshold yet.** A gate needs a distribution to be calibrated against, and there is not one
yet. Picking numbers now would mean guessing, and a guessed gate is worse than none: it either
blocks correct changes or passes everything and is quietly ignored. Block C sets thresholds against
the baseline this produces, which is the same order step 27 already imposes on the reflection
rubric.

**What `passed` does mean.** A metric's pass rule comes from a field on the case, or from an
invariant this repository already states — `claim_citation_coverage` passes only at 1.0 because
CLAUDE.md invariant 1 makes citation a hard export gate. There is no third source, and a metric with
neither reports `passed=None` rather than a default that looks like a judgement.

### 6. The DEV benchmark is fixture-backed, and it says so

Twenty-six cases in `eval/benchmarks/dev.json`, each pointing at a committed output in
`eval/fixtures/outputs/`. Twenty-three carry questions from the twenty in
`scripts/measure_jobs.py`; three are authored contract cases. **No case asserts an external fact
about a real company.** The fixtures cite `example.com`, and a `required_fact` is a statement about
the fixture's own text.

**Why not real research outputs.** There are none to commit, for the reason `measurements/` is
gitignored: they carry third-party page text. Fabricating company facts to fill the gap would
produce a benchmark that measures agreement with an invention.

**Why eight cases are deliberately defective.** A benchmark that scores 1.0 everywhere gives Block C
no distribution to calibrate against, and gives the twelve detectors nothing to detect. Each of the
eight is tagged `known-defect` and says in `notes` what is wrong with it, and a test asserts that
the set of failing cases is exactly the set of labelled ones.

**Trade-off, and it is the honest limitation of this whole block.** The DEV benchmark exercises the
evaluators end to end and pins the contract. **It does not yet measure this system's research
quality**, because nothing in it came out of a real job. Making it do so needs a run whose outputs
can be committed — which means a decision about publishing report bodies that Phase 4's later steps
own.

### 7. HOLDOUT is not created

One split, `dev`. `BenchmarkSplit` is a one-value literal and adding a second is one literal and one
directory.

**Why.** A held-out set defends against overfitting to a set that has produced a baseline worth
defending. This one has produced nothing yet, and no threshold reads it. Creating it now would mean
two datasets with no measurement behind either, and twice the fixtures to keep honest.

---

## Consequences

- `python -m eval.run` is a fourth thing a person runs, beside `pytest`, `check_model.py` and
  `measure_jobs.py`. It writes to `measurements/eval/`, which is already gitignored, because
  per-case details quote report text derived from third-party pages.
- **`llm_client.py` gains `temperature`**, defaulting to the SDK's `omit`. Every agent's request is
  byte-for-byte the one it sent before, asserted by a test. The judge is the only caller that passes
  it.
- **`eval/` is excluded from the container image** (`.dockerignore`, `NOT_IN_THE_IMAGE`). No
  production process runs an evaluation, and the fixtures are report bodies.
- The trace linkage guidelines §15 asks for exists in one direction and is honest about the other:
  every eval result carries `job_id` and `thread_id`, which are the same string by construction and
  are the LangSmith join key. There is **no LangSmith run id** on an eval row, because nothing in
  this repository records one — that would be new instrumentation, and step 24 owns it.
- Nothing in `eval/` is imported by any production module, and no graph node, route or worker path
  changed.

## What would reopen this

- **A judge score that disagrees with a human by more than a point**, once step 27's hand-scoring
  pass runs. That changes the rubric and bumps `JUDGE_RUBRIC_VERSION`; it does not change this
  record's shape.
- **A capability RAGAS or DeepEval genuinely supplies** that these twelve metrics and this judge do
  not. Decision 4 is a statement about today's requirements, not a permanent refusal.
- **A committed corpus of real outputs.** That is what would turn decision 6's trade-off from a
  limitation into history, and it needs a decision about publishing report bodies.
