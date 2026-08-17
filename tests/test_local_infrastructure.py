"""
WHY THIS FILE EXISTS
    docker-compose.yml declares numbers that other documents derive. The one that matters is
    the queue's visibility timeout, because [ADR 0010](../docs/adr/0010-*.md) decision 8 does
    not assert it - it derives it:

        visibility_timeout > MAX_JOB_RUNTIME + 3 x LLM_MAIN_TIMEOUT_S + 10

    A worker will check that inequality at startup and refuse to run when it fails. That worker
    is a later stage. Until it exists the arithmetic is a sentence in a document, and a sentence
    is exactly the thing that goes stale when someone changes a timeout - so it is checked here
    instead, against `config.py`'s real defaults, from the moment the queue is declared rather
    than from the moment something consumes it.

    **Nothing here starts a container or opens a socket.** These are file contents compared
    against configuration, which is why they belong in the offline suite: a broken relationship
    between two numbers should fail on a laptop with Docker switched off.

WHO CALLS IT
    pytest, as part of the offline suite.
"""

from __future__ import annotations

import re
from pathlib import Path

from config import load_config

_ROOT = Path(__file__).resolve().parent.parent

_COMPOSE = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
}

DEVELOPMENT_MAIN_TIMEOUT_S = 180.0
"""What `.env` sets `LLM_MAIN_TIMEOUT_S` to, and what ADR 0010 decision 8's second row sizes
the local queue for. The NIM tier generates at roughly 15-20 tokens/second, so 60s would cap a
Synthesizer pass below a full report - the override is measured, not preference.

Restated here rather than read from `.env`, because that file is gitignored: a test that read
it would pass on this machine and have nothing to read on a clean checkout.
"""

BACKOFF_SECONDS = 10
"""guidelines §17's retry schedule for a main-tier call: 2s + 8s between three attempts."""


def test_the_local_queue_outlives_the_longest_a_worker_invocation_can_take() -> None:
    """ADR 0010 decision 8, checked rather than quoted.

    The bound is not `visibility > MAX_JOB_RUNTIME`. The runtime bound can only be checked
    *between* nodes - a node in flight is inside a blocking LLM request - so the real figure is
    the runtime bound plus the longest a single node can take, which is three attempts at the
    main-tier timeout plus the backoff between them.
    """
    config = load_config({**_ENV, "LLM_MAIN_TIMEOUT_S": str(DEVELOPMENT_MAIN_TIMEOUT_S)})
    worst_case_node = 3 * config.llm_main_timeout_s + BACKOFF_SECONDS

    visibility = _compose_number("JOBS_QUEUE_VISIBILITY_TIMEOUT")

    assert visibility > config.max_job_runtime + worst_case_node


def test_the_local_queue_also_covers_the_production_timeouts() -> None:
    """The same inequality at the documented defaults, which is what a developer runs when they
    have not overridden anything. It has far more headroom, and saying so keeps the local number
    from looking like it was sized for the wrong case."""
    config = load_config(_ENV)
    worst_case_node = 3 * config.llm_main_timeout_s + BACKOFF_SECONDS

    visibility = _compose_number("JOBS_QUEUE_VISIBILITY_TIMEOUT")

    assert visibility > config.max_job_runtime + worst_case_node


def test_the_queue_gives_up_after_three_deliveries() -> None:
    """ARCHITECTURE.md §11's "three deliveries, then the dead-letter queue", expressed where
    SQS can enforce it rather than where application code would have to."""
    assert _compose_number("JOBS_QUEUE_MAX_RECEIVE_COUNT") == 3


def test_the_queue_is_fifo() -> None:
    """ADR 0010 decision 4. A FIFO queue delivers at most one message per message group at a
    time, and with the group set to the job id that is what keeps one job to one writer - the
    precondition ADR 0005's `_write_findings` is allowed to assume. A standard queue would
    break it silently, so the `.fifo` suffix is load-bearing rather than naming.
    """
    assert _compose_setting("JOBS_QUEUE_NAME").endswith(".fifo")
    assert _compose_setting("JOBS_DLQ_NAME").endswith(".fifo")


def test_compose_runs_the_versions_the_architecture_names() -> None:
    """PostgreSQL 16 and Redis 7 (ARCHITECTURE.md §17). The PostgreSQL integration suite's
    claim is specifically about 16, so a silent bump would leave that claim unverified."""
    assert "image: postgres:16-alpine" in _COMPOSE
    assert "image: redis:7-alpine" in _COMPOSE


def test_compose_mounts_bootstrap_scripts_that_exist() -> None:
    """A mount naming a missing directory is not an error - Docker creates an empty one - so
    the container starts, the init hook runs nothing, and the failure surfaces later as a
    missing queue."""
    for mounted in re.findall(r"- \./(docker/[\w/-]+):", _COMPOSE):
        directory = _ROOT / mounted

        assert directory.is_dir(), mounted
        assert list(directory.glob("*.sh")), mounted


def _compose_setting(name: str) -> str:
    match = re.search(rf"^\s*{name}:\s*(\S+)\s*$", _COMPOSE, re.MULTILINE)

    assert match is not None, f"docker-compose.yml does not set {name}"
    return match.group(1)


def _compose_number(name: str) -> int:
    return int(_compose_setting(name))
