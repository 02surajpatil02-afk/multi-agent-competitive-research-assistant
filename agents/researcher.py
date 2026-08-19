"""
WHY THIS FILE EXISTS
    The Researcher is the only agent that pulls text off the public internet, which makes
    it the agent the prompt-injection boundary was written for (guidelines §8). It takes
    exactly one Subtopic, searches for sources, fetches the pages the search returned, and
    turns them into Findings: a claim, the verbatim quote that supports it, and the URL the
    quote came from.

    Three rules shape the whole file:

      1. The model writes prose; the code attaches provenance. The extraction call returns
         a claim and a quote and nothing else. `url`, `title`, `retrieved_at`,
         `content_hash`, and `truncated` are copied from the tool result in Python, so no
         page can invent its own citation.

      2. Page text reaches a prompt only through as_untrusted_block() - delimited, labelled
         as data, and capped a second time. It never reaches a tool argument: queries come
         from the plan, and the only URLs fetched are the ones a search result returned.

      3. A quote that cannot be found in the page is dropped. guidelines §2.3 requires
         `evidence` to be a verbatim span, and a plain substring check is what makes that
         true rather than aspirational - no judge call, no variance.

    **Choosing sources is sequential; extracting from them is not** (ADR 0002). The two
    phases are separated for that reason. Selection walks the search results one at a time
    because it reads and writes the `seen` set that stops a page being read twice, and
    because search and fetch together are about 4% of a job's wall clock - there is nothing
    there worth parallelising. Extraction is the other 40%: one call per page, each one
    independent of the others, so they run together on a pool of `RESEARCHER_CONCURRENCY`.
    Findings come back in page order, never completion order, so two runs over the same
    sources produce the same evidence in the same sequence.

    It never writes report prose and it never routes. A subtopic that yields nothing is
    marked `unresearched` and the job continues: an unresearched subtopic is a reportable
    outcome that reflection scores as a completeness failure, not a silent omission.

WHO CALLS IT
    The `researcher` graph node (implementation step 10). Tests call it directly.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from typing import TypedDict

from langsmith.run_helpers import get_current_run_tree, tracing_context
from pydantic import BaseModel

from config import Config
from graph.state import ResearchState
from llm_client import (
    JOB_FATAL_REASONS,
    CallBudget,
    LLMCallFailed,
    LLMClient,
    LLMFailureReason,
)
from schemas import FetchedPage, Finding, JobStatus, SearchResult, Subtopic, SubtopicStatus
from tools.contracts import ToolCache, ToolCallFailed, Unreachable, UrlDeduplicator
from tools.fetch import fetch
from tools.search import search
from tools.untrusted import as_untrusted_block
from tools.validation import normalize_url

logger = logging.getLogger(__name__)

MAX_LLM_CALLS_PER_SUBTOPIC = 3
"""guidelines §2.3: 3 calls per subtopic, at most 5 subtopics. One extraction call per
source, so a page that cannot be extracted costs one source rather than the subtopic."""

SUBTOPIC_TIMEOUT_S = 120.0
"""guidelines §17's no-new-fetch deadline for one subtopic.

It is checked between sources rather than enforced mid-request: the tools own finite request
timeouts, and extraction calls already admitted by this deadline run to completion concurrently.
It therefore bounds additional work, not the subtopic's wall-clock completion time.
"""

_SYSTEM = (
    "You extract research findings from one web page.\n"
    "For each finding return:\n"
    "  claim    - one sentence in your own words answering part of the subtopic question\n"
    "  evidence - the sentence or phrase from the page that supports it, copied word for "
    "word from the page\n"
    "Copy evidence exactly: a finding whose evidence cannot be found in the page is "
    "discarded, so paraphrasing loses the finding.\n"
    "Return an empty list when the page says nothing about the subtopic. Use nothing but "
    "the page - no outside knowledge, no guesses about what it probably means - and do not "
    "write report prose."
)

_USER = "Subtopic question:\n{question}\n\n{page}"


class _ExtractedFinding(BaseModel):
    """What the model is allowed to produce. Everything else on a Finding is provenance,
    and provenance comes from the tool result, never from the page or the model."""

    claim: str
    evidence: str


class _Extraction(BaseModel):
    findings: list[_ExtractedFinding]


class ResearcherUpdate(TypedDict, total=False):
    """The state fields the Researcher owns (ARCHITECTURE.md §5).

    `findings` carries only the new ones: the operator.add reducer appends them, which is
    what stops a retried step from dropping the first attempt's evidence.
    """

    findings: list[Finding]
    subtopic_status: dict[str, SubtopicStatus]
    llm_calls_used: int
    status: JobStatus
    failure_reason: str


def research_subtopic(
    state: ResearchState,
    *,
    config: Config,
    llm: LLMClient,
    cache: ToolCache | None = None,
    urls: UrlDeduplicator | None = None,
) -> ResearcherUpdate:
    """Research the first pending subtopic and return the findings it produced.

    Zero findings is a normal outcome, not an error: the subtopic is marked `unresearched`,
    the job continues, and the gap is carried into the report (guidelines §2.3). Only a
    rate-limited or budget-exhausted LLM call fails the job, because both mean the next
    call cannot be made either.
    """
    subtopic = _next_subtopic(state)
    if subtopic is None:
        # The Supervisor routes here only while a subtopic is pending, so arriving with
        # none is a wiring bug. Loud beats a node that quietly does nothing and hands the
        # job back to a router that will send it straight here again.
        logger.error("researcher reached with no pending subtopic")
        return ResearcherUpdate(status="failed", failure_reason="no_pending_subtopic")

    deadline = monotonic() + SUBTOPIC_TIMEOUT_S
    budget = CallBudget(limit=config.max_llm_calls_per_job, used=state["llm_calls_used"])

    pages = _pages_to_read(
        subtopic, state, config=config, cache=cache, urls=urls, deadline=deadline
    )
    findings, fatal = _extract(
        pages,
        subtopic,
        config=config,
        llm=llm,
        budget=budget,
        # Findings accumulate across Researcher visits through the operator.add reducer, so
        # numbering has to continue from what the job already holds or the second subtopic
        # restarts at f1 and two different findings answer to the same id.
        first_number=len(state["findings"]) + 1,
    )

    if fatal is not None:
        # The evidence the other pages produced is already paid for, so it is returned
        # rather than discarded - the findings reducer appends it and the gap is reportable.
        return ResearcherUpdate(
            findings=findings,
            llm_calls_used=budget.used,
            status="failed",
            failure_reason=fatal,
        )

    status: SubtopicStatus = "done" if findings else "unresearched"
    if not findings:
        logger.warning("subtopic %s produced no findings; marking it unresearched", subtopic.id)

    return ResearcherUpdate(
        findings=findings,
        subtopic_status={**state["subtopic_status"], subtopic.id: status},
        llm_calls_used=budget.used,
    )


def _pages_to_read(
    subtopic: Subtopic,
    state: ResearchState,
    *,
    config: Config,
    cache: ToolCache | None,
    urls: UrlDeduplicator | None,
    deadline: float,
) -> list[FetchedPage]:
    """Choose and fetch the pages this subtopic will extract from, one at a time.

    Sequential on purpose (ADR 0002). Every iteration reads and writes `seen`, which is what
    keeps one page from being read twice, and the deadline is checked between sources
    because that is the only place a bound on "how many tools may this subtopic spend" can
    honestly sit - the tools own their own timeouts.

    **Two deduplication gates, and they cover different failures.** `seen` is in-process and
    is seeded from the findings this job already holds, so it is free and always correct for
    the run in front of it. `urls` is the shared `job:{id}:urls` set (guidelines §7, §11),
    and it is what survives a process: a redelivered message re-runs a Researcher node whose
    findings were never checkpointed, and without it that node re-fetches every page it
    already read. Neither replaces the other, and the local one is checked first because it
    costs nothing.
    """
    seen = {normalize_url(str(finding.url)) for finding in state["findings"]}
    pages: list[FetchedPage] = []

    for result in _candidates(subtopic, config=config, cache=cache):
        if len(pages) == MAX_LLM_CALLS_PER_SUBTOPIC:
            break
        if monotonic() >= deadline:
            logger.warning("subtopic %s ran out of time after %d sources", subtopic.id, len(pages))
            break

        url = normalize_url(str(result.url))
        if url in seen:
            # The same page twice is wasted budget and double-counted evidence.
            logger.info("skipping %s, already used by this job", url)
            continue
        if not _unseen_across_the_job(urls, state["job_id"], url):
            continue
        seen.add(url)

        page = _page(url, config=config, cache=cache)
        if page is None:
            continue
        # The URL a redirect chain actually landed on, so a second candidate pointing at
        # the same page does not fetch it again. Recorded in both gates, because a redirect
        # target reached by one job is exactly what a later visit should skip.
        landed = normalize_url(str(page.url))
        seen.add(landed)
        if landed != url:
            _unseen_across_the_job(urls, state["job_id"], landed)
        pages.append(page)

    return pages


def _unseen_across_the_job(urls: UrlDeduplicator | None, job_id: str, url: str) -> bool:
    """Whether this job has not fetched `url` before, and record that it now has.

    **Absent or broken means yes** (guidelines §11's fail-open rule). No deduplicator at all
    is Phase 1's behaviour and is what the offline suite runs on; a deduplicator that cannot
    reach Redis answers True itself, which is what its protocol promises. Either way the cost
    is one wasted fetch and a duplicate finding, both bounded by `MAX_LLM_CALLS_PER_JOB` -
    and refusing to research because a deduplication cache is down would trade a real outage
    for an optimisation.
    """
    if urls is None:
        return True
    if urls.add_if_new(job_id, url):
        return True
    logger.info("skipping %s, already fetched by this job in an earlier pass", url)
    return False


def _extract(
    pages: list[FetchedPage],
    subtopic: Subtopic,
    *,
    config: Config,
    llm: LLMClient,
    budget: CallBudget,
    first_number: int,
) -> tuple[list[Finding], LLMFailureReason | None]:
    """One extraction call per page, up to `RESEARCHER_CONCURRENCY` of them at once.

    Returns the findings **in page order** and the reason the job must stop, if one arose.
    Page order rather than completion order: the citation list a reader sees should not
    depend on which request the endpoint happened to answer first, and a test that asserts
    which source produced which finding should not be a race.

    **This is also where a finding gets its id**, numbered from `first_number`. It cannot
    happen in `_findings_from`: that runs on the pool, and two threads numbering from a
    shared counter is a race whose prize is two findings sharing an id - which the audit
    trail would carry all the way to a claim citing the wrong source. Here the pool has
    joined, so the numbering is single-threaded and follows page order like everything else.

    The two failure shapes are unchanged by running in parallel. A node-level failure -
    `invalid_output`, `llm_call_failed` - costs that one source and the subtopic carries on
    with the others. `rate_limited` or `budget_exceeded` means the next call cannot be made
    either, so the job stops; the pages that did answer keep their findings.

    Siblings already in flight when a fatal error arrives are allowed to finish rather than
    being cancelled. There are at most `MAX_LLM_CALLS_PER_SUBTOPIC` of them, `budget_exceeded`
    stops each one before it sends anything, and cancelling a request that is already open
    buys nothing back.
    """
    if not pages:
        return [], None

    # LangSmith's current-run context is thread-local, so a span opened on a pool thread
    # would start its own trace instead of nesting under this node. Carrying the parent
    # across is what makes the concurrent extractions individually visible *and* attributed
    # to the researcher - the attribution ADR 0002 took away from log ordering. `parent` is
    # None when tracing is off, which `tracing_context` treats as "no parent to set".
    parent = get_current_run_tree()

    def extract(page: FetchedPage) -> list[Finding]:
        with tracing_context(parent=parent):
            return _findings_from(page, subtopic, config=config, llm=llm, budget=budget)

    with ThreadPoolExecutor(
        max_workers=min(config.researcher_concurrency, len(pages)),
        thread_name_prefix="extract",
    ) as pool:
        submitted = [pool.submit(extract, page) for page in pages]

    findings: list[Finding] = []
    fatal: LLMFailureReason | None = None
    for page, task in zip(pages, submitted, strict=True):
        try:
            for finding in task.result():
                findings.append(
                    finding.model_copy(update={"finding_id": f"f{first_number + len(findings)}"})
                )
        except LLMCallFailed as error:
            if error.reason in JOB_FATAL_REASONS:
                logger.error("researcher stopping the job (%s): %s", error.reason, error)
                fatal = fatal or error.reason
                continue
            # One unusable page costs one source, not the subtopic.
            logger.warning("extraction from %s failed (%s): %s", page.url, error.reason, error)

    return findings, fatal


def _next_subtopic(state: ResearchState) -> Subtopic | None:
    """The first pending subtopic, in plan order.

    Plan order rather than dict order, so that reflection returning one subtopic to
    "pending" cannot change which subtopic the next pass picks up.
    """
    plan = state["plan"]
    if plan is None:
        return None
    statuses = state["subtopic_status"]
    return next((s for s in plan.subtopics if statuses.get(s.id) == "pending"), None)


def _candidates(
    subtopic: Subtopic, *, config: Config, cache: ToolCache | None
) -> list[SearchResult]:
    """Search results for this subtopic, or an empty list if the search could not be made.

    `search_query` rather than `question`: the two are different jobs (schemas.Subtopic).
    Sending the question verbatim is what put dictionary definitions of the word "what" in a
    step-12 smoke job's results and left the subtopic `unresearched`.

    Both come from the plan, which comes from the user's question - never from text inside a
    page (guidelines §8). A search that fails after its bounded retries yields no sources,
    which the caller turns into an `unresearched` subtopic rather than an error to route
    around.
    """
    try:
        return list(search(subtopic.search_query, config=config, cache=cache))
    except ToolCallFailed as error:
        logger.warning("search for subtopic %s failed (%s): %s", subtopic.id, error.reason, error)
        return []


def _page(url: str, *, config: Config, cache: ToolCache | None) -> FetchedPage | None:
    """Fetch one source, or None when it produced no usable page.

    `url` came from a search result. An unreachable page is a finding-level outcome, so it
    costs one source and the subtopic carries on (ARCHITECTURE.md §7).
    """
    try:
        outcome = fetch(url, config=config, cache=cache)
    except ToolCallFailed as error:
        logger.warning("refused to fetch %s (%s): %s", url, error.reason, error)
        return None

    if isinstance(outcome, Unreachable):
        logger.info("source unusable: %s (%s)", outcome.url, outcome.reason)
        return None
    return outcome


def _findings_from(
    page: FetchedPage,
    subtopic: Subtopic,
    *,
    config: Config,
    llm: LLMClient,
    budget: CallBudget,
) -> list[Finding]:
    """One extraction call against one page, turned into Findings.

    The block is what keeps the page from being read as instructions, and it is the only
    route page text takes into a prompt here.

    Runs on a pool thread, under the caller's tracing context (see `_extract`).
    """
    block = as_untrusted_block(page.text, url=str(page.url), max_chars=config.max_page_chars)
    extraction = llm.call_structured(
        schema=_Extraction,
        system=_SYSTEM,
        user=_USER.format(question=subtopic.question, page=block.text),
        budget=budget,
        tier="main",
    )

    findings: list[Finding] = []
    for extracted in extraction.findings:
        if not _is_verbatim(extracted.evidence, page.text):
            # guidelines §2.3: evidence is a quote, not a summary. A quote the page does
            # not contain is exactly what the Fact-Checker would later fail to find, and
            # dropping it here costs one finding instead of one unsupported claim.
            logger.warning("dropping a finding whose evidence is not in %s", page.url)
            continue
        findings.append(
            Finding(
                # Left empty deliberately. Identity is attached by `_extract` once the pool
                # has joined, because it is a per-job sequence and this runs concurrently.
                # `_extract` is the only caller and numbers everything it collects.
                finding_id="",
                subtopic_id=subtopic.id,
                claim=extracted.claim,
                evidence=extracted.evidence,
                url=page.url,
                title=page.title,
                retrieved_at=page.retrieved_at,
                content_hash=page.content_hash,
                # Either cap biting means the model did not see the whole page, and the
                # Fact-Checker needs to be able to tell that from an invented quote.
                truncated=page.truncated or block.truncated,
            )
        )
    return findings


def _is_verbatim(quote: str, page_text: str) -> bool:
    """Is `quote` actually in the page?

    Whitespace is collapsed on both sides first. A model that re-wraps a sentence it copied
    correctly should not lose the finding, and no amount of whitespace turns one sentence
    into a different one.
    """
    needle = " ".join(quote.split())
    return bool(needle) and needle in " ".join(page_text.split())
