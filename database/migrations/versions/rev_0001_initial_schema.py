"""The five application tables: jobs, findings, claims, claim_sources, audit_events.

Revision ID: 0001
Revises:
Create Date: 2026-08-15

ARCHITECTURE.md §9's schema, created in one revision because the five tables reference each
other and half of them is not a working audit trail.

This file is a snapshot, not a view of database/schema.py: it says what the schema looked
like at this revision and must keep saying that after schema.py changes, so it repeats the
definitions rather than importing them. A test applies this migration and compares the
result against schema.py, which is what stops the two from drifting apart quietly
(tests/test_database.py).

LangGraph's checkpoint tables are deliberately absent. The checkpointer creates them
through its own `setup()`, and Alembic does not touch them (guidelines §19).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_TIMESTAMP = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "jobs",
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
        sa.CheckConstraint(
            "status IN ('running', 'awaiting_approval', 'approved', 'rejected', 'failed')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint(
            "quality_flag IS NULL OR quality_flag IN ('below_threshold', 'unscored')",
            name="ck_jobs_quality_flag",
        ),
    )
    # §9 writes this one as `(user_id, created_at desc)`. The columns are what matter: a
    # b-tree scans backwards as cheaply as forwards, so the newest-first job list uses this
    # index either way (database/schema.py).
    op.create_index("ix_jobs_user_created", "jobs", ["user_id", "created_at"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "findings",
        sa.Column("job_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("finding_id", sa.Text(), nullable=False),
        sa.Column("subtopic", sa.Text(), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("retrieved_at", _TIMESTAMP, nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        # ADR 0003: `f1`, `f2`, ... is a per-job sequence, so the job is half of the key.
        sa.PrimaryKeyConstraint("job_id", "finding_id"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.job_id"], name="fk_findings_job", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_findings_job_url", "findings", ["job_id", "url"])

    op.create_table(
        "claims",
        sa.Column("claim_id", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("supported", sa.Boolean(), nullable=True),
        sa.Column("verdict_note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("claim_id"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.job_id"], name="fk_claims_job", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_claims_job", "claims", ["job_id"])
    op.create_index("ix_claims_job_supported", "claims", ["job_id", "supported"])

    op.create_table(
        "claim_sources",
        sa.Column("claim_id", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("finding_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("claim_id", "finding_id"),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claims.claim_id"], name="fk_claim_sources_claim", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "finding_id"],
            ["findings.job_id", "findings.finding_id"],
            name="fk_claim_sources_finding",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_claim_sources_finding", "claim_sources", ["finding_id"])

    op.create_table(
        "audit_events",
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
        sa.CheckConstraint(
            "action IN ('job_created', 'plan_produced', 'subtopic_researched', 'revision', "
            "'reflection_failed', 'gate_opened', 'reviewer_decision', 'export_attempted', "
            "'export_result', 'retention_delete')",
            name="ck_audit_events_action",
        ),
    )
    op.create_index("ix_audit_events_job_created", "audit_events", ["job_id", "created_at"])


def downgrade() -> None:
    # Dropped in dependency order: claim_sources references both claims and findings.
    op.drop_table("audit_events")
    op.drop_table("claim_sources")
    op.drop_table("claims")
    op.drop_table("findings")
    op.drop_table("jobs")
