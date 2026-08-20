"""
WHY THIS FILE EXISTS
    tests/test_artifacts.py proves what `ArtifactStore` does with a scripted client, and
    tests/test_graph_persistence.py proves what the export node does with a scripted bucket.
    Neither can prove what **S3** does with the call - and three of step 22a's properties are
    the service's answer rather than our code's:

      * a `PutObject` really creates an object under `reports/{job_id}.json`, and the bytes
        that come back out are the approved report;
      * a presigned URL is really fetchable by something holding no credentials at all, which
        is the entire reason report bytes never stream through the API (guidelines §12);
      * and a write that S3 itself refuses really exhausts on the documented schedule.

    A fake can be wrong about all three in the same direction as the code that calls it. So
    these run against LocalStack's S3 from `docker-compose.yml`, through the same
    `artifacts.build_artifact_store()` the worker and the API use, with `endpoint_url` as the
    only difference from AWS.

    **The failure path is a real failure, not an injected one.** Exhausting the retry needs S3
    to refuse three times, and pointing the store at a bucket that does not exist is how that
    is arranged here - a genuine `NoSuchBucket` from the real client, three times, on the real
    schedule. Monkeypatching the client would have tested the monkeypatch.

    **`job_finished` on real PostgreSQL lives next door**, in tests/test_database_postgres.py,
    because it is a constraint-and-transaction question rather than an S3 one. The database
    under this file is the offline SQLite one, for the same reason
    tests/test_queue_localstack.py uses it: what is under test here is the bucket.

    **No AWS credentials, and no AWS.** The fixture supplies the placeholders boto3 insists on
    and LocalStack ignores; nothing here can reach a real account, because `endpoint_url` is
    where every request goes.

WHO CALLS IT
    `pytest -m integration`, with `SQS_ENDPOINT_URL` set - the same variable the queue layer
    uses, because it names the one LocalStack that serves both. Unset, every test here skips,
    which is what keeps plain `pytest` offline (guidelines §18).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import boto3
import httpx
import pytest
from dbharness import a_finding, a_report, migrated_engine, new_job_id
from fakes import FakeQueue
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.engine import Engine

import artifacts as artifacts_module
import scripts.reexport_job as reexport_job
from app import create_application
from artifacts import ArtifactError, ArtifactStore, build_artifact_store, object_key
from config import load_config
from database import queries
from graph.state import state_serde
from routes.auth import ApiKeyAuthenticator, Identity, hash_key

pytestmark = pytest.mark.integration

ENDPOINT_VARIABLE = "SQS_ENDPOINT_URL"

SKIP_REASON = (
    f"{ENDPOINT_VARIABLE} is not set. Start the local infrastructure with "
    "`docker compose up -d --wait`, then set it to http://localhost:4566."
)

COMPOSE_BUCKET = "research-reports"
"""The name `docker-compose.yml` passes to the bootstrap script. Restated rather than parsed
out of the compose file, because what is under test is that the bucket LocalStack actually has
matches the design - and reading the same file it was built from would agree with it however
it changed."""

REGION = "ap-south-1"

USER = "22222222-2222-4222-8222-222222222222"
REVIEWER = "11111111-1111-4111-8111-111111111111"
_QUESTION = "Compare TCS and Infosys on cloud strategy."
_OPERATOR = "ops-alice"

_ENV = {
    "DATABASE_URL": "postgresql://user:pw@localhost:5432/research",
    "SQS_QUEUE_URL": "https://sqs.ap-south-1.amazonaws.com/1/research-jobs.fifo",
    "S3_BUCKET": COMPOSE_BUCKET,
}
"""What the **API** starts from. Deliberately carrying no LLM and no Tavily credential, which
is ADR 0012 decision 4 and still true after step 22a: presigning is not executing."""

_KEYS: dict[str, Identity] = {
    hash_key("submitter-key"): Identity(user_id=USER, role="submitter"),
    hash_key("reviewer-key"): Identity(user_id=REVIEWER, role="reviewer"),
}
_SUBMITTER_AUTH = {"Authorization": "Bearer submitter-key"}


# --- Reaching LocalStack ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def localstack(monkeypatch: pytest.MonkeyPatch) -> str:
    """Where LocalStack is, and the credentials boto3 insists on before it will talk to it.

    The credentials are set here rather than read from the environment, so this suite can never
    depend on - or accidentally use - a real AWS profile. `AWS_PROFILE` is cleared for the same
    reason.

    Autouse, so an unset variable skips this module rather than failing it.
    """
    endpoint = os.environ.get(ENDPOINT_VARIABLE, "").strip()
    if not endpoint:
        pytest.skip(SKIP_REASON)

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    return endpoint


@pytest.fixture(autouse=True)
def waited(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The retry backoff, recorded rather than waited. The schedule is asserted in
    tests/test_artifacts.py; spending ten real seconds re-proving it against a container would
    buy nothing and would make this layer slow enough to skip."""
    recorded: list[float] = []
    monkeypatch.setattr(artifacts_module, "sleep", recorded.append)
    return recorded


@pytest.fixture
def s3(localstack: str) -> Any:
    """A raw client, for the two things the boundary deliberately does not expose: creating a
    bucket, and reading an object back to check what was written."""
    return boto3.client("s3", region_name=REGION, endpoint_url=localstack)


@pytest.fixture
def throwaway(s3: Any, localstack: str) -> Iterator[tuple[ArtifactStore, str]]:
    """A bucket of this test's own, so one case's objects are never another's.

    A bucket per test rather than a shared one that is emptied between them: the compose bucket
    is what `uvicorn` and `python -m worker` point at locally, and a suite that deleted objects
    from it would delete a developer's.
    """
    name = f"probe-{uuid4().hex[:12]}"
    s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION})

    yield build_artifact_store(name, region=REGION, endpoint_url=localstack), name

    listed = s3.list_objects_v2(Bucket=name).get("Contents") or []
    for stored in listed:
        s3.delete_object(Bucket=name, Key=stored["Key"])
    s3.delete_bucket(Bucket=name)


@pytest.fixture
def db(tmp_path: Path) -> Engine:
    return migrated_engine(tmp_path)


def _exported_job(db: Engine) -> str:
    """A job whose gate passed and whose body is durable, with no artifact yet - the state the
    export node is in at the moment it calls `put_report`."""
    job_id = new_job_id()
    queries.create_job(
        db,
        job_id=job_id,
        user_id=USER,
        question=_QUESTION,
        idempotency_key=f"key-{job_id}",
        actor=USER,
    )
    queries.record_research(
        db, job_id=job_id, new_findings=[a_finding("f1")], subtopic="s1", status="done"
    )
    report = a_report(("c1", ["f1"]))
    queries.record_claims(db, job_id=job_id, report=report)
    queries.record_export_result(db, job_id=job_id, report=report, uncited=())
    return job_id


def _failed_export(db: Engine) -> str:
    """That job, after the artifact write was exhausted: terminal, body preserved, no object."""
    job_id = _exported_job(db)
    queries.record_artifact_failed(db, job_id=job_id, actor="system", key=object_key(job_id))
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


# --- 1. The bucket Compose declares -------------------------------------------------------


def test_the_report_bucket_exists(s3: Any) -> None:
    # A mount that names a missing directory is not an error, so a bootstrap script that never
    # ran looks exactly like one that did until something asks for the bucket.
    assert s3.head_bucket(Bucket=COMPOSE_BUCKET)["ResponseMetadata"]["HTTPStatusCode"] == 200


# --- 2. The write, against real S3 --------------------------------------------------------


def test_a_write_creates_the_object_under_the_documented_key(
    throwaway: tuple[ArtifactStore, str], s3: Any, db: Engine
) -> None:
    store, bucket = throwaway
    job_id = _exported_job(db)
    row = queries.read_job(db, job_id)
    assert row is not None

    key = store.put_report(job_id, row.report_json)

    assert key == f"reports/{job_id}.json"
    stored = s3.get_object(Bucket=bucket, Key=key)
    assert stored["ContentType"] == "application/json"


def test_the_bytes_that_come_back_are_the_approved_report(
    throwaway: tuple[ArtifactStore, str], s3: Any, db: Engine
) -> None:
    # The artifact is a projection of `jobs.report_json`, so the two must be the same document
    # - that is what makes ADR 0009's recovery a re-export rather than a re-run.
    store, bucket = throwaway
    job_id = _exported_job(db)
    row = queries.read_job(db, job_id)
    assert row is not None

    store.put_report(job_id, row.report_json)

    body = s3.get_object(Bucket=bucket, Key=object_key(job_id))["Body"].read()
    assert json.loads(body.decode("utf-8")) == row.report_json


def test_exported_at_is_null_until_the_object_exists_and_stamped_only_after(
    throwaway: tuple[ArtifactStore, str], db: Engine
) -> None:
    """ADR 0009 decision 1, with a real object on the other side of the stamp.

    The order is what is under test: the body is durable, the column is still NULL, the write
    happens, and only then does the row claim an export.
    """
    store, _ = throwaway
    job_id = _exported_job(db)

    before = queries.read_job(db, job_id)
    assert before is not None
    assert before.report_json is not None and before.exported_at is None

    key = store.put_report(job_id, before.report_json)
    queries.record_artifact_written(db, job_id=job_id, actor="system", key=key)

    after = queries.read_job(db, job_id)
    assert after is not None and after.exported_at is not None


def test_a_second_write_overwrites_rather_than_making_a_second_artifact(
    throwaway: tuple[ArtifactStore, str], s3: Any, db: Engine
) -> None:
    # The key is derived from the job id alone, which is what makes a replayed export node and
    # an operator's re-export converge instead of littering the bucket.
    store, bucket = throwaway
    job_id = _exported_job(db)
    row = queries.read_job(db, job_id)
    assert row is not None

    store.put_report(job_id, row.report_json)
    store.put_report(job_id, row.report_json)

    listed = s3.list_objects_v2(Bucket=bucket).get("Contents") or []
    assert [stored["Key"] for stored in listed] == [object_key(job_id)]


# --- 3. The failure, from a real refusal --------------------------------------------------


def test_an_exhausted_write_preserves_the_body_and_leaves_no_export_date(
    localstack: str, db: Engine, waited: list[float]
) -> None:
    """ADR 0009's first shipping condition, with S3 doing the refusing.

    The bucket does not exist, so all three attempts get a real `NoSuchBucket` from the real
    client - which is a truer rehearsal of "storage is having a bad day" than any injected
    exception, because the error travels the whole path a production one would.
    """
    store = build_artifact_store(
        f"missing-{uuid4().hex[:12]}", region=REGION, endpoint_url=localstack
    )
    job_id = _exported_job(db)
    row = queries.read_job(db, job_id)
    assert row is not None

    with pytest.raises(ArtifactError, match="after 3 attempts"):
        store.put_report(job_id, row.report_json)

    assert waited == [2.0, 8.0]

    # What the export node does with that, and what the job is left holding.
    queries.record_artifact_failed(db, job_id=job_id, actor="system", key=object_key(job_id))
    queries.finish_job(
        db,
        job_id=job_id,
        status="failed",
        failure_reason="export_write_failed",
        quality_flag=None,
        revision_count=0,
        llm_calls_used=30,
    )

    failed = queries.read_job(db, job_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.report_json is not None
    assert failed.exported_at is None
    trail = {event.action: event.detail for event in queries.read_audit_events(db, job_id)}
    assert trail["job_finished"]["failure_reason"] == "export_write_failed"


# --- 4. The presigned URL, fetched by something holding no credentials ---------------------


def test_the_report_route_is_a_404_before_the_artifact_exists(
    throwaway: tuple[ArtifactStore, str], db: Engine
) -> None:
    store, _ = throwaway
    job_id = _exported_job(db)

    with _api(db, store) as client:
        response = client.get(f"/jobs/{job_id}/report", headers=_SUBMITTER_AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_exported"


def test_the_report_route_answers_a_url_that_really_downloads_the_object(
    throwaway: tuple[ArtifactStore, str], db: Engine
) -> None:
    """guidelines §12's *"report bytes never stream through the API"*, demonstrated.

    The URL is fetched with a plain HTTP client carrying no credentials and no API key, which
    is exactly what a caller's browser is - and what comes back is the approved report.
    """
    store, _ = throwaway
    job_id = _exported_job(db)
    row = queries.read_job(db, job_id)
    assert row is not None
    key = store.put_report(job_id, row.report_json)
    queries.record_artifact_written(db, job_id=job_id, actor="system", key=key)

    with _api(db, store) as client:
        response = client.get(f"/jobs/{job_id}/report", headers=_SUBMITTER_AUTH)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"url", "expires_at"}

    downloaded = httpx.get(body["url"], timeout=10.0)
    assert downloaded.status_code == 200
    assert downloaded.json() == row.report_json


def test_the_url_is_signed_for_the_documented_fifteen_minutes(
    throwaway: tuple[ArtifactStore, str], localstack: str, db: Engine
) -> None:
    """guidelines §12's 15-minute lifetime, asserted **in the signature** rather than beside it.

    A URL that reported fifteen minutes in `expires_at` and was signed for an hour would be the
    interesting bug, and `X-Amz-Expires` is where the answer actually is - it is what real S3
    checks the clock against.

    **What this layer cannot prove is that the expiry is enforced**, and that is stated here
    rather than left as an apparently-passing test. LocalStack does not validate presigned
    signatures under the configuration `docker-compose.yml` runs (`S3_SKIP_SIGNATURE_VALIDATION`
    defaults on), so an expired URL still serves the object locally - a fetch after the deadline
    returns `200` here and `403` against AWS. Turning validation on would mean changing the
    Compose service that stage 1 settled, for a property only real S3 can answer. So the
    assertion is on what we send; enforcement is AWS's half of the contract and belongs to the
    first deployment that has one (Phase 5).
    """
    store, _ = throwaway
    job_id = _exported_job(db)
    row = queries.read_job(db, job_id)
    assert row is not None
    queries.record_artifact_written(
        db, job_id=job_id, actor="system", key=store.put_report(job_id, row.report_json)
    )

    with _api(db, store) as client:
        body = client.get(f"/jobs/{job_id}/report", headers=_SUBMITTER_AUTH).json()

    assert "X-Amz-Expires=900" in body["url"]
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert 800 < (expires_at - datetime.now(UTC)).total_seconds() <= 900


# --- 5. The operator's recovery, end to end -----------------------------------------------


def test_the_re_export_script_recovers_a_failed_export_against_real_s3(
    throwaway: tuple[ArtifactStore, str], s3: Any, db: Engine
) -> None:
    """ADR 0009 decision 4, with the object landing in a real bucket.

    Nothing is re-run: the body the script writes is the row that was already there when the
    first attempt failed.
    """
    store, bucket = throwaway
    job_id = _failed_export(db)
    before = queries.read_job(db, job_id)
    assert before is not None

    assert reexport_job.reexport(db, store, job_id=job_id, actor=_OPERATOR) == 0

    stored = s3.get_object(Bucket=bucket, Key=object_key(job_id))["Body"].read()
    assert json.loads(stored.decode("utf-8")) == before.report_json

    after = queries.read_job(db, job_id)
    assert after is not None and after.exported_at is not None


def test_the_recovered_job_still_reads_failed_and_is_still_downloadable(
    throwaway: tuple[ArtifactStore, str], db: Engine
) -> None:
    """ADR 0009's consequence in one test: *"A recovered job reads `status: "failed"` forever,
    with a downloadable artifact."*

    A client branches on this route's answer, not on the status, when it wants the object.
    """
    store, _ = throwaway
    job_id = _failed_export(db)

    with _api(db, store) as client:
        assert client.get(f"/jobs/{job_id}/report", headers=_SUBMITTER_AUTH).status_code == 404

    reexport_job.reexport(db, store, job_id=job_id, actor=_OPERATOR)

    with _api(db, store) as client:
        response = client.get(f"/jobs/{job_id}/report", headers=_SUBMITTER_AUTH)
        status_body = client.get(f"/jobs/{job_id}", headers=_SUBMITTER_AUTH).json()

    assert response.status_code == 200
    assert httpx.get(response.json()["url"], timeout=10.0).status_code == 200
    assert status_body["status"] == "failed"
    # And the terminal record still says why it failed, unedited.
    finished = [
        event for event in queries.read_audit_events(db, job_id) if event.action == "job_finished"
    ]
    assert len(finished) == 1
    assert finished[0].detail["failure_reason"] == "export_write_failed"


def test_the_recovery_reaches_s3_and_postgres_and_nothing_else(
    throwaway: tuple[ArtifactStore, str], db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrow surface, asserted where it can actually be violated.

    tests/test_reexport_job.py pins the import list; this pins the *runtime* dependency set by
    taking the two things a graph would need out of the environment entirely. A recovery that
    quietly built an `LLMClient` would fail here rather than in review.
    """
    store, _ = throwaway
    for variable in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "TAVILY_API_KEY", "REDIS_URL"):
        monkeypatch.delenv(variable, raising=False)
    job_id = _failed_export(db)

    assert reexport_job.reexport(db, store, job_id=job_id, actor=_OPERATOR) == 0

    row = queries.read_job(db, job_id)
    assert row is not None and row.exported_at is not None


# --- The API under test -------------------------------------------------------------------


def _api(db: Engine, store: ArtifactStore) -> TestClient:
    """The real application, holding a real presigner and no graph.

    `load_config(_ENV)` carries no LLM and no Tavily credential, which is ADR 0012 decision 4
    still holding after step 22a: presigning an object is not executing a node.
    """
    application = create_application(
        config=load_config(_ENV),
        engine=db,
        checkpoints=InMemorySaver(serde=state_serde()),
        queue=cast(Any, FakeQueue()),
        authenticator=ApiKeyAuthenticator(_KEYS),
        artifacts=store,
    )
    return TestClient(application)
