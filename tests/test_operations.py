"""
WHY THIS FILE EXISTS
    `operations.py` is the only code in this repository whose whole purpose is to change durable
    state that nothing else is going to change - a `jobs` row whose worker died without writing
    it, and a message three deliveries could not make work. Getting it wrong in the safe
    direction leaves an operator without a repair; getting it wrong in the unsafe direction
    fails a job somebody is still running. So the tests below are weighted towards what must
    **not** happen.

    Five groups, in the order a failure hurts.

    **Age selects; it never authorises** (ADR 0021 decision 2). `select_candidates` is driven at
    its boundary, `awaiting_approval` is proven not to be a candidate at all, and the one outcome
    that fails a job is proven unreachable without a dead-lettered message. There is no test that
    fails a job for being old, because there is no code that can.

    **The fence decides, and it decides before anything is read.** A busy lock writes nothing. A
    lock double that changes the row *while it is being acquired* proves the reread happens after
    acquisition rather than before it - which is the whole of ADR 0016 decision 3 in one test.

    **`decide` and `replay_verdict` are pure**, so the interesting states are dataclass literals:
    a terminal checkpoint beside a `running` row, a pending interrupt beside one, a job that has
    never run. Arranging those through three real services would be slower and would prove less.

    **The checkpoint reader is checked against the real thing.** `read_checkpoint` finds a
    pending interrupt by reading a LangGraph channel name directly, because building a compiled
    graph in an operator tool would drag five agents and an LLM client into a process that must
    not reach a model. So one test drives the **real graph** to the **real gate** and asserts
    this reader and `graph.get_state().interrupts` agree. A LangGraph upgrade that moves the
    channel fails here rather than quietly reporting that no reviewer is waiting.

    **A replay puts back the message that came out.** The group id and the deduplication id are
    asserted on the resent message, because losing either is losing ADR 0010 decision 4's
    ordering or ADR 0007's gate-visit key.

WHO CALLS IT
    pytest. No service, no network, no AWS: SQLite for the rows, `InMemorySaver` for the
    checkpoints, `FakeQueue` for both queues, and the FakeLLM and recorded web for the one test
    that runs a graph.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from dbharness import migrated_engine, new_job_id
from fakes import FakeQueue, FakeS3, imported_modules
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
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.memory import InMemorySaver
from openai import OpenAI
from sqlalchemy.engine import Engine

import operations
from artifacts import ArtifactStore
from config import Config, load_config
from database import locks as database_locks
from database import queries
from database.schema import audit_events, jobs
from graph.build import ResearchGraph, build_graph
from graph.state import run_config, state_serde
from jobqueue import JobMessage, QueueError
from llm_client import LLMClient
from operations import (
    CheckpointView,
    JobEvidence,
    Replayer,
    decide,
    read_checkpoint,
    reconciler_for,
    replay_verdict,
    select_candidates,
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

USER = "22222222-2222-4222-8222-222222222222"
OPERATOR = "alice@example.com"
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


# --- What a job looks like at rest ------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Engine:
    return migrated_engine(tmp_path)


@pytest.fixture
def saver() -> InMemorySaver:
    return InMemorySaver(serde=state_serde())


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


def a_job(
    db: Engine,
    *,
    status: str = "queued",
    question: str = "Compare A and B",
    age_hours: float = 0.0,
) -> str:
    """A row exactly as `POST /jobs` leaves it, moved to `status` and aged if asked.

    **Every timestamp is stamped explicitly**, rather than left to `func.now()`. The tests below
    compare against a fixed `NOW`, and a row whose age depended on the wall clock would select
    or not select itself depending on the hour the suite happened to run.
    """
    job_id = new_job_id()
    queries.create_job(
        db,
        job_id=job_id,
        user_id=USER,
        question=question,
        idempotency_key=f"key-{job_id}",
        actor=USER,
    )
    if status != "queued":
        queries.set_job_status(db, job_id=job_id, status=cast(Any, status))
    _stamp(db, job_id, NOW - timedelta(hours=age_hours))
    return job_id


def _stamp(db: Engine, job_id: str, when: datetime) -> None:
    """Move `jobs.created_at` and every audit row this job has to one instant.

    Through the table objects rather than raw SQL, because `job_id` is a `Uuid` column and
    SQLAlchemy stores it in SQLite as 32 hex characters with no dashes - a hand-written
    `WHERE job_id = :id` silently matches nothing and the test passes for the wrong reason.
    """
    with db.begin() as conn:
        conn.execute(sa.update(jobs).where(jobs.c.job_id == job_id).values(created_at=when))
        conn.execute(
            sa.update(audit_events).where(audit_events.c.job_id == job_id).values(created_at=when)
        )


def worked_recently(db: Engine, job_id: str, *, minutes_ago: int) -> None:
    """One durable node event at a known time, which is what `select_candidates` measures from.

    A job whose row is hours old but which committed an audit row a minute ago is working, and
    the whole point of measuring from the last event is that it must not be selected.
    """
    with db.begin() as conn:
        conn.execute(
            sa.insert(audit_events).values(
                job_id=job_id,
                actor="system",
                action="subtopic_researched",
                detail={"subtopic": "s1"},
                created_at=NOW - timedelta(minutes=minutes_ago),
            )
        )


def a_checkpoint(saver: InMemorySaver, job_id: str, **values: Any) -> Any:
    """Put one checkpoint in place through the real saver, with no graph involved.

    `InMemorySaver` stores each channel as a versioned blob and rebuilds `channel_values` from
    `channel_versions` on the way out, so a hand-written checkpoint has to carry versions for
    every channel it wants read back. That is the saver's real storage shape rather than a
    detail of the fake: the same is true of `PostgresSaver`.
    """
    channels: dict[str, Any] = {
        "status": "running",
        "failure_reason": None,
        "quality_flag": None,
        "revision_count": 0,
        "llm_calls_used": 0,
        **values,
    }
    versions = {name: "1" for name in channels}
    checkpoint = {
        "v": 4,
        "id": f"checkpoint-{job_id}",
        "ts": NOW.isoformat(),
        "channel_values": channels,
        "channel_versions": versions,
        "versions_seen": {},
    }
    return saver.put(_thread(job_id), cast(Any, checkpoint), cast(Any, {}), cast(Any, versions))


def an_interrupt(saver: InMemorySaver, job_id: str) -> None:
    """The pending write LangGraph leaves behind when a node calls `interrupt()`.

    Written through the saver's own `put_writes`, so what `read_checkpoint` sees is a real
    pending write on a real checkpoint - and the test further down proves this is the same thing
    the real graph produces at the real gate.
    """
    stored = saver.get_tuple(_thread(job_id))
    assert stored is not None
    saver.put_writes(
        stored.config, [(operations.INTERRUPT_CHANNEL, {"value": "approve me"})], "human_gate"
    )


def _thread(job_id: str) -> Any:
    """`run_config`, plus the namespace the savers require on a write."""
    config = dict(run_config(job_id))
    config["configurable"] = {**cast(Any, config["configurable"]), "checkpoint_ns": ""}
    return config


def audit_actions(db: Engine, job_id: str) -> list[str]:
    return [event.action for event in queries.read_audit_events(db, job_id)]


def status_of(db: Engine, job_id: str) -> str:
    row = queries.read_job(db, job_id)
    assert row is not None
    return cast(str, row.status)


class BusyLock:
    """An execution fence somebody else is holding. `try_acquire` never succeeds."""

    def __init__(self, _engine: Engine, job_id: str) -> None:
        self.job_id = job_id
        self.acquired = False

    def __enter__(self) -> BusyLock:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def try_acquire(self) -> bool:
        return False

    def release(self) -> None:
        return None


# --- 1. Candidate selection is a clock and nothing else ---------------------------------


def test_only_queued_and_running_rows_are_ever_candidates(db: Engine) -> None:
    """**`awaiting_approval` is not in the list, and that is the safety property.** A job may
    wait at the human gate for days; a sweep able to touch it would be the one design that could
    close a review nobody answered (ADR 0021 decision 2)."""
    assert operations.CANDIDATE_STATUSES == ("queued", "running")

    for status in ("queued", "running", "awaiting_approval"):
        a_job(db, status=status, age_hours=10)

    selected = select_candidates(db, min_age_seconds=0, now=NOW)
    assert sorted(row.status for row in selected) == ["queued", "running"]


def test_a_finished_job_is_never_a_candidate_whatever_its_status_says(db: Engine) -> None:
    """`completed_at` is what "this job has ended" means - `finish_job` is its only writer - so
    a row whose status was left behind by a dead process stops being selectable the moment the
    job genuinely finishes."""
    job_id = a_job(db, status="running", age_hours=10)
    queries.finish_job(
        db,
        job_id=job_id,
        status="approved",
        failure_reason=None,
        quality_flag=None,
        revision_count=0,
        llm_calls_used=4,
    )

    assert select_candidates(db, min_age_seconds=0, now=NOW) == []


def test_age_is_measured_from_the_last_durable_event_not_from_submission(db: Engine) -> None:
    """A job submitted this morning that checkpointed a node a minute ago is working. Selecting
    it by `created_at` would put a live job in front of an operator on every single run."""
    working = a_job(db, status="running", age_hours=10)
    worked_recently(db, working, minutes_ago=1)

    stalled = a_job(db, status="running", age_hours=10)

    selected = {row.job_id for row in select_candidates(db, min_age_seconds=3600, now=NOW)}
    assert selected == {stalled}


def test_the_threshold_is_a_boundary_and_the_default_is_derived_from_redelivery() -> None:
    """7200 is not a taste: three deliveries of the queue's 1800-second visibility window is
    5400 seconds, and the default is that plus half an hour, so a job still working its way
    through legitimate redelivery is not a candidate at all."""
    config = load_config(_ENV)
    assert config.stale_job_min_age_seconds == 7200
    assert config.stale_job_min_age_seconds > 3 * 1800


def test_selecting_one_job_still_applies_the_age_filter(db: Engine) -> None:
    """`--job-id` narrows the sweep; it is not a way round the rule. An operator who really
    means it passes `--min-age-seconds 0`, which is one visible decision rather than a hidden
    exemption."""
    job_id = a_job(db, status="running")

    assert select_candidates(db, min_age_seconds=3600, now=NOW, job_id=job_id) == []
    assert len(select_candidates(db, min_age_seconds=0, now=NOW, job_id=job_id)) == 1


# --- 2. The fence decides, before anything is read --------------------------------------


def test_a_busy_execution_fence_writes_nothing_at_all(
    db: Engine, saver: InMemorySaver, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single most important test in this file. A worker holding ADR 0016's advisory lock is
    running that job, so a reconciler that wrote anything would be the second writer the lock
    exists to prevent."""
    job_id = a_job(db, status="running")
    monkeypatch.setattr(database_locks, "job_execution_lock", BusyLock)

    reconciler = reconciler_for(
        db, saver, actor=OPERATOR, apply=True, queue=cast(Any, queue), dead_lettered=[job_id]
    )
    result = reconciler.reconcile(job_id)

    assert result.outcome == "owned"
    assert result.applied is False
    assert status_of(db, job_id) == "running"
    assert "job_reconciled" not in audit_actions(db, job_id)
    assert queue.sent == []


def test_durable_state_is_reread_after_the_fence_is_acquired_not_before(
    db: Engine, saver: InMemorySaver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0016 decision 3, in one test: a reconciler that waited behind a live worker must not
    act on state it observed before that worker finished.

    The lock double finishes the job *while it is being acquired*. If the row were read first,
    the reconciler would decide from a `running` job and write a terminal row over a terminal
    row; because it is read afterwards, it sees the finished job and does nothing.
    """
    job_id = a_job(db, status="running")

    class FinishesOnAcquire(BusyLock):
        def try_acquire(self) -> bool:
            queries.finish_job(
                db,
                job_id=self.job_id,
                status="approved",
                failure_reason=None,
                quality_flag=None,
                revision_count=1,
                llm_calls_used=9,
            )
            return True

    monkeypatch.setattr(database_locks, "job_execution_lock", FinishesOnAcquire)
    result = reconciler_for(
        db, saver, actor=OPERATOR, apply=True, queue=None, dead_lettered=[job_id]
    ).reconcile(job_id)

    assert result.outcome == "no_change"
    assert status_of(db, job_id) == "approved"


def test_a_reconciliation_that_raises_is_reported_and_does_not_end_the_sweep(
    db: Engine, saver: InMemorySaver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One poisonous row must not leave the other nineteen unexamined, and the operator needs an
    inventory rather than a traceback."""
    first = a_job(db, status="running", age_hours=10)
    second = a_job(db, status="running", age_hours=10)

    def explode(_engine: Engine, job_id: str) -> BusyLock:
        if job_id == first:
            raise RuntimeError("the lock connection went away")
        return BusyLock(_engine, job_id)

    monkeypatch.setattr(database_locks, "job_execution_lock", explode)
    results = reconciler_for(db, saver, actor=OPERATOR, apply=True, queue=None).sweep(
        select_candidates(db, min_age_seconds=0, now=NOW)
    )

    outcomes = {result.job_id: result.outcome for result in results}
    assert outcomes == {first: "errored", second: "owned"}


# --- 3. What the evidence decides -------------------------------------------------------


def evidence(**overrides: Any) -> JobEvidence:
    base: dict[str, Any] = {
        "job_id": "job",
        "status": "running",
        "completed_at": None,
        "checkpoint": None,
        "in_dead_letter_queue": False,
    }
    return JobEvidence(**{**base, **overrides})


def view(**overrides: Any) -> CheckpointView:
    base: dict[str, Any] = {
        "status": "running",
        "failure_reason": None,
        "quality_flag": None,
        "revision_count": 0,
        "llm_calls_used": 0,
        "waiting_at_gate": False,
    }
    return CheckpointView(**{**base, **overrides})


def test_a_pending_interrupt_beside_a_running_row_is_repaired_to_the_gate() -> None:
    """The one state nothing else recovers from: both gate routes refuse a job that is not
    `awaiting_approval`, so a pending interrupt beside a `running` row is a gate no person can
    ever answer. It is checked before every other repair for that reason."""
    assert decide(evidence(checkpoint=view(waiting_at_gate=True))).outcome == "repaired_gate"


def test_a_row_that_already_matches_the_interrupt_is_left_alone() -> None:
    matching = evidence(status="awaiting_approval", checkpoint=view(waiting_at_gate=True))
    assert decide(matching).outcome == "no_change"


def test_a_terminal_checkpoint_beside_an_unfinished_row_repairs_the_row() -> None:
    ended = evidence(checkpoint=view(status="failed", failure_reason="job_timeout"))
    assert decide(ended).outcome == "repaired_terminal"


def test_a_pending_interrupt_wins_over_a_dead_lettered_message() -> None:
    """Repair before failure. A reviewer owed a gate gets the gate, even though the queue has
    given up on the message that would have delivered them there."""
    both = evidence(checkpoint=view(waiting_at_gate=True), in_dead_letter_queue=True)
    assert decide(both).outcome == "repaired_gate"


def test_a_job_is_failed_only_with_a_dead_lettered_message_as_evidence() -> None:
    """**The property that makes "no job is failed for being old" true of the code.** The same
    evidence with the message absent is `skipped`, and there is no third input that could turn
    it into a failure."""
    orphaned = evidence(checkpoint=view(), in_dead_letter_queue=True)
    assert decide(orphaned).outcome == "failed"

    assert decide(evidence(checkpoint=view())).outcome == "skipped"


def test_a_queued_job_nothing_ever_ran_is_re_enqueued_rather_than_failed() -> None:
    """ADR 0010 decision 10 keeps the row when an enqueue fails, because it holds the
    idempotency key a re-enqueue targets. This is the other end of that decision."""
    assert decide(evidence(status="queued")).outcome == "requeued"


def test_a_queued_job_with_a_checkpoint_is_not_re_enqueued() -> None:
    """A checkpoint means something did run it, so "nothing picked this up" is false and the
    honest answer is that redelivery is still the recovery path."""
    assert decide(evidence(status="queued", checkpoint=view())).outcome == "skipped"


def test_a_finished_job_and_a_missing_row_are_both_left_alone() -> None:
    assert decide(evidence(completed_at=NOW, status="approved")).outcome == "no_change"
    assert decide(evidence(status=None)).outcome == "skipped"


@pytest.mark.parametrize(
    "case",
    [
        evidence(checkpoint=view()),
        evidence(status="queued", checkpoint=view()),
        evidence(status=None),
    ],
)
def test_ambiguous_evidence_never_mutates(case: JobEvidence) -> None:
    assert decide(case).mutates is False


# --- 4. Applying a decision, once ---------------------------------------------------------


def test_a_terminal_checkpoint_writes_the_row_the_worker_would_have(
    db: Engine, saver: InMemorySaver
) -> None:
    """The repair has to produce the same row `worker._finalise` produces, values included -
    otherwise a recovered job reads differently from one that recovered itself."""
    job_id = a_job(db, status="running")
    a_checkpoint(
        saver,
        job_id,
        status="failed",
        failure_reason="job_timeout",
        quality_flag="below_threshold",
        revision_count=2,
        llm_calls_used=41,
    )

    result = reconciler_for(db, saver, actor=OPERATOR, apply=True, queue=None).reconcile(job_id)

    assert result.outcome == "repaired_terminal"
    row = queries.read_job(db, job_id)
    assert row is not None
    assert (row.status, row.revision_count, row.llm_calls_used) == ("failed", 2, 41)
    assert row.completed_at is not None
    assert row.quality_flag == "below_threshold"

    finished = next(
        event for event in queries.read_audit_events(db, job_id) if event.action == "job_finished"
    )
    assert finished.detail == {"status": "failed", "failure_reason": "job_timeout"}


def test_a_dead_lettered_orphan_is_failed_with_the_reason_the_worker_uses(
    db: Engine, saver: InMemorySaver
) -> None:
    """`job_dead_lettered` rather than a new word: it is what `worker._finalise` writes on a
    final delivery, and this sweep exists for the case where the worker never got to write it."""
    job_id = a_job(db, status="running")
    a_checkpoint(saver, job_id)

    result = reconciler_for(
        db, saver, actor=OPERATOR, apply=True, queue=None, dead_lettered=[job_id]
    ).reconcile(job_id)

    assert result.outcome == "failed"
    finished = next(
        event for event in queries.read_audit_events(db, job_id) if event.action == "job_finished"
    )
    assert finished.detail["failure_reason"] == "job_dead_lettered"


def test_a_re_enqueued_job_gets_the_message_the_api_would_have_sent(
    db: Engine, saver: InMemorySaver, queue: FakeQueue
) -> None:
    """Same group id, same deduplication id, same three identifiers - so ADR 0010 decision 4's
    per-job ordering survives a recovery."""
    job_id = a_job(db, status="queued")

    result = reconciler_for(
        db, saver, actor=OPERATOR, apply=True, queue=cast(Any, queue)
    ).reconcile(job_id)

    assert result.outcome == "requeued"
    sent = queue.only()
    assert sent.group_id == job_id
    assert sent.deduplication_id == f"key-{job_id}"
    assert set(sent.body) == {"job_id", "user_id", "idempotency_key"}
    assert status_of(db, job_id) == "queued"


def test_the_repair_of_a_gate_makes_the_job_answerable_again(
    db: Engine, saver: InMemorySaver
) -> None:
    job_id = a_job(db, status="running")
    a_checkpoint(saver, job_id)
    an_interrupt(saver, job_id)

    result = reconciler_for(db, saver, actor=OPERATOR, apply=True, queue=None).reconcile(job_id)

    assert result.outcome == "repaired_gate"
    assert status_of(db, job_id) == "awaiting_approval"


def test_every_mutation_records_who_made_it(db: Engine, saver: InMemorySaver) -> None:
    """`ck_audit_events_actor` refuses `unknown` one layer down, and the two repairs that finish
    no job would otherwise leave no record of the operator at all (ADR 0021 decision 5)."""
    job_id = a_job(db, status="running")
    a_checkpoint(saver, job_id)
    an_interrupt(saver, job_id)

    reconciler_for(db, saver, actor=OPERATOR, apply=True, queue=None).reconcile(job_id)

    reconciled = next(
        event for event in queries.read_audit_events(db, job_id) if event.action == "job_reconciled"
    )
    assert reconciled.actor == OPERATOR
    assert reconciled.detail["outcome"] == "repaired_gate"
    assert reconciled.detail["previous_status"] == "running"


def test_running_the_sweep_again_changes_nothing_and_writes_no_second_audit_row(
    db: Engine, saver: InMemorySaver
) -> None:
    """Idempotency, asserted on both halves: the second run decides nothing to do, and even a
    forced repeat of the same outcome cannot double the trail."""
    job_id = a_job(db, status="running")
    a_checkpoint(saver, job_id)
    an_interrupt(saver, job_id)

    reconciler = reconciler_for(db, saver, actor=OPERATOR, apply=True, queue=None)
    first = reconciler.reconcile(job_id)
    second = reconciler.reconcile(job_id)

    assert (first.outcome, second.outcome) == ("repaired_gate", "no_change")
    assert audit_actions(db, job_id).count("job_reconciled") == 1

    # And directly: the guard is on (job_id, outcome), not on "did anything change".
    queries.record_reconciliation(
        db, job_id=job_id, actor=OPERATOR, outcome="repaired_gate", detail={"reason": "again"}
    )
    assert audit_actions(db, job_id).count("job_reconciled") == 1


def test_two_different_outcomes_are_both_recorded(db: Engine) -> None:
    """The guard must not silence a genuinely different repair later in a job's life."""
    job_id = a_job(db, status="running")
    for outcome in ("repaired_gate", "failed"):
        queries.record_reconciliation(
            db, job_id=job_id, actor=OPERATOR, outcome=outcome, detail={"reason": "x"}
        )
    assert audit_actions(db, job_id).count("job_reconciled") == 2


# --- 5. Dry run writes nothing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "interrupted", "expected"),
    [
        ("running", True, "repaired_gate"),
        ("queued", False, "requeued"),
    ],
)
def test_a_dry_run_reports_the_action_and_performs_none_of_it(
    db: Engine,
    saver: InMemorySaver,
    queue: FakeQueue,
    status: str,
    interrupted: bool,
    expected: str,
) -> None:
    """The default mode of both mutating tools. It has to reach the same decision as `--apply` -
    an operator reads this report and then runs the command again - while touching nothing."""
    job_id = a_job(db, status=status)
    if interrupted:
        a_checkpoint(saver, job_id)
        an_interrupt(saver, job_id)

    result = reconciler_for(
        db, saver, actor=OPERATOR, apply=False, queue=cast(Any, queue)
    ).reconcile(job_id)

    assert result.outcome == expected
    assert result.applied is False
    assert result.mutating is True
    assert status_of(db, job_id) == status
    assert "job_reconciled" not in audit_actions(db, job_id)
    assert queue.sent == []


def test_a_sweep_with_no_queue_cannot_fail_or_re_enqueue_anything(
    db: Engine, saver: InMemorySaver
) -> None:
    """Absence narrows the sweep in the safe direction, and says so rather than erroring.

    With no dead-letter queue there is no evidence that a message is never coming back, so
    nothing can be failed. With no queue at all nothing can be sent, so a job that would have
    been re-enqueued is reported as skipped with the reason - which is what an operator running
    against a half-configured environment needs to see.
    """
    queued = a_job(db, status="queued")
    running = a_job(db, status="running")
    a_checkpoint(saver, running)

    reconciler = reconciler_for(db, saver, actor=OPERATOR, apply=True, queue=None)

    assert reconciler.reconcile(running).outcome == "skipped"

    withheld = reconciler.reconcile(queued)
    assert withheld.outcome == "skipped"
    assert "no queue" in withheld.reason
    assert status_of(db, queued) == "queued"


# --- 6. The checkpoint reader agrees with the real graph ----------------------------------

_SUBTOPICS = (
    "What is A's cloud revenue?",
    "What is B's cloud revenue?",
    "How do their partnerships compare?",
)


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> RecordedWeb:
    recorded = RecordedWeb()
    for index, question in enumerate(_SUBTOPICS, 1):
        page = Page(
            url=f"https://source-{index}.example/report",
            title=f"Source {index}",
            text=f"Source {index} reported cloud revenue of $1.2bn in FY24.\nBoilerplate.",
        )
        recorded.index(question, page)
    recorded.install(monkeypatch)
    return recorded


@pytest.fixture
def gated_graph(db: Engine, saver: InMemorySaver, web: RecordedWeb) -> Iterator[ResearchGraph]:
    config: Config = load_config(_ENV)
    fake = FakeLLM(
        supervisor=[
            decision("planner"),
            *[decision("researcher")] * 3,
            decision("synthesizer"),
            decision("fact_checker"),
        ],
        planner=[plan(*_SUBTOPICS)],
        researcher=[quote_the_page()] * 6,
        synthesizer=[draft(1)],
        fact_checker=[verdict_batch(quote="Source 1 reported cloud revenue of $1.2bn in FY24.")],
        reflection=[rubric()],
    )
    yield build_graph(
        config=config,
        llm=LLMClient(config, client=cast(OpenAI, fake)),
        db=db,
        artifacts=ArtifactStore("research-reports", client=FakeS3()),
        checkpointer=saver,
    )


def test_the_checkpoint_reader_finds_the_same_interrupt_the_graph_does(
    db: Engine, saver: InMemorySaver, gated_graph: ResearchGraph
) -> None:
    """**The test that guards a private LangGraph constant.**

    `read_checkpoint` finds a pending interrupt by looking for the `__interrupt__` channel in a
    checkpoint's pending writes, because building a compiled graph inside an operator tool would
    drag five agents and an LLM client into a process that must not reach a model. That is a
    real coupling to a dependency's internals, so it is asserted against the dependency: the
    real graph runs to the real human gate, and the two answers must agree.

    A LangGraph upgrade that renames the channel fails here, loudly, rather than making every
    reconciliation quietly report that no reviewer is waiting.
    """
    job_id = a_job(db, status="running")
    question = cast(Any, queries.read_job(db, job_id)).question
    from graph.state import new_state

    gated_graph.invoke(
        new_state(job_id=job_id, user_id=USER, question=question), run_config(job_id)
    )

    snapshot = gated_graph.get_state(run_config(job_id))
    assert snapshot.interrupts, "the harness did not reach the human gate"

    read = read_checkpoint(saver, job_id)
    assert read is not None
    assert read.waiting_at_gate is bool(snapshot.interrupts)
    assert read.llm_calls_used == snapshot.values["llm_calls_used"]
    assert read.status == snapshot.values["status"]


def test_a_job_nothing_has_ever_run_has_no_checkpoint(saver: InMemorySaver) -> None:
    assert read_checkpoint(saver, new_job_id()) is None


def test_a_checkpoint_with_no_pending_writes_is_not_waiting_for_a_reviewer(
    saver: InMemorySaver,
) -> None:
    """`pending_writes` is None on a checkpoint saved before any task wrote. That is a job that
    has run nothing, not one holding a person."""
    job_id = new_job_id()
    a_checkpoint(saver, job_id)
    stored = saver.get_tuple(run_config(job_id))
    assert isinstance(stored, CheckpointTuple)

    read = read_checkpoint(saver, job_id)
    assert read is not None and read.waiting_at_gate is False


# --- 7. Replay refuses more than it allows ------------------------------------------------


def a_dead_letter(job_id: str, *, kind: str = "start", deliveries: int = 3) -> JobMessage:
    dedup = f"key-{job_id}" if kind == "start" else f"{job_id}:7"
    return JobMessage(
        job_id=job_id,
        user_id=USER,
        idempotency_key=f"key-{job_id}",
        receipt_handle=dedup,
        receive_count=deliveries,
        group_id=job_id,
        deduplication_id=dedup,
    )


def test_a_terminal_job_is_never_replayed() -> None:
    assert replay_verdict(evidence(completed_at=NOW, status="failed")).allowed is False


def test_a_job_waiting_for_a_reviewer_is_never_replayed() -> None:
    """A gate decision moves this job, not a message. Replaying a start message here would be
    refused by the worker anyway, and a resume for a visit nobody answered has nothing to
    resume with."""
    assert replay_verdict(evidence(checkpoint=view(waiting_at_gate=True))).allowed is False
    assert replay_verdict(evidence(status="awaiting_approval")).allowed is False


def test_a_terminal_checkpoint_sends_you_to_the_reconciler_instead() -> None:
    ended = evidence(checkpoint=view(status="approved"))
    verdict = replay_verdict(ended)
    assert verdict.allowed is False
    assert "reconcile" in verdict.reason


def test_a_missing_row_is_never_replayed() -> None:
    assert replay_verdict(evidence(status=None)).allowed is False


@pytest.mark.parametrize("status", ["queued", "running"])
def test_an_unfinished_job_is_the_one_case_that_may_be_replayed(status: str) -> None:
    assert replay_verdict(evidence(status=status, checkpoint=view())).allowed is True


def test_a_live_execution_owner_stops_a_replay(
    db: Engine, saver: InMemorySaver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fourth delivery pushed at a job somebody is running is the one thing a recovery tool
    must never do."""
    job_id = a_job(db, status="running")
    a_checkpoint(saver, job_id)
    monkeypatch.setattr(database_locks, "job_execution_lock", BusyLock)

    jobs, dead_letters = FakeQueue(), FakeQueue()
    result = Replayer(
        engine=db,
        checkpoints=saver,
        queue=cast(Any, jobs),
        dead_letters=cast(Any, dead_letters),
        apply=True,
    ).replay(a_dead_letter(job_id))

    assert result.outcome == "owned"
    assert jobs.sent == []


def test_a_replay_puts_back_the_message_that_came_out(db: Engine, saver: InMemorySaver) -> None:
    """Same `MessageGroupId`, same `MessageDeduplicationId`, same body. Minting a new
    deduplication id would break ADR 0007's gate-visit key, under which one gate visit is one
    message however many times it is sent."""
    job_id = a_job(db, status="running")
    a_checkpoint(saver, job_id)
    message = a_dead_letter(job_id, kind="resume")

    jobs, dead_letters = FakeQueue(), FakeQueue()
    result = Replayer(
        engine=db,
        checkpoints=saver,
        queue=cast(Any, jobs),
        dead_letters=cast(Any, dead_letters),
        apply=True,
    ).replay(message)

    assert (result.outcome, result.applied, result.kind) == ("replayable", True, "resume")
    resent = jobs.only()
    assert resent.group_id == job_id
    assert resent.deduplication_id == f"{job_id}:7"
    assert resent.body == {
        "job_id": job_id,
        "user_id": USER,
        "idempotency_key": f"key-{job_id}",
    }
    assert dead_letters.deleted == [f"{job_id}:7"]


def test_a_dry_run_replay_sends_nothing_and_deletes_nothing(
    db: Engine, saver: InMemorySaver
) -> None:
    job_id = a_job(db, status="running")
    a_checkpoint(saver, job_id)

    jobs, dead_letters = FakeQueue(), FakeQueue()
    result = Replayer(
        engine=db,
        checkpoints=saver,
        queue=cast(Any, jobs),
        dead_letters=cast(Any, dead_letters),
        apply=False,
    ).replay(a_dead_letter(job_id))

    assert (result.outcome, result.applied) == ("replayable", False)
    assert jobs.sent == [] and dead_letters.deleted == []


def test_a_refused_replay_leaves_the_message_in_the_dead_letter_queue(
    db: Engine, saver: InMemorySaver
) -> None:
    """The alarm keeps firing, which is correct: nobody has dealt with it yet."""
    job_id = a_job(db, status="running")
    queries.finish_job(
        db,
        job_id=job_id,
        status="failed",
        failure_reason="job_dead_lettered",
        quality_flag=None,
        revision_count=0,
        llm_calls_used=0,
    )

    jobs, dead_letters = FakeQueue(), FakeQueue()
    result = Replayer(
        engine=db,
        checkpoints=saver,
        queue=cast(Any, jobs),
        dead_letters=cast(Any, dead_letters),
        apply=True,
    ).replay(a_dead_letter(job_id))

    assert result.outcome == "refused"
    assert dead_letters.deleted == []


def test_a_message_read_without_its_fifo_attributes_cannot_be_resent() -> None:
    """`receive()` does not ask for them, and guessing at a group id would silently break
    per-job ordering. Refusing is the only honest answer."""
    queue = FakeQueue()
    bare = JobMessage(
        job_id="job", user_id=USER, idempotency_key="key", receipt_handle="r", receive_count=1
    )
    with pytest.raises(QueueError):
        queue.resend(bare)


# --- 8. Reading a dead-letter queue does not empty it -------------------------------------


def test_inspecting_the_dead_letter_queue_puts_every_message_back() -> None:
    """SQS has no peek, so reading is what hides a message - and a tool that left them hidden
    would be an inspection that emptied the queue an alarm is watching."""
    dead_letters = FakeQueue()
    for _ in range(3):
        job_id = new_job_id()
        dead_letters.send_start(job_id=job_id, user_id=USER, idempotency_key=f"key-{job_id}")

    found = operations.read_dead_letter_messages(cast(Any, dead_letters), limit=10)

    assert len(found) == 3
    assert dead_letters.deleted == []
    assert len(dead_letters.pending()) == 3, "the messages were not released"
    assert all(handle for handle, timeout in dead_letters.visibility_extensions if timeout == 0)


def test_reading_stops_at_the_limit() -> None:
    dead_letters = FakeQueue()
    for _ in range(5):
        job_id = new_job_id()
        dead_letters.send_start(job_id=job_id, user_id=USER, idempotency_key=f"key-{job_id}")

    assert len(operations.read_dead_letter_messages(cast(Any, dead_letters), limit=2)) == 2


def test_a_messages_kind_is_read_from_the_key_it_was_deduplicated_on() -> None:
    """A start dedupes on the idempotency key, a resume on ADR 0007's `job_id:calls_used`. It is
    a label for a human: nothing branches on it."""
    job_id = new_job_id()
    assert operations.message_kind(a_dead_letter(job_id, kind="resume")) == "resume"
    assert operations.message_kind(a_dead_letter(job_id, kind="start")) == "start"

    bare = JobMessage(
        job_id=job_id, user_id=USER, idempotency_key="k", receipt_handle="r", receive_count=1
    )
    assert operations.message_kind(bare) == "unknown"


# --- 9. What this module is not allowed to reach ------------------------------------------


def test_the_operations_module_cannot_reach_a_model_or_a_graph() -> None:
    """The same boundary `scripts/reexport_job.py` holds, and for the same reason: a recovery
    tool that could re-bill a model would defeat the point of recovering rather than re-running.

    Asserted on the import list, because the property is about what this process is *able* to
    do. `graph.state` is fine - it is the state shape and the serializer - and `graph.build` is
    what would bring five agents and an LLM client with it.
    """
    imports = imported_modules(operations)

    assert "llm_client" not in imports
    assert "agents" not in imports
    assert "tools" not in imports
    assert "redisstore" not in imports
    assert "artifacts" not in imports

    # `graph` alone is too coarse - `graph.state` is the state shape and the serializer, and is
    # fine; `graph.build` is what would bring five agents and an LLM client with it. So the
    # import statements are read rather than the file, whose prose explains that distinction.
    source = Path(str(operations.__file__)).read_text(encoding="utf-8")
    modules = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "graph.state" in modules
    assert "graph.build" not in modules


def test_the_audit_action_is_declared_in_the_schema_and_its_migration() -> None:
    """The CHECK is built from `get_args(AuditAction)`, so the literal and the migration have to
    land together - `rev_0004` is what makes the first `job_reconciled` row writable."""
    from typing import get_args

    from database.schema import AuditAction

    assert "job_reconciled" in get_args(AuditAction)

    revision = (
        Path(__file__).resolve().parent.parent
        / "database"
        / "migrations"
        / "versions"
        / "rev_0004_audit_action_gains_job_reconciled.py"
    )
    assert revision.exists()
    assert "job_reconciled" in revision.read_text(encoding="utf-8")


def test_the_check_constraint_really_refuses_an_unknown_action(db: Engine) -> None:
    """The migration is what this proves, rather than the Python literal: `rev_0004` ran against
    this database, and the constraint it wrote allows the new action and nothing else."""
    job_id = a_job(db)
    with db.begin() as conn:
        conn.execute(
            sa.insert(audit_events).values(
                job_id=job_id, actor=OPERATOR, action="job_reconciled", detail={"outcome": "x"}
            )
        )
    with pytest.raises(sa.exc.IntegrityError), db.begin() as conn:
        conn.execute(
            sa.insert(audit_events).values(
                job_id=job_id, actor=OPERATOR, action="job_unreconciled", detail={}
            )
        )
