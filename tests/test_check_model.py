"""
WHY THIS FILE EXISTS
    The preflight is only useful if it reports a failure rather than becoming one. These
    tests drive each check against a scripted endpoint and assert the verdict, including
    the cases that matter most: an endpoint that accepts `response_format` but ignores it,
    and one that accepts `tools` but answers with prose anyway. Both look like success at
    the HTTP layer and would break every agent.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from fakes import FakeOpenAI, completion, status_error
from openai import OpenAI

from config import Config, load_config
from scripts.check_model import (
    check_endpoint_answers,
    check_json_mode,
    check_tool_calling,
    main,
    measure_throughput,
    run_checks,
)

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
}


def _config(**overrides: str) -> Config:
    return load_config({**_ENV, **overrides})


def _openai(*script: object) -> OpenAI:
    return cast(OpenAI, FakeOpenAI(*script))


# --- Does the endpoint answer at all? ----------------------------------------------


def test_an_answering_endpoint_passes() -> None:
    result = check_endpoint_answers(_openai("ok"), "main-model")

    assert result.ok
    assert "ok" in result.detail


def test_a_rejected_request_is_reported_not_raised() -> None:
    # A bad key must produce a readable line, not a traceback halfway through the run.
    result = check_endpoint_answers(_openai(status_error(401)), "main-model")

    assert not result.ok
    assert result.detail


def test_an_empty_completion_fails_the_check() -> None:
    result = check_endpoint_answers(_openai(completion(content=None)), "main-model")

    assert not result.ok
    assert "empty" in result.detail


# --- JSON mode ---------------------------------------------------------------------


def test_json_mode_passes_when_the_reply_parses() -> None:
    result = check_json_mode(_openai('{"status": "ok"}'), "main-model")

    assert result.ok


def test_json_mode_fails_when_response_format_is_accepted_but_ignored() -> None:
    # The dangerous case: the call succeeds and the reply is prose. Every structured
    # agent call would then burn its validation retry and fail.
    result = check_json_mode(_openai("Sure! Here you go."), "main-model")

    assert not result.ok
    assert "Sure" in result.detail


def test_json_mode_fails_when_response_format_is_rejected() -> None:
    result = check_json_mode(_openai(status_error(400)), "main-model")

    assert not result.ok


def test_json_mode_requests_response_format() -> None:
    fake = FakeOpenAI('{"status": "ok"}')

    check_json_mode(cast(OpenAI, fake), "main-model")

    assert fake.completions.calls[0]["response_format"] == {"type": "json_object"}


# --- Tool calling ------------------------------------------------------------------


def test_tool_calling_passes_when_the_model_calls_the_tool() -> None:
    result = check_tool_calling(_openai(completion(tool_called="ping")), "main-model")

    assert result.ok
    assert "ping" in result.detail


def test_tool_calling_fails_when_the_model_replies_with_text_instead() -> None:
    result = check_tool_calling(_openai("I would call ping."), "main-model")

    assert not result.ok
    assert "without a tool call" in result.detail


def test_tool_calling_fails_when_tools_are_rejected() -> None:
    result = check_tool_calling(_openai(status_error(400)), "main-model")

    assert not result.ok


# --- Throughput --------------------------------------------------------------------


def test_throughput_reports_the_numbers_and_the_configured_limit() -> None:
    result = measure_throughput(_openai(*["ok"] * 5), "main-model", configured_rpm=40)

    assert result.ok
    assert "5 sequential requests" in result.detail
    assert "LLM_RPM_LIMIT is 40" in result.detail


def test_throughput_fails_and_says_which_request_broke() -> None:
    result = measure_throughput(_openai("ok", "ok", status_error(500)), "main-model", 40)

    assert not result.ok
    assert "request 3 of 5" in result.detail


# --- The whole run -----------------------------------------------------------------


def test_one_model_gets_three_capability_checks_and_one_measurement() -> None:
    fake = _openai(*["ok", '{"status": "ok"}', completion(tool_called="ping")] + ["ok"] * 5)

    results = run_checks(fake, _config())

    assert len(results) == 4
    assert all(result.ok for result in results)


def test_a_distinct_fast_model_is_checked_too() -> None:
    # LLM_FAST_MODEL runs the Supervisor and the reflection node, so it needs the same
    # capabilities. When it falls back to LLM_MODEL there is nothing extra to check.
    script = ["ok", '{"status": "ok"}', completion(tool_called="ping")] * 2 + ["ok"] * 5
    fake = _openai(*script)

    results = run_checks(fake, _config(LLM_FAST_MODEL="fast-model"))

    assert len(results) == 7
    assert [result.name for result in results if "fast-model" in result.name] != []


def test_a_failed_check_does_not_stop_the_others() -> None:
    fake = _openai(status_error(401), status_error(401), status_error(401), *["ok"] * 5)

    results = run_checks(fake, _config())

    assert [result.ok for result in results] == [False, False, False, True]


def test_a_missing_variable_exits_with_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The first thing an operator hits after editing .env, and the reason main() reads
    # the environment itself instead of taking a Config. Run from an empty directory so
    # the repository's own .env cannot answer the question for it.
    #
    # Since ADR 0012 decision 4 `load_config()` accepts an environment with no LLM variable in
    # it - that is what lets the API process start without one - so the loud failure belongs to
    # the process that assumes an LLM. This preflight is one of the three, and the message has
    # to name the project's own variable rather than the SDK's `OPENAI_API_KEY`.
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    assert main() == 1
    assert "LLM_BASE_URL is required" in capsys.readouterr().out
