"""`ck_jobs_status` widens to allow `queued`.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-17

ADR 0010 decision 1. `POST /jobs` writes the row and the worker is what starts running it, so
between the two the honest value is `queued` - and until this revision the CHECK constraint
refuses it.

**The ordering is not optional, and it is a deploy-time rule rather than a repository one.**
guidelines §19's backward-compatibility rule says the widening revision must be applied before
any code writes the value. That holds by construction: `alembic upgrade head` runs as its own
task and must exit 0 before the new service revision starts, so the constraint is already wide
when the first `queued` row is written. The constraint and `schemas.JobStatus` land in one commit
because `database/schema.py` builds the CHECK from `get_args(JobStatus)` and a test compares the
two - splitting them would fail that test rather than satisfy the rule.

**Two dialects, because only one of them can alter a constraint.** PostgreSQL swaps the CHECK in
place. SQLite has no `ALTER TABLE ... DROP CONSTRAINT` at all, so the table is recreated - and
`copy_from` states the definition explicitly rather than reflecting it, because SQLite reflection
cannot see a named CHECK constraint and a recreate without this would silently drop both of them.
Restating the table is what a migration does here anyway: rev_0001 is a snapshot that repeats the
definitions rather than importing them.

The downgrade narrows the constraint again, and moves any `queued` row to `running` first -
otherwise a rollback would fail on data rather than on schema, which is the one thing
guidelines §19's rollback path must not do.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_TIMESTAMP = sa.DateTime(timezone=True)

_WIDE = "status IN ('queued', 'running', 'awaiting_approval', 'approved', 'rejected', 'failed')"
_NARROW = "status IN ('running', 'awaiting_approval', 'approved', 'rejected', 'failed')"


def upgrade() -> None:
    _replace_the_status_check(_WIDE)


def downgrade() -> None:
    op.execute("UPDATE jobs SET status = 'running' WHERE status = 'queued'")
    _replace_the_status_check(_NARROW)


def _replace_the_status_check(check: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        # Recreate the table around the new constraint. `copy_from` is the whole definition,
        # so the indexes and the other CHECK survive the copy - and `recreate="always"` is
        # required rather than tidy: batch mode rebuilds only when an operation inside the
        # block demands it, and there is no operation here. The constraint change *is* the
        # rebuild.
        with op.batch_alter_table("jobs", copy_from=_jobs(check), recreate="always"):
            pass
        return

    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_status", "jobs", check)


def _jobs(status_check: str) -> sa.Table:
    """`jobs` exactly as rev_0001 created it, with `status_check` in place of its CHECK."""
    return sa.Table(
        "jobs",
        sa.MetaData(),
        sa.Column("job_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("quality_flag", sa.Text(), nullable=True),
        sa.Column("revision_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("llm_calls_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("report_json", _JSON, nullable=True),
        sa.Column("exported_at", _TIMESTAMP, nullable=True),
        sa.Column("created_at", _TIMESTAMP, server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", _TIMESTAMP, nullable=True),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
        sa.CheckConstraint(status_check, name="ck_jobs_status"),
        sa.CheckConstraint(
            "quality_flag IS NULL OR quality_flag IN ('below_threshold', 'unscored')",
            name="ck_jobs_quality_flag",
        ),
        sa.Index("ix_jobs_user_created", "user_id", "created_at"),
        sa.Index("ix_jobs_status", "status"),
    )
