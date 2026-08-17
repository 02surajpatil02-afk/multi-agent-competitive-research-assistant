"""
WHY THIS FILE EXISTS
    tests/test_redisstore.py proves what happens when Redis is **gone**, which a broken client
    can show. It cannot show what happens when Redis is there, and that is where the three
    interesting properties live:

      * **Storage and expiry.** A TTL is a promise the server keeps, not the client.
      * **Atomicity.** The rate limiter's trim-count-insert is one Lua script precisely
        because a read-then-write across two round trips reintroduces the race between
        processes that the shared limiter exists to prevent.
      * **Sharing.** *"Two workers each politely limiting themselves to 40 requests per minute
        produce 80"* (guidelines §11) is a statement about two clients against one key, and no
        single-process fake can be wrong about it in the right way.

    `tools/contracts.py` already refused an in-memory stand-in for exactly this reason - *"one
    would pass the tests while telling you nothing about the behaviour that matters, which is
    what happens when the cache is shared across workers and when it is unavailable"* - so the
    behaviour that matters is proven here, against the Redis 7 in `docker-compose.yml`.

    **It refuses database 0.** Every case here flushes the database it is given, and database 0
    is what `REDIS_URL` defaults to - the one an application run locally would be using. The
    guard is the same shape as `pgharness`'s refusal of a database whose name does not say
    `test`, for the same reason: the guard is cheap and the mistake is not recoverable.

WHO CALLS IT
    `pytest -m redis`, with `TEST_REDIS_URL` set. Unset, every test here skips, which is what
    keeps plain `pytest` offline (guidelines §18).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest
from redis import Redis

from redisstore import (
    RATE_LIMIT_KEY,
    URL_SET_TTL_S,
    RedisCache,
    RedisHealth,
    RedisRateLimiter,
    RedisUrlDeduplicator,
    build_redis,
    reachable,
)

pytestmark = pytest.mark.redis

URL_VARIABLE = "TEST_REDIS_URL"

SKIP_REASON = (
    f"{URL_VARIABLE} is not set. Start the local infrastructure with "
    "`docker compose up -d --wait`, then set it to a Redis database that is not 0, "
    "for example redis://localhost:6379/15."
)

APPLICATION_DATABASE = 0
"""What `REDIS_URL` defaults to, and therefore the one database these tests must never touch:
every case below begins by flushing the one it is given."""

JOB = "11111111-1111-4111-8111-111111111111"

SHORT_TTL_S = 1
"""How long a key lives when a test is waiting for it to expire. Production's TTLs are 6h and
24h (guidelines §11); this exists so that "it really expired" is a fact a test can observe."""


@pytest.fixture
def redis_url() -> str:
    """The database these tests may flush, or a skip.

    Raises rather than skips when the URL is set but points at database 0: a developer who
    exported the wrong value wants to be told, and a silent skip would look like the suite
    passing.
    """
    url = os.environ.get(URL_VARIABLE, "").strip()
    if not url:
        pytest.skip(SKIP_REASON)

    database = url.rsplit("/", 1)[-1]
    if not database.isdigit() or int(database) == APPLICATION_DATABASE:
        raise RuntimeError(
            f"{URL_VARIABLE} names database {database!r}, and every test here begins by "
            f"flushing it. Point it at a numbered database other than {APPLICATION_DATABASE}."
        )
    return url


@pytest.fixture
def client(redis_url: str) -> Iterator[Redis]:
    """A client onto an empty database.

    Flushed on the way in rather than on the way out: a test that dies half way through
    leaves keys behind, and the next one should not inherit them.
    """
    connection = build_redis(redis_url)
    connection.flushdb()
    yield connection
    connection.close()


# --- The caches, against a server that really stores things -------------------------------


def test_a_value_written_comes_back(client: Redis) -> None:
    cache = RedisCache(client)

    cache.set("cache:search:abc", '[{"title": "A page"}]', 3600)

    assert cache.get("cache:search:abc") == '[{"title": "A page"}]'


def test_a_key_that_was_never_written_is_a_miss(client: Redis) -> None:
    assert RedisCache(client).get("cache:fetch:nothing-here") is None


def test_a_cached_value_expires_when_its_ttl_lapses(client: Redis) -> None:
    """The TTL is the server's promise, and this is the only place it can be checked.

    guidelines §11 gives both caches 24h; the value here is one second so the lapse is
    observable, and what is under test is that `setex` really carries an expiry rather than
    storing forever.
    """
    cache = RedisCache(client)
    cache.set("cache:search:brief", "{}", SHORT_TTL_S)
    assert cache.get("cache:search:brief") == "{}"

    time.sleep(SHORT_TTL_S + 0.5)

    assert cache.get("cache:search:brief") is None


def test_the_documented_ttl_is_what_lands_on_the_key(client: Redis) -> None:
    # guidelines §11's 24h row for `cache:fetch:{hash}`, read back off the server rather than
    # off the call. The caller supplies it - `tools/fetch.py` has since Phase 1 - so what this
    # pins is that the value survives the write unchanged.
    RedisCache(client).set("cache:fetch:abc", "{}", 24 * 60 * 60)

    assert 24 * 60 * 60 - 5 < client.ttl("cache:fetch:abc") <= 24 * 60 * 60


# --- The per-job URL set ------------------------------------------------------------------


def test_a_url_is_new_once_and_then_never_again(client: Redis) -> None:
    deduplicator = RedisUrlDeduplicator(client)

    assert deduplicator.add_if_new(JOB, "https://example.com/a") is True
    assert deduplicator.add_if_new(JOB, "https://example.com/a") is False


def test_two_jobs_do_not_share_a_url_set(client: Redis) -> None:
    """`job:{id}:urls` is per job, and the key is what makes it so. A URL one job read must
    not hide a page from another job that has never seen it."""
    deduplicator = RedisUrlDeduplicator(client)
    other = "22222222-2222-4222-8222-222222222222"

    assert deduplicator.add_if_new(JOB, "https://example.com/a") is True

    assert deduplicator.add_if_new(other, "https://example.com/a") is True


def test_the_url_set_carries_the_documented_expiry(client: Redis) -> None:
    # guidelines §11's 6h row. Without an expiry the set would outlive every job that ever
    # ran and Redis would accumulate one key per job forever.
    RedisUrlDeduplicator(client).add_if_new(JOB, "https://example.com/a")

    assert 0 < client.ttl(f"job:{JOB}:urls") <= URL_SET_TTL_S


def test_the_url_set_survives_a_new_client(redis_url: str, client: Redis) -> None:
    """The property the in-process `seen` set cannot have, and the reason this exists at all:
    a redelivered message runs a new process, and the URLs a dead invocation fetched are still
    known to the job."""
    RedisUrlDeduplicator(client).add_if_new(JOB, "https://example.com/a")

    second = build_redis(redis_url)
    try:
        assert RedisUrlDeduplicator(second).add_if_new(JOB, "https://example.com/a") is False
    finally:
        second.close()


# --- The shared limiter -------------------------------------------------------------------


def test_the_limiter_grants_exactly_its_limit_and_then_refuses(client: Redis) -> None:
    limiter = RedisRateLimiter(client, requests_per_minute=5)

    granted = [limiter.acquire() for _ in range(8)]

    assert granted == [True] * 5 + [False] * 3


def test_two_clients_share_one_bucket(redis_url: str, client: Redis) -> None:
    """**The decisive test for the whole limiter.** Two clients are two workers.

    guidelines §11's failure in one sentence: *"Two workers each politely limiting themselves
    to 40 requests per minute produce 80."* A per-process limiter passes every other test in
    this file and fails this one, which is why it is here and why it uses two connections
    rather than one object twice.
    """
    first = RedisRateLimiter(client, requests_per_minute=6)
    other = build_redis(redis_url)
    try:
        second = RedisRateLimiter(other, requests_per_minute=6)

        granted = sum(limiter.acquire() for limiter in (first, second) for _ in range(6))
    finally:
        other.close()

    assert granted == 6  # six between them, not six each


def test_concurrent_callers_never_over_grant(redis_url: str, client: Redis) -> None:
    """The race the Lua script exists to close, provoked rather than argued.

    Twelve threads on four connections ask for eight tokens at once. A read-then-write
    implementation over-grants here; an atomic one cannot, whatever the interleaving.
    """
    limit = 8
    connections = [build_redis(redis_url) for _ in range(4)]
    try:
        limiters = [RedisRateLimiter(each, requests_per_minute=limit) for each in connections]
        with ThreadPoolExecutor(max_workers=12) as pool:
            granted = list(pool.map(lambda index: limiters[index % 4].acquire(), range(12)))
    finally:
        for connection in connections:
            connection.close()

    assert sum(granted) == limit
    assert client.zcard(RATE_LIMIT_KEY) == limit


def test_the_bucket_expires_so_a_quiet_deployment_leaves_nothing_behind(client: Redis) -> None:
    # `PEXPIRE` on every acquisition. Without it the sorted set outlives the traffic and a
    # deployment that goes quiet keeps a key alive forever.
    RedisRateLimiter(client, requests_per_minute=5).acquire()

    assert 0 < client.ttl(RATE_LIMIT_KEY) <= 60


def test_a_token_becomes_available_again_as_the_window_rolls(client: Redis) -> None:
    """ "Rolling 60s" (guidelines §11), demonstrated at one second rather than sixty.

    A fixed-window counter would pass the exhaustion test above and fail this one in the
    other direction - it would release *all* its tokens at a boundary rather than each one
    as it ages out.
    """
    limiter = RedisRateLimiter(client, requests_per_minute=2)
    assert [limiter.acquire() for _ in range(3)] == [True, True, False]

    # Age the two tokens past the window by rewriting their scores, which is what sixty
    # seconds of waiting would do and takes no time at all.
    old = int(time.time() * 1000) - 61_000
    for member in cast(list[str], client.zrange(RATE_LIMIT_KEY, 0, -1)):
        client.zadd(RATE_LIMIT_KEY, {member: old})

    assert limiter.acquire() is True


# --- Health -------------------------------------------------------------------------------


def test_a_live_redis_reports_reachable(client: Redis) -> None:
    assert reachable(client) is True
    assert RedisHealth(client).reachable() is True


def test_a_closed_connection_reports_unreachable(redis_url: str) -> None:
    """What `/health` sees when Redis goes away mid-flight, against a real client rather than
    a fake: the port is refused, `redis-py` raises, and the probe answers `False`."""
    unreachable = build_redis(_with_a_port_nothing_listens_on(redis_url))

    try:
        assert RedisHealth(unreachable).reachable() is False
    finally:
        unreachable.close()


def _with_a_port_nothing_listens_on(url: str) -> str:
    """The same URL, pointed at a port no service holds. Cheaper and more deterministic than
    stopping the container, and it exercises the same `ConnectionError` path."""
    head, _, tail = url.rpartition(":")
    database = tail.split("/", 1)[1] if "/" in tail else "0"
    return f"{head}:1/{database}"


# --- The end-to-end wiring ----------------------------------------------------------------


def test_a_cache_a_url_set_and_a_limiter_share_one_client(client: Redis) -> None:
    """All three responsibilities on one connection, which is how `worker.py` builds them.

    They use different key spaces and different Redis types - a string, a set, a sorted set -
    so the only way they could interfere is a key collision, and this is where that would
    show up.
    """
    RedisCache(client).set("cache:search:abc", "[]", 60)
    RedisUrlDeduplicator(client).add_if_new(JOB, "https://example.com/a")
    RedisRateLimiter(client, requests_per_minute=5).acquire()

    keys = sorted(cast(list[str], client.keys("*")))

    assert keys == ["cache:search:abc", f"job:{JOB}:urls", RATE_LIMIT_KEY]
    assert cast(Any, client.type("cache:search:abc")) == "string"
    assert cast(Any, client.type(f"job:{JOB}:urls")) == "set"
    assert cast(Any, client.type(RATE_LIMIT_KEY)) == "zset"
