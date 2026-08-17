"""
WHY THIS FILE EXISTS
    Step 13 is the schema and the statements that use it, and both fail in ways that are
    expensive to discover later: a constraint that exists in database/schema.py but not in
    the migration is a green test suite and a broken deploy, and a write that is not keyed
    the way a replayed node needs it is a duplicate row nobody notices until the audit trail
    is read.

    Four groups, in the order the failures matter.

    **The migration is the schema.** Every test here runs against a database built by
    `alembic upgrade head`, and one test compares that result against database/schema.py and
    demands they be identical. That is what stops the two definitions from drifting.

    **The constraints are the specification, enforced.** A status outside the vocabulary, an
    audit row whose actor is `unknown`, a finding pointing at a job that does not exist, a
    `claim_sources` row pointing at a finding that does not exist, a duplicate
    `idempotency_key` - each is refused by the database rather than by a convention, because
    the ones enforced by convention are the ones that eventually are not.

    **The writes converge under replay.** A node can run twice: it writes its rows, the
    process dies before the checkpoint, and the redelivered message runs it again
    (ARCHITECTURE.md §11). Findings are therefore written by key and claims are replaced
    wholesale, and both are asserted by calling the function twice. `audit_events` is the
    deliberate exception - a node that genuinely ran twice truthfully has two rows.

    **A failed write leaves nothing behind.** Each write function is one transaction, so a
    statement that fails halfway takes the rest of it with it, including the audit row. An
    audit row that survives the write it describes would be a record of something that did
    not happen.

    SQLite is what these run on, and tests/dbharness.py says what that does and does not
    prove.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from dbharness import AUTOGENERATE_OPTS, a_finding, a_report, migrated_engine, new_job_id
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from database import queries
from database.schema import (
    CHECKPOINTER_TABLES,
    alembic_include_name,
    audit_events,
    findings,
    jobs,
    metadata,
)
from schemas import Claim, Report, ResearchPlan, Section, Source, Subtopic, Verdict

_ROOT = Path(__file__).resolve().parent.parent

_TABLES = ("jobs", "findings", "claims", "claim_sources", "audit_events")


_INDEXES: dict[str, set[tuple[str, tuple[str, ...]]]] = {
    "jobs": {
        ("ix_jobs_user_created", ("user_id", "created_at")),
        ("ix_jobs_status", ("status",)),
    },
    "findings": {("ix_findings_job_url", ("job_id", "url"))},
    "claims": {
        ("ix_claims_job", ("job_id",)),
        ("ix_claims_job_supported", ("job_id", "supported")),
    },
    "claim_sources": {("ix_claim_sources_finding", ("finding_id",))},
    "audit_events": {("ix_audit_events_job_created", ("job_id", "created_at"))},
}
"""ARCHITECTURE.md §9's indexes, one entry per line of that document.

`ix_jobs_user_created` is the one place this file differs from §9's text, which writes it
`(user_id, created_at desc)`: the direction is dropped deliberately and database/schema.py
says why.
"""


@pytest.fixture
def db(tmp_path: Path) -> Engine:
    """A migrated database of this test's own."""
    return migrated_engine(tmp_path)


@pytest.fixture
def job(db: Engine) -> str:
    """A job row every other row can hang off, created the way `POST /jobs` will."""
    job_id = new_job_id()
    queries.create_job(
        db,
        job_id=job_id,
        user_id=new_job_id(),
        question="Compare TCS and Infosys on cloud strategy.",
        idempotency_key="key-1",
        actor="submitter-7",
    )
    return job_id


# --- The migration ------------------------------------------------------------------


def test_the_migration_creates_the_five_application_tables(db: Engine) -> None:
    tables = set(sa.inspect(db).get_table_names())

    assert set(_TABLES) <= tables
    # The checkpointer owns its own tables through setup(); Alembic does not touch them
    # (guidelines §19). A migration that created them would be two owners for one schema.
    assert not {name for name in tables if name.startswith("checkpoint")}


def test_the_migration_and_the_schema_definition_agree(db: Engine) -> None:
    # The one test that stops database/schema.py and the migration from drifting apart. The
    # migration is a snapshot and must stay one, so it repeats the definitions rather than
    # importing them - which only works while something compares the two.
    #
    # `AUTOGENERATE_OPTS` rather than a bare `compare_type`, so this runs the options env.py
    # actually configures. On SQLite the difference is invisible - there are no checkpoint
    # tables to exclude - and running a different configuration from production is the exact
    # class of gap the safeguard below exists to close.
    with db.connect() as conn:
        context = MigrationContext.configure(conn, opts=AUTOGENERATE_OPTS)

        assert compare_metadata(context, metadata) == []


def test_the_migration_names_every_documented_index(db: Engine) -> None:
    inspector = sa.inspect(db)

    for table, expected in _INDEXES.items():
        found = {
            (index["name"], tuple(index["column_names"])) for index in inspector.get_indexes(table)
        }
        assert expected <= found, table


def test_the_migration_can_be_undone(tmp_path: Path) -> None:
    # A migration with no working downgrade is a one-way door, and the deploy in guidelines
    # §19 rolls back by redeploying the previous task definition against this schema.
    engine = migrated_engine(tmp_path)

    command.downgrade(_alembic(engine), "base")

    assert not set(_TABLES) & set(sa.inspect(engine).get_table_names())


def _alembic(engine: Engine) -> AlembicConfig:
    """Alembic pointed at this repository's migrations and at `engine`'s database."""
    config = AlembicConfig(_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(_ROOT / "database" / "migrations"))
    config.set_main_option("sqlalchemy.url", str(engine.url))
    return config


def test_rev_0002_widens_the_status_check_and_the_narrow_one_refuses_queued(
    tmp_path: Path,
) -> None:
    """ADR 0010 decision 1's ordering rule, on the dialect that recreates the table for it.

    SQLite has no `ALTER TABLE ... DROP CONSTRAINT`, so rev_0002 rebuilds `jobs` around the new
    CHECK - which is a whole-table operation on a revision that means to change one constraint.
    Stepping to 0001 and back is what shows both halves happened: the narrow constraint really
    refuses `queued`, and the wide one really accepts it.
    """
    engine = migrated_engine(tmp_path)
    command.downgrade(_alembic(engine), "0001")

    with pytest.raises(IntegrityError):
        _create_job(engine, "key-early")

    command.upgrade(_alembic(engine), "head")
    job_id = _create_job(engine, "key-later")

    row = queries.read_job(engine, job_id)
    assert row is not None and row.status == "queued"


def test_the_downgrade_moves_a_queued_job_to_running(tmp_path: Path) -> None:
    # A rollback must fail on schema or not at all, never on data (guidelines §19): narrowing
    # the constraint with a `queued` row still in the table would abort part-way through.
    engine = migrated_engine(tmp_path)
    job_id = _create_job(engine, "key-1")

    command.downgrade(_alembic(engine), "0001")

    row = queries.read_job(engine, job_id)
    assert row is not None and row.status == "running"


def test_the_status_check_survives_the_table_being_rebuilt(tmp_path: Path) -> None:
    """The hazard `copy_from` exists to avoid, checked rather than trusted.

    SQLite reflection cannot see a named CHECK constraint, so a recreate that reflected the
    table instead of restating it would drop both of them silently - and a job status outside
    the vocabulary would then be accepted with no test anywhere noticing.
    """
    engine = migrated_engine(tmp_path)
    job_id = _create_job(engine, "key-1")

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(sa.update(jobs).where(jobs.c.job_id == job_id).values(status="nearly_done"))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(sa.update(jobs).where(jobs.c.job_id == job_id).values(quality_flag="great"))


def _create_job(engine: Engine, key: str) -> str:
    job_id = new_job_id()
    queries.create_job(
        engine,
        job_id=job_id,
        user_id=new_job_id(),
        question="Compare TCS and Infosys on cloud strategy.",
        idempotency_key=key,
        actor="submitter-7",
    )
    return job_id


# --- The ownership boundary between Alembic and the checkpointer ---------------------
#
# Two things create tables in this database: Alembic owns the five in database/schema.py, and
# LangGraph's `PostgresSaver.setup()` owns four that `metadata` deliberately does not describe
# (guidelines §19). Autogenerate proposes dropping whatever it finds and `metadata` does not
# name, so the second owner's tables look exactly like abandoned ones to it.
#
# These run on SQLite with the four table names created by hand. That is enough, because what
# is under test is the name filter rather than anything PostgreSQL decides - and it means the
# safeguard is covered by the suite that needs no service running. The same property is
# asserted against a real `setup()` in tests/test_checkpointer_postgres.py.


def test_autogenerate_would_drop_the_checkpointer_tables_without_the_safeguard(
    db: Engine,
) -> None:
    """The failure mode itself, so the test below cannot pass by accident.

    Without this, a `checkpoint_tables` fixture that quietly created nothing would make every
    other test in this section green while proving nothing at all.
    """
    _pretend_a_worker_has_run(db)

    with db.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        dropped = _tables_proposed_for_removal(compare_metadata(context, metadata))

    assert dropped == set(CHECKPOINTER_TABLES)


def test_autogenerate_ignores_the_checkpointer_tables(db: Engine) -> None:
    """The safeguard: the same database, the options env.py configures, and no proposal."""
    _pretend_a_worker_has_run(db)

    with db.connect() as conn:
        context = MigrationContext.configure(conn, opts=AUTOGENERATE_OPTS)

        assert compare_metadata(context, metadata) == []


def test_autogenerate_still_reports_a_table_the_schema_no_longer_describes(db: Engine) -> None:
    """The safeguard is a deny-list, and this is what that buys.

    "Ignore everything `metadata` does not name" would be shorter and would also silence a
    table that was deleted from database/schema.py and still exists in the database - which is
    a migration somebody has to write, not noise.
    """
    _pretend_a_worker_has_run(db)
    with db.begin() as conn:
        conn.execute(sa.text("CREATE TABLE leftover_experiment (id TEXT PRIMARY KEY)"))

    with db.connect() as conn:
        context = MigrationContext.configure(conn, opts=AUTOGENERATE_OPTS)
        dropped = _tables_proposed_for_removal(compare_metadata(context, metadata))

    assert dropped == {"leftover_experiment"}


def test_autogenerate_still_reports_a_column_the_schema_does_not_have(db: Engine) -> None:
    """Application-table drift, with the checkpointer's tables present the whole time.

    A filter that excluded too much would pass every test above and quietly stop detecting the
    thing autogenerate is for.
    """
    _pretend_a_worker_has_run(db)
    with db.begin() as conn:
        conn.execute(sa.text("ALTER TABLE jobs ADD COLUMN scratch TEXT"))

    with db.connect() as conn:
        context = MigrationContext.configure(conn, opts=AUTOGENERATE_OPTS)
        differences = [str(difference) for difference in compare_metadata(context, metadata)]

    assert any("remove_column" in text and "scratch" in text for text in differences), differences


def test_the_safeguard_answers_for_tables_and_nothing_else() -> None:
    """It defends ownership of a table, not of a string. An index or a column that happens to
    share a name with one of the four is still compared, because nothing owns it but us."""
    parents: dict[Any, Any] = {}

    assert alembic_include_name("checkpoints", "table", parents) is False
    assert alembic_include_name("jobs", "table", parents) is True
    assert alembic_include_name("checkpoints", "index", parents) is True
    assert alembic_include_name("checkpoints", "column", parents) is True
    # A schema name is passed as None, and refusing it would exclude the default schema.
    assert alembic_include_name(None, "schema", parents) is True


def _pretend_a_worker_has_run(db: Engine) -> None:
    """The four tables `PostgresSaver.setup()` creates, as bare names.

    Their columns are irrelevant: the safeguard filters on the table name, and a faithful copy
    of LangGraph's DDL here would be a second definition of something this repository does not
    own. `tests/test_checkpointer_postgres.py` runs the real `setup()`.
    """
    with db.begin() as conn:
        for table in sorted(CHECKPOINTER_TABLES):
            conn.execute(sa.text(f"CREATE TABLE {table} (thread_id TEXT PRIMARY KEY)"))


def _tables_proposed_for_removal(differences: list[Any]) -> set[str]:
    return {
        difference[1].name
        for difference in differences
        if isinstance(difference, tuple) and difference[0] == "remove_table"
    }


# --- The constraints ----------------------------------------------------------------


def test_a_job_status_outside_the_vocabulary_is_refused(db: Engine, job: str) -> None:
    # schemas.py's JobStatus, enforced by the database rather than by convention: the state,
    # the API, and the rows cannot drift apart on what a status is allowed to be.
    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(sa.update(jobs).where(jobs.c.job_id == job).values(status="nearly_done"))


def test_a_quality_flag_outside_the_vocabulary_is_refused(db: Engine, job: str) -> None:
    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(sa.update(jobs).where(jobs.c.job_id == job).values(quality_flag="good"))


def test_no_quality_flag_is_a_value_the_column_allows(db: Engine, job: str) -> None:
    # None means the rubric ran and the report passed. It is not the same as "unscored", and
    # the column has to be able to say so.
    with db.begin() as conn:
        conn.execute(sa.update(jobs).where(jobs.c.job_id == job).values(quality_flag=None))

    assert queries.read_job(db, job) is not None


def test_a_duplicate_idempotency_key_is_refused(db: Engine, job: str) -> None:
    # The database is the arbiter of a duplicate submission, which is what `POST /jobs` turns
    # into a 409 carrying the existing job_id (ARCHITECTURE.md §9).
    with pytest.raises(IntegrityError):
        queries.create_job(
            db,
            job_id=new_job_id(),
            user_id=new_job_id(),
            question="Compare TCS and Infosys on cloud strategy.",
            idempotency_key="key-1",
            actor="submitter-7",
        )


def test_a_finding_cannot_belong_to_a_job_that_does_not_exist(db: Engine) -> None:
    with pytest.raises(IntegrityError):
        queries.record_research(
            db,
            job_id=new_job_id(),
            new_findings=[a_finding()],
            subtopic="s1",
            status="done",
        )


def test_two_jobs_may_each_have_their_own_f1(db: Engine, job: str) -> None:
    # ADR 0003: finding ids are a per-job sequence, so every job has an f1 and the primary
    # key has to be the pair. A global key on the column would collide on the second job.
    other = new_job_id()
    queries.create_job(
        db,
        job_id=other,
        user_id=new_job_id(),
        question="Another question.",
        idempotency_key="key-2",
        actor="submitter-7",
    )

    for job_id in (job, other):
        queries.record_research(
            db, job_id=job_id, new_findings=[a_finding("f1")], subtopic="s1", status="done"
        )

    assert len(queries.read_findings(db, job)) == 1
    assert len(queries.read_findings(db, other)) == 1


def test_the_same_finding_id_cannot_be_inserted_twice_in_one_job(db: Engine, job: str) -> None:
    # The key is what makes the replay behaviour in database/queries.py possible: a blind
    # second insert is refused, so the write has to be the one that converges.
    written = a_finding("f1")
    queries.record_research(db, job_id=job, new_findings=[written], subtopic="s1", status="done")

    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(
            sa.insert(findings).values(
                job_id=job,
                finding_id=written.finding_id,
                subtopic=written.subtopic_id,
                claim=written.claim,
                evidence=written.evidence,
                url=str(written.url),
                title=written.title,
                retrieved_at=written.retrieved_at,
                content_hash=written.content_hash,
                truncated=written.truncated,
            )
        )


def test_a_claim_source_cannot_point_at_a_finding_that_does_not_exist(db: Engine, job: str) -> None:
    # The composite foreign key is what makes "which URL supports this sentence?" a query
    # rather than an investigation - and a row pointing at nothing would answer it wrongly.
    queries.record_research(
        db, job_id=job, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )

    with pytest.raises(IntegrityError):
        queries.record_claims(db, job_id=job, report=_report_citing("f2"))


def test_an_audit_row_can_never_say_the_actor_was_unknown(db: Engine, job: str) -> None:
    # "An approval row with actor = 'unknown' is a bug, not a tolerable gap" (guidelines §9).
    for actor in ("unknown", "  "):
        with pytest.raises(IntegrityError), db.begin() as conn:
            conn.execute(
                sa.insert(audit_events).values(
                    job_id=job, actor=actor, action="gate_opened", detail={}
                )
            )


def test_an_audit_action_outside_the_vocabulary_is_refused(db: Engine, job: str) -> None:
    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(
            sa.insert(audit_events).values(
                job_id=job, actor="system", action="looked_at_it", detail={}
            )
        )


def test_deleting_a_job_takes_its_findings_claims_and_audit_trail_with_it(
    db: Engine, job: str
) -> None:
    # Retention deletes a job; nothing of it may be left pointing at a row that is gone
    # (guidelines §9). The sweep itself is Phase 5 - this is the cascade it will rely on.
    queries.record_research(
        db, job_id=job, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )
    queries.record_claims(db, job_id=job, report=a_report(("c1", ["f1"])))

    with db.begin() as conn:
        conn.execute(sa.delete(jobs).where(jobs.c.job_id == job))

    assert queries.read_findings(db, job) == []
    assert queries.read_claims(db, job) == []
    assert queries.read_claim_sources(db, job) == []
    assert queries.read_audit_events(db, job) == []


# --- What each write records --------------------------------------------------------


def test_create_job_records_the_submitter_not_the_system(db: Engine, job: str) -> None:
    # `system` is for transitions the graph made on its own. Creating a job is something a
    # caller did, and the row has to say which one (guidelines §9).
    row = queries.read_job(db, job)
    events = queries.read_audit_events(db, job)

    assert row is not None
    # `queued`, not `running`: nothing is running it until the worker receives its message
    # (ADR 0010 decisions 1 and 2).
    assert row.status == "queued"
    assert row.report_json is None and row.exported_at is None and row.completed_at is None
    assert [(event.actor, event.action) for event in events] == [("submitter-7", "job_created")]


def test_a_researcher_visit_writes_its_findings_and_says_how_the_subtopic_ended(
    db: Engine, job: str
) -> None:
    queries.record_research(
        db,
        job_id=job,
        new_findings=[a_finding("f1"), a_finding("f2", subtopic_id="s1")],
        subtopic="s1",
        status="done",
    )

    rows = queries.read_findings(db, job)
    event = queries.read_audit_events(db, job)[-1]

    assert [row.finding_id for row in rows] == ["f1", "f2"]
    assert rows[0].evidence == a_finding("f1").evidence  # the verbatim quote, not a summary
    assert rows[0].url == str(a_finding("f1").url)
    assert rows[0].subtopic == "s1" and rows[0].truncated is False
    assert event.actor == "system"
    assert event.action == "subtopic_researched"
    assert event.detail == {"subtopic": "s1", "status": "done", "findings": 2}


def test_a_subtopic_that_produced_nothing_is_recorded_as_unresearched(db: Engine, job: str) -> None:
    # Zero findings is a normal outcome, and the gap has to be visible afterwards rather than
    # looking like a subtopic nobody researched.
    queries.record_research(db, job_id=job, new_findings=[], subtopic="s2", status="unresearched")

    event = queries.read_audit_events(db, job)[-1]

    assert event.detail == {"subtopic": "s2", "status": "unresearched", "findings": 0}
    assert queries.read_findings(db, job) == []


def test_claims_and_their_sources_are_written_from_the_draft(db: Engine, job: str) -> None:
    findings_written = [a_finding("f1"), a_finding("f2")]
    queries.record_research(
        db, job_id=job, new_findings=findings_written, subtopic="s1", status="done"
    )

    queries.record_claims(
        db,
        job_id=job,
        report=a_report(("c1", ["f1", "f2"]), ("c2", ["f2"]), findings=findings_written),
    )

    written = {row.claim_id: row for row in queries.read_claims(db, job)}
    sources = {(row.claim_id, row.finding_id) for row in queries.read_claim_sources(db, job)}

    assert set(written) == {"c1", "c2"}
    assert written["c1"].section == "sec1"
    # Not checked yet: the verdict is the Fact-Checker's, and this draft has not seen it.
    assert written["c1"].supported is None and written["c1"].verdict_note is None
    assert sources == {("c1", "f1"), ("c1", "f2"), ("c2", "f2")}


def test_a_verdict_lands_on_its_claim(db: Engine, job: str) -> None:
    _drafted(db, job)

    queries.record_verdicts(
        db,
        job_id=job,
        verdicts=[
            Verdict(claim_id="c1", supported=True, quote="q", note="stated"),
            Verdict(claim_id="c2", supported=False, note="source unreachable"),
        ],
    )

    written = {row.claim_id: row for row in queries.read_claims(db, job)}
    assert (written["c1"].supported, written["c1"].verdict_note) == (True, "stated")
    assert (written["c2"].supported, written["c2"].verdict_note) == (False, "source unreachable")


def test_a_verdict_for_a_claim_the_database_does_not_have_is_logged(
    db: Engine, job: str, caplog: pytest.LogCaptureFixture
) -> None:
    # A mirror mismatch is worth seeing and is not worth discarding a finished report over.
    _drafted(db, job)

    with caplog.at_level(logging.WARNING):
        queries.record_verdicts(
            db, job_id=job, verdicts=[Verdict(claim_id="c9", supported=False, note="gone")]
        )

    assert "c9" in caplog.text


def test_the_export_result_and_the_report_body_are_written_together(db: Engine, job: str) -> None:
    # jobs.report_json is where Phase 2 keeps the approved body, and the audit row and the
    # body must not be able to disagree about whether an export happened.
    _drafted(db, job)
    report = a_report(("c1", ["f1"]))

    queries.record_export_result(db, job_id=job, report=report, uncited=())

    row = queries.read_job(db, job)
    event = queries.read_audit_events(db, job)[-1]
    assert row is not None
    assert row.report_json == report.model_dump(mode="json")
    assert row.exported_at is not None
    assert (event.actor, event.action) == ("system", "export_result")
    assert event.detail == {"result": "exported", "claims": 1}


def test_a_blocked_export_stores_no_report_and_names_the_uncited_claims(
    db: Engine, job: str
) -> None:
    _drafted(db, job)

    queries.record_export_result(db, job_id=job, report=None, uncited=["c2"])

    row = queries.read_job(db, job)
    event = queries.read_audit_events(db, job)[-1]
    assert row is not None
    assert row.report_json is None and row.exported_at is None
    assert event.detail == {"result": "blocked", "uncited_claims": ["c2"]}


def test_finishing_a_job_stamps_the_outcome_the_counters_and_completed_at(
    db: Engine, job: str
) -> None:
    # The counters are persisted so budget behaviour stays auditable once state is gone, and
    # completed_at is set here and nowhere else (ARCHITECTURE.md §9).
    queries.finish_job(
        db,
        job_id=job,
        status="approved",
        quality_flag="below_threshold",
        revision_count=2,
        llm_calls_used=41,
    )

    row = queries.read_job(db, job)
    assert row is not None
    assert row.status == "approved"
    assert row.quality_flag == "below_threshold"
    assert (row.revision_count, row.llm_calls_used) == (2, 41)
    assert row.completed_at is not None


def test_the_audit_trail_reads_back_as_a_timeline(db: Engine, job: str) -> None:
    queries.record_plan(db, job_id=job, plan=_plan())
    queries.record_research(
        db, job_id=job, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )
    queries.record_revision(
        db,
        job_id=job,
        revision=1,
        route="researcher",
        failed_dimensions=["research_completeness"],
        weighted_score=3.1,
    )
    queries.record_reflection_failed(db, job_id=job)
    queries.record_export_attempt(db, job_id=job, claims_checked=2)

    events = queries.read_audit_events(db, job)

    assert [event.action for event in events] == [
        "job_created",
        "plan_produced",
        "subtopic_researched",
        "revision",
        "reflection_failed",
        "export_attempted",
    ]
    assert events[1].detail == {"subtopics": ["s1", "s2", "s3"]}
    assert events[3].detail == {
        "revision": 1,
        "route": "researcher",
        "failed_dimensions": ["research_completeness"],
        "weighted_score": 3.1,
    }
    assert events[4].detail == {"quality_flag": "unscored"}


# --- Replay, and what a failed write leaves behind ----------------------------------


def test_replaying_a_researcher_visit_writes_no_duplicate_findings(db: Engine, job: str) -> None:
    # The node writes its rows and the checkpoint is written after it returns, so a crash in
    # between means the redelivered message runs the same node again (ARCHITECTURE.md §11).
    visit = [a_finding("f1"), a_finding("f2")]

    queries.record_research(db, job_id=job, new_findings=visit, subtopic="s1", status="done")
    queries.record_research(db, job_id=job, new_findings=visit, subtopic="s1", status="done")

    assert [row.finding_id for row in queries.read_findings(db, job)] == ["f1", "f2"]
    # The audit trail is the deliberate exception: the node really did run twice.
    events = queries.read_audit_events(db, job)
    researched = [e for e in events if e.action == "subtopic_researched"]
    assert len(researched) == 2


def test_a_replayed_finding_refreshes_the_row_it_already_wrote(db: Engine, job: str) -> None:
    # Finding ids are numbered from what state already holds, so a replayed node mints the
    # same ids again - and the row has to end up agreeing with the checkpoint rather than
    # keeping the abandoned attempt's text under a live id.
    queries.record_research(
        db, job_id=job, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )
    second = a_finding("f1", url="https://example.com/other").model_copy(
        update={"evidence": "A different quote, from the attempt that survived."}
    )

    queries.record_research(db, job_id=job, new_findings=[second], subtopic="s1", status="done")

    rows = queries.read_findings(db, job)
    assert len(rows) == 1
    assert rows[0].evidence == "A different quote, from the attempt that survived."
    assert rows[0].url == "https://example.com/other"


def test_a_second_draft_replaces_the_first_ones_claims(db: Engine, job: str) -> None:
    # One draft exists at a time, so "this job's claims" must answer with the claims that are
    # in the report - not with two revisions' worth.
    _drafted(db, job)

    queries.record_claims(db, job_id=job, report=a_report(("c3", ["f1"])))

    assert [row.claim_id for row in queries.read_claims(db, job)] == ["c3"]
    assert {row.claim_id for row in queries.read_claim_sources(db, job)} == {"c3"}


def test_a_write_that_fails_halfway_leaves_neither_half(db: Engine) -> None:
    # Each write function is one transaction. An audit row that survived the write it
    # describes would be a record of something that did not happen - so a finding whose job
    # does not exist must take its own audit row down with it.
    missing = new_job_id()

    with pytest.raises(IntegrityError):
        queries.record_research(
            db, job_id=missing, new_findings=[a_finding("f1")], subtopic="s1", status="done"
        )

    assert queries.read_audit_events(db, missing) == []


def test_a_draft_whose_citation_is_broken_leaves_the_previous_claims_intact(
    db: Engine, job: str
) -> None:
    # record_claims deletes the old claim set before inserting the new one. If the insert
    # fails, the delete has to go with it, or a bad draft would erase a good one.
    _drafted(db, job)

    with pytest.raises(IntegrityError):
        queries.record_claims(db, job_id=job, report=_report_citing("f7"))

    assert {row.claim_id for row in queries.read_claims(db, job)} == {"c1", "c2"}


# --- Helpers ------------------------------------------------------------------------


def _plan() -> ResearchPlan:
    return ResearchPlan(
        subtopics=[
            Subtopic(id=f"s{n}", question=f"Question {n}?", search_query=f"query {n}")
            for n in (1, 2, 3)
        ],
        success_criteria=["Cites public sources"],
    )


def _report_citing(missing: str) -> Report:
    """A draft whose claim rests on a finding the database does not have.

    The Synthesizer cannot produce this against its own state - it fails the job instead -
    so the case here is the database being behind the checkpoint. The foreign key has to
    hold either way, which is the point of putting it in the database rather than in a rule.
    """
    known = a_finding("f1")
    return Report(
        sections=[Section(id="sec1", heading="Cloud", body="Both firms grew.")],
        claims=[Claim(claim_id="c9", section_id="sec1", text="TCS grew.", finding_ids=[missing])],
        sources=[Source(url=known.url, title=known.title, finding_ids=["f1"])],
    )


def _drafted(db: Engine, job: str) -> None:
    """A job with one finding and a two-claim draft written against it."""
    queries.record_research(
        db, job_id=job, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )
    queries.record_claims(db, job_id=job, report=a_report(("c1", ["f1"]), ("c2", ["f1"])))


# --- Counting reviewer edits (ADR 0006) ---------------------------------------------


def test_reviewer_edits_are_counted_from_the_audit_trail(db: Engine, job: str) -> None:
    """The bound's input, read from the trail rather than from a column of its own.

    The rows themselves are written by the authenticated endpoint in step 18, so they are
    inserted directly here: what is under test is the query the endpoint will call before it
    decides whether a fourth edit may start.
    """
    assert queries.count_reviewer_edits(db, job) == 0

    for decision in ("edit", "edit", "reject"):
        with db.begin() as conn:
            conn.execute(
                sa.insert(audit_events).values(
                    job_id=job,
                    actor="reviewer-3",
                    action="reviewer_decision",
                    detail={"decision": decision, "note": None},
                )
            )

    assert queries.count_reviewer_edits(db, job) == 2  # the rejection is not an edit


def test_a_replayed_gate_opening_does_not_reopen_a_claimed_gate(db: Engine, job: str) -> None:
    """ADR 0007 root cause 1, at the statement that had it.

    The gate node runs twice per visit and `claim_gate` runs between the two, so the second
    execution used to hand an answered gate straight back to `awaiting_approval` - defeating
    the conditional update that exists to let exactly one reviewer resume the thread.
    """
    queries.record_gate_opened(db, job_id=job, calls_used=16, summary={"quality_flag": None})
    assert _status(db, job) == "awaiting_approval"

    assert queries.claim_gate(db, job) is True
    queries.record_gate_opened(db, job_id=job, calls_used=16, summary={"quality_flag": None})

    assert _status(db, job) == "running"  # the claim survives the replay
    assert len(_events(db, job, "gate_opened")) == 1  # and still one row for one visit


def test_the_next_gate_visit_opens_the_gate_again(db: Engine, job: str) -> None:
    # The guard is keyed, not a latch: a higher call count is a different visit, so it opens
    # the gate and writes its own row.
    queries.record_gate_opened(db, job_id=job, calls_used=16, summary={})
    queries.claim_gate(db, job)

    queries.record_gate_opened(db, job_id=job, calls_used=21, summary={})

    assert _status(db, job) == "awaiting_approval"
    assert [event.detail["calls_used"] for event in _events(db, job, "gate_opened")] == [16, 21]


def test_one_gate_visit_records_one_reviewer_decision(db: Engine, job: str) -> None:
    """ADR 0007 invariant 1, at the statement that enforces it.

    The endpoint's retry path already avoids the second call; this is the durable guarantee
    underneath it, because `count_reviewer_edits` counts rows and a duplicate would spend a
    reviewer's edit on an edit that never happened.
    """
    for _ in range(2):
        queries.record_reviewer_decision(
            db, job_id=job, actor="reviewer-3", decision="edit", note=None, edits="a", calls_used=16
        )

    rows = _events(db, job, "reviewer_decision")
    assert len(rows) == 1
    assert rows[0].detail == {"decision": "edit", "note": None, "edits": "a", "calls_used": 16}
    assert queries.count_reviewer_edits(db, job) == 1


def test_a_later_gate_visit_takes_its_own_decision(db: Engine, job: str) -> None:
    # Two visits, two decisions. The key is what tells them apart, so the second one is not
    # mistaken for a retry of the first.
    queries.record_reviewer_decision(
        db, job_id=job, actor="reviewer-3", decision="edit", note=None, edits="a", calls_used=16
    )
    queries.record_reviewer_decision(
        db, job_id=job, actor="reviewer-3", decision="approve", note=None, edits=None, calls_used=21
    )

    assert [row.detail["decision"] for row in _events(db, job, "reviewer_decision")] == [
        "edit",
        "approve",
    ]


def test_reading_a_gate_visits_decision_answers_only_for_that_visit(db: Engine, job: str) -> None:
    queries.record_reviewer_decision(
        db, job_id=job, actor="reviewer-3", decision="edit", note=None, edits="a", calls_used=16
    )

    # The whole decision, not just its verb: the worker rebuilds a `GateDecision` from this
    # rather than reading the reviewer's text out of a queue message (ADR 0011 decision 2).
    assert queries.read_gate_decision(db, job, calls_used=16) == {
        "decision": "edit",
        "note": None,
        "edits": "a",
        "calls_used": 16,
    }
    assert queries.read_gate_decision(db, job, calls_used=21) is None


def test_one_job_s_decisions_are_not_read_as_anothers(db: Engine, job: str) -> None:
    # The key is `(job_id, calls_used)`, and two jobs pause at the same call count routinely.
    other = _second_job(db)
    queries.record_reviewer_decision(
        db,
        job_id=other,
        actor="reviewer-3",
        decision="reject",
        note=None,
        edits=None,
        calls_used=16,
    )

    assert queries.read_gate_decision(db, job, calls_used=16) is None
    theirs = queries.read_gate_decision(db, other, calls_used=16)
    assert theirs is not None and theirs["decision"] == "reject"


def test_setting_a_status_leaves_a_finished_job_alone(db: Engine, job: str) -> None:
    """`finalize` stays authoritative about how a job ended (ADR 0007).

    The endpoint reconciles `jobs.status` from the checkpoint after every gate decision, and
    an approved job must not be walked back to `running` by that reconcile.
    """
    queries.set_job_status(db, job_id=job, status="awaiting_approval")
    assert _status(db, job) == "awaiting_approval"

    queries.finish_job(
        db, job_id=job, status="approved", quality_flag=None, revision_count=0, llm_calls_used=17
    )
    queries.set_job_status(db, job_id=job, status="running")

    assert _status(db, job) == "approved"


def _status(db: Engine, job_id: str) -> str:
    row = queries.read_job(db, job_id)
    assert row is not None
    return str(row.status)


def _events(db: Engine, job_id: str, action: str) -> list[sa.Row[Any]]:
    return [event for event in queries.read_audit_events(db, job_id) if event.action == action]


def _second_job(db: Engine) -> str:
    job_id = new_job_id()
    queries.create_job(
        db,
        job_id=job_id,
        user_id=new_job_id(),
        question="Another question.",
        idempotency_key="key-second",
        actor="submitter-7",
    )
    return job_id


def test_one_job_s_edits_are_not_counted_against_another(db: Engine, job: str) -> None:
    other = new_job_id()
    queries.create_job(
        db,
        job_id=other,
        user_id=new_job_id(),
        question="Another question.",
        idempotency_key="key-other",
        actor="submitter-7",
    )
    with db.begin() as conn:
        conn.execute(
            sa.insert(audit_events).values(
                job_id=other,
                actor="reviewer-3",
                action="reviewer_decision",
                detail={"decision": "edit"},
            )
        )

    assert queries.count_reviewer_edits(db, job) == 0
    assert queries.count_reviewer_edits(db, other) == 1
