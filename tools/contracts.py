"""
WHY THIS FILE EXISTS
    The vocabulary the tool boundary speaks: how a call fails, how a source turns out to
    be unusable, and what a cache has to look like. It sits below search.py and fetch.py
    so both can share it without importing each other.

    The important distinction is between the two failure shapes, because the guidance
    handles them differently:

      * ToolCallFailed is raised. The argument was wrong, or the search service could not
        be reached after its bounded retries. The caller records it in state - a give-up
        is never swallowed (guidelines §7).

      * Unreachable is returned, not raised. A blocked, oversized, wrong-typed, or
        timed-out page is "a legitimate finding-level outcome, not an error to route
        around" (ARCHITECTURE.md §7). The job carries on and reports the gap; making it an
        exception would invite callers to treat a normal outcome as a crash.

WHO CALLS IT
    tools/search.py, tools/fetch.py, tools/validation.py, and the Researcher and
    Fact-Checker once they exist (step 8).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

ToolFailureReason = Literal[
    "invalid_query",
    "invalid_url",
    "blocked_url",
    "search_unavailable",
]

ARGUMENT_REASONS: frozenset[ToolFailureReason] = frozenset(
    {"invalid_query", "invalid_url", "blocked_url"}
)
"""Reasons where retrying the same argument is pointless, because the argument itself is
what was rejected. `search_unavailable` is the other kind: the argument was fine and the
service was not, so a different query - or the same one later - may well work."""


class ToolCallFailed(RuntimeError):
    """A tool call that could not be made, or could not be completed.

    One exception with a reason field rather than a hierarchy, matching llm_client.py:
    callers branch on the reason, and `failure_reason` is already a string on
    ResearchState.
    """

    def __init__(self, reason: ToolFailureReason, message: str) -> None:
        super().__init__(message)
        self.reason: ToolFailureReason = reason


UnreachableReason = Literal[
    "timeout",
    "connection_error",
    "http_error",  # 4xx - the page is not there, or not for us
    "server_error",  # 5xx - retried first, per guidelines §17
    "too_large",  # over MAX_FETCH_BYTES
    "wrong_content_type",  # not text/html or text/plain
    "redirect_blocked",  # a hop resolved to a private address
    "too_many_redirects",
    "robots_denied",
    "empty_content",  # fetched and cleaned, but there was no text in it
]


@dataclass(frozen=True)
class Unreachable:
    """A source that produced no usable page. Not an error - an outcome.

    At fact-check time an unreachable source produces `supported=false` with the note
    "source unreachable", never a guess (guidelines §2.5).
    """

    url: str
    reason: UnreachableReason
    detail: str


class ToolCache(Protocol):
    """The `cache:search:{hash}` / `cache:fetch:{hash}` contract (guidelines §11).

    Declared here and wired into both tools now; the Redis implementation lands in
    implementation step 21. No in-memory stand-in is provided - one would pass the tests
    while telling you nothing about the behaviour that matters, which is what happens when
    the cache is shared across workers and when it is unavailable.

    Caches fail open: when Redis is down a miss costs one extra call, which is bounded by
    MAX_LLM_CALLS_PER_JOB. An implementation should therefore swallow its own connection
    errors and return None, not raise.
    """

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...


class UrlDeduplicator(Protocol):
    """The per-job `job:{id}:urls` set (guidelines §7, §11).

    Not wired into search() or fetch(), because deduplication is scoped to a job and the
    tool functions do not take a job id - the Researcher does, and step 21 wires it at
    that call site. What this file can do without Redis is the part deduplication depends
    on: normalising a URL so that two spellings of one page compare equal. That lives in
    validation.py and is used today to collapse duplicates inside a single search
    response.
    """

    def add_if_new(self, job_id: str, url: str) -> bool: ...
