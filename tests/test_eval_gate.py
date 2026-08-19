"""
WHY THIS FILE EXISTS
    The gate is the only thing in this repository that can fail a build over an evaluation
    result, so what it refuses to fail on matters as much as what it catches.

    Two halves.

    **The rules.** Each of the six is driven from both sides: intact, and broken in the exact
    way it exists to catch. The one worth reading is `declared_failures_still_fail` - a
    committed defect that stops being caught looks like an improvement, and a gate that only
    checked for *unexpected* failures would go green on it.

    **What must never fail it.** A metric mean moving, a judge score of any value, a slower
    run. Those are asserted explicitly, because the pressure over time is always to add "just
    one" threshold, and the first one is added by editing a file that has no test saying it
    must not.

    The reports under test are built through the real `EvalRun.to_json()` rather than
    hand-written dicts, so the gate is checked against the document the runner actually writes.

WHO CALLS IT
    pytest. No service, no network, no provider.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eval.gate import (
    EXIT_CONTRACT_VIOLATED,
    EXIT_OK,
    EXIT_UNUSABLE_REPORT,
    GateError,
    check,
    load_report,
    main,
)
from eval.judge import JUDGE_DIMENSIONS, JudgeOutcome, JudgeVerdict
from eval.metrics import METRIC_NAMES, METRICS_VERSION, MetricResult
from eval.outputs import RunMetadata
from eval.report import CaseResult, EvalRun, write_json

_AT = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _metrics(failing: tuple[str, ...] = ()) -> tuple[MetricResult, ...]:
    """One result per registered metric, failing exactly the ones named."""
    return tuple(
        MetricResult(
            metric=name,
            score=0.0 if name in failing else 1.0,
            passed=name not in failing,
            explanation="because",
        )
        for name in METRIC_NAMES
    )


def _case(
    case_id: str,
    *,
    failing: tuple[str, ...] = (),
    declared: tuple[str, ...] | None = None,
    status: str | None = None,
    error: str | None = None,
    metrics: tuple[MetricResult, ...] | None = None,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        split="dev",
        status=status or ("failed" if failing else "evaluated"),  # type: ignore[arg-type]
        category="company_comparison",
        difficulty="medium",
        provenance="repository_fixture",
        tags=("known-defect",) if failing else ("healthy",),
        metrics=_metrics(failing) if metrics is None else metrics,
        metadata=RunMetadata(job_id="job-1", thread_id="job-1"),
        error=error,
        expect_failing_metrics=failing if declared is None else declared,
    )


def _report(*results: CaseResult, **overrides: Any) -> dict[str, Any]:
    """A report as the runner writes one, then any mutation a test needs."""
    run = EvalRun(
        run_id="eval-test",
        started_at=_AT,
        finished_at=_AT,
        benchmark_path="eval/benchmarks/dev.json",
        benchmark_version="dev-1",
        split="dev",
        judge_enabled=False,
        results=results,
        selection={"cases": None, "tags": None, "input_mode": "fixture"},
    )
    body = run.to_json()
    body.update(overrides)
    return body


# --- 1. The intact contract ---------------------------------------------------------------


def test_a_clean_report_passes() -> None:
    outcome = check(
        _report(_case("healthy-one"), _case("defect-one", failing=("source_diversity",)))
    )

    assert outcome.passed
    assert outcome.violations == ()
    assert outcome.exit_code == EXIT_OK


def test_the_verdict_names_the_benchmark_and_the_evaluator_version() -> None:
    # "Did the system change, or did the ruler?" is the first question two disagreeing reports
    # raise, and the gate's own output has to answer it.
    outcome = check(_report(_case("healthy-one")))

    assert outcome.benchmark_version == "dev-1"
    assert outcome.evaluator_version == METRICS_VERSION


def test_a_passing_verdict_says_it_gated_no_quality() -> None:
    lines = "\n".join(check(_report(_case("healthy-one"))).lines())

    assert "PASS" in lines
    assert "says nothing about research quality" in lines


# --- 2. The run must have completed --------------------------------------------------------


def test_a_benchmark_row_that_did_not_parse_fails_the_gate() -> None:
    body = _report(_case("healthy-one"))
    body["counts"]["benchmark_problems"] = 1
    body["benchmark_problems"] = [
        {"case_id": "BAD ID", "problem": "case_id: string does not match"}
    ]

    outcome = check(body)

    assert not outcome.passed
    assert [violation.rule for violation in outcome.violations] == ["benchmark_parses"]
    assert "BAD ID" in str(outcome.violations[0])


def test_a_case_that_could_not_be_evaluated_fails_the_gate() -> None:
    body = _report(
        _case("healthy-one"),
        _case("broken", status="errored", error="missing fixture", metrics=()),
    )

    outcome = check(body)

    rules = [violation.rule for violation in outcome.violations]
    assert "no_evaluator_errors" in rules
    assert "missing fixture" in str(outcome.violations[0])


def test_a_skipped_case_fails_the_gate() -> None:
    body = _report(_case("skipped-one", status="skipped", error="no job_id", metrics=()))

    outcome = check(body)

    assert "no_skipped_cases" in [violation.rule for violation in outcome.violations]


def test_a_run_that_evaluated_nothing_fails_the_gate() -> None:
    # Silently gating zero cases is how a filtered job passes forever.
    outcome = check(_report())

    assert "cases_selected" in [violation.rule for violation in outcome.violations]


def test_an_errored_case_is_not_also_reported_as_a_contract_mismatch() -> None:
    # It has no metrics to compare, and one fault should produce one violation.
    body = _report(_case("broken", status="errored", error="missing fixture", metrics=()))

    rules = [violation.rule for violation in check(body).violations]

    assert "no_unexpected_failures" not in rules
    assert "declared_failures_still_fail" not in rules


# --- 3. Every metric must still run --------------------------------------------------------


def test_a_metric_that_stopped_running_fails_the_gate() -> None:
    # The regression the per-case contract cannot catch: a deleted or silent evaluator can
    # never fail a case, so every declared failing set would still match.
    body = _report(_case("healthy-one"))
    del body["metric_aggregates"]["source_diversity"]

    outcome = check(body)

    rules = [violation.rule for violation in outcome.violations]
    assert "metrics_present" in rules
    assert "source_diversity" in " ".join(str(v) for v in outcome.violations)


def test_a_metric_that_ran_on_only_some_cases_fails_the_gate() -> None:
    body = _report(_case("healthy-one"), _case("healthy-two"))
    body["metric_aggregates"]["terminal_success"]["cases"] = 1

    outcome = check(body)

    assert "metrics_ran_on_every_case" in [violation.rule for violation in outcome.violations]


def test_a_metric_this_build_does_not_know_fails_the_gate() -> None:
    # A report from a different evaluator build, or a metric added without updating this one.
    body = _report(_case("healthy-one"))
    body["metric_aggregates"]["vibes"] = {"cases": 1}

    outcome = check(body)

    assert "metrics_registry_matches" in [violation.rule for violation in outcome.violations]


# --- 4. The per-case regression contract ---------------------------------------------------


def test_a_healthy_case_that_starts_failing_fails_the_gate() -> None:
    body = _report(_case("healthy-one", failing=("source_diversity",), declared=()))

    outcome = check(body)

    assert [violation.rule for violation in outcome.violations] == ["no_unexpected_failures"]
    assert "source_diversity" in str(outcome.violations[0])


def test_a_known_defect_that_stops_being_caught_fails_the_gate() -> None:
    # The quiet one. An evaluator that stopped detecting a defect still in the fixture looks
    # exactly like an improvement, and nothing else in this file would notice.
    body = _report(_case("defect-one", failing=(), declared=("source_diversity",)))

    outcome = check(body)

    assert [violation.rule for violation in outcome.violations] == ["declared_failures_still_fail"]
    assert "no longer fails" in str(outcome.violations[0])


def test_a_defect_that_starts_failing_something_else_too_fails_the_gate() -> None:
    body = _report(
        _case(
            "defect-one",
            failing=("source_diversity", "duplicate_source_absence"),
            declared=("source_diversity",),
        )
    )

    outcome = check(body)

    assert [violation.rule for violation in outcome.violations] == ["no_unexpected_failures"]
    assert "duplicate_source_absence" in str(outcome.violations[0])


def test_a_contract_naming_a_metric_that_does_not_exist_fails_the_gate() -> None:
    # Catches a renamed metric whose benchmark entry was not followed through.
    body = _report(_case("defect-one", failing=(), declared=("vibes",)))

    rules = [violation.rule for violation in check(body).violations]

    assert "contract_names_real_metrics" in rules
    # And it is not also reported as a defect that stopped being caught, which would send a
    # reader looking for a regression that is really a typo.
    assert "declared_failures_still_fail" not in rules


def test_every_violation_is_reported_rather_than_only_the_first() -> None:
    # A gate that stops at the first problem costs a second CI run to find the second one.
    body = _report(
        _case("healthy-one", failing=("source_diversity",), declared=()),
        _case("defect-one", failing=(), declared=("citation_presence",)),
    )

    outcome = check(body)

    assert len(outcome.violations) == 2


# --- 5. What must never fail the gate ------------------------------------------------------


def test_a_metric_mean_moving_does_not_fail_the_gate() -> None:
    # The whole point of ADR 0018: no percentage threshold, anywhere.
    body = _report(_case("healthy-one"))
    body["metric_aggregates"]["source_diversity"].update({"mean": 0.11, "min": 0.05, "max": 0.2})

    assert check(body).passed


def test_a_judge_score_of_any_value_does_not_fail_the_gate() -> None:
    # The rubric is uncalibrated; a judge number cannot fail a build (ADR 0017 decision 3).
    verdict = JudgeVerdict.model_validate(
        {dimension: 1 for dimension in JUDGE_DIMENSIONS} | {"explanation": "poor"}
    )
    judged = CaseResult(
        case_id="healthy-one",
        split="dev",
        status="evaluated",
        metrics=_metrics(),
        judge=JudgeOutcome("judge-model", "eval-judge-v1", verdict=verdict),
    )

    assert check(_report(judged)).passed


def test_a_judge_error_does_not_fail_the_gate() -> None:
    judged = CaseResult(
        case_id="healthy-one",
        split="dev",
        status="evaluated",
        metrics=_metrics(),
        judge=JudgeOutcome("judge-model", "eval-judge-v1", error="llm_call_failed: unreachable"),
    )

    assert check(_report(judged)).passed


def test_run_statistics_moving_does_not_fail_the_gate() -> None:
    body = _report(_case("healthy-one"))
    body["run_statistics"]["latency_seconds"] = {"n": 1, "p50": 99999.0, "p95": 99999.0}
    body["duration_seconds"] = 900.0

    assert check(body).passed


def test_adding_a_healthy_case_does_not_fail_the_gate() -> None:
    assert check(_report(_case("healthy-one"), _case("healthy-two"))).passed


# --- 6. Reading a report -------------------------------------------------------------------


def test_a_written_report_round_trips_through_the_gate(tmp_path: Path) -> None:
    run = EvalRun(
        run_id="eval-test",
        started_at=_AT,
        finished_at=_AT,
        benchmark_path="eval/benchmarks/dev.json",
        benchmark_version="dev-1",
        split="dev",
        judge_enabled=False,
        results=(_case("healthy-one"),),
        selection={"input_mode": "fixture"},
    )
    path = write_json(run, tmp_path / "report.json")

    assert check(load_report(path)).passed


def test_a_missing_or_unreadable_report_is_not_a_contract_violation(tmp_path: Path) -> None:
    with pytest.raises(GateError, match="could not be read"):
        load_report(tmp_path / "absent.json")

    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(GateError, match="not valid JSON"):
        load_report(path)


def test_a_document_that_is_not_a_report_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    with pytest.raises(GateError, match="does not look like an evaluation report"):
        load_report(path)


def test_a_database_mode_report_is_refused_rather_than_failed(tmp_path: Path) -> None:
    # `--from-database` legitimately skips every fixture-backed case, so gating such a report
    # would fail for a reason that is not a regression.
    body = _report(_case("healthy-one"))
    body["selection"] = {"input_mode": "database"}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(GateError, match="input_mode"):
        load_report(path)


# --- 7. Exit codes -------------------------------------------------------------------------


def test_the_three_exit_codes_are_distinct() -> None:
    # A build that cannot tell an infrastructure fault from a contract violation will
    # eventually "fix" the former by editing a benchmark.
    assert (EXIT_OK, EXIT_CONTRACT_VIOLATED, EXIT_UNUSABLE_REPORT) == (0, 1, 2)


def test_the_cli_exits_zero_on_an_intact_contract(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report(_case("healthy-one"))), encoding="utf-8")

    assert main([str(path)]) == EXIT_OK
    assert "PASS" in capsys.readouterr().out


def test_the_cli_exits_one_on_a_violation(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "report.json"
    body = _report(_case("healthy-one", failing=("source_diversity",), declared=()))
    path.write_text(json.dumps(body), encoding="utf-8")

    assert main([str(path)]) == EXIT_CONTRACT_VIOLATED
    assert "FAIL" in capsys.readouterr().out


def test_the_cli_exits_two_when_there_is_nothing_to_judge(tmp_path: Path, capsys: Any) -> None:
    assert main([str(tmp_path / "absent.json")]) == EXIT_UNUSABLE_REPORT
    assert "could not run" in capsys.readouterr().out


# --- 8. The committed DEV benchmark ---------------------------------------------------------


def test_the_committed_benchmark_passes_its_own_gate(tmp_path: Path) -> None:
    """The end-to-end claim: `eval.run` then `eval.gate` over the real benchmark, offline.

    This is the exact pair of commands the `eval` CI job runs, so a red build here is a red
    build there.
    """
    from eval.run import main as run_main

    assert run_main(["--out", str(tmp_path), "--run-id", "gate-check"]) == 0
    assert main([str(tmp_path / "gate-check.json")]) == EXIT_OK


def test_every_known_defect_case_declares_the_metrics_it_fails(tmp_path: Path) -> None:
    """And the declaration is exact - not "at least one", which would let a defect drift."""
    from eval.run import main as run_main

    run_main(["--out", str(tmp_path), "--run-id", "gate-check"])
    body = json.loads((tmp_path / "gate-check.json").read_text(encoding="utf-8"))

    defects = [case for case in body["cases"] if "known-defect" in case["tags"]]
    assert len(defects) == 8
    for case in defects:
        assert case["expect_failing_metrics"], case["case_id"]
        assert sorted(case["failed_metrics"]) == sorted(case["expect_failing_metrics"])


def test_every_healthy_case_declares_no_failing_metric(tmp_path: Path) -> None:
    from eval.run import main as run_main

    run_main(["--out", str(tmp_path), "--run-id", "gate-check"])
    body = json.loads((tmp_path / "gate-check.json").read_text(encoding="utf-8"))

    for case in body["cases"]:
        if "known-defect" in case["tags"]:
            continue
        assert case["expect_failing_metrics"] == []
        assert case["failed_metrics"] == []


def test_the_benchmark_run_is_reproducible(tmp_path: Path) -> None:
    """Two runs of the same benchmark produce the same twelve results for every case.

    The metrics are pure functions, so this is meant to be trivially true - and it is the
    property the whole gate rests on, because a contract compared against a moving measurement
    is not a contract.
    """
    from eval.run import main as run_main

    def _cases(run_id: str) -> Any:
        run_main(["--out", str(tmp_path), "--run-id", run_id])
        body = json.loads((tmp_path / f"{run_id}.json").read_text(encoding="utf-8"))
        return [
            {"case_id": case["case_id"], "status": case["status"], "metrics": case["metrics"]}
            for case in body["cases"]
        ]

    assert _cases("first") == _cases("second")
