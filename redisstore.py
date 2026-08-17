"""
WHY THIS FILE EXISTS
    The one place that talks to Redis, the way `jobqueue.py` is the one place that talks to
    SQS. Everything above it deals in the protocols it implements - `ToolCache`,
    `UrlDeduplicator`, and the `RateLimiter` the LLM client declares - so no agent, no tool
    and no node ever imports `redis`.

    **It holds three responsibilities and exactly two failure policies, and the split is the
    whole design** (ARCHITECTURE.md §20 row 29, guidelines §11):

    | Responsibility | Key | When Redis is down |
    |---|---|---|
    | Search cache | `cache:search:{hash}` | **Fail open.** Treat it as a miss |
    | Fetch cache | `cache:fetch:{hash}` | **Fail open.** Treat it as a miss |
    | Per-job URL dedupe | `job:{id}:urls` | **Fail open.** Allow the fetch |
    | Shared LLM rate limiter | `ratelimit:llm` | **FAIL CLOSED.** No token, no LLM call |

    A cache miss costs one call, which `MAX_LLM_CALLS_PER_JOB` already bounds. An unlimited
    limiter costs the whole tier at once, across every worker - so **a limiter that fails
    open is not a limiter**, and the two halves of this file behave in opposite directions on
    purpose. `RedisCache` and `RedisUrlDeduplicator` swallow `RedisError` and answer as
    though the key were absent; `RedisRateLimiter` turns it into a refusal.

    **The limiter is one Lua script rather than three commands, and that is not an
    optimisation.** The failure it exists to prevent is *"two workers each politely limiting
    themselves to 40 requests per minute produce 80"* (guidelines §11) - which is a race
    between processes. A read-then-write across two round trips reintroduces exactly that
    race one level up, so the trim, the count and the insert are one atomic script. It is the
    cross-process version of the argument `CallBudget._lock` already makes in `llm_client.py`.

    **The window is a sorted set because guidelines §11 says "rolling 60s".** A fixed-window
    counter is one INCR and would be simpler, and it permits a full limit at the end of one
    window and another at the start of the next - 80 requests in a moment against a limit of
    40, which is the number this exists to hold.

WHO CALLS IT
    `worker.py` builds all three at startup - it is the process that runs graphs and calls
    models. `app.py` builds only the health probe, because the API reaches Redis for exactly
    one reason: to say whether the workers can work (guidelines §12, ARCHITECTURE.md §8).
    Nothing else imports `redis`.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, cast

from redis import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

OPERATION_TIMEOUT_S = 5.0
"""How long any single Redis command may take (guidelines §17's rate-limiter row).

The row gives the limiter 5s, borrowed from the database-query row. It is applied to every
command rather than only to the limiter's, because the alternative is two clients with two
timeouts and one of them undocumented - and on the fail-open paths a bound this size is
already far longer than the work it is avoiding.
"""

URL_SET_TTL_S = 6 * 60 * 60
"""guidelines §11's `job:{id}:urls` row. Long enough to outlive a job - the 20-minute
runtime bound is per invocation, and a job that waits at the gate holds no URLs it has not
already fetched - and short enough that a finished job's set does not sit there for a day."""

RATE_LIMIT_KEY = "ratelimit:llm"
"""guidelines §11. One key for the whole deployment, which is what "shared and global"
means: every worker's requests land in the same window."""

RATE_LIMIT_WINDOW_S = 60
"""The "rolling 60s" of guidelines §11, against which `LLM_RPM_LIMIT` is counted."""

_ACQUIRE_TOKEN = """
local cutoff = tonumber(ARGV[1]) - tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) then
    redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
    redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
    return 1
end
return 0
"""
"""Trim the window, count what is left, and take a token if there is room - atomically.

`PEXPIRE` on every successful acquisition is what stops the key outliving the traffic: a
deployment that goes quiet drops its bucket a window later rather than keeping a sorted set
alive forever.
"""


def build_redis(url: str) -> Redis:
    """The client every object below shares.

    `decode_responses` is on because everything stored here is text - a cached JSON body, a
    normalised URL, a token id - and one decoding rule beats each caller remembering.

    Both timeouts are set: without `socket_connect_timeout` an unreachable host blocks for
    the operating system's connect timeout, which on some platforms is a minute or more, and
    a fail-open cache that stalls a node for a minute has failed neither open nor closed.
    """
    return Redis.from_url(
        url,
        decode_responses=True,
        socket_timeout=OPERATION_TIMEOUT_S,
        socket_connect_timeout=OPERATION_TIMEOUT_S,
    )


def reachable(client: Redis) -> bool:
    """Whether Redis answers. Used by `/health` and by the worker's startup check.

    A boolean rather than an exception because both callers want to *report* the answer -
    one in a JSON body a stranger may read, one in a startup message - and neither wants the
    connection error's text, which can carry a host and a port (guidelines §16).
    """
    try:
        return bool(client.ping())
    except RedisError:
        logger.exception("redis did not answer a ping")
        return False


class RedisHealth:
    """`routes.api.RedisProbe`: one method, for `/health` and nothing else.

    It exists so the API can hold a Redis dependency without holding a cache or a bucket it
    would never use. The worker calls `reachable()` above directly, because at startup there
    is no route layer to keep narrow.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    def reachable(self) -> bool:
        return reachable(self._client)


class RedisCache:
    """`cache:search:{hash}` and `cache:fetch:{hash}` (guidelines §7, §11). **Fails open.**

    It implements `tools.contracts.ToolCache` and adds nothing to it: the keys and the TTLs
    are the callers' - `tools/search.py` and `tools/fetch.py` build both today and have since
    Phase 1 - so this file cannot change what is cached or for how long, only where it lands.

    Every path swallows `RedisError` and answers as though the key were absent, which is what
    that protocol's docstring already promises callers: *"An implementation should therefore
    swallow its own connection errors and return None, not raise."*
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    def get(self, key: str) -> str | None:
        try:
            value = cast(str | None, self._client.get(key))
        except RedisError:
            # Not `exception`: a Redis outage would otherwise write one stack trace per
            # search and per fetch, and the caller is about to log the miss anyway.
            logger.warning("redis unavailable reading %s; treating it as a miss", key)
            return None
        return value

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        # `set(..., ex=)` rather than `setex`, which redis-py deprecated: one write, one
        # expiry, and no window in which the key exists without a TTL.
        try:
            self._client.set(key, value, ex=ttl_seconds)
        except RedisError:
            logger.warning("redis unavailable writing %s; the entry is not cached", key)


class RedisUrlDeduplicator:
    """The per-job `job:{id}:urls` set (guidelines §7, §11). **Fails open.**

    It implements `tools.contracts.UrlDeduplicator`. The Researcher already keeps an
    in-process `seen` set seeded from the job's own findings; this is the half that survives
    a process, so a redelivered message or a second Researcher visit does not re-fetch a page
    the job has already read.

    **A failure allows the fetch.** The cost is one wasted fetch and a duplicate finding,
    both bounded by `MAX_LLM_CALLS_PER_JOB` - and refusing to research because a
    deduplication cache is down would trade a real outage for an optimisation.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    def add_if_new(self, job_id: str, url: str) -> bool:
        """True when this job had not seen `url`, and it is now recorded.

        `SADD` answers how many members it added, so "was it new?" and "record it" are one
        round trip rather than a read followed by a write that another visit could interleave
        with. The TTL is refreshed on every addition, which keeps a long job's set alive for
        as long as the job keeps finding pages.
        """
        key = f"job:{job_id}:urls"
        try:
            added = self._client.sadd(key, url)
            if added:
                self._client.expire(key, URL_SET_TTL_S)
        except RedisError:
            logger.warning("redis unavailable checking %s; allowing the fetch", key)
            return True
        return bool(added)


class RedisRateLimiter:
    """The shared `ratelimit:llm` token bucket (guidelines §11). **Fails closed.**

    One key across every worker, because a per-process limiter is a different thing wearing
    the same name. The bound and the retries are guidelines §17's rate-limiter row - 5s per
    attempt, two retries at 2s and 8s - and both are the caller's, in `llm_client.py`, so
    that this object answers exactly one question: is there a token right now?

    **A refusal is a `False`, not an exception, and a Redis outage is also a `False`.**
    guidelines §11 says *"no token, no LLM call"* without distinguishing the two causes, and
    the caller's correct response is identical either way - so the log line below says which
    happened and the return value says what it means. Answering `False` rather than raising is
    also what keeps this module free of any dependency on `llm_client`: the failure *reason* a
    caller routes on belongs next to the other four, not here.
    """

    def __init__(self, client: Redis, *, requests_per_minute: int) -> None:
        self._client = client
        self._limit = requests_per_minute
        self._acquire = client.register_script(_ACQUIRE_TOKEN)

    def acquire(self) -> bool:
        """Take one token if the window has room. True when one was taken.

        The token's member is a fresh uuid rather than the timestamp: two requests in the same
        millisecond would otherwise be one member of the sorted set, and the second would be
        granted for free - which is the over-grant this whole file exists to prevent, arriving
        by a different door.
        """
        now_ms = int(time.time() * 1000)
        try:
            granted = cast(
                Any,
                self._acquire(
                    keys=[RATE_LIMIT_KEY],
                    args=[now_ms, RATE_LIMIT_WINDOW_S * 1000, self._limit, uuid.uuid4().hex],
                ),
            )
        except RedisError:
            logger.exception("redis could not be reached for a rate-limit token")
            return False

        if not int(granted):
            logger.warning(
                "rate limit reached: %d requests already sent in the last %ds",
                self._limit,
                RATE_LIMIT_WINDOW_S,
            )
            return False
        return True
