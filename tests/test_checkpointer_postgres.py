"""
WHY THIS FILE EXISTS
    The Postgres checkpointer is the reason the human gate works. `interrupt()` can hold a job
    for days, and in-memory state dies with the worker - so without a durable checkpoint an
    approval would re-run the whole research pipeline and re-bill every LLM call already spent
    (guidelines §4, §10). Every test in the offline suite that exercises that runs on
    `InMemorySaver`, which is the one thing it cannot prove: the state never leaves the process,
    so nothing about serialisation, `setup()`, or a restart is under test there.

    Four questions, in the order they would fail a deploy.

    **Does `setup()` work, and does it work twice?** LangGraph creates and migrates its own
    tables, Alembic never touches them, and a worker calls `setup()` at startup rather than
    guessing (graph/build.py). Every restart therefore runs it again.

    **Do the two owners coexist?** The five application tables and the checkpointer's tables
    live in one database with two different things creating them. A migration that clobbered
    the checkpointer, or a `setup()` that clobbered the audit trail, is a phase-3 outage.

    **Does the state survive leaving the process?** Not a new saver in the same interpreter -
    a different process, which is the only version of the question a restart asks. And it has
    to come back as the state models rather than as dictionaries, because `state_serde()`
    allows exactly those types and nothing else this process happens to import (guidelines §16).

    **Do the resume semantics still hold?** A job paused at the gate by one process, approved
    by another, exports - and does not re-plan or re-research on the way, which is the entire
    economic argument for the checkpointer.

WHO CALLS IT
    `pytest -m postgres`, against the PostgreSQL 16 in docker-compose.yml. No network calls:
    the LLM and the web are the same fakes the offline suite uses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata, produce_migrations
from alembic.migration import MigrationContext
from dbharness import AUTOGENERATE_OPTS, new_job_id
from fakes import FakeQueue
from fastapi import FastAPI
from fastapi.testclient import TestClient
from harness import (
    Answer,
    FakeLLM,
    Page,
    RecordedWeb,
    decision,
    draft,
    plan,
    quote_the_page,
    rubric,
    verdict_batch,
)
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from openai import OpenAI
from pgharness import URL_VARIABLE, empty_database, migrated_engine, postgres_url, upgrade_to_head
from sqlalchemy.engine import Engine

from app import _checkpoint_reader, create_application
from config import load_config
from database import queries
from database.queries import create_database_engine
from database.schema import CHECKPOINTER_TABLES, metadata
from graph.build import ResearchGraph, build_graph, postgres_checkpointer
from graph.state import ResearchState, new_state, run_config
from jobqueue import JobQueue
from llm_client import LLMClient
from routes.auth import Identity, hash_key
from schemas import Finding, Report, ResearchPlan, Verdict

pytestmark = pytest.mark.postgres

_ROOT = Path(__file__).resolve().parent.parent

_APPLICATION_TABLES = ("jobs", "findings", "claims", "claim_sources", "audit_events")

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_FAST_MODEL": "fast-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
}

_REVIEWER_ID = "11111111-1111-4111-8111-111111111111"
"""`jobs.user_id` is a UUID column, so the identity behind an API key has to be one."""

_QUESTION = "Compare TCS and Infosys on cloud strategy."
_SUBTOPICS = (
    "What is TCS cloud revenue?",
    "What is Infosys cloud revenue?",
    "How do their cloud partnerships compare?",
)

_ROUTE_TO_THE_GATE = [
    decision("planner"),
    *[decision("researcher")] * 3,
    decision("synthesizer"),
    decision("fact_checker"),
]


# --- Fixtures -------------------------------------------------------------------------


@pytest.fixture
def empty_pg() -> str:
    return empty_database()


@pytest.fixture
def pg() -> Engine:
    """A migrated database, so the checkpointer's tables land beside the application's."""
    return migrated_engine()


@pytest.fixture
def pg_url() -> str:
    return postgres_url()


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> RecordedWeb:
    recorded = RecordedWeb()
    for index, question in enumerate(_SUBTOPICS, 1):
        recorded.index(question, _page(f"{index}a"), _page(f"{index}b"))
    recorded.install(monkeypatch)
    return recorded


@pytest.fixture
def job(pg: Engine) -> str:
    """The job row `POST /jobs` inserts. Everything the graph writes hangs off it."""
    job_id = new_job_id()
    queries.create_job(
        pg,
        job_id=job_id,
        user_id=new_job_id(),
        question=_QUESTION,
        idempotency_key="key-1",
        actor="submitter-7",
    )
    return job_id


# --- setup() ---------------------------------------------------------------------------


def test_setup_creates_the_checkpointer_tables_on_an_empty_database(empty_pg: str) -> None:
    """The first worker to start against a fresh database. Nothing else creates these."""
    with postgres_checkpointer(empty_pg):
        pass

    assert set(CHECKPOINTER_TABLES) <= _tables(empty_pg)


def test_setup_is_safe_to_run_again(empty_pg: str) -> None:
    """A worker calls `setup()` at startup rather than guessing, so every restart runs it
    again - and it is also how the checkpointer applies its own migrations."""
    for _ in range(3):
        with postgres_checkpointer(empty_pg):
            pass

    assert set(CHECKPOINTER_TABLES) <= _tables(empty_pg)


def test_setup_does_not_disturb_the_application_tables(pg: Engine, pg_url: str) -> None:
    """Two owners, one database. Alembic owns the five application tables and the checkpointer
    owns its own (guidelines §19), and neither may touch the other's.

    The comparison is unrestricted and expects nothing at all. It used to have to filter its
    own results down to the five application tables, because `metadata` does not describe the
    checkpointer's and autogenerate proposed dropping them; `alembic_include_name` is what
    removed the need, and asserting the empty list is what proves it did.
    """
    with postgres_checkpointer(pg_url):
        pass

    with pg.connect() as conn:
        context = MigrationContext.configure(conn, opts=AUTOGENERATE_OPTS)

        assert compare_metadata(context, metadata) == []

    assert set(_APPLICATION_TABLES) <= _tables(pg_url)


def test_autogenerate_proposes_no_drop_against_a_database_a_worker_has_used(
    pg: Engine, pg_url: str
) -> None:
    """What `alembic revision --autogenerate` would actually write into a revision file.

    `compare_metadata` above answers in diffs; this answers in operations, which is the form
    that reaches a migration script and then a production database. Measured before the
    safeguard: four `DropTableOp` and three `remove_index`, against the state every paused job
    resumes from.
    """
    with postgres_checkpointer(pg_url):
        pass

    with pg.connect() as conn:
        context = MigrationContext.configure(conn, opts=AUTOGENERATE_OPTS)
        upgrade_ops = produce_migrations(context, metadata).upgrade_ops

    assert upgrade_ops is not None
    assert [diff for diff in upgrade_ops.as_diffs() if "remove" in str(diff[0])] == []
    assert upgrade_ops.is_empty()


def test_setup_creates_no_table_the_safeguard_does_not_name(empty_pg: str) -> None:
    """The staleness alarm for `CHECKPOINTER_TABLES`.

    The safeguard names four tables rather than excluding everything `metadata` does not
    describe, which keeps a genuinely abandoned application table detectable - and costs this:
    a LangGraph release that adds a fifth checkpoint table would fall outside the deny-list and
    autogenerate would offer to drop it again.

    So the real `setup()` is asked what it creates, and the answer has to be exactly the four.
    Equality, not containment: containment is what the test above it asserts, and it is
    precisely the half that cannot notice a new table.
    """
    with postgres_checkpointer(empty_pg):
        pass

    assert _tables(empty_pg) == set(CHECKPOINTER_TABLES)


def test_the_migration_can_run_after_a_worker_has_already_started(empty_pg: str) -> None:
    """The deploy order in reverse. `alembic upgrade head` runs as its own task before the new
    revision starts (guidelines §19), but a rollback or a re-run can meet a database whose
    checkpointer tables are already there, and the migration must not care."""
    with postgres_checkpointer(empty_pg):
        pass

    upgrade_to_head(empty_pg)

    tables = _tables(empty_pg)
    assert set(_APPLICATION_TABLES) <= tables
    assert set(CHECKPOINTER_TABLES) <= tables


# --- The window between the migration and the first worker ------------------------------
#
# `migrate` exits 0 and `api` starts; the checkpointer's tables arrive later, when a worker
# calls `setup()`. A 2026-08-18 review found the API reporting healthy in that window and
# answering `500` where the contract promises `409`. Both are checked here against the real
# thing, because the failure is a PostgreSQL error class - `UndefinedTable` - and a fake can
# only imitate it.


def test_health_reports_the_missing_checkpoint_tables_and_then_recovers(empty_pg: str) -> None:
    """False readiness, and the only cure that keeps ADR 0012 intact.

    The API may not create these tables: Alembic does not own them (guidelines §19) and this
    process never calls `setup()` (ADR 0012 decisions 1 and 3). So the honest answer while they
    are absent is `degraded` - a deployment in which no job can run - and the recovery is a
    worker starting, not anything the API does.
    """
    upgrade_to_head(empty_pg)
    assert not set(CHECKPOINTER_TABLES) <= _tables(empty_pg)

    with TestClient(_api_against(empty_pg)) as client:
        before = client.get("/health")

        assert before.status_code == 503
        assert before.json()["checks"] == {"db": True, "redis": True, "checkpoints": False}
        # Reading did not repair it, which is the ADR 0012 half of the claim.
        assert not set(CHECKPOINTER_TABLES) <= _tables(empty_pg)

    with postgres_checkpointer(empty_pg):  # a worker starts
        pass

    with TestClient(_api_against(empty_pg)) as client:
        recovered = client.get("/health")

    assert recovered.status_code == 200
    assert recovered.json()["checks"]["checkpoints"] is True


def test_deciding_before_any_worker_has_started_is_a_conflict_not_a_500(empty_pg: str) -> None:
    """The ordering fix, against the real `UndefinedTable`.

    `POST /jobs/{id}/approve` read the gate-visit key from the checkpointer before checking the
    job's status, so a `queued` job on this database raised inside `_gate_visit` and the
    reviewer got `500 internal_error` where guidelines §12 promises `409`. A queued job has
    never been invoked - no checkpoint, no possible decision - so refusing it first is both
    correct and sufficient.
    """
    upgrade_to_head(empty_pg)
    engine = create_database_engine(empty_pg)
    job_id = new_job_id()
    queries.create_job(
        engine,
        job_id=job_id,
        user_id=_REVIEWER_ID,
        question=_QUESTION,
        idempotency_key=f"key-{job_id}",
        actor=_REVIEWER_ID,
    )

    with TestClient(_api_against(empty_pg, engine=engine)) as client:
        response = client.post(
            f"/jobs/{job_id}/approve",
            json={"decision": "approve"},
            headers={"Authorization": "Bearer reviewer-key"},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_awaiting_approval"
    row = queries.read_job(engine, job_id)
    assert row is not None and row.status == "queued"  # nothing was claimed or recorded


def _api_against(url: str, *, engine: Engine | None = None) -> FastAPI:
    """The real application, wired to a real `PostgresSaver` **that nobody has `setup()`**.

    `app._checkpoint_reader` is the production wiring and is used unchanged, because the point
    is what the deployed reader does against this database rather than what a fake would do.
    """
    checkpoints, _pool = _checkpoint_reader(url)
    return create_application(
        config=load_config(_ENV),
        engine=engine if engine is not None else create_database_engine(url),
        checkpoints=checkpoints,
        queue=cast(JobQueue, FakeQueue()),
        # The table holds hashes, never keys (guidelines §16).
        keys={hash_key("reviewer-key"): Identity(user_id=_REVIEWER_ID, role="reviewer")},
    )


# --- What the checkpoint holds ----------------------------------------------------------


def test_a_paused_job_is_readable_through_a_second_saver(
    pg: Engine, pg_url: str, job: str, web: RecordedWeb
) -> None:
    """The same interpreter, a different pool and a different saver: the first thing a restart
    is, minus the process boundary."""
    _run_to_the_gate(pg, pg_url, job)

    with postgres_checkpointer(pg_url) as reader:
        checkpoint = reader.get(run_config(job))

    assert checkpoint is not None
    assert checkpoint["channel_values"]["question"] == _QUESTION


def test_the_state_models_come_back_as_themselves(
    pg: Engine, pg_url: str, job: str, web: RecordedWeb
) -> None:
    """`state_serde()` allows the five state models and the serializer's own safe types, and
    nothing else. A checkpoint that came back as dictionaries would still look fine until the
    resumed graph asked a plan for its subtopics."""
    _run_to_the_gate(pg, pg_url, job)

    with postgres_checkpointer(pg_url) as reader:
        graph = _graph(_fake(), pg, reader)
        state = cast(ResearchState, graph.get_state(run_config(job)).values)

    assert isinstance(state["plan"], ResearchPlan)
    assert all(isinstance(finding, Finding) for finding in state["findings"])
    assert isinstance(state["report"], Report)
    assert all(isinstance(verdict, Verdict) for verdict in state["verdicts"])


def test_a_fresh_process_can_load_the_checkpoint(
    pg: Engine, pg_url: str, job: str, web: RecordedWeb
) -> None:
    """The restart, for real: a different interpreter, with nothing of this one's memory.

    This is the claim the checkpointer exists to support - a job paused at the gate on Monday
    is resumable by a worker that starts on Wednesday - and an in-process saver cannot make it.
    """
    _run_to_the_gate(pg, pg_url, job)

    result = subprocess.run(
        [sys.executable, "-c", _READ_THE_CHECKPOINT, job],
        capture_output=True,
        text=True,
        # The repository root is the import root, and `-c` puts the working directory on the
        # path - which is how `python -m worker` will find these modules too.
        cwd=_ROOT,
        env={**os.environ, URL_VARIABLE: pg_url},
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"interrupted|6|{_QUESTION}"


# --- Resume semantics -------------------------------------------------------------------


def test_a_job_paused_by_one_saver_is_approved_by_another(
    pg: Engine, pg_url: str, job: str, web: RecordedWeb
) -> None:
    """The gate, across a restart. The pausing saver and its pool are gone by the time the
    reviewer decides, which is what Phase 3 makes routine: the API and the worker are separate
    processes, and days can pass between the two halves."""
    _run_to_the_gate(pg, pg_url, job)

    with postgres_checkpointer(pg_url) as resumer:
        graph = _graph(_fake(), pg, resumer)
        state = cast(
            ResearchState, graph.invoke(Command(resume={"decision": "approve"}), run_config(job))
        )

    assert state["status"] == "approved"
    row = queries.read_job(pg, job)
    assert row is not None
    assert row.status == "approved"
    assert row.report_json is not None


def test_the_resumed_job_does_not_research_the_question_again(
    pg: Engine, pg_url: str, job: str, web: RecordedWeb
) -> None:
    """The economic argument, asserted rather than assumed. Everything before the gate was paid
    for once; a resume that re-planned would re-bill every call already spent (guidelines §10).
    """
    _run_to_the_gate(pg, pg_url, job)

    with postgres_checkpointer(pg_url) as resumer:
        fake = _fake()
        _graph(fake, pg, resumer).invoke(Command(resume={"decision": "approve"}), run_config(job))

    assert fake.requests_for("planner") == []
    assert fake.requests_for("researcher") == []
    # One `subtopic_researched` row per subtopic, written by the run before the gate, and none
    # added by the resume.
    actions = [row.action for row in queries.read_audit_events(pg, job)]
    assert actions.count("subtopic_researched") == len(_SUBTOPICS)


def test_a_rejected_job_is_recorded_as_rejected_across_the_restart(
    pg: Engine, pg_url: str, job: str, web: RecordedWeb
) -> None:
    """The other decision, so the resume path is not proven only for the happy one."""
    _run_to_the_gate(pg, pg_url, job)

    with postgres_checkpointer(pg_url) as resumer:
        graph = _graph(_fake(), pg, resumer)
        graph.invoke(Command(resume={"decision": "reject"}), run_config(job))

    row = queries.read_job(pg, job)
    assert row is not None
    assert row.status == "rejected"
    assert row.report_json is None


# --- Helpers ----------------------------------------------------------------------------

_READ_THE_CHECKPOINT = """
import os, sys
from graph.build import (
    postgres_checkpointer,
)
from graph.state import (
    ResearchState,
    new_state,
    run_config,
)

with postgres_checkpointer(os.environ["TEST_DATABASE_URL"]) as saver:
    saved = saver.get_tuple(run_config(sys.argv[1]))

values = saved.checkpoint["channel_values"]
paused = any(write[1] == "__interrupt__" for write in saved.pending_writes or ())
print(f"{'interrupted' if paused else 'running'}|{len(values['findings'])}|{values['question']}")
"""
"""What a worker starting fresh does: open the checkpointer, read the thread, and find the job
paused at the gate with its evidence intact.

Run in its own interpreter, so nothing of the test's memory is available to it - which is the
only way to ask whether the state really left the process. It prints one line rather than
asserting, so a failure shows what the fresh process actually saw.
"""


def _page(tag: str) -> Page:
    return Page(
        url=f"https://source-{tag}.example/report",
        title=f"Source {tag}",
        text=(
            f"Source {tag} reported cloud revenue of $1.2bn in FY24.\n"
            "The rest of the page is boilerplate."
        ),
    )


def _fake(**overrides: list[Answer]) -> FakeLLM:
    """The script for one clean job, as the offline graph tests drive it."""
    script: dict[str, list[Answer]] = {
        "supervisor": list(_ROUTE_TO_THE_GATE),
        "planner": [plan(*_SUBTOPICS)],
        "researcher": [quote_the_page()] * 6,
        "synthesizer": [draft(1)],
        "fact_checker": [
            verdict_batch(quote="Source 1a reported cloud revenue of $1.2bn in FY24.")
        ],
        "reflection": [rubric()],
    }
    return FakeLLM(**{**script, **overrides})


def _graph(fake: FakeLLM, db: Engine, checkpointer: PostgresSaver) -> ResearchGraph:
    config = load_config(_ENV)
    return build_graph(
        config=config,
        llm=LLMClient(config, client=cast(OpenAI, fake)),
        db=db,
        checkpointer=checkpointer,
    )


def _run_to_the_gate(db: Engine, url: str, job_id: str) -> None:
    """One job, up to the interrupt, in a checkpointer that is closed before the test goes on.

    Closing it is the point: everything after this runs against a pool that did not exist when
    the job paused.
    """
    with postgres_checkpointer(url) as saver:
        graph = _graph(_fake(), db, saver)
        graph.invoke(
            new_state(job_id=job_id, user_id="user-1", question=_QUESTION), run_config(job_id)
        )


def _tables(url: str) -> set[str]:
    engine = sa.create_engine(queries.sqlalchemy_url(url), poolclass=sa.pool.NullPool)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()
