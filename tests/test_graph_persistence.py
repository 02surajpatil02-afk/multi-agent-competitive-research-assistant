"""
WHY THIS FILE EXISTS
    Steps 15 and 16: the audit trail is written *while* the job runs, not reconstructed
    afterwards, and the approved report is what `jobs.report_json` ends up holding.

    Every test here runs a whole job through the real graph with a real database under it -
    the real five agents, the real reflection node, the real tool boundary, the real
    statements in database/queries.py - and then reads the rows back. Only the two outside
    things are replaced: the model answers from a script and the web is recorded
    (tests/harness.py). The database is SQLite, and tests/dbharness.py says exactly what
    that does and does not prove.

    Four claims are worth the file.

    **The rows say what the job did.** The findings that reached state are the findings in
    the table, with the URLs they were fetched from; the claims are the current draft's, with
    the Fact-Checker's verdict on them; and `claim_sources` carries the claim-to-finding
    links the export gate exists to enforce. A trail assembled at the end could not say any
    of that after a crash, which is why it is not assembled at the end.

    **The export gate's outcome is durable, both ways.** A passing export stores the body and
    stamps `exported_at`; a blocked one stores no body and names the uncited claims. The gate
    itself is unchanged - it is arithmetic over the report in state - and the database only
    records what it decided.

    **A replayed node converges.** The same Researcher node run twice writes one set of
    findings and two audit rows, because a node that genuinely ran twice truthfully ran
    twice, and a finding that was written twice is one finding.

    **Without a database nothing changes.** The same job with `db=None` runs the same way and
    writes nothing, which is what keeps Phase 1's behaviour - and the measurement harness -
    exactly as it was.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from dbharness import a_finding, migrated_engine, new_job_id
from fakes import server_error
from harness import (
    Answer,
    FakeLLM,
    Page,
    RecordedWeb,
    decision,
    draft,
    plan,
    quote_the_page,
    rubric,
    verdict_batch,
)
from langgraph.types import Command
from openai import OpenAI
from pydantic import HttpUrl
from sqlalchemy.engine import Engine

import llm_client
from config import load_config
from database import queries
from graph.build import (
    NodeDeps,
    ResearchGraph,
    build_graph,
    export_node,
    researcher_node,
)
from graph.state import (
    ResearchState,
    new_state,
    run_config,
)
from llm_client import LLMClient
from schemas import Claim, Report, Section, Source

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

_SUBMITTER = "submitter-7"


# --- The job under test -------------------------------------------------------------


def _sentence(tag: str) -> str:
    return f"Source {tag} reported cloud revenue of $1.2bn in FY24."


def _page(tag: str) -> Page:
    return Page(
        url=f"https://source-{tag}.example/report",
        title=f"Source {tag}",
        text=f"{_sentence(tag)}\nThe rest of the page is boilerplate.",
    )


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> RecordedWeb:
    recorded = RecordedWeb()
    for index, question in enumerate(_SUBTOPICS, 1):
        recorded.index(question, _page(f"{index}a"), _page(f"{index}b"))
    recorded.install(monkeypatch)
    return recorded


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Backoff, recorded instead of waited. A retry schedule is not what these tests are
    about, and waiting for one makes them slow for no gain."""
    recorded: list[float] = []
    monkeypatch.setattr(llm_client, "sleep", recorded.append)
    return recorded


@pytest.fixture
def db(tmp_path: Path) -> Engine:
    return migrated_engine(tmp_path)


@pytest.fixture
def job(db: Engine) -> str:
    """The job row `POST /jobs` will insert, created here because the API is step 18.

    Everything the graph writes hangs off it, which is the point: a findings row for a job
    that does not exist is refused by the foreign key rather than orphaned.
    """
    job_id = new_job_id()
    queries.create_job(
        db,
        job_id=job_id,
        user_id=new_job_id(),
        question=_QUESTION,
        idempotency_key="key-1",
        actor=_SUBMITTER,
    )
    return job_id


def _fake(**overrides: list[Answer]) -> FakeLLM:
    """The script for one clean job, as tests/test_graph_integration.py drives it."""
    script: dict[str, list[Answer]] = {
        "supervisor": list(_ROUTE_TO_THE_GATE),
        "planner": [plan(*_SUBTOPICS)],
        "researcher": [quote_the_page()] * 6,
        "synthesizer": [draft(1)],
        "fact_checker": [verdict_batch(quote=_sentence("1a"))],
        "reflection": [rubric()],
    }
    return FakeLLM(**{**script, **overrides})


def _graph(fake: FakeLLM, db: Engine | None) -> ResearchGraph:
    config = load_config(_ENV)
    return build_graph(config=config, llm=LLMClient(config, client=cast(OpenAI, fake)), db=db)


def _decide(compiled: ResearchGraph, job_id: str, decided: str) -> ResearchState:
    """Run the job to the gate and resume it with a reviewer's decision."""
    settings = run_config(job_id)
    compiled.invoke(new_state(job_id=job_id, user_id="user-1", question=_QUESTION), settings)
    resumed = compiled.invoke(Command(resume={"decision": decided}), settings)
    return cast(ResearchState, resumed)


def _approved(fake: FakeLLM, db: Engine | None, job_id: str) -> ResearchState:
    return _decide(_graph(fake, db), job_id, "approve")


# --- What one job leaves behind -----------------------------------------------------


def test_a_whole_job_writes_its_audit_trail_in_order(
    web: RecordedWeb, db: Engine, job: str
) -> None:
    # ARCHITECTURE.md §9's actions, as one clean job produces them. `reviewer_decision` is
    # absent on purpose: it carries the reviewer's identity, so it is written by the
    # authenticated endpoint in step 18, not by the graph.
    _approved(_fake(), db, job)

    trail = [(event.actor, event.action) for event in queries.read_audit_events(db, job)]

    assert trail == [
        (_SUBMITTER, "job_created"),
        ("system", "plan_produced"),
        ("system", "subtopic_researched"),
        ("system", "subtopic_researched"),
        ("system", "subtopic_researched"),
        ("system", "gate_opened"),
        ("system", "export_attempted"),
        ("system", "export_result"),
    ]


def test_every_finding_is_written_with_the_page_it_was_read_from(
    web: RecordedWeb, db: Engine, job: str
) -> None:
    # The audit trail's whole purpose: a claim made in March has to be explicable in June,
    # which needs the URL, the verbatim quote, and the hash of the text that was read.
    final = _approved(_fake(), db, job)

    rows = queries.read_findings(db, job)
    written = {row.finding_id: row for row in rows}

    assert len(rows) == len(final["findings"]) == 6
    for finding in final["findings"]:
        row = written[finding.finding_id]
        assert row.url == str(finding.url)
        assert row.evidence == finding.evidence
        assert row.content_hash == finding.content_hash
        assert row.subtopic == finding.subtopic_id
        assert row.truncated is finding.truncated
    assert {row.url for row in rows} <= set(web.fetched)


def test_the_claims_and_their_sources_are_written_with_the_verdict(
    web: RecordedWeb, db: Engine, job: str
) -> None:
    final = _approved(_fake(), db, job)
    report = final["report"]
    assert report is not None

    written = {row.claim_id: row for row in queries.read_claims(db, job)}
    sources = {(row.claim_id, row.finding_id) for row in queries.read_claim_sources(db, job)}

    assert set(written) == {claim.claim_id for claim in report.claims}
    assert all(row.supported is True for row in written.values())
    assert all(row.verdict_note for row in written.values())
    # claim_sources is what turns "which URL supports this sentence?" into a query.
    assert sources == {
        (claim.claim_id, finding_id) for claim in report.claims for finding_id in claim.finding_ids
    }


def test_the_approved_report_is_what_jobs_report_json_holds(
    web: RecordedWeb, db: Engine, job: str
) -> None:
    # Step 16, and ARCHITECTURE.md §8's Phase 2 answer to "where does an export go before S3
    # exists": here, in the same node that will hold the PutObject in Phase 3.
    final = _approved(_fake(), db, job)
    report = final["report"]
    assert report is not None

    row = queries.read_job(db, job)

    assert row is not None
    assert row.report_json == report.model_dump(mode="json")
    assert row.exported_at is not None
    assert row.status == "approved"
    assert row.completed_at is not None
    assert (row.revision_count, row.llm_calls_used) == (
        final["revision_count"],
        final["llm_calls_used"],
    )
    assert row.quality_flag is None  # the rubric ran and the report passed


def test_the_jobs_row_call_count_is_stale_while_a_job_waits_at_the_gate(
    web: RecordedWeb, db: Engine, job: str
) -> None:
    """`jobs.llm_calls_used` is written by finalize only, so at the gate it still reads 0.

    Pinned because it is a trap rather than a detail: `POST /jobs/{id}/approve` has to refuse an
    edit that cannot fit the remaining call budget (ADR 0006 decision 7), and a check written
    against this column would read a full budget every time and allow every edit silently. The
    live number is in the checkpoint, which is what the gate payload carries.
    """
    settings = run_config(job)
    paused = _graph(_fake(), db).invoke(
        new_state(job_id=job, user_id="user-1", question=_QUESTION), settings
    )

    row = queries.read_job(db, job)

    assert paused["llm_calls_used"] > 0  # the checkpoint knows what the job has spent
    assert row is not None
    assert row.llm_calls_used == 0  # the row does not, until finalize writes it
    assert row.status == "awaiting_approval"  # written with gate_opened, before the pause


def test_a_rejected_job_is_recorded_as_rejected_and_exports_nothing(
    web: RecordedWeb, db: Engine, job: str
) -> None:
    # A rejection is a decision, not a failure. Nothing is exported, and the findings and
    # claims are kept - state is retained (ARCHITECTURE.md §15).
    final = _decide(_graph(_fake(), db), job, "reject")

    row = queries.read_job(db, job)
    actions = [event.action for event in queries.read_audit_events(db, job)]

    assert final["status"] == "rejected"
    assert row is not None
    assert row.status == "rejected"
    assert row.report_json is None and row.exported_at is None
    assert row.completed_at is not None
    assert "export_attempted" not in actions
    assert queries.read_findings(db, job) != []


def test_an_uncited_claim_blocks_the_export_and_stores_no_report(db: Engine, job: str) -> None:
    # CLAUDE.md invariant 1, with the database watching. The check is the same arithmetic it
    # has always been; what is new is that the attempt and the refusal are both on the record.
    finding = a_finding("f1")
    queries.record_research(db, job_id=job, new_findings=[finding], subtopic="s1", status="done")
    state = _state_with(job, _report_with_an_uncited_claim(finding.url))

    update = export_node(state, deps=_deps(db))

    row = queries.read_job(db, job)
    events = queries.read_audit_events(db, job)
    assert update == {"status": "failed", "failure_reason": "uncited_claims"}
    assert row is not None
    assert row.report_json is None and row.exported_at is None
    assert [event.action for event in events[-2:]] == ["export_attempted", "export_result"]
    assert events[-1].detail == {"result": "blocked", "uncited_claims": ["c2"]}


# --- Revisions, failures, and replays -----------------------------------------------


def test_a_revision_is_recorded_and_the_claims_follow_the_latest_draft(
    web: RecordedWeb, db: Engine, job: str
) -> None:
    # A revision writes a whole new draft with new claim ids. "This job's claims" has to mean
    # the ones in the report, not two passes' worth.
    fake = _fake(
        supervisor=[*_ROUTE_TO_THE_GATE, decision("fact_checker")],
        synthesizer=[draft(1), draft(2)],
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * 2,
        reflection=[rubric(citation_coverage=4), rubric()],
    )
    final = _approved(fake, db, job)
    report = final["report"]
    assert report is not None

    revisions = [e for e in queries.read_audit_events(db, job) if e.action == "revision"]
    written = {row.claim_id for row in queries.read_claims(db, job)}

    assert final["revision_count"] == 1
    assert len(revisions) == 1
    assert revisions[0].detail["revision"] == 1
    assert revisions[0].detail["route"] == "synthesizer"
    assert revisions[0].detail["failed_dimensions"] == ["citation_coverage"]
    assert written == {claim.claim_id for claim in report.claims}


def test_a_scoring_failure_is_recorded_and_the_job_carries_unscored(
    web: RecordedWeb, slept: list[float], db: Engine, job: str
) -> None:
    # The rubric never ran, so the reviewer was the only quality judgement on this job. The
    # row is what says so afterwards - `unscored` is not a pass (ARCHITECTURE.md §15).
    final = _approved(_fake(reflection=[server_error()] * 3), db, job)

    row = queries.read_job(db, job)
    actions = [event.action for event in queries.read_audit_events(db, job)]

    assert final["quality_flag"] == "unscored"
    assert "reflection_failed" in actions
    assert row is not None
    assert row.quality_flag == "unscored"
    assert row.status == "approved"  # the export gate still ran, and still passed


def test_replaying_the_researcher_node_writes_one_set_of_findings(
    web: RecordedWeb, db: Engine, job: str
) -> None:
    # The node writes its rows before it returns and the checkpoint is written after, so a
    # crash in between replays this node (ARCHITECTURE.md §11). Two runs, one set of rows.
    fake = _fake(researcher=[quote_the_page()] * 12)
    state = _planned_state(job, fake, db)

    first = researcher_node(state, deps=_deps(db, fake))
    second = researcher_node(state, deps=_deps(db, fake))

    rows = queries.read_findings(db, job)
    events = queries.read_audit_events(db, job)
    researched = [e for e in events if e.action == "subtopic_researched"]
    assert [f.finding_id for f in first["findings"]] == [f.finding_id for f in second["findings"]]
    assert len(rows) == len(first["findings"])
    assert len(researched) == 2  # the node really did run twice


def test_the_graph_writes_nothing_when_no_database_is_configured(
    web: RecordedWeb, db: Engine, job: str
) -> None:
    # Phase 1's behaviour, unchanged: the job runs, state carries everything, and the tables
    # stay as empty as they were. This is what `scripts/measure_jobs.py` still does.
    final = _approved(_fake(), None, job)

    row = queries.read_job(db, job)

    assert final["status"] == "approved"
    assert queries.read_findings(db, job) == []
    assert queries.read_claims(db, job) == []
    assert queries.read_claim_sources(db, job) == []
    assert [e.action for e in queries.read_audit_events(db, job)] == ["job_created"]
    assert row is not None
    # `queued` is what `create_job` wrote and nothing moved it: the worker is the only thing
    # that writes `running` (ADR 0010 decision 2), and this graph has no database to write
    # through at all. A row still saying `queued` after a job ran to `approved` in state is
    # exactly the disconnection this test is about.
    assert row.status == "queued" and row.report_json is None


# --- Helpers ------------------------------------------------------------------------


def _deps(db: Engine, fake: FakeLLM | None = None) -> NodeDeps:
    config = load_config(_ENV)
    client = LLMClient(config, client=cast(OpenAI, fake or _fake()))
    return NodeDeps(config=config, llm=client, db=db)


def _state_with(job_id: str, report: Report) -> ResearchState:
    state = new_state(job_id=job_id, user_id="user-1", question=_QUESTION)
    state["report"] = report
    return state


def _planned_state(job_id: str, fake: FakeLLM, db: Engine) -> ResearchState:
    """A job the Planner has finished, ready for its first Researcher visit."""
    state = new_state(job_id=job_id, user_id="user-1", question=_QUESTION)
    from agents.planner import plan_research

    update = plan_research(state, config=load_config(_ENV), llm=_deps(db, fake).llm)
    state.update(cast(ResearchState, update))
    return state


def _report_with_an_uncited_claim(url: HttpUrl) -> Report:
    """One claim that reaches a source, and one that reaches nothing.

    The Synthesizer cannot write the second - it fails the job first - so the case exists
    for the gate, which runs even when the reviewer has already approved.
    """
    return Report(
        sections=[Section(id="sec1", heading="Cloud", body="Both firms grew.")],
        claims=[
            Claim(claim_id="c1", section_id="sec1", text="TCS grew.", finding_ids=["f1"]),
            Claim(claim_id="c2", section_id="sec1", text="Infosys grew.", finding_ids=["f9"]),
        ],
        sources=[Source(url=url, title="Annual report", finding_ids=["f1"])],
    )


# --- The gate's own rows (step 17) --------------------------------------------------


def test_the_gate_records_one_opening_per_visit_and_not_one_per_replay(
    web: RecordedWeb, db: Engine, job: str
) -> None:
    """One `gate_opened` row per gate visit, through an edit and back.

    LangGraph re-runs an interrupted node from the top on resume, so the gate node's
    pre-interrupt code executes twice per visit. The row is keyed on the job's call count at
    the pause, which is identical across that replay and higher at the next visit - so this
    counts visits, not executions (ADR 0005's convergence rule, ADR 0006 decision 9).
    """
    fake = _fake(
        supervisor=[*_ROUTE_TO_THE_GATE, decision("fact_checker")],
        synthesizer=[draft(1), draft(2)],
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * 2,
        reflection=[rubric(), rubric()],
    )
    compiled = _graph(fake, db)
    settings = run_config(job)

    compiled.invoke(new_state(job_id=job, user_id="user-1", question=_QUESTION), settings)
    compiled.invoke(Command(resume={"decision": "edit", "edits": "Tighten section two."}), settings)
    final = compiled.invoke(Command(resume={"decision": "approve"}), settings)

    events = queries.read_audit_events(db, job)
    openings = [event for event in events if event.action == "gate_opened"]
    assert len(openings) == 2  # the first draft, and the edited one
    assert [event.actor for event in openings] == ["system", "system"]
    assert openings[0].detail["calls_used"] < openings[1].detail["calls_used"]
    assert openings[0].detail["unsupported_claims"] == 0
    # No revision was counted for the edit, so no `revision` row was written either.
    assert not [event for event in events if event.action == "revision"]
    assert final["status"] == "approved"


def test_the_job_row_says_awaiting_approval_while_the_reviewer_holds_it(
    web: RecordedWeb, db: Engine, job: str
) -> None:
    # What a poller sees while the graph is stopped. The graph's own `status` cannot say it -
    # an interrupted node's update is discarded - so the row is where it lives.
    settings = run_config(job)
    compiled = _graph(_fake(), db)
    compiled.invoke(new_state(job_id=job, user_id="user-1", question=_QUESTION), settings)

    waiting = queries.read_job(db, job)
    final = compiled.invoke(Command(resume={"decision": "approve"}), settings)
    finished = queries.read_job(db, job)

    assert waiting is not None and waiting.status == "awaiting_approval"
    assert finished is not None and finished.status == "approved"
    assert final["status"] == "approved"
