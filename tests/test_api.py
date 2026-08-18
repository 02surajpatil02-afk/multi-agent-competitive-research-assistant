"""
WHY THIS FILE EXISTS
    The outermost boundary, tested through the real routes: a real FastAPI application, the
    real authentication dependency, the real statements, a real migrated database, and the
    real graph. Only the model and the web are replaced (tests/harness.py), so what is under
    test is the contract in guidelines §12 and §16 rather than a mock of it.

    Four groups, in the order the failures matter.

    **Nobody gets in without a key.** Every route that touches job data answers `401`
    unauthenticated, and `/health` is the single exception - the one guidelines §18 names as
    a shipping condition. A `submitter` presenting a perfectly valid key to the gate gets
    `403`, because approving is an authorization decision and not a spelling of "read".

    **The gate's refusals cost nothing.** ADR 0006 bounds reviewer edits two ways, and both
    are checked before the graph is touched: a refused edit writes no `reviewer_decision`,
    resumes nothing, and spends no LLM call. The decisive one is the budget check, which
    reads the **checkpoint** - `jobs.llm_calls_used` is `0` for the whole time a job waits at
    the gate, so a check against it would allow every edit silently. That is asserted here
    with the two numbers deliberately disagreeing.

    **A decision names the person who made it.** `audit_events.actor` is the `user_id` the
    API key maps to, never anything from the request body.

    **Deciding twice is not deciding twice.** The gate is claimed with a conditional update,
    so the second caller is told the gate was already answered rather than resuming the same
    thread again.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from dbharness import migrated_engine, new_job_id
from fakes import FakeQueue, FakeS3, s3_error
from fastapi.testclient import TestClient
from harness import (
    FakeLLM,
    Page,
    RecordedWeb,
    decision,
    draft,
    outside_untrusted_blocks,
    plan,
    quote_the_page,
    rubric,
    verdict_batch,
)
from langgraph.checkpoint.memory import InMemorySaver
from openai import OpenAI
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

import artifacts as artifacts_module
import scripts.reexport_job as reexport_job
from app import create_application
from artifacts import ArtifactStore
from config import Config, load_config
from database import queries
from database.schema import audit_events, jobs
from graph.build import (
    ResearchGraph,
    build_graph,
)
from graph.state import (
    new_state,
    run_config,
    state_serde,
)
from llm_client import LLMClient
from routes.api import MAX_REVIEWER_TEXT_CHARS, _idempotency_key
from routes.auth import AuthConfigError, Identity, hash_key, identity_from, load_api_keys
from worker import WorkerDeps
from worker import handle as worker_handle

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_FAST_MODEL": "fast-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
}

_QUESTION = "Compare TCS and Infosys on cloud strategy."
_SUBTOPICS = (
    "What is TCS cloud revenue?",
    "What is Infosys cloud revenue?",
    "How do their cloud partnerships compare?",
)

_CALLS_TO_THE_GATE = 16
"""What one clean job spends before it pauses: 1 planner, 6 researcher, 1 synthesizer,
1 fact-checker, 1 reflection, and 6 Supervisor hops. Used to put the budget exactly where a
test needs it rather than approximately there."""

_BUCKET = "research-reports"

REVIEWER = "11111111-1111-4111-8111-111111111111"
SUBMITTER = "22222222-2222-4222-8222-222222222222"
OUTSIDER = "33333333-3333-4333-8333-333333333333"

_KEYS: dict[str, Identity] = {
    hash_key("reviewer-key"): Identity(user_id=REVIEWER, role="reviewer"),
    hash_key("submitter-key"): Identity(user_id=SUBMITTER, role="submitter"),
    hash_key("outsider-key"): Identity(user_id=OUTSIDER, role="submitter"),
}
"""Three callers, because three things need proving: a reviewer may decide, a submitter may
not, and a submitter may not read someone else's job. The table holds hashes, never keys."""

_REVIEWER_AUTH = {"Authorization": "Bearer reviewer-key"}
_SUBMITTER_AUTH = {"Authorization": "Bearer submitter-key"}
_OUTSIDER_AUTH = {"Authorization": "Bearer outsider-key"}

PROTECTED = [
    ("POST", "/jobs", {"question": _QUESTION}),
    ("GET", "/jobs/{job_id}", None),
    ("GET", "/jobs/{job_id}/gate", None),
    ("POST", "/jobs/{job_id}/approve", {"decision": "approve"}),
    ("GET", "/jobs/{job_id}/report", None),
]
"""Every route except `/health`. guidelines §18 makes "unauthenticated is rejected on all of
them" a Phase 2 shipping condition, so the list is parametrized rather than sampled."""


# --- The application under test -----------------------------------------------------


def _sentence(tag: str) -> str:
    return f"Source {tag} reported cloud revenue of $1.2bn in FY24."


def _page(tag: str) -> Page:
    return Page(
        url=f"https://source-{tag}.example/report",
        title=f"Source {tag}",
        text=f"{_sentence(tag)}\nThe rest of the page is boilerplate.",
    )


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> RecordedWeb:
    recorded = RecordedWeb()
    for index, question in enumerate(_SUBTOPICS, 1):
        recorded.index(question, _page(f"{index}a"), _page(f"{index}b"))
    recorded.install(monkeypatch)
    return recorded


@pytest.fixture
def db(tmp_path: Path) -> Engine:
    return migrated_engine(tmp_path)


@pytest.fixture
def fake() -> FakeLLM:
    """One clean job to the gate, plus what a single reviewer edit needs after it.

    The third draft is for the ADR 0007 tests only: an edit whose persistence fails has
    already spent its Synthesizer call, so the retry that finishes it spends another. Nothing
    else consumes it, and an unused answer costs a test nothing.
    """
    return FakeLLM(
        supervisor=[
            decision("planner"),
            *[decision("researcher")] * 3,
            decision("synthesizer"),
            decision("fact_checker"),
            decision("fact_checker"),
        ],
        planner=[plan(*_SUBTOPICS)],
        researcher=[quote_the_page()] * 6,
        synthesizer=[draft(1), draft(2), draft(3)],
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * 2,
        reflection=[rubric(), rubric()],
    )


@pytest.fixture
def config() -> Config:
    return load_config(_ENV)


@pytest.fixture
def saver() -> InMemorySaver:
    """One checkpoint store, shared by the graph these tests drive and the reader the API holds.

    That sharing is the Stage 2 shape in miniature: the worker writes checkpoints and the API
    only ever reads them (ADR 0012). In production they are two processes against one Postgres;
    here they are two objects against one saver, and the API side still constructs no graph.
    """
    return InMemorySaver(serde=state_serde())


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def bucket() -> FakeS3:
    """The report bucket, in memory, shared by the worker that writes and the API that signs.

    That sharing is the deployment in miniature: the worker's role has `PutObject` and the
    API's has only the signature (guidelines §13's least-privilege table), and here they are
    two `ArtifactStore` objects over one dictionary.
    """
    return FakeS3()


class _UnreachableRedis:
    """A `RedisProbe` that says no. What `/health` sees when Redis is down."""

    def reachable(self) -> bool:
        return False


class _RaisingRedis:
    """A `RedisProbe` whose client raises rather than answering - a connection reset rather
    than a refusal. `/health` has to survive it as a failed check, not a failed request."""

    def reachable(self) -> bool:
        raise ConnectionError("boom: redis went away mid-check")


@pytest.fixture
def graph(
    config: Config, fake: FakeLLM, db: Engine, saver: InMemorySaver, bucket: FakeS3
) -> ResearchGraph:
    """The graph **the worker would hold**. The API never sees it.

    These tests build one because something has to move a job to the gate before a reviewer can
    decide on it, and since ADR 0011 that something is no longer the request that decides.

    It holds the artifact store for the same reason: writing the object is the worker's, and
    `GET /jobs/{id}/report` has nothing to sign for until the worker has written one.
    """
    return build_graph(
        config=config,
        llm=LLMClient(config, client=cast(OpenAI, fake)),
        db=db,
        artifacts=ArtifactStore(_BUCKET, client=bucket),
        checkpointer=saver,
    )


@pytest.fixture
def client(
    config: Config, db: Engine, saver: InMemorySaver, queue: FakeQueue, bucket: FakeS3
) -> Iterator[TestClient]:
    with TestClient(_application(config, db, saver, queue, bucket)) as made:
        yield made


@pytest.fixture
def unraising_client(
    config: Config, db: Engine, saver: InMemorySaver, queue: FakeQueue, bucket: FakeS3
) -> Iterator[TestClient]:
    """The same application, returning a server error instead of re-raising it.

    `TestClient` re-raises an unhandled exception by default, which is what every other test
    here wants: a `500` nobody asked for should fail the test loudly rather than be asserted
    on. The ADR 0007 tests are about the response a reviewer actually receives when a resume
    dies, so this one lets it come back as a response - which is what uvicorn does in front of
    the same handler.
    """
    application = _application(config, db, saver, queue, bucket)
    with TestClient(application, raise_server_exceptions=False) as made:
        yield made


def _application(
    config: Config,
    db: Engine,
    saver: InMemorySaver,
    queue: FakeQueue,
    bucket: FakeS3 | None = None,
) -> Any:
    """The API under test: a config, an engine, a **checkpoint reader** and a queue.

    There is no `graph=` argument any more, and that absence is the assertion ADR 0012 asks
    for - the API cannot execute a node because it is never handed anything that could.

    `FakeQueue` is cast rather than declared a subclass: `JobQueue` holds a boto3 client and
    the fake holds a list, so inheritance would mean inheriting a constructor that wants one.
    What has to match is the four methods the API and the worker call, and a test that calls
    all four is the check on that.
    """
    return create_application(
        config=config,
        engine=db,
        checkpoints=saver,
        queue=cast(Any, queue),
        keys=_KEYS,
        # A **presigner** and nothing more: the Protocol the route layer declares has no
        # `put_report` on it, because the API's task role may not write an object.
        artifacts=None if bucket is None else ArtifactStore(_BUCKET, client=bucket),
    )


def _worker(graph: ResearchGraph, db: Engine, queue: FakeQueue) -> WorkerDeps:
    """The worker process these tests drive, wired to the same database and checkpoint store."""
    return WorkerDeps(
        config=load_config(_ENV),
        engine=db,
        graph=graph,
        queue=cast(Any, queue),
        final_delivery_at=3,
    )


def _work(graph: ResearchGraph, db: Engine, queue: FakeQueue) -> int:
    """Do what the worker would do with whatever the API enqueued, and say how many messages.

    Since ADR 0011 a gate decision only *records and enqueues*, so a test that wants to see the
    outcome has to run the queue down. This is deliberately the worker's real logic - the
    checkpoint decides start from resume, the decision is read back out of the audit trail -
    rather than a second copy of it living in this file.

    It drains what is visible **once**. A message the worker did not delete stays in flight
    rather than returning, which is what makes this terminate: redelivery is a separate,
    explicit `queue.redeliver()`, because in production it is a timeout rather than an
    immediate retry.
    """
    deps = _worker(graph, db, queue)
    handled = 0
    while (message := queue.receive()) is not None:
        worker_handle(deps, message)
        handled += 1
    return handled


def _submitted(client: TestClient) -> str:
    """A job row, created the way a submitter creates one."""
    response = client.post("/jobs", json={"question": _QUESTION}, headers=_SUBMITTER_AUTH)
    assert response.status_code == 202, response.text
    return cast(str, response.json()["job_id"])


def _at_the_gate(client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue) -> str:
    """A submitted job, run to the human gate **through the queue**, the way production runs it.

    `_submitted` posts the job, which writes a `queued` row and enqueues one start message; the
    worker then picks that message up, moves the row to `running`, and invokes the graph until
    the gate node interrupts. Nothing here writes `awaiting_approval` - the gate node does,
    through the same engine.

    Driving the start message rather than invoking the graph directly is what keeps the queue
    empty afterwards, so a test that counts messages after a gate decision is counting the
    decision's message and not a leftover.
    """
    job_id = _submitted(client)
    assert _work(graph, db, queue) == 1
    assert _status(db, job_id) == "awaiting_approval"
    return job_id


def _decisions(db: Engine, job_id: str) -> list[Any]:
    return [
        event
        for event in queries.read_audit_events(db, job_id)
        if event.action == "reviewer_decision"
    ]


def _openings(db: Engine, job_id: str) -> list[Any]:
    return [
        event for event in queries.read_audit_events(db, job_id) if event.action == "gate_opened"
    ]


def _status(db: Engine, job_id: str) -> str:
    row = queries.read_job(db, job_id)
    assert row is not None
    return cast(str, row.status)


# --- Authentication -----------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_every_route_but_health_refuses_an_unauthenticated_caller(
    method: str, path: str, body: dict[str, Any] | None, client: TestClient
) -> None:
    response = client.request(method, path.format(job_id=new_job_id()), json=body)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_an_unknown_key_is_refused_like_no_key_at_all(
    method: str, path: str, body: dict[str, Any] | None, client: TestClient
) -> None:
    # Same answer for an absent header, a wrong scheme and a wrong key: telling them apart
    # would say which half of the credential to keep working on.
    for header in ({"Authorization": "Bearer nope"}, {"Authorization": "Basic reviewer-key"}):
        response = client.request(
            method, path.format(job_id=new_job_id()), json=body, headers=header
        )

        assert response.status_code == 401


def test_health_is_the_one_route_that_needs_no_key(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"db": True, "redis": True}}


def test_health_says_nothing_a_stranger_should_not_know(client: TestClient) -> None:
    # A status and one boolean per dependency. No secret, no connection string, no hostname,
    # no version, no job data, no counts, no error text (guidelines §16).
    body = response = client.get("/health").json()

    assert set(body) == {"status", "checks"}
    assert all(isinstance(value, bool) for value in body["checks"].values())
    # One boolean per dependency the process actually reaches, and `redis` joined that list
    # at step 21 - before which it was deliberately absent rather than false (guidelines §12).
    assert set(body["checks"]) == {"db", "redis"}
    assert "sqlite" not in str(response).lower()


def test_health_reports_redis_and_degrades_when_it_is_gone(
    config: Config, db: Engine, saver: InMemorySaver, queue: FakeQueue
) -> None:
    """`/health` is how a deployment finds out the workers cannot work.

    The API itself needs no Redis - it calls no model and fetches no page. What an
    unreachable Redis means is that the **shared rate limiter is gone**, and that limiter
    fails closed, so no LLM call can be made anywhere. Failing the check is what takes the
    task out of the target group so new jobs stop arriving (ARCHITECTURE.md §8), which beats
    accepting jobs that would sit `queued` while every worker fails its first node.
    """
    application = create_application(
        config=config,
        engine=db,
        checkpoints=saver,
        queue=cast(Any, queue),
        keys=_KEYS,
        redis=_UnreachableRedis(),
    )

    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "checks": {"db": True, "redis": False}}


def test_a_redis_probe_that_raises_is_a_failed_check_not_a_failed_request(
    config: Config, db: Engine, saver: InMemorySaver, queue: FakeQueue
) -> None:
    # A health route that 500s tells a load balancer nothing it can act on, and the reason
    # belongs in the log rather than in a body an anonymous caller reads (guidelines §16).
    application = create_application(
        config=config,
        engine=db,
        checkpoints=saver,
        queue=cast(Any, queue),
        keys=_KEYS,
        redis=_RaisingRedis(),
    )

    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["checks"] == {"db": True, "redis": False}
    assert "boom" not in response.text


# --- Authorization ------------------------------------------------------------------


def test_a_submitter_may_not_decide_at_the_gate(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # guidelines §18's other named shipping condition. A valid key is not permission.
    job_id = _at_the_gate(client, graph, db, queue)

    response = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_SUBMITTER_AUTH
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "reviewer_role_required"
    assert _decisions(db, job_id) == []  # refused before anything was recorded


def test_a_submitter_cannot_read_someone_elses_job(client: TestClient) -> None:
    job_id = _submitted(client)

    response = client.get(f"/jobs/{job_id}", headers=_OUTSIDER_AUTH)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_the_owner"


def test_a_reviewer_may_read_a_job_it_did_not_submit(client: TestClient) -> None:
    # Deciding without reading would be approving unseen, which is what the gate exists to
    # prevent - so the role that decides can read.
    job_id = _submitted(client)

    assert client.get(f"/jobs/{job_id}", headers=_REVIEWER_AUTH).status_code == 200


# --- Submitting -------------------------------------------------------------------


def test_a_submitted_job_is_accepted_and_recorded(client: TestClient, db: Engine) -> None:
    response = client.post("/jobs", json={"question": _QUESTION}, headers=_SUBMITTER_AUTH)

    body = response.json()
    row = queries.read_job(db, body["job_id"])
    assert response.status_code == 202
    assert body["status"] == "queued"
    assert row is not None
    assert row.user_id == SUBMITTER  # the identity came from the key, not the body
    assert row.question == _QUESTION
    assert [event.action for event in queries.read_audit_events(db, body["job_id"])] == [
        "job_created"
    ]


def test_the_same_question_twice_in_one_day_is_one_job(client: TestClient, db: Engine) -> None:
    # The unique constraint decides, and the caller gets the job they already have
    # (ARCHITECTURE.md §9). Two jobs would be 60 calls of budget spent on a duplicate.
    first = _submitted(client)

    response = client.post("/jobs", json={"question": _QUESTION}, headers=_SUBMITTER_AUTH)

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "duplicate_job",
        "message": "This question was already submitted today",
        "job_id": first,
    }
    with db.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(jobs)).scalar_one() == 1


def test_two_callers_asking_the_same_question_get_their_own_jobs(client: TestClient) -> None:
    # The key is scoped to the user, so one caller's question never collides with another's.
    first = _submitted(client)

    response = client.post("/jobs", json={"question": _QUESTION}, headers=_OUTSIDER_AUTH)

    assert response.status_code == 202
    assert response.json()["job_id"] != first


@pytest.mark.parametrize("question", ["", "   ", "\x00\x01"])
def test_an_empty_question_is_refused(question: str, client: TestClient) -> None:
    response = client.post("/jobs", json={"question": question}, headers=_SUBMITTER_AUTH)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_question"


def test_a_question_longer_than_the_cap_is_refused(client: TestClient) -> None:
    response = client.post("/jobs", json={"question": "a" * 501}, headers=_SUBMITTER_AUTH)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_question"


def test_control_characters_are_stripped_before_the_question_is_stored(
    client: TestClient, db: Engine
) -> None:
    # It reaches a prompt and a row, so it is cleaned at the edge (guidelines §16).
    dirty = "Compare" + chr(0) + " TCS" + chr(0x200B) + " and" + chr(10) + " Infosys."

    response = client.post("/jobs", json={"question": dirty}, headers=_SUBMITTER_AUTH)

    row = queries.read_job(db, response.json()["job_id"])
    assert row is not None
    assert row.question == "Compare TCS and Infosys."


def test_a_body_the_api_cannot_parse_uses_the_one_error_shape(client: TestClient) -> None:
    response = client.post("/jobs", json={"not_a_question": 1}, headers=_SUBMITTER_AUTH)

    assert response.status_code == 400
    assert set(response.json()["error"]) == {"code", "message", "job_id"}


# --- What the API puts on the queue (ADR 0010) ---------------------------------------
#
# The endpoint's whole job from here is: one row, then one pointer message. Everything below
# is a property of that message rather than of the response, because the response says
# `queued` either way and the message is what makes the difference between a job that runs
# and a job that sits there.


def test_a_submitted_job_produces_exactly_one_start_message(
    client: TestClient, db: Engine, queue: FakeQueue
) -> None:
    """The pointer message, in full: three identifiers, the group id, the deduplication id.

    `MessageGroupId = job_id` is the load-bearing one (ADR 0010 decision 4): a FIFO queue
    delivers at most one message per group at a time, which is what keeps one job to one
    writer and lets ADR 0005's `_write_findings` do a read-then-write at all. A change to a
    standard queue, or to a constant group id, would break that silently.
    """
    job_id = _submitted(client)

    message = queue.only()

    assert message.body == {
        "job_id": job_id,
        "user_id": SUBMITTER,
        "idempotency_key": _idempotency_key(SUBMITTER, _QUESTION),
    }
    assert message.group_id == job_id
    assert message.deduplication_id == _idempotency_key(SUBMITTER, _QUESTION)
    assert _status(db, job_id) == "queued"


def test_the_start_message_carries_no_question_and_no_state(
    client: TestClient, queue: FakeQueue
) -> None:
    # §20 row 8: identifiers only, never state. The question is user text, so keeping it out
    # of the queue is also what keeps untrusted input off the wire - the worker reads it from
    # the row it has to read anyway.
    _submitted(client)

    body = queue.only().body

    assert set(body) == {"job_id", "user_id", "idempotency_key"}
    assert _QUESTION not in str(body)


def test_the_row_is_committed_before_the_message_is_sent(
    client: TestClient, db: Engine, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0010 decision 10's order, read at the one instant it is observable.

    A message with no row would name a job that does not exist, and the worker would have
    nothing to do but delete it. A row with no message is the recoverable half, which is why
    it is the one allowed to be true alone.
    """
    seen: list[str | None] = []
    original = queue.send_start

    def watch(**kwargs: Any) -> None:
        row = queries.read_job(db, kwargs["job_id"])
        seen.append(None if row is None else cast(str, row.status))
        original(**kwargs)

    monkeypatch.setattr(queue, "send_start", watch)
    _submitted(client)

    assert seen == ["queued"]


def test_a_send_that_fails_answers_503_and_leaves_the_job_queued(
    client: TestClient, db: Engine, queue: FakeQueue
) -> None:
    """ADR 0010 decision 10. `202` would claim the job was accepted for processing, and in
    exactly this case nothing will process it.

    The row stays: it holds the `idempotency_key` that makes a resubmission converge on this
    same job rather than creating a second one, and it is what a re-enqueue would target.
    """
    queue.fail_next = True

    response = client.post("/jobs", json={"question": _QUESTION}, headers=_SUBMITTER_AUTH)

    body = response.json()
    assert response.status_code == 503
    assert body["error"]["code"] == "enqueue_failed"
    assert set(body["error"]) == {"code", "message", "job_id"}
    assert _status(db, body["error"]["job_id"]) == "queued"
    assert queue.sent == []


def test_a_duplicate_submission_enqueues_nothing(client: TestClient, queue: FakeQueue) -> None:
    # The unique constraint refuses the row, so the send never happens - which is the reason
    # the deduplication id is belt-and-braces rather than the mechanism.
    _submitted(client)

    again = client.post("/jobs", json={"question": _QUESTION}, headers=_SUBMITTER_AUTH)

    assert again.status_code == 409
    assert len(queue.sent) == 1


def test_submitting_runs_no_graph_no_model_and_no_search(
    client: TestClient, fake: FakeLLM, web: RecordedWeb, queue: FakeQueue
) -> None:
    """ADR 0012, from the outside: `POST /jobs` costs a row and a message and nothing else.

    The fake model and the recorded web are wired into this test for one purpose - to be
    untouched. A request that reached either would mean the API process had done the worker's
    work, which is the property §19's container table depends on.
    """
    _submitted(client)

    assert fake.requests == []
    assert web.queries == [] and web.fetched == []
    assert len(queue.sent) == 1


def test_a_gate_decision_is_durable_before_the_resume_is_enqueued(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order that makes the reviewer's decision survive a machine failure.

    The worker reads the decision out of `audit_events` keyed by the visit, so a message sent
    before the row was written could be received before it exists - and the worker would find
    no decision, leave the message, and log. Writing first makes that race impossible rather
    than unlikely.
    """
    seen: list[int] = []
    original = queue.send_resume

    def watch(**kwargs: Any) -> None:
        seen.append(len(_decisions(db, kwargs["job_id"])))
        original(**kwargs)

    job_id = _at_the_gate(client, graph, db, queue)
    monkeypatch.setattr(queue, "send_resume", watch)

    client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)

    assert seen == [1]  # the decision was already on record when the message was sent


def test_the_resume_message_never_carries_the_reviewers_words(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # ADR 0011 decision 2, on the wire. The instruction reaches the Synthesizer through
    # Postgres, which is also what keeps the authenticated record of who said it authoritative.
    job_id = _at_the_gate(client, graph, db, queue)

    client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": "Tighten section two.", "note": "sources are thin"},
        headers=_REVIEWER_AUTH,
    )

    resume = queue.sent[-1]
    assert set(resume.body) == {"job_id", "user_id", "idempotency_key"}
    assert "Tighten" not in str(resume.body) and "thin" not in str(resume.body)
    assert resume.group_id == job_id  # the same group as the start message: one job, one writer
    assert resume.deduplication_id == f"{job_id}:{_CALLS_TO_THE_GATE}"  # ADR 0007's visit key


def test_a_resume_that_cannot_be_enqueued_answers_503_with_the_decision_recorded(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    """The same `503` as `POST /jobs`, for the same reason: the work is not moving yet.

    The decision is recorded and the gate is claimed, so the honest answer is not `200`. The
    fix is to send the identical decision again, which ADR 0007 makes a retry that writes
    nothing and counts nothing - so the reviewer is not charged an edit for an SQS outage.
    """
    job_id = _at_the_gate(client, graph, db, queue)
    queue.fail_next = True

    response = client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "enqueue_failed"
    assert len(_decisions(db, job_id)) == 1  # the human's decision stands
    assert _status(db, job_id) == "running"  # the gate is claimed, so nobody decides it twice
    retry = client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)
    assert retry.status_code == 200
    assert queries.count_reviewer_edits(db, job_id) == 1  # and it cost one edit, not two
    assert len(queue.sent) == 2


def test_a_refused_edit_enqueues_nothing(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # ADR 0006's whole point, extended by ADR 0011: a refused edit now spends no worker
    # either, because `refuse_edit()` runs before the enqueue as well as before the claim.
    job_id = _at_the_gate(client, graph, db, queue)
    _pretend_edits(db, job_id, count=3)

    response = client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reviewer_edit_limit_reached"
    assert len(queue.sent) == 1  # the start message, and nothing since


# --- Reading ------------------------------------------------------------------------


def test_an_unknown_job_is_a_404_in_the_documented_shape(client: TestClient) -> None:
    missing = new_job_id()

    response = client.get(f"/jobs/{missing}", headers=_REVIEWER_AUTH)

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "job_not_found",
        "message": "No such job",
        "job_id": missing,
    }


def test_polling_a_job_returns_the_documented_fields(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    job_id = _at_the_gate(client, graph, db, queue)

    body = client.get(f"/jobs/{job_id}", headers=_SUBMITTER_AUTH).json()

    assert set(body) == {"job_id", "status", "phase", "revision_count", "quality_flag", "report"}
    assert body["status"] == "awaiting_approval"
    assert body["phase"] == "human_gate"  # a coarse label, derived from the row (ADR 0012)
    assert body["report"] is None  # nothing is exported until the gate passes


def test_a_job_no_worker_has_picked_up_reports_that_it_is_queued(client: TestClient) -> None:
    # The window between `POST /jobs` and a worker receiving its message. It is short in
    # production and permanent here, because nothing drains the queue in this test.
    job_id = _submitted(client)

    assert client.get(f"/jobs/{job_id}", headers=_SUBMITTER_AUTH).json()["phase"] == "queued"


def test_the_report_route_is_a_404_while_a_job_is_still_at_the_gate(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # `exported_at IS NULL` is the whole predicate (ADR 0009 decision 3), and a job waiting
    # for a reviewer has no artifact by definition.
    job_id = _at_the_gate(client, graph, db, queue)

    response = client.get(f"/jobs/{job_id}/report", headers=_SUBMITTER_AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_exported"


def test_the_report_route_answers_a_presigned_url_once_the_artifact_exists(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    bucket: FakeS3,
) -> None:
    """ADR 0009 decision 3, and guidelines §12's documented body: `{url, expires_at}`.

    **The API never streams report bytes.** What comes back is a signature over an object the
    worker wrote, which is what keeps a 20-minute job's output off the request path.
    """
    job_id = _at_the_gate(client, graph, db, queue)
    client.post(f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH)
    _work(graph, db, queue)  # the export, and the artifact write, run in the worker

    response = client.get(f"/jobs/{job_id}/report", headers=_SUBMITTER_AUTH)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"url", "expires_at"}
    assert f"reports/{job_id}.json" in body["url"]
    # 15 minutes, in the signature itself rather than only in the field beside it.
    assert "X-Amz-Expires=900" in body["url"]
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert 800 < (expires_at - datetime.now(UTC)).total_seconds() <= 900
    # And the object really is there, under the key the URL names.
    assert bucket.body(f"reports/{job_id}.json")


def test_the_report_route_stays_a_404_when_the_artifact_write_was_exhausted(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    bucket: FakeS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure ADR 0009 exists for, seen from the caller's side.

    The report is finished, approved and preserved, and there is no artifact - so this route
    says so, and the body is read from `GET /jobs/{id}` instead. Answering `200` with a URL
    for an object nobody can fetch is exactly what keying on `exported_at` prevents.
    """
    monkeypatch.setattr(artifacts_module, "sleep", lambda _delay: None)
    job_id = _at_the_gate(client, graph, db, queue)
    bucket.script.extend([s3_error(), s3_error(), s3_error()])
    client.post(f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH)
    _work(graph, db, queue)

    response = client.get(f"/jobs/{job_id}/report", headers=_SUBMITTER_AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_exported"
    # The job failed, and the approved body is still where the specification says to read it.
    status_body = client.get(f"/jobs/{job_id}", headers=_SUBMITTER_AUTH).json()
    assert status_body["status"] == "failed"
    assert status_body["report"] is not None
    assert bucket.objects == {}


def test_a_recovered_artifact_is_downloadable_while_the_job_still_reads_failed(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    bucket: FakeS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0009's consequence, stated as a test because it reads like a contradiction.

    A job that failed at export reads `status: "failed"` forever - `finish_job` is never
    called again and nothing rewrites history - and its artifact is still reachable, because
    this route keys on the artifact rather than on the status. A client branches on this
    route's answer, not on the status, when it wants the object.
    """
    monkeypatch.setattr(artifacts_module, "sleep", lambda _delay: None)
    job_id = _at_the_gate(client, graph, db, queue)
    bucket.script.extend([s3_error(), s3_error(), s3_error()])
    client.post(f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH)
    _work(graph, db, queue)
    assert client.get(f"/jobs/{job_id}/report", headers=_SUBMITTER_AUTH).status_code == 404

    # What the operator runs, against the same database and the same bucket.
    assert (
        reexport_job.reexport(
            db, ArtifactStore(_BUCKET, client=bucket), job_id=job_id, actor="ops-alice"
        )
        == 0
    )

    assert client.get(f"/jobs/{job_id}/report", headers=_SUBMITTER_AUTH).status_code == 200
    assert client.get(f"/jobs/{job_id}", headers=_SUBMITTER_AUTH).json()["status"] == "failed"


# --- Reading the gate -----------------------------------------------------------------

_GATE_KEYS = [
    "job_id",
    "unsupported_claims",
    "unresearched_subtopics",
    "quality_flag",
    "score",
    "failed_dimensions",
    "revision_count",
    "llm_calls_used",
    "report",
    "claims",
]
"""ARCHITECTURE.md §12's order, written out rather than imported from the function under
test - a fixture that read the value it is checking would agree with it however it changed.
The same order is asserted at the gate node in test_graph_build.py; this is the HTTP half."""

_NOT_AT_THE_GATE = ["queued", "running", "approved", "rejected", "failed"]
"""Every status that is not `awaiting_approval`, `queued` included since ADR 0010 decision 1
made it the value a job starts at."""


@pytest.fixture
def web_with_a_dead_subtopic(monkeypatch: pytest.MonkeyPatch) -> RecordedWeb:
    """The same recorded web, except the third subtopic finds nothing.

    A search that returns no results is what marks a subtopic `unresearched`, which is one of
    the two problems ARCHITECTURE.md §12 puts in front of the report.
    """
    recorded = RecordedWeb()
    recorded.index(_SUBTOPICS[0], _page("1a"), _page("1b"))
    recorded.index(_SUBTOPICS[1], _page("2a"), _page("2b"))
    recorded.index(_SUBTOPICS[2])
    recorded.install(monkeypatch)
    return recorded


@pytest.fixture
def troubled() -> FakeLLM:
    """A job that reaches the gate carrying both of §12's problems and a failing score.

    `citation_coverage=4` is the interesting number: the weighted score is 4.8, comfortably
    over the threshold, and the report still fails because coverage is a hard gate. With
    `MAX_REVISIONS=0` the cap is already reached, so the job goes straight to the reviewer
    flagged `below_threshold` instead of starting a cycle.
    """
    return FakeLLM(
        supervisor=[
            decision("planner"),
            *[decision("researcher")] * 3,
            decision("synthesizer"),
            decision("fact_checker"),
        ],
        planner=[plan(*_SUBTOPICS)],
        researcher=[quote_the_page()] * 4,
        synthesizer=[draft(1)],
        fact_checker=[verdict_batch(quote=None, supported=False, note="not in the source")],
        reflection=[rubric(citation_coverage=4)],
    )


def _gate_body(client: TestClient, job_id: str, headers: dict[str, str] | None = None) -> Any:
    response = client.get(f"/jobs/{job_id}/gate", headers=headers or _REVIEWER_AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def _force_status(db: Engine, job_id: str, status: str) -> None:
    """Put the row in a status without driving the graph there.

    These are precondition tests about what the route does with a status, not about how a job
    arrives at one - and `set_job_status` deliberately refuses a finished row, which is the
    behaviour under test elsewhere.
    """
    with db.begin() as conn:
        conn.execute(sa.update(jobs).where(jobs.c.job_id == job_id).values(status=status))


def test_the_gate_view_returns_the_payload_keys_in_the_documented_order(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # Problems first, then quality, then the report (ARCHITECTURE.md §12). A reviewer who has
    # to hunt for the problems will approve past them, so the order is the contract.
    job_id = _at_the_gate(client, graph, db, queue)

    assert list(_gate_body(client, job_id)) == _GATE_KEYS


def test_the_gate_view_is_exactly_what_the_gate_node_interrupted_with(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph
) -> None:
    """The decisive test for ADR 0013: rebuilt from the checkpoint, not approximated.

    `interrupt()` discards the node's writes and LangGraph re-runs the node from the top on
    resume, so the checkpoint holds the state the gate node was invoked with. Rebuilding the
    payload from it therefore reproduces it rather than resembling it - which is why nothing
    has to be persisted for this route to work.
    """
    job_id = _submitted(client)
    paused = graph.invoke(
        new_state(job_id=job_id, user_id=SUBMITTER, question=_QUESTION), run_config(job_id)
    )

    assert _gate_body(client, job_id) == paused["__interrupt__"][0].value


def test_the_gate_view_shows_the_draft_that_the_status_route_cannot(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # The problem this route exists for: `GET /jobs/{id}`'s `report` is the *exported* body,
    # and nothing is exported until the reviewer approves. Reading that route at the gate
    # tells a reviewer nothing about what they are approving.
    job_id = _at_the_gate(client, graph, db, queue)

    body = _gate_body(client, job_id)

    assert body["job_id"] == job_id
    assert body["report"]["sections"][0]["body"]
    assert body["claims"]
    assert all(claim["sources"] for claim in body["claims"])
    assert all(claim["quote"] for claim in body["claims"])
    assert client.get(f"/jobs/{job_id}", headers=_REVIEWER_AUTH).json()["report"] is None


def test_the_gate_view_puts_the_problems_in_front_of_the_reviewer(
    web_with_a_dead_subtopic: RecordedWeb,
    config: Config,
    troubled: FakeLLM,
    db: Engine,
    saver: InMemorySaver,
    queue: FakeQueue,
) -> None:
    # Both of §12's problems at once: a subtopic that found nothing, and claims the
    # Fact-Checker would not support. Neither is visible anywhere else in the API.
    scored = load_config({**_ENV, "MAX_REVISIONS": "0"})
    graph = build_graph(
        config=scored,
        llm=LLMClient(scored, client=cast(OpenAI, troubled)),
        db=db,
        checkpointer=saver,
    )
    with TestClient(_application(scored, db, saver, queue)) as client:
        job_id = _at_the_gate(client, graph, db, queue)
        body = _gate_body(client, job_id)

    assert body["unresearched_subtopics"] == ["s3"]
    assert body["unsupported_claims"] == [claim["claim_id"] for claim in body["claims"]]
    assert body["quality_flag"] == "below_threshold"
    assert body["failed_dimensions"] == ["citation_coverage"]
    assert body["score"]["citation_coverage"] == 4
    assert all(claim["supported"] is False for claim in body["claims"])


def test_the_gate_view_carries_the_values_no_table_holds(
    web_with_a_dead_subtopic: RecordedWeb,
    config: Config,
    troubled: FakeLLM,
    db: Engine,
    saver: InMemorySaver,
    queue: FakeQueue,
) -> None:
    """The five values that exist only in the checkpoint, with the row disagreeing.

    A route rebuilt from `jobs`, `claims` and `claim_sources` would answer with a null report,
    a null quality flag, no score, no failed dimensions and no quote - and it would pass a
    test that only checked the keys were present. So the row is read alongside the body here
    and asserted to be exactly as empty as it really is.
    """
    scored = load_config({**_ENV, "MAX_REVISIONS": "0"})
    graph = build_graph(
        config=scored,
        llm=LLMClient(scored, client=cast(OpenAI, troubled)),
        db=db,
        checkpointer=saver,
    )
    with TestClient(_application(scored, db, saver, queue)) as client:
        job_id = _at_the_gate(client, graph, db, queue)
        body = _gate_body(client, job_id)

    row = queries.read_job(db, job_id)
    assert row is not None
    # What Postgres has while the job waits: nothing a reviewer could judge.
    assert (row.report_json, row.quality_flag, row.revision_count) == (None, None, 0)
    rows = queries.read_claims(db, job_id)
    assert all(claim.verdict_note == "not in the source" for claim in rows)
    # What the checkpoint has, and therefore what the route answers with.
    assert body["score"]["weighted_score"] > 0
    assert body["failed_dimensions"] == ["citation_coverage"]
    assert body["quality_flag"] == "below_threshold"
    assert body["report"]["sections"][0]["body"] == "What the sources say."
    assert all(claim["quote"] is None for claim in body["claims"])


def test_the_gate_view_reports_a_revision_the_jobs_row_has_not_recorded(
    web: RecordedWeb, config: Config, db: Engine, saver: InMemorySaver, queue: FakeQueue
) -> None:
    # `jobs.revision_count` is written by `finalize`, so it reads 0 for the whole time a job
    # waits at the gate. The checkpoint knows what really happened, and the reviewer needs
    # the checkpoint's number to judge how much automatic rework has already been spent.
    revised = FakeLLM(
        supervisor=[
            decision("planner"),
            *[decision("researcher")] * 3,
            decision("synthesizer"),
            *[decision("fact_checker")] * 2,
        ],
        planner=[plan(*_SUBTOPICS)],
        researcher=[quote_the_page()] * 6,
        synthesizer=[draft(1), draft(2)],
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * 2,
        # The first pass fails the hard coverage gate and routes to the Synthesizer; the
        # second passes, so the job reaches the gate having spent one revision.
        reflection=[rubric(citation_coverage=4), rubric()],
    )
    graph = build_graph(
        config=config,
        llm=LLMClient(config, client=cast(OpenAI, revised)),
        db=db,
        checkpointer=saver,
    )
    with TestClient(_application(config, db, saver, queue)) as client:
        job_id = _at_the_gate(client, graph, db, queue)
        body = _gate_body(client, job_id)

    row = queries.read_job(db, job_id)
    assert row is not None
    assert row.revision_count == 0  # the row: stale by design until finalize
    assert body["revision_count"] == 1  # the checkpoint: what actually happened


def test_the_gate_view_reports_the_call_count_the_decision_will_be_keyed_on(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # ADR 0007's gate visit is (job_id, calls_used), and ADR 0013 adds no second identifier:
    # the payload's `llm_calls_used` is that key, read from the snapshot `POST /approve` reads.
    job_id = _at_the_gate(client, graph, db, queue)

    body = _gate_body(client, job_id)

    assert body["llm_calls_used"] == graph.get_state(run_config(job_id)).values["llm_calls_used"]
    assert body["llm_calls_used"] == _CALLS_TO_THE_GATE
    # And a decision taken straight after the read answers that same visit.
    decided = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )
    assert decided.status_code == 200


def test_reading_the_gate_decides_nothing(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    fake: FakeLLM,
    queue: FakeQueue,
) -> None:
    # Reading is not deciding. No write, no audit row, no gate claim, no status change, no
    # LLM or tool request - the route loads a checkpoint, it does not run a graph.
    job_id = _at_the_gate(client, graph, db, queue)
    events = len(queries.read_audit_events(db, job_id))
    spent, searched = len(fake.requests), len(web.queries)

    _gate_body(client, job_id)
    _gate_body(client, job_id)

    assert len(queries.read_audit_events(db, job_id)) == events
    assert _decisions(db, job_id) == []
    assert _status(db, job_id) == "awaiting_approval"
    assert (len(fake.requests), len(web.queries)) == (spent, searched)


def test_a_submitter_reads_the_gate_of_its_own_job_and_a_reviewer_reads_any(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # The same policy as `GET /jobs/{id}`: this is job data, and a reviewer is asked to decide
    # on work it did not submit (guidelines §16).
    job_id = _at_the_gate(client, graph, db, queue)

    assert client.get(f"/jobs/{job_id}/gate", headers=_SUBMITTER_AUTH).status_code == 200
    assert client.get(f"/jobs/{job_id}/gate", headers=_REVIEWER_AUTH).status_code == 200


def test_a_submitter_cannot_read_someone_elses_gate(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    job_id = _at_the_gate(client, graph, db, queue)

    response = client.get(f"/jobs/{job_id}/gate", headers=_OUTSIDER_AUTH)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_the_owner"


def test_an_unknown_job_has_no_gate_to_read(client: TestClient) -> None:
    missing = new_job_id()

    response = client.get(f"/jobs/{missing}/gate", headers=_REVIEWER_AUTH)

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "job_not_found",
        "message": "No such job",
        "job_id": missing,
    }


@pytest.mark.parametrize("status", _NOT_AT_THE_GATE)
def test_a_job_that_is_not_awaiting_approval_has_no_gate_payload(
    status: str, client: TestClient, db: Engine
) -> None:
    job_id = _submitted(client)
    _force_status(db, job_id, status)

    response = client.get(f"/jobs/{job_id}/gate", headers=_REVIEWER_AUTH)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_awaiting_approval"


def test_a_job_nobody_has_run_has_no_gate_payload(client: TestClient) -> None:
    # A `queued` job has no checkpoint and no gate. The status check refuses it first, which is
    # why this and the parametrized case above are the same answer by two different routes.
    job_id = _submitted(client)

    response = client.get(f"/jobs/{job_id}/gate", headers=_REVIEWER_AUTH)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_awaiting_approval"


def test_a_row_awaiting_approval_with_no_checkpoint_is_refused_rather_than_guessed(
    client: TestClient, db: Engine
) -> None:
    # ADR 0007 invariant 4 says the two cannot disagree. If they ever do, there is genuinely
    # no gate to show, and the contradiction belongs in the log rather than in the response.
    job_id = _submitted(client)
    _force_status(db, job_id, "awaiting_approval")

    response = client.get(f"/jobs/{job_id}/gate", headers=_REVIEWER_AUTH)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_awaiting_approval"


def test_the_payload_projection_needs_none_of_the_agent_stack() -> None:
    """What makes ADR 0012 possible: the API can reach the payload without an LLM client.

    `graph/build.py` imports all five agents, the LLM client, the tool boundary and the
    database layer, so a payload builder living there would drag every one of them into a
    process that ARCHITECTURE.md §19 says must never run the graph or call the LLM. This is a
    static check because that is the property - what the module *imports*, not what it runs.
    """
    tree = ast.parse((Path(__file__).resolve().parent.parent / "graph" / "state.py").read_text())
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    # `config`, `langchain_core` and `langgraph` joined when ADR 0012 moved `run_config`,
    # `state_serde` and `refuse_edit` here so the route layer could reach them without the
    # agent stack. None of the three is an agent, a tool, or the LLM client - which is the
    # property this test exists to hold.
    assert imported == {
        "__future__",
        "config",
        "langchain_core",
        "langgraph",
        "operator",
        "schemas",
        "typing",
    }


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }


@pytest.mark.parametrize("module", ["routes/api.py", "app.py"])
def test_the_api_process_imports_nothing_that_could_run_a_node(module: str) -> None:
    """ADR 0012 decision 1 and ADR 0011 decision 7, asserted by import rather than by review.

    This is the test that fails if the API ever starts executing the graph again, and it fails
    at the first import rather than at the first request - because you cannot invoke a graph
    you never built, and you cannot build one without `graph.build`, an agent, or an
    `LLMClient`. `graph.state` is allowed and is the distinction that matters: it holds the
    state contract and three pure functions, and imports no agent, no tool and no client
    (which the test above pins).

    The complement is in tests/test_worker.py: the worker imports all three, deliberately.
    """
    imported = _top_level_imports(Path(__file__).resolve().parent.parent / module)
    source = (Path(__file__).resolve().parent.parent / module).read_text(encoding="utf-8")

    assert "agents" not in imported
    assert "tools" not in imported
    assert "llm_client" not in imported
    assert "openai" not in imported
    assert "graph.build" not in source  # `graph.state` is fine; the builder is not
    assert "build_graph" not in source


def test_every_route_answers_with_no_llm_or_tavily_variable_set(
    web: RecordedWeb, graph: ResearchGraph, db: Engine, saver: InMemorySaver, queue: FakeQueue
) -> None:
    """ADR 0012 decision 4's requirement, driven rather than argued.

    The API is built from a configuration with **no** `LLM_*` and no `TAVILY_API_KEY` - the
    environment §13's least-privilege table gives that container - and every route still
    answers. `GET /jobs/{id}/gate` is the one that would fail first if the payload projection
    ever reacquired an agent import, which is why it is exercised with a real gate open rather
    than with a 404.

    The worker in this test still has credentials, because it is a different process. That is
    the whole point of the boundary.
    """
    blind = load_config({})
    assert (blind.llm_api_key, blind.tavily_api_key) == (None, None)

    with TestClient(_application(blind, db, saver, queue)) as client:
        job_id = _at_the_gate(client, graph, db, queue)

        assert client.get("/health").json() == {
            "status": "ok",
            "checks": {"db": True, "redis": True},
        }
        assert client.get(f"/jobs/{job_id}", headers=_SUBMITTER_AUTH).status_code == 200
        assert list(_gate_body(client, job_id)) == _GATE_KEYS
        assert client.get(f"/jobs/{job_id}/report", headers=_SUBMITTER_AUTH).status_code == 404
        decided = client.post(
            f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
        )

    assert decided.status_code == 200
    assert len(queue.sent) == 2


def test_the_route_layer_is_handed_nothing_that_could_execute_a_graph(
    config: Config, db: Engine, saver: InMemorySaver, queue: FakeQueue
) -> None:
    # `RouteDeps` is the whole of what a route can reach, so listing its fields is listing the
    # API's capabilities. A graph or an LLM client appearing here is the regression.
    application = _application(config, db, saver, queue)

    deps = application.state.deps

    assert {field.name for field in fields(deps)} == {
        "config",
        "engine",
        "checkpoints",
        "queue",
        "keys",
        # A health probe with one method, and no cache, no URL set and no rate-limit bucket:
        # the API reaches Redis to report on the workers, never to do their work (step 21).
        "redis",
        # A presigner with one method, and no `put_report`: signing for an object is the
        # API's, writing one is the worker's (step 22a, guidelines §13).
        "artifacts",
    }


# --- Deciding at the gate -----------------------------------------------------------

_EDIT = {"decision": "edit", "edits": "Tighten section two."}
"""The decision most of the tests below send, because it is the one with a bound, a cost, and
a return to the gate - approve and reject end the job and prove less per request."""


def test_a_reviewer_can_approve_and_the_export_runs(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    """The whole approve path across the new boundary: the API records, the worker exports.

    The response says `running` and that is the contract now (ADR 0011 decision 5): the gate is
    answered and the work is queued, and a caller that needs the outcome polls `GET /jobs/{id}`.
    Asserting the export straight off the response would be asserting the Phase 2 behaviour that
    this stage deliberately removed.
    """
    job_id = _at_the_gate(client, graph, db, queue)

    response = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )

    assert response.status_code == 200
    assert response.json() == {"job_id": job_id, "status": "running"}
    assert queries.read_job(db, job_id).status == "running"  # type: ignore[union-attr]

    _work(graph, db, queue)

    row = queries.read_job(db, job_id)
    assert row is not None
    assert row.status == "approved"
    assert row.report_json is not None  # the export gate passed and stored the body
    assert row.exported_at is not None  # and the artifact write succeeded (ADR 0009 dec 1)
    # Both messages acknowledged, and nothing left in flight to be redelivered.
    assert len(queue.deleted) == 2 and queue.in_flight() == []


def test_a_reviewer_can_reject_and_nothing_is_exported(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    job_id = _at_the_gate(client, graph, db, queue)

    response = client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "reject", "note": "the sources are weak"},
        headers=_REVIEWER_AUTH,
    )
    _work(graph, db, queue)

    row = queries.read_job(db, job_id)
    assert response.json()["status"] == "running"
    assert row is not None
    assert row.status == "rejected"
    assert row.report_json is None and row.exported_at is None


def test_the_decision_records_who_made_it_and_which_gate_visit_it_answers(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # "The report was approved" is not worth auditing; "this person approved it" is
    # (guidelines §9). The identity is the key's, and the body cannot override it.
    #
    # `calls_used` is ADR 0007's gate-visit key, and it is the *same* value the `gate_opened`
    # row carries - which is what makes "which opening does this decision answer?" a join.
    job_id = _at_the_gate(client, graph, db, queue)

    client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "approve", "note": "reads well"},
        headers=_REVIEWER_AUTH,
    )

    decided = _decisions(db, job_id)
    assert len(decided) == 1
    assert decided[0].actor == REVIEWER
    assert decided[0].detail == {
        "decision": "approve",
        "note": "reads well",
        "edits": None,
        "calls_used": _CALLS_TO_THE_GATE,
    }
    assert [event.detail["calls_used"] for event in _openings(db, job_id)] == [_CALLS_TO_THE_GATE]


def test_the_gate_opening_stays_a_system_event(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # `gate_opened` is the graph saying it stopped; `reviewer_decision` is a person answering.
    # Two actors, two events, and the API writes only the second (ADR 0006 decision 9).
    job_id = _at_the_gate(client, graph, db, queue)
    client.post(f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH)

    trail = [(event.actor, event.action) for event in queries.read_audit_events(db, job_id)]

    assert ("system", "gate_opened") in trail
    assert (REVIEWER, "reviewer_decision") in trail
    assert trail.index(("system", "gate_opened")) < trail.index((REVIEWER, "reviewer_decision"))


def test_a_second_decision_on_a_job_the_worker_has_finished_is_refused(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # Repeated requests, and concurrent ones: the gate is claimed with a conditional update, so
    # exactly one caller answers it, and once the worker has finished the job there is no gate
    # left to answer at all.
    job_id = _at_the_gate(client, graph, db, queue)
    client.post(f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH)
    _work(graph, db, queue)

    again = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "job_not_awaiting_approval"
    assert len(_decisions(db, job_id)) == 1  # the second one recorded nothing
    assert len(queue.sent) == 2  # and enqueued nothing: one start, one resume


def test_the_same_decision_again_before_the_worker_runs_is_a_retry(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    """The window the asynchronous contract opens, and what ADR 0011 decision 3 puts in it.

    Between the decision and a worker picking it up, the job is `running` with its gate visit
    already answered. A reviewer who sends the identical decision again in that window is
    retrying - it is the recovery path for a resume they cannot see - so it answers `200`,
    writes no second row, and costs no second edit. The deduplication id is the visit key, so
    the retry collapses onto the message already queued instead of queueing a second one.
    """
    job_id = _at_the_gate(client, graph, db, queue)
    client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)

    retry = client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)

    assert retry.status_code == 200
    assert retry.json() == {"job_id": job_id, "status": "running"}
    assert len(_decisions(db, job_id)) == 1
    assert queries.count_reviewer_edits(db, job_id) == 1  # one edit asked for, one counted
    assert len(queue.sent) == 2  # the retry collapsed onto the queued resume


def test_a_job_that_never_reached_the_gate_cannot_be_decided(
    client: TestClient, db: Engine
) -> None:
    job_id = _submitted(client)

    response = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_awaiting_approval"
    assert _decisions(db, job_id) == []


def test_a_decision_the_api_cannot_parse_is_refused(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # An edit with nothing to apply, and a decision outside the three: neither reaches the
    # graph, because guessing which one was meant is the silent wrong answer to refuse.
    job_id = _at_the_gate(client, graph, db, queue)

    for body in ({"decision": "edit"}, {"decision": "export"}):
        response = client.post(f"/jobs/{job_id}/approve", json=body, headers=_REVIEWER_AUTH)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
    assert _decisions(db, job_id) == []


# --- The reviewer edit, and the two bounds that refuse it ---------------------------


def test_an_edit_runs_one_synthesizer_pass_and_comes_back_to_the_gate(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    fake: FakeLLM,
    queue: FakeQueue,
) -> None:
    # Step 17's flow, driven through the API and the queue this time: the endpoint records the
    # edit, the worker runs one pass over the evidence the job already holds, and the job is
    # handed back to the reviewer (ADR 0006, ADR 0011).
    job_id = _at_the_gate(client, graph, db, queue)
    before = fake.roles.count("researcher")

    response = client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": "Tighten section two."},
        headers=_REVIEWER_AUTH,
    )
    _work(graph, db, queue)

    state = graph.get_state(run_config(job_id))
    row = queries.read_job(db, job_id)
    assert response.status_code == 200
    assert response.json()["status"] == "running"  # the work is queued, not done
    assert row is not None and row.status == "awaiting_approval"  # back to the human
    assert state.next == ("human_gate",)
    assert state.values["revision_count"] == 0  # an edit is not a revision
    assert state.values["reviewer_edit_text"] == "Tighten section two."
    assert fake.roles.count("researcher") == before  # no research, ever, from an edit
    assert web.queries == list(_SUBTOPICS)
    assert len(_decisions(db, job_id)) == 1


def test_an_edit_beyond_the_bound_is_refused_before_the_graph_runs(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    fake: FakeLLM,
    queue: FakeQueue,
) -> None:
    # MAX_REVIEWER_EDITS = 3, counted from the trail this endpoint writes.
    job_id = _at_the_gate(client, graph, db, queue)
    _pretend_edits(db, job_id, count=3)
    spent = len(fake.requests)

    response = client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": "One more thing."},
        headers=_REVIEWER_AUTH,
    )

    row = queries.read_job(db, job_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reviewer_edit_limit_reached"
    assert len(fake.requests) == spent  # no LLM call was made
    assert len(_decisions(db, job_id)) == 3  # no fourth decision was recorded
    assert row is not None and row.status == "awaiting_approval"  # the gate is still open


def test_an_edit_the_budget_cannot_fund_is_refused_before_the_graph_runs(
    web: RecordedWeb, db: Engine, fake: FakeLLM, saver: InMemorySaver, queue: FakeQueue
) -> None:
    # The job has spent 16 of its 18 calls, so two remain and an edit needs three.
    config = load_config({**_ENV, "MAX_LLM_CALLS_PER_JOB": "18"})
    graph = build_graph(
        config=config,
        llm=LLMClient(config, client=cast(OpenAI, fake)),
        db=db,
        checkpointer=saver,
    )
    with TestClient(_application(config, db, saver, queue)) as client:
        job_id = _at_the_gate(client, graph, db, queue)
        spent = len(fake.requests)

        response = client.post(
            f"/jobs/{job_id}/approve",
            json={"decision": "edit", "edits": "Tighten section two."},
            headers=_REVIEWER_AUTH,
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_call_budget_for_edit"
    assert len(fake.requests) == spent
    assert _decisions(db, job_id) == []


def test_the_budget_check_reads_the_checkpoint_and_not_the_jobs_row(
    web: RecordedWeb, db: Engine, fake: FakeLLM, saver: InMemorySaver, queue: FakeQueue
) -> None:
    """The decisive test for ADR 0006 decision 7, with the two numbers disagreeing.

    `jobs.llm_calls_used` is written by `finalize`, so a job waiting at the gate has `0` in
    that column while its checkpoint knows what it really spent. A budget check written
    against the row would compute a full budget and allow every edit, silently - so the two
    are made to disagree here and the refusal has to follow the checkpoint.
    """
    config = load_config({**_ENV, "MAX_LLM_CALLS_PER_JOB": "18"})
    graph = build_graph(
        config=config,
        llm=LLMClient(config, client=cast(OpenAI, fake)),
        db=db,
        checkpointer=saver,
    )
    with TestClient(_application(config, db, saver, queue)) as client:
        job_id = _at_the_gate(client, graph, db, queue)

        row = queries.read_job(db, job_id)
        live = graph.get_state(run_config(job_id)).values["llm_calls_used"]
        response = client.post(
            f"/jobs/{job_id}/approve",
            json={"decision": "edit", "edits": "Tighten section two."},
            headers=_REVIEWER_AUTH,
        )

    assert row is not None
    assert row.llm_calls_used == 0  # the row: stale by design
    assert live == _CALLS_TO_THE_GATE  # the checkpoint: the truth
    assert response.status_code == 409  # and the refusal followed the checkpoint


def test_a_reviewer_can_still_approve_a_job_whose_edits_are_exhausted(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # A refusal is about the edit, not about the reviewer: approve and reject cost nothing
    # and stay available, which is what keeps a bounded job finishable.
    job_id = _at_the_gate(client, graph, db, queue)
    _pretend_edits(db, job_id, count=3)

    response = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )
    _work(graph, db, queue)

    assert response.status_code == 200
    assert _status(db, job_id) == "approved"


def _pretend_edits(db: Engine, job_id: str, *, count: int) -> None:
    """Reviewer decisions this endpoint would have written, without spending a job to get them.

    Inserted directly because what is under test is the bound, not the three edits: each real
    one would cost a Synthesizer, a Fact-Checker and a reflection pass.
    """
    for _ in range(count):
        with db.begin() as conn:
            conn.execute(
                sa.insert(audit_events).values(
                    job_id=job_id,
                    actor=REVIEWER,
                    action="reviewer_decision",
                    detail={"decision": "edit", "note": None, "edits": "earlier"},
                )
            )


# --- The reviewer's own text, cleaned at the edge (ADR 0006 decision 8) -------------
#
# Reviewer text is authenticated input, so it is deliberately *not* wrapped in an untrusted
# block - it reaches the Synthesizer's prompt as an instruction to follow. That is what makes
# cleaning it load-bearing rather than tidy: a field the model obeys is the field a control
# character must not survive in, and the same string is written to `audit_events.detail`. Both
# of guidelines §16's destinations - a prompt and the database - are on this path.


def _last_draft_instructions(fake: FakeLLM) -> str:
    """What the Synthesizer was told to act on, with the fetched pages removed.

    The reviewer's words live outside the untrusted block by design (ADR 0006 decision 8), so
    reading only that half proves the cleaned text arrived as an *instruction* rather than
    merely appearing somewhere in a prompt that also quotes whole web pages.
    """
    return outside_untrusted_blocks(fake.requests_for("synthesizer")[-1].user)


def test_control_characters_are_stripped_from_the_reviewer_instruction(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    fake: FakeLLM,
    queue: FakeQueue,
) -> None:
    job_id = _at_the_gate(client, graph, db, queue)
    dirty = "Tighten" + chr(0) + " section" + chr(0x200B) + " two" + chr(10) + " please."

    response = client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": dirty, "note": "looks" + chr(0) + " thin"},
        headers=_REVIEWER_AUTH,
    )
    # The worker is what reaches the Synthesizer, and it reads the reviewer's text out of the
    # audit trail rather than off the message - so this is also what proves the cleaned value
    # is the one that survives the crossing (ADR 0011 decision 2).
    _work(graph, db, queue)

    assert response.status_code == 200
    decided = _decisions(db, job_id)[0]
    assert decided.detail["edits"] == "Tighten section two please."
    assert decided.detail["note"] == "looks thin"
    # The cleaned value is the one the model was shown, not the raw one.
    assert "Tighten section two please." in _last_draft_instructions(fake)
    assert chr(0) not in fake.requests_for("synthesizer")[-1].user


def test_whitespace_in_the_reviewer_instruction_is_collapsed(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    fake: FakeLLM,
    queue: FakeQueue,
) -> None:
    job_id = _at_the_gate(client, graph, db, queue)

    client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": "  Tighten    section \t two.  "},
        headers=_REVIEWER_AUTH,
    )
    _work(graph, db, queue)

    assert _decisions(db, job_id)[0].detail["edits"] == "Tighten section two."
    assert "Tighten section two." in _last_draft_instructions(fake)


@pytest.mark.parametrize("field", ["edits", "note"])
def test_reviewer_text_longer_than_the_cap_is_refused(
    field: str,
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    fake: FakeLLM,
    queue: FakeQueue,
) -> None:
    # The same cap the question gets, for the same reason: this text reaches a prompt and a
    # row. `note` is capped too - it does not reach a prompt, but it does reach the database.
    job_id = _at_the_gate(client, graph, db, queue)
    body = {"decision": "edit", "edits": "Tighten section two.", field: "a" * 501}
    spent = len(fake.requests)

    response = client.post(f"/jobs/{job_id}/approve", json=body, headers=_REVIEWER_AUTH)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert set(response.json()["error"]) == {"code", "message", "job_id"}
    assert len(fake.requests) == spent
    assert _decisions(db, job_id) == []


def test_reviewer_text_at_the_cap_is_accepted(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    # The boundary is inclusive, like the question's.
    job_id = _at_the_gate(client, graph, db, queue)

    response = client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": "a" * MAX_REVIEWER_TEXT_CHARS},
        headers=_REVIEWER_AUTH,
    )

    assert response.status_code == 200
    assert _decisions(db, job_id)[0].detail["edits"] == "a" * MAX_REVIEWER_TEXT_CHARS


def test_an_edit_that_is_only_control_characters_is_refused(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    fake: FakeLLM,
    queue: FakeQueue,
) -> None:
    """The gap between the model's rule and the cleaned value.

    `GateDecision` refuses an edit whose `edits` is empty, and `"\\x00\\x00".strip()` is not
    empty - `str.strip()` removes whitespace, and a NUL is not whitespace. So this body passes
    validation and cleans down to nothing, which is an edit with no instruction: a Synthesizer
    pass that would cost three calls and produce the same draft again.
    """
    job_id = _at_the_gate(client, graph, db, queue)
    spent = len(fake.requests)

    response = client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": chr(0) + chr(0x200B)},
        headers=_REVIEWER_AUTH,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert len(fake.requests) == spent
    assert _decisions(db, job_id) == []


def test_refused_reviewer_text_records_no_decision_and_resumes_nothing(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    fake: FakeLLM,
    queue: FakeQueue,
) -> None:
    # The cleaning runs before the job is even read, so a refusal costs the job nothing at all
    # and leaves the gate exactly as it was - approve and reject stay available.
    job_id = _at_the_gate(client, graph, db, queue)
    spent = len(fake.requests)

    client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": "a" * 501},
        headers=_REVIEWER_AUTH,
    )

    snapshot = graph.get_state(run_config(job_id))
    assert snapshot.next == ("human_gate",)
    assert snapshot.interrupts  # the graph never moved
    assert _status(db, job_id) == "awaiting_approval"
    assert _decisions(db, job_id) == []
    assert len(fake.requests) == spent
    assert len(queue.sent) == 1  # the start message only: no resume, so no worker was spent
    # Approve and reject stay available, exactly as they do after an edit the bounds refuse.
    still_open = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )
    _work(graph, db, queue)
    assert still_open.status_code == 200
    assert _status(db, job_id) == "approved"


# --- One decision per gate visit, and a resume that dies (ADR 0007, ADR 0011) --------
#
# Every test below starts from the same measured failure: a reviewer sends `edit`, the graph
# resumes, and a database write inside the Synthesizer raises. What used to happen then was
# three separate wrongs - the row said `awaiting_approval` while the checkpoint sat at
# `synthesizer` with no interrupt, the reviewer got a bare `Internal Server Error`, and the
# retry that fixed the job wrote a second decision and spent a second edit.
#
# **The failure has moved processes, and every rule survived the move.** The resume is the
# worker's now (ADR 0011), so the reviewer never sees the 500 - what they see is a `200` and a
# job that stops moving. ADR 0007's four cases, its reconcile table and its one-row-per-visit
# rule are unchanged; what changed is who runs the `finally`, and that **redelivery, not the
# reviewer, is now the recovery path**. The retry stays safe rather than necessary, which is
# what these tests are here to hold.


def _fail_the_synthesizer_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break the one write the Synthesizer node makes, and nothing else.

    The agent still succeeds, so the failure lands exactly where ADR 0007's probe put it:
    after the gate node has completed and the graph has moved on, which is the window where
    the row and the checkpoint used to disagree.
    """

    def raise_instead(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("INSERT INTO claims ...", {}, Exception("connection lost"))

    monkeypatch.setattr(queries, "record_claims", raise_instead)


def _failed_edit(
    client: TestClient,
    graph: ResearchGraph,
    monkeypatch: pytest.MonkeyPatch,
    db: Engine,
    queue: FakeQueue,
) -> str:
    """A job whose reviewer `edit` was recorded and whose resume then died **in the worker**.

    The endpoint answers `200` and enqueues; the worker picks the resume up, the Synthesizer's
    write raises, and the message is left undeleted because ADR 0010 decision 6 deletes on
    three outcomes and this is none of them.
    """
    job_id = _at_the_gate(client, graph, db, queue)
    accepted = client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)
    assert accepted.status_code == 200

    _fail_the_synthesizer_write(monkeypatch)
    _work(graph, db, queue)
    monkeypatch.undo()
    return job_id


def test_a_failed_resume_leaves_the_row_and_the_checkpoint_agreeing(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0007's headline divergence, asserted from both sides, in the process that owns it.

    The checkpoint is mid-edit-pass with no interrupt to answer, so `awaiting_approval` is a
    lie: it tells a poller a human is holding the job, and it lets a second reviewer past the
    status check and into the same thread. `running` is what is actually true, and the worker's
    `finally` is what writes it (ADR 0011 decision 4).
    """
    job_id = _failed_edit(client, graph, monkeypatch, db, queue)

    snapshot = graph.get_state(run_config(job_id))
    assert snapshot.next == ("synthesizer",)
    assert not snapshot.interrupts  # nobody is being waited on
    assert _status(db, job_id) == "running"  # and the row now says so
    assert len(_decisions(db, job_id)) == 1  # the human did decide, and that stands
    # And the message is still there, which is what makes this recoverable without a reviewer.
    assert queue.deleted == [queue.sent[0].deduplication_id]  # the start message, and only it
    assert [held.deduplication_id for held in queue.in_flight()] == [queue.sent[1].deduplication_id]


def test_a_resume_that_dies_before_the_gate_node_completes_leaves_the_gate_open(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    queue: FakeQueue,
) -> None:
    """The other half of ADR 0007's reconcile table, and the half only the reconcile fixes.

    `claim_gate` has already moved the row to `running`, and then the gate node itself fails
    on the replay - so the interrupt is still pending and a human really is still holding this
    job. `running` would be the wrong answer here for the same reason `awaiting_approval` is
    the wrong answer after the node completes: the status is derived from the checkpoint, not
    asserted beside it.

    The predicate is the pending interrupt rather than `next`, which is what makes these two
    failures distinguishable at all - both report `next == ("human_gate",)` at some point.
    """
    job_id = _at_the_gate(client, graph, db, queue)

    def raise_instead(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("UPDATE jobs ...", {}, Exception("connection lost"))

    client.post(f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH)
    monkeypatch.setattr(queries, "record_gate_opened", raise_instead)
    _work(graph, db, queue)
    monkeypatch.undo()

    snapshot = graph.get_state(run_config(job_id))
    assert snapshot.next == ("human_gate",)
    assert snapshot.interrupts  # the graph is still stopped for a person
    assert _status(db, job_id) == "awaiting_approval"  # so the row says so again
    assert len(_decisions(db, job_id)) == 1  # and the decision stands
    # Redelivery is the recovery: the same message, the same decision, no reviewer involved.
    assert queue.redeliver() == 1
    _work(graph, db, queue)
    assert _status(db, job_id) == "approved"
    assert len(_decisions(db, job_id)) == 1


def test_an_unexpected_failure_returns_the_documented_error_envelope(
    web: RecordedWeb,
    unraising_client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catch-all envelope, on a failure the API can still have.

    "One shape, everywhere" is only true if the framework's default 500 cannot leak through it
    (guidelines §12, §16). A stack trace, an internal path, or the failing SQL in a response
    body is the leak the catch-all exists to stop - the reason goes to the log.

    The failure is a database one now rather than a resume: since ADR 0011 the endpoint records
    and enqueues, so writing the decision is the step that can still die under it.
    """
    job_id = _at_the_gate(unraising_client, graph, db, queue)

    def raise_instead(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("INSERT INTO audit_events ...", {}, Exception("connection lost"))

    monkeypatch.setattr(queries, "record_reviewer_decision", raise_instead)
    response = unraising_client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)
    monkeypatch.undo()

    body = response.json()
    assert response.status_code == 500
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "job_id"}
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["job_id"] == job_id
    for leaked in ("Traceback", "INSERT INTO", "sqlalchemy", "connection lost", "audit_events"):
        assert leaked not in response.text
    assert len(queue.sent) == 1  # and nothing was enqueued for a decision that was not recorded


def test_redelivery_finishes_the_job_and_costs_no_second_edit(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovery path, which is now the queue's rather than the reviewer's.

    `count_reviewer_edits` counts rows, so a second one would spend one of ADR 0006's three
    edits on an edit that never happened - the consequence a reviewer would actually feel. The
    worker writes no decision row at all, so redelivery cannot produce one.
    """
    job_id = _failed_edit(client, graph, monkeypatch, db, queue)

    assert queue.redeliver() == 1
    _work(graph, db, queue)

    assert _status(db, job_id) == "awaiting_approval"  # the edit pass finished
    assert len(_decisions(db, job_id)) == 1
    assert queries.count_reviewer_edits(db, job_id) == 1  # one edit asked for, one counted
    assert queue.in_flight() == []  # and the message was acknowledged this time


def test_the_reviewer_retrying_after_a_failed_resume_changes_nothing(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0007 invariant 2 across the new boundary: safe, and no longer load-bearing.

    A reviewer who cannot see the worker sends the same decision again, and it must remain what
    it was: no second row, no second edit, and a `200`. What it no longer has to be is the fix -
    the message the worker did not delete is. The deduplication id is the visit key, so the
    retry does not even add a message.
    """
    job_id = _failed_edit(client, graph, monkeypatch, db, queue)

    retry = client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)

    assert retry.status_code == 200
    assert retry.json() == {"job_id": job_id, "status": "running"}
    assert len(_decisions(db, job_id)) == 1
    assert queries.count_reviewer_edits(db, job_id) == 1
    assert len(queue.sent) == 2  # start and one resume: the retry collapsed onto the resume


def test_a_redelivery_resumes_only_the_node_that_was_in_flight(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    fake: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A redelivery invokes the same thread, so LangGraph replays from the last checkpoint. The
    # cost is bounded by the single node that was in flight - which is the whole reason this
    # record needs no compensating action: there is nothing to undo, only something to finish.
    job_id = _failed_edit(client, graph, monkeypatch, db, queue)
    before = {role: fake.roles.count(role) for role in ("planner", "researcher", "synthesizer")}
    searched = list(web.queries)

    queue.redeliver()
    _work(graph, db, queue)

    after = {role: fake.roles.count(role) for role in before}
    assert after["planner"] == before["planner"]
    assert after["researcher"] == before["researcher"]
    assert after["synthesizer"] == before["synthesizer"] + 1  # only the node that was in flight
    assert web.queries == searched  # and no search was re-issued
    assert web.queries == list(_SUBTOPICS)
    assert _status(db, job_id) == "awaiting_approval"


def test_a_different_decision_on_a_decided_visit_is_refused(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A reviewer cannot change their mind during a failure. That is a real cost, accepted:
    # the alternative is two decisions racing on one thread (ADR 0007 invariant 3).
    job_id = _failed_edit(client, graph, monkeypatch, db, queue)

    response = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "gate_already_decided"
    assert response.json()["error"]["job_id"] == job_id
    assert len(_decisions(db, job_id)) == 1  # nothing was written
    assert _status(db, job_id) == "running"  # and nothing was enqueued to change it
    assert len(queue.sent) == 2


def test_the_gate_replay_does_not_reopen_the_gate(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    queue: FakeQueue,
) -> None:
    """`claim_gate`'s `running` has to survive the whole resume, not milliseconds of it.

    LangGraph re-runs the interrupted gate node from the top, so `record_gate_opened` executes
    again on the way *out* of the gate. With its status write outside the already-guarded
    branch, that handed the row straight back to `awaiting_approval` - and a second reviewer
    arriving mid-pass would pass the status check, claim, and invoke the same thread.

    The status is read from inside the Synthesizer node, which is the first thing to run after
    the gate node completes: that is the moment the old code got wrong.
    """
    seen: list[str] = []
    original = queries.record_claims

    def watch(*args: Any, **kwargs: Any) -> None:
        seen.append(_status(db, cast(str, kwargs["job_id"])))
        original(*args, **kwargs)

    job_id = _at_the_gate(client, graph, db, queue)
    monkeypatch.setattr(queries, "record_claims", watch)

    response = client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)
    _work(graph, db, queue)

    assert response.status_code == 200
    assert seen == ["running"]  # the gate stayed claimed for the whole edit pass
    assert _status(db, job_id) == "awaiting_approval"  # and the *next* visit opened it again
    assert len(_openings(db, job_id)) == 2


def test_two_gate_visits_on_one_job_carry_different_keys(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, queue: FakeQueue
) -> None:
    """The uniqueness the visit key rests on, asserted rather than argued.

    It is a property of the topology, not of the counter: `human_gate` is reachable only from
    `reflection`, and a second draft to score costs a Synthesizer, a Supervisor hop, a
    Fact-Checker and a reflection pass. A future edge that reached the gate without spending a
    call would collide two visits onto one key silently - so it fails here first.

    It is also what keeps the two **resume messages** apart: `MessageDeduplicationId` is that
    same key (ADR 0010 decision 4), so two visits colliding would mean SQS silently discarding
    the second decision's message.
    """
    job_id = _at_the_gate(client, graph, db, queue)
    client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)
    _work(graph, db, queue)

    client.post(f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH)
    _work(graph, db, queue)

    openings = [event.detail["calls_used"] for event in _openings(db, job_id)]
    decided = [event.detail["calls_used"] for event in _decisions(db, job_id)]
    assert len(openings) == 2
    assert openings[0] < openings[1]  # strictly greater at the next visit
    assert decided == openings  # each decision answers the opening it was sent to
    resumes = [message.deduplication_id for message in queue.sent[1:]]
    assert resumes == [f"{job_id}:{key}" for key in openings]  # two visits, two messages
    assert _status(db, job_id) == "approved"


# --- The key table itself -----------------------------------------------------------


def test_the_key_table_is_validated_once_at_startup() -> None:
    table = load_api_keys(json.dumps({hash_key("k"): {"user_id": REVIEWER, "role": "reviewer"}}))

    assert table[hash_key("k")] == Identity(user_id=REVIEWER, role="reviewer")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        "[]",
        "{}",
        json.dumps({"abc": {"user_id": REVIEWER}}),
        json.dumps({"abc": {"user_id": REVIEWER, "role": "admin"}}),
    ],
)
def test_an_unusable_key_table_stops_the_process_rather_than_every_request(
    raw: str | None,
) -> None:
    # A service that cannot authenticate anyone should fail to boot. Refusing every caller
    # instead would look like an outage nobody can explain.
    with pytest.raises(AuthConfigError):
        load_api_keys(raw)


def test_the_table_holds_hashes_and_never_a_key() -> None:
    # An operator reading the environment, a log line, or a crash dump finds a digest.
    raw = json.dumps({hash_key("super-secret"): {"user_id": REVIEWER, "role": "reviewer"}})

    assert "super-secret" not in raw
    assert identity_from("Bearer super-secret", load_api_keys(raw)) is not None
    assert identity_from("Bearer wrong", load_api_keys(raw)) is None
