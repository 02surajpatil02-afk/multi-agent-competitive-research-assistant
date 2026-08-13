"""
WHY THIS FILE EXISTS
    Every boundary in this system is a typed schema, so no agent ever parses another
    agent's prose (guidelines §3). This file holds all of them in one place, because they
    are small and they only make sense together: a Verdict points at a Claim, a Claim
    points at Findings, and a Report is the thing the reflection node scores.

    The validation rules here are the ones the guidance states as invariants, not general
    strictness. A Report with no sources is ungrounded, a Claim with no finding_ids breaks
    the audit trail, and a Verdict that says "supported" without a quote is the exact
    failure the Fact-Checker contract exists to prevent. Each one fails validation, which
    triggers the single bounded retry and then an explicit failure - never a default
    substituted for a field the model did not produce.

    It deliberately holds no behaviour. Weighting a ReflectionScore, choosing a route, and
    enforcing the Supervisor transition table are decisions made in code elsewhere; a
    schema that scored itself would put reflection logic in the type system.

WHO CALLS IT
    The five agent modules, the reflection node, the tool boundary, graph/state.py, and
    the database layer that persists findings and claims.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl, PlainSerializer, model_validator

Url = Annotated[HttpUrl, PlainSerializer(str, return_type=str)]
"""An http/https URL that validates like ``HttpUrl`` and dumps as a plain string.

``HttpUrl`` validates, but ``model_dump()`` leaves the value as a ``Url`` object, so a
model carrying one cannot be handed to anything that serialises: ``json.dumps`` refuses it,
and so does LangGraph's checkpointer, which is what puts ``Finding`` and ``Report`` into a
checkpoint on every node. Saying how the field serialises here fixes that once, for the
checkpoint, for ``jobs.report_json``, and for the API - rather than at each call site, where
one forgotten ``mode="json"`` is a crash in the middle of a job.

Validation is unchanged, so a string round-trips back to a validated URL on the way in.
"""

# --- Shared vocabulary -------------------------------------------------------------
# One definition per set of allowed values, so the state, the API, and the database
# cannot drift apart on what a status or a flag is allowed to be.

SupervisorTarget = Literal["planner", "researcher", "synthesizer", "fact_checker", "finalize"]
"""Reflection is absent on purpose: the Supervisor cannot route to it. The graph reaches
the reflection node by a fixed edge after the Fact-Checker, which is what keeps it control
flow rather than a delegation (guidelines §5)."""

ReflectionRoute = Literal["researcher", "synthesizer", "fact_checker", "human_gate"]

SubtopicStatus = Literal["pending", "done", "unresearched"]

JobStatus = Literal["running", "awaiting_approval", "approved", "rejected", "failed"]

QualityFlag = Literal["below_threshold", "unscored"]
"""Carried alongside ``None``, which means the rubric ran and the report passed.

``"unscored"`` means the rubric never ran. The two must never be collapsed: an unscored
report has had no automated quality judgement at all, so the reviewer is the only one in
the loop (ARCHITECTURE.md §6)."""

REFLECTION_DIMENSIONS = (
    "research_completeness",
    "source_correctness",
    "citation_coverage",
    "factual_consistency",
    "report_quality",
)
"""The five dimensions, in rubric order. The same vocabulary is used inline as a gate and
offline as a measurement (guidelines §6, §15)."""


# --- Supervisor --------------------------------------------------------------------


class SupervisorDecision(BaseModel):
    """What the Supervisor proposes. The transition table then disposes, in plain Python.

    A decision outside the table is rejected in code rather than trusted because the model
    produced it (guidelines §5).
    """

    next: SupervisorTarget
    reason: str


# --- Planner -----------------------------------------------------------------------


MAX_SEARCH_QUERY_CHARS = 120
"""How long a subtopic's search query may be.

Generous enough that a real query fits and tight enough that a natural-language sentence does
not. Enforced by validation rather than by truncation because cutting a query mid-word makes a
different, worse query - and the client's one bounded retry carries the error back, which is
the mechanism that already exists for output that does not meet a contract (guidelines §3)."""


class Subtopic(BaseModel):
    """One researchable slice of the question, plus the query that finds sources for it.

    `question` is what the Researcher's extraction prompt answers against; `search_query` is
    what reaches the search tool. They are two different jobs, and conflating them cost real
    findings: step 12 sent 150-character natural-language questions to Tavily verbatim and got
    back dictionary definitions of the word "what", leaving subtopics `unresearched`.

    Both come from the plan, so neither is text a third party wrote - the rule that a tool
    argument never originates in a fetched page is unchanged (guidelines §8).
    """

    id: str
    question: str
    search_query: str = Field(min_length=1, max_length=MAX_SEARCH_QUERY_CHARS)


class ResearchPlan(BaseModel):
    """3-5 subtopics and what a good answer would contain.

    The job never proceeds on an empty plan: researching an unplanned question produces a
    report nobody can evaluate, because there is no stated success criterion to evaluate
    it against (guidelines §2.2).
    """

    subtopics: list[Subtopic] = Field(min_length=3, max_length=5)
    success_criteria: list[str] = Field(min_length=1)


# --- Researcher --------------------------------------------------------------------


class Finding(BaseModel):
    """One piece of evidence, tied to the URL it came from.

    ``evidence`` is a verbatim span from the fetched text, never a summary - the audit
    trail depends on it being locatable in the source later (guidelines §2.3, §9).
    """

    finding_id: str
    subtopic_id: str
    claim: str
    evidence: str
    url: Url
    title: str
    retrieved_at: datetime
    content_hash: str  # sha256 of the fetched text, so a changed page is detectable
    truncated: bool
    """True when the page was cut at MAX_PAGE_CHARS. A quote that cannot be found because
    the text was never read is a different failure from a quote that was invented, and the
    Fact-Checker cannot tell them apart without this flag."""


# --- Synthesizer -------------------------------------------------------------------


class Section(BaseModel):
    id: str
    heading: str
    body: str


class Claim(BaseModel):
    """A sentence in the report, with the findings it came from.

    ``finding_ids`` is never empty. It is the audit link that becomes a ``claim_sources``
    row, and the export gate refuses any claim that cannot reach a source URL through it
    (guidelines §9).
    """

    claim_id: str
    section_id: str
    text: str
    finding_ids: list[str] = Field(min_length=1)


class Source(BaseModel):
    """A cited URL. ``finding_ids`` is non-empty because Report.sources is a view over the
    findings actually cited, not an independent store - a source no finding retrieved is
    exactly the drift the audit trail exists to prevent (ARCHITECTURE.md §9)."""

    url: Url
    title: str
    finding_ids: list[str] = Field(min_length=1)


class Report(BaseModel):
    """The current draft. One draft exists at a time.

    An empty ``sources`` list means the report is ungrounded. That is a failure, not a
    result: do not return it, do not log a warning and continue (guidelines §3).
    """

    sections: list[Section]
    claims: list[Claim]
    sources: list[Source] = Field(min_length=1)


# --- Fact-Checker ------------------------------------------------------------------


class Verdict(BaseModel):
    """One claim, checked against its cited source text.

    The strictest contract in the system. The Fact-Checker either produces a verbatim
    quote that supports the claim, or marks it unsupported. It never infers (§2.5).
    """

    claim_id: str
    supported: bool
    quote: str | None = None
    note: str

    @model_validator(mode="after")
    def _supported_needs_a_quote(self) -> Verdict:
        # supported=true with no quote is a schema violation, not a tolerable gap: it is
        # what "the source implies this" looks like once it reaches the report. A blank
        # quote is the same violation with whitespace in it.
        if self.supported and not (self.quote or "").strip():
            raise ValueError("supported=true requires a verbatim quote from the source")
        return self


# --- Human approval gate (not an agent) --------------------------------------------


class GateDecision(BaseModel):
    """What a reviewer decided at the human gate.

    The same shape as ``POST /jobs/{id}/approve``'s request body (ARCHITECTURE.md §10),
    because the endpoint's job is to record the identity and hand this straight to the
    graph. In Phase 1 it arrives as the resume value; from Phase 2 the endpoint sends it.
    """

    decision: Literal["approve", "reject", "edit"]
    note: str | None = None
    edits: str | None = None
    """The reviewer's text, which becomes ``reviewer_edit_text`` for one Synthesizer pass."""

    @model_validator(mode="after")
    def _edit_needs_text(self) -> GateDecision:
        # An edit with nothing to apply would route a Synthesizer pass that has been given
        # no instruction, which costs two calls and produces the same draft again.
        if self.decision == "edit" and not (self.edits or "").strip():
            raise ValueError("an edit decision requires the reviewer's text in `edits`")
        return self


# --- Reflection node (not an agent) ------------------------------------------------


class ReflectionScore(BaseModel):
    """One reflection pass: what the model scored, and what the code decided.

    The model returns the five dimension scores and a rationale. ``weighted_score``,
    ``failed_dimensions``, and ``route`` are computed in plain Python by the reflection
    node and passed in when the object is built - which is why they are required here
    rather than defaulted. This mirrors "the model proposes; the code disposes" and
    shrinks what an injected page can influence to five integers rather than a routing
    string (ARCHITECTURE.md §6, §20 row 23).
    """

    research_completeness: int = Field(ge=1, le=5)
    source_correctness: int = Field(ge=1, le=5)
    citation_coverage: int = Field(ge=1, le=5)
    factual_consistency: int = Field(ge=1, le=5)
    report_quality: int = Field(ge=1, le=5)
    rationale: str

    weighted_score: float = Field(ge=1.0, le=5.0)  # weights sum to 1.0, so 1-5 is the range
    failed_dimensions: list[str]
    route: ReflectionRoute


# --- Tool boundary -----------------------------------------------------------------


class SearchResult(BaseModel):
    """Everything the tool layer returns is normalised to this shape before an agent sees
    it. Tavily is used because it returns cleaned content and the URL together, which is
    what the audit trail needs (guidelines §7)."""

    title: str
    url: Url
    content: str  # cleaned page text, not a snippet
    published_at: datetime | None = None
    truncated: bool = False
    """True when `content` was cut at MAX_PAGE_CHARS.

    Not in the schema as guidelines §7 prints it. It is needed because two documented
    rules meet here: ARCHITECTURE.md §7 caps page text at normalisation, and guidelines
    §2.3 requires the resulting Finding to carry truncated=true. Without a field the tool
    layer has no way to tell the Researcher which of the two happened, and a Finding whose
    quote is missing because the text was cut is indistinguishable from one that was
    invented. Defaulted, so every construction in the guidance still validates."""


class FetchedPage(BaseModel):
    """One page retrieved by `fetch`, cleaned and capped.

    The counterpart to SearchResult on the fetch path. The guidance names the fields it
    needs - Finding carries `content_hash` and `truncated`, and reproducibility needs the
    retrieval time - without naming a type to carry them between the tool and the agent.
    """

    url: Url  # the FINAL url, after any redirects were followed and re-validated
    title: str
    text: str  # cleaned, capped at MAX_PAGE_CHARS
    truncated: bool
    content_hash: str
    """sha256 of `text` - the cleaned, capped text, which is what the model actually read.

    Hashing the cleaned text rather than the raw bytes is what makes "is this the page the
    claim was made from?" answerable in June for a claim made in March: it is stable
    against markup and ad churn that changes the bytes without changing the prose
    (guidelines §9)."""
    retrieved_at: datetime
