"""
WHY THIS FILE EXISTS
    Argument validation is the first thing every tool call goes through and the whole of
    the SSRF defence. These tests pin the two behaviours that matter: a query is cleaned
    or refused rather than quietly mangled, and a URL is judged on the address it resolves
    to rather than on how the hostname looks.

    URL normalisation is here too, because deduplication is only as good as the rule that
    decides two URLs are the same page - and the failure mode of getting it wrong is
    losing evidence, not saving a fetch.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

import socket

import pytest
from fakes import patch_dns

from tools.contracts import ToolCallFailed
from tools.validation import MAX_QUERY_CHARS, normalize_url, validate_query, validate_url

_PUBLIC = "93.184.216.34"


# --- Query validation --------------------------------------------------------------


def test_a_plain_query_is_returned_unchanged() -> None:
    assert (
        validate_query("TCS versus Infosys cloud strategy") == "TCS versus Infosys cloud strategy"
    )


def test_control_characters_are_stripped() -> None:
    # A newline or a null in a query is either a mistake or an attempt at something.
    assert validate_query("cloud\x00 strategy\n2024") == "cloud strategy 2024"


def test_surrounding_and_repeated_whitespace_is_collapsed() -> None:
    assert validate_query("  cloud    strategy  ") == "cloud strategy"


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_an_empty_query_is_rejected(query: str) -> None:
    with pytest.raises(ToolCallFailed) as raised:
        validate_query(query)

    assert raised.value.reason == "invalid_query"


def test_an_over_long_query_is_rejected_rather_than_shortened() -> None:
    # Cutting a query changes the question. A caller that gets a refusal can shorten it
    # deliberately; one that gets silently truncated results cannot tell that it happened.
    with pytest.raises(ToolCallFailed) as raised:
        validate_query("x" * (MAX_QUERY_CHARS + 1))

    assert raised.value.reason == "invalid_query"


def test_a_query_at_the_cap_is_accepted() -> None:
    assert len(validate_query("x" * MAX_QUERY_CHARS)) == MAX_QUERY_CHARS


# --- URL validation ----------------------------------------------------------------


def test_a_public_https_url_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_dns(monkeypatch, {"example.com": _PUBLIC})

    assert validate_url("https://example.com/report") == "https://example.com/report"


@pytest.mark.parametrize(
    "url", ["ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)", "/relative/path"]
)
def test_a_non_http_url_is_rejected(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_dns(monkeypatch, {"example.com": _PUBLIC})

    with pytest.raises(ToolCallFailed) as raised:
        validate_url(url)

    assert raised.value.reason == "invalid_url"


def test_a_url_with_no_host_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_dns(monkeypatch, {})

    with pytest.raises(ToolCallFailed) as raised:
        validate_url("https:///just-a-path")

    assert raised.value.reason == "invalid_url"


def test_an_unresolvable_host_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_dns(monkeypatch, {})

    with pytest.raises(ToolCallFailed) as raised:
        validate_url("https://nowhere.invalid/x")

    assert raised.value.reason == "invalid_url"


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",  # the cloud metadata endpoint - the reason this check exists
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "0.0.0.0",
        "::1",
        "fd00::1",
        "fe80::1",
        "::ffff:169.254.169.254",  # the metadata endpoint wearing an IPv6 hat
    ],
)
def test_a_host_resolving_to_a_private_address_is_blocked(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Checked on the resolved address, not the name: a hostname that looks like anything
    # at all can point at the metadata endpoint.
    patch_dns(monkeypatch, {"totally-normal.com": address})

    with pytest.raises(ToolCallFailed) as raised:
        validate_url("https://totally-normal.com/x")

    assert raised.value.reason == "blocked_url"


def test_a_literal_private_ip_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_dns(monkeypatch, {"169.254.169.254": "169.254.169.254"})

    with pytest.raises(ToolCallFailed) as raised:
        validate_url("http://169.254.169.254/latest/meta-data/")

    assert raised.value.reason == "blocked_url"


def test_credentials_in_the_url_do_not_disguise_the_real_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # http://example.com@169.254.169.254/ has a host of 169.254.169.254, and the resolved
    # address is what gets checked - so the trick costs nothing extra to defend.
    patch_dns(monkeypatch, {"169.254.169.254": "169.254.169.254"})

    with pytest.raises(ToolCallFailed) as raised:
        validate_url("http://example.com@169.254.169.254/latest/")

    assert raised.value.reason == "blocked_url"


def test_every_resolved_address_is_checked_not_only_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A host answering with one public and one private address. Which address the
    # connection actually uses is not ours to decide, so one bad answer blocks the name.
    def resolve(host: str, *_args: object, **_kwargs: object) -> list[object]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    with pytest.raises(ToolCallFailed) as raised:
        validate_url("https://split-horizon.com/x")

    assert raised.value.reason == "blocked_url"


# --- URL normalisation, which deduplication depends on ------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM/Path", "https://example.com/Path"),  # scheme and host only
        ("https://example.com:443/x", "https://example.com/x"),  # default port dropped
        ("http://example.com:80/x", "http://example.com/x"),
        ("https://example.com/x#section", "https://example.com/x"),  # servers never see it
        ("  https://example.com/x  ", "https://example.com/x"),
    ],
)
def test_urls_that_mean_the_same_page_normalise_together(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://example.com/x", "https://example.com/x/"),  # a path is a path
        ("https://example.com/x?a=1", "https://example.com/x"),  # a query selects content
        ("https://example.com/x?a=1&b=2", "https://example.com/x?b=2&a=1"),  # order can matter
        ("https://example.com/x", "https://example.com:8443/x"),  # non-default port
        ("https://example.com/x", "http://example.com/x"),  # different scheme
    ],
)
def test_urls_that_may_be_different_pages_stay_different(left: str, right: str) -> None:
    # Over-normalising loses a source. Under-normalising costs one duplicate fetch, which
    # is the cheaper mistake of the two.
    assert normalize_url(left) != normalize_url(right)


def test_normalisation_is_idempotent() -> None:
    once = normalize_url("HTTPS://Example.com:443/a?b=1#c")

    assert normalize_url(once) == once
