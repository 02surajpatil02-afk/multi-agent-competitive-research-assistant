"""
WHY THIS FILE EXISTS
    The five application tables, exactly as ARCHITECTURE.md §9 specifies them: jobs,
    findings, claims, claim_sources, audit_events. They are defined once, here, as
    SQLAlchemy Core tables - not as ORM models, because nothing in this system needs an
    identity map or lazy loading, and a mapped class per table would be five classes
    standing in front of five INSERTs.

    Three things are worth reading before the columns.

    **The allowed values come from schemas.py.** `jobs.status` and `jobs.quality_flag` are
    CHECK constraints built from the same Literals the state and the API use, because
    schemas.py already says why: one definition per set of allowed values, so the state,
    the API, and the database cannot drift apart on what a status is allowed to be.

    **The keys carry ADR 0003.** `findings` has a composite primary key `(job_id,
    finding_id)`, because finding ids are a per-job sequence - `f1`, `f2`, ... - so every
    job has an `f1` and a primary key on the column alone would collide on the second job
    inserted. `claim_sources` therefore carries `job_id` as half of its foreign key into
    findings (ARCHITECTURE.md §9).

    **`audit_events` is append-only.** No row is ever updated, and none is deleted except
    by the retention sweep, which writes its own row saying so. `actor` has a CHECK that
    refuses the string `unknown`: an approval row that cannot say who approved is
    accountability theatre, and the database is the cheapest place to make that impossible
    (guidelines §9).

    The types are portable on purpose. `JSONB` is what PostgreSQL gets and plain `JSON` is
    what anything else gets, so the offline test suite - which runs on SQLite, because
    Phase 2 has no Postgres to run against until Compose arrives in Phase 3 - exercises
    these same definitions rather than a second copy of them.

WHO CALLS IT
    database/queries.py, the Alembic migration in database/migrations/ (which is checked
    against this file by a test), and the tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, get_args

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from schemas import JobStatus, QualityFlag

if TYPE_CHECKING:
    # Only for the two hook argument types below. Kept out of the runtime imports because
    # database.schema is on the API's and the graph's import path, and neither needs Alembic
    # loaded to read a table definition.
    from alembic.runtime.environment import NameFilterParentNames, NameFilterType

metadata = sa.MetaData()
"""Every application table. The checkpointer's tables are not in here (guidelines §19)."""

CHECKPOINTER_TABLES = frozenset(
    {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
)
"""The tables LangGraph's `PostgresSaver.setup()` owns, named here so Alembic can be told to
leave them alone. **This is the complement of `metadata`, not an addition to it** - nothing here
is a `sa.Table`, and adding one would hand Alembic a second owner for a schema it does not
manage.

**Why the list has to exist at all.** Autogenerate compares the database against `metadata` and
proposes dropping whatever it finds that `metadata` does not describe. In a database where a
worker has run - which is every database from Phase 3 onwards - that is these four tables, and
the proposal is `DropTableOp` on the state every paused job depends on.

**It is a deny-list rather than an allow-list, deliberately.** "Ignore everything `metadata`
does not name" would be shorter and would also silence the case Alembic is *supposed* to catch:
a table dropped from this file that still exists in the database. Naming the four keeps that
detection intact for everything else.

The cost of naming them is that the list can go stale if LangGraph adds a fifth.
`tests/test_checkpointer_postgres.py` runs the real `setup()` and fails if it creates a table
this set does not name, so the staleness is loud rather than silent.
"""

SYSTEM_ACTOR = "system"
"""`audit_events.actor` for a transition the graph made on its own. Anything that happened
at the human gate carries the reviewer's identity instead, never this (guidelines §9)."""

AuditAction = Literal[
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
]
"""The actions ARCHITECTURE.md §9 enumerates, plus `reflection_failed`.

That one is not an addition of ours: ARCHITECTURE.md §15 requires an `audit_events` row
when the scoring call fails and the report is kept, and §9's list simply does not carry it.
Both are in the specification, so the vocabulary follows the requirement rather than the
list, and the discrepancy is worth correcting in §9 rather than silently working around.
"""


def alembic_include_name(
    name: str | None, type_: NameFilterType, parent_names: NameFilterParentNames
) -> bool:
    """Whether autogenerate may consider a reflected object. Alembic's `include_name` hook.

    Excluding a table here stops it being reflected at all, so its indexes and constraints go
    with it - which is why this one predicate covers the whole table rather than needing a
    matching rule per object type.

    **It answers for tables and nothing else.** An index or a constraint that happens to share
    a name with one of these tables is still compared, because the boundary being defended is
    ownership of a table, not of a string.

    Wired up in database/migrations/env.py, which is the only place that runs a real
    autogenerate. The tests pass it to `compare_metadata` directly so they check this function
    rather than a copy of it.
    """
    if type_ == "table":
        return name not in CHECKPOINTER_TABLES
    return True


def _json() -> sa.types.TypeEngine[Any]:
    """`JSONB` on PostgreSQL, `JSON` everywhere else.

    ARCHITECTURE.md §9 specifies JSONB for `report_json` and `audit_events.detail`. The
    variant is what lets the offline suite run these definitions on SQLite without a second
    schema to keep in step.
    """
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _timestamp() -> sa.DateTime:
    """`timestamptz`. Every time in this schema is an instant rather than a wall clock:
    `retrieved_at` is what makes a claim explicable in June, and a naive column would make
    that answer depend on which machine read it."""
    return sa.DateTime(timezone=True)


def _one_of(column: str, values: tuple[str, ...]) -> str:
    """`column IN ('a', 'b')`, built from the Literal that already defines the vocabulary.

    The values are our own type arguments, never anything a caller sent, so composing the
    SQL text here cannot be an injection - what goes in is what this repository wrote.
    """
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


# --- jobs ---------------------------------------------------------------------------

jobs = sa.Table(
    "jobs",
    metadata,
    sa.Column("job_id", sa.Uuid(as_uuid=False), primary_key=True),
    # Single tenant today. Every table carries the owner so tenant scoping is an additive
    # change later rather than a migration (CLAUDE.md phase plan).
    sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
    sa.Column("question", sa.Text, nullable=False),
    # UNIQUE NOT NULL, derived server-side as sha256(user_id + question + date). The
    # database is the arbiter of a duplicate submission, not an application-level check
    # that races between two API tasks (ARCHITECTURE.md §9).
    sa.Column("idempotency_key", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("quality_flag", sa.Text),
    # Persisted so budget behaviour is auditable after the job ends.
    sa.Column("revision_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("llm_calls_used", sa.Integer, nullable=False, server_default=sa.text("0")),
    # The approved Report body, written by the export node only after the gate passes.
    # NULL means nothing was ever exported. This is what makes the report retrievable in
    # Phase 2, before S3 exists (ARCHITECTURE.md §8).
    sa.Column("report_json", _json()),
    sa.Column("exported_at", _timestamp()),
    sa.Column("created_at", _timestamp(), nullable=False, server_default=sa.func.now()),
    # Set by finalize only.
    sa.Column("completed_at", _timestamp()),
    sa.UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
    sa.CheckConstraint(_one_of("status", get_args(JobStatus)), name="ck_jobs_status"),
    sa.CheckConstraint(
        f"quality_flag IS NULL OR {_one_of('quality_flag', get_args(QualityFlag))}",
        name="ck_jobs_quality_flag",
    ),
    # The owner's job list, newest first. ARCHITECTURE.md §9 writes this one as
    # `(user_id, created_at desc)`; the columns are what matter and the direction is not.
    # A b-tree is scanned backwards just as cheaply as forwards, so `WHERE user_id = ?
    # ORDER BY created_at DESC` uses this index either way - while a DESC index is an
    # expression index, which neither Alembic's comparison nor SQLite's reflection can
    # represent, and which would therefore cost the migration-drift test its strictness for
    # no measurable gain.
    sa.Index("ix_jobs_user_created", "user_id", "created_at"),
    # The gate-expiry and retention sweeps.
    sa.Index("ix_jobs_status", "status"),
)


# --- findings -----------------------------------------------------------------------

findings = sa.Table(
    "findings",
    metadata,
    sa.Column(
        "job_id",
        sa.Uuid(as_uuid=False),
        sa.ForeignKey("jobs.job_id", ondelete="CASCADE", name="fk_findings_job"),
        primary_key=True,
    ),
    # `f1`, `f2`, ... - unique within a job and not beyond it (ADR 0003).
    sa.Column("finding_id", sa.Text, primary_key=True),
    # Which planned subtopic produced it. `Finding.subtopic_id` in the schema; the column
    # name is ARCHITECTURE.md §9's.
    sa.Column("subtopic", sa.Text, nullable=False),
    sa.Column("claim", sa.Text, nullable=False),
    # The verbatim quote, never a summary. A claim is not explicable without it, which is
    # why it is kept for the full retention window (guidelines §9).
    sa.Column("evidence", sa.Text, nullable=False),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    # Reproducibility: was the page you are reading now the page the claim was made from?
    sa.Column("retrieved_at", _timestamp(), nullable=False),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column("truncated", sa.Boolean, nullable=False),
    # Per-job URL dedupe verification, and the natural lookup when reflection excludes a
    # failing URL. The primary key's leading column already serves the per-job lookup.
    sa.Index("ix_findings_job_url", "job_id", "url"),
)


# --- claims -------------------------------------------------------------------------

claims = sa.Table(
    "claims",
    metadata,
    # Minted by the Synthesizer as a uuid4 hex, so it is unique across jobs and a global
    # primary key is safe here in a way it is not for findings (agents/synthesizer.py).
    sa.Column("claim_id", sa.Text, primary_key=True),
    sa.Column(
        "job_id",
        sa.Uuid(as_uuid=False),
        sa.ForeignKey("jobs.job_id", ondelete="CASCADE", name="fk_claims_job"),
        nullable=False,
    ),
    sa.Column("section", sa.Text, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    # The Fact-Checker's current verdict. NULL until the claim has been checked - a claim
    # exists from the moment the Synthesizer writes it, and the check is a later pass.
    sa.Column("supported", sa.Boolean),
    sa.Column("verdict_note", sa.Text),
    sa.Index("ix_claims_job", "job_id"),
    # "Show me the unsupported claims first" at the gate, in one query.
    sa.Index("ix_claims_job_supported", "job_id", "supported"),
)


# --- claim_sources ------------------------------------------------------------------

claim_sources = sa.Table(
    "claim_sources",
    metadata,
    sa.Column(
        "claim_id",
        sa.Text,
        sa.ForeignKey("claims.claim_id", ondelete="CASCADE", name="fk_claim_sources_claim"),
        primary_key=True,
    ),
    sa.Column("job_id", sa.Uuid(as_uuid=False), nullable=False),
    sa.Column("finding_id", sa.Text, primary_key=True),
    # The composite half of ADR 0003: a finding is identified by its job and its id.
    sa.ForeignKeyConstraint(
        ["job_id", "finding_id"],
        ["findings.job_id", "findings.finding_id"],
        ondelete="CASCADE",
        name="fk_claim_sources_finding",
    ),
    # The reverse question - "which claims rest on this source?" - which is what you ask
    # when a source turns out to be wrong (ARCHITECTURE.md §9).
    sa.Index("ix_claim_sources_finding", "finding_id"),
)


# --- audit_events -------------------------------------------------------------------

audit_events = sa.Table(
    "audit_events",
    metadata,
    # A sequence, so the timeline has an order even when two events land in the same
    # timestamp tick. BIGSERIAL on PostgreSQL; SQLite only autoincrements an INTEGER key.
    sa.Column(
        "event_id",
        sa.BigInteger().with_variant(sa.Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    sa.Column(
        "job_id",
        sa.Uuid(as_uuid=False),
        sa.ForeignKey("jobs.job_id", ondelete="CASCADE", name="fk_audit_events_job"),
        nullable=False,
    ),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("detail", _json(), nullable=False),
    sa.Column("created_at", _timestamp(), nullable=False, server_default=sa.func.now()),
    # "An approval row with actor = 'unknown' is a bug, not a tolerable gap" (guidelines
    # §9). An empty actor is the same bug with whitespace in it.
    sa.CheckConstraint("actor <> 'unknown' AND trim(actor) <> ''", name="ck_audit_events_actor"),
    sa.CheckConstraint(_one_of("action", get_args(AuditAction)), name="ck_audit_events_action"),
    # The audit trail is always read as a per-job timeline.
    sa.Index("ix_audit_events_job_created", "job_id", "created_at"),
)
