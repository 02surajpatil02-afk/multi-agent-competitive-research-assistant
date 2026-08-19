"""
WHY THIS FILE EXISTS
    The deterministic half of the evaluation, which is the half that should be doing most of
    the work. Coverage is arithmetic, duplicate sources are a set operation, and a claim that
    cites a finding nobody retrieved is a lookup - spending a judge call on any of them buys
    variance in a number that should be exact (guidelines §15, ADR 0017 decision 2).

    Every metric here is a **pure function of one output and one case**. No I/O, no clock, no
    network, no model, no configuration. That is what makes the reproducibility test in
    tests/test_eval_metrics.py meaningful rather than decorative: the same two inputs give the
    same twelve results, forever.

    Four conventions, applied to all twelve without exception.

    **`score` is 0.0-1.0 and higher is better**, so twelve metrics can be averaged, compared
    and charted without a per-metric direction table. Metrics that are naturally a *rate of
    badness* - duplicates, unsupported claims - are reported as their complement, and their
    docstring says so.

    **`score=None` means "this case says nothing about this".** It is not zero and it is not
    one. `required_fact_coverage` on a case with no required facts has no answer, and inventing
    1.0 would silently lift every aggregate that includes it.

    **`passed` comes from the case, or from an invariant this repository already states, or it
    is `None`.** There is no third source and there are no constants in this file that a reader
    of a benchmark row could not have predicted. Two metrics pass only at 1.0 because CLAUDE.md
    invariant 1 makes citation a hard export gate; every other pass rule is a case field.

    **`details` carries the evidence, not a summary of it.** Which entity was missing, which URL
    was duplicated, which claim cited what. A metric that says 0.6 and nothing else costs the
    reader the run they have to do again to find out why.

    **What is deliberately not here.** Nothing in this file claims that a lexical match proves
    semantic correctness: `required_fact_coverage`, `expected_entity_coverage` and
    `forbidden_claim_absence` are string containment and say so in their own docstrings. The
    questions those cannot answer - is this faithful to its evidence, is it well synthesised,
    did it resolve the contradiction - are the judge's, in eval/judge.py.

WHO CALLS IT
    eval/run.py runs `DETERMINISTIC_METRICS` over each case, and tests/test_eval_metrics.py
    holds each metric's boundaries.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from eval.outputs import ResearchOutput
from eval.schema import EvalCase
from schemas import Report


@dataclass(frozen=True)
class MetricResult:
    """One metric's answer about one output.

    `passed` is deliberately three-valued. `True` and `False` mean a stated rule was applied;
    `None` means there was no rule to apply, which is a different statement from "it passed"
    and has to survive into the report as one.
    """

    metric: str
    score: float | None
    passed: bool | None
    explanation: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "score": self.score,
            "passed": self.passed,
            "explanation": self.explanation,
            "details": self.details,
        }


Metric = Callable[[ResearchOutput, EvalCase], MetricResult]

_NO_REPORT = "no report on this output, so there is nothing to measure"


def terminal_success(output: ResearchOutput, case: EvalCase) -> MetricResult:
    """Did the job end the way this case expects?

    Inputs: `output.status`, `case.expected_status`.
    Score: 1.0 on a match, 0.0 otherwise. Pass: the score is 1.0.

    The expectation is per case rather than "approved everywhere", because a case about
    insufficient evidence expects `failed` and scoring that as a failure would punish the
    system for behaving as documented.

    Limitation: it says nothing about *why* a job ended where it did. `metadata.failure_reason`
    carries that into the report unscored, because the set of reasons is a runtime vocabulary
    and pinning a case to one would make it a test of `llm_client`, not of research quality.
    """
    matched = output.status == case.expected_status
    return MetricResult(
        metric="terminal_success",
        score=1.0 if matched else 0.0,
        passed=matched,
        explanation=(
            f"the job ended {output.status!r} and the case expects {case.expected_status!r}"
        ),
        details={
            "status": output.status,
            "expected_status": case.expected_status,
            "failure_reason": output.metadata.failure_reason,
        },
    )


def required_fact_coverage(output: ResearchOutput, case: EvalCase) -> MetricResult:
    """What share of the facts this case requires appear anywhere in the report text?

    Inputs: `case.required_facts` (each a set of accepted phrasings), the report's headings,
    section bodies and claim texts.
    Score: matched facts / required facts. Pass: `None` unless every fact matched, which is
    what "required" means - a case that wants a softer rule states fewer facts.

    **Limitation, and it is the important one: this is case-insensitive substring matching and
    it proves nothing semantic.** A report that contains "cloud revenue" inside a sentence
    denying it scores the same as one that asserts it, and a correct fact written in words the
    case did not anticipate scores zero. It is a cheap regression check; the judge's
    `completeness` dimension is what actually answers "did it find what an analyst would find".
    """
    if not case.required_facts:
        return _not_applicable("required_fact_coverage", "the case states no required facts")
    if output.report is None:
        return _not_applicable("required_fact_coverage", _NO_REPORT)

    haystack = _report_text(output.report)
    matched: list[str] = []
    missing: list[str] = []
    for fact in case.required_facts:
        hit = next((phrase for phrase in fact.any_of if phrase.lower() in haystack), None)
        (matched if hit is not None else missing).append(fact.id)

    score = len(matched) / len(case.required_facts)
    return MetricResult(
        metric="required_fact_coverage",
        score=score,
        passed=not missing,
        explanation=f"{len(matched)} of {len(case.required_facts)} required facts appear",
        details={"matched": matched, "missing": missing},
    )


def forbidden_claim_absence(output: ResearchOutput, case: EvalCase) -> MetricResult:
    """Does the report avoid every phrase this case forbids?

    Inputs: `case.forbidden_claims`, the report text.
    Score: 1 - matched / forbidden, so 1.0 means none of them appear. Pass: nothing matched.

    This is the contradiction-handling check a deterministic evaluator can actually make: a
    case names the wrong answer, and the metric asks whether the report says it. It cannot see
    a contradiction phrased differently, and it cannot tell an assertion from a quotation of
    something the report goes on to refute - both belong to the judge's
    `contradiction_handling` dimension.
    """
    if not case.forbidden_claims:
        return _not_applicable("forbidden_claim_absence", "the case forbids no claims")
    if output.report is None:
        return _not_applicable("forbidden_claim_absence", _NO_REPORT)

    haystack = _report_text(output.report)
    found = [phrase for phrase in case.forbidden_claims if phrase.lower() in haystack]
    score = 1.0 - len(found) / len(case.forbidden_claims)
    return MetricResult(
        metric="forbidden_claim_absence",
        score=score,
        passed=not found,
        explanation=(
            "no forbidden phrase appears"
            if not found
            else f"{len(found)} of {len(case.forbidden_claims)} forbidden phrases appear"
        ),
        details={"found": found, "forbidden": list(case.forbidden_claims)},
    )


def citation_presence(output: ResearchOutput, case: EvalCase) -> MetricResult:
    """Is there a grounded report at all - at least one claim, and at least one source?

    Inputs: `output.report`.
    Score: 1.0 when a valid report carries both, 0.0 otherwise. Pass: the score is 1.0.

    It looks trivial and it is the metric that catches the worst outcome: a job that reports
    success while carrying nothing anyone can check. `Report` itself refuses an empty `sources`
    list (schemas.py), so a 0.0 here means either no report or one that failed validation - and
    `structured_output_validity` says which.

    **The one case it does not apply to is a case that expects the job not to have exported.**
    A job the benchmark expects to end `failed` has no report by design, and scoring 0.0 there
    would turn documented behaviour into a quality regression - which is the same mistake
    `expected_status` exists to prevent. Nothing else softens the check: a case that expects an
    approved job cannot state its way out of needing a grounded report.
    """
    report = output.report
    if report is None and case.expected_status != "approved":
        return _not_applicable(
            "citation_presence",
            f"the case expects the job to end {case.expected_status!r}, which carries no report",
        )
    ok = report is not None and bool(report.claims) and bool(report.sources)
    return MetricResult(
        metric="citation_presence",
        score=1.0 if ok else 0.0,
        passed=ok,
        explanation=(
            "the report carries claims and sources"
            if ok
            else "there is no report carrying both a claim and a source"
        ),
        details={
            "claims": 0 if report is None else len(report.claims),
            "sources": 0 if report is None else len(report.sources),
        },
    )


def claim_citation_coverage(output: ResearchOutput, case: EvalCase) -> MetricResult:
    """What share of claims reach a source URL through the findings they cite?

    Inputs: `output.report.claims`, `output.report.sources`.
    Score: cited claims / claims. **Pass: 1.0 and nothing less** - this is the in-report form of
    the export gate, and CLAUDE.md invariant 1 makes it absolute: an uncited claim does not get
    a warning, the export does not happen. The rule is quoted from the invariant rather than
    chosen here, which is why it is the one metric with a fixed pass point and no case field.

    The arithmetic mirrors `graph.build._uncited_claims` exactly: a claim is cited when at least
    one of its `finding_ids` sits behind one of the report's sources. Deliberately a mirror
    rather than an import - a metric that called the code it is checking would agree with it
    however that code changed.

    Limitation: structural only. It proves a claim *has* a source, never that the source
    supports it. That is `claim_support_rate` (the Fact-Checker's answer) and the judge's
    `faithfulness` dimension.
    """
    del case
    report = output.report
    if report is None:
        return _not_applicable("claim_citation_coverage", _NO_REPORT)
    if not report.claims:
        return _not_applicable("claim_citation_coverage", "the report carries no claims")

    cited_ids = {finding_id for source in report.sources for finding_id in source.finding_ids}
    uncited = [
        claim.claim_id for claim in report.claims if not cited_ids.intersection(claim.finding_ids)
    ]
    known = {finding.finding_id for finding in output.findings}
    unknown = sorted(
        {
            finding_id
            for claim in report.claims
            for finding_id in claim.finding_ids
            if known and finding_id not in known
        }
    )
    score = 1.0 - len(uncited) / len(report.claims)
    return MetricResult(
        metric="claim_citation_coverage",
        score=score,
        passed=not uncited,
        explanation=(
            f"{len(report.claims) - len(uncited)} of {len(report.claims)} claims reach a source"
        ),
        details={
            "uncited_claims": uncited,
            # ADR 0003's failure mode: a claim citing a finding id that no finding in this job
            # has. It does not change the score - such a claim may still reach a source - but it
            # is the thing to look at first when this metric drops.
            "claims_citing_unknown_findings": unknown,
        },
    )


def source_diversity(output: ResearchOutput, case: EvalCase) -> MetricResult:
    """How many different hosts the cited sources come from.

    Inputs: `output.report.sources`, `case.min_distinct_domains`.
    Score: distinct hosts / sources, so 1.0 means every cited source is a different host and a
    report resting three claims on one site scores 1/3. Pass: `None` unless the case states
    `min_distinct_domains`, because how much diversity a question needs is a property of the
    question, not of this file.

    Limitation: **host, not registrable domain.** `ir.example.com` and `www.example.com` count
    as two, because working out that they are one organisation needs a public-suffix list, and
    adding a dependency to sharpen one metric is not a trade worth making yet
    (docs/evaluation.md, "Known limitations").
    """
    report = output.report
    if report is None:
        return _not_applicable("source_diversity", _NO_REPORT)
    if not report.sources:
        return _not_applicable("source_diversity", "the report cites no sources")

    hosts = Counter(_host(str(source.url)) for source in report.sources)
    distinct = len(hosts)
    passed = None if case.min_distinct_domains is None else distinct >= case.min_distinct_domains
    return MetricResult(
        metric="source_diversity",
        score=distinct / len(report.sources),
        passed=passed,
        explanation=f"{distinct} distinct host(s) across {len(report.sources)} cited source(s)",
        details={
            "distinct_hosts": distinct,
            "sources": len(report.sources),
            "min_distinct_domains": case.min_distinct_domains,
            "hosts": dict(sorted(hosts.items())),
        },
    )


def duplicate_source_absence(output: ResearchOutput, case: EvalCase) -> MetricResult:
    """Does the report cite the same URL more than once, as two separate sources?

    Inputs: `output.report.sources`, and `output.findings` for the same page fetched twice.
    Score: 1 - duplicate source entries / sources, so 1.0 means every cited URL appears once.
    Pass: no duplicates. Definitional rather than case-stated: `Report.sources` is documented as
    a view over the findings actually cited (schemas.py), and one URL is one view of one page.

    The finding-level count in `details` is the related but different question - the same page
    retrieved twice inside one job, which the per-job URL set exists to prevent (guidelines §7,
    §11). It is reported and not scored, because a legitimate re-fetch by the Fact-Checker
    produces no second finding and a duplicate there means something else went wrong.
    """
    del case
    report = output.report
    if report is None:
        return _not_applicable("duplicate_source_absence", _NO_REPORT)
    if not report.sources:
        return _not_applicable("duplicate_source_absence", "the report cites no sources")

    urls = Counter(_normalised(str(source.url)) for source in report.sources)
    repeated = {url: count for url, count in urls.items() if count > 1}
    extra = sum(count - 1 for count in repeated.values())
    finding_urls = Counter(_normalised(str(finding.url)) for finding in output.findings)
    return MetricResult(
        metric="duplicate_source_absence",
        score=1.0 - extra / len(report.sources),
        passed=not repeated,
        explanation=(
            "every cited URL appears once"
            if not repeated
            else f"{len(repeated)} URL(s) are cited more than once"
        ),
        details={
            "duplicate_source_urls": dict(sorted(repeated.items())),
            "duplicate_finding_urls": {
                url: count for url, count in sorted(finding_urls.items()) if count > 1
            },
        },
    )


def minimum_useful_output(output: ResearchOutput, case: EvalCase) -> MetricResult:
    """Is there enough report here to be worth a reviewer's time?

    Inputs: `output.report`, `case.min_claims` (default 1 - a report with no claim asserts
    nothing, which needs no case to say).
    Score: the share of three checks that hold - enough claims, every section body non-empty,
    every claim text non-empty. Pass: all three.

    Limitation: length is not quality. This catches the empty and the degenerate, and says
    nothing about the merely bad - which is the judge's `synthesis_quality`.
    """
    report = output.report
    if report is None:
        return _not_applicable("minimum_useful_output", _NO_REPORT)

    wanted = case.min_claims or 1
    empty_sections = [section.id for section in report.sections if not section.body.strip()]
    empty_claims = [claim.claim_id for claim in report.claims if not claim.text.strip()]
    checks = {
        "enough_claims": len(report.claims) >= wanted,
        "sections_have_bodies": not empty_sections,
        "claims_have_text": not empty_claims,
    }
    held = sum(1 for ok in checks.values() if ok)
    return MetricResult(
        metric="minimum_useful_output",
        score=held / len(checks),
        passed=held == len(checks),
        explanation=f"{held} of {len(checks)} minimum-output checks hold",
        details={
            "claims": len(report.claims),
            "min_claims": wanted,
            "sections": len(report.sections),
            "empty_sections": empty_sections,
            "empty_claims": empty_claims,
            **checks,
        },
    )


def structured_output_validity(output: ResearchOutput, case: EvalCase) -> MetricResult:
    """Did the stored report body validate against `schemas.Report`?

    Inputs: `output.schema_errors`, filled by whichever loader produced this output.
    Score: 1.0 when there are no errors, 0.0 otherwise. Pass: the score is 1.0.

    A job with no report at all scores 1.0 here, and that is correct rather than generous: it
    emitted nothing invalid. `terminal_success` and `citation_presence` are the metrics that
    have something to say about a missing report; this one is only about malformed ones.
    """
    del case
    valid = not output.schema_errors
    return MetricResult(
        metric="structured_output_validity",
        score=1.0 if valid else 0.0,
        passed=valid,
        explanation=(
            "the report body validates against the repository schema"
            if valid
            else f"the report body failed validation with {len(output.schema_errors)} problem(s)"
        ),
        details={"schema_errors": list(output.schema_errors)},
    )


def expected_entity_coverage(output: ResearchOutput, case: EvalCase) -> MetricResult:
    """What share of the entities this case requires are named in the report?

    Inputs: `case.required_entities`, the report text.
    Score: named / required. Pass: `None` unless all of them are named - a comparison of two
    companies that never names one of them has not answered the question.

    Same lexical limitation as `required_fact_coverage`: case-insensitive substring matching, so
    an abbreviation the case did not list reads as absent, and a company named only inside a
    sentence about something else reads as present.
    """
    if not case.required_entities:
        return _not_applicable("expected_entity_coverage", "the case requires no entities")
    if output.report is None:
        return _not_applicable("expected_entity_coverage", _NO_REPORT)

    haystack = _report_text(output.report)
    named = [entity for entity in case.required_entities if entity.lower() in haystack]
    missing = [entity for entity in case.required_entities if entity.lower() not in haystack]
    return MetricResult(
        metric="expected_entity_coverage",
        score=len(named) / len(case.required_entities),
        passed=not missing,
        explanation=f"{len(named)} of {len(case.required_entities)} required entities are named",
        details={"named": named, "missing": missing},
    )


def research_coverage(output: ResearchOutput, case: EvalCase) -> MetricResult:
    """What share of the planned subtopics actually produced research?

    Inputs: `output.planned_subtopics`, `output.subtopic_status`,
    `case.expect_all_subtopics_researched`.
    Score: subtopics not marked `unresearched` / planned subtopics. Pass: `None` unless the case
    states the expectation - and a case may legitimately state `false`, meaning "this job runs
    out of evidence and the gap must still be visible" rather than "any gap is fine".

    Limitation: a subtopic marked `done` with one thin finding counts the same as one with six.
    Depth is the judge's `completeness` dimension; this is breadth, and breadth is countable.
    """
    planned = output.planned_subtopics
    if not planned:
        return _not_applicable("research_coverage", "no plan was recorded for this output")

    unresearched = [
        subtopic for subtopic in planned if output.subtopic_status.get(subtopic) == "unresearched"
    ]
    missing_status = [subtopic for subtopic in planned if subtopic not in output.subtopic_status]
    score = 1.0 - len(unresearched) / len(planned)
    expected = case.expect_all_subtopics_researched
    passed = None if expected is None else (not unresearched) == expected
    return MetricResult(
        metric="research_coverage",
        score=score,
        passed=passed,
        explanation=(
            f"{len(planned) - len(unresearched)} of {len(planned)} planned subtopics "
            "produced research"
        ),
        details={
            "planned": list(planned),
            "unresearched": unresearched,
            "no_status_recorded": missing_status,
            "expect_all_subtopics_researched": expected,
        },
    )


def claim_support_rate(output: ResearchOutput, case: EvalCase) -> MetricResult:
    """Of the claims the Fact-Checker checked, what share did it support?

    Inputs: `output.verdicts`, `case.max_unsupported_claims`.
    Score: supported / checked. Pass: `None` unless the case states a ceiling on unsupported
    claims, because how many an answer may carry to the gate is a property of the question -
    a report about a contested topic legitimately carries some.

    **Claims with no verdict are excluded from the denominator, not counted as unsupported.**
    Unchecked and unsupported are different states (eval/outputs.py), and `details` carries the
    unchecked count so a job whose Fact-Checker never ran cannot hide behind a 1.0 over two
    claims.

    Limitation: this reports the Fact-Checker's own opinion. It is a regression check on that
    component, never independent verification - the judge's `faithfulness` dimension is the
    second opinion, and neither re-fetches the source.
    """
    checked = [verdict for verdict in output.verdicts if verdict.supported is not None]
    if not checked:
        return _not_applicable("claim_support_rate", "no claim on this output carries a verdict")

    unsupported = [verdict.claim_id for verdict in checked if verdict.supported is False]
    ceiling = case.max_unsupported_claims
    passed = None if ceiling is None else len(unsupported) <= ceiling
    return MetricResult(
        metric="claim_support_rate",
        score=(len(checked) - len(unsupported)) / len(checked),
        passed=passed,
        explanation=f"{len(checked) - len(unsupported)} of {len(checked)} checked claims supported",
        details={
            "checked": len(checked),
            "unsupported_claims": unsupported,
            "unchecked": len(output.verdicts) - len(checked),
            "max_unsupported_claims": ceiling,
        },
    )


DETERMINISTIC_METRICS: tuple[Metric, ...] = (
    terminal_success,
    structured_output_validity,
    citation_presence,
    claim_citation_coverage,
    claim_support_rate,
    required_fact_coverage,
    expected_entity_coverage,
    forbidden_claim_absence,
    research_coverage,
    source_diversity,
    duplicate_source_absence,
    minimum_useful_output,
)
"""Every deterministic metric, in the order a report prints them: the ones that say whether
there is a usable answer first, then what it contains, then how well sourced it is.

A tuple rather than a registry with decorators. Twelve functions in one module do not need a
plugin system, and an explicit list is what makes "which metrics ran?" answerable by reading
one line."""

METRIC_NAMES: tuple[str, ...] = tuple(metric.__name__ for metric in DETERMINISTIC_METRICS)


def evaluate_deterministic(output: ResearchOutput, case: EvalCase) -> list[MetricResult]:
    """Every deterministic metric, in order. Pure, and therefore reproducible."""
    return [metric(output, case) for metric in DETERMINISTIC_METRICS]


# --- Helpers --------------------------------------------------------------------------


def _not_applicable(metric: str, why: str) -> MetricResult:
    """No score and no verdict, with the reason. Never 0.0 and never 1.0 - see the module
    docstring on why a "not applicable" that scores would move every aggregate it is in."""
    return MetricResult(metric=metric, score=None, passed=None, explanation=why)


def _report_text(report: Report) -> str:
    """Everything a lexical check may look in, lowercased once.

    Headings, section bodies and claim texts - the report as a reader sees it. Deliberately
    **not** finding evidence: a fact that appears only in a quote the report never used is a
    fact the report did not deliver.
    """
    parts: Iterable[str] = (
        *(section.heading for section in report.sections),
        *(section.body for section in report.sections),
        *(claim.text for claim in report.claims),
    )
    return "\n".join(parts).lower()


def _host(url: str) -> str:
    """The host, lowercased, with a leading `www.` removed. See `source_diversity` on why this
    is the host rather than the registrable domain."""
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def _normalised(url: str) -> str:
    """A URL comparable with another. Scheme and host lowercased, a trailing slash on the path
    removed, and the fragment dropped - the differences that do not make a different page.

    The query string is kept: `?id=2` really is a different page.
    """
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}{path}{query}"
