"""
WHY THIS FILE EXISTS
    The six endpoints in guidelines §12 and ARCHITECTURE.md §10, and nothing else. This is
    the outermost boundary in the system, so it gets the same typed treatment as every
    internal one: a model in, a model out, and one error shape for every failure.

    Six things here are decisions rather than plumbing.

    **One endpoint owns all three gate outcomes.** There is no `/reject` route and no `/edit`
    route: `POST /jobs/{id}/approve` takes `{decision: "approve" | "reject" | "edit"}`,
    because approving a report is an authorization decision and splitting it across three
    URLs would mean three places to get that authorization right (ARCHITECTURE.md §10).

    **One gate visit takes one decision, and re-sending it is a retry** (ADR 0007). The visit
    is identified by the job's live call count, which is the key `gate_opened` already carries;
    the same decision on a visit that has one continues the graph from the checkpoint without
    writing a second row, a different one is refused with `gate_already_decided`, and the
    job's status is reconciled from the checkpoint on the way out whether the resume returned
    or raised. The failure this closes is a resume that dies halfway: the row said a human was
    still holding the job, the checkpoint said it was mid-pass, and the retry that fixed it
    charged the reviewer a second edit.

    **An edit is refused before anything is spent.** `refuse_edit()` runs against the live
    call count from the checkpoint and the edit count from the audit trail, and a refusal
    returns `409` having written no decision, resumed no graph, and spent no LLM call. The
    live count is deliberately not `jobs.llm_calls_used`: that column is written by
    `finalize`, so it reads `0` for the whole time a job waits at the gate and a check
    against it would allow every edit silently (ADR 0006 decision 7).

    **The gate is claimed before it is answered.** Two reviewers deciding at once is a
    conditional update - `awaiting_approval → running`, one winner - so the loser gets the
    same `409` a second decision on a finished job gets, rather than both resuming the same
    thread.

    **Identity comes from the credential.** `audit_events.actor` is the `user_id` the API key
    maps to, never a name in the body. An approval that cannot say who made it is
    accountability theatre (guidelines §9, §16).

    **The artifact route keys on the artifact, never on the status** (ADR 0009 decision 3).
    `GET /jobs/{id}/report` answers a 15-minute presigned URL when `jobs.exported_at` is set
    and `404 not_exported` when it is not - so a job that failed at export and was recovered by
    an operator's re-export is downloadable while still reading `status: "failed"`. The API
    signs; it never streams report bytes and never writes an object.

    **The gate is read on its own route, not on `GET /jobs/{id}`** (ADR 0013). A reviewer
    must be able to see what they are approving, and none of the six fields the status route
    carries is the draft: `report` there means the *exported* body, which is null until the
    export gate passes. `GET /jobs/{id}/gate` answers with the payload the gate node already
    builds, rebuilt from the checkpoint. Keeping it off the status route is deliberate - that
    one is polled every few seconds for twenty minutes, and a full report on every poll is
    the cost of a rare read paid by the common one.

    **Both write routes end in an enqueue, and neither runs a graph** (ADR 0010, ADR 0011).
    `POST /jobs` commits the row and then sends a start message; `POST /jobs/{id}/approve`
    records the decision, claims the gate, and sends a resume. That is what makes both of
    them return in milliseconds, and it is why the second one answers `running` rather than
    the outcome - a caller that needs the outcome polls `GET /jobs/{id}`.

    **A send that fails is a `503`, never a `202` or a `200`.** The row and the decision are
    already committed by then, so the honest answer is that the work is not moving yet; the
    fix is the same request again, which ADR 0007 makes a retry that writes nothing.

WHO CALLS IT
    app.py mounts the router. Tests drive it through Starlette's TestClient.
"""

from __future__ import annotations

import hashlib
import logging
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from config import Config
from database import queries
from graph.state import TERMINAL_STATUSES, ResearchState, refuse_edit, reviewer_payload, run_config
from jobqueue import JobQueue, QueueError
from routes.auth import Authenticator, Identity
from schemas import GateDecision, JobStatus, QualityFlag

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_QUESTION_CHARS = 500
"""How long a research question may be.

guidelines §16 requires the question to be length-capped and control characters stripped
before it reaches a prompt or the database, without naming a number. 500 is generous for a
competitive-research question - the measured set's longest is well under it - and small
enough that the Planner's prompt cannot be crowded out by one field. It is a constant rather
than a setting because nothing about an environment should change what a valid question is.
"""

MAX_REVIEWER_TEXT_CHARS = 500
"""How long a reviewer's `edits` or `note` may be, after cleaning.

**Deliberately the same number as the question**, because ADR 0006 decision 8 asks for "the
same treatment `POST /jobs` already gives `question`" and a second, unrelated limit would be a
second policy to explain. The two are named separately rather than shared because they cap
different fields for the same reason, and one could later move without dragging the other.

The reason it needs a cap at all is the same reason the question does: `edits` reaches the
Synthesizer's prompt, and both fields reach `audit_events.detail`.
"""

PROBE_THREAD_ID = "health-probe"
"""The thread id `/health` reads to find out whether the checkpoint store answers.

Not a job id, and it cannot collide with one: job ids are UUIDs. The read is expected to find
nothing - what is under test is whether the store can be asked at all.
"""


# --- What comes in and goes out -----------------------------------------------------


class SubmitRequest(BaseModel):
    question: str


class SubmitResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobResponse(BaseModel):
    """`GET /jobs/{id}`'s documented body (guidelines §12).

    `report` is present only once the export gate has passed and written it, which is what
    `null` means here - not "still running".
    """

    job_id: str
    status: JobStatus
    phase: str
    revision_count: int
    quality_flag: QualityFlag | None
    report: dict[str, Any] | None


class DecisionResponse(BaseModel):
    job_id: str
    status: JobStatus


class RedisProbe(Protocol):
    """The one Redis question the API asks: does it answer?

    Declared here rather than imported for the reason ADR 0012 gives: the route layer imports
    no agent, no tool, and nothing it does not need. It needs one boolean for `/health`, so
    that is the whole contract - no `get`, no `set`, no bucket.
    """

    def reachable(self) -> bool: ...


class ReportPresigner(Protocol):
    """The one S3 question the API asks: what is this job's artifact URL, and until when?

    A Protocol for the same reason `RedisProbe` is one, and with a sharper edge: the API's
    task role may **presign** objects and the worker's may **write** them (guidelines §13's
    least-privilege table), so the surface the route layer declares is the half it is allowed
    to use. `put_report` is deliberately not in here.
    """

    def presign(self, job_id: str) -> tuple[str, datetime]: ...


@dataclass(frozen=True)
class RouteDeps:
    """Everything the routes need, built once at startup and read from `app.state`.

    **There is no graph and no LLM client here** (ADR 0012). `checkpoints` is a checkpoint
    *reader* on the API's own pool: two routes read durable state through it - the gate-visit
    key and the gate view - and neither needs the topology, an agent, or a credential. The
    worker owns `setup()`, execution, and every LLM call.

    `redis` is a health probe and nothing more (step 21). The API never caches, never
    deduplicates a URL, and never takes a rate-limit token - those are the worker's, because
    the worker is what fetches pages and calls models.
    """

    config: Config
    engine: Engine
    checkpoints: BaseCheckpointSaver[Any]
    queue: JobQueue
    authenticator: Authenticator
    """Whichever of the two credentials this deployment accepts (ADR 0020 decision 2).

    The route layer never learns which. It asks for the caller behind a header and receives an
    `Identity` or `None`, exactly as it did when the only answer came from a hashed key table.
    """

    redis: RedisProbe | None = None
    artifacts: ReportPresigner | None = None
    """What `GET /jobs/{id}/report` signs a URL with (step 22a).

    Optional on the same terms as `redis`: a test that is not about the artifact route should
    not have to supply one, and `app.py` always does. It is a presigner and nothing else - the
    API never writes an object and never holds report bytes.
    """


class ApiError(Exception):
    """One error shape, everywhere (guidelines §12).

    `code` is a stable string callers branch on; `message` is for humans and may change.
    Nothing else reaches the body - no stack trace, no internal path, no SQL.
    """

    def __init__(self, status_code: int, code: str, message: str, job_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.job_id = job_id

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content={"error": {"code": self.code, "message": self.message, "job_id": self.job_id}},
        )


# --- Authentication and authorization -----------------------------------------------


def _deps(request: Request) -> RouteDeps:
    return cast(RouteDeps, request.app.state.deps)


def _caller(request: Request) -> Identity:
    """The authenticated caller, or `401`.

    Absent header, wrong scheme, unknown key, expired token, wrong issuer and a token carrying
    no role group are one answer on purpose: distinguishing them tells an attacker which half of
    the credential to work on.
    """
    identity = _deps(request).authenticator.identity(request.headers.get("authorization"))
    if identity is None:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED, "unauthenticated", "A valid credential is required"
        )
    return identity


def _reviewer(request: Request) -> Identity:
    """The caller, if they may decide at the gate. A valid `submitter` key gets `403`."""
    identity = _caller(request)
    if not identity.is_reviewer:
        raise ApiError(
            status.HTTP_403_FORBIDDEN, "reviewer_role_required", "Only a reviewer may decide here"
        )
    return identity


def _owned(job: sa.Row[Any], identity: Identity) -> None:
    """A submitter reads its own jobs; a reviewer reads any job it may be asked to decide on.

    Deciding without reading would be approving unseen, which is the one thing the gate
    exists to prevent (ARCHITECTURE.md §10's role table).
    """
    if job.user_id != identity.user_id and not identity.is_reviewer:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "not_the_owner",
            "This job belongs to someone else",
            job_id=job.job_id,
        )


# --- 1. Submit a research question --------------------------------------------------


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
def submit_job(body: SubmitRequest, request: Request) -> SubmitResponse:
    """Create the job row, then enqueue the pointer that makes a worker pick it up.

    The row is written and committed first, and only then is the message sent. That order is
    the one that can be recovered from: a row with no message is queryable and re-enqueueable,
    while a message with no row would name a job that does not exist.

    The idempotency key is derived here, server-side, from the caller's own identity: a
    client cannot weaken it by sending a random value, and the database - not an
    application-level check that races between two API tasks - is what decides a duplicate
    (ARCHITECTURE.md §9).
    """
    deps = _deps(request)
    identity = _caller(request)
    question = _clean_question(body.question)
    job_id = str(uuid4())
    key = _idempotency_key(identity.user_id, question)

    try:
        queries.create_job(
            deps.engine,
            job_id=job_id,
            user_id=identity.user_id,
            question=question,
            idempotency_key=key,
            actor=identity.user_id,
        )
    except IntegrityError:
        existing = queries.read_job_by_idempotency_key(deps.engine, key)
        if existing is None:  # pragma: no cover - a unique violation with no row to find
            raise ApiError(
                status.HTTP_409_CONFLICT, "duplicate_job", "This question is already submitted"
            ) from None
        logger.info("duplicate submission for job %s", existing.job_id)
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "duplicate_job",
            "This question was already submitted today",
            job_id=existing.job_id,
        ) from None

    # The insert is committed and the send is a separate call - they cannot share a
    # transaction, so ADR 0010 decision 10 chooses which half is allowed to be true alone. The
    # row stays: it holds the `idempotency_key` that makes a resubmission converge on this same
    # job rather than creating a second one, and it is what a re-enqueue would target.
    try:
        deps.queue.send_start(job_id=job_id, user_id=identity.user_id, idempotency_key=key)
    except QueueError:
        logger.exception("job %s: recorded but not enqueued", job_id)
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "enqueue_failed",
            "The job was recorded but could not be queued",
            job_id=job_id,
        ) from None

    return SubmitResponse(job_id=job_id, status="queued")


# --- 2. Poll status, and read the report once it exists -----------------------------


@router.get("/jobs/{job_id}")
def read_job(job_id: str, request: Request) -> JobResponse:
    deps = _deps(request)
    identity = _caller(request)
    job = _job_or_404(deps.engine, job_id)
    _owned(job, identity)

    return JobResponse(
        job_id=job.job_id,
        status=job.status,
        phase=_phase(job),
        revision_count=job.revision_count,
        quality_flag=job.quality_flag,
        report=job.report_json,
    )


# --- 3. Read what the gate is asking about, then decide -----------------------------


@router.get("/jobs/{job_id}/gate")
def read_gate(job_id: str, request: Request) -> JSONResponse:
    """What the reviewer is being asked to approve (ADR 0013).

    The body is `graph.state.reviewer_payload()` **verbatim**, keys and order unchanged -
    the same value the gate node passes to `interrupt()`. It is deliberately not a Pydantic
    model here: a second definition of that shape would be a second thing to keep in step
    with §12's ordering, and the ordering is the contract.

    **It is rebuilt from the checkpoint, not from the job row**, because five of its values
    exist nowhere else: `score`, `failed_dimensions`, `revision_count`, the report's section
    bodies, and each claim's quote. `jobs.report_json` is null until export, `revision_count`
    and `quality_flag` are written by `finalize`, and no table holds a section body or a
    verdict quote. A route built from Postgres rows would answer with a report the reviewer
    cannot read.

    **Rebuilding reproduces the payload exactly.** `interrupt()` discards the node's writes
    and LangGraph re-runs the node from the top on resume, so the checkpoint holds the state
    the gate node was invoked with - the same property ADR 0007 relies on for its visit key.
    Nothing has to be persisted to make this work, and nothing is.

    **Reading is not deciding.** No write, no audit row, no gate claim, no status change, no
    LLM call, no node execution. `get_state` loads a checkpoint; it does not run a graph.
    """
    deps = _deps(request)
    identity = _caller(request)
    job = _job_or_404(deps.engine, job_id)
    _owned(job, identity)

    if job.status != "awaiting_approval":
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "job_not_awaiting_approval",
            f"This job is {job.status} and has no gate open",
            job_id=job_id,
        )

    live = _live_state(deps.checkpoints, job_id)
    if live is None:
        # The row says a human is holding this job and the checkpoint has never heard of it,
        # which contradicts ADR 0007 invariant 4. There is genuinely no gate to show, so the
        # caller gets the code that says that; the contradiction goes to the log, where an
        # explanation an anonymous caller must not see belongs (guidelines §16).
        logger.error("job %s is awaiting_approval with no checkpoint behind it", job_id)
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "job_not_awaiting_approval",
            "This job has no gate open",
            job_id=job_id,
        )

    return JSONResponse(status_code=status.HTTP_200_OK, content=reviewer_payload(live))


@router.post("/jobs/{job_id}/approve")
def decide(job_id: str, body: GateDecision, request: Request) -> DecisionResponse:
    """Approve, reject, or edit - the one authenticated endpoint that owns all three.

    The order below is the contract, not a preference: nothing is written and nothing is
    resumed until the decision is known to be allowed, so a refused edit costs the job
    nothing at all.

    Which of ADR 0007's four cases this is comes first, because it decides what the rest of
    the request may do. A visit that already carries this exact decision is a **retry** - the
    reviewer's answer to a resume that died - so it claims nothing, records nothing, counts
    nothing, and resumes the graph from where the failure left it.

    The reviewer's text is cleaned before any of that, and `body` is rebound to the cleaned
    decision, so the audit row and the graph both read the cleaned value (ADR 0006 decision 8).
    """
    deps = _deps(request)
    identity = _reviewer(request)
    body = _clean_decision(body)
    job = _job_or_404(deps.engine, job_id)

    if job.status in TERMINAL_STATUSES or job.status == "queued":
        # A finished job has no gate to answer, however this request is phrased. The retry
        # rule below is scoped to a visit that has not finished.
        #
        # **`queued` is refused here rather than three lines further down**, and the ordering
        # is the point. A queued job has never been invoked: nothing has written a checkpoint
        # for it and no gate visit can carry a decision, so there is nothing for the read
        # below to learn - and on a database where Alembic has run but no worker has started
        # yet, LangGraph's own tables do not exist and that read raises. The caller would get
        # `500 internal_error` where the contract promises `409 job_not_awaiting_approval`.
        # Nothing else moves: `running` and `awaiting_approval` both fall through exactly as
        # before, which is what keeps ADR 0007's retry - a second identical decision arriving
        # after the worker has already resumed, and so after the row says `running` - working.
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "job_not_awaiting_approval",
            f"This job is {job.status} and has no gate open",
            job_id=job_id,
        )

    calls_used = _gate_visit(deps.checkpoints, job_id)
    recorded = queries.read_gate_decision(deps.engine, job_id, calls_used=calls_used)

    if recorded is None:
        _accept(deps, job, identity, body, calls_used=calls_used)
    elif recorded["decision"] != body.decision:
        # A reviewer who changes their mind waits for the job to come back to the gate, which
        # is the only point at which a new decision is a new decision (ADR 0007 invariant 3).
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "gate_already_decided",
            f"This gate visit was already answered with {recorded['decision']}",
            job_id=job_id,
        )
    else:
        logger.info("job %s: %s retried by %s", job_id, body.decision, identity.user_id)

    # ADR 0011 decision 3: a retried decision writes nothing, counts nothing, and enqueues -
    # which is what "continues the graph" means in a process that no longer runs one.
    _enqueue_resume(deps, job, calls_used=calls_used)

    decided = _job_or_404(deps.engine, job_id)
    return DecisionResponse(job_id=job_id, status=decided.status)


def _accept(
    deps: RouteDeps,
    job: sa.Row[Any],
    identity: Identity,
    body: GateDecision,
    *,
    calls_used: int,
) -> None:
    """A gate visit nobody has answered: check it, claim it, and record the decision.

    The order is ADR 0006's and ARCHITECTURE.md §12's, unchanged. The refusals come before
    the claim so a refused edit leaves the gate open, and the decision is written **before**
    the resume so a machine failure afterwards cannot lose the fact that a human decided.
    """
    job_id = cast(str, job.job_id)
    if job.status != "awaiting_approval":
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "job_not_awaiting_approval",
            f"This job is {job.status} and has no gate open",
            job_id=job_id,
        )

    if body.decision == "edit":
        _refuse_unaffordable_edit(deps, job_id, llm_calls_used=calls_used)

    if not queries.claim_gate(deps.engine, job_id):
        # Someone else answered this gate between the read above and here.
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "job_not_awaiting_approval",
            "This gate was already answered",
            job_id=job_id,
        )

    queries.record_reviewer_decision(
        deps.engine,
        job_id=job_id,
        actor=identity.user_id,
        decision=body.decision,
        note=body.note,
        edits=body.edits,
        calls_used=calls_used,
    )
    logger.info("job %s: %s by %s", job_id, body.decision, identity.user_id)


def _enqueue_resume(deps: RouteDeps, job: sa.Row[Any], *, calls_used: int) -> None:
    """Hand the job back to a worker. **The decision does not travel with it** (ADR 0011).

    It is already durable in `audit_events`, keyed by the same `(job_id, calls_used)` the
    message deduplicates on, and the worker reads it from there - so the reviewer's own words
    never reach the queue, and §20 row 8's "identifiers only, never state" survives.

    A failed send answers the same `503` `POST /jobs` uses, for the same reason: the decision is
    recorded and the gate is claimed, so the honest answer is that the work is not moving yet.
    Re-sending the identical decision is the fix, and ADR 0007 makes that a retry that writes
    nothing and counts nothing.

    **`_reconcile_status` is deliberately gone.** After ADR 0011 there is no resume here for it
    to bracket, and a second writer of that column with nothing to bracket is a hazard rather
    than a safety net (decision 4). `claim_gate` has already written `running`, which is true:
    the gate is answered and the work is queued. The worker owns the reconcile from now on.
    """
    job_id = cast(str, job.job_id)
    try:
        deps.queue.send_resume(
            job_id=job_id,
            user_id=cast(str, job.user_id),
            idempotency_key=cast(str, job.idempotency_key),
            calls_used=calls_used,
        )
    except QueueError:
        logger.exception("job %s: decision recorded but the resume was not enqueued", job_id)
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "enqueue_failed",
            "The decision was recorded but the job could not be queued",
            job_id=job_id,
        ) from None


def _refuse_unaffordable_edit(deps: RouteDeps, job_id: str, *, llm_calls_used: int) -> None:
    """Both of ADR 0006's edit bounds, checked before the graph is touched.

    The count of edits comes from the `reviewer_decision` rows this endpoint writes, and the
    call count comes from the **checkpoint** - the two things that are true right now, rather
    than the job row, which will not know either until the job ends.
    """
    refusal = refuse_edit(
        config=deps.config,
        llm_calls_used=llm_calls_used,
        edits_made=queries.count_reviewer_edits(deps.engine, job_id),
    )
    if refusal is None:
        return

    logger.info("job %s: edit refused (%s)", job_id, refusal)
    raise ApiError(
        status.HTTP_409_CONFLICT,
        refusal,
        "This job cannot take another edit",
        job_id=job_id,
    )


# --- 4. The exported artifact -------------------------------------------------------


@router.get("/jobs/{job_id}/report")
def read_report(job_id: str, request: Request) -> JSONResponse:
    """A 15-minute presigned URL for the artifact, or `404 not_exported`.

    **It keys on the artifact, not on the status** (ADR 0009 decision 3). That is
    ARCHITECTURE.md §3's sentence made executable - *"whether a report is downloadable is
    answered by the artifact existing"* - and it is what makes a recovered job's artifact
    reachable without rewriting the job's history: a job that failed at export and was
    re-exported by an operator reads `status: "failed"` forever and still answers `200` here.

    `exported_at IS NULL` is `404` and covers every way there can be no object: the job has not
    reached export, it was rejected, the gate blocked it, or the write was exhausted and the
    body is preserved in `jobs.report_json` with no artifact behind it. A caller that wants the
    body in that last case reads `GET /jobs/{id}`, which has always carried `report`.

    **The API never streams report bytes** (guidelines §12). It signs a URL and the client
    fetches the object directly, so a 20-minute job's output never occupies a worker - and
    signing reaches nothing, so this route needs no Redis, no graph, no LLM and no node
    execution, which keeps ADR 0012's boundary exactly where it was.
    """
    deps = _deps(request)
    identity = _caller(request)
    job = _job_or_404(deps.engine, job_id)
    _owned(job, identity)

    if job.exported_at is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "not_exported",
            "No artifact exists for this job",
            job_id=job_id,
        )

    if deps.artifacts is None:
        # The row says an artifact was written and this process cannot sign for it, which is a
        # deployment that is missing `S3_BUCKET` rather than anything the caller did. It gets
        # the generic failure; the explanation goes to the log (guidelines §16).
        logger.error("job %s has an artifact and this process has no presigner", job_id)
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "The request could not be completed",
            job_id=job_id,
        )

    url, expires_at = deps.artifacts.presign(job_id)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"url": url, "expires_at": expires_at.isoformat()},
    )


# --- 5. Liveness --------------------------------------------------------------------


@router.get("/health")
def health(request: Request) -> JSONResponse:
    """The single unauthenticated route, and minimal by design.

    A status and one boolean per dependency: no secret, no connection string, no hostname,
    no version, no job data, no counts, no error text. If a check needs to explain itself,
    that explanation goes to the logs (guidelines §16).

    **`redis` appeared with step 21, which is when the dependency became real.** Until then
    the key was deliberately absent rather than `false`, because reporting a dependency
    nothing reaches for would have failed every health check forever - and an unhealthy task
    is never replaced, which is the exact failure this route exists to avoid.

    **An unreachable Redis is `degraded`, not a detail.** The API itself does not need Redis:
    it makes no LLM call and fetches no page. What it is reporting is whether the *workers*
    can work - the shared rate limiter fails closed, so with Redis gone no LLM call can be
    made anywhere in the deployment. Failing this check is what takes the task out of the
    target group so new jobs stop arriving in the first place (ARCHITECTURE.md §8), which
    beats accepting jobs that would sit `queued` while every worker fails its first node.

    **`checkpoints` appeared with step 22b/c, and it reports the same kind of fact.** The API
    reads LangGraph's tables and owns none of them: Alembic never touches them (guidelines
    §19) and this process never calls `setup()` (ADR 0012 decisions 1 and 3), so on a database
    that has been migrated but never had a worker start against it, they do not exist. That is
    not a broken API - it can still accept jobs and report their status - it is a deployment
    in which **no job can ever run**, which is exactly what the Redis row above reports and
    exactly what a person needs told. It is `False` only in that one window: the tables are
    created once by the first worker and never go away.
    """
    deps = _deps(request)
    checks = {
        "db": _database_reachable(deps.engine),
        "redis": _redis_reachable(deps.redis),
        "checkpoints": _checkpoints_readable(deps.checkpoints),
    }
    healthy = all(checks.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )


# --- Helpers ------------------------------------------------------------------------


def _printable(raw: str) -> str:
    """Control and formatting characters out, runs of whitespace collapsed to one space.

    Unicode's C category covers control and formatting characters, which is what would
    otherwise carry an invisible instruction into a prompt (guidelines §16). Newlines and tabs
    are in it too, so what comes back is a single line - which is the point on a field the
    model is told to act on, because forged prompt structure needs a line break to look like
    prompt structure.
    """
    stripped = "".join(character for character in raw if unicodedata.category(character)[0] != "C")
    return " ".join(stripped.split())


def _clean_question(raw: str) -> str:
    """The question, cleaned and capped before it reaches a prompt or a row."""
    question = _printable(raw)
    if not question:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_question", "The question is empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_question",
            f"The question is longer than {MAX_QUESTION_CHARS} characters",
        )
    return question


def _clean_decision(body: GateDecision) -> GateDecision:
    """The reviewer's own words, cleaned at the edge exactly as the question is (ADR 0006).

    **Reviewer text is authenticated input, which is why this matters rather than why it does
    not.** Decision 8 deliberately does *not* wrap it in an untrusted block: it reaches the
    Synthesizer's prompt as an instruction the model is told to follow, because a reviewer is
    an authorised human and not a fetched page. A field the model obeys is exactly the field a
    control character should not survive in - and the same string is written to
    `audit_events.detail`, so both of guidelines §16's destinations, a prompt and the database,
    are on this path.

    It returns the decision the rest of the request uses, so there is no later point at which
    the raw text is still reachable: the audit row and the graph both read what comes back
    from here. `model_copy` rather than a fresh `GateDecision` so a field added to the model
    later is carried through instead of silently dropped.
    """
    edits = _clean_reviewer_text(body.edits, "edits")
    note = _clean_reviewer_text(body.note, "note")

    if body.decision == "edit" and edits is None:
        # `GateDecision` already refuses an edit with no text; this is the same rule applied
        # again after cleaning, for an instruction that was nothing but control characters.
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_request",
            "An edit needs the reviewer's instruction in `edits`",
        )
    return body.model_copy(update={"edits": edits, "note": note})


def _clean_reviewer_text(raw: str | None, field: str) -> str | None:
    """One reviewer field, cleaned and capped. Nothing left after cleaning is `None`.

    An empty result becomes `None` rather than `""` because the two mean the same thing to
    every reader downstream, and one spelling of "the reviewer said nothing" is easier to
    assert than two.
    """
    if raw is None:
        return None
    cleaned = _printable(raw)
    if len(cleaned) > MAX_REVIEWER_TEXT_CHARS:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_request",
            f"`{field}` is longer than {MAX_REVIEWER_TEXT_CHARS} characters",
        )
    return cleaned or None


def _idempotency_key(user_id: str, question: str) -> str:
    """`sha256(user_id + question + date)` (ARCHITECTURE.md §9).

    The date is deliberate: the same question tomorrow is a new job, because competitive
    research goes stale and re-asking is how it is refreshed.
    """
    today = datetime.now(UTC).date().isoformat()
    return hashlib.sha256(f"{user_id}{question}{today}".encode()).hexdigest()


def _job_or_404(engine: Engine, job_id: str) -> sa.Row[Any]:
    job = queries.read_job(engine, job_id)
    if job is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "job_not_found", "No such job", job_id=job_id)
    return job


def _live_state(checkpoints: BaseCheckpointSaver[Any], job_id: str) -> ResearchState | None:
    """The job's durable state, or None if it has never run. No graph, no topology, no LLM.

    ADR 0012 decision 3. `get_tuple` loads a checkpoint the worker wrote, and reading durable
    state is not executing a graph - which is why this route layer needs neither the agent stack
    nor an LLM credential to serve the gate view.
    """
    stored = checkpoints.get_tuple(run_config(job_id))
    if stored is None:
        return None
    values = stored.checkpoint["channel_values"]
    return cast(ResearchState, values) if values else None


def _gate_visit(checkpoints: BaseCheckpointSaver[Any], job_id: str) -> int:
    """Which gate visit a decision answers: the job's live call count (ADR 0007).

    One read serves two purposes, which is why it happens for all three decisions rather than
    only for `edit`. It is the visit key - identical across the gate node's replay, and
    strictly greater at the next visit, because no edge reaches `human_gate` without spending
    a call - and it is the number `refuse_edit()` needs, which must be the checkpoint's rather
    than `jobs.llm_calls_used` (ADR 0006 decision 7).

    A job that has never run answers `0`. No gate visit can carry that key - reaching the gate
    costs at least a Planner call - so it matches nothing, and the status check refuses the
    request a moment later anyway.
    """
    live = _live_state(checkpoints, job_id)
    return 0 if live is None else live["llm_calls_used"]


def _phase(job: sa.Row[Any]) -> str:
    """A coarse progress label, not a stream (guidelines §12).

    **Derived from `jobs.status`, not from the checkpoint** (ADR 0012 decision 2). That is sound
    because ADR 0007 invariant 4 is an *if and only if*: the row says `awaiting_approval`
    exactly when the checkpoint holds a pending interrupt at the gate, and it is written by the
    process that holds the checkpoint. Reading the projection **removes** a duplicated
    derivation rather than adding one - and it makes `GET /jobs/{id}` a single-row read.

    `awaiting_approval` renders as its node name because that is the one node a caller can act
    on, which keeps a node name in the vocabulary guidelines §12 documents.

    The trade is admitted rather than hidden: the API can no longer say *which* node a running
    job is in. That was never actionable - the three states a caller can act on are "not
    started", "waiting for me" and "ended" - and per-node progress belongs to Phase 4's trace.
    """
    return "human_gate" if job.status == "awaiting_approval" else cast(str, job.status)


def _database_reachable(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception:  # noqa: BLE001 - the reason goes to the log, never to the caller
        logger.exception("health check could not reach the database")
        return False
    return True


def _checkpoints_readable(checkpoints: BaseCheckpointSaver[Any]) -> bool:
    """Whether the checkpoint store can be read, for `/health` and for nothing else.

    **One read, of a thread id no job can have, and never a write.** `get_tuple` on an unknown
    thread answers `None` when the store is there and raises when it is not, which is the
    whole question - and it asks it through the saver interface, so no table name, no SQL
    dialect and no DDL enters this layer. ADR 0012 is untouched: the API reads what the worker
    created and creates nothing.

    A probe id rather than a real job id, because a health check must not depend on a job
    existing, and `PROBE_THREAD_ID` cannot collide with one - job ids are UUIDs.
    """
    try:
        checkpoints.get_tuple(run_config(PROBE_THREAD_ID))
    except Exception:  # noqa: BLE001 - the reason goes to the log, never to the caller
        logger.exception("health check could not read the checkpoint store")
        return False
    return True


def _redis_reachable(redis: RedisProbe | None) -> bool:
    """Whether Redis answers, for `/health` and for nothing else.

    `None` means this process was built without one, which is what every API test that is not
    about health does - and it reports `True`, because a check that was never configured is
    not a failing dependency. The production wiring in `app.py` always passes one.

    The probe is a Protocol rather than a `Redis`, so the route layer keeps its ADR 0012
    property of importing nothing it does not need: `redis` is a worker dependency, and the
    API borrows one method of it.
    """
    if redis is None:
        return True
    try:
        return redis.reachable()
    except Exception:  # noqa: BLE001 - the reason goes to the log, never to the caller
        logger.exception("health check could not reach redis")
        return False
