"""
WHY THIS FILE EXISTS
    The five endpoints in guidelines §12 and ARCHITECTURE.md §10, and nothing else. This is
    the outermost boundary in the system, so it gets the same typed treatment as every
    internal one: a model in, a model out, and one error shape for every failure.

    Five things here are decisions rather than plumbing.

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

    Two Phase-2 shapes worth knowing before reading:

      * `POST /jobs` inserts the job row and returns `202`. **It enqueues nothing**, because
        SQS and the worker are Phase 3 (§21 step 20). The row and its `job_created` audit
        event are real; what is missing is the thing that would pick the job up.
      * The gate decision **resumes the graph in-process**. ARCHITECTURE.md §12 has the
        endpoint enqueue a resume message for the worker instead, and that is what the
        marked line below becomes in Phase 3 - the queue does not exist yet, and a decision
        that recorded itself and then did nothing would be worse than one that runs.

WHO CALLS IT
    app.py mounts the router. Tests drive it through Starlette's TestClient.
"""

from __future__ import annotations

import hashlib
import logging
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from langgraph.types import Command
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from config import Config
from database import queries
from graph.build import TERMINAL_STATUSES, ResearchGraph, refuse_edit, run_config
from graph.state import ResearchState
from routes.auth import Identity, identity_from
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


@dataclass(frozen=True)
class RouteDeps:
    """Everything the routes need, built once at startup and read from `app.state`.

    `graph` is the compiled graph the API resumes at the gate and reads live state from. In
    Phase 3 the resume becomes an enqueue and the worker holds the graph; the read stays,
    because the reviewer payload's call count comes from the checkpoint either way.
    """

    config: Config
    engine: Engine
    graph: ResearchGraph
    keys: dict[str, Identity]


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

    Absent header, wrong scheme and unknown key are one answer on purpose: distinguishing
    them tells an attacker which half of the credential to work on.
    """
    identity = identity_from(request.headers.get("authorization"), _deps(request).keys)
    if identity is None:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED, "unauthenticated", "A valid API key is required"
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
    """Create the job row and accept the question. **Nothing runs it yet** - the queue and
    the worker are Phase 3, so this is where a job starts and waits.

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

    return SubmitResponse(job_id=job_id, status="running")


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
        phase=_phase(deps.graph, job),
        revision_count=job.revision_count,
        quality_flag=job.quality_flag,
        report=job.report_json,
    )


# --- 3. Decide at the human gate ----------------------------------------------------


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

    if job.status in TERMINAL_STATUSES:
        # A finished job has no gate to answer, however this request is phrased. The retry
        # rule below is scoped to a visit that has not finished.
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "job_not_awaiting_approval",
            f"This job is {job.status} and has no gate open",
            job_id=job_id,
        )

    calls_used = _gate_visit(deps.graph, job_id)
    recorded = queries.read_gate_decision(deps.engine, job_id, calls_used=calls_used)

    if recorded is None:
        _accept(deps, job, identity, body, calls_used=calls_used)
    elif recorded != body.decision:
        # A reviewer who changes their mind waits for the job to come back to the gate, which
        # is the only point at which a new decision is a new decision (ADR 0007 invariant 3).
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "gate_already_decided",
            f"This gate visit was already answered with {recorded}",
            job_id=job_id,
        )
    else:
        logger.info("job %s: %s retried by %s", job_id, body.decision, identity.user_id)

    _resume(deps, job_id, body)

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


def _resume(deps: RouteDeps, job_id: str, body: GateDecision) -> None:
    """Run the graph on from the gate, and reconcile the row with the checkpoint either way.

    Phase 3 replaces the invoke with an enqueue and lets the worker resume (ARCHITECTURE.md
    §12). Until the queue exists, the decision runs here rather than going nowhere.

    A retry invokes the same thread - `thread_id = job_id` - so LangGraph replays from the
    last checkpoint and the Planner, the Researcher and any finished Synthesizer pass do not
    run again. The cost of a retry is the single node that was in flight, which re-executes
    and converges because ADR 0005 keys every graph-time write. There is nothing to undo here,
    only something to finish.

    The reconcile is in a `finally` because the failure path is the one that needed it: a
    resume that raised used to leave the row claiming a human still held a job the checkpoint
    had already moved past (ADR 0007). It is not wrapped in its own `try`: if it fails too,
    that is a database the request cannot reach at all, and Python keeps the original
    exception on the chain for the log.
    """
    try:
        deps.graph.invoke(Command(resume=body.model_dump()), run_config(job_id))
    finally:
        _reconcile_status(deps, job_id)


def _reconcile_status(deps: RouteDeps, job_id: str) -> None:
    """`jobs.status`, derived from the checkpoint rather than asserted beside it.

    **The predicate is the pending interrupt, not `next`.** A job that has not yet entered the
    gate also reports `next == ("human_gate",)` while no human is being waited on; only an
    interrupt recorded against that task means the graph has actually stopped for a person.

    A job `finalize` has already ended is left alone - `set_job_status` refuses to touch a
    finished row - so the two transitions the graph owns stay the graph's.
    """
    waiting = bool(deps.graph.get_state(run_config(job_id)).interrupts)
    status: JobStatus = "awaiting_approval" if waiting else "running"
    queries.set_job_status(deps.engine, job_id=job_id, status=status)


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
    """Always `404` in Phase 2.

    The artifact is a presigned S3 URL and S3 arrives in Phase 3; the approved body is read
    from `GET /jobs/{id}` until then, which already carries `report`. No stand-in is built
    (ARCHITECTURE.md §8, §10).
    """
    deps = _deps(request)
    identity = _caller(request)
    _owned(_job_or_404(deps.engine, job_id), identity)

    raise ApiError(
        status.HTTP_404_NOT_FOUND, "not_exported", "No artifact exists for this job", job_id=job_id
    )


# --- 5. Liveness --------------------------------------------------------------------


@router.get("/health")
def health(request: Request) -> JSONResponse:
    """The single unauthenticated route, and minimal by design.

    A status and one boolean per dependency: no secret, no connection string, no hostname,
    no version, no job data, no counts, no error text. If a check needs to explain itself,
    that explanation goes to the logs (guidelines §16).

    **Redis is absent rather than false.** It arrives in Phase 3 and nothing reaches for it
    yet, so reporting it unhealthy would fail every health check and mean an unhealthy task
    is never replaced - the exact failure this route exists to avoid. The key appears when
    the dependency does.
    """
    healthy = _database_reachable(_deps(request).engine)
    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ok" if healthy else "degraded", "checks": {"db": healthy}},
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


def _live_state(graph: ResearchGraph, job_id: str) -> ResearchState | None:
    """The job's state as the checkpoint holds it right now, or None if it has never run."""
    values = graph.get_state(run_config(job_id)).values
    if not values:
        return None
    return cast(ResearchState, values)


def _gate_visit(graph: ResearchGraph, job_id: str) -> int:
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
    live = _live_state(graph, job_id)
    return 0 if live is None else live["llm_calls_used"]


def _phase(graph: ResearchGraph, job: sa.Row[Any]) -> str:
    """A coarse progress label, not a stream (guidelines §12).

    It is read off the checkpoint's next node, which is the one place that knows where a job
    actually is. A job that has ended reports how it ended, and a job that has not started
    reports that it is queued - which in Phase 2 is every job, because nothing dequeues yet.
    """
    if job.status in ("approved", "rejected", "failed"):
        return cast(str, job.status)
    following = graph.get_state(run_config(job.job_id)).next
    return following[0] if following else "queued"


def _database_reachable(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception:  # noqa: BLE001 - the reason goes to the log, never to the caller
        logger.exception("health check could not reach the database")
        return False
    return True
