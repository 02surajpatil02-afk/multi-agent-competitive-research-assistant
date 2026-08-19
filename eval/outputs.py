"""
WHY THIS FILE EXISTS
    What an evaluator is handed: one finished research job, flattened into the few things a
    metric can actually read. It is the seam that makes evaluation cheap - **nothing here runs
    the graph, calls a model, or fetches a page** (ADR 0017 decision 1). A job takes minutes
    and real LLM calls; re-running one to score it would make the eval set too expensive to
    run on every prompt change, which is the same as not having one.

    Two loaders, because there are two places a finished job survives:

      * `load_output_file()` - a committed JSON fixture. Offline, deterministic, and what the
        DEV benchmark and the whole test suite run on.
      * `load_output_from_database()` - a real job, read back through `database/queries.py`'s
        existing `read_*` statements. It adds no table, no column and no write; it is the same
        rows `GET /jobs/{id}` already serves, projected differently.

    Three things about the shape are decisions rather than plumbing.

    **`ClaimVerdict` is a projection of `Verdict`, not `Verdict` itself.** `Verdict` refuses
    `supported=true` without a verbatim quote, and it is right to - that rule is the
    Fact-Checker's whole contract. But `claims` stores `supported` and `verdict_note` and
    **not** the quote, so a database-loaded output could never build one. Requiring a field the
    durable store does not keep would mean the database loader could only ever load unsupported
    claims, which is exactly backwards.

    **A report that does not validate is recorded, not raised.** `schema_errors` carries it and
    `structured_output_validity` scores it. A loader that raised would turn "the system emitted
    a malformed report" - a real and interesting failure - into a crashed evaluation run.

    **`RunMetadata` is the canonical evaluation metadata contract, and it is short on purpose.**
    Every field is one this repository can populate from something it already writes. What is
    deliberately absent is listed in docs/evaluation.md: per-agent and per-node identity (those
    live on LangSmith spans, not on a job), a provider name (the endpoint is a URL and nothing
    labels it), and any user, account or team identity, which this application does not own.

WHO CALLS IT
    eval/metrics.py reads a `ResearchOutput`, eval/run.py builds one per case, and
    tests/test_eval_outputs.py holds both loaders.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.engine import Engine, Row

from database import queries
from schemas import Finding, Report

OutputSource = Literal["fixture", "database"]
"""Where an output came from. Carried into the report because a metric's meaning does not
change between the two but its *coverage* does: a database-loaded output has no verdict quotes
and no subtopic ids that never produced a finding, and a reader has to know which they are
looking at."""


class OutputError(RuntimeError):
    """An output that could not be loaded at all - no file, unreadable JSON, no such job.

    Distinct from a report that failed validation, which is a finding rather than an error: one
    means there is nothing to evaluate, the other means the thing to evaluate is broken and
    that is the result.
    """


@dataclass(frozen=True)
class ClaimVerdict:
    """The Fact-Checker's answer for one claim, as the durable stores actually keep it.

    `supported=None` means the claim was never checked - a real state, because a claim exists
    from the moment the Synthesizer writes it and the check is a later pass (database/schema.py).
    Collapsing "unchecked" into "unsupported" would make an incomplete job look like a wrong one.
    """

    claim_id: str
    supported: bool | None
    note: str | None = None
    quote: str | None = None
    """Present only from a fixture. `claims` does not store it (see the module docstring)."""


@dataclass(frozen=True)
class RunMetadata:
    """The canonical evaluation metadata contract: what a run can say about itself.

    `job_id` and `thread_id` are the same string by construction - `graph.state.run_config()`
    sets `thread_id = job_id` - and both are carried because they are the join keys into two
    different systems: the database row, and the LangSmith trace. Naming only one would make
    the other look unavailable.
    """

    job_id: str | None = None
    thread_id: str | None = None
    model: str | None = None
    status: str | None = None
    failure_reason: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    latency_seconds: float | None = None
    llm_calls_used: int | None = None
    revision_count: int | None = None
    quality_flag: str | None = None
    source: OutputSource = "fixture"

    def to_json(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "thread_id": self.thread_id,
            "model": self.model,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "latency_seconds": self.latency_seconds,
            "llm_calls_used": self.llm_calls_used,
            "revision_count": self.revision_count,
            "quality_flag": self.quality_flag,
            "source": self.source,
        }


@dataclass(frozen=True)
class ResearchOutput:
    """One finished job, as an evaluator sees it.

    `report is None` covers two different situations and the metrics treat them the same way -
    every report-dependent metric reports "not applicable" rather than zero. A job that failed
    before writing a report and a report that failed to validate both mean the same thing to a
    citation metric: there is nothing to count. What tells them apart is `status` and
    `schema_errors`, which `terminal_success` and `structured_output_validity` read.
    """

    question: str
    status: str
    report: Report | None
    findings: tuple[Finding, ...] = ()
    verdicts: tuple[ClaimVerdict, ...] = ()
    planned_subtopics: tuple[str, ...] = ()
    subtopic_status: Mapping[str, str] = field(default_factory=dict)
    metadata: RunMetadata = field(default_factory=RunMetadata)
    schema_errors: tuple[str, ...] = ()
    """Why the stored report did not validate against `schemas.Report`, if it did not. Empty is
    the healthy case, including for a job that never produced a report at all."""


# --- The fixture file ---------------------------------------------------------------


class _FileVerdict(BaseModel):
    model_config = {"extra": "forbid"}

    claim_id: str
    supported: bool | None = None
    note: str | None = None
    quote: str | None = None


class _OutputFile(BaseModel):
    """The committed fixture format. Strict about its own shape, lenient about the report.

    `report` is `dict` rather than `Report` on purpose: `ResearchOutput` records a report that
    does not validate instead of refusing to load, and a typed field here would take that
    decision away from the loader.
    """

    model_config = {"extra": "forbid"}

    question: str
    status: str
    failure_reason: str | None = None
    job_id: str | None = None
    model: str | None = None
    planned_subtopics: list[str] = Field(default_factory=list)
    subtopic_status: dict[str, str] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    report: dict[str, Any] | None = None
    verdicts: list[_FileVerdict] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    latency_seconds: float | None = None
    llm_calls_used: int | None = None
    revision_count: int | None = None
    quality_flag: str | None = None
    notes: str | None = None
    """What this fixture is for, in the file itself. Every known-defect fixture says so here,
    so the file explains its own low scores without a reader having to find the case."""


def load_output_file(path: Path) -> ResearchOutput:
    """Read one committed research output. Raises `OutputError` when there is nothing to read."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise OutputError(f"{path}: could not be read ({error})") from error
    try:
        loaded: Any = json.loads(text)
    except ValueError as error:
        raise OutputError(f"{path}: is not valid JSON ({error})") from error
    try:
        parsed = _OutputFile.model_validate(loaded)
    except ValidationError as error:
        raise OutputError(
            f"{path}: is not a research output ({error.error_count()} problems)"
        ) from error

    report, schema_errors = _validated_report(parsed.report, source=str(path))
    return ResearchOutput(
        question=parsed.question,
        status=parsed.status,
        report=report,
        findings=tuple(parsed.findings),
        verdicts=tuple(
            ClaimVerdict(
                claim_id=verdict.claim_id,
                supported=verdict.supported,
                note=verdict.note,
                quote=verdict.quote,
            )
            for verdict in parsed.verdicts
        ),
        planned_subtopics=tuple(parsed.planned_subtopics),
        subtopic_status=dict(parsed.subtopic_status),
        metadata=RunMetadata(
            job_id=parsed.job_id,
            thread_id=parsed.job_id,
            model=parsed.model,
            status=parsed.status,
            failure_reason=parsed.failure_reason,
            started_at=parsed.started_at,
            completed_at=parsed.completed_at,
            latency_seconds=_latency(
                parsed.latency_seconds, parsed.started_at, parsed.completed_at
            ),
            llm_calls_used=parsed.llm_calls_used,
            revision_count=parsed.revision_count,
            quality_flag=parsed.quality_flag,
            source="fixture",
        ),
        schema_errors=schema_errors,
    )


# --- A real job -----------------------------------------------------------------------


def load_output_from_database(engine: Engine, job_id: str) -> ResearchOutput:
    """One finished job, read back out of the five application tables.

    **Read-only, and it adds nothing.** Four existing `read_*` statements and no new column:
    `jobs` carries the question, the status, the counters and the exported body; `findings`
    carries the evidence; `claims` carries each verdict; and `audit_events` carries the plan and
    what each Researcher visit did, which is where the subtopic coverage comes from.

    Two things a database-loaded output genuinely cannot carry, and both are the store's shape
    rather than a gap here: a verdict's quote (`claims` keeps `supported` and the note), and
    `report_json` before the export gate passes, which is `NULL` for every job that did not
    reach export. A job that failed therefore loads with `report=None`, which is the honest
    answer - the report it had was never made durable.
    """
    job = queries.read_job(engine, job_id)
    if job is None:
        raise OutputError(f"no job {job_id} in this database")

    planned, researched = _subtopics_from_audit(queries.read_audit_events(engine, job_id))
    report, schema_errors = _validated_report(job.report_json, source=f"jobs.report_json/{job_id}")

    return ResearchOutput(
        question=job.question,
        status=job.status,
        report=report,
        findings=tuple(_finding(row) for row in queries.read_findings(engine, job_id)),
        verdicts=tuple(
            ClaimVerdict(claim_id=row.claim_id, supported=row.supported, note=row.verdict_note)
            for row in queries.read_claims(engine, job_id)
        ),
        planned_subtopics=planned,
        subtopic_status=researched,
        metadata=RunMetadata(
            job_id=job_id,
            thread_id=job_id,
            # `jobs` does not record which model ran the job. The model is on the LangSmith
            # `llm` spans (`ls_model_name`), and inventing a column for it here would be new
            # telemetry for a fact another system already holds (guidelines §14).
            model=None,
            status=job.status,
            # ADR 0008: a failed job's reason lives in the `job_finished` audit row, not in a
            # column, so it is read from the trail rather than from `jobs`.
            failure_reason=_failure_reason(queries.read_audit_events(engine, job_id)),
            started_at=job.created_at,
            completed_at=job.completed_at,
            latency_seconds=_latency(None, job.created_at, job.completed_at),
            llm_calls_used=job.llm_calls_used,
            revision_count=job.revision_count,
            quality_flag=job.quality_flag,
            source="database",
        ),
        schema_errors=schema_errors,
    )


def _finding(row: Row[Any]) -> Finding:
    """One `findings` row as the schema the rest of the system uses.

    `subtopic` is the column and `subtopic_id` is the field - database/schema.py records why
    the two names differ - so this is the one place the rename is spelled out.
    """
    return Finding(
        finding_id=row.finding_id,
        subtopic_id=row.subtopic,
        claim=row.claim,
        evidence=row.evidence,
        url=row.url,
        title=row.title,
        retrieved_at=row.retrieved_at,
        content_hash=row.content_hash,
        truncated=bool(row.truncated),
    )


def _subtopics_from_audit(events: Sequence[Row[Any]]) -> tuple[tuple[str, ...], dict[str, str]]:
    """What was planned, and how each subtopic turned out, from the audit trail.

    `subtopic_status` is state and state is not a table (ARCHITECTURE.md §5), so the trail is
    where this lives durably: `plan_produced` carries the planned ids and each
    `subtopic_researched` row carries the status that visit resolved to. Later visits overwrite
    earlier ones, which is what the state field does too.
    """
    planned: tuple[str, ...] = ()
    researched: dict[str, str] = {}
    for event in events:
        detail = event.detail if isinstance(event.detail, dict) else {}
        if event.action == "plan_produced":
            subtopics = detail.get("subtopics")
            if isinstance(subtopics, list):
                planned = tuple(str(item) for item in subtopics)
        elif event.action == "subtopic_researched":
            subtopic, status = detail.get("subtopic"), detail.get("status")
            if isinstance(subtopic, str) and isinstance(status, str):
                researched[subtopic] = status
    return planned, researched


def _failure_reason(events: Sequence[Row[Any]]) -> str | None:
    """The reason on the `job_finished` row, which is where ADR 0009 decision 5 put it."""
    for event in reversed(list(events)):
        if event.action == "job_finished":
            detail = event.detail if isinstance(event.detail, dict) else {}
            reason = detail.get("failure_reason")
            return str(reason) if isinstance(reason, str) else None
    return None


# --- Shared ---------------------------------------------------------------------------


def _validated_report(body: Any, *, source: str) -> tuple[Report | None, tuple[str, ...]]:
    """A stored report body against `schemas.Report`, keeping the failure rather than raising.

    `None` in and `(None, ())` out: a job with no report is not a job with a broken one.
    """
    if body is None:
        return None, ()
    if not isinstance(body, dict):
        return None, (f"{source}: the report body is not a JSON object",)
    try:
        return Report.model_validate(body), ()
    except ValidationError as error:
        return None, tuple(
            f"{'.'.join(str(part) for part in item['loc']) or '<report>'}: {item['msg']}"
            for item in error.errors()
        )


def _latency(
    recorded: float | None, started_at: datetime | None, completed_at: datetime | None
) -> float | None:
    """A recorded wall-clock time, or the one the two timestamps imply, or nothing.

    Derived rather than required, because `jobs` carries the two timestamps and no duration -
    and a job that is still running has a start and no end, which is `None` rather than zero.
    """
    if recorded is not None:
        return recorded
    if started_at is None or completed_at is None:
        return None
    return round((completed_at - started_at).total_seconds(), 3)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
