"""
WHY THIS FILE EXISTS
    `search(query)` - the first of the two tools. Tavily is used for exactly one reason:
    it returns cleaned page content **and** the URL together. The audit trail needs both,
    and stitching a separate scrape onto a URL-only search result is where content and
    citation quietly stop matching (guidelines §7).

    Everything Tavily hands back is normalised to SearchResult before an agent sees it, so
    no agent ever touches the provider's response shape. That is what lets the Researcher
    depend on this function rather than on Tavily.

WHO CALLS IT
    The Researcher (step 8). The Fact-Checker never searches - it re-fetches only
    (guidelines §2.5).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from email.utils import parsedate_to_datetime
from time import sleep
from typing import Any

from tavily import (
    BadRequestError,
    InvalidAPIKeyError,
    MissingAPIKeyError,
    TavilyClient,
    UsageLimitExceededError,
)
from tavily.errors import ForbiddenError

from config import Config
from schemas import SearchResult
from tools.contracts import ToolCache, ToolCallFailed
from tools.validation import normalize_url, validate_query

logger = logging.getLogger(__name__)

# guidelines §17: search 15s, 2 retries, 1s and 4s. The backoff list length is the retry
# count, as it is in every row of that table.
TIMEOUT_S = 15.0
_BACKOFF_S: tuple[float, ...] = (1.0, 4.0)

_CACHE_TTL_S = 24 * 60 * 60  # guidelines §11: cache:search:{hash}, 24h

_TERMINAL_ERRORS = (
    InvalidAPIKeyError,
    MissingAPIKeyError,
    BadRequestError,
    ForbiddenError,
    UsageLimitExceededError,
)
"""Retrying any of these repeats a request the service has already refused on its merits.
Everything else - the SDK's own timeout type, and whatever its HTTP layer raises
underneath - is treated as retryable, which is why the retry branch catches broadly rather
than naming exception types from a library we do not otherwise depend on."""


def search(
    query: str,
    *,
    config: Config,
    client: TavilyClient | None = None,
    cache: ToolCache | None = None,
) -> list[SearchResult]:
    """Run one search and return normalised results, newest provider shape hidden.

    Raises ToolCallFailed with reason `invalid_query` if the argument is rejected, or
    `search_unavailable` if the service could not be reached within its retry bound. It
    does not return an empty list to mean failure: "that query yields no findings for the
    subtopic" is the caller's conclusion to draw, and it needs to know which happened.
    """
    cleaned = validate_query(query)
    key = f"cache:search:{hashlib.sha256(cleaned.encode()).hexdigest()}"

    cached = _from_cache(cache, key)
    if cached is not None:
        logger.info("search cache hit for %r", cleaned)
        return cached
    logger.info("search cache miss for %r", cleaned)

    payload = _call_tavily(cleaned, config=config, client=client)
    results = _normalize(payload, max_chars=config.max_page_chars)

    if cache is not None:
        cache.set(
            key, json.dumps([result.model_dump(mode="json") for result in results]), _CACHE_TTL_S
        )
    return results


def _call_tavily(query: str, *, config: Config, client: TavilyClient | None) -> dict[str, Any]:
    """The bounded retry loop. Returns Tavily's raw response for _normalize to handle."""
    tavily = client or TavilyClient(api_key=config.tavily_api_key)
    retries = 0

    while True:
        try:
            # include_raw_content asks for the cleaned page, not the short extract:
            # SearchResult.content is documented as page text, not a snippet.
            response = tavily.search(query, include_raw_content=True, timeout=TIMEOUT_S)
        except _TERMINAL_ERRORS as error:
            raise ToolCallFailed(
                "search_unavailable", f"Tavily refused the request: {error}"
            ) from error
        except Exception as error:
            if retries == len(_BACKOFF_S):
                raise ToolCallFailed(
                    "search_unavailable",
                    f"Tavily unreachable after {retries} retries: {error}",
                ) from error
            delay = _BACKOFF_S[retries]
            retries += 1
            logger.warning("search failed (%s), retrying in %.1fs", error, delay)
            sleep(delay)
        else:
            return dict(response)


def _normalize(payload: dict[str, Any], *, max_chars: int) -> list[SearchResult]:
    """Turn Tavily's response into SearchResults, dropping what cannot be used.

    A result missing a URL or a body is skipped rather than failing the whole search: one
    malformed entry should cost one source, not the query. Duplicates are collapsed by
    normalised URL, because the same page returned twice is double-counted evidence.
    """
    results: list[SearchResult] = []
    seen: set[str] = set()

    for raw in payload.get("results") or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        # raw_content is the cleaned page; content is the short extract it falls back to.
        body = str(raw.get("raw_content") or raw.get("content") or "")
        if not url or not body:
            logger.warning("dropping search result with no url or no content: %r", url)
            continue

        try:
            key = normalize_url(url)
        except ValueError:
            logger.warning("dropping search result with an unparseable url: %r", url)
            continue
        if key in seen:
            logger.info("dropping duplicate search result: %s", key)
            continue

        try:
            result = SearchResult.model_validate(
                {
                    "title": str(raw.get("title") or url),
                    "url": key,
                    "content": body[:max_chars],
                    "published_at": _published_at(raw.get("published_date")),
                    "truncated": len(body) > max_chars,
                }
            )
        except ValueError:
            logger.warning("dropping search result that failed validation: %r", url)
            continue

        seen.add(key)
        results.append(result)

    return results


def _published_at(value: object) -> datetime | None:
    """Tavily dates arrive ISO-formatted or RFC 2822 depending on the topic.

    A date we cannot parse is not worth failing a source over - the field is optional in
    the schema precisely because plenty of pages do not publish one.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    for parse in (datetime.fromisoformat, parsedate_to_datetime):
        try:
            return parse(value.strip())
        except (TypeError, ValueError):
            continue
    return None


def _from_cache(cache: ToolCache | None, key: str) -> list[SearchResult] | None:
    """A corrupt or unreadable entry counts as a miss - caches fail open (guidelines §11)."""
    if cache is None:
        return None
    stored = cache.get(key)
    if stored is None:
        return None
    try:
        return [SearchResult.model_validate(item) for item in json.loads(stored)]
    except (ValueError, TypeError):
        logger.warning("discarding unreadable cache entry %s", key)
        return None
