"""
WHY THIS FILE EXISTS
    The one thing in this repository that can fail CI over an evaluation result - and it is
    deliberately not about research quality.

    **It gates the contract, not the quality**
    ([ADR 0018](../docs/adr/0018-the-ci-evaluation-gate-protects-the-contract-not-the-quality.md)).
    The DEV benchmark is fixture-backed: its questions are real, its outputs are authored files
    citing `example.com`, and no case asserts an external fact about a real company. A
    percentage threshold over that data would be a number about our own fixtures wearing the
    clothes of a quality measurement, and the first time it failed nobody would know whether
    the system got worse or the fixture got edited.

    So what this checks is the set of things the fixture benchmark **can** answer exactly:

      * the benchmark still parses, and every row became a case;
      * every case ran - nothing errored, nothing skipped;
      * every metric still ran on every case, so an evaluator cannot silently stop evaluating;
      * and **each committed output still fails exactly the metrics it is declared to fail**.

    That last one is the whole design. `EvalCase.expect_failing_metrics` names, per case, the
    metrics its output is known to break - eight cases declare one or two, eighteen declare
    none. It is redundant with the case's own expectations, and the redundancy *is* the check:
    an evaluator that stops catching a defect turns green, and green is what this notices.

    **Three things it deliberately never fails on**, because none of them is evidence about
    this repository:

      * a metric mean, minimum or maximum moving;
      * any judge score, at any value - the rubric is uncalibrated (ADR 0017 decision 3);
      * latency, call counts or revision counts.

    There is no threshold in this file. There is no percentage in this file. There is no
    blended score anywhere near it.

    **It is a pure function of one report file.** Everything it needs - the counts, the
    per-case `failed_metrics`, the declared `expect_failing_metrics`, the metric aggregates -
    is already in the JSON `eval/report.py` writes, so the gate opens no benchmark, loads no
    fixture, and runs no evaluator. That is what lets CI keep the two steps honest: one command
    produces the evidence, a second judges it, and the second cannot quietly re-decide the first.

WHO CALLS IT
    `python -m eval.gate <report.json>`, from the `eval` job in `.github/workflows/ci.yml`,
    and tests/test_eval_gate.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval.metrics import METRIC_NAMES, METRICS_VERSION

EXIT_OK = 0
EXIT_CONTRACT_VIOLATED = 1
EXIT_UNUSABLE_REPORT = 2
"""Three outcomes, kept apart because a person reacts to each differently.

`1` means the repository changed in a way the contract forbids - read the violations and either
fix the code or update the benchmark deliberately. `2` means the gate never got to judge
anything: no report, unreadable JSON, or a report from a mode this contract is not defined over.
A build that cannot tell those apart will eventually "fix" an infrastructure fault by editing a
benchmark.
"""

GATED_INPUT_MODE = "fixture"
"""The only input mode the regression contract is defined over.

`--from-database` legitimately skips every case that names no `job_id`, which is all of them
today, so gating such a report would either pass vacuously or fail for a reason that is not a
regression. Refusing it is `EXIT_UNUSABLE_REPORT`, not a violation.
"""


class GateError(RuntimeError):
    """The report could not be read or is not one this contract is defined over."""


@dataclass(frozen=True)
class Violation:
    """One broken rule, named so a build log says what to do about it."""

    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


@dataclass(frozen=True)
class GateOutcome:
    """What the gate decided, and everything it looked at while deciding."""

    passed: bool
    violations: tuple[Violation, ...]
    report_path: str
    run_id: str
    benchmark_version: str
    evaluator_version: str
    counts: dict[str, int]

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.passed else EXIT_CONTRACT_VIOLATED

    def lines(self) -> list[str]:
        """The whole verdict, in the shape a CI log is read in: answer first, reasons after."""
        head = "PASS" if self.passed else "FAIL"
        lines = [
            f"eval gate {head} - {self.report_path}",
            f"  run          : {self.run_id}",
            f"  benchmark    : {self.benchmark_version} | evaluators: {self.evaluator_version}",
            f"  cases        : {self.counts.get('total', 0)} total | "
            f"{self.counts.get('evaluated', 0)} evaluated | {self.counts.get('failed', 0)} "
            f"failed | {self.counts.get('skipped', 0)} skipped | "
            f"{self.counts.get('errored', 0)} errored",
        ]
        if self.violations:
            lines += ["", f"  {len(self.violations)} contract violation(s):"]
            lines += [f"    {violation}" for violation in self.violations]
        else:
            lines += [
                "",
                "  The evaluation framework and the committed benchmark contract are intact.",
                "  This gate says nothing about research quality: the DEV benchmark is",
                "  fixture-backed, and no metric score or judge score is gated",
                "  (docs/evaluation.md, 'What is and is not gated').",
            ]
        return lines


def check(report: dict[str, Any], *, report_path: str = "<report>") -> GateOutcome:
    """Judge one evaluation report against the regression contract.

    Pure, and total: it raises `GateError` only when there is nothing to judge, and otherwise
    returns every violation it found rather than the first. A gate that stops at the first
    problem costs a second CI run to find the second one.
    """
    counts = _counts(report)
    violations: list[Violation] = [
        *_run_completed(report, counts),
        *_metrics_all_ran(report, counts),
        *_case_contract(report),
    ]
    return GateOutcome(
        passed=not violations,
        violations=tuple(violations),
        report_path=report_path,
        run_id=str(report.get("run_id", "?")),
        benchmark_version=str(_benchmark(report).get("version", "?")),
        evaluator_version=str(report.get("evaluator_version", "?")),
        counts=counts,
    )


# --- The rules -------------------------------------------------------------------------


def _run_completed(report: dict[str, Any], counts: dict[str, int]) -> list[Violation]:
    """Did the evaluation actually evaluate everything it was given?

    Four ways it did not, and each is an operational fault rather than a judgement about the
    research: a benchmark row that would not parse, a case whose output would not load, a case
    skipped in a mode this contract is not defined over, and an empty selection.
    """
    violations: list[Violation] = []

    if counts.get("benchmark_problems", 0):
        problems = report.get("benchmark_problems", [])
        violations.append(
            Violation(
                "benchmark_parses",
                f"{counts['benchmark_problems']} benchmark row(s) did not parse: "
                + "; ".join(
                    f"{item.get('case_id')} ({item.get('problem')})"
                    for item in problems
                    if isinstance(item, dict)
                ),
            )
        )

    if not counts.get("total", 0):
        violations.append(Violation("cases_selected", "the run evaluated no cases at all"))

    if counts.get("errored", 0):
        violations.append(
            Violation(
                "no_evaluator_errors",
                f"{counts['errored']} case(s) could not be evaluated: "
                + "; ".join(
                    f"{case['case_id']} ({case.get('error')})"
                    for case in _cases(report)
                    if case.get("status") == "errored"
                ),
            )
        )

    if counts.get("skipped", 0):
        violations.append(
            Violation(
                "no_skipped_cases",
                f"{counts['skipped']} case(s) were skipped; every fixture-backed case must run",
            )
        )

    return violations


def _metrics_all_ran(report: dict[str, Any], counts: dict[str, int]) -> list[Violation]:
    """Did every registered metric produce a result for every case that ran?

    The regression this catches is the one the per-case contract below cannot: a metric that is
    deleted, renamed, or quietly returns nothing can never *fail* a case, so every declared
    failing set would still match while the evaluator had stopped evaluating.
    """
    aggregates = report.get("metric_aggregates")
    if not isinstance(aggregates, dict):
        return [Violation("metrics_present", "the report carries no metric_aggregates block")]

    expected_cases = counts.get("evaluated", 0) + counts.get("failed", 0)
    violations: list[Violation] = []

    missing = [name for name in METRIC_NAMES if name not in aggregates]
    if missing:
        violations.append(
            Violation("metrics_present", f"the report is missing metric(s): {sorted(missing)}")
        )

    for name in METRIC_NAMES:
        aggregate = aggregates.get(name)
        if not isinstance(aggregate, dict):
            continue
        ran = aggregate.get("cases")
        if ran != expected_cases:
            violations.append(
                Violation(
                    "metrics_ran_on_every_case",
                    f"{name} produced a result for {ran} case(s), not {expected_cases}",
                )
            )

    unknown = sorted(set(aggregates) - set(METRIC_NAMES))
    if unknown:
        violations.append(
            Violation(
                "metrics_registry_matches",
                f"the report carries metric(s) this build does not know: {unknown}. "
                f"Evaluator version {METRICS_VERSION} - is the report from a different build?",
            )
        )

    return violations


def _case_contract(report: dict[str, Any]) -> list[Violation]:
    """Does each committed output still fail exactly the metrics it declares?

    The core rule. A case that declares nothing must fail nothing; a case that declares
    `["source_diversity"]` must fail that and only that. Both directions matter:

      * an **unexpected** failure means a healthy fixture regressed, or a metric got stricter;
      * a **missing** failure means an evaluator stopped catching a defect that is still there,
        which is the quiet one - it looks like an improvement.
    """
    violations: list[Violation] = []
    known = set(METRIC_NAMES)

    for case in _cases(report):
        if case.get("status") in ("errored", "skipped"):
            continue  # already reported by `_run_completed`; no metrics to compare
        case_id = str(case.get("case_id", "?"))
        declared = _names(case.get("expect_failing_metrics"))
        actual = _names(case.get("failed_metrics"))

        nonexistent = sorted(declared - known)
        if nonexistent:
            violations.append(
                Violation(
                    "contract_names_real_metrics",
                    f"{case_id} declares metric(s) that do not exist: {nonexistent}",
                )
            )

        unexpected = sorted(actual - declared)
        if unexpected:
            violations.append(
                Violation(
                    "no_unexpected_failures",
                    f"{case_id} failed {unexpected}, which it does not declare",
                )
            )

        absent = sorted(declared - actual - set(nonexistent))
        if absent:
            violations.append(
                Violation(
                    "declared_failures_still_fail",
                    f"{case_id} no longer fails {absent}; an evaluator may have stopped "
                    "catching a defect that is still in the fixture",
                )
            )

    return violations


# --- Reading the report ------------------------------------------------------------------


def load_report(path: Path) -> dict[str, Any]:
    """One report file, checked far enough to be judgeable.

    The input-mode check is here rather than among the rules because a database-mode report is
    not a *violation* - it is a report this contract is not defined over, and calling it a
    failure would teach a reader to fix it by editing the benchmark.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise GateError(f"{path}: could not be read ({error})") from error
    try:
        loaded: Any = json.loads(text)
    except ValueError as error:
        raise GateError(f"{path}: is not valid JSON ({error})") from error
    if not isinstance(loaded, dict):
        raise GateError(f"{path}: the top level must be a JSON object")
    if not isinstance(loaded.get("cases"), list):
        raise GateError(f"{path}: does not look like an evaluation report (no `cases` list)")

    mode = loaded.get("selection", {}).get("input_mode")
    if mode != GATED_INPUT_MODE:
        raise GateError(
            f"{path}: input_mode is {mode!r}; the regression contract is defined over "
            f"{GATED_INPUT_MODE!r} runs only"
        )
    return loaded


def _counts(report: dict[str, Any]) -> dict[str, int]:
    counts = report.get("counts")
    if not isinstance(counts, dict):
        return {}
    return {key: value for key, value in counts.items() if isinstance(value, int)}


def _benchmark(report: dict[str, Any]) -> dict[str, Any]:
    benchmark = report.get("benchmark")
    return benchmark if isinstance(benchmark, dict) else {}


def _cases(report: dict[str, Any]) -> list[dict[str, Any]]:
    cases = report.get("cases")
    return [case for case in cases if isinstance(case, dict)] if isinstance(cases, list) else []


def _names(value: Any) -> set[str]:
    return {str(item) for item in value} if isinstance(value, list) else set()


# --- Entry point -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Judge one report. `0` contract intact, `1` violated, `2` nothing judgeable."""
    parser = argparse.ArgumentParser(
        prog="eval.gate",
        description=(
            "Fail the build when the evaluation framework or the committed benchmark contract "
            "regresses. It gates neither metric scores nor judge scores."
        ),
    )
    parser.add_argument("report", type=Path, help="the JSON report `python -m eval.run` wrote")
    args = parser.parse_args(argv)

    try:
        report = load_report(args.report)
    except GateError as error:
        print(f"eval gate could not run: {error}", flush=True)
        return EXIT_UNUSABLE_REPORT

    outcome = check(report, report_path=str(args.report))
    for line in outcome.lines():
        print(line, flush=True)
    return outcome.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
