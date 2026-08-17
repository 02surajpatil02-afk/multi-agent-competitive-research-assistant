"""
WHY THIS FILE EXISTS
    `redisstore.py` holds three responsibilities and two opposite failure policies
    (ARCHITECTURE.md §20 row 29), and the policies are the part that is expensive to get
    wrong: a cache that raised would turn a Redis blip into a failed job, and a limiter that
    returned True would hand the whole LLM tier to every worker at once.

    **This file tests the failure half offline, and only the failure half.** What it needs
    for that is a *broken* Redis, not a working one - a client whose commands raise - and
    that is something a few lines can be. What it deliberately does not do is fake a working
    Redis: `tools/contracts.py` already ruled that out in terms, and the reason applies here
    too:

        "No in-memory stand-in is provided - one would pass the tests while telling you
        nothing about the behaviour that matters, which is what happens when the cache is
        shared across workers and when it is unavailable."

    So storing, reading, expiring, and above all *sharing* are proven against the real Redis
    7 in tests/test_redis_integration.py, which is marked `redis`. Between the two files:
    this one says what happens when Redis is gone, that one says what happens when it is not.

WHO CALLS IT
    pytest. No service, no network - every client here is broken on purpose.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from redisstore import (
    RATE_LIMIT_KEY,
    URL_SET_TTL_S,
    RedisCache,
    RedisHealth,
    RedisRateLimiter,
    RedisUrlDeduplicator,
    reachable,
)

JOB = "11111111-1111-4111-8111-111111111111"
URL = "https://example.com/report"


class _BrokenRedis:
    """Every command raises, which is what an unreachable Redis looks like from here.

    `redis-py` raises `ConnectionError` for a host that will not answer and `TimeoutError`
    for one that answers too slowly; both derive from `RedisError`, which is what
    `redisstore` catches. One of them is enough to prove the branch.
    """

    def __init__(self) -> None:
        self.attempts = 0

    def _fail(self, *_args: Any, **_kwargs: Any) -> Any:
        self.attempts += 1
        raise RedisConnectionError("redis is not answering")

    ping = get = set = sadd = expire = _fail

    def register_script(self, _source: str) -> Any:
        return self._fail


def _broken() -> tuple[Redis, _BrokenRedis]:
    """A broken client, typed as the real one. The cast is the whole point of the fake: what
    is under test is `redisstore`'s error handling, not `redis-py`."""
    broken = _BrokenRedis()
    return cast(Redis, broken), broken


# --- Fail open: the caches (guidelines §11, ARCHITECTURE.md §20 row 29) ------------------


def test_a_read_against_a_dead_redis_is_a_miss_not_a_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The contract `ToolCache` states: *"swallow its own connection errors and return None,
    not raise"*. A miss costs one call, which `MAX_LLM_CALLS_PER_JOB` already bounds - and
    refusing to research because a cache is down would trade a real outage for an
    optimisation."""
    client, broken = _broken()

    with caplog.at_level(logging.WARNING):
        value = RedisCache(client).get("cache:search:abc")

    assert value is None
    assert broken.attempts == 1
    assert "treating it as a miss" in caplog.text


def test_a_write_against_a_dead_redis_is_dropped_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The write is the half that runs *after* the work succeeded, so raising here would fail
    # a search or a fetch that had already produced its answer.
    client, broken = _broken()

    with caplog.at_level(logging.WARNING):
        RedisCache(client).set("cache:fetch:abc", "{}", 3600)

    assert broken.attempts == 1
    assert "is not cached" in caplog.text


def test_a_dead_redis_allows_the_fetch(caplog: pytest.LogCaptureFixture) -> None:
    """The URL set fails open too, and `True` is the permissive answer: this job has not
    fetched the URL, so go ahead. The cost is one wasted fetch and a duplicate finding, both
    bounded by the call budget."""
    client, broken = _broken()

    with caplog.at_level(logging.WARNING):
        allowed = RedisUrlDeduplicator(client).add_if_new(JOB, URL)

    assert allowed is True
    assert broken.attempts == 1
    assert "allowing the fetch" in caplog.text


def test_the_url_set_is_keyed_and_expired_as_documented() -> None:
    """guidelines §11's `job:{id}:urls` row, asserted on the commands rather than on a value.

    The TTL is refreshed only when a member is genuinely new, which is what keeps a long
    job's set alive without rewriting an expiry on every duplicate.
    """
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class _Recording:
        def sadd(self, key: str, *values: str) -> int:
            calls.append(("sadd", (key, *values)))
            return 1 if values[0] == URL else 0

        def expire(self, key: str, ttl: int) -> bool:
            calls.append(("expire", (key, ttl)))
            return True

    deduplicator = RedisUrlDeduplicator(cast(Redis, _Recording()))

    assert deduplicator.add_if_new(JOB, URL) is True
    assert deduplicator.add_if_new(JOB, "https://example.com/other") is False
    assert calls == [
        ("sadd", (f"job:{JOB}:urls", URL)),
        ("expire", (f"job:{JOB}:urls", URL_SET_TTL_S)),
        ("sadd", (f"job:{JOB}:urls", "https://example.com/other")),
    ]
    assert URL_SET_TTL_S == 6 * 60 * 60  # guidelines §11


# --- Fail closed: the limiter (guidelines §11, §17) --------------------------------------


def test_a_dead_redis_grants_no_token(caplog: pytest.LogCaptureFixture) -> None:
    """**The decisive test for the fail-closed half.** A limiter that answered `True` when it
    could not reach its bucket would not be a limiter: every worker would discover the tier's
    real ceiling simultaneously, by hitting 429s together (guidelines §11)."""
    client, _ = _broken()

    with caplog.at_level(logging.ERROR):
        granted = RedisRateLimiter(client, requests_per_minute=40).acquire()

    assert granted is False
    assert "rate-limit token" in caplog.text


def test_a_full_window_grants_no_token(caplog: pytest.LogCaptureFixture) -> None:
    # The other cause of "no token", and it produces the same answer on purpose: §11's rule
    # is "no token, no LLM call", and the caller's response is identical either way.
    class _Full:
        def register_script(self, _source: str) -> Any:
            return lambda keys, args: 0

    with caplog.at_level(logging.WARNING):
        granted = RedisRateLimiter(cast(Redis, _Full()), requests_per_minute=40).acquire()

    assert granted is False
    assert "rate limit reached" in caplog.text


def test_the_limiter_asks_for_one_key_and_the_configured_limit() -> None:
    """The arguments the Lua script is handed, which is where "shared and global" is decided.

    One key for the whole deployment - not one per worker, not one per job - is what makes
    two workers share a ceiling rather than each keep their own (guidelines §11).
    """
    seen: list[tuple[list[str], list[Any]]] = []

    class _Recording:
        def register_script(self, _source: str) -> Any:
            def run(keys: list[str], args: list[Any]) -> int:
                seen.append((keys, args))
                return 1

            return run

    assert RedisRateLimiter(cast(Redis, _Recording()), requests_per_minute=40).acquire() is True

    keys, args = seen[0]
    assert keys == [RATE_LIMIT_KEY] == ["ratelimit:llm"]
    assert args[1] == 60_000  # the rolling 60s window, in milliseconds
    assert args[2] == 40  # LLM_RPM_LIMIT, not a per-process share of it


def test_two_tokens_in_the_same_millisecond_are_two_members() -> None:
    """The over-grant that a timestamp-keyed member would let through.

    Two requests in one millisecond would be one member of the sorted set, so the second
    would be free - the exact failure this file's fail-closed half exists to prevent,
    arriving by a different door.
    """
    members: list[str] = []

    class _Recording:
        def register_script(self, _source: str) -> Any:
            def run(keys: list[str], args: list[Any]) -> int:
                members.append(str(args[3]))
                return 1

            return run

    limiter = RedisRateLimiter(cast(Redis, _Recording()), requests_per_minute=40)
    limiter.acquire()
    limiter.acquire()

    assert len(set(members)) == 2


# --- The health probe ---------------------------------------------------------------------


def test_an_unreachable_redis_reports_unreachable(caplog: pytest.LogCaptureFixture) -> None:
    client, _ = _broken()

    with caplog.at_level(logging.ERROR):
        assert reachable(client) is False
        assert RedisHealth(client).reachable() is False


def test_the_probe_answers_a_boolean_rather_than_the_connection_error() -> None:
    """`/health` is unauthenticated and its body must carry no host, no port and no error
    text (guidelines §16), so the probe returns a boolean and logs the rest."""
    client, _ = _broken()

    answer = RedisHealth(client).reachable()

    assert answer is False
    assert isinstance(answer, bool)
