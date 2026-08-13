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

WHO CALLS IT
    Every graph node - the five agents, the reflection node, the human gate, export, and
    finalize - plus the worker, which builds the initial state for a new job.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

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
