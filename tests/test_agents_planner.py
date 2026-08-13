"""
WHY THIS FILE EXISTS
    The Planner has one visible job - produce a plan - and one invisible one: refuse to let
    a job start on a plan that cannot be researched or evaluated. These tests pin the
    refusals, because the happy path is the part that would never regress silently.

    The seeding test matters more than it looks. Without one "pending" entry per subtopic,
    the Supervisor's next test finds nothing pending, decides every subtopic is resolved,
    and routes a job with no findings straight to the Synthesizer.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

import json
from typing import cast

import pytest
from fakes import FakeOpenAI, imported_modules, rate_limit_error, status_error
from openai import OpenAI

import agents.planner
import llm_client
from agents.planner import PlannerUpdate, plan_research
from config import Config, load_config
from graph.state import ResearchState, new_state
from llm_client import LLMClient
from schemas import ResearchPlan

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_FAST_MODEL": "fast-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
}

_OWNED = set(PlannerUpdate.__annotations__)


def _config(**overrides: str) -> Config:
    return load_config({**_ENV, **overrides})


def _llm(*script: object) -> tuple[LLMClient, FakeOpenAI]:
    fake = FakeOpenAI(*script)
    return LLMClient(_config(), client=cast(OpenAI, fake)), fake


def _state(**overrides: object) -> ResearchState:
    state = new_state(
        job_id="job-1", user_id="user-1", question="Compare TCS and Infosys on cloud."
    )
    state.update(cast(ResearchState, overrides))
    return state


def _plan(*ids: str, criteria: list[str] | None = None) -> str:
    return json.dumps(
        {
            "subtopics": [
                {"id": name, "question": f"What about {name}?", "search_query": name}
                for name in ids
            ],
            "success_criteria": criteria if criteria is not None else ["Cites public sources"],
        }
    )


_VALID = _plan("s1", "s2", "s3")


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(llm_client, "sleep", recorded.append)
    return recorded


# --- A valid plan --------------------------------------------------------------------


def test_a_valid_plan_is_returned_as_a_research_plan() -> None:
    llm, _ = _llm(_VALID)

    update = plan_research(_state(), config=_config(), llm=llm)

    plan = update["plan"]
    assert isinstance(plan, ResearchPlan)
    assert [subtopic.id for subtopic in plan.subtopics] == ["s1", "s2", "s3"]
    assert plan.success_criteria == ["Cites public sources"]


def test_every_planned_subtopic_starts_pending() -> None:
    # The Supervisor routes on this dict, so an unseeded subtopic is a subtopic nobody
    # researches.
    llm, _ = _llm(_VALID)

    update = plan_research(_state(), config=_config(), llm=llm)

    assert update["subtopic_status"] == {"s1": "pending", "s2": "pending", "s3": "pending"}


def test_the_question_is_what_gets_planned() -> None:
    llm, fake = _llm(_VALID)

    plan_research(_state(question="What has Zoho launched?"), config=_config(), llm=llm)

    assert "What has Zoho launched?" in fake.completions.calls[0]["messages"][1]["content"]


def test_a_plan_costs_one_call_and_is_counted() -> None:
    llm, fake = _llm(_VALID)

    update = plan_research(_state(llm_calls_used=4), config=_config(), llm=llm)

    assert len(fake.completions.calls) == 1
    assert update["llm_calls_used"] == 5


def test_the_planner_writes_only_the_fields_it_owns() -> None:
    # ARCHITECTURE.md §5 gives every field an owner. A Planner that wrote findings or a
    # report would be a contract violation even if the plan came out fine.
    llm, _ = _llm(_VALID)

    update = plan_research(_state(), config=_config(), llm=llm)

    assert set(update) <= _OWNED
    assert set(update) == {"plan", "subtopic_status", "llm_calls_used"}


def test_a_successful_plan_leaves_the_job_running() -> None:
    llm, _ = _llm(_VALID)

    assert "status" not in plan_research(_state(), config=_config(), llm=llm)


# --- The 3-5 subtopic constraint -----------------------------------------------------


@pytest.mark.parametrize("ids", [("s1", "s2"), ("s1", "s2", "s3", "s4", "s5", "s6")])
def test_a_plan_outside_three_to_five_subtopics_is_rejected(ids: tuple[str, ...]) -> None:
    # Enforced by the schema, so it costs the documented validation retry rather than a
    # check the Planner would have to remember to make.
    llm, fake = _llm(_plan(*ids), _plan(*ids))

    update = plan_research(_state(), config=_config(), llm=llm)

    assert update["status"] == "failed"
    assert update["failure_reason"] == "invalid_output"
    assert len(fake.completions.calls) == 2


@pytest.mark.parametrize("ids", [("s1", "s2", "s3"), ("s1", "s2", "s3", "s4", "s5")])
def test_three_and_five_subtopics_are_both_accepted(ids: tuple[str, ...]) -> None:
    llm, _ = _llm(_plan(*ids))

    update = plan_research(_state(), config=_config(), llm=llm)

    assert len(update["plan"].subtopics) == len(ids)


def test_a_plan_with_no_success_criteria_is_rejected() -> None:
    # There would be nothing for reflection or the eval set to score the report against.
    empty = _plan("s1", "s2", "s3", criteria=[])
    llm, _ = _llm(empty, empty)

    assert plan_research(_state(), config=_config(), llm=llm)["status"] == "failed"


def test_duplicate_subtopic_ids_fail_the_job() -> None:
    # Two subtopics sharing an id collapse to one status entry, and every Finding written
    # under it is attributed to the wrong subtopic.
    llm, _ = _llm(_plan("s1", "s1", "s2"))

    update = plan_research(_state(), config=_config(), llm=llm)

    assert update["status"] == "failed"
    assert update["failure_reason"] == "invalid_plan"
    assert "plan" not in update


# --- Invalid structured output, through the client's one retry -----------------------


def test_invalid_output_is_retried_once_and_then_accepted() -> None:
    llm, fake = _llm(_plan("s1", "s2"), _VALID)

    update = plan_research(_state(), config=_config(), llm=llm)

    assert [subtopic.id for subtopic in update["plan"].subtopics] == ["s1", "s2", "s3"]
    assert len(fake.completions.calls) == 2
    assert update["llm_calls_used"] == 2


def test_the_retry_is_the_client_s_and_carries_the_error() -> None:
    # The Planner supplies a schema and a prompt; retrying and explaining the failure is
    # the client's job and is not re-implemented here.
    llm, fake = _llm("not json at all", _VALID)

    plan_research(_state(), config=_config(), llm=llm)

    assert "did not validate" in fake.completions.calls[1]["messages"][-1]["content"]


def test_output_that_never_validates_fails_the_job() -> None:
    llm, fake = _llm("not json", "still not json")

    update = plan_research(_state(), config=_config(), llm=llm)

    assert update["status"] == "failed"
    assert update["failure_reason"] == "invalid_output"
    assert update["llm_calls_used"] == 2
    assert len(fake.completions.calls) == 2  # one retry, not a loop


# --- Failure behaviour ---------------------------------------------------------------


def test_a_rate_limited_planner_fails_the_job(slept: list[float]) -> None:
    llm, _ = _llm(*[rate_limit_error() for _ in range(4)])

    update = plan_research(_state(), config=_config(), llm=llm)

    assert update["status"] == "failed"
    assert update["failure_reason"] == "rate_limited"


def test_an_exhausted_budget_fails_the_job_without_a_call() -> None:
    llm, fake = _llm(_VALID)

    update = plan_research(_state(llm_calls_used=60), config=_config(), llm=llm)

    assert update["status"] == "failed"
    assert update["failure_reason"] == "budget_exceeded"
    assert fake.completions.calls == []


def test_an_unreachable_endpoint_fails_the_job() -> None:
    llm, _ = _llm(status_error(401))

    update = plan_research(_state(), config=_config(), llm=llm)

    assert update["status"] == "failed"
    assert update["failure_reason"] == "llm_call_failed"


def test_a_failed_plan_never_leaves_a_partial_plan_behind() -> None:
    # Researching an unplanned question produces a report nobody can evaluate.
    llm, _ = _llm("not json", "still not json")

    update = plan_research(_state(), config=_config(), llm=llm)

    assert "plan" not in update
    assert "subtopic_status" not in update


# --- The Planner has no tools --------------------------------------------------------


def test_the_planner_cannot_reach_the_web() -> None:
    imports = imported_modules(agents.planner)

    assert "tools" not in imports
    assert not {"httpx", "tavily"} & imports


def test_the_planner_makes_exactly_one_kind_of_call() -> None:
    llm, fake = _llm(_VALID)

    plan_research(_state(), config=_config(), llm=llm)

    assert [call["model"] for call in fake.completions.calls] == ["main-model"]
