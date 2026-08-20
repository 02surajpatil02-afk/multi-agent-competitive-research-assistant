"""
WHY THIS FILE EXISTS
    tests/test_worker.py proves what the worker does with a message. It cannot prove what SQS
    does with one - and four of ADR 0010's decisions are queue *attributes* rather than
    application code:

      * FIFO with `MessageGroupId = job_id`, which is what keeps one job to one writer
        (decision 4) and therefore what lets ADR 0005's `_write_findings` read then write;
      * `MessageDeduplicationId`, which is what makes a reviewer's retry one message
        rather than two;
      * `ApproximateReceiveCount`, which is the only number that can count redeliveries
        because a message body is immutable (decision 3);
      * `maxReceiveCount = 3` and the redrive policy, which is where "three deliveries, then
        the dead-letter queue" is actually enforced (decision 6).

    `FakeQueue` models all four, and a model of a guarantee is not the guarantee. So these run
    against the real thing: LocalStack's SQS from `docker-compose.yml`, through the same
    `jobqueue.build_queue()` the worker and the API use, with `endpoint_url` as the only
    difference from AWS.

    **Two kinds of test, deliberately not mixed.** The first few read the queue Compose
    *declares*, because that is the lease the worker renews. The rest create a throwaway queue
    with a one-second visibility timeout, so redelivery and lease-extension tests take seconds
    rather than half an hour.

    **No AWS credentials, and no AWS.** The fixture supplies the placeholder credentials boto3
    insists on and LocalStack ignores; nothing here can reach a real account, because
    `endpoint_url` is where every request goes.

WHO CALLS IT
    `pytest -m integration`, with `SQS_ENDPOINT_URL` set. Unset, every test here skips, which
    is what keeps plain `pytest` offline (guidelines §18).
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
import pytest
from dbharness import migrated_engine, new_job_id
from harness import (
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
from langgraph.checkpoint.memory import InMemorySaver
from openai import OpenAI
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

import operations
from config import Config, load_config
from database import queries
from graph.build import ResearchGraph, build_graph
from graph.state import run_config, state_serde
from jobqueue import JobQueue, build_queue, is_fifo, max_receive_count, visibility_timeout
from llm_client import LLMClient
from worker import WorkerDeps, check_queue, handle

pytestmark = pytest.mark.integration

ENDPOINT_VARIABLE = "SQS_ENDPOINT_URL"

SKIP_REASON = (
    f"{ENDPOINT_VARIABLE} is not set. Start the local infrastructure with "
    "`docker compose up -d --wait`, then set it to http://localhost:4566."
)

COMPOSE_QUEUE = "research-jobs.fifo"
COMPOSE_DLQ = "research-jobs-dlq.fifo"
"""The names `docker-compose.yml` passes to the bootstrap script. Restated rather than parsed
out of the compose file, because what is under test is that the queue LocalStack actually has
matches the design - and reading the same file the queue was built from would agree with it
however it changed."""

REGION = "ap-south-1"
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

USER = "22222222-2222-4222-8222-222222222222"

SHORT_VISIBILITY_S = 1
"""How long a delivery stays invisible on a throwaway queue. Production's is 1800s and derived
(ADR 0010 decision 8); this one exists so that "the message came back" is a fact a test can
wait for. Nothing here runs a real node, so there is nothing for a longer one to protect."""

RECEIVE_TIMEOUT_S = 15.0
"""How long a helper waits for a message that should be there. Long enough that a slow
container is not a failure, short enough that a message that never arrives is."""


# --- Reaching LocalStack ----------------------------------------------------------------


_REAL_GETADDRINFO = socket.getaddrinfo
"""Captured before anything fakes DNS, so one host can be let back through it below."""


@pytest.fixture(autouse=True)
def localstack(monkeypatch: pytest.MonkeyPatch) -> str:
    """Where LocalStack is, and the credentials boto3 insists on before it will talk to it.

    The credentials are set here rather than read from the environment, so this suite can never
    depend on - or accidentally use - a real AWS profile. `AWS_PROFILE` is cleared for the same
    reason: a developer with one configured must not have it consulted.

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


@pytest.fixture
def sqs(localstack: str) -> Any:
    """A raw client, for the two things the boundary deliberately does not expose: creating a
    queue, and asking "is it empty?" without a twenty-second long poll."""
    return boto3.client("sqs", region_name=REGION, endpoint_url=localstack)


def _queue_url(sqs: Any, name: str) -> str:
    return cast(str, sqs.get_queue_url(QueueName=name)["QueueUrl"])


@pytest.fixture
def compose_queue(sqs: Any, localstack: str) -> JobQueue:
    """The queue `docker compose up` created, reached the way the worker reaches it."""
    return build_queue(_queue_url(sqs, COMPOSE_QUEUE), region=REGION, endpoint_url=localstack)


@pytest.fixture
def throwaway(sqs: Any, localstack: str) -> Iterator[tuple[JobQueue, str, str]]:
    """A queue of this test's own, shaped like the real one but quick to redeliver.

    A queue per test rather than a shared one that is purged between them: SQS's dedup window
    is five minutes and its purge is rate-limited, so two tests sharing a queue would silently
    depend on the order they ran in.
    """
    suffix = uuid4().hex[:12]
    dlq_url = sqs.create_queue(
        QueueName=f"probe-{suffix}-dlq.fifo", Attributes={"FifoQueue": "true"}
    )["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    url = sqs.create_queue(
        QueueName=f"probe-{suffix}.fifo",
        Attributes={
            "FifoQueue": "true",
            "VisibilityTimeout": str(SHORT_VISIBILITY_S),
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"}),
        },
    )["QueueUrl"]

    yield build_queue(url, region=REGION, endpoint_url=localstack), url, dlq_url

    for created in (url, dlq_url):
        sqs.delete_queue(QueueUrl=created)


def _receive(queue: JobQueue) -> Any:
    """One message, waited for. The wait is the test's, not the queue's long poll."""
    deadline = time.monotonic() + RECEIVE_TIMEOUT_S
    while time.monotonic() < deadline:
        message = queue.receive()
        if message is not None:
            return message
    raise AssertionError("no message arrived within the timeout")


def _peek(sqs: Any, url: str) -> list[Any]:
    """Whatever is immediately visible, without a long poll. Used to assert emptiness, which
    is the one question a twenty-second wait answers no better than a zero-second one."""
    response = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=1, WaitTimeSeconds=0)
    return cast(list[Any], response.get("Messages") or [])


def _wait_for_the_visibility_timeout() -> None:
    """What redelivery costs when it is real. The queue's own timeout, plus a margin for the
    container's clock rather than ours."""
    time.sleep(SHORT_VISIBILITY_S + 0.5)


# --- 1. The queue Compose declares ------------------------------------------------------


def test_the_job_queue_and_its_dead_letter_queue_both_exist(sqs: Any) -> None:
    # A mount that names a missing directory is not an error, so a bootstrap script that never
    # ran looks exactly like one that did until something asks for the queue.
    assert _queue_url(sqs, COMPOSE_QUEUE)
    assert _queue_url(sqs, COMPOSE_DLQ)


def test_the_job_queue_is_fifo_and_so_is_its_dead_letter_queue(
    sqs: Any, compose_queue: JobQueue
) -> None:
    """ADR 0010 decision 4, on the real queue rather than in the name.

    FIFO can only be set at creation, so this is not a setting that can be corrected later -
    which is why the bootstrap script creates-if-absent rather than creating and ignoring the
    error, and why this is worth asserting rather than assuming.
    """
    assert is_fifo(compose_queue.attributes())

    dlq = sqs.get_queue_attributes(QueueUrl=_queue_url(sqs, COMPOSE_DLQ), AttributeNames=["All"])[
        "Attributes"
    ]
    assert is_fifo(dlq)


def test_the_job_queue_gives_up_after_three_deliveries(compose_queue: JobQueue) -> None:
    # ARCHITECTURE.md §11's "three deliveries, then the dead-letter queue", expressed where
    # SQS enforces it rather than where application code would have to.
    attributes = compose_queue.attributes()

    assert max_receive_count(attributes) == 3
    assert COMPOSE_DLQ in json.loads(attributes["RedrivePolicy"])["deadLetterTargetArn"]


def test_the_worker_would_start_against_the_queue_compose_creates(
    compose_queue: JobQueue,
) -> None:
    """The worker derives its heartbeat from the attributes the queue actually has."""
    settings = check_queue(compose_queue)

    assert settings.final_delivery_at == 3
    assert settings.visibility_timeout_s == 1800
    assert visibility_timeout(compose_queue.attributes()) == settings.visibility_timeout_s


# --- 2. What the boundary puts on the wire ----------------------------------------------


def test_a_start_message_makes_the_round_trip_carrying_its_three_identifiers(
    throwaway: tuple[JobQueue, str, str], sqs: Any
) -> None:
    queue, url, _ = throwaway
    job_id = new_job_id()

    queue.send_start(job_id=job_id, user_id=USER, idempotency_key="key-1")

    message = _receive(queue)
    assert (message.job_id, message.user_id, message.idempotency_key) == (job_id, USER, "key-1")
    assert message.receive_count == 1  # the first delivery is delivery one
    assert message.receipt_handle  # and it is what `delete` needs

    queue.delete(message)
    assert _peek(sqs, url) == []


def test_the_message_group_is_the_job_id_so_one_job_has_one_writer(
    throwaway: tuple[JobQueue, str, str], sqs: Any
) -> None:
    """The single-writer guarantee, demonstrated rather than asserted from an attribute.

    This is the property ADR 0005's `_write_findings` is allowed to assume, and the reason
    ADR 0010 decision 4 calls FIFO load-bearing: while one message for a job is in flight, SQS
    will not hand out another from the same group, whatever mix of starts, resumes, retries and
    redeliveries produced them. A standard queue would deliver both at once and break it
    silently - which is what makes this worth a real queue.
    """
    queue, url, _ = throwaway
    job_id = new_job_id()
    queue.send_start(job_id=job_id, user_id=USER, idempotency_key="key-1")
    queue.send_resume(job_id=job_id, user_id=USER, idempotency_key="key-1", calls_used=16)

    first = _receive(queue)

    assert _peek(sqs, url) == []  # the group is blocked while the first is in flight
    queue.delete(first)
    second = _receive(queue)
    assert second.job_id == job_id  # and only then does the next one arrive


def test_two_jobs_are_not_blocked_by_each_other(
    throwaway: tuple[JobQueue, str, str], sqs: Any
) -> None:
    # The other half of the same guarantee: the serialisation is per job, not per queue, so one
    # slow job does not stop every other one. Two groups, two messages, both available at once.
    queue, url, _ = throwaway
    first, second = new_job_id(), new_job_id()
    queue.send_start(job_id=first, user_id=USER, idempotency_key="key-1")
    queue.send_start(job_id=second, user_id=USER, idempotency_key="key-2")

    held = _receive(queue)

    assert [message["Body"] for message in _peek(sqs, url)] != []  # the other job is available
    assert held.job_id in (first, second)


def test_a_retried_decision_inside_the_window_produces_one_message(
    throwaway: tuple[JobQueue, str, str], sqs: Any
) -> None:
    """ADR 0010 decision 4's second row: the gate-visit key deduplicates the queue too.

    The reviewer's retry is ADR 0007's recovery path, and it must not queue a second resume for
    the same visit. `MessageDeduplicationId = f"{job_id}:{calls_used}"` is what makes that a
    queue guarantee instead of an application check.
    """
    queue, url, _ = throwaway
    job_id = new_job_id()

    for _ in range(3):
        queue.send_resume(job_id=job_id, user_id=USER, idempotency_key="key-1", calls_used=16)

    first = _receive(queue)
    queue.delete(first)
    assert _peek(sqs, url) == []  # the two retries were accepted and discarded


def test_two_gate_visits_produce_two_messages(
    throwaway: tuple[JobQueue, str, str],
) -> None:
    # The uniqueness the visit key rests on, at the queue: a second visit costs a Synthesizer,
    # a Fact-Checker and a reflection pass, so `calls_used` is strictly greater and the
    # deduplication id is different. Two visits colliding would mean SQS silently discarding
    # the second reviewer's decision.
    queue, _, _ = throwaway
    job_id = new_job_id()

    queue.send_resume(job_id=job_id, user_id=USER, idempotency_key="key-1", calls_used=16)
    queue.send_resume(job_id=job_id, user_id=USER, idempotency_key="key-1", calls_used=22)

    first = _receive(queue)
    queue.delete(first)
    second = _receive(queue)
    queue.delete(second)


def test_a_start_and_a_resume_for_one_job_are_never_deduplicated_together(
    throwaway: tuple[JobQueue, str, str],
) -> None:
    # The start dedupes on the job's idempotency key and a resume on the visit key, so they
    # cannot collide - which matters because they travel in the same message group.
    queue, _, _ = throwaway
    job_id = new_job_id()

    queue.send_start(job_id=job_id, user_id=USER, idempotency_key="key-1")
    queue.send_resume(job_id=job_id, user_id=USER, idempotency_key="key-1", calls_used=16)

    first = _receive(queue)
    queue.delete(first)
    second = _receive(queue)
    queue.delete(second)


# --- 3. Delivery, redelivery, and the dead-letter queue ----------------------------------


def test_a_message_that_is_not_deleted_comes_back_with_a_higher_delivery_count(
    throwaway: tuple[JobQueue, str, str],
) -> None:
    """At-least-once, and the number that counts it.

    `attempt` is deliberately not in the body (ADR 0010 decision 3) because an SQS body is
    immutable once sent, so a field inside it cannot count redeliveries.
    `ApproximateReceiveCount` can, and this is where that claim is checked against SQS rather
    than against a fake.
    """
    queue, _, _ = throwaway
    job_id = new_job_id()
    queue.send_start(job_id=job_id, user_id=USER, idempotency_key="key-1")

    first = _receive(queue)
    _wait_for_the_visibility_timeout()
    second = _receive(queue)

    assert (first.receive_count, second.receive_count) == (1, 2)
    assert first.job_id == second.job_id
    queue.delete(second)


def test_visibility_extension_keeps_the_message_owned_then_disappearance_allows_redelivery(
    throwaway: tuple[JobQueue, str, str], sqs: Any
) -> None:
    """The real ChangeMessageVisibility boundary and the heartbeat-disappearance recovery."""
    queue, url, _ = throwaway
    job_id = new_job_id()
    queue.send_start(job_id=job_id, user_id=USER, idempotency_key="key-1")

    first = _receive(queue)
    queue.extend_visibility(first, visibility_timeout_s=3)

    time.sleep(1.5)  # beyond the queue's original one-second visibility
    assert _peek(sqs, url) == []

    time.sleep(2.0)  # no further heartbeat: the renewed lease now expires
    second = _receive(queue)
    assert second.receive_count == 2
    assert second.job_id == first.job_id
    queue.delete(second)


def test_a_message_reaches_the_dead_letter_queue_after_the_third_delivery(
    throwaway: tuple[JobQueue, str, str], sqs: Any
) -> None:
    """`maxReceiveCount = 3`, enforced by the queue rather than by the worker.

    The worker's part is only to *not delete* - and this is what the queue then does with the
    message. Which is also why ADR 0010 decision 9 has the final delivery finalise the job and
    still leave the message: the job stops being pollable, and the alarm on DLQ depth fires.
    """
    queue, url, dlq_url = throwaway
    job_id = new_job_id()
    queue.send_start(job_id=job_id, user_id=USER, idempotency_key="key-1")

    for delivery in range(1, 4):
        assert _receive(queue).receive_count == delivery
        _wait_for_the_visibility_timeout()

    deadline = time.monotonic() + RECEIVE_TIMEOUT_S
    dead: list[Any] = []
    while time.monotonic() < deadline and not dead:
        _peek(sqs, url)  # the receive attempt that moves it is what SQS acts on
        dead = _peek(sqs, dlq_url)

    assert [json.loads(message["Body"])["job_id"] for message in dead] == [job_id]


# --- 3a. Phase 5 block C: reading and recovering a dead-letter queue -----------------------
#
# Three properties that a fake can only model. LocalStack is not real SQS either, but it is the
# real API and the real queue attributes, which is where all three of these live.


def test_the_dead_letter_queue_is_resolved_from_the_redrive_policy(
    throwaway: tuple[JobQueue, str, str],
) -> None:
    """The block C tooling never takes a second `SQS_DLQ_URL` variable: it reads the queue's own
    `RedrivePolicy`, turns the target ARN into a name, and asks SQS for the URL.

    A duplicate setting is a setting that can disagree with the queue it describes, and an
    operator pointed at the wrong dead-letter queue would draw conclusions from someone else's
    messages (ADR 0021 decision 6).
    """
    queue, _url, dlq_url = throwaway

    resolved = queue.dead_letter_queue()
    assert resolved is not None
    assert resolved.attributes()["QueueArn"].endswith(dlq_url.rsplit("/", 1)[-1])


def test_a_queue_with_no_redrive_policy_has_no_dead_letter_queue(sqs: Any, localstack: str) -> None:
    """None is a real answer, and the tooling treats it as "nothing can be shown to be
    orphaned" rather than as an error - which is the safe direction for that absence."""
    suffix = uuid4().hex[:12]
    url = sqs.create_queue(QueueName=f"lonely-{suffix}.fifo", Attributes={"FifoQueue": "true"})[
        "QueueUrl"
    ]
    try:
        assert build_queue(url, region=REGION, endpoint_url=localstack).dead_letter_queue() is None
    finally:
        sqs.delete_queue(QueueUrl=url)


def test_inspecting_a_dead_letter_queue_leaves_every_message_on_it(
    throwaway: tuple[JobQueue, str, str], localstack: str
) -> None:
    """**SQS has no peek**, so reading is what hides a message - and this is the property no
    single-process fake can be wrong about in the right way. The messages must be visible again
    to a *different* client immediately afterwards, because the thing that has to keep seeing
    them is the DLQ-depth alarm.
    """
    queue, _url, dlq_url = throwaway
    dead_letters = build_queue(dlq_url, region=REGION, endpoint_url=localstack)

    for index in range(3):
        job_id = new_job_id()
        dead_letters.send_start(job_id=job_id, user_id=USER, idempotency_key=f"key-{index}")

    found = operations.read_dead_letter_messages(dead_letters, limit=10)
    assert len(found) == 3
    assert all(message.group_id and message.deduplication_id for message in found), (
        "the FIFO attributes a replay needs were not asked for"
    )

    # A second reader, immediately: the release really happened rather than the timeout lapsing.
    again = operations.read_dead_letter_messages(
        build_queue(dlq_url, region=REGION, endpoint_url=localstack), limit=10
    )
    assert {message.job_id for message in again} == {message.job_id for message in found}


def test_a_replayed_message_keeps_the_group_and_deduplication_id_it_was_sent_with(
    throwaway: tuple[JobQueue, str, str], localstack: str
) -> None:
    """The whole of ADR 0021 decision 6's "the message that goes back is the message that came
    out", against a real FIFO queue.

    A resume message's deduplication id is ADR 0007's gate-visit key, and minting a new one
    would mean a visit could be answered twice. The five-minute window is not in the way here
    for the reason the decision gives: a dead-lettered message is at least three deliveries old,
    and this one has never been on the jobs queue at all.
    """
    queue, url, dlq_url = throwaway
    dead_letters = build_queue(dlq_url, region=REGION, endpoint_url=localstack)

    job_id = new_job_id()
    dead_letters.send_resume(
        job_id=job_id, user_id=USER, idempotency_key=f"key-{job_id}", calls_used=7
    )
    # **Read while holding, exactly as `scripts/replay_dlq.py` does.** An inspection releases
    # each message with a zero visibility timeout, and a released receipt handle is no longer
    # in flight - so the delete below would be refused. That is why the two readers differ, and
    # this is where the difference is real rather than modelled.
    (dead,) = dead_letters.receive_batch(
        max_messages=10, wait_seconds=1, visibility_timeout_s=operations.REPLAY_VISIBILITY_S
    )
    assert dead.deduplication_id == f"{job_id}:7"

    queue.resend(dead)
    dead_letters.delete(dead)

    # Read back through `receive_batch`, because `receive()` deliberately does not ask SQS for
    # the FIFO attributes - the worker routes on durable state and has no use for either.
    (replayed,) = queue.receive_batch(
        max_messages=10, wait_seconds=SHORT_VISIBILITY_S, visibility_timeout_s=SHORT_VISIBILITY_S
    )
    assert replayed.job_id == job_id
    assert replayed.group_id == job_id
    assert replayed.deduplication_id == f"{job_id}:7"


# --- 4. A real worker, a real queue ------------------------------------------------------


def _sentence(tag: str) -> str:
    return f"Source {tag} reported cloud revenue of $1.2bn in FY24."


def _page(tag: str) -> Page:
    return Page(
        url=f"https://source-{tag}.example/report",
        title=f"Source {tag}",
        text=f"{_sentence(tag)}\nThe rest of the page is boilerplate.",
    )


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch, localstack: str) -> RecordedWeb:
    """The recorded web, plus the one host it must not refuse.

    `RecordedWeb.install()` replaces `socket.getaddrinfo` with a fake that raises for every host
    it was not told about - which is what keeps the offline suite offline (guidelines §18) - and
    boto3 resolves through that same function, for a literal address as much as for a name. So
    LocalStack is let back through **by name**, and nothing else is: the queue is real, the web
    is recorded, and that is exactly the boundary this file exists to test.
    """
    recorded = RecordedWeb()
    for index, question in enumerate(_SUBTOPICS, 1):
        recorded.index(question, _page(f"{index}a"), _page(f"{index}b"))
    recorded.install(monkeypatch)

    faked = socket.getaddrinfo
    allowed = urlsplit(localstack).hostname

    def resolve(host: str, *args: Any, **kwargs: Any) -> Any:
        if host == allowed:
            return _REAL_GETADDRINFO(host, *args, **kwargs)
        return faked(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return recorded


@pytest.fixture
def config() -> Config:
    return load_config(_ENV)


@pytest.fixture
def db(tmp_path: Path) -> Engine:
    return migrated_engine(tmp_path)


@pytest.fixture
def fake() -> FakeLLM:
    return FakeLLM(
        supervisor=[
            decision("planner"),
            *[decision("researcher")] * 3,
            decision("synthesizer"),
            decision("fact_checker"),
        ],
        planner=[plan(*_SUBTOPICS)] * 2,
        researcher=[quote_the_page()] * 8,
        synthesizer=[draft(1), draft(2)],
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * 2,
        reflection=[rubric()] * 2,
    )


@pytest.fixture
def graph(config: Config, fake: FakeLLM, db: Engine) -> ResearchGraph:
    return build_graph(
        config=config,
        llm=LLMClient(config, client=cast(OpenAI, fake)),
        db=db,
        checkpointer=InMemorySaver(serde=state_serde()),
    )


def _queued(db: Engine, queue: JobQueue) -> str:
    job_id = new_job_id()
    key = f"key-{job_id}"
    queries.create_job(
        db, job_id=job_id, user_id=USER, question=_QUESTION, idempotency_key=key, actor=USER
    )
    queue.send_start(job_id=job_id, user_id=USER, idempotency_key=key)
    return job_id


def _deps(config: Config, db: Engine, graph: ResearchGraph, queue: JobQueue) -> WorkerDeps:
    return WorkerDeps(config=config, engine=db, graph=graph, queue=queue, final_delivery_at=3)


def test_a_worker_runs_a_job_from_a_real_message_and_acknowledges_it(
    web: RecordedWeb,
    config: Config,
    db: Engine,
    graph: ResearchGraph,
    throwaway: tuple[JobQueue, str, str],
    sqs: Any,
) -> None:
    """The whole Stage 2 path with SQS in it: a row, a message, a worker, a gate.

    Everything except the queue is the offline suite's - SQLite, the FakeLLM, the recorded web -
    because what this adds is the one component `FakeQueue` was standing in for.
    """
    queue, url, _ = throwaway
    job_id = _queued(db, queue)

    handle(_deps(config, db, graph, queue), _receive(queue))

    row = queries.read_job(db, job_id)
    assert row is not None and row.status == "awaiting_approval"
    assert graph.get_state(run_config(job_id)).interrupts
    _wait_for_the_visibility_timeout()
    assert _peek(sqs, url) == []  # deleted, so it does not come back


def test_a_worker_that_fails_leaves_the_message_for_the_queue_to_redeliver(
    web: RecordedWeb,
    config: Config,
    db: Engine,
    graph: ResearchGraph,
    throwaway: tuple[JobQueue, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redelivery as the retry, end to end: the worker declines to delete and SQS does the rest.

    The failure is a database write, which is the one that genuinely escapes a node - the agents
    turn an LLM failure into `status="failed"` themselves, and `database/queries.py` deliberately
    catches nothing.
    """
    queue, _, _ = throwaway
    deps = _deps(config, db, graph, queue)
    job_id = _queued(db, queue)
    original = queries.record_plan
    broken = [True]

    def maybe_raise(*args: Any, **kwargs: Any) -> Any:
        if broken[0]:
            broken[0] = False
            raise OperationalError("record_plan ...", {}, Exception("connection lost"))
        return original(*args, **kwargs)

    monkeypatch.setattr(queries, "record_plan", maybe_raise)
    handle(deps, _receive(queue))
    row = queries.read_job(db, job_id)
    assert row is not None and row.status == "running"  # reconciled, not left at the gate

    _wait_for_the_visibility_timeout()
    redelivered = _receive(queue)
    handle(deps, redelivered)

    assert redelivered.receive_count == 2
    row = queries.read_job(db, job_id)
    assert row is not None and row.status == "awaiting_approval"
    assert queries.read_audit_events(db, job_id)[1].action == "plan_produced"


def test_the_final_real_delivery_finalises_the_job_and_leaves_it_for_the_dlq(
    web: RecordedWeb,
    config: Config,
    db: Engine,
    graph: ResearchGraph,
    throwaway: tuple[JobQueue, str, str],
    sqs: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0010 decision 9's two requirements, against a queue that really does dead-letter.

    The job becomes terminal so a submitter stops polling it, and the message is left so it
    reaches the DLQ and the alarm fires. Deleting it would satisfy the first and silence the
    second, which is why they are asserted together.
    """
    queue, url, dlq_url = throwaway
    deps = _deps(config, db, graph, queue)
    job_id = _queued(db, queue)
    monkeypatch.setattr(
        queries,
        "record_plan",
        lambda *_a, **_k: (_ for _ in ()).throw(
            OperationalError("record_plan ...", {}, Exception("connection lost"))
        ),
    )

    for delivery in range(1, 4):
        message = _receive(queue)
        assert message.receive_count == delivery
        handle(deps, message)
        _wait_for_the_visibility_timeout()

    row = queries.read_job(db, job_id)
    assert row is not None
    assert row.status == "failed" and row.completed_at is not None
    assert graph.get_state(run_config(job_id)).values["failure_reason"] == "job_dead_lettered"

    deadline = time.monotonic() + RECEIVE_TIMEOUT_S
    dead: list[Any] = []
    while time.monotonic() < deadline and not dead:
        _peek(sqs, url)
        dead = _peek(sqs, dlq_url)
    assert [json.loads(message["Body"])["job_id"] for message in dead] == [job_id]
