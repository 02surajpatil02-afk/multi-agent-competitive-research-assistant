"""
WHY THIS FILE EXISTS
    A database for a test, built the way the real one is built: by running
    `alembic upgrade head`. The tests that use it are then testing the schema that ships
    rather than a second copy of it created with `metadata.create_all()`, which is the
    failure this file exists to make impossible - a constraint that is in schema.py and
    missing from the migration would otherwise pass every test and fail the deploy.

    **The database is SQLite, and that is a real limitation rather than a detail.** Phase 2
    has no PostgreSQL to run against: Compose arrives in Phase 3, and `pytest` is supposed
    to need no running service and make no network calls at all (guidelines §18). So the
    offline suite runs the migration and the statements on SQLite, which is enough to prove
    the columns, the keys, the foreign keys, the CHECK constraints, the indexes, and every
    query in database/queries.py - and is *not* enough to prove anything PostgreSQL-specific:
    JSONB behaviour, `timestamptz` semantics, the statement timeout, or concurrent writers.
    Those are verified against a real PostgreSQL when Phase 3 provides one.

    Foreign keys are the reason `create_database_engine` has a SQLite branch: SQLite ignores
    them unless a connection asks for them, and a test suite that cannot see a broken foreign
    key would prove nothing about the audit trail's shape.

WHO CALLS IT
    tests/test_database.py and tests/test_graph_persistence.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic import HttpUrl
from sqlalchemy.engine import Engine

from database.queries import create_database_engine
from schemas import Claim, Finding, Report, Section, Source

_ROOT = Path(__file__).resolve().parent.parent
"""The repository root, so a test finds alembic.ini wherever pytest was started from."""


def migrated_engine(directory: Path) -> Engine:
    """A SQLite database with every migration applied, and an engine onto it.

    `directory` is a test's `tmp_path`, so each test gets its own file and nothing leaks
    between them.
    """
    url = f"sqlite+pysqlite:///{(directory / 'jobs.db').as_posix()}"
    config = AlembicConfig(_ROOT / "alembic.ini")
    # Both are absolute, so neither depends on the working directory pytest was started in.
    config.set_main_option("script_location", str(_ROOT / "database" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return create_database_engine(url)


def new_job_id() -> str:
    """A real UUID, because `jobs.job_id` is one. It is also the checkpointer's thread id
    and the LangSmith trace tag, so the three cannot disagree (ARCHITECTURE.md §9)."""
    return str(uuid4())


def a_finding(
    finding_id: str = "f1", *, subtopic_id: str = "s1", url: str | None = None
) -> Finding:
    return Finding(
        finding_id=finding_id,
        subtopic_id=subtopic_id,
        claim="Cloud revenue grew.",
        evidence="TCS reported cloud revenue of $1.2bn in FY24.",
        url=HttpUrl(url or f"https://example.com/{finding_id}"),
        title="Annual report",
        retrieved_at=datetime(2026, 8, 15, 9, 30, tzinfo=UTC),
        content_hash=f"hash-{finding_id}",
        truncated=False,
    )


def a_report(*claims: tuple[str, list[str]], findings: list[Finding] | None = None) -> Report:
    """A report whose claims cite the finding ids they are given.

    `sources` is what the export gate reads, so it is built from the same ids the claims
    cite - the Synthesizer derives it the same way, from the findings actually cited.
    """
    known = {finding.finding_id: finding for finding in (findings or [a_finding()])}
    cited = sorted({fid for _, fids in claims for fid in fids if fid in known})
    return Report(
        sections=[Section(id="sec1", heading="Cloud", body="Both firms grew.")],
        claims=[
            Claim(claim_id=claim_id, section_id="sec1", text="TCS grew.", finding_ids=finding_ids)
            for claim_id, finding_ids in claims
        ],
        sources=[
            Source(url=known[fid].url, title=known[fid].title, finding_ids=[fid]) for fid in cited
        ],
    )
