"""
WHY THIS FILE EXISTS
    The aggregation, and one property above all others: **no opaque overall score.** The whole
    value of seventeen numbers is that a regression names itself, and the cheapest way to lose
    that is for someone to add a helpful `overall` key. A test that reads the serialised report
    and refuses one is the only thing that keeps that decision from eroding.

    The other property worth pinning is that "not applicable" never becomes a number. A metric
    that answered on three of twenty-six cases has a mean that means very little, and the
    aggregate has to carry the count that says so.

WHO CALLS IT
    pytest. No service, no network, no provider.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from eval.judge import JUDGE_DIMENSIONS, JudgeOutcome, JudgeVerdict
from eval.metrics import MetricResult
from eval.outputs import RunMetadata
from eval.report import (
    CSV_COLUMNS,
    CaseResult,
    EvalRun,
    judge_ran_but_scored_nothing,
    write_csv,
    write_json,
)
from eval.schema import CaseProblem

_AT = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _metric(name: str, score: float | None, passed: bool | None) -> MetricResult:
    return MetricResult(metric=name, score=score, passed=passed, explanation="because")


def _verdict(value: int = 4) -> JudgeVerdict:
    return JudgeVerdict.model_validate(
        {dimension: value for dimension in JUDGE_DIMENSIONS} | {"explanation": "fine"}
    )


def _run(*results: CaseResult, problems: tuple[CaseProblem, ...] = ()) -> EvalRun:
    return EvalRun(
        run_id="eval-test",
        started_at=_AT,
        finished_at=_AT,
        benchmark_path="eval/benchmarks/dev.json",
        benchmark_version="dev-1",
        split="dev",
        judge_enabled=False,
        results=results,
        problems=problems,
    )


def _case(
    case_id: str,
    *,
    status: str = "evaluated",
    metrics: tuple[MetricResult, ...] = (),
    judge: JudgeOutcome | None = None,
    metadata: RunMetadata | None = None,
    error: str | None = None,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        split="dev",
        status=status,  # type: ignore[arg-type]
        category="company_comparison",
        difficulty="medium",
        provenance="synthetic_contract",
        metrics=metrics,
        judge=judge,
        metadata=metadata,
        error=error,
    )


# --- 1. Counts ----------------------------------------------------------------------------


def test_the_four_case_statuses_sum_to_the_total() -> None:
    run = _run(
        _case("a"),
        _case("b", status="failed"),
        _case("c", status="skipped", error="no job_id"),
        _case("d", status="errored", error="missing fixture"),
    )

    counts = run.counts()

    assert counts["total"] == 4
    assert counts["evaluated"] + counts["failed"] + counts["skipped"] + counts["errored"] == 4


def test_unparseable_benchmark_rows_are_counted_outside_that_sum() -> None:
    # They never became cases, so counting them among the results would claim the run
    # evaluated something it never saw.
    run = _run(_case("a"), problems=(CaseProblem("bad", "duplicate case_id"),))

    assert run.counts()["total"] == 1
    assert run.counts()["benchmark_problems"] == 1


# --- 2. Metric aggregates -------------------------------------------------------------------


def test_a_metric_mean_ignores_the_cases_it_did_not_apply_to() -> None:
    run = _run(
        _case("a", metrics=(_metric("terminal_success", 1.0, True),)),
        _case("b", metrics=(_metric("terminal_success", 0.0, False),)),
        _case("c", metrics=(_metric("terminal_success", None, None),)),
    )

    aggregate = run.metric_aggregates()["terminal_success"]

    assert aggregate["cases"] == 3
    assert aggregate["scored"] == 2
    assert aggregate["not_applicable"] == 1
    assert aggregate["mean"] == 0.5  # not 0.333: a None is absent, not zero
    assert (aggregate["passed"], aggregate["failed"], aggregate["no_pass_rule"]) == (1, 1, 1)


def test_a_metric_no_case_produced_reports_none_rather_than_zero() -> None:
    aggregate = _run(_case("a")).metric_aggregates()["source_diversity"]

    assert aggregate["cases"] == 0
    assert aggregate["mean"] is None
    assert aggregate["min"] is None


def test_every_registered_metric_appears_in_the_aggregates() -> None:
    from eval.metrics import METRIC_NAMES

    assert list(_run(_case("a")).metric_aggregates()) == list(METRIC_NAMES)


# --- 3. Judge aggregates ---------------------------------------------------------------------


def test_each_judge_dimension_keeps_its_own_mean() -> None:
    run = _run(
        _case("a", judge=JudgeOutcome("m", "v1", verdict=_verdict(5))),
        _case("b", judge=JudgeOutcome("m", "v1", verdict=_verdict(3))),
    )

    aggregate = run.judge_aggregates()

    assert aggregate["scored"] == 2
    assert aggregate["dimensions"]["faithfulness"]["mean"] == 4.0
    assert set(aggregate["dimensions"]) == set(JUDGE_DIMENSIONS)


def test_a_judge_error_is_counted_and_named_without_costing_the_scored_ones() -> None:
    run = _run(
        _case("a", judge=JudgeOutcome("m", "v1", verdict=_verdict(4))),
        _case("b", judge=JudgeOutcome("m", "v1", error="invalid_output: no")),
    )

    aggregate = run.judge_aggregates()

    assert (aggregate["attempted"], aggregate["scored"], aggregate["errored"]) == (2, 1, 1)
    assert aggregate["errors"] == [{"case_id": "b", "error": "invalid_output: no"}]
    assert aggregate["dimensions"]["relevance"]["mean"] == 4.0


# --- 4. Run statistics -----------------------------------------------------------------------


def test_latency_and_calls_are_reported_from_the_metadata_and_never_scored() -> None:
    run = _run(
        _case("a", metadata=RunMetadata(latency_seconds=100.0, llm_calls_used=20)),
        _case("b", metadata=RunMetadata(latency_seconds=300.0, llm_calls_used=40)),
        _case("c", metadata=RunMetadata(failure_reason="job_timeout")),
    )

    statistics = run.run_statistics()

    assert statistics["outputs_with_metadata"] == 3
    assert statistics["latency_seconds"]["n"] == 2
    assert statistics["latency_seconds"]["p50"] == 100.0  # nearest-rank, a real observation
    assert statistics["llm_calls_used"]["max"] == 40
    assert statistics["failure_reasons"] == {"job_timeout": 1}


# --- 5. The serialised report ------------------------------------------------------------------


def test_the_report_carries_no_overall_quality_score(tmp_path: Path) -> None:
    # ADR 0017 decision 5. Seventeen numbers stay seventeen numbers.
    run = _run(_case("a", metrics=(_metric("terminal_success", 1.0, True),)))

    body = json.loads(write_json(run, tmp_path / "report.json").read_text(encoding="utf-8"))

    assert "overall" not in body
    assert "overall_score" not in body
    assert "quality_score" not in body
    assert set(body) >= {"counts", "metric_aggregates", "judge_aggregates", "cases"}


def test_the_report_records_what_the_run_was_and_which_rubric_judged_it(
    tmp_path: Path,
) -> None:
    run = EvalRun(
        run_id="eval-test",
        started_at=_AT,
        finished_at=_AT,
        benchmark_path="eval/benchmarks/dev.json",
        benchmark_version="dev-1",
        split="dev",
        judge_enabled=True,
        judge_model="judge-model",
        judge_base_url="https://example.invalid/v1",
        judge_rubric_version="eval-judge-v1",
        results=(_case("a"),),
    )

    body = json.loads(write_json(run, tmp_path / "report.json").read_text(encoding="utf-8"))

    assert body["benchmark"] == {
        "path": "eval/benchmarks/dev.json",
        "version": "dev-1",
        "split": "dev",
    }
    assert body["judge"]["enabled"] is True
    assert body["judge"]["model"] == "judge-model"
    assert body["judge"]["rubric_version"] == "eval-judge-v1"


def test_each_case_carries_its_raw_metrics_and_its_run_identity(tmp_path: Path) -> None:
    run = _run(
        _case(
            "a",
            metrics=(_metric("terminal_success", 1.0, True),),
            metadata=RunMetadata(job_id="job-1", thread_id="job-1", latency_seconds=12.0),
        )
    )

    body = json.loads(write_json(run, tmp_path / "report.json").read_text(encoding="utf-8"))
    case = body["cases"][0]

    assert case["metrics"][0]["metric"] == "terminal_success"
    assert case["run_metadata"]["job_id"] == "job-1"
    assert case["run_metadata"]["thread_id"] == "job-1"


def test_a_failing_case_names_the_metrics_that_failed(tmp_path: Path) -> None:
    run = _run(
        _case(
            "a",
            status="failed",
            metrics=(
                _metric("terminal_success", 1.0, True),
                _metric("source_diversity", 0.3, False),
            ),
        )
    )

    body = json.loads(write_json(run, tmp_path / "report.json").read_text(encoding="utf-8"))

    assert body["cases"][0]["failed_metrics"] == ["source_diversity"]


# --- 6. CSV --------------------------------------------------------------------------------------


def test_the_csv_is_one_row_per_case_per_metric(tmp_path: Path) -> None:
    run = _run(
        _case(
            "a",
            metrics=(
                _metric("terminal_success", 1.0, True),
                _metric("source_diversity", None, None),
            ),
        ),
        _case("b", status="errored", error="missing fixture"),
    )

    path = write_csv(run, tmp_path / "report.csv")
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))

    assert list(rows[0]) == list(CSV_COLUMNS)
    assert [row["case_id"] for row in rows] == ["a", "a", "b"]
    assert rows[0]["score"] == "1.0000"
    # A not-applicable metric is an empty cell, never a zero.
    assert (rows[1]["score"], rows[1]["passed"]) == ("", "")
    # A case that produced no metrics still gets a row, carrying its error.
    assert (rows[2]["metric"], rows[2]["explanation"]) == ("", "missing fixture")


# --- 7. The terminal summary ----------------------------------------------------------------------


def test_the_summary_says_that_no_gate_was_applied() -> None:
    # The runner reports; it does not judge the repository. Block C is where that changes.
    lines = "\n".join(_run(_case("a")).summary_lines())

    assert "No threshold was applied" in lines
    assert "disabled" in lines


def test_the_summary_lists_the_cases_that_did_not_run_and_the_ones_that_failed() -> None:
    run = _run(
        _case("gone", status="errored", error="missing fixture"),
        _case(
            "weak",
            status="failed",
            metrics=(_metric("source_diversity", 0.2, False),),
        ),
        problems=(CaseProblem("typo", "duplicate case_id"),),
    )

    lines = "\n".join(run.summary_lines())

    assert "missing fixture" in lines
    assert "duplicate case_id" in lines
    assert "source_diversity" in lines


# --- 8. Provider health, which is not a quality question ---------------------------------


def test_a_judge_that_scored_nothing_is_reported_as_a_provider_fault() -> None:
    # A wrong model id, an expired key, an endpoint that is down: the report looks calm and
    # five dimensions are silently missing everywhere.
    run = _run(_case("a", judge=JudgeOutcome("m", "v1", error="llm_call_failed: unreachable")))

    assert judge_ran_but_scored_nothing(run) is True


def test_one_scored_case_is_enough_to_say_the_provider_answered() -> None:
    # Deliberately not a threshold. How well anything scored is not this predicate's business.
    run = _run(
        _case("a", judge=JudgeOutcome("m", "v1", verdict=_verdict(1))),
        _case("b", judge=JudgeOutcome("m", "v1", error="llm_call_failed: unreachable")),
    )

    assert judge_ran_but_scored_nothing(run) is False


def test_a_run_with_no_judge_at_all_is_not_a_provider_fault() -> None:
    assert judge_ran_but_scored_nothing(_run(_case("a"))) is False


# --- 9. Run identity for comparing two reports -------------------------------------------


def test_the_report_names_the_evaluator_version_and_how_long_it_took(tmp_path: Path) -> None:
    # "Did the system change, or did the ruler?" is the first question two disagreeing reports
    # raise, and only the evaluator version answers it.
    from eval.metrics import METRICS_VERSION

    run = EvalRun(
        run_id="eval-test",
        started_at=_AT,
        finished_at=datetime(2026, 8, 19, 12, 0, 3, tzinfo=UTC),
        benchmark_path="eval/benchmarks/dev.json",
        benchmark_version="dev-1",
        split="dev",
        judge_enabled=False,
        results=(_case("a"),),
    )

    body = json.loads(write_json(run, tmp_path / "report.json").read_text(encoding="utf-8"))

    assert body["evaluator_version"] == METRICS_VERSION
    assert body["duration_seconds"] == 3.0


def test_each_case_carries_its_declared_regression_contract(tmp_path: Path) -> None:
    # It is here so `eval/gate.py` can be a pure function of one report file.
    run = _run(
        CaseResult(
            case_id="defect-one",
            split="dev",
            status="failed",
            metrics=(_metric("source_diversity", 0.2, False),),
            expect_failing_metrics=("source_diversity",),
        )
    )

    body = json.loads(write_json(run, tmp_path / "report.json").read_text(encoding="utf-8"))

    assert body["cases"][0]["expect_failing_metrics"] == ["source_diversity"]
    assert body["cases"][0]["failed_metrics"] == ["source_diversity"]
