"""
WHY THIS FILE EXISTS
    Twelve deterministic metrics, each asserted at the boundary it is supposed to sit on:
    the pass side, the fail side, and the "this case says nothing about it" side. The third is
    the one worth being strict about - a metric that quietly scores 1.0 when a case states no
    expectation lifts every aggregate it appears in, and nothing in the report would show it.

    Two properties are asserted about the set rather than about any one metric.

    **Reproducibility.** The same output and the same case give the same twelve results, twice
    in a row and in a fresh evaluation. These are pure functions and that is what makes an eval
    number comparable across runs at all.

    **The pass rule always comes from somewhere visible.** Every `passed` here is traced in the
    test name to either a case field or a repository invariant, so a hidden threshold added
    later would have to break a test whose name says what it is protecting.

WHO CALLS IT
    pytest. No service, no network, no provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eval.metrics import (
    DETERMINISTIC_METRICS,
    METRIC_NAMES,
    MetricResult,
    citation_presence,
    claim_citation_coverage,
    claim_support_rate,
    duplicate_source_absence,
    evaluate_deterministic,
    expected_entity_coverage,
    forbidden_claim_absence,
    minimum_useful_output,
    required_fact_coverage,
    research_coverage,
    source_diversity,
    structured_output_validity,
    terminal_success,
)
from eval.outputs import ClaimVerdict, ResearchOutput, RunMetadata
from eval.schema import EvalCase
from schemas import Claim, Finding, Report, Section, Source

# --- Builders --------------------------------------------------------------------------


def a_finding(finding_id: str, url: str) -> Finding:
    return Finding(
        finding_id=finding_id,
        subtopic_id="s1",
        claim="Something was reported.",
        evidence="The source reports something.",
        url=url,  # type: ignore[arg-type]
        title=f"Source {finding_id}",
        retrieved_at=datetime(2026, 8, 18, 9, 30, tzinfo=UTC),
        content_hash=f"sha256-{finding_id}",
        truncated=False,
    )


def a_report(
    *,
    claims: list[tuple[str, str, list[str]]],
    source_urls: list[tuple[str, list[str]]],
    body: str = "TCS and Infosys both grew their cloud revenue.",
) -> Report:
    return Report(
        sections=[Section(id="sec1", heading="Cloud", body=body)],
        claims=[
            Claim(claim_id=cid, section_id="sec1", text=text, finding_ids=fids)
            for cid, text, fids in claims
        ],
        sources=[
            Source(url=url, title="Source", finding_ids=fids)  # type: ignore[arg-type]
            for url, fids in source_urls
        ],
    )


HEALTHY_REPORT = a_report(
    claims=[
        ("c1", "TCS grew its cloud revenue.", ["f1"]),
        ("c2", "Infosys grew its cloud revenue.", ["f2"]),
    ],
    source_urls=[("https://a.example.com/one", ["f1"]), ("https://b.example.org/two", ["f2"])],
)


def an_output(**overrides: Any) -> ResearchOutput:
    defaults: dict[str, Any] = {
        "question": "Compare TCS and Infosys on cloud strategy",
        "status": "approved",
        "report": HEALTHY_REPORT,
        "findings": (
            a_finding("f1", "https://a.example.com/one"),
            a_finding("f2", "https://b.example.org/two"),
        ),
        "verdicts": (
            ClaimVerdict("c1", supported=True),
            ClaimVerdict("c2", supported=True),
        ),
        "planned_subtopics": ("s1", "s2"),
        "subtopic_status": {"s1": "done", "s2": "done"},
        "metadata": RunMetadata(job_id="job-1", thread_id="job-1"),
    }
    defaults.update(overrides)
    return ResearchOutput(**defaults)


def a_case(**overrides: Any) -> EvalCase:
    defaults: dict[str, Any] = {
        "case_id": "cmp-example",
        "split": "dev",
        "question": "Compare TCS and Infosys on cloud strategy",
        "category": "company_comparison",
        "difficulty": "medium",
        "provenance": "synthetic_contract",
        "output_ref": "../fixtures/outputs/cmp-example.json",
        "expected_status": "approved",
    }
    defaults.update(overrides)
    return EvalCase.model_validate(defaults)


def _result(results: list[MetricResult], name: str) -> MetricResult:
    return next(result for result in results if result.metric == name)


# --- 1. terminal_success ----------------------------------------------------------------


def test_terminal_success_passes_when_the_status_is_the_one_the_case_expects() -> None:
    result = terminal_success(an_output(), a_case())

    assert (result.score, result.passed) == (1.0, True)


def test_terminal_success_fails_on_a_different_status() -> None:
    result = terminal_success(an_output(status="failed"), a_case())

    assert (result.score, result.passed) == (0.0, False)


def test_a_case_that_expects_a_failure_passes_when_the_job_failed() -> None:
    # The system behaving as documented must not read as a quality regression.
    output = an_output(status="failed", report=None, findings=(), verdicts=())
    result = terminal_success(output, a_case(expected_status="failed"))

    assert result.passed is True


# --- 2. structured_output_validity ------------------------------------------------------


def test_structured_output_validity_reads_the_loaders_errors() -> None:
    assert structured_output_validity(an_output(), a_case()).score == 1.0

    broken = an_output(report=None, schema_errors=("sources: too short",))
    result = structured_output_validity(broken, a_case())

    assert (result.score, result.passed) == (0.0, False)
    assert result.details["schema_errors"] == ["sources: too short"]


def test_a_job_with_no_report_at_all_emitted_nothing_invalid() -> None:
    result = structured_output_validity(an_output(report=None), a_case())

    assert result.passed is True


# --- 3. citation_presence ---------------------------------------------------------------


def test_citation_presence_needs_both_a_claim_and_a_source() -> None:
    assert citation_presence(an_output(), a_case()).passed is True

    result = citation_presence(an_output(report=None), a_case())
    assert (result.score, result.passed) == (0.0, False)


def test_citation_presence_does_not_apply_when_the_case_expects_no_export() -> None:
    # The one softening, and it comes from the case's `expected_status`, not from this metric.
    result = citation_presence(an_output(report=None), a_case(expected_status="failed"))

    assert (result.score, result.passed) == (None, None)


# --- 4. claim_citation_coverage ---------------------------------------------------------


def test_every_claim_reaching_a_source_passes() -> None:
    result = claim_citation_coverage(an_output(), a_case())

    assert (result.score, result.passed) == (1.0, True)


def test_one_uncited_claim_fails_even_though_the_others_are_cited() -> None:
    # CLAUDE.md invariant 1: an uncited claim does not get a warning, the export does not
    # happen. So this metric passes at 1.0 and nowhere below it.
    report = a_report(
        claims=[
            ("c1", "TCS grew its cloud revenue.", ["f1"]),
            ("c2", "A second thing was reported.", ["f9"]),
        ],
        source_urls=[("https://a.example.com/one", ["f1"])],
    )
    result = claim_citation_coverage(an_output(report=report), a_case())

    assert result.score == 0.5
    assert result.passed is False
    assert result.details["uncited_claims"] == ["c2"]


def test_a_claim_citing_a_finding_the_job_never_retrieved_is_reported() -> None:
    # ADR 0003's `report_cites_unknown_findings`. It is evidence rather than score: such a
    # claim may still reach a source, and this is the first thing to look at when it does not.
    report = a_report(
        claims=[("c1", "TCS grew.", ["f1", "f9"])],
        source_urls=[("https://a.example.com/one", ["f1"])],
    )
    result = claim_citation_coverage(an_output(report=report), a_case())

    assert result.score == 1.0
    assert result.details["claims_citing_unknown_findings"] == ["f9"]


def test_claim_citation_coverage_does_not_apply_without_a_report() -> None:
    assert claim_citation_coverage(an_output(report=None), a_case()).score is None


# --- 5. claim_support_rate --------------------------------------------------------------


def test_claim_support_rate_counts_only_the_claims_that_were_checked() -> None:
    # Unchecked and unsupported are different states; folding them together would make an
    # incomplete job look like a wrong one.
    output = an_output(
        verdicts=(
            ClaimVerdict("c1", supported=True),
            ClaimVerdict("c2", supported=False),
            ClaimVerdict("c3", supported=None),
        )
    )
    result = claim_support_rate(output, a_case())

    assert result.score == 0.5
    assert result.details == {
        "checked": 2,
        "unsupported_claims": ["c2"],
        "unchecked": 1,
        "max_unsupported_claims": None,
    }


def test_claim_support_rate_has_no_verdict_without_a_stated_ceiling() -> None:
    output = an_output(verdicts=(ClaimVerdict("c1", supported=False),))

    assert claim_support_rate(output, a_case()).passed is None
    assert claim_support_rate(output, a_case(max_unsupported_claims=0)).passed is False
    assert claim_support_rate(output, a_case(max_unsupported_claims=1)).passed is True


def test_claim_support_rate_does_not_apply_when_nothing_was_checked() -> None:
    assert claim_support_rate(an_output(verdicts=()), a_case()).score is None


# --- 6. required_fact_coverage ----------------------------------------------------------


def test_a_required_fact_matches_any_of_its_accepted_phrasings() -> None:
    case = a_case(
        required_facts=[
            {"id": "growth", "any_of": ["revenue from cloud services", "cloud revenue"]}
        ]
    )
    result = required_fact_coverage(an_output(), case)

    assert (result.score, result.passed) == (1.0, True)


def test_a_missing_fact_lowers_the_score_and_fails() -> None:
    case = a_case(
        required_facts=[
            {"id": "growth", "any_of": ["cloud revenue"]},
            {"id": "headcount", "any_of": ["headcount"]},
        ]
    )
    result = required_fact_coverage(an_output(), case)

    assert result.score == 0.5
    assert result.passed is False
    assert result.details["missing"] == ["headcount"]


def test_required_fact_coverage_does_not_apply_when_the_case_states_none() -> None:
    # Not 1.0. A "not applicable" that scored would lift every aggregate it is in.
    result = required_fact_coverage(an_output(), a_case())

    assert (result.score, result.passed) == (None, None)


# --- 7. expected_entity_coverage --------------------------------------------------------


def test_entity_coverage_is_case_insensitive_and_reports_what_is_missing() -> None:
    result = expected_entity_coverage(an_output(), a_case(required_entities=["tcs", "Wipro"]))

    assert result.score == 0.5
    assert result.passed is False
    assert result.details == {"named": ["tcs"], "missing": ["Wipro"]}


def test_naming_every_required_entity_passes() -> None:
    result = expected_entity_coverage(an_output(), a_case(required_entities=["TCS", "Infosys"]))

    assert (result.score, result.passed) == (1.0, True)


# --- 8. forbidden_claim_absence ---------------------------------------------------------


def test_a_forbidden_phrase_present_in_the_report_fails() -> None:
    report = a_report(
        claims=[("c1", "TCS grew.", ["f1"])],
        source_urls=[("https://a.example.com/one", ["f1"])],
        body="The evidence conclusively proves one product is better.",
    )
    result = forbidden_claim_absence(
        an_output(report=report), a_case(forbidden_claims=["conclusively proves"])
    )

    assert (result.score, result.passed) == (0.0, False)
    assert result.details["found"] == ["conclusively proves"]


def test_a_report_that_avoids_every_forbidden_phrase_passes() -> None:
    result = forbidden_claim_absence(an_output(), a_case(forbidden_claims=["conclusively proves"]))

    assert (result.score, result.passed) == (1.0, True)


def test_forbidden_claim_absence_does_not_apply_when_nothing_is_forbidden() -> None:
    assert forbidden_claim_absence(an_output(), a_case()).score is None


# --- 9. research_coverage ---------------------------------------------------------------


def test_research_coverage_counts_subtopics_that_produced_something() -> None:
    output = an_output(
        planned_subtopics=("s1", "s2", "s3"),
        subtopic_status={"s1": "done", "s2": "done", "s3": "unresearched"},
    )
    result = research_coverage(output, a_case())

    assert result.score == pytest.approx(2 / 3)
    assert result.details["unresearched"] == ["s3"]


def test_a_case_may_expect_a_gap_and_still_require_it_to_be_visible() -> None:
    # The documented behaviour of a job whose evidence ran out is to report the gap, so a case
    # that expects one passes on it - and a case that expects none fails on the same output.
    output = an_output(
        planned_subtopics=("s1", "s2"), subtopic_status={"s1": "done", "s2": "unresearched"}
    )

    assert research_coverage(output, a_case(expect_all_subtopics_researched=False)).passed is True
    assert research_coverage(output, a_case(expect_all_subtopics_researched=True)).passed is False


def test_research_coverage_does_not_apply_without_a_plan() -> None:
    assert research_coverage(an_output(planned_subtopics=()), a_case()).score is None


# --- 10. source_diversity ---------------------------------------------------------------


def test_source_diversity_counts_distinct_hosts_and_ignores_a_www_prefix() -> None:
    report = a_report(
        claims=[("c1", "TCS grew.", ["f1"])],
        source_urls=[
            ("https://www.example.com/one", ["f1"]),
            ("https://example.com/two", ["f1"]),
            ("https://other.example.org/three", ["f1"]),
        ],
    )
    result = source_diversity(an_output(report=report), a_case())

    assert result.details["distinct_hosts"] == 2
    assert result.score == pytest.approx(2 / 3)


def test_source_diversity_only_has_a_verdict_when_the_case_states_a_minimum() -> None:
    assert source_diversity(an_output(), a_case()).passed is None
    assert source_diversity(an_output(), a_case(min_distinct_domains=2)).passed is True
    assert source_diversity(an_output(), a_case(min_distinct_domains=3)).passed is False


# --- 11. duplicate_source_absence -------------------------------------------------------


def test_the_same_url_cited_twice_fails() -> None:
    report = a_report(
        claims=[("c1", "TCS grew.", ["f1"])],
        source_urls=[
            ("https://a.example.com/one", ["f1"]),
            ("https://a.example.com/one/", ["f1"]),
        ],
    )
    result = duplicate_source_absence(an_output(report=report), a_case())

    # A trailing slash does not make a different page, which is what normalisation is for.
    assert (result.score, result.passed) == (0.5, False)


def test_a_query_string_does_make_a_different_page() -> None:
    report = a_report(
        claims=[("c1", "TCS grew.", ["f1"])],
        source_urls=[
            ("https://a.example.com/one", ["f1"]),
            ("https://a.example.com/one?page=2", ["f1"]),
        ],
    )

    assert duplicate_source_absence(an_output(report=report), a_case()).passed is True


def test_the_same_page_fetched_twice_is_reported_and_not_scored() -> None:
    output = an_output(
        findings=(
            a_finding("f1", "https://a.example.com/one"),
            a_finding("f2", "https://a.example.com/one"),
        )
    )
    result = duplicate_source_absence(output, a_case())

    assert result.passed is True
    assert result.details["duplicate_finding_urls"] == {"https://a.example.com/one": 2}


# --- 12. minimum_useful_output ----------------------------------------------------------


def test_minimum_useful_output_defaults_to_needing_one_claim() -> None:
    assert minimum_useful_output(an_output(), a_case()).passed is True


def test_too_few_claims_against_the_cases_minimum_fails() -> None:
    result = minimum_useful_output(an_output(), a_case(min_claims=5))

    assert result.passed is False
    assert result.score == pytest.approx(2 / 3)
    assert result.details["enough_claims"] is False


def test_an_empty_section_body_fails() -> None:
    report = a_report(
        claims=[("c1", "TCS grew.", ["f1"])],
        source_urls=[("https://a.example.com/one", ["f1"])],
        body="   ",
    )
    result = minimum_useful_output(an_output(report=report), a_case())

    assert result.passed is False
    assert result.details["empty_sections"] == ["sec1"]


# --- The set ----------------------------------------------------------------------------


def test_every_metric_is_registered_once_and_named_by_its_function() -> None:
    assert len(METRIC_NAMES) == len(set(METRIC_NAMES)) == len(DETERMINISTIC_METRICS)
    assert "terminal_success" in METRIC_NAMES


def test_evaluating_the_same_output_twice_gives_identical_results() -> None:
    # Pure functions, which is what makes two eval runs comparable at all.
    output, case = an_output(), a_case(required_entities=["TCS"], min_distinct_domains=2)

    first = [result.to_json() for result in evaluate_deterministic(output, case)]
    second = [result.to_json() for result in evaluate_deterministic(output, case)]

    assert first == second


def test_a_failed_job_scores_no_report_metric_at_zero() -> None:
    # Twelve zeroes for a job that correctly failed would be twelve wrong numbers.
    output = an_output(status="failed", report=None, findings=(), verdicts=(), schema_errors=())
    results = evaluate_deterministic(output, a_case(expected_status="failed"))

    assert _result(results, "terminal_success").score == 1.0
    assert _result(results, "claim_citation_coverage").score is None
    assert _result(results, "citation_presence").score is None
    assert _result(results, "source_diversity").score is None
    assert _result(results, "minimum_useful_output").score is None


def test_no_metric_ever_returns_a_score_outside_zero_to_one() -> None:
    for output in (an_output(), an_output(report=None), an_output(status="failed")):
        for result in evaluate_deterministic(output, a_case(min_claims=9)):
            assert result.score is None or 0.0 <= result.score <= 1.0
