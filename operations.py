"""
WHY THIS FILE EXISTS
    Phase 5 block C's operational recovery, in one place: what a stale `jobs` row means, whether
    a dead-lettered message may be replayed, and what evidence either answer needs. The three
    scripts in `scripts/` are argument parsing and printing around the functions below - the
    decisions live here, so they can be driven by a test with no AWS, no LLM and no network at
    all.

    This closes the one residual condition the runtime has always accepted and recorded: a
    process killed hard on its last delivery leaves a `queued` or `running` row behind while its
    message goes to the dead-letter queue, and nothing was ever going to notice. ADR 0010
    decision 9 deferred that sweep to "a later operational phase"; this is it, and
    [ADR 0021](docs/adr/0021-stale-job-reconciliation-and-dlq-recovery.md) is the record.

    Four things here are the decision rather than plumbing.

    **Age selects a candidate; it never authorises a change.** `select_candidates` is a query
    with a clock in it and nothing else. Every mutation below additionally requires the per-job
    PostgreSQL execution fence, a reread of durable state taken *after* that fence was acquired,
    and evidence specific to the outcome. **No job is ever failed because it is old** - the
    closest thing to it needs a message sitting in the dead-letter queue to say so.

    **The fence is ADR 0016's, reused rather than reinvented.** A reconciler with its own notion
    of ownership would be a second answer to "who may write this job", and the first thing two
    ownership mechanisms do is disagree. `pg_try_advisory_lock` is taken once, without waiting:
    a busy lock is not a delay, it is the answer - some worker owns this job, so this run leaves
    it alone. PostgreSQL releases the lock when the holding backend dies, which is what makes a
    hard-killed worker recoverable at all.

    **Repair is preferred to failure, and both are preferred to guessing.** A checkpoint holding
    a pending interrupt means a reviewer is owed a gate, so the row is repaired to
    `awaiting_approval` rather than failed. A checkpoint that already reached a terminal status
    means the job finished and only its row is stale, so the row is finished from the
    checkpoint's own values. Only when neither is true **and** the queue has dead-lettered the
    job is it recorded as failed. Anything else is `skipped`, deliberately, and reported.

    **A dead-lettered message is replayed by a person, one at a time, or not at all.** There is
    no automatic redrive here and there must not be: the whole reason a message reached the DLQ
    is that three deliveries could not make it work, and replaying every such message is how an
    outage becomes an outage repeated four times. `replay_verdict` refuses more cases than it
    allows, and the message that goes back is the message that came out - same `MessageGroupId`,
    same `MessageDeduplicationId`, so FIFO ordering and ADR 0007's gate-visit key survive it.

WHO CALLS IT
    scripts/reconcile_jobs.py, scripts/inspect_dlq.py and scripts/replay_dlq.py. Nothing in the
    request or job path imports it: the API and the worker are unchanged by block C.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.engine import Engine, Row

from database import locks as database_locks
from database import queries
from graph.state import TERMINAL_STATUSES, run_config, state_serde
from jobqueue import JobMessage, JobQueue, QueueError
from schemas import JobStatus

logger = logging.getLogger(__name__)

CANDIDATE_STATUSES: tuple[JobStatus, ...] = ("queued", "running")
"""The only two statuses this sweep will look at.

**`awaiting_approval` is deliberately absent.** A job may legitimately wait at the human gate
for days - that is what the gate is - so an age-based sweep that could touch it would be the
one design able to close a review nobody answered. Gate expiry is a separate, still-deferred
decision (ARCHITECTURE.md §15's last row, Phase 5 step 32), and keeping it separate means this
tool cannot possibly perform it by accident.
"""

INTERRUPT_CHANNEL = "__interrupt__"
"""The pending-write channel LangGraph records an `interrupt()` on.

`worker.py` asks the compiled graph - `graph.get_state(...).interrupts` - and gets this derived
for it. **This module has no graph and must not build one**: `graph.build` imports all five
agents, the LLM client and the tool boundary, and an operator recovery tool that could reach a
model is exactly what `scripts/reexport_job.py` refuses to be. So the same fact is read from the
checkpoint tuple's own pending writes, which is where LangGraph puts it and where
`StateSnapshot.interrupts` is assembled from.

**It is a private constant of a dependency, and that is a real risk stated rather than hidden.**
`tests/test_operations.py` drives the *real* graph to the *real* gate and asserts that this
reader and `graph.get_state().interrupts` give the same answer, so a LangGraph upgrade that
moves the channel fails a test rather than silently reporting that no reviewer is waiting.
"""

INSPECTION_VISIBILITY_S = 30
"""How long a dead-letter message stays hidden while an operator looks at it.

SQS has no peek, so reading is what hides a message. Every read below is followed by an explicit
`release()` that hands it straight back; this window is only what covers the moment between the
two, and 30 seconds is long enough that a slow release still lands inside it.
"""

REPLAY_VISIBILITY_S = 120
"""How long a replay owns a message while it checks durable state and acts.

Longer than an inspection because there is real work in between - an advisory lock, a job read,
a checkpoint read, a send and a delete - and short enough that an abandoned replay returns the
message to the dead-letter queue promptly rather than hiding evidence for half an hour.
"""

DEAD_LETTER_REASON = "job_dead_lettered"
"""The `failure_reason` a reconciled orphan is recorded with.

**Not a new word.** It is the one `worker._finalise` already writes when a final delivery fails,
and this sweep exists for exactly the case where the worker never got to write it - the process
died before it could. Inventing `job_orphaned` beside it would give one event two names.
"""

Outcome = Literal[
    "owned",
    "no_change",
    "repaired_gate",
    "repaired_terminal",
    "requeued",
    "failed",
    "skipped",
    "errored",
]
"""What one reconciliation did, or did not do.

`owned` and `skipped` are both "nothing happened" and are deliberately different words: `owned`
means a live execution owner exists and the sweep is right to stay away, `skipped` means the
evidence did not add up to any of the repairs. One is healthy and the other wants a person.
"""

MUTATING_OUTCOMES = frozenset({"repaired_gate", "repaired_terminal", "requeued", "failed"})
"""The outcomes that write something. Everything else is a report."""


# --- Reading durable state without building a graph -----------------------------------


@dataclass(frozen=True)
class CheckpointView:
    """What a job's checkpoint says about it, as far as an operator tool needs to know.

    The five value fields are exactly what `worker._finalise` reads before it writes a terminal
    row, which is not a coincidence: repairing a stale row from a terminal checkpoint has to
    produce the row the worker would have produced.
    """

    status: JobStatus | None
    failure_reason: str | None
    quality_flag: str | None
    revision_count: int
    llm_calls_used: int
    waiting_at_gate: bool

    @property
    def terminal(self) -> bool:
        return self.status is not None and self.status in TERMINAL_STATUSES


def read_checkpoint(checkpoints: BaseCheckpointSaver[Any], job_id: str) -> CheckpointView | None:
    """The job's durable state, or None when nothing has ever run it.

    `get_tuple` is the same read `routes/api.py` uses for the gate view (ADR 0012 decision 3):
    reading durable state is not executing a graph, which is why this needs neither the agent
    stack nor a provider credential.
    """
    stored = checkpoints.get_tuple(run_config(job_id))
    if stored is None:
        return None

    values: dict[str, Any] = stored.checkpoint.get("channel_values") or {}
    # `pending_writes` is None on a checkpoint saved before any task wrote, which is a job that
    # has run nothing rather than one that is waiting for a person.
    writes = stored.pending_writes or []
    waiting = any(channel == INTERRUPT_CHANNEL for _task, channel, _value in writes)
    status = values.get("status")
    return CheckpointView(
        status=cast("JobStatus | None", status) if status else None,
        failure_reason=values.get("failure_reason"),
        quality_flag=values.get("quality_flag"),
        revision_count=int(values.get("revision_count") or 0),
        llm_calls_used=int(values.get("llm_calls_used") or 0),
        waiting_at_gate=waiting,
    )


@contextmanager
def checkpoint_reader(database_url: str) -> Iterator[PostgresSaver]:
    """A `PostgresSaver` this process only ever reads through, closed when the script ends.

    **`setup()` is deliberately not called.** It is DDL for tables LangGraph owns, and an
    operator recovery tool has no business migrating anything - the worker creates them and this
    attaches to what the worker created, exactly as the API does (ADR 0012 decisions 1 and 3).

    It is a context manager because a script is short-lived, and because a psycopg pool nobody
    closes makes the interpreter wait five seconds per pool thread on the way out. `app.py`
    keeps its own opener for the opposite lifetime - a pool that lives as long as the server and
    is closed by a FastAPI lifespan - and the two are separate for that reason rather than by
    oversight.

    `state_serde()` is shared with the worker unchanged: it exists precisely so the answer does
    not depend on which process reads the bytes.
    """
    with ConnectionPool(
        database_url,
        connection_class=Connection[DictRow],
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    ) as pool:
        yield PostgresSaver(pool, serde=state_serde())


# --- The dead-letter queue, read without consuming it ----------------------------------


def read_dead_letter_messages(queue: JobQueue, *, limit: int) -> list[JobMessage]:
    """Up to `limit` messages from a dead-letter queue, put straight back.

    **Nothing here deletes anything.** Each message is received - which is the only way SQS
    lets you see one - and then released with a zero visibility timeout, so the queue an
    operator inspects is the queue the alarm is still watching and the queue the next person
    will see.

    A release that fails is logged and not raised: the message reappears when
    `INSPECTION_VISIBILITY_S` lapses, so the cost of the failure is half a minute of a hidden
    message rather than an inspection that reports nothing.
    """
    found: list[JobMessage] = []
    while len(found) < limit:
        batch = queue.receive_batch(
            max_messages=min(10, limit - len(found)),
            # A dead-letter queue is either empty or has messages waiting; there is nothing to
            # long-poll for, and an operator running this by hand should not wait 20 seconds to
            # be told the queue is empty.
            wait_seconds=1,
            visibility_timeout_s=INSPECTION_VISIBILITY_S,
        )
        if not batch:
            break
        found.extend(batch)

    for message in found:
        try:
            queue.release(message)
        except QueueError:
            logger.warning(
                "job %s: a dead-letter message could not be released and stays hidden for %ds",
                message.job_id,
                INSPECTION_VISIBILITY_S,
            )
    return found


def message_kind(message: JobMessage) -> str:
    """Whether a message was a start or a gate resume, read from its deduplication id.

    The two shapes are `jobqueue.py`'s own: a start dedupes on the job's `idempotency_key`, a
    resume on `f"{job_id}:{calls_used}"` - ADR 0007's gate-visit key. So the id says which kind
    of message this was, which is worth knowing when a DLQ entry is being explained.

    **It is a label for a human and never an input to a decision.** Everything below decides
    from durable state; nothing branches on this.
    """
    dedup = message.deduplication_id or ""
    job, _, visit = dedup.rpartition(":")
    if job == message.job_id and visit.isdigit():
        return "resume"
    return "start" if dedup else "unknown"


# --- Candidate selection: a clock, and nothing else -------------------------------------


def select_candidates(
    engine: Engine, *, min_age_seconds: int, now: datetime, job_id: str | None = None
) -> list[Row[Any]]:
    """The `queued` and `running` rows old enough to be worth inspecting.

    "Old enough" is measured from the job's **last durable activity** - the newest
    `audit_events` row, falling back to `created_at` for a job that has not produced one yet -
    rather than from submission. A job that has been checkpointing nodes for ten minutes is
    working, and selecting it by its submission time would put a live job in front of an
    operator every single run.

    `now` is a parameter because a sweep that reads the clock itself cannot be tested against a
    boundary, and this is a boundary worth testing.
    """
    rows = queries.read_unfinished_jobs(engine, statuses=CANDIDATE_STATUSES, job_id=job_id)
    return [row for row in rows if age_seconds(row, now=now) >= min_age_seconds]


def age_seconds(row: Row[Any], *, now: datetime) -> float:
    """How long since this job last did anything durable, in seconds.

    Times are normalised to UTC before subtracting. `database/schema.py` uses `timestamptz`
    throughout, and PostgreSQL hands those back aware - but SQLite, which the offline suite runs
    on, hands the same columns back naive. Subtracting an aware from a naive raises, so the
    normalisation is what lets one function serve both stores.
    """
    last = _as_utc(row.last_event_at) or _as_utc(row.created_at)
    if last is None:
        # A job with neither timestamp cannot be aged, so it is treated as infinitely old and
        # inspected. Nothing follows from that on its own: inspection authorises nothing.
        return float("inf")
    return (now - last).total_seconds()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# --- What is true about one job ----------------------------------------------------------


@dataclass(frozen=True)
class JobEvidence:
    """Everything a decision below is allowed to use, gathered under the execution fence.

    It is a value rather than a set of arguments so that `decide` and `replay_verdict` are pure
    functions of it: the interesting cases - a terminal checkpoint beside a `running` row, a
    pending interrupt beside one - are then a dataclass literal in a test rather than a
    database, a checkpointer and a queue arranged to produce them.
    """

    job_id: str
    status: JobStatus | None
    """None when there is no `jobs` row at all, which a dead-letter message can name."""

    completed_at: datetime | None
    checkpoint: CheckpointView | None
    in_dead_letter_queue: bool

    @property
    def terminal(self) -> bool:
        """`completed_at` and not the status string, for `set_job_status`'s reason: `finish_job`
        is the only writer of that column, so it is what "this job has ended" means."""
        return self.completed_at is not None

    @property
    def waiting_at_gate(self) -> bool:
        return self.checkpoint is not None and self.checkpoint.waiting_at_gate


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str

    @property
    def mutates(self) -> bool:
        return self.outcome in MUTATING_OUTCOMES


def decide(evidence: JobEvidence) -> Decision:
    """What should happen to one job, from durable evidence alone.

    The order is the decision: **repair before failure, failure only with evidence, and skip
    rather than guess.**

    1. A finished job is finished. Nothing here can talk over `finish_job`.
    2. A pending interrupt means a reviewer is owed a gate. That is the one state nothing else
       recovers from - `GET /jobs/{id}/gate` and `POST /jobs/{id}/approve` both refuse a job
       that is not `awaiting_approval` - so it is repaired before anything else is considered.
    3. A terminal checkpoint means the job really did end and only its row is stale. The row is
       finished from the checkpoint's own values, which is the row the worker would have
       written.
    4. Only now, and only when the queue has dead-lettered the job, is it recorded as failed.
       The message in the dead-letter queue is the evidence that no delivery is coming back;
       without it this branch is unreachable, which is what makes "no job is failed for being
       old" a property of the code.
    5. A `queued` job that has never run and is not dead-lettered lost its message somewhere -
       most likely to ADR 0010 decision 10's `503 enqueue_failed`, which keeps the row on
       purpose because it holds the idempotency key a re-enqueue needs. Putting it back is the
       repair, and it is safe even if the original message does turn out to exist: FIFO groups
       and the execution fence still allow only one worker, and the second delivery finds the
       job already terminal or continues it from the checkpoint.
    6. Everything else is ambiguous and is reported as such. A `running` job with a live-looking
       checkpoint and no dead-lettered message is very probably mid-redelivery, and the right
       thing to do about it is nothing.
    """
    if evidence.status is None:
        return Decision("skipped", "there is no job row for this id")
    if evidence.terminal:
        return Decision("no_change", f"the job already finished as {evidence.status}")

    if evidence.waiting_at_gate:
        if evidence.status == "awaiting_approval":
            return Decision("no_change", "the row already matches the durable interrupt")
        return Decision(
            "repaired_gate",
            f"the checkpoint holds a pending interrupt while the row says {evidence.status}",
        )

    checkpoint = evidence.checkpoint
    if checkpoint is not None and checkpoint.terminal:
        return Decision(
            "repaired_terminal",
            f"the checkpoint reached {checkpoint.status} while the row says {evidence.status}",
        )

    if evidence.in_dead_letter_queue:
        return Decision(
            "failed",
            "the message is in the dead-letter queue and no recoverable durable state remains",
        )

    if evidence.status == "queued" and checkpoint is None:
        return Decision("requeued", "nothing has ever run this job and no message is holding it")

    return Decision(
        "skipped",
        "the job is unfinished with no terminal checkpoint, no interrupt and no dead-lettered "
        "message; redelivery is still the recovery path",
    )


# --- Doing it ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    """One candidate, what was decided about it, and whether anything was written."""

    job_id: str
    status: JobStatus | None
    outcome: Outcome
    reason: str
    applied: bool

    @property
    def mutating(self) -> bool:
        return self.outcome in MUTATING_OUTCOMES

    def line(self) -> str:
        wrote = "applied" if self.applied else "would apply" if self.mutating else "no write"
        return (
            f"{self.job_id}  {self.status or '-':<18}  {self.outcome:<18}  "
            f"{wrote:<11}  {self.reason}"
        )


@dataclass(frozen=True)
class Reconciler:
    """One sweep's collaborators and its one policy switch.

    `apply` is the switch, and it defaults nowhere: both scripts state it explicitly, because a
    tool that mutates durable state when an argument is forgotten is a tool that mutates durable
    state when an argument is forgotten.

    `queue` may be None, and then the `requeued` outcome is simply unavailable - a run with no
    queue configured reports `skipped` instead of writing a message it cannot send. `dead_lettered`
    may be empty for the same reason: without a dead-letter queue to read, nothing is ever
    recorded as failed, which is the safe direction for that particular absence.
    """

    engine: Engine
    checkpoints: BaseCheckpointSaver[Any]
    actor: str
    apply: bool
    queue: JobQueue | None = None
    dead_lettered: frozenset[str] = frozenset()

    def sweep(self, candidates: Sequence[Row[Any]]) -> list[Result]:
        """Reconcile every candidate, one at a time, and never stop for one of them.

        Per-candidate error isolation is `eval/run.py`'s rule for the same reason: a sweep that
        died on the third of twenty rows would leave seventeen unexamined and give the operator
        one traceback instead of an inventory.
        """
        return [self.reconcile(row.job_id) for row in candidates]

    def reconcile(self, job_id: str) -> Result:
        """One job, under the fence, from state reread inside it.

        The order is ADR 0016 decision 3's, with a reconciler in the worker's place:

            try the per-job lock once  ->  reread the row and the checkpoint  ->  decide
            ->  apply  ->  release

        A busy lock ends it immediately with `owned`. **It does not wait**, and that is the
        difference between this and the worker: a worker that gives up its turn burns one of
        three deliveries, while a sweep that gives up simply runs again later.
        """
        try:
            with database_locks.job_execution_lock(self.engine, job_id) as fence:
                if not fence.try_acquire():
                    job = queries.read_job(self.engine, job_id)
                    return Result(
                        job_id=job_id,
                        status=cast("JobStatus | None", job.status if job else None),
                        outcome="owned",
                        reason="another process holds this job's execution lock",
                        applied=False,
                    )
                evidence = self.evidence(job_id)
                decision = self._withhold_what_cannot_be_done(decide(evidence))
                applied = self._apply(evidence, decision)
                return Result(
                    job_id=job_id,
                    status=evidence.status,
                    outcome=decision.outcome,
                    reason=decision.reason,
                    applied=applied,
                )
        except Exception as error:  # noqa: BLE001 - reported per candidate, never fatal
            logger.exception("job %s: reconciliation failed", job_id)
            return Result(
                job_id=job_id,
                status=None,
                outcome="errored",
                reason=f"{type(error).__name__}: {error}",
                applied=False,
            )

    def _withhold_what_cannot_be_done(self, decision: Decision) -> Decision:
        """Turn an action this run cannot perform into a reported skip.

        The only one is `requeued` with no queue configured. `decide` stays a pure function of
        durable state - whether a message can be sent is a fact about this run, not about the
        job - and reporting it as `skipped` with the reason is what an operator running against
        a half-configured environment needs, rather than a traceback or a silent no-op.
        """
        if decision.outcome == "requeued" and self.queue is None:
            return Decision("skipped", "no queue is configured, so nothing can re-enqueue it")
        return decision

    def evidence(self, job_id: str) -> JobEvidence:
        """The row and the checkpoint, read fresh. Only ever called with the fence held."""
        job = queries.read_job(self.engine, job_id)
        return JobEvidence(
            job_id=job_id,
            status=cast("JobStatus | None", job.status) if job is not None else None,
            completed_at=job.completed_at if job is not None else None,
            checkpoint=read_checkpoint(self.checkpoints, job_id),
            in_dead_letter_queue=job_id in self.dead_lettered,
        )

    def _apply(self, evidence: JobEvidence, decision: Decision) -> bool:
        """Carry out a decision, or report that a dry run would have.

        Every write below already exists and is already keyed to converge: `set_job_status`
        cannot talk over a finished job, `finish_job` guards its own `job_finished` row, and
        `send_start` carries the job's original deduplication id. `record_reconciliation` is the
        one addition, and it is guarded on `(job_id, outcome)` so a second sweep cannot double
        the trail.
        """
        if not decision.mutates or not self.apply:
            return False

        if decision.outcome == "repaired_gate":
            queries.set_job_status(self.engine, job_id=evidence.job_id, status="awaiting_approval")
        elif decision.outcome == "repaired_terminal":
            self._finish(evidence, status=_required_checkpoint(evidence).status)
        elif decision.outcome == "failed":
            self._finish(evidence, status="failed", failure_reason=DEAD_LETTER_REASON)
        elif decision.outcome == "requeued":
            self._requeue(evidence.job_id)

        queries.record_reconciliation(
            self.engine,
            job_id=evidence.job_id,
            actor=self.actor,
            outcome=decision.outcome,
            detail={"previous_status": evidence.status, "reason": decision.reason},
        )
        return True

    def _finish(
        self,
        evidence: JobEvidence,
        *,
        status: JobStatus | None,
        failure_reason: str | None = None,
    ) -> None:
        """Write the terminal row the worker would have written, from the checkpoint's values.

        `failure_reason` is the checkpoint's own on the repair path - the job failed for
        whatever reason it recorded - and `job_dead_lettered` on the orphan path, where the
        checkpoint has no opinion because nothing lived long enough to form one.
        """
        checkpoint = evidence.checkpoint
        assert status is not None, "a terminal repair needs a status"
        queries.finish_job(
            self.engine,
            job_id=evidence.job_id,
            status=status,
            failure_reason=(
                failure_reason
                if failure_reason is not None
                else (checkpoint.failure_reason if checkpoint else None)
            ),
            quality_flag=cast(Any, checkpoint.quality_flag) if checkpoint else None,
            revision_count=checkpoint.revision_count if checkpoint else 0,
            llm_calls_used=checkpoint.llm_calls_used if checkpoint else 0,
        )

    def _requeue(self, job_id: str) -> None:
        """Put a start message back for a job nothing ever picked up.

        It goes through `send_start` with the job's own identifiers, so the group id is the job
        id and the deduplication id is the idempotency key - the identical message `POST /jobs`
        would have sent, which is what keeps ADR 0010 decision 4 true across a recovery.
        """
        queue = self.queue
        assert queue is not None, "requeue was decided with no queue configured"
        job = queries.read_job(self.engine, job_id)
        assert job is not None, "requeue was decided for a job with no row"
        queue.send_start(job_id=job_id, user_id=job.user_id, idempotency_key=job.idempotency_key)


def _required_checkpoint(evidence: JobEvidence) -> CheckpointView:
    assert evidence.checkpoint is not None, "a terminal repair needs a checkpoint"
    return evidence.checkpoint


def reconciler_for(
    engine: Engine,
    checkpoints: BaseCheckpointSaver[Any],
    *,
    actor: str,
    apply: bool,
    queue: JobQueue | None,
    dead_lettered: Sequence[str] = (),
) -> Reconciler:
    """The factory the scripts use, so `frozenset(...)` is written once."""
    return Reconciler(
        engine=engine,
        checkpoints=checkpoints,
        actor=actor,
        apply=apply,
        queue=queue,
        dead_lettered=frozenset(dead_lettered),
    )


# --- Replaying one dead-lettered message ---------------------------------------------------

ReplayOutcome = Literal["replayable", "refused", "owned", "errored"]
"""What a replay decided. `replayable` plus `applied=False` is a dry run; `replayable` plus
`applied=True` is a message that really went back onto the job queue."""


@dataclass(frozen=True)
class ReplayVerdict:
    """Whether one dead-lettered message may go back on the job queue, and why."""

    allowed: bool
    reason: str


def replay_verdict(evidence: JobEvidence) -> ReplayVerdict:
    """The safety rules, as a pure function of durable state.

    It refuses more than it allows, on purpose. Each refusal is a state in which putting the
    message back would either do nothing or do harm:

    * **No row** - the message names a job that does not exist. Nothing can run it.
    * **Terminal** - the job finished. A replay would be handled and deleted immediately by the
      worker's first branch, so the only thing it would achieve is another delivery.
    * **Waiting at the gate** - a reviewer's decision is what moves this job, not a message. A
      start message replayed here is refused by the worker anyway, and a resume for a visit
      nobody has answered has nothing to resume with.
    * **A terminal checkpoint beside a non-terminal row** - the job ended and its row is stale.
      That is `reconcile_jobs.py`'s job, and doing it by re-running the graph would be a much
      more expensive way to write one row.

    What it allows is the case the dead-letter queue exists for: a `queued` or `running` job
    with unfinished durable state, whose three deliveries were consumed by something that has
    since been fixed.
    """
    if evidence.status is None:
        return ReplayVerdict(False, "there is no job row for this id")
    if evidence.terminal:
        return ReplayVerdict(False, f"the job already finished as {evidence.status}")
    if evidence.waiting_at_gate or evidence.status == "awaiting_approval":
        return ReplayVerdict(
            False, "the job is waiting for a reviewer; a gate decision moves it, not a replay"
        )
    checkpoint = evidence.checkpoint
    if checkpoint is not None and checkpoint.terminal:
        return ReplayVerdict(
            False,
            f"the checkpoint reached {checkpoint.status}; reconcile the row rather than replaying",
        )
    if evidence.status in CANDIDATE_STATUSES:
        return ReplayVerdict(True, f"the job is {evidence.status} with unfinished durable state")
    return ReplayVerdict(False, f"the job is {evidence.status}, which is not a replayable state")


@dataclass(frozen=True)
class ReplayResult:
    """One dead-lettered message, what was decided about it, and whether it was actually sent.

    `applied` is separate from `outcome` for the same reason it is on `Result`: a dry run has to
    be able to say "this one would be replayed" without saying "this one was".
    """

    job_id: str
    kind: str
    outcome: ReplayOutcome
    reason: str
    applied: bool

    def line(self) -> str:
        wrote = (
            "replayed"
            if self.applied
            else "would replay"
            if self.outcome == "replayable"
            else "no write"
        )
        return f"{self.job_id}  {self.kind:<7}  {self.outcome:<11}  {wrote:<12}  {self.reason}"


@dataclass(frozen=True)
class Replayer:
    """One deliberate replay path: the job queue, the dead-letter queue, and the same fence.

    The fence is taken for the same reason the reconciler takes it - a job with a live execution
    owner must not have a fourth delivery pushed at it - and it is released before the send, so
    the worker that picks the message up is not waiting on this process.
    """

    engine: Engine
    checkpoints: BaseCheckpointSaver[Any]
    queue: JobQueue
    dead_letters: JobQueue
    apply: bool

    def replay(self, message: JobMessage) -> ReplayResult:
        """Check, then send, then delete. In that order and never any other.

        **The delete happens only after a successful send**, so a send that fails leaves the
        message where it was and the alarm still firing. The reverse order would be a recovery
        that can lose a job.

        Deleting it afterwards is deliberate and is the one destructive thing here: a replayed
        message left in the dead-letter queue would keep the alarm on forever and would be
        replayed again by the next operator. The body is three identifiers and it is logged
        before the delete, so what was removed is on the record.
        """
        kind = message_kind(message)
        try:
            with database_locks.job_execution_lock(self.engine, message.job_id) as fence:
                if not fence.try_acquire():
                    return ReplayResult(
                        message.job_id,
                        kind,
                        "owned",
                        "another process holds the execution lock",
                        applied=False,
                    )
                evidence = self._evidence(message.job_id)
                verdict = replay_verdict(evidence)
                if not verdict.allowed:
                    return ReplayResult(
                        message.job_id, kind, "refused", verdict.reason, applied=False
                    )
                if not self.apply:
                    return ReplayResult(
                        message.job_id, kind, "replayable", verdict.reason, applied=False
                    )

            logger.info(
                "job %s: replaying its %s message (group=%s dedup=%s deliveries=%d)",
                message.job_id,
                kind,
                message.group_id,
                message.deduplication_id,
                message.receive_count,
            )
            self.queue.resend(message)
            self.dead_letters.delete(message)
            return ReplayResult(message.job_id, kind, "replayable", verdict.reason, applied=True)
        except Exception as error:  # noqa: BLE001 - one message must not end the run
            logger.exception("job %s: replay failed", message.job_id)
            return ReplayResult(
                message.job_id, kind, "errored", f"{type(error).__name__}: {error}", applied=False
            )

    def _evidence(self, job_id: str) -> JobEvidence:
        job = queries.read_job(self.engine, job_id)
        return JobEvidence(
            job_id=job_id,
            status=cast("JobStatus | None", job.status) if job is not None else None,
            completed_at=job.completed_at if job is not None else None,
            checkpoint=read_checkpoint(self.checkpoints, job_id),
            # It is in the dead-letter queue by construction - that is where it was read from.
            in_dead_letter_queue=True,
        )
