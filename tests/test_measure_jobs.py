"""
WHY THIS FILE EXISTS
    The measurement harness is not production code, but the numbers it publishes are, so a
    defect in how it is wired shows up as a wrong figure in the docs rather than as a broken
    job. This file covers the one such defect found so far, and it is a **lifecycle** bug
    rather than a logic one.

    `LLMClient.__init__` applies `wrap_openai` when tracing is on. That wrapper patches the
    OpenAI object **in place** and does not refuse to do it twice - so a harness that built
    one OpenAI client and then a new `LLMClient` per job wrapped the same object again on
    every job. Job N ran under N layers and emitted N nested spans per request, the outermost
    reporting a running token total.

    Nothing about the job changed: one HTTP request per counted call, so no extra spend, no
    extra rate pressure, and `CallBudget` counted exactly what it always counted. What broke
    was the telemetry - a token figure read from a multi-job trace was inflated by the job's
    ordinal, and that is the number a cost estimate is built from.

    The fix is one `LLMClient` for the whole run. These tests pin both halves: that the
    harness shares one, and that sharing it is what keeps the wrapper count at one. The
    third test pins the third-party behaviour the other two exist because of, so that a
    LangSmith release which starts refusing to double-wrap fails here and says so, rather
    than leaving the reason for this shape unexplained.

    No network, and no real credentials: the OpenAI client is never asked to send anything,
    and the graph is replaced by a stub, because what is under test is which object reached
    it rather than what it did.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from openai import OpenAI

import scripts.measure_jobs as measure_jobs
from config import Config, load_config
from graph.state import ResearchState, new_state
from llm_client import LLMClient
from scripts.measure_jobs import run_job
from tools.contracts import ToolCache

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_FAST_MODEL": "fast-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
}

_TRACED = {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "ls-key"}


def _config(**overrides: str) -> Config:
    return load_config({**_ENV, **overrides})


def _openai() -> OpenAI:
    """A real SDK client. It is never asked to send a request - only to be wrapped."""
    return OpenAI(base_url="https://example.invalid/v1", api_key="key", max_retries=0)


def _llm(**overrides: str) -> LLMClient:
    return LLMClient(_config(**overrides), client=_openai())


def _wrapper_layers(client: OpenAI) -> int:
    """How many decorators sit on `chat.completions.create`, counted through `__wrapped__`.

    `wrap_openai` wraps with `functools.wraps`, so each application leaves one more link in
    that chain. Every assertion below compares this against the same client before wrapping
    rather than against an absolute number, because the SDK puts a decorator of its own there
    too and that is not this file's business.
    """
    layers = 0
    target: Any = client.chat.completions.create
    while hasattr(target, "__wrapped__"):
        layers += 1
        target = target.__wrapped__
    return layers


class _StubGraph:
    """Enough of a compiled graph for `run_job`: it streams nothing and holds one state.

    `run_job` is being driven for its lifecycle, not its output, so the graph does no work.
    A real one here would need the whole recorded-web harness to say something these tests
    do not ask.
    """

    def __init__(self) -> None:
        self.state: ResearchState = new_state("job", "user", "Compare two vendors.")

    def stream(self, *_args: Any, **_kwargs: Any) -> Iterator[Any]:
        return iter(())

    def get_state(self, _settings: Any) -> Any:
        # `next` empty means "not paused at the gate", so run_job does not try to resume.
        return SimpleNamespace(next=(), values=self.state)


def _record_clients(monkeypatch: pytest.MonkeyPatch) -> list[LLMClient]:
    """Replace `build_graph` with a stub, and return the list of clients handed to it."""
    handed: list[LLMClient] = []

    def build(*, config: Config, llm: LLMClient, cache: ToolCache | None = None) -> Any:
        handed.append(llm)
        return _StubGraph()

    monkeypatch.setattr(measure_jobs, "build_graph", build)
    return handed


# --- The shared client ---------------------------------------------------------------


def test_every_job_in_a_run_uses_the_client_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    # `run_job` takes a built client rather than building one. That is the whole fix: the
    # object identity is what keeps `wrap_openai` from being applied a second time.
    handed = _record_clients(monkeypatch)
    llm = LLMClient(_config(), client=_openai())

    for index in range(1, 4):
        run_job(index, 3, "comparison", f"Compare vendor {index}.", config=_config(), llm=llm)

    assert handed == [llm, llm, llm]


def test_a_traced_run_adds_one_wrapper_layer_however_many_jobs_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The regression itself. Six jobs used to mean six layers on one client, and the trace's
    # root token total was multiplied by the job's ordinal.
    _record_clients(monkeypatch)
    config = _config(**_TRACED)
    client = _openai()
    bare = _wrapper_layers(client)

    llm = LLMClient(config, client=client)
    for index in range(1, 7):
        run_job(index, 6, "comparison", f"Compare vendor {index}.", config=config, llm=llm)

    assert _wrapper_layers(client) - bare == 1


def test_building_a_client_per_job_is_what_stacked_the_layers() -> None:
    # Why the two tests above are written the way they are. `wrap_openai` mutates the client
    # it is handed and has no idempotency guard, so this is the behaviour being designed
    # around rather than a defect being asserted. If a LangSmith release starts refusing to
    # double-wrap, this fails - which is the right way to find out that the shared-client
    # lifecycle could be relaxed.
    config = _config(**_TRACED)
    client = _openai()
    bare = _wrapper_layers(client)

    for _job in range(3):
        LLMClient(config, client=client)

    assert _wrapper_layers(client) - bare == 3


def test_tracing_off_leaves_the_client_untouched() -> None:
    # The gate the whole test suite depends on: with tracing off nothing is wrapped, so the
    # scripted fakes every other test injects are handed straight through.
    client = _openai()
    bare = _wrapper_layers(client)

    LLMClient(_config(), client=client)

    assert _config().langsmith_tracing is False
    assert _wrapper_layers(client) == bare


# --- The row a job produces ------------------------------------------------------------


def test_a_job_that_did_nothing_still_produces_a_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # A crashed or empty job is data, not a stopped run. The stub graph does no work, so this
    # is the floor: the row exists and carries the state's own numbers.
    _record_clients(monkeypatch)

    result = run_job(1, 1, "comparison", "Compare two vendors.", config=_config(), llm=_llm())

    assert result.question == "Compare two vendors."
    assert result.status == "running"  # the stub never reaches a terminal status
    assert result.llm_calls_used == 0
    assert result.prompt_tokens is None  # not recorded here since telemetry moved to LangSmith
