"""
WHY THIS FILE EXISTS
    Configuration is where a limit silently becomes the wrong number. These tests pin the
    defaults to CLAUDE.md's environment table, prove that a missing required variable
    fails loudly rather than defaulting to something plausible, and check that a Config is
    safe to log.

WHO CALLS IT
    pytest.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from agents.researcher import MAX_LLM_CALLS_PER_SUBTOPIC
from config import MAX_RESEARCHER_CONCURRENCY, load_config

# Distinctive fake values, so a leak test can look for them by name. DATABASE_URL is a
# secret when it is set - it carries a password - but it is not required until Phase 2.
_SECRETS = {
    "LLM_API_KEY": "llm-key-should-not-be-printed",
    "TAVILY_API_KEY": "tavily-key-should-not-be-printed",
    "DATABASE_URL": "postgresql://user:db-password-should-not-be-printed@localhost:5432/x",
}

_REQUIRED = {
    "LLM_BASE_URL": "https://integrate.api.nvidia.com/v1",
    "LLM_MODEL": "main-model",
    "LLM_API_KEY": _SECRETS["LLM_API_KEY"],
    "TAVILY_API_KEY": _SECRETS["TAVILY_API_KEY"],
}


def _env(**overrides: str) -> Mapping[str, str]:
    """A minimally valid environment, plus whatever the test is varying."""
    return {**_REQUIRED, **overrides}


@pytest.mark.parametrize("name", sorted(_REQUIRED))
def test_a_missing_required_variable_fails_and_names_itself(name: str) -> None:
    env = {key: value for key, value in _REQUIRED.items() if key != name}

    with pytest.raises(ValueError, match=name):
        load_config(env)


def test_an_empty_string_counts_as_unset_for_a_required_variable() -> None:
    with pytest.raises(ValueError, match="LLM_MODEL"):
        load_config(_env(LLM_MODEL="   "))


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [
        ("llm_rpm_limit", 40),
        ("llm_main_timeout_s", 60.0),  # guidelines §17's main tier stays the default
        ("max_job_runtime", 1200),  # guidelines §17's whole-job row; configured, not enforced
        ("redis_url", "redis://localhost:6379/0"),
        ("aws_region", "ap-south-1"),
        ("langsmith_tracing", False),
        ("langsmith_project", "competitive-research"),
        ("max_revisions", 2),
        ("max_supervisor_hops", 24),
        ("max_llm_calls_per_job", 60),
        ("max_reviewer_edits", 3),
        ("reflection_pass_threshold", 3.5),
        ("max_fetch_bytes", 2_097_152),
        ("max_page_chars", 24_000),
        ("researcher_concurrency", 3),  # ADR 0002
        ("retention_days", 365),
        ("app_env", "local"),
        ("log_level", "INFO"),
    ],
)
def test_defaults_match_the_documented_table(attribute: str, expected: object) -> None:
    assert getattr(load_config(_env()), attribute) == expected


@pytest.mark.parametrize(
    "attribute", ["database_url", "sqs_queue_url", "s3_bucket", "auth_keys_secret_id"]
)
def test_phase_gated_variables_are_none_until_their_phase(attribute: str) -> None:
    # Postgres and the auth secret arrive in Phase 2, SQS and S3 in Phase 3. Requiring
    # any of them now would make a Phase 1 job impossible to start.
    assert getattr(load_config(_env()), attribute) is None


def test_phase_one_needs_no_database() -> None:
    # Phase 1 is one process with in-memory state: no checkpointer tables, no audit rows,
    # nothing to connect to. Demanding a connection string would force a plausible-looking
    # fake into every developer's .env, which is worse than no value at all.
    config = load_config(_env())

    assert config.database_url is None
    assert config.llm_model == "main-model"


def test_a_database_url_is_kept_when_it_is_set() -> None:
    # Phase 2 onwards. The Phase 2 database layer is what turns this into a hard
    # requirement at its point of use; nothing in Phase 1 reads it.
    config = load_config(_env(DATABASE_URL="postgresql://localhost:5432/x"))

    assert config.database_url == "postgresql://localhost:5432/x"


def test_fast_model_falls_back_to_the_main_model() -> None:
    # The two-tier split trims cost at the edges; a missing fast model must not stop a job.
    assert load_config(_env()).llm_fast_model == "main-model"


def test_fast_model_is_used_when_set() -> None:
    assert load_config(_env(LLM_FAST_MODEL="cheap-model")).llm_fast_model == "cheap-model"


def test_tracing_without_a_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="LANGSMITH_API_KEY"):
        load_config(_env(LANGSMITH_TRACING="true"))


def test_tracing_with_a_key_is_accepted() -> None:
    config = load_config(_env(LANGSMITH_TRACING="true", LANGSMITH_API_KEY="ls-key"))

    assert config.langsmith_tracing is True


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("TRUE", True), ("off", False)])
def test_booleans_accept_the_usual_spellings(raw: str, expected: bool) -> None:
    config = load_config(_env(LANGSMITH_TRACING=raw, LANGSMITH_API_KEY="ls-key"))

    assert config.langsmith_tracing is expected


def test_an_unrecognised_boolean_is_an_error_not_a_silent_false() -> None:
    # "maybe" defaulting to False would silently disable tracing for a whole environment.
    with pytest.raises(ValueError, match="LANGSMITH_TRACING"):
        load_config(_env(LANGSMITH_TRACING="maybe"))


def test_a_non_numeric_limit_is_rejected_by_name() -> None:
    with pytest.raises(ValueError, match="MAX_REVISIONS"):
        load_config(_env(MAX_REVISIONS="two"))


def test_an_unknown_app_env_is_rejected() -> None:
    with pytest.raises(ValueError, match="APP_ENV"):
        load_config(_env(APP_ENV="staging"))


# --- RESEARCHER_CONCURRENCY (ADR 0002) ----------------------------------------------


def test_the_concurrency_ceiling_is_the_researchers_own_call_cap() -> None:
    # config must not import an agent, so MAX_RESEARCHER_CONCURRENCY restates the number
    # rather than reading it. This is the test that stops the two drifting apart: a change
    # to the Researcher's per-subtopic cap has to be made in both places or fail here.
    assert MAX_RESEARCHER_CONCURRENCY == MAX_LLM_CALLS_PER_SUBTOPIC


def test_concurrency_can_be_turned_down_to_one() -> None:
    # The setting a suspected concurrency problem is diagnosed with, without a code change.
    assert load_config(_env(RESEARCHER_CONCURRENCY="1")).researcher_concurrency == 1


@pytest.mark.parametrize("raw", ["0", "-1", "4", "10"])
def test_a_concurrency_outside_the_bounds_is_refused_at_startup(raw: str) -> None:
    # Refused rather than clamped. Above the cap means whoever set it expected parallelism a
    # subtopic has no work for; below 1 means no extraction runs at all. Clamping either
    # would run a job that behaves differently from the one that was configured, and until
    # the shared Redis limiter exists this number is all that bounds one job's request rate.
    with pytest.raises(ValueError, match="RESEARCHER_CONCURRENCY"):
        load_config(_env(RESEARCHER_CONCURRENCY=raw))


def test_a_non_numeric_concurrency_is_rejected_by_name() -> None:
    with pytest.raises(ValueError, match="RESEARCHER_CONCURRENCY"):
        load_config(_env(RESEARCHER_CONCURRENCY="three"))


# --- .env, for local development only ----------------------------------------------


@pytest.fixture
def clean_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every variable this project reads, and restore them afterwards.

    load_dotenv writes straight into os.environ, so without this a test that loads a
    .env file would leak its values into the tests that run after it.
    """
    names = [*_REQUIRED, *_SECRETS, "LLM_FAST_MODEL", "LLM_RPM_LIMIT", "APP_ENV", "LOG_LEVEL"]
    for name in names:
        monkeypatch.delenv(name, raising=False)


def _write_dotenv(directory: Path, **values: str) -> None:
    directory.joinpath(".env").write_text(
        "\n".join(f"{name}={value}" for name, value in values.items()), encoding="utf-8"
    )


def test_dotenv_is_read_when_no_mapping_is_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_environ: None
) -> None:
    _write_dotenv(tmp_path, **_REQUIRED)
    monkeypatch.chdir(tmp_path)

    assert load_config().llm_model == "main-model"


def test_the_real_environment_beats_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_environ: None
) -> None:
    # This is what makes the same code path correct in a container, where the task
    # definition is the only source and a stray .env must not override it.
    _write_dotenv(tmp_path, **_REQUIRED)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_MODEL", "from-the-real-environment")

    assert load_config().llm_model == "from-the-real-environment"


def test_a_missing_dotenv_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_environ: None
) -> None:
    # Production has no .env at all. The absence must surface as the normal missing
    # variable message, not as a file error.
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="LLM_MODEL is required"):
        load_config()


def test_passing_a_mapping_never_touches_the_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_environ: None
) -> None:
    # Every other test in this file relies on this: an explicit mapping is the whole
    # source, so .env can neither fill a gap nor override a value.
    _write_dotenv(tmp_path, LLM_MODEL="from-the-dotenv")
    monkeypatch.chdir(tmp_path)

    assert load_config(_env()).llm_model == "main-model"
    assert "LLM_MODEL" not in os.environ


def test_secrets_are_not_in_the_repr() -> None:
    # Printing a Config at startup is a normal thing to do, and it must not leak. The
    # database URL is included because a connection string carries a password.
    printed = repr(load_config(_env(**_SECRETS)))

    for secret in _SECRETS.values():
        assert secret not in printed
    assert "main-model" in printed  # non-secret values are still visible


AUTOMATIC_WORKFLOW_HOPS = 20
"""guidelines §5's derivation for the automatic workflow: 5 subtopics, 2 revisions."""


def test_the_reviewer_edit_bound_leaves_the_hop_guard_room() -> None:
    """20 automatic hops + 3 edit hops = 23, under the guard's 24 (ADR 0006 decision 6).

    Asserted rather than believed, because the arithmetic is what says the guard must not be
    lowered to 20 when the edit path is bounded: three permitted edits are three legitimate
    hops, and a guard at 20 would stop a job that used them.
    """
    config = load_config(_env())

    assert AUTOMATIC_WORKFLOW_HOPS + config.max_reviewer_edits < config.max_supervisor_hops
