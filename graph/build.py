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

    Phase 1 scope, stated so the gaps are not mistaken for oversights. The checkpointer is
    in-memory, because there is no gate to resume to yet (guidelines §4). `export` runs the
    claim-to-URL gate and writes no artifact; the approved body goes to `jobs.report_json`
    in Phase 2 and to S3 in Phase 3, in this same node. `finalize` writes the terminal
    status; `audit_events` and `completed_at` arrive with the database.

WHO CALLS IT
    The worker, once it exists (implementation step 20). Tests call `build_graph()` and the
    node functions directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from agents.fact_checker import FactCheckerUpdate, check_report
from agents.planner import PlannerUpdate, plan_research
from agents.researcher import ResearcherUpdate, research_subtopic
from agents.supervisor import decide_next
from agents.synthesizer import SynthesizerUpdate, write_report
from config import Config
from graph.reflection import reflect
from graph.state import ResearchState
from llm_client import LLMClient
from schemas import (
    Finding,
    GateDecision,
    JobStatus,
    ReflectionRoute,
    ReflectionScore,
    Report,
    ResearchPlan,
    SupervisorTarget,
    Verdict,
)
from tools.contracts import ToolCache

logger = logging.getLogger(__name__)

CHECKPOINTED_TYPES = (ResearchPlan, Finding, Report, Verdict, ReflectionScore)
"""The Pydantic types that travel in `ResearchState`, and therefore into every checkpoint.

LangGraph will not rebuild a class it was not told about. Left unregistered it hands back
the field dict instead - and a `Finding` that came back as a dict fails on the next
`.url`, or compares unequal to one that did not. Naming them is also least privilege: a
checkpoint can reconstruct these five classes and the serializer's built-in safe types, and
nothing else the process happens to be able to import (guidelines §16).

Only top-level types belong here. `model_dump()` flattens nested models - `Section`,
`Claim`, `Source`, `Subtopic` - into dicts that their parent re-validates on the way back.
"""

ResearchGraph = CompiledStateGraph[ResearchState, None, ResearchState, ResearchState]
"""What `build_graph()` hands back. Named because the four type parameters tell a reader
nothing, and repeating them at every call site would."""

GateRoute = Literal["export", "synthesizer", "finalize"]
"""The gate's three outcomes, in ARCHITECTURE.md §12's order: approve, edit, reject."""

TERMINAL_STATUSES: frozenset[JobStatus] = frozenset({"approved", "rejected", "failed"})
"""The three `status` values a job may end on (ARCHITECTURE.md §3). Reaching `finalize` on
any other value is a wiring bug, and `finalize` says so rather than inventing an outcome."""


@dataclass(frozen=True)
class NodeDeps:
    """The collaborators the six LLM-using nodes need, carried as one argument.

    It exists for two concrete reasons rather than tidiness. Six nodes need the same two or
    three values; and binding those values with `functools.partial` under their own names
    would put a parameter called `config` in a node signature, which LangGraph reads as a
    request for its own `RunnableConfig` and warns about. One `deps` parameter is
    unambiguous.
    """

    config: Config
    llm: LLMClient
    cache: ToolCache | None = None


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


def build_graph(*, config: Config, llm: LLMClient, cache: ToolCache | None = None) -> ResearchGraph:
    """Wire the five agents and the four control-flow nodes, and compile.

    The whole topology is in one function on purpose: ARCHITECTURE.md §3's diagram should be
    checkable against this code by reading it, without following any indirection.
    """
    deps = NodeDeps(config=config, llm=llm, cache=cache)
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
        "human_gate", human_gate_node, destinations=("export", "synthesizer", "finalize")
    )
    builder.add_node("export", export_node)
    builder.add_node("finalize", finalize_node)

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

    # Phase 1 only. The gate can hold a job for days and in-memory state dies with the
    # process, so Phase 2 swaps InMemorySaver for the Postgres one - keeping this serde,
    # because what a checkpoint may rebuild does not depend on where it is stored.
    return builder.compile(
        checkpointer=InMemorySaver(
            serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINTED_TYPES)
        )
    )


def run_config(job_id: str) -> RunnableConfig:
    """Which thread this run belongs to, and nothing else.

    `thread_id = job_id` is the pairing that gives one job one checkpoint history, and it is
    what makes the human gate resumable across a restart (guidelines §4).
    """
    return {"configurable": {"thread_id": job_id}}


# --- The five agent nodes ----------------------------------------------------------


def supervisor_node(state: ResearchState, *, deps: NodeDeps) -> Command[SupervisorTarget]:
    """Name the next node, or finalize. The one routing decision in the normal path."""
    if _job_already_failed(state, "supervisor"):
        return Command(goto="finalize")
    outcome = decide_next(state, config=deps.config, llm=deps.llm)
    return Command(goto=outcome.decision.next, update=outcome.update)


def planner_node(state: ResearchState, *, deps: NodeDeps) -> PlannerUpdate:
    """3-5 subtopics and the success criteria, or a failed job."""
    return plan_research(state, config=deps.config, llm=deps.llm)


def researcher_node(state: ResearchState, *, deps: NodeDeps) -> ResearcherUpdate:
    """Findings for one pending subtopic. `findings` carries only the new ones; the
    operator.add reducer on the state appends them."""
    return research_subtopic(state, config=deps.config, llm=deps.llm, cache=deps.cache)


def synthesizer_node(state: ResearchState, *, deps: NodeDeps) -> SynthesizerUpdate:
    """The draft, written from findings only."""
    return write_report(state, config=deps.config, llm=deps.llm)


def fact_checker_node(state: ResearchState, *, deps: NodeDeps) -> FactCheckerUpdate:
    """One batched verification pass over the current draft's claims."""
    return check_report(state, config=deps.config, llm=deps.llm, cache=deps.cache)


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
    goto: ReflectionRoute | Literal["finalize"] = (
        "finalize" if outcome.route is None else outcome.route
    )
    return Command(goto=goto, update=outcome.update)


def human_gate_node(state: ResearchState) -> Command[GateRoute]:
    """Pause for a reviewer, then route on what they decided.

    `interrupt()` aborts the node and re-runs it from the top on resume, so nothing written
    before this line would survive it. That is why `status` becomes `awaiting_approval`
    where the interrupt is observed - the API, in Phase 2 - and not here.

    The payload is deliberately minimal for now. What the reviewer is shown - unsupported
    claims and unresearched subtopics first, then `quality_flag`, then the report - is the
    gate's Phase 2 contract (ARCHITECTURE.md §12, implementation step 17).
    """
    raw = interrupt({"job_id": state["job_id"]})

    try:
        decision = GateDecision.model_validate(raw)
    except ValidationError as error:
        # The gate is the backstop the whole injection defense leans on. A decision nobody
        # can parse is not an approval, and guessing which of the three it meant is exactly
        # the silent wrong answer guidelines §3 refuses.
        logger.error("gate resumed with a decision that does not validate: %s", error)
        return Command(
            goto="finalize",
            update=GateUpdate(status="failed", failure_reason="invalid_gate_decision"),
        )

    if decision.decision == "approve":
        logger.info("job %s approved at the gate", state["job_id"])
        return Command(goto="export")

    if decision.decision == "reject":
        # Not a failure - a decision. `failure_reason` stays None, and the reviewer's note
        # becomes an audit_events row once the database arrives in Phase 2.
        logger.info(
            "job %s rejected at the gate: %s", state["job_id"], decision.note or "no reason given"
        )
        return Command(goto="finalize", update=GateUpdate(status="rejected"))

    # An edit is human-triggered, so it is not a revision and does not touch
    # `revision_count`. The edited draft carries fresh claim ids, so the Supervisor's
    # existing "some claim has no verdict" row sends it through the Fact-Checker like any
    # other draft (ARCHITECTURE.md §12).
    logger.info("job %s edited at the gate; one Synthesizer pass follows", state["job_id"])
    return Command(goto="synthesizer", update=GateUpdate(reviewer_edit_text=decision.edits))


def export_node(state: ResearchState) -> TerminalUpdate:
    """The claim-to-URL gate. The project's first invariant, and it is code, not a score.

    Every claim must reach at least one source URL, or the export does not happen - not a
    warning, and not a footnote on an exported report (CLAUDE.md invariant 1). Coverage is
    arithmetic, so a judge here would add variance to a number that should be exact
    (ARCHITECTURE.md §20 row 12).

    No artifact is written in Phase 1. The approved body goes to `jobs.report_json` in
    Phase 2 and to S3 in Phase 3, in this same node.
    """
    report = state["report"]
    if report is None:
        logger.error("export reached with no report")
        return TerminalUpdate(status="failed", failure_reason="no_report_to_export")

    uncited = _uncited_claims(report)
    if uncited:
        logger.error("export blocked: %d claim(s) reach no source URL: %s", len(uncited), uncited)
        return TerminalUpdate(status="failed", failure_reason="uncited_claims")

    logger.info("export gate passed: %d claims, every one cited", len(report.claims))
    return TerminalUpdate(status="approved")


def finalize_node(state: ResearchState) -> TerminalUpdate:
    """The single terminal node. It records the outcome; it does not decide it.

    Every path here already carries its terminal status - a tripped guard, a rejection at
    the gate, or the export gate's answer. Arriving without one is a wiring bug, and a job
    that ends loudly is cheaper to diagnose than one that ends plausibly.
    """
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


def _uncited_claims(report: Report) -> list[str]:
    """Claim ids that cannot reach a source URL through their finding_ids.

    The in-state form of the `claim_sources` check guidelines §9 describes: a claim is cited
    when at least one finding it rests on sits behind one of the report's sources.
    """
    cited = {finding_id for source in report.sources for finding_id in source.finding_ids}
    return [claim.claim_id for claim in report.claims if not cited.intersection(claim.finding_ids)]
