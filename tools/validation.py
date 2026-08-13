"""
WHY THIS FILE EXISTS
    Nothing leaves this process until it has been through here. Both tools validate their
    argument before any request is made, which is the first of the three rules in
    guidelines §7 and the whole of the SSRF defence in §16.

    The SSRF check is the part worth reading carefully. `fetch` makes a request from
    inside our network, so an unchecked URL is a request-forgery primitive pointed at the
    cloud metadata endpoint. The check resolves the hostname and inspects the **resolved
    IP**, not the name - `metadata.evil.com` resolving to 169.254.169.254 is the reason
    name-based checking does not work. It is re-run on every redirect hop by fetch.py,
    because a public URL that redirects to a private address is the standard bypass.

    URL normalisation lives here too, because deduplication depends on it: two spellings
    of one page have to compare equal before "have we already fetched this?" can mean
    anything.

WHO CALLS IT
    tools/search.py and tools/fetch.py. fetch.py calls the URL checks again after every
    redirect.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from tools.contracts import ToolCallFailed

MAX_QUERY_CHARS = 400
"""The query length cap. Guidelines §7 requires a cap without naming a number; 400 is the
longest query the Tavily API accepts, so a longer one cannot succeed anyway."""

_CONTROL_CHARACTERS = {chr(code) for code in range(32)} | {chr(127)}

_DEFAULT_PORTS = {"http": 80, "https": 443}


def validate_query(query: str) -> str:
    """Return the query to send, or raise if it cannot be sent.

    Control characters are stripped and whitespace collapsed. An over-long query is
    **rejected rather than shortened**: silently cutting "compare TCS and Infosys on cloud
    strategy in 2024" down to something else searches for a question nobody asked, and a
    loud failure is cheaper to debug than a quiet wrong answer.
    """
    cleaned = "".join(" " if character in _CONTROL_CHARACTERS else character for character in query)
    cleaned = " ".join(cleaned.split())

    if not cleaned:
        raise ToolCallFailed("invalid_query", "query is empty after cleaning")
    if len(cleaned) > MAX_QUERY_CHARS:
        raise ToolCallFailed(
            "invalid_query",
            f"query is {len(cleaned)} characters, over the {MAX_QUERY_CHARS} cap",
        )
    return cleaned


def validate_url(url: str) -> str:
    """Return the normalised URL to request, or raise if it must not be requested.

    Raises ToolCallFailed with reason `invalid_url` when the URL is not an absolute
    http/https URL, and `blocked_url` when it resolves to an address we refuse to reach.
    The two are separate reasons because they mean different things: one is a malformed
    argument, the other is a request we are declining to make.
    """
    parts = urlsplit(url.strip())

    if parts.scheme not in ("http", "https"):
        raise ToolCallFailed("invalid_url", f"scheme must be http or https, got {parts.scheme!r}")
    if not parts.hostname:
        raise ToolCallFailed("invalid_url", f"no host in {url!r}")

    ensure_public_host(parts.hostname)
    return normalize_url(url)


def ensure_public_host(host: str) -> None:
    """Resolve `host` and refuse if any address it answers with is not public.

    Every address is checked, not just the first: a name that resolves to one public and
    one private address must not be reachable through the public one, because which
    address the connection actually uses is not ours to decide.
    """
    try:
        resolved = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise ToolCallFailed("invalid_url", f"cannot resolve {host!r}: {error}") from error

    addresses = {str(info[4][0]) for info in resolved}
    if not addresses:
        raise ToolCallFailed("invalid_url", f"{host!r} resolved to nothing")

    for address in sorted(addresses):
        if not _is_public_address(address):
            raise ToolCallFailed(
                "blocked_url",
                f"{host!r} resolves to {address}, which is not a public address",
            )


def normalize_url(url: str) -> str:
    """Collapse spellings of one page to one string, for deduplication.

    Only changes that cannot alter which document is returned:

      * lowercase the scheme and the host - both are case-insensitive
      * drop the default port for the scheme
      * drop the fragment - servers never see it

    Deliberately **not** done: stripping the query, sorting query parameters, or removing
    a trailing slash. Each can point at a different document, and merging two real pages
    into one would lose evidence rather than save a fetch.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()

    netloc = host
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def _is_public_address(address: str) -> bool:
    """Reject the ranges guidelines §16 names, plus the rest of the non-public space.

    A superset of the documented list rather than exactly it: reserved, multicast, and
    unspecified addresses are no more valid a fetch target than 10/8 is, and enumerating
    only the five named ranges would leave the others reachable.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False

    # ::ffff:169.254.169.254 is the metadata endpoint wearing an IPv6 hat.
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped

    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_reserved
        or parsed.is_multicast
        or parsed.is_unspecified
    )
