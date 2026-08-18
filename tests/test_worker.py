"""
WHY THIS FILE EXISTS
    The worker is the process that decides what a message means, and every one of those
    decisions is a durable-state question rather than a message-shape question (ADR 0010
    decision 5). That makes it exactly the kind of code where a plausible implementation and a
    correct one are hard to tell apart by reading, so this file drives the real `handle()` and
    the real `run()` against the real graph, with only the model and the web replaced.

    Five groups, in the order a failure hurts.

    **A message is deleted on three outcomes and on nothing else** (ADR 0010 decision 6). Every
    other path leaves it, and redelivery is the retry - so the tests below assert what is *not*
    deleted at least as often as what is. `FakeQueue` holds a received message in flight until
    a test calls `redeliver()`, which is the visibility timeout made explicit.

    **The checkpoint discriminates start, resume and continue.** The three branches are driven
    by putting the checkpoint in each of the three states and sending the same pointer message
    at it, because that is the whole design: no message type, no attempt field, no state on the
    wire.

    **A redelivery resumes; it never restarts.** The evidence is the audit trail and the
    FakeLLM's own call log - one plan, one set of findings, and only the node that was in
    flight running twice.

    **The two bounds that end a job the graph did not end itself** - the per-invocation runtime
    bound and the final delivery - both write a `failure_reason` and both differ in what they
    do with the message. The runtime bound deletes; the final delivery deliberately does not,
    so the DLQ alarm still fires (decision 9).

    **Shutdown finishes what it is holding.** The signal is raised for real, mid-invocation,
    and the assertion is that the invocation completed, its message was acknowledged, and the
    next one was never received.

WHO CALLS IT
    pytest. No service, no network: SQLite for the rows, `InMemorySaver` for the checkpoints,
    and `FakeQueue` for SQS. The same properties against real SQS are in
    tests/test_queue_localstack.py, which is marked `integration`.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from dbharness import migrated_engine, new_job_id
from fakes import FakeQueue, FakeS3
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
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from artifacts import ArtifactStore
from config import Config, load_config
from database import queries
from database.schema import jobs
from graph.build import ResearchGraph, build_graph
from graph.state import run_config, state_serde
from jobqueue import JobMessage, QueueError
from llm_client import LLMClient
from worker import (
    BACKOFF_SECONDS,
    RECEIVE_ERROR_PAUSE_S,
    Shutdown,
    WorkerDeps,
    check_queue,
    check_redis,
    handle,
    required_credentials,
    required_visibility_timeout,
    run,
)

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_FAST_MODEL": "fast-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
    "DATABASE_URL": "postgresql://user:pw@localhost:5432/research",
    "SQS_QUEUE_URL": "https://sqs.ap-south-1.amazonaws.com/1/research-jobs.fifo",
    "S3_BUCKET": "research-reports",
}

_QUESTION = "Compare TCS and Infosys on cloud strategy."
_SUBTOPICS = (
    "What is TCS cloud revenue?",
    "What is Infosys cloud revenue?",
    "How do their cloud partnerships compare?",
)

USER = "22222222-2222-4222-8222-222222222222"
REVIEWER = "11111111-1111-4111-8111-111111111111"

FINAL_DELIVERY_AT = 3
"""`maxReceiveCount` on the local queue (docker-compose.yml), so the numbers here and the
numbers `docker compose up` creates are the same three."""


# --- The worker under test ------------------------------------------------------------


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
def config() -> Config:
    return load_config(_ENV)


@pytest.fixture
def fake() -> FakeLLM:
    """One clean job to the gate, then enough for a reviewer edit and a retry after a failure.

    The extra drafts and hops are what a redelivery costs: the node that was in flight runs
    again, and an answer nobody asks for costs a test nothing.
    """
    return FakeLLM(
        supervisor=[
            decision("planner"),
            *[decision("researcher")] * 3,
            decision("synthesizer"),
            *[decision("fact_checker")] * 4,
            decision("synthesizer"),
            decision("finalize"),
        ],
        planner=[plan(*_SUBTOPICS)] * 4,
        researcher=[quote_the_page()] * 8,
        synthesizer=[draft(1), draft(2), draft(3), draft(4)],
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * 4,
        reflection=[rubric()] * 4,
    )


@pytest.fixture
def saver() -> InMemorySaver:
    return InMemorySaver(serde=state_serde())


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def bucket() -> FakeS3:
    """The report bucket, in memory. Wrapped in the real `ArtifactStore` below, so the key,
    the retry schedule and the JSON body under test are production's."""
    return FakeS3()


@pytest.fixture
def graph(
    config: Config, fake: FakeLLM, db: Engine, saver: InMemorySaver, bucket: FakeS3
) -> ResearchGraph:
    return build_graph(
        config=config,
        llm=LLMClient(config, client=cast(OpenAI, fake)),
        db=db,
        # The worker is the process that writes an artifact (step 22a), so the graph these
        # tests drive has one - otherwise an approved job would end with `exported_at` NULL
        # and nothing here would exercise the write the real worker performs.
        artifacts=ArtifactStore("research-reports", client=bucket),
        checkpointer=saver,
    )


@pytest.fixture
def deps(config: Config, db: Engine, graph: ResearchGraph, queue: FakeQueue) -> WorkerDeps:
    return WorkerDeps(
        config=config,
        engine=db,
        graph=graph,
        queue=cast(Any, queue),
        final_delivery_at=FINAL_DELIVERY_AT,
    )


# --- What the API would have left behind ----------------------------------------------


def _queued(db: Engine, queue: FakeQueue, *, question: str = _QUESTION) -> str:
    """A job row and its start message, exactly as `POST /jobs` leaves them.

    Written through the same statements the endpoint uses rather than through the endpoint,
    because what is under test here is what the worker does with the result - and a test that
    booted a FastAPI application to produce two rows would be testing the wrong boundary.
    """
    job_id = new_job_id()
    key = f"key-{job_id}"
    queries.create_job(
        db, job_id=job_id, user_id=USER, question=question, idempotency_key=key, actor=USER
    )
    queue.send_start(job_id=job_id, user_id=USER, idempotency_key=key)
    return job_id


def _decide(db: Engine, graph: ResearchGraph, job_id: str, **decided: Any) -> None:
    """A reviewer decision, recorded and claimed the way the endpoint records and claims it,
    then enqueued as a resume - the three steps ADR 0011 decision 1 leaves behind."""
    calls_used = _calls_used(graph, job_id)
    assert queries.claim_gate(db, job_id)
    queries.record_reviewer_decision(
        db,
        job_id=job_id,
        actor=REVIEWER,
        decision=decided.get("decision", "approve"),
        note=decided.get("note"),
        edits=decided.get("edits"),
        calls_used=calls_used,
    )


def _resume(queue: FakeQueue, graph: ResearchGraph, job_id: str) -> None:
    queue.send_resume(
        job_id=job_id,
        user_id=USER,
        idempotency_key=f"key-{job_id}",
        calls_used=_calls_used(graph, job_id),
    )


def _calls_used(graph: ResearchGraph, job_id: str) -> int:
    """ADR 0007's gate-visit key, derived from the checkpoint - the identical computation the
    endpoint performs, which is the point of ADR 0011 decision 2."""
    return cast(int, graph.get_state(run_config(job_id)).values["llm_calls_used"])


def _drain(deps: WorkerDeps, queue: FakeQueue) -> int:
    """Handle everything visible, once each, and say how many messages that was."""
    handled = 0
    while (message := queue.receive()) is not None:
        handle(deps, message)
        handled += 1
    return handled


def _status(db: Engine, job_id: str) -> str:
    row = queries.read_job(db, job_id)
    assert row is not None
    return cast(str, row.status)


def _actions(db: Engine, job_id: str) -> list[str]:
    return [event.action for event in queries.read_audit_events(db, job_id)]


def _break_write(monkeypatch: pytest.MonkeyPatch, name: str, *, times: int) -> None:
    """Make one `queries` write raise for its next `times` calls, then work again.

    A database write is the failure that genuinely escapes a node: the agents turn an LLM
    failure into `status="failed"` themselves, and `database/queries.py` deliberately does not
    catch anything (guidelines §17's 0 retries, fail loudly). So this is what "the worker died
    mid-run" looks like from inside a test, and `times` is what says how many deliveries it
    survives.
    """
    original = getattr(queries, name)
    remaining = [times]

    def maybe_raise(*args: Any, **kwargs: Any) -> Any:
        if remaining[0] > 0:
            remaining[0] -= 1
            raise OperationalError(f"{name} ...", {}, Exception("connection lost"))
        return original(*args, **kwargs)

    monkeypatch.setattr(queries, name, maybe_raise)


# --- 1. A new job ---------------------------------------------------------------------


def test_a_queued_job_starts_runs_to_the_gate_and_the_message_is_deleted(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue, fake: FakeLLM
) -> None:
    """The whole start path: `queued -> running`, the graph, and the acknowledgement.

    The status the row ends on is `awaiting_approval` rather than `running`, and that is the
    reconcile rather than the gate node alone - both write it, and ADR 0011 decision 4 makes
    the worker the process that derives it from the checkpoint on the way out.
    """
    job_id = _queued(db, queue)

    assert _drain(deps, queue) == 1

    assert _status(db, job_id) == "awaiting_approval"
    assert queue.deleted == [f"key-{job_id}"]  # the graph interrupted: outcome one of three
    assert queue.in_flight() == []
    assert fake.roles[:2] == ["supervisor", "planner"]  # it really ran, from the beginning
    assert _actions(db, job_id) == [
        "job_created",
        "plan_produced",
        *["subtopic_researched"] * 3,
        "gate_opened",
    ]


def test_the_worker_is_the_only_thing_that_moves_a_job_out_of_queued(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue
) -> None:
    """ADR 0010 decisions 1 and 2, read off the row at the one moment it can be.

    `running` is written on receipt and before the first invocation, so the window in which a
    job says `queued` is exactly the queue wait. The read is taken from inside the first node
    the graph runs, which is the only place in a test that is *during* the invocation.
    """
    seen: list[str] = []
    original = queries.record_plan

    def watch(*args: Any, **kwargs: Any) -> None:
        seen.append(_status(db, cast(str, kwargs["job_id"])))
        original(*args, **kwargs)

    job_id = _queued(db, queue)
    assert _status(db, job_id) == "queued"  # the API's value, and nothing has run it

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(queries, "record_plan", watch)
        _drain(deps, queue)

    assert seen == ["running"]
    assert "job_started" not in _actions(db, job_id)  # decision 2: no such audit action


def test_a_duplicate_delivery_does_not_run_the_job_a_second_time(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue, fake: FakeLLM
) -> None:
    """At-least-once delivery, answered by the checkpoint rather than by a dedupe table.

    The second delivery of a start message finds a checkpoint holding a pending interrupt, so
    it is a resume - and with no decision on record there is nothing to resume with, which is
    the one case ADR 0011 decision 2 says to leave loudly rather than guess at. What matters
    here is the negative: no second plan, no second search, no second set of findings.
    """
    job_id = _queued(db, queue)
    message = queue.receive()
    assert message is not None
    handle(deps, message)
    spent, searched = list(fake.roles), list(web.queries)
    findings = len(queries.read_findings(db, job_id))

    handle(deps, message)  # the same message again, as SQS may deliver it

    assert fake.roles == spent
    assert web.queries == searched
    assert len(queries.read_findings(db, job_id)) == findings
    assert _actions(db, job_id).count("plan_produced") == 1
    assert _status(db, job_id) == "awaiting_approval"


def test_a_message_naming_a_job_with_no_row_is_deleted_rather_than_cycled(
    deps: WorkerDeps, queue: FakeQueue
) -> None:
    # The row is committed before the message is sent (ADR 0010 decision 10), so this cannot
    # be a race - it is an unanswerable message, and three deliveries of it would only delay
    # the DLQ it was always going to reach.
    orphan = JobMessage(
        job_id=new_job_id(),
        user_id=USER,
        idempotency_key="key",
        receipt_handle="handle",
        receive_count=1,
    )

    handle(deps, orphan)

    assert queue.deleted == ["handle"]


def test_a_message_for_a_finished_job_is_deleted_without_invoking_anything(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue, fake: FakeLLM
) -> None:
    # Outcome three of ADR 0010 decision 6: already terminal when the message arrived. A
    # redelivered resume after the job ended is the ordinary way this happens.
    job_id = _queued(db, queue)
    _drain(deps, queue)
    _decide(db, deps.graph, job_id, decision="approve")
    _resume(queue, deps.graph, job_id)
    message = queue.receive()
    assert message is not None
    handle(deps, message)
    assert _status(db, job_id) == "approved"
    spent = list(fake.roles)

    handle(deps, message)  # the same resume delivered again, after the job ended

    assert fake.roles == spent  # nothing was invoked
    assert queue.deleted == [f"key-{job_id}", message.receipt_handle, message.receipt_handle]
    assert queue.in_flight() == []


# --- 2. The human gate, resumed -------------------------------------------------------


def test_a_paused_job_resumes_from_the_decision_on_record(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue, bucket: FakeS3
) -> None:
    """ADR 0011's headline: the decision is read from the audit trail, keyed by the visit.

    Nothing about the decision travelled on the wire, and the worker never re-derived the key
    from the message - it read `llm_calls_used` off the same checkpoint it used to discover
    that this was a resume at all.
    """
    job_id = _queued(db, queue)
    _drain(deps, queue)
    _decide(db, deps.graph, job_id, decision="approve", note="reads well")
    _resume(queue, deps.graph, job_id)

    assert _drain(deps, queue) == 1

    row = queries.read_job(db, job_id)
    assert row is not None
    assert row.status == "approved"
    # The body is durable and the artifact exists, which is what `exported_at` now means
    # (ADR 0009 decision 1) - and the object is really in the bucket, not merely claimed.
    assert row.report_json is not None and row.exported_at is not None
    assert bucket.body(f"reports/{job_id}.json") == row.report_json
    assert queue.in_flight() == []


def test_the_resume_message_carries_identifiers_and_never_the_decision(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue
) -> None:
    # §20 row 8, checked on the wire rather than argued: the reviewer's own words reach the
    # Synthesizer's prompt, and they do it through Postgres.
    job_id = _queued(db, queue)
    _drain(deps, queue)
    _decide(db, deps.graph, job_id, decision="edit", edits="Tighten section two.", note="thin")
    _resume(queue, deps.graph, job_id)

    body = queue.sent[-1].body

    assert set(body) == {"job_id", "user_id", "idempotency_key"}
    assert "Tighten section two." not in str(body)
    assert "thin" not in str(body)


def test_an_edit_reaches_the_synthesizer_with_the_reviewers_text(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue, fake: FakeLLM
) -> None:
    """The load-bearing half of ADR 0011 decision 2: the worker rebuilds the exact decision.

    An `edit` is the decision that carries text, so it is the one that proves the text
    survived the crossing - and `reviewer_edit_text` on the checkpoint is what the next
    Synthesizer pass reads.
    """
    job_id = _queued(db, queue)
    _drain(deps, queue)
    researched = fake.roles.count("researcher")
    _decide(db, deps.graph, job_id, decision="edit", edits="Tighten section two.")
    _resume(queue, deps.graph, job_id)

    _drain(deps, queue)

    assert "Tighten section two." in fake.requests_for("synthesizer")[-1].user
    assert fake.roles.count("researcher") == researched  # an edit never researches
    assert _status(db, job_id) == "awaiting_approval"  # and it comes back to the gate
    assert _actions(db, job_id).count("gate_opened") == 2


def test_the_worker_writes_no_reviewer_decision_of_its_own(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue
) -> None:
    # Only the authenticated endpoint writes one, because only it knows who decided
    # (guidelines §9, §16). A worker that wrote one on resume would make a redelivery spend
    # one of ADR 0006's three edits.
    job_id = _queued(db, queue)
    _drain(deps, queue)
    _decide(db, deps.graph, job_id, decision="edit", edits="Tighten section two.")
    _resume(queue, deps.graph, job_id)

    _drain(deps, queue)
    queue.redeliver()
    _drain(deps, queue)

    assert _actions(db, job_id).count("reviewer_decision") == 1
    assert queries.count_reviewer_edits(db, job_id) == 1


def test_a_resume_for_a_visit_with_no_decision_is_left_for_redelivery(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue, fake: FakeLLM
) -> None:
    """ADR 0011 decision 2's loud gap: no decision row means no guess.

    Guessing which of the three decisions was meant is exactly the silent wrong answer
    `human_gate_node` already refuses when a resume payload does not validate. Leaving the
    message means the endpoint may have caught up by the time it comes back.
    """
    job_id = _queued(db, queue)
    _drain(deps, queue)
    _resume(queue, deps.graph, job_id)  # a resume nobody decided
    spent = list(fake.roles)

    _drain(deps, queue)

    assert fake.roles == spent  # the graph was not invoked
    assert len(queue.in_flight()) == 1  # and the message is still there
    assert _status(db, job_id) == "awaiting_approval"  # the gate is still open


def test_previously_completed_nodes_are_not_restarted_by_a_resume(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue, fake: FakeLLM
) -> None:
    # The reason the durable checkpointer exists: an approval two days later costs the export
    # and nothing else (guidelines §4, §10).
    job_id = _queued(db, queue)
    _drain(deps, queue)
    before = {role: fake.roles.count(role) for role in ("planner", "researcher", "synthesizer")}
    searched = list(web.queries)
    _decide(db, deps.graph, job_id, decision="approve")
    _resume(queue, deps.graph, job_id)

    _drain(deps, queue)

    assert {role: fake.roles.count(role) for role in before} == before
    assert web.queries == searched


# --- 3. A delivery that died mid-run --------------------------------------------------


def test_a_delivery_that_dies_leaves_the_message_and_reconciles_the_row(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure path, from both ends: nothing is acknowledged, and the row is not left lying.

    `running` is what the checkpoint says - no interrupt is pending - and the reconcile in the
    `finally` is what writes it whether the invocation returned or raised (ADR 0011 decision 4).
    """
    job_id = _queued(db, queue)
    _break_write(monkeypatch, "record_plan", times=1)

    assert _drain(deps, queue) == 1

    assert queue.deleted == []
    assert len(queue.in_flight()) == 1
    assert _status(db, job_id) == "running"
    assert not deps.graph.get_state(run_config(job_id)).interrupts


def test_a_redelivery_continues_from_the_checkpoint_rather_than_restarting(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    fake: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0010 decision 5's fourth branch - a checkpoint with no pending interrupt.

    This is the case no message shape could have captured, and the one `invoke(None)` exists
    for. The failure is placed inside the first Researcher visit, so "continued" and
    "restarted" produce visibly different evidence: a restart would re-plan and search every
    subtopic again.

    **What a redelivery does cost is the node that was in flight, and that is admitted rather
    than hidden:** the first subtopic is searched twice, because LangGraph re-runs an
    uncheckpointed node from the top and no search cache is wired in this test. One node, not
    a job - which is the whole reason ADR 0007 needed no compensating action.
    """
    job_id = _queued(db, queue)
    _break_write(monkeypatch, "record_research", times=1)
    _drain(deps, queue)
    assert len(web.queries) == 1  # it died inside the first Researcher visit

    assert queue.redeliver() == 1
    _drain(deps, queue)

    assert _status(db, job_id) == "awaiting_approval"
    assert web.queries == [_SUBTOPICS[0], *_SUBTOPICS]  # the one in flight, then all three
    assert fake.roles.count("planner") == 1  # and the plan was not made again
    assert _actions(db, job_id).count("plan_produced") == 1
    assert queue.deleted == [f"key-{job_id}"]


def test_a_node_that_died_before_its_checkpoint_runs_again_and_converges(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    fake: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0005's keyed writes, exercised by the redelivery they were written for.

    The Researcher node wrote its rows and then died before LangGraph checkpointed it, so the
    node runs again on the next delivery and mints the same finding ids (ADR 0003). The
    database has to end up agreeing with the checkpoint rather than holding an orphan under a
    live id - which is what `_write_findings`' insert-or-refresh is for.
    """
    job_id = _queued(db, queue)

    original = queries.record_research
    died = [False]

    def write_then_die(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)  # the rows land
        if not died[0]:  # and then the node fails, before the checkpoint
            died[0] = True
            raise OperationalError("record_research ...", {}, Exception("connection lost"))

    monkeypatch.setattr(queries, "record_research", write_then_die)
    _drain(deps, queue)
    queue.redeliver()
    _drain(deps, queue)

    rows = queries.read_findings(db, job_id)
    state = deps.graph.get_state(run_config(job_id)).values
    assert _status(db, job_id) == "awaiting_approval"
    assert [row.finding_id for row in rows] == [finding.finding_id for finding in state["findings"]]
    # The visit that died is honestly recorded twice: `audit_events` is append-only, and a
    # node that really did run twice truthfully has two rows (ADR 0005).
    assert _actions(db, job_id).count("subtopic_researched") == 4


def test_a_checkpoint_with_a_pending_interrupt_is_never_continued_blindly(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue, fake: FakeLLM
) -> None:
    """The discrimination itself, stated as the property that keeps `invoke(None)` honest.

    `invoke(None)` on a job stopped at the gate would carry it past a human without a
    decision. The predicate is the pending interrupt, so the same pointer message means
    "continue" in one checkpoint state and "resume" in the other, and never the wrong one.
    """
    job_id = _queued(db, queue)
    _drain(deps, queue)
    assert deps.graph.get_state(run_config(job_id)).interrupts
    spent = list(fake.roles)

    queue.send_resume(job_id=job_id, user_id=USER, idempotency_key="k", calls_used=999)
    _drain(deps, queue)

    assert fake.roles == spent  # no node ran: there is no decision for this visit
    assert _status(db, job_id) == "awaiting_approval"
    assert len(queue.in_flight()) == 1


# --- 4. The two bounds that end a job the graph did not end -----------------------------


def test_the_runtime_bound_fails_the_job_between_nodes_and_deletes_the_message(
    web: RecordedWeb, db: Engine, graph: ResearchGraph, queue: FakeQueue, fake: FakeLLM
) -> None:
    """ADR 0010 decision 7, with the bound set to zero so it trips at the first boundary.

    The bound is per invocation and checked *between* nodes, which is why the assertion is
    about `failure_reason` and the terminal row rather than about how far the graph got: a
    node already in flight is inside a blocking request and is not interrupted.
    """
    bounded = load_config({**_ENV, "MAX_JOB_RUNTIME": "0"})
    deps = WorkerDeps(
        config=bounded,
        engine=db,
        graph=graph,
        queue=cast(Any, queue),
        final_delivery_at=FINAL_DELIVERY_AT,
    )
    job_id = _queued(db, queue)

    _drain(deps, queue)

    state = graph.get_state(run_config(job_id)).values
    assert state["failure_reason"] == "job_timeout"
    assert _status(db, job_id) == "failed"
    assert queue.deleted == [f"key-{job_id}"]  # terminal is one of the three delete outcomes
    assert queue.in_flight() == []


def test_nothing_further_is_spent_after_the_runtime_bound_trips(
    web: RecordedWeb, db: Engine, graph: ResearchGraph, queue: FakeQueue, fake: FakeLLM
) -> None:
    """ADR 0010 decision 7's other requirement, which is about cost rather than status.

    A bound that ends the job and then runs one more node has not bounded anything a budget
    can feel. The count is taken the instant the bound trips and again after the job is
    terminal, and nothing may come between them.
    """
    bounded = load_config({**_ENV, "MAX_JOB_RUNTIME": "0"})
    deps = WorkerDeps(
        config=bounded,
        engine=db,
        graph=graph,
        queue=cast(Any, queue),
        final_delivery_at=FINAL_DELIVERY_AT,
    )
    job_id = _queued(db, queue)

    _drain(deps, queue)
    spent = list(fake.roles)
    queue.redeliver()  # nothing is left to redeliver, but a duplicate could still arrive
    _drain(deps, queue)

    assert spent == ["supervisor"]  # one node ran, the bound tripped at its boundary
    assert fake.roles == spent  # and the finalise added nothing
    assert _status(db, job_id) == "failed"


def test_the_job_is_terminal_before_its_message_is_acknowledged(
    web: RecordedWeb, db: Engine, graph: ResearchGraph, queue: FakeQueue
) -> None:
    """The ordering that makes the runtime bound recoverable rather than a hole.

    If the message were deleted first and the finalise then died, the job would sit `running`
    forever with nothing left to redeliver. The read is taken inside `delete`, which is the
    only place that can say which happened first.
    """
    bounded = load_config({**_ENV, "MAX_JOB_RUNTIME": "0"})
    seen: list[str] = []
    job_id = _queued(db, queue)
    original = queue.delete

    def watch(message: Any) -> None:
        seen.append(_status(db, job_id))
        original(message)

    deps = WorkerDeps(
        config=bounded,
        engine=db,
        graph=graph,
        queue=cast(Any, queue),
        final_delivery_at=FINAL_DELIVERY_AT,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(queue, "delete", watch)
        _drain(deps, queue)

    assert seen == ["failed"]


def test_the_final_delivery_finalises_the_job_and_still_lets_it_reach_the_dlq(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0010 decision 9, and the reason it is two requirements rather than one.

    The job becomes terminal so a poller stops waiting for it, **and** the message is left so
    it reaches the dead-letter queue and the alarm on DLQ depth fires. Deleting it would empty
    the queue and silence the alarm that says something is broken.
    """
    job_id = _queued(db, queue)
    _break_write(monkeypatch, "record_plan", times=FINAL_DELIVERY_AT)

    for delivery in range(FINAL_DELIVERY_AT):
        assert _drain(deps, queue) == 1
        assert queue.deleted == []
        if delivery < FINAL_DELIVERY_AT - 1:
            assert queue.redeliver() == 1

    state = deps.graph.get_state(run_config(job_id)).values
    assert state["failure_reason"] == "job_dead_lettered"
    assert _status(db, job_id) == "failed"
    assert len(queue.in_flight()) == 1  # left for the DLQ, deliberately


def test_a_delivery_before_the_last_one_is_not_finalised(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other edge of the same rule: a failure with deliveries left is a retry, not an
    # outcome, and a job failed on delivery one could never be recovered by delivery two.
    job_id = _queued(db, queue)
    _break_write(monkeypatch, "record_plan", times=1)

    _drain(deps, queue)

    state = deps.graph.get_state(run_config(job_id)).values
    assert state["failure_reason"] is None
    assert _status(db, job_id) == "running"


def test_a_queue_with_no_redrive_policy_never_treats_a_delivery_as_the_last(
    web: RecordedWeb,
    config: Config,
    db: Engine,
    graph: ResearchGraph,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # None is a real answer: without a redrive policy a message is redelivered forever and
    # there is no DLQ to finalise a job into, so finalising it would be inventing an outcome.
    deps = WorkerDeps(
        config=config, engine=db, graph=graph, queue=cast(Any, queue), final_delivery_at=None
    )
    job_id = _queued(db, queue)
    _break_write(monkeypatch, "record_plan", times=10)

    for _ in range(4):
        _drain(deps, queue)
        queue.redeliver()

    assert _status(db, job_id) == "running"
    assert deps.graph.get_state(run_config(job_id)).values["failure_reason"] is None
    assert queue.deleted == []


# --- 5. The loop, and shutdown ----------------------------------------------------------


@pytest.fixture
def restored_signals() -> Iterator[None]:
    """Put SIGTERM and SIGINT back afterwards.

    `Shutdown.install()` is process-wide, and pytest's own handling of Ctrl-C is the thing it
    would otherwise replace for the rest of the run.
    """
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    yield
    for number, handler in previous.items():
        signal.signal(number, handler)


def test_a_signal_stops_new_work_and_finishes_the_message_in_flight(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    restored_signals: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM raised for real, in the middle of an invocation.

    The flag is read between messages, never inside one, because raising out of a handler
    could unwind the middle of a graph invocation and leave the checkpoint behind the database
    - the one thing ADR 0005 decision 2 says to avoid. So the job being run when the signal
    arrives finishes and is acknowledged, and the job behind it is never received.
    """
    shutdown = Shutdown()
    shutdown.install()
    first = _queued(db, queue)
    second = _queued(db, queue, question="A different question entirely.")
    original = queries.record_plan

    def signal_mid_invocation(*args: Any, **kwargs: Any) -> None:
        signal.raise_signal(signal.SIGTERM)
        original(*args, **kwargs)

    monkeypatch.setattr(queries, "record_plan", signal_mid_invocation)

    assert run(deps, shutdown=shutdown) == 1

    assert shutdown.requested
    assert _status(db, first) == "awaiting_approval"  # the invocation in flight finished
    assert queue.deleted == [f"key-{first}"]  # and was acknowledged
    assert _status(db, second) == "queued"  # the next one was never received
    assert len(queue.pending()) == 1  # and is still on the queue for another worker


def test_a_second_signal_is_not_escalated_to_a_hard_exit(
    deps: WorkerDeps, restored_signals: None
) -> None:
    # The container runtime already escalates - SIGTERM then SIGKILL after its grace period -
    # and a worker that killed itself faster would only lose the checkpoint the first signal
    # was protecting.
    shutdown = Shutdown()
    shutdown.install()

    signal.raise_signal(signal.SIGTERM)
    signal.raise_signal(signal.SIGINT)

    assert shutdown.requested
    assert run(deps, shutdown=shutdown) == 0  # and it simply does no more work


def test_a_receive_that_raises_pauses_instead_of_spinning(
    deps: WorkerDeps, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A queue that cannot be reached is a hot loop unless something says otherwise.

    The pause is deliberately not a backoff schedule and not a bound to give up at: the worker
    has one job to do, and an operator watching the DLQ alarm is the escalation. What this
    pins is that the loop survives the error, waits, and tries again.
    """
    shutdown = Shutdown()
    slept: list[float] = []
    receives = [0]

    def receive() -> None:
        receives[0] += 1
        if receives[0] == 1:
            raise QueueError("the queue is unreachable")
        shutdown.requested = True  # the second attempt ends the loop rather than the test

    monkeypatch.setattr(deps.queue, "receive", receive)
    # The module's own `time`, so the pause is observed rather than waited out. Patching
    # `time.sleep` globally would stop every other library in the process from sleeping too.
    monkeypatch.setattr("worker.time.sleep", slept.append)

    with caplog.at_level(logging.ERROR):
        assert run(deps, shutdown=shutdown) == 0

    assert receives[0] == 2  # it tried again rather than giving up
    assert slept == [RECEIVE_ERROR_PAUSE_S]
    assert "could not receive" in caplog.text


def test_an_empty_receive_is_not_a_handled_message(
    deps: WorkerDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Long polling answers "nothing" all day on an idle queue, and that is not work.
    shutdown = Shutdown()
    answers = [None, None]

    def receive() -> None:
        if not answers:
            shutdown.requested = True
            return None
        return answers.pop()

    monkeypatch.setattr(deps.queue, "receive", receive)

    assert run(deps, shutdown=shutdown) == 0


def test_one_poisonous_job_does_not_stop_the_worker_serving_the_next(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The failure is caught rather than re-raised, and this is the reason: a worker that died
    # on one bad job would stop every other job on the queue behind it.
    broken = _queued(db, queue)
    healthy = _queued(db, queue, question="A different question entirely.")
    _break_write(monkeypatch, "record_plan", times=1)

    assert _drain(deps, queue) == 2

    assert _status(db, broken) == "running"  # left for redelivery
    assert _status(db, healthy) == "awaiting_approval"  # and the next job ran normally


# --- 6. What the worker refuses to start against ------------------------------------------


def _attributes(**overrides: str) -> dict[str, str]:
    """The local queue's attributes, as SQS reports them."""
    return {
        "FifoQueue": "true",
        "VisibilityTimeout": "1800",
        "RedrivePolicy": '{"deadLetterTargetArn":"arn:dlq","maxReceiveCount":"3"}',
        **overrides,
    }


class _StubQueue:
    """Something with `attributes()`, which is all `check_queue` reads."""

    def __init__(self, attributes: dict[str, str]) -> None:
        self._attributes = attributes

    def attributes(self) -> dict[str, str]:
        return self._attributes


def test_the_worker_accepts_the_queue_compose_creates(config: Config) -> None:
    # The numbers here are docker-compose.yml's, so this fails if either side drifts from
    # ADR 0010 decision 8's inequality.
    assert check_queue(cast(Any, _StubQueue(_attributes())), config) == 3


def test_a_standard_queue_is_refused_because_fifo_is_load_bearing(config: Config) -> None:
    # FIFO with `MessageGroupId = job_id` is what keeps one job to one writer, which
    # ADR 0005's `_write_findings` is allowed to assume. A standard queue breaks it silently.
    with pytest.raises(RuntimeError, match="FIFO"):
        check_queue(cast(Any, _StubQueue(_attributes(FifoQueue="false"))), config)


def test_a_visibility_timeout_that_does_not_cover_the_bound_is_refused(config: Config) -> None:
    """Both edges of ADR 0010 decision 8, one second apart.

    The invariant is not `visibility > MAX_JOB_RUNTIME`: the runtime bound can only be checked
    between nodes, so the queue has to cover the bound *plus* the longest a single node can
    take. Refused loudly rather than clamped, because a queue that behaves differently from
    the one that was configured is expensive to read back off a run.
    """
    required = required_visibility_timeout(config)
    assert required == config.max_job_runtime + 3 * config.llm_main_timeout_s + BACKOFF_SECONDS

    with pytest.raises(RuntimeError, match="visibility timeout"):
        check_queue(
            cast(Any, _StubQueue(_attributes(VisibilityTimeout=str(int(required))))), config
        )
    assert (
        check_queue(
            cast(Any, _StubQueue(_attributes(VisibilityTimeout=str(int(required) + 1)))), config
        )
        == 3
    )


def test_a_queue_with_no_redrive_policy_starts_with_a_warning(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    attributes = _attributes()
    del attributes["RedrivePolicy"]

    with caplog.at_level(logging.WARNING):
        assert check_queue(cast(Any, _StubQueue(attributes)), config) is None

    assert "no redrive policy" in caplog.text


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "SQS_QUEUE_URL",
        # Step 22a. Discovering a missing bucket at export time would fail a job that had
        # already paid for its whole pipeline.
        "S3_BUCKET",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "TAVILY_API_KEY",
    ],
)
def test_the_worker_refuses_to_start_without_each_variable_it_uses(name: str) -> None:
    """ADR 0012 decision 4's other half: the API stopped requiring these, so the worker states
    them itself. A worker that started without an endpoint would fail on its first job."""
    without = {key: value for key, value in _ENV.items() if key != name}

    with pytest.raises(ValueError, match=name):
        required_credentials(load_config(without))


def test_a_complete_environment_narrows_to_the_seven_values(config: Config) -> None:
    credentials = required_credentials(config)

    assert credentials.queue_url == _ENV["SQS_QUEUE_URL"]
    assert credentials.s3_bucket == "research-reports"
    assert credentials.llm_model == "main-model"


# --- 7. The process boundary ------------------------------------------------------------


def test_the_worker_is_the_process_that_owns_the_llm_and_the_graph() -> None:
    """ADR 0012, asserted by import rather than by review.

    The complement lives in tests/test_api.py: the route layer imports none of these. Stated
    from both sides because the requirement is a boundary, and a boundary with only one side
    checked is a boundary that moves.
    """
    source = (Path(__file__).resolve().parent.parent / "worker.py").read_text(encoding="utf-8")

    assert "from llm_client import LLMClient" in source
    assert "from graph.build import" in source
    assert "build_graph(" in source


def test_the_worker_holds_one_graph_for_the_life_of_the_process(
    deps: WorkerDeps, graph: ResearchGraph
) -> None:
    # `WorkerDeps` carries the graph rather than the pieces because building one per message
    # would rebuild five agents and a connection pool for every job.
    assert deps.graph is graph
    with pytest.raises(AttributeError):  # frozen: nothing rebuilds it mid-run
        deps.graph = graph  # type: ignore[misc]


def test_the_jobs_row_is_the_only_place_a_status_is_written(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue
) -> None:
    # A sanity check on ADR 0010's "three transitions, three owners": the statuses a single
    # clean job passes through, in order, with nothing between them.
    job_id = _queued(db, queue)
    seen = [_status(db, job_id)]
    _drain(deps, queue)
    seen.append(_status(db, job_id))
    _decide(db, deps.graph, job_id, decision="approve")
    seen.append(_status(db, job_id))
    _resume(queue, deps.graph, job_id)
    _drain(deps, queue)
    seen.append(_status(db, job_id))

    assert seen == ["queued", "awaiting_approval", "running", "approved"]
    with db.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(jobs)).scalar_one() == 1


# --- 8. Redis, and what the worker refuses to start without (step 21) ---------------------


class _Redis:
    """A client whose `ping` answers, or raises the way an unreachable one does."""

    def __init__(self, *, up: bool) -> None:
        self.up = up

    def ping(self) -> bool:
        if not self.up:
            raise RedisConnectionError("redis is not answering")
        return True


def test_a_worker_refuses_to_start_when_redis_does_not_answer(config: Config) -> None:
    """The fail-closed rule, reaching startup (guidelines §11, §17).

    The shared rate limiter is the one Redis responsibility that fails closed, so a worker
    that started against an unreachable Redis would take a message, fail its first node with
    `rate_limiter_unavailable`, leave the message, and repeat that until the job
    dead-lettered. Refusing here costs one log line instead of three deliveries.
    """
    with pytest.raises(RuntimeError, match="rate limiter"):
        check_redis(cast(Any, _Redis(up=False)), config)


def test_a_worker_starts_against_a_redis_that_answers(config: Config) -> None:
    check_redis(cast(Any, _Redis(up=True)), config)


def test_the_refusal_is_printable_on_a_windows_console(config: Config) -> None:
    """The last thing a failing worker prints has to survive the console it prints to.

    A Windows console defaults to cp1252, where a section sign raises `UnicodeEncodeError`
    while the process is already dying - so the message that explains the refusal would be
    replaced by a traceback about encoding it. Measured, not theorised: it happened during
    the step-21 smoke run.
    """
    with pytest.raises(RuntimeError) as raised:
        check_redis(cast(Any, _Redis(up=False)), config)

    str(raised.value).encode("cp1252")  # raises if a non-encodable character crept back in


def test_the_refusal_names_redis_without_leaking_the_error(config: Config) -> None:
    # The message is an operator's first read, so it names the URL it tried and the rule it
    # is enforcing - and not the driver's exception text.
    with pytest.raises(RuntimeError) as raised:
        check_redis(cast(Any, _Redis(up=False)), config)

    assert config.redis_url in str(raised.value)
    assert "not answering" not in str(raised.value)


def test_the_worker_is_the_process_that_owns_redis() -> None:
    """Step 21's half of the ADR 0012 boundary, asserted by import.

    The worker builds the cache, the URL set and the limiter; the API builds one health
    probe and nothing else. The complement is in tests/test_api.py, which pins that
    `RouteDeps` carries no cache and no bucket.
    """
    source = (Path(__file__).resolve().parent.parent / "worker.py").read_text(encoding="utf-8")

    assert "RedisCache(redis)" in source
    assert "RedisUrlDeduplicator(redis)" in source
    assert "RedisRateLimiter(redis" in source
    assert "check_redis(redis, config)" in source
