"""
WHY THIS FILE EXISTS
    The Supervisor decides which agent runs next. It is also the single most important
    security boundary in the system: it routes on structured state and nothing else, so no
    page, however well crafted, can reach the component that chooses what happens next
    (guidelines §2.1, §8).

    That boundary is enforced here by construction rather than by intention. The prompt this
    file builds contains only booleans and counts - is there a plan, how many subtopics are
    pending, how many claims lack a verdict, how many hops and calls have been spent. No
    question text, no subtopic text, no claim text, no page text. There is nothing in it for
    an injected instruction to travel in.

    The code decides; the model explains. `allowed_target()` is the transition table in
    guidelines §5 written in plain Python, and it is the sole authority for the route. The
    LLM call is advisory: it runs, its proposal is logged, and it never controls or blocks
    where the job goes (ADR 0001).

    That split exists because of what the first two real jobs did. The route returned here
    was always `allowed_target(state)` - the model's answer only ever reached an equality
    check - so a proposal could not select a route, it could only agree, or disagree and
    kill the job. Two different fast models disagreed on 2 of 10 calls and killed both jobs.
    Validation cannot catch this: `next` is a Literal of the five node names, so a wrong
    target is schema-valid, and the client's validation retry never fires. Asking a model a
    question whose answer is already known can only lose.

    What the advisory call still earns: the disagreement rate is a measurable signal about
    the fast model, logged on every hop, and it is the evidence that decides whether the
    call is worth keeping at all (ADR 0001, "revisit when").

    Reflection is deliberately absent from the target literal. The graph reaches it by a
    fixed edge after the Fact-Checker, which is what keeps it control flow rather than
    something the Supervisor can delegate to (guidelines §5).

WHO CALLS IT
    The `supervisor` graph node (implementation step 10). Tests call it directly.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import NamedTuple, TypedDict

from config import Config
from graph.state import ResearchState
from llm_client import JOB_FATAL_REASONS, CallBudget, LLMCallFailed, LLMClient
from schemas import JobStatus, SupervisorDecision, SupervisorTarget

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You route a research graph. You are given counters describing one job and you name "
    "the node that runs next.\n"
    "A subtopic is resolved when it is done OR unresearched. Only a pending subtopic is "
    "unresolved: `unresearched` means it was already attempted and yielded nothing, so it "
    "is finished and must never be sent back to the researcher.\n"
    "The rules, in order:\n"
    "  no plan yet                                  -> planner\n"
    "  subtopics_pending is 1 or more               -> researcher\n"
    "  subtopics_pending is 0 and no draft yet      -> synthesizer\n"
    "  a draft exists with claims lacking a verdict -> fact_checker\n"
    "  a loop guard has tripped                     -> finalize\n"
    "You see counters and nothing else - no page text, no report text, no search results - "
    "because routing must not depend on anything a third party wrote. Give the rule you "
    "applied as the reason."
)
"""ADR 0001 point 6: `pending` is the only unresolved state, said out loud.

The old wording asked for "every subtopic resolved" without defining "resolved", while the
counter beside it is called `subtopics_unresearched` - which reads as "not researched yet,
so research it". A real job routed on that reading. The route no longer depends on the
model, so this is not load-bearing; a clearer prompt just makes the disagreement rate a
cleaner signal.
"""


class SupervisorUpdate(TypedDict, total=False):
    """The state fields the Supervisor owns (ARCHITECTURE.md §5)."""

    hop_count: int
    llm_calls_used: int
    status: JobStatus
    failure_reason: str


class SupervisorOutcome(NamedTuple):
    """What the Supervisor produces: a routing decision and a state update.

    The decision is not a state field. `ResearchState` has no `next`, because the graph's
    conditional edge consumes the target directly (ARCHITECTURE.md §3) - so it is returned
    alongside the update rather than written into state.
    """

    decision: SupervisorDecision
    update: SupervisorUpdate


def decide_next(state: ResearchState, *, config: Config, llm: LLMClient) -> SupervisorOutcome:
    """Name the next node, or finalize the job.

    The route is `allowed_target(state)` on every path that continues the job. The advisory
    call is made after the route is already known, and cannot change it (ADR 0001).

    The two loop guards are checked before the call, so a job that has already run out of
    hops or budget does not spend another call discovering that.
    """
    hop = state["hop_count"] + 1

    if state["hop_count"] >= config.max_supervisor_hops:
        return _stop(
            hop,
            "hop_limit_exceeded",
            f"routing stopped after {state['hop_count']} hops",
        )
    if state["llm_calls_used"] >= config.max_llm_calls_per_job:
        return _stop(
            hop,
            "budget_exceeded",
            f"job has used its budget of {config.max_llm_calls_per_job} LLM calls",
        )

    allowed = allowed_target(state)
    if allowed is None:
        # Every claim has a verdict, which the fixed fact_checker -> reflection edge means
        # the Supervisor never sees. Arriving here is a wiring bug, and a job that ends
        # loudly is cheaper to diagnose than one that routes somewhere plausible.
        logger.error("supervisor reached in a state the transition table does not cover")
        return _stop(hop, "no_valid_transition", "no transition applies to this state")

    budget = CallBudget(limit=config.max_llm_calls_per_job, used=state["llm_calls_used"])
    try:
        proposal = llm.call_structured(
            schema=SupervisorDecision,
            system=_SYSTEM,
            user=_summary(state),
            budget=budget,
            tier="fast",
        )
    except LLMCallFailed as error:
        if error.reason in JOB_FATAL_REASONS:
            # ADR 0001 point 5, unchanged from before this ADR. `budget_exceeded` is
            # CLAUDE.md invariant 3. `rate_limited` stays fatal because both tiers share one
            # 40 RPM account limit (guidelines §13): a rate-limited routing call means the
            # next Researcher call is rate limited too, so failing here fails early with an
            # accurate reason rather than deeper with the same one.
            logger.error("supervisor stopping the job (%s): %s", error.reason, error)
            return SupervisorOutcome(
                SupervisorDecision(next="finalize", reason=f"routing failed: {error.reason}"),
                SupervisorUpdate(
                    hop_count=hop,
                    llm_calls_used=budget.used,
                    status="failed",
                    failure_reason=error.reason,
                ),
            )
        # ADR 0001 point 4: the route never depended on this call, so a call that broke
        # cannot block it. Loud, because an advisory signal that is silently absent is worse
        # than no signal - the disagreement rate would quietly become unmeasurable.
        logger.warning(
            "supervisor advisory unavailable (%s): %s; routing to %s from state",
            error.reason,
            error,
            allowed,
        )
        return _route(allowed, hop, budget, f"advisory unavailable ({error.reason})")

    if proposal.next != allowed:
        # ADR 0001 point 3. This is the line the disagreement rate is measured from.
        logger.warning(
            "supervisor advisory disagreed: proposed %r, routing to %r from state",
            proposal.next,
            allowed,
        )
        return _route(allowed, hop, budget, f"advisory proposed {proposal.next}")

    logger.info("routing to %s: %s", allowed, proposal.reason)
    return _route(allowed, hop, budget, proposal.reason)


def allowed_target(state: ResearchState) -> SupervisorTarget | None:
    """The one target guidelines §5 allows for this state, or None when none applies.

    Every condition reads a structured field: a None check, a dict of statuses, and a set
    comparison over claim ids. None of them reads text a third party wrote.

    None means the job falls through to the reflection node, which is a fixed graph edge
    rather than a Supervisor decision - `reflection` is not in the target literal.
    """
    if state["plan"] is None:
        return "planner"
    if any(status == "pending" for status in state["subtopic_status"].values()):
        return "researcher"

    report = state["report"]
    if report is None:
        return "synthesizer"

    checked = {verdict.claim_id for verdict in state["verdicts"]}
    if any(claim.claim_id not in checked for claim in report.claims):
        return "fact_checker"
    return None


def _route(
    target: SupervisorTarget, hop: int, budget: CallBudget, reason: str
) -> SupervisorOutcome:
    """Continue the job on the state's own route, whatever the advisory call said.

    `reason` is the only thing that varies between the three ways of getting here - the
    model's rationale when it agreed, and a code-written note when it disagreed or could not
    be reached. The target never varies: it is `allowed_target(state)` (ADR 0001).
    """
    return SupervisorOutcome(
        SupervisorDecision(next=target, reason=reason),
        SupervisorUpdate(hop_count=hop, llm_calls_used=budget.used),
    )


def _stop(hop: int, reason: str, explanation: str) -> SupervisorOutcome:
    """A guard tripped. Route to finalize with the reason recorded - never silently."""
    logger.error("supervisor stopping the job (%s): %s", reason, explanation)
    return SupervisorOutcome(
        SupervisorDecision(next="finalize", reason=explanation),
        SupervisorUpdate(hop_count=hop, status="failed", failure_reason=reason),
    )


def _summary(state: ResearchState) -> str:
    """The routing prompt: booleans and counts, and nothing else.

    Claim and subtopic ids are left out even though they are short. They are strings that
    were produced downstream of fetched pages, and counting them answers every question the
    transition table asks - so there is no reason to carry the text.
    """
    statuses = Counter(state["subtopic_status"].values())
    report = state["report"]
    checked = {verdict.claim_id for verdict in state["verdicts"]}
    unverified = (
        0 if report is None else sum(1 for claim in report.claims if claim.claim_id not in checked)
    )

    return (
        f"plan_exists: {state['plan'] is not None}\n"
        f"subtopics_pending: {statuses['pending']}\n"
        f"subtopics_done: {statuses['done']}\n"
        f"subtopics_unresearched: {statuses['unresearched']}\n"
        f"findings_collected: {len(state['findings'])}\n"
        f"draft_exists: {report is not None}\n"
        f"claims_in_draft: {0 if report is None else len(report.claims)}\n"
        f"claims_without_a_verdict: {unverified}\n"
        f"hops_used: {state['hop_count']}\n"
        f"llm_calls_used: {state['llm_calls_used']}"
    )
