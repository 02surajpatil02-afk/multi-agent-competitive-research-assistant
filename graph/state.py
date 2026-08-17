"""
WHY THIS FILE EXISTS
    ResearchState is the contract between agents. Agents never call each other: they read
    and write this dict, and the Supervisor decides who runs next (CLAUDE.md invariant 5).
    Defining it in one place is what makes that invariant checkable rather than aspirational.

    Two design points are load-bearing and easy to get wrong:

    1. Exactly two fields accumulate. ``findings`` and ``verdicts`` carry operator.add, so
       a Researcher step that times out and retries cannot drop what the first attempt
       produced. Everything else is last-write-wins, and adding a third reducer is a
       design change, not a detail.

    2. ``revision_count`` counts improvement cycles, not passes, and starts at 0. The
       initial report is pass 1 at revision_count 0 and is not a revision. With
       MAX_REVISIONS = 2 that means two automatic cycles and three report-producing
       passes, and the cap is checked with >= (ARCHITECTURE.md §3).

    The checkpointer keys on thread_id = job_id: one job, one thread, one checkpoint
    history. That pairing is what makes the human gate resumable across a process restart.
    It is wired where the checkpointer is configured, not here.

    This file holds no routing and no guard logic. Comparing revision_count against
    MAX_REVISIONS belongs to the reflection node, and comparing hop_count against
    MAX_SUPERVISOR_HOPS belongs to the Supervisor; putting either here would spread one
    decision across two files.

    It does hold `reviewer_payload()` and the three projections behind it, which are pure
    functions of the state above and nothing else - no LLM, no tool, no node, no database.
    They live here rather than beside the gate node because two processes need them
    (ADR 0013): the gate node builds the payload it interrupts with, and `GET
    /jobs/{id}/gate` rebuilds the same payload from the checkpoint so a reviewer can read
    what they are being asked to approve. graph/build.py imports every agent, the LLM
    client and the tool boundary, so an API that reached the payload through it would drag
    all three into a process that must not have them.

WHO CALLS IT
    Every graph node - the five agents, the reflection node, the human gate, export, and
    finalize - plus the worker, which builds the initial state for a new job, and
    routes/api.py, which reads the projections below out of the checkpoint.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from config import Config
from schemas import (
    Finding,
    JobStatus,
    QualityFlag,
    ReflectionScore,
    Report,
    ResearchPlan,
    SubtopicStatus,
    Verdict,
)


class ResearchState(TypedDict):
    """The whole state of one research job, as it travels through the graph."""

    # Set once when the job is created, never rewritten.
    job_id: str  # also the checkpointer thread_id and the LangSmith trace tag
    user_id: str  # single tenant today; present so tenant scoping is additive later
    question: str

    # Planner output. None is the Supervisor's first routing test.
    plan: ResearchPlan | None

    # Researcher progress. Routing reads this dict rather than the findings themselves,
    # which is what keeps fetched page text away from the Supervisor. It doubles as the
    # retry scope: reflection returns a thin subtopic to "pending" and the pending set is
    # what the next Researcher pass acts on.
    subtopic_status: dict[str, SubtopicStatus]

    # Accumulated. See the reducer note in the module docstring.
    findings: Annotated[list[Finding], operator.add]

    # The current draft. The Synthesizer writes it; the reflection node sets it back to
    # None when it routes to the Researcher, so new evidence cannot bypass synthesis.
    report: Report | None

    # Accumulated across passes, so "did revision 2 fix it?" is answerable.
    verdicts: Annotated[list[Verdict], operator.add]

    # One score per reflection pass. The history is what makes "did it improve?"
    # answerable; the latest is the current score.
    reflection_scores: list[ReflectionScore]

    # The dimensions failing right now - current, not accumulated. The per-pass history is
    # already in reflection_scores, so keeping a second one here would be duplication.
    # Empty with quality_flag == "unscored" means unknown, not clean.
    failed_dimensions: list[str]

    # The three loop guards. Each fails for a different reason and each is compared
    # against its own limit in config.
    revision_count: int  # improvement cycles, 0-based
    hop_count: int
    llm_calls_used: int

    # None means the rubric ran and the report passed. Written only by the reflection node.
    quality_flag: QualityFlag | None

    # Set by an edit decision at the gate, consumed by the next Synthesizer pass, then set
    # back to None by that same pass - so a reviewer's edit is applied exactly once. The
    # text is also an audit_events row, so it survives the clear.
    reviewer_edit_text: str | None

    # Externally visible job state. failure_reason is never left None on a failure.
    status: JobStatus
    failure_reason: str | None


def new_state(job_id: str, user_id: str, question: str) -> ResearchState:
    """The state a job starts with, before the Supervisor's first hop.

    Every field is set explicitly. A partially built state would make the Supervisor's
    first routing test - "is plan None?" - depend on a key that might not be there.
    """
    return ResearchState(
        job_id=job_id,
        user_id=user_id,
        question=question,
        plan=None,
        subtopic_status={},
        findings=[],
        report=None,
        verdicts=[],
        reflection_scores=[],
        failed_dimensions=[],
        revision_count=0,
        hop_count=0,
        llm_calls_used=0,
        quality_flag=None,
        reviewer_edit_text=None,
        status="running",
        failure_reason=None,
    )


# --- What the reviewer is shown at the gate ----------------------------------------


def reviewer_payload(state: ResearchState) -> dict[str, Any]:
    """What the reviewer is shown, in the order ARCHITECTURE.md §12 puts it.

    **The order is the contract, not a preference.** Unsupported claims and unresearched
    subtopics come first, then `quality_flag`, then the score breakdown, then the report, and
    the per-claim sources last - because a reviewer who has to hunt for the problems will
    approve past them. Python keeps insertion order, so the keys below are that order and a
    test asserts it, at the gate node and again on the HTTP body.

    `llm_calls_used` is here for one concrete reason: it is the live number `refuse_edit()`
    needs, and the gate is where a caller can read it without loading the checkpoint itself.
    It is also ADR 0007's gate-visit key, which is why ADR 0013 adds no second identifier.

    **A pure function of state, and that is what makes the API able to call it** (ADR 0013).
    Everything it returns is already in the checkpoint, so rebuilding the payload costs a
    checkpoint read and nothing else - no LLM call, no tool call, no node execution. Five of
    these values exist *only* there: `score`, `failed_dimensions`, `revision_count`, the
    report's section bodies, and each claim's quote. `jobs.report_json` is null until the
    export gate passes, and no table holds a section body or a verdict quote at all.
    """
    report = state["report"]
    scores = state["reflection_scores"]
    latest = scores[-1] if scores else None
    return {
        "job_id": state["job_id"],
        "unsupported_claims": unsupported_claims(state),
        "unresearched_subtopics": unresearched_subtopics(state),
        # None means the rubric ran and the report passed. "unscored" means it never ran, and
        # a client that reads an empty `failed_dimensions` as "no problems" without reading
        # this flag has a bug (ARCHITECTURE.md §12).
        "quality_flag": state["quality_flag"],
        "score": latest.model_dump(mode="json") if latest is not None else None,
        "failed_dimensions": list(state["failed_dimensions"]),
        "revision_count": state["revision_count"],
        "llm_calls_used": state["llm_calls_used"],
        "report": report.model_dump(mode="json") if report is not None else None,
        "claims": _claims_with_sources(state),
    }


def unsupported_claims(state: ResearchState) -> list[str]:
    """Claim ids in the *current* draft the Fact-Checker did not support.

    Verdicts accumulate across passes (guidelines §4), so the current draft's claim ids are
    what filters them - an earlier draft's unsupported claim is not this draft's problem.
    """
    report = state["report"]
    if report is None:
        return []
    current = {claim.claim_id for claim in report.claims}
    return [
        verdict.claim_id
        for verdict in state["verdicts"]
        if verdict.claim_id in current and not verdict.supported
    ]


def unresearched_subtopics(state: ResearchState) -> list[str]:
    return [
        subtopic
        for subtopic, status in state["subtopic_status"].items()
        if status == "unresearched"
    ]


def _claims_with_sources(state: ResearchState) -> list[dict[str, Any]]:
    """Every claim with the URLs it rests on and the quote the Fact-Checker found.

    This is the part of the payload that makes an approval a judgement rather than a
    formality: the reviewer sees what each sentence is standing on.
    """
    report = state["report"]
    if report is None:
        return []
    urls = {finding.finding_id: str(finding.url) for finding in state["findings"]}
    verdicts = {verdict.claim_id: verdict for verdict in state["verdicts"]}
    claims = []
    for claim in report.claims:
        verdict = verdicts.get(claim.claim_id)
        claims.append(
            {
                "claim_id": claim.claim_id,
                "text": claim.text,
                "sources": [urls[fid] for fid in claim.finding_ids if fid in urls],
                "supported": None if verdict is None else verdict.supported,
                "quote": None if verdict is None else verdict.quote,
                "note": None if verdict is None else verdict.note,
            }
        )
    return claims


EditRefusal = Literal["reviewer_edit_limit_reached", "insufficient_call_budget_for_edit"]
"""Why a reviewer edit was not allowed to start. Each is a stable error code the step-18
endpoint returns with a `409`, so a caller branches on the string rather than on prose."""

EDIT_CALL_COST = 3
"""Logical calls one reviewer edit needs: a Synthesizer pass, a Fact-Checker pass, and a
reflection pass. The Supervisor hop it also costs is already inside `MAX_SUPERVISOR_HOPS`
(guidelines §13's `1 + 24 + 45 + 3 × (3 + E)`), and this is the minimum - a validation retry
buys a second request for the same logical call, which is what the budget guard still
backstops (ADR 0006 decision 7)."""

TERMINAL_STATUSES: frozenset[JobStatus] = frozenset({"approved", "rejected", "failed"})
"""The three `status` values a job may end on (ARCHITECTURE.md §3). Reaching `finalize` on
any other value is a wiring bug, and `finalize` says so rather than inventing an outcome."""


def refuse_edit(*, config: Config, llm_calls_used: int, edits_made: int) -> EditRefusal | None:
    """May a reviewer edit start? `None` means yes (ADR 0006 decisions 6 and 7).

    Decided **before** the graph is resumed, so a refused edit spends nothing. Both bounds
    protect the same thing: a reviewer holding an approvable report, who asks for a change
    and gets a failed job instead - which is what happens today when the Supervisor's budget
    guard trips halfway through the edit pass, because `export` then never runs.

    `llm_calls_used` is the **live** count, from the checkpoint by way of the gate payload.
    It is never `jobs.llm_calls_used`: that column is written by `finalize` and reads `0` for
    the whole time a job waits at the gate, so a check against it would compute a full budget
    every time and allow every edit silently.

    The endpoint in step 18 turns each answer into a `409` with the reason as its stable
    error code. This function is the decision; the HTTP shape is not.
    """
    if edits_made >= config.max_reviewer_edits:
        return "reviewer_edit_limit_reached"
    if config.max_llm_calls_per_job - llm_calls_used < EDIT_CALL_COST:
        return "insufficient_call_budget_for_edit"
    return None


def run_config(job_id: str) -> RunnableConfig:
    """Which thread this run belongs to, and nothing else.

    `thread_id = job_id` is the pairing that gives one job one checkpoint history, and it is
    what makes the human gate resumable across a restart (guidelines §4).
    """
    return {"configurable": {"thread_id": job_id}}


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


def state_serde() -> JsonPlusSerializer:
    """What a checkpoint may rebuild, wherever it is stored.

    Both savers take this one, because the answer does not depend on where the bytes go: the
    five state models and the serializer's own safe types, and nothing else this process
    happens to be able to import (guidelines §16).
    """
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINTED_TYPES)
