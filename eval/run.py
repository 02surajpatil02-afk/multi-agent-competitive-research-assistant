"""
WHY THIS FILE EXISTS
    `python -m eval.run` - the only entry point into the evaluation subsystem. It loads a
    benchmark, loads each case's already-produced research output, runs the twelve
    deterministic metrics, optionally asks the judge, and writes one JSON report.

    Four things it does deliberately.

    **It defaults to costing nothing.** No `--judge`, no provider call, no credential, no
    network. That is what lets the whole thing run in CI and in a test, and it is why the
    judge's configuration is only read when the judge is switched on.

    **One bad case cannot end the run.** Every per-case step is inside one `try`, and a failure
    becomes an `errored` result carrying the reason. A benchmark row that does not parse, a
    fixture that is missing, a job id that is not in the database - all three are results.

    **It never exits non-zero because a metric scored badly.** The exit code answers "did the
    evaluation run?", not "is the research good enough?". There is no threshold here and there
    is not meant to be one yet: Block C picks thresholds against the distribution this produces,
    and a gate built before that distribution exists is a number somebody guessed
    (docs/evaluation.md, "Why CI quality thresholds are deferred").

    **The report goes to `measurements/` by default**, which is gitignored, for the same reason
    `scripts/measure_jobs.py` writes there: the per-case details quote report text derived from
    third-party pages. What gets published is the summary, into the docs, by a person.

WHO CALLS IT
    A person:

        python -m eval.run                                   # DEV, deterministic only
        python -m eval.run --csv                             # and a flat CSV beside it
        python -m eval.run --judge --judge-model <id>         # plus the five judge dimensions

    and tests/test_eval_runner.py, which drives `main()` with a temporary benchmark.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import Engine

from config import Config, load_config, required
from database.queries import create_database_engine
from eval.judge import JUDGE_RUBRIC_VERSION, Judge, JudgeOutcome
from eval.metrics import evaluate_deterministic
from eval.outputs import OutputError, ResearchOutput, load_output_file, load_output_from_database
from eval.report import CaseResult, CaseStatus, EvalRun, write_csv, write_json
from eval.schema import Benchmark, BenchmarkError, EvalCase, load_benchmark
from llm_client import LLMClient

logger = logging.getLogger(__name__)

DEFAULT_BENCHMARK = Path("eval/benchmarks/dev.json")
DEFAULT_OUTPUT_DIR = Path("measurements/eval")
"""Gitignored, like every other measured artefact in this repository. See the module docstring."""


def main(argv: list[str] | None = None) -> int:
    """Run one evaluation. `0` means the run completed, whatever the scores were."""
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level)

    try:
        benchmark = load_benchmark(args.benchmark)
    except BenchmarkError as error:
        _emit(f"benchmark error: {error}")
        return 1

    cases = benchmark.select(
        split=args.split,
        case_ids=frozenset(args.case) if args.case else None,
        tags=frozenset(args.tag) if args.tag else None,
    )
    if not cases:
        _emit(f"no cases selected from {args.benchmark}; nothing to evaluate")
        return 1

    try:
        judge = _build_judge(args) if args.judge else None
    except ValueError as error:
        _emit(f"judge configuration error: {error}")
        return 1

    engine = create_database_engine(args.from_database) if args.from_database else None

    started_at = datetime.now(UTC)
    results = tuple(
        _evaluate_case(case, benchmark=benchmark, engine=engine, judge=judge) for case in cases
    )
    run = EvalRun(
        run_id=args.run_id or _run_id(started_at),
        started_at=started_at,
        finished_at=datetime.now(UTC),
        benchmark_path=str(args.benchmark),
        benchmark_version=benchmark.version,
        split=args.split,
        judge_enabled=judge is not None,
        judge_model=args.judge_model if judge is not None else None,
        judge_base_url=args.judge_base_url if judge is not None else None,
        judge_rubric_version=JUDGE_RUBRIC_VERSION if judge is not None else None,
        results=results,
        problems=benchmark.problems,
        selection={
            "cases": sorted(args.case) or None,
            "tags": sorted(args.tag) or None,
            "input_mode": "database" if engine is not None else "fixture",
        },
    )

    report_path = write_json(run, args.out / f"{run.run_id}.json")
    for line in run.summary_lines():
        _emit(line)
    _emit("")
    _emit(f"  report      : {report_path}")
    if args.csv:
        _emit(f"  csv         : {write_csv(run, args.out / f'{run.run_id}.csv')}")
    return 0


# --- One case -------------------------------------------------------------------------


def _evaluate_case(
    case: EvalCase, *, benchmark: Benchmark, engine: Engine | None, judge: Judge | None
) -> CaseResult:
    """Everything for one case, with every failure isolated to this case.

    The bare `except Exception` is the error-isolation rule, not carelessness: an evaluator is
    a measuring instrument, and one that stops measuring because case 9 was strange has lost
    the twenty-three results it already had. The reason is recorded on the result.
    """
    try:
        output = _load_output(case, benchmark=benchmark, engine=engine)
    except _CaseSkipped as skipped:
        return _bare(case, "skipped", str(skipped))
    except OutputError as error:
        return _bare(case, "errored", str(error))
    except Exception as error:  # noqa: BLE001 - see the docstring
        logger.exception("case %s could not load its output", case.case_id)
        return _bare(case, "errored", f"{type(error).__name__}: {error}")

    try:
        metrics = tuple(evaluate_deterministic(output, case))
    except Exception as error:  # noqa: BLE001 - one broken metric must not end the run
        logger.exception("case %s failed during deterministic evaluation", case.case_id)
        return _bare(case, "errored", f"{type(error).__name__}: {error}")

    verdict: JudgeOutcome | None = None
    if judge is not None:
        # `Judge.score` catches every LLM failure itself and returns an outcome carrying the
        # error, so a judge that cannot be reached costs this case its five dimensions and the
        # twelve deterministic metrics above still stand.
        verdict = judge.score(output, case)

    failed = any(metric.passed is False for metric in metrics)
    return CaseResult(
        case_id=case.case_id,
        split=case.split,
        status="failed" if failed else "evaluated",
        question=case.question,
        category=case.category,
        difficulty=case.difficulty,
        provenance=case.provenance,
        tags=tuple(case.tags),
        output_ref=case.output_ref,
        metrics=metrics,
        judge=verdict,
        metadata=output.metadata,
    )


class _CaseSkipped(RuntimeError):
    """This case has nothing to evaluate in this mode - a choice, not a fault."""


def _load_output(case: EvalCase, *, benchmark: Benchmark, engine: Engine | None) -> ResearchOutput:
    """The already-produced output this case is scored against.

    Two modes, and the database one skips rather than errors on a case with no `job_id`: a
    fixture-backed case is not broken, it simply is not a real job, and calling that an error
    would make `--from-database` report twenty-four faults on a healthy benchmark.
    """
    if engine is not None:
        if case.job_id is None:
            raise _CaseSkipped("--from-database was used and this case names no job_id")
        return load_output_from_database(engine, case.job_id)
    return load_output_file(benchmark.path.parent / case.output_ref)


def _bare(case: EvalCase, status: CaseStatus, error: str) -> CaseResult:
    """A case that produced no metrics, carrying why."""
    return CaseResult(
        case_id=case.case_id,
        split=case.split,
        status=status,
        question=case.question,
        category=case.category,
        difficulty=case.difficulty,
        provenance=case.provenance,
        tags=tuple(case.tags),
        output_ref=case.output_ref,
        error=error,
    )


# --- The judge, when it is asked for ---------------------------------------------------


def _build_judge(args: argparse.Namespace) -> Judge:
    """One `LLMClient` pointed at the configured judge endpoint, wrapped in a `Judge`.

    **The provider is a base URL and a model id, exactly as it is for every other caller in
    this repository** - there is no provider class here for the same reason `llm_client.py` has
    none (ARCHITECTURE.md §20 row 4). The two values default to the `LLM_*` configuration the
    system already has, so judging with the same model the system runs on needs no extra
    variable; `--judge-model` / `EVAL_JUDGE_MODEL` and `--judge-base-url` / `EVAL_JUDGE_BASE_URL`
    are how a *different* judge is chosen, which is the case worth supporting because a model
    grading its own output is the obvious way to get a flattering number.

    `LLM_API_KEY` is the credential either way, and it is required loudly here rather than at
    the first request.
    """
    config = load_config()
    model = args.judge_model or config.llm_model
    base_url = args.judge_base_url or config.llm_base_url
    if not model:
        raise ValueError("--judge needs a model: set --judge-model, EVAL_JUDGE_MODEL or LLM_MODEL")
    if not base_url:
        raise ValueError(
            "--judge needs an endpoint: set --judge-base-url, EVAL_JUDGE_BASE_URL or LLM_BASE_URL"
        )
    required(config.llm_api_key, "LLM_API_KEY")

    args.judge_model, args.judge_base_url = model, base_url
    return Judge(LLMClient(_judge_config(config, model, base_url)), model=model)


def _judge_config(config: Config, model: str, base_url: str) -> Config:
    """The process configuration with the judge's endpoint substituted in.

    `dataclasses.replace` rather than a second configuration type: everything else the client
    reads - the timeout, the tracing flag, the project - is the same process's configuration,
    and a parallel `JudgeConfig` would be a second place for those to drift. Both model fields
    are set, so a judge call can never silently land on the fast tier's model.
    """
    return replace(config, llm_base_url=base_url, llm_model=model, llm_fast_model=model)


# --- Arguments ------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eval.run",
        description="Score already-produced research outputs against a benchmark (Phase 4).",
    )
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=DEFAULT_BENCHMARK,
        help=f"benchmark file to load (default: {DEFAULT_BENCHMARK})",
    )
    parser.add_argument(
        "--split",
        default=None,
        help="evaluate only this split; omit for every case in the file",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        metavar="CASE_ID",
        help="evaluate only this case; repeatable",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="TAG",
        help="evaluate only cases carrying this tag; repeatable",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"where the report is written (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--csv", action="store_true", help="also write a flat CSV of the results")
    parser.add_argument(
        "--from-database",
        default=None,
        metavar="DATABASE_URL",
        help=(
            "load each case's output from a real job instead of its fixture. Cases with no "
            "job_id are skipped"
        ),
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help="also run the LLM judge. Off by default: this is the only flag that costs money",
    )
    parser.add_argument(
        "--judge-model",
        default=os.environ.get("EVAL_JUDGE_MODEL") or None,
        help="model id for the judge (default: EVAL_JUDGE_MODEL, then LLM_MODEL)",
    )
    parser.add_argument(
        "--judge-base-url",
        default=os.environ.get("EVAL_JUDGE_BASE_URL") or None,
        help="OpenAI-compatible endpoint for the judge (default: EVAL_JUDGE_BASE_URL, "
        "then LLM_BASE_URL)",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="name this run instead of deriving one from the clock",
    )
    parser.add_argument("--log-level", default="WARNING", help="logging level (default: WARNING)")
    return parser.parse_args(argv)


def _run_id(started_at: datetime) -> str:
    return f"eval-{started_at.strftime('%Y%m%d-%H%M%S')}"


def _emit(line: str) -> None:
    print(line, flush=True)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
