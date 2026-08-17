"""
WHY THIS FILE EXISTS
    search() has one job an agent can see - return SearchResults - and several an agent
    should never have to think about: refuse a bad query, retry a flaky service exactly
    twice, cap page text, and collapse duplicate URLs. These tests pin all of them against
    a scripted Tavily, with no credentials and no network.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from fakes import FakeCache, FakeTavily
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from tavily import InvalidAPIKeyError, TavilyClient, UsageLimitExceededError

import tools.search
from config import Config, load_config
from redisstore import RedisCache
from schemas import SearchResult
from tools.contracts import ToolCallFailed
from tools.search import search

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
}


def _config(**overrides: str) -> Config:
    return load_config({**_ENV, **overrides})


def _result(url: str, content: str = "Revenue grew 12%.", **extra: Any) -> dict[str, Any]:
    return {"title": "A page", "url": url, "raw_content": content, **extra}


def _payload(*results: dict[str, Any]) -> dict[str, Any]:
    return {"results": list(results)}


def _tavily(*script: Any) -> tuple[TavilyClient, FakeTavily]:
    fake = FakeTavily(script=list(script))
    return cast(TavilyClient, fake), fake


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(tools.search, "sleep", recorded.append)
    return recorded


# --- A valid search ----------------------------------------------------------------


def test_a_valid_search_returns_normalised_results() -> None:
    client, _ = _tavily(_payload(_result("https://example.com/a")))

    results = search("cloud strategy", config=_config(), client=client)

    assert len(results) == 1
    assert isinstance(results[0], SearchResult)
    assert str(results[0].url) == "https://example.com/a"
    assert results[0].content == "Revenue grew 12%."
    assert results[0].truncated is False


def test_the_cleaned_query_is_what_gets_sent() -> None:
    client, fake = _tavily(_payload(_result("https://example.com/a")))

    search("  cloud\x00  strategy  ", config=_config(), client=client)

    assert fake.calls[0]["query"] == "cloud strategy"


def test_the_documented_timeout_is_applied() -> None:
    client, fake = _tavily(_payload(_result("https://example.com/a")))

    search("cloud", config=_config(), client=client)

    assert fake.calls[0]["timeout"] == 15.0


def test_the_full_page_is_requested_not_the_snippet() -> None:
    # SearchResult.content is documented as page text, not a snippet, and the audit trail
    # needs the text the quote came from.
    client, fake = _tavily(_payload(_result("https://example.com/a")))

    search("cloud", config=_config(), client=client)

    assert fake.calls[0]["include_raw_content"] is True


def test_the_short_extract_is_used_when_there_is_no_full_page() -> None:
    payload = _payload({"title": "A", "url": "https://example.com/a", "content": "short extract"})
    client, _ = _tavily(payload)

    assert search("cloud", config=_config(), client=client)[0].content == "short extract"


def test_a_publication_date_is_parsed_when_present() -> None:
    client, _ = _tavily(_payload(_result("https://example.com/a", published_date="2024-10-21")))

    published = search("cloud", config=_config(), client=client)[0].published_at

    assert published is not None
    assert published.year == 2024


def test_an_unparseable_date_costs_the_date_not_the_source() -> None:
    client, _ = _tavily(_payload(_result("https://example.com/a", published_date="last Tuesday")))

    results = search("cloud", config=_config(), client=client)

    assert len(results) == 1
    assert results[0].published_at is None


# --- Malformed arguments -----------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", "x" * 401])
def test_a_malformed_query_is_rejected_before_any_call(query: str) -> None:
    client, fake = _tavily()

    with pytest.raises(ToolCallFailed) as raised:
        search(query, config=_config(), client=client)

    assert raised.value.reason == "invalid_query"
    assert fake.calls == []  # nothing left the process


# --- Normalisation and deduplication -----------------------------------------------


def test_duplicate_urls_in_one_response_are_collapsed() -> None:
    # The same page twice is double-counted evidence, not two sources.
    client, _ = _tavily(
        _payload(
            _result("https://example.com/a"),
            _result("HTTPS://Example.com/a#intro"),
            _result("https://example.com/b"),
        )
    )

    results = search("cloud", config=_config(), client=client)

    assert [str(result.url) for result in results] == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_results_carry_the_normalised_url() -> None:
    client, _ = _tavily(_payload(_result("HTTPS://Example.COM:443/a#top")))

    assert str(search("cloud", config=_config(), client=client)[0].url) == "https://example.com/a"


@pytest.mark.parametrize(
    "broken",
    [
        {"title": "A", "url": "", "raw_content": "text"},
        {"title": "A", "url": "https://example.com/a", "raw_content": ""},
        {"title": "A", "raw_content": "text"},
        "not even a dict",
    ],
)
def test_an_unusable_result_costs_one_source_not_the_query(broken: Any) -> None:
    client, _ = _tavily(_payload(broken, _result("https://example.com/good")))

    results = search("cloud", config=_config(), client=client)

    assert [str(result.url) for result in results] == ["https://example.com/good"]


def test_no_results_is_an_empty_list_not_an_error() -> None:
    client, _ = _tavily(_payload())

    assert search("cloud", config=_config(), client=client) == []


# --- MAX_PAGE_CHARS on the search path ----------------------------------------------


def test_page_text_is_capped_and_the_cut_is_recorded() -> None:
    # The cap applies here too: search content reaches a prompt the same way fetched text
    # does, and the Finding built from it has to be able to say it was cut.
    config = _config(MAX_PAGE_CHARS="50")
    client, _ = _tavily(_payload(_result("https://example.com/a", content="x" * 200)))

    result = search("cloud", config=config, client=client)[0]

    assert len(result.content) == 50
    assert result.truncated is True


def test_text_under_the_cap_is_not_marked_truncated() -> None:
    config = _config(MAX_PAGE_CHARS="50")
    client, _ = _tavily(_payload(_result("https://example.com/a", content="x" * 50)))

    assert search("cloud", config=config, client=client)[0].truncated is False


# --- External-service failure -------------------------------------------------------


def test_a_flaky_service_is_retried_on_the_documented_schedule(slept: list[float]) -> None:
    client, fake = _tavily(
        RuntimeError("connection reset"),
        RuntimeError("connection reset"),
        _payload(_result("https://example.com/a")),
    )

    results = search("cloud", config=_config(), client=client)

    assert len(results) == 1
    assert len(fake.calls) == 3  # 1 attempt + 2 retries
    assert slept == [1.0, 4.0]


def test_retry_exhaustion_is_loud(slept: list[float]) -> None:
    # Returning [] here would be indistinguishable from "the web has nothing on this",
    # and the caller has to be able to tell those apart.
    client, fake = _tavily(*[RuntimeError("connection reset") for _ in range(3)])

    with pytest.raises(ToolCallFailed) as raised:
        search("cloud", config=_config(), client=client)

    assert raised.value.reason == "search_unavailable"
    assert len(fake.calls) == 3
    assert slept == [1.0, 4.0]


@pytest.mark.parametrize(
    "error", [InvalidAPIKeyError("bad key"), UsageLimitExceededError("quota gone")]
)
def test_a_refusal_on_the_merits_is_not_retried(error: Exception, slept: list[float]) -> None:
    # A bad key is not going to become a good key two seconds later.
    client, fake = _tavily(error)

    with pytest.raises(ToolCallFailed) as raised:
        search("cloud", config=_config(), client=client)

    assert raised.value.reason == "search_unavailable"
    assert len(fake.calls) == 1
    assert slept == []


# --- The cache interface -------------------------------------------------------------


def test_a_miss_calls_the_service_and_stores_the_result() -> None:
    cache = FakeCache()
    client, fake = _tavily(_payload(_result("https://example.com/a")))

    search("cloud", config=_config(), client=client, cache=cache)

    assert len(fake.calls) == 1
    assert len(cache.sets) == 1
    key, ttl = cache.sets[0]
    assert key.startswith("cache:search:")
    assert ttl == 24 * 60 * 60


def test_a_hit_skips_the_service_entirely() -> None:
    cache = FakeCache()
    client, fake = _tavily(_payload(_result("https://example.com/a")))
    search("cloud", config=_config(), client=client, cache=cache)
    fake.calls.clear()

    results = search("cloud", config=_config(), client=client, cache=cache)

    assert fake.calls == []
    assert str(results[0].url) == "https://example.com/a"


def test_an_unreadable_cache_entry_is_a_miss_not_a_crash() -> None:
    # Caches fail open. A bad entry costs one call, which the job budget already bounds.
    cache = FakeCache(stored={"cache:search:unused": "{ this is not json"})
    client, fake = _tavily(_payload(_result("https://example.com/a")))
    search("cloud", config=_config(), client=client, cache=cache)
    cache.stored[cache.sets[0][0]] = "{ this is not json"

    client, fake = _tavily(_payload(_result("https://example.com/a")))
    results = search("cloud", config=_config(), client=client, cache=cache)

    assert len(results) == 1
    assert len(fake.calls) == 1  # the bad entry cost one call, not a crash


def test_what_is_cached_round_trips_to_the_same_results() -> None:
    cache = FakeCache()
    client, _ = _tavily(_payload(_result("https://example.com/a", content="x" * 500)))

    original = search("cloud", config=_config(MAX_PAGE_CHARS="100"), client=client, cache=cache)
    stored = json.loads(cache.stored[cache.sets[0][0]])

    assert [SearchResult.model_validate(item) for item in stored] == original
    assert stored[0]["truncated"] is True


def test_a_dead_redis_costs_one_call_and_not_the_search() -> None:
    """The fail-open rule end to end, through the real `RedisCache` (guidelines §11).

    The tests above prove the *interface* fails open, using a fake. This one composes the
    real implementation over a client whose every command raises, which is what a Redis
    outage actually looks like - and the search still returns its results. A cache that let
    a connection error through would turn a Redis blip into a failed subtopic.
    """
    cache = RedisCache(cast(Redis, _UnreachableRedis()))
    client, fake = _tavily(_payload(_result("https://example.com/a")))

    results = search("cloud", config=_config(), client=client, cache=cache)

    assert len(results) == 1
    assert len(fake.calls) == 1  # the outage cost one call, which the job budget bounds


class _UnreachableRedis:
    """Every command raises, the way redis-py reports a host that will not answer."""

    def get(self, _key: str) -> str | None:
        raise RedisConnectionError("redis is not answering")

    def set(self, _key: str, _value: str, ex: int | None = None) -> None:
        raise RedisConnectionError("redis is not answering")
