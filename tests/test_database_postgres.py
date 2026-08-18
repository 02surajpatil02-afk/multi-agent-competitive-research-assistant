"""
WHY THIS FILE EXISTS
    tests/test_database.py proves the schema and every statement in database/queries.py
    against SQLite, and tests/dbharness.py names what that cannot reach: JSONB, `timestamptz`,
    the server-side statement timeout, and two writers arriving at the same instant. Those are
    not details. Three of Phase 2's load-bearing behaviours are decided by exactly them.

    **The gate's keyed reads are JSON path expressions.** `record_gate_opened`,
    `read_gate_decision` and `count_reviewer_edits` all filter on `detail['calls_used']` or
    `detail['decision']`, and SQLAlchemy compiles those to different SQL on JSONB than on
    SQLite's JSON. A query that finds the row on SQLite and misses it on PostgreSQL would hand
    an answered gate to the next reviewer who asked, which is ADR 0007's whole subject.

    **`claim_gate` is a race, and SQLite cannot have one.** Its conditional UPDATE is the
    arbiter between two reviewers deciding at once, and "exactly one winner" is a statement
    about row locks under concurrent transactions. Here it is asserted with both callers
    provably in flight at the same time.

    **The migration has never run against the database it will run against.** `alembic upgrade
    head` on an empty PostgreSQL, compared against database/schema.py, is what turns "the
    deploy should work" into a fact.

    Everything here is marked `postgres` and skips when TEST_DATABASE_URL is unset. What it
    does *not* do is repeat the SQLite suite: the constraint vocabulary, the replay
    convergence, and the per-write audit rows are proven there, and only the cases whose answer
    depends on the engine are proven again here.

WHO CALLS IT
    `pytest -m postgres`, against the PostgreSQL 16 in docker-compose.yml.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, get_args

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from dbharness import AUTOGENERATE_OPTS, a_finding, a_report, new_job_id
from pgharness import (
    alembic_config,
    empty_database,
    migrated_engine,
    postgres_url,
    upgrade_to_head,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from database import queries
from database.schema import (
    AuditAction,
    audit_events,
    claim_sources,
    claims,
    findings,
    jobs,
    metadata,
)
from schemas import Claim, JobStatus, Report, ResearchPlan, Section, Source, Subtopic

pytestmark = pytest.mark.postgres

_TABLES = ("jobs", "findings", "claims", "claim_sources", "audit_events")

_INDEXES: dict[str, set[str]] = {
    "jobs": {"ix_jobs_user_created", "ix_jobs_status"},
    "findings": {"ix_findings_job_url"},
    "claims": {"ix_claims_job", "ix_claims_job_supported"},
    "claim_sources": {"ix_claim_sources_finding"},
    "audit_events": {"ix_audit_events_job_created"},
}
"""ARCHITECTURE.md §9's indexes. The columns are asserted in the SQLite suite; what is worth
re-asserting on PostgreSQL is that the migration actually created them here."""


@pytest.fixture
def empty_pg() -> str:
    """A database with nothing in it, and the URL onto it."""
    return empty_database()


@pytest.fixture
def pg_url() -> str:
    """The URL a test opens its own connection with.

    Read from the environment rather than off `Engine.url`, because SQLAlchemy renders that
    with the password masked and a reconstructed URL cannot log in.
    """
    return postgres_url()


@pytest.fixture
def pg() -> Engine:
    """A migrated database of this test's own."""
    return migrated_engine()


@pytest.fixture
def job(pg: Engine) -> str:
    """A job row every other row can hang off, created the way `POST /jobs` creates one."""
    job_id = new_job_id()
    queries.create_job(
        pg,
        job_id=job_id,
        user_id=new_job_id(),
        question="Compare TCS and Infosys on cloud strategy.",
        idempotency_key="key-1",
        actor="submitter-7",
    )
    return job_id


# --- The migration, against the database it will actually run against -----------------


def test_the_migration_runs_against_an_empty_database(empty_pg: str) -> None:
    """`alembic upgrade head` from nothing - the first step of every deploy (guidelines §19)."""
    upgrade_to_head(empty_pg)

    engine = queries.create_database_engine(empty_pg)
    tables = set(sa.inspect(engine).get_table_names())

    assert set(_TABLES) <= tables
    # The checkpointer owns its own tables through setup(); Alembic does not touch them.
    assert not {name for name in tables if name.startswith("checkpoint")}


def test_the_migration_and_the_schema_definition_agree_on_postgres(pg: Engine) -> None:
    """The drift test, run on the dialect that decides what a column type means.

    On SQLite `_json()` yields `JSON` and `_timestamp()` yields a `DATETIME` that ignores the
    timezone flag, so the SQLite run of this test cannot tell JSONB from JSON or `timestamptz`
    from `timestamp`. Here it can, and `compare_type=True` makes it.
    """
    with pg.connect() as conn:
        context = MigrationContext.configure(conn, opts=AUTOGENERATE_OPTS)

        assert compare_metadata(context, metadata) == []


def test_the_migration_creates_every_documented_index(pg: Engine) -> None:
    inspector = sa.inspect(pg)

    for table, expected in _INDEXES.items():
        found = {index["name"] for index in inspector.get_indexes(table)}

        assert expected <= found, table


def test_the_migration_can_be_undone_on_postgres(pg: Engine, pg_url: str) -> None:
    """A migration with no working downgrade is a one-way door (guidelines §19).

    Proven separately from the SQLite case because PostgreSQL enforces drop ordering against
    real foreign keys, so a downgrade that drops `jobs` before `findings` fails here and
    passes there.
    """
    command.downgrade(alembic_config(pg_url), "base")

    assert not set(_TABLES) & set(sa.inspect(pg).get_table_names())


# --- rev_0002: the CHECK constraint that lets a job be `queued` (ADR 0010) -------------
#
# The revision has to land before any code writes the value, which is guidelines §19's
# backward-compatibility rule and ADR 0010 decision 1's first requirement. On PostgreSQL the
# constraint is swapped in place; on SQLite the whole table is recreated around it. Only one of
# those two dialects is the one production runs, and until now only the other had been tried.


def _status_check(engine: Engine) -> str:
    """`ck_jobs_status` as PostgreSQL itself renders it."""
    with engine.connect() as conn:
        return str(
            conn.execute(
                sa.text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_jobs_status'"
                )
            ).scalar_one()
        )


def test_the_status_check_names_the_whole_documented_vocabulary(pg: Engine) -> None:
    # `schemas.JobStatus` is what `database/schema.py` builds the CHECK from, so this is the
    # two agreeing on the database rather than in Python.
    rendered = _status_check(pg)

    for status in get_args(JobStatus):
        assert f"'{status}'" in rendered, status


@pytest.mark.parametrize("status", list(get_args(JobStatus)))
def test_every_status_in_the_vocabulary_is_accepted(status: str, pg: Engine, job: str) -> None:
    """`queued` included, which is the value rev_0002 exists for.

    Parametrized over the whole vocabulary rather than over the new value alone, because the
    failure mode of a rewritten CHECK is not "the new value is missing" - it is "an old one
    was dropped while nobody was looking".
    """
    with pg.begin() as conn:
        conn.execute(sa.update(jobs).where(jobs.c.job_id == job).values(status=status))

    assert _status(pg, job) == status


def test_a_new_job_is_queued_on_postgres(pg: Engine, job: str) -> None:
    # `create_job` writes `queued` (ADR 0010 decision 2), so a constraint that still refused it
    # would fail every submission on the first deploy after the API ships.
    assert _status(pg, job) == "queued"


def test_the_revision_before_it_refuses_a_queued_job(empty_pg: str) -> None:
    """The ordering rule, proven by standing on the wrong side of it.

    At rev_0001 the CHECK does not list `queued`, so `POST /jobs` running against that schema
    would fail on its first insert. That is exactly why guidelines §19 requires the widening
    revision to be applied - as its own task, exiting 0 - **before** the new service revision
    starts, and why this is worth a test rather than a sentence.
    """
    command.upgrade(alembic_config(empty_pg), "0001")
    engine = queries.create_database_engine(empty_pg)

    with pytest.raises(IntegrityError):
        queries.create_job(
            engine,
            job_id=new_job_id(),
            user_id=new_job_id(),
            question="Compare TCS and Infosys on cloud strategy.",
            idempotency_key="key-early",
            actor="submitter-7",
        )

    command.upgrade(alembic_config(empty_pg), "head")
    job_id = new_job_id()
    queries.create_job(
        engine,
        job_id=job_id,
        user_id=new_job_id(),
        question="Compare TCS and Infosys on cloud strategy.",
        idempotency_key="key-later",
        actor="submitter-7",
    )
    assert _status(engine, job_id) == "queued"


def test_the_downgrade_moves_a_queued_job_to_running_rather_than_failing(
    pg: Engine, pg_url: str, job: str
) -> None:
    """A rollback must fail on schema or not at all - never on data (guidelines §19).

    Narrowing the constraint with a `queued` row still in the table would abort the downgrade
    part-way, which is the one thing the rollback path cannot afford. So the revision moves the
    row first, and `running` is the honest destination: at rev_0001 that is what a job nobody
    has finished is called.
    """
    assert _status(pg, job) == "queued"

    command.downgrade(alembic_config(pg_url), "0001")

    assert _status(pg, job) == "running"
    assert "'queued'" not in _status_check(pg)
    with pytest.raises(IntegrityError), pg.begin() as conn:
        conn.execute(sa.update(jobs).where(jobs.c.job_id == job).values(status="queued"))


def test_the_widening_and_the_narrowing_leave_everything_else_alone(
    pg: Engine, pg_url: str, job: str
) -> None:
    """The risk a constraint swap carries, checked on both sides of the round trip.

    rev_0002 touches one CHECK and nothing else, and the drift test above proves that at head.
    What it cannot see is a downgrade-then-upgrade that loses an index or the other CHECK on
    the way through - so the round trip is made and the schema compared again.
    """
    command.downgrade(alembic_config(pg_url), "0001")
    command.upgrade(alembic_config(pg_url), "head")

    with pg.connect() as conn:
        context = MigrationContext.configure(conn, opts=AUTOGENERATE_OPTS)
        assert compare_metadata(context, metadata) == []
    found = {index["name"] for index in sa.inspect(pg).get_indexes("jobs")}
    assert _INDEXES["jobs"] <= found
    assert _status(pg, job) == "running"  # the row survived, moved by the downgrade


# --- rev_0003: the CHECK constraint that lets a job say why it finished (ADR 0009) -----
#
# Same shape as rev_0002 above and the same deploy-time rule: the widening revision has to be
# applied before `finish_job` writes its first `job_finished` row. The difference worth testing
# separately is the table - `audit_events` carries a foreign key, a second CHECK and an index,
# and the SQLite path recreates all of that while the PostgreSQL path swaps one constraint.
# Only the second is what production runs.


def _action_check(engine: Engine) -> str:
    """`ck_audit_events_action` as PostgreSQL itself renders it."""
    with engine.connect() as conn:
        return str(
            conn.execute(
                sa.text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_audit_events_action'"
                )
            ).scalar_one()
        )


def test_the_action_check_names_the_whole_documented_vocabulary(pg: Engine) -> None:
    # `database.schema.AuditAction` is what the CHECK is built from, so this is the two
    # agreeing on the database rather than in Python.
    rendered = _action_check(pg)

    for action in get_args(AuditAction):
        assert f"'{action}'" in rendered, action


@pytest.mark.parametrize("action", list(get_args(AuditAction)))
def test_every_action_in_the_vocabulary_is_accepted(action: str, pg: Engine, job: str) -> None:
    """`job_finished` included, which is the value rev_0003 exists for.

    Parametrized over the whole vocabulary rather than the new value alone, for rev_0002's
    reason: the failure mode of a rewritten CHECK is not "the new value is missing" - it is
    "an old one was dropped while nobody was looking".
    """
    with pg.begin() as conn:
        conn.execute(
            sa.insert(audit_events).values(job_id=job, actor="system", action=action, detail={})
        )

    written = [event.action for event in queries.read_audit_events(pg, job)]
    assert action in written


def test_finishing_a_job_writes_its_reason_to_the_trail_on_postgres(pg: Engine, job: str) -> None:
    """ADR 0009 decision 5 on the engine that will run it.

    This is the row that stops a failed job's reason living only in a checkpoint - the risk
    ADR 0008 accepted for Phase 2 and assigned here: *"If a job's checkpoint is ever pruned,
    its `jobs` row still says `failed` for the remaining retention window with nothing left to
    say why."*
    """
    queries.finish_job(
        pg,
        job_id=job,
        status="failed",
        failure_reason="export_write_failed",
        quality_flag=None,
        revision_count=0,
        llm_calls_used=30,
    )

    event = queries.read_audit_events(pg, job)[-1]
    assert (event.actor, event.action) == ("system", "job_finished")
    assert event.detail == {"status": "failed", "failure_reason": "export_write_failed"}


def test_one_job_finishes_once_however_often_finalize_runs(pg: Engine, job: str) -> None:
    # The guard is a JSONB-free read on `action`, but the convergence rule is the same one
    # ADR 0005 applies to every graph-time write, and it is worth proving on this engine too.
    for _ in range(3):
        queries.finish_job(
            pg,
            job_id=job,
            status="approved",
            failure_reason=None,
            quality_flag=None,
            revision_count=0,
            llm_calls_used=16,
        )

    finished = [
        event for event in queries.read_audit_events(pg, job) if event.action == "job_finished"
    ]
    assert len(finished) == 1


def test_the_recoverable_set_is_one_indexed_query(pg: Engine, job: str) -> None:
    """ADR 0009 decision 1's predicate, run as SQL against PostgreSQL.

    *"Which approved reports have no artifact?"* has to be a query rather than an
    investigation, which is the same property `claim_sources` was built for one level down.
    `ix_jobs_status` already indexes the leading column.
    """
    queries.record_research(
        pg, job_id=job, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )
    queries.record_export_result(pg, job_id=job, report=a_report(("c1", ["f1"])), uncited=[])
    queries.finish_job(
        pg,
        job_id=job,
        status="failed",
        failure_reason="export_write_failed",
        quality_flag=None,
        revision_count=0,
        llm_calls_used=30,
    )

    with pg.connect() as conn:
        recoverable = (
            conn.execute(
                sa.select(jobs.c.job_id).where(
                    jobs.c.status == "failed",
                    jobs.c.report_json.is_not(None),
                    jobs.c.exported_at.is_(None),
                )
            )
            .scalars()
            .all()
        )

    assert list(recoverable) == [job]

    # And once the artifact exists, the job leaves the set without its history changing.
    queries.record_artifact_written(pg, job_id=job, actor="ops-alice", key=f"reports/{job}.json")

    with pg.connect() as conn:
        still = (
            conn.execute(
                sa.select(jobs.c.job_id).where(
                    jobs.c.status == "failed",
                    jobs.c.report_json.is_not(None),
                    jobs.c.exported_at.is_(None),
                )
            )
            .scalars()
            .all()
        )

    assert list(still) == []
    assert _status(pg, job) == "failed"


def test_the_revision_before_it_refuses_a_job_finished_row(empty_pg: str) -> None:
    """The ordering rule, proven by standing on the wrong side of it.

    At rev_0002 the CHECK does not list `job_finished`, so a `finalize` running against that
    schema would fail on every terminal job. That is why guidelines §19 requires the widening
    revision to be applied - as its own task, exiting 0 - **before** the new service revision
    starts.
    """
    command.upgrade(alembic_config(empty_pg), "0002")
    engine = queries.create_database_engine(empty_pg)
    job_id = new_job_id()
    queries.create_job(
        engine,
        job_id=job_id,
        user_id=new_job_id(),
        question="Compare TCS and Infosys on cloud strategy.",
        idempotency_key="key-early",
        actor="submitter-7",
    )

    with pytest.raises(IntegrityError):
        queries.finish_job(
            engine,
            job_id=job_id,
            status="failed",
            failure_reason="export_write_failed",
            quality_flag=None,
            revision_count=0,
            llm_calls_used=1,
        )

    upgrade_to_head(empty_pg)
    queries.finish_job(
        engine,
        job_id=job_id,
        status="failed",
        failure_reason="export_write_failed",
        quality_flag=None,
        revision_count=0,
        llm_calls_used=1,
    )
    assert queries.read_audit_events(engine, job_id)[-1].action == "job_finished"
    engine.dispose()


def test_the_downgrade_removes_the_rows_the_narrow_check_cannot_hold(empty_pg: str) -> None:
    """rev_0003's downgrade deletes its `job_finished` rows, and that is stated out loud
    rather than discovered.

    `audit_events` is append-only everywhere else, and rev_0002 had somewhere to move its data
    to - `queued` became `running`. There is no other action meaning "the job finished", so a
    rollback either deletes these rows or fails on data, and guidelines §19's rollback path
    must not fail on data. The fact is not lost: the status is still on the `jobs` row and the
    reason is still in the checkpoint, which is where ADR 0008 left it before this revision.
    """
    upgrade_to_head(empty_pg)
    engine = queries.create_database_engine(empty_pg)
    job_id = new_job_id()
    queries.create_job(
        engine,
        job_id=job_id,
        user_id=new_job_id(),
        question="Compare TCS and Infosys on cloud strategy.",
        idempotency_key="key-down",
        actor="submitter-7",
    )
    queries.finish_job(
        engine,
        job_id=job_id,
        status="failed",
        failure_reason="export_write_failed",
        quality_flag=None,
        revision_count=0,
        llm_calls_used=1,
    )
    engine.dispose()

    command.downgrade(alembic_config(empty_pg), "0002")

    engine = queries.create_database_engine(empty_pg)
    actions = [event.action for event in queries.read_audit_events(engine, job_id)]
    assert "job_finished" not in actions
    assert "job_created" in actions  # and nothing else was taken with it
    assert _status(engine, job_id) == "failed"  # the outcome is still on the row
    engine.dispose()


# --- The types SQLite cannot represent ------------------------------------------------


def test_the_json_columns_are_jsonb(pg: Engine) -> None:
    """`report_json` and `audit_events.detail` are JSONB, which ARCHITECTURE.md §9 specifies
    and every keyed gate read depends on."""
    types = {
        ("jobs", "report_json"): _column_type(pg, "jobs", "report_json"),
        ("audit_events", "detail"): _column_type(pg, "audit_events", "detail"),
    }

    assert types == {("jobs", "report_json"): "jsonb", ("audit_events", "detail"): "jsonb"}


def test_a_report_body_survives_the_round_trip_through_jsonb(pg: Engine, job: str) -> None:
    """JSONB is not a text column: it reorders keys and normalises numbers. What has to come
    back unchanged is the report itself, because `GET /jobs/{id}` serves this row."""
    queries.record_research(
        pg, job_id=job, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )
    report = a_report(("c1", ["f1"]))
    queries.record_claims(pg, job_id=job, report=report)

    queries.record_export_result(pg, job_id=job, report=report, uncited=[])

    row = queries.read_job(pg, job)
    assert row is not None
    assert row.report_json == report.model_dump(mode="json")


def test_every_timestamp_column_is_timestamptz(pg: Engine) -> None:
    """`retrieved_at` is what makes a claim explicable in June, and a naive column would make
    that answer depend on which machine read it (database/schema.py)."""
    columns = [
        ("jobs", "created_at"),
        ("jobs", "completed_at"),
        ("jobs", "exported_at"),
        ("findings", "retrieved_at"),
        ("audit_events", "created_at"),
    ]

    types = {(table, column): _column_type(pg, table, column) for table, column in columns}

    assert set(types.values()) == {"timestamp with time zone"}


def test_a_finding_keeps_its_instant_across_timezones(pg: Engine, job: str) -> None:
    """Written in UTC, read back as the same instant with an offset attached - not as a naive
    wall clock the reader has to guess the zone of."""
    queries.record_research(
        pg, job_id=job, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )

    row = queries.read_findings(pg, job)[0]

    assert row.retrieved_at.tzinfo is not None
    assert row.retrieved_at == datetime(2026, 8, 15, 9, 30, tzinfo=UTC)


def test_the_audit_trail_is_ordered_by_a_real_sequence(pg: Engine, job: str) -> None:
    """`event_id` is BIGSERIAL here and a plain INTEGER key on SQLite. Two rows written inside
    one timestamp tick still have an order, and it is the sequence that knows it."""
    for _ in range(3):
        queries.record_reflection_failed(pg, job_id=job)

    ids = [row.event_id for row in queries.read_audit_events(pg, job)]

    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


# --- The constraints, enforced by this engine -----------------------------------------


def test_a_job_status_outside_the_vocabulary_is_refused(pg: Engine, job: str) -> None:
    with pytest.raises(IntegrityError), pg.begin() as conn:
        conn.execute(sa.update(jobs).where(jobs.c.job_id == job).values(status="nearly_done"))


def test_an_audit_row_can_never_say_the_actor_was_unknown(pg: Engine, job: str) -> None:
    """`ck_audit_events_actor` uses `trim()`, and an engine that spelled it differently would
    let an approval through with a whitespace actor (guidelines §9)."""
    for actor in ("unknown", "   "):
        with pytest.raises(IntegrityError), pg.begin() as conn:
            conn.execute(
                sa.insert(audit_events).values(
                    job_id=job, actor=actor, action="reviewer_decision", detail={}
                )
            )


def test_two_jobs_may_each_have_their_own_f1(pg: Engine, job: str) -> None:
    """ADR 0003's composite primary key. Finding ids are a per-job sequence, so every job has
    an `f1` and a key on the column alone would collide on the second job inserted."""
    other = new_job_id()
    queries.create_job(
        pg,
        job_id=other,
        user_id=new_job_id(),
        question="Another question.",
        idempotency_key="key-2",
        actor="submitter-7",
    )

    for job_id in (job, other):
        queries.record_research(
            pg, job_id=job_id, new_findings=[a_finding("f1")], subtopic="s1", status="done"
        )

    assert sorted(row.job_id for row in _all(pg, findings)) == sorted([job, other])


def test_a_claim_source_cannot_point_at_a_finding_that_does_not_exist(pg: Engine, job: str) -> None:
    """The composite half of ADR 0003: a finding is identified by its job and its id, so the
    foreign key carries both columns."""
    queries.record_research(
        pg, job_id=job, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )
    queries.record_claims(pg, job_id=job, report=a_report(("c1", ["f1"])))

    with pytest.raises(IntegrityError), pg.begin() as conn:
        conn.execute(sa.insert(claim_sources).values(claim_id="c1", job_id=job, finding_id="f99"))


def test_deleting_a_job_takes_its_findings_claims_and_audit_trail_with_it(
    pg: Engine, job: str
) -> None:
    """`ON DELETE CASCADE` is what the Phase 5 retention sweep will lean on, and PostgreSQL is
    the first engine here that enforces it without a per-connection pragma."""
    queries.record_research(
        pg, job_id=job, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )
    queries.record_claims(pg, job_id=job, report=a_report(("c1", ["f1"])))

    with pg.begin() as conn:
        conn.execute(sa.delete(jobs).where(jobs.c.job_id == job))

    assert _all(pg, findings) == []
    assert _all(pg, claims) == []
    assert _all(pg, claim_sources) == []
    assert _all(pg, audit_events) == []


def test_a_write_that_fails_halfway_takes_its_audit_row_with_it(pg: Engine) -> None:
    """Each write function is one transaction (ADR 0005). An audit row that survived the write
    it describes would be a record of something that did not happen, so a finding whose job
    does not exist takes its own audit row down with it."""
    missing = new_job_id()

    with pytest.raises(IntegrityError):
        queries.record_research(
            pg, job_id=missing, new_findings=[a_finding("f1")], subtopic="s1", status="done"
        )

    assert queries.read_audit_events(pg, missing) == []


def test_a_draft_whose_citation_is_broken_leaves_the_previous_claims_intact(
    pg: Engine, job: str
) -> None:
    """`record_claims` deletes the old claim set before inserting the new one, so the delete
    has to roll back with the failed insert or a bad draft erases a good one.

    PostgreSQL is where this is worth re-asserting: a failed statement aborts the whole
    transaction here, and everything after it in the same block would fail too - so a rollback
    that looked correct on SQLite could still leave the wrong rows behind here.
    """
    queries.record_plan(pg, job_id=job, plan=_plan())
    queries.record_research(
        pg, job_id=job, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )
    queries.record_claims(pg, job_id=job, report=a_report(("c1", ["f1"]), ("c2", ["f1"])))

    with pytest.raises(IntegrityError):
        queries.record_claims(pg, job_id=job, report=_report_citing("f7"))

    assert {row.claim_id for row in queries.read_claims(pg, job)} == {"c1", "c2"}
    assert {row.finding_id for row in queries.read_claim_sources(pg, job)} == {"f1"}


# --- The Phase 2 gate statements, on JSONB --------------------------------------------


def test_opening_the_gate_writes_one_row_and_moves_the_job(pg: Engine, job: str) -> None:
    queries.record_gate_opened(pg, job_id=job, calls_used=12, summary={"claims": 4})

    row = queries.read_job(pg, job)
    assert row is not None
    assert row.status == "awaiting_approval"
    assert _actions(pg, job) == ["job_created", "gate_opened"]


def test_a_replayed_gate_opening_does_not_reopen_a_claimed_gate(pg: Engine, job: str) -> None:
    """ADR 0007 invariant 4, and the case the JSON path expression decides.

    LangGraph re-runs an interrupted node from the top on resume, so the gate node's
    pre-interrupt code executes twice per visit. `calls_used` identifies the visit. If the
    keyed lookup missed on JSONB, the replay would set `awaiting_approval` again milliseconds
    after `claim_gate` moved the row to `running` - handing an answered gate to the next
    reviewer who asked.
    """
    queries.record_gate_opened(pg, job_id=job, calls_used=12, summary={"claims": 4})
    assert queries.claim_gate(pg, job) is True

    queries.record_gate_opened(pg, job_id=job, calls_used=12, summary={"claims": 4})

    row = queries.read_job(pg, job)
    assert row is not None
    assert row.status == "running"
    assert _actions(pg, job).count("gate_opened") == 1


def test_the_next_gate_visit_opens_the_gate_again(pg: Engine, job: str) -> None:
    """A later visit costs a Synthesizer, a Fact-Checker and a reflection pass, so its
    `calls_used` is strictly greater - which is what makes it a different visit."""
    queries.record_gate_opened(pg, job_id=job, calls_used=12, summary={})
    queries.claim_gate(pg, job)

    queries.record_gate_opened(pg, job_id=job, calls_used=15, summary={})

    row = queries.read_job(pg, job)
    assert row is not None
    assert row.status == "awaiting_approval"
    assert _actions(pg, job).count("gate_opened") == 2


def test_one_gate_visit_records_one_reviewer_decision(pg: Engine, job: str) -> None:
    """ADR 0007 invariant 1. A resume can die after the row is written and the reviewer's fix
    is to send the same request again; without the key that retry would spend one of the three
    edits ADR 0006 allows."""
    for note in ("first", "second"):
        queries.record_reviewer_decision(
            pg, job_id=job, actor="reviewer-1", decision="edit", note=note, edits="x", calls_used=12
        )

    recorded = queries.read_gate_decision(pg, job, calls_used=12)
    assert recorded is not None and recorded["decision"] == "edit"
    assert queries.count_reviewer_edits(pg, job) == 1


def test_reading_a_gate_visits_decision_answers_only_for_that_visit(pg: Engine, job: str) -> None:
    queries.record_reviewer_decision(
        pg, job_id=job, actor="reviewer-1", decision="approve", note=None, edits=None, calls_used=12
    )

    answered = queries.read_gate_decision(pg, job, calls_used=12)
    assert answered is not None and answered["decision"] == "approve"
    assert queries.read_gate_decision(pg, job, calls_used=15) is None


def test_one_job_s_decisions_are_not_read_as_anothers(pg: Engine, job: str) -> None:
    other = new_job_id()
    queries.create_job(
        pg,
        job_id=other,
        user_id=new_job_id(),
        question="Another question.",
        idempotency_key="key-2",
        actor="submitter-7",
    )
    queries.record_reviewer_decision(
        pg, job_id=job, actor="reviewer-1", decision="edit", note=None, edits="x", calls_used=12
    )

    assert queries.read_gate_decision(pg, other, calls_used=12) is None
    assert queries.count_reviewer_edits(pg, other) == 0


def test_reviewer_edits_are_counted_from_the_audit_trail(pg: Engine, job: str) -> None:
    """The input to ADR 0006's bound, read from the trail rather than from a column of its own.
    Only `edit` decisions count - an approve and a reject at other visits must not."""
    for index, decision in enumerate(("edit", "approve", "edit", "reject")):
        queries.record_reviewer_decision(
            pg,
            job_id=job,
            actor="reviewer-1",
            decision=decision,
            note=None,
            edits="x" if decision == "edit" else None,
            calls_used=10 + index,
        )

    assert queries.count_reviewer_edits(pg, job) == 2


def test_setting_a_status_leaves_a_finished_job_alone(pg: Engine, job: str) -> None:
    """`set_job_status` reconciles the row with the checkpoint after a resume, and
    `completed_at IS NULL` is what stops it talking over `finalize` (ADR 0007 invariant 4)."""
    queries.set_job_status(pg, job_id=job, status="awaiting_approval")
    assert _status(pg, job) == "awaiting_approval"

    queries.finish_job(
        pg,
        job_id=job,
        status="approved",
        failure_reason=None,
        quality_flag=None,
        revision_count=1,
        llm_calls_used=12,
    )
    queries.set_job_status(pg, job_id=job, status="failed")

    assert _status(pg, job) == "approved"


# --- Replay convergence, on the engine that will run it -------------------------------


def test_replaying_a_researcher_visit_writes_no_duplicate_findings(pg: Engine, job: str) -> None:
    """A node writes its rows, the process dies before the checkpoint, and the redelivered
    message runs it again (ADR 0005). Findings are written by key, so the second run refreshes
    rather than duplicating."""
    for _ in range(2):
        queries.record_research(
            pg,
            job_id=job,
            new_findings=[a_finding("f1"), a_finding("f2")],
            subtopic="s1",
            status="done",
        )

    assert [row.finding_id for row in queries.read_findings(pg, job)] == ["f1", "f2"]
    # `audit_events` is the deliberate exception: a node that genuinely ran twice has two rows.
    assert _actions(pg, job).count("subtopic_researched") == 2


def test_a_second_draft_replaces_the_first_ones_claims(pg: Engine, job: str) -> None:
    queries.record_research(
        pg, job_id=job, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )
    queries.record_claims(pg, job_id=job, report=a_report(("c1", ["f1"]), ("c2", ["f1"])))

    queries.record_claims(pg, job_id=job, report=a_report(("c3", ["f1"])))

    assert [row.claim_id for row in queries.read_claims(pg, job)] == ["c3"]
    assert [row.claim_id for row in queries.read_claim_sources(pg, job)] == ["c3"]


# --- Two callers at once, which is the case SQLite cannot have ------------------------


def test_only_one_of_two_simultaneous_gate_claims_wins(pg: Engine, pg_url: str, job: str) -> None:
    """Two reviewers deciding at once - the case `claim_gate` exists for.

    Both callers are held inside their transaction until a third connection releases the row,
    so this is a genuine race rather than two calls that happened to run in sequence. The
    conditional UPDATE has exactly one winner: the loser re-evaluates its WHERE clause against
    the committed row, finds `running`, and matches nothing.
    """
    queries.record_gate_opened(pg, job_id=job, calls_used=12, summary={})

    results = _race(lambda: queries.claim_gate(pg, job), engine=pg, url=pg_url, job=job, callers=2)

    assert sorted(results) == [False, True]
    assert _status(pg, job) == "running"


def test_a_crowd_of_gate_claims_still_produces_one_winner(
    pg: Engine, pg_url: str, job: str
) -> None:
    """The same property with five callers, so a two-caller pass cannot be luck.

    Five is the connection pool's size. Past it a caller would have to open a connection of its
    own, which is the thing `_race` warms the pool to avoid.
    """
    queries.record_gate_opened(pg, job_id=job, calls_used=12, summary={})

    results = _race(lambda: queries.claim_gate(pg, job), engine=pg, url=pg_url, job=job, callers=5)

    assert results.count(True) == 1
    assert _status(pg, job) == "running"


def test_a_gate_that_was_never_opened_cannot_be_claimed(pg: Engine, job: str) -> None:
    """`create_job` leaves the row `running`, so there is nothing to claim - which is the
    `409 job_not_awaiting_approval` the endpoint returns."""
    assert queries.claim_gate(pg, job) is False


def test_two_simultaneous_submissions_of_one_question_create_one_job(pg: Engine) -> None:
    """`idempotency_key` is `UNIQUE NOT NULL` because the database is the arbiter of a
    duplicate submission, not an application-level check that races between two API tasks
    (ARCHITECTURE.md §9). The loser gets `IntegrityError`, which `POST /jobs` turns into a
    `409` carrying the `job_id` the caller already has.
    """
    user = new_job_id()
    ready = threading.Barrier(2)

    def submit() -> str | None:
        job_id = new_job_id()
        ready.wait(timeout=10)
        try:
            queries.create_job(
                pg,
                job_id=job_id,
                user_id=user,
                question="Compare TCS and Infosys on cloud strategy.",
                idempotency_key="same-key",
                actor="submitter-7",
            )
        except IntegrityError:
            return None
        return job_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result(timeout=30) for future in [pool.submit(submit) for _ in range(2)]]

    created = [job_id for job_id in outcomes if job_id is not None]
    assert len(created) == 1
    existing = queries.read_job_by_idempotency_key(pg, "same-key")
    assert existing is not None
    assert existing.job_id == created[0]
    assert len(_all(pg, jobs)) == 1


# --- The statement timeout, which only a server can enforce ---------------------------


def test_the_application_engine_carries_the_five_second_statement_timeout(pg: Engine) -> None:
    """guidelines §17's database row: 5s, 0 retries, fail loudly. It is set as a connection
    option, so it is the server that cuts a hanging query off rather than the client giving up
    while the query keeps running."""
    with pg.connect() as conn:
        assert conn.execute(sa.text("SHOW statement_timeout")).scalar_one() == "5s"


# --- Helpers --------------------------------------------------------------------------


def _plan() -> ResearchPlan:
    """`ResearchPlan` refuses fewer than three subtopics, so this is the smallest valid one."""
    return ResearchPlan(
        subtopics=[
            Subtopic(id=f"s{n}", question=f"Question {n}?", search_query=f"query {n}")
            for n in (1, 2, 3)
        ],
        success_criteria=["Cites public sources"],
    )


def _report_citing(missing: str) -> Report:
    """A draft whose claim rests on a finding the database does not have.

    The Synthesizer cannot produce this against its own state - it fails the job instead - so
    the case is the database being behind the checkpoint. `sources` still names the finding
    that does exist, because `Report` refuses an empty one.
    """
    known = a_finding("f1")
    return Report(
        sections=[Section(id="sec1", heading="Cloud", body="Both firms grew.")],
        claims=[Claim(claim_id="c9", section_id="sec1", text="TCS grew.", finding_ids=[missing])],
        sources=[Source(url=known.url, title=known.title, finding_ids=["f1"])],
    )


def _column_type(engine: Engine, table: str, column: str) -> str:
    """The type PostgreSQL reports, in its own words."""
    with engine.connect() as conn:
        return str(
            conn.execute(
                sa.text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = :table AND column_name = :column"
                ),
                {"table": table, "column": column},
            ).scalar_one()
        )


def _all(engine: Engine, table: sa.Table) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(table)).all())


def _actions(engine: Engine, job_id: str) -> list[str]:
    return [str(row.action) for row in queries.read_audit_events(engine, job_id)]


def _status(engine: Engine, job_id: str) -> str:
    row = queries.read_job(engine, job_id)
    assert row is not None
    return str(row.status)


def _race(
    call: Callable[[], bool], *, engine: Engine, url: str, job: str, callers: int
) -> list[bool]:
    """Run `call` from `callers` threads with every one of them provably in flight at once.

    A separate connection takes `SELECT ... FOR UPDATE` on the job row first. Each caller then
    blocks inside its own transaction, and the holder does not let go until PostgreSQL itself
    reports that all of them are waiting on a lock - which is what makes this a race rather
    than two calls that happened to run one after the other. They are then released together.

    **The pool is warmed first, and that is not an optimisation.** Opening a connection costs
    tens of milliseconds and is done while holding the GIL for part of the time, so a caller
    that has to make one may not reach the row until long after its siblings have. Warming
    turns each caller's first act into a pooled checkout, which leaves the window below wide
    enough that it stops being a timing question.

    The hold is deliberately short. A blocked UPDATE is still a running statement, so it spends
    the engine's 5-second `statement_timeout`; three seconds for a state that is now reached in
    milliseconds leaves the margin wide and the failure mode loud.
    """
    _warm(engine, callers)
    holder = sa.create_engine(queries.sqlalchemy_url(url), poolclass=sa.pool.NullPool)
    try:
        with ThreadPoolExecutor(max_workers=callers) as pool:
            with holder.begin() as held:
                held.execute(
                    sa.select(jobs.c.status).where(jobs.c.job_id == job).with_for_update()
                ).one()
                futures = [pool.submit(call) for _ in range(callers)]
                _wait_until_all_blocked(held, futures=futures, waiters=callers)
            # The holder committed on the way out of that block, so the callers are moving now.
            return [bool(future.result(timeout=30)) for future in futures]
    finally:
        holder.dispose()


def _warm(engine: Engine, count: int) -> None:
    """Leave `count` open connections idle in the pool.

    They are opened together and handed back together, so the pool really holds `count` of
    them rather than one that was reused `count` times. `count` must not exceed the pool's
    size, or the surplus is discarded on return and the caller is back to connecting by hand.
    """
    pool = engine.pool
    # Only a QueuePool keeps connections after they are handed back, and it is what
    # `create_database_engine` gives PostgreSQL. Anything else would make the warming a no-op.
    assert isinstance(pool, sa.pool.QueuePool)
    assert count <= pool.size(), f"{count} callers is more than the pool holds"

    connections = [engine.connect() for _ in range(count)]
    for connection in connections:
        connection.close()


_ACTIVITY = sa.text(
    "SELECT pid, state, wait_event_type, wait_event, left(query, 50) AS query "
    "FROM pg_stat_activity WHERE datname = current_database()"
)


def _wait_until_all_blocked(
    conn: sa.Connection, *, futures: list[Future[bool]], waiters: int
) -> None:
    """Block until `waiters` sessions in this database are waiting on a lock.

    The first waiter queues on the row's `transactionid`; every later one queues behind it on
    a `tuple` lock. Both are `Lock`, which is why the type is what is counted.

    A caller that finished instead of blocking is reported as itself rather than as a missing
    waiter - "it raised this" is a far shorter path to the cause than "one of them is absent".
    """
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        for future in futures:
            if future.done() and future.exception() is not None:
                raise AssertionError(f"a caller failed instead of blocking: {future.exception()!r}")
        if sum(row.wait_event_type == "Lock" for row in conn.execute(_ACTIVITY)) >= waiters:
            return
        time.sleep(0.02)

    raise AssertionError(
        f"fewer than {waiters} callers reached the row lock within 3s:\n"
        + "\n".join(str(tuple(row)) for row in conn.execute(_ACTIVITY).all())
    )
