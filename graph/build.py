"""
WHY THIS FILE EXISTS
    The nine nodes and the edges between them - the first point at which a whole job runs
    end to end (ARCHITECTURE.md §21 step 10). Everything here is wiring: the agents and the
    reflection node already own their decisions, and this file turns each answer into a
    state update and an edge. A node that re-derived a route the agent had already worked
    out would be two copies of one decision, and the copies would drift.

    Three things are worth reading before the code.

    **How a route travels from a node to an edge.** LangGraph offers exactly two channels:
    a state field, or a `Command`. ARCHITECTURE.md §4.1 rules out the first - "ResearchState
    has no `next`, because the graph's conditional edge consumes the target directly" - so
    the three nodes that choose where a job goes next return `Command(goto=..., update=...)`,
    which is that sentence written in code. `destinations=` on each of them keeps the
    topology declared where the wiring is, so `get_graph()` still draws the documented shape
    and still reports those edges as conditional. Every other edge is fixed `add_edge`.

    **A failed job is not routed, it is finalized.** The four agent nodes reach a router by
    a fixed edge - three to the Supervisor, the Fact-Checker to reflection - so a node that
    has just failed the job hands it to a component whose whole purpose is to send it
    somewhere. The Supervisor would route a plan-less job straight back to the Planner and
    spend the whole hop budget rediscovering the same failure; reflection would score a
    draft whose claims were never checked. Both routers read `status` before they do anything
    else. That check is what makes "empty plan after one retry -> finalize, status=failed"
    true of the graph and not only of the Planner (ARCHITECTURE.md §15).

    **The three documented guards are the only ones that stop a job.** LangGraph has a
    recursion limit of its own, and it raises `GraphRecursionError` - a framework exception
    with no `status`, no `failure_reason`, and nothing a caller can branch on. It is left at
    its default, because it does not need changing: `MAX_SUPERVISOR_HOPS` = 24 bounds the
    longest job to 50 super-steps, and LangGraph's default limit of 1,000 sits well above
    that. A test measures that gap rather than leaving it assumed (guidelines §5).

    **Both durable stores are injected, and both are optional here** (implementation steps
    14-16). `checkpointer` is the Postgres one in any process that has to survive a restart
    - `postgres_checkpointer()` below builds it - and the in-memory one otherwise, which is
    what the offline test suite and the single-process measurement harness run on.
    `db` is the SQLAlchemy engine the nodes write findings, claims, `claim_sources`, and
    audit events through as the job runs; without it the graph behaves exactly as it did in
    Phase 1. Neither is a fallback the code chooses on its own: a caller that wants
    durability passes both, and one that does not gets no surprise.

    **Where a node writes, it writes before it returns.** LangGraph applies a node's update
    and writes the checkpoint after the node returns, so a crash in between leaves the
    database one node ahead of the checkpoint and the redelivered message replays that node
    (ARCHITECTURE.md §11). Every graph-time write is keyed so that replay converges rather
    than duplicating - see database/queries.py, which owns the transaction for each one. A
    write that fails propagates: guidelines §17 gives the database 0 retries, and a job
    whose audit trail silently disagrees with what it did is worse than a loud failure.

    The gate's own rows - `gate_opened` and `reviewer_decision`, with the reviewer's
    identity rather than `system` - are not written here. They arrive with the authenticated
    endpoint that produces them, in implementation step 17.

WHO CALLS IT
    The worker, once it exists (implementation step 20). Tests call `build_graph()` and the
    node functions directly.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from pydantic import ValidationError
from sqlalchemy.engine import Engine

from agents.fact_checker import FactCheckerUpdate, check_report
from agents.planner import PlannerUpdate, plan_research
from agents.researcher import ResearcherUpdate, research_subtopic
from agents.supervisor import decide_next
from agents.synthesizer import SynthesizerUpdate, write_report
from config import Config
from database import queries
from graph.reflection import ReflectionOutcome, reflect
from graph.state import (
    TERMINAL_STATUSES,
    ResearchState,
    reviewer_payload,
    state_serde,
    unresearched_subtopics,
    unsupported_claims,
)
from llm_client import LLMClient
from schemas import (
    GateDecision,
    JobStatus,
    ReflectionRoute,
    Report,
    SubtopicStatus,
    SupervisorTarget,
)
from tools.contracts import ToolCache, UrlDeduplicator

logger = logging.getLogger(__name__)

ResearchGraph = CompiledStateGraph[ResearchState, None, ResearchState, ResearchState]
"""What `build_graph()` hands back. Named because the four type parameters tell a reader
nothing, and repeating them at every call site would."""

GateRoute = Literal["export", "synthesizer", "finalize"]
"""The gate's three outcomes, in ARCHITECTURE.md §12's order: approve, edit, reject."""


@dataclass(frozen=True)
class NodeDeps:
    """The collaborators the nodes need, carried as one argument.

    It exists for two concrete reasons rather than tidiness. Most nodes need the same two or
    three values; and binding those values with `functools.partial` under their own names
    would put a parameter called `config` in a node signature, which LangGraph reads as a
    request for its own `RunnableConfig` and warns about. One `deps` parameter is
    unambiguous.
    """

    config: Config
    llm: LLMClient
    cache: ToolCache | None = None
    urls: UrlDeduplicator | None = None
    """The per-job `job:{id}:urls` set, or None to deduplicate within the process only.

    None is Phase 1's behaviour: the Researcher's in-process `seen` set still stops one visit
    reading a page twice. What is missing without it is the half that survives a process - a
    redelivered message re-running a Researcher node whose findings were never checkpointed
    (guidelines §7, §11).
    """
    db: Engine | None = None
    """Where the audit trail is written, or None to run without one.

    None is Phase 1's behaviour exactly: the job runs, state carries everything, and nothing
    outlives the process. It is what the offline test suite and `scripts/measure_jobs.py`
    use, because neither has a database and neither is what persistence is for.
    """


class GateUpdate(TypedDict, total=False):
    """The state fields the human gate owns (ARCHITECTURE.md §5)."""

    status: JobStatus
    failure_reason: str
    reviewer_edit_text: str | None


class TerminalUpdate(TypedDict, total=False):
    """The state fields `export` and `finalize` own.

    One type for both nodes because they own the same two fields: how the job ended, and -
    when it ended badly - why. Two identically shaped types would be two names for one
    contract.
    """

    status: JobStatus
    failure_reason: str


# --- The graph ---------------------------------------------------------------------


def build_graph(
    *,
    config: Config,
    llm: LLMClient,
    cache: ToolCache | None = None,
    urls: UrlDeduplicator | None = None,
    db: Engine | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> ResearchGraph:
    """Wire the five agents and the four control-flow nodes, and compile.

    The whole topology is in one function on purpose: ARCHITECTURE.md §3's diagram should be
    checkable against this code by reading it, without following any indirection.

    `db` and `checkpointer` are the two durable stores, and both default to absent: no
    engine means no rows are written, and no checkpointer means the in-memory one. A process
    that has to survive a restart passes `postgres_checkpointer()` below and an engine from
    `database.queries.create_database_engine()`; the test suite and the measurement harness
    pass neither, which is why they still run with no service behind them.
    """
    deps = NodeDeps(config=config, llm=llm, cache=cache, urls=urls, db=db)
    builder = StateGraph(ResearchState)

    # The five agents. Each node hands the state to one agent function and returns that
    # agent's own update, unchanged.
    builder.add_node(
        "supervisor",
        partial(supervisor_node, deps=deps),
        destinations=("planner", "researcher", "synthesizer", "fact_checker", "finalize"),
    )
    builder.add_node("planner", partial(planner_node, deps=deps))
    builder.add_node("researcher", partial(researcher_node, deps=deps))
    builder.add_node("synthesizer", partial(synthesizer_node, deps=deps))
    builder.add_node("fact_checker", partial(fact_checker_node, deps=deps))

    # The four control-flow nodes. Not agents: no tools, no persona, no goal of their own.
    builder.add_node(
        "reflection",
        partial(reflection_node, deps=deps),
        destinations=("researcher", "synthesizer", "fact_checker", "human_gate", "finalize"),
    )
    builder.add_node(
        "human_gate",
        partial(human_gate_node, deps=deps),
        destinations=("export", "synthesizer", "finalize"),
    )
    builder.add_node("export", partial(export_node, deps=deps))
    builder.add_node("finalize", partial(finalize_node, deps=deps))

    # The fixed edges. The Supervisor is visited between agents, which is what makes routing
    # a state decision rather than one agent handing off to another (CLAUDE.md invariant 5).
    builder.add_edge(START, "supervisor")
    builder.add_edge("planner", "supervisor")
    builder.add_edge("researcher", "supervisor")
    builder.add_edge("synthesizer", "supervisor")
    # Deliberately not a Supervisor decision: `reflection` is absent from SupervisorTarget,
    # which is what keeps it control flow rather than a delegation (guidelines §5).
    builder.add_edge("fact_checker", "reflection")
    # Both export outcomes end the same way. Only the state they write differs.
    builder.add_edge("export", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer or InMemorySaver(serde=state_serde()))


@contextmanager
def postgres_checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    """The durable checkpointer, with its tables created if they are not there.

    This is the reason the gate works. `interrupt()` can hold a job for days, and in-memory
    state dies with the worker - so without this, an approval would re-run the whole
    research pipeline and re-bill every LLM call already spent (guidelines §4, §10).

    **`setup()` owns its own tables.** LangGraph creates and migrates them itself; Alembic
    never touches them, and they are deliberately absent from database/schema.py
    (guidelines §19). Calling it repeatedly is safe - it is how the checkpointer applies its
    own migrations - so a worker calls it at startup rather than guessing.

    The pool is small on purpose: a worker runs one job at a time (ARCHITECTURE.md §11), so
    one connection is the whole working set and the second is headroom for the pool
    replacing a connection without the job waiting on it.

    Used as a context manager, so the pool closes when the process is done with it:

        with postgres_checkpointer(url) as checkpointer:
            graph = build_graph(config=config, llm=llm, db=engine, checkpointer=checkpointer)
    """
    # `connection_class` says these connections hand back dict rows, which is both what the
    # checkpointer reads its own tables with and what makes the pool's type match what it
    # expects. Without it the checkpointer is handed rows of tuples and fails at the first
    # read.
    with ConnectionPool(
        database_url,
        connection_class=Connection[DictRow],
        min_size=1,
        max_size=2,
        # Both are the checkpointer's requirements, not preferences: it manages its own
        # transactions, and it reads its rows back by column name.
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    ) as pool:
        saver = PostgresSaver(pool, serde=state_serde())
        saver.setup()
        yield saver


# --- The five agent nodes ----------------------------------------------------------


def supervisor_node(state: ResearchState, *, deps: NodeDeps) -> Command[SupervisorTarget]:
    """Name the next node, or finalize. The one routing decision in the normal path."""
    if _job_already_failed(state, "supervisor"):
        return Command(goto="finalize")
    outcome = decide_next(state, config=deps.config, llm=deps.llm)
    return Command(goto=outcome.decision.next, update=outcome.update)


def planner_node(state: ResearchState, *, deps: NodeDeps) -> PlannerUpdate:
    """3-5 subtopics and the success criteria, or a failed job."""
    update = plan_research(state, config=deps.config, llm=deps.llm)
    plan = update.get("plan")
    if deps.db is not None and plan is not None:
        queries.record_plan(deps.db, job_id=state["job_id"], plan=plan)
    return update


def researcher_node(state: ResearchState, *, deps: NodeDeps) -> ResearcherUpdate:
    """Findings for one pending subtopic. `findings` carries only the new ones; the
    operator.add reducer on the state appends them, and the same list is what reaches the
    `findings` table."""
    update = research_subtopic(
        state, config=deps.config, llm=deps.llm, cache=deps.cache, urls=deps.urls
    )
    if deps.db is not None:
        subtopic, status = _resolved_subtopic(state, update)
        queries.record_research(
            deps.db,
            job_id=state["job_id"],
            new_findings=update.get("findings", []),
            subtopic=subtopic,
            status=status,
        )
    return update


def synthesizer_node(state: ResearchState, *, deps: NodeDeps) -> SynthesizerUpdate:
    """The draft, written from findings only.

    Its claims and their `claim_sources` rows are written here rather than at export,
    because the audit trail has to be written while the job runs rather than reconstructed
    after it (implementation step 15).
    """
    update = write_report(state, config=deps.config, llm=deps.llm)
    report = update.get("report")
    if deps.db is not None and report is not None:
        queries.record_claims(deps.db, job_id=state["job_id"], report=report)
    return update


def fact_checker_node(state: ResearchState, *, deps: NodeDeps) -> FactCheckerUpdate:
    """One batched verification pass over the current draft's claims."""
    update = check_report(state, config=deps.config, llm=deps.llm, cache=deps.cache)
    if deps.db is not None:
        queries.record_verdicts(
            deps.db, job_id=state["job_id"], verdicts=update.get("verdicts", [])
        )
    return update


# --- The four control-flow nodes ---------------------------------------------------


def reflection_node(
    state: ResearchState, *, deps: NodeDeps
) -> Command[ReflectionRoute | Literal["finalize"]]:
    """Score the draft and route: a targeted retry, the gate, or a failed job.

    `ReflectionOutcome.route` is consumed as it is. `None` is not a route - it means the job
    ended here, and `ReflectionRoute` has no `finalize` value precisely so that a failure
    cannot be expressed as one.
    """
    if _job_already_failed(state, "reflection"):
        return Command(goto="finalize")
    outcome = reflect(state, config=deps.config, llm=deps.llm)
    if deps.db is not None:
        _record_reflection(state["job_id"], outcome, deps.db)
    goto: ReflectionRoute | Literal["finalize"] = (
        "finalize" if outcome.route is None else outcome.route
    )
    return Command(goto=goto, update=outcome.update)


def human_gate_node(state: ResearchState, *, deps: NodeDeps) -> Command[GateRoute]:
    """Pause for a reviewer, then route on what they decided.

    `interrupt()` aborts the node and re-runs it from the top on resume, so nothing this
    function returns before that line survives - which is why `status` cannot become
    `awaiting_approval` in the update. It becomes that **in the job row**, written with the
    `gate_opened` event before the pause, because "this job is waiting for a human" has to be
    durable and visible to a poller while the graph is stopped (ARCHITECTURE.md §12).

    **The gate is the only writer of `reviewer_edit_text`** (ADR 0006). It sets the text on an
    `edit` and clears it on `approve` and `reject`, so the field means "an edit is in flight"
    and nothing else has to remember whether one is. The Synthesizer reads it for the one
    pass that follows; reflection reads it as the marker that sends that pass back here
    rather than into an automatic cycle.
    """
    if deps.db is not None:
        queries.record_gate_opened(
            deps.db,
            job_id=state["job_id"],
            calls_used=state["llm_calls_used"],
            summary=_gate_summary(state),
        )

    raw = interrupt(reviewer_payload(state))

    try:
        decision = GateDecision.model_validate(raw)
    except ValidationError as error:
        # The gate is the backstop the whole injection defense leans on. A decision nobody
        # can parse is not an approval, and guessing which of the three it meant is exactly
        # the silent wrong answer guidelines §3 refuses. `reviewer_edit_text` is left as it
        # was: this is not one of the three decisions, and the job is ending anyway.
        logger.error("gate resumed with a decision that does not validate: %s", error)
        return Command(
            goto="finalize",
            update=GateUpdate(status="failed", failure_reason="invalid_gate_decision"),
        )

    if decision.decision == "approve":
        logger.info("job %s approved at the gate", state["job_id"])
        return Command(goto="export", update=GateUpdate(reviewer_edit_text=None))

    if decision.decision == "reject":
        # Not a failure - a decision. `failure_reason` stays None, and the reviewer's note
        # becomes a `reviewer_decision` audit row when the endpoint that authenticates them
        # arrives in step 18.
        logger.info(
            "job %s rejected at the gate: %s", state["job_id"], decision.note or "no reason given"
        )
        return Command(
            goto="finalize", update=GateUpdate(status="rejected", reviewer_edit_text=None)
        )

    # An edit is human-triggered, so it is not a revision and does not touch
    # `revision_count`. The edited draft carries fresh claim ids, so the Supervisor's
    # existing "some claim has no verdict" row sends it through the Fact-Checker like any
    # other draft (ARCHITECTURE.md §12).
    logger.info("job %s edited at the gate; one Synthesizer pass follows", state["job_id"])
    return Command(goto="synthesizer", update=GateUpdate(reviewer_edit_text=decision.edits))


def export_node(state: ResearchState, *, deps: NodeDeps) -> TerminalUpdate:
    """The claim-to-URL gate, and Phase 2's artifact write.

    Every claim must reach at least one source URL, or the export does not happen - not a
    warning, and not a footnote on an exported report (CLAUDE.md invariant 1). Coverage is
    arithmetic, so a judge here would add variance to a number that should be exact
    (ARCHITECTURE.md §20 row 12). The check itself is unchanged and runs on the report in
    state, whether or not there is a database underneath.

    What the database adds is the record: `export_attempted` before the check, so an export
    that fails the invariant is still visible as an attempt, and then `export_result`
    either way. A passing export writes the approved body to `jobs.report_json` and stamps
    `exported_at` in the same transaction as its audit row, which is where the report is
    read from until S3 arrives in Phase 3 - in this same node (ARCHITECTURE.md §8).

    **A blocked export is never retried.** An uncited claim is a defect in the report, not
    an infrastructure error, and the two are handled differently on purpose.
    """
    report = state["report"]
    if report is None:
        logger.error("export reached with no report")
        return TerminalUpdate(status="failed", failure_reason="no_report_to_export")

    if deps.db is not None:
        queries.record_export_attempt(
            deps.db, job_id=state["job_id"], claims_checked=len(report.claims)
        )

    uncited = _uncited_claims(report)
    if uncited:
        logger.error("export blocked: %d claim(s) reach no source URL: %s", len(uncited), uncited)
        if deps.db is not None:
            queries.record_export_result(
                deps.db, job_id=state["job_id"], report=None, uncited=uncited
            )
        return TerminalUpdate(status="failed", failure_reason="uncited_claims")

    if deps.db is not None:
        queries.record_export_result(deps.db, job_id=state["job_id"], report=report, uncited=())

    logger.info("export gate passed: %d claims, every one cited", len(report.claims))
    return TerminalUpdate(status="approved")


def finalize_node(state: ResearchState, *, deps: NodeDeps) -> TerminalUpdate:
    """The single terminal node. It records the outcome; it does not decide it.

    Every path here already carries its terminal status - a tripped guard, a rejection at
    the gate, or the export gate's answer. Arriving without one is a wiring bug, and a job
    that ends loudly is cheaper to diagnose than one that ends plausibly.

    It is also the only place `jobs.completed_at` is set (ARCHITECTURE.md §9), and where the
    two counters become durable, so budget behaviour stays auditable once state is gone.
    """
    update = _terminal_update(state)

    if deps.db is not None:
        queries.finish_job(
            deps.db,
            job_id=state["job_id"],
            status=update.get("status", state["status"]),
            quality_flag=state["quality_flag"],
            revision_count=state["revision_count"],
            llm_calls_used=state["llm_calls_used"],
        )
    return update


def _terminal_update(state: ResearchState) -> TerminalUpdate:
    """How the job ended, and - when it ended badly and nobody said why - that too."""
    status = state["status"]

    if status not in TERMINAL_STATUSES:
        logger.error("finalize reached with a non-terminal status (%s)", status)
        return TerminalUpdate(status="failed", failure_reason="no_terminal_status")

    if status == "failed" and state["failure_reason"] is None:
        # guidelines §4: failure_reason is never left None on a failure.
        logger.error("finalize reached with status=failed and no reason recorded")
        return TerminalUpdate(failure_reason="unrecorded_failure")

    logger.info("job %s finished with status %s", state["job_id"], status)
    return TerminalUpdate()


# --- Helpers -----------------------------------------------------------------------


def _job_already_failed(state: ResearchState, node: str) -> bool:
    """Has a node upstream already failed this job? Says so in the log when it has.

    Both routers answer this before they route, and both then go straight to `finalize`
    writing nothing: the reason was recorded by whichever node failed, and routing onwards
    would spend a call rediscovering a decision that has already been made.
    """
    if state["status"] != "failed":
        return False
    logger.error(
        "%s reached with the job already failed (%s); going straight to finalize",
        node,
        state["failure_reason"],
    )
    return True


def _gate_summary(state: ResearchState) -> dict[str, Any]:
    """The counts that make a `gate_opened` row worth reading, without the report in it.

    The payload itself is `graph.state.reviewer_payload()`, which this node interrupts with
    and `GET /jobs/{id}/gate` rebuilds from the checkpoint (ADR 0013). It lives there rather
    than here because it is a pure projection of state, and the API must be able to reach it
    without importing an agent.
    """
    return {
        "unsupported_claims": len(unsupported_claims(state)),
        "unresearched_subtopics": len(unresearched_subtopics(state)),
        "quality_flag": state["quality_flag"],
    }


def _resolved_subtopic(
    state: ResearchState, update: ResearcherUpdate
) -> tuple[str | None, SubtopicStatus | None]:
    """Which subtopic this Researcher visit finished, and how it turned out.

    Read off the update rather than recomputed from the plan: the Researcher marks exactly
    one subtopic `done` or `unresearched` per visit, so the one status that changed is the
    visit's subject. Asking the plan again would be a second copy of the agent's "next
    pending subtopic" rule, and the copies would drift.

    A visit that failed before it resolved anything changes no status and answers (None,
    None) - its findings are still written, because they were still paid for.
    """
    before = state["subtopic_status"]
    changed = [
        (subtopic, status)
        for subtopic, status in update.get("subtopic_status", {}).items()
        if before.get(subtopic) != status
    ]
    return changed[0] if len(changed) == 1 else (None, None)


def _record_reflection(job_id: str, outcome: ReflectionOutcome, db: Engine) -> None:
    """The two reflection events the audit trail names, and nothing else.

    A pass, or a cap that routes to the gate, is not an event of its own: the score is in
    state and in the checkpoint, and `jobs.quality_flag` carries the outcome at finalize.
    What is worth reconstructing later is that a revision was started, or that the rubric
    never ran at all - `unscored` is not a pass, and this row is what says so afterwards
    (ARCHITECTURE.md §9, §15).
    """
    update = outcome.update
    if outcome.route is not None and "revision_count" in update:
        scores = update.get("reflection_scores", [])
        queries.record_revision(
            db,
            job_id=job_id,
            revision=update["revision_count"],
            route=outcome.route,
            failed_dimensions=update.get("failed_dimensions", []),
            weighted_score=scores[-1].weighted_score if scores else 0.0,
        )
    elif update.get("quality_flag") == "unscored":
        queries.record_reflection_failed(db, job_id=job_id)


def _uncited_claims(report: Report) -> list[str]:
    """Claim ids that cannot reach a source URL through their finding_ids.

    The in-state form of the `claim_sources` check guidelines §9 describes: a claim is cited
    when at least one finding it rests on sits behind one of the report's sources.
    """
    cited = {finding_id for source in report.sources for finding_id in source.finding_ids}
    return [claim.claim_id for claim in report.claims if not cited.intersection(claim.finding_ids)]
