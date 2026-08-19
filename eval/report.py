"""
WHY THIS FILE EXISTS
    One evaluation run, as a machine-readable document and as ten lines of terminal.

    The shape is decided by one rule, which is ADR 0017 decision 5: **there is no overall
    quality score.** Twelve deterministic metrics and five judge dimensions stay seventeen
    numbers all the way through, because "quality fell 0.4" tells nobody what to look at, and a
    weighted blend of things measured on different scales - a rate, a share, a 1-5 opinion -
    hides which one moved. The aggregates below are per metric, and the per-case results carry
    every raw component under them.

    Three further decisions.

    **A case has four outcomes, not two.** `evaluated` and `failed` differ by whether a stated
    pass rule was broken; `skipped` and `errored` are the two ways a case produced no metrics at
    all, and they are kept apart because one is a choice (no output for this case in this mode)
    and the other is a fault (the output would not load). Collapsing them is how a benchmark
    quietly stops covering half its cases.

    **`failed` is a statement about the case's own expectations, and nothing here acts on it.**
    No exit code, no threshold, no gate. Block C calibrates thresholds against the baseline this
    produces, and adding one now would mean picking numbers before there is a distribution to
    pick them from (docs/evaluation.md, "Why CI quality thresholds are deferred").

    **Errors are recorded as text and never as an exception object.** A traceback can carry a
    connection string; `str(error)` on a `ValueError` this package raised carries a path and a
    reason. The report is a file someone may attach to an issue.

WHO CALLS IT
    eval/run.py builds one `EvalRun` and asks it for JSON, CSV and the summary, and
    tests/test_eval_report.py holds the aggregation.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from eval.judge import JUDGE_DIMENSIONS, JudgeOutcome
from eval.metrics import METRIC_NAMES, MetricResult
from eval.outputs import RunMetadata
from eval.schema import CaseProblem

CaseStatus = Literal["evaluated", "failed", "skipped", "errored"]
"""How one case ended.

  * `evaluated` - every metric with a pass rule passed.
  * `failed`    - the metrics ran and at least one stated rule was broken. **A result, not an
                  error**: this is the benchmark doing its job.
  * `skipped`   - deliberately not evaluated in this mode, with a reason.
  * `errored`   - could not be evaluated. A malformed case, or an output that would not load.
"""


@dataclass(frozen=True)
class CaseResult:
    """One case's complete result, including the identity that joins it to everything else."""

    case_id: str
    split: str
    status: CaseStatus
    question: str = ""
    category: str = ""
    difficulty: str = ""
    provenance: str = ""
    tags: tuple[str, ...] = ()
    output_ref: str = ""
    metrics: tuple[MetricResult, ...] = ()
    judge: JudgeOutcome | None = None
    metadata: RunMetadata | None = None
    error: str | None = None
    """Why this case is `skipped` or `errored`. `None` on a case that ran."""

    @property
    def failed_metrics(self) -> tuple[str, ...]:
        return tuple(metric.metric for metric in self.metrics if metric.passed is False)

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "split": self.split,
            "status": self.status,
            "question": self.question,
            "category": self.category,
            "difficulty": self.difficulty,
            "provenance": self.provenance,
            "tags": list(self.tags),
            "output_ref": self.output_ref,
            "error": self.error,
            "failed_metrics": list(self.failed_metrics),
            "metrics": [metric.to_json() for metric in self.metrics],
            "judge": None if self.judge is None else self.judge.to_json(),
            "run_metadata": None if self.metadata is None else self.metadata.to_json(),
        }


@dataclass(frozen=True)
class EvalRun:
    """Everything one invocation of the runner produced."""

    run_id: str
    started_at: datetime
    finished_at: datetime
    benchmark_path: str
    benchmark_version: str
    split: str | None
    judge_enabled: bool
    results: tuple[CaseResult, ...]
    problems: tuple[CaseProblem, ...] = ()
    judge_model: str | None = None
    judge_base_url: str | None = None
    judge_rubric_version: str | None = None
    selection: dict[str, Any] = field(default_factory=dict)
    """What this run was asked to evaluate - the case ids and tags filtered on, and the input
    mode. Recorded because two reports over the same benchmark are only comparable when they
    covered the same cases, and that is not visible from the counts."""

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "benchmark": {
                "path": self.benchmark_path,
                "version": self.benchmark_version,
                "split": self.split,
            },
            "selection": self.selection,
            "judge": {
                "enabled": self.judge_enabled,
                "model": self.judge_model,
                "base_url": self.judge_base_url,
                "rubric_version": self.judge_rubric_version,
                "dimensions": list(JUDGE_DIMENSIONS),
            },
            "counts": self.counts(),
            "metric_aggregates": self.metric_aggregates(),
            "judge_aggregates": self.judge_aggregates(),
            "run_statistics": self.run_statistics(),
            "benchmark_problems": [
                {"case_id": problem.case_id, "problem": problem.problem}
                for problem in self.problems
            ],
            "cases": [result.to_json() for result in self.results],
        }

    def counts(self) -> dict[str, int]:
        """Cases by outcome.

        `total` is every case this run was asked about, including the ones that never ran, so
        the four statuses always sum to it. `benchmark_problems` sits outside that sum because
        those rows never became cases - they are what `load_benchmark()` could not parse, and
        counting them among the results would claim the run evaluated something it never saw.
        """
        by_status = Counter(result.status for result in self.results)
        return {
            "total": len(self.results),
            "evaluated": by_status["evaluated"],
            "failed": by_status["failed"],
            "skipped": by_status["skipped"],
            "errored": by_status["errored"],
            "benchmark_problems": len(self.problems),
        }

    def metric_aggregates(self) -> dict[str, dict[str, Any]]:
        """Per metric, over every case that produced one.

        `not_applicable` is counted separately from `scored` and never folded into it, because a
        metric that answered on three of twenty-four cases has a mean that means very little -
        and a reader can only know that if the count is next to it.
        """
        aggregates: dict[str, dict[str, Any]] = {}
        for name in METRIC_NAMES:
            results = [
                metric
                for result in self.results
                for metric in result.metrics
                if metric.metric == name
            ]
            scores = [metric.score for metric in results if metric.score is not None]
            verdicts = [metric.passed for metric in results if metric.passed is not None]
            aggregates[name] = {
                "cases": len(results),
                "scored": len(scores),
                "not_applicable": len(results) - len(scores),
                "mean": _mean(scores),
                "min": min(scores) if scores else None,
                "max": max(scores) if scores else None,
                "passed": sum(1 for verdict in verdicts if verdict),
                "failed": sum(1 for verdict in verdicts if not verdict),
                "no_pass_rule": len(results) - len(verdicts),
            }
        return aggregates

    def judge_aggregates(self) -> dict[str, Any]:
        """Per judge dimension, over the cases the judge actually scored.

        Each dimension keeps its own mean. There is no blended judge score here for the same
        reason there is no overall score anywhere else.
        """
        outcomes = [result.judge for result in self.results if result.judge is not None]
        scored = [outcome.verdict for outcome in outcomes if outcome.verdict is not None]
        per_dimension = {
            dimension: {
                "scored": len(scored),
                "mean": _mean([float(getattr(verdict, dimension)) for verdict in scored]),
                "min": min((getattr(verdict, dimension) for verdict in scored), default=None),
                "max": max((getattr(verdict, dimension) for verdict in scored), default=None),
            }
            for dimension in JUDGE_DIMENSIONS
        }
        return {
            "attempted": len(outcomes),
            "scored": len(scored),
            "errored": len(outcomes) - len(scored),
            "errors": [
                {"case_id": result.case_id, "error": result.judge.error}
                for result in self.results
                if result.judge is not None and result.judge.error is not None
            ],
            "dimensions": per_dimension,
        }

    def run_statistics(self) -> dict[str, Any]:
        """What the evaluated jobs cost, from the metadata they carried.

        Reported and never scored. Latency is a property of the run that produced the output,
        not of the answer's quality, and guidelines §14 already owns the latency targets - a
        second set of numbers here would be the duplicate telemetry that section forbids.
        """
        metadata = [result.metadata for result in self.results if result.metadata is not None]
        latencies = [item.latency_seconds for item in metadata if item.latency_seconds is not None]
        calls = [item.llm_calls_used for item in metadata if item.llm_calls_used is not None]
        revisions = [item.revision_count for item in metadata if item.revision_count is not None]
        return {
            "outputs_with_metadata": len(metadata),
            "latency_seconds": {
                "n": len(latencies),
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
                "max": max(latencies, default=None),
            },
            "llm_calls_used": {
                "n": len(calls),
                "p50": _percentile([float(value) for value in calls], 0.50),
                "max": max(calls, default=None),
            },
            "revision_count": {
                "n": len(revisions),
                "max": max(revisions, default=None),
            },
            "failure_reasons": dict(
                sorted(
                    Counter(item.failure_reason for item in metadata if item.failure_reason).items()
                )
            ),
            "output_sources": dict(sorted(Counter(item.source for item in metadata).items())),
        }

    def summary_lines(self) -> list[str]:
        """The terminal view: what ran, what each metric said, and what broke."""
        counts = self.counts()
        lines = [
            f"eval run {self.run_id}",
            f"  benchmark   : {self.benchmark_path} ({self.benchmark_version})"
            f"{'' if self.split is None else f', split {self.split}'}",
            f"  judge       : {self._judge_line()}",
            f"  cases       : {counts['total']} total | {counts['evaluated']} evaluated | "
            f"{counts['failed']} failed | {counts['skipped']} skipped | "
            f"{counts['errored']} errored",
            f"  unparseable : {counts['benchmark_problems']} benchmark row(s)",
            "",
            "  metric                       mean   scored  n/a  pass  fail",
        ]
        for name, values in self.metric_aggregates().items():
            lines.append(
                f"    {name:<26} {_number(values['mean']):>5}  {values['scored']:>6} "
                f"{values['not_applicable']:>4} {values['passed']:>5} {values['failed']:>5}"
            )

        judge = self.judge_aggregates()
        if judge["attempted"]:
            lines += ["", f"  judge scored {judge['scored']} of {judge['attempted']} attempted"]
            lines += [
                f"    {dimension:<26} {_number(values['mean']):>5}  (1-5)"
                for dimension, values in judge["dimensions"].items()
            ]

        problems = [
            f"    {result.case_id:<28} {result.error}"
            for result in self.results
            if result.status in ("errored", "skipped")
        ] + [f"    {problem.case_id:<28} {problem.problem}" for problem in self.problems]
        if problems:
            lines += ["", "  cases that did not run:", *problems]

        failures = [
            f"    {result.case_id:<28} {', '.join(result.failed_metrics)}"
            for result in self.results
            if result.status == "failed"
        ]
        if failures:
            lines += ["", "  cases with a failing metric:", *failures]

        lines += [
            "",
            "  No threshold was applied and no gate was evaluated: this run reports, it does",
            "  not judge the repository (Block C calibrates thresholds against these numbers).",
        ]
        return lines

    def _judge_line(self) -> str:
        if not self.judge_enabled:
            return "disabled (deterministic metrics only, no provider call)"
        return f"{self.judge_model} @ {self.judge_base_url} | rubric {self.judge_rubric_version}"


# --- Writing it out -------------------------------------------------------------------


def write_json(run: EvalRun, path: Path) -> Path:
    """The machine-readable report. Indented, because it is read by people too, and a
    one-line JSON document produces a diff nobody can review."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run.to_json(), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


CSV_COLUMNS = (
    "run_id",
    "case_id",
    "split",
    "category",
    "difficulty",
    "provenance",
    "case_status",
    "metric",
    "score",
    "passed",
    "explanation",
)
"""One row per case per metric - long rather than wide, so a new metric adds rows rather than
changing every consumer's column layout."""


def write_csv(run: EvalRun, path: Path) -> Path:
    """The same results, flattened, for a spreadsheet or a quick `sort | uniq -c`.

    Deliberately does **not** carry `details`: those are nested structures, and flattening them
    into a cell produces something that is neither readable nor parseable. The JSON report is
    the complete record and this is the convenience view.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for result in run.results:
            if not result.metrics:
                writer.writerow(
                    [
                        run.run_id,
                        result.case_id,
                        result.split,
                        result.category,
                        result.difficulty,
                        result.provenance,
                        result.status,
                        "",
                        "",
                        "",
                        result.error or "",
                    ]
                )
                continue
            for metric in result.metrics:
                writer.writerow(
                    [
                        run.run_id,
                        result.case_id,
                        result.split,
                        result.category,
                        result.difficulty,
                        result.provenance,
                        result.status,
                        metric.metric,
                        "" if metric.score is None else f"{metric.score:.4f}",
                        "" if metric.passed is None else str(metric.passed).lower(),
                        metric.explanation,
                    ]
                )
    return path


# --- Arithmetic -----------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    """`None` rather than 0.0 for an empty set, so "nothing was measured" cannot be read as
    "everything scored zero"."""
    return round(sum(values) / len(values), 4) if values else None


def _percentile(values: Iterable[float], fraction: float) -> float | None:
    """Nearest-rank, so every percentile is a real observation.

    The same choice `scripts/measure_jobs.py` makes, and for the same reason: at these sample
    sizes an interpolated p95 is an invented number.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(fraction * len(ordered)))
    return round(ordered[rank - 1], 3)


def _number(value: float | None) -> str:
    return "  -  " if value is None else f"{value:.2f}"
