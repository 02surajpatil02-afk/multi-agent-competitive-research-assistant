"""
WHY THIS FILE EXISTS
    What `alembic upgrade head` runs before it runs a migration: where the database is, and
    what the schema is supposed to look like.

    Two decisions are worth reading.

    **The URL comes from the environment, not from alembic.ini.** A connection string is a
    secret (guidelines §16), and a migration needs exactly one thing to run. Building the
    whole `Config` here instead would mean an LLM key and a Tavily key had to be present to
    apply a migration, which is a startup failure waiting for the deploy step that runs
    migrations as its own ECS task (guidelines §19).

    **The engine is a plain one.** `database.queries.create_database_engine` applies §17's
    5-second statement timeout, which is a bound on application queries; a migration is not
    one, and a data migration that legitimately takes longer than five seconds should not be
    killed halfway.

    `target_metadata` is what makes `alembic check` and autogenerate able to compare the
    migrations against database/schema.py. A test does exactly that, so the two cannot
    drift apart quietly.

    **`include_name` is what stops that comparison being destructive.** Two things own tables
    in this database: Alembic owns the five application tables, and LangGraph's
    `PostgresSaver.setup()` owns four checkpoint tables that `metadata` deliberately does not
    describe (guidelines §19). Autogenerate proposes dropping anything it finds and
    `metadata` does not name, so against any database a worker has run - measured: four
    `DropTableOp` and three `remove_index` - it would offer to delete the state every paused
    job resumes from. The hook excludes exactly those four by name and nothing else, so a
    genuine application-schema difference is still reported.

WHO CALLS IT
    Alembic, when a migration command runs. Nothing imports it.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from database.queries import sqlalchemy_url
from database.schema import alembic_include_name, metadata

target_metadata = metadata


def _database_url() -> str:
    """The database to migrate: whatever the caller passed, else `DATABASE_URL`.

    The main option is what a test sets when it points Alembic at a temporary database.
    """
    url = context.config.get_main_option("sqlalchemy.url", None) or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set, so there is no database to migrate")
    # The same normalisation the application engine does: `postgresql://` means psycopg2 to
    # SQLAlchemy, and this project installs psycopg 3. A migration that cannot connect is a
    # deploy that stops at its first step (guidelines §19).
    return sqlalchemy_url(url)


def run_migrations() -> None:
    """Online mode only. Nothing in this project generates SQL scripts with `--sql`, and an
    offline branch that is never run is a branch nobody would notice breaking."""
    engine = create_engine(_database_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                # Without this, autogenerate against any database a worker has started
                # against proposes dropping LangGraph's four checkpoint tables - see the
                # hook and `CHECKPOINTER_TABLES` in database/schema.py.
                include_name=alembic_include_name,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
