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

    **`MAX_JOB_RUNTIME` bounds one invocation, not a job's lifetime** (ADR 0010 decision 7). A
    job that waits three days at the gate must not fail on resume. The bound is checked between
    nodes, because a node in flight is inside a blocking LLM request - so the effective bound is
    the configured one plus the longest a single node can take, which is what decision 8 sizes
    the visibility timeout against, and what this worker refuses to start without.

    **The reviewer's decision is read from the audit trail, never from the message** (ADR 0011).
    Which visit to read is derived from the checkpoint's own `llm_calls_used`, the identical
    computation the endpoint performed when it recorded the decision. The key is derived on both
    sides and passed between them nowhere.

    **`jobs.status` is reconciled in a `finally`, on both paths** (ADR 0011 decision 4). The
    rule is ADR 0007 invariant 4 unchanged and the predicate is still the pending interrupt
    rather than `next`; what moved is which process owns the `finally`.

    **Shutdown finishes the message it is holding** (ADR 0010's graceful-shutdown row). SIGTERM
    stops the next receive, never the invocation in flight: LangGraph checkpoints after each
    node, so letting the current invocation return is what leaves the checkpoint and the
    database agreeing. A worker killed harder than that leaves the message undeleted, which is
    the recovery path rather than a hole.

WHO CALLS IT
    Nothing imports it. It is an entrypoint: `python -m worker`.
"""

from __future__ import annotations

import logging
import signal
import time
from contextlib import ExitStack
from dataclasses import dataclass
from types import FrameType
from typing import Any, cast

from langgraph.types import Command
from openai import OpenAI
from redis import Redis
from sqlalchemy.engine import Engine

from config import Config, load_config, required
from database import queries
from database.queries import create_database_engine
from graph.build import ResearchGraph, build_graph, postgres_checkpointer
from graph.state import ResearchState, new_state, run_config
from jobqueue import (
    JobMessage,
    JobQueue,
    QueueError,
    build_queue,
    is_fifo,
    max_receive_count,
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

BACKOFF_SECONDS = 10
"""guidelines §17's main-tier retry schedule: three attempts with 2s + 8s between them. It is
the constant half of decision 8's worst-case node."""

RECEIVE_ERROR_PAUSE_S = 5.0
"""How long to wait after a receive that raised, before polling again.

Without it a queue that is unreachable turns into a hot loop against a broken endpoint. It is
deliberately not a backoff schedule: the worker has one job to do and no bound to give up at -
an operator watching the logs is the escalation, which is what the DLQ alarm is for.
"""


def worst_case_node_seconds(config: Config) -> float:
    """The longest a single node can take: three main-tier attempts plus the backoff between
    them (ADR 0010 decision 8). The runtime bound can only be checked between nodes, so this is
    the slack the queue's visibility timeout has to cover on top of it."""
    return 3 * config.llm_main_timeout_s + BACKOFF_SECONDS


def required_visibility_timeout(config: Config) -> float:
    """`MAX_JOB_RUNTIME + 3 x LLM_MAIN_TIMEOUT_S + 10`, which the queue must exceed."""
    return config.max_job_runtime + worst_case_node_seconds(config)


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


class Shutdown:
    """Whether SIGTERM or SIGINT has arrived.

    A flag rather than an exception because of *where* the signal lands: raising out of a
    handler could unwind the middle of a graph invocation and leave the checkpoint behind the
    database, which is the one thing ADR 0005 decision 2 says to avoid. The loop reads the flag
    between messages instead, and the invocation in flight is always allowed to finish.

    A second signal is not escalated to a hard exit. The container runtime already escalates -
    SIGTERM then SIGKILL after its grace period - and a worker that killed itself faster than
    that would only lose the checkpoint the first signal was trying to protect.
    """

    def __init__(self) -> None:
        self.requested = False

    def install(self) -> None:
        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, self._request)

    def _request(self, signum: int, _frame: FrameType | None) -> None:
        logger.info("signal %s received; finishing the message in flight then stopping", signum)
        self.requested = True


# --- The loop -------------------------------------------------------------------------


def run(deps: WorkerDeps, *, shutdown: Shutdown, max_messages: int | None = None) -> int:
    """Poll until shutdown, and answer how many messages were handled.

    `max_messages` bounds the loop for a test. In production it is None and the loop ends only
    on a signal, which is what `handled` counts against.
    """
    handled = 0
    while not shutdown.requested and (max_messages is None or handled < max_messages):
        try:
            message = deps.queue.receive()
        except QueueError:
            logger.exception("could not receive from the queue")
            time.sleep(RECEIVE_ERROR_PAUSE_S)
            continue

        if message is None:
            continue

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

    try:
        if _handle_or_skip(deps, message):
            deps.queue.delete(message)
    except Exception:
        logger.exception("job %s: invocation failed", message.job_id)
        if final:
            # Last chance to say what happened to it. The message is deliberately not deleted,
            # so it reaches the dead-letter queue and the alarm on DLQ depth fires.
            _finalise(deps, message.job_id, reason="job_dead_lettered")
        # Not re-raised: one poisonous job must not stop the worker serving every other one.
        # The message stays invisible until its visibility timeout lapses, then redelivers.


def _handle_or_skip(deps: WorkerDeps, message: JobMessage) -> bool:
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

    state = _checkpoint_state(deps.graph, message.job_id)
    if state is None:
        return _start(deps, message)
    return _continue(deps, message, state)


def _start(deps: WorkerDeps, message: JobMessage) -> bool:
    """A job nothing has run yet. `queued -> running`, then the graph from its first state."""
    job = queries.read_job(deps.engine, message.job_id)
    assert job is not None  # read a moment ago in `_handle_or_skip`
    queries.set_job_status(deps.engine, job_id=message.job_id, status="running")
    logger.info("job %s: starting", message.job_id)

    return _invoke(
        deps,
        message.job_id,
        new_state(job_id=message.job_id, user_id=message.user_id, question=job.question),
    )


def _continue(deps: WorkerDeps, message: JobMessage, state: ResearchState) -> bool:
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

    `_invoke`'s `finally` is the one writer of the column from here on, which is what ADR 0011
    decision 4 asks for: derived from the checkpoint, on both paths, after the work.
    """
    if not _waiting_at_the_gate(deps.graph, message.job_id):
        logger.info("job %s: continuing a run that did not finish", message.job_id)
        return _invoke(deps, message.job_id, None)

    calls_used = state["llm_calls_used"]
    recorded = queries.read_gate_decision(deps.engine, message.job_id, calls_used=calls_used)
    if recorded is None:
        # A resume message whose visit has no decision row is a bug, and guessing which of the
        # three decisions was meant is the silent wrong answer the gate node already refuses.
        # Leaving the message means it redelivers, by which time the endpoint may have caught up.
        logger.error(
            "job %s: a resume message for visit %d has no decision on record",
            message.job_id,
            calls_used,
        )
        return False

    decision = GateDecision(
        decision=cast(Any, recorded["decision"]),
        note=recorded.get("note"),
        edits=recorded.get("edits"),
    )
    logger.info("job %s: resuming with %s", message.job_id, decision.decision)
    return _invoke(deps, message.job_id, Command(resume=decision.model_dump()))


def _invoke(deps: WorkerDeps, job_id: str, first: Any) -> bool:
    """Run the graph and say whether this delivery finished with the job.

    **`stream` rather than `invoke`, for one reason:** the runtime bound can only be checked
    between nodes, and consuming the update stream is where "between nodes" exists. It is the
    same shape `scripts/measure_jobs.py::_drain` already uses.

    The reconcile is in a `finally` because the failure path is the one that needs it: an
    invocation that raised used to leave the row claiming a human still held a job the
    checkpoint had already moved past (ADR 0007 invariant 4, ADR 0011 decision 4).
    """
    deadline = time.monotonic() + deps.config.max_job_runtime
    try:
        for _update in deps.graph.stream(first, run_config(job_id), stream_mode="updates"):
            if time.monotonic() >= deadline:
                logger.error("job %s: over MAX_JOB_RUNTIME for this invocation", job_id)
                _finalise(deps, job_id, reason="job_timeout")
                return True
    finally:
        _reconcile_status(deps, job_id)

    # The graph stopped. Either a person is being waited on, or the job ended - and both are
    # outcomes this delivery is finished with.
    return True


def _finalise(deps: WorkerDeps, job_id: str, *, reason: str) -> None:
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

    A failure here is logged rather than raised. It is already the unhappy path, and letting it
    propagate would replace a job that at least has a reason with one that has nothing.
    """
    try:
        deps.graph.update_state(run_config(job_id), {"status": "failed", "failure_reason": reason})
        state = _checkpoint_state(deps.graph, job_id)
        queries.finish_job(
            deps.engine,
            job_id=job_id,
            status="failed",
            quality_flag=None if state is None else state["quality_flag"],
            revision_count=0 if state is None else state["revision_count"],
            llm_calls_used=0 if state is None else state["llm_calls_used"],
        )
        logger.error("job %s: failed with %s", job_id, reason)
    except Exception:
        logger.exception("job %s: could not record %s", job_id, reason)


def _reconcile_status(deps: WorkerDeps, job_id: str) -> None:
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
        return

    status: JobStatus = "awaiting_approval" if waiting else "running"
    queries.set_job_status(deps.engine, job_id=job_id, status=status)


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


def check_queue(queue: JobQueue, config: Config) -> int | None:
    """Refuse to start against a queue that cannot hold a job, and answer its `maxReceiveCount`.

    Two checks, both from ADR 0010, both loud rather than clamped - the same reason
    `config._researcher_concurrency` refuses a value out of range: a queue that behaves
    differently from the one that was configured is expensive to read back off a run.

    **FIFO** (decision 4) is what keeps one job to one writer, which `_write_findings` assumes.
    **The visibility timeout** (decision 8) has to exceed the runtime bound plus the longest a
    single node can take, or a job still running goes back on the queue and a second worker
    picks it up.
    """
    attributes = queue.attributes()

    if not is_fifo(attributes):
        raise RuntimeError(
            "the job queue is not FIFO; ADR 0010 decision 4 needs MessageGroupId to keep one "
            "job to one writer"
        )

    required = required_visibility_timeout(config)
    actual = visibility_timeout(attributes)
    if actual <= required:
        raise RuntimeError(
            f"the queue's visibility timeout is {actual}s and must exceed {required:.0f}s "
            f"(MAX_JOB_RUNTIME={config.max_job_runtime} + 3 x "
            f"LLM_MAIN_TIMEOUT_S={config.llm_main_timeout_s} + {BACKOFF_SECONDS})"
        )

    final_delivery_at = max_receive_count(attributes)
    if final_delivery_at is None:
        logger.warning("the job queue has no redrive policy, so no delivery is ever the last")
    return final_delivery_at


@dataclass(frozen=True)
class Credentials:
    """The six variables this process cannot start without, narrowed once (ADR 0012 dec 4).

    They are optional on `Config` so the **API** can start with none of the last four set -
    that is what makes guidelines §13's least-privilege table a property of the code. The
    consequence is that each process has to state what it needs, and this is the worker's
    statement: it runs the graph, so it assumes an LLM, a web-search key, a database and a
    queue, and it says so at startup rather than at the first job.
    """

    database_url: str
    queue_url: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    tavily_api_key: str


def required_credentials(config: Config) -> Credentials:
    """Narrow all six loudly, naming the first one that is missing.

    Separate from `main()` so the statement can be tested without opening a connection pool -
    "which variables does the worker refuse to start without?" is a question worth a test, and
    an entrypoint that has already built half a process is not where it can be asked.
    """
    return Credentials(
        database_url=required(config.database_url, "DATABASE_URL"),
        queue_url=required(config.sqs_queue_url, "SQS_QUEUE_URL"),
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
    final_delivery_at = check_queue(queue, config)

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
                checkpointer=checkpointer,
            ),
            queue=queue,
            final_delivery_at=final_delivery_at,
        )
        logger.info("worker ready on %s, redis at %s", credentials.queue_url, config.redis_url)
        run(deps, shutdown=shutdown)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
