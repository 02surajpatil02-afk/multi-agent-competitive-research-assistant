"""`ck_audit_events_action` widens to allow `job_finished`.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-18

ADR 0009 decision 5. A failed job's reason has lived in the checkpoint since
[ADR 0008](../../../docs/adr/0008-a-failed-jobs-reason-lives-in-the-checkpoint-for-phase-2.md),
which chose the shape - a `job_finished` audit row carrying `{status, failure_reason}`, never
a `jobs.failure_reason` column - and deliberately did not build it. Phase 3 is the trigger
ADR 0008 predicted: recovering an export that failed is the first operation that has to read
a failure's reason from a durable place, and `export_write_failed` is the first reason that
can occur at all.

**The ordering is the point, and it is a deploy-time rule rather than a repository one.**
guidelines §19's backward-compatibility rule says the widening revision must be applied before
any code writes the value, and that holds by construction: `alembic upgrade head` runs as its
own task and must exit 0 before the new service revision starts, so the constraint is already
wide when `finish_job` writes its first `job_finished` row. The constraint and
`schemas`/`database.schema.AuditAction` land in one commit because the CHECK is built from
`get_args(AuditAction)` and a test compares the two - splitting them would fail that test
rather than satisfy the rule.

**Two dialects, because only one of them can alter a constraint.** PostgreSQL swaps the CHECK
in place. SQLite has no `ALTER TABLE ... DROP CONSTRAINT`, so the table is recreated - and
`copy_from` states the definition explicitly rather than reflecting it, because SQLite
reflection cannot see a named CHECK constraint and a recreate without this would silently drop
both of them. This is rev_0002's shape, applied to a different table.

**The downgrade deletes the rows it cannot keep, and that is worth stating out loud.**
`audit_events` is append-only everywhere else in this system - "no row is ever updated, and
none is deleted except by the retention sweep" - and a narrowed CHECK cannot coexist with a
row whose action it forbids. rev_0002 had somewhere to move its data to (`queued` became
`running`); there is no other action that means "the job finished", so there is nothing to
rewrite these rows into. Deleting them is what keeps guidelines §19's rollback path failing on
schema rather than on data. The fact itself is not lost by the rollback: the status is still on
the `jobs` row and the reason is still in the checkpoint, which is exactly where ADR 0008 left
it before this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
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
    "retention_delete",
)
"""Exactly what rev_0001 allowed. Restated rather than imported: a migration says what the
schema looked like at this revision and has to keep saying it after `database/schema.py`
changes again."""

_WIDE_ACTIONS = (*_NARROW_ACTIONS[:-1], "job_finished", _NARROW_ACTIONS[-1])
"""The same list with `job_finished` in it. It sits before `retention_delete` for the same
reason `database/schema.py` puts it there - the vocabulary reads in the order a job's life
runs - and the position has no effect on the constraint."""


def _check(actions: tuple[str, ...]) -> str:
    return "action IN (" + ", ".join(f"'{action}'" for action in actions) + ")"


def upgrade() -> None:
    _replace_the_action_check(_check(_WIDE_ACTIONS))


def downgrade() -> None:
    op.execute("DELETE FROM audit_events WHERE action = 'job_finished'")
    _replace_the_action_check(_check(_NARROW_ACTIONS))


def _replace_the_action_check(check: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        # Recreate the table around the new constraint. `copy_from` is the whole definition,
        # so the index, the foreign key and the actor CHECK survive the copy - and
        # `recreate="always"` is required rather than tidy: batch mode rebuilds only when an
        # operation inside the block demands it, and there is no operation here. The
        # constraint change *is* the rebuild.
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
