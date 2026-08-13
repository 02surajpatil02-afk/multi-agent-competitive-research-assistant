"""
WHY THIS FILE EXISTS
    The Planner turns the user's question into 3-5 researchable subtopics and the success
    criteria a good answer has to meet. It is the first agent to run and it is the cheapest
    place to stop a bad job: researching an unplanned question produces a report nobody can
    evaluate, because there is no stated success criterion to evaluate it against
    (guidelines §2.2). So an invalid plan fails the job rather than degrading into
    "research something".

    It also seeds `subtopic_status`, one "pending" entry per planned subtopic. The plan is
    where those ids are born, and without the seed the Supervisor's next test - "is any
    subtopic pending?" - would be false on an empty dict and route a job with no findings
    straight to the Synthesizer. The Researcher and the reflection node overwrite
    individual keys afterwards (ARCHITECTURE.md §5).

    It has no tools and makes no network call of its own. Everything it produces comes from
    one structured LLM call, and the single bounded validation retry inside that call is the
    "1 + 1 retry" budget guidelines §2.2 gives it.

WHO CALLS IT
    The `planner` graph node (implementation step 10). Tests call it directly.
"""

from __future__ import annotations

import logging
from typing import TypedDict

from config import Config
from graph.state import ResearchState
from llm_client import CallBudget, LLMCallFailed, LLMClient
from schemas import MAX_SEARCH_QUERY_CHARS, JobStatus, ResearchPlan, SubtopicStatus

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You plan competitive research. Break the question into 3 to 5 subtopics that can each "
    "be researched independently on the public web, and state the criteria a good answer "
    "must meet.\n"
    "Give every subtopic a short unique id (s1, s2, s3, ...), a specific question that can "
    "be answered from public sources, and the web search query that will find those "
    "sources.\n"
    "search_query is not the question. Write it the way you would type it into a search "
    "engine: the company and product names that must appear, the key terms, nothing else. "
    "No question words, no filler, at most "
    f"{MAX_SEARCH_QUERY_CHARS} characters.\n"
    "  good: TCS Infosys cloud strategy annual report\n"
    "  bad:  What are the publicly stated cloud strategy goals and priorities of TCS and "
    "Infosys as outlined in their annual reports?\n"
    "You produce the plan and nothing else: do not answer the question, do not state facts "
    "about the companies involved, and do not suggest sources."
)
"""The `bad` example is verbatim from a step-12 smoke job. That query returned dictionary
definitions of the word "what" and left the subtopic `unresearched`, so the prompt shows the
model the real failure rather than describing it."""

_USER = "Research question:\n{question}"


class PlannerUpdate(TypedDict, total=False):
    """The state fields the Planner owns. Nothing else may appear in its return value.

    A per-agent partial-update type is how ARCHITECTURE.md §5's ownership table is checked
    by mypy rather than by review: a Planner that wrote `findings` would not type-check.
    """

    plan: ResearchPlan
    subtopic_status: dict[str, SubtopicStatus]
    llm_calls_used: int
    status: JobStatus
    failure_reason: str


def plan_research(state: ResearchState, *, config: Config, llm: LLMClient) -> PlannerUpdate:
    """Produce the research plan, or fail the job.

    There is no partial success here. Every failure - a call that could not be made, output
    that would not validate, or a plan whose subtopic ids collide - ends with
    `status="failed"` and a reason, because the alternative is a job researching a question
    it never planned.
    """
    budget = CallBudget(limit=config.max_llm_calls_per_job, used=state["llm_calls_used"])

    try:
        plan = llm.call_structured(
            schema=ResearchPlan,
            system=_SYSTEM,
            user=_USER.format(question=state["question"]),
            budget=budget,
            tier="main",
        )
    except LLMCallFailed as error:
        logger.error("planner failed (%s): %s", error.reason, error)
        return PlannerUpdate(
            llm_calls_used=budget.used,
            status="failed",
            failure_reason=error.reason,
        )

    ids = [subtopic.id for subtopic in plan.subtopics]
    if len(set(ids)) != len(ids):
        # Duplicate ids would collapse subtopic_status to fewer entries than the plan has,
        # and every Finding written under the shared id would be attributed to the wrong
        # subtopic. That is an invalid plan, which guidelines §2.2 fails the job for.
        logger.error("planner returned duplicate subtopic ids: %s", ids)
        return PlannerUpdate(
            llm_calls_used=budget.used,
            status="failed",
            failure_reason="invalid_plan",
        )

    pending: dict[str, SubtopicStatus] = {subtopic_id: "pending" for subtopic_id in ids}
    logger.info("planned %d subtopics: %s", len(ids), ids)
    return PlannerUpdate(plan=plan, subtopic_status=pending, llm_calls_used=budget.used)
