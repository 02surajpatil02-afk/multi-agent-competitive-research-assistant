"""
WHY THIS FILE EXISTS
    The judge is the only part of the evaluation subsystem that can cost money, so what is
    asserted here is mostly about what it does *not* do: it never runs unless it is asked for,
    it never raises, and it never reaches a real provider from a test. Every case below drives
    the real `Judge` over the real `LLMClient` with a `FakeOpenAI` underneath - the same
    arrangement `tests/test_llm_client.py` uses - so the validation retry, the transport
    backoff and the budget are the ones that ship.

    Three properties are the ones that would hurt if they broke.

    **A malformed judge answer costs one case its dimensions and nothing else.** That is the
    difference between an evaluation harness and a fragile script.

    **The rubric version travels with every outcome, including a failed one.** A score is only
    comparable against another score from the same rubric, and "which rubric produced this" has
    to survive into the report.

    **The report reaches the prompt inside an untrusted block.** It is written from pages other
    people wrote, and the judge gets the same treatment the reflection node gives the same text.

WHO CALLS IT
    pytest. No service, no network, no provider - `FakeOpenAI` answers every request.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fakes import FakeOpenAI, server_error, status_error, timeout_error
from openai import OpenAI

import llm_client
from config import Config, load_config
from eval.judge import (
    JUDGE_DIMENSIONS,
    JUDGE_MAX_REQUESTS_PER_CASE,
    JUDGE_RUBRIC_VERSION,
    JUDGE_TEMPERATURE,
    Judge,
    JudgeVerdict,
)
from eval.outputs import ClaimVerdict, ResearchOutput, RunMetadata
from eval.schema import EvalCase
from llm_client import LLMClient
from schemas import Claim, Finding, Report, Section, Source
from tools.untrusted import BEGIN_MARKER, END_MARKER

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "judge-model",
    "LLM_FAST_MODEL": "fast-model",
    "LLM_API_KEY": "key",
}

_SCORES = {dimension: 4 for dimension in JUDGE_DIMENSIONS}
_ANSWER = json.dumps({**_SCORES, "explanation": "Well sourced and on topic."})


def _config(**overrides: str) -> Config:
    return load_config({**_ENV, **overrides})


def _judge(*script: object) -> tuple[Judge, FakeOpenAI]:
    fake = FakeOpenAI(*script)
    client = LLMClient(_config(), client=cast(OpenAI, fake))
    return Judge(client, model="judge-model"), fake


def _output(**overrides: Any) -> ResearchOutput:
    report = Report(
        sections=[Section(id="sec1", heading="Cloud", body="TCS and Infosys both grew.")],
        claims=[
            Claim(claim_id="c1", section_id="sec1", text="TCS grew.", finding_ids=["f1"]),
        ],
        sources=[
            Source(
                url="https://a.example.com/one",  # type: ignore[arg-type]
                title="Annual report",
                finding_ids=["f1"],
            )
        ],
    )
    finding = Finding(
        finding_id="f1",
        subtopic_id="s1",
        claim="Cloud revenue grew.",
        evidence="Cloud revenue grew year on year.",
        url="https://a.example.com/one",  # type: ignore[arg-type]
        title="Annual report",
        retrieved_at=datetime(2026, 8, 18, 9, 30, tzinfo=UTC),
        content_hash="sha256-f1",
        truncated=False,
    )
    defaults: dict[str, Any] = {
        "question": "Compare TCS and Infosys on cloud strategy",
        "status": "approved",
        "report": report,
        "findings": (finding,),
        "verdicts": (ClaimVerdict("c1", supported=True),),
        "metadata": RunMetadata(job_id="job-1", thread_id="job-1"),
    }
    defaults.update(overrides)
    return ResearchOutput(**defaults)


def _case(**overrides: Any) -> EvalCase:
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


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Captures the client's backoff instead of serving it."""
    recorded: list[float] = []
    monkeypatch.setattr(llm_client, "sleep", recorded.append)
    return recorded


# --- 1. A scored case --------------------------------------------------------------------


def test_a_valid_answer_becomes_five_dimensions_and_an_explanation() -> None:
    judge, fake = _judge(_ANSWER)

    outcome = judge.score(_output(), _case())

    assert outcome.scored
    assert outcome.verdict is not None
    assert outcome.verdict.scores() == _SCORES
    assert outcome.verdict.explanation == "Well sourced and on topic."
    assert len(fake.completions.calls) == 1


def test_the_five_dimensions_stay_separate_in_the_serialised_outcome() -> None:
    # No blended judge score anywhere: "quality dropped 0.4" names nothing to act on.
    judge, _ = _judge(_ANSWER)

    body = judge.score(_output(), _case()).to_json()

    assert set(body["scores"]) == set(JUDGE_DIMENSIONS)
    assert "overall" not in body
    assert "score" not in body


def test_the_model_and_rubric_version_travel_with_the_outcome() -> None:
    judge, _ = _judge(_ANSWER)

    outcome = judge.score(_output(), _case())

    assert outcome.model == "judge-model"
    assert outcome.rubric_version == JUDGE_RUBRIC_VERSION


def test_a_caller_may_pin_a_different_rubric_version() -> None:
    fake = FakeOpenAI(_ANSWER)
    judge = Judge(
        LLMClient(_config(), client=cast(OpenAI, fake)),
        model="judge-model",
        rubric_version="eval-judge-v2-trial",
    )

    assert judge.score(_output(), _case()).rubric_version == "eval-judge-v2-trial"


# --- 2. The request it builds --------------------------------------------------------------


def test_the_judge_asks_at_temperature_zero() -> None:
    # A judge that scores one report differently on two runs cannot compare two runs.
    judge, fake = _judge(_ANSWER)

    judge.score(_output(), _case())

    assert fake.completions.calls[0]["temperature"] == JUDGE_TEMPERATURE == 0.0


def test_the_report_and_its_evidence_arrive_inside_one_untrusted_block() -> None:
    judge, fake = _judge(_ANSWER)

    judge.score(_output(), _case())

    user = str(fake.completions.calls[0]["messages"][1]["content"])
    assert user.count(BEGIN_MARKER) == 1
    assert user.count(END_MARKER) == 1
    inside = user.split(BEGIN_MARKER)[1].split(END_MARKER)[0]
    assert "Cloud revenue grew year on year." in inside  # the evidence
    assert "TCS grew." in inside  # the report's claim


def test_the_question_and_the_cases_expectations_are_outside_the_block() -> None:
    judge, fake = _judge(_ANSWER)

    judge.score(_output(), _case(required_entities=["TCS", "Infosys"]))

    user = str(fake.completions.calls[0]["messages"][1]["content"])
    before = user.split(BEGIN_MARKER)[0]
    assert "Compare TCS and Infosys on cloud strategy" in before
    assert "must name: TCS, Infosys" in before


def test_the_judge_is_not_told_the_counts_that_are_already_measured_exactly() -> None:
    # Showing a judge a number it cannot verify invites it to score the number.
    judge, fake = _judge(_ANSWER)

    judge.score(_output(), _case(min_sources=4, min_distinct_domains=3))

    user = str(fake.completions.calls[0]["messages"][1]["content"])
    assert "min_sources" not in user
    assert "min_distinct_domains" not in user


def test_the_judge_uses_the_main_tier() -> None:
    judge, fake = _judge(_ANSWER)

    judge.score(_output(), _case())

    assert fake.completions.calls[0]["model"] == "judge-model"


# --- 3. Failure is a result, never an exception --------------------------------------------


def test_a_malformed_answer_becomes_an_error_after_the_clients_one_retry() -> None:
    judge, fake = _judge("not json at all", '{"relevance": 9}')

    outcome = judge.score(_output(), _case())

    assert not outcome.scored
    assert outcome.error is not None and outcome.error.startswith("invalid_output")
    # The retry is the client's, and it really ran: two requests for one logical call.
    assert len(fake.completions.calls) == 2
    assert outcome.rubric_version == JUDGE_RUBRIC_VERSION


def test_a_malformed_answer_corrected_on_the_retry_still_scores() -> None:
    judge, fake = _judge("{}", _ANSWER)

    outcome = judge.score(_output(), _case())

    assert outcome.scored
    assert len(fake.completions.calls) == 2


def test_an_unreachable_endpoint_becomes_an_error_after_the_bounded_retries(
    slept: list[float],
) -> None:
    judge, _ = _judge(timeout_error(), server_error(), timeout_error())

    outcome = judge.score(_output(), _case())

    assert outcome.error is not None and outcome.error.startswith("llm_call_failed")
    # guidelines §17's main tier: two retries, at 2s and 8s. The judge adds no schedule of its
    # own, which is the whole point of going through `LLMClient`.
    assert slept == [2.0, 8.0]


def test_a_rejected_request_becomes_an_error_without_retrying() -> None:
    judge, fake = _judge(status_error(401))

    outcome = judge.score(_output(), _case())

    assert outcome.error is not None and "llm_call_failed" in outcome.error
    assert len(fake.completions.calls) == 1


def test_a_job_with_no_report_is_not_sent_to_the_judge_at_all() -> None:
    judge, fake = _judge()

    outcome = judge.score(_output(report=None), _case(expected_status="failed"))

    assert outcome.error == "there is no report to judge"
    assert fake.completions.calls == []


def test_one_case_never_spends_more_than_the_stated_request_ceiling(
    slept: list[float],
) -> None:
    # The backstop CLAUDE.md invariant 3 puts on a job, applied to one judged case.
    assert JUDGE_MAX_REQUESTS_PER_CASE == 6
    judge, fake = _judge(*[timeout_error()] * 3)

    judge.score(_output(), _case())

    assert len(fake.completions.calls) <= JUDGE_MAX_REQUESTS_PER_CASE
    assert slept == [2.0, 8.0]


# --- 4. The verdict schema ------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 6, -1])
def test_a_dimension_outside_one_to_five_is_refused(value: int) -> None:
    with pytest.raises(ValueError):
        JudgeVerdict.model_validate({**_SCORES, "relevance": value, "explanation": "x"})


def test_every_dimension_is_required() -> None:
    with pytest.raises(ValueError, match="faithfulness"):
        JudgeVerdict.model_validate(
            {key: 4 for key in JUDGE_DIMENSIONS if key != "faithfulness"} | {"explanation": "x"}
        )
