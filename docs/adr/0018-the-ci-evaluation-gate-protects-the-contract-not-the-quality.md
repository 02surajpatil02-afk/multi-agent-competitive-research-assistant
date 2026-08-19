# ADR 0018 — The CI evaluation gate protects the contract, not the quality

- **Status:** **Accepted and built, 2026-08-19** (Phase 4 block C). Adds one CI job and one
  manual workflow; changes no Phase 3 runtime semantics and no existing CI job
- **Date:** 2026-08-19
- **Affects:** `eval/gate.py` (new) · `eval/schema.py` · `eval/report.py` · `eval/run.py` ·
  `eval/metrics.py` · `eval/benchmarks/dev.json` · `.github/workflows/ci.yml` ·
  `.github/workflows/eval-judge.yml` (new) · `docs/evaluation.md`
- **Follows:** [ADR 0017](0017-deterministic-evaluators-and-a-custom-structured-judge.md), whose
  decision 5 deferred every threshold to this block

---

## Context

ADR 0017 shipped the evaluation engine with **no gate at all**, and gave a reason: *"a gate needs
a distribution to be calibrated against, and there is not one yet."* Block C is the block that has
the distribution. The question it has to answer is not *"do we gate?"* but **"which of these
seventeen numbers is evidence about this repository?"**

The baseline was produced first and inspected before anything was chosen
(`python -m eval.run`, 2026-08-19, 26 cases, no judge):

| metric | cases | scored | n/a | mean | min | max | pass | fail |
|---|---|---|---|---|---|---|---|---|
| `terminal_success` | 26 | 26 | 0 | 1.00 | 1.00 | 1.00 | 26 | 0 |
| `structured_output_validity` | 26 | 26 | 0 | 0.96 | 0.00 | 1.00 | 25 | 1 |
| `citation_presence` | 26 | 25 | 1 | 0.96 | 0.00 | 1.00 | 24 | 1 |
| `claim_citation_coverage` | 26 | 24 | 2 | 0.99 | 0.80 | 1.00 | 23 | 1 |
| `claim_support_rate` | 26 | 24 | 2 | 0.97 | 0.50 | 1.00 | 13 | 2 |
| `required_fact_coverage` | 26 | 3 | 23 | 1.00 | 1.00 | 1.00 | 3 | 0 |
| `expected_entity_coverage` | 26 | 23 | 3 | 0.98 | 0.50 | 1.00 | 22 | 1 |
| `forbidden_claim_absence` | 26 | 2 | 24 | 0.50 | 0.00 | 1.00 | 1 | 1 |
| `research_coverage` | 26 | 26 | 0 | 0.92 | 0.00 | 1.00 | 12 | 0 |
| `source_diversity` | 26 | 24 | 2 | 0.83 | 0.17 | 1.00 | 8 | 1 |
| `duplicate_source_absence` | 26 | 24 | 2 | 0.99 | 0.86 | 1.00 | 23 | 1 |
| `minimum_useful_output` | 26 | 24 | 2 | 0.99 | 0.67 | 1.00 | 23 | 1 |

26 cases: 18 evaluated, 8 failed, 0 skipped, 0 errored, 0 benchmark problems.

**Reading that table is what decided this record.** Three things about it are disqualifying for a
percentage threshold:

1. **Every number describes our own fixtures.** The outputs are authored files citing
   `example.com`. `source_diversity` at 0.83 is a fact about how many hostnames were typed into
   twenty-four JSON files, not about how the Researcher sources a report.
2. **Eight of the twenty-six cases are deliberately broken.** Every mean is dragged by defects that
   are *supposed* to be there, so the mean has no interpretation as "how good the system is" and
   moves whenever the healthy/defective ratio changes — which happens whenever a case is added.
3. **Four metrics have a scored population of 3, 2, 23 and 24 out of 26.** `forbidden_claim_absence`
   at 0.50 is one healthy case and one defect. A threshold on a two-observation mean is noise with a
   decimal point.

A threshold on any of those would fail one day and nobody would be able to say whether the system
regressed or a fixture was edited. That failure mode is worse than no gate, because it burns the
credibility a gate runs on.

But the same run also shows something that **is** exact and **is** about this repository: every one
of the twenty-six cases classified exactly as intended, eight failing precisely the metrics their
`notes` describe. That is a contract, and it is checkable without a single percentage.

---

## Decision

### 1. The gate is a regression contract over committed fixtures, not a quality threshold

`eval/gate.py` fails a build on six rules and nothing else:

| Rule | What it catches |
|---|---|
| `benchmark_parses` | a benchmark row that no longer validates |
| `cases_selected` | a run that evaluated nothing |
| `no_evaluator_errors` | a case whose output would not load |
| `no_skipped_cases` | a fixture-backed case that did not run |
| `metrics_present` · `metrics_ran_on_every_case` · `metrics_registry_matches` | an evaluator that was deleted, renamed, or silently stopped producing results |
| `contract_names_real_metrics` · `no_unexpected_failures` · `declared_failures_still_fail` | a committed output that stopped failing exactly the metrics it declares |

**There is no percentage anywhere in the file**, and a test asserts that a metric mean, a metric
minimum, a judge score and a run duration all move without failing it.

### 2. The contract lives on the case, as `expect_failing_metrics`

Each `EvalCase` names the metrics its committed output is expected to fail. Eighteen declare none;
eight declare one or two, matching the baseline exactly.

**The field is deliberately redundant with the case's other expectations, and the redundancy is the
check.** `cmp-datadog-newrelic-half` fails `expected_entity_coverage` because its fixture names one
of two required entities — the metric derives that, and this field asserts it independently. An
evaluator that stops catching the defect makes the derived result disagree with the declared one.

**Both directions are gated, and the second is the one that matters.** An *unexpected* failure means
a healthy fixture regressed or a metric got stricter — loud, and someone will look. A *missing*
failure means an evaluator stopped detecting a defect that is still in the fixture, which looks
exactly like an improvement and is the regression nothing else in this repository would notice.

**Trade-off.** Adding a case now means declaring its contract, and a legitimate change to a metric
means editing the benchmark deliberately. That friction is the feature: it converts "a number moved"
into "someone stated what the new expectation is."

### 3. The gate is a pure function of one report file

`python -m eval.run` produces the evidence; `python -m eval.gate <report.json>` judges it. The gate
opens no benchmark, loads no fixture, and runs no evaluator.

**Why two commands rather than one `--gate` flag.** The measurement and the judgement must not be
able to re-decide each other, and separating them makes that structural rather than careful. It also
keeps `eval.run`'s existing contract intact — it still exits 0 whatever the scores are, which is what
its own tests and `docs/evaluation.md` already promise — and it makes the report a CI artifact that
can be inspected without re-running anything.

### 4. Three exit codes, because a person reacts to each differently

`0` contract intact · `1` contract violated · `2` nothing judgeable — no report, unreadable JSON, or
a `--from-database` report, which legitimately skips every fixture-backed case and is therefore a
mode this contract is not defined over.

**Why not two.** A build that cannot distinguish an infrastructure fault from a contract violation
will eventually "fix" the former by editing a benchmark. That is the specific mistake this costs one
integer to prevent.

### 5. The judge stays out of pull-request CI, and gets a manual workflow instead

`.github/workflows/eval-judge.yml` is `workflow_dispatch` only — no `pull_request`, no `push`, no
`schedule` — so it can never become a required check, and normal CI stays provider-independent.

**Why a workflow at all, rather than "run it locally".** One reviewed place with one credential path
and one artifact is worth having *before* there is anything important to judge; the alternative is
that the first real judge run is improvised in someone's shell with a key pasted into it.

**No judge threshold, and that is unchanged from ADR 0017 decision 3.** The rubric is uncalibrated,
and judging authored fixtures that cite `example.com` measures the fixtures. The workflow's only
failure condition is `--require-judge-scores`: exit 1 when the judge was enabled and scored
**nothing at all**. That is a provider-health check — a wrong model id, an expired key, an endpoint
that is down — and it exists because that failure otherwise goes green with five dimensions silently
missing everywhere. One scored case satisfies it; how well anything scored is not its business.

### 6. Its own CI job, and no infrastructure

The `eval` job needs no Docker, no service, no network and no credential: the twelve metrics are pure
functions over committed fixtures. It is separate from `unit` because the two answer different
questions and a build log should say which one broke — `unit` answers *"does the code work"*, `eval`
answers *"does the evaluation framework still hold its contract"*.

**The six Phase 3 jobs are untouched**, and a test asserts each is still present, that the eval job
carries no provider variable, and that the judge workflow is unreachable from `ci.yml`.

### 7. No Prometheus, no Grafana, no metrics endpoint

The repository exposes no metrics surface today — `/health` returns three boolean checks, there is no
`prometheus_client` dependency, no `/metrics` route, and no scrape configuration. Building one would
mean a client library, a route, an exporter, a scrape target and a dashboard, for a system with no
deployment to scrape.

**So nothing was implemented and the recommended counters are written down instead**
(`docs/evaluation.md`, "Runtime metrics"). They land with Phase 5's CloudWatch work, which is where
there is finally something to observe.

---

## Consequences

- CI gains a seventh job, the cheapest in the pipeline, and one artifact per run.
- The report gains two fields — `evaluator_version` (`det-v1`) and `duration_seconds` — because the
  first question two disagreeing reports raise is *"did the system change, or did the ruler?"*, and
  only a version on the evaluator set answers it.
- `EvalCase` gains `expect_failing_metrics`, read by the gate and by nothing else. **No metric reads
  it**, so it cannot influence a score.
- `eval.run` gains `--require-judge-scores`, off by default and used only by the manual workflow.
- **Nothing in the deterministic path can reach a provider**, and a test asserts the CI job's
  environment carries no `LLM_*` or `TAVILY_*` variable.

## What would reopen this

- **A benchmark built from real, committed research outputs.** That is what makes a metric
  distribution evidence about the system rather than about our fixtures, and it is the precondition
  for any percentage threshold. `docs/evaluation.md`, "What remains before real quality gating", is
  the sequence.
- **A calibrated judge rubric** (step 27's hand-scoring pass). Until judge and human agree within a
  point, a judge number cannot fail anything.
- **A HOLDOUT split**, once DEV has been tuned against often enough for overfitting to be a real
  risk. It is still not created, for the reason ADR 0017 decision 7 gave: nothing reads it yet.
