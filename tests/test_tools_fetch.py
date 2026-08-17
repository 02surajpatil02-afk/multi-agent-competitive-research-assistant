"""
WHY THIS FILE EXISTS
    fetch() is the one place this system makes an outbound request to an address someone
    else chose, so most of these tests are about what it refuses to do: follow a redirect
    into the private range, read a body bigger than the cap, parse something that is not
    text, or retry an answer that will not change.

    The redirect-to-metadata test is the one to read first. It is the standard SSRF
    bypass, and it is the reason this module follows redirects by hand instead of letting
    httpx do it.

    Every test drives a MockTransport and a fake resolver: no sockets, no DNS, no
    credentials.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import cast

import httpx
import pytest
from fakes import FakeCache, mock_http, patch_dns, pdf_bytes
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

import tools.fetch
from config import Config, load_config
from redisstore import RedisCache
from schemas import FetchedPage
from tools.contracts import ToolCallFailed, Unreachable
from tools.fetch import fetch

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
}

_PUBLIC = "93.184.216.34"
_PAGE = "https://example.com/report"

_HTML = """
<html><head><title>Annual report</title><style>.a{color:red}</style></head>
<body><script>var x = 1;</script><h1>Results</h1><p>Revenue grew 12%.</p></body></html>
"""


def _config(**overrides: str) -> Config:
    return load_config({**_ENV, **overrides})


@pytest.fixture(autouse=True)
def dns(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_dns(
        monkeypatch,
        {
            "example.com": _PUBLIC,
            "redirect.com": _PUBLIC,
            "metadata.evil.com": "169.254.169.254",
            "169.254.169.254": "169.254.169.254",
        },
    )


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(tools.fetch, "sleep", recorded.append)
    return recorded


class _Site:
    """A scripted origin server. Answers /robots.txt itself so every test does not have to."""

    def __init__(
        self,
        answer: httpx.Response | Exception | Callable[[int], httpx.Response | Exception],
        *,
        robots: str | None = None,
    ) -> None:
        self._answer = answer
        self._robots = robots
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            if self._robots is None:
                return httpx.Response(404)
            return httpx.Response(200, text=self._robots, headers={"content-type": "text/plain"})

        self.requests.append(str(request.url))
        answer = self._answer
        if callable(answer) and not isinstance(answer, httpx.Response):
            answer = answer(len(self.requests))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def client(self) -> httpx.Client:
        return mock_http(self)


def _html_response(body: str = _HTML, **kwargs: object) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


# --- A valid fetch ------------------------------------------------------------------


def test_a_valid_html_page_is_cleaned_into_a_page() -> None:
    site = _Site(_html_response())

    page = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(page, FetchedPage)
    assert page.title == "Annual report"
    assert "Revenue grew 12%." in page.text
    assert page.truncated is False


def test_script_and_style_never_reach_the_text() -> None:
    # They are not prose, and they are the parts of a page most likely to look like
    # instructions to a model reading it.
    site = _Site(_html_response())

    page = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(page, FetchedPage)
    assert "var x = 1" not in page.text
    assert "color:red" not in page.text


def test_plain_text_is_kept_as_it_is() -> None:
    site = _Site(
        httpx.Response(200, text="Revenue grew 12%.", headers={"content-type": "text/plain"})
    )

    page = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(page, FetchedPage)
    assert page.text == "Revenue grew 12%."


def test_the_hash_covers_the_text_the_model_will_read() -> None:
    # Hashing the cleaned text is what makes "is this the page the claim came from?"
    # survive a markup change that leaves the prose alone.
    import hashlib

    site = _Site(_html_response())

    page = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(page, FetchedPage)
    assert page.content_hash == hashlib.sha256(page.text.encode()).hexdigest()


def test_a_page_with_no_text_is_unreachable_not_an_empty_finding() -> None:
    site = _Site(_html_response("<html><body><script>x()</script></body></html>"))

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "empty_content"


# --- Rejected arguments --------------------------------------------------------------


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "not-a-url"])
def test_an_invalid_url_is_rejected_before_any_request(url: str) -> None:
    site = _Site(_html_response())

    with pytest.raises(ToolCallFailed) as raised:
        fetch(url, config=_config(), client=site.client())

    assert raised.value.reason == "invalid_url"
    assert site.requests == []


def test_an_ssrf_target_is_rejected_before_any_request() -> None:
    site = _Site(_html_response())

    with pytest.raises(ToolCallFailed) as raised:
        fetch("http://metadata.evil.com/latest/meta-data/", config=_config(), client=site.client())

    assert raised.value.reason == "blocked_url"
    assert site.requests == []


# --- Redirects, which is where SSRF actually gets interesting ------------------------


def test_a_redirect_is_followed_and_the_final_url_is_the_one_recorded() -> None:
    def answer(attempt: int) -> httpx.Response:
        if attempt == 1:
            return httpx.Response(301, headers={"location": "https://example.com/final"})
        return _html_response()

    site = _Site(answer)

    page = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(page, FetchedPage)
    assert str(page.url) == "https://example.com/final"


def test_a_redirect_into_the_private_range_is_refused() -> None:
    # The standard bypass: a public URL that redirects to the metadata endpoint. This is
    # why redirects are followed by hand - a client that follows them internally has
    # already made the request by the time anyone can look.
    def answer(attempt: int) -> httpx.Response:
        if attempt == 1:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
        raise AssertionError("the blocked hop must never be requested")

    site = _Site(answer)

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "redirect_blocked"
    assert site.requests == [_PAGE]


def test_a_relative_redirect_is_resolved_before_it_is_checked() -> None:
    def answer(attempt: int) -> httpx.Response:
        if attempt == 1:
            return httpx.Response(302, headers={"location": "/elsewhere"})
        return _html_response()

    site = _Site(answer)

    page = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(page, FetchedPage)
    assert str(page.url) == "https://example.com/elsewhere"


def test_a_redirect_loop_is_bounded() -> None:
    site = _Site(httpx.Response(302, headers={"location": "https://example.com/round"}))

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "too_many_redirects"


def test_a_redirect_with_no_location_is_not_followed_into_nothing() -> None:
    site = _Site(httpx.Response(302))

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "http_error"


# --- Content type and size ------------------------------------------------------------


@pytest.mark.parametrize("content_type", ["image/png", "application/json", ""])
def test_a_content_type_we_cannot_read_is_unreachable(content_type: str) -> None:
    # Text, HTML, and PDF are the three we can read. Everything else would need guessing.
    site = _Site(httpx.Response(200, content=b"...", headers={"content-type": content_type}))

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "wrong_content_type"


# --- PDF ------------------------------------------------------------------------------
# Added on measurement: three step-12 smoke jobs rejected Infosys's annual report and
# investor presentation, which are the best primary sources these questions have.


def _pdf_site(*pages: str, title: str = "", robots: str | None = None) -> _Site:
    return _Site(
        httpx.Response(
            200, content=pdf_bytes(*pages, title=title), headers={"content-type": "application/pdf"}
        ),
        robots=robots,
    )


def test_a_pdf_is_read_and_its_text_extracted() -> None:
    site = _pdf_site("Cloud revenue grew 12 percent", title="Annual Report 2026")

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, FetchedPage)
    assert "Cloud revenue grew 12 percent" in outcome.text
    assert outcome.title == "Annual Report 2026"
    assert outcome.truncated is False


def test_every_page_of_a_pdf_reaches_the_text() -> None:
    site = _pdf_site("First page finding", "Second page finding")

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, FetchedPage)
    assert "First page finding" in outcome.text
    assert "Second page finding" in outcome.text


def test_a_pdf_with_no_metadata_title_falls_back_to_the_url() -> None:
    site = _pdf_site("Some evidence")

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, FetchedPage)
    assert outcome.title == _PAGE


def test_a_pdf_longer_than_the_page_cap_is_truncated() -> None:
    # The bound exists because extraction is our own work, with no request timeout around
    # it. Hitting it must set the same flag the character cap sets (guidelines §2.3).
    site = _pdf_site(*[f"page {i} text" for i in range(tools.fetch._MAX_PDF_PAGES + 5)])

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, FetchedPage)
    assert outcome.truncated is True
    assert "page 0 text" in outcome.text
    assert f"page {tools.fetch._MAX_PDF_PAGES + 4} text" not in outcome.text


def test_a_pdf_that_cannot_be_parsed_is_unreachable_not_an_error() -> None:
    # Encrypted, malformed, or scanned-with-no-text-layer all land here. One source lost,
    # the job continues - never a guess about what the document said.
    site = _Site(
        httpx.Response(
            200, content=b"%PDF-1.4 not really", headers={"content-type": "application/pdf"}
        )
    )

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "empty_content"


def test_a_pdf_hash_covers_the_extracted_text() -> None:
    # Provenance is identical to the HTML path: the hash is over the cleaned, capped text
    # the model actually read, so "is this the document that claim came from?" stays
    # answerable (guidelines §9).
    site = _pdf_site("Cloud revenue grew 12 percent")

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, FetchedPage)
    assert outcome.content_hash == hashlib.sha256(outcome.text.encode()).hexdigest()


def test_a_pdf_over_the_byte_cap_is_refused_before_it_is_parsed() -> None:
    # MAX_FETCH_BYTES holds against the bytes, whatever they decode to.
    site = _Site(
        httpx.Response(
            200,
            content=b"%PDF-1.4" + b"x" * (3 * 1024 * 1024),
            headers={"content-type": "application/pdf"},
        )
    )

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "too_large"


def test_robots_still_blocks_a_pdf() -> None:
    # Nothing about the SSRF, robots, or redirect rules is content-type aware, and adding a
    # parser must not have made a hole in any of them.
    site = _pdf_site("Cloud revenue grew 12 percent", robots="User-agent: *\nDisallow: /")

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "robots_denied"


def test_a_declared_length_over_the_cap_is_refused() -> None:
    site = _Site(
        httpx.Response(
            200,
            text="x",
            headers={"content-type": "text/plain", "content-length": str(3 * 1024 * 1024)},
        )
    )

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "too_large"


def test_a_body_over_the_cap_is_refused_even_when_the_header_lied() -> None:
    # A server that ignores Content-Length can still send two gigabytes, so the cap has to
    # hold against the bytes rather than against the claim.
    config = _config(MAX_FETCH_BYTES="100")
    site = _Site(httpx.Response(200, text="x" * 500, headers={"content-type": "text/plain"}))

    outcome = fetch(_PAGE, config=config, client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "too_large"


def test_text_over_max_page_chars_is_cut_at_the_head_and_flagged() -> None:
    config = _config(MAX_PAGE_CHARS="40")
    site = _Site(
        httpx.Response(200, text="A" * 60 + "B" * 60, headers={"content-type": "text/plain"})
    )

    page = fetch(_PAGE, config=config, client=site.client())

    assert isinstance(page, FetchedPage)
    assert page.text == "A" * 40  # the head is what is kept
    assert page.truncated is True


def test_text_at_the_cap_is_not_flagged() -> None:
    config = _config(MAX_PAGE_CHARS="40")
    site = _Site(httpx.Response(200, text="A" * 40, headers={"content-type": "text/plain"}))

    page = fetch(_PAGE, config=config, client=site.client())

    assert isinstance(page, FetchedPage)
    assert page.truncated is False


# --- Failure, retries, and giving up ---------------------------------------------------


def test_a_timeout_is_retried_once_then_reported(slept: list[float]) -> None:
    site = _Site(httpx.ConnectTimeout("too slow"))

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "timeout"
    assert len(site.requests) == 2  # 1 attempt + 1 retry
    assert slept == [2.0]


def test_a_transient_failure_that_clears_produces_a_page(slept: list[float]) -> None:
    def answer(attempt: int) -> httpx.Response | Exception:
        return httpx.ConnectTimeout("too slow") if attempt == 1 else _html_response()

    site = _Site(answer)

    page = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(page, FetchedPage)
    assert slept == [2.0]


def test_a_server_error_is_retried(slept: list[float]) -> None:
    site = _Site(httpx.Response(503, text="down", headers={"content-type": "text/plain"}))

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "server_error"
    assert len(site.requests) == 2


def test_a_connection_error_is_retried(slept: list[float]) -> None:
    site = _Site(httpx.ConnectError("refused"))

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "connection_error"
    assert len(site.requests) == 2


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(404, text="gone", headers={"content-type": "text/plain"}), "http_error"),
        (httpx.Response(403, text="no", headers={"content-type": "text/plain"}), "http_error"),
        (
            httpx.Response(200, content=b"x", headers={"content-type": "image/png"}),
            "wrong_content_type",
        ),
    ],
)
def test_a_settled_answer_is_not_retried(
    response: httpx.Response, reason: str, slept: list[float]
) -> None:
    site = _Site(response)

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == reason
    assert len(site.requests) == 1
    assert slept == []


# --- robots.txt -------------------------------------------------------------------------


def test_a_disallowed_path_is_not_fetched() -> None:
    site = _Site(_html_response(), robots="User-agent: *\nDisallow: /report")

    outcome = fetch(_PAGE, config=_config(), client=site.client())

    assert isinstance(outcome, Unreachable)
    assert outcome.reason == "robots_denied"
    assert site.requests == []


def test_an_allowed_path_is_fetched() -> None:
    site = _Site(_html_response(), robots="User-agent: *\nDisallow: /private")

    assert isinstance(fetch(_PAGE, config=_config(), client=site.client()), FetchedPage)


def test_a_missing_robots_file_does_not_block_the_fetch() -> None:
    # Most of the web has no robots.txt, and being unable to read a politeness file is a
    # poor reason to lose a source.
    site = _Site(_html_response(), robots=None)

    assert isinstance(fetch(_PAGE, config=_config(), client=site.client()), FetchedPage)


# --- The cache interface -----------------------------------------------------------------


def test_a_hit_skips_the_request_entirely() -> None:
    cache = FakeCache()
    site = _Site(_html_response())
    fetch(_PAGE, config=_config(), client=site.client(), cache=cache)
    site.requests.clear()

    page = fetch(_PAGE, config=_config(), client=site.client(), cache=cache)

    assert isinstance(page, FetchedPage)
    assert site.requests == []


def test_a_miss_stores_the_page_under_the_documented_ttl() -> None:
    cache = FakeCache()
    site = _Site(_html_response())

    fetch(_PAGE, config=_config(), client=site.client(), cache=cache)

    key, ttl = cache.sets[0]
    assert key.startswith("cache:fetch:")
    assert ttl == 24 * 60 * 60


def test_an_unreachable_page_is_not_cached() -> None:
    # Caching "this was down for ten seconds" for a day would turn a blip into an outage.
    cache = FakeCache()
    site = _Site(httpx.Response(404, text="gone", headers={"content-type": "text/plain"}))

    fetch(_PAGE, config=_config(), client=site.client(), cache=cache)

    assert cache.sets == []


def test_a_dead_redis_costs_one_fetch_and_not_the_page() -> None:
    """The other half of the fail-open rule, composed the same way (guidelines §11).

    `fetch` is the more expensive of the two to lose - a page that cannot be read becomes an
    `unreachable` source and a claim the Fact-Checker cannot support - so a Redis outage must
    cost a repeated request and nothing else.
    """
    site = _Site(_html_response())
    cache = RedisCache(cast(Redis, _UnreachableRedis()))

    page = fetch(_PAGE, config=_config(), client=site.client(), cache=cache)

    assert isinstance(page, FetchedPage)
    assert site.requests  # the page was really fetched, because the cache could not answer


class _UnreachableRedis:
    """Every command raises, the way redis-py reports a host that will not answer."""

    def get(self, _key: str) -> str | None:
        raise RedisConnectionError("redis is not answering")

    def set(self, _key: str, _value: str, ex: int | None = None) -> None:
        raise RedisConnectionError("redis is not answering")
