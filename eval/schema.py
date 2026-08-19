"""
WHY THIS FILE EXISTS
    One evaluation case, typed, plus the file that holds a benchmark of them. It is the
    contract between "what a good answer looks like" and every evaluator that checks one,
    which is why the expectations live here as fields rather than inside the metrics as
    constants: a threshold written into an evaluator is a threshold nobody can see in the
    data (guidelines §15, ADR 0017 decision 4).

    Three things are worth reading before the fields.

    **Every pass rule a metric applies comes from a field below, or from an invariant this
    repository already states.** There is no third source. `min_distinct_domains` is the
    case's opinion about source diversity; `claim_citation_coverage` passing only at 1.0 is
    CLAUDE.md invariant 1, not this file's idea. A metric with neither is reported with
    `passed=None` rather than being given a default that looks like a judgement.

    **A malformed case is data, not a crash.** `load_benchmark()` validates each case on its
    own and returns the ones that parsed alongside the problems it found, so one bad row
    cannot cost a run its other twenty-three results. The runner reports the problems as
    errored cases.

    **`provenance` is how honest the case is about where its expectations came from**, and it
    is a required field because the answer is not obvious from the outside:

      * `repository_fixture` - the question is one of the twenty in
        `scripts/measure_jobs.py`, which are the shapes this system is actually for, and every
        expectation is a structural property of the committed fixture output beside it.
      * `synthetic_contract` - the question and the output were both authored to exercise one
        evaluator boundary.

    **Neither kind asserts an external fact.** No case in this repository claims to know what
    TCS's cloud revenue was; the fixtures cite `example.com`, and a `required_fact` is a
    statement about the fixture's own text (docs/evaluation.md, "What the DEV benchmark is
    not").

WHO CALLS IT
    eval/metrics.py reads the expectations, eval/run.py loads the file, and
    tests/test_eval_schema.py holds the validation behaviour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from schemas import JobStatus

BenchmarkSplit = Literal["dev"]
"""The splits that exist. **`holdout` is deliberately absent**: a held-out set is only worth
creating once the DEV set has produced a baseline worth defending against overfitting, and
creating it now would mean two datasets with no measurement behind either (docs/evaluation.md,
"Why HOLDOUT is deferred"). Adding it is one literal and one directory."""

Category = Literal[
    "factual_extraction",
    "multi_source_synthesis",
    "company_comparison",
    "contradiction_handling",
    "citation_grounding",
    "insufficient_evidence",
    "research_coverage",
    "duplicate_sources",
    "entity_coverage",
    "source_diversity",
]
"""What a case is for. Every one of these is checkable against what the repository's schemas
actually carry - which is why "human review output behaviour" is not among them: no output
this package can load carries a gate payload, so a category for it would be a label with no
evaluator behind it."""

Difficulty = Literal["easy", "medium", "hard"]
"""How hard the case is for the *system*, not for the evaluator. Reported in the aggregates so
a regression can be read as "the hard cases moved", which is the useful shape."""

Provenance = Literal["repository_fixture", "synthetic_contract"]

CASE_ID = r"^[a-z0-9][a-z0-9-]{2,63}$"
"""Stable, lowercase, and usable as a filename. Case ids are joined to results in the report
and quoted in commit messages, so they are kept boring on purpose."""


class RequiredFact(BaseModel):
    """One thing the report has to say, expressed as the phrasings that count as saying it.

    `any_of` exists because a report is prose: "cloud revenue" and "revenue from cloud
    services" are the same fact, and a single required string would fail a correct answer for
    writing itself differently.

    **This is lexical matching and it proves nothing semantic.** A report that contains the
    phrase inside a sentence denying it scores the same as one that asserts it. It is a cheap
    regression check on coverage, and the judge's `completeness` dimension is what is actually
    being asked when the question is "did it find what an analyst would find" (guidelines §15).
    """

    model_config = {"extra": "forbid"}

    id: str
    any_of: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _phrases_are_not_blank(self) -> RequiredFact:
        if any(not phrase.strip() for phrase in self.any_of):
            raise ValueError("every phrase in any_of must be non-empty")
        return self


class EvalCase(BaseModel):
    """One question, the output it is scored against, and what a good answer would contain.

    Deliberately not one field per metric. `min_sources` and `min_distinct_domains` are two
    different opinions about sourcing and both are optional, because a case that has nothing
    to say about diversity should say nothing rather than inherit a number.
    """

    model_config = {"extra": "forbid"}
    """An unknown key is a typo, and a typo in a benchmark is an expectation that silently
    stops being checked. Refusing the key is how that becomes a loud problem in the report."""

    case_id: str = Field(pattern=CASE_ID)
    split: BenchmarkSplit
    question: str = Field(min_length=10)
    category: Category
    difficulty: Difficulty
    provenance: Provenance

    output_ref: str
    """The research output this case is scored against, as a path relative to the benchmark
    file. Cases and outputs are separate files because one output can legitimately be asked
    two different questions - entity coverage in one case, source diversity in another."""

    job_id: str | None = None
    """The job whose output this is, when it is a real one. `None` for every fixture-backed
    case, which is all of them today. It is what `--from-database` loads by, and it is also
    the LangSmith `thread_id`, so it is the single identifier that joins an eval row to a
    database row and to a trace (docs/evaluation.md, "Trace linkage")."""

    expected_status: JobStatus
    """How the job should have ended. Required rather than defaulted to `approved`, because a
    case about insufficient evidence expects `failed` and a default would quietly turn that
    into a failing metric."""

    required_entities: list[str] = Field(default_factory=list)
    required_facts: list[RequiredFact] = Field(default_factory=list)

    forbidden_claims: list[str] = Field(default_factory=list)
    """Phrases the report must not contain - a contradiction of the evidence, or a claim the
    question's scope rules out. Lexical, with the same limitation `RequiredFact` carries."""

    min_claims: int | None = Field(default=None, ge=1)
    min_sources: int | None = Field(default=None, ge=1)
    min_distinct_domains: int | None = Field(default=None, ge=1)
    max_unsupported_claims: int | None = Field(default=None, ge=0)

    expect_all_subtopics_researched: bool | None = None
    """`True` means every planned subtopic must have produced findings. `False` states that
    this case expects a gap and that the gap must still be reported rather than hidden - which
    is the documented behaviour of a job whose evidence ran out (CLAUDE.md, reflection)."""

    entity_scope: list[str] = Field(default_factory=list)
    """The companies or products the question is about. Narrower than `required_entities`:
    scope is what the question covers, required entities are what the answer must name. Kept
    for reporting and for later slicing, not scored."""

    temporal_scope: str | None = None
    """"the last 12 months", "since 2023" - as the question states it. Not scored: nothing in
    an output carries a publication date that could be checked against it without re-fetching
    every source, which is the expensive thing this package exists to avoid."""

    expect_failing_metrics: list[str] = Field(default_factory=list)
    """**The regression contract**: exactly which metrics this case's committed output is
    expected to fail, by name. Empty - the default - means it must fail none.

    It is deliberately redundant with the expectations above, and the redundancy *is* the
    check. `cmp-datadog-newrelic-half` fails `expected_entity_coverage` because its fixture
    names one of two required entities; this field says so independently, so an evaluator that
    quietly stops catching that defect breaks the contract instead of turning green
    ([ADR 0018](../docs/adr/0018-the-ci-evaluation-gate-protects-the-contract-not-the-quality.md)).

    Read by `eval/gate.py` and by nothing else. **No metric reads it**, and it is not an
    expectation about a good answer - it is a statement about a file this repository committed.
    Names are checked against the metric registry by the gate rather than here, because
    `eval.metrics` imports this module and the dependency cannot run both ways."""

    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    """Why this case exists, in a sentence. Every `known-defect` case uses it to say what is
    wrong with its fixture, so a low score reads as intended rather than as a bug."""


@dataclass(frozen=True)
class CaseProblem:
    """A case that could not be loaded, and why. Carried into the report as an errored case
    rather than raised, so a benchmark with one bad row still produces results."""

    case_id: str
    """The `case_id` if it parsed, otherwise the case's index in the file, so a problem is
    always locatable in the source."""

    problem: str


@dataclass(frozen=True)
class Benchmark:
    """One benchmark file, loaded: what parsed, and what did not."""

    version: str
    path: Path
    cases: tuple[EvalCase, ...]
    problems: tuple[CaseProblem, ...]

    def select(
        self,
        *,
        split: BenchmarkSplit | None = None,
        case_ids: frozenset[str] | None = None,
        tags: frozenset[str] | None = None,
    ) -> tuple[EvalCase, ...]:
        """The cases a run should evaluate. Filters compose with AND, and no filter means all."""
        chosen = self.cases
        if split is not None:
            chosen = tuple(case for case in chosen if case.split == split)
        if case_ids is not None:
            chosen = tuple(case for case in chosen if case.case_id in case_ids)
        if tags is not None:
            chosen = tuple(case for case in chosen if tags.intersection(case.tags))
        return chosen


class BenchmarkError(RuntimeError):
    """The file itself could not be read - missing, not JSON, or not the expected shape.

    Distinct from a `CaseProblem`, and the distinction is the error-isolation rule: a broken
    case is isolated and reported, a broken *file* leaves nothing to isolate it from.
    """


def load_benchmark(path: Path) -> Benchmark:
    """Read a benchmark file, validating each case on its own.

    Duplicate `case_id`s are a problem rather than an exception for the same reason a
    malformed case is: the other cases are still worth running, and a duplicate id would
    otherwise silently overwrite a result in the report.
    """
    document = _read_document(path)
    version = document.get("version")
    if not isinstance(version, str) or not version.strip():
        raise BenchmarkError(f"{path}: the benchmark needs a non-empty string `version`")

    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list):
        raise BenchmarkError(f"{path}: the benchmark needs a `cases` list")

    cases: list[EvalCase] = []
    problems: list[CaseProblem] = []
    seen: set[str] = set()

    for index, raw in enumerate(raw_cases):
        label = _label(raw, index)
        if not isinstance(raw, dict):
            problems.append(CaseProblem(label, "the case is not a JSON object"))
            continue
        try:
            case = EvalCase.model_validate(raw)
        except ValidationError as error:
            problems.append(CaseProblem(label, _compact(error)))
            continue
        if case.case_id in seen:
            problems.append(CaseProblem(case.case_id, "duplicate case_id"))
            continue
        seen.add(case.case_id)
        cases.append(case)

    return Benchmark(version=version, path=path, cases=tuple(cases), problems=tuple(problems))


def _read_document(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BenchmarkError(f"{path}: could not be read ({error})") from error
    try:
        loaded: Any = json.loads(text)
    except ValueError as error:
        raise BenchmarkError(f"{path}: is not valid JSON ({error})") from error
    if not isinstance(loaded, dict):
        raise BenchmarkError(f"{path}: the top level must be a JSON object")
    return loaded


def _label(raw: Any, index: int) -> str:
    """What to call a case that may not have parsed far enough to have a name."""
    if isinstance(raw, dict):
        case_id = raw.get("case_id")
        if isinstance(case_id, str) and case_id:
            return case_id
    return f"case[{index}]"


def _compact(error: ValidationError) -> str:
    """Pydantic's report on one line, so it fits in a report field and a terminal row."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<case>'}: {item['msg']}"
        for item in error.errors()
    )
