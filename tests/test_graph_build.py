"""
WHY THIS FILE EXISTS
    Step 10 adds no decisions - the agents and the reflection node already made them - so
    these tests are about wiring, and wiring fails in ways unit tests on the agents cannot
    see. Four groups matter most.

    **The topology is the documented one.** ARCHITECTURE.md §3 draws nine nodes and a fixed
    set of edges, and the test reads them back off the compiled graph. The two that would be
    invariant breaches rather than bugs get their own assertions: nothing reaches `export`
    except through `human_gate` (CLAUDE.md invariant 6), and the Supervisor cannot route to
    `reflection` (invariant 8).

    **A failed job stops.** Three agents hand their result to the Supervisor and the
    Fact-Checker hands its result to reflection, so a node that has just failed the job hands
    it to something whose whole purpose is to route it onwards. Both routers are tested for
    going straight to `finalize` without spending a call.

    **The guards stop the graph, and they stop it first.** The hop guard and the revision cap
    are tested end to end - a job that never converges, and a report that never scores well
    enough. The same run also measures how many super-steps the hop guard bounds a job to,
    because LangGraph's own recursion limit raises an exception with no `status` and no
    `failure_reason`, and "our guard trips long before that one" should be a number rather
    than a belief.

    **The whole job runs.** Three end-to-end runs with scripted routing and stubbed agents:
    the normal path to the gate and out through export, the completeness retry - the one that
    would silently do nothing if the draft were not invalidated - and a report that never
    scores well enough, which has to reach the reviewer flagged rather than quietly.

    There is no network here and no real LLM. Routing and scoring run against the real
    Supervisor and the real reflection node with a scripted client; the four agents that
    need tools or long prompts are stubbed, because what is under test is where their answer
    goes, not how they produced it (guidelines §18).

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast, get_args, get_type_hints

import pytest
from fakes import FakeOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command
from openai import OpenAI
from psycopg.rows import dict_row
from pydantic import BaseModel, HttpUrl

import graph.build
from agents.fact_checker import FactCheckerUpdate
from agents.planner import PlannerUpdate
from agents.researcher import ResearcherUpdate
from agents.supervisor import SupervisorUpdate
from agents.synthesizer import SynthesizerUpdate
from config import Config, load_config
from graph.build import (
    CHECKPOINTED_TYPES,
    TERMINAL_STATUSES,
    GateUpdate,
    NodeDeps,
    TerminalUpdate,
    build_graph,
    export_node,
    fact_checker_node,
    finalize_node,
    human_gate_node,
    planner_node,
    postgres_checkpointer,
    reflection_node,
    refuse_edit,
    researcher_node,
    reviewer_payload,
    run_config,
    state_serde,
    supervisor_node,
    synthesizer_node,
)
from graph.reflection import ReflectionUpdate
from graph.state import ResearchState, new_state
from llm_client import LLMClient
from schemas import (
    REFLECTION_DIMENSIONS,
    Claim,
    Finding,
    ReflectionScore,
    Report,
    ResearchPlan,
    Section,
    Source,
    Subtopic,
    Verdict,
)

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_FAST_MODEL": "fast-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
}

_NODES = {
    "supervisor",
    "planner",
    "researcher",
    "synthesizer",
    "fact_checker",
    "reflection",
    "human_gate",
    "export",
    "finalize",
}

_FIXED_EDGES = {
    ("__start__", "supervisor"),
    ("planner", "supervisor"),
    ("researcher", "supervisor"),
    ("synthesizer", "supervisor"),
    ("fact_checker", "reflection"),
    ("export", "finalize"),
    ("finalize", "__end__"),
}

_URL = "https://example.com/a"


# --- Building blocks ---------------------------------------------------------------


def _config(**overrides: str) -> Config:
    return load_config({**_ENV, **overrides})


def _llm(*script: object) -> tuple[LLMClient, FakeOpenAI]:
    fake = FakeOpenAI(*script)
    return LLMClient(_config(), client=cast(OpenAI, fake)), fake


def _deps(*script: object) -> tuple[NodeDeps, FakeOpenAI]:
    llm, fake = _llm(*script)
    return NodeDeps(config=_config(), llm=llm), fake


def _plain_deps() -> NodeDeps:
    """Deps for `export` and `finalize`, the two nodes that call no model.

    `db` is left None, which is the Phase 1 behaviour these tests are about: the export gate
    is arithmetic over the report in state and answers the same way whether or not there is
    a database underneath. What the two nodes write when there is one is
    tests/test_graph_persistence.py.
    """
    return _deps()[0]


def _decision(target: str, reason: str = "the rule says so") -> str:
    return json.dumps({"next": target, "reason": reason})


def _rubric(**scores: object) -> str:
    """A scored response. Every dimension is 5 unless the test says otherwise."""
    body: dict[str, object] = dict.fromkeys(REFLECTION_DIMENSIONS, 5)
    body["rationale"] = "Thorough and well cited."
    body.update(scores)
    return json.dumps(body)


def _plan() -> ResearchPlan:
    return ResearchPlan(
        subtopics=[
            Subtopic(
                id="s1", question="What is TCS cloud revenue?", search_query="TCS cloud revenue"
            ),
            Subtopic(
                id="s2",
                question="What is Infosys cloud revenue?",
                search_query="Infosys cloud revenue",
            ),
            Subtopic(
                id="s3",
                question="How do their partnerships compare?",
                search_query="TCS Infosys cloud partnerships",
            ),
        ],
        success_criteria=["Cites public sources"],
    )


def _finding(finding_id: str = "f1", subtopic_id: str = "s1") -> Finding:
    return Finding(
        finding_id=finding_id,
        subtopic_id=subtopic_id,
        claim="Cloud revenue grew.",
        evidence="TCS reported cloud revenue of $1.2bn.",
        url=HttpUrl(_URL),
        title="Annual report",
        retrieved_at=datetime(2026, 8, 12, tzinfo=UTC),
        content_hash="abc",
        truncated=False,
    )


def _report(*claim_ids: str, cited: str = "f1") -> Report:
    return Report(
        sections=[Section(id="sec1", heading="Cloud", body="Both firms grew.")],
        claims=[
            Claim(claim_id=cid, section_id="sec1", text="TCS grew.", finding_ids=[cited])
            for cid in claim_ids
        ],
        sources=[Source(url=HttpUrl(_URL), title="A", finding_ids=[cited])],
    )


def _state(**overrides: object) -> ResearchState:
    state = new_state(job_id="job-1", user_id="user-1", question="Compare TCS and Infosys.")
    state.update(cast(ResearchState, overrides))
    return state


def _planned(**overrides: object) -> ResearchState:
    """A job past the Planner, with every subtopic resolved and one finding in hand."""
    defaults: dict[str, object] = {
        "plan": _plan(),
        "subtopic_status": {"s1": "done", "s2": "done", "s3": "done"},
        "findings": [_finding()],
    }
    return _state(**{**defaults, **overrides})


def _checked(**overrides: object) -> ResearchState:
    """A job with a draft whose every claim already has a verdict - reflection's input."""
    defaults: dict[str, object] = {
        "report": _report("c1"),
        "verdicts": [Verdict(claim_id="c1", supported=True, quote="q", note="stated")],
    }
    return _planned(**{**defaults, **overrides})


class _FakePool:
    """Enough of `psycopg_pool.ConnectionPool` to see how it was built, and no database.

    It records what it was constructed with and whether it was closed, which is all the
    checkpointer factory's contract is: a pool built from this URL, with these connection
    settings, released when the caller is done with it.
    """

    def __init__(self, conninfo: str, **kwargs: Any) -> None:
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.closed = False

    def __enter__(self) -> _FakePool:
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True


class _FakeSaver:
    """`PostgresSaver` minus PostgreSQL: what it was handed, and whether `setup()` ran."""

    def __init__(self, pool: _FakePool, *, serde: JsonPlusSerializer) -> None:
        self.pool = pool
        self.serde = serde
        self.setups = 0

    def setup(self) -> None:
        self.setups += 1


class _Agents:
    """Deterministic stand-ins for the four agents that need tools or long prompts.

    They do the smallest thing that keeps the Supervisor's transition table moving: the
    Researcher resolves one pending subtopic per pass, and the Synthesizer mints fresh claim
    ids on every pass. That second one is not decoration - "some claim has no verdict" is how
    a re-synthesized draft finds its way back to the Fact-Checker, so a stub that reused ids
    would test a graph that cannot revise (ARCHITECTURE.md §20 row 35).
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._passes = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(graph.build, "plan_research", self.plan)
        monkeypatch.setattr(graph.build, "research_subtopic", self.research)
        monkeypatch.setattr(graph.build, "write_report", self.synthesize)
        monkeypatch.setattr(graph.build, "check_report", self.check)

    def plan(self, state: ResearchState, **_: Any) -> PlannerUpdate:
        self.calls.append("planner")
        plan = _plan()
        return PlannerUpdate(
            plan=plan,
            subtopic_status={subtopic.id: "pending" for subtopic in plan.subtopics},
        )

    def research(self, state: ResearchState, **_: Any) -> ResearcherUpdate:
        self.calls.append("researcher")
        plan = state["plan"]
        assert plan is not None, "the researcher must never be reached before the planner"
        statuses = state["subtopic_status"]
        subtopic = next(s for s in plan.subtopics if statuses.get(s.id) == "pending")
        finding_id = f"f{len(state['findings']) + 1}"
        return ResearcherUpdate(
            findings=[_finding(finding_id, subtopic_id=subtopic.id)],
            subtopic_status={**statuses, subtopic.id: "done"},
        )

    def synthesize(self, state: ResearchState, **_: Any) -> SynthesizerUpdate:
        self.calls.append("synthesizer")
        self._passes += 1
        cited = state["findings"][0].finding_id
        return SynthesizerUpdate(report=_report(f"c{self._passes}", cited=cited))

    def check(self, state: ResearchState, **_: Any) -> FactCheckerUpdate:
        self.calls.append("fact_checker")
        report = state["report"]
        assert report is not None, "the fact-checker must never be reached without a draft"
        return FactCheckerUpdate(
            verdicts=[
                Verdict(claim_id=claim.claim_id, supported=True, quote="q", note="stated")
                for claim in report.claims
            ]
        )


# --- The topology ------------------------------------------------------------------


def test_the_graph_compiles_with_the_nine_documented_nodes() -> None:
    llm, _ = _llm()

    drawn = build_graph(config=_config(), llm=llm).get_graph()

    assert set(drawn.nodes) - {"__start__", "__end__"} == _NODES


def test_the_fixed_edges_are_the_documented_ones() -> None:
    llm, _ = _llm()

    drawn = build_graph(config=_config(), llm=llm).get_graph()

    fixed = {(edge.source, edge.target) for edge in drawn.edges if not edge.conditional}
    assert fixed == _FIXED_EDGES


def test_the_routing_nodes_declare_the_documented_targets() -> None:
    llm, _ = _llm()

    drawn = build_graph(config=_config(), llm=llm).get_graph()

    routed: dict[str, set[str]] = {}
    for edge in drawn.edges:
        if edge.conditional:
            routed.setdefault(edge.source, set()).add(edge.target)
    assert routed == {
        "supervisor": {"planner", "researcher", "synthesizer", "fact_checker", "finalize"},
        "reflection": {"researcher", "synthesizer", "fact_checker", "human_gate", "finalize"},
        "human_gate": {"export", "synthesizer", "finalize"},
    }


def test_no_edge_reaches_export_except_through_the_human_gate() -> None:
    # CLAUDE.md invariant 6. The gate is the backstop for everything the automated checks
    # miss, so a second way into export would not be a shortcut - it would remove the gate.
    llm, _ = _llm()

    drawn = build_graph(config=_config(), llm=llm).get_graph()

    assert {edge.source for edge in drawn.edges if edge.target == "export"} == {"human_gate"}


def test_the_supervisor_cannot_route_to_reflection() -> None:
    # CLAUDE.md invariant 8. Reflection is reached by a fixed edge from the Fact-Checker,
    # which is what keeps it control flow rather than something the Supervisor delegates to.
    llm, _ = _llm()

    drawn = build_graph(config=_config(), llm=llm).get_graph()

    from_supervisor = {edge.target for edge in drawn.edges if edge.source == "supervisor"}
    assert "reflection" not in from_supervisor
    assert ("fact_checker", "reflection") in _FIXED_EDGES


def test_the_graph_compiles_with_the_in_memory_checkpointer_when_given_none() -> None:
    # The default, and what the offline suite and the measurement harness run on. A process
    # that has to survive a restart passes `postgres_checkpointer()` instead - the graph
    # never reaches for a database on its own (implementation step 14).
    llm, _ = _llm()

    compiled = build_graph(config=_config(), llm=llm)

    assert type(compiled.checkpointer).__name__ == "InMemorySaver"


# --- What each node receives and returns -------------------------------------------


def test_the_agent_nodes_return_their_agent_update_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The node adds nothing and drops nothing: whatever the agent decided is what reaches
    # the state. Anything else would be a second copy of the agent's contract.
    seen: dict[str, ResearchState] = {}
    answers: dict[str, Any] = {
        "plan_research": PlannerUpdate(plan=_plan()),
        "research_subtopic": ResearcherUpdate(findings=[_finding()]),
        "write_report": SynthesizerUpdate(report=_report("c1")),
        "check_report": FactCheckerUpdate(verdicts=[]),
    }

    def stub(agent: str) -> Any:
        def answer(state: ResearchState, **_: Any) -> Any:
            seen[agent] = state
            return answers[agent]

        return answer

    for name in answers:
        monkeypatch.setattr(graph.build, name, stub(name))

    deps, fake = _deps()
    state = _planned()
    nodes = [
        ("plan_research", planner_node),
        ("research_subtopic", researcher_node),
        ("write_report", synthesizer_node),
        ("check_report", fact_checker_node),
    ]
    for name, node in nodes:
        assert node(state, deps=deps) is answers[name]
        assert seen[name] is state

    assert fake.completions.calls == []  # the node itself never calls the model


def test_the_tool_using_nodes_pass_the_cache_through(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Researcher and the Fact-Checker are the two agents that reach the web, and the
    # cache is the argument that decides whether they reach it twice for the same page.
    passed: list[object] = []

    def record(state: ResearchState, cache: object = None, **_: Any) -> dict[str, object]:
        passed.append(cache)
        return {}

    for name in ("research_subtopic", "check_report"):
        monkeypatch.setattr(graph.build, name, record)
    cache = object()
    deps = NodeDeps(config=_config(), llm=_llm()[0], cache=cast(Any, cache))

    researcher_node(_planned(), deps=deps)
    fact_checker_node(_checked(), deps=deps)

    assert passed == [cache, cache]


def test_the_agent_nodes_write_only_the_fields_their_agents_own() -> None:
    # Ownership is checked by mypy on the return types; this pins the sets themselves, so a
    # node that started writing status or hop_count would fail here as well.
    assert set(PlannerUpdate.__annotations__) <= set(ResearchState.__annotations__)
    assert set(ResearcherUpdate.__annotations__) <= set(ResearchState.__annotations__)
    assert set(SynthesizerUpdate.__annotations__) <= set(ResearchState.__annotations__)
    assert set(FactCheckerUpdate.__annotations__) <= set(ResearchState.__annotations__)
    assert set(SupervisorUpdate.__annotations__) <= set(ResearchState.__annotations__)
    assert set(ReflectionUpdate.__annotations__) <= set(ResearchState.__annotations__)
    assert set(GateUpdate.__annotations__) == {"status", "failure_reason", "reviewer_edit_text"}
    assert set(TerminalUpdate.__annotations__) == {"status", "failure_reason"}


# --- Supervisor routing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "state"),
    [
        ("planner", _state()),
        ("researcher", _state(plan=_plan(), subtopic_status={"s1": "pending"})),
        ("synthesizer", _planned()),
        ("fact_checker", _planned(report=_report("c1"))),
    ],
)
def test_the_supervisor_node_routes_where_the_decision_says(
    target: str, state: ResearchState
) -> None:
    deps, _ = _deps(_decision(target))

    command = supervisor_node(state, deps=deps)

    assert command.goto == target


def test_the_supervisor_node_carries_the_supervisor_update() -> None:
    deps, _ = _deps(_decision("planner"))

    command = supervisor_node(_state(), deps=deps)

    assert command.update == {"hop_count": 1, "llm_calls_used": 1}


def test_a_tripped_hop_guard_routes_to_finalize() -> None:
    deps, fake = _deps()

    limit = _config().max_supervisor_hops
    command = supervisor_node(_state(hop_count=limit), deps=deps)

    assert command.goto == "finalize"
    assert command.update == {
        "hop_count": limit + 1,
        "status": "failed",
        "failure_reason": "hop_limit_exceeded",
    }
    assert fake.completions.calls == []


def test_a_tripped_budget_guard_routes_to_finalize() -> None:
    deps, fake = _deps()

    command = supervisor_node(_state(llm_calls_used=60), deps=deps)

    assert command.goto == "finalize"
    assert cast(dict[str, Any], command.update)["failure_reason"] == "budget_exceeded"
    assert fake.completions.calls == []


def test_a_route_the_state_does_not_allow_is_ignored_by_the_graph() -> None:
    # ADR 0001: the code decides and the model explains, so the graph takes the state's
    # route and a disagreeing proposal costs the job nothing.
    deps, _ = _deps(_decision("synthesizer"))

    command = supervisor_node(_state(), deps=deps)

    assert command.goto == "planner"
    assert "failure_reason" not in cast(dict[str, Any], command.update)


def test_a_job_that_already_failed_leaves_the_supervisor_without_a_call() -> None:
    # The Planner, the Researcher, and the Synthesizer all hand their result here. Routing a
    # failed job would spend twelve hops rediscovering a decision already made.
    deps, fake = _deps()

    command = supervisor_node(_state(status="failed", failure_reason="invalid_plan"), deps=deps)

    assert command.goto == "finalize"
    assert command.update is None
    assert fake.completions.calls == []


# --- Reflection routing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("route", "scores"),
    [
        ("human_gate", {}),
        ("researcher", {"research_completeness": 1, "source_correctness": 1}),
        ("synthesizer", {"citation_coverage": 4}),
        # Two dimensions tie at the bottom; rubric order breaks it toward the Fact-Checker.
        (
            "fact_checker",
            {"research_completeness": 3, "factual_consistency": 1, "report_quality": 1},
        ),
    ],
)
def test_the_reflection_node_routes_where_the_outcome_says(
    route: str, scores: dict[str, int]
) -> None:
    deps, _ = _deps(_rubric(**scores))

    command = reflection_node(_checked(), deps=deps)

    assert command.goto == route


def test_a_researcher_route_invalidates_the_draft_on_the_way_out() -> None:
    # Without this the Supervisor would match "a draft exists, claims unverified" and send
    # the job to the Fact-Checker, and the new findings would never enter the report.
    deps, _ = _deps(_rubric(research_completeness=1, source_correctness=1))

    command = reflection_node(_checked(), deps=deps)

    update = cast(dict[str, Any], command.update)
    assert command.goto == "researcher"
    assert update["report"] is None
    assert set(update["subtopic_status"].values()) == {"pending"}
    assert update["revision_count"] == 1


def test_an_outcome_with_no_route_finalizes_the_job() -> None:
    # `None` is not a route: ReflectionRoute has four values and none of them is finalize,
    # so a failure cannot be expressed as one.
    deps, _ = _deps()

    command = reflection_node(_checked(llm_calls_used=60), deps=deps)

    update = cast(dict[str, Any], command.update)
    assert command.goto == "finalize"
    assert update["status"] == "failed"
    assert update["failure_reason"] == "budget_exceeded"


def test_a_job_that_already_failed_leaves_reflection_without_a_call() -> None:
    # The Fact-Checker's fixed edge lands here, so a failed verification would otherwise be
    # scored as if the draft had been checked.
    deps, fake = _deps()

    command = reflection_node(_checked(status="failed", failure_reason="invalid_output"), deps=deps)

    assert command.goto == "finalize"
    assert command.update is None
    assert fake.completions.calls == []


# --- The gate, export, and finalize ------------------------------------------------


def test_the_gate_shows_the_reviewer_the_problems_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ARCHITECTURE.md §12's order is the contract, not a preference: a reviewer who has to
    # hunt for the problems will approve past them. Insertion order is the rendered order.
    payloads: list[Any] = []

    def pause(payload: Any) -> dict[str, str]:
        payloads.append(payload)
        return {"decision": "approve"}

    monkeypatch.setattr(graph.build, "interrupt", pause)

    human_gate_node(_checked(), deps=_plain_deps())

    assert list(payloads[0]) == [
        "job_id",
        "unsupported_claims",
        "unresearched_subtopics",
        "quality_flag",
        "score",
        "failed_dimensions",
        "revision_count",
        "llm_calls_used",
        "report",
        "claims",
    ]


def test_the_gate_payload_carries_the_problems_and_the_evidence() -> None:
    # What the reviewer is judging: which claims failed verification, which subtopics found
    # nothing, and what each surviving claim is standing on.
    state = _checked(
        subtopic_status={"s1": "done", "s2": "unresearched", "s3": "done"},
        verdicts=[Verdict(claim_id="c1", supported=False, quote=None, note="not in the source")],
    )

    payload = reviewer_payload(state)

    assert payload["unsupported_claims"] == ["c1"]
    assert payload["unresearched_subtopics"] == ["s2"]
    assert payload["claims"] == [
        {
            "claim_id": "c1",
            "text": "TCS grew.",
            "sources": [_URL],
            "supported": False,
            "quote": None,
            "note": "not in the source",
        }
    ]
    # The live call count, which is what an edit's budget check reads (ADR 0006 decision 7).
    assert payload["llm_calls_used"] == state["llm_calls_used"]


def test_the_gate_routes_approve_into_export(monkeypatch: pytest.MonkeyPatch) -> None:
    command = _gate_decision(monkeypatch, {"decision": "approve"})

    assert command.goto == "export"
    # The gate is the only writer of the field, and an approval ends any edit in flight.
    assert command.update == {"reviewer_edit_text": None}


def test_the_gate_routes_reject_to_finalize_without_exporting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _gate_decision(monkeypatch, {"decision": "reject", "note": "sources are weak"})

    assert command.goto == "finalize"
    assert command.update == {"status": "rejected", "reviewer_edit_text": None}


def test_the_gate_routes_edit_to_the_synthesizer_and_carries_the_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _gate_decision(
        monkeypatch, {"decision": "edit", "edits": "Drop the unsupported claim."}
    )

    assert command.goto == "synthesizer"
    assert command.update == {"reviewer_edit_text": "Drop the unsupported claim."}


def test_an_edit_is_not_a_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    # Human-triggered, so it cannot be starved by MAX_REVISIONS and does not consume a cycle.
    command = _gate_decision(monkeypatch, {"decision": "edit", "edits": "Tighten section two."})

    assert "revision_count" not in cast(dict[str, Any], command.update)


@pytest.mark.parametrize(
    "raw",
    [
        {"decision": "export"},  # not one of the three
        {"decision": "edit"},  # an edit with nothing to apply
        "approve",  # not the documented shape at all
        None,
    ],
)
def test_a_gate_decision_that_does_not_validate_fails_the_job(
    raw: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gate is the backstop the whole injection defense leans on. Guessing which of the
    # three a malformed decision meant is exactly the silent wrong answer to refuse.
    command = _gate_decision(monkeypatch, raw)

    assert command.goto == "finalize"
    assert cast(dict[str, Any], command.update)["failure_reason"] == "invalid_gate_decision"


def test_the_export_gate_blocks_a_claim_that_reaches_no_source() -> None:
    # CLAUDE.md invariant 1. Not a warning on an exported report - the export does not happen.
    report = _report("c1")
    report.claims.append(Claim(claim_id="c2", section_id="sec1", text="X.", finding_ids=["f9"]))

    update = export_node(_planned(report=report), deps=_plain_deps())

    assert update == {"status": "failed", "failure_reason": "uncited_claims"}


def test_the_export_gate_passes_when_every_claim_is_cited() -> None:
    update = export_node(_planned(report=_report("c1", "c2")), deps=_plain_deps())

    assert update == {"status": "approved"}


def test_export_without_a_report_fails_rather_than_exporting_nothing() -> None:
    update = export_node(_planned(), deps=_plain_deps())

    assert update == {"status": "failed", "failure_reason": "no_report_to_export"}


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_finalize_records_a_terminal_status_without_changing_it(status: str) -> None:
    update = finalize_node(_state(status=status, failure_reason="whatever"), deps=_plain_deps())

    assert update == {}


def test_finalize_fails_loudly_when_it_is_reached_with_no_outcome() -> None:
    # Every documented path here carries a terminal status. Arriving without one is a wiring
    # bug, and a job that ends loudly is cheaper to diagnose than one that ends plausibly.
    update = finalize_node(_state(), deps=_plain_deps())

    assert update == {"status": "failed", "failure_reason": "no_terminal_status"}


def test_finalize_never_leaves_a_failure_without_a_reason() -> None:
    update = finalize_node(_state(status="failed"), deps=_plain_deps())

    assert update == {"failure_reason": "unrecorded_failure"}


def _gate_decision(monkeypatch: pytest.MonkeyPatch, raw: object) -> Command[Any]:
    """Run `human_gate_node` as if a reviewer had resumed it with `raw`.

    `interrupt()` is stubbed so the decision handling can be read one branch at a time. The
    real pause and the real resume are covered end to end further down, where the graph runs
    with a checkpointer under it.
    """
    monkeypatch.setattr(graph.build, "interrupt", lambda _payload: raw)
    return human_gate_node(_checked(), deps=_plain_deps())


# --- The whole job, end to end -----------------------------------------------------


def test_the_normal_path_runs_to_the_gate_and_out_through_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ARCHITECTURE.md §3's normal execution path, one scripted response per LLM call, in the
    # order the graph makes them. An extra call fails the FakeOpenAI script.
    agents = _Agents()
    agents.install(monkeypatch)
    config = _config()
    llm, fake = _llm(
        _decision("planner"),
        _decision("researcher"),
        _decision("researcher"),
        _decision("researcher"),
        _decision("synthesizer"),
        _decision("fact_checker"),
        _rubric(),
    )
    compiled = build_graph(config=config, llm=llm)
    settings = run_config("job-1")

    paused = compiled.invoke(_state(), settings)

    assert agents.calls == ["planner", "researcher", "researcher", "researcher"] + [
        "synthesizer",
        "fact_checker",
    ]
    assert compiled.get_state(settings).next == ("human_gate",)
    # The reviewer's payload, problems first (ARCHITECTURE.md §12). Its shape is asserted
    # where the gate is tested; here the point is that the pause carries it at all.
    assert paused["__interrupt__"][0].value["job_id"] == "job-1"
    assert paused["__interrupt__"][0].value["unsupported_claims"] == []
    assert paused["quality_flag"] is None  # the rubric ran and the report passed

    final = compiled.invoke(Command(resume={"decision": "approve"}), settings)

    assert final["status"] == "approved"
    assert final["failure_reason"] is None
    assert fake.completions.script == []  # every scripted call was used


def test_a_completeness_retry_reaches_the_gate_through_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The most important targeted retry in the system, and the one that would silently do
    # nothing if reflection did not invalidate the draft: new findings must pass through the
    # Synthesizer and the Fact-Checker before a reviewer ever sees them.
    agents = _Agents()
    agents.install(monkeypatch)
    config = _config()
    llm, _ = _llm(
        _decision("planner"),
        *[_decision("researcher")] * 3,
        _decision("synthesizer"),
        _decision("fact_checker"),
        _rubric(research_completeness=1, source_correctness=1),
        _decision("researcher"),
        _decision("researcher"),
        _decision("synthesizer"),
        _decision("fact_checker"),
        _rubric(),
    )
    compiled = build_graph(config=config, llm=llm)
    settings = run_config("job-1")

    paused = compiled.invoke(_state(), settings)

    assert agents.calls.count("researcher") == 6  # three subtopics, re-researched
    assert agents.calls[-2:] == ["synthesizer", "fact_checker"]
    assert paused["revision_count"] == 1
    assert len(paused["findings"]) == 6  # operator.add accumulated both passes
    assert paused["report"].claims[0].claim_id == "c2"  # the draft written after the retry
    assert compiled.get_state(settings).next == ("human_gate",)


def test_the_revision_cap_ends_the_reflection_loop_at_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CLAUDE.md invariant 2: hitting the cap is a visible outcome, never a silent pass.
    agents = _Agents()
    agents.install(monkeypatch)
    config = _config()
    failing = _rubric(citation_coverage=4)
    llm, _ = _llm(
        _decision("planner"),
        *[_decision("researcher")] * 3,
        _decision("synthesizer"),
        _decision("fact_checker"),
        failing,
        _decision("fact_checker"),
        failing,
        _decision("fact_checker"),
        failing,
    )
    compiled = build_graph(config=config, llm=llm)
    settings = run_config("job-1")

    paused = compiled.invoke(_state(), settings)

    assert paused["revision_count"] == 2  # two cycles, three report-producing passes
    assert paused["quality_flag"] == "below_threshold"
    assert paused["failed_dimensions"] == ["citation_coverage"]
    assert compiled.get_state(settings).next == ("human_gate",)


def test_a_job_that_never_converges_stops_at_the_hop_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Planner that writes no plan makes the Supervisor route to it forever. The guard is
    # what ends that, with a reason a caller can act on.
    agents = _Agents()
    agents.install(monkeypatch)
    monkeypatch.setattr(graph.build, "plan_research", lambda state, **_: PlannerUpdate())
    config = _config()
    llm, _ = _llm(*[_decision("planner")] * config.max_supervisor_hops)
    compiled = build_graph(config=config, llm=llm)

    final = compiled.invoke(_state(), run_config("job-1"))

    assert final["status"] == "failed"
    assert final["failure_reason"] == "hop_limit_exceeded"
    assert final["hop_count"] == config.max_supervisor_hops + 1


def test_the_hop_guard_bounds_how_long_a_job_can_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # LangGraph has a recursion limit of its own, and it raises GraphRecursionError - a
    # framework exception with no status and no failure_reason. It is left at its default,
    # so this measures the gap rather than leaving it assumed. A hop can cost at most three
    # super-steps (the Supervisor, the agent, and reflection after the Fact-Checker), plus
    # the four no hop pays for: human_gate, export, finalize, and the step into END.
    monkeypatch.setattr(graph.build, "plan_research", lambda state, **_: PlannerUpdate())
    config = _config()
    llm, _ = _llm(*[_decision("planner")] * config.max_supervisor_hops)
    compiled = build_graph(config=config, llm=llm)

    steps = sum(1 for _ in compiled.stream(_state(), run_config("job-1"), stream_mode="updates"))

    # Two super-steps per hop on this path (Supervisor, Planner), plus the two the guard
    # itself pays for: the Supervisor hop that trips it and finalize.
    assert steps == 2 * config.max_supervisor_hops + 2
    assert steps <= 3 * config.max_supervisor_hops + 4


# --- The checkpointer and the thread -----------------------------------------------


def test_the_thread_is_the_job_id() -> None:
    # One job, one thread, one checkpoint history - the pairing the human gate is resumable
    # across a restart because of.
    settings = run_config("job-7")

    assert settings["configurable"]["thread_id"] == "job-7"


def test_state_is_checkpointed_under_the_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    agents = _Agents()
    agents.install(monkeypatch)
    config = _config()
    llm, _ = _llm(_decision("planner"), _decision("researcher"))
    compiled = build_graph(config=config, llm=llm)

    with pytest.raises(AssertionError):  # the script runs out mid-job, on purpose
        compiled.invoke(_state(), run_config("job-1"))

    assert compiled.get_state(run_config("job-1")).values["plan"] is not None
    assert compiled.get_state(run_config("other-job")).values == {}


def test_resuming_does_not_re_execute_completed_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    # guidelines §18. Approval after two days must cost nothing beyond the export, which is
    # the concrete reason the checkpointer exists at all.
    agents = _Agents()
    agents.install(monkeypatch)
    config = _config()
    llm, _ = _llm(
        _decision("planner"),
        *[_decision("researcher")] * 3,
        _decision("synthesizer"),
        _decision("fact_checker"),
        _rubric(),
    )
    compiled = build_graph(config=config, llm=llm)
    settings = run_config("job-1")
    compiled.invoke(_state(), settings)
    before = list(agents.calls)

    final = compiled.invoke(Command(resume={"decision": "approve"}), settings)

    assert agents.calls == before  # no agent ran again
    assert final["status"] == "approved"


def test_the_graph_uses_the_checkpointer_it_is_given() -> None:
    # The seam step 14 needs: the worker hands in the Postgres saver, and nothing about the
    # topology changes because of it.
    llm, _ = _llm()
    saver = InMemorySaver(serde=state_serde())

    compiled = build_graph(config=_config(), llm=llm, checkpointer=saver)

    assert compiled.checkpointer is saver


def test_a_graph_that_did_not_start_a_job_resumes_it_from_the_checkpointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # What durable checkpointing is *for*: the process that approves a job is not the process
    # that ran it. A second graph object, sharing only the checkpointer, picks the job up at
    # the gate - and its LLM has no script at all, so any re-executed node fails the test
    # rather than quietly re-billing (guidelines §10).
    agents = _Agents()
    agents.install(monkeypatch)
    saver = InMemorySaver(serde=state_serde())
    llm, _ = _llm(
        _decision("planner"),
        *[_decision("researcher")] * 3,
        _decision("synthesizer"),
        _decision("fact_checker"),
        _rubric(),
    )
    settings = run_config("job-1")
    build_graph(config=_config(), llm=llm, checkpointer=saver).invoke(_state(), settings)
    before = list(agents.calls)

    resumed, _ = _llm()
    final = build_graph(config=_config(), llm=resumed, checkpointer=saver).invoke(
        Command(resume={"decision": "approve"}), settings
    )

    assert agents.calls == before  # no agent ran again
    assert final["status"] == "approved"


def test_the_postgres_checkpointer_creates_its_tables_and_carries_the_state_serde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory's wiring, with the pool and the saver stubbed.

    **What it proves:** the connection string reaches the pool, the connections are the
    dict-row autocommit ones the checkpointer requires, `setup()` runs before anything uses
    it - that is how it owns its own tables (guidelines §19) - the serde is the one that can
    rebuild a `Finding`, and the pool is closed on the way out.

    **What it does not prove:** anything about PostgreSQL. No database is involved here, and
    none can be until Compose provides one in Phase 3.
    """
    monkeypatch.setattr(graph.build, "ConnectionPool", _FakePool)
    monkeypatch.setattr(graph.build, "PostgresSaver", _FakeSaver)

    with postgres_checkpointer("postgresql://user@host/jobs") as checkpointer:
        saver = cast(_FakeSaver, checkpointer)
        pool = saver.pool

        assert pool.conninfo == "postgresql://user@host/jobs"
        assert pool.kwargs["kwargs"] == {"autocommit": True, "row_factory": dict_row}
        assert saver.setups == 1
        # A Finding that came back as a dict fails on the next `.url`, and it fails days
        # later, in the resumed job - so the durable saver gets the same serde as the memory
        # one, and this is what says so.
        packed = saver.serde.dumps_typed(_finding())
        assert saver.serde.loads_typed(packed) == _finding()
        assert not pool.closed

    assert pool.closed


def test_every_model_in_the_state_is_registered_with_the_checkpointer() -> None:
    # LangGraph will not rebuild a class it was not told about - it hands back the field
    # dict, and the failure surfaces later as an AttributeError on a resumed job. Deriving
    # the set from ResearchState is what makes adding a sixth model fail here first.
    hints = get_type_hints(ResearchState)
    found: set[type[BaseModel]] = set()
    for hint in hints.values():
        found |= _models_in(hint)

    assert found == set(CHECKPOINTED_TYPES)


def test_the_state_models_survive_the_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    agents = _Agents()
    agents.install(monkeypatch)
    config = _config()
    llm, _ = _llm(
        _decision("planner"),
        *[_decision("researcher")] * 3,
        _decision("synthesizer"),
        _decision("fact_checker"),
        _rubric(),
    )
    compiled = build_graph(config=config, llm=llm)
    settings = run_config("job-1")
    compiled.invoke(_state(), settings)

    values = compiled.get_state(settings).values

    assert isinstance(values["plan"], ResearchPlan)
    assert isinstance(values["findings"][0], Finding)
    assert isinstance(values["report"], Report)
    assert isinstance(values["verdicts"][0], Verdict)
    assert isinstance(values["reflection_scores"][0], ReflectionScore)
    assert str(values["findings"][0].url) == _URL  # the URL is a URL again, not a string


def _models_in(annotation: Any) -> set[type[BaseModel]]:
    """Every Pydantic model reachable from one state annotation."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {annotation}
    found: set[type[BaseModel]] = set()
    for arg in get_args(annotation):
        found |= _models_in(arg)
    return found


def test_a_finished_job_carries_no_state_field_the_contract_does_not_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ResearchState is the contract between agents. A node that slipped an extra key into
    # state would put a value in the checkpoint that nothing owns and nothing documents.
    agents = _Agents()
    agents.install(monkeypatch)
    config = _config()
    llm, _ = _llm(
        _decision("planner"),
        *[_decision("researcher")] * 3,
        _decision("synthesizer"),
        _decision("fact_checker"),
        _rubric(),
    )
    compiled = build_graph(config=config, llm=llm)
    settings = run_config("job-1")
    compiled.invoke(_state(), settings)
    compiled.invoke(Command(resume={"decision": "approve"}), settings)

    assert set(compiled.get_state(settings).values) == set(ResearchState.__annotations__)


# --- Whether an edit may start at all (ADR 0006) -----------------------------------


def test_an_edit_is_allowed_while_both_bounds_have_room() -> None:
    assert refuse_edit(config=_config(), llm_calls_used=10, edits_made=2) is None


def test_the_fourth_reviewer_edit_is_refused() -> None:
    # MAX_REVIEWER_EDITS = 3. The bound exists because an edit is human-triggered, so
    # MAX_REVISIONS does not bound it and nothing else did.
    assert (
        refuse_edit(config=_config(), llm_calls_used=0, edits_made=3)
        == "reviewer_edit_limit_reached"
    )


@pytest.mark.parametrize(("used", "refused"), [(58, True), (57, False)])
def test_an_edit_is_refused_when_the_budget_cannot_fund_its_three_calls(
    used: int, refused: bool
) -> None:
    # An edit needs a Synthesizer, a Fact-Checker and a reflection pass. With exactly three
    # of the 60 left it may start; with two it may not, because it would start, spend, and
    # die on the Supervisor's guard - losing a report the reviewer could have approved,
    # since export never runs on a failed job.
    answer = refuse_edit(config=_config(), llm_calls_used=used, edits_made=0)

    assert (answer == "insufficient_call_budget_for_edit") is refused


def test_the_edit_bounds_are_read_from_config_not_hard_coded() -> None:
    lowered = _config(MAX_REVIEWER_EDITS="1", MAX_LLM_CALLS_PER_JOB="10")

    assert (
        refuse_edit(config=lowered, llm_calls_used=0, edits_made=1) == "reviewer_edit_limit_reached"
    )
    assert (
        refuse_edit(config=lowered, llm_calls_used=8, edits_made=0)
        == "insufficient_call_budget_for_edit"
    )
