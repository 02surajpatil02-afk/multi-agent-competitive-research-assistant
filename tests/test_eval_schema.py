"""
WHY THIS FILE EXISTS
    Two things, and the second is the one that keeps the benchmark honest over time.

    **`load_benchmark` isolates a bad case.** The whole error-isolation promise starts here: a
    row that does not validate has to come back as a problem beside twenty-five results, not as
    an exception that costs the run everything. A test that only checked the happy path would
    pass against a loader that raised on the first bad row.

    **The committed DEV benchmark is checked as data.** Every `output_ref` resolves, every id is
    unique, every case validates, and every known-defect case says so in `notes`. Those are the
    properties a reviewer would otherwise have to check by hand on every benchmark edit, and
    they are exactly the ones that rot quietly.

WHO CALLS IT
    pytest. No service, no network, no provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eval.schema import Benchmark, BenchmarkError, EvalCase, load_benchmark

DEV_BENCHMARK = Path("eval/benchmarks/dev.json")

_MINIMAL: dict[str, Any] = {
    "case_id": "cmp-example",
    "split": "dev",
    "question": "Compare Alpha and Beta on their platform strategy",
    "category": "company_comparison",
    "difficulty": "medium",
    "provenance": "synthetic_contract",
    "output_ref": "../fixtures/outputs/cmp-example.json",
    "expected_status": "approved",
}


def _write(tmp_path: Path, *cases: Any, version: str | None = "test-1") -> Path:
    document: dict[str, Any] = {"cases": list(cases)}
    if version is not None:
        document["version"] = version
    path = tmp_path / "bench.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# --- 1. One case ----------------------------------------------------------------------


def test_a_minimal_case_validates_and_defaults_everything_optional() -> None:
    case = EvalCase.model_validate(_MINIMAL)

    assert case.case_id == "cmp-example"
    assert case.required_entities == []
    assert case.required_facts == []
    # Every optional expectation is None rather than a number, which is what lets a metric say
    # "this case states no rule" instead of inheriting one nobody wrote down.
    assert case.min_sources is None
    assert case.min_distinct_domains is None
    assert case.max_unsupported_claims is None
    assert case.expect_all_subtopics_researched is None
    assert case.job_id is None


def test_an_unknown_key_is_refused_rather_than_ignored() -> None:
    # A typo in a benchmark is an expectation that silently stops being checked.
    with pytest.raises(ValueError, match="min_source"):
        EvalCase.model_validate({**_MINIMAL, "min_source": 4})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_id", "Not A Slug"),
        ("split", "holdout"),  # deliberately does not exist yet
        ("category", "vibes"),
        ("difficulty", "impossible"),
        ("provenance", "made_up"),
        ("expected_status", "done"),
        ("question", "too short"),
        ("min_claims", 0),
        ("max_unsupported_claims", -1),
    ],
)
def test_a_value_outside_the_vocabulary_is_refused(field: str, value: Any) -> None:
    with pytest.raises(ValueError, match=field):
        EvalCase.model_validate({**_MINIMAL, field: value})


def test_the_regression_contract_defaults_to_expecting_no_failure() -> None:
    # Absent means "this output must fail nothing", which is what makes eighteen healthy cases
    # need no extra line in the benchmark (ADR 0018).
    assert EvalCase.model_validate(_MINIMAL).expect_failing_metrics == []


def test_the_regression_contract_accepts_the_metrics_a_case_is_known_to_fail() -> None:
    case = EvalCase.model_validate({**_MINIMAL, "expect_failing_metrics": ["source_diversity"]})

    assert case.expect_failing_metrics == ["source_diversity"]


def test_a_required_fact_needs_at_least_one_non_blank_phrase() -> None:
    with pytest.raises(ValueError):
        EvalCase.model_validate({**_MINIMAL, "required_facts": [{"id": "x", "any_of": []}]})
    with pytest.raises(ValueError, match="non-empty"):
        EvalCase.model_validate({**_MINIMAL, "required_facts": [{"id": "x", "any_of": ["  "]}]})


# --- 2. A malformed case is isolated, not fatal ----------------------------------------


def test_one_malformed_case_does_not_cost_the_others(tmp_path: Path) -> None:
    good = {**_MINIMAL, "case_id": "cmp-good"}
    other = {**_MINIMAL, "case_id": "cmp-other"}
    path = _write(tmp_path, good, {**_MINIMAL, "case_id": "BAD ID"}, other)

    benchmark = load_benchmark(path)

    assert [case.case_id for case in benchmark.cases] == ["cmp-good", "cmp-other"]
    assert len(benchmark.problems) == 1
    assert benchmark.problems[0].case_id == "BAD ID"
    assert "case_id" in benchmark.problems[0].problem


def test_a_case_that_is_not_an_object_is_a_problem_locatable_by_index(tmp_path: Path) -> None:
    path = _write(tmp_path, _MINIMAL, "not a case")

    benchmark = load_benchmark(path)

    assert len(benchmark.cases) == 1
    assert benchmark.problems[0].case_id == "case[1]"


def test_a_duplicate_case_id_is_a_problem_rather_than_a_silent_overwrite(tmp_path: Path) -> None:
    # Two rows with one id would produce one result and no sign that a case stopped running.
    path = _write(tmp_path, _MINIMAL, {**_MINIMAL, "question": "Compare Gamma and Delta on ads"})

    benchmark = load_benchmark(path)

    assert len(benchmark.cases) == 1
    assert benchmark.problems[0].problem == "duplicate case_id"


# --- 3. A broken file has nothing to isolate -------------------------------------------


def test_a_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkError, match="could not be read"):
        load_benchmark(tmp_path / "absent.json")


def test_a_file_that_is_not_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "bench.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="not valid JSON"):
        load_benchmark(path)


def test_a_benchmark_without_a_version_raises(tmp_path: Path) -> None:
    # The version is what a published number names when it says which benchmark produced it.
    with pytest.raises(BenchmarkError, match="version"):
        load_benchmark(_write(tmp_path, _MINIMAL, version=None))


def test_a_benchmark_without_cases_raises(tmp_path: Path) -> None:
    path = tmp_path / "bench.json"
    path.write_text(json.dumps({"version": "test-1"}), encoding="utf-8")
    with pytest.raises(BenchmarkError, match="cases"):
        load_benchmark(path)


# --- 4. Selection ----------------------------------------------------------------------


def test_selection_filters_compose_and_no_filter_means_everything(tmp_path: Path) -> None:
    one = {**_MINIMAL, "case_id": "cmp-one", "tags": ["healthy", "comparison"]}
    two = {**_MINIMAL, "case_id": "cmp-two", "tags": ["known-defect"]}
    benchmark = load_benchmark(_write(tmp_path, one, two))

    assert len(benchmark.select()) == 2
    assert [case.case_id for case in benchmark.select(case_ids=frozenset({"cmp-two"}))] == [
        "cmp-two"
    ]
    assert [case.case_id for case in benchmark.select(tags=frozenset({"healthy"}))] == ["cmp-one"]
    assert benchmark.select(split="dev", tags=frozenset({"absent"})) == ()


# --- 5. The committed DEV benchmark ----------------------------------------------------


@pytest.fixture(scope="module")
def dev() -> Benchmark:
    return load_benchmark(DEV_BENCHMARK)


def test_the_dev_benchmark_loads_with_no_problems(dev: Benchmark) -> None:
    assert dev.problems == ()
    # The size guidelines §15 asks a dataset to reach. Asserted as a range rather than an exact
    # number so adding a case is an ordinary edit, and shrinking it below the range is not.
    assert 20 <= len(dev.cases) <= 30


def test_every_dev_case_points_at_an_output_that_exists(dev: Benchmark) -> None:
    for case in dev.cases:
        resolved = DEV_BENCHMARK.parent / case.output_ref
        assert resolved.is_file(), f"{case.case_id} points at a missing output: {resolved}"


def test_every_dev_case_is_in_the_dev_split_and_uniquely_named(dev: Benchmark) -> None:
    assert {case.split for case in dev.cases} == {"dev"}
    assert len({case.case_id for case in dev.cases}) == len(dev.cases)


def test_the_dev_benchmark_covers_more_than_one_category_and_difficulty(dev: Benchmark) -> None:
    # A benchmark of twenty-six comparisons at one difficulty measures one thing twenty-six
    # times, which is the failure mode a size check alone would not catch.
    assert len({case.category for case in dev.cases}) >= 6
    assert {case.difficulty for case in dev.cases} == {"easy", "medium", "hard"}


def test_every_known_defect_case_explains_itself(dev: Benchmark) -> None:
    # A low score has to read as intended rather than as a bug, and the note is where that is
    # said. Without it, the eight deliberate failures below are indistinguishable from rot.
    defects = [case for case in dev.cases if "known-defect" in case.tags]
    assert len(defects) >= 5
    for case in defects:
        assert case.notes and "KNOWN DEFECT" in case.notes, case.case_id


def test_only_known_defect_cases_declare_failing_metrics(dev: Benchmark) -> None:
    # The regression contract and the label have to agree, or a reader cannot tell a
    # deliberate failure from an undocumented one.
    for case in dev.cases:
        declared = bool(case.expect_failing_metrics)
        labelled = "known-defect" in case.tags
        assert declared == labelled, case.case_id


def test_no_dev_case_carries_a_job_id(dev: Benchmark) -> None:
    # Every DEV case is fixture-backed. `job_id` is what `--from-database` loads by, and a case
    # that carried one here would claim a real job this repository does not ship.
    assert [case.job_id for case in dev.cases] == [None] * len(dev.cases)
