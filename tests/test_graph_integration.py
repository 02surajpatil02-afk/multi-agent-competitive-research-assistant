"""
WHY THIS FILE EXISTS
    Every test here runs a **whole job** through the real graph: the real five agents, the
    real reflection node, the real LangGraph wiring, the real LLM client, and the real tool
    boundary. Only two things are replaced, and both are the outside world - the model
    answers from a script, and the web is recorded (tests/harness.py).

    That is a different question from the one tests/test_graph_build.py asks. Step 10's tests
    stub the four heavy agents, because what was under test was where an agent's answer goes.
    Here nothing is stubbed, so what is under test is whether the agents' contracts actually
    compose: whether a finding id minted in the Researcher survives into a Claim, a
    Verdict, the export gate's arithmetic, and a checkpoint - and whether the documented
    routes are the ones a real run takes.

    Four groups are worth reading in order.

    **The normal path**, from a question to an approved export, with the provenance
    assertions attached to it: a Finding carries the URL of the page it was actually fetched
    from, the Fact-Checker re-reads only URLs already in findings, and every exported claim
    reaches a source.

    **The three targeted retries and the cap.** Each of reflection's routes is driven end to
    end, because each one is a different claim about state: a Researcher route must leave the
    other subtopics alone and re-enter through synthesis, a Synthesizer route must not
    re-research, a Fact-Checker route must not invent findings, and the cap must stop the loop
    visibly rather than silently.

    **The guards**, each tripped against a lowered limit rather than a long run, so the test
    says which limit it is testing.

    **The accounting.** `llm_calls_used` is compared against the requests the fake actually
    received, so a validation retry, a transport retry, and a 429 schedule are all counted
    where guidelines §13 says they are counted - and the fact that exactly one extra request
    appears is what says no agent added a retry loop of its own.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

import json
from typing import cast

import pytest
from fakes import FakeCache, rate_limit_error, server_error
from harness import (
    Answer,
    FakeLLM,
    LLMRequest,
    Page,
    RecordedWeb,
    decision,
    draft,
    extraction,
    plan,
    quote_the_page,
    rubric,
    verdict_batch,
)
from langgraph.types import Command
from openai import OpenAI

import llm_client
from config import load_config
from graph.build import ResearchGraph, build_graph, run_config
from graph.state import ResearchState, new_state
from llm_client import LLMClient
from tools.contracts import ToolCache

_ENV = {
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "main-model",
    "LLM_FAST_MODEL": "fast-model",
    "LLM_API_KEY": "key",
    "TAVILY_API_KEY": "key",
}

_QUESTION = "Compare TCS and Infosys on cloud strategy."
_SUBTOPICS = (
    "What is TCS cloud revenue?",
    "What is Infosys cloud revenue?",
    "How do their cloud partnerships compare?",
)

_ROUTE_TO_THE_GATE = [
    decision("planner"),
    *[decision("researcher")] * 3,
    decision("synthesizer"),
    decision("fact_checker"),
]
"""The six routing decisions a job with three subtopics needs to reach reflection. The
Supervisor checks each against its own transition table, so this list is the expected path
rather than a way of choosing one: a graph that routed differently would be told so."""

_CALLS_TO_THE_GATE = [
    "supervisor",
    "planner",
    *["supervisor", "researcher", "researcher"] * 3,
    "supervisor",
    "synthesizer",
    "supervisor",
    "fact_checker",
    "reflection",
]
"""Every LLM call one clean job makes, in order - two Researcher calls per subtopic, one
source each. ARCHITECTURE.md §3's normal execution path, counted."""


# --- The recorded corpus and the script ---------------------------------------------


def _sentence(tag: str) -> str:
    """The one quotable sentence on a page. Findings quote it, so it is also the evidence."""
    return f"Source {tag} reported cloud revenue of $1.2bn in FY24."


def _page(tag: str) -> Page:
    return Page(
        url=f"https://source-{tag}.example/report",
        title=f"Source {tag}",
        text=f"{_sentence(tag)}\nThe rest of the page is boilerplate.",
    )


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> RecordedWeb:
    """Three subtopics, two recorded sources each, served under the real tools."""
    recorded = RecordedWeb()
    for index, question in enumerate(_SUBTOPICS, 1):
        recorded.index(question, _page(f"{index}a"), _page(f"{index}b"))
    recorded.install(monkeypatch)
    return recorded


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every delay the LLM client waited, so a backoff schedule is assertable."""
    recorded: list[float] = []
    monkeypatch.setattr(llm_client, "sleep", recorded.append)
    return recorded


def _pages() -> dict[str, Page]:
    return {page.url: page for page in (_page(f"{i}{a}") for i in (1, 2, 3) for a in "ab")}


def _fake(**overrides: list[Answer]) -> FakeLLM:
    """The script for one clean job. A test overrides only the agent it is about."""
    script: dict[str, list[Answer]] = {
        "supervisor": list(_ROUTE_TO_THE_GATE),
        "planner": [plan(*_SUBTOPICS)],
        "researcher": [quote_the_page()] * 6,
        "synthesizer": [draft(1)],
        "fact_checker": [verdict_batch(quote=_sentence("1a"))],
        "reflection": [rubric()],
    }
    return FakeLLM(**{**script, **overrides})


def _graph(fake: FakeLLM, *, cache: ToolCache | None = None, **env: str) -> ResearchGraph:
    config = load_config({**_ENV, **env})
    return build_graph(config=config, llm=LLMClient(config, client=cast(OpenAI, fake)), cache=cache)


def _start() -> ResearchState:
    return new_state(job_id="job-1", user_id="user-1", question=_QUESTION)


def _approve(compiled: ResearchGraph) -> ResearchState:
    """Run a job to the gate and approve it, returning the finished state."""
    settings = run_config("job-1")
    compiled.invoke(_start(), settings)
    return cast(ResearchState, compiled.invoke(Command(resume={"decision": "approve"}), settings))


# --- The normal path ----------------------------------------------------------------


def test_the_normal_path_calls_the_documented_agents_in_the_documented_order(
    web: RecordedWeb,
) -> None:
    fake = _fake()

    compiled = _graph(fake)
    compiled.invoke(_start(), run_config("job-1"))

    assert fake.roles == _CALLS_TO_THE_GATE
    assert web.queries == list(_SUBTOPICS)  # one search per subtopic, from the plan


def test_an_approved_job_ends_with_findings_a_report_verdicts_and_a_score(
    web: RecordedWeb,
) -> None:
    fake = _fake()

    final = _approve(_graph(fake))

    assert final["status"] == "approved"
    assert final["failure_reason"] is None
    assert len(final["findings"]) == 6
    assert final["report"] is not None
    assert len(final["verdicts"]) == 6
    assert len(final["reflection_scores"]) == 1
    assert final["quality_flag"] is None  # the rubric ran and the report passed
    assert fake.unused() == {}  # every scripted answer was used, and no answer was missing


def test_the_job_pauses_at_the_gate_before_anything_is_exported(web: RecordedWeb) -> None:
    # CLAUDE.md invariant 6. The reviewer is the only way into export, so a finished
    # reflection pass leaves the job running rather than approved.
    compiled = _graph(_fake())
    settings = run_config("job-1")

    paused = compiled.invoke(_start(), settings)

    assert compiled.get_state(settings).next == ("human_gate",)
    assert paused["status"] == "running"
    # The reviewer's payload, built from a real job: six claims, each with the URL it rests
    # on and the Fact-Checker's quote (ARCHITECTURE.md §12).
    payload = paused["__interrupt__"][0].value
    assert payload["job_id"] == "job-1"
    assert payload["unsupported_claims"] == []
    assert len(payload["claims"]) == len(paused["report"].claims)
    assert all(claim["sources"] for claim in payload["claims"])


def test_every_finding_carries_the_page_it_was_actually_fetched_from(web: RecordedWeb) -> None:
    # Provenance is attached in Python from the tool result, never by the model. The pairing
    # is what that means in practice: this quote and this URL came from the same fetch.
    pages = _pages()

    final = _approve(_graph(_fake()))

    for finding in final["findings"]:
        page = pages[str(finding.url)]
        assert finding.evidence == _sentence(page.title.removeprefix("Source "))
        assert finding.title == page.title
        assert finding.truncated is False
        assert len(finding.content_hash) == 64
    assert len({finding.finding_id for finding in final["findings"]}) == 6


def test_the_researcher_fetches_only_urls_a_search_returned(web: RecordedWeb) -> None:
    # guidelines §8, mechanism 2: a URL to fetch comes from a search result, never from
    # anywhere else. Every recorded page is reachable, so a fetch outside this set would
    # have succeeded - it simply never happens.
    final = _approve(_graph(_fake()))

    offered = {url for urls in web.results.values() for url in urls}
    assert set(web.fetched) == offered
    assert {str(finding.url) for finding in final["findings"]} == offered


def test_the_fact_checker_re_fetches_only_urls_already_in_findings(web: RecordedWeb) -> None:
    # guidelines §2.5: it re-fetches and never searches. Each page is therefore fetched
    # twice in one job - once by the Researcher, once at verification time.
    final = _approve(_graph(_fake()))

    cited = [str(finding.url) for finding in final["findings"]]
    assert sorted(web.fetched) == sorted(cited * 2)
    assert web.queries == list(_SUBTOPICS)  # no search was made at verification time


def test_every_claim_in_the_exported_report_reaches_a_source_url(web: RecordedWeb) -> None:
    # CLAUDE.md invariant 1, run against a report nobody hand-built.
    final = _approve(_graph(_fake()))

    report = final["report"]
    assert report is not None
    cited = {finding_id for source in report.sources for finding_id in source.finding_ids}
    known = {finding.finding_id for finding in final["findings"]}
    assert cited <= known  # sources are a view over findings, not an independent store
    for claim in report.claims:
        assert cited.intersection(claim.finding_ids)


def test_the_cache_is_consulted_by_both_tools_and_provenance_survives_it(
    web: RecordedWeb,
) -> None:
    # The cache is the argument that decides whether the two web-facing agents reach the
    # same page twice. With one wired in, the Fact-Checker's re-read is served from it.
    cache = FakeCache()

    final = _approve(_graph(_fake(), cache=cache))

    assert [key for key in cache.gets if key.startswith("cache:search:")]
    assert [key for key in cache.gets if key.startswith("cache:fetch:")]
    assert sorted(web.fetched) == sorted({str(f.url) for f in final["findings"]})
    assert len(final["findings"]) == 6


# --- The three targeted retries and the cap ------------------------------------------


def test_a_completeness_failure_re_researches_only_the_thin_subtopic(web: RecordedWeb) -> None:
    # The most important retry in the system. The third subtopic ends the first pass with
    # one source because its second page yields nothing, which is what "thin" means to
    # reflection - and the other two subtopics must be left exactly as they were.
    fake = _fake(
        supervisor=[*_ROUTE_TO_THE_GATE, decision("synthesizer"), decision("fact_checker")],
        researcher=[*[quote_the_page()] * 5, extraction(), quote_the_page()],
        synthesizer=[draft(1), draft(2)],
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * 2,
        reflection=[rubric(research_completeness=1, source_correctness=1), rubric()],
    )
    compiled = _graph(fake)
    settings = run_config("job-1")

    paused = compiled.invoke(_start(), settings)

    assert paused["revision_count"] == 1
    assert web.queries == [*_SUBTOPICS, _SUBTOPICS[2]]  # only the thin subtopic was re-run
    assert [f.subtopic_id for f in paused["findings"]] == ["s1", "s1", "s2", "s2", "s3", "s3"]
    assert paused["subtopic_status"] == {"s1": "done", "s2": "done", "s3": "done"}
    # The draft written after the retry, which only exists because the Researcher route
    # invalidated the first one - an un-invalidated draft would have routed to the
    # Fact-Checker and the new evidence would never have entered the report.
    assert len(paused["report"].claims) == 6
    # Both passes accumulated: five claims in the first draft, one per finding it had, and
    # six in the second, because the retry found the source the first pass missed. Claim ids
    # are minted per draft, so the eleven verdicts are eleven distinct ids and every claim in
    # the current report was verified in the second pass, not carried over from the first.
    assert len(paused["verdicts"]) == 11
    assert len({verdict.claim_id for verdict in paused["verdicts"]}) == 11
    current = {claim.claim_id for claim in paused["report"].claims}
    assert current <= {verdict.claim_id for verdict in paused["verdicts"]}
    assert compiled.get_state(settings).next == ("human_gate",)
    assert fake.unused() == {}


def test_a_retry_that_finds_nothing_does_not_start_another_identical_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR 0004, end to end. The third subtopic yields nothing on its first visit, and the
    # retry sent to the other two re-reaches only URLs their own findings already carry, so
    # they yield nothing either. All three are now `unresearched`, and the second reflection
    # pass has nowhere left to send the Researcher - the loop stops at the gate with a cycle
    # still in hand, rather than re-issuing the same query against the same cached results.
    recorded = RecordedWeb()
    for index, question in enumerate(_SUBTOPICS, 1):
        recorded.index(question, _page(f"{index}a"), _page(f"{index}b"))
    recorded.install(monkeypatch)
    research_only = rubric(
        research_completeness=1, source_correctness=1, factual_consistency=4, report_quality=4
    )
    fake = FakeLLM(
        supervisor=[
            *_ROUTE_TO_THE_GATE,
            decision("researcher"),
            decision("synthesizer"),
            decision("fact_checker"),
        ],
        planner=[plan(*_SUBTOPICS)],
        # Two usable sources each for the first two subtopics, nothing for the third. Neither
        # retry visit makes a call at all: every URL it would read is already in `seen`.
        researcher=[*[quote_the_page()] * 4, *[extraction()] * 2],
        synthesizer=[draft(1), draft(2)],
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * 2,
        reflection=[research_only] * 2,
    )
    compiled = _graph(fake)
    settings = run_config("job-1")

    paused = compiled.invoke(_start(), settings)

    # One search per subtopic, plus the two legitimate retries. There is no sixth.
    assert recorded.queries == [*_SUBTOPICS, _SUBTOPICS[0], _SUBTOPICS[1]]
    assert paused["subtopic_status"] == dict.fromkeys(("s1", "s2", "s3"), "unresearched")
    assert fake.roles.count("reflection") == 2
    # The cap did not stop this - MAX_REVISIONS is 2 and only one cycle was spent. The second
    # reflection pass declined to start one because no cycle could have changed anything.
    assert paused["revision_count"] == 1
    assert paused["quality_flag"] == "below_threshold"  # the gap is reported, not passed
    assert paused["failed_dimensions"] == ["research_completeness", "source_correctness"]
    assert len(paused["findings"]) == 4  # nothing was lost on the way to the gate
    assert compiled.get_state(settings).next == ("human_gate",)
    assert fake.unused() == {}


def test_a_citation_failure_redrafts_without_researching_again(web: RecordedWeb) -> None:
    fake = _fake(
        supervisor=[*_ROUTE_TO_THE_GATE, decision("fact_checker")],
        synthesizer=[draft(1), draft(2)],
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * 2,
        reflection=[rubric(citation_coverage=4), rubric()],
    )
    compiled = _graph(fake)
    settings = run_config("job-1")

    paused = compiled.invoke(_start(), settings)

    assert paused["revision_count"] == 1
    assert web.queries == list(_SUBTOPICS)  # the findings were kept, not gathered again
    assert len(paused["findings"]) == 6
    assert fake.roles.count("researcher") == 6
    # A fresh draft with fresh claim ids, re-verified: the previous report is not reused.
    # Six claims per draft over two drafts giving twelve distinct verdict ids is the property
    # the Supervisor routes on - it is how "this draft still needs checking" stays true.
    assert len(paused["report"].claims) == 6
    assert len({verdict.claim_id for verdict in paused["verdicts"]}) == 12
    assert compiled.get_state(settings).next == ("human_gate",)


def test_a_consistency_failure_re_verifies_without_inventing_findings(web: RecordedWeb) -> None:
    fake = _fake(
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * 2,
        reflection=[
            rubric(research_completeness=3, factual_consistency=1, report_quality=1),
            rubric(),
        ],
    )
    compiled = _graph(fake)
    settings = run_config("job-1")

    paused = compiled.invoke(_start(), settings)

    # The documented route: reflection sends the draft back to the Fact-Checker, whose fixed
    # edge returns it to reflection. The Supervisor is not consulted, so no extra decision.
    assert fake.roles[-4:] == ["fact_checker", "reflection", "fact_checker", "reflection"]
    assert len(paused["findings"]) == 6
    assert web.queries == list(_SUBTOPICS)
    # The contrast with a redraft: one draft verified twice, so the twelve verdicts carry
    # only six distinct ids. Nothing was re-synthesized, so nothing was re-minted.
    assert len(paused["report"].claims) == 6
    assert len(paused["verdicts"]) == 12
    assert len({verdict.claim_id for verdict in paused["verdicts"]}) == 6
    assert paused["revision_count"] == 1
    assert fake.unused() == {}


def test_the_revision_cap_ends_the_loop_at_the_gate_with_the_flag_set(web: RecordedWeb) -> None:
    # CLAUDE.md invariant 2: hitting the cap is a visible outcome carried in the response,
    # never a silent pass. Two cycles, three report-producing passes, then the gate.
    failing = rubric(citation_coverage=4)
    fake = _fake(
        supervisor=[*_ROUTE_TO_THE_GATE, decision("fact_checker"), decision("fact_checker")],
        synthesizer=[draft(1), draft(2), draft(3)],
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * 3,
        reflection=[failing] * 3,
    )
    compiled = _graph(fake)
    settings = run_config("job-1")

    paused = compiled.invoke(_start(), settings)

    assert paused["revision_count"] == 2
    assert paused["quality_flag"] == "below_threshold"
    assert paused["failed_dimensions"] == ["citation_coverage"]
    assert len(paused["reflection_scores"]) == 3
    assert fake.roles.count("reflection") == 3  # no fourth cycle was started
    assert compiled.get_state(settings).next == ("human_gate",)
    assert fake.unused() == {}


# --- The guards ----------------------------------------------------------------------


def test_the_hop_guard_stops_a_job_at_its_limit(web: RecordedWeb) -> None:
    fake = _fake(supervisor=[decision("planner"), decision("researcher")])

    final = _graph(fake, MAX_SUPERVISOR_HOPS="2").invoke(_start(), run_config("job-1"))

    assert final["status"] == "failed"
    assert final["failure_reason"] == "hop_limit_exceeded"
    assert final["hop_count"] == 3  # the hop that tripped the guard is still counted


def test_the_supervisor_refuses_to_spend_a_call_it_has_no_budget_for(web: RecordedWeb) -> None:
    # The guard runs before the call, so the third request is never sent.
    fake = _fake()

    final = _graph(fake, MAX_LLM_CALLS_PER_JOB="2").invoke(_start(), run_config("job-1"))

    assert len(fake.requests) == 2  # the Supervisor's own hop, then the Planner
    assert final["status"] == "failed"
    assert final["failure_reason"] == "budget_exceeded"


def test_the_call_budget_stops_an_agent_part_way_through_its_own_work(web: RecordedWeb) -> None:
    # The backstop inside the client, for the calls a node makes between two hops: the
    # Researcher extracts from its first source and is refused the second.
    fake = _fake()

    final = _graph(fake, MAX_LLM_CALLS_PER_JOB="4").invoke(_start(), run_config("job-1"))

    assert len(fake.requests) == 4
    assert final["status"] == "failed"
    assert final["failure_reason"] == "budget_exceeded"
    assert len(final["findings"]) == 1  # what the first source produced is kept


def test_a_route_the_transition_table_forbids_does_not_stop_a_whole_job(
    web: RecordedWeb,
) -> None:
    # ADR 0001, end to end. The routing model is wrong-but-valid on the very first hop - a
    # job with no plan can only go to the Planner - and the job still runs to approval. This
    # is the shape of the two real smoke jobs that step 12 lost to `invalid_route`.
    fake = _fake(supervisor=[decision("synthesizer"), *_ROUTE_TO_THE_GATE[1:]])

    final = _approve(_graph(fake))

    assert final["status"] == "approved"
    assert final["failure_reason"] is None
    assert fake.roles == _CALLS_TO_THE_GATE  # the wrong proposal changed no routing at all


def test_a_state_no_transition_covers_stops_the_job_without_a_call(web: RecordedWeb) -> None:
    # Every claim already has a verdict, which the fixed fact_checker -> reflection edge
    # means the Supervisor can never legitimately see. Reached here by handing a finished
    # job back at START, which is the shape a wiring bug would produce.
    fake = _fake()
    compiled = _graph(fake)
    finished = _approve(compiled)
    spent = len(fake.requests)

    final = compiled.invoke(finished, run_config("job-2"))

    assert len(fake.requests) == spent  # the guard runs before the call
    assert final["status"] == "failed"
    assert final["failure_reason"] == "no_valid_transition"


# --- LLM call accounting --------------------------------------------------------------


def test_llm_calls_used_counts_every_request_the_client_sent(web: RecordedWeb) -> None:
    fake = _fake()

    final = _approve(_graph(fake))

    assert final["llm_calls_used"] == len(fake.requests) == len(_CALLS_TO_THE_GATE)
    assert final["hop_count"] == len(_ROUTE_TO_THE_GATE)


def test_a_validation_retry_is_a_second_request_and_is_counted(web: RecordedWeb) -> None:
    # guidelines §3: one bounded validation retry, and it costs a call, because every
    # request spends a token of the same rate limit.
    invalid = json.dumps({"subtopics": [], "success_criteria": []})
    fake = _fake(planner=[invalid, plan(*_SUBTOPICS)])

    final = _approve(_graph(fake))

    assert [request.is_retry for request in fake.requests_for("planner")] == [False, True]
    assert final["llm_calls_used"] == len(fake.requests) == len(_CALLS_TO_THE_GATE) + 1
    assert final["status"] == "approved"


def test_a_transport_failure_is_retried_once_by_the_client_and_counted(
    web: RecordedWeb, slept: list[float]
) -> None:
    # The retry, the delay, and the counting all belong to the client. Exactly one extra
    # request and exactly one documented delay is what says no agent added a loop of its own.
    fake = _fake(researcher=[server_error(), *[quote_the_page()] * 6])

    final = _approve(_graph(fake))

    assert len(fake.requests_for("researcher")) == 7
    assert slept == [2.0]  # guidelines §17, main tier's first transport backoff
    assert final["llm_calls_used"] == len(_CALLS_TO_THE_GATE) + 1
    assert len(final["findings"]) == 6  # the retried source was not lost


def test_a_rate_limited_job_fails_after_the_documented_schedule(
    web: RecordedWeb, slept: list[float]
) -> None:
    # guidelines §13: a rate-limited job fails visibly rather than producing a shorter report.
    fake = _fake(supervisor=[rate_limit_error() for _ in range(4)])

    final = _graph(fake).invoke(_start(), run_config("job-1"))

    assert slept == [2.0, 8.0, 30.0]
    assert len(fake.requests) == 4  # the first call plus three retries, all counted
    assert final["llm_calls_used"] == 4
    assert final["failure_reason"] == "rate_limited"


def test_routing_and_scoring_use_the_fast_model_and_the_agents_use_the_main_one(
    web: RecordedWeb,
) -> None:
    # The two-tier split from CLAUDE.md's model table: the Supervisor and the reflection
    # node are the only callers cheap enough to run on the fast model.
    fake = _fake()

    _approve(_graph(fake))

    assert fake.models_for("supervisor") == {"fast-model"}
    assert fake.models_for("reflection") == {"fast-model"}
    for role in ("planner", "researcher", "synthesizer", "fact_checker"):
        assert fake.models_for(role) == {"main-model"}


# --- The checkpoint -------------------------------------------------------------------


def test_resuming_at_the_gate_does_not_repeat_completed_work(web: RecordedWeb) -> None:
    # guidelines §18 and §10: approval after two days must cost nothing beyond the export.
    # This is the same claim step 10 makes with stubs, made here with the real agents, so a
    # repeated node would show up as a real search, a real fetch, and a real call.
    fake = _fake()
    compiled = _graph(fake)
    settings = run_config("job-1")
    compiled.invoke(_start(), settings)
    spent, fetched, searched = len(fake.requests), list(web.fetched), list(web.queries)

    final = compiled.invoke(Command(resume={"decision": "approve"}), settings)

    assert len(fake.requests) == spent
    assert web.fetched == fetched
    assert web.queries == searched
    assert final["status"] == "approved"


def test_a_resumed_job_keeps_the_findings_and_the_report_it_paused_with(
    web: RecordedWeb,
) -> None:
    # The models in state are rebuilt from the checkpoint rather than handed back as dicts,
    # which is what makes the export gate's arithmetic work after a restart.
    compiled = _graph(_fake())
    settings = run_config("job-1")
    paused = compiled.invoke(_start(), settings)
    before = [finding.finding_id for finding in paused["findings"]]

    final = compiled.invoke(Command(resume={"decision": "approve"}), settings)

    assert [finding.finding_id for finding in final["findings"]] == before
    assert final["report"] == paused["report"]
    assert str(final["findings"][0].url) in web.fetched


# --- The reviewer-edit path (ADR 0006) -----------------------------------------------


_EDIT = "Add the missing information about Product B."
"""One reviewer instruction, reused so the assertions can look for it by identity."""


def _edit_script(*, reflection: list[Answer], drafts: int = 2, checks: int = 2) -> FakeLLM:
    """The clean job, plus what one reviewer edit needs after it.

    An edit costs a Synthesizer pass, the Supervisor hop that follows it, a Fact-Checker
    pass, and a reflection pass - the 3 logical calls guidelines §13 counts, plus the hop
    already inside the 24.
    """
    return _fake(
        supervisor=[*_ROUTE_TO_THE_GATE, *[decision("fact_checker")] * (drafts - 1)],
        synthesizer=[draft(revision) for revision in range(1, drafts + 1)],
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * checks,
        reflection=reflection,
    )


def _edit_once(compiled: ResearchGraph) -> ResearchState:
    """Run to the gate, send one edit, and stop wherever that leaves the job."""
    settings = run_config("job-1")
    compiled.invoke(_start(), settings)
    resumed = compiled.invoke(Command(resume={"decision": "edit", "edits": _EDIT}), settings)
    return cast(ResearchState, resumed)


def test_a_failing_edit_returns_to_the_gate_and_never_reaches_the_researcher(
    web: RecordedWeb,
) -> None:
    # The rule ADR 0006 exists for. The second rubric is the same failing score the
    # completeness-retry test above uses, so without the edit-path rule this job would route
    # to the Researcher - re-researching on a reviewer's wording, which the record forbids.
    fake = _edit_script(
        reflection=[rubric(), rubric(research_completeness=1, source_correctness=1)]
    )

    compiled = _graph(fake)
    paused = _edit_once(compiled)

    assert compiled.get_state(run_config("job-1")).next == ("human_gate",)  # back to the human
    # Search is the only way to reach a source this job has not read, and the Researcher is
    # the only caller that searches. Neither moved, and no finding was added - which is what
    # "the edit worked over existing evidence" means structurally. The pages the Fact-Checker
    # re-read are the ones the findings already name, so nothing new was reached.
    assert web.queries == list(_SUBTOPICS)
    assert fake.roles.count("researcher") == 6  # the initial pass only
    assert len(paused["findings"]) == 6
    assert set(web.fetched) == {str(finding.url) for finding in paused["findings"]}
    assert paused["revision_count"] == 0  # an edit is not a revision
    assert paused["quality_flag"] == "below_threshold"  # failing, and reported as such
    assert paused["report"] is not None  # the draft was not invalidated
    assert fake.unused() == {}


def test_an_edited_draft_is_verified_before_it_reaches_the_reviewer(web: RecordedWeb) -> None:
    # An edited claim is a new claim: it has no verdict, so the Supervisor's existing row
    # sends the draft through the Fact-Checker like any other (ARCHITECTURE.md §12).
    fake = _edit_script(reflection=[rubric(), rubric()])

    paused = _edit_once(_graph(fake))

    assert fake.roles[-4:] == ["synthesizer", "supervisor", "fact_checker", "reflection"]
    # Two drafts, both checked: an edited claim is a new claim and is verified like any other.
    assert len({verdict.claim_id for verdict in paused["verdicts"]}) == 12
    assert len(paused["reflection_scores"]) == 2


def test_the_reviewer_instruction_reaches_the_synthesizer_exactly_once(
    web: RecordedWeb,
) -> None:
    # The gate is the only writer of the field, so the instruction is visible for exactly one
    # Synthesizer pass - and the routing rule is what leaves no second pass to re-apply it.
    fake = _edit_script(reflection=[rubric(), rubric()])

    compiled = _graph(fake)
    paused = _edit_once(compiled)
    settings = run_config("job-1")
    final = compiled.invoke(Command(resume={"decision": "approve"}), settings)

    drafting = fake.requests_for("synthesizer")
    assert [_EDIT in request.user for request in drafting] == [False, True]
    assert paused["reviewer_edit_text"] == _EDIT  # still set while the edit is in flight
    assert final["reviewer_edit_text"] is None  # the approval cleared it
    assert final["status"] == "approved"


def test_a_rejection_clears_the_reviewer_instruction(web: RecordedWeb) -> None:
    fake = _edit_script(reflection=[rubric(), rubric()])
    compiled = _graph(fake)
    _edit_once(compiled)

    final = compiled.invoke(
        Command(resume={"decision": "reject", "note": "still thin"}), run_config("job-1")
    )

    assert final["status"] == "rejected"
    assert final["reviewer_edit_text"] is None


def test_an_edit_that_cites_a_finding_the_job_does_not_have_fails_the_job(
    web: RecordedWeb,
) -> None:
    # Grounding is not relaxed for a reviewer (ADR 0006). An instruction the evidence cannot
    # support must not become an invented citation: the existing guard fails the job, loudly,
    # rather than exporting a claim that reaches no source.
    fake = _edit_script(reflection=[rubric()], drafts=2, checks=1)
    fake._queues["synthesizer"] = [draft(1), _draft_citing_nothing()]

    final = _edit_once(_graph(fake))

    assert final["status"] == "failed"
    assert final["failure_reason"] == "report_cites_unknown_findings"
    assert final["report"] is not None  # the previous draft is still there, unexported


def test_three_edits_stay_inside_the_hop_guard(web: RecordedWeb) -> None:
    # ADR 0006's arithmetic, asserted rather than believed: an edit costs exactly +1 hop, so
    # MAX_REVIEWER_EDITS = 3 puts the ceiling at 20 + 3 = 23, under MAX_SUPERVISOR_HOPS = 24.
    fake = _edit_script(reflection=[rubric()] * 4, drafts=4, checks=4)
    compiled = _graph(fake)
    settings = run_config("job-1")
    compiled.invoke(_start(), settings)
    hops = [cast(ResearchState, compiled.get_state(settings).values)["hop_count"]]

    for _ in range(3):
        state = compiled.invoke(Command(resume={"decision": "edit", "edits": _EDIT}), settings)
        hops.append(cast(ResearchState, state)["hop_count"])

    final = compiled.invoke(Command(resume={"decision": "approve"}), settings)
    assert [after - before for before, after in zip(hops[:-1], hops[1:], strict=True)] == [1, 1, 1]
    assert hops[-1] < 24
    assert final["status"] == "approved"
    assert final["revision_count"] == 0  # three edits, no revisions


def test_the_call_budget_still_stops_an_edit_that_cannot_afford_itself(
    web: RecordedWeb,
) -> None:
    # `refuse_edit()` keeps an unaffordable edit from starting, and it runs in the endpoint
    # (step 18). This is the backstop underneath it, unchanged: an edit that starts and runs
    # out mid-pass still trips the Supervisor's guard rather than quietly producing less.
    fake = _edit_script(reflection=[rubric(), rubric()])

    final = _edit_once(_graph(fake, MAX_LLM_CALLS_PER_JOB="17"))

    assert final["status"] == "failed"
    assert final["failure_reason"] == "budget_exceeded"


def _draft_citing_nothing() -> Answer:
    """A draft whose only claim rests on a finding id this job never minted."""

    def answer(_request: LLMRequest) -> str:
        return json.dumps(
            {
                "sections": [{"id": "s1", "heading": "Product B", "body": "Added on request."}],
                "claims": [
                    {
                        "claim_id": "c1",
                        "section_id": "s1",
                        "text": "Product B launched in 2025.",
                        "finding_ids": ["f99"],
                    }
                ],
            }
        )

    return answer
