"""
WHY THIS FILE EXISTS
    One OpenAI-compatible client that every agent and the reflection node call through.
    There is no provider class and no provider abstraction: NIM in development and the
    production endpoint both speak the same protocol, so swapping a model is a config
    change plus a preflight plus an eval run, never a code change (ARCHITECTURE.md §20
    row 4).

    Putting all of it here means five agents do not each re-implement the same four
    concerns, and the numbers live in one place next to the guidelines section that
    states them:

      * Structured output. The caller passes a Pydantic schema; this file puts the JSON
        Schema in the prompt, asks for JSON mode, and validates the answer. Exactly one
        validation retry, and it carries the validation error into the retry prompt,
        because a retry that repeats the same prompt is a coin flip (guidelines §3).
      * Timeouts and transport retries, per model tier (guidelines §17).
      * 429 backoff. Numeric Retry-After is honoured through the documented 30-second
        policy ceiling; otherwise the client uses the normal schedule (guidelines §13, §17).
      * The per-job call budget. Every request sent counts, including retries, because
        every request spends a token of the same 40 RPM tier.
      * Tracing, when it is switched on. `wrap_openai` puts one LangSmith span around each
        request, carrying the model, the latency, the provider's own token usage, and the
        exception when there was one. It is a wrapper on the client rather than code in
        `_send`, so the retry logic below reads exactly as it did before tracing existed.

    What it deliberately does not do:

      * No observability of its own. There was a per-request `CallRecord` here, written to a
        log line and to `measurements/requests.jsonl`; it is gone. LangSmith records the same
        four facts - node, latency, tokens, outcome - and two systems answering one question
        is the cost without the benefit (CLAUDE.md's observability rows).
      * No rate limiter of its own. The shared bucket is one Redis key across every worker
        (`redisstore.RedisRateLimiter`), injected as `limiter=` and declared here as a
        Protocol, so this file needs no Redis dependency. A per-process limiter would be a
        different thing wearing the same name: two workers each politely limiting to 40 RPM
        produce 80. Passing none disables limiting entirely, which is what the offline suite
        and `scripts/measure_jobs.py` run on.
      * No writes to ResearchState. A LangGraph node owns its state update, so this file
        counts calls into a CallBudget the node hands it and the node writes the total
        back to llm_calls_used.
      * No prompts. What to ask is the agent's contract; how to ask it safely is this
        file's.

    Every failure arrives as LLMCallFailed carrying a reason, because the caller's
    correct response differs by reason (guidelines §17):

      | reason                    | caller's response                                    |
      | rate_limited              | fail the JOB, failure_reason="rate_limited"          |
      | budget_exceeded           | fail the JOB (CLAUDE.md invariant 3)                 |
      | llm_call_failed           | fail the NODE, record the reason                     |
      | invalid_output            | fail the NODE, record the reason (guidelines §3)     |
      | rate_limiter_unavailable  | fail the NODE, record the reason (guidelines §17)    |

    JOB_FATAL_REASONS is that first column, so the distinction is written down once
    rather than re-derived in five agent modules.

WHO CALLS IT
    The five agents and the reflection node, once each is written (step 8, step 9).
    scripts/check_model.py does NOT: the preflight tests endpoint capabilities such as
    tool calling that this client never uses, so it talks to the SDK directly.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass, field
from time import sleep
from typing import Literal, Protocol, TypeVar

from langsmith.wrappers import wrap_openai
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from config import Config, required

logger = logging.getLogger(__name__)

ModelTier = Literal["main", "fast"]
"""Which model runs this call. The tier picks the model, the timeout, and the transport
backoff together, because guidelines §17 sets all three per tier. `fast` is the Supervisor
and the reflection node; everything else is `main`."""

LLMFailureReason = Literal[
    "rate_limited",
    "budget_exceeded",
    "llm_call_failed",
    "invalid_output",
    "rate_limiter_unavailable",
]

JOB_FATAL_REASONS: frozenset[LLMFailureReason] = frozenset({"rate_limited", "budget_exceeded"})
"""Reasons that fail the whole job rather than one node. The other three fail the node,
which lets the graph carry on and report the gap.

`rate_limiter_unavailable` is deliberately not here: guidelines §17's row says *"fail the
node"*, and the node is the right blast radius because a limiter that is unreachable now may
answer on the next node - whereas a rate-limited endpoint has already told us the whole job
is over budget."""

# guidelines §17, one row per tier. A retry count and its backoff list are the same
# length in every row of that table, so the list length is the retry count.
_FAST_TIMEOUT_S = 30.0
"""guidelines §17's fast tier. The main tier's timeout is `Config.llm_main_timeout_s`, which
defaults to that table's 60s - only main is configurable, and that field says why."""

_TRANSPORT_BACKOFF_S: dict[ModelTier, tuple[float, ...]] = {
    "main": (2.0, 8.0),
    "fast": (1.0, 4.0),
}
_RATE_LIMIT_BACKOFF_S: tuple[float, ...] = (2.0, 8.0, 30.0)
MAX_RETRY_AFTER_S = max(_RATE_LIMIT_BACKOFF_S)
"""Largest provider-directed 429 delay we will serve.

Thirty seconds is already the retry policy's largest fallback and the fast tier's complete
request timeout.  Honouring a larger header would let the provider replace this client's finite
retry policy with an arbitrarily long sleep; clipping it preserves Retry-After semantics without
letting one response escape the job's bounded-wait policy.
"""

_LIMITER_BACKOFF_S: tuple[float, ...] = (2.0, 8.0)
"""guidelines §17's rate-limiter row: two retries, at 2s and 8s. The 5s bound on each
attempt belongs to the limiter's own client, because it is a socket timeout rather than
something this file can impose on a call it does not make.

It is a separate schedule from `_RATE_LIMIT_BACKOFF_S` above, which answers a 429 from the
*endpoint*. The two look alike and mean opposite things: one is our own bucket saying "not
yet", the other is the provider saying "you already did"."""

_VALIDATION_ATTEMPTS = 2
"""One call plus one validation retry, then fail explicitly (guidelines §3, §17)."""

_SCHEMA_INSTRUCTION = (
    "Return one JSON object and nothing else - no prose, no markdown fences. "
    "It must validate against this JSON Schema:\n{schema}"
)

_VALIDATION_RETRY_PROMPT = (
    "That response did not validate against the schema. Correct exactly these problems "
    "and return only the corrected JSON object:\n{errors}"
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class LLMCallFailed(RuntimeError):
    """A call that could not be completed, with the reason the caller routes on.

    One exception with a reason field rather than a hierarchy: callers branch on the
    reason, and `failure_reason` is already a string on ResearchState.
    """

    def __init__(self, reason: LLMFailureReason, message: str) -> None:
        super().__init__(message)
        self.reason: LLMFailureReason = reason


@dataclass
class CallBudget:
    """The per-job LLM call ceiling, carried through one node's work.

    A node builds one from state, hands it to the client, and writes `used` back to
    `llm_calls_used` when it returns. The Supervisor's guard normally stops a job before
    it gets here; this is the backstop that stops call 61 from ever being sent
    (ARCHITECTURE.md §16, CLAUDE.md invariant 3).

    Every request counts, retries included. `MAX_LLM_CALLS_PER_JOB` = 60 is the binding
    guard rather than headroom: the per-component caps sum above it (guidelines §13).

    **It is shared between threads.** Since ADR 0002 the Researcher runs a subtopic's
    extraction calls on a small pool, and every one of them spends from the same budget, so
    `spend()` is guarded. Nothing else here needs a lock: `used` is read after the pool has
    joined, and an int read is atomic anyway.
    """

    limit: int
    used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def spend(self) -> None:
        """Claim one call, or fail the job. Called immediately before each request.

        The check and the increment are one critical section. Without that, two threads can
        both read `used == 59` against a limit of 60 and both proceed, which is call 61 sent
        - and CLAUDE.md invariant 3 is a hard ceiling, not an approximate one.

        Worth knowing before anyone decides the lock is redundant: **the race it prevents
        cannot be provoked on a GIL build.** The check and the increment are a few bytecodes
        apart and the interpreter almost never switches between them - measured across
        switch intervals from 5ms to 100ns, an unguarded version never over-granted. The
        lock is here because CPython does not promise not to switch there, and because a
        free-threaded build removes the interpreter lock that is currently hiding it. The
        test that would catch a regression says the same thing about itself.
        """
        with self._lock:
            if self.used >= self.limit:
                raise LLMCallFailed(
                    "budget_exceeded",
                    f"job has used its budget of {self.limit} LLM calls",
                )
            self.used += 1


class RateLimiter(Protocol):
    """The shared `ratelimit:llm` bucket, as this file uses it (guidelines §11).

    Declared here rather than imported, for the same reason `ToolCache` is declared in
    `tools/contracts.py`: the consumer owns the contract, so this module needs no dependency
    on Redis and a test can hand in a scripted one.

    **`acquire()` answers, it does not wait.** A limiter that blocked would turn "the tier is
    saturated" into a stall with no bound, and the bound belongs to the caller, which is what
    guidelines §17's row gives it: 5s per attempt, two retries at 2s and 8s, then fail the
    node with `rate_limiter_unavailable`.
    """

    def acquire(self) -> bool: ...


class LLMClient:
    """The single client. One instance per process is enough; it holds no job state."""

    def __init__(
        self,
        config: Config,
        client: OpenAI | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._config = config
        # None means no limiting at all, which is Phase 1's behaviour and what the offline
        # suite runs on. The worker passes one, because it is the process that makes the
        # calls; nothing falls back to a per-process limiter, because a per-process limiter
        # would satisfy the type and not the requirement (guidelines §11).
        self._limiter = limiter
        # The two model names are narrowed here, once. `Config` carries them as optional so the
        # API process can start with no LLM credential at all (ADR 0012 decision 4) - and this
        # is the constructor of the thing that assumes one, so this is where the loud failure
        # belongs rather than at the first request.
        self._main_model = required(config.llm_model, "LLM_MODEL")
        self._fast_model = required(config.llm_fast_model, "LLM_FAST_MODEL")
        # The endpoint and the credential are narrowed on the same line as the models, and
        # only when this builds its own client: an injected one brought its own. Without it
        # the SDK raises `OpenAIError: Missing credentials`, which names `OPENAI_API_KEY` -
        # a variable this project does not have - instead of the one that is actually unset.
        self._client = client or OpenAI(
            base_url=required(config.llm_base_url, "LLM_BASE_URL"),
            api_key=required(config.llm_api_key, "LLM_API_KEY"),
            # The SDK retries by default. Left on, it would multiply every schedule in
            # guidelines §17 and silently spend budget nothing counted.
            max_retries=0,
        )
        if config.langsmith_tracing:
            # One span per request, with the model, the latency, the provider's usage block,
            # and the exception if it raised. Gated on the config flag so the test suite's
            # injected fakes are never wrapped, and so tracing off costs nothing.
            self._client = wrap_openai(self._client)

    def call_structured(
        self,
        *,
        schema: type[ModelT],
        system: str,
        user: str,
        budget: CallBudget,
        tier: ModelTier = "main",
    ) -> ModelT:
        """Ask for one JSON object, validate it against `schema`, and return it.

        Raises LLMCallFailed on every failure path. Never returns a partially valid
        object and never substitutes a default for a field the model did not produce: a
        wrong value survives into the report and looks deliberate (guidelines §3).
        """
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": _system_prompt(system, schema)},
            {"role": "user", "content": user},
        ]
        last_error: ValidationError | None = None

        for attempt in range(1, _VALIDATION_ATTEMPTS + 1):
            raw = self._send(messages, budget=budget, tier=tier)
            try:
                value = schema.model_validate_json(raw)
            except ValidationError as error:
                last_error = error
                logger.warning(
                    "%s did not validate on attempt %d of %d: %s",
                    schema.__name__,
                    attempt,
                    _VALIDATION_ATTEMPTS,
                    error,
                )
                if attempt < _VALIDATION_ATTEMPTS:
                    messages = [
                        *messages,
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": _VALIDATION_RETRY_PROMPT.format(errors=error),
                        },
                    ]
                continue
            return value

        raise LLMCallFailed(
            "invalid_output",
            f"{schema.__name__} did not validate after {_VALIDATION_ATTEMPTS} attempts",
        ) from last_error

    def _timeout_for(self, tier: ModelTier) -> float:
        """guidelines §17's per-tier timeout, with only the main tier configurable.

        A slow endpoint can invalidate the main row - generation speed decides how many
        output tokens fit in it - and cannot invalidate the fast row, whose callers produce
        a handful of tokens. `Config.llm_main_timeout_s` carries the whole argument.
        """
        return self._config.llm_main_timeout_s if tier == "main" else _FAST_TIMEOUT_S

    def _take_a_token(self, model: str) -> None:
        """One token from the shared bucket, or fail the node. **Never call the LLM without
        one** (guidelines §11, §17).

        **It is called immediately after `budget.spend()`, and that placement is the answer to
        "does a retry consume a token?" - yes.** A retried request is a real request against
        the same 40 RPM tier, which is the argument `CallBudget` already makes for counting
        every attempt: *"every request spends a token of the same tier"*. Putting the two
        claims on adjacent lines is what keeps them from drifting apart, because a limiter that
        counted logical calls while the budget counted attempts would let a job with three
        transport retries send four requests on one token.

        The bound is guidelines §17's rate-limiter row. Two retries at 2s and 8s, then the node
        fails with `rate_limiter_unavailable` - loudly, rather than waiting on a bucket that
        may never open. **A full bucket and an unreachable Redis end here identically**, on
        purpose: §11's rule is "no token, no LLM call", and the limiter's own log line is what
        says which of the two it was.

        `None` is no limiter at all - Phase 1, the offline suite, and `scripts/measure_jobs.py`
        - and it is the one path that sends a request without a token, because there is no
        bucket for it to come from.
        """
        if self._limiter is None:
            return

        for attempt, delay in enumerate((*_LIMITER_BACKOFF_S, None)):
            if self._limiter.acquire():
                return
            if delay is None:
                break
            logger.warning(
                "no rate-limit token for %s (attempt %d), retrying in %.1fs",
                model,
                attempt + 1,
                delay,
            )
            sleep(delay)

        raise LLMCallFailed(
            "rate_limiter_unavailable",
            f"no rate-limit token for {model} after {len(_LIMITER_BACKOFF_S)} retries",
        )

    def _send(
        self,
        messages: list[ChatCompletionMessageParam],
        *,
        budget: CallBudget,
        tier: ModelTier,
    ) -> str:
        """One logical request, with the transport and 429 retries around it.

        The two retry counters are independent because their exhaustion behaviour differs:
        a rate-limited job fails, a flaky connection fails only the node.
        """
        model = self._main_model if tier == "main" else self._fast_model
        transport_backoff = _TRANSPORT_BACKOFF_S[tier]
        transport_retries = 0
        rate_limit_retries = 0

        while True:
            budget.spend()
            self._take_a_token(model)
            try:
                response = self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    timeout=self._timeout_for(tier),
                )
            except RateLimitError as error:
                if rate_limit_retries == len(_RATE_LIMIT_BACKOFF_S):
                    # A rate-limited job fails visibly. It does not silently produce a
                    # shorter report (guidelines §13).
                    raise LLMCallFailed(
                        "rate_limited",
                        f"{model} rate limited after {rate_limit_retries} retries",
                    ) from error
                delay = _retry_after(error)
                if delay is None:
                    delay = _RATE_LIMIT_BACKOFF_S[rate_limit_retries]
                rate_limit_retries += 1
                logger.warning("%s rate limited, retrying in %.1fs", model, delay)
                sleep(delay)
            except (InternalServerError, APITimeoutError, APIConnectionError) as error:
                if transport_retries == len(transport_backoff):
                    raise LLMCallFailed(
                        "llm_call_failed",
                        f"{model} unreachable after {transport_retries} retries: {error}",
                    ) from error
                delay = transport_backoff[transport_retries]
                transport_retries += 1
                logger.warning("%s call failed (%s), retrying in %.1fs", model, error, delay)
                sleep(delay)
            except APIStatusError as error:
                # 4xx that is not a 429: a bad request, a bad key, an unknown model.
                # Retrying cannot fix any of them, so fail now and say the status.
                raise LLMCallFailed(
                    "llm_call_failed",
                    f"{model} rejected the request with status {error.status_code}",
                ) from error
            else:
                content = response.choices[0].message.content if response.choices else None
                # An empty completion is invalid output, not a transport problem: it goes
                # to the validation retry, which is the layer that can ask again better.
                return content or ""


def _system_prompt(system: str, schema: type[BaseModel]) -> str:
    """The agent's instructions, plus the JSON contract the client owns."""
    rendered = json.dumps(schema.model_json_schema(), separators=(",", ":"))
    return f"{system}\n\n{_SCHEMA_INSTRUCTION.format(schema=rendered)}"


def _retry_after(error: RateLimitError) -> float | None:
    """Bounded seconds from Retry-After, or None to use the documented schedule.

    The header may also carry an HTTP date, which is not worth parsing for a value that
    already has a sane fallback. Negative and non-finite numbers are invalid delays, and a
    numeric provider value cannot exceed the largest delay our own policy would choose.
    """
    response = getattr(error, "response", None)
    header = response.headers.get("retry-after") if response is not None else None
    if header is None:
        return None
    try:
        delay = float(header)
    except ValueError:
        return None
    if not math.isfinite(delay) or delay < 0:
        return None
    return min(delay, MAX_RETRY_AFTER_S)
