"""
WHY THIS FILE EXISTS
    `bedrock.py` is an adapter, and an adapter's whole job is translation: a message list into
    a Converse request, a Converse response back into one string, and botocore's exceptions into
    the three kinds the retry loop answers. None of that is visible from a passing job, and all
    of it is the kind of thing that is wrong in a way nothing else notices - a system prompt sent
    as a user turn still produces a report, just a worse one.

    So this file drives the real adapter over a fake `converse`, and asserts the request it
    built and the classification it chose.

    **Nothing here reaches AWS, and nothing here can.** The fake client is a plain object with a
    `converse` method. The timeout and retry settings are read off `client_config`, which is a
    pure function precisely so they can be, and the one test that cares which arguments
    `build_bedrock_provider` passes replaces `boto3.client` rather than calling it - because
    constructing a real client resolves the credential chain, and no test here may read a
    developer's AWS profile, an access key, or an instance metadata service.

    The seam is proven twice over, and on purpose. This file proves the adapter translates; the
    ADR 0022 tests in tests/test_llm_client.py prove the client's policy - the budget, both
    backoff schedules, the validation retry - runs unchanged over a provider that is not the
    OpenAI one. Neither would catch the other's regression.

WHO CALLS IT
    pytest, as part of the offline suite.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from typing import Any, cast

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)

import llm_client
from bedrock import (
    CONNECT_TIMEOUT_S,
    MAX_OUTPUT_TOKENS,
    SDK_TIMEOUT_HEADROOM_S,
    BedrockConverseProvider,
    build_bedrock_provider,
    client_config,
    converse_request,
    response_text,
    token_usage,
)
from config import Config, load_config
from llm_client import (
    JOB_FATAL_REASONS,
    MAX_RETRY_AFTER_S,
    CallBudget,
    ChatMessage,
    LLMCallFailed,
    LLMClient,
    ProviderError,
)
from schemas import SupervisorDecision

_ENV = {
    "LLM_PROVIDER": "bedrock",
    "BEDROCK_MODEL_ID": "apac.amazon.nova-pro-v1:0",
    "TAVILY_API_KEY": "key",
}

_VALID = json.dumps({"next": "planner", "reason": "no plan yet"})

_MESSAGES = [
    ChatMessage("system", "You route the graph."),
    ChatMessage("user", "What runs next?"),
]


def _config(**overrides: str) -> Config:
    return load_config({**_ENV, **overrides})


class _FakeBedrock:
    """Shaped like the one method `bedrock.py` calls, and recording what it was called with."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError(f"converse was called {len(self.calls)} times; scripted fewer")
        answer = self.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _reply(text: str, usage: tuple[int, int] | None = None) -> dict[str, Any]:
    """A Converse response, in the shape the runtime documents."""
    response: dict[str, Any] = {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
    }
    if usage is not None:
        response["usage"] = {
            "inputTokens": usage[0],
            "outputTokens": usage[1],
            "totalTokens": sum(usage),
        }
    return response


def _provider(*script: Any, read_timeout_s: float = 210.0) -> BedrockConverseProvider:
    return BedrockConverseProvider(client=_FakeBedrock(*script), read_timeout_s=read_timeout_s)


def _client_error(code: str, retry_after: str | None = None) -> ClientError:
    response: dict[str, Any] = {"Error": {"Code": code, "Message": "from bedrock"}}
    if retry_after is not None:
        response["ResponseMetadata"] = {"HTTPHeaders": {"retry-after": retry_after}}
    return ClientError(response, "Converse")


# --- The Converse request the adapter builds -------------------------------------------


def test_system_instructions_go_to_the_converse_system_field() -> None:
    """The mapping that is easiest to get wrong and hardest to notice: Converse takes system
    instructions in their own top-level field, not as a turn in the message list."""
    request = converse_request(_MESSAGES, model="m", temperature=None, max_tokens=100)

    assert request["system"] == [{"text": "You route the graph."}]
    assert all(turn["role"] != "system" for turn in request["messages"])


def test_user_and_assistant_turns_keep_their_order_and_their_text() -> None:
    """The validation retry sends four messages - system, user, the rejected answer, the
    correction - and the correction only works if the model can still see what it said."""
    messages = [
        *_MESSAGES,
        ChatMessage("assistant", "{bad json}"),
        ChatMessage("user", "that did not validate"),
    ]

    request = converse_request(messages, model="m", temperature=None, max_tokens=100)

    assert request["messages"] == [
        {"role": "user", "content": [{"text": "What runs next?"}]},
        {"role": "assistant", "content": [{"text": "{bad json}"}]},
        {"role": "user", "content": [{"text": "that did not validate"}]},
    ]


def test_no_prompt_text_is_added_reordered_or_rewritten() -> None:
    """ADR 0022 decision 4: the JSON contract belongs to `call_structured` and is identical for
    both providers, so an adapter that edited a prompt would make the two incomparable."""
    request = converse_request(_MESSAGES, model="m", temperature=None, max_tokens=100)

    sent = [block["text"] for block in request["system"]] + [
        block["text"] for turn in request["messages"] for block in turn["content"]
    ]
    assert sent == [message.content for message in _MESSAGES]


def test_the_configured_model_id_is_what_converse_is_asked_for() -> None:
    request = converse_request(
        _MESSAGES, model="apac.amazon.nova-pro-v1:0", temperature=None, max_tokens=100
    )

    assert request["modelId"] == "apac.amazon.nova-pro-v1:0"


def test_max_output_tokens_is_always_sent() -> None:
    request = converse_request(_MESSAGES, model="m", temperature=None, max_tokens=4096)

    assert request["inferenceConfig"]["maxTokens"] == 4096


def test_no_temperature_is_sent_unless_a_caller_asks_for_one() -> None:
    # The same rule the OpenAI adapter follows with `omit`: the model's own default stays in
    # force for every agent, and only the evaluation judge asks for a value.
    request = converse_request(_MESSAGES, model="m", temperature=None, max_tokens=100)

    assert "temperature" not in request["inferenceConfig"]


def test_a_requested_temperature_reaches_the_inference_config() -> None:
    request = converse_request(_MESSAGES, model="m", temperature=0.0, max_tokens=100)

    assert request["inferenceConfig"]["temperature"] == 0.0


def test_the_provider_sends_the_request_it_built() -> None:
    fake = _FakeBedrock(_reply(_VALID))
    provider = BedrockConverseProvider(client=fake, read_timeout_s=210.0)

    provider.complete(messages=_MESSAGES, model="nova", timeout=180.0, temperature=None)

    assert fake.calls[0]["modelId"] == "nova"
    assert fake.calls[0]["inferenceConfig"]["maxTokens"] == MAX_OUTPUT_TOKENS


# --- Reading the response ---------------------------------------------------------------


def test_the_assistant_text_is_extracted_from_the_content_blocks() -> None:
    assert response_text(_reply(_VALID), "m") == _VALID


def test_several_text_blocks_are_joined_in_order() -> None:
    response = {"output": {"message": {"content": [{"text": '{"a":'}, {"text": "1}"}]}}}

    assert response_text(response, "m") == '{"a":1}'


def test_a_response_with_no_text_block_reads_as_empty_output() -> None:
    """Empty is invalid output rather than a transport problem, exactly as an empty OpenAI
    completion is: the validation retry is the layer that can ask again better."""
    assert response_text({"output": {"message": {"content": []}}}, "m") == ""


def test_a_response_that_is_not_a_converse_response_is_fatal() -> None:
    """Not retried, because a shape this code was not written against does not improve on the
    third attempt - it means the SDK, the model or the endpoint is not what was expected."""
    with pytest.raises(ProviderError) as raised:
        response_text({"nothing": "useful"}, "m")

    assert raised.value.kind == "fatal"


def test_a_malformed_content_list_is_fatal() -> None:
    with pytest.raises(ProviderError) as raised:
        response_text({"output": {"message": {"content": "not a list"}}}, "m")

    assert raised.value.kind == "fatal"


@pytest.mark.parametrize(
    "text",
    [
        "```json\n" + _VALID + "\n```",
        "```\n" + _VALID + "\n```",
        "  ```json\n" + _VALID + "\n```  ",
    ],
)
def test_a_markdown_fence_around_the_json_is_removed(text: str) -> None:
    """The one place the adapter touches what the model said, and it removes rather than adds.
    Nova has no JSON mode to enforce the "no fences" instruction the prompt already carries, and
    spending the single validation retry - and a call from a 60-call budget - on punctuation
    buys nothing."""
    provider = _provider(_reply(text))

    answer = provider.complete(messages=_MESSAGES, model="m", timeout=60.0, temperature=None)

    assert json.loads(answer) == json.loads(_VALID)


def test_text_that_merely_contains_a_backtick_is_left_alone() -> None:
    body = json.dumps({"next": "planner", "reason": "the `plan` field is empty"})
    provider = _provider(_reply(body))

    assert provider.complete(messages=_MESSAGES, model="m", timeout=60.0, temperature=None) == body


# --- Token usage: recorded, never invented ------------------------------------------------


def test_token_usage_is_read_from_the_response_when_it_is_there() -> None:
    assert token_usage(_reply(_VALID, usage=(1200, 340))) == (1200, 340)


@pytest.mark.parametrize(
    "response",
    [
        _reply(_VALID),
        {"usage": {}},
        {"usage": {"inputTokens": "many", "outputTokens": 4}},
        "not a response",
    ],
)
def test_an_absent_or_unusable_usage_block_is_reported_as_unknown(response: Any) -> None:
    """Unknown rather than zero and rather than estimated. A fabricated token count is worse
    than no token count, because it looks like a measurement (ADR 0022 decision 8)."""
    assert token_usage(response) is None


def test_the_providers_own_token_counts_are_logged_with_the_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _provider(_reply(_VALID, usage=(1200, 340)))

    with caplog.at_level(logging.INFO, logger="bedrock"):
        provider.complete(messages=_MESSAGES, model="nova", timeout=60.0, temperature=None)

    logged = caplog.text
    assert "input_tokens=1200" in logged
    assert "output_tokens=340" in logged
    assert "nova" in logged
    # No cost anywhere: pricing is not in the response, and a hard-coded rate is a number that
    # is wrong the first time AWS changes one.
    for word in ("cost", "usd", "$"):
        assert word not in logged.lower()


# --- Classifying what AWS said -------------------------------------------------------------


@pytest.mark.parametrize(
    "code", ["ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"]
)
def test_throttling_becomes_the_rate_limited_kind(code: str) -> None:
    """Which puts it on `_RATE_LIMIT_BACKOFF_S` and fails the job when that runs out - the same
    treatment a 429 gets from an OpenAI-compatible endpoint."""
    provider = _provider(_client_error(code))

    with pytest.raises(ProviderError) as raised:
        provider.complete(messages=_MESSAGES, model="m", timeout=60.0, temperature=None)

    assert raised.value.kind == "rate_limited"


@pytest.mark.parametrize(
    "code",
    [
        "InternalServerException",
        "ServiceUnavailableException",
        "ModelNotReadyException",
        "ModelTimeoutException",
    ],
)
def test_a_transient_service_failure_becomes_the_transport_kind(code: str) -> None:
    provider = _provider(_client_error(code))

    with pytest.raises(ProviderError) as raised:
        provider.complete(messages=_MESSAGES, model="m", timeout=60.0, temperature=None)

    assert raised.value.kind == "transport"


@pytest.mark.parametrize(
    "code",
    [
        "AccessDeniedException",
        "ValidationException",
        "ResourceNotFoundException",
        "ModelErrorException",
        "UnrecognizedClientException",
        "SomethingNobodyHasSeen",
    ],
)
def test_a_configuration_or_authorization_failure_is_fatal(code: str) -> None:
    """Including the unrecognised one. An unknown code is more likely to be a denied action
    than a transient fault, and retrying the former spends three calls to print one message
    three times."""
    provider = _provider(_client_error(code))

    with pytest.raises(ProviderError) as raised:
        provider.complete(messages=_MESSAGES, model="m", timeout=60.0, temperature=None)

    assert raised.value.kind == "fatal"
    assert code in str(raised.value)


def test_a_throttle_that_names_a_delay_carries_it_to_the_caller() -> None:
    provider = _provider(_client_error("ThrottlingException", retry_after="7"))

    with pytest.raises(ProviderError) as raised:
        provider.complete(messages=_MESSAGES, model="m", timeout=60.0, temperature=None)

    assert raised.value.retry_after == 7.0


@pytest.mark.parametrize("header", ["-1", "later", str(MAX_RETRY_AFTER_S + 3600)])
def test_a_delay_the_policy_would_not_choose_is_bounded_or_dropped(header: str) -> None:
    """A provider must not be able to replace a finite retry policy with an arbitrarily long
    sleep, so the ceiling is the caller's `MAX_RETRY_AFTER_S` and an unusable value falls back
    to the caller's own schedule."""
    provider = _provider(_client_error("ThrottlingException", retry_after=header))

    with pytest.raises(ProviderError) as raised:
        provider.complete(messages=_MESSAGES, model="m", timeout=60.0, temperature=None)

    assert raised.value.retry_after in (None, MAX_RETRY_AFTER_S)


@pytest.mark.parametrize(
    "error",
    [
        ReadTimeoutError(endpoint_url="https://bedrock-runtime.ap-south-1.amazonaws.com"),
        EndpointConnectionError(endpoint_url="https://bedrock-runtime.ap-south-1.amazonaws.com"),
    ],
)
def test_a_socket_failure_becomes_the_transport_kind(error: Exception) -> None:
    provider = _provider(error)

    with pytest.raises(ProviderError) as raised:
        provider.complete(messages=_MESSAGES, model="m", timeout=60.0, temperature=None)

    assert raised.value.kind == "transport"


def test_a_missing_credential_is_fatal_rather_than_retried() -> None:
    """The failure a misconfigured task role produces. Retrying it three times per call, for
    every node of a job, would turn one wrong IAM policy into a very long log."""
    provider = _provider(NoCredentialsError())

    with pytest.raises(ProviderError) as raised:
        provider.complete(messages=_MESSAGES, model="m", timeout=60.0, temperature=None)

    assert raised.value.kind == "fatal"


# --- Timeouts: the socket outlasts the request ---------------------------------------------


def test_the_sdk_read_timeout_outlasts_the_application_timeout() -> None:
    """The deployment waits 180 seconds for a main-tier request. A client built to read for 180
    would race it and lose about half the time, reporting a transport failure for a generation
    that was merely slow - and charging another call to retry it."""
    settings = client_config(180.0)

    assert settings.read_timeout == 180.0 + SDK_TIMEOUT_HEADROOM_S
    assert settings.read_timeout > 180.0
    assert settings.connect_timeout == CONNECT_TIMEOUT_S


def test_botocore_does_not_retry_underneath_the_clients_schedules() -> None:
    """Left on, botocore's `standard` mode would retry a throttled request underneath
    `LLMClient._send`, turning one counted call into three uncounted ones against a shared rate
    limit that granted one token."""
    assert client_config(180.0).retries["total_max_attempts"] == 1


def test_the_client_is_built_for_the_region_it_is_given_and_for_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The region is the only thing this deployment chooses. **There is no key, no secret and
    no endpoint override** - boto3 signs with the ECS task role through its ordinary provider
    chain, which is the whole of ADR 0022 decision 3, and this is where that is asserted rather
    than assumed.

    boto3 is replaced rather than called: constructing a real client resolves the credential
    chain, and no test here may read a developer's AWS profile.
    """
    built: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def record(*args: Any, **kwargs: Any) -> Any:
        built.append((args, kwargs))
        return object()

    monkeypatch.setattr("bedrock.boto3.client", record)

    provider = build_bedrock_provider(region="us-east-1", app_timeout_s=60.0)

    (service,), kwargs = built[0]
    assert service == "bedrock-runtime"
    assert kwargs["region_name"] == "us-east-1"
    assert set(kwargs) == {"region_name", "config"}
    assert kwargs["config"].read_timeout == 60.0 + SDK_TIMEOUT_HEADROOM_S
    assert provider._read_timeout_s == 60.0 + SDK_TIMEOUT_HEADROOM_S


def test_a_request_longer_than_the_client_can_serve_is_refused_rather_than_shortened() -> None:
    """botocore fixes `read_timeout` on the client, so a longer request cannot be honoured. A
    silent shorter bound would look like an endpoint that times out; this looks like the
    configuration mistake it is."""
    provider = _provider(_reply(_VALID), read_timeout_s=45.0)

    with pytest.raises(ProviderError) as raised:
        provider.complete(messages=_MESSAGES, model="m", timeout=180.0, temperature=None)

    assert raised.value.kind == "fatal"


@pytest.mark.parametrize("main_timeout", ["10", "60", "180"])
def test_the_socket_covers_whichever_tier_is_the_slower_one(main_timeout: str) -> None:
    """`LLMClient` asks for the larger of the two tier timeouts, so neither tier can trip the
    refusal above however `LLM_MAIN_TIMEOUT_S` is set - including the case where the fast
    tier's fixed 30s is the larger of the two."""
    client = LLMClient(_config(LLM_MAIN_TIMEOUT_S=main_timeout), provider=_provider())
    longest = max(client._timeout_for("main"), client._timeout_for("fast"))

    assert client_config(longest).read_timeout > longest


# --- End to end through the real client ------------------------------------------------------


def test_a_valid_converse_reply_becomes_a_validated_schema_object() -> None:
    """The whole contract, over Bedrock: a Pydantic class in, an instance of it out, and no
    agent aware of which provider answered."""
    provider = _provider(_reply(_VALID))

    decision = LLMClient(_config(), provider=provider).call_structured(
        schema=SupervisorDecision,
        system="You route the graph.",
        user="What runs next?",
        budget=CallBudget(limit=60),
    )

    assert decision.next == "planner"


def test_malformed_json_from_nova_takes_the_one_validation_retry() -> None:
    provider = _provider(_reply("not json at all"), _reply(_VALID))

    decision = LLMClient(_config(), provider=provider).call_structured(
        schema=SupervisorDecision,
        system="You route the graph.",
        user="What runs next?",
        budget=CallBudget(limit=60),
    )

    assert decision.next == "planner"
    assert len(cast(_FakeBedrock, provider._client).calls) == 2


def test_json_that_does_not_fit_the_schema_fails_the_node_after_one_retry() -> None:
    """Never a partially valid object and never a substituted default: an unvalidated
    dictionary reaching an agent is what this contract exists to prevent."""
    wrong = _reply(json.dumps({"next": "reflection", "reason": "not an allowed route"}))
    provider = _provider(wrong, wrong)

    with pytest.raises(LLMCallFailed) as raised:
        LLMClient(_config(), provider=provider).call_structured(
            schema=SupervisorDecision,
            system="You route the graph.",
            user="What runs next?",
            budget=CallBudget(limit=60),
        )

    assert raised.value.reason == "invalid_output"
    assert len(cast(_FakeBedrock, provider._client).calls) == 2


def test_bedrock_throttling_reaches_the_clients_rate_limit_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The end-to-end version of the classification tests above**, because a `kind` that no
    schedule reads would be a translation into nothing. A `ThrottlingException` that never clears
    fails the *job* - guidelines section 13's rule that a rate-limited job fails visibly rather
    than quietly producing a shorter report - after the documented 2s/8s/30s."""
    slept: list[float] = []
    monkeypatch.setattr(llm_client, "sleep", slept.append)
    provider = _provider(*[_client_error("ThrottlingException") for _ in range(4)])

    with pytest.raises(LLMCallFailed) as raised:
        LLMClient(_config(), provider=provider).call_structured(
            schema=SupervisorDecision,
            system="You route the graph.",
            user="What runs next?",
            budget=CallBudget(limit=60),
        )

    assert raised.value.reason == "rate_limited"
    assert raised.value.reason in JOB_FATAL_REASONS
    assert slept == [2.0, 8.0, 30.0]


def test_an_access_denied_does_not_multiply_into_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure a misconfigured task role produces. One call, one message, no sleeping - not
    three attempts per call for every node of a job."""
    slept: list[float] = []
    monkeypatch.setattr(llm_client, "sleep", slept.append)
    provider = _provider(_client_error("AccessDeniedException"))
    budget = CallBudget(limit=60)

    with pytest.raises(LLMCallFailed) as raised:
        LLMClient(_config(), provider=provider).call_structured(
            schema=SupervisorDecision,
            system="You route the graph.",
            user="What runs next?",
            budget=budget,
        )

    assert raised.value.reason == "llm_call_failed"
    assert budget.used == 1
    assert slept == []


# --- The boundary: who may import what, and what a plain `pytest` may touch -------------------


def _top_level_imports(path: Path) -> set[str]:
    """The modules a file imports **at module scope**, which is the distinction that matters
    here: `llm_client` imports `bedrock` inside a function on purpose, and reading every import
    in the file would not be able to tell the two apart."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module.split(".")[0])
    return names


def test_importing_the_client_does_not_import_boto3() -> None:
    """**The rule that keeps `pytest` offline.** Constructing a botocore session resolves the AWS
    credential chain, which on a developer's machine means reading their profile - and on this
    machine a `login_session` profile makes that raise. `llm_client` imports `bedrock` inside the
    branch that needs it, so a process on the OpenAI path never goes near boto3."""
    root = Path(__file__).resolve().parent.parent
    imports = _top_level_imports(root / "llm_client.py")

    assert "boto3" not in imports
    assert "botocore" not in imports
    assert "bedrock" not in imports


@pytest.mark.parametrize(
    "module", ["supervisor", "planner", "researcher", "synthesizer", "fact_checker"]
)
def test_no_agent_imports_a_provider(module: str) -> None:
    """ADR 0022 decision 1, asserted by import rather than by review: the provider choice must
    not reach an agent, or the abstraction is in the wrong place. The complement is every agent
    test in this suite, which drives the real agents over `FakeOpenAI` and knows nothing about
    either provider."""
    root = Path(__file__).resolve().parent.parent
    imports = _top_level_imports(root / "agents" / f"{module}.py")

    for forbidden in ("bedrock", "boto3", "botocore", "openai"):
        assert forbidden not in imports, f"agents/{module}.py imports {forbidden}"


def test_the_reflection_node_imports_no_provider_either() -> None:
    """It is not one of the five agents, and it is the sixth caller of `call_structured`."""
    root = Path(__file__).resolve().parent.parent
    imports = _top_level_imports(root / "graph" / "reflection.py")

    for forbidden in ("bedrock", "boto3", "botocore", "openai"):
        assert forbidden not in imports


def test_bedrock_is_the_only_module_that_talks_to_bedrock() -> None:
    """The same "one place that talks to X" rule `jobqueue.py`, `artifacts.py` and
    `redisstore.py` already follow. A second `bedrock-runtime` client would be a second place
    for the timeout, the retry setting and the error mapping to live."""
    root = Path(__file__).resolve().parent.parent
    others = (
        [path for path in root.glob("*.py") if path.name not in ("bedrock.py",)]
        + list((root / "agents").glob("*.py"))
        + list((root / "graph").glob("*.py"))
        + list((root / "routes").glob("*.py"))
    )

    for path in others:
        # The construction, not the word: `config.py` names the API in a docstring, and
        # explaining what a value is for is not the same as building a client for it.
        assert 'boto3.client("bedrock-runtime"' not in path.read_text(encoding="utf-8"), (
            f"{path.name} builds its own Bedrock client"
        )
