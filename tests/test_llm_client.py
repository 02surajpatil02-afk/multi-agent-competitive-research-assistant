"""
WHY THIS FILE EXISTS
    The client's job is to be boring under failure: retry the right number of times, wait
    the documented number of seconds, count every request, and fail loudly with a reason
    the caller can route on. None of that is visible from a happy-path run, so it is
    tested directly against a scripted fake endpoint - no network, no sleeping.

    Each test names the guidelines §17 row it pins, because these numbers are the ones a
    later change is most likely to drift.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

import json
import threading
from typing import cast

import pytest
from fakes import (
    FakeOpenAI,
    completion,
    rate_limit_error,
    server_error,
    status_error,
    timeout_error,
)
from openai import OpenAI

import llm_client
from config import Config, load_config
from llm_client import (
    JOB_FATAL_REASONS,
    MAX_RETRY_AFTER_S,
    CallBudget,
    LLMCallFailed,
    LLMClient,
    ModelTier,
)
from schemas import SupervisorDecision

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_FAST_MODEL": "fast-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
}

_VALID = json.dumps({"next": "planner", "reason": "no plan yet"})


def _config(**overrides: str) -> Config:
    return load_config({**_ENV, **overrides})


def _client(*script: object) -> tuple[LLMClient, FakeOpenAI]:
    fake = FakeOpenAI(*script)
    return LLMClient(_config(), client=cast(OpenAI, fake)), fake


def _ask(
    client: LLMClient,
    budget: CallBudget | None = None,
    tier: ModelTier = "main",
) -> SupervisorDecision:
    return client.call_structured(
        schema=SupervisorDecision,
        system="You route the graph.",
        user="What runs next?",
        budget=budget or CallBudget(limit=60),
        tier=tier,
    )


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Captures backoff instead of serving it, so the suite stays under a second."""
    recorded: list[float] = []
    monkeypatch.setattr(llm_client, "sleep", recorded.append)
    return recorded


# --- The happy path and the request it builds --------------------------------------


def test_a_valid_response_is_parsed_into_the_schema() -> None:
    client, fake = _client(_VALID)

    decision = _ask(client)

    assert decision.next == "planner"
    assert len(fake.completions.calls) == 1


def test_a_valid_response_costs_exactly_one_call() -> None:
    client, _ = _client(_VALID)
    budget = CallBudget(limit=60)

    _ask(client, budget)

    assert budget.used == 1


def test_json_mode_is_requested() -> None:
    # The client relies on native JSON mode; scripts/check_model.py is what confirms the
    # endpoint supports it before a model change ships.
    client, fake = _client(_VALID)

    _ask(client)

    assert fake.completions.calls[0]["response_format"] == {"type": "json_object"}


def test_the_system_prompt_carries_the_agent_text_and_the_schema() -> None:
    # The agent says what to ask; the client owns the JSON contract.
    client, fake = _client(_VALID)

    _ask(client)

    system = fake.completions.calls[0]["messages"][0]["content"]
    assert "You route the graph." in system
    assert "fact_checker" in system  # from the SupervisorDecision literal, via its schema


def test_the_main_tier_uses_the_main_model_and_its_timeout() -> None:
    client, fake = _client(_VALID)

    _ask(client, tier="main")

    assert fake.completions.calls[0]["model"] == "main-model"
    assert fake.completions.calls[0]["timeout"] == 60.0


def test_the_fast_tier_uses_the_fast_model_and_its_timeout() -> None:
    # The Supervisor and the reflection node are the only callers on this tier.
    client, fake = _client(_VALID)

    _ask(client, tier="fast")

    assert fake.completions.calls[0]["model"] == "fast-model"
    assert fake.completions.calls[0]["timeout"] == 30.0


def test_the_main_tier_timeout_is_configurable() -> None:
    # A slow endpoint raises it in its own environment. Step 12's third smoke job lost the
    # Synthesizer three times to the 60s default at ~15-20 output tokens/second.
    fake = FakeOpenAI(_VALID)
    config = _config(LLM_MAIN_TIMEOUT_S="120")

    _ask(LLMClient(config, client=cast(OpenAI, fake)))

    assert fake.completions.calls[0]["timeout"] == 120.0


def test_the_fast_tier_timeout_is_not_configurable() -> None:
    # Only the main row moves. Its callers send counters and receive a few tokens, so no
    # generation speed makes 30s tight - and one knob is easier to explain than two.
    fake = FakeOpenAI(_VALID)
    config = _config(LLM_MAIN_TIMEOUT_S="120")

    _ask(LLMClient(config, client=cast(OpenAI, fake)), tier="fast")

    assert fake.completions.calls[0]["timeout"] == 30.0


# --- Structured-output validation: one retry, carrying the error --------------------


def test_an_invalid_response_is_retried_once() -> None:
    client, fake = _client(json.dumps({"next": "reflection", "reason": "nope"}), _VALID)

    decision = _ask(client)

    assert decision.next == "planner"
    assert len(fake.completions.calls) == 2


def test_the_retry_carries_the_validation_error_into_the_prompt() -> None:
    # A retry that repeats the same prompt is a coin flip (guidelines §3).
    client, fake = _client(json.dumps({"next": "reflection", "reason": "nope"}), _VALID)

    _ask(client)

    retry_messages = fake.completions.calls[1]["messages"]
    assert retry_messages[-2]["role"] == "assistant"  # what the model actually said
    assert retry_messages[-1]["role"] == "user"
    assert "did not validate" in retry_messages[-1]["content"]
    assert "next" in retry_messages[-1]["content"]  # the offending field, from pydantic


def test_a_second_invalid_response_fails_explicitly() -> None:
    bad = json.dumps({"reason": "missing the next field"})
    client, fake = _client(bad, bad)

    with pytest.raises(LLMCallFailed) as raised:
        _ask(client)

    assert raised.value.reason == "invalid_output"
    assert len(fake.completions.calls) == 2  # one retry, not a loop
    assert raised.value.reason not in JOB_FATAL_REASONS  # the node fails, the job continues


def test_an_empty_completion_is_treated_as_invalid_output() -> None:
    # Not a transport problem: the validation layer is the one that can ask again better.
    client, fake = _client(completion(content=None), _VALID)

    decision = _ask(client)

    assert decision.next == "planner"
    assert len(fake.completions.calls) == 2


def test_each_validation_attempt_has_its_own_bounded_transport_retries(
    slept: list[float],
) -> None:
    """The retry layers are nested: validation does not share a transport counter."""
    invalid = json.dumps({"next": "reflection", "reason": "not an allowed route"})
    client, fake = _client(
        timeout_error(),
        timeout_error(),
        invalid,
        timeout_error(),
        timeout_error(),
        _VALID,
    )

    assert _ask(client).next == "planner"
    assert len(fake.completions.calls) == 6
    assert slept == [2.0, 8.0, 2.0, 8.0]


def test_validation_retries_are_counted_against_the_budget() -> None:
    bad = json.dumps({"reason": "missing the next field"})
    client, _ = _client(bad, _VALID)
    budget = CallBudget(limit=60)

    _ask(client, budget)

    assert budget.used == 2


# --- Transport failures: guidelines §17, main 2 retries at 2s/8s --------------------


def test_a_timeout_is_retried_on_the_main_schedule(slept: list[float]) -> None:
    client, fake = _client(timeout_error(), timeout_error(), _VALID)

    _ask(client)

    assert len(fake.completions.calls) == 3
    assert slept == [2.0, 8.0]


def test_the_fast_tier_backs_off_faster(slept: list[float]) -> None:
    client, _ = _client(timeout_error(), timeout_error(), _VALID)

    _ask(client, tier="fast")

    assert slept == [1.0, 4.0]


def test_a_server_error_is_retried_like_a_timeout(slept: list[float]) -> None:
    client, fake = _client(server_error(), _VALID)

    _ask(client)

    assert len(fake.completions.calls) == 2
    assert slept == [2.0]


def test_transport_retries_are_bounded_and_fail_the_node(slept: list[float]) -> None:
    client, fake = _client(timeout_error(), timeout_error(), timeout_error())

    with pytest.raises(LLMCallFailed) as raised:
        _ask(client)

    assert raised.value.reason == "llm_call_failed"
    assert raised.value.reason not in JOB_FATAL_REASONS
    assert len(fake.completions.calls) == 3
    assert slept == [2.0, 8.0]


def test_a_non_retryable_status_fails_immediately(slept: list[float]) -> None:
    # A bad key or an unknown model. Retrying cannot fix either, so do not spend budget.
    client, fake = _client(status_error(401))

    with pytest.raises(LLMCallFailed) as raised:
        _ask(client)

    assert raised.value.reason == "llm_call_failed"
    assert "401" in str(raised.value)
    assert len(fake.completions.calls) == 1
    assert slept == []


# --- 429: guidelines §17, 3 retries, Retry-After or 2s/8s/30s -----------------------


def test_a_429_honours_retry_after_when_the_endpoint_sends_it(slept: list[float]) -> None:
    client, _ = _client(rate_limit_error(retry_after="12"), _VALID)

    _ask(client)

    assert slept == [12.0]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("12", 12.0),
        (str(MAX_RETRY_AFTER_S), MAX_RETRY_AFTER_S),
        (str(MAX_RETRY_AFTER_S + 120), MAX_RETRY_AFTER_S),
    ],
)
def test_numeric_retry_after_is_honoured_only_through_the_policy_cap(
    header: str, expected: float, slept: list[float]
) -> None:
    client, _ = _client(rate_limit_error(retry_after=header), _VALID)

    _ask(client)

    assert slept == [expected]


def test_a_429_without_retry_after_uses_the_documented_schedule(slept: list[float]) -> None:
    client, _ = _client(rate_limit_error(), rate_limit_error(), rate_limit_error(), _VALID)

    _ask(client)

    assert slept == [2.0, 8.0, 30.0]


def test_an_unparseable_retry_after_falls_back_to_the_schedule(slept: list[float]) -> None:
    # The header may carry an HTTP date. There is a sane fallback, so do not parse it.
    client, _ = _client(rate_limit_error(retry_after="Wed, 12 Aug 2026 10:00:00 GMT"), _VALID)

    assert _ask(client).next == "planner"
    assert slept == [2.0]


@pytest.mark.parametrize("header", ["-1", "nan", "inf", "-inf"])
def test_an_invalid_numeric_retry_after_falls_back_to_the_schedule(
    header: str, slept: list[float]
) -> None:
    client, _ = _client(rate_limit_error(retry_after=header), _VALID)

    assert _ask(client).next == "planner"
    assert slept == [2.0]


def test_repeated_provider_retry_after_values_cannot_create_an_unbounded_wait(
    slept: list[float],
) -> None:
    client, _ = _client(
        rate_limit_error(retry_after="86400"),
        rate_limit_error(retry_after="86400"),
        rate_limit_error(retry_after="86400"),
        _VALID,
    )

    _ask(client)

    assert slept == [MAX_RETRY_AFTER_S] * 3


def test_rate_limiting_that_does_not_clear_fails_the_job(slept: list[float]) -> None:
    # A rate-limited job fails visibly. It does not silently produce a shorter report.
    client, fake = _client(*[rate_limit_error() for _ in range(4)])

    with pytest.raises(LLMCallFailed) as raised:
        _ask(client)

    assert raised.value.reason == "rate_limited"
    assert raised.value.reason in JOB_FATAL_REASONS
    assert len(fake.completions.calls) == 4  # 1 attempt + 3 retries
    assert slept == [2.0, 8.0, 30.0]


def test_the_two_retry_kinds_are_counted_independently(slept: list[float]) -> None:
    # A 429 must not consume a transport retry, or a flaky-and-busy endpoint gives up
    # earlier than either schedule says it should.
    client, fake = _client(
        rate_limit_error(), timeout_error(), rate_limit_error(), timeout_error(), _VALID
    )

    _ask(client)

    assert len(fake.completions.calls) == 5
    assert slept == [2.0, 2.0, 8.0, 8.0]


# --- The per-job call budget -------------------------------------------------------


def test_every_request_counts_including_retries(slept: list[float]) -> None:
    # 60 sits 16 above the worst case of 44 precisely as headroom for these.
    client, fake = _client(timeout_error(), rate_limit_error(), _VALID)
    budget = CallBudget(limit=60)

    _ask(client, budget)

    assert budget.used == len(fake.completions.calls) == 3


def test_an_exhausted_budget_stops_the_call_before_it_is_sent() -> None:
    client, fake = _client(_VALID)
    budget = CallBudget(limit=44, used=44)

    with pytest.raises(LLMCallFailed) as raised:
        _ask(client, budget)

    assert raised.value.reason == "budget_exceeded"
    assert raised.value.reason in JOB_FATAL_REASONS
    assert fake.completions.calls == []  # call 45 was never sent


def test_the_budget_bounds_the_retries_too(slept: list[float]) -> None:
    # The budget is the outer limit. A retry schedule cannot spend past it.
    client, fake = _client(timeout_error(), _VALID)
    budget = CallBudget(limit=1)

    with pytest.raises(LLMCallFailed) as raised:
        _ask(client, budget)

    assert raised.value.reason == "budget_exceeded"
    assert len(fake.completions.calls) == 1
    assert budget.used == 1


def test_a_budget_spends_up_to_its_limit_and_then_fails() -> None:
    budget = CallBudget(limit=2)

    budget.spend()
    budget.spend()

    assert budget.used == 2
    with pytest.raises(LLMCallFailed) as raised:
        budget.spend()
    assert raised.value.reason == "budget_exceeded"


def test_a_budget_resumes_from_the_calls_a_job_has_already_made() -> None:
    # A node builds this from state["llm_calls_used"] and writes `used` back afterwards.
    budget = CallBudget(limit=60, used=41)

    budget.spend()

    assert budget.used == 42


def test_a_budget_spent_from_several_threads_lands_on_exactly_its_limit() -> None:
    # Since ADR 0002 one budget is spent from a pool, so `spend()` is guarded.
    #
    # **What this test does and does not prove.** It pins the behaviour - 64 threads racing
    # for 50 tokens get 50 - and it is the test that fails first on a free-threaded build.
    # It does **not** prove the lock is what produced that result: on a GIL build the check
    # and the increment are a few bytecodes apart and the interpreter almost never switches
    # between them, so removing the lock still passes here. Measured, at switch intervals
    # from 5ms down to 100ns, across 20 trials each: zero over-grants without the lock.
    #
    # The lock is there because CPython does not promise not to switch there and PEP 703
    # removes the GIL that currently hides it, not because a test could provoke it today.
    # The guarantee that *is* deterministic today is behavioural and lives one level up:
    # test_agents_researcher.py::test_overlapping_extractions_cannot_overspend_the_job_budget.
    limit = 50
    threads = 64
    budget = CallBudget(limit=limit)
    start = threading.Barrier(threads, timeout=10.0)
    granted: list[bool] = []
    guard = threading.Lock()

    def claim() -> None:
        start.wait()
        try:
            budget.spend()
            allowed = True
        except LLMCallFailed:
            allowed = False
        with guard:
            granted.append(allowed)

    workers = [threading.Thread(target=claim) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10.0)

    assert sum(granted) == limit
    assert budget.used == limit


# --- Contract details worth pinning ------------------------------------------------


def test_the_reasons_that_fail_the_whole_job_are_the_two_documented_ones() -> None:
    assert JOB_FATAL_REASONS == {"rate_limited", "budget_exceeded"}


def test_the_sdk_does_not_retry_underneath_us() -> None:
    # Left at its default the SDK would multiply every schedule in guidelines §17 and
    # spend budget that nothing counted.
    client = LLMClient(_config())

    assert client._client.max_retries == 0
    assert str(client._client.base_url).startswith("https://example.invalid/v1")


# --- The shared rate limiter (guidelines §11, §17) ----------------------------------
#
# The bucket itself lives in `redisstore.py` and is proven against real Redis in the
# `redis`-marked layer. What belongs here is the half this file owns: when a token is
# taken, what happens when one cannot be, and the bound on trying.


class _Limiter:
    """A scripted `RateLimiter`. `answers` is consumed in order, then it grants forever."""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.calls = 0

    def acquire(self) -> bool:
        self.calls += 1
        return self.answers.pop(0) if self.answers else True


def _limited(limiter: _Limiter, *script: object) -> tuple[LLMClient, FakeOpenAI]:
    fake = FakeOpenAI(*script)
    return LLMClient(_config(), client=cast(OpenAI, fake), limiter=limiter), fake


def test_a_token_is_taken_before_every_request() -> None:
    limiter = _Limiter()
    client, fake = _limited(limiter, _VALID)

    _ask(client)

    assert limiter.calls == 1
    assert len(fake.completions.calls) == 1


def test_no_token_means_no_request_at_all(slept: list[float]) -> None:
    """guidelines §11's rule, asserted on the thing that matters: the endpoint was never
    called. A limiter that let the request through after giving up would be decoration."""
    limiter = _Limiter(False, False, False)
    client, fake = _limited(limiter, _VALID)

    with pytest.raises(LLMCallFailed) as raised:
        _ask(client)

    assert raised.value.reason == "rate_limiter_unavailable"
    assert fake.completions.calls == []  # nothing was sent


def test_the_limiter_retries_on_the_documented_schedule(slept: list[float]) -> None:
    # guidelines §17's rate-limiter row: two retries, at 2s and 8s. Distinct from the 429
    # schedule above, which answers the endpoint rather than our own bucket.
    limiter = _Limiter(False, False, False)
    client, _ = _limited(limiter, _VALID)

    with pytest.raises(LLMCallFailed):
        _ask(client)

    assert slept == [2.0, 8.0]
    assert limiter.calls == 3  # one attempt, then two retries


def test_a_token_that_arrives_on_a_retry_sends_the_request(slept: list[float]) -> None:
    # The retry exists to be useful: a bucket that opens during the backoff proceeds
    # normally, and the job never learns there was a wait.
    limiter = _Limiter(False, True)
    client, fake = _limited(limiter, _VALID)

    decision = _ask(client)

    assert decision.next == "planner"
    assert slept == [2.0]
    assert len(fake.completions.calls) == 1


def test_a_transport_retry_spends_a_second_token(slept: list[float]) -> None:
    """**The decisive test for "does a retry consume a token?" - yes.**

    A retried request is a real request against the same 40 RPM tier, which is the argument
    `CallBudget` already makes for counting every attempt. A limiter that counted logical
    calls while the budget counted attempts would let a job with two transport retries send
    three requests on one token.
    """
    limiter = _Limiter()
    budget = CallBudget(limit=60)
    client, _ = _limited(limiter, timeout_error(), timeout_error(), _VALID)

    _ask(client, budget)

    assert limiter.calls == 3
    assert budget.used == 3  # the two counters agree, request for request


def test_the_limiter_is_asked_after_the_budget_is_spent() -> None:
    """Order matters at the ceiling: a job that has exhausted `MAX_LLM_CALLS_PER_JOB` must
    not take a token from a bucket every other job is sharing, for a request it will never
    be allowed to send."""
    limiter = _Limiter()
    client, fake = _limited(limiter, _VALID)

    with pytest.raises(LLMCallFailed) as raised:
        _ask(client, CallBudget(limit=1, used=1))

    assert raised.value.reason == "budget_exceeded"
    assert limiter.calls == 0
    assert fake.completions.calls == []


def test_no_limiter_at_all_sends_the_request() -> None:
    # Phase 1's behaviour, and what the offline suite and `scripts/measure_jobs.py` run on:
    # there is no bucket, so there is no token to wait for.
    client, fake = _client(_VALID)

    _ask(client)

    assert len(fake.completions.calls) == 1


def test_a_limiter_failure_fails_the_node_not_the_job() -> None:
    # guidelines §17 says "fail the node". A limiter that is unreachable now may answer on
    # the next node, unlike a rate-limited endpoint, which has already spoken for the job.
    assert "rate_limiter_unavailable" not in JOB_FATAL_REASONS
