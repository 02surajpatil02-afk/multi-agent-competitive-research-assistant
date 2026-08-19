"""
WHY THIS FILE EXISTS
    docker-compose.yml declares the SQS visibility lease that ADR 0015's worker renews. The
    cadence is derived from that real queue value:

        renewal_interval = visibility_timeout / 3

    That leaves one interval for the first renewal, one for a retry after a transient failure,
    and one as the remaining safety margin. This file keeps Compose and that derivation tied
    together without asserting the invalid static node-duration proof ADR 0015 supersedes.

    **Nothing here starts a container or opens a socket.** These are file contents compared
    against configuration, which is why they belong in the offline suite: a broken relationship
    between two numbers should fail on a laptop with Docker switched off.

WHO CALLS IT
    pytest, as part of the offline suite.
"""

from __future__ import annotations

import re
from pathlib import Path

from worker import visibility_renewal_interval

_ROOT = Path(__file__).resolve().parent.parent

_COMPOSE = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_the_local_queue_derives_a_three_part_visibility_lease() -> None:
    visibility = _compose_number("JOBS_QUEUE_VISIBILITY_TIMEOUT")
    interval = visibility_renewal_interval(visibility)

    assert visibility == 1800  # unchanged by the heartbeat work
    assert interval == 600
    assert visibility - 2 * interval == interval  # margin after one failed retry


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
