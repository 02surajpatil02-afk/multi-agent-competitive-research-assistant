"""
WHY THIS FILE EXISTS
    Every statement that reads or writes the five application tables, as plain functions.
    There is no repository class and no session-per-request machinery: a function that
    takes the engine and does one thing is the smallest thing that can own a transaction,
    and it is what the graph nodes, the API, and the tests all call.

    **Each write function is one transaction, and the transaction is the unit of work.**
    `record_research` writes the findings and the audit row together or writes neither;
    `record_claims` replaces the claim set and its `claim_sources` together. That is why
    the functions take an `Engine` rather than a `Connection` - a caller cannot accidentally
    split one of these in half, and there is no ambient session to forget to commit.

    **A failed write fails loudly.** No retry, no swallowed exception: guidelines §17 gives
    the database row 0 retries and ARCHITECTURE.md §15 says a PostgreSQL failure fails the
    node and takes the task out of service. So the SQLAlchemy error propagates out of the
    node that was writing, which is the loud give-up those two sections describe. Turning it
    into `status=failed` here would invent a failure vocabulary the specification does not
    have, and swallowing it would leave a job whose audit trail silently disagrees with what
    it did.

    **Writes are keyed so that a replayed node converges rather than duplicating.** The
    graph writes a row inside the node and LangGraph writes the checkpoint after the node
    returns, so a crash in between leaves the database one node ahead of the checkpoint; on
    redelivery that node runs again (ARCHITECTURE.md §11). `findings` is therefore written
    by primary key - insert what is new, refresh what is already there - and `claims` and
    `claim_sources` are replaced wholesale for the current draft, which is the same
    operation whether it runs once or three times. `audit_events` is the deliberate
    exception: it is append-only, and a node that genuinely ran twice truthfully has two
    rows.

    The read-then-write in `_write_findings` is safe only because one job has one execution writer.
    ADR 0016 enforces that precondition with a session-scoped PostgreSQL advisory lock spanning the
    complete worker invocation. SQS visibility renewal normally prevents redelivery; the database
    lock remains the fence when queue ownership becomes unsafe while a node is still finishing.

WHO CALLS IT
    The graph nodes in graph/build.py, as the job runs. The FastAPI routes from step 18,
    for job creation and the reviewer's reads. The tests.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, Row

from database.schema import (
    SYSTEM_ACTOR,
    AuditAction,
    audit_events,
    claim_sources,
    claims,
    findings,
    jobs,
)
from schemas import Finding, JobStatus, QualityFlag, ReflectionRoute, Report, ResearchPlan, Verdict

logger = logging.getLogger(__name__)

QUERY_TIMEOUT_MS = 5_000
"""guidelines §17's database row: 5s, 0 retries, fail loudly. Enforced by the server, so a
query that hangs is cut off rather than holding a node open until the whole-job bound."""


def create_database_engine(url: str) -> Engine:
    """One engine for the process, with the two dialect details that are load-bearing.

    PostgreSQL gets §17's statement timeout. SQLAlchemy retries nothing by default, which
    is the same row's "0 retries", so there is nothing to switch off.

    SQLite gets `PRAGMA foreign_keys=ON`. It is what the offline test suite runs against, and
    SQLite ignores foreign keys unless asked. A test suite that cannot see a broken foreign key
    would prove nothing about the audit trail's shape. The `postgres`-marked suite runs these
    same statements through the PostgreSQL branch above, so the timeout is exercised too.
    """
    if url.startswith("sqlite"):
        engine = sa.create_engine(url)
        sa.event.listen(engine, "connect", _enable_sqlite_foreign_keys)
        return engine
    return sa.create_engine(
        sqlalchemy_url(url), connect_args={"options": f"-c statement_timeout={QUERY_TIMEOUT_MS}"}
    )


def sqlalchemy_url(url: str) -> str:
    """`DATABASE_URL`, with the driver SQLAlchemy needs named in it.

    `DATABASE_URL` is the ordinary libpq string - `postgresql://user:pw@host/db` - because
    that is what RDS hands you, what `psql` takes, and what the checkpointer's own psycopg
    pool takes. SQLAlchemy reads the scheme as a driver name, and bare `postgresql://` means
    **psycopg2**, which this project does not install: without this the first query fails
    with `No module named 'psycopg2'`, and it fails at deploy time rather than in a test.

    A URL that already names a driver is left alone, so `postgresql+psycopg://` also works.
    """
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _enable_sqlite_foreign_keys(connection: Any, _record: Any) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# --- Job creation -------------------------------------------------------------------


def create_job(
    engine: Engine,
    *,
    job_id: str,
    user_id: str,
    question: str,
    idempotency_key: str,
    actor: str,
) -> None:
    """Insert the job row and its `job_created` audit event, in one transaction.

    `actor` is the authenticated submitter, not `system`: creating a job is something a
    caller did, and guidelines §9 reserves `system` for transitions the graph made on its
    own.

    A duplicate `idempotency_key` raises `IntegrityError`. That is the design: the unique
    constraint is the arbiter, and `POST /jobs` turns the violation into a `409` carrying
    the existing `job_id` rather than checking first and racing (ARCHITECTURE.md §9).

    **The row starts `queued`, and the worker is the only thing that moves it to `running`**
    (ADR 0010 decisions 1 and 2). Before the queue existed this wrote `running` for a job
    nothing was running, which made "how many jobs are actually executing?" unanswerable -
    the first question anyone asks during an incident.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.insert(jobs).values(
                job_id=job_id,
                user_id=user_id,
                question=question,
                idempotency_key=idempotency_key,
                status="queued",
            )
        )
        _audit(conn, job_id=job_id, actor=actor, action="job_created", detail={})


# --- What the graph records as it runs ----------------------------------------------


def record_plan(engine: Engine, *, job_id: str, plan: ResearchPlan) -> None:
    """The Planner produced a plan. The plan itself lives in state and in the checkpoint;
    what is durable here is that it happened and what it covered (ARCHITECTURE.md §9)."""
    with engine.begin() as conn:
        _audit(
            conn,
            job_id=job_id,
            actor=SYSTEM_ACTOR,
            action="plan_produced",
            detail={"subtopics": [subtopic.id for subtopic in plan.subtopics]},
        )


def record_research(
    engine: Engine,
    *,
    job_id: str,
    new_findings: Sequence[Finding],
    subtopic: str | None,
    status: str | None,
) -> None:
    """One Researcher visit: its findings and the audit row, together or not at all.

    `subtopic` and `status` are `None` when the visit failed before it resolved a subtopic.
    The findings it had already paid for are still written, because that is what the
    `operator.add` reducer does with them in state.
    """
    with engine.begin() as conn:
        _write_findings(conn, job_id=job_id, new_findings=new_findings)
        _audit(
            conn,
            job_id=job_id,
            actor=SYSTEM_ACTOR,
            action="subtopic_researched",
            detail={"subtopic": subtopic, "status": status, "findings": len(new_findings)},
        )


def record_claims(engine: Engine, *, job_id: str, report: Report) -> None:
    """Mirror the current draft's claims and their sources into the database.

    One draft exists at a time (schemas.py), so the claim set is replaced rather than
    appended to: a revision writes a whole new draft with new claim ids, and leaving the
    previous pass's rows behind would mean a query for "this job's claims" answering with
    claims that are not in the report. `claim_sources` is deleted explicitly rather than
    left to the cascade, so the behaviour does not depend on which database is underneath.

    `supported` and `verdict_note` are not written here. They are the Fact-Checker's, and
    a fresh draft has not been checked yet.
    """
    claim_rows = [
        {
            "claim_id": claim.claim_id,
            "job_id": job_id,
            "section": claim.section_id,
            "text": claim.text,
        }
        for claim in report.claims
    ]
    source_rows = [
        {"claim_id": claim.claim_id, "job_id": job_id, "finding_id": finding_id}
        for claim in report.claims
        for finding_id in claim.finding_ids
    ]

    with engine.begin() as conn:
        conn.execute(sa.delete(claim_sources).where(claim_sources.c.job_id == job_id))
        conn.execute(sa.delete(claims).where(claims.c.job_id == job_id))
        if claim_rows:
            conn.execute(sa.insert(claims), claim_rows)
        if source_rows:
            conn.execute(sa.insert(claim_sources), source_rows)


def record_verdicts(engine: Engine, *, job_id: str, verdicts: Sequence[Verdict]) -> None:
    """Write each claim's current verdict onto its row.

    `claims.supported` is the current value, not a history - the history is the verdicts in
    state, one set per pass. A verdict naming a claim this job does not have is logged
    rather than raised: it means the draft in state and the rows disagree, which is worth
    seeing, and failing the job over a mirror mismatch would discard a finished report.
    """
    if not verdicts:
        return

    with engine.begin() as conn:
        unknown = [
            verdict.claim_id
            for verdict in verdicts
            if conn.execute(
                sa.update(claims)
                .where(claims.c.job_id == job_id, claims.c.claim_id == verdict.claim_id)
                .values(supported=verdict.supported, verdict_note=verdict.note)
            ).rowcount
            == 0
        ]

    if unknown:
        logger.warning("job %s: verdicts for claims not in the database: %s", job_id, unknown)


def record_revision(
    engine: Engine,
    *,
    job_id: str,
    revision: int,
    route: ReflectionRoute,
    failed_dimensions: Sequence[str],
    weighted_score: float,
) -> None:
    """One automatic improvement cycle started: which one, why, and where it went."""
    with engine.begin() as conn:
        _audit(
            conn,
            job_id=job_id,
            actor=SYSTEM_ACTOR,
            action="revision",
            detail={
                "revision": revision,
                "route": route,
                "failed_dimensions": list(failed_dimensions),
                "weighted_score": weighted_score,
            },
        )


def record_reflection_failed(engine: Engine, *, job_id: str) -> None:
    """The scoring call failed and the report was kept (ARCHITECTURE.md §15).

    `unscored` is not a pass, and this row is what says so afterwards: the reviewer was the
    only quality judgement on this job.
    """
    with engine.begin() as conn:
        _audit(
            conn,
            job_id=job_id,
            actor=SYSTEM_ACTOR,
            action="reflection_failed",
            detail={"quality_flag": "unscored"},
        )


def record_gate_opened(
    engine: Engine, *, job_id: str, calls_used: int, summary: Mapping[str, Any]
) -> None:
    """The job reached the human gate: the row that says so, and `awaiting_approval`.

    Both in one transaction, because a job whose status says it is waiting and whose trail
    does not say why is a job nobody can reconstruct (ARCHITECTURE.md §12).

    **Written once per gate visit, and that needs a key.** LangGraph re-runs an interrupted
    node from the top when the job resumes, so the gate node's pre-interrupt code executes
    twice per visit - measured, not assumed. `calls_used` is the state's `llm_calls_used` at
    the pause: identical across those two executions, and strictly greater at the next gate
    visit, because reaching the gate again costs a Synthesizer, a Fact-Checker and a
    reflection pass. So it identifies the visit, and a second insert for the same visit is
    skipped. This is ADR 0005's "keyed so a replayed node converges", applied to the one
    audit row a replay would otherwise duplicate.

    **The status write is inside that guard, and that placement is the point** (ADR 0007
    invariant 4). It looks like a write that needs no care - setting the same value twice is
    the same value - and that reasoning is true on the way *in* and false on the way *out*:
    the replay happens milliseconds after `claim_gate` moved the row to `running`, so a write
    outside the guard hands an answered gate back to the next reviewer who asks. The status
    is `awaiting_approval` only while a visit is genuinely open, which is the same condition
    the audit row is already written under.
    """
    with engine.begin() as conn:
        already = conn.execute(
            sa.select(sa.func.count())
            .select_from(audit_events)
            .where(
                audit_events.c.job_id == job_id,
                audit_events.c.action == "gate_opened",
                audit_events.c.detail["calls_used"].as_integer() == calls_used,
            )
        ).scalar_one()

        if not already:
            conn.execute(
                sa.update(jobs).where(jobs.c.job_id == job_id).values(status="awaiting_approval")
            )
            _audit(
                conn,
                job_id=job_id,
                actor=SYSTEM_ACTOR,
                action="gate_opened",
                detail={"calls_used": calls_used, **summary},
            )


def claim_gate(engine: Engine, job_id: str) -> bool:
    """Take the job out of `awaiting_approval`, and say whether this caller is the one who did.

    Two reviewers deciding at once is the case this exists for: the conditional update has
    exactly one winner, so the loser is told the gate was already answered instead of both
    resuming the same thread and both spending its budget. The database does the deciding,
    for the same reason `idempotency_key` does it at submission - an application-level
    "check then act" races between two API tasks (ARCHITECTURE.md §9).

    `running` is what it moves to, because that is what the job is about to be: the graph
    resumes immediately afterwards and writes the terminal status itself.
    """
    with engine.begin() as conn:
        claimed = conn.execute(
            sa.update(jobs)
            .where(jobs.c.job_id == job_id, jobs.c.status == "awaiting_approval")
            .values(status="running")
        )
    return claimed.rowcount == 1


def set_job_status(engine: Engine, *, job_id: str, status: JobStatus) -> None:
    """Move a job that has not finished yet to `status`, and leave a finished one alone.

    This is where `POST /jobs/{id}/approve` reconciles the row with the checkpoint after a
    resume, whether the resume returned or raised (ADR 0007 invariant 4). Before that record,
    a resume that died left the row saying a human was still holding a job the checkpoint had
    already moved past.

    **`completed_at IS NULL` is the guard, rather than a list of terminal statuses.**
    `finish_job` is the only writer of that column, so "this job has ended" is a fact the row
    already carries; repeating `approved | rejected | failed` here would be a second copy of
    ARCHITECTURE.md §3's vocabulary to keep in step. `finalize` stays authoritative about how
    a job ended, and nothing here can talk over it.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.update(jobs)
            .where(jobs.c.job_id == job_id, jobs.c.completed_at.is_(None))
            .values(status=status)
        )


def record_reviewer_decision(
    engine: Engine,
    *,
    job_id: str,
    actor: str,
    decision: str,
    note: str | None,
    edits: str | None,
    calls_used: int,
) -> None:
    """What the reviewer decided, who they were, and which gate visit they answered.

    `actor` is the authenticated identity from the API key, never a name in the request body:
    "the report was approved" is not worth auditing, and "this person approved it" is
    (guidelines §9, §16). The edit text lives here too, which is what lets
    `reviewer_edit_text` be cleared from state at the next gate decision without losing it.

    **One row per gate visit** (ADR 0007 invariant 1). `calls_used` is the same key
    `record_gate_opened` writes above - the job's `llm_calls_used` at the pause - so "which
    opening does this decision answer?" is a join rather than an assumption, and the
    query-then-insert guard is what makes a retried decision write nothing.

    That guard is not tidiness. A resume can die after this row is written, and the reviewer's
    fix is to send the same request again; `count_reviewer_edits` counts rows, so without the
    key an infrastructure failure would spend one of the three edits ADR 0006 allows on an
    edit that never happened.
    """
    with engine.begin() as conn:
        if _detail_for_visit(conn, job_id=job_id, calls_used=calls_used) is not None:
            return
        _audit(
            conn,
            job_id=job_id,
            actor=actor,
            action="reviewer_decision",
            detail={"decision": decision, "note": note, "edits": edits, "calls_used": calls_used},
        )


def read_gate_decision(engine: Engine, job_id: str, *, calls_used: int) -> Mapping[str, Any] | None:
    """The whole decision on record for one gate visit - `{decision, note, edits, calls_used}` -
    or None if nobody has answered it.

    **One query, two readers** (ADR 0011 decision 2). `POST /jobs/{id}/approve` compares
    `["decision"]` for ADR 0007's four cases - the same decision again is a retry, a different
    one is refused, and only an unanswered visit claims the gate. The **worker** rebuilds a
    `GateDecision` from the same mapping to resume the graph with, which is what keeps the
    reviewer's text out of the queue message entirely.

    It returns the detail rather than the decision string because a second near-identical query
    beside this one would be two statements answering one question over one row, and they would
    drift - and then ADR 0007's four-case table would depend on which one a reader called.
    """
    with engine.connect() as conn:
        return _detail_for_visit(conn, job_id=job_id, calls_used=calls_used)


def count_reviewer_edits(engine: Engine, job_id: str) -> int:
    """How many times this job has been sent back for an edit.

    Counted from the audit trail rather than from a column, because the trail already has to
    record every reviewer decision and a second home for the same fact would be a second
    thing to keep true (ADR 0006).

    **Returns 0 until step 18 exists.** `reviewer_decision` rows carry the reviewer's
    identity, so they are written by the authenticated endpoint, not by the graph. This is
    the query that endpoint calls before it decides whether an edit may start.
    """
    with engine.connect() as conn:
        return conn.execute(
            sa.select(sa.func.count())
            .select_from(audit_events)
            .where(
                audit_events.c.job_id == job_id,
                audit_events.c.action == "reviewer_decision",
                audit_events.c.detail["decision"].as_string() == "edit",
            )
        ).scalar_one()


def record_export_attempt(engine: Engine, *, job_id: str, actor: str, claims_checked: int) -> None:
    """The export gate is about to run. Recorded before the check, so an attempt that
    fails the invariant is still visible as an attempt (ARCHITECTURE.md §9).

    `actor` is `SYSTEM_ACTOR` from the export node and the operator's identity from
    `scripts/reexport_job.py`, which writes this same pair of rows when it recovers an
    artifact (ADR 0009 decision 4). A recovered export is then exactly as auditable as an
    original one, and the trail says which of the two it was without a second action name.
    """
    with engine.begin() as conn:
        _audit(
            conn,
            job_id=job_id,
            actor=actor,
            action="export_attempted",
            detail={"claims": claims_checked},
        )


def record_export_result(
    engine: Engine, *, job_id: str, report: Report | None, uncited: Sequence[str]
) -> None:
    """What the export gate decided, and - when it passed - the report body itself.

    **This is the point at which the report is durable, and it is deliberately not the point
    at which the job counts as exported** (ADR 0009 decision 1). Phase 2 wrote `report_json`
    and stamped `exported_at` here together, which was correct while there was no artifact and
    "exported" could only mean "the body was stored". Once S3 exists that reading is wrong: a
    job whose `PutObject` failed would claim an export date for an object nobody can fetch.
    `exported_at` is stamped by `record_artifact_written` below, after the write succeeds, and
    the split is what makes *"which approved reports have no artifact?"* the SQL predicate
    `status = 'failed' AND report_json IS NOT NULL AND exported_at IS NULL`.

    A blocked export writes no report and lists the uncited claims. Nothing is retried -
    an uncited claim is a defect in the report, not an infrastructure error.
    """
    with engine.begin() as conn:
        if report is None:
            _audit(
                conn,
                job_id=job_id,
                actor=SYSTEM_ACTOR,
                action="export_result",
                detail={"result": "blocked", "uncited_claims": list(uncited)},
            )
            return

        conn.execute(
            sa.update(jobs)
            .where(jobs.c.job_id == job_id)
            .values(report_json=report.model_dump(mode="json"))
        )
        _audit(
            conn,
            job_id=job_id,
            actor=SYSTEM_ACTOR,
            action="export_result",
            detail={"result": "exported", "claims": len(report.claims)},
        )


def record_artifact_written(engine: Engine, *, job_id: str, actor: str, key: str) -> None:
    """The artifact exists. Stamp `exported_at` and say so, in one transaction.

    **`exported_at IS NOT NULL` means the object was written**, and nothing else may set it
    (ADR 0009 decision 1). That is what `GET /jobs/{id}/report` keys on, so the column and the
    bucket must not be able to disagree - hence one transaction, and hence a stamp that happens
    strictly after the `PutObject` returned rather than beside it.

    Written by the export node under `system`, and by the re-export script under the operator
    who ran it.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.update(jobs).where(jobs.c.job_id == job_id).values(exported_at=sa.func.now())
        )
        _audit(
            conn,
            job_id=job_id,
            actor=actor,
            action="export_result",
            detail={"result": "artifact_written", "key": key},
        )


def record_artifact_failed(engine: Engine, *, job_id: str, actor: str, key: str) -> None:
    """The bounded write was exhausted. **`exported_at` is not touched** (ADR 0009 decision 1).

    The reason the job ends with is `export_write_failed`, and that lives on the `job_finished`
    row `finish_job` writes below. This row is what the *re-export script* needs: it never
    finalises anything, so a recovery attempt that also failed would otherwise leave no trace
    at all beyond a log line on somebody's terminal.
    """
    with engine.begin() as conn:
        _audit(
            conn,
            job_id=job_id,
            actor=actor,
            action="export_result",
            detail={"result": "artifact_write_failed", "key": key},
        )


def finish_job(
    engine: Engine,
    *,
    job_id: str,
    status: JobStatus,
    failure_reason: str | None,
    quality_flag: QualityFlag | None,
    revision_count: int,
    llm_calls_used: int,
) -> None:
    """The terminal row, and the one `job_finished` audit event that explains it.

    `completed_at` is set here and nowhere else (ARCHITECTURE.md §9). The two counters are
    persisted so budget behaviour stays auditable after the job ends, which is the only place
    they can be read once state is gone.

    **`job_finished` is ADR 0009 decision 5, and it is why the row is written here rather than
    beside this call.** ADR 0008 chose the shape - `{status, failure_reason}` in the trail, not
    a `jobs.failure_reason` column - and left the reason living only in the checkpoint, with
    the accepted risk stated: a pruned checkpoint leaves a `jobs` row saying `failed` with
    nothing left to say why. Recovering a failed export is the first operation that has to read
    that reason from a durable place, so the row stops being optional in this phase. Writing it
    inside this transaction is what makes the status and its explanation impossible to
    disagree.

    **One row per job, because a job finishes once.** `finish_job` can genuinely run twice - a
    replayed `finalize` node, or the worker finalising a job whose invocation raised - and the
    guard is the same "keyed so a replayed node converges" rule `record_gate_opened` follows,
    with the job itself as the key. A second finalisation that disagrees with the first is
    logged rather than recorded: the trail keeps the first answer, and the disagreement is
    visible rather than silently overwritten.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.update(jobs)
            .where(jobs.c.job_id == job_id)
            .values(
                status=status,
                quality_flag=quality_flag,
                revision_count=revision_count,
                llm_calls_used=llm_calls_used,
                completed_at=sa.func.now(),
            )
        )
        _record_job_finished(conn, job_id=job_id, status=status, failure_reason=failure_reason)


# --- Reads --------------------------------------------------------------------------


def read_job(engine: Engine, job_id: str) -> Row[Any] | None:
    with engine.connect() as conn:
        return conn.execute(sa.select(jobs).where(jobs.c.job_id == job_id)).one_or_none()


def read_job_by_idempotency_key(engine: Engine, key: str) -> Row[Any] | None:
    """The job a duplicate submission already created.

    `POST /jobs` reads this after the unique constraint refuses the insert, so the caller
    gets `409` with the `job_id` they already have rather than a second job (ARCHITECTURE.md
    §9).
    """
    with engine.connect() as conn:
        statement = sa.select(jobs).where(jobs.c.idempotency_key == key)
        return conn.execute(statement).one_or_none()


def read_findings(engine: Engine, job_id: str) -> Sequence[Row[Any]]:
    with engine.connect() as conn:
        statement = (
            sa.select(findings).where(findings.c.job_id == job_id).order_by(findings.c.finding_id)
        )
        return conn.execute(statement).all()


def read_claims(engine: Engine, job_id: str) -> Sequence[Row[Any]]:
    with engine.connect() as conn:
        return conn.execute(sa.select(claims).where(claims.c.job_id == job_id)).all()


def read_claim_sources(engine: Engine, job_id: str) -> Sequence[Row[Any]]:
    with engine.connect() as conn:
        return conn.execute(sa.select(claim_sources).where(claim_sources.c.job_id == job_id)).all()


def read_unfinished_jobs(
    engine: Engine, *, statuses: Sequence[JobStatus], job_id: str | None = None
) -> Sequence[Row[Any]]:
    """Every job that has not finished, in one of `statuses`, with the time of its last event.

    **This is candidate selection for the block C reconciler and nothing more**
    ([ADR 0021](../docs/adr/0021-stale-job-reconciliation-and-dlq-recovery.md) decision 2). It
    answers "which rows are worth looking at"; whether any of them may be changed is decided
    afterwards, from the execution fence and from durable state. Age selects; it never
    authorises.

    `last_event_at` is the newest `audit_events.created_at` for the job, as a correlated
    subquery, and it is why no `jobs.updated_at` column was added. The audit trail already has
    to record every node event, so "when did this job last do anything durable" is a fact the
    database holds - and a second column carrying the same fact would be a second thing to keep
    true (ADR 0006 made the same call about `count_reviewer_edits`).

    It is NULL only for a job whose `job_created` row is gone, which cannot happen while the
    trail is append-only; callers fall back to `created_at` anyway, because a caller that
    crashed on a NULL would be refusing to look at the very row most in need of looking at.

    `completed_at IS NULL` is repeated beside the status filter deliberately. `finish_job` is
    the only writer of that column, so it - not the status string - is what "this job has
    ended" means (`set_job_status` says the same thing), and a row whose status was left behind
    by a dead process must not be selectable as a candidate once it has genuinely finished.
    """
    last_event_at = (
        sa.select(sa.func.max(audit_events.c.created_at))
        .where(audit_events.c.job_id == jobs.c.job_id)
        .scalar_subquery()
        .label("last_event_at")
    )
    statement = (
        sa.select(jobs, last_event_at)
        .where(jobs.c.completed_at.is_(None), jobs.c.status.in_(list(statuses)))
        .order_by(jobs.c.created_at)
    )
    if job_id is not None:
        statement = statement.where(jobs.c.job_id == job_id)
    with engine.connect() as conn:
        return conn.execute(statement).all()


def record_reconciliation(
    engine: Engine, *, job_id: str, actor: str, outcome: str, detail: Mapping[str, Any]
) -> None:
    """One `job_reconciled` row per (job, outcome), written by the operator sweep.

    **`actor` is the person who ran the sweep**, for the reason `scripts/reexport_job.py` gives
    about `--actor`: a durable row changed by a machine that cannot say who asked is
    accountability theatre, and `ck_audit_events_actor` refuses `unknown` one layer down.

    **The guard is `record_gate_opened`'s, keyed on `(job_id, outcome)`.** The sweep is meant to
    be safe to run again, and its mutations are self-limiting - a repaired gate projection is
    already `awaiting_approval` the second time, and a finished job is not a candidate at all -
    so a second run normally decides `no_change` and writes nothing. This makes that true even
    when it does not: an outcome already on record is not recorded twice, so re-running the
    sweep cannot inflate the trail (ADR 0021 decision 5).
    """
    with engine.begin() as conn:
        recorded = conn.execute(
            sa.select(audit_events.c.event_id)
            .where(
                audit_events.c.job_id == job_id,
                audit_events.c.action == "job_reconciled",
                audit_events.c.detail["outcome"].as_string() == outcome,
            )
            .limit(1)
        ).scalar_one_or_none()
        if recorded is not None:
            return
        _audit(
            conn,
            job_id=job_id,
            actor=actor,
            action="job_reconciled",
            detail={"outcome": outcome, **detail},
        )


def read_audit_events(engine: Engine, job_id: str) -> Sequence[Row[Any]]:
    """The per-job timeline, in the order the events happened.

    Ordered by `event_id` rather than `created_at`: two events written in the same
    timestamp tick still have an order, and the sequence is the one that knows it.
    """
    with engine.connect() as conn:
        statement = (
            sa.select(audit_events)
            .where(audit_events.c.job_id == job_id)
            .order_by(audit_events.c.event_id)
        )
        return conn.execute(statement).all()


# --- Helpers ------------------------------------------------------------------------


def _audit(
    conn: sa.Connection,
    *,
    job_id: str,
    actor: str,
    action: AuditAction,
    detail: Mapping[str, Any],
) -> None:
    """Append one audit row inside the caller's transaction.

    It takes a `Connection` rather than an `Engine` precisely because it must not be a
    transaction of its own: an audit row that committed while the write it describes rolled
    back would be a record of something that did not happen.
    """
    conn.execute(
        sa.insert(audit_events).values(
            job_id=job_id, actor=actor, action=action, detail=dict(detail)
        )
    )


def _record_job_finished(
    conn: sa.Connection, *, job_id: str, status: JobStatus, failure_reason: str | None
) -> None:
    """The one terminal audit row, inside `finish_job`'s transaction (ADR 0009 decision 5).

    The query-then-insert guard is `record_gate_opened`'s, keyed on the job rather than on a
    gate visit, because "this job has finished" happens once however many times the code that
    records it runs.
    """
    recorded = conn.execute(
        sa.select(audit_events.c.detail)
        .where(audit_events.c.job_id == job_id, audit_events.c.action == "job_finished")
        .order_by(audit_events.c.event_id)
        .limit(1)
    ).scalar_one_or_none()

    if recorded is not None:
        if recorded.get("status") != status or recorded.get("failure_reason") != failure_reason:
            logger.warning(
                "job %s finished again as %s/%s; the trail keeps the first answer %s/%s",
                job_id,
                status,
                failure_reason,
                recorded.get("status"),
                recorded.get("failure_reason"),
            )
        return

    _audit(
        conn,
        job_id=job_id,
        actor=SYSTEM_ACTOR,
        action="job_finished",
        detail={"status": status, "failure_reason": failure_reason},
    )


def _detail_for_visit(conn: sa.Connection, *, job_id: str, calls_used: int) -> Any | None:
    """The `reviewer_decision` detail on record for one gate visit, inside the caller's
    transaction - the whole mapping rather than a projection of it (ADR 0011 decision 2).

    The predicate is ADR 0007's gate-visit key - `(job_id, calls_used)` - and it is the same
    one `record_gate_opened` finds its own row with, deliberately: one key, two rows, and a
    join between them.
    """
    return conn.execute(
        sa.select(audit_events.c.detail)
        .where(
            audit_events.c.job_id == job_id,
            audit_events.c.action == "reviewer_decision",
            audit_events.c.detail["calls_used"].as_integer() == calls_used,
        )
        .order_by(audit_events.c.event_id)
        .limit(1)
    ).scalar_one_or_none()


def _write_findings(conn: sa.Connection, *, job_id: str, new_findings: Sequence[Finding]) -> None:
    """Insert the findings this job does not have, and refresh the ones it does.

    The refresh is what makes a replayed Researcher node converge. Finding ids are numbered
    from what state already holds (ADR 0003), so a node that ran, wrote rows, and crashed
    before its checkpoint will mint the same ids again on redelivery - and the database has
    to end up agreeing with the checkpoint rather than keeping an orphan under a live id.
    """
    if not new_findings:
        return

    existing = set(
        conn.scalars(sa.select(findings.c.finding_id).where(findings.c.job_id == job_id))
    )
    rows = [_finding_row(job_id, finding) for finding in new_findings]

    fresh = [row for row in rows if row["finding_id"] not in existing]
    if fresh:
        conn.execute(sa.insert(findings), fresh)

    for row in (row for row in rows if row["finding_id"] in existing):
        conn.execute(
            sa.update(findings)
            .where(
                findings.c.job_id == job_id,
                findings.c.finding_id == row["finding_id"],
            )
            .values({column: row[column] for column in row if column not in _FINDING_KEY})
        )


_FINDING_KEY = ("job_id", "finding_id")


def _finding_row(job_id: str, finding: Finding) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "finding_id": finding.finding_id,
        "subtopic": finding.subtopic_id,
        "claim": finding.claim,
        "evidence": finding.evidence,
        "url": str(finding.url),
        "title": finding.title,
        "retrieved_at": finding.retrieved_at,
        "content_hash": finding.content_hash,
        "truncated": finding.truncated,
    }
