"""
WHY THIS FILE EXISTS
    One place that turns environment variables into a validated Config object, so that
    every limit in the system - the three loop guards, the content caps, the rate limit -
    is read from here instead of being hard-coded at its point of use. The variables and
    their defaults are CLAUDE.md's environment table; this file adds nothing to it. It is
    deliberately not a configuration framework: a frozen dataclass and a function that
    fills it in are enough, and a missing required variable fails loudly at startup rather
    than surfacing as a confusing error later.

    For local development it reads `.env` first, because otherwise every command needs the
    whole variable table set by hand. That file never wins against a variable that is
    already set, so the same code path is correct in a container where there is no `.env`
    at all and the environment is the only source.

WHO CALLS IT
    The API entrypoint and the worker entrypoint, once each at startup, which then pass
    the Config down. Nothing imports a module-level singleton, so tests can build a Config
    from a plain dict without touching os.environ.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from dotenv import load_dotenv

AppEnv = Literal["local", "dev", "prod"]

_DOTENV_PATH = Path(".env")
"""Relative to the working directory, not searched for. Every command in CLAUDE.md runs
from the repository root, and a config file found by walking up the tree is a config file
nobody can predict."""

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

MAX_RESEARCHER_CONCURRENCY = 3
"""The ceiling on RESEARCHER_CONCURRENCY, and the reason it has one.

It is `agents.researcher.MAX_LLM_CALLS_PER_SUBTOPIC` - a subtopic never reads more than
that many pages, so a larger pool could only ever hold idle threads while claiming a rate
budget the shared limiter cannot yet see. The number is restated here rather than imported
because config must not depend on an agent, and `test_config.py` asserts the two agree, so
the duplication fails a test rather than drifting.
"""


@dataclass(frozen=True)
class Config:
    """Every environment variable in CLAUDE.md's table, validated once.

    Secrets are marked ``repr=False`` so that logging or printing a Config - which is a
    normal thing to do at startup - cannot leak a credential (guidelines §16).
    """

    # LLM. One OpenAI-compatible client covers development and production, so there is no
    # provider class and no provider abstraction (ARCHITECTURE.md §20 row 4).
    llm_base_url: str | None
    llm_model: str | None
    llm_fast_model: str | None  # routing and reflection scoring only; falls back to llm_model
    llm_api_key: str | None = field(repr=False)
    """The four LLM fields are optional, and that is ADR 0012 decision 4 rather than laxity.

    The API process must start, serve every route and pass its health check with **no LLM or
    Tavily credential in its environment** - that is what makes guidelines §13's least-privilege
    table describe the code rather than an intention. So `load_config()` stops requiring them,
    and the process that needs them - the worker, and the two scripts - refuses to start without
    them instead, with the same loud failure a missing variable has always given.

    The cost is admitted rather than hidden: `mypy --strict` now demands narrowing at every
    consumer, which is a handful of places and is also the point - it names each one that
    assumes an LLM.
    """
    llm_rpm_limit: int  # shared client-side limit; the NIM free tier is ~40 RPM
    llm_main_timeout_s: float
    """How long one main-model request may take, in seconds.

    Configurable because it is the one timeout an endpoint's generation speed can invalidate.
    guidelines §17 sets 60s, and that stays the default: the Synthesizer is the only agent
    whose output is large, and 60s is ample against a production API. The NIM development
    tier generates at roughly 15-20 tokens/second, which turns 60s into a cap of about a
    thousand output tokens - under a full report, which is what failed step 12's third smoke
    job three times over.

    So a slow development endpoint raises it in its own environment, and the production
    number stays where every reader of guidelines §17 expects to find it. The fast tier is
    deliberately not configurable: the Supervisor and the reflection node send counters and
    receive a handful of tokens, so no plausible generation speed makes 30s tight.
    """

    # External research tool. Optional for the same reason the four above are: the API never
    # fetches a page, so it never needs this credential (ADR 0012 decision 4).
    tavily_api_key: str | None = field(repr=False)

    # Data stores. DATABASE_URL is required from Phase 2, when the Postgres checkpointer
    # and the audit tables arrive; Phase 1 runs a single process with in-memory state and
    # no database at all. It carries a password, so it is a secret too.
    database_url: str | None = field(repr=False)
    redis_url: str

    # AWS. Required from Phase 3; None until then.
    sqs_queue_url: str | None
    s3_bucket: str | None
    aws_region: str

    aws_endpoint_url: str | None
    """Where the AWS APIs live, when they are not AWS.

    None against real AWS, and the LocalStack address locally. It is the only difference
    between the two, which is what makes the local integration tests worth running at all -
    the same client, the same calls, a different address.
    """

    # Observability.
    langsmith_tracing: bool
    langsmith_api_key: str | None = field(repr=False)
    langsmith_project: str

    # Loop guards. Three independent limits, because they fail for different reasons
    # (guidelines §5): oscillation, a reflection loop that never converges, and everything
    # else including one agent burning calls internally.
    max_revisions: int
    max_supervisor_hops: int
    max_llm_calls_per_job: int
    reflection_pass_threshold: float

    max_reviewer_edits: int
    """How many times one reviewer may send a job back for an edit (ADR 0006).

    A reviewer edit is human-triggered, so `MAX_REVISIONS` does not bound it and nothing else
    did until this existed. Each edit costs 3 logical calls and exactly 1 hop, which is what
    makes 3 a derived number rather than a guess: it puts the hop ceiling at 20 + 3 = 23,
    under `MAX_SUPERVISOR_HOPS` = 24, and the call ceiling at 88, where the 60-call budget
    binds first (guidelines §5, §13).

    Enforced before the graph is resumed - `graph.build.refuse_edit()` - so a refused edit
    spends nothing at all.
    """

    researcher_concurrency: int
    """How many of one subtopic's page extractions may be in flight at once (ADR 0002).

    The Researcher reads at most `MAX_LLM_CALLS_PER_SUBTOPIC` pages per subtopic and the
    extractions are independent of each other, so running them together turns a subtopic's
    LLM time from the sum of three calls into the longest of them. Search, deduplication,
    and fetching stay sequential; only the extraction calls overlap.

    1 restores the previous strictly-sequential behaviour and is the setting to reach for
    when diagnosing anything that looks like a concurrency problem.

    **Rate-limit implication.** This multiplies a single job's request rate by at most this
    number. The measured baseline was 1.8 requests/minute against `LLM_RPM_LIMIT` = 40, so 3
    leaves the development tier with room; a larger value or a second concurrent job does
    not, and until the shared Redis limiter exists (guidelines §11) nothing but this number
    bounds how many requests one job has open at once.
    """

    max_job_runtime: int
    """How long one job may run, in seconds. guidelines §17's whole-job row.

    **Configured, not enforced.** Nothing in the graph reads this yet: the whole-job timeout
    arrives with the worker in Phase 3, which is the first component that owns a job's
    lifetime. It is here so the number has one home and the measurement harness can say when
    a run went over it - not so that anything currently stops at it. A value the code claims
    to honour and does not is worse than no value (guidelines §17).
    """

    # Content caps, enforced at the tool boundary before any byte reaches a prompt.
    max_fetch_bytes: int
    max_page_chars: int

    # Auth. Required from Phase 2; None until then.
    auth_keys_secret_id: str | None

    auth_keys: str | None = field(repr=False)
    """The API keys, hashed, as JSON: `{"<sha256 of the key>": {"user_id", "role"}}`.

    guidelines §16 keeps secrets in environment variables locally and in Secrets Manager in
    AWS, so this is the local half of one thing: the same JSON that `AUTH_KEYS_SECRET_ID`
    names in Phase 5, read from the environment until there is a Secrets Manager to read it
    from. It holds **hashes**, never keys, so a leaked environment does not hand anyone a
    working credential.

    `routes/auth.py` parses and validates it once, at startup, so a malformed value fails
    the process rather than the first request.
    """

    # Operations.
    retention_days: int
    app_env: AppEnv
    log_level: str


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from ``env``, defaulting to the process environment.

    When no mapping is passed, `.env` is read first. It never overrides a variable that is
    already set, so a container's real environment always wins over a stray file, and a
    caller that passes its own mapping - every test does - never touches the filesystem.

    Raises ValueError, naming the variable, when a required value is missing or a value
    cannot be parsed. Called once at startup; the failure is meant to be fatal.
    """
    if env is None:
        load_dotenv(_DOTENV_PATH)
    source = os.environ if env is None else env

    llm_model = _optional(source, "LLM_MODEL")
    langsmith_tracing = _bool(source, "LANGSMITH_TRACING", default=False)
    langsmith_api_key = _optional(source, "LANGSMITH_API_KEY")
    if langsmith_tracing and langsmith_api_key is None:
        raise ValueError("LANGSMITH_API_KEY is required when LANGSMITH_TRACING is true")

    return Config(
        llm_base_url=_optional(source, "LLM_BASE_URL"),
        llm_model=llm_model,
        # CLAUDE.md: falls back to LLM_MODEL. The two-tier split is an optimisation, so a
        # missing fast model must not stop the job.
        llm_fast_model=_optional(source, "LLM_FAST_MODEL") or llm_model,
        llm_api_key=_optional(source, "LLM_API_KEY"),
        llm_rpm_limit=_int(source, "LLM_RPM_LIMIT", default=40),
        # guidelines §17's main-tier row. Overridden only where the endpoint is slow.
        llm_main_timeout_s=_float(source, "LLM_MAIN_TIMEOUT_S", default=60.0),
        tavily_api_key=_optional(source, "TAVILY_API_KEY"),
        database_url=_optional(source, "DATABASE_URL"),
        redis_url=_optional(source, "REDIS_URL") or "redis://localhost:6379/0",
        sqs_queue_url=_optional(source, "SQS_QUEUE_URL"),
        s3_bucket=_optional(source, "S3_BUCKET"),
        aws_region=_optional(source, "AWS_REGION") or "ap-south-1",
        aws_endpoint_url=_optional(source, "AWS_ENDPOINT_URL"),
        langsmith_tracing=langsmith_tracing,
        langsmith_api_key=langsmith_api_key,
        langsmith_project=_optional(source, "LANGSMITH_PROJECT") or "competitive-research",
        max_revisions=_int(source, "MAX_REVISIONS", default=2),
        # 20 is the automatic workflow's derived maximum (guidelines §5) and MAX_REVIEWER_EDITS
        # = 3 makes three more hops legitimate, so the ceiling is 23 and 24 is that plus one.
        # It is ordinary headroom above a derived ceiling, not margin for an unbounded path -
        # ADR 0006 decision 6 bounded the edits and withdrew the instruction to reduce this
        # to 20, which would kill a job that used its three permitted edits.
        max_supervisor_hops=_int(source, "MAX_SUPERVISOR_HOPS", default=24),
        max_llm_calls_per_job=_int(source, "MAX_LLM_CALLS_PER_JOB", default=60),
        reflection_pass_threshold=_float(source, "REFLECTION_PASS_THRESHOLD", default=3.5),
        # ADR 0006's bound on the reviewer-edit path. 3 keeps the hop ceiling at 23.
        max_reviewer_edits=_int(source, "MAX_REVIEWER_EDITS", default=3),
        researcher_concurrency=_researcher_concurrency(source),
        # guidelines §17's whole-job row, 20 minutes. Configured only - see the field.
        max_job_runtime=_int(source, "MAX_JOB_RUNTIME", default=1200),
        max_fetch_bytes=_int(source, "MAX_FETCH_BYTES", default=2_097_152),
        max_page_chars=_int(source, "MAX_PAGE_CHARS", default=24_000),
        auth_keys_secret_id=_optional(source, "AUTH_KEYS_SECRET_ID"),
        auth_keys=_optional(source, "AUTH_KEYS"),
        retention_days=_int(source, "RETENTION_DAYS", default=365),
        app_env=_app_env(source),
        log_level=_optional(source, "LOG_LEVEL") or "INFO",
    )


def required(value: str | None, name: str) -> str:
    """A configured value the caller cannot run without, narrowed loudly.

    The four LLM fields and `tavily_api_key` are optional on `Config` so the API can start
    without them (ADR 0012 decision 4). The processes that *do* need them - the worker, the
    preflight, the measurement harness - call this at startup, which keeps the failure exactly
    where it has always been: fatal, at startup, naming the variable.

    It replaces the private `_required` that read the environment directly. Nothing reads a
    required environment variable any more; what is required now depends on which process is
    asking, and only that process knows.
    """
    if not value:
        raise ValueError(f"{name} is required and is not set")
    return value


def _optional(env: Mapping[str, str], name: str) -> str | None:
    """An unset variable and one set to the empty string mean the same thing here."""
    return env.get(name, "").strip() or None


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    """An unrecognised value is an error, not a silent False."""
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _researcher_concurrency(env: Mapping[str, str]) -> int:
    """ADR 0002's pool size, refused loudly rather than clamped quietly.

    A value over the ceiling means whoever set it expected more parallelism than a subtopic
    has work for, and a value under 1 means no extraction would run at all. Both are
    mistakes worth failing at startup for, which is cheaper than reading the number back off
    a run that behaved differently from the one that was configured.
    """
    value = _int(env, "RESEARCHER_CONCURRENCY", default=MAX_RESEARCHER_CONCURRENCY)
    if not 1 <= value <= MAX_RESEARCHER_CONCURRENCY:
        raise ValueError(
            f"RESEARCHER_CONCURRENCY must be between 1 and "
            f"{MAX_RESEARCHER_CONCURRENCY}, got {value}"
        )
    return value


def _app_env(env: Mapping[str, str]) -> AppEnv:
    value = env.get("APP_ENV", "").strip() or "local"
    if value not in ("local", "dev", "prod"):
        raise ValueError(f"APP_ENV must be local, dev, or prod, got {value!r}")
    return cast(AppEnv, value)
