"""
WHY THIS FILE EXISTS
    ADR 0009 decision 4 is a script rather than a route, and a script that talks to production
    needs its refusals tested at least as carefully as its happy path. Four of its properties
    are the decision itself, and each of the four is asserted here:

      * **`--actor` is mandatory.** The `ck_audit_events_actor` CHECK exists so a row cannot
        say a machine did something a person did, and this is the layer above it.
      * **It only ever re-projects an existing body.** A job with no `report_json` and a job
        that already has an artifact are both refused, because neither is the recoverable set
        ADR 0009 decision 1 defines.
      * **It never rewrites the job's status.** The job stays `failed` with a downloadable
        artifact, which is what ADR 0009's consequences section says out loud - and what
        `GET /jobs/{id}/report` keying on `exported_at` is what makes coherent.
      * **It constructs no graph, no `LLMClient`, no tool and no Redis.** Asserted by reading
        the module's imports rather than by inspection, which is the fifth of ADR 0009's
        shipping conditions and the same technique the agent boundary tests already use: what
        matters is what the process *can* reach, not what it happens to call.

WHO CALLS IT
    pytest. No service, no network, no AWS.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import pytest
from dbharness import a_report, migrated_engine, new_job_id
from fakes import FakeS3, imported_modules, s3_error
from sqlalchemy.engine import Engine

import artifacts as artifacts_module
import scripts.reexport_job as reexport_job
from artifacts import ArtifactStore
from database import queries

_BUCKET = "research-reports"
_QUESTION = "Compare TCS and Infosys on cloud strategy."
_OPERATOR = "ops-alice"


@pytest.fixture(autouse=True)
def waited(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The artifact retry's backoff, recorded rather than waited. Autouse, because the failure
    tests below would otherwise spend ten real seconds proving a schedule."""
    recorded: list[float] = []
    monkeypatch.setattr(artifacts_module, "sleep", recorded.append)
    return recorded


@pytest.fixture
def db(tmp_path: Path) -> Engine:
    return migrated_engine(tmp_path)


@pytest.fixture
def bucket() -> FakeS3:
    return FakeS3()


@pytest.fixture
def store(bucket: FakeS3) -> ArtifactStore:
    return ArtifactStore(_BUCKET, client=cast(Any, bucket))


def _failed_export(db: Engine) -> str:
    """A job in exactly the state ADR 0009 decision 1 calls recoverable: approved, gate-passed,
    body preserved, no artifact, and terminal with the reason on the trail."""
    job_id = new_job_id()
    queries.create_job(
        db,
        job_id=job_id,
        user_id=new_job_id(),
        question=_QUESTION,
        idempotency_key=f"key-{job_id}",
        actor="submitter-7",
    )
    queries.record_export_result(db, job_id=job_id, report=a_report(("c1", ["f1"])), uncited=())
    queries.record_artifact_failed(db, job_id=job_id, actor="system", key=f"reports/{job_id}.json")
    queries.finish_job(
        db,
        job_id=job_id,
        status="failed",
        failure_reason="export_write_failed",
        quality_flag=None,
        revision_count=0,
        llm_calls_used=30,
    )
    return job_id


# --- 1. The argument that cannot be defaulted -------------------------------------------


def test_it_refuses_to_run_without_an_actor() -> None:
    """ADR 0009's fourth shipping condition. A default here would be a machine identity for
    something a person did, which is the exact thing `audit_events.actor` refuses."""
    with pytest.raises(SystemExit):
        reexport_job.parse_args(["some-job-id"])


def test_an_actor_and_a_job_id_are_what_it_takes() -> None:
    args = reexport_job.parse_args(["job-7", "--actor", _OPERATOR])

    assert (args.job_id, args.actor) == ("job-7", _OPERATOR)


# --- 2. What it does to a recoverable job ------------------------------------------------


def test_it_writes_the_object_and_stamps_exported_at(
    db: Engine, store: ArtifactStore, bucket: FakeS3
) -> None:
    job_id = _failed_export(db)
    before = queries.read_job(db, job_id)
    assert before is not None and before.exported_at is None

    assert reexport_job.reexport(db, store, job_id=job_id, actor=_OPERATOR) == 0

    row = queries.read_job(db, job_id)
    assert row is not None
    assert row.exported_at is not None
    # The object is the body that was already durable - a re-projection, never a re-run.
    assert bucket.body(f"reports/{job_id}.json") == row.report_json


def test_it_records_both_rows_under_the_operator(db: Engine, store: ArtifactStore) -> None:
    # ADR 0009 decision 4: a recovered export is exactly as auditable as an original one.
    job_id = _failed_export(db)

    reexport_job.reexport(db, store, job_id=job_id, actor=_OPERATOR)

    recovered = [
        (event.actor, event.action, event.detail)
        for event in queries.read_audit_events(db, job_id)
        if event.actor == _OPERATOR
    ]
    assert [action for _, action, _ in recovered] == ["export_attempted", "export_result"]
    assert recovered[1][2] == {"result": "artifact_written", "key": f"reports/{job_id}.json"}


def test_the_job_still_reads_failed_afterwards(db: Engine, store: ArtifactStore) -> None:
    """ADR 0009's alternatives table rejects flipping the job to `approved` in terms: the job
    did fail at export, and the artifact was recovered afterwards. History is not rewritten;
    `GET /jobs/{id}/report` keying on the artifact is what makes that non-contradictory."""
    job_id = _failed_export(db)
    before = queries.read_job(db, job_id)
    assert before is not None

    reexport_job.reexport(db, store, job_id=job_id, actor=_OPERATOR)

    row = queries.read_job(db, job_id)
    assert row is not None
    assert row.status == "failed"
    assert row.completed_at == before.completed_at  # untouched, not re-stamped
    finished = [
        event for event in queries.read_audit_events(db, job_id) if event.action == "job_finished"
    ]
    assert len(finished) == 1
    assert finished[0].detail == {"status": "failed", "failure_reason": "export_write_failed"}


def test_running_it_twice_leaves_one_artifact(
    db: Engine, store: ArtifactStore, bucket: FakeS3
) -> None:
    # The key is derived from the job id, so a second run overwrites rather than littering -
    # and the second run is refused anyway, because the job is no longer recoverable.
    job_id = _failed_export(db)

    assert reexport_job.reexport(db, store, job_id=job_id, actor=_OPERATOR) == 0
    assert reexport_job.reexport(db, store, job_id=job_id, actor=_OPERATOR) == 1

    assert list(bucket.objects) == [f"reports/{job_id}.json"]


# --- 3. What it refuses ------------------------------------------------------------------


def test_it_refuses_a_job_that_has_no_stored_report(
    db: Engine, store: ArtifactStore, bucket: FakeS3, caplog: pytest.LogCaptureFixture
) -> None:
    # Re-running the pipeline is emphatically not this script's job (ARCHITECTURE.md §8).
    job_id = new_job_id()
    queries.create_job(
        db,
        job_id=job_id,
        user_id=new_job_id(),
        question=_QUESTION,
        idempotency_key=f"key-{job_id}",
        actor="submitter-7",
    )

    with caplog.at_level(logging.ERROR):
        assert reexport_job.reexport(db, store, job_id=job_id, actor=_OPERATOR) == 1

    assert bucket.puts == []
    assert "no stored report" in caplog.text


def test_it_refuses_a_job_whose_artifact_already_exists(
    db: Engine, store: ArtifactStore, bucket: FakeS3, caplog: pytest.LogCaptureFixture
) -> None:
    job_id = _failed_export(db)
    queries.record_artifact_written(db, job_id=job_id, actor="system", key=f"reports/{job_id}.json")

    with caplog.at_level(logging.ERROR):
        assert reexport_job.reexport(db, store, job_id=job_id, actor=_OPERATOR) == 1

    assert bucket.puts == []
    assert "already exists" in caplog.text


def test_it_refuses_a_job_that_does_not_exist(db: Engine, store: ArtifactStore) -> None:
    assert reexport_job.reexport(db, store, job_id=new_job_id(), actor=_OPERATOR) == 1


def test_a_recovery_that_also_fails_leaves_a_row_and_no_export_date(
    db: Engine, bucket: FakeS3, waited: list[float]
) -> None:
    """S3 can still be unwell when the operator tries. The attempt is recorded under them, the
    job is untouched, and the exit code says it did not work - which is what a person running
    this from a terminal needs."""
    job_id = _failed_export(db)
    bucket.script.extend([s3_error(), s3_error(), s3_error()])
    store = ArtifactStore(_BUCKET, client=cast(Any, bucket))

    assert reexport_job.reexport(db, store, job_id=job_id, actor=_OPERATOR) == 1

    row = queries.read_job(db, job_id)
    assert row is not None
    assert row.exported_at is None and row.report_json is not None
    assert row.status == "failed"
    assert waited == [2.0, 8.0]
    failures = [
        event
        for event in queries.read_audit_events(db, job_id)
        if event.detail.get("result") == "artifact_write_failed" and event.actor == _OPERATOR
    ]
    assert len(failures) == 1


# --- 4. The process boundary --------------------------------------------------------------


def test_the_script_can_reach_no_graph_no_model_and_no_tool() -> None:
    """ADR 0009's fifth shipping condition, and its consequences section: *"It must never
    construct a graph, an `LLMClient`, or a tool."*

    Asserted on the import list rather than on behaviour, because the property is about what
    this process is able to do at all. A recovery that could re-bill a model would defeat the
    reason recovery is a re-export in the first place.
    """
    imports = imported_modules(reexport_job)

    assert "graph" not in imports
    assert "llm_client" not in imports
    assert "tools" not in imports
    assert "agents" not in imports
    assert "langgraph" not in imports
    assert "redisstore" not in imports
    assert "redis" not in imports
    assert "jobqueue" not in imports
    # What it does reach: the row, the bucket, and the configuration that names them.
    assert {"artifacts", "config", "database"} <= imports
