"""
WHY THIS FILE EXISTS
    The two loaders, and the one property that matters about both: **a broken report is a
    result, not an exception.** `schema_errors` is what `structured_output_validity` scores, so
    a loader that raised on a malformed body would turn the most interesting failure the system
    can produce - "it emitted a report that is not a report" - into a crashed evaluation run.

    The database loader gets the same treatment every other database test in this repository
    gets: a real migration on a real SQLite file through `dbharness`, and the real statements
    from `database/queries.py`. It reads nothing that is not already written by a job, which is
    the claim worth pinning - evaluation adds no column and no write.

WHO CALLS IT
    pytest. No service, no network, no provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from dbharness import a_finding, a_report, migrated_engine, new_job_id
from sqlalchemy.engine import Engine

from database import queries
from eval.outputs import (
    OutputError,
    load_output_file,
    load_output_from_database,
)
from schemas import ResearchPlan, Subtopic, Verdict

_REPORT: dict[str, Any] = {
    "sections": [{"id": "sec1", "heading": "Cloud", "body": "Both firms grew."}],
    "claims": [
        {"claim_id": "c1", "section_id": "sec1", "text": "TCS grew.", "finding_ids": ["f1"]}
    ],
    "sources": [{"url": "https://example.com/f1", "title": "Annual report", "finding_ids": ["f1"]}],
}

_FINDING: dict[str, Any] = {
    "finding_id": "f1",
    "subtopic_id": "s1",
    "claim": "Cloud revenue grew.",
    "evidence": "Cloud revenue grew year on year.",
    "url": "https://example.com/f1",
    "title": "Annual report",
    "retrieved_at": "2026-08-18T09:30:00+00:00",
    "content_hash": "sha256-f1",
    "truncated": False,
}


def _write(tmp_path: Path, **overrides: Any) -> Path:
    body: dict[str, Any] = {
        "question": "Compare TCS and Infosys on cloud strategy",
        "status": "approved",
        "job_id": "e0a10001-1111-4111-8111-000000000001",
        "model": "fixture-model",
        "planned_subtopics": ["s1"],
        "subtopic_status": {"s1": "done"},
        "findings": [_FINDING],
        "report": _REPORT,
        "verdicts": [{"claim_id": "c1", "supported": True, "quote": "Cloud revenue grew"}],
        "started_at": "2026-08-18T09:20:00+00:00",
        "completed_at": "2026-08-18T09:31:00+00:00",
        "llm_calls_used": 28,
        "revision_count": 1,
    }
    body.update(overrides)
    path = tmp_path / "output.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


# --- 1. The fixture loader --------------------------------------------------------------


def test_a_healthy_fixture_loads_into_the_repository_schemas(tmp_path: Path) -> None:
    output = load_output_file(_write(tmp_path))

    assert output.report is not None
    assert output.report.claims[0].claim_id == "c1"
    assert output.findings[0].subtopic_id == "s1"
    assert output.verdicts[0].supported is True
    assert output.schema_errors == ()
    assert output.metadata.source == "fixture"


def test_the_thread_id_is_the_job_id(tmp_path: Path) -> None:
    # `graph.state.run_config()` sets `thread_id = job_id`, so this is the single identifier
    # that joins an eval row to a database row and to a LangSmith trace.
    output = load_output_file(_write(tmp_path))

    assert output.metadata.thread_id == output.metadata.job_id


def test_latency_is_derived_from_the_two_timestamps_when_it_is_not_recorded(
    tmp_path: Path,
) -> None:
    output = load_output_file(_write(tmp_path))

    assert output.metadata.latency_seconds == 660.0


def test_a_recorded_latency_wins_over_the_derived_one(tmp_path: Path) -> None:
    output = load_output_file(_write(tmp_path, latency_seconds=42.5))

    assert output.metadata.latency_seconds == 42.5


def test_a_job_that_never_finished_has_no_latency(tmp_path: Path) -> None:
    # None rather than zero: a job still running has a start and no end.
    output = load_output_file(_write(tmp_path, completed_at=None))

    assert output.metadata.latency_seconds is None


def test_a_malformed_report_is_recorded_rather_than_raised(tmp_path: Path) -> None:
    broken = {"sections": [], "claims": [{"claim_id": "c1", "section_id": "s"}], "sources": []}

    output = load_output_file(_write(tmp_path, report=broken))

    assert output.report is None
    assert output.schema_errors  # what structured_output_validity scores
    assert any("sources" in error for error in output.schema_errors)


def test_a_job_with_no_report_has_no_schema_errors(tmp_path: Path) -> None:
    # A job that produced nothing is not a job that produced something invalid.
    output = load_output_file(_write(tmp_path, report=None, status="failed"))

    assert output.report is None
    assert output.schema_errors == ()


def test_a_missing_or_unreadable_file_raises(tmp_path: Path) -> None:
    with pytest.raises(OutputError, match="could not be read"):
        load_output_file(tmp_path / "absent.json")

    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(OutputError, match="not valid JSON"):
        load_output_file(path)


def test_an_unknown_key_in_a_fixture_is_refused(tmp_path: Path) -> None:
    # The fixture format is strict about itself, so a mistyped field is loud rather than lost.
    with pytest.raises(OutputError, match="not a research output"):
        load_output_file(_write(tmp_path, subtopics_status={"s1": "done"}))


# --- 2. The database loader --------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return migrated_engine(tmp_path)


def _run_a_job(engine: Engine, *, status: str = "approved") -> str:
    """One finished job written the way the graph writes one - the real statements, in order."""
    job_id = new_job_id()
    queries.create_job(
        engine,
        job_id=job_id,
        user_id=new_job_id(),
        question="Compare TCS and Infosys on cloud strategy",
        idempotency_key=f"key-{job_id}",
        actor="alice@example.com",
    )
    plan = ResearchPlan(
        subtopics=[
            Subtopic(id="s1", question="What is the cloud strategy?", search_query="cloud"),
            Subtopic(id="s2", question="What are the partnerships?", search_query="partners"),
            Subtopic(id="s3", question="What is the investment?", search_query="investment"),
        ],
        success_criteria=["Every claim cites a public source"],
    )
    queries.record_plan(engine, job_id=job_id, plan=plan)
    findings = [a_finding("f1", subtopic_id="s1"), a_finding("f2", subtopic_id="s2")]
    queries.record_research(
        engine, job_id=job_id, new_findings=findings[:1], subtopic="s1", status="done"
    )
    queries.record_research(
        engine, job_id=job_id, new_findings=findings[1:], subtopic="s2", status="done"
    )
    queries.record_research(
        engine, job_id=job_id, new_findings=[], subtopic="s3", status="unresearched"
    )
    report = a_report(("c1", ["f1"]), ("c2", ["f2"]), findings=findings)
    queries.record_claims(engine, job_id=job_id, report=report)
    queries.record_verdicts(
        engine,
        job_id=job_id,
        verdicts=[
            Verdict(claim_id="c1", supported=True, quote="TCS reported", note="stated"),
            Verdict(claim_id="c2", supported=False, quote=None, note="not stated"),
        ],
    )
    queries.record_export_result(engine, job_id=job_id, report=report, uncited=())
    queries.finish_job(
        engine,
        job_id=job_id,
        status=status,  # type: ignore[arg-type]
        failure_reason=None if status == "approved" else "export_write_failed",
        quality_flag=None,
        revision_count=1,
        llm_calls_used=31,
    )
    return job_id


def test_a_real_job_loads_out_of_the_five_tables(engine: Engine) -> None:
    job_id = _run_a_job(engine)

    output = load_output_from_database(engine, job_id)

    assert output.status == "approved"
    assert output.question.startswith("Compare TCS")
    assert output.report is not None and len(output.report.claims) == 2
    assert [finding.finding_id for finding in output.findings] == ["f1", "f2"]
    assert output.metadata.source == "database"
    assert output.metadata.llm_calls_used == 31
    assert output.metadata.revision_count == 1


def test_the_plan_and_the_subtopic_outcomes_come_from_the_audit_trail(engine: Engine) -> None:
    # `subtopic_status` is state and state is not a table, so the trail is where research
    # coverage lives durably.
    job_id = _run_a_job(engine)

    output = load_output_from_database(engine, job_id)

    assert output.planned_subtopics == ("s1", "s2", "s3")
    assert output.subtopic_status == {"s1": "done", "s2": "done", "s3": "unresearched"}


def test_verdicts_load_without_a_quote_because_the_table_does_not_keep_one(
    engine: Engine,
) -> None:
    # This is why `ClaimVerdict` exists rather than `Verdict`: `Verdict` refuses supported=true
    # with no quote, and `claims` stores only `supported` and `verdict_note`.
    job_id = _run_a_job(engine)

    output = load_output_from_database(engine, job_id)

    supported = {verdict.claim_id: verdict.supported for verdict in output.verdicts}
    assert supported == {"c1": True, "c2": False}
    assert all(verdict.quote is None for verdict in output.verdicts)


def test_a_failed_jobs_reason_comes_from_the_job_finished_row(engine: Engine) -> None:
    # ADR 0008 / ADR 0009 decision 5: there is no `jobs.failure_reason` column.
    job_id = _run_a_job(engine, status="failed")

    output = load_output_from_database(engine, job_id)

    assert output.metadata.failure_reason == "export_write_failed"


def test_an_unknown_job_raises(engine: Engine) -> None:
    with pytest.raises(OutputError, match="no job"):
        load_output_from_database(engine, new_job_id())
