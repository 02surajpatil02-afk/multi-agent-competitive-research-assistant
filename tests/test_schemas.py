"""
WHY THIS FILE EXISTS
    The schemas carry invariants the whole system leans on: a report is grounded, a claim
    can reach a source URL, and a "supported" verdict has a quote behind it. These are
    contract tests for those invariants (guidelines §18) - each one asserts that an
    invalid object is rejected, because the point of a schema here is to turn a bad model
    response into a bounded retry and then a visible failure.

    Rejection cases go through model_validate on a dict, which is how these objects
    actually arrive: parsed JSON from a structured LLM call.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import HttpUrl, ValidationError

from schemas import (
    MAX_SEARCH_QUERY_CHARS,
    Claim,
    Finding,
    ReflectionScore,
    Report,
    ResearchPlan,
    SearchResult,
    Section,
    Source,
    Subtopic,
    SupervisorDecision,
    Verdict,
)


def _subtopics(count: int) -> list[Subtopic]:
    return [
        Subtopic(id=f"s{n}", question=f"question {n}", search_query=f"query {n}")
        for n in range(count)
    ]


def _claim(claim_id: str = "c1") -> Claim:
    return Claim(claim_id=claim_id, section_id="sec1", text="A claim.", finding_ids=["f1"])


def _source() -> Source:
    return Source(url=HttpUrl("https://example.com/a"), title="A page", finding_ids=["f1"])


def _finding_fields(**overrides: Any) -> dict[str, Any]:
    return {
        "finding_id": "f1",
        "subtopic_id": "s1",
        "claim": "Revenue grew.",
        "evidence": "Revenue grew 12% year on year.",
        "url": "https://example.com/report",
        "title": "Annual report",
        "retrieved_at": datetime.now(UTC),
        "content_hash": "abc123",
        "truncated": False,
        **overrides,
    }


def _score_fields(**overrides: Any) -> dict[str, Any]:
    return {
        "research_completeness": 4,
        "source_correctness": 4,
        "citation_coverage": 5,
        "factual_consistency": 4,
        "report_quality": 4,
        "rationale": "Solid coverage.",
        "weighted_score": 4.2,
        "failed_dimensions": [],
        "route": "human_gate",
        **overrides,
    }


# --- Planner -----------------------------------------------------------------------


@pytest.mark.parametrize("count", [3, 4, 5])
def test_a_plan_of_three_to_five_subtopics_is_accepted(count: int) -> None:
    plan = ResearchPlan(subtopics=_subtopics(count), success_criteria=["names both firms"])

    assert len(plan.subtopics) == count


@pytest.mark.parametrize("count", [0, 2, 6])
def test_a_plan_outside_three_to_five_subtopics_is_rejected(count: int) -> None:
    with pytest.raises(ValidationError):
        ResearchPlan(subtopics=_subtopics(count), success_criteria=["something"])


def test_a_subtopic_needs_a_search_query() -> None:
    # It is what reaches the search tool, so an absent one would silently fall back to
    # something - and the something used to be the 150-character question.
    with pytest.raises(ValidationError):
        Subtopic(id="s1", question="What is TCS cloud revenue?")  # type: ignore[call-arg]


@pytest.mark.parametrize("query", ["", "x" * (MAX_SEARCH_QUERY_CHARS + 1)])
def test_a_search_query_outside_its_bounds_is_rejected(query: str) -> None:
    # The cap is what stops a natural-language sentence being sent as a query. Validation
    # rather than truncation, because cutting mid-word makes a different, worse query.
    with pytest.raises(ValidationError):
        Subtopic(id="s1", question="q", search_query=query)


def test_a_search_query_at_the_cap_is_accepted() -> None:
    subtopic = Subtopic(id="s1", question="q", search_query="x" * MAX_SEARCH_QUERY_CHARS)

    assert len(subtopic.search_query) == MAX_SEARCH_QUERY_CHARS


def test_a_plan_without_success_criteria_is_rejected() -> None:
    # There would be nothing for reflection or the offline eval to score against.
    with pytest.raises(ValidationError):
        ResearchPlan(subtopics=_subtopics(3), success_criteria=[])


# --- Researcher --------------------------------------------------------------------


def test_a_finding_carries_its_evidence_and_the_url_it_came_from() -> None:
    finding = Finding.model_validate(_finding_fields())

    assert str(finding.url) == "https://example.com/report"
    assert finding.evidence == "Revenue grew 12% year on year."


@pytest.mark.parametrize(
    "missing", ["url", "evidence", "content_hash", "truncated", "retrieved_at"]
)
def test_a_finding_missing_an_audit_field_is_rejected(missing: str) -> None:
    fields = _finding_fields()
    del fields[missing]

    with pytest.raises(ValidationError):
        Finding.model_validate(fields)


def test_a_finding_url_must_be_a_url() -> None:
    with pytest.raises(ValidationError):
        Finding.model_validate(_finding_fields(url="not-a-url"))


# --- Synthesizer -------------------------------------------------------------------


def test_a_report_with_no_sources_is_rejected() -> None:
    # Empty sources means the report is ungrounded. That is a failure, not a result.
    with pytest.raises(ValidationError):
        Report(sections=[Section(id="sec1", heading="H", body="B")], claims=[_claim()], sources=[])


def test_a_grounded_report_is_accepted() -> None:
    report = Report(
        sections=[Section(id="sec1", heading="H", body="B")],
        claims=[_claim()],
        sources=[_source()],
    )

    assert len(report.sources) == 1


def test_a_claim_with_no_finding_ids_is_rejected() -> None:
    # finding_ids is the audit link that becomes a claim_sources row. Without it the
    # export gate has nothing to check.
    with pytest.raises(ValidationError):
        Claim(claim_id="c1", section_id="sec1", text="A claim.", finding_ids=[])


def test_a_source_with_no_finding_ids_is_rejected() -> None:
    # Report.sources is a view over the findings actually cited, so a source that no
    # finding retrieved is exactly the drift the audit trail exists to prevent.
    with pytest.raises(ValidationError):
        Source(url=HttpUrl("https://example.com/a"), title="A page", finding_ids=[])


# --- Fact-Checker ------------------------------------------------------------------


def test_a_supported_verdict_without_a_quote_is_rejected() -> None:
    with pytest.raises(ValidationError, match="verbatim quote"):
        Verdict(claim_id="c1", supported=True, quote=None, note="looks right")


def test_a_supported_verdict_with_a_blank_quote_is_rejected() -> None:
    # Whitespace is not a quote. It is the same violation with characters in it.
    with pytest.raises(ValidationError, match="verbatim quote"):
        Verdict(claim_id="c1", supported=True, quote="   ", note="looks right")


def test_a_supported_verdict_with_a_quote_is_accepted() -> None:
    verdict = Verdict(
        claim_id="c1",
        supported=True,
        quote="Revenue grew 12% year on year.",
        note="matches the cited paragraph",
    )

    assert verdict.supported is True


def test_an_unsupported_verdict_needs_no_quote() -> None:
    # This is the documented outcome for an unreachable source - never a guess.
    verdict = Verdict(claim_id="c1", supported=False, quote=None, note="source unreachable")

    assert verdict.quote is None


# --- Supervisor --------------------------------------------------------------------


@pytest.mark.parametrize(
    "target", ["planner", "researcher", "synthesizer", "fact_checker", "finalize"]
)
def test_the_supervisor_may_route_to_each_documented_target(target: str) -> None:
    decision = SupervisorDecision.model_validate({"next": target, "reason": "because"})

    assert decision.next == target


@pytest.mark.parametrize("target", ["reflection", "human_gate", "export", "anything"])
def test_the_supervisor_cannot_route_outside_the_transition_table(target: str) -> None:
    # reflection especially: the graph reaches it by a fixed edge after the Fact-Checker,
    # which is what keeps it control flow rather than a delegation.
    with pytest.raises(ValidationError):
        SupervisorDecision.model_validate({"next": target, "reason": "because"})


# --- Reflection node ---------------------------------------------------------------


def test_a_complete_reflection_score_is_accepted() -> None:
    score = ReflectionScore.model_validate(_score_fields())

    assert score.route == "human_gate"


@pytest.mark.parametrize(
    "dimension",
    [
        "research_completeness",
        "source_correctness",
        "citation_coverage",
        "factual_consistency",
        "report_quality",
    ],
)
def test_a_reflection_score_missing_a_dimension_is_rejected(dimension: str) -> None:
    fields = _score_fields()
    del fields[dimension]

    with pytest.raises(ValidationError):
        ReflectionScore.model_validate(fields)


@pytest.mark.parametrize("value", [0, 6, -1])
def test_a_dimension_outside_one_to_five_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        ReflectionScore.model_validate(_score_fields(citation_coverage=value))


@pytest.mark.parametrize("route", ["planner", "finalize", "reflection", "export"])
def test_a_reflection_route_outside_its_table_is_rejected(route: str) -> None:
    # Reflection routes to a specialist or to the gate. It never routes to the Planner and
    # never emits a SupervisorDecision.
    with pytest.raises(ValidationError):
        ReflectionScore.model_validate(_score_fields(route=route))


@pytest.mark.parametrize("route", ["researcher", "synthesizer", "fact_checker", "human_gate"])
def test_each_documented_reflection_route_is_accepted(route: str) -> None:
    assert ReflectionScore.model_validate(_score_fields(route=route)).route == route


def test_a_weighted_score_outside_the_rubric_range_is_rejected() -> None:
    # The weights sum to 1.0 over 1-5 scores, so anything outside 1-5 is a weighting bug.
    with pytest.raises(ValidationError):
        ReflectionScore.model_validate(_score_fields(weighted_score=7.0))


# --- Tool boundary -----------------------------------------------------------------


def test_a_search_result_may_have_no_publication_date() -> None:
    # Plenty of pages do not publish one, and that is not a reason to drop the source.
    result = SearchResult(title="A page", url=HttpUrl("https://example.com/a"), content="text")

    assert result.published_at is None
