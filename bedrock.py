"""
WHY THIS FILE EXISTS
    The one place that talks to Amazon Bedrock, the way `jobqueue.py` is the one place that
    talks to SQS, `artifacts.py` the one place that talks to S3, and `redisstore.py` the one
    place that talks to Redis. It implements `llm_client.ChatProvider` and nothing else: one
    Converse request, the text out of it, and this SDK's failures translated into the three
    kinds `LLMClient._send` already knows how to answer.

    Five things here are decisions rather than plumbing.

    **Converse, not an OpenAI compatibility shim.** Bedrock's `converse` is the one runtime API
    that takes the same request shape for every model family on the platform, which is what
    makes `BEDROCK_MODEL_ID` a configuration value rather than a code branch. A shim would add
    a translation layer whose failures look like model failures, in exchange for reusing a
    client that would still need this file's error mapping.

    **No credential is read, constructed, or configurable.** `boto3.client("bedrock-runtime")`
    picks the ECS task role up through the ordinary provider chain, exactly as `jobqueue.py`
    and `artifacts.py` already do for SQS and S3. There is no Bedrock API key, no access key in
    the container, and no Bedrock entry in Secrets Manager - authorization is
    `bedrock:InvokeModel` on the worker task role, and that is the whole of it
    (ADR 0022 decision 3).

    **Structured output is the caller's contract and is enforced by the caller.** Nova has no
    JSON mode to switch on, so what arrives is whatever the model wrote. This file adds no
    instruction of its own - `LLMClient.call_structured` already puts the JSON Schema in the
    system prompt, and its one validation retry already carries the pydantic error back. The
    single concession here is `_json_text`, which strips a markdown fence the model wrapped its
    JSON in, because burning the one validation retry on punctuation would cost a call from a
    60-call budget for no information (ADR 0022 decision 4).

    **The retry schedules are the caller's too, and botocore's are switched off**
    (`total_max_attempts=1`), for the same reason `llm_client.py` switches off the OpenAI SDK's
    and `artifacts.py` switches off S3's: two retry layers multiply into an attempt count
    nobody wrote down, and every attempt spends a token of the shared rate limit and a call of
    the per-job budget. This file classifies a failure; it never waits and never re-sends.

    **The transport bound is derived from the application's, never invented.** botocore has no
    per-call timeout - `read_timeout` belongs to the client - so a client built with a bound
    shorter than `LLM_MAIN_TIMEOUT_S` would end requests the policy above still considers live,
    and the job would see transport failures that were really our own socket. `build_bedrock_
    provider` therefore takes the longest timeout the caller may ask for and adds
    `SDK_TIMEOUT_HEADROOM_S`; `complete` refuses a longer one rather than silently serving a
    shorter bound.

WHO CALLS IT
    `llm_client.LLMClient` builds one when `LLM_PROVIDER=bedrock`, and imports it inside that
    branch so that a process on the OpenAI path never constructs a botocore session. Nothing
    else imports boto3 for Bedrock, and no agent, node or route imports this module at all.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotoConnectionError

from llm_client import MAX_RETRY_AFTER_S, ChatMessage, ProviderError, ProviderFailureKind

logger = logging.getLogger(__name__)

SDK_TIMEOUT_HEADROOM_S = 30.0
"""How much longer the socket may wait than the application will.

The application's bound is `LLM_MAIN_TIMEOUT_S` - 180 in the deployment - and botocore's
`read_timeout` is per client rather than per call, so the two have to be ordered rather than
equal. If they were equal, a request that took exactly its allowance would race the socket and
lose about half the time, turning a slow-but-successful generation into a `transport` failure
and a retry that costs another call.

Thirty seconds is deliberately modest: it is enough to cover connection setup and the response
being written out after generation finishes, and small enough that a genuinely hung socket is
still released while the same job is running rather than after it has given up.
"""

CONNECT_TIMEOUT_S = 10.0
"""How long establishing the connection may take, separately from waiting for the answer.

Split from the read bound for the same reason `artifacts.OPERATION_TIMEOUT_S` is applied to
both halves: a connect that hangs and a generation that is merely slow are different failures,
and a single number means the first one waits as long as the second is allowed to.
"""

MAX_OUTPUT_TOKENS = 8192
"""`inferenceConfig.maxTokens` on every Converse request.

Stated here rather than inherited, because a model's own default is a property of the model and
this value has to hold across a `BEDROCK_MODEL_ID` change. It is sized for the largest output
any caller produces - the Synthesizer's full report - and every other caller (a plan, an
extraction, a verdict batch, a rubric) is far smaller.

A truncated answer is not a silent failure: it is invalid JSON, so it reaches
`call_structured`'s validation retry exactly as any other malformed output does.
"""

RATE_LIMITED_CODES = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceQuotaExceededException",
    }
)
"""Bedrock saying "not now". The caller waits on `_RATE_LIMIT_BACKOFF_S` and fails the job when
that runs out, which is the same treatment a 429 gets from an OpenAI-compatible endpoint."""

TRANSPORT_CODES = frozenset(
    {
        "InternalServerException",
        "ServiceUnavailableException",
        "ModelNotReadyException",
        "ModelTimeoutException",
    }
)
"""Bedrock saying "try again" - a 5xx, a model still warming up, or a generation that ran past
the service's own limit. The caller retries on the tier's schedule and fails the node.

**`ModelErrorException` is deliberately absent.** It is the model itself refusing a request, and
a request that is wrong now is wrong on the third attempt too; it falls through to `fatal`.
"""


class BedrockConverseProvider:
    """One Bedrock Runtime client, behind `llm_client.ChatProvider`.

    A small object rather than module functions for the reason `ArtifactStore` is one: it holds
    a boto3 client and the bound that client was built with, and passing both to every call
    would put the same two arguments in one signature for no gain.
    """

    def __init__(self, *, client: Any, read_timeout_s: float) -> None:
        self._client = client
        self._read_timeout_s = read_timeout_s

    def complete(
        self,
        *,
        messages: Sequence[ChatMessage],
        model: str,
        timeout: float,
        temperature: float | None,
    ) -> str:
        """One Converse request, and the text it produced.

        `timeout` is checked rather than applied, and that is the honest shape of the
        constraint: botocore fixes `read_timeout` on the client, so what this can promise is
        that the socket outlasts the request the caller is willing to wait for. A caller asking
        for longer than this client was built for is a configuration mistake, so it is refused
        as one instead of being served a shorter bound it did not ask for.
        """
        if timeout > self._read_timeout_s:
            raise ProviderError(
                "fatal",
                f"{model}: a {timeout:.0f}s request needs a client built for at least that "
                f"long, and this one reads for {self._read_timeout_s:.0f}s",
            )

        request = converse_request(
            messages, model=model, temperature=temperature, max_tokens=MAX_OUTPUT_TOKENS
        )
        try:
            response = self._client.converse(**request)
        except ClientError as error:
            raise _from_client_error(error, model) from error
        except (BotoConnectionError, HTTPClientError) as error:
            # A dropped connection, a refused endpoint, or our own read timeout. The same
            # class of failure `APITimeoutError` and `APIConnectionError` are on the other
            # provider, and it gets the same schedule.
            raise ProviderError("transport", f"{model} unreachable: {error}") from error
        except BotoCoreError as error:
            # Everything else botocore raises before a request is even made: no credentials,
            # an unknown region, a malformed parameter. Retrying repeats the mistake.
            raise ProviderError("fatal", f"{model} could not be called: {error}") from error

        usage = token_usage(response)
        if usage is not None:
            # The provider's own numbers, recorded and not derived. There is no cost figure
            # here on purpose: pricing is not in the response, and a hard-coded rate would be
            # a number that is wrong the first time AWS changes one (ADR 0022 decision 8).
            logger.info(
                "bedrock converse: model=%s input_tokens=%d output_tokens=%d",
                model,
                usage[0],
                usage[1],
            )
        return _json_text(response_text(response, model))


def converse_request(
    messages: Sequence[ChatMessage],
    *,
    model: str,
    temperature: float | None,
    max_tokens: int,
) -> dict[str, Any]:
    """The `converse` keyword arguments for one exchange, as a plain dictionary.

    **A pure function, so the mapping can be read and tested without a client.** Converse splits
    a conversation two ways that OpenAI's schema does not: system instructions live in their own
    top-level `system` field rather than as a turn, and every turn's content is a list of typed
    blocks rather than a string. Both translations happen here and nowhere else.

    **No prompt text is changed, added or reordered.** The system block carries exactly what
    `llm_client._system_prompt` produced, and the user and assistant turns carry exactly what
    the caller wrote - which is what makes the two providers comparable at all.
    """
    system = [{"text": message.content} for message in messages if message.role == "system"]
    turns = [
        {"role": message.role, "content": [{"text": message.content}]}
        for message in messages
        if message.role != "system"
    ]

    inference: dict[str, Any] = {"maxTokens": max_tokens}
    if temperature is not None:
        # Omitted unless a caller asked, so the model's own default stays in force - the same
        # rule the OpenAI adapter follows with `omit`.
        inference["temperature"] = temperature

    request: dict[str, Any] = {
        "modelId": model,
        "messages": turns,
        "inferenceConfig": inference,
    }
    if system:
        request["system"] = system
    return request


def response_text(response: Any, model: str) -> str:
    """The assistant's text out of a Converse response.

    A response with content blocks and no text in them - a tool-use block, or an empty list -
    returns "", which is the same answer an empty OpenAI completion gives and reaches the same
    validation retry.

    A response that is not shaped like a Converse response at all is a different thing and is
    raised as `fatal`: it means the SDK, the model or the endpoint is not what this code was
    written against, and no retry schedule improves that.
    """
    try:
        blocks = response["output"]["message"]["content"]
    except (KeyError, TypeError, IndexError) as error:
        raise ProviderError(
            "fatal", f"{model} returned no Converse output block: {response!r}"
        ) from error

    if not isinstance(blocks, list):
        raise ProviderError("fatal", f"{model} returned a malformed content list: {blocks!r}")

    return "".join(
        str(block["text"]) for block in blocks if isinstance(block, dict) and "text" in block
    )


def token_usage(response: Any) -> tuple[int, int] | None:
    """`(input_tokens, output_tokens)` when Converse reported them, else None.

    Converse returns a `usage` block with `inputTokens` and `outputTokens`. It is read
    defensively and never computed: a missing block means the numbers are unknown, and an
    unknown token count is reported as unknown rather than estimated.
    """
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    incoming, outgoing = usage.get("inputTokens"), usage.get("outputTokens")
    if not isinstance(incoming, int) or not isinstance(outgoing, int):
        return None
    return incoming, outgoing


def build_bedrock_provider(*, region: str, app_timeout_s: float) -> BedrockConverseProvider:
    """The production client. **It takes no credential and no endpoint override.**

    boto3 finds the ECS task role through its ordinary provider chain, which is the whole of
    Bedrock authentication here: no key to rotate, nothing in Secrets Manager, and nothing in
    the task definition but a region and a model id (ADR 0022 decision 3).

    `app_timeout_s` is the longest request the caller will wait for. The socket is built to
    outlast it by `SDK_TIMEOUT_HEADROOM_S`, so a slow generation ends as a slow generation
    rather than as a transport failure.

    **botocore's own retries are off** (`total_max_attempts=1`). Left on, its `standard` mode
    would retry a throttled request underneath `LLMClient._send`, turning one counted call into
    three uncounted ones against a shared rate limit that granted one token.
    """
    settings = client_config(app_timeout_s)
    client = boto3.client("bedrock-runtime", region_name=region, config=settings)
    return BedrockConverseProvider(client=client, read_timeout_s=settings.read_timeout)


def client_config(app_timeout_s: float) -> BotoConfig:
    """The botocore settings the client is built with.

    **A pure function, separate from `build_bedrock_provider`, so the two properties that
    matter can be read without constructing a client.** Constructing one resolves the AWS
    credential chain, which would mean the offline suite reading whatever profile the
    developer's machine happens to have - and the rule for these tests is that no AWS
    credential, profile or metadata service is ever consulted.
    """
    return BotoConfig(
        connect_timeout=CONNECT_TIMEOUT_S,
        read_timeout=app_timeout_s + SDK_TIMEOUT_HEADROOM_S,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )


def _from_client_error(error: ClientError, model: str) -> ProviderError:
    """One AWS error code, classified into the three kinds the caller answers.

    The default is `fatal`, deliberately. An unrecognised code is more likely to be a denied
    action or a bad request than a transient one, and retrying the former costs three calls and
    three tokens to produce the same message three times.
    """
    code = str(error.response.get("Error", {}).get("Code", ""))
    kind: ProviderFailureKind = (
        "rate_limited"
        if code in RATE_LIMITED_CODES
        else "transport"
        if code in TRANSPORT_CODES
        else "fatal"
    )
    retry_after = _retry_after_header(error) if kind == "rate_limited" else None
    return ProviderError(kind, f"{model}: {code or 'unknown error'}", retry_after=retry_after)


def _retry_after_header(error: ClientError) -> float | None:
    """Bedrock's own directive in seconds, bounded, or None to use the caller's schedule.

    Bounded by `llm_client.MAX_RETRY_AFTER_S` rather than by a number of this file's own: the
    argument for a ceiling is the caller's - a provider must not be able to replace a finite
    retry policy with an arbitrarily long sleep - so the ceiling is the caller's too.
    """
    metadata = error.response.get("ResponseMetadata")
    headers = metadata.get("HTTPHeaders", {}) if isinstance(metadata, dict) else {}
    raw = headers.get("retry-after") if isinstance(headers, dict) else None
    if raw is None:
        return None
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        return None
    if delay < 0:
        return None
    return min(delay, MAX_RETRY_AFTER_S)


_FENCE = "```"


def _json_text(text: str) -> str:
    """The JSON object out of a reply that may have wrapped it in a markdown fence.

    **The one place this file touches what the model said, and it removes rather than adds.**
    The system prompt already asks for no fences; Nova has no JSON mode that could enforce it,
    and a fenced object is a formatting slip rather than a wrong answer. Left alone it would
    fail validation and spend the one retry - and a call from a 60-call budget - on punctuation.

    Anything that is not exactly a fenced block is returned untouched, so a reply that merely
    contains a backtick is not rewritten.
    """
    stripped = text.strip()
    if not stripped.startswith(_FENCE) or not stripped.endswith(_FENCE):
        return text

    inner = stripped[len(_FENCE) : -len(_FENCE)]
    # ```json\n{...}\n``` - the opening fence may name a language, which is the first line.
    first, newline, rest = inner.partition("\n")
    if newline and not first.strip().startswith("{"):
        inner = rest
    return inner.strip()
