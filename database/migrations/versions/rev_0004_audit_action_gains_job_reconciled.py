"""`ck_audit_events_action` widens to allow `job_reconciled`.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-20

[ADR 0021](../../../docs/adr/0021-stale-job-reconciliation-and-dlq-recovery.md) decision 5.
The Phase 5 block C sweep repairs a `jobs` row from durable state, and two of its four
mutations - a gate projection restored to `awaiting_approval`, and a `queued` job whose enqueue
never landed put back on the queue - finish no job and therefore write no `job_finished` row.
Without this action those mutations would change a durable row with no record of who made them
or why, which is the thing `ck_audit_events_actor` exists to make impossible one layer down.

**This is `rev_0003` with a different literal in it**, deliberately: the ordering rule, the
two dialect branches and the destructive downgrade are the same decisions for the same reasons,
so the shape is copied rather than reinvented.

**The ordering is a deploy-time rule rather than a repository one.** guidelines §19 requires the
widening revision to be applied before any code writes the value, and that holds by
construction: `alembic upgrade head` runs as its own task and must exit 0 before the new service
revision starts. The constraint and `database.schema.AuditAction` land in one commit because the
CHECK is built from `get_args(AuditAction)` and a test compares the two.

**Two dialects, because only one of them can alter a constraint.** PostgreSQL swaps the CHECK in
place. SQLite has no `ALTER TABLE ... DROP CONSTRAINT`, so the table is recreated - and
`copy_from` states the definition explicitly, because SQLite reflection cannot see a named CHECK
constraint and a recreate without it would silently drop both.

**The downgrade deletes the rows it cannot keep.** A narrowed CHECK cannot coexist with a row
whose action it forbids, and there is no other action that means "an operator repaired this row
from durable state". What the rollback loses is the record of the repair, not the repair: the
status is on the `jobs` row and, for the two mutations that finish a job, the reason is in the
`job_finished` row rev_0003 already allows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_TIMESTAMP = sa.DateTime(timezone=True)

_NARROW_ACTIONS = (
    "job_created",
    "plan_produced",
    "subtopic_researched",
    "revision",
    "reflection_failed",
    "gate_opened",
    "reviewer_decision",
    "export_attempted",
    "export_result",
    "job_finished",
    "retention_delete",
)
"""Exactly what rev_0003 allowed. Restated rather than imported: a migration says what the
schema looked like at this revision and has to keep saying it after `database/schema.py`
changes again."""

_WIDE_ACTIONS = (*_NARROW_ACTIONS[:-1], "job_reconciled", _NARROW_ACTIONS[-1])
"""The same list with `job_reconciled` in it, in the position `database/schema.py` gives it -
the vocabulary reads in the order a job's life runs. The position has no effect on the
constraint."""


def _check(actions: tuple[str, ...]) -> str:
    return "action IN (" + ", ".join(f"'{action}'" for action in actions) + ")"


def upgrade() -> None:
    _replace_the_action_check(_check(_WIDE_ACTIONS))


def downgrade() -> None:
    op.execute("DELETE FROM audit_events WHERE action = 'job_reconciled'")
    _replace_the_action_check(_check(_NARROW_ACTIONS))


def _replace_the_action_check(check: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        # Recreate the table around the new constraint. `copy_from` is the whole definition, so
        # the index, the foreign key and the actor CHECK survive the copy - and
        # `recreate="always"` is required rather than tidy: batch mode rebuilds only when an
        # operation inside the block demands it, and there is no operation here.
        with op.batch_alter_table(
            "audit_events", copy_from=_audit_events(check), recreate="always"
        ):
            pass
        return

    op.drop_constraint("ck_audit_events_action", "audit_events", type_="check")
    op.create_check_constraint("ck_audit_events_action", "audit_events", check)


def _audit_events(action_check: str) -> sa.Table:
    """`audit_events` exactly as rev_0001 created it, with `action_check` in place of its
    action CHECK."""
    return sa.Table(
        "audit_events",
        sa.MetaData(),
        sa.Column(
            "event_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("job_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("detail", _JSON, nullable=False),
        sa.Column("created_at", _TIMESTAMP, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.job_id"], name="fk_audit_events_job", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "actor <> 'unknown' AND trim(actor) <> ''", name="ck_audit_events_actor"
        ),
        sa.CheckConstraint(action_check, name="ck_audit_events_action"),
        sa.Index("ix_audit_events_job_created", "job_id", "created_at"),
    )
