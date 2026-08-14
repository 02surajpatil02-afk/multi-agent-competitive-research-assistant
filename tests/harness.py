"""
WHY THIS FILE EXISTS
    Guidelines §18 asks for two things before a whole job can be tested honestly: a FakeLLM
    that returns scripted structured responses per agent, and recorded search and fetch
    responses replayed instead of fetched. This module is both, because a graph test needs
    them together - one deterministic environment for one whole job, with no network in it.

    Three decisions are worth reading before the code.

    **It sits underneath the real client, not beside it.** `FakeLLM` is shaped like the one
    attribute path `LLMClient` uses, so a graph test still runs the real validation retry,
    the real 429 and transport backoff, and the real `CallBudget`. A second LLM client
    wearing a test name would prove nothing about the code that ships, which is why there is
    no `BaseLLM` and no provider class here either.

    **Answers are queued per agent, not per position.** One job interleaves six callers, so a
    single positional script means one extra Researcher call renumbers every entry after it.
    The caller is identified by the JSON Schema `llm_client` puts in the system prompt, so
    each queue reads as that one agent's sequence.

    **An answer may be computed from the request it is answering.** Finding ids are numbered at
    run time (`f1`, `f2`, … - ADR 0003) and claim ids are the Synthesizer's own, so a static
    script cannot cite them. A callable answer reads them out of the prompt it was given - which
    is what a real model does - and that is also what makes "the Synthesizer was actually shown
    these finding ids" assertable.

    Recorded pages are served through the *real* tools. `RecordedWeb` replaces Tavily's
    client and httpx's transport and nothing else, so tools/search.py still normalises the
    provider response and tools/fetch.py still runs its SSRF check, its robots.txt read, its
    redirect handling, its byte cap, and its HTML cleaning. Only the socket is fake.

    Two of the three patches are process-wide for the duration of a test - `socket.getaddrinfo`
    through `patch_dns`, and `httpx.Client` through the `tools.fetch` module's reference to it.
    That is deliberate: `fetch()` builds its own client, so there is no narrower seam, and
    monkeypatch undoes both at the end of the test.

    **Only `FakeLLM` is reached concurrently.** Since ADR 0002 a subtopic's extraction calls
    run on a pool, so its queues are guarded. `RecordedWeb` is not: search and fetch stay on
    the sequential half of the Researcher, so nothing it records is written from two threads.

WHO CALLS IT
    tests/test_graph_integration.py and tests/test_graph_injection.py.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from threading import Lock
from typing import Any

import httpx
import pytest
from fakes import completion, patch_dns

import tools.search
from tools.untrusted import BEGIN_MARKER, END_MARKER

# --- The fake LLM ------------------------------------------------------------------

ROLE_SCHEMAS: dict[str, str] = {
    "supervisor": "SupervisorDecision",
    "planner": "ResearchPlan",
    "researcher": "_Extraction",
    "synthesizer": "_Draft",
    "fact_checker": "_VerdictBatch",
    "reflection": "_Rubric",
}
"""Which schema each caller asks for. That is the only thing in a request that says who is
calling, and it is a fact about the code rather than about the prompt wording: the schema is
the caller's contract, so a renamed schema should fail here loudly rather than silently
route a Researcher answer to the Synthesizer."""

_ROLE_BY_SCHEMA = {schema: role for role, schema in ROLE_SCHEMAS.items()}

_SCHEMA_MARKER = "JSON Schema:\n"
"""Where llm_client._system_prompt puts the JSON Schema. Read rather than guessed, so a
change to that prompt fails these tests instead of quietly mis-identifying every caller."""


@dataclass(frozen=True)
class LLMRequest:
    """One request, as the fake saw it. What a test inspects to answer "what was it asked?"."""

    role: str
    model: str  # the model id, which is how a test checks the tier the caller used
    system: str
    user: str
    schema: dict[str, Any]
    is_retry: bool
    """True on the second half of the client's validation retry - the request that carries
    the assistant's rejected answer and the validation error."""


Answer = str | Exception | Callable[[LLMRequest], str]
"""What a queue entry may be: a response body, an exception the transport raises, or a
function that writes the body from the request. The three cover scripted output, the failure
paths llm_client owns, and the answers that have to cite ids minted during the run."""


class FakeLLM:
    """A deterministic stand-in for the OpenAI client, answering per requesting agent.

    Built with one queue per agent - `FakeLLM(planner=[...], supervisor=[...])` - and every
    request is recorded. A queue that runs out fails the test naming the agent, so an extra
    call is an immediate failure rather than a silent pass.

    It deliberately implements none of `LLMClient`'s behaviour: no validation, no retry, no
    backoff, no budget. Those run for real above it, which is the point.
    """

    def __init__(self, **answers: list[Answer]) -> None:
        unknown = sorted(set(answers) - set(ROLE_SCHEMAS))
        if unknown:
            raise AssertionError(
                f"no agent answers to {unknown}; the agents are {sorted(ROLE_SCHEMAS)}"
            )
        self._queues: dict[str, list[Answer]] = {role: list(q) for role, q in answers.items()}
        self._lock = Lock()
        self.requests: list[LLMRequest] = []
        self.chat = _Chat(completions=_Completions(self))

    def send(self, **kwargs: Any) -> Any:
        """Answer one request from the queue belonging to whoever asked.

        Guarded, because the Researcher's extraction calls now arrive concurrently
        (ADR 0002): recording the request, checking the queue, and popping from it have to
        be one step or two threads take the same answer. The answer itself is built outside
        the lock so a callable that reads its own request cannot serialise the pool.

        A queue is still consumed in order. What is no longer guaranteed is *which* of two
        overlapping Researcher calls takes the next entry, so a script that needs to say
        "this page fails and that one does not" writes an answer that reads the page.
        """
        request = _read_request(kwargs)
        with self._lock:
            self.requests.append(request)
            queue = self._queues.get(request.role, [])
            if not queue:
                raise AssertionError(
                    f"the {request.role} made {len(self.requests_for(request.role))} calls; "
                    "the test scripted fewer"
                )
            answer = queue.pop(0)

        if isinstance(answer, Exception):
            raise answer
        return completion(answer(request) if callable(answer) else answer)

    @property
    def roles(self) -> list[str]:
        """Who called, in order. The routing sequence of a whole job, in one list."""
        return [request.role for request in self.requests]

    def requests_for(self, role: str) -> list[LLMRequest]:
        return [request for request in self.requests if request.role == role]

    def models_for(self, role: str) -> set[str]:
        """The model ids one agent's calls went to - `fast` and `main` are checked with it."""
        return {request.model for request in self.requests_for(role)}

    def unused(self) -> dict[str, int]:
        """Scripted answers nobody asked for. Empty means the script was consumed exactly."""
        return {role: len(queue) for role, queue in self._queues.items() if queue}


@dataclass
class _Completions:
    fake: FakeLLM

    def create(self, **kwargs: Any) -> Any:
        return self.fake.send(**kwargs)


@dataclass
class _Chat:
    completions: _Completions


def _read_request(kwargs: dict[str, Any]) -> LLMRequest:
    """Turn one `chat.completions.create` call into the request a test can read."""
    messages = list(kwargs["messages"])
    system = str(messages[0]["content"])
    schema = _schema_in(system)
    title = str(schema.get("title", ""))

    role = _ROLE_BY_SCHEMA.get(title)
    if role is None:
        raise AssertionError(f"no known agent asks for {title!r}; ROLE_SCHEMAS is out of date")

    return LLMRequest(
        role=role,
        model=str(kwargs["model"]),
        system=system,
        user=str(messages[1]["content"]),
        schema=schema,
        is_retry=len(messages) > 2,
    )


def _schema_in(system: str) -> dict[str, Any]:
    """The JSON Schema llm_client appended to the agent's own system prompt."""
    marker = system.rfind(_SCHEMA_MARKER)
    if marker == -1:
        raise AssertionError("the system prompt carries no JSON Schema; llm_client changed shape")
    loaded: Any = json.loads(system[marker + len(_SCHEMA_MARKER) :])
    if not isinstance(loaded, dict):
        raise AssertionError(f"the system prompt's schema is not an object: {loaded!r}")
    return loaded


# --- Reading a prompt --------------------------------------------------------------


def outside_untrusted_blocks(text: str) -> str:
    """Everything in `text` that is not inside an untrusted block.

    The ids an agent may cite live out here; everything a third party wrote lives inside. A
    page that prints `finding_id: forged` in its own body is therefore invisible to the
    readers below - which is the same rule the agents themselves work to, and is why an
    injected id cannot enter a report through this harness either.
    """
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        if line == BEGIN_MARKER:
            inside = True
        elif line == END_MARKER:
            inside = False
        elif not inside:
            kept.append(line)
    return "\n".join(kept)


def inside_untrusted_block(text: str, number: int = 1) -> str:
    """The body of the `number`-th untrusted block in `text`, blocks counted from 1."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line == BEGIN_MARKER:
            current = []
        elif line == END_MARKER and current is not None:
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(line)

    if len(blocks) < number:
        raise AssertionError(f"the prompt holds {len(blocks)} untrusted blocks, not {number}")
    return "\n".join(blocks[number - 1])


def finding_ids_in(user: str) -> list[str]:
    """The finding ids the Synthesizer was shown, in prompt order."""
    return [
        line.removeprefix("finding_id: ").strip()
        for line in outside_untrusted_blocks(user).splitlines()
        if line.startswith("finding_id: ")
    ]


def claim_ids_in(user: str) -> list[str]:
    """The claim ids the Fact-Checker was asked about, in prompt order."""
    return [
        line.removeprefix("- claim_id: ").split("|")[0].strip()
        for line in outside_untrusted_blocks(user).splitlines()
        if line.startswith("- claim_id: ")
    ]


# --- Writing an answer -------------------------------------------------------------


def decision(target: str, reason: str = "the transition table allows this one") -> str:
    """A SupervisorDecision. The Supervisor compares it against its own table, so a target
    the state does not allow is how a test exercises the invalid-route guard."""
    return json.dumps({"next": target, "reason": reason})


def plan(*questions: str) -> str:
    """A ResearchPlan with one subtopic per question, ids s1, s2, ... - the ids the Planner's
    prompt asks for, and the ids the rest of a job then routes on."""
    return json.dumps(
        {
            "subtopics": [
                {"id": f"s{n}", "question": q, "search_query": q[:120]}
                for n, q in enumerate(questions, 1)
            ],
            "success_criteria": ["Every claim cites a public source"],
        }
    )


def extraction(*findings: tuple[str, str]) -> str:
    """An extraction answer, as (claim, evidence) pairs. Evidence that is not in the page is
    dropped by the Researcher, so a test that wants a Finding must quote the page."""
    return json.dumps({"findings": [{"claim": c, "evidence": e} for c, e in findings]})


def quote_the_page(claim: str = "The source reports a cloud revenue figure.") -> Answer:
    """An extraction answer that quotes the first line of whatever page it was shown.

    This is the ordinary Researcher answer for a graph test. Reading the page out of the
    prompt rather than hard-coding it means a test does not have to know which source the
    Researcher reached first, and it keeps the evidence verbatim, which is what the
    Researcher's own substring check requires.
    """

    def answer(request: LLMRequest) -> str:
        page = inside_untrusted_block(request.user)
        return extraction((claim, page.splitlines()[0]))

    return answer


def draft(revision: int, *, text: str | None = None) -> Answer:
    """A Synthesizer answer: one claim per finding it was shown, each citing that finding.

    `revision` goes into the claim ids because a redrafted report has to carry new ones -
    "some claim has no verdict" is what sends a fresh draft back to the Fact-Checker, so a
    stub that reused ids would describe a graph that cannot revise.
    """

    def answer(request: LLMRequest) -> str:
        finding_ids = finding_ids_in(request.user)
        if not finding_ids:
            raise AssertionError("the synthesizer was shown no finding ids to cite")
        return json.dumps(
            {
                "sections": [
                    {"id": "sec1", "heading": "Cloud revenue", "body": "What the sources say."}
                ],
                "claims": [
                    {
                        "claim_id": f"c{revision}-{n}",
                        "section_id": "sec1",
                        "text": text or f"Source {n} reports a cloud revenue figure.",
                        "finding_ids": [finding_id],
                    }
                    for n, finding_id in enumerate(finding_ids, 1)
                ],
            }
        )

    return answer


def verdict_batch(*, quote: str | None, supported: bool = True, note: str = "stated") -> Answer:
    """A Fact-Checker answer covering every claim it was asked about.

    `quote` is passed rather than defaulted because a supported verdict is supposed to carry
    a passage from the source; a test that wants the forged case passes `quote=None`, which
    `Verdict` refuses.
    """

    def answer(request: LLMRequest) -> str:
        return json.dumps(
            {
                "verdicts": [
                    {"claim_id": claim_id, "supported": supported, "quote": quote, "note": note}
                    for claim_id in claim_ids_in(request.user)
                ]
            }
        )

    return answer


RUBRIC_DIMENSIONS = (
    "research_completeness",
    "source_correctness",
    "citation_coverage",
    "factual_consistency",
    "report_quality",
)
"""The five rubric dimensions, written out rather than imported from schemas: a test double
that read the value under test would agree with it however that value changed."""


def rubric(**scores: int) -> str:
    """A reflection answer. Every dimension is 5 unless the test says otherwise."""
    body: dict[str, Any] = dict.fromkeys(RUBRIC_DIMENSIONS, 5)
    body["rationale"] = "Complete, well sourced, and readable."
    body.update(scores)
    return json.dumps(body)


# --- The recorded web --------------------------------------------------------------

_PUBLIC_IP = "93.184.216.34"
"""What every recorded host resolves to. Public, so tools/validation.py's SSRF check lets
the fetch happen - the private-address refusals have their own tests in test_tools_fetch.py."""

_REAL_HTTPX_CLIENT = httpx.Client
"""The real class, kept before `install()` replaces the name it is normally reached by.
Without this the factory below would build itself, because the patch lands on the one module
attribute `tools.fetch` and this file both look the class up through."""


@dataclass(frozen=True)
class Page:
    """One recorded page: what it says, and the URL it says it at."""

    url: str
    title: str
    text: str
    """The prose, one paragraph per line. A Finding's evidence has to be a verbatim span of
    this, once tools/fetch.py has cleaned the markup back off."""

    def html(self) -> str:
        """The page as it arrives over the wire.

        The body is HTML-escaped, which matters for the injection tests: a page carrying a
        copy of the untrusted-block delimiter has to arrive as *text* saying `<<<END ...>>>`
        rather than as markup, and escaping is exactly how a real page would carry it.
        """
        body = "".join(f"<p>{escape(line)}</p>" for line in self.text.splitlines() if line.strip())
        return f"<html><head><title>{escape(self.title)}</title></head><body>{body}</body></html>"


@dataclass
class RecordedWeb:
    """A search index and an origin server, wired under the real tool functions.

    Records every query and every fetched URL, because most of what the graph tests assert
    about the tool boundary is a statement about those two lists: the queries came from the
    plan, and the URLs came from a search result or from a Finding already in state.
    """

    pages: dict[str, Page] = field(default_factory=dict)
    results: dict[str, list[str]] = field(default_factory=dict)
    queries: list[str] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)

    def publish(self, *pages: Page) -> None:
        """Make pages reachable without putting them in any search result.

        Used for the URL an injected page invites the system to fetch: it resolves, it
        answers, and the test asserting it was never requested therefore means something.
        """
        for page in pages:
            self.pages[page.url] = page

    def index(self, query: str, *pages: Page) -> None:
        """Make `query` return these pages, in this order, and make them reachable."""
        self.publish(*pages)
        self.results[query] = [page.url for page in pages]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace DNS, Tavily's client, and httpx's client - and nothing else."""
        patch_dns(monkeypatch, dict.fromkeys(self._hosts(), _PUBLIC_IP))
        monkeypatch.setattr(tools.search, "TavilyClient", self._tavily)
        # tools/fetch.py builds its own client, so the module attribute it calls through is
        # the only seam. It is the same httpx module object this file imported.
        monkeypatch.setattr(httpx, "Client", self._client)

    def answer_search(self, query: str) -> dict[str, Any]:
        """Tavily's response shape for one query. tools/search.py normalises it for real."""
        self.queries.append(query)
        urls = self.results.get(query)
        if urls is None:
            raise AssertionError(f"the graph searched for something nobody recorded: {query!r}")
        return {
            "results": [
                {"title": self.pages[url].title, "url": url, "raw_content": self.pages[url].text}
                for url in urls
            ]
        }

    def _hosts(self) -> list[str]:
        return [httpx.URL(url).host for url in self.pages]

    def _tavily(self, **_kwargs: Any) -> _TavilyStub:
        return _TavilyStub(self)

    def _client(self, **kwargs: Any) -> httpx.Client:
        """The real httpx.Client with a scripted transport, so redirect handling, the
        streaming byte cap, and the headers fetch.py sets all still run."""
        return _REAL_HTTPX_CLIENT(**kwargs, transport=httpx.MockTransport(self._respond))

    def _respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            # 404 is "no rules", which is how fetch.py's fail-open path reads it.
            return httpx.Response(404)

        url = str(request.url)
        self.fetched.append(url)
        page = self.pages.get(url)
        if page is None:
            raise AssertionError(f"the graph fetched a page nobody recorded: {url}")
        return httpx.Response(
            200, text=page.html(), headers={"content-type": "text/html; charset=utf-8"}
        )


@dataclass
class _TavilyStub:
    """Shaped like the one method tools/search.py calls."""

    web: RecordedWeb

    def search(self, query: str, **_kwargs: Any) -> dict[str, Any]:
        return self.web.answer_search(query)
