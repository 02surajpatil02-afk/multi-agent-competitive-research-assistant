"""
WHY THIS FILE EXISTS
    The runner end to end, and the two claims that make it usable as a harness rather than a
    script.

    **One bad case does not end the run.** Asserted three ways - a benchmark row that will not
    parse, a fixture that is not there, and a case that has nothing to evaluate in the mode it
    was asked for. All three produce a report with the other cases in it.

    **It never exits non-zero because the research scored badly.** The DEV benchmark contains
    deliberate defects and the run still returns 0, because the exit code answers "did the
    evaluation run?" There is no gate here and Block C is where that changes.

    It also runs the **committed DEV benchmark** for real, which is what makes the benchmark a
    thing that works rather than a thing that parses.

WHO CALLS IT
    pytest. No service, no network, no provider - `--judge` is never passed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eval.report import CaseResult
from eval.run import DEFAULT_BENCHMARK, main, parse_args

_CASE: dict[str, Any] = {
    "case_id": "cmp-example",
    "split": "dev",
    "question": "Compare Alpha and Beta on their platform strategy",
    "category": "company_comparison",
    "difficulty": "medium",
    "provenance": "synthetic_contract",
    "output_ref": "outputs/cmp-example.json",
    "expected_status": "approved",
    "required_entities": ["Alpha", "Beta"],
}

_OUTPUT: dict[str, Any] = {
    "question": "Compare Alpha and Beta on their platform strategy",
    "status": "approved",
    "job_id": "e0a10001-1111-4111-8111-000000000001",
    "planned_subtopics": ["s1"],
    "subtopic_status": {"s1": "done"},
    "findings": [
        {
            "finding_id": "f1",
            "subtopic_id": "s1",
            "claim": "Alpha reported a platform.",
            "evidence": "Alpha reported a platform.",
            "url": "https://a.example.com/one",
            "title": "Source",
            "retrieved_at": "2026-08-18T09:30:00+00:00",
            "content_hash": "sha256-f1",
            "truncated": False,
        }
    ],
    "report": {
        "sections": [{"id": "sec1", "heading": "Platforms", "body": "Alpha and Beta both ship."}],
        "claims": [
            {"claim_id": "c1", "section_id": "sec1", "text": "Alpha ships.", "finding_ids": ["f1"]}
        ],
        "sources": [{"url": "https://a.example.com/one", "title": "Source", "finding_ids": ["f1"]}],
    },
    "verdicts": [{"claim_id": "c1", "supported": True, "quote": "Alpha reported a platform."}],
    "llm_calls_used": 12,
}


def _bench(tmp_path: Path, *cases: Any, outputs: dict[str, Any] | None = None) -> Path:
    """A benchmark file and its outputs, laid out the way the committed one is."""
    path = tmp_path / "bench.json"
    path.write_text(json.dumps({"version": "test-1", "cases": list(cases)}), encoding="utf-8")
    (tmp_path / "outputs").mkdir(exist_ok=True)
    for name, body in (outputs or {"cmp-example": _OUTPUT}).items():
        (tmp_path / "outputs" / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")
    return path


def _report(out: Path, run_id: str = "eval-test") -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((out / f"{run_id}.json").read_text(encoding="utf-8"))
    return loaded


def _run(benchmark: Path, out: Path, *extra: str) -> int:
    return main(["--benchmark", str(benchmark), "--out", str(out), "--run-id", "eval-test", *extra])


# --- 1. A healthy run ----------------------------------------------------------------------


def test_a_healthy_case_is_evaluated_and_written(tmp_path: Path) -> None:
    out = tmp_path / "out"

    assert _run(_bench(tmp_path, _CASE), out) == 0

    body = _report(out)
    assert body["counts"] == {
        "total": 1,
        "evaluated": 1,
        "failed": 0,
        "skipped": 0,
        "errored": 0,
        "benchmark_problems": 0,
    }
    assert body["cases"][0]["case_id"] == "cmp-example"
    assert body["metric_aggregates"]["terminal_success"]["mean"] == 1.0


def test_the_judge_is_off_unless_it_is_asked_for(tmp_path: Path) -> None:
    # The default path makes no provider call and needs no credential, which is what lets this
    # whole subsystem run in CI.
    out = tmp_path / "out"

    _run(_bench(tmp_path, _CASE), out)

    body = _report(out)
    assert body["judge"]["enabled"] is False
    assert body["judge"]["model"] is None
    assert body["judge"]["rubric_version"] is None
    assert body["cases"][0]["judge"] is None


def test_a_csv_is_written_only_when_it_is_asked_for(tmp_path: Path) -> None:
    out = tmp_path / "out"

    _run(_bench(tmp_path, _CASE), out)
    assert not (out / "eval-test.csv").exists()

    _run(_bench(tmp_path, _CASE), out, "--csv")
    assert (out / "eval-test.csv").exists()


def test_selection_by_case_id_and_by_tag(tmp_path: Path) -> None:
    other = {**_CASE, "case_id": "cmp-other", "tags": ["slow"]}
    benchmark = _bench(
        tmp_path, _CASE, other, outputs={"cmp-example": _OUTPUT, "cmp-other": _OUTPUT}
    )
    out = tmp_path / "out"

    _run(benchmark, out, "--case", "cmp-other")
    assert [case["case_id"] for case in _report(out)["cases"]] == ["cmp-other"]

    _run(benchmark, out, "--tag", "slow")
    assert [case["case_id"] for case in _report(out)["cases"]] == ["cmp-other"]


# --- 2. Error isolation ----------------------------------------------------------------------


def test_a_benchmark_row_that_will_not_parse_does_not_stop_the_others(tmp_path: Path) -> None:
    benchmark = _bench(tmp_path, {**_CASE, "case_id": "BAD ID"}, _CASE)
    out = tmp_path / "out"

    assert _run(benchmark, out) == 0

    body = _report(out)
    assert body["counts"]["evaluated"] == 1
    assert body["counts"]["benchmark_problems"] == 1
    assert body["benchmark_problems"][0]["case_id"] == "BAD ID"


def test_a_case_whose_output_is_missing_is_errored_and_the_run_continues(
    tmp_path: Path,
) -> None:
    absent = {**_CASE, "case_id": "cmp-absent", "output_ref": "outputs/nowhere.json"}
    out = tmp_path / "out"

    assert _run(_bench(tmp_path, absent, _CASE), out) == 0

    body = _report(out)
    assert body["counts"]["errored"] == 1
    assert body["counts"]["evaluated"] == 1
    errored = next(case for case in body["cases"] if case["status"] == "errored")
    assert "could not be read" in errored["error"]
    assert errored["metrics"] == []


def test_a_case_whose_output_is_not_json_is_errored(tmp_path: Path) -> None:
    benchmark = _bench(tmp_path, _CASE)
    (tmp_path / "outputs" / "cmp-example.json").write_text("{", encoding="utf-8")
    out = tmp_path / "out"

    assert _run(benchmark, out) == 0
    assert _report(out)["counts"]["errored"] == 1


def test_a_case_with_a_broken_report_body_is_evaluated_rather_than_errored(
    tmp_path: Path,
) -> None:
    # A malformed report is the most interesting failure the system can produce. It has to be
    # a score, not a crash.
    broken = {**_OUTPUT, "report": {"sections": [], "claims": [], "sources": []}}
    out = tmp_path / "out"

    _run(_bench(tmp_path, _CASE, outputs={"cmp-example": broken}), out)

    body = _report(out)
    assert body["counts"]["errored"] == 0
    assert body["counts"]["failed"] == 1
    case = body["cases"][0]
    validity = next(m for m in case["metrics"] if m["metric"] == "structured_output_validity")
    assert validity["score"] == 0.0


# --- 3. A failing metric is a result, not an exit code -------------------------------------------


def test_a_case_that_fails_its_own_expectations_still_exits_zero(tmp_path: Path) -> None:
    demanding = {**_CASE, "required_entities": ["Alpha", "Gamma"], "min_distinct_domains": 4}
    out = tmp_path / "out"

    assert _run(_bench(tmp_path, demanding), out) == 0

    body = _report(out)
    assert body["counts"]["failed"] == 1
    assert set(body["cases"][0]["failed_metrics"]) == {
        "expected_entity_coverage",
        "source_diversity",
    }


# --- 4. Operational failures do exit non-zero -----------------------------------------------------


def test_a_missing_benchmark_file_exits_one(tmp_path: Path) -> None:
    assert main(["--benchmark", str(tmp_path / "absent.json"), "--out", str(tmp_path)]) == 1


def test_a_selection_that_matches_nothing_exits_one(tmp_path: Path) -> None:
    # Silently reporting zero cases as a success is how a filtered CI job passes forever.
    assert _run(_bench(tmp_path, _CASE), tmp_path / "out", "--case", "cmp-nobody") == 1


# --- 5. Database mode -----------------------------------------------------------------------------


def test_a_fixture_backed_case_is_skipped_rather_than_errored_in_database_mode(
    tmp_path: Path,
) -> None:
    # A case with no `job_id` is not broken; it simply is not a real job.
    out = tmp_path / "out"
    database = f"sqlite+pysqlite:///{(tmp_path / 'empty.db').as_posix()}"

    assert _run(_bench(tmp_path, _CASE), out, "--from-database", database) == 0

    body = _report(out)
    assert body["counts"]["skipped"] == 1
    assert body["counts"]["errored"] == 0
    assert "job_id" in body["cases"][0]["error"]
    assert body["selection"]["input_mode"] == "database"


# --- 6. The committed DEV benchmark ---------------------------------------------------------------


def test_the_dev_benchmark_runs_end_to_end_with_no_errors(tmp_path: Path) -> None:
    out = tmp_path / "out"

    assert main(["--out", str(out), "--run-id", "eval-dev"]) == 0

    body = _report(out, "eval-dev")
    counts = body["counts"]
    assert counts["errored"] == 0
    assert counts["skipped"] == 0
    assert counts["benchmark_problems"] == 0
    assert counts["total"] == counts["evaluated"] + counts["failed"]
    assert body["benchmark"]["path"] == str(DEFAULT_BENCHMARK)


def test_the_dev_benchmarks_deliberate_defects_are_the_cases_that_fail(tmp_path: Path) -> None:
    # The benchmark is not all-healthy on purpose: a flat set of 1.0s gives Block C no
    # distribution to calibrate against. What must hold is that the failures are the labelled
    # ones and nothing else has quietly joined them.
    out = tmp_path / "out"
    main(["--out", str(out), "--run-id", "eval-dev"])

    body = _report(out, "eval-dev")
    failing = {case["case_id"] for case in body["cases"] if case["status"] == "failed"}
    labelled = {case["case_id"] for case in body["cases"] if "known-defect" in case["tags"]}

    assert failing == labelled


def test_the_dev_run_reports_latency_and_calls_without_scoring_them(tmp_path: Path) -> None:
    out = tmp_path / "out"
    main(["--out", str(out), "--run-id", "eval-dev"])

    statistics = _report(out, "eval-dev")["run_statistics"]

    assert statistics["latency_seconds"]["n"] > 0
    assert statistics["llm_calls_used"]["max"] is not None
    assert statistics["output_sources"] == {"fixture": statistics["outputs_with_metadata"]}


# --- 7. Arguments ---------------------------------------------------------------------------------


def test_the_defaults_point_at_the_committed_benchmark_and_a_gitignored_output_dir() -> None:
    args = parse_args([])

    assert args.benchmark == DEFAULT_BENCHMARK
    assert args.out.parts[0] == "measurements"
    assert args.judge is False
    assert args.from_database is None


def test_the_judge_model_and_endpoint_default_to_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "some-judge")
    monkeypatch.setenv("EVAL_JUDGE_BASE_URL", "https://judge.invalid/v1")

    args = parse_args([])

    assert args.judge_model == "some-judge"
    assert args.judge_base_url == "https://judge.invalid/v1"


def test_requiring_judge_scores_is_off_by_default() -> None:
    # It is a provider-health switch for the manual judge workflow, never something a
    # deterministic run has an opinion about.
    assert parse_args([]).require_judge_scores is False
    assert parse_args(["--require-judge-scores"]).require_judge_scores is True


def test_a_deterministic_run_never_fails_on_the_judge_health_switch(tmp_path: Path) -> None:
    # No judge was attempted, so there is nothing for the switch to be unhappy about.
    out = tmp_path / "out"

    assert _run(_bench(tmp_path, _CASE), out, "--require-judge-scores") == 0


def test_a_case_result_names_its_failing_metrics() -> None:
    assert CaseResult(case_id="a", split="dev", status="evaluated").failed_metrics == ()
