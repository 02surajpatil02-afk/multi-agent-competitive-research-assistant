"""
WHY THIS FILE EXISTS
    ResearchState is the contract between agents, so the things worth testing about it are
    the ones a later change could quietly break: which fields exist, what a job starts
    with, which two fields accumulate, and the counting semantics of revision_count. The
    reducer test matters most - dropping operator.add from findings would lose a
    Researcher retry's evidence with no error anywhere.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

import operator
from datetime import UTC, datetime
from typing import Any, get_args, get_type_hints

import pytest
from pydantic import HttpUrl

from config import load_config
from graph.state import ResearchState, new_state
from schemas import Finding, QualityFlag, Verdict

# The field list from ARCHITECTURE.md §5. Adding a field to state is a design decision,
# not a detail - three were added at architecture review and nothing else was, because
# everything else the design needs is derivable from what is already here. This test
# forces the next addition to be deliberate.
_DOCUMENTED_FIELDS = {
    "job_id",
    "user_id",
    "question",
    "plan",
    "subtopic_status",
    "findings",
    "report",
    "verdicts",
    "reflection_scores",
    "failed_dimensions",
    "revision_count",
    "hop_count",
    "llm_calls_used",
    "quality_flag",
    "reviewer_edit_text",
    "status",
    "failure_reason",
}

_MINIMAL_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
}


def _state() -> ResearchState:
    return new_state(job_id="job-1", user_id="user-1", question="Compare A and B on cloud.")


def _hints() -> dict[str, Any]:
    return get_type_hints(ResearchState, include_extras=True)


def _verdict(claim_id: str) -> Verdict:
    return Verdict(claim_id=claim_id, supported=False, quote=None, note="source unreachable")


# --- Shape and defaults ------------------------------------------------------------


def test_state_holds_exactly_the_documented_fields() -> None:
    assert set(_hints()) == _DOCUMENTED_FIELDS


def test_a_new_state_sets_every_field() -> None:
    # A partially built state would make the Supervisor's first routing test - "is plan
    # None?" - depend on a key that might not be there.
    assert set(_state()) == _DOCUMENTED_FIELDS


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("plan", None),
        ("subtopic_status", {}),
        ("findings", []),
        ("report", None),
        ("verdicts", []),
        ("reflection_scores", []),
        ("failed_dimensions", []),
        ("revision_count", 0),
        ("hop_count", 0),
        ("llm_calls_used", 0),
        ("quality_flag", None),
        ("reviewer_edit_text", None),
        ("status", "running"),
        ("failure_reason", None),
    ],
)
def test_new_state_defaults(field: str, expected: object) -> None:
    assert dict(_state())[field] == expected


def test_the_job_identifiers_are_carried_through() -> None:
    state = _state()

    assert state["job_id"] == "job-1"
    assert state["user_id"] == "user-1"
    assert state["question"] == "Compare A and B on cloud."


def test_a_new_state_has_no_plan_so_the_first_hop_is_the_planner() -> None:
    # This is the Supervisor's first routing test, stated as a property of the start state.
    assert _state()["plan"] is None


# --- Reducers ----------------------------------------------------------------------


def test_only_findings_and_verdicts_accumulate() -> None:
    # Everything else is last-write-wins. A third reducer is a design change.
    accumulating = {name for name, hint in _hints().items() if hasattr(hint, "__metadata__")}

    assert accumulating == {"findings", "verdicts"}


@pytest.mark.parametrize("field", ["findings", "verdicts"])
def test_the_accumulating_fields_use_operator_add(field: str) -> None:
    (reducer,) = _hints()[field].__metadata__

    assert reducer is operator.add


def test_the_reducer_appends_rather_than_replacing() -> None:
    # This is the whole point: a Researcher step that times out and retries must not drop
    # the findings the first attempt produced.
    (reducer,) = _hints()["verdicts"].__metadata__
    first, second = _verdict("c1"), _verdict("c2")

    assert reducer([first], [second]) == [first, second]


def test_the_findings_reducer_accumulates_findings() -> None:
    # The type is the boundary: routing reads subtopic_status, never the findings, and a
    # Finding always carries the URL its evidence came from.
    finding = Finding(
        finding_id="f1",
        subtopic_id="s1",
        claim="Revenue grew.",
        evidence="Revenue grew 12% year on year.",
        url=HttpUrl("https://example.com/report"),
        title="Annual report",
        retrieved_at=datetime.now(UTC),
        content_hash="abc123",
        truncated=False,
    )
    (reducer,) = _hints()["findings"].__metadata__

    assert reducer([], [finding]) == [finding]


# --- revision_count semantics ------------------------------------------------------


def test_a_job_starts_at_revision_zero() -> None:
    # The initial report is pass 1 at revision_count 0. It is not a revision.
    assert _state()["revision_count"] == 0


def test_max_revisions_allows_two_cycles_and_three_passes() -> None:
    # Walks the documented loop: reflection starts a cycle only while the count is below
    # the cap, and the cap is checked with >=. Two cycles, three report-producing passes.
    config = load_config(_MINIMAL_ENV)
    state = _state()
    passes = 1  # the initial report

    while state["revision_count"] < config.max_revisions:
        state["revision_count"] += 1
        passes += 1

    assert config.max_revisions == 2
    assert state["revision_count"] == 2
    assert passes == 3


@pytest.mark.parametrize(("count", "capped"), [(0, False), (1, False), (2, True), (3, True)])
def test_the_cap_check_is_greater_than_or_equal(count: int, capped: bool) -> None:
    # With >, a MAX_REVISIONS of 2 would allow three cycles and four passes.
    config = load_config(_MINIMAL_ENV)

    assert (count >= config.max_revisions) is capped


# --- quality_flag --------------------------------------------------------------------


def test_quality_flag_starts_none_meaning_nothing_has_been_scored_yet() -> None:
    assert _state()["quality_flag"] is None


def test_the_flag_has_exactly_the_two_documented_values() -> None:
    assert set(get_args(QualityFlag)) == {"below_threshold", "unscored"}


def test_unscored_is_not_the_same_as_passed() -> None:
    # None means the rubric ran and the report passed; "unscored" means it never ran. A
    # consumer that treats the flag as a two-state boolean would collapse them, so None is
    # deliberately not a member of the literal - it is the third case.
    assert None not in get_args(QualityFlag)
    assert "unscored" in get_args(QualityFlag)


# --- reviewer_edit_text ----------------------------------------------------------------


def test_reviewer_edit_text_starts_unset() -> None:
    # Set once at the gate, consumed once by the next Synthesizer pass, then cleared. Only
    # the starting point is testable until those two nodes exist.
    assert _state()["reviewer_edit_text"] is None


def test_reviewer_edit_text_is_optional_so_it_can_be_cleared_after_use() -> None:
    assert type(None) in get_args(_hints()["reviewer_edit_text"])
