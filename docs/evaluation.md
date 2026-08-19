# Evaluation — the offline quality measurement

> **Status: built, 2026-08-19 — Phase 4 blocks A+B and C.** `eval/` exists, the DEV benchmark
> exists, the twelve deterministic metrics and the optional judge run, `python -m eval.run` produces
> a JSON report, and `python -m eval.gate` fails CI when the evaluation framework or the committed
> benchmark contract regresses.
>
> **What that gate is, precisely: a regression contract over committed fixtures. Not a quality
> threshold.** There is no percentage in it, no metric score is gated, and no judge score is gated.
> §14 is the full list of what does and does not fail a build, and §15 says what evidence would be
> needed before a semantic-quality threshold could exist at all.
>
> **Still deliberately not built: a HOLDOUT split, Prometheus/Grafana, and any judge threshold.**
> Sections 16, 18 and 14 say why.
>
> The decisions behind all of it are
> [ADR 0017](adr/0017-deterministic-evaluators-and-a-custom-structured-judge.md) (the engine) and
> [ADR 0018](adr/0018-the-ci-evaluation-gate-protects-the-contract-not-the-quality.md) (the gate).
> This document is how to use what those records decided.

`docs/engineering-guidelines.md` §15 is the requirement; ADR 0017 and ADR 0018 record where the
implementation departs from it and why. Where they disagree, the ADRs are later and win.

---

## 1. What observability already existed before Phase 4

The audit in ADR 0017's Context found a system that measures execution thoroughly and quality not at
all. Nothing below was rebuilt, and **no telemetry was duplicated**.

| Signal | Where it already lives | Reused by evaluation as |
|---|---|---|
| Job identity | `jobs.job_id`, which is also the checkpointer `thread_id` (`graph/state.run_config()`) | `RunMetadata.job_id` / `.thread_id` — the join key into the database and into LangSmith |
| Job outcome | `jobs.status`, and the `job_finished` audit row's `{status, failure_reason}` ([ADR 0009](adr/0009-recovering-an-export-that-failed-after-approval.md) decision 5) | `terminal_success`, and `failure_reason` reported unscored |
| Timing | `jobs.created_at` / `jobs.completed_at` | `RunMetadata.latency_seconds`, derived |
| Budget | `jobs.llm_calls_used`, `jobs.revision_count` | `run_statistics`, reported unscored |
| Quality flag | `jobs.quality_flag` (`below_threshold` / `unscored` / `NULL`) | carried into `RunMetadata` |
| Evidence | `findings` — claim, verbatim evidence, URL, `retrieved_at`, `content_hash`, `truncated` | `source_diversity`, `duplicate_source_absence`, the judge's evidence block |
| The report | `jobs.report_json`, written by the export node | every report-dependent metric |
| Claim verification | `claims.supported`, `claims.verdict_note` | `claim_support_rate` |
| Claim grounding | `claim_sources`, and `Report.sources` in the body | `claim_citation_coverage` |
| Research breadth | `plan_produced` and `subtopic_researched` audit rows | `research_coverage` |
| Per-request detail | LangSmith spans — model, latency, provider token usage, exception (`wrap_openai` in `llm_client.py`) | **not read.** Deliberate: see §13 |
| Metrics endpoint / Prometheus | **nothing.** No `/metrics` route, no client library, no scrape config | nothing — §18 records what is recommended instead |
| Per-job cost and node shares | `scripts/measure_jobs.py` and `measurements/jobs.jsonl` | **not read.** A separate harness with a separate question |
| Health | `/health`'s `checks.postgres` / `checks.redis` / `checks.checkpoints` | not relevant to a finished job |

**Gaps found, and what was done about each.**

| Gap | Decision |
|---|---|
| **Nothing measures research quality.** Every signal above is about execution | This whole subsystem. It is the gap Phase 4 exists for |
| **No committed corpus of real research outputs.** `measurements/` is gitignored because its rows carry third-party page text; the two n=20 runs live in LangSmith and in local JSONL | The DEV benchmark is **fixture-backed** (§5), and ADR 0017 decision 6 records this as the block's honest limitation |
| **No standardized trace metadata.** Nothing sets `job_id`, `agent`, `model` or `revision` under those names on a LangSmith run (guidelines §14 already says so) | **Not fixed here.** That is step 24, and evaluation was built so it does not depend on it: linkage is by `job_id`, which is the `thread_id` LangGraph already sets |
| **No structured JSON logging.** `worker.py` uses `logging.basicConfig` | **Not fixed here.** Step 25, and unrelated to offline evaluation |
| **`jobs` does not record which model ran a job** | `RunMetadata.model` is `None` from the database loader, and says so. Adding a column would be new telemetry for a fact the LangSmith spans already hold (guidelines §14's no-duplication rule) |

**What was added in block A+B:** `eval/` and one optional parameter on `LLMClient.call_structured`.
**What block C added on top:** `eval/gate.py`, one `expect_failing_metrics` field, and two report
fields — `evaluator_version` and `duration_seconds` (§11).
Nothing else, and no new telemetry surface.

---

## 2. The evaluation metadata contract

`eval/outputs.RunMetadata`. Deliberately short: every field is one this repository can populate from
something it already writes.

| Field | Source | Populated from a fixture | From the database |
|---|---|---|---|
| `job_id` | `jobs.job_id` | yes | yes |
| `thread_id` | the same string — `run_config()` sets `thread_id = job_id` | yes | yes |
| `model` | — | if the fixture states one | **no** — no column holds it |
| `status` | `jobs.status` | yes | yes |
| `failure_reason` | the `job_finished` audit row | yes | yes |
| `started_at` / `completed_at` | `jobs.created_at` / `jobs.completed_at` | yes | yes |
| `latency_seconds` | recorded, or derived from the two timestamps | yes | yes |
| `llm_calls_used`, `revision_count`, `quality_flag` | `jobs` | yes | yes |
| `source` | `fixture` or `database` | yes | yes |

The case's own identity — `case_id`, `split`, `category`, `difficulty`, `provenance`, `tags` — is on
the `CaseResult`, not here, because it belongs to the benchmark rather than to the run.

**Deliberately absent, and why.**

- **`agent` / `graph_node`.** Those exist per *span*, not per job. A finished job has no single node.
- **`provider`.** The endpoint is a URL; nothing in this system labels it with a vendor name, and
  inventing one would be a field only this document knew the meaning of.
- **A LangSmith run id.** Nothing in this repository records one. Adding it is step 24's.
- **Any user, account or team identity.** The application is single tenant and does not own that data.

---

## 3. The benchmark case schema

`eval/schema.EvalCase`, a Pydantic model with `extra: forbid` — an unknown key is a typo, and a typo
in a benchmark is an expectation that silently stops being checked.

| Field | Meaning |
|---|---|
| `case_id` | stable, lowercase-kebab, unique in the file |
| `split` | `dev`. **`holdout` does not exist** — §16 |
| `question` | the research question, as it would be submitted |
| `category` | one of ten (below) |
| `difficulty` | `easy` / `medium` / `hard` — how hard for the *system* |
| `provenance` | `repository_fixture` or `synthetic_contract` — §5 |
| `output_ref` | path to the output this case scores, relative to the benchmark file |
| `job_id` | the real job, when there is one. `null` for every DEV case. What `--from-database` loads by |
| `expected_status` | required, never defaulted — a case about insufficient evidence expects `failed` |
| `required_entities` | companies or products the answer must name |
| `required_facts` | `{id, any_of: [...]}` — one fact, several accepted phrasings |
| `forbidden_claims` | phrases the report must not contain |
| `min_claims`, `min_sources`, `min_distinct_domains`, `max_unsupported_claims` | the numeric expectations, all optional |
| `expect_all_subtopics_researched` | `true`, `false` (a gap is expected and must still be visible), or unset |
| `entity_scope`, `temporal_scope` | reported, not scored — §12 says why `temporal_scope` cannot be |
| `expect_failing_metrics` | **the regression contract**: exactly which metrics this committed output is expected to fail. Empty for every healthy case. Read by the gate and by no metric — §14 |
| `tags`, `notes` | slicing, and why the case exists |

**Every pass rule a metric applies comes from a field here, or from an invariant this repository
already states.** There is no third source, and there are no hidden constants in `eval/metrics.py`.

---

## 4. The categories

`factual_extraction` · `multi_source_synthesis` · `company_comparison` · `contradiction_handling` ·
`citation_grounding` · `insufficient_evidence` · `research_coverage` · `duplicate_sources` ·
`entity_coverage` · `source_diversity`.

**"Human-review output behaviour" is deliberately not a category.** No output this package can load
carries a gate payload — `reviewer_payload()` is built from the live checkpoint, and neither
`jobs.report_json` nor a fixture holds it — so a category for it would be a label with no evaluator
behind it.

---

## 5. The DEV benchmark

`eval/benchmarks/dev.json` (version `dev-1`) and `eval/fixtures/outputs/*.json`. **26 cases, 24
fixture outputs** — two cases share one output, which is why cases and outputs are separate files.

| | Count |
|---|---|
| Cases | 26 |
| `repository_fixture` — question from `scripts/measure_jobs.py`'s twenty | 23 |
| `synthetic_contract` — question and output both authored | 3 |
| Tagged `healthy` | 18 |
| Tagged `known-defect` | 8 |
| Difficulties | 3 easy · 11 medium · 12 hard |

By category: 4 `company_comparison` · 4 `multi_source_synthesis` · 4 `citation_grounding` ·
3 `factual_extraction` · 2 each `entity_coverage`, `source_diversity`, `contradiction_handling`,
`research_coverage`, `insufficient_evidence` · 1 `duplicate_sources`.

### What the DEV benchmark is not

**No case asserts an external fact about a real company.** Twenty-three questions are real — they
are the shapes this system is for, taken from the committed measurement set — but every *output* is
an authored fixture citing `example.com`, `example.org` and `example.net`. A `required_fact` is
therefore a statement about the fixture's own text, never about the world.

That is a deliberate refusal, not an oversight. There is no committed corpus of real research
outputs to label, for the same reason `measurements/` is gitignored: those rows carry fetched
third-party page text. Fabricating company facts to fill the gap would produce a benchmark that
measures agreement with an invention.

**So this benchmark exercises the evaluators end to end and pins the contract. It does not yet
measure this system's research quality.** Making it do so needs a run whose outputs can be
committed, which is a decision about publishing report bodies that Phase 4's later steps own.

### Why eight cases are deliberately defective

A benchmark that scores 1.0 everywhere gives Block C no distribution to calibrate against, and gives
the twelve detectors nothing to detect. Each defective case is tagged `known-defect` and says in
`notes` what is wrong with it, and `tests/test_eval_runner.py` asserts that the set of failing cases
is **exactly** the set of labelled ones — so a real regression cannot hide among the intended
failures.

| Case | What is wrong with its fixture | Which metric catches it |
|---|---|---|
| `cmp-datadog-newrelic-half` | a comparison that never names the second company | `expected_entity_coverage` |
| `cit-elastic-unknown-finding` | a claim citing a finding id the job never retrieved (ADR 0003's failure mode) | `claim_citation_coverage`, `claim_support_rate` |
| `cit-unity-invalid-report` | a stored report body that violates `schemas.Report` | `structured_output_validity`, `citation_presence` |
| `con-notion-confluence-overclaim` | asserts certainty its evidence does not carry | `forbidden_claim_absence` |
| `dup-salesforce-hubspot` | one URL cited twice as two sources | `duplicate_source_absence` |
| `div-single-host` | every source from one host | `source_diversity` |
| `ins-hashicorp-thin` | two of three subtopics found nothing; one claim survives | `minimum_useful_output` |
| `sup-okta-unsupported` | every claim cited, two of four unsupported by the Fact-Checker | `claim_support_rate` |

The last one is the pair worth reading together: **coverage and support are different properties**,
which is exactly what guidelines §15 warns about — a report can reach 100% citation coverage with
wrong citations.

---

## 6. The deterministic evaluators

Twelve pure functions in `eval/metrics.py`. Every one returns
`MetricResult{metric, score, passed, explanation, details}`.

**Three conventions apply to all twelve.** `score` is `0.0–1.0` and higher is better, so metrics
that are naturally a rate of badness are reported as their complement. `score=None` means *"this
case says nothing about this"* — not zero, not one, and excluded from every aggregate. `passed=None`
means there was no rule to apply.

| # | Metric | Definition | Inputs | Pass rule | Limitation |
|---|---|---|---|---|---|
| 1 | `terminal_success` | did the job end where the case expects | `status`, `expected_status` | score is 1.0 | says nothing about *why* a job ended there |
| 2 | `structured_output_validity` | did the stored report validate against `schemas.Report` | `schema_errors` from the loader | score is 1.0 | a job with no report scores 1.0 — it emitted nothing invalid |
| 3 | `citation_presence` | is there a report with at least one claim and one source | `report` | score is 1.0. **Not applicable when the case expects a non-approved status** — a job expected to fail has no report by design | binary; says nothing about quality |
| 4 | `claim_citation_coverage` | share of claims reaching a source URL through their `finding_ids` | `report.claims`, `report.sources` | **1.0 and nothing less** — CLAUDE.md invariant 1 | structural only: proves a claim *has* a source, never that the source supports it |
| 5 | `claim_support_rate` | share of *checked* claims the Fact-Checker supported | `verdicts` | `max_unsupported_claims`, else `None` | reports the Fact-Checker's own opinion; not independent verification. Unchecked claims are excluded from the denominator, and counted in `details` |
| 6 | `required_fact_coverage` | share of required facts appearing in the report text | `required_facts`, report text | every fact matched | **lexical substring matching. Proves nothing semantic** — see §7 |
| 7 | `expected_entity_coverage` | share of required entities named | `required_entities`, report text | every entity named | same lexical limitation |
| 8 | `forbidden_claim_absence` | 1 − share of forbidden phrases present | `forbidden_claims`, report text | none present | same lexical limitation; cannot tell an assertion from a quotation it refutes |
| 9 | `research_coverage` | share of planned subtopics not `unresearched` | `planned_subtopics`, `subtopic_status` | `expect_all_subtopics_researched`, else `None` | breadth, not depth: one thin finding counts as `done` |
| 10 | `source_diversity` | distinct hosts ÷ cited sources | `report.sources` | `min_distinct_domains`, else `None` | **host, not registrable domain** — `ir.example.com` and `www.example.com` count as two. Fixing that needs a public-suffix dependency |
| 11 | `duplicate_source_absence` | 1 − extra source entries sharing a normalised URL | `report.sources`, `findings` | no duplicates | trailing slash is normalised away, a query string is not. The same page fetched twice is reported in `details` and **not** scored |
| 12 | `minimum_useful_output` | share of three checks: enough claims, section bodies non-empty, claim texts non-empty | `report`, `min_claims` (default 1) | all three | length is not quality |

**Latency, retries and failure statistics are reported and never scored.** They appear under
`run_statistics` in the report — p50/p95 latency, `llm_calls_used`, revision counts, failure reasons.
Scoring them here would duplicate the latency targets guidelines §14 already owns, which is the rule
that section states about duplicate telemetry.

---

## 7. Deterministic versus judge — who answers what

| Question | Answered by | Why there |
|---|---|---|
| Did every claim reach a source? | deterministic (#4) | arithmetic, and an export invariant |
| How many distinct publishers? | deterministic (#10) | a set operation |
| Did it name the companies asked about? | deterministic (#7), **lexically** | cheap; the judge's `relevance` is the real answer |
| Did it find what an analyst would find? | **judge — `completeness`** | needs judgement about a domain, not a string search |
| Does each cited source actually support its claim? | **judge — `faithfulness`**, with `claim_support_rate` (#5) as the Fact-Checker's own regression check | the deterministic form can only see whether a verdict exists |
| Is this synthesis or a stack of sourced sentences? | **judge — `synthesis_quality`** | no count reaches it |
| What did it do where sources disagree? | **judge — `contradiction_handling`**, with `forbidden_claim_absence` (#8) catching one named wrong answer | the deterministic form only sees the phrases the case anticipated |

The rule from guidelines §15 stands: **prefer a deterministic check wherever one exists.** Spending a
judge call on coverage adds variance to a number that should be exact.

---

## 8. The judge rubric

`eval/judge.py`, version **`eval-judge-v1`**. Five dimensions, whole numbers **1–5**, plus one
`explanation`. The version travels with every score, because two rubric versions are two different
measurements.

| Dimension | 1 | 3 | 5 |
|---|---|---|---|
| `relevance` | answers a different question | partly on-target | answers exactly what was asked |
| `faithfulness` | contradicts its evidence | one clearly unsupported statement | every statement traceable to the evidence shown |
| `completeness` | near-empty | the obvious aspects | covers what the question needs, **and names what it could not find** |
| `synthesis_quality` | disconnected fragments | related within sections | draws conclusions the individual sources do not state |
| `contradiction_handling` | asserts one side as fact | mentions the disagreement in passing | states both positions and explains which is better supported |

Values 2 and 4 are defined in the prompt itself (`_SYSTEM`); the table above shows the anchors.
`contradiction_handling` scores 5 when the evidence shows no disagreement at all — there was nothing
to mishandle.

**The prompt tells the judge not to reward length, confident tone, or citation count**, because
citation count is already measured exactly and a judge that re-scored it would put one number in the
report twice.

**The rubric is uncalibrated**, exactly as the reflection rubric is until step 27's hand-scoring
pass. A judge score is a relative signal between runs of the same rubric version.

### How the judge works

- **Through `LLMClient.call_structured`** — the same structured-output contract, the same one
  validation retry, the same guidelines §17 schedules, the same `CallBudget`. No second HTTP client
  and no second retry policy.
- **Temperature 0.0**, so two runs of one rubric over one report are comparable.
- **The report and its evidence go in as untrusted content**, through `as_untrusted_block()` — the
  same treatment the reflection node gives the same text (guidelines §8).
- **Bounded at `JUDGE_MAX_REQUESTS_PER_CASE = 6`** — one logical call is at most two requests, each
  retriable twice.
- **A failure is a result, not a crash.** `JudgeOutcome` carries either a verdict or an error string,
  so one unreachable endpoint costs one case its five dimensions and nothing else.
- **It is not a sixth agent** (CLAUDE.md invariant 8): no tools, no state, no place in the graph, and
  no production module imports it.

---

## 9. Configuring the judge

The provider is a base URL and a model id, exactly as it is for every other caller in this
repository — there is no provider class, for the same reason `llm_client.py` has none.

| Setting | Flag | Environment fallback | Then |
|---|---|---|---|
| Model | `--judge-model` | `EVAL_JUDGE_MODEL` | `LLM_MODEL` |
| Endpoint | `--judge-base-url` | `EVAL_JUDGE_BASE_URL` | `LLM_BASE_URL` |
| Credential | — | `LLM_API_KEY` | required, loudly, at startup |

Defaulting to the `LLM_*` values means judging with the model the system runs on needs no new
variable. The overrides exist because **a model grading its own output is the obvious way to get a
flattering number**, so choosing a different judge has to be one flag.

`_judge_config()` builds the client with `dataclasses.replace(config, ...)` rather than a second
configuration type, so the timeout, the tracing flag and the project stay the process's own.

---

## 10. Running it

Deterministic only — **no credential, no network, no cost**:

```bash
python -m eval.run
```

With a CSV beside the JSON, and only the cases carrying a tag:

```bash
python -m eval.run --csv --tag known-defect
```

With the judge — **the only flag that costs money**:

```bash
python -m eval.run --judge --judge-model <model-id> --judge-base-url https://integrate.api.nvidia.com/v1
```

Against real jobs instead of the fixtures. Cases with no `job_id` are **skipped**, with the reason:

```bash
python -m eval.run --from-database postgresql://research:research@localhost:5432/research
```

| Option | Default | What it does |
|---|---|---|
| `--benchmark` | `eval/benchmarks/dev.json` | which benchmark file to load |
| `--split` | every case in the file | filter by split |
| `--case` / `--tag` | none | repeatable filters, composed with AND |
| `--out` | `measurements/eval` | where the report is written. **Gitignored** — per-case details quote report text derived from third-party pages, the same reason `measurements/` is |
| `--csv` | off | also write the flat CSV |
| `--from-database` | off | load outputs from a real database instead of the fixtures |
| `--judge` | **off** | run the judge as well |
| `--judge-model` / `--judge-base-url` | §9 | which judge |
| `--require-judge-scores` | off | exit 1 when the judge was enabled and scored **nothing at all**. A provider-health switch, never a score threshold — §14 |
| `--run-id` | derived from the clock | name the run |

**Exit codes.** `0` means the evaluation ran, whatever the scores were. `1` means it could not run:
an unreadable benchmark file, a selection matching no cases, a judge asked for with no model or
endpoint configured, or `--require-judge-scores` with a judge that answered nothing. **A failing
metric never changes the exit code** — §14.

### Checking the regression contract

The runner reports. The **gate** is what fails a build, and it is a separate command reading the
report the runner wrote:

```bash
python -m eval.gate measurements/eval/ci.json
```

Those two commands, in that order, are exactly what the `eval` CI job runs. Producing the evidence
and judging it are kept apart deliberately, so the judgement cannot quietly re-decide the
measurement — and so the report survives as an artifact whatever the verdict.

| Exit | Meaning |
|---|---|
| `0` | the contract is intact |
| `1` | **a contract violation** — read the violations and either fix the code or update the benchmark deliberately |
| `2` | **nothing judgeable** — no report, unreadable JSON, or a `--from-database` report, which this contract is not defined over |

`1` and `2` are separate because a build that cannot tell an infrastructure fault from a contract
violation will eventually "fix" the former by editing a benchmark.

---

## 11. What a run produces

### The JSON report — `measurements/eval/<run-id>.json`

```text
run_id, started_at, finished_at, duration_seconds
evaluator_version                              # which ruler produced these numbers
benchmark        { path, version, split }
selection        { cases, tags, input_mode }
judge            { enabled, model, base_url, rubric_version, dimensions }
counts           { total, evaluated, failed, skipped, errored, benchmark_problems }
metric_aggregates{ <metric>: { cases, scored, not_applicable, mean, min, max,
                               passed, failed, no_pass_rule } }
judge_aggregates { attempted, scored, errored, errors[], dimensions{ <dim>: {mean,min,max} } }
run_statistics   { latency_seconds{n,p50,p95,max}, llm_calls_used{n,p50,max},
                   revision_count, failure_reasons, output_sources }
benchmark_problems[ { case_id, problem } ]
cases[           { case_id, split, status, question, category, difficulty, provenance,
                   tags, output_ref, error,
                   failed_metrics[], expect_failing_metrics[],   # what the gate compares
                   metrics[ {metric, score, passed, explanation, details} ],
                   judge{ model, rubric_version, scores, explanation, error },
                   run_metadata{ ...§2... } } ]
```

**Every raw component metric survives into `cases[].metrics[]`, with its `details`.** An aggregate is
never the only record of a number.

**`evaluator_version` is the field to look at first when two reports disagree.** It names the
deterministic metric set (`det-v1` today) and answers the question that otherwise gets answered
wrongly: *did the system change, or did the ruler?* A metric whose definition was tightened looks
exactly like a regression in the thing it measures. It is bumped whenever a metric is added, removed,
renamed, or has its scoring or pass rule changed — the same discipline `JUDGE_RUBRIC_VERSION` already
asks for the judge.

### The CSV — `--csv`

One row per case per metric: `run_id, case_id, split, category, difficulty, provenance, case_status,
metric, score, passed, explanation`. Long rather than wide, so a new metric adds rows rather than
changing every consumer's columns. It deliberately omits `details`, which are nested; the JSON is the
complete record.

### The terminal summary

The counts, one line per metric (mean, scored, not-applicable, pass, fail), the judge dimensions when
enabled, the cases that did not run, the cases with a failing metric, and a closing line saying no
threshold was applied.

### Aggregates, and the one that does not exist

`mean` is over the cases where the metric applied; a metric nothing scored reports `null` rather than
`0.0`. Percentiles are **nearest-rank**, so every one is a real observation — the same choice
`scripts/measure_jobs.py` makes.

**There is no overall quality score, and adding one is a decision that needs an ADR.** Twelve
deterministic metrics and five judge dimensions stay seventeen numbers, because "quality fell 0.4"
names nothing anyone can act on, and averaging a rate, a share and a 1–5 opinion produces a number
whose units do not exist. `tests/test_eval_report.py` refuses an `overall` key.

### Case outcomes

| Status | Meaning |
|---|---|
| `evaluated` | metrics ran; every stated pass rule passed |
| `failed` | metrics ran; at least one stated rule was broken. **A result, not an error** |
| `skipped` | deliberately not evaluated in this mode, with a reason |
| `errored` | could not be evaluated — an output that would not load |

Rows that never parsed into a case are counted separately, as `benchmark_problems`.

**One bad case never ends a run.** Loading, metric evaluation and judging are each isolated per case,
and `tests/test_eval_runner.py` drives all three failure shapes.

---

## 12. Trace and job linkage

Every eval result carries `run_metadata.job_id` and `run_metadata.thread_id`, and **they are the same
string by construction** — `graph.state.run_config()` sets `thread_id = job_id`, which is what
LangSmith records on every run in a job's trace.

So: from an eval row, the `job_id` opens the database rows and the trace; from a trace, the
`thread_id` finds any eval row that scored that job.

**LangSmith is not required and is never called.** Deterministic evaluation runs entirely offline. If
LangSmith is unavailable, nothing here degrades. What is genuinely missing is a LangSmith **run id**
on an eval row — nothing in this repository records one, and adding it is step 24's job, not this
subsystem's. No second observability platform was created.

---

## 13. Known limitations

1. **The DEV benchmark does not yet measure this system's research quality.** Its outputs are
   authored fixtures. It exercises the evaluators and pins the contract; that is all (§5).
2. **Three metrics are lexical.** Required facts, entities and forbidden claims are case-insensitive
   substring matching, and a correct answer phrased unexpectedly reads as a miss.
3. **The judge rubric is uncalibrated.** Step 27's hand-scoring pass is what would change that.
4. **`source_diversity` counts hosts, not registrable domains.** Two subdomains of one organisation
   count as two publishers.
5. **`claim_support_rate` reports the Fact-Checker's own opinion.** It is a regression check on that
   component, not independent verification. Neither it nor the judge re-fetches a source, so
   guidelines §15's "are the sources reachable?" is **not** measured here at all — that needs an HTTP
   request per source, which this subsystem deliberately does not make.
6. **`temporal_scope` is recorded and not scored.** Checking "in the last 12 months" needs a
   publication date per source, and nothing in an output carries one without re-fetching.
7. **A database-loaded output has no verdict quotes and no model**, because no column holds either.
8. **A failed job has no `report_json`**, so every report-dependent metric reports not-applicable
   rather than zero. That is correct, and it means a benchmark of only failing jobs would produce
   almost no signal.
9. **Judge determinism is bounded by the provider.** Temperature 0.0 is requested; whether an
   endpoint honours it is the endpoint's business.
10. **Per-request telemetry is not read.** Retry counts and token usage live on LangSmith spans, and
    reading them would make deterministic evaluation depend on a service it currently does not need.
11. **The CI gate proves the framework works, not that the research is good.** It catches a broken
    evaluator, a corrupted benchmark and a defect that stopped being detected. It cannot catch a
    system that got worse at research, because the benchmark it runs on contains no real research
    (§14, §15).
12. **The regression contract has to be maintained by hand.** Adding a case means declaring what it
    should fail, and a deliberate change to a metric means editing the benchmark. That friction is
    intended - it turns "a number moved" into "someone stated the new expectation" - but it is
    friction, and a stale contract is the way this gate would rot.

---

## 14. What is and is not gated

Block C answered one question: **which of these seventeen numbers is evidence about this
repository?** The answer was reached by producing the baseline first and reading it, and it is
recorded in full in
[ADR 0018](adr/0018-the-ci-evaluation-gate-protects-the-contract-not-the-quality.md).

### The DEV baseline of 2026-08-19

```text
metric                       mean   scored  n/a  pass  fail
  terminal_success            1.00      26    0    26     0
  structured_output_validity  0.96      26    0    25     1
  citation_presence           0.96      25    1    24     1
  claim_citation_coverage     0.99      24    2    23     1
  claim_support_rate          0.97      24    2    13     2
  required_fact_coverage      1.00       3   23     3     0
  expected_entity_coverage    0.98      23    3    22     1
  forbidden_claim_absence     0.50       2   24     1     1
  research_coverage           0.92      26    0    12     0
  source_diversity            0.83      24    2     8     1
  duplicate_source_absence    0.99      24    2    23     1
  minimum_useful_output       0.99      24    2    23     1

26 cases: 18 evaluated, 8 failed, 0 skipped, 0 errored, 0 benchmark problems
```

**Read that table with §5 in hand, because three things about it disqualify every one of those means
as a threshold.** Every number describes our own authored fixtures. Eight of the twenty-six cases are
deliberately broken, so every mean is dragged by defects that are supposed to be there and moves
whenever a case is added. And four metrics have a scored population of 3, 2, 23 and 24 — a threshold
on `forbidden_claim_absence`'s two observations is noise with a decimal point.

### Metric classification

| Metric | Class | Why |
|---|---|---|
| `terminal_success` | **B — observational** | Exact and cheap, but its value here is a fact about fixture `status` fields we typed. Its *per-case classification* is gated (below); its mean is not |
| `structured_output_validity` | **B — observational** | Same. A report that does not validate is a real defect, and the case carrying one is gated by name — but "96% of fixtures validate" is a statement about our fixtures |
| `citation_presence` | **B — observational** | Same shape |
| `claim_citation_coverage` | **B — observational** | It is the in-report form of an *architectural* invariant (CLAUDE.md invariant 1), which is why it passes only at 1.0 per case. But the export gate in `graph/build.py` is what enforces that invariant on real jobs and `tests/test_graph_build.py` is what proves it — gating the aggregate here would be a second, weaker copy of a check that already exists in the right place |
| `claim_support_rate` | **B — observational** | Reports the Fact-Checker's opinion over authored verdicts |
| `required_fact_coverage` | **C — not currently gatable** | Lexical, and scored on **3 of 26** cases. Semantic correctness is what it gestures at and not what it measures |
| `expected_entity_coverage` | **C — not currently gatable** | Lexical. Same |
| `forbidden_claim_absence` | **C — not currently gatable** | Lexical, and scored on **2 of 26** cases |
| `research_coverage` | **B — observational** | Breadth over fixture subtopic maps |
| `source_diversity` | **C — not currently gatable** | Counts hosts we invented, and counts hosts rather than registrable domains (§13) |
| `duplicate_source_absence` | **B — observational** | Exact, but again over authored source lists |
| `minimum_useful_output` | **B — observational** | Length is not quality |
| **Judge dimensions (5)** | **C — not currently gatable** | The rubric is uncalibrated (ADR 0017 decision 3), and judging fixtures citing `example.com` measures the fixtures |

**No metric is class A on its own value.** What *is* class A — a hard regression invariant — is the
framework and the benchmark contract:

| Hard invariant | Evidence it is safe to gate |
|---|---|
| The benchmark parses, and every row became a case | Exact, binary, and entirely about this repository |
| Every selected case ran — nothing errored, nothing skipped | Same |
| Every registered metric produced a result for every case | Same. Catches an evaluator that was deleted or went silent |
| **Every committed output still fails exactly the metrics it declares** | Exact per case, derived from the baseline above rather than chosen. Eight cases declare one or two metrics; eighteen declare none |

That last row is the design. `EvalCase.expect_failing_metrics` is deliberately redundant with the
case's other expectations, and **the redundancy is the check**: an evaluator that stops catching a
committed defect makes the derived result disagree with the declared one. Both directions matter —
an *unexpected* failure means a healthy fixture regressed or a metric got stricter, and a *missing*
failure means a detector stopped detecting, which looks like an improvement and is the one nothing
else would notice.

### Exactly what fails CI

| Rule | Fails when |
|---|---|
| `benchmark_parses` | a benchmark row no longer validates |
| `cases_selected` | the run evaluated no cases |
| `no_evaluator_errors` | a case's output would not load |
| `no_skipped_cases` | a fixture-backed case did not run |
| `metrics_present` | a registered metric is absent from the report |
| `metrics_ran_on_every_case` | a metric produced results for fewer cases than ran |
| `metrics_registry_matches` | the report carries a metric this build does not know |
| `contract_names_real_metrics` | a case declares a metric that does not exist |
| `no_unexpected_failures` | a case failed a metric it does not declare |
| `declared_failures_still_fail` | a case stopped failing a metric it declares |

### Exactly what does not fail CI

- **Any metric mean, minimum or maximum moving**, in either direction. There is no percentage
  anywhere in `eval/gate.py`, and `tests/test_eval_gate.py` asserts that a mean can move freely.
- **Any judge score, at any value**, and any judge error.
- **Latency, LLM call counts, revision counts, or how long the evaluation itself took.**
- **Adding a healthy case**, or adding a defective one that declares its contract.

### Why judge scores are still not gated

Nothing changed from ADR 0017 decision 3. The rubric is uncalibrated — step 27's hand-scoring pass is
what would change that — and the benchmark it would score is fixture-backed, so a judge number today
describes authored files. A threshold on it would be a number about our own writing, enforced on
everyone else's changes.

The judge therefore runs only from `.github/workflows/eval-judge.yml`, by hand (§19), and its only
failure condition is a **provider-health** one: `--require-judge-scores` exits 1 when the judge was
enabled and scored *nothing at all* — a wrong model id, an expired key, an endpoint that is down. One
scored case satisfies it. How well anything scored is not its business.

---

## 15. What remains before real quality gating

This is the sequence, and none of it is implemented. **Until it is, no percentage threshold and no
judge threshold should be added, whatever a baseline looks like.**

1. **Collect safe, committable real system outputs.** Run `scripts/measure_jobs.py` and decide what
   may be published: `measurements/` is gitignored because its rows carry fetched third-party page
   text, so this is a licensing-and-privacy decision before it is an engineering one. It is the
   blocker, and everything below waits on it.
2. **Replace or augment the authored fixtures** with those outputs, keeping the questions that are
   already real. A mixed benchmark is fine and is probably the honest end state: contract cases keep
   the evaluators sharp, real cases measure the system.
3. **Write grounded expected facts and evidence** from what a competent analyst would find for each
   question — not from what the system happened to produce, which would grade it against itself.
4. **Hand-review a representative subset**, across all three question shapes and all three
   difficulties, and record the reviewer's judgement per case.
5. **Run the deterministic metrics** over the real outputs and inspect the distribution. Expect it to
   look nothing like the fixture baseline above.
6. **Run the judge** over the same set, at the same rubric version.
7. **Compare judge against human** (step 27). Any dimension where they differ by more than a point is
   a rubric defect, not a system defect; fix the rubric and bump `JUDGE_RUBRIC_VERSION`.
8. **Choose thresholds from that distribution** — and prefer "no dimension may drop more than X since
   the last release" over an absolute floor, because a relative rule survives a benchmark that grows.
9. **Create HOLDOUT** once DEV has been tuned against often enough for overfitting to be real.
10. **Only then** may a semantic-quality gate block a release.

**What a grounded benchmark should contain**, when it is built: 30–50 questions across the three
shapes; per case, the entities and facts a competent analyst would expect, the sources that would
count as adequate, and the claims that would be wrong; real committed outputs carrying their
`job_id`, so every score joins back to a database row and a trace; and a stated review date, because
a competitive research answer decays.

---

## 16. Why HOLDOUT is still deferred

Unchanged from block A+B, and Block C did not create one.

A held-out split defends against overfitting to a set that has produced a baseline worth defending.
This one has produced a baseline over authored fixtures, no threshold reads it, and no prompt has
been tuned against it.

Creating it now would mean two datasets with no measurement behind either, and twice the fixtures to
keep honest — which is exactly the "more files to look sophisticated" failure the engineering rules
name.

`BenchmarkSplit` is a one-value literal and `--split` already exists, so adding `holdout` is one
literal and one directory on the day step 9 above is reached.

---

## 17. Why neither RAGAS nor DeepEval

Neither is a dependency of this project, and neither was added.
[ADR 0017](adr/0017-deterministic-evaluators-and-a-custom-structured-judge.md) decision 4 carries the
full argument; in short:

- **RAGAS** targets retrieval-augmented generation, where the unit is a retrieved chunk and a
  ground-truth answer. This system's unit is a `Finding` with a URL and a verbatim quote, and its
  ground truth is *"which source says this"* — which `claim_sources` and the export gate already
  answer exactly.
- **DeepEval** is a pytest-shaped assertion layer over LLM judges. guidelines §15 already deferred it
  explicitly and asked for an ADR before adding it; the concrete need it named — pytest-based
  evaluation **inside CI** — has not appeared, because the judge is deliberately not in CI.
- guidelines §20's rule is that a pin must name the requirement it serves. Neither can, once the
  twelve deterministic metrics and the judge exist.

**The cost of that decision is admitted:** every metric here is one this repository maintains, and
there is no external benchmark to compare a number against.

---

## 18. Runtime metrics — why there is no Prometheus and no Grafana

**Nothing was implemented, and that is a decision rather than an omission**
([ADR 0018](adr/0018-the-ci-evaluation-gate-protects-the-contract-not-the-quality.md) decision 7).

The audit found no metrics surface to extend: `/health` returns three boolean checks
(`postgres`, `redis`, `checkpoints`) and nothing numeric, there is no `/metrics` route, no
`prometheus_client` dependency, and no scrape configuration anywhere in the repository or in
`docker-compose.yml`.

Adding one now means a client library, a route, an exporter, a scrape target, a retention policy and
a dashboard — for a system that is not deployed anywhere, so there is nothing to scrape and nobody
to page. It would also be a **second** answer to *"is the infrastructure healthy?"*, which
`docs/ARCHITECTURE.md` §14 already assigns to CloudWatch, and that section's rule is that a signal
goes in exactly one of the two layers.

**Recommended when Phase 5 gives the system a deployment**, in the order they earn their place.
Each has to be maintainable — a counter nobody increments correctly is worse than no counter,
because it is a number people trust:

| Metric | Where it would be incremented | Why it earns its place |
|---|---|---|
| `jobs_started_total` / `jobs_completed_total` / `jobs_failed_total{reason}` | `worker.py`, beside the `jobs.status` writes that already exist | "how many jobs are actually executing" is the first question in an incident, and today it is a SQL query |
| `job_duration_seconds` | `finalize`, from `created_at`/`completed_at` | guidelines §14 already sets latency targets; this is what compares against them continuously rather than per measurement run |
| `llm_requests_total` / `llm_retries_total{reason}` | `llm_client._send`, where the budget and the retries already are | The 40 RPM tier is the binding constraint on worker count (guidelines §13) |
| `visibility_lease_losses_total` | `worker.py`'s renewal path ([ADR 0015](adr/0015-visibility-leases-replace-static-duration-ownership.md)) | A rising count means jobs are being redelivered mid-node, which is invisible today until the DLQ fills |
| `exports_total{result}` | the export node | ADR 0009's recoverable set — `failed` with a report and no artifact — is currently something an operator has to go and look for |
| `human_review_waits_total` / gate age | the gate node, plus the Phase 5 expiry sweep | Gate expiry does not exist yet (step 32); this is the signal that says whether it is needed |

**Deliberately not recommended:** `eval_runs_total` and `eval_case_errors_total`. Evaluation is an
offline harness run by a person or by CI, not a running service; it already writes a timestamped JSON
report per run and CI keeps that as an artifact. A counter would be a third place answering a
question two already answer exactly.

**And no Grafana dashboard without a real metrics source.** A dashboard over nothing is a screenshot.

---

## 19. Running the judge from CI

`.github/workflows/eval-judge.yml`, `workflow_dispatch` only — no `pull_request`, no `push`, no
`schedule`. It can never become a required check, and `tests/test_eval_ci_workflows.py` asserts that,
along with the fact that `ci.yml` never references it.

| Input | Meaning |
|---|---|
| `model` | the judge model id, required — `EVAL_JUDGE_MODEL` |
| `base_url` | the OpenAI-compatible endpoint, required, defaulting to the documented development one — `EVAL_JUDGE_BASE_URL` |
| `tag` | optional benchmark tag, to judge a slice rather than all 26 |

The credential is the repository secret **`EVAL_JUDGE_API_KEY`**, passed as `LLM_API_KEY`. It is
never echoed: the workflow prints no environment, and the runner logs a model id and an endpoint but
no key. An unset secret arrives as an empty value and `config.required()` fails at startup naming
`LLM_API_KEY`, which is the loud failure this repository uses everywhere else.

**There is deliberately no `--from-database` input.** The workflow uploads its report as an artifact,
and a database-backed run would put real report text — derived from third-party pages — into it.

**Failure semantics, which are the point of the workflow having a shape at all:**

| Situation | Outcome |
|---|---|
| Missing or wrong model / endpoint / key | **exit 1** at startup, naming the variable |
| The judge scored nothing at all — every call failed | **exit 1** via `--require-judge-scores`. A provider fault, not a quality result |
| Some cases scored, some failed | **exit 0.** The report names the failed ones under `judge_aggregates.errors` |
| Every case scored, and every score is 1 out of 5 | **exit 0.** Judge scores are observational; §14 says why |

---

## 20. What CI runs

Seven jobs. **The six Phase 3 jobs are untouched by Block C**, and a test asserts each is still
present along with the LocalStack diagnostics in `integration` and `container`.

| Job | Needs | What it answers |
|---|---|---|
| `quality` | nothing | ruff, ruff format, mypy --strict, whitespace |
| `unit` | nothing | the offline suite |
| **`eval`** | **nothing** | the deterministic benchmark, then the regression gate |
| `postgres` | PostgreSQL 16 | the real-database layer |
| `redis` | Redis 7 | the shared limiter and the caches |
| `integration` | LocalStack | real SQS and S3 |
| `container` | the built image | both entrypoints, from outside |

The `eval` job is its own job rather than a step inside `unit` because the two answer different
questions and a build log should say which one broke. It runs:

```bash
uv run python -m eval.run --run-id ci --csv
```
```bash
uv run python -m eval.gate measurements/eval/ci.json
```

then uploads `measurements/eval/` as an artifact, `if: always()`, so a violation is diagnosable
without re-running anything.

**It needs no Docker, no service, no network and no credential**, and its environment carries no
`LLM_*` or `TAVILY_API_KEY` variable at all — asserted by `tests/test_eval_ci_workflows.py`, because
that is the property that keeps every pull request and every fork's CI provider-independent.

---

## Where to look next

| Question | File |
|---|---|
| Why is the engine built this way? | [ADR 0017](adr/0017-deterministic-evaluators-and-a-custom-structured-judge.md) |
| Why does the gate check contracts rather than scores? | [ADR 0018](adr/0018-the-ci-evaluation-gate-protects-the-contract-not-the-quality.md) |
| Exactly what fails CI? | §14, and `eval/gate.py`'s six rules |
| What does the requirement say? | `docs/engineering-guidelines.md` §15 |
| What is the case schema, exactly? | `eval/schema.py` |
| What does a metric mean, exactly? | that metric's docstring in `eval/metrics.py` |
| What does the judge actually ask? | `_SYSTEM` in `eval/judge.py` |
| How is a metric's boundary pinned? | `tests/test_eval_metrics.py` |
