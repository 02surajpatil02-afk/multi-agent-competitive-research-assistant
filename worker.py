"""
WHY THIS FILE EXISTS
    `python -m worker`. The process that actually runs a job: it long-polls the queue, decides
    from the checkpoint what a message means, invokes the graph, and deletes the message only
    once the work is safely durable. It is the only process that constructs an `LLMClient` or
    executes a node (ADR 0012).

    Six things here are decisions rather than plumbing, and each is an accepted record.

    **The message says which job. The checkpoint says what to do with it** (ADR 0010 decision
    5). There is no message type, no `attempt` field, and no state on the wire - four
    situations are discriminated from durable state alone:

        jobs.completed_at IS NOT NULL   -> terminal: delete, do nothing
        no checkpoint for thread_id     -> start:    invoke(new_state(...))
        checkpoint, pending interrupt   -> resume:   invoke(Command(resume=<the decision>))
        checkpoint, no interrupt        -> continue: invoke(None)

    The last one is the case a shape could not capture: a worker that died mid-run leaves a
    checkpoint with no interrupt, and `invoke(None)` carries it on from the last completed node
    rather than restarting it.

    **The message is deleted on exactly three outcomes** (ADR 0010 decision 6): the graph
    interrupted at the gate, the job reached a terminal status, or it was already terminal when
    the message arrived. On nothing else - an unhandled failure leaves the message, and
    redelivery is the retry. **Delivery is at-least-once and this file does not pretend
    otherwise**: the same node can run twice, which is exactly why ADR 0005 keys every
    graph-time write so a replayed node converges.

    **`MAX_JOB_RUNTIME` is a no-new-node deadline for one invocation, not a hard wall and not a
    job's lifetime** (ADR 0010 decision 7, clarified by ADR 0015). A job that waits three days at
    the gate must not fail on resume. The deadline is checked between nodes; a node already in
    flight may finish after it. SQS ownership is therefore maintained by a visibility heartbeat,
    and PostgreSQL serializes same-job execution if that queue lease becomes unsafe while a node is
    still finishing. A static visibility timeout is not treated as a node-duration prediction.

    **The reviewer's decision is read from the audit trail, never from the message** (ADR 0011).
    Which visit to read is derived from the checkpoint's own `llm_calls_used`, the identical
    computation the endpoint performed when it recorded the decision. The key is derived on both
    sides and passed between them nowhere.

    **`jobs.status` is reconciled in a `finally`, on both paths** (ADR 0011 decision 4). The
    rule is ADR 0007 invariant 4 unchanged and the predicate is still the pending interrupt
    rather than `next`; what moved is which process owns the `finally`.

    **Shutdown stops at the next checkpoint boundary** (ARCHITECTURE.md §11, gl §12). SIGTERM
    is read before a message is started and between nodes. While the current node completes, a
    background heartbeat keeps renewing the delivery; once the checkpoint is durable the worker
    stops that heartbeat, leaves the message undeleted, and the next delivery carries on from the
    checkpoint (ADR 0005 decision 2, ADR 0015).

    The 120-second stop grace period is Fargate's maximum graceful-stop opportunity, not a promise
    that every node finishes inside it. A harder kill stops the heartbeat and leaves the message
    undeleted; visibility expiry and checkpoint resume remain the recovery path.

WHO CALLS IT
    Nothing imports it. It is an entrypoint: `python -m worker`.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, cast

from langgraph.types import Command
from openai import OpenAI
from redis import Redis
from sqlalchemy.engine import Engine

from artifacts import build_artifact_store
from config import Config, load_config, required
from database import locks as database_locks
from database import queries
from database.queries import QUERY_TIMEOUT_MS, create_database_engine
from graph.build import ResearchGraph, build_graph, postgres_checkpointer
from graph.state import ResearchState, new_state, run_config
from jobqueue import (
    JobMessage,
    JobQueue,
    OwnershipLost,
    QueueError,
    build_queue,
    is_fifo,
    max_receive_count,
    sqs_call_envelope_seconds,
    visibility_timeout,
)
from llm_client import LLMClient
from redisstore import (
    RedisCache,
    RedisRateLimiter,
    RedisUrlDeduplicator,
    build_redis,
    reachable,
)
from schemas import GateDecision, JobStatus

logger = logging.getLogger(__name__)

VISIBILITY_RENEWAL_DIVISOR = 3
"""Renew after one third of the current SQS visibility window.

After a failed bounded SDK call, the one recovery attempt is placed halfway through the remaining
safe-start window rather than delayed by another full healthy cadence.
"""

MAX_CONSECUTIVE_RENEWAL_FAILURES = 2
"""One transient SQS failure is tolerated; the second relinquishes at the next checkpoint."""

JOB_LOCK_RETRY_INTERVAL_S = QUERY_TIMEOUT_MS / 1000.0
"""Retry a busy advisory lock on the database query-timeout cadence.

The try-lock statement itself never blocks behind another job.  Reusing the existing five-second
database operation policy makes SIGTERM/lease-loss response bounded without inventing a second
independent timing constant.
"""

RECEIVE_ERROR_PAUSE_S = 5.0
"""How long to wait after a receive that raised, before polling again.

Without it a queue that is unreachable turns into a hot loop against a broken endpoint. It is
deliberately not a backoff schedule: the worker has one job to do and no bound to give up at -
an operator watching the logs is the escalation, which is what the DLQ alarm is for.
"""


def visibility_renewal_interval(visibility_timeout_s: int) -> float:
    """Cadence derived from the queue's real lease duration, never a duplicate setting."""
    if visibility_timeout_s <= 0:
        raise ValueError("visibility_timeout_s must be positive")
    return visibility_timeout_s / VISIBILITY_RENEWAL_DIVISOR


def visibility_safe_renewal_start(
    estimated_expiry: float, *, visibility_timeout_s: int, call_envelope_s: float
) -> float:
    """Latest start whose complete bounded call still preserves the existing V/3 margin."""
    return estimated_expiry - visibility_renewal_interval(visibility_timeout_s) - call_envelope_s


def visibility_failure_retry_at(
    now: float,
    *,
    estimated_expiry: float,
    visibility_timeout_s: int,
    call_envelope_s: float,
) -> float | None:
    """Place one recovery attempt halfway through the remaining safe-start window.

    Botocore has already exhausted its three-attempt internal policy when ``QueueError`` reaches
    the lease.  The midpoint is derived entirely from the remaining lease, the V/3 safety margin,
    and the bounded call envelope: it retries earlier than another healthy cadence while retaining
    equal scheduler headroom on either side.  If the window is gone, no safe retry is claimed.
    """
    latest = visibility_safe_renewal_start(
        estimated_expiry,
        visibility_timeout_s=visibility_timeout_s,
        call_envelope_s=call_envelope_s,
    )
    if now >= latest:
        return None
    return now + (latest - now) / 2


@dataclass(frozen=True)
class VisibilityLeaseState:
    lease_started_at: float
    last_attempt_started_at: float | None
    last_successful_renewal_started_at: float | None
    estimated_expiry: float
    current_monotonic: float
    remaining_s: float
    next_attempt_at: float
    scheduling_lateness_s: float
    consecutive_failures: int


class _NodeAdmission:
    """Atomic linearization point between an ownership stop and starting one graph node."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closed_reason: str | None = None
        self._in_flight = False

    def close(self, reason: str) -> None:
        with self._lock:
            if self._closed_reason is None:
                self._closed_reason = reason

    def begin(self) -> bool:
        with self._lock:
            if self._closed_reason is not None:
                return False
            if self._in_flight:
                raise RuntimeError("a graph node is already admitted")
            self._in_flight = True
            return True

    def finish(self) -> None:
        with self._lock:
            self._in_flight = False

    @property
    def closed_reason(self) -> str | None:
        with self._lock:
            return self._closed_reason


class VisibilityLease:
    """Background renewal for one received SQS delivery.

    The graph runs blocking provider calls on the main thread, so renewal has to live on a
    different thread. `ownership_lost` is monotonic: after SQS rejects the receipt handle or two
    consecutive renewals fail, this worker never again claims exclusivity and stops before another
    graph node at the next durable checkpoint.
    """

    def __init__(
        self,
        queue: JobQueue,
        message: JobMessage,
        *,
        visibility_timeout_s: int,
        renewal_call_envelope_s: float | None = None,
    ) -> None:
        self._queue = queue
        self._message = message
        self._visibility_timeout_s = visibility_timeout_s
        self._interval_s = visibility_renewal_interval(visibility_timeout_s)
        self._call_envelope_s = (
            sqs_call_envelope_seconds()
            if renewal_call_envelope_s is None
            else renewal_call_envelope_s
        )
        self._stop = threading.Event()
        self._ownership_lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        now = time.monotonic()
        self._lease_started_at = (
            message.received_at_monotonic if message.received_at_monotonic > 0 else now
        )
        self._last_attempt_started_at: float | None = None
        self._last_successful_renewal_started_at: float | None = None
        self._estimated_expiry = self._lease_started_at + visibility_timeout_s
        self._next_attempt_at = self._lease_started_at + self._interval_s
        self._scheduling_lateness_s = 0.0
        self._consecutive_failures = 0
        self._node_admission: _NodeAdmission | None = None

    @property
    def ownership_lost(self) -> bool:
        return self._ownership_lost.is_set()

    @property
    def state(self) -> VisibilityLeaseState:
        now = time.monotonic()
        with self._state_lock:
            return VisibilityLeaseState(
                lease_started_at=self._lease_started_at,
                last_attempt_started_at=self._last_attempt_started_at,
                last_successful_renewal_started_at=self._last_successful_renewal_started_at,
                estimated_expiry=self._estimated_expiry,
                current_monotonic=now,
                remaining_s=max(0.0, self._estimated_expiry - now),
                next_attempt_at=self._next_attempt_at,
                scheduling_lateness_s=self._scheduling_lateness_s,
                consecutive_failures=self._consecutive_failures,
            )

    def bind_node_admission(self, admission: _NodeAdmission) -> None:
        with self._state_lock:
            self._node_admission = admission
            lost = self._ownership_lost.is_set()
        if lost:
            admission.close("visibility ownership lost")

    def unbind_node_admission(self, admission: _NodeAdmission) -> None:
        with self._state_lock:
            if self._node_admission is admission:
                self._node_admission = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("visibility lease already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"visibility-{self._message.job_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "job %s: visibility heartbeat started; lease=%ds cadence=%.1fs",
            self._message.job_id,
            self._visibility_timeout_s,
            self._interval_s,
        )

    def stop(self, *, reason: str) -> None:
        """Stop and join before acknowledgement, so no renewal can race with DeleteMessage."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        logger.info("job %s: visibility heartbeat stopped (%s)", self._message.job_id, reason)

    def _mark_ownership_lost(self, reason: str) -> None:
        self._ownership_lost.set()
        with self._state_lock:
            admission = self._node_admission
        if admission is not None:
            admission.close(reason)

    def _run(self) -> None:
        while True:
            with self._state_lock:
                next_renewal = self._next_attempt_at
            if self._stop.wait(max(0.0, next_renewal - time.monotonic())):
                return

            attempt_started_at = time.monotonic()
            with self._state_lock:
                self._scheduling_lateness_s = max(0.0, attempt_started_at - next_renewal)
                self._last_attempt_started_at = attempt_started_at
                estimated_expiry = self._estimated_expiry

            latest_safe_start = visibility_safe_renewal_start(
                estimated_expiry,
                visibility_timeout_s=self._visibility_timeout_s,
                call_envelope_s=self._call_envelope_s,
            )
            if attempt_started_at > latest_safe_start:
                logger.error(
                    "job %s: no bounded visibility renewal fits before the V/3 safety margin; "
                    "ownership is unsafe",
                    self._message.job_id,
                )
                self._mark_ownership_lost("visibility renewal safe deadline passed")
                return

            try:
                self._queue.extend_visibility(
                    self._message, visibility_timeout_s=self._visibility_timeout_s
                )
            except OwnershipLost:
                logger.exception(
                    "job %s: visibility receipt is no longer owned; stopping at the next "
                    "checkpoint",
                    self._message.job_id,
                )
                self._mark_ownership_lost("SQS rejected the receipt handle")
                return
            except QueueError:
                failed_at = time.monotonic()
                with self._state_lock:
                    self._consecutive_failures += 1
                    failures = self._consecutive_failures
                    estimated_expiry = self._estimated_expiry
                logger.exception(
                    "job %s: visibility renewal failed (%d/%d)",
                    self._message.job_id,
                    failures,
                    MAX_CONSECUTIVE_RENEWAL_FAILURES,
                )
                if failures >= MAX_CONSECUTIVE_RENEWAL_FAILURES:
                    logger.error(
                        "job %s: visibility lease is unsafe; relinquishing at the next checkpoint",
                        self._message.job_id,
                    )
                    self._mark_ownership_lost("visibility renewal failures exhausted")
                    return

                retry_at = visibility_failure_retry_at(
                    failed_at,
                    estimated_expiry=estimated_expiry,
                    visibility_timeout_s=self._visibility_timeout_s,
                    call_envelope_s=self._call_envelope_s,
                )
                if retry_at is None:
                    logger.error(
                        "job %s: a failed renewal left no safe bounded retry window",
                        self._message.job_id,
                    )
                    self._mark_ownership_lost("no safe visibility renewal retry remains")
                    return
                with self._state_lock:
                    self._next_attempt_at = retry_at
                logger.warning(
                    "job %s: visibility retry scheduled in %.1fs from remaining safe window",
                    self._message.job_id,
                    max(0.0, retry_at - failed_at),
                )
            else:
                with self._state_lock:
                    self._consecutive_failures = 0
                    self._last_successful_renewal_started_at = attempt_started_at
                    # SQS may apply the new timeout before the response reaches us.  Attempt start
                    # is therefore conservative; completion would overestimate ownership by the
                    # whole response time in the worst case.
                    self._estimated_expiry = attempt_started_at + self._visibility_timeout_s
                    self._next_attempt_at = attempt_started_at + self._interval_s
                logger.info(
                    "job %s: visibility renewed for %ds",
                    self._message.job_id,
                    self._visibility_timeout_s,
                )


class Shutdown:
    """Whether SIGTERM or SIGINT has arrived.

    **A flag rather than an exception, because of *where* the signal lands.** Raising out of a
    handler could unwind the middle of a node and leave the checkpoint behind the database,
    which is the one thing ADR 0005 decision 2 says to avoid. So it refuses a message after
    receive and atomically closes the next-node admission gate. A node admitted first may finish;
    a stop recorded first prevents LangGraph from starting it.

    A second signal is not escalated to a hard exit. The container runtime already escalates -
    SIGTERM then SIGKILL after its grace period - and a worker that killed itself faster than
    that would only lose the checkpoint the first signal was trying to protect.
    """

    def __init__(self) -> None:
        self._requested = threading.Event()
        self._state_lock = threading.Lock()
        self._node_admission: _NodeAdmission | None = None

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    @requested.setter
    def requested(self, value: bool) -> None:
        if value:
            self._requested.set()
            with self._state_lock:
                admission = self._node_admission
            if admission is not None:
                admission.close("shutdown requested")
        else:
            self._requested.clear()

    def bind_node_admission(self, admission: _NodeAdmission) -> None:
        with self._state_lock:
            self._node_admission = admission
            requested = self._requested.is_set()
        if requested:
            admission.close("shutdown requested")

    def unbind_node_admission(self, admission: _NodeAdmission) -> None:
        with self._state_lock:
            if self._node_admission is admission:
                self._node_admission = None

    def install(self) -> None:
        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, self._request)

    def _request(self, signum: int, _frame: FrameType | None) -> None:
        logger.info("signal %s received; stopping at the next checkpoint boundary", signum)
        self.requested = True


@dataclass(frozen=True)
class QueueSettings:
    """The two queue attributes the worker carries after validating them at startup."""

    visibility_timeout_s: int
    final_delivery_at: int | None


@dataclass(frozen=True)
class WorkerDeps:
    """What one worker needs, built once at startup.

    It carries the graph rather than the pieces because the graph is what an invocation needs,
    and building one per message would rebuild five agents and a connection pool for every job.
    """

    config: Config
    engine: Engine
    graph: ResearchGraph
    queue: JobQueue
    final_delivery_at: int | None
    """`maxReceiveCount` from the queue's redrive policy, or None when it has none.

    None means no delivery is ever the last one, so nothing is ever dead-lettered and a job is
    never finalised for that reason - which is the honest behaviour for a queue that would
    redeliver forever.
    """

    queue_visibility_timeout_s: int = 1800
    """The initial SQS lease and every renewal, read from queue attributes in production.

    The default preserves the established local queue for tests that construct `WorkerDeps`
    directly. The worker entrypoint always supplies the value returned by `check_queue()`.
    """

    shutdown: Shutdown = field(default_factory=Shutdown)
    """The stop signal, read by the loop **and** by the invocation.

    It lives here rather than being passed down five call frames because both readers already
    take `deps`, and because a worker has exactly one of these: two flags disagreeing about
    whether the process is stopping is the bug this shape makes impossible.

    It defaults to one nobody ever sets, so a test that is not about shutdown constructs
    `WorkerDeps` exactly as it did before and sees the behaviour it did before.
    """


# --- The loop -------------------------------------------------------------------------


def run(deps: WorkerDeps, *, max_messages: int | None = None) -> int:
    """Poll until shutdown, and answer how many messages were handled.

    `max_messages` bounds the loop for a test. In production it is None and the loop ends only
    on a signal, which is what `handled` counts against.

    **The flag is checked twice per iteration, and the second check is the one that matters.**
    A `receive()` is a 20-second long poll, so a SIGTERM arriving one millisecond after it
    starts is not seen by the loop condition until the poll returns - by which time it may be
    holding a job nobody should now begin. Checking again after the receive is what makes
    "stop taking new work" true rather than nearly true.

    A message received and not started is **left in flight deliberately**: it is not deleted
    and nothing is written for it, so it redelivers to whichever worker is still up. It costs
    that message one of its three deliveries, which is the price of at-least-once and cheaper
    than starting a twenty-minute job at the moment the process is going away.
    """
    handled = 0
    while not deps.shutdown.requested and (max_messages is None or handled < max_messages):
        try:
            message = deps.queue.receive()
        except QueueError:
            logger.exception("could not receive from the queue")
            time.sleep(RECEIVE_ERROR_PAUSE_S)
            continue

        if message is None:
            continue

        if deps.shutdown.requested:
            logger.info(
                "job %s: received during shutdown and not started; it stays on the queue",
                message.job_id,
            )
            break

        handle(deps, message)
        handled += 1

    logger.info("worker stopped after handling %d messages", handled)
    return handled


def handle(deps: WorkerDeps, message: JobMessage) -> None:
    """One message, from receipt to acknowledgement or to deliberate non-acknowledgement.

    **The message is deleted here and nowhere else**, on ADR 0010 decision 6's three outcomes.
    Every other path leaves it, so the failure is retried by redelivery rather than by anything
    this file has to remember to do.

    The final delivery is the exception that proves the rule: it finalises the job *and* leaves
    the message, so the job becomes terminal and the DLQ alarm still fires. Those are two
    requirements, not one (ADR 0010 decision 9).
    """
    final = _is_final_delivery(deps, message)
    logger.info(
        "job %s: delivery %d%s", message.job_id, message.receive_count, " (final)" if final else ""
    )

    lease = VisibilityLease(
        deps.queue, message, visibility_timeout_s=deps.queue_visibility_timeout_s
    )
    try:
        lease.start()
    except Exception:
        logger.exception("job %s: could not start its visibility heartbeat", message.job_id)
        if final:
            _finalise_if_execution_lock_is_available(
                deps, message.job_id, reason="job_dead_lettered"
            )
        return

    acknowledge = False
    invocation_failed = False
    try:
        with database_locks.job_execution_lock(deps.engine, message.job_id) as execution_lock:
            if _wait_for_execution_lock(deps, message, lease, execution_lock):
                try:
                    # Every durable read that decides start/resume/continue is deliberately below
                    # lock acquisition.  A redelivery that waited behind an old owner must not act
                    # on a checkpoint or reviewer decision it observed before that owner finished.
                    acknowledge = _handle_or_skip(deps, message, lease)
                except Exception:
                    invocation_failed = True
                    logger.exception("job %s: invocation failed", message.job_id)
                    if final:
                        # Last chance to say what happened to it.  This finalisation remains under
                        # the same per-job lock as ordinary graph mutation.
                        _finalise(deps, message.job_id, reason="job_dead_lettered")
                    # Not re-raised: one poisonous job must not stop the worker serving every other
                    # one.  The message remains unacknowledged for redelivery/DLQ handling.
    finally:
        # The execution-lock context exits before this point.  A replacement can inspect fresh
        # durable state only after the old owner has finished its checkpoint/reconcile work.
        reason = (
            "ownership lost"
            if lease.ownership_lost
            else "shutdown relinquishment"
            if deps.shutdown.requested and not acknowledge
            else "delivery complete"
            if acknowledge
            else "unfinished delivery"
        )
        lease.stop(reason=reason)

    if invocation_failed:
        return
    if acknowledge and lease.ownership_lost:
        logger.error(
            "job %s: durable outcome reached after ownership became unsafe; message not "
            "acknowledged",
            message.job_id,
        )
        return
    if acknowledge:
        try:
            # The heartbeat is joined above, so it cannot extend a message after this succeeds.
            deps.queue.delete(message)
        except Exception:
            logger.exception("job %s: acknowledgement failed", message.job_id)
            if final:
                _finalise_if_execution_lock_is_available(
                    deps, message.job_id, reason="job_dead_lettered"
                )


def _finalise_if_execution_lock_is_available(deps: WorkerDeps, job_id: str, *, reason: str) -> None:
    """Preserve final-delivery handling without mutating beside another execution owner.

    These fallback sites sit outside the ordinary lock lifetime: heartbeat startup failed, or
    acknowledgement failed after the heartbeat and lock were deliberately released. They do not
    wait without a healthy lease. An immediately available lock still makes the established final
    delivery outcome durable; a busy lock means the legitimate owner is responsible for the job and
    this delivery changes nothing.
    """
    with database_locks.job_execution_lock(deps.engine, job_id) as execution_lock:
        if execution_lock.try_acquire():
            _finalise(deps, job_id, reason=reason)
        else:
            logger.warning(
                "job %s: final-delivery fallback skipped because another execution owner exists",
                job_id,
            )


def _wait_for_execution_lock(
    deps: WorkerDeps,
    message: JobMessage,
    lease: VisibilityLease,
    execution_lock: database_locks.JobExecutionLock,
) -> bool:
    """Maintain delivery ownership while polling a non-blocking per-job PostgreSQL lock.

    Waiting is delivery time, not ``MAX_JOB_RUNTIME``: that accepted bound starts only when the
    graph is invoked.  Abandoning a healthy receipt merely because the previous owner is finishing
    would burn one of three receive attempts without advancing the job, so the wait is bounded by
    SIGTERM or this receipt's lease safety rather than by an unrelated job-runtime clock.
    """
    waiting_logged = False
    while not deps.shutdown.requested and not lease.ownership_lost:
        if execution_lock.try_acquire():
            if deps.shutdown.requested or lease.ownership_lost:
                logger.info(
                    "job %s: execution lock acquired after delivery ownership was relinquished; "
                    "starting no work",
                    message.job_id,
                )
                return False
            return True

        if not waiting_logged:
            logger.info(
                "job %s: another worker owns execution; maintaining visibility while waiting",
                message.job_id,
            )
            waiting_logged = True
        time.sleep(JOB_LOCK_RETRY_INTERVAL_S)

    logger.info(
        "job %s: stopped waiting for the execution lock (%s)",
        message.job_id,
        "shutdown" if deps.shutdown.requested else "visibility ownership lost",
    )
    return False


def _handle_or_skip(deps: WorkerDeps, message: JobMessage, lease: VisibilityLease) -> bool:
    """Run the job as far as this delivery takes it. True when the message may be deleted."""
    job = queries.read_job(deps.engine, message.job_id)
    if job is None:
        # The row is written and committed before the message is sent (ADR 0010 decision 10),
        # so a message with no row cannot be a race. Deleting it stops an unanswerable message
        # cycling to the DLQ for three deliveries first.
        logger.error("job %s: a queue message names a job with no row", message.job_id)
        return True

    if job.completed_at is not None:
        logger.info("job %s: already %s; nothing to do", message.job_id, job.status)
        return True

    if deps.shutdown.requested or lease.ownership_lost:
        logger.info("job %s: ownership relinquished before graph execution", message.job_id)
        return False

    state = _checkpoint_state(deps.graph, message.job_id)
    if state is None:
        return _start(deps, message, lease)
    return _continue(deps, message, state, lease)


def _start(deps: WorkerDeps, message: JobMessage, lease: VisibilityLease) -> bool:
    """A job nothing has run yet. `queued -> running`, then the graph from its first state."""
    job = queries.read_job(deps.engine, message.job_id)
    assert job is not None  # read a moment ago in `_handle_or_skip`
    queries.set_job_status(deps.engine, job_id=message.job_id, status="running")
    logger.info("job %s: starting", message.job_id)

    return _invoke(
        deps,
        message.job_id,
        new_state(job_id=message.job_id, user_id=message.user_id, question=job.question),
        lease,
    )


def _continue(
    deps: WorkerDeps, message: JobMessage, state: ResearchState, lease: VisibilityLease
) -> bool:
    """A job with a checkpoint: either a reviewer answered its gate, or a delivery died mid-run.

    **Which of the two is read from the checkpoint, never from the message.** A pending
    interrupt means the graph is stopped at the gate waiting for a person; no interrupt means a
    previous invocation ended somewhere else and the job simply has further to go.

    **Nothing writes `jobs.status` here, and that is the correction rather than an omission.**
    Writing `running` on the way in looked harmless - the row already says `running` on both
    paths that reach an invocation, because `claim_gate` writes it when a reviewer decides and
    the previous delivery's reconcile writes it when one died mid-run. It is not harmless on
    the third path: a start message redelivered while the job waits at the gate has no decision
    to resume with, so it returns below **without invoking**, and the write would have left the
    row saying `running` while the checkpoint held a pending interrupt. That is ADR 0007
    invariant 4 broken in the direction that cannot be recovered from - `GET /jobs/{id}/gate`
    and `POST /jobs/{id}/approve` both refuse a job that is not `awaiting_approval`, so the
    gate would be unanswerable and the job stuck for good. At-least-once delivery makes that an
    ordinary event rather than an exotic one.

    `_invoke`'s `finally` is the writer of the column on the two paths that invoke, which is what
    ADR 0011 decision 4 asks for: derived from the checkpoint, on both, after the work. The third
    path returns without invoking, and reconciles for itself - see below for the sequence that
    makes that necessary rather than defensive.
    """
    if state["status"] == "failed" and state["failure_reason"] == "job_timeout":
        # A prior timeout may have persisted its checkpoint and then lost the database write.
        # Finish that exact durable outcome before considering any pending graph task: advancing
        # the graph here would spend another node after the no-new-node deadline already tripped.
        logger.info("job %s: retrying incomplete timeout finalization", message.job_id)
        return _finalise(deps, message.job_id, reason="job_timeout")

    if not _waiting_at_the_gate(deps.graph, message.job_id):
        logger.info("job %s: continuing a run that did not finish", message.job_id)
        return _invoke(deps, message.job_id, None, lease)

    calls_used = state["llm_calls_used"]
    recorded = queries.read_gate_decision(deps.engine, message.job_id, calls_used=calls_used)
    if recorded is None:
        # **The row is reconciled first, and that is the correction of 2026-08-18.** Reaching
        # this branch usually means the row already says `awaiting_approval`, because
        # `record_gate_opened` commits that value before `interrupt()` is reached - but not
        # always, and the exception is what a container makes ordinary:
        #
        #   1. a first gate attempt dies *after* `record_gate_opened` committed. Its audit row
        #      landed, so the keyed guard is now armed and no later execution of that visit
        #      writes the status again; and the reconcile in `_invoke`'s `finally` truthfully
        #      wrote `running`, because no interrupt was pending yet.
        #   2. the redelivery re-runs the gate node, the write is skipped as designed, the pause
        #      is taken - and the process is SIGKILLed before the `finally` can reconcile.
        #
        # That leaves a pending interrupt beside a row saying `running`, which is ADR 0007
        # invariant 4 broken in the direction nothing recovers from: `GET /jobs/{id}/gate` and
        # `POST /jobs/{id}/approve` both refuse a job that is not `awaiting_approval`, so
        # without this call every later delivery took this branch unchanged and the third one
        # dead-lettered a job no person could ever answer.
        #
        # The write itself is ADR 0007 invariant 4 unchanged - derived from the checkpoint, by
        # the process that holds it (ADR 0011 decision 4) - and `set_job_status` cannot talk
        # over a finished job. On the ordinary path it writes the value the row already has.
        # A message with no decision is either a duplicate start delivery at an open gate or a
        # malformed resume. In both cases the durable interrupt is already the authoritative
        # outcome. Once its row projection is usable, redelivery is unnecessary and this receipt
        # may be acknowledged; if repair fails, it remains for another attempt.
        acknowledge = _gate_delivery_is_acknowledgeable(deps, message.job_id)
        logger.error(
            "job %s: a resume message for visit %d has no decision on record",
            message.job_id,
            calls_used,
        )
        return acknowledge

    decision = GateDecision(
        decision=cast(Any, recorded["decision"]),
        note=recorded.get("note"),
        edits=recorded.get("edits"),
    )
    logger.info("job %s: resuming with %s", message.job_id, decision.decision)
    return _invoke(deps, message.job_id, Command(resume=decision.model_dump()), lease)


def _invoke(deps: WorkerDeps, job_id: str, first: Any, lease: VisibilityLease) -> bool:
    """Run the graph and say whether this delivery finished with the job.

    A Python ``for update in graph.stream(...)`` is not a pre-node boundary: requesting the next
    iterator item runs the next node before the loop body can inspect a stop flag.  This invokes
    LangGraph with ``interrupt_after='*'`` and synchronous durability, so each call admits exactly
    one graph node, waits for its checkpoint, and returns control here before another can start.
    ``_NodeAdmission`` makes the lease-loss/SIGTERM check and that one-node start one atomic policy
    decision: whichever closes or admits first defines whether the node was already in flight.

    The two checks are not symmetrical, and the order says which is which. The runtime bound is
    a job **outcome** - the job really did exceed what it is allowed - so it finalises and the
    message is deleted. Shutdown is not an outcome: the job is untouched, the message is left,
    and the next delivery carries on from the checkpoint. Nothing is written to say a job was
    interrupted, because nothing about it changed.

    The reconcile is in a `finally` because the failure path is the one that needs it: an
    invocation that raised used to leave the row claiming a human still held a job the
    checkpoint had already moved past (ADR 0007 invariant 4, ADR 0011 decision 4).
    """
    deadline = time.monotonic() + deps.config.max_job_runtime
    admission = _NodeAdmission()
    lease.bind_node_admission(admission)
    deps.shutdown.bind_node_admission(admission)
    next_input = first
    gate_outcome_checked = False
    try:
        while admission.begin():
            try:
                # The graph is sequential by architecture.  Static interrupt-after therefore
                # yields one real node (plus LangGraph's interrupt marker), checkpoints it, and
                # returns.  A subsequent call with None resumes its pending task.
                for _update in deps.graph.stream(
                    next_input,
                    run_config(job_id),
                    stream_mode="updates",
                    interrupt_after="*",
                    durability="sync",
                ):
                    pass
            finally:
                admission.finish()
            next_input = None

            if lease.ownership_lost:
                logger.error(
                    "job %s: stopping at a checkpoint because the visibility lease is unsafe",
                    job_id,
                )
                return False
            if time.monotonic() >= deadline:
                logger.error(
                    "job %s: MAX_JOB_RUNTIME no-new-node deadline reached for this invocation",
                    job_id,
                )
                return _finalise(deps, job_id, reason="job_timeout")
            if deps.shutdown.requested:
                # **Whether this delivery is finished is asked of durable state, not assumed.**
                # The update just consumed may have been the gate's interrupt or the last node
                # of a job that ended, in which case the graph has genuinely stopped and ADR
                # 0010 decision 6 says to delete - stopping early must not turn a finished
                # delivery into one that burns a redelivery for nothing. Anything else leaves
                # the message.
                finished = _delivery_is_finished(deps, job_id)
                logger.info(
                    "job %s: stopping at a checkpoint boundary; message %s",
                    job_id,
                    "acknowledged" if finished else "left for redelivery",
                )
                return finished

            snapshot = deps.graph.get_state(run_config(job_id))
            if snapshot.interrupts:
                # A checkpointed pause is not sufficient by itself: the API projects the gate from
                # jobs.status.  Acknowledge only once that projection is already usable or has been
                # successfully repaired from a fresh checkpoint read.
                gate_outcome_checked = True
                return _gate_delivery_is_acknowledgeable(deps, job_id)
            if not snapshot.next:
                # No pending task means this delivery reached a terminal durable outcome.
                return True

        logger.info(
            "job %s: no new graph node admitted (%s)",
            job_id,
            admission.closed_reason or "execution stopped",
        )
        return False
    finally:
        deps.shutdown.unbind_node_admission(admission)
        lease.unbind_node_admission(admission)
        if not gate_outcome_checked:
            _reconcile_status(deps, job_id)


def _delivery_is_finished(deps: WorkerDeps, job_id: str) -> bool:
    """Two of ADR 0010 decision 6's three outcomes, asked of durable state.

    Used only when shutdown stops an invocation part-way: everywhere else the graph running out
    of nodes *is* the answer. The third outcome - "it was already terminal when the message
    arrived" - is `_handle_or_skip`'s and cannot be reached from here.

    Both reads are cheap and neither is a guess: the pending interrupt is what `awaiting_approval`
    means (ADR 0007 invariant 4), and `completed_at` is the only column `finish_job` writes.
    """
    if _waiting_at_the_gate(deps.graph, job_id):
        return _gate_delivery_is_acknowledgeable(deps, job_id)
    job = queries.read_job(deps.engine, job_id)
    return job is not None and job.completed_at is not None


def _finalise(deps: WorkerDeps, job_id: str, *, reason: str) -> bool:
    """End a job the graph did not end itself: the checkpoint first, then the row.

    Used by the runtime bound and by the final delivery (ADR 0010 decisions 7 and 9). Both need
    the same two things - a state that says why, and a row a poller can see has ended.

    **It does not invoke the graph, and that is a correction to the mechanism both decisions
    describe rather than a change to what they require.** Both were written as
    `update_state(status="failed")` followed by `invoke(None)`, on the reasoning that the next
    router answers a failed status by routing to `finalize` without spending a call. Measured,
    that reasoning does not hold, in opposite ways on the two paths it was written for:

      * **After a node raised** - the final delivery's case - `update_state` discards the failed
        task, so the graph has nothing left to run. `invoke(None)` returns immediately,
        `finalize` never executes, and `jobs.status` stays `running` with `completed_at` NULL.
        Decision 9's "the job is terminal **and** the alarm fires" got only the second half.
      * **After a node returned** - the runtime bound's case - the pending task survives, so
        `invoke(None)` **runs the next node first**. That is one more LLM call after the bound
        tripped, which is exactly what decision 7's own test requirement forbids.

    Writing `finish_job` here is what `finalize_node` does when it is reachable - the node
    "records the outcome, it does not decide it", and there is no audit row and no other write
    behind it - so this is the same record, made by the process that owns the job's lifetime
    when the graph cannot make it. `update_state` still runs first, so the checkpoint and the
    row agree, and a redelivery finds `completed_at` set and deletes the message.

    A failure here is logged rather than raised because final-delivery callers require best-effort
    cleanup. The boolean is load-bearing for timeout acknowledgement: it is true only after both
    the checkpoint and terminal row/audit transaction succeeded.
    """
    try:
        deps.graph.update_state(run_config(job_id), {"status": "failed", "failure_reason": reason})
        state = _checkpoint_state(deps.graph, job_id)
        if state is None:
            raise RuntimeError("finalization checkpoint could not be read back")
        queries.finish_job(
            deps.engine,
            job_id=job_id,
            status="failed",
            # The reason reaches the `job_finished` row from here too (ADR 0009 decision 5).
            # `job_timeout` and `job_dead_lettered` are the two failures no node is alive to
            # record, which is exactly the case ADR 0008 said the checkpoint alone could not
            # be trusted to explain later.
            failure_reason=reason,
            quality_flag=state["quality_flag"],
            revision_count=state["revision_count"],
            llm_calls_used=state["llm_calls_used"],
        )
        terminal = queries.read_job(deps.engine, job_id)
        if terminal is None or terminal.completed_at is None or terminal.status != "failed":
            raise RuntimeError("terminal job row could not be verified after finalization")
        logger.error("job %s: failed with %s", job_id, reason)
        return True
    except Exception:
        logger.exception("job %s: could not record %s", job_id, reason)
        return False


def _gate_delivery_is_acknowledgeable(deps: WorkerDeps, job_id: str) -> bool:
    """Whether a durable human interrupt has a usable API projection.

    The ordinary gate node commits ``awaiting_approval`` before interrupting, so that path needs
    only the row read. An inconsistent row takes the more expensive recovery path: reread the
    checkpoint, reconcile, then verify the row. Any uncertainty preserves redelivery.
    """
    try:
        job = queries.read_job(deps.engine, job_id)
    except Exception:
        logger.exception("job %s: could not verify its human-gate projection", job_id)
        return False

    if job is None:
        logger.error("job %s: human interrupt has no job row", job_id)
        return False
    if job.completed_at is not None:
        return True
    if job.status == "awaiting_approval":
        return True

    try:
        if not _reconcile_status(deps, job_id):
            return False
        repaired = queries.read_job(deps.engine, job_id)
    except Exception:
        logger.exception("job %s: could not reconcile its human-gate projection", job_id)
        return False

    usable = repaired is not None and (
        repaired.completed_at is not None or repaired.status == "awaiting_approval"
    )
    if not usable:
        logger.error("job %s: human-gate projection remains unusable after reconciliation", job_id)
    return usable


def _reconcile_status(deps: WorkerDeps, job_id: str) -> bool:
    """`jobs.status`, derived from the checkpoint rather than asserted beside it.

    ADR 0007 invariant 4, moved from the API to here and unchanged in rule: terminal is left
    alone, a pending interrupt means `awaiting_approval`, and anything else means `running`.
    The predicate is the interrupt and not `next`, because a job that has not yet entered the
    gate also reports `next == ("human_gate",)` while no human is being waited on.
    """
    try:
        waiting = _waiting_at_the_gate(deps.graph, job_id)
    except Exception:
        logger.exception("job %s: could not read the checkpoint to reconcile its status", job_id)
        return False

    status: JobStatus = "awaiting_approval" if waiting else "running"
    queries.set_job_status(deps.engine, job_id=job_id, status=status)
    return True


def _waiting_at_the_gate(graph: ResearchGraph, job_id: str) -> bool:
    return bool(graph.get_state(run_config(job_id)).interrupts)


def _checkpoint_state(graph: ResearchGraph, job_id: str) -> ResearchState | None:
    """The job's durable state, or None when nothing has ever run it."""
    values = graph.get_state(run_config(job_id)).values
    return cast(ResearchState, values) if values else None


def _is_final_delivery(deps: WorkerDeps, message: JobMessage) -> bool:
    """Whether a failure now sends this message to the dead-letter queue."""
    return deps.final_delivery_at is not None and message.receive_count >= deps.final_delivery_at


# --- Startup --------------------------------------------------------------------------


def check_queue(queue: JobQueue) -> QueueSettings:
    """Validate load-bearing queue attributes and return the worker's runtime settings.

    Two checks are loud rather than clamped - the same reason
    `config._researcher_concurrency` refuses a value out of range: a queue that behaves
    differently from the one that was configured is expensive to read back off a run.

    **FIFO** (decision 4) is what keeps one job to one writer, which `_write_findings` assumes.
    **Visibility** must be positive because ADR 0015 derives a one-third renewal cadence from the
    queue's real value. It is no longer compared with a fictional static worst-case node duration;
    active renewal is the ownership mechanism.
    """
    attributes = queue.attributes()

    if not is_fifo(attributes):
        raise RuntimeError(
            "the job queue is not FIFO; ADR 0010 decision 4 needs MessageGroupId to keep one "
            "job to one writer"
        )

    actual = visibility_timeout(attributes)
    if actual <= 0:
        raise RuntimeError("the job queue must have a positive visibility timeout")
    call_envelope = sqs_call_envelope_seconds()
    if actual <= VISIBILITY_RENEWAL_DIVISOR * call_envelope:
        raise RuntimeError(
            f"the job queue visibility timeout ({actual}s) cannot fit the V/3 cadence, "
            f"V/3 safety margin, and bounded SQS renewal call ({call_envelope:.0f}s)"
        )

    final_delivery_at = max_receive_count(attributes)
    if final_delivery_at is None:
        logger.warning("the job queue has no redrive policy, so no delivery is ever the last")
    return QueueSettings(
        visibility_timeout_s=actual,
        final_delivery_at=final_delivery_at,
    )


@dataclass(frozen=True)
class Credentials:
    """The seven variables this process cannot start without, narrowed once (ADR 0012 dec 4).

    They are optional on `Config` so the **API** can start with none of the last four set -
    that is what makes guidelines §13's least-privilege table a property of the code. The
    consequence is that each process has to state what it needs, and this is the worker's
    statement: it runs the graph, so it assumes an LLM, a web-search key, a database, a queue
    and a bucket, and it says so at startup rather than at the first job.

    **`S3_BUCKET` joined the list in step 22a**, because the export node now writes an
    artifact. Discovering a missing bucket at export time would mean failing a job that had
    already paid for its whole pipeline, which is the failure this whole function exists to
    move to startup.
    """

    database_url: str
    queue_url: str
    s3_bucket: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    tavily_api_key: str


def required_credentials(config: Config) -> Credentials:
    """Narrow all seven loudly, naming the first one that is missing.

    Separate from `main()` so the statement can be tested without opening a connection pool -
    "which variables does the worker refuse to start without?" is a question worth a test, and
    an entrypoint that has already built half a process is not where it can be asked.
    """
    return Credentials(
        database_url=required(config.database_url, "DATABASE_URL"),
        queue_url=required(config.sqs_queue_url, "SQS_QUEUE_URL"),
        s3_bucket=required(config.s3_bucket, "S3_BUCKET"),
        llm_base_url=required(config.llm_base_url, "LLM_BASE_URL"),
        llm_api_key=required(config.llm_api_key, "LLM_API_KEY"),
        llm_model=required(config.llm_model, "LLM_MODEL"),
        tavily_api_key=required(config.tavily_api_key, "TAVILY_API_KEY"),
    )


def check_redis(client: Redis, config: Config) -> None:
    """Refuse to start when Redis does not answer, and say so (guidelines §11, §17).

    **This is the same "refuse loudly rather than clamp quietly" `check_queue` applies above,
    and it is the fail-closed rule reaching startup.** The shared rate limiter is the one
    Redis responsibility that fails closed: without a token there is no LLM call, so a worker
    that started against an unreachable Redis would take a message, fail its first node with
    `rate_limiter_unavailable`, leave the message, and do that again on every redelivery until
    the job dead-lettered. Failing here costs one log line instead of three deliveries.

    The caches and the URL set share this client and would have been happy without it - they
    fail open by design - so what this check really protects is the limiter. It is stated that
    way rather than as "Redis must be up", because the two halves of `redisstore` genuinely
    differ and the difference is the design (ARCHITECTURE.md §20 row 29).
    """
    if not reachable(client):
        # ASCII only, deliberately: this is the last thing a failing worker prints, and a
        # Windows console defaults to cp1252, where a section sign raises
        # `UnicodeEncodeError` while the process is already busy dying. Every other startup
        # refusal in this file follows the same rule.
        raise RuntimeError(
            f"redis at {config.redis_url} did not answer. The shared rate limiter fails "
            f"closed (guidelines section 11), so a worker cannot make an LLM call without it"
        )


def main() -> int:  # pragma: no cover - the entrypoint itself; its parts are tested
    """Build everything once, then poll until a signal arrives."""
    config = load_config()
    logging.basicConfig(level=config.log_level)

    credentials = required_credentials(config)
    queue = build_queue(
        credentials.queue_url, region=config.aws_region, endpoint_url=config.aws_endpoint_url
    )
    queue_settings = check_queue(queue)

    redis = build_redis(config.redis_url)
    check_redis(redis, config)

    shutdown = Shutdown()
    shutdown.install()

    with ExitStack() as stack:
        checkpointer = stack.enter_context(postgres_checkpointer(credentials.database_url))
        engine = create_database_engine(credentials.database_url)
        stack.callback(engine.dispose)
        stack.callback(redis.close)
        # The worker is the only process that calls a model, so it is the only one that holds
        # the limiter - and the limiter is the reason `redis` is not optional here (§11).
        llm = LLMClient(
            config,
            client=OpenAI(base_url=credentials.llm_base_url, api_key=credentials.llm_api_key),
            limiter=RedisRateLimiter(redis, requests_per_minute=config.llm_rpm_limit),
        )
        deps = WorkerDeps(
            config=config,
            engine=engine,
            graph=build_graph(
                config=config,
                llm=llm,
                cache=RedisCache(redis),
                urls=RedisUrlDeduplicator(redis),
                db=engine,
                # The worker is the only process that writes an artifact; the API only
                # presigns one (guidelines §13's least-privilege table).
                artifacts=build_artifact_store(
                    credentials.s3_bucket,
                    region=config.aws_region,
                    endpoint_url=config.aws_endpoint_url,
                ),
                checkpointer=checkpointer,
            ),
            queue=queue,
            final_delivery_at=queue_settings.final_delivery_at,
            queue_visibility_timeout_s=queue_settings.visibility_timeout_s,
            # Installed before the graph is built, so a signal arriving during a slow startup
            # is already recorded by the time the loop first looks.
            shutdown=shutdown,
        )
        logger.info("worker ready on %s, redis at %s", credentials.queue_url, config.redis_url)
        run(deps)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
