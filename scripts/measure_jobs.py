"""
WHY THIS FILE EXISTS
    Step 12 (ARCHITECTURE.md §21). Six numbers in gl §14 and three rows in gl §13 are
    estimates, and both documents say so. This script runs real jobs against the real
    endpoint and the real web, records what each one actually cost, and turns those
    estimates into a measured baseline.

    It is a measurement harness, not a feature. Nothing here belongs in production, and it
    reaches the graph only through seams that already exist:

      * `build_graph(cache=...)` - the `ToolCache` protocol. Here a dict-backed cache that
        counts its own gets, hits, and misses. Because `search()` and `fetch()` both call
        `cache.get()` before doing anything else, the same object doubles as a live probe
        for "is this job searching or fetching right now?" without a line of production
        code emitting progress events.

    Four decisions worth reading before the code.

    **Jobs run one at a time.** gl §13: a single job can consume a full minute of the 40
    RPM development tier, so two concurrent jobs saturate it. Concurrency here would
    measure the rate limiter rather than the system.

    **One `LLMClient` for the whole run**, built in `main()` and handed to every job. It
    holds no job state, and `LLMClient.__init__` applies `wrap_openai` when tracing is on -
    a wrapper that patches the client in place and does not refuse to do it twice. Building
    one per job therefore stacked a layer per job and inflated the traced token totals; the
    note at the construction site carries the detail.

    **The human gate is auto-approved.** That is a measurement-harness decision, taken so
    the export path is measured too. It is emphatically **not** a production bypass: the
    gate node, its `interrupt()`, and the export gate all run unchanged, and the approval
    is injected from this script the way Phase 2's endpoint will inject a reviewer's.

    **A crashed job is data.** It is written to the JSONL with its failure and the run
    continues. First contact with the real web is meant to produce failures; losing them
    would defeat the point.

    Every row lands in `measurements/jobs.jsonl` the moment its job finishes, so a stall
    or a Ctrl-C at job 17 loses nothing, and a re-run skips questions already recorded.
    That directory is gitignored: the rows carry fetched third-party page text and report
    bodies, and `run.log` carries every query and URL the tools logged at INFO. Only the
    summary table is published, into the docs.

    **Per-request detail comes from LangSmith, not from this file.** There was a
    `_CallLog` telemetry sink here writing `measurements/requests.jsonl`; it is gone,
    along with the `CallRecord` machinery in `llm_client.py` that fed it. Per-call latency,
    the provider's token usage, retries, and which node made a request are all in the
    trace, so `prompt_tokens` and `completion_tokens` are no longer recorded in the row -
    they are `None`, and `--summary` says where to read them instead.

WHO CALLS IT
    A person, in this order:

        python scripts/measure_jobs.py --limit 1     # one smoke job, then stop and read it
        python scripts/measure_jobs.py               # the remaining jobs, resumable
        python scripts/measure_jobs.py --summary     # the table, from the JSONL, no network

    Needs the same real credentials `check_model.py` does. Exit code 0 means every job this
    invocation attempted ended `approved`; 1 means at least one did not, or `--summary`
    found no data to summarise.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import threading
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from langgraph.types import Command

from config import Config, load_config, required
from graph.build import (
    ResearchGraph,
    build_graph,
)
from graph.state import (
    ResearchState,
    new_state,
    run_config,
)
from llm_client import LLMClient
from schemas import Report, Verdict

logger = logging.getLogger(__name__)

_MEASUREMENTS_DIR = Path("measurements")
_RESULTS_PATH = _MEASUREMENTS_DIR / "jobs.jsonl"
_LOG_PATH = _MEASUREMENTS_DIR / "run.log"

_USER_ID = "measurement-harness"
"""Single tenant today, so this is only ever a label. It names where the job came from."""

_HEARTBEAT_S = 15.0
"""How often to print "still going" while a node runs. `stream_mode="updates"` reports a
node only after it finishes, so without this a 90-second Researcher step and a dead
terminal look identical."""

_QUESTION_CHARS = 72
"""Questions are printed truncated, so one job start is one console line."""

# The price assumption the derived cost is stated under. CLAUDE.md names the production
# LLM row as "Production LLM API" without naming the model, so there is no real price to
# read - these are a placeholder, deliberately visible and deliberately printed with every
# number derived from them. Development spend is $0: NIM's free tier bills nothing.
# Replace both when the production model is chosen, then re-run --summary.
_PRICE_PER_M_INPUT_USD = 1.00
_PRICE_PER_M_OUTPUT_USD = 3.00

MEASUREMENT_QUESTIONS: tuple[tuple[str, str], ...] = (
    # These are measurement inputs, NOT the evaluation dataset. There is no expected
    # evidence attached and nothing here is graded - the eval set is 30-50 questions with
    # expected evidence, it lives in eval/, and it arrives in Phase 4 (gl §15). What these
    # 20 have to do is exercise the three question shapes gl §15 names against pages that
    # really exist, so the baseline is measured on the work the system is actually for.
    ("comparison", "Compare TCS and Infosys on cloud strategy"),
    ("comparison", "Compare Snowflake and Databricks on data warehousing"),
    ("comparison", "Compare Stripe and Adyen on enterprise payment infrastructure"),
    ("comparison", "Compare Zoom and Microsoft Teams on enterprise video conferencing"),
    ("comparison", "Compare Shopify and BigCommerce on enterprise e-commerce platforms"),
    ("comparison", "Compare Datadog and New Relic on observability products"),
    ("comparison", "Compare Notion and Atlassian Confluence on team knowledge management"),
    ("event_tracking", "What has OpenAI launched in the last 12 months?"),
    ("event_tracking", "What products has Anthropic released in the last 12 months?"),
    ("event_tracking", "What has Salesforce announced about its AI products since 2025?"),
    ("event_tracking", "What acquisitions has ServiceNow made in the last 12 months?"),
    ("event_tracking", "What has Nvidia announced in data center GPUs in the last 12 months?"),
    ("event_tracking", "What has Cloudflare launched for developers in the last 12 months?"),
    ("event_tracking", "What pricing and licensing changes has HashiCorp made since 2023?"),
    ("threat_analysis", "What are the competitive threats to Wix?"),
    ("threat_analysis", "What are the competitive threats to Twilio?"),
    ("threat_analysis", "What are the competitive threats to Okta in identity management?"),
    ("threat_analysis", "What are the competitive threats to Elastic in search and observability?"),
    ("threat_analysis", "What are the competitive threats to Unity in game engines?"),
    ("threat_analysis", "What are the competitive threats to DocuSign in e-signature?"),
)


# --- Recording the two things a job's own state does not carry ---------------------


class _MeasuringCache:
    """The `ToolCache` protocol, backed by a dict, counting what it was asked for.

    One instance per job, not one per run. Two reasons: gl §14's cache-hit target is about
    a revision re-issuing a query inside one job, and a per-job cache measures the same
    thing whether the 20 jobs ran in one invocation or five - which matters, because this
    runner is resumable. What it measures is **process-local cache reuse**, and it must
    never be published as a Redis or cross-worker number.

    `ttl_seconds` is accepted and ignored. The entries live for one job, which is far
    shorter than the 24h TTL the tools ask for, so honouring it could not change an answer.
    """

    def __init__(self) -> None:
        self._entries: dict[str, str] = {}
        self.hits = 0
        self.misses = 0
        self._since_last_node: Counter[str] = Counter()

    def get(self, key: str) -> str | None:
        self._since_last_node[_activity(key)] += 1
        stored = self._entries.get(key)
        if stored is None:
            self.misses += 1
        else:
            self.hits += 1
        return stored

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._entries[key] = value

    def take_activity(self) -> Counter[str]:
        """What the tools did since the last time this was asked, then reset.

        Both tools look in the cache before they act, so counting lookups by key prefix is
        a count of searches and fetches - which is how the progress line can say "searching,
        fetching x3" without production code emitting anything.
        """
        taken = self._since_last_node
        self._since_last_node = Counter()
        return taken


def _activity(key: str) -> str:
    """`cache:search:{hash}` -> `search`. Anything else is counted under its own name."""
    parts = key.split(":")
    return parts[1] if len(parts) > 2 and parts[0] == "cache" else key


# --- One measured job --------------------------------------------------------------


@dataclass(frozen=True)
class _JobResult:
    """One row of `measurements/jobs.jsonl`. Every published number traces to a field here."""

    question: str
    shape: str
    status: str
    failure_reason: str | None
    recorded_at: str  # so a published number can state its measurement date
    model: str  # so a published cost can state which model produced the tokens
    wall_seconds: float
    node_seconds: dict[str, float]
    llm_calls_used: int
    hop_count: int
    revision_count: int
    quality_flag: str | None
    prompt_tokens: int | None  # None since telemetry was removed; read them in LangSmith
    completion_tokens: int | None
    findings: int
    claims: int
    sources: int
    supported: int
    unsupported: int
    unresearched_subtopics: int
    cache_hits: int
    cache_misses: int


class _Heartbeat:
    """Prints elapsed while a node runs, so a slow job and a dead terminal look different.

    A background thread is the only way to print anything during a node: the stream call
    blocks in the main thread until the node returns. It reports time since the last node
    *finished*, which is the honest statement - what is running now is not observable
    without production code emitting custom events, and that is out of scope for step 12.
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._stop = threading.Event()
        # One tuple, assigned in one statement, so the reader thread never sees a node
        # paired with another node's timestamp. Cheaper than a lock and just as correct.
        self._at: tuple[str, float] = ("start", time.monotonic())
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def mark(self, node: str) -> None:
        self._at = (node, time.monotonic())

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.wait(_HEARTBEAT_S):
            node, since = self._at
            _emit(f"{self._prefix} ...          | {time.monotonic() - since:6.1f}s since {node}")


class _Progress:
    """The live view of one job, and the per-node timings that outlive it.

    Node latency is the gap between two `stream_mode="updates"` events, which is the node
    plus whatever the graph did around it. That is the right unit here: it sums to the wall
    clock, which is the number gl §14 sets a target on.
    """

    def __init__(self, index: int, total: int, budget: int) -> None:
        self._prefix = f"[{index:02d}/{total:02d}]"
        self._budget = budget
        self._started = time.monotonic()
        self._last_event = self._started
        self._revision = 0
        self._heartbeat = _Heartbeat(self._prefix)
        self.node_seconds: dict[str, float] = {}
        # Read off each node's state update rather than counted per request. It is the
        # number invariant 3 is actually enforced against, and it is exact at the moments
        # a line is printed - which is every moment this is read.
        self.calls = 0

    def start(self, question: str) -> None:
        _emit(f"{self._prefix} START        | {_short(question)}")
        self._heartbeat.start()

    def node(self, name: str, update: object, activity: Counter[str]) -> None:
        now = time.monotonic()
        self.node_seconds[name] = self.node_seconds.get(name, 0.0) + (now - self._last_event)
        self._last_event = now
        self._heartbeat.mark(name)

        if isinstance(update, dict):
            self._revision = int(update.get("revision_count", self._revision))
            self.calls = int(update.get("llm_calls_used", self.calls))
        if name == "human_gate":
            # Its line was already printed when the interrupt arrived. Its time still counts.
            return

        details = [_activity_summary(activity), _score_summary(update)]
        suffix = "".join(f" | {detail}" for detail in details if detail)
        _emit(f"{self._prefix} {name:<12} | {self._line()}{suffix}")

    def gate(self) -> None:
        _emit(f"{self._prefix} human_gate   | auto-approved for measurement")

    def finish(self, status: str, detail: str, runtime_bound: int) -> None:
        self._heartbeat.stop()
        label = "COMPLETE" if status == "approved" else "FAILED"
        # MAX_JOB_RUNTIME is configured and nothing enforces it (config.py), so the harness
        # reports the overrun rather than causing it. Saying "not enforced" on the line keeps
        # the run honest about which numbers stopped the job and which only watched it.
        over = "" if self.wall_seconds <= runtime_bound else " | OVER MAX_JOB_RUNTIME"
        _emit(f"{self._prefix} {label:<12} | {self._line()} | {detail}{over}")

    @property
    def wall_seconds(self) -> float:
        return time.monotonic() - self._started

    def _line(self) -> str:
        return (
            f"{self.wall_seconds:6.1f}s | calls {self.calls:2d}/{self._budget} "
            f"| rev {self._revision}"
        )


def run_job(
    index: int,
    total: int,
    shape: str,
    question: str,
    *,
    config: Config,
    llm: LLMClient,
) -> _JobResult:
    """Run one job end to end and return its row. Never raises: a crash is a row too.

    The client is built once by `main()` and shared by every job. It holds no job state, and
    building one per job is what nested the LangSmith spans - see the note there.
    """
    cache = _MeasuringCache()
    compiled = build_graph(config=config, llm=llm, cache=cache)
    job_id = f"measure-{index:02d}"
    settings = run_config(job_id)
    progress = _Progress(index, total, config.max_llm_calls_per_job)
    progress.start(question)

    crash: str | None = None
    try:
        opening = new_state(job_id, _USER_ID, question)
        _drain(compiled.stream(opening, settings, stream_mode="updates"), progress, cache)
        if _paused_at_gate(compiled, settings):
            progress.gate()
            approval: Command[Any] = Command(resume={"decision": "approve"})
            _drain(compiled.stream(approval, settings, stream_mode="updates"), progress, cache)
    except Exception as error:  # noqa: BLE001 - a crashed job is data, not a stopped run
        crash = f"{type(error).__name__}: {error}"
        logger.exception("job %s crashed", job_id)

    state = _final_state(compiled, settings)
    result = _to_result(
        shape=shape,
        question=question,
        crash=crash,
        state=state,
        config=config,
        progress=progress,
        cache=cache,
    )
    progress.finish(result.status, result.failure_reason or result.status, config.max_job_runtime)
    return result


def _drain(stream: Iterator[Any], progress: _Progress, cache: _MeasuringCache) -> None:
    """Consume one stream, reporting each node as it finishes."""
    for event in stream:
        if not isinstance(event, dict):
            continue
        for name, update in event.items():
            # The interrupt surfaces here too, but it is not a node and it is not what the
            # resume decision is taken from - see _paused_at_gate.
            if name != "__interrupt__":
                progress.node(str(name), update, cache.take_activity())


def _paused_at_gate(compiled: ResearchGraph, settings: Any) -> bool:
    """Is the job sitting at the gate's `interrupt()`, waiting for a reviewer?

    Asked of the checkpoint rather than inferred from a stream event. The checkpoint is what
    actually holds the job, so this stays true regardless of how interrupts are surfaced in
    the update stream - and a missed resume would leave the export path unmeasured, which is
    half of what this run exists to measure.
    """
    return bool(compiled.get_state(settings).next)


def _final_state(compiled: ResearchGraph, settings: Any) -> ResearchState | None:
    """The state the job ended on, or None when even reading it failed."""
    try:
        return cast(ResearchState, compiled.get_state(settings).values)
    except Exception as error:  # noqa: BLE001 - a job whose state is unreadable is still a row
        logger.error("could not read the final state: %s", error)
        return None


def _to_result(
    *,
    shape: str,
    question: str,
    crash: str | None,
    state: ResearchState | None,
    config: Config,
    progress: _Progress,
    cache: _MeasuringCache,
) -> _JobResult:
    """Everything measured about one job, in one row.

    A crash outranks whatever status the state happens to hold: the graph did not finish,
    so calling the job by its last written status would report a job that never ended.
    """
    report: Report | None = state["report"] if state else None
    verdicts: list[Verdict] = state["verdicts"] if state else []
    statuses: list[str] = list(state["subtopic_status"].values()) if state else []

    return _JobResult(
        question=question,
        shape=shape,
        status="crashed" if crash else (state["status"] if state else "unknown"),
        failure_reason=crash or (state["failure_reason"] if state else "no final state"),
        recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
        model=required(config.llm_model, "LLM_MODEL"),
        wall_seconds=round(progress.wall_seconds, 1),
        node_seconds={name: round(value, 1) for name, value in progress.node_seconds.items()},
        # State is the authority while there is one - it is the number invariant 3 is
        # enforced against. A crash that lost the state falls back to the last update seen.
        llm_calls_used=state["llm_calls_used"] if state else progress.calls,
        hop_count=state["hop_count"] if state else 0,
        revision_count=state["revision_count"] if state else 0,
        quality_flag=state["quality_flag"] if state else None,
        # Not counted here any more - the provider's usage numbers are on the LangSmith
        # spans. None rather than 0, because "not recorded" and "zero tokens" differ.
        prompt_tokens=None,
        completion_tokens=None,
        findings=len(state["findings"]) if state else 0,
        claims=len(report.claims) if report else 0,
        sources=len(report.sources) if report else 0,
        supported=sum(1 for verdict in verdicts if verdict.supported),
        unsupported=sum(1 for verdict in verdicts if not verdict.supported),
        unresearched_subtopics=sum(1 for value in statuses if value == "unresearched"),
        cache_hits=cache.hits,
        cache_misses=cache.misses,
    )


# --- The summary -------------------------------------------------------------------


def summarize(rows: list[dict[str, Any]]) -> list[str]:
    """The published table, rebuilt from the JSONL alone.

    Takes dicts rather than `_JobResult` on purpose: the summary has to be reproducible
    from the file long after the run, without re-running anything.
    """
    latencies = [_number(row, "wall_seconds") for row in rows]
    calls = [_number(row, "llm_calls_used") for row in rows]
    # Rows recorded before the telemetry was removed carry their own token counts; rows
    # recorded after it carry None and are read in LangSmith instead. Only the rows that
    # actually have the numbers are averaged, so a missing count never becomes a zero.
    counted = [row for row in rows if row.get("prompt_tokens") is not None]
    prompt = [_number(row, "prompt_tokens") for row in counted]
    completion = [_number(row, "completion_tokens") for row in counted]
    tokens = [p + c for p, c in zip(prompt, completion, strict=True)]
    approved = [row for row in rows if row.get("status") == "approved"]
    revised = [row for row in rows if _number(row, "revision_count") > 0]
    hits = sum(_number(row, "cache_hits") for row in rows)
    lookups = hits + sum(_number(row, "cache_misses") for row in rows)
    dates = sorted(str(row.get("recorded_at", ""))[:10] for row in rows)

    p50_prompt = _percentile(prompt, 0.50)
    p50_completion = _percentile(completion, 0.50)
    cost = _cost(p50_prompt, p50_completion)
    token_lines = (
        [
            f"  tokens per job        : p50 {_percentile(tokens, 0.50):,.0f}, "
            f"max {max(tokens):,.0f}  (n={len(counted)} of {len(rows)} rows)",
            f"     prompt             : p50 {p50_prompt:,.0f}, max {max(prompt):,.0f}",
            f"     completion         : p50 {p50_completion:,.0f}, max {max(completion):,.0f}",
        ]
        if counted
        else ["  tokens per job        : not recorded in these rows - read them in LangSmith"]
    )
    cost_lines = (
        [
            f"  derived production cost = ${cost:.2f}/job",
            f"    (assumption: {p50_prompt:,.0f} input @ ${_PRICE_PER_M_INPUT_USD:.2f}/M",
            f"               + {p50_completion:,.0f} output @ ${_PRICE_PER_M_OUTPUT_USD:.2f}/M,",
            "                 assumed production-model prices - NOT provider spend)",
        ]
        if counted
        else ["  derived production cost = not derivable without token counts (see LangSmith)"]
    )

    lines = [
        f"measurements from {_RESULTS_PATH}",
        f"  sample      : n={len(rows)} jobs, recorded {dates[0]} to {dates[-1]}",
        f"  model       : {', '.join(sorted({str(row.get('model', '?')) for row in rows}))}",
        "",
        f"  success rate          : {len(approved)}/{len(rows)} "
        f"({_share(len(approved), len(rows))}) reached approved",
        f"  job latency p50       : {_duration(_percentile(latencies, 0.50))}",
        f"  job latency p95       : {_duration(_percentile(latencies, 0.95))}  "
        f"(n={len(rows)}: effectively one extreme observation, not a distribution)",
        f"  llm calls per job     : p50 {_percentile(calls, 0.50):.0f}, max {max(calls):.0f}",
        *token_lines,
        f"  cache hit rate        : {_share(hits, lookups)} "
        f"({hits:.0f} hits of {lookups:.0f} lookups; process-local reuse within one job)",
        f"  revision rate         : {_share(len(revised), len(rows))} "
        f"({len(revised)} of {len(rows)} jobs ran at least one revision; "
        "rubric is uncalibrated - a regression baseline, not a quality claim)",
        "",
        "  failures by reason:",
        *_failure_lines(rows),
        "",
        f"  per-node share of {_duration(sum(latencies))} total wall clock:",
        *_node_lines(rows, sum(latencies)),
        "",
        *cost_lines,
        "  actual NIM development spend = $0",
    ]
    return lines


def _failure_lines(rows: list[dict[str, Any]]) -> list[str]:
    reasons = Counter(
        str(row.get("failure_reason") or row.get("status"))
        for row in rows
        if row.get("status") != "approved"
    )
    if not reasons:
        return ["    none - every job reached approved"]
    return [f"    {reason:<28} x{count}" for reason, count in reasons.most_common()]


def _node_lines(rows: list[dict[str, Any]], total_seconds: float) -> list[str]:
    """Share of total wall clock per node. The remainder is time outside any node."""
    per_node: dict[str, float] = {}
    for row in rows:
        seconds = row.get("node_seconds")
        if isinstance(seconds, dict):
            for name, value in seconds.items():
                per_node[str(name)] = per_node.get(str(name), 0.0) + float(value)
    if not per_node or total_seconds <= 0:
        return ["    no node timings recorded"]
    ranked = sorted(per_node.items(), key=lambda item: item[1], reverse=True)
    return [
        f"    {name:<14} {value / total_seconds * 100:5.1f}%  ({_duration(value)})"
        for name, value in ranked
    ]


def _cost(prompt_tokens: float, completion_tokens: float) -> float:
    """Derived, never observed. The arithmetic is printed next to the answer."""
    return (
        prompt_tokens / 1_000_000 * _PRICE_PER_M_INPUT_USD
        + completion_tokens / 1_000_000 * _PRICE_PER_M_OUTPUT_USD
    )


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank, so p50 and p95 are both real observations rather than interpolations.

    At n=20 that puts p95 on the 19th of 20 values, which is why it is published as a
    baseline and not as a distribution estimate.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def _number(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _share(part: float, whole: float) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "n/a"


def _duration(seconds: float) -> str:
    return f"{seconds:.0f}s ({int(seconds) // 60}m{int(seconds) % 60:02d}s)"


# --- Console and files -------------------------------------------------------------


def _emit(line: str) -> None:
    """Flushed immediately: a progress view that arrives in blocks is not a progress view."""
    print(line, flush=True)


def _short(question: str) -> str:
    return question if len(question) <= _QUESTION_CHARS else f"{question[:_QUESTION_CHARS]}..."


def _activity_summary(activity: Counter[str]) -> str:
    """`{"search": 1, "fetch": 3}` -> `searching, fetching x3`."""
    verbs = {"search": "searching", "fetch": "fetching"}
    return ", ".join(
        f"{verbs.get(name, name)}{f' x{count}' if count > 1 else ''}"
        for name, count in sorted(activity.items())
    )


def _score_summary(update: object) -> str:
    """The reflection node's own numbers, read out of the update it just wrote.

    Read with getattr rather than attribute access: a progress line must never be the thing
    that ends a measured job, so an update shaped differently than expected costs the line.
    """
    if not isinstance(update, dict):
        return ""
    scores = update.get("reflection_scores")
    if isinstance(scores, list) and scores:
        weighted = getattr(scores[-1], "weighted_score", None)
        if isinstance(weighted, (int, float)):
            return f"score {weighted:.2f} -> {getattr(scores[-1], 'route', '?')}"
    flag = update.get("quality_flag")
    return f"quality_flag {flag}" if flag else ""


def _configure_logging() -> None:
    """Application logging to a file, never the console.

    Two reasons, and the second is the load-bearing one: the progress view has to stay
    readable, and `tools/search.py` and `tools/fetch.py` log queries and URLs at INFO -
    which is exactly the fetched-content exposure the console must not carry. The file is
    gitignored for the same reason.
    """
    handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _append(result: _JobResult) -> None:
    """One row, opened and closed per job, so a Ctrl-C at job 17 loses nothing."""
    with _RESULTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(result)) + "\n")


def _load_results() -> list[dict[str, Any]]:
    if not _RESULTS_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in _RESULTS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            _emit(f"skipping an unreadable row in {_RESULTS_PATH}")
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# --- Entry point -------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure real research jobs (step 12).")
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print the table from measurements/jobs.jsonl and exit; makes no network calls",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="run at most this many jobs this invocation. --limit 1 is the smoke job",
    )
    args = parser.parse_args()

    if args.summary:
        rows = _load_results()
        if not rows:
            _emit(f"no measurements in {_RESULTS_PATH}; run the jobs first")
            return 1
        for line in summarize(rows):
            _emit(line)
        return 0

    try:
        config = load_config()
    except ValueError as error:
        _emit(f"configuration error: {error}")
        return 1

    _MEASUREMENTS_DIR.mkdir(exist_ok=True)
    _configure_logging()

    # One client for the whole run, built here rather than per job. `LLMClient.__init__`
    # applies `wrap_openai` when tracing is on, and that wrapper patches the OpenAI object in
    # place with no idempotency guard - so a client built per job wrapped the same underlying
    # object again each time, and job N emitted N nested spans per request whose outermost
    # carried a running token total. Behaviour was never affected (one HTTP request per
    # counted call, and CallBudget counts requests), but a token figure read from a multi-job
    # trace was inflated by the job's ordinal. It holds no job state, so sharing it is also
    # what `LLMClient` says it is for: "one instance per process is enough".
    #
    # It builds its own OpenAI client with max_retries=0, which is the setting this run needs
    # anyway: the SDK's own retry would multiply every schedule in gl §17 and the measurement
    # has to see the retry behaviour that ships. Handing in a client built here would only be
    # a second place to get that wrong.
    llm = LLMClient(config)

    recorded = {str(row.get("question", "")) for row in _load_results()}
    total = len(MEASUREMENT_QUESTIONS)
    _emit(f"endpoint: {config.llm_base_url}")
    _emit(f"main model: {config.llm_model}   fast model: {config.llm_fast_model}")
    _emit(f"results: {_RESULTS_PATH} ({len(recorded)} of {total} already recorded)")
    _emit(f"logs: {_LOG_PATH}")
    _emit(
        f"limits: main timeout {config.llm_main_timeout_s:.0f}s | "
        f"revisions {config.max_revisions} | hops {config.max_supervisor_hops} | "
        f"calls {config.max_llm_calls_per_job} | "
        f"runtime {config.max_job_runtime}s (configured, NOT enforced)\n"
    )

    ran = 0
    unfinished = 0
    for index, (shape, question) in enumerate(MEASUREMENT_QUESTIONS, start=1):
        if question in recorded:
            _emit(f"[{index:02d}/{total:02d}] SKIP         | already recorded")
            continue
        if args.limit is not None and ran >= args.limit:
            break
        result = run_job(index, total, shape, question, config=config, llm=llm)
        _append(result)
        ran += 1
        if result.status != "approved":
            unfinished += 1

    _emit(f"\n{ran} job(s) run, {unfinished} did not reach approved")
    _emit(f"python scripts/measure_jobs.py --summary rebuilds the table from {_RESULTS_PATH}")
    return 1 if unfinished else 0


if __name__ == "__main__":
    raise SystemExit(main())
