"""
WHY THIS FILE EXISTS
    `fetch(url)` - the second of the two tools, and the one that makes a request from
    inside our network. That is why it, and not Tavily, owns the SSRF defence, the byte
    cap, and the redirect handling: none of those can be delegated to a service that makes
    the request somewhere else (guidelines §16).

    Redirects are followed by hand rather than by httpx, because the rule is "re-check
    after every redirect" and a client that follows them internally has already made the
    request to the private address by the time you can inspect it.

    The size limits are enforced in the order they can first be violated: the declared
    Content-Length, then the bytes as they stream in, then the cleaned text. All three are
    enforced before any fetched byte reaches a prompt (guidelines §7).

    HTML, plain text, and PDF all converge on one path. Only the extraction differs - a
    parser for markup, a parser for PDF, nothing for plain text. Everything the audit trail
    and the injection defence depend on happens after that join and is therefore identical
    for all three: the character cap, the sha256 over the cleaned text, the truncation flag,
    and the untrusted-content wrapper the Researcher applies. A source type is added by
    teaching this file how to get text out of it, and by changing nothing else.

WHO CALLS IT
    The Researcher, and the Fact-Checker for re-fetching a URL already in a Finding
    (step 8). Only URLs that came from a search result or an existing Finding are ever
    passed here - a URL found inside page text is not fetchable (guidelines §8, §16).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import BytesIO
from time import sleep
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from pypdf import PdfReader

from config import Config
from schemas import FetchedPage
from tools.contracts import ToolCache, ToolCallFailed, Unreachable, UnreachableReason
from tools.validation import validate_url

logger = logging.getLogger(__name__)

# guidelines §17: fetch 10s, 1 retry, 2s.
TIMEOUT_S = 10.0
_BACKOFF_S: tuple[float, ...] = (2.0,)

_RETRYABLE: frozenset[UnreachableReason] = frozenset(
    {"timeout", "connection_error", "server_error"}
)
"""A 4xx, a wrong content type, an oversized body, and a blocked redirect are all settled
answers. Retrying them spends time to be told the same thing again."""

_PDF_CONTENT_TYPE = "application/pdf"

_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "text/plain", _PDF_CONTENT_TYPE})
"""guidelines §7, plus PDF. Anything else is unreachable - there is still no path for images,
office documents, or any other binary.

PDF was added on evidence rather than on principle: three step-12 smoke jobs rejected Infosys's
annual report and investor presentation as `wrong_content_type`, and those are the best primary
sources a competitive-research question has. Everything a PDF goes through afterwards is the
same as HTML - the same byte cap, the same character cap, the same hash over the cleaned text,
the same untrusted-content wrapper at the Researcher."""

_MAX_PDF_PAGES = 50
"""How many pages of one PDF are read. Not a documented number; a bound is required because
extraction is our own work rather than a request with a timeout around it, and an annual report
can run to hundreds of pages. Stopping here sets `truncated`, which is the same signal the
character cap sets and which the Fact-Checker already knows how to read."""

_CACHE_TTL_S = 24 * 60 * 60  # guidelines §11: cache:fetch:{hash}, 24h

_MAX_REDIRECTS = 5
"""Not a documented number. A bound is required because the redirect loop is ours, and
five is enough for the ordinary http->https->www->canonical chain."""

USER_AGENT = "competitive-research/0.1"
"""Sent on every request and used to match robots.txt rules. Not a documented value; a
crawler that will not name itself cannot honestly claim to respect robots.txt."""

_HEADERS = {"user-agent": USER_AGENT, "accept": "text/html, text/plain, application/pdf"}

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
        "table", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6",
    }
)  # fmt: skip


def fetch(
    url: str,
    *,
    config: Config,
    client: httpx.Client | None = None,
    cache: ToolCache | None = None,
) -> FetchedPage | Unreachable:
    """Retrieve one page, or say why it could not be used.

    Raises ToolCallFailed for a rejected argument - a non-http URL, or one resolving to a
    private address. Returns Unreachable for everything that goes wrong afterwards,
    because a page that cannot be read is a finding-level outcome the job continues past,
    not an error to route around (ARCHITECTURE.md §7).
    """
    target = validate_url(url)
    key = f"cache:fetch:{hashlib.sha256(target.encode()).hexdigest()}"

    cached = _from_cache(cache, key)
    if cached is not None:
        logger.info("fetch cache hit for %s", target)
        return cached
    logger.info("fetch cache miss for %s", target)

    owns_client = client is None
    http = client or httpx.Client(follow_redirects=False, timeout=TIMEOUT_S, headers=_HEADERS)
    try:
        if not _robots_allows(http, target, config):
            return Unreachable(target, "robots_denied", "robots.txt disallows this path")
        outcome = _fetch_with_retry(http, target, config)
    finally:
        if owns_client:
            http.close()

    if isinstance(outcome, FetchedPage) and cache is not None:
        cache.set(key, outcome.model_dump_json(), _CACHE_TTL_S)
    return outcome


def _fetch_with_retry(http: httpx.Client, target: str, config: Config) -> FetchedPage | Unreachable:
    retries = 0
    while True:
        outcome = _fetch_once(http, target, config)
        if not isinstance(outcome, Unreachable) or outcome.reason not in _RETRYABLE:
            return outcome
        if retries == len(_BACKOFF_S):
            logger.warning("giving up on %s after %d retries: %s", target, retries, outcome.reason)
            return outcome
        delay = _BACKOFF_S[retries]
        retries += 1
        logger.warning("fetch of %s failed (%s), retrying in %.1fs", target, outcome.reason, delay)
        sleep(delay)


def _fetch_once(http: httpx.Client, target: str, config: Config) -> FetchedPage | Unreachable:
    """One attempt, following redirects by hand and re-validating every hop."""
    current = target
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            response = http.send(http.build_request("GET", current, headers=_HEADERS), stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return Unreachable(target, "http_error", "redirect with no location")
                    try:
                        current = validate_url(urljoin(current, location))
                    except ToolCallFailed as error:
                        # The standard SSRF bypass: a public URL that redirects to
                        # 169.254.169.254. Loud, because it is a security event.
                        logger.warning("refused redirect from %s: %s", current, error)
                        return Unreachable(target, "redirect_blocked", str(error))
                    continue

                refusal = _refuse_response(target, response, config)
                if refusal is not None:
                    return refusal
                body = _read_capped(response, config.max_fetch_bytes)
                if body is None:
                    return Unreachable(target, "too_large", "body exceeded MAX_FETCH_BYTES")
                return _to_page(current, response, body, config)
            finally:
                response.close()

        return Unreachable(target, "too_many_redirects", f"more than {_MAX_REDIRECTS} redirects")
    except httpx.TimeoutException as error:
        return Unreachable(target, "timeout", str(error) or "request timed out")
    except httpx.HTTPError as error:
        return Unreachable(target, "connection_error", str(error) or type(error).__name__)


def _refuse_response(target: str, response: httpx.Response, config: Config) -> Unreachable | None:
    """Status, content type, and declared size - the checks that need no body."""
    if response.status_code >= 500:
        return Unreachable(target, "server_error", f"status {response.status_code}")
    if response.status_code >= 400:
        return Unreachable(target, "http_error", f"status {response.status_code}")

    content_type = _content_type(response)
    if content_type not in _ALLOWED_CONTENT_TYPES:
        return Unreachable(
            target, "wrong_content_type", f"content-type was {content_type or 'absent'}"
        )

    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.max_fetch_bytes:
        return Unreachable(target, "too_large", f"content-length {declared} over MAX_FETCH_BYTES")
    return None


def _read_capped(response: httpx.Response, limit: int) -> bytes | None:
    """Stream the body, stopping the moment it goes over. None means it went over.

    Streaming rather than reading: a server that ignores Content-Length can still send two
    gigabytes, and the cap has to hold against the bytes rather than against the claim.
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _to_page(
    url: str, response: httpx.Response, body: bytes, config: Config
) -> FetchedPage | Unreachable:
    media = _content_type(response)

    if media == _PDF_CONTENT_TYPE:
        extracted = _pdf_to_text(body)
        if extracted is None:
            # A PDF we could not read is the same outcome as a page with no text in it: one
            # source lost, the job continues. The detail says which of the two it was, so
            # "how often does extraction fail?" stays answerable from the logs.
            return Unreachable(url, "empty_content", "pdf text could not be extracted")
        title, text, page_capped = extracted
    elif media == "text/html":
        title, text = _html_to_text(_decode(body, response.charset_encoding))
        page_capped = False
    else:
        title, text, page_capped = "", _decode(body, response.charset_encoding), False

    cleaned = _collapse(text)
    if not cleaned:
        return Unreachable(url, "empty_content", "nothing left after cleaning")

    # Either bound biting means the model did not see the whole document, and guidelines §2.3
    # needs the resulting Finding to say so.
    truncated = page_capped or len(cleaned) > config.max_page_chars
    kept = cleaned[: config.max_page_chars]

    return FetchedPage.model_validate(
        {
            # The final URL, after every redirect was followed and re-validated.
            "url": url,
            "title": (title.strip() or url)[:300],
            "text": kept,
            "truncated": truncated,
            "content_hash": hashlib.sha256(kept.encode()).hexdigest(),
            "retrieved_at": datetime.now(UTC),
        }
    )


def _pdf_to_text(body: bytes) -> tuple[str, str, bool] | None:
    """`(title, text, page_capped)` from PDF bytes, or None if it could not be read.

    None rather than an exception because an unreadable PDF is a finding-level outcome like
    any other unusable source (ARCHITECTURE.md §7). Encrypted files, malformed files, and
    scanned images with no text layer all land here - there is no OCR path, and a scanned
    page produces no text rather than a wrong one.
    """
    try:
        reader = PdfReader(BytesIO(body))
        pages = reader.pages[:_MAX_PDF_PAGES]
        text = "\n".join(page.extract_text() or "" for page in pages)
        # `metadata` is absent on plenty of real PDFs, and `title` inside it may be None.
        title = str(getattr(reader.metadata, "title", "") or "")
    except Exception as error:  # noqa: BLE001 - pypdf raises many shapes for one outcome
        logger.warning("could not extract text from a pdf: %s", error)
        return None
    return title, text, len(reader.pages) > _MAX_PDF_PAGES


def _content_type(response: httpx.Response) -> str:
    """The media type without its parameters: `text/html; charset=utf-8` -> `text/html`."""
    header = str(response.headers.get("content-type", ""))
    return header.split(";")[0].strip().lower()


def _decode(body: bytes, declared: str | None) -> str:
    """errors="replace" on purpose: a page with a few bad bytes is still evidence."""
    try:
        return body.decode(declared or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _robots_allows(http: httpx.Client, url: str, config: Config) -> bool:
    """Respect robots.txt (guidelines §7).

    Fails open: only a robots.txt we actually retrieved and that actually disallows the
    path blocks a fetch. A missing, redirected, erroring, or unreadable robots.txt is
    treated as no rules, which is how crawlers behave in practice - and being unable to
    read a politeness file is a poor reason to lose a source.
    """
    parts = urlsplit(url)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    try:
        response = http.send(http.build_request("GET", robots_url, headers=_HEADERS), stream=True)
        try:
            if response.status_code != 200:
                return True
            body = _read_capped(response, config.max_fetch_bytes)
        finally:
            response.close()
    except httpx.HTTPError as error:
        logger.warning("could not read %s (%s); proceeding", robots_url, error)
        return True

    if body is None:
        logger.warning("%s is over MAX_FETCH_BYTES; proceeding", robots_url)
        return True

    parser = RobotFileParser()
    parser.parse(_decode(body, response.charset_encoding).splitlines())
    return parser.can_fetch(USER_AGENT, url)


class _HtmlTextExtractor(HTMLParser):
    """HTML to text with the standard library.

    A dedicated parser would handle more of the web's malformed markup, but this needs to
    drop scripts and styles, keep block boundaries, and read the title - which html.parser
    does. A dependency has to name a requirement it serves, and "nicer output on broken
    markup" is not one until a measurement says the extraction is costing us findings.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _html_to_text(html: str) -> tuple[str, str]:
    extractor = _HtmlTextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.title, extractor.text()


def _collapse(text: str) -> str:
    """One space between words, at most one blank line between blocks."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    collapsed: list[str] = []
    for line in lines:
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    return "\n".join(collapsed).strip()


def _from_cache(cache: ToolCache | None, key: str) -> FetchedPage | None:
    """A corrupt entry counts as a miss - caches fail open (guidelines §11)."""
    if cache is None:
        return None
    stored = cache.get(key)
    if stored is None:
        return None
    try:
        return FetchedPage.model_validate_json(stored)
    except ValueError:
        logger.warning("discarding unreadable cache entry %s", key)
        return None
