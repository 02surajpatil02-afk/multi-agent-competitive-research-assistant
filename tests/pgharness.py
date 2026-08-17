"""
WHY THIS FILE EXISTS
    A real PostgreSQL 16 for a test, built the way the real one is built: by running
    `alembic upgrade head` against an empty schema. It is the second half of what
    tests/dbharness.py starts, and that file names the gap this one closes - SQLite proves
    the columns, the keys, the CHECK constraints and every statement in database/queries.py,
    and proves nothing about JSONB, `timestamptz`, the statement timeout, or two writers
    arriving at once.

    Three decisions are worth reading before the fixtures.

    **It is opt-in, through `TEST_DATABASE_URL` and nothing else.** `pytest` is supposed to
    need no running service and make no network calls (guidelines §18), so an unset variable
    skips rather than fails, and the variable is read from the process environment rather than
    from `.env` - a developer who has a URL sitting in a file has not asked for the offline
    suite to start opening sockets. Setting it is the explicit act that opts in.

    **It refuses a database whose name does not say `test`.** Every case here begins by
    dropping the whole schema, so pointing it at the database `uvicorn app:app` uses would
    destroy local development data on the first green run. The guard is cheap and the mistake
    is not recoverable, which is the whole argument for it. `docker-compose.yml` creates
    `research_test` beside `research` for exactly this.

    **Isolation is a dropped schema, not a dropped database.** `DROP SCHEMA public CASCADE`
    takes the five application tables, the checkpointer's own tables, and `alembic_version`
    with it, so each test starts from the same empty database a first deploy starts from -
    and creating a database per test would cost a connection to a second one and a template
    lock for no additional proof.

WHO CALLS IT
    tests/test_database_postgres.py and tests/test_checkpointer_postgres.py, which wrap these
    functions in their own fixtures the way tests/test_database.py wraps dbharness. Both files
    are marked `postgres`, so `pytest -m postgres` runs exactly this layer and
    `pytest -m "not postgres"` runs exactly the offline one.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.engine import Engine

from database.queries import create_database_engine, sqlalchemy_url

_ROOT = Path(__file__).resolve().parent.parent
"""The repository root, so Alembic finds its scripts wherever pytest was started from."""

URL_VARIABLE = "TEST_DATABASE_URL"

SKIP_REASON = (
    f"{URL_VARIABLE} is not set. Start the local infrastructure with "
    "`docker compose up -d --wait`, then set it to the research_test database."
)


def postgres_url() -> str:
    """The database these tests may destroy, or a skip.

    Raises rather than skips when the URL is set but points somewhere unsafe: a developer who
    exported the wrong value wants to be told, and a silent skip would look like the suite
    passing.
    """
    url = os.environ.get(URL_VARIABLE, "").strip()
    if not url:
        pytest.skip(SKIP_REASON)

    database = sa.engine.make_url(url).database or ""
    if "test" not in database.lower():
        raise RuntimeError(
            f"{URL_VARIABLE} names the database {database!r}, and every test here begins by "
            f"dropping its schema. Point it at a database whose name says test."
        )
    return _with_resolved_host(url)


def _with_resolved_host(url: str) -> str:
    """The same URL with its host name replaced by the address it resolves to.

    **This is what lets a checkpointer test run a graph and reach a real database at once.**
    The graph tests replace `socket.getaddrinfo` with a fake that refuses every host it was not
    told about - which is what keeps the offline suite offline (guidelines §18) - and the patch
    is process-wide for the duration of the test. A database URL naming `localhost` therefore
    fails to resolve inside exactly the tests that need it most, and fails slowly, as a pool
    timeout rather than as anything that names DNS.

    Resolving here, once, before any fake is installed, removes the question: psycopg skips
    resolution entirely for a literal address. An address is left alone, so a URL that already
    carries one costs nothing.
    """
    parsed = sa.engine.make_url(url)
    if not parsed.host or _is_an_address(parsed.host):
        return url
    resolved = parsed.set(host=socket.gethostbyname(parsed.host))
    return resolved.render_as_string(hide_password=False)


def _is_an_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def empty_database() -> str:
    """An empty database of this test's own: no application tables, no checkpointer tables, no
    migration history. What a first deploy meets, and where every checkpointer test starts.

    Returns the URL, because `PostgresSaver` wants one and does not take an engine.
    """
    url = postgres_url()
    _drop_everything(url)
    return url


def migrated_engine() -> Engine:
    """A migrated database of this test's own, and the engine the application would use.

    The engine is `create_database_engine`'s, not a plain one, so these tests run through the
    same 5-second statement timeout production gets (guidelines §17) rather than around it.

    It is deliberately the same name as `dbharness.migrated_engine`: same job, different
    engine underneath, and the two are never imported into one file.
    """
    url = empty_database()
    upgrade_to_head(url)
    return create_database_engine(url)


def upgrade_to_head(url: str) -> None:
    """`alembic upgrade head`, the same command the deploy runs (guidelines §19)."""
    command.upgrade(alembic_config(url), "head")


def alembic_config(url: str) -> AlembicConfig:
    """Alembic pointed at this repository's migrations and at `url`.

    Both paths are absolute, so neither depends on the working directory pytest was started in
    - the same reason tests/dbharness.py builds its config this way.
    """
    config = AlembicConfig(_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(_ROOT / "database" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _drop_everything(url: str) -> None:
    """Back to an empty database, whatever the last test left behind.

    Done on the way in rather than on the way out: a test that dies half way through leaves a
    dirty schema, and the next one should not inherit it.

    The engine is disposed immediately, because an idle connection of ours is one more thing
    `DROP SCHEMA` could contend with in a later test.
    """
    engine = sa.create_engine(sqlalchemy_url(url), poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            conn.execute(sa.text("CREATE SCHEMA public"))
    finally:
        engine.dispose()
