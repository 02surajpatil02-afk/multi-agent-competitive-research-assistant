"""
WHY THIS FILE EXISTS
    The preflight. Swapping a model is a config change plus this script plus an eval run,
    never a code change - which only holds if something actually checks that the new
    endpoint can do what the agents need. It confirms the configured endpoint answers,
    supports JSON mode and tool calling, and reports the throughput it observed
    (CLAUDE.md, ARCHITECTURE.md §17).

    It talks to the SDK directly rather than through llm_client.py on purpose. Two of the
    four checks - tool calling, and how fast the endpoint answers under a burst - are
    capabilities of the endpoint that the client never exercises, and running them
    through the client's retry and validation layers would hide exactly the failure the
    preflight exists to surface.

    Every check catches its own failure and reports it. A preflight that stops at the
    first problem tells you less than one that reports all four.

WHO CALLS IT
    A person, after changing LLM_MODEL, LLM_FAST_MODEL, or LLM_BASE_URL:

        python scripts/check_model.py

    Exit code 0 means every check passed, 1 means at least one did not.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from openai import OpenAI
from openai.types.chat import ChatCompletionFunctionToolParam

from config import Config, load_config, required

_PREFLIGHT_TIMEOUT_S = 30.0
"""Guidelines §17's fast-tier LLM timeout. Every prompt below is a few tokens."""

_THROUGHPUT_REQUESTS = 5
"""Small on purpose. The development tier allows roughly 40 requests a minute, and a
preflight that eats a quarter of it is a preflight people stop running."""

_PING = "Reply with the single word: ok"

_JSON_PROMPT = 'Reply with this exact JSON object and nothing else: {"status": "ok"}'

_TOOL_PROMPT = "Call the ping tool. Do not reply with text."

_PING_TOOL: ChatCompletionFunctionToolParam = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Returns pong. Used only to check that tool calling works.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"[{'PASS' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


def check_endpoint_answers(client: OpenAI, model: str) -> CheckResult:
    """The endpoint is reachable, the key works, and the model id exists."""
    name = f"{model} answers"
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": _PING}],
            timeout=_PREFLIGHT_TIMEOUT_S,
        )
    except Exception as error:  # noqa: BLE001 - a preflight reports every failure shape
        return CheckResult(name, False, str(error))

    content = response.choices[0].message.content if response.choices else None
    if not content:
        return CheckResult(name, False, "answered with an empty completion")
    return CheckResult(name, True, f"answered {content.strip()[:40]!r}")


def check_json_mode(client: OpenAI, model: str) -> CheckResult:
    """JSON mode works, which is what llm_client.py relies on for structured output.

    If this fails, the fallback is parse-and-validate without response_format
    (guidelines §3) - a deliberate change, not something to discover at runtime.
    """
    name = f"{model} supports JSON mode"
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": _JSON_PROMPT}],
            response_format={"type": "json_object"},
            timeout=_PREFLIGHT_TIMEOUT_S,
        )
    except Exception as error:  # noqa: BLE001
        return CheckResult(name, False, str(error))

    content = response.choices[0].message.content if response.choices else None
    if not content:
        return CheckResult(name, False, "answered with an empty completion")
    try:
        json.loads(content)
    except ValueError:
        return CheckResult(name, False, f"response_format accepted but returned {content[:60]!r}")
    return CheckResult(name, True, "response_format accepted and the reply parsed as JSON")


def check_tool_calling(client: OpenAI, model: str) -> CheckResult:
    """Tool calling works. The Researcher and the Fact-Checker need it (guidelines §7)."""
    name = f"{model} supports tool calling"
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": _TOOL_PROMPT}],
            tools=[_PING_TOOL],
            timeout=_PREFLIGHT_TIMEOUT_S,
        )
    except Exception as error:  # noqa: BLE001
        return CheckResult(name, False, str(error))

    tool_calls = response.choices[0].message.tool_calls if response.choices else None
    if not tool_calls:
        return CheckResult(name, False, "tools accepted but the model replied without a tool call")
    return CheckResult(name, True, f"called {_tool_name(tool_calls[0])}")


def _tool_name(tool_call: object) -> str:
    """The SDK's tool-call type is a union and only the function shape carries a name.

    Read defensively: the preflight's verdict is "a tool call came back", and the name is
    detail for the person reading the output.
    """
    name = getattr(getattr(tool_call, "function", None), "name", None)
    return str(name) if name else "an unnamed tool"


def measure_throughput(client: OpenAI, model: str, configured_rpm: int) -> CheckResult:
    """Report what the endpoint actually sustained, next to what we told it to expect.

    This is a measurement, not a pass/fail gate: it fails only if a request broke for a
    reason other than rate limiting. It does not establish the endpoint's ceiling - five
    sequential requests are too few to trip a 40 RPM limit - but it does show the latency
    the limiter and the job timeout have to live with, and it surfaces a 429 immediately
    if the tier is already saturated.
    """
    name = f"{model} throughput"
    started = time.monotonic()
    for index in range(_THROUGHPUT_REQUESTS):
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": _PING}],
                timeout=_PREFLIGHT_TIMEOUT_S,
            )
        except Exception as error:  # noqa: BLE001
            elapsed = time.monotonic() - started
            return CheckResult(
                name,
                False,
                f"request {index + 1} of {_THROUGHPUT_REQUESTS} failed "
                f"after {elapsed:.1f}s: {error}",
            )

    elapsed = time.monotonic() - started
    observed_rpm = _THROUGHPUT_REQUESTS / elapsed * 60 if elapsed > 0 else float("inf")
    return CheckResult(
        name,
        True,
        f"{_THROUGHPUT_REQUESTS} sequential requests in {elapsed:.1f}s "
        f"(~{observed_rpm:.0f} req/min sustained; LLM_RPM_LIMIT is {configured_rpm})",
    )


def run_checks(client: OpenAI, config: Config) -> list[CheckResult]:
    """Capability checks for every distinct configured model, then one measurement."""
    # The preflight is a process that assumes an LLM, so it narrows loudly (ADR 0012 dec 4).
    main_model = required(config.llm_model, "LLM_MODEL")
    models = [main_model]
    if config.llm_fast_model and config.llm_fast_model != main_model:
        models.append(config.llm_fast_model)

    results: list[CheckResult] = []
    for model in models:
        results.append(check_endpoint_answers(client, model))
        results.append(check_json_mode(client, model))
        results.append(check_tool_calling(client, model))
    results.append(measure_throughput(client, main_model, config.llm_rpm_limit))
    return results


def main() -> int:
    try:
        config = load_config()
        # The preflight is a process that assumes an LLM, so it narrows the four variables it
        # cannot run without before it builds anything (ADR 0012 decision 4). `load_config`
        # itself no longer refuses them - the API starts with none of them set.
        base_url = required(config.llm_base_url, "LLM_BASE_URL")
        api_key = required(config.llm_api_key, "LLM_API_KEY")
        required(config.llm_model, "LLM_MODEL")
    except ValueError as error:
        # The first thing an operator hits after editing .env. A named variable beats a
        # traceback, and it keeps the exit code meaningful.
        print(f"configuration error: {error}")
        return 1

    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        max_retries=0,  # the preflight reports what happened; it does not paper over it
    )

    print(f"endpoint: {config.llm_base_url}")
    print(f"main model: {config.llm_model}")
    print(f"fast model: {config.llm_fast_model}\n")

    results = run_checks(client, config)
    for result in results:
        print(result.line())

    failed = [result for result in results if not result.ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
