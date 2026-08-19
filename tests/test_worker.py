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

    **Shutdown stops at the next checkpoint boundary.** The signal is raised for real, from
    inside a node, and the assertions are that the node finished and is durable, the node after
    it never started, the message was **not** acknowledged, and the redelivery continues from
    the checkpoint without replaying anything. A delivery whose graph genuinely reached the gate
    is still acknowledged, because that is an outcome rather than an interruption.

WHO CALLS IT
    pytest. No service, no network: SQLite for the rows, `InMemorySaver` for the checkpoints,
    and `FakeQueue` for SQS. The same properties against real SQS are in
    tests/test_queue_localstack.py, which is marked `integration`.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
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

import worker
from artifacts import ArtifactStore
from config import Config, load_config
from database import locks as database_locks
from database import queries
from database.schema import jobs
from graph.build import ResearchGraph, build_graph
from graph.state import run_config, state_serde
from jobqueue import JobMessage, OwnershipLost, QueueError
from llm_client import LLMClient
from worker import (
    MAX_CONSECUTIVE_RENEWAL_FAILURES,
    RECEIVE_ERROR_PAUSE_S,
    QueueSettings,
    Shutdown,
    VisibilityLease,
    WorkerDeps,
    check_queue,
    check_redis,
    handle,
    required_credentials,
    run,
    visibility_failure_retry_at,
    visibility_renewal_interval,
    visibility_safe_renewal_start,
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


def test_a_resume_for_a_visit_with_no_decision_acks_only_the_still_usable_gate(
    web: RecordedWeb, deps: WorkerDeps, db: Engine, queue: FakeQueue, fake: FakeLLM
) -> None:
    """ADR 0011 decision 2's loud gap: no decision row means no guess.

    Guessing which of the three decisions was meant is exactly the silent wrong answer
    `human_gate_node` already refuses when a resume payload does not validate. The graph is not
    resumed; the already-usable gate remains open, so this pointer adds no recovery value and may
    be acknowledged without inventing or duplicating a reviewer decision.
    """
    job_id = _queued(db, queue)
    _drain(deps, queue)
    _resume(queue, deps.graph, job_id)  # a resume nobody decided
    spent = list(fake.roles)

    _drain(deps, queue)

    assert fake.roles == spent  # the graph was not invoked
    assert queue.in_flight() == []
    assert _status(db, job_id) == "awaiting_approval"  # the gate is still open
    assert _actions(db, job_id).count("reviewer_decision") == 0


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
    assert queue.in_flight() == []  # usable gate is durable; blind continuation never occurred
    assert _actions(db, job_id).count("reviewer_decision") == 0


def test_the_row_is_projected_before_the_checkpoint_can_hold_the_interrupt(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering the ordinary path depends on, pinned so it cannot move.

    `record_gate_opened` commits `awaiting_approval` and the `gate_opened` row in one
    transaction **before** `interrupt()` is reached, so on the first execution of a visit the row
    is projected strictly before a pending interrupt exists to be seen. That is why a kill in
    that window cannot strand a job.

    The observation is taken from inside the gate node, immediately after that write and so
    before the pause. It is deliberately not the whole story - the write is keyed, so a *replay*
    of the same visit skips it and the reconcile becomes the writer, which is the sequence the
    two tests below are about.
    """
    seen: list[str] = []
    original = queries.record_gate_opened

    def observe(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        seen.append(_status(db, cast(str, kwargs["job_id"])))

    monkeypatch.setattr(queries, "record_gate_opened", observe)
    job_id = _queued(db, queue)

    _drain(deps, queue)

    assert seen and all(status == "awaiting_approval" for status in seen), seen
    assert deps.graph.get_state(run_config(job_id)).interrupts


class _HardKill(BaseException):
    """A process that stops where it stands.

    `BaseException` rather than `Exception` for one reason: `handle` catches `Exception`, so this
    escapes it and skips everything after the point it is raised - which is what a SIGKILL does
    and what an ordinary test double cannot imitate.
    """


def test_a_kill_between_the_gate_row_and_the_pause_is_recovered_by_redelivery(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    bucket: FakeS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate node that dies after its keyed write, recovered by the redelivery.

    This is the first of the two failures that together strand a job, and on its own it is
    harmless. The keyed write landed, so the reconcile in `_invoke`'s `finally` sees no pending
    interrupt and truthfully writes `running`; the redelivery re-runs the gate node, the keyed
    write is skipped as designed, the pause is taken, and the reconcile agrees with it.

    **What it establishes is the state the next test starts from**: from here on, no execution of
    this visit will write the status again, so the reconcile is the only thing maintaining it.
    """
    original = queries.record_gate_opened
    died = [False]

    def write_then_die(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)  # the row and `awaiting_approval` land
        if not died[0]:  # and then the process dies, before interrupt() is reached
            died[0] = True
            raise OperationalError("killed at the gate", {}, Exception("connection lost"))

    monkeypatch.setattr(queries, "record_gate_opened", write_then_die)
    job_id = _queued(db, queue)

    _drain(deps, queue)
    # `running`, not `awaiting_approval`: the reconcile corrected the projection, because the
    # pause the row was written in anticipation of never happened.
    assert _status(db, job_id) == "running"
    assert not deps.graph.get_state(run_config(job_id)).interrupts
    assert queue.deleted == []  # and nothing was acknowledged

    assert queue.redeliver() == 1
    _drain(deps, queue)

    assert deps.graph.get_state(run_config(job_id)).interrupts
    assert _status(db, job_id) == "awaiting_approval"
    assert _actions(db, job_id).count("gate_opened") == 1  # keyed: one visit, one row


def test_a_redelivery_reprojects_a_gate_a_killed_replay_left_saying_running(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    bucket: FakeS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The sequence that really does strand a job, and the one line that stops it.**

    A 2026-08-18 review claimed a hard kill could leave a pending interrupt beside a row saying
    `running`, with no decision on record - a job `GET /jobs/{id}/gate` and
    `POST /jobs/{id}/approve` both refuse, so unapprovable, and dead-lettered after three
    deliveries. On the ordinary path that is impossible, because the gate row is committed
    before the pause. It takes two failures, and this test performs both:

      1. the gate node dies after `record_gate_opened` committed, which arms the keyed guard so
         no later execution of this visit writes the status, and leaves the row at `running`;
      2. the redelivery reaches `interrupt()`, LangGraph checkpoints the pause, and the process
         is then SIGKILLed.

    Failure 2 is emulated by raising a `BaseException` where the reconcile would run. That is
    deliberate rather than convenient: `handle` catches `Exception`, so a `BaseException` escapes
    it and **both** of the writes that follow an invocation are skipped - the reconcile and the
    `queue.delete`. Suppressing only the reconcile would leave the message acknowledged, which
    no killed process ever does, and would test a state that cannot occur.

    The third delivery is the fixed branch: it writes what the checkpoint says, verifies the gate
    is usable, acknowledges that now-unnecessary delivery, and the reviewer can then finish the job.
    """
    original = queries.record_gate_opened
    died = [False]

    def write_then_die(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        if not died[0]:
            died[0] = True
            raise OperationalError("killed at the gate", {}, Exception("connection lost"))

    monkeypatch.setattr(queries, "record_gate_opened", write_then_die)
    job_id = _queued(db, queue)
    _drain(deps, queue)  # failure 1
    assert _status(db, job_id) == "running"

    def hard_kill(*_args: Any, **_kwargs: Any) -> None:
        raise _HardKill

    monkeypatch.setattr(worker, "_reconcile_status", hard_kill)
    queue.redeliver()
    with pytest.raises(_HardKill):  # failure 2: nothing after the checkpoint runs
        _drain(deps, queue)
    monkeypatch.undo()

    # The stranded state, reached entirely through the real code paths.
    assert deps.graph.get_state(run_config(job_id)).interrupts
    assert _status(db, job_id) == "running"
    assert queue.deleted == []

    assert queue.redeliver() == 1
    _drain(deps, queue)  # the delivery that used to change nothing

    assert _status(db, job_id) == "awaiting_approval"  # answerable again
    assert queue.deleted == [f"key-{job_id}"]  # durable, usable gate: redelivery unnecessary
    assert _actions(db, job_id).count("reviewer_decision") == 0

    _decide(db, deps.graph, job_id, decision="approve")
    _resume(queue, deps.graph, job_id)
    _drain(deps, queue)

    row = queries.read_job(db, job_id)
    assert row is not None
    assert row.status == "approved" and row.completed_at is not None
    assert bucket.body(f"reports/{job_id}.json") == row.report_json
    assert _actions(db, job_id).count("reviewer_decision") == 1


def test_a_consistent_gate_acks_without_a_redundant_reconciliation(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary gate already commits its row projection before interrupting."""

    def redundant(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("a consistent gate does not need checkpoint reconciliation")

    monkeypatch.setattr(worker, "_reconcile_status", redundant)
    job_id = _queued(db, queue)

    _drain(deps, queue)

    assert deps.graph.get_state(run_config(job_id)).interrupts
    assert _status(db, job_id) == "awaiting_approval"
    assert queue.deleted == [f"key-{job_id}"]


def test_a_gate_with_a_running_projection_is_reconciled_before_acknowledgement(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = queries.record_gate_opened

    def leave_running(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        queries.set_job_status(db, job_id=kwargs["job_id"], status="running")

    monkeypatch.setattr(queries, "record_gate_opened", leave_running)
    job_id = _queued(db, queue)

    _drain(deps, queue)

    assert deps.graph.get_state(run_config(job_id)).interrupts
    assert _status(db, job_id) == "awaiting_approval"
    assert queue.deleted == [f"key-{job_id}"]
    assert _actions(db, job_id).count("gate_opened") == 1


def test_failed_gate_reconciliation_preserves_redelivery_then_repairs_and_resumes(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    bucket: FakeS3,
    fake: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_gate = queries.record_gate_opened
    original_reconcile = worker._reconcile_status
    failed = [False]

    def leave_running(*args: Any, **kwargs: Any) -> None:
        original_gate(*args, **kwargs)
        queries.set_job_status(db, job_id=kwargs["job_id"], status="running")

    def fail_once(*args: Any, **kwargs: Any) -> bool:
        if not failed[0]:
            failed[0] = True
            return False
        return original_reconcile(*args, **kwargs)

    monkeypatch.setattr(queries, "record_gate_opened", leave_running)
    monkeypatch.setattr(worker, "_reconcile_status", fail_once)
    job_id = _queued(db, queue)

    _drain(deps, queue)
    spent = list(fake.roles)

    assert deps.graph.get_state(run_config(job_id)).interrupts
    assert _status(db, job_id) == "running"
    assert queue.deleted == []
    assert _actions(db, job_id).count("gate_opened") == 1

    assert queue.redeliver() == 1
    _drain(deps, queue)

    assert _status(db, job_id) == "awaiting_approval"
    assert queue.deleted == [f"key-{job_id}"]
    assert fake.roles == spent  # the durable gate was inspected, not replayed
    assert _actions(db, job_id).count("gate_opened") == 1
    assert _actions(db, job_id).count("reviewer_decision") == 0

    _decide(db, deps.graph, job_id, decision="approve")
    _resume(queue, deps.graph, job_id)
    _drain(deps, queue)

    row = queries.read_job(db, job_id)
    assert row is not None and row.status == "approved" and row.completed_at is not None
    assert bucket.body(f"reports/{job_id}.json") == row.report_json
    assert _actions(db, job_id).count("gate_opened") == 1
    assert _actions(db, job_id).count("reviewer_decision") == 1


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


def test_timeout_finish_job_failure_preserves_redelivery_and_recovers_without_another_node(
    web: RecordedWeb,
    db: Engine,
    graph: ResearchGraph,
    queue: FakeQueue,
    fake: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bounded = load_config({**_ENV, "MAX_JOB_RUNTIME": "0"})
    deps = WorkerDeps(
        config=bounded,
        engine=db,
        graph=graph,
        queue=cast(Any, queue),
        final_delivery_at=FINAL_DELIVERY_AT,
    )
    original = queries.finish_job
    failed = [False]

    def fail_once(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("failure_reason") == "job_timeout" and not failed[0]:
            failed[0] = True
            raise OperationalError("finish_job failed", {}, Exception("connection lost"))
        original(*args, **kwargs)

    monkeypatch.setattr(queries, "finish_job", fail_once)
    job_id = _queued(db, queue)

    _drain(deps, queue)

    assert graph.get_state(run_config(job_id)).values["failure_reason"] == "job_timeout"
    assert _status(db, job_id) == "running"
    assert queue.deleted == []
    assert len(queue.in_flight()) == 1
    spent = list(fake.roles)

    assert queue.redeliver() == 1
    _drain(deps, queue)

    assert _status(db, job_id) == "failed"
    assert queue.deleted == [f"key-{job_id}"]
    assert fake.roles == spent  # the durable timeout marker is finalized, not advanced
    assert _actions(db, job_id).count("job_finished") == 1


def test_timeout_checkpoint_finalization_failure_preserves_redelivery(
    web: RecordedWeb,
    db: Engine,
    graph: ResearchGraph,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bounded = load_config({**_ENV, "MAX_JOB_RUNTIME": "0"})
    deps = WorkerDeps(
        config=bounded,
        engine=db,
        graph=graph,
        queue=cast(Any, queue),
        final_delivery_at=FINAL_DELIVERY_AT,
    )
    original = graph.update_state

    def fail_timeout(config: Any, values: Any, *args: Any, **kwargs: Any) -> Any:
        if values.get("failure_reason") == "job_timeout":
            raise OperationalError("checkpoint failed", {}, Exception("connection lost"))
        return original(config, values, *args, **kwargs)

    monkeypatch.setattr(graph, "update_state", fail_timeout)
    job_id = _queued(db, queue)

    _drain(deps, queue)

    assert graph.get_state(run_config(job_id)).values["failure_reason"] is None
    assert _status(db, job_id) == "running"
    assert queue.deleted == []
    assert len(queue.in_flight()) == 1
    assert "job_finished" not in _actions(db, job_id)


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


def test_a_signal_lets_the_node_in_flight_finish_and_starts_no_other(
    web: RecordedWeb,
    config: Config,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    fake: FakeLLM,
    restored_signals: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM raised for real, from inside the Planner node.

    **The node that was running finishes; the node after it never starts.** The flag is read
    between nodes and never inside one, because raising out of a handler could unwind the
    middle of a node and leave the checkpoint behind the database - the one thing ADR 0005
    decision 2 says to avoid. Between nodes is safe precisely because LangGraph has just
    checkpointed there.

    So the evidence is: the plan is durable, the Researcher never ran, the job is still
    `running`, and **the message was not acknowledged** - it is still in flight for redelivery.
    Before 2026-08-18 the whole job ran to the gate and was deleted, which needed the grace
    period to cover a job rather than a node.
    """
    shutdown = Shutdown()
    shutdown.install()
    deps = WorkerDeps(
        config=config,
        engine=db,
        graph=graph,
        queue=cast(Any, queue),
        final_delivery_at=FINAL_DELIVERY_AT,
        shutdown=shutdown,
    )
    first = _queued(db, queue)
    second = _queued(db, queue, question="A different question entirely.")
    original = queries.record_plan
    renewed = threading.Event()
    extend_visibility = queue.extend_visibility

    monkeypatch.setattr(worker, "visibility_renewal_interval", lambda _timeout: 0.01)

    def renew(message: Any, *, visibility_timeout_s: int) -> None:
        extend_visibility(message, visibility_timeout_s=visibility_timeout_s)
        renewed.set()

    monkeypatch.setattr(queue, "extend_visibility", renew)

    def signal_mid_node(*args: Any, **kwargs: Any) -> None:
        signal.raise_signal(signal.SIGTERM)
        assert renewed.wait(timeout=1.0)  # ownership stays live while this node finishes
        original(*args, **kwargs)  # the node completes after the signal

    monkeypatch.setattr(queries, "record_plan", signal_mid_node)

    assert run(deps) == 1

    assert shutdown.requested
    # The node in flight finished and its work is durable.
    assert "plan_produced" in _actions(db, first)
    assert deps.graph.get_state(run_config(first)).values["plan"] is not None
    # The node after it never started: no search, no findings, no gate.
    assert web.queries == []
    assert "subtopic_researched" not in _actions(db, first)
    assert _status(db, first) == "running"
    # And nothing was acknowledged, so redelivery is what continues it.
    assert queue.deleted == []
    assert len(queue.in_flight()) == 1
    renewals = len(queue.visibility_extensions)
    time.sleep(0.03)
    assert len(queue.visibility_extensions) == renewals  # stopped on relinquishment
    assert _status(db, second) == "queued"  # the next message was never received
    assert len(queue.pending()) == 1


def test_a_message_received_during_the_poll_is_not_started(
    web: RecordedWeb,
    config: Config,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    fake: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race the loop condition alone cannot win.

    `receive()` is a twenty-second long poll. A SIGTERM arriving one millisecond after it starts
    is not seen by the `while` until the poll returns - and it may return holding a job. Checking
    the flag again *after* the receive is what makes "stop taking new work" true rather than
    nearly true.

    The signal is set by the receive itself here, which is the only way to place it inside a
    call that has already been entered.
    """
    shutdown = Shutdown()
    deps = WorkerDeps(
        config=config,
        engine=db,
        graph=graph,
        queue=cast(Any, queue),
        final_delivery_at=FINAL_DELIVERY_AT,
        shutdown=shutdown,
    )
    job_id = _queued(db, queue)
    receive = queue.receive

    def signal_during_the_poll() -> Any:
        message = receive()  # the poll returns a real message...
        shutdown.requested = True  # ...and the signal landed while it was in flight
        return message

    monkeypatch.setattr(queue, "receive", signal_during_the_poll)

    assert run(deps) == 0  # received, deliberately not handled

    assert _status(db, job_id) == "queued"  # never started
    assert fake.roles == []  # no node ran, so no call was made
    assert queue.deleted == []  # and it was not acknowledged
    assert len(queue.in_flight()) == 1  # it stays for another worker
    assert queue.visibility_extensions == []  # it was never committed to processing


def _lease_message() -> JobMessage:
    return JobMessage(
        job_id="11111111-1111-4111-8111-111111111111",
        user_id=USER,
        idempotency_key="lease-key",
        receipt_handle="opaque-handle",
        receive_count=1,
    )


def test_one_transient_heartbeat_failure_recovers_without_losing_ownership(
    queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0
    recovered = threading.Event()
    monkeypatch.setattr(worker, "visibility_renewal_interval", lambda _timeout: 0.01)

    timeout = cast(int, 0.09)  # sub-second lease keeps this thread test deterministic and fast

    def renew(_message: Any, *, visibility_timeout_s: int) -> None:
        nonlocal attempts
        assert visibility_timeout_s == timeout
        attempts += 1
        if attempts == 1:
            raise QueueError("transient SQS failure")
        recovered.set()

    monkeypatch.setattr(queue, "extend_visibility", renew)
    lease = VisibilityLease(
        cast(Any, queue),
        _lease_message(),
        visibility_timeout_s=timeout,
        renewal_call_envelope_s=0.001,
    )

    lease.start()
    assert recovered.wait(timeout=1.0)
    assert not lease.ownership_lost
    lease.stop(reason="test complete")


def test_two_consecutive_heartbeat_failures_make_ownership_unsafe(
    queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0
    failed_twice = threading.Event()
    monkeypatch.setattr(worker, "visibility_renewal_interval", lambda _timeout: 0.01)

    timeout = cast(int, 0.09)  # sub-second lease keeps this thread test deterministic and fast

    def renew(_message: Any, *, visibility_timeout_s: int) -> None:
        nonlocal attempts
        assert visibility_timeout_s == timeout
        attempts += 1
        if attempts == MAX_CONSECUTIVE_RENEWAL_FAILURES:
            failed_twice.set()
        raise QueueError("SQS remains unavailable")

    monkeypatch.setattr(queue, "extend_visibility", renew)
    lease = VisibilityLease(
        cast(Any, queue),
        _lease_message(),
        visibility_timeout_s=timeout,
        renewal_call_envelope_s=0.001,
    )

    lease.start()
    assert failed_twice.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while not lease.ownership_lost and time.monotonic() < deadline:
        time.sleep(0.001)
    assert lease.ownership_lost
    lease.stop(reason="ownership lost")


def test_a_failed_renewal_retry_is_derived_earlier_than_the_next_healthy_cadence() -> None:
    # Initial attempt at t=600; the bounded 93s call fails at t=693.  Preserving the final 600s
    # makes t=1107 the latest safe new start, and the midpoint policy retries at t=900.
    retry_at = visibility_failure_retry_at(
        693.0,
        estimated_expiry=1800.0,
        visibility_timeout_s=1800,
        call_envelope_s=93.0,
    )

    assert retry_at == 900.0
    assert retry_at < 1200.0
    assert (
        visibility_safe_renewal_start(1800.0, visibility_timeout_s=1800, call_envelope_s=93.0)
        == 1107.0
    )


def test_no_renewal_retry_is_claimed_after_the_safe_start_deadline() -> None:
    assert (
        visibility_failure_retry_at(
            1107.0,
            estimated_expiry=1800.0,
            visibility_timeout_s=1800,
            call_envelope_s=93.0,
        )
        is None
    )


def test_scheduler_lateness_past_the_safe_deadline_loses_ownership_without_a_call(
    queue: FakeQueue,
) -> None:
    now = time.monotonic()
    message = JobMessage(
        **{
            **_lease_message().__dict__,
            "received_at_monotonic": now - 1200.0,
        }
    )
    lease = VisibilityLease(cast(Any, queue), message, visibility_timeout_s=1800)

    lease.start()
    deadline = time.monotonic() + 1.0
    while not lease.ownership_lost and time.monotonic() < deadline:
        time.sleep(0.001)

    assert lease.ownership_lost
    assert queue.visibility_extensions == []
    assert lease.state.scheduling_lateness_s >= 600.0
    lease.stop(reason="unsafe before call")


def test_successful_renewal_uses_attempt_start_for_expiry_and_restores_healthy_schedule(
    queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    renewed = threading.Event()
    monkeypatch.setattr(worker, "visibility_renewal_interval", lambda _timeout: 0.01)

    def renew(_message: Any, *, visibility_timeout_s: int) -> None:
        assert visibility_timeout_s == 0.09
        renewed.set()

    monkeypatch.setattr(queue, "extend_visibility", renew)
    lease = VisibilityLease(
        cast(Any, queue),
        _lease_message(),
        visibility_timeout_s=cast(int, 0.09),
        renewal_call_envelope_s=0.001,
    )

    lease.start()
    assert renewed.wait(timeout=1.0)
    state = lease.state
    assert state.last_attempt_started_at is not None
    assert state.last_successful_renewal_started_at == state.last_attempt_started_at
    assert state.estimated_expiry == pytest.approx(state.last_attempt_started_at + 0.09)
    assert state.next_attempt_at == pytest.approx(state.last_attempt_started_at + 0.01)
    assert state.consecutive_failures == 0
    lease.stop(reason="state observed")


def test_an_invalid_receipt_handle_loses_ownership_immediately(
    queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempted = threading.Event()
    monkeypatch.setattr(worker, "visibility_renewal_interval", lambda _timeout: 0.01)

    def renew(_message: Any, *, visibility_timeout_s: int) -> None:
        assert visibility_timeout_s == 1800
        attempted.set()
        raise OwnershipLost("receipt handle is no longer in flight")

    monkeypatch.setattr(queue, "extend_visibility", renew)
    lease = VisibilityLease(cast(Any, queue), _lease_message(), visibility_timeout_s=1800)

    lease.start()
    assert attempted.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while not lease.ownership_lost and time.monotonic() < deadline:
        time.sleep(0.001)
    assert lease.ownership_lost
    lease.stop(reason="ownership lost")


def test_ownership_loss_during_a_node_stops_after_its_checkpoint_without_acknowledging(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner_started = threading.Event()
    attempted = threading.Event()
    original = queries.record_plan
    monkeypatch.setattr(worker, "visibility_renewal_interval", lambda _timeout: 0.001)

    def lose(_message: Any, *, visibility_timeout_s: int) -> None:
        assert visibility_timeout_s == 1800
        assert planner_started.wait(timeout=1.0)
        attempted.set()
        raise OwnershipLost("receipt handle is no longer in flight")

    def finish_after_loss(*args: Any, **kwargs: Any) -> None:
        planner_started.set()
        assert attempted.wait(timeout=1.0)
        time.sleep(0.01)  # let the heartbeat publish ownership_lost before the node returns
        original(*args, **kwargs)

    monkeypatch.setattr(queue, "extend_visibility", lose)
    monkeypatch.setattr(queries, "record_plan", finish_after_loss)
    job_id = _queued(db, queue)

    assert _drain(deps, queue) == 1

    assert deps.graph.get_state(run_config(job_id)).values["plan"] is not None
    assert web.queries == []  # no Researcher node started after the durable Planner checkpoint
    assert queue.deleted == []
    assert _status(db, job_id) == "running"


class _ContendedExecutionLock:
    """Cross-worker lock behaviour for worker orchestration tests.

    PostgreSQL itself is exercised in ``test_database_postgres``.  This narrow double supplies the
    same try/acquire/release surface so the worker test can deterministically pause a redelivery.
    """

    mutex = threading.Lock()
    waiter_seen = threading.Event()

    def __init__(self, _engine: Engine, job_id: str) -> None:
        self.job_id = job_id
        self._acquired = False

    def __enter__(self) -> _ContendedExecutionLock:
        return self

    def __exit__(self, *_args: Any) -> None:
        if self._acquired:
            self.mutex.release()
            self._acquired = False

    def try_acquire(self) -> bool:
        if self._acquired:
            return True
        self._acquired = self.mutex.acquire(blocking=False)
        if not self._acquired:
            self.waiter_seen.set()
        return self._acquired


def test_redelivery_heartbeats_while_waiting_then_rereads_the_checkpoint_after_lock(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    fake: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A owns the current node; B owns a new receipt but executes nothing until A releases.

    A's queue lease is deliberately lost while its Planner is in flight.  The database execution
    lock remains held until that node has returned and synchronously checkpointed.  B renews its own
    receipt while waiting, then reloads the new checkpoint and continues with the Researcher rather
    than replaying the Planner or creating a competing branch.
    """
    _ContendedExecutionLock.mutex = threading.Lock()
    _ContendedExecutionLock.waiter_seen = threading.Event()
    monkeypatch.setattr(database_locks, "job_execution_lock", _ContendedExecutionLock)
    monkeypatch.setattr(worker, "JOB_LOCK_RETRY_INTERVAL_S", 0.001)
    monkeypatch.setattr(worker, "visibility_renewal_interval", lambda _timeout: 0.01)

    planner_started = threading.Event()
    first_receipt_lost = threading.Event()
    second_receipt_renewed = threading.Event()
    original_plan = queries.record_plan
    original_renew = queue.extend_visibility

    def renew(message: JobMessage, *, visibility_timeout_s: int) -> None:
        if message.receipt_handle == "receipt-a":
            assert planner_started.wait(timeout=1.0)
            first_receipt_lost.set()
            raise OwnershipLost("A's SQS receipt expired")
        original_renew(message, visibility_timeout_s=visibility_timeout_s)
        if message.receipt_handle == "receipt-b":
            second_receipt_renewed.set()

    def finish_plan_after_redelivery_is_waiting(*args: Any, **kwargs: Any) -> None:
        planner_started.set()
        assert first_receipt_lost.wait(timeout=1.0)
        assert _ContendedExecutionLock.waiter_seen.wait(timeout=1.0)
        assert second_receipt_renewed.wait(timeout=1.0)
        original_plan(*args, **kwargs)

    monkeypatch.setattr(queue, "extend_visibility", renew)
    monkeypatch.setattr(queries, "record_plan", finish_plan_after_redelivery_is_waiting)
    job_id = _queued(db, queue)
    received = cast(JobMessage, queue.receive())
    first = JobMessage(
        job_id=received.job_id,
        user_id=received.user_id,
        idempotency_key=received.idempotency_key,
        receipt_handle="receipt-a",
        receive_count=1,
    )
    second = JobMessage(
        job_id=received.job_id,
        user_id=received.user_id,
        idempotency_key=received.idempotency_key,
        receipt_handle="receipt-b",
        receive_count=2,
    )

    owner = threading.Thread(target=handle, args=(deps, first))
    replacement = threading.Thread(target=handle, args=(deps, second))
    owner.start()
    assert planner_started.wait(timeout=1.0)
    replacement.start()
    owner.join(timeout=5.0)
    replacement.join(timeout=10.0)

    assert not owner.is_alive() and not replacement.is_alive()
    assert fake.roles.count("planner") == 1
    assert _actions(db, job_id).count("plan_produced") == 1
    assert web.queries == list(_SUBTOPICS)
    assert _status(db, job_id) == "awaiting_approval"
    assert any(handle_ == "receipt-b" for handle_, _timeout in queue.visibility_extensions)
    assert "receipt-a" not in queue.deleted
    assert "receipt-b" in queue.deleted


def test_sigterm_while_waiting_for_the_execution_lock_starts_no_graph_work(
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    fake: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BusyLock:
        def __init__(self, _engine: Engine, _job_id: str) -> None:
            pass

        def __enter__(self) -> BusyLock:
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

        def try_acquire(self) -> bool:
            waiting.set()
            return False

    waiting = threading.Event()
    monkeypatch.setattr(database_locks, "job_execution_lock", BusyLock)
    monkeypatch.setattr(worker, "JOB_LOCK_RETRY_INTERVAL_S", 0.001)
    job_id = _queued(db, queue)
    message = cast(JobMessage, queue.receive())

    thread = threading.Thread(target=handle, args=(deps, message))
    thread.start()
    assert waiting.wait(timeout=1.0)
    deps.shutdown.requested = True
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert fake.roles == []
    assert _status(db, job_id) == "queued"
    assert queue.deleted == []


def test_a_gate_outcome_stops_the_heartbeat_before_acknowledgement(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renewed = threading.Event()
    extend_visibility = queue.extend_visibility
    original_gate = queries.record_gate_opened
    monkeypatch.setattr(worker, "visibility_renewal_interval", lambda _timeout: 0.01)

    def renew(message: Any, *, visibility_timeout_s: int) -> None:
        extend_visibility(message, visibility_timeout_s=visibility_timeout_s)
        renewed.set()

    def hold_gate(*args: Any, **kwargs: Any) -> None:
        assert renewed.wait(timeout=1.0)
        original_gate(*args, **kwargs)

    monkeypatch.setattr(queue, "extend_visibility", renew)
    monkeypatch.setattr(queries, "record_gate_opened", hold_gate)
    job_id = _queued(db, queue)

    assert _drain(deps, queue) == 1
    assert _status(db, job_id) == "awaiting_approval"
    assert queue.deleted == [f"key-{job_id}"]

    renewals_at_delete = len(queue.visibility_extensions)
    time.sleep(0.03)
    assert len(queue.visibility_extensions) == renewals_at_delete


def test_a_terminal_outcome_stops_the_heartbeat_before_acknowledgement(
    web: RecordedWeb,
    deps: WorkerDeps,
    db: Engine,
    queue: FakeQueue,
    bucket: FakeS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _queued(db, queue)
    assert _drain(deps, queue) == 1
    _decide(db, deps.graph, job_id, decision="approve")
    _resume(queue, deps.graph, job_id)

    renewed = threading.Event()
    extend_visibility = queue.extend_visibility
    put_object = bucket.put_object
    monkeypatch.setattr(worker, "visibility_renewal_interval", lambda _timeout: 0.01)

    def renew(message: Any, *, visibility_timeout_s: int) -> None:
        extend_visibility(message, visibility_timeout_s=visibility_timeout_s)
        renewed.set()

    def hold_export(**kwargs: Any) -> dict[str, Any]:
        assert renewed.wait(timeout=1.0)
        return put_object(**kwargs)

    monkeypatch.setattr(queue, "extend_visibility", renew)
    monkeypatch.setattr(bucket, "put_object", hold_export)

    assert _drain(deps, queue) == 1
    assert _status(db, job_id) == "approved"
    assert queue.deleted[-1].startswith(f"{job_id}:")

    renewals_at_delete = len(queue.visibility_extensions)
    time.sleep(0.03)
    assert len(queue.visibility_extensions) == renewals_at_delete


def test_the_job_stopped_by_a_signal_resumes_without_replaying_its_node(
    web: RecordedWeb,
    config: Config,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    fake: FakeLLM,
    bucket: FakeS3,
    restored_signals: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of stopping cleanly: continuing is free.

    The stopped job has a checkpoint with no pending interrupt, which is ADR 0010 decision 5's
    fourth branch - `invoke(None)`. The Planner does **not** run again, which is the whole point
    of stopping between nodes rather than mid-node, and the job goes on to the gate and out.
    """
    shutdown = Shutdown()
    shutdown.install()
    deps = WorkerDeps(
        config=config,
        engine=db,
        graph=graph,
        queue=cast(Any, queue),
        final_delivery_at=FINAL_DELIVERY_AT,
        shutdown=shutdown,
    )
    job_id = _queued(db, queue)
    original = queries.record_plan
    signalled = [False]

    def signal_once_mid_node(*args: Any, **kwargs: Any) -> None:
        if not signalled[0]:  # the first delivery only; the redelivery runs undisturbed
            signalled[0] = True
            signal.raise_signal(signal.SIGTERM)
        original(*args, **kwargs)

    # Not undone afterwards: `monkeypatch` is one instance per test, so undoing it here would
    # also uninstall the `web` fixture's recorded responses and send the Researcher at the
    # real internet.
    monkeypatch.setattr(queries, "record_plan", signal_once_mid_node)
    run(deps)
    planned = fake.roles.count("planner")

    # A fresh worker - the signal is not carried across a restart - takes the redelivery.
    shutdown.requested = False
    assert queue.redeliver() == 1
    _drain(deps, queue)

    assert fake.roles.count("planner") == planned  # the completed node was not replayed
    assert _actions(db, job_id).count("plan_produced") == 1
    assert _status(db, job_id) == "awaiting_approval"
    assert queue.deleted == [f"key-{job_id}"]  # the gate is a durable outcome, so now it is

    _decide(db, deps.graph, job_id, decision="approve")
    _resume(queue, deps.graph, job_id)
    _drain(deps, queue)

    row = queries.read_job(db, job_id)
    assert row is not None
    assert row.status == "approved" and row.completed_at is not None
    assert bucket.body(f"reports/{job_id}.json") == row.report_json


def test_a_signal_after_the_gate_still_acknowledges_the_durable_outcome(
    web: RecordedWeb,
    config: Config,
    graph: ResearchGraph,
    db: Engine,
    queue: FakeQueue,
    restored_signals: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping early must not turn a finished delivery into an unfinished one.

    ADR 0010 decision 6's three outcomes are unchanged: if the graph ran out of nodes - because
    it interrupted at the gate or because the job ended - that delivery is done and the message
    is deleted, whatever the flag says. The check sits at the *top* of the update loop, so it can
    only stop work that has not started; it never intercepts a stream that has already ended.
    """
    shutdown = Shutdown()
    shutdown.install()
    deps = WorkerDeps(
        config=config,
        engine=db,
        graph=graph,
        queue=cast(Any, queue),
        final_delivery_at=FINAL_DELIVERY_AT,
        shutdown=shutdown,
    )
    job_id = _queued(db, queue)
    original = queries.record_gate_opened

    def signal_at_the_gate(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        signal.raise_signal(signal.SIGTERM)  # the last node of this delivery

    monkeypatch.setattr(queries, "record_gate_opened", signal_at_the_gate)

    assert run(deps) == 1

    assert shutdown.requested
    assert _status(db, job_id) == "awaiting_approval"
    assert queue.deleted == [f"key-{job_id}"]  # a human-gate outcome is still an outcome


def test_a_second_signal_is_not_escalated_to_a_hard_exit(
    deps: WorkerDeps, restored_signals: None
) -> None:
    # The container runtime already escalates - SIGTERM then SIGKILL after its grace period -
    # and a worker that killed itself faster would only lose the checkpoint the first signal
    # was protecting.
    deps.shutdown.install()

    signal.raise_signal(signal.SIGTERM)
    signal.raise_signal(signal.SIGINT)

    assert deps.shutdown.requested
    assert run(deps) == 0  # and it simply does no more work


def test_a_receive_that_raises_pauses_instead_of_spinning(
    deps: WorkerDeps, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A queue that cannot be reached is a hot loop unless something says otherwise.

    The pause is deliberately not a backoff schedule and not a bound to give up at: the worker
    has one job to do, and an operator watching the DLQ alarm is the escalation. What this
    pins is that the loop survives the error, waits, and tries again.
    """
    slept: list[float] = []
    receives = [0]

    def receive() -> None:
        receives[0] += 1
        if receives[0] == 1:
            raise QueueError("the queue is unreachable")
        deps.shutdown.requested = True  # the second attempt ends the loop rather than the test

    monkeypatch.setattr(deps.queue, "receive", receive)
    # The module's own `time`, so the pause is observed rather than waited out. Patching
    # `time.sleep` globally would stop every other library in the process from sleeping too.
    monkeypatch.setattr("worker.time.sleep", slept.append)

    with caplog.at_level(logging.ERROR):
        assert run(deps) == 0

    assert receives[0] == 2  # it tried again rather than giving up
    assert slept == [RECEIVE_ERROR_PAUSE_S]
    assert "could not receive" in caplog.text


def test_an_empty_receive_is_not_a_handled_message(
    deps: WorkerDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Long polling answers "nothing" all day on an idle queue, and that is not work.
    answers = [None, None]

    def receive() -> None:
        if not answers:
            deps.shutdown.requested = True
            return None
        return answers.pop()

    monkeypatch.setattr(deps.queue, "receive", receive)

    assert run(deps) == 0


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


def test_the_worker_accepts_the_queue_compose_creates() -> None:
    settings = check_queue(cast(Any, _StubQueue(_attributes())))

    assert settings == QueueSettings(visibility_timeout_s=1800, final_delivery_at=3)
    assert visibility_renewal_interval(settings.visibility_timeout_s) == 600


def test_a_standard_queue_is_refused_because_fifo_is_load_bearing() -> None:
    # FIFO with `MessageGroupId = job_id` is what keeps one job to one writer, which
    # ADR 0005's `_write_findings` is allowed to assume. A standard queue breaks it silently.
    with pytest.raises(RuntimeError, match="FIFO"):
        check_queue(cast(Any, _StubQueue(_attributes(FifoQueue="false"))))


def test_a_non_positive_visibility_lease_is_refused() -> None:
    with pytest.raises(RuntimeError, match="positive visibility"):
        check_queue(cast(Any, _StubQueue(_attributes(VisibilityTimeout="0"))))


def test_a_visibility_lease_too_short_for_one_bounded_renewal_is_refused() -> None:
    with pytest.raises(RuntimeError, match="bounded SQS renewal call"):
        check_queue(cast(Any, _StubQueue(_attributes(VisibilityTimeout="279"))))


def test_a_queue_with_no_redrive_policy_starts_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attributes = _attributes()
    del attributes["RedrivePolicy"]

    with caplog.at_level(logging.WARNING):
        assert check_queue(cast(Any, _StubQueue(attributes))).final_delivery_at is None

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
