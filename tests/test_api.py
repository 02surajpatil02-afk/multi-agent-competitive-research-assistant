"""
WHY THIS FILE EXISTS
    The outermost boundary, tested through the real routes: a real FastAPI application, the
    real authentication dependency, the real statements, a real migrated database, and the
    real graph. Only the model and the web are replaced (tests/harness.py), so what is under
    test is the contract in guidelines §12 and §16 rather than a mock of it.

    Four groups, in the order the failures matter.

    **Nobody gets in without a key.** Every route that touches job data answers `401`
    unauthenticated, and `/health` is the single exception - the one guidelines §18 names as
    a shipping condition. A `submitter` presenting a perfectly valid key to the gate gets
    `403`, because approving is an authorization decision and not a spelling of "read".

    **The gate's refusals cost nothing.** ADR 0006 bounds reviewer edits two ways, and both
    are checked before the graph is touched: a refused edit writes no `reviewer_decision`,
    resumes nothing, and spends no LLM call. The decisive one is the budget check, which
    reads the **checkpoint** - `jobs.llm_calls_used` is `0` for the whole time a job waits at
    the gate, so a check against it would allow every edit silently. That is asserted here
    with the two numbers deliberately disagreeing.

    **A decision names the person who made it.** `audit_events.actor` is the `user_id` the
    API key maps to, never anything from the request body.

    **Deciding twice is not deciding twice.** The gate is claimed with a conditional update,
    so the second caller is told the gate was already answered rather than resuming the same
    thread again.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from dbharness import migrated_engine, new_job_id
from fastapi.testclient import TestClient
from harness import (
    FakeLLM,
    Page,
    RecordedWeb,
    decision,
    draft,
    outside_untrusted_blocks,
    plan,
    quote_the_page,
    rubric,
    verdict_batch,
)
from openai import OpenAI
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from app import create_application
from config import Config, load_config
from database import queries
from database.schema import audit_events, jobs
from graph.build import ResearchGraph, build_graph, run_config
from graph.state import new_state
from llm_client import LLMClient
from routes.api import MAX_REVIEWER_TEXT_CHARS
from routes.auth import AuthConfigError, Identity, hash_key, identity_from, load_api_keys

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

_CALLS_TO_THE_GATE = 16
"""What one clean job spends before it pauses: 1 planner, 6 researcher, 1 synthesizer,
1 fact-checker, 1 reflection, and 6 Supervisor hops. Used to put the budget exactly where a
test needs it rather than approximately there."""

REVIEWER = "11111111-1111-4111-8111-111111111111"
SUBMITTER = "22222222-2222-4222-8222-222222222222"
OUTSIDER = "33333333-3333-4333-8333-333333333333"

_KEYS: dict[str, Identity] = {
    hash_key("reviewer-key"): Identity(user_id=REVIEWER, role="reviewer"),
    hash_key("submitter-key"): Identity(user_id=SUBMITTER, role="submitter"),
    hash_key("outsider-key"): Identity(user_id=OUTSIDER, role="submitter"),
}
"""Three callers, because three things need proving: a reviewer may decide, a submitter may
not, and a submitter may not read someone else's job. The table holds hashes, never keys."""

_REVIEWER_AUTH = {"Authorization": "Bearer reviewer-key"}
_SUBMITTER_AUTH = {"Authorization": "Bearer submitter-key"}
_OUTSIDER_AUTH = {"Authorization": "Bearer outsider-key"}

PROTECTED = [
    ("POST", "/jobs", {"question": _QUESTION}),
    ("GET", "/jobs/{job_id}", None),
    ("POST", "/jobs/{job_id}/approve", {"decision": "approve"}),
    ("GET", "/jobs/{job_id}/report", None),
]
"""Every route except `/health`. guidelines §18 makes "unauthenticated is rejected on all of
them" a Phase 2 shipping condition, so the list is parametrized rather than sampled."""


# --- The application under test -----------------------------------------------------


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
def db(tmp_path: Path) -> Engine:
    return migrated_engine(tmp_path)


@pytest.fixture
def fake() -> FakeLLM:
    """One clean job to the gate, plus what a single reviewer edit needs after it.

    The third draft is for the ADR 0007 tests only: an edit whose persistence fails has
    already spent its Synthesizer call, so the retry that finishes it spends another. Nothing
    else consumes it, and an unused answer costs a test nothing.
    """
    return FakeLLM(
        supervisor=[
            decision("planner"),
            *[decision("researcher")] * 3,
            decision("synthesizer"),
            decision("fact_checker"),
            decision("fact_checker"),
        ],
        planner=[plan(*_SUBTOPICS)],
        researcher=[quote_the_page()] * 6,
        synthesizer=[draft(1), draft(2), draft(3)],
        fact_checker=[verdict_batch(quote=_sentence("1a"))] * 2,
        reflection=[rubric(), rubric()],
    )


@pytest.fixture
def config() -> Config:
    return load_config(_ENV)


@pytest.fixture
def graph(config: Config, fake: FakeLLM, db: Engine) -> ResearchGraph:
    return build_graph(config=config, llm=LLMClient(config, client=cast(OpenAI, fake)), db=db)


@pytest.fixture
def client(config: Config, db: Engine, graph: ResearchGraph) -> Iterator[TestClient]:
    with TestClient(create_application(config=config, engine=db, graph=graph, keys=_KEYS)) as made:
        yield made


@pytest.fixture
def unraising_client(config: Config, db: Engine, graph: ResearchGraph) -> Iterator[TestClient]:
    """The same application, returning a server error instead of re-raising it.

    `TestClient` re-raises an unhandled exception by default, which is what every other test
    here wants: a `500` nobody asked for should fail the test loudly rather than be asserted
    on. The ADR 0007 tests are about the response a reviewer actually receives when a resume
    dies, so this one lets it come back as a response - which is what uvicorn does in front of
    the same handler.
    """
    application = create_application(config=config, engine=db, graph=graph, keys=_KEYS)
    with TestClient(application, raise_server_exceptions=False) as made:
        yield made


def _submitted(client: TestClient) -> str:
    """A job row, created the way a submitter creates one."""
    response = client.post("/jobs", json={"question": _QUESTION}, headers=_SUBMITTER_AUTH)
    assert response.status_code == 202, response.text
    return cast(str, response.json()["job_id"])


def _at_the_gate(client: TestClient, graph: ResearchGraph) -> str:
    """A submitted job, run to the human gate the way the Phase 3 worker will run it."""
    job_id = _submitted(client)
    graph.invoke(
        new_state(job_id=job_id, user_id=SUBMITTER, question=_QUESTION), run_config(job_id)
    )
    return job_id


def _decisions(db: Engine, job_id: str) -> list[Any]:
    return [
        event
        for event in queries.read_audit_events(db, job_id)
        if event.action == "reviewer_decision"
    ]


def _openings(db: Engine, job_id: str) -> list[Any]:
    return [
        event for event in queries.read_audit_events(db, job_id) if event.action == "gate_opened"
    ]


def _status(db: Engine, job_id: str) -> str:
    row = queries.read_job(db, job_id)
    assert row is not None
    return cast(str, row.status)


# --- Authentication -----------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_every_route_but_health_refuses_an_unauthenticated_caller(
    method: str, path: str, body: dict[str, Any] | None, client: TestClient
) -> None:
    response = client.request(method, path.format(job_id=new_job_id()), json=body)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_an_unknown_key_is_refused_like_no_key_at_all(
    method: str, path: str, body: dict[str, Any] | None, client: TestClient
) -> None:
    # Same answer for an absent header, a wrong scheme and a wrong key: telling them apart
    # would say which half of the credential to keep working on.
    for header in ({"Authorization": "Bearer nope"}, {"Authorization": "Basic reviewer-key"}):
        response = client.request(
            method, path.format(job_id=new_job_id()), json=body, headers=header
        )

        assert response.status_code == 401


def test_health_is_the_one_route_that_needs_no_key(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"db": True}}


def test_health_says_nothing_a_stranger_should_not_know(client: TestClient) -> None:
    # A status and one boolean per dependency. No secret, no connection string, no hostname,
    # no version, no job data, no counts, no error text (guidelines §16).
    body = response = client.get("/health").json()

    assert set(body) == {"status", "checks"}
    assert all(isinstance(value, bool) for value in body["checks"].values())
    # Redis arrives in Phase 3; reporting it unhealthy now would fail every check forever.
    assert "redis" not in body["checks"]
    assert "sqlite" not in str(response).lower()


# --- Authorization ------------------------------------------------------------------


def test_a_submitter_may_not_decide_at_the_gate(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine
) -> None:
    # guidelines §18's other named shipping condition. A valid key is not permission.
    job_id = _at_the_gate(client, graph)

    response = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_SUBMITTER_AUTH
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "reviewer_role_required"
    assert _decisions(db, job_id) == []  # refused before anything was recorded


def test_a_submitter_cannot_read_someone_elses_job(client: TestClient) -> None:
    job_id = _submitted(client)

    response = client.get(f"/jobs/{job_id}", headers=_OUTSIDER_AUTH)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_the_owner"


def test_a_reviewer_may_read_a_job_it_did_not_submit(client: TestClient) -> None:
    # Deciding without reading would be approving unseen, which is what the gate exists to
    # prevent - so the role that decides can read.
    job_id = _submitted(client)

    assert client.get(f"/jobs/{job_id}", headers=_REVIEWER_AUTH).status_code == 200


# --- Submitting -------------------------------------------------------------------


def test_a_submitted_job_is_accepted_and_recorded(client: TestClient, db: Engine) -> None:
    response = client.post("/jobs", json={"question": _QUESTION}, headers=_SUBMITTER_AUTH)

    body = response.json()
    row = queries.read_job(db, body["job_id"])
    assert response.status_code == 202
    assert body["status"] == "running"
    assert row is not None
    assert row.user_id == SUBMITTER  # the identity came from the key, not the body
    assert row.question == _QUESTION
    assert [event.action for event in queries.read_audit_events(db, body["job_id"])] == [
        "job_created"
    ]


def test_the_same_question_twice_in_one_day_is_one_job(client: TestClient, db: Engine) -> None:
    # The unique constraint decides, and the caller gets the job they already have
    # (ARCHITECTURE.md §9). Two jobs would be 60 calls of budget spent on a duplicate.
    first = _submitted(client)

    response = client.post("/jobs", json={"question": _QUESTION}, headers=_SUBMITTER_AUTH)

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "duplicate_job",
        "message": "This question was already submitted today",
        "job_id": first,
    }
    with db.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(jobs)).scalar_one() == 1


def test_two_callers_asking_the_same_question_get_their_own_jobs(client: TestClient) -> None:
    # The key is scoped to the user, so one caller's question never collides with another's.
    first = _submitted(client)

    response = client.post("/jobs", json={"question": _QUESTION}, headers=_OUTSIDER_AUTH)

    assert response.status_code == 202
    assert response.json()["job_id"] != first


@pytest.mark.parametrize("question", ["", "   ", "\x00\x01"])
def test_an_empty_question_is_refused(question: str, client: TestClient) -> None:
    response = client.post("/jobs", json={"question": question}, headers=_SUBMITTER_AUTH)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_question"


def test_a_question_longer_than_the_cap_is_refused(client: TestClient) -> None:
    response = client.post("/jobs", json={"question": "a" * 501}, headers=_SUBMITTER_AUTH)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_question"


def test_control_characters_are_stripped_before_the_question_is_stored(
    client: TestClient, db: Engine
) -> None:
    # It reaches a prompt and a row, so it is cleaned at the edge (guidelines §16).
    dirty = "Compare" + chr(0) + " TCS" + chr(0x200B) + " and" + chr(10) + " Infosys."

    response = client.post("/jobs", json={"question": dirty}, headers=_SUBMITTER_AUTH)

    row = queries.read_job(db, response.json()["job_id"])
    assert row is not None
    assert row.question == "Compare TCS and Infosys."


def test_a_body_the_api_cannot_parse_uses_the_one_error_shape(client: TestClient) -> None:
    response = client.post("/jobs", json={"not_a_question": 1}, headers=_SUBMITTER_AUTH)

    assert response.status_code == 400
    assert set(response.json()["error"]) == {"code", "message", "job_id"}


# --- Reading ------------------------------------------------------------------------


def test_an_unknown_job_is_a_404_in_the_documented_shape(client: TestClient) -> None:
    missing = new_job_id()

    response = client.get(f"/jobs/{missing}", headers=_REVIEWER_AUTH)

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "job_not_found",
        "message": "No such job",
        "job_id": missing,
    }


def test_polling_a_job_returns_the_documented_fields(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph
) -> None:
    job_id = _at_the_gate(client, graph)

    body = client.get(f"/jobs/{job_id}", headers=_SUBMITTER_AUTH).json()

    assert set(body) == {"job_id", "status", "phase", "revision_count", "quality_flag", "report"}
    assert body["status"] == "awaiting_approval"
    assert body["phase"] == "human_gate"  # a coarse label, read off the checkpoint
    assert body["report"] is None  # nothing is exported until the gate passes


def test_a_job_nobody_has_run_reports_that_it_is_queued(client: TestClient) -> None:
    # Phase 2 has no worker, so this is every job until Phase 3 dequeues one.
    job_id = _submitted(client)

    assert client.get(f"/jobs/{job_id}", headers=_SUBMITTER_AUTH).json()["phase"] == "queued"


def test_the_report_route_is_a_404_until_s3_exists(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph
) -> None:
    # Phase 2 answers `404 not exported` by design; the body is read from GET /jobs/{id},
    # which already carries it. No stand-in for S3 is built (ARCHITECTURE.md §8).
    job_id = _at_the_gate(client, graph)
    client.post(f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH)

    response = client.get(f"/jobs/{job_id}/report", headers=_SUBMITTER_AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_exported"
    # And the approved body is where the specification says to read it instead.
    assert client.get(f"/jobs/{job_id}", headers=_SUBMITTER_AUTH).json()["report"] is not None


# --- Deciding at the gate -----------------------------------------------------------


def test_a_reviewer_can_approve_and_the_export_runs(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine
) -> None:
    job_id = _at_the_gate(client, graph)

    response = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )

    row = queries.read_job(db, job_id)
    assert response.status_code == 200
    assert response.json() == {"job_id": job_id, "status": "approved"}
    assert row is not None
    assert row.status == "approved"
    assert row.report_json is not None  # the export gate passed and stored the body
    assert row.exported_at is not None


def test_a_reviewer_can_reject_and_nothing_is_exported(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine
) -> None:
    job_id = _at_the_gate(client, graph)

    response = client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "reject", "note": "the sources are weak"},
        headers=_REVIEWER_AUTH,
    )

    row = queries.read_job(db, job_id)
    assert response.json()["status"] == "rejected"
    assert row is not None
    assert row.status == "rejected"
    assert row.report_json is None and row.exported_at is None


def test_the_decision_records_who_made_it_and_which_gate_visit_it_answers(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine
) -> None:
    # "The report was approved" is not worth auditing; "this person approved it" is
    # (guidelines §9). The identity is the key's, and the body cannot override it.
    #
    # `calls_used` is ADR 0007's gate-visit key, and it is the *same* value the `gate_opened`
    # row carries - which is what makes "which opening does this decision answer?" a join.
    job_id = _at_the_gate(client, graph)

    client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "approve", "note": "reads well"},
        headers=_REVIEWER_AUTH,
    )

    decided = _decisions(db, job_id)
    assert len(decided) == 1
    assert decided[0].actor == REVIEWER
    assert decided[0].detail == {
        "decision": "approve",
        "note": "reads well",
        "edits": None,
        "calls_used": _CALLS_TO_THE_GATE,
    }
    assert [event.detail["calls_used"] for event in _openings(db, job_id)] == [_CALLS_TO_THE_GATE]


def test_the_gate_opening_stays_a_system_event(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine
) -> None:
    # `gate_opened` is the graph saying it stopped; `reviewer_decision` is a person answering.
    # Two actors, two events, and the API writes only the second (ADR 0006 decision 9).
    job_id = _at_the_gate(client, graph)
    client.post(f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH)

    trail = [(event.actor, event.action) for event in queries.read_audit_events(db, job_id)]

    assert ("system", "gate_opened") in trail
    assert (REVIEWER, "reviewer_decision") in trail
    assert trail.index(("system", "gate_opened")) < trail.index((REVIEWER, "reviewer_decision"))


def test_a_second_decision_on_the_same_gate_is_refused(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine
) -> None:
    # Repeated requests, and concurrent ones: the gate is claimed with a conditional update,
    # so exactly one caller resumes the thread.
    job_id = _at_the_gate(client, graph)
    client.post(f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH)

    again = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "job_not_awaiting_approval"
    assert len(_decisions(db, job_id)) == 1  # the second one recorded nothing


def test_a_job_that_never_reached_the_gate_cannot_be_decided(
    client: TestClient, db: Engine
) -> None:
    job_id = _submitted(client)

    response = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_awaiting_approval"
    assert _decisions(db, job_id) == []


def test_a_decision_the_api_cannot_parse_is_refused(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine
) -> None:
    # An edit with nothing to apply, and a decision outside the three: neither reaches the
    # graph, because guessing which one was meant is the silent wrong answer to refuse.
    job_id = _at_the_gate(client, graph)

    for body in ({"decision": "edit"}, {"decision": "export"}):
        response = client.post(f"/jobs/{job_id}/approve", json=body, headers=_REVIEWER_AUTH)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
    assert _decisions(db, job_id) == []


# --- The reviewer edit, and the two bounds that refuse it ---------------------------


def test_an_edit_runs_one_synthesizer_pass_and_comes_back_to_the_gate(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, fake: FakeLLM
) -> None:
    # Step 17's flow, driven through the API this time: one pass over the evidence the job
    # already holds, verified, scored, and handed back to the reviewer (ADR 0006).
    job_id = _at_the_gate(client, graph)
    before = fake.roles.count("researcher")

    response = client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": "Tighten section two."},
        headers=_REVIEWER_AUTH,
    )

    state = graph.get_state(run_config(job_id))
    row = queries.read_job(db, job_id)
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_approval"  # back to the human
    assert row is not None and row.status == "awaiting_approval"
    assert state.next == ("human_gate",)
    assert state.values["revision_count"] == 0  # an edit is not a revision
    assert state.values["reviewer_edit_text"] == "Tighten section two."
    assert fake.roles.count("researcher") == before  # no research, ever, from an edit
    assert web.queries == list(_SUBTOPICS)
    assert len(_decisions(db, job_id)) == 1


def test_an_edit_beyond_the_bound_is_refused_before_the_graph_runs(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, fake: FakeLLM
) -> None:
    # MAX_REVIEWER_EDITS = 3, counted from the trail this endpoint writes.
    job_id = _at_the_gate(client, graph)
    _pretend_edits(db, job_id, count=3)
    spent = len(fake.requests)

    response = client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": "One more thing."},
        headers=_REVIEWER_AUTH,
    )

    row = queries.read_job(db, job_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reviewer_edit_limit_reached"
    assert len(fake.requests) == spent  # no LLM call was made
    assert len(_decisions(db, job_id)) == 3  # no fourth decision was recorded
    assert row is not None and row.status == "awaiting_approval"  # the gate is still open


def test_an_edit_the_budget_cannot_fund_is_refused_before_the_graph_runs(
    web: RecordedWeb, db: Engine, fake: FakeLLM
) -> None:
    # The job has spent 16 of its 18 calls, so two remain and an edit needs three.
    config = load_config({**_ENV, "MAX_LLM_CALLS_PER_JOB": "18"})
    graph = build_graph(config=config, llm=LLMClient(config, client=cast(OpenAI, fake)), db=db)
    with TestClient(
        create_application(config=config, engine=db, graph=graph, keys=_KEYS)
    ) as client:
        job_id = _at_the_gate(client, graph)
        spent = len(fake.requests)

        response = client.post(
            f"/jobs/{job_id}/approve",
            json={"decision": "edit", "edits": "Tighten section two."},
            headers=_REVIEWER_AUTH,
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_call_budget_for_edit"
    assert len(fake.requests) == spent
    assert _decisions(db, job_id) == []


def test_the_budget_check_reads_the_checkpoint_and_not_the_jobs_row(
    web: RecordedWeb, db: Engine, fake: FakeLLM
) -> None:
    """The decisive test for ADR 0006 decision 7, with the two numbers disagreeing.

    `jobs.llm_calls_used` is written by `finalize`, so a job waiting at the gate has `0` in
    that column while its checkpoint knows what it really spent. A budget check written
    against the row would compute a full budget and allow every edit, silently - so the two
    are made to disagree here and the refusal has to follow the checkpoint.
    """
    config = load_config({**_ENV, "MAX_LLM_CALLS_PER_JOB": "18"})
    graph = build_graph(config=config, llm=LLMClient(config, client=cast(OpenAI, fake)), db=db)
    with TestClient(
        create_application(config=config, engine=db, graph=graph, keys=_KEYS)
    ) as client:
        job_id = _at_the_gate(client, graph)

        row = queries.read_job(db, job_id)
        live = graph.get_state(run_config(job_id)).values["llm_calls_used"]
        response = client.post(
            f"/jobs/{job_id}/approve",
            json={"decision": "edit", "edits": "Tighten section two."},
            headers=_REVIEWER_AUTH,
        )

    assert row is not None
    assert row.llm_calls_used == 0  # the row: stale by design
    assert live == _CALLS_TO_THE_GATE  # the checkpoint: the truth
    assert response.status_code == 409  # and the refusal followed the checkpoint


def test_a_reviewer_can_still_approve_a_job_whose_edits_are_exhausted(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine
) -> None:
    # A refusal is about the edit, not about the reviewer: approve and reject cost nothing
    # and stay available, which is what keeps a bounded job finishable.
    job_id = _at_the_gate(client, graph)
    _pretend_edits(db, job_id, count=3)

    response = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def _pretend_edits(db: Engine, job_id: str, *, count: int) -> None:
    """Reviewer decisions this endpoint would have written, without spending a job to get them.

    Inserted directly because what is under test is the bound, not the three edits: each real
    one would cost a Synthesizer, a Fact-Checker and a reflection pass.
    """
    for _ in range(count):
        with db.begin() as conn:
            conn.execute(
                sa.insert(audit_events).values(
                    job_id=job_id,
                    actor=REVIEWER,
                    action="reviewer_decision",
                    detail={"decision": "edit", "note": None, "edits": "earlier"},
                )
            )


# --- The reviewer's own text, cleaned at the edge (ADR 0006 decision 8) -------------
#
# Reviewer text is authenticated input, so it is deliberately *not* wrapped in an untrusted
# block - it reaches the Synthesizer's prompt as an instruction to follow. That is what makes
# cleaning it load-bearing rather than tidy: a field the model obeys is the field a control
# character must not survive in, and the same string is written to `audit_events.detail`. Both
# of guidelines §16's destinations - a prompt and the database - are on this path.


def _last_draft_instructions(fake: FakeLLM) -> str:
    """What the Synthesizer was told to act on, with the fetched pages removed.

    The reviewer's words live outside the untrusted block by design (ADR 0006 decision 8), so
    reading only that half proves the cleaned text arrived as an *instruction* rather than
    merely appearing somewhere in a prompt that also quotes whole web pages.
    """
    return outside_untrusted_blocks(fake.requests_for("synthesizer")[-1].user)


def test_control_characters_are_stripped_from_the_reviewer_instruction(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, fake: FakeLLM
) -> None:
    job_id = _at_the_gate(client, graph)
    dirty = "Tighten" + chr(0) + " section" + chr(0x200B) + " two" + chr(10) + " please."

    response = client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": dirty, "note": "looks" + chr(0) + " thin"},
        headers=_REVIEWER_AUTH,
    )

    assert response.status_code == 200
    decided = _decisions(db, job_id)[0]
    assert decided.detail["edits"] == "Tighten section two please."
    assert decided.detail["note"] == "looks thin"
    # The cleaned value is the one the model was shown, not the raw one.
    assert "Tighten section two please." in _last_draft_instructions(fake)
    assert chr(0) not in fake.requests_for("synthesizer")[-1].user


def test_whitespace_in_the_reviewer_instruction_is_collapsed(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, fake: FakeLLM
) -> None:
    job_id = _at_the_gate(client, graph)

    client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": "  Tighten    section \t two.  "},
        headers=_REVIEWER_AUTH,
    )

    assert _decisions(db, job_id)[0].detail["edits"] == "Tighten section two."
    assert "Tighten section two." in _last_draft_instructions(fake)


@pytest.mark.parametrize("field", ["edits", "note"])
def test_reviewer_text_longer_than_the_cap_is_refused(
    field: str,
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    fake: FakeLLM,
) -> None:
    # The same cap the question gets, for the same reason: this text reaches a prompt and a
    # row. `note` is capped too - it does not reach a prompt, but it does reach the database.
    job_id = _at_the_gate(client, graph)
    body = {"decision": "edit", "edits": "Tighten section two.", field: "a" * 501}
    spent = len(fake.requests)

    response = client.post(f"/jobs/{job_id}/approve", json=body, headers=_REVIEWER_AUTH)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert set(response.json()["error"]) == {"code", "message", "job_id"}
    assert len(fake.requests) == spent
    assert _decisions(db, job_id) == []


def test_reviewer_text_at_the_cap_is_accepted(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine
) -> None:
    # The boundary is inclusive, like the question's.
    job_id = _at_the_gate(client, graph)

    response = client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": "a" * MAX_REVIEWER_TEXT_CHARS},
        headers=_REVIEWER_AUTH,
    )

    assert response.status_code == 200
    assert _decisions(db, job_id)[0].detail["edits"] == "a" * MAX_REVIEWER_TEXT_CHARS


def test_an_edit_that_is_only_control_characters_is_refused(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, fake: FakeLLM
) -> None:
    """The gap between the model's rule and the cleaned value.

    `GateDecision` refuses an edit whose `edits` is empty, and `"\\x00\\x00".strip()` is not
    empty - `str.strip()` removes whitespace, and a NUL is not whitespace. So this body passes
    validation and cleans down to nothing, which is an edit with no instruction: a Synthesizer
    pass that would cost three calls and produce the same draft again.
    """
    job_id = _at_the_gate(client, graph)
    spent = len(fake.requests)

    response = client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": chr(0) + chr(0x200B)},
        headers=_REVIEWER_AUTH,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert len(fake.requests) == spent
    assert _decisions(db, job_id) == []


def test_refused_reviewer_text_records_no_decision_and_resumes_nothing(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine, fake: FakeLLM
) -> None:
    # The cleaning runs before the job is even read, so a refusal costs the job nothing at all
    # and leaves the gate exactly as it was - approve and reject stay available.
    job_id = _at_the_gate(client, graph)
    spent = len(fake.requests)

    client.post(
        f"/jobs/{job_id}/approve",
        json={"decision": "edit", "edits": "a" * 501},
        headers=_REVIEWER_AUTH,
    )

    snapshot = graph.get_state(run_config(job_id))
    assert snapshot.next == ("human_gate",)
    assert snapshot.interrupts  # the graph never moved
    assert _status(db, job_id) == "awaiting_approval"
    assert _decisions(db, job_id) == []
    assert len(fake.requests) == spent
    # Approve and reject stay available, exactly as they do after an edit the bounds refuse.
    still_open = client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )
    assert still_open.json()["status"] == "approved"


# --- One decision per gate visit, and a resume that dies (ADR 0007) -----------------
#
# Every test below starts from the same measured failure: a reviewer sends `edit`, the graph
# resumes, and a database write inside the Synthesizer raises. What used to happen then was
# three separate wrongs - the row said `awaiting_approval` while the checkpoint sat at
# `synthesizer` with no interrupt, the reviewer got a bare `Internal Server Error`, and the
# retry that fixed the job wrote a second decision and spent a second edit.

_EDIT = {"decision": "edit", "edits": "Tighten section two."}


def _fail_the_synthesizer_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break the one write the Synthesizer node makes, and nothing else.

    The agent still succeeds, so the failure lands exactly where ADR 0007's probe put it:
    after the gate node has completed and the graph has moved on, which is the window where
    the row and the checkpoint used to disagree.
    """

    def raise_instead(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("INSERT INTO claims ...", {}, Exception("connection lost"))

    monkeypatch.setattr(queries, "record_claims", raise_instead)


def _failed_edit(
    client: TestClient, graph: ResearchGraph, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, Any]:
    """A job whose reviewer `edit` was recorded and whose resume then died."""
    job_id = _at_the_gate(client, graph)
    _fail_the_synthesizer_write(monkeypatch)
    response = client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)
    monkeypatch.undo()
    return job_id, response


def test_a_failed_resume_leaves_the_row_and_the_checkpoint_agreeing(
    web: RecordedWeb,
    unraising_client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0007's headline divergence, asserted from both sides.

    The checkpoint is mid-edit-pass with no interrupt to answer, so `awaiting_approval` is a
    lie: it tells a poller a human is holding the job, and it lets a second reviewer past the
    status check and into the same thread. `running` is what is actually true.
    """
    job_id, response = _failed_edit(unraising_client, graph, monkeypatch)

    snapshot = graph.get_state(run_config(job_id))
    assert response.status_code == 500
    assert snapshot.next == ("synthesizer",)
    assert not snapshot.interrupts  # nobody is being waited on
    assert _status(db, job_id) == "running"  # and the row now says so
    assert len(_decisions(db, job_id)) == 1  # the human did decide, and that stands


def test_a_resume_that_dies_before_the_gate_node_completes_leaves_the_gate_open(
    web: RecordedWeb,
    unraising_client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of ADR 0007's reconcile table, and the half only the reconcile fixes.

    `claim_gate` has already moved the row to `running`, and then the gate node itself fails
    on the replay - so the interrupt is still pending and a human really is still holding this
    job. `running` would be the wrong answer here for the same reason `awaiting_approval` is
    the wrong answer after the node completes: the status is derived from the checkpoint, not
    asserted beside it.

    The predicate is the pending interrupt rather than `next`, which is what makes these two
    failures distinguishable at all - both report `next == ("human_gate",)` at some point.
    """
    job_id = _at_the_gate(unraising_client, graph)

    def raise_instead(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("UPDATE jobs ...", {}, Exception("connection lost"))

    monkeypatch.setattr(queries, "record_gate_opened", raise_instead)
    response = unraising_client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )
    monkeypatch.undo()

    snapshot = graph.get_state(run_config(job_id))
    assert response.status_code == 500
    assert snapshot.next == ("human_gate",)
    assert snapshot.interrupts  # the graph is still stopped for a person
    assert _status(db, job_id) == "awaiting_approval"  # so the row says so again
    # And the decision stands, so the retry is the recovery path here too.
    assert len(_decisions(db, job_id)) == 1
    retry = unraising_client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )
    assert retry.json() == {"job_id": job_id, "status": "approved"}
    assert len(_decisions(db, job_id)) == 1


def test_an_unexpected_failure_returns_the_documented_error_envelope(
    web: RecordedWeb,
    unraising_client: TestClient,
    graph: ResearchGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "One shape, everywhere" is only true if the framework's default 500 cannot leak through
    # it (guidelines §12, §16). A stack trace, an internal path, or the failing SQL in a
    # response body is the leak the catch-all exists to stop - the reason goes to the log.
    job_id, response = _failed_edit(unraising_client, graph, monkeypatch)

    body = response.json()
    assert response.status_code == 500
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "job_id"}
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["job_id"] == job_id
    for leaked in ("Traceback", "INSERT INTO", "sqlalchemy", "connection lost", "claims"):
        assert leaked not in response.text


def test_retrying_the_same_decision_records_nothing_new_and_finishes_the_job(
    web: RecordedWeb,
    unraising_client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry path *is* the recovery path: the same request again, and no second row.

    `count_reviewer_edits` counts rows, so a second one would spend one of ADR 0006's three
    edits on an edit that never happened - the consequence a reviewer would actually feel.
    """
    job_id, _ = _failed_edit(unraising_client, graph, monkeypatch)

    retry = unraising_client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)

    assert retry.status_code == 200
    assert retry.json()["status"] == "awaiting_approval"  # the edit pass finished
    assert _status(db, job_id) == "awaiting_approval"
    assert len(_decisions(db, job_id)) == 1
    assert queries.count_reviewer_edits(db, job_id) == 1  # one edit asked for, one counted


def test_a_retry_resumes_only_the_node_that_was_in_flight(
    web: RecordedWeb,
    unraising_client: TestClient,
    graph: ResearchGraph,
    fake: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A retry invokes the same thread, so LangGraph replays from the last checkpoint. The
    # cost is bounded by the single node that was in flight - which is the whole reason this
    # record needs no compensating action: there is nothing to undo, only something to finish.
    job_id, _ = _failed_edit(unraising_client, graph, monkeypatch)
    before = {role: fake.roles.count(role) for role in ("planner", "researcher", "synthesizer")}
    searched = list(web.queries)

    unraising_client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)

    after = {role: fake.roles.count(role) for role in before}
    assert after["planner"] == before["planner"]
    assert after["researcher"] == before["researcher"]
    assert after["synthesizer"] == before["synthesizer"] + 1  # only the node that was in flight
    assert web.queries == searched  # and no search was re-issued
    assert web.queries == list(_SUBTOPICS)


def test_a_different_decision_on_a_decided_visit_is_refused(
    web: RecordedWeb,
    unraising_client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A reviewer cannot change their mind during a failure. That is a real cost, accepted:
    # the alternative is two decisions racing on one thread (ADR 0007 invariant 3).
    job_id, _ = _failed_edit(unraising_client, graph, monkeypatch)

    response = unraising_client.post(
        f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "gate_already_decided"
    assert response.json()["error"]["job_id"] == job_id
    assert len(_decisions(db, job_id)) == 1  # nothing was written
    assert _status(db, job_id) == "running"  # and nothing was resumed


def test_the_gate_replay_does_not_reopen_the_gate(
    web: RecordedWeb,
    client: TestClient,
    graph: ResearchGraph,
    db: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claim_gate`'s `running` has to survive the whole resume, not milliseconds of it.

    LangGraph re-runs the interrupted gate node from the top, so `record_gate_opened` executes
    again on the way *out* of the gate. With its status write outside the already-guarded
    branch, that handed the row straight back to `awaiting_approval` - and a second reviewer
    arriving mid-pass would pass the status check, claim, and invoke the same thread.

    The status is read from inside the Synthesizer node, which is the first thing to run after
    the gate node completes: that is the moment the old code got wrong.
    """
    seen: list[str] = []
    original = queries.record_claims

    def watch(*args: Any, **kwargs: Any) -> None:
        seen.append(_status(db, cast(str, kwargs["job_id"])))
        original(*args, **kwargs)

    job_id = _at_the_gate(client, graph)
    monkeypatch.setattr(queries, "record_claims", watch)

    response = client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)

    assert response.status_code == 200
    assert seen == ["running"]  # the gate stayed claimed for the whole edit pass
    assert _status(db, job_id) == "awaiting_approval"  # and the *next* visit opened it again
    assert len(_openings(db, job_id)) == 2


def test_two_gate_visits_on_one_job_carry_different_keys(
    web: RecordedWeb, client: TestClient, graph: ResearchGraph, db: Engine
) -> None:
    """The uniqueness the visit key rests on, asserted rather than argued.

    It is a property of the topology, not of the counter: `human_gate` is reachable only from
    `reflection`, and a second draft to score costs a Synthesizer, a Supervisor hop, a
    Fact-Checker and a reflection pass. A future edge that reached the gate without spending a
    call would collide two visits onto one key silently - so it fails here first.
    """
    job_id = _at_the_gate(client, graph)
    client.post(f"/jobs/{job_id}/approve", json=_EDIT, headers=_REVIEWER_AUTH)

    client.post(f"/jobs/{job_id}/approve", json={"decision": "approve"}, headers=_REVIEWER_AUTH)

    openings = [event.detail["calls_used"] for event in _openings(db, job_id)]
    decided = [event.detail["calls_used"] for event in _decisions(db, job_id)]
    assert len(openings) == 2
    assert openings[0] < openings[1]  # strictly greater at the next visit
    assert decided == openings  # each decision answers the opening it was sent to


# --- The key table itself -----------------------------------------------------------


def test_the_key_table_is_validated_once_at_startup() -> None:
    table = load_api_keys(json.dumps({hash_key("k"): {"user_id": REVIEWER, "role": "reviewer"}}))

    assert table[hash_key("k")] == Identity(user_id=REVIEWER, role="reviewer")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        "[]",
        "{}",
        json.dumps({"abc": {"user_id": REVIEWER}}),
        json.dumps({"abc": {"user_id": REVIEWER, "role": "admin"}}),
    ],
)
def test_an_unusable_key_table_stops_the_process_rather_than_every_request(
    raw: str | None,
) -> None:
    # A service that cannot authenticate anyone should fail to boot. Refusing every caller
    # instead would look like an outage nobody can explain.
    with pytest.raises(AuthConfigError):
        load_api_keys(raw)


def test_the_table_holds_hashes_and_never_a_key() -> None:
    # An operator reading the environment, a log line, or a crash dump finds a digest.
    raw = json.dumps({hash_key("super-secret"): {"user_id": REVIEWER, "role": "reviewer"}})

    assert "super-secret" not in raw
    assert identity_from("Bearer super-secret", load_api_keys(raw)) is not None
    assert identity_from("Bearer wrong", load_api_keys(raw)) is None
