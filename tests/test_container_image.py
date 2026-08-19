"""
WHY THIS FILE EXISTS
    `Dockerfile`, `.dockerignore` and the `app` profile in `docker-compose.yml` are three
    files that say what the two processes run as. Almost nothing in them fails loudly: an
    image that copies a `.env` still starts, an API container handed an LLM key still serves
    every route, and a worker with a published port still consumes its queue. The failures are
    silent, so they are asserted here.

    Four claims are worth a test rather than a sentence:

    **One image, two entrypoints** (ARCHITECTURE.md §19, decision 25). `api` and `worker` must
    resolve to the same build and the same tag. Two images would be a design change, and it
    would arrive as a one-line edit.

    **The API holds no LLM credential** (docs/adr/0012-*.md). The image unavoidably carries
    agent code the API never executes; what makes that acceptable is that the process cannot
    call a model, and the environment is where that is decided.

    **Nothing in the image is a secret.** `.dockerignore` keeps `.env` out of the build
    context, the Dockerfile names its sources rather than copying `.`, and the worker's
    provider variables are passed through rather than defaulted - a layer is not somewhere a
    credential can be taken back out of.

    **A container reaches a service by its Compose name.** `localhost` inside a container is
    the container, so a host-only URL in a container's environment produces a connection
    refused at the first query rather than at startup.

    **Nothing here starts a container or opens a socket** - these are file contents compared
    against each other, the same rule `test_local_infrastructure.py` follows. What needs a
    running stack is in `test_container_runtime.py`.

WHO CALLS IT
    pytest, as part of the offline suite.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

_ROOT = Path(__file__).resolve().parent.parent

_DOCKERFILE = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
_DOCKERIGNORE = (_ROOT / ".dockerignore").read_text(encoding="utf-8")

APPLICATION_SERVICES = ("migrate", "api", "worker")
"""The three services the `app` profile adds. All three run the one image."""

INFRASTRUCTURE_SERVICES = ("postgres", "redis", "localstack")
"""The three that were already here, and that `docker compose up -d --wait` still starts on
its own - which is what the `postgres`, `integration` and `redis` test layers run against."""

NOT_IN_THE_IMAGE = frozenset({"tests", "eval"})
"""Top-level importable names the image deliberately does not carry. `tests` is not runtime
code, and a production process may not import the harnesses or the recorded web responses.

`eval` is out for the same two reasons: no production process runs an evaluation, and the
benchmark fixtures are report bodies and page quotes that have no business in a deployed
layer. It is an offline harness, like `scripts/measure_jobs.py` next to it."""

PROVIDER_VARIABLES = (
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_FAST_MODEL",
    "LLM_API_KEY",
    "TAVILY_API_KEY",
)
"""What only the worker may have, and what no file in this repository may give a value to."""


def _compose() -> dict[str, Any]:
    """docker-compose.yml as a document.

    Parsed rather than pattern-matched because what is under test is structure - which
    services share a build, which publishes a port, which URL names which host - and because
    the shared build arrives through a YAML merge key that only exists once the document has
    been read. The untyped surface stops here: everything after this is a plain mapping.
    """
    text = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    return cast(dict[str, Any], yaml.safe_load(text))


def _service(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], _compose()["services"][name])


def _environment(name: str) -> dict[str, str | None]:
    return cast(dict[str, "str | None"], _service(name).get("environment", {}))


def _instructions() -> str:
    """The Dockerfile with its line continuations joined, so a COPY spanning two lines is one
    instruction. Comments go first, because a `#` inside a continued instruction is stripped
    by the builder too."""
    without_comments = re.sub(r"^\s*#.*$", "", _DOCKERFILE, flags=re.MULTILINE)
    return re.sub(r"\\\s*\n", " ", without_comments)


def _copy_sources() -> list[list[str]]:
    """The source arguments of every COPY, one list per instruction.

    A COPY is `COPY <source>... <destination>`, so the last argument is dropped, and so are
    the `--from=` and `--chown=` flags: what is being asked is which repository paths reach
    the image.
    """
    sources: list[list[str]] = []
    for instruction in re.findall(r"^COPY\s+(.+?)$", _instructions(), re.MULTILINE):
        arguments = [word for word in instruction.split() if not word.startswith("--")]
        if len(arguments) >= 2:
            sources.append(arguments[:-1])
    return sources


def _copied_names() -> set[str]:
    """The first path segment of every copied source: `graph/` and `scripts/reexport_job.py`
    both answer for their top-level name, because a package is copied whole."""
    return {source.strip("./").split("/")[0] for group in _copy_sources() for source in group}


def _importable_names() -> set[str]:
    """Every top-level name a module in this repository could import: the flat `*.py` modules
    and the package directories beside them."""
    modules = {path.stem for path in _ROOT.glob("*.py")}
    packages = {path.parent.name for path in _ROOT.glob("*/__init__.py")}
    return modules | packages


def _dockerignore_patterns() -> set[str]:
    lines = _DOCKERIGNORE.splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


# --- The image ------------------------------------------------------------------------


def test_there_is_a_dockerfile_at_the_repository_root() -> None:
    """The build context is the repository root, because the repository root is the import
    root: `uvicorn app:app` and `python -m worker` both resolve their imports from it."""
    assert (_ROOT / "Dockerfile").is_file()
    assert (_ROOT / ".dockerignore").is_file()


def test_one_image_carries_both_entrypoints() -> None:
    """ARCHITECTURE.md decision 25. The two commands are what differ; the contents do not.

    Checked as "both source files are copied" rather than as "the image works", because the
    latter needs a daemon - `test_container_runtime.py` imports both inside the built image.
    """
    copied = _copied_names()

    assert "app.py" in copied
    assert "worker.py" in copied


def test_the_image_carries_every_module_the_two_processes_can_import() -> None:
    """The failure this catches: a new top-level module lands, the Dockerfile is not touched,
    and the import error appears when a container starts rather than when the build runs.

    It is deliberately name-level rather than file-level - a package is copied whole - so the
    question it answers is "does the image know about this module at all?"
    """
    copied = {name.removesuffix(".py") for name in _copied_names()}

    missing = _importable_names() - copied - NOT_IN_THE_IMAGE

    assert not missing, f"in the repository but not copied into the image: {sorted(missing)}"


def test_the_image_carries_what_a_migration_needs() -> None:
    """`alembic upgrade head` is one of the three commands (guidelines §19), and it reads
    `alembic.ini` from the working directory and its revisions from `database/migrations/`."""
    assert "alembic.ini" in _copied_names()
    assert (_ROOT / "database" / "migrations").is_dir()


def test_the_image_leaves_the_development_scripts_out() -> None:
    """`scripts/` is copied file by file rather than whole, so the two development tools stay
    out: `check_model.py` is a preflight against a real endpoint and `measure_jobs.py` runs
    real jobs and writes `measurements/`. What is copied is ADR 0009's operator recovery,
    which re-projects a stored report and can reach nothing that could re-bill a model."""
    from_scripts = {source for group in _copy_sources() for source in group}

    assert "scripts/reexport_job.py" in from_scripts
    assert "scripts/check_model.py" not in from_scripts
    assert "scripts/measure_jobs.py" not in from_scripts


def test_the_dockerfile_names_its_sources_rather_than_copying_the_context() -> None:
    """`COPY . .` is how a `.env`, a measurement file or a personal document reaches a layer.

    `.dockerignore` is the other half of this and is checked below; both exist because either
    on its own is one edit away from being wrong.
    """
    for group in _copy_sources():
        assert "." not in group, group


def test_nothing_in_the_image_is_a_secret() -> None:
    """No ARG and no ENV carrying a credential. Every value the two processes need arrives as
    an environment variable at run time, which is also what lets one image serve local Compose
    and a deployment without being rebuilt."""
    for declared in re.findall(r"^(?:ARG|ENV)\s+(.+?)$", _instructions(), re.MULTILINE):
        for name in re.findall(r"([A-Z][A-Z0-9_]*)=", declared):
            secretish = ("KEY", "SECRET", "TOKEN", "PASSWORD")
            assert not any(word in name for word in secretish), name

    assert ".env" not in _copied_names()


def test_the_runtime_user_is_not_root() -> None:
    """A process that can rewrite its own source is one an injected page could aim at, and the
    two things this container does with untrusted input - fetching pages, and scoring text
    derived from them - are why that matters here rather than in general."""
    users = re.findall(r"^USER\s+(\S+)\s*$", _instructions(), re.MULTILINE)

    assert users, "the Dockerfile never switches away from root"
    assert users[-1] not in {"root", "0"}


def test_the_build_is_staged_so_the_runtime_holds_no_package_manager() -> None:
    """uv resolves the lockfile in the first stage and is left there. What reaches the runtime
    is /opt/venv and the application source."""
    stages = re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)\s*$", _instructions(), re.MULTILINE)

    assert len(stages) >= 2, "the build is single-stage, so uv would ship with it"
    assert re.search(r"^COPY --from=\S+ /opt/venv /opt/venv$", _instructions(), re.MULTILINE)


def test_the_dependencies_come_from_the_lockfile_and_not_from_a_resolution() -> None:
    """`--frozen` is what makes the build reproducible: the lockfile pins every transitive
    version, and a build that would have to update it fails instead of drifting. `--no-dev`
    is what keeps pytest, ruff and mypy out of a process that serves HTTP."""
    install = re.search(r"^RUN uv sync (.+?)$", _instructions(), re.MULTILINE)

    assert install is not None, "the image does not install its dependencies with uv"
    assert "--frozen" in install.group(1)
    assert "--no-dev" in install.group(1)


def test_the_base_image_names_the_python_this_project_requires() -> None:
    """pyproject.toml requires >= 3.13. A base image that drifted below it would fail on
    syntax rather than on a version check, somewhere unhelpful."""
    assert re.search(r"^FROM python:3\.13-slim AS ", _instructions(), re.MULTILINE)


# --- The build context ----------------------------------------------------------------


def test_the_build_context_excludes_secrets_and_developer_files() -> None:
    """The Dockerfile copies named paths, so this list is the second lock rather than the only
    one - but a `.env` inside the context is one careless edit away from being a layer, and an
    image layer is not a place a credential can be taken back out of."""
    excluded = _dockerignore_patterns()

    for pattern in (".git", ".venv", "__pycache__", "tests", "measurements", ".env", ".vscode"):
        assert pattern in excluded, pattern


def test_the_build_context_excludes_the_local_instruction_file() -> None:
    """FINDING 5. `CLAUDE.md` is gitignored and local, and a build context is uploaded whole
    to the daemon - which on a remote or shared builder is another machine entirely."""
    assert "CLAUDE.md" in _dockerignore_patterns()


def test_the_build_context_excludes_the_tool_caches() -> None:
    """They are large, they are machine-specific, and `.mypy_cache` holds a copy of the source
    it analysed - so excluding them is both the size argument and the leak argument."""
    excluded = _dockerignore_patterns()

    for cache in (".mypy_cache", ".pytest_cache", ".ruff_cache"):
        assert cache in excluded, cache


def test_the_build_context_excludes_the_private_working_documents() -> None:
    """`docs/engineering-guidelines.md` and `docs/interview-prep.md` are gitignored personal
    documents (CLAUDE.md). An image is a worse place to leak them from than a repository."""
    assert "docs" in _dockerignore_patterns()


# --- The Compose services -------------------------------------------------------------


def test_the_application_services_share_one_image_and_one_build() -> None:
    """ARCHITECTURE.md decision 25, at the point where it would be broken: three services, one
    build context, one tag."""
    builds = {name: _service(name).get("build") for name in APPLICATION_SERVICES}
    images = {name: _service(name).get("image") for name in APPLICATION_SERVICES}

    assert len(set(images.values())) == 1, images
    assert all(build == {"context": "."} for build in builds.values()), builds


def test_the_three_services_differ_only_in_their_command() -> None:
    """Which is the whole claim behind one image: the API serves HTTP, the worker consumes the
    queue, and the migration runs Alembic - from identical contents."""
    assert _service("api")["command"][:2] == ["uvicorn", "app:app"]
    assert _service("worker")["command"] == ["python", "-m", "worker"]
    assert _service("migrate")["command"] == ["alembic", "upgrade", "head"]


def test_only_the_api_publishes_a_port() -> None:
    """The worker serves nothing and makes no authorization decision (ARCHITECTURE.md §19), so
    a published port on it would be an opening with nothing behind it."""
    published = _service("api")["ports"]

    assert len(published) == 1
    assert published[0].endswith(":8000")
    for name in ("worker", "migrate"):
        assert "ports" not in _service(name), name


def test_the_api_container_holds_no_llm_or_search_credential() -> None:
    """docs/adr/0012-*.md, where it is actually decided.

    The image carries agent code the API never executes, and what makes that acceptable is
    that the process could not call a model if it tried. This is that claim as configuration
    rather than as intent.
    """
    for variable in PROVIDER_VARIABLES:
        assert variable not in _environment("api"), variable


def test_the_worker_gets_its_provider_credentials_from_the_environment() -> None:
    """Declared so they are passed through, and given no value, so this file never holds one.

    Unset, `python -m worker` refuses to start and names the first one it is missing, which is
    the behaviour that should be visible - rather than a placeholder that starts a worker able
    to take a job it cannot run.
    """
    environment = _environment("worker")

    for variable in PROVIDER_VARIABLES:
        assert variable in environment, variable
        assert environment[variable] is None, f"{variable} has a value in docker-compose.yml"


CLIENT_FACING_VARIABLES = frozenset({"S3_PUBLIC_ENDPOINT_URL"})
"""The one exception to the rule below, and it is an exception on purpose.

`S3_PUBLIC_ENDPOINT_URL` is not an address this process calls - it is the address the API
*signs into a presigned URL* for a client outside the Compose network. SigV4 covers the host,
so it has to be the client's address before the signature exists rather than rewritten in
afterwards, and locally the client is a browser on the developer's machine.
"""


def test_no_application_service_addresses_a_store_on_localhost() -> None:
    """`localhost` inside a container is the container. A host-only URL here would produce a
    connection refused at the first query, which is a slower way to learn it than this."""
    for name in APPLICATION_SERVICES:
        for variable, value in _environment(name).items():
            if value is None or variable in CLIENT_FACING_VARIABLES:
                continue
            assert "localhost" not in value, f"{name}.{variable}"
            assert "127.0.0.1" not in value, f"{name}.{variable}"


def test_the_api_signs_report_urls_for_a_client_outside_the_network() -> None:
    """FINDING 1, and the failure it prevents.

    The API's artifact store used to be built with `AWS_ENDPOINT_URL`, so `GET
    /jobs/{id}/report` handed a browser `http://localstack:4566/...` - a hostname that resolves
    on the Compose network and nowhere else. The reviewer downloading a report is by
    definition outside that network.

    It cannot be fixed after signing: SigV4 covers the host, so rewriting the URL invalidates
    it. The address has to be right when the signature is made, which is what this separate
    variable is for. **Only the API gets it** - the worker's `PutObject` is a real call from
    inside the network and must keep using the internal address.
    """
    assert "localhost" in str(_environment("api")["S3_PUBLIC_ENDPOINT_URL"])
    assert _environment("api")["AWS_ENDPOINT_URL"] == "http://localstack:4566"
    assert "S3_PUBLIC_ENDPOINT_URL" not in _environment("worker")
    assert "S3_PUBLIC_ENDPOINT_URL" not in _environment("migrate")


def test_the_local_api_is_published_to_this_machine_only() -> None:
    """FINDING 3. `"8000:8000"` binds every interface, which puts an API holding two published
    development keys on the developer's LAN - a café, a co-working space, a conference network.

    `127.0.0.1:` keeps it reachable from this machine and nowhere else, which is the entire
    local requirement. A deployment publishes nothing this way; the task sits behind a target
    group (ARCHITECTURE.md §18).
    """
    published = _service("api")["ports"][0]

    assert published.startswith("127.0.0.1:"), published


def test_every_internal_url_names_a_compose_service() -> None:
    """PostgreSQL, Redis and LocalStack on the Compose network, on their real ports rather
    than on the host ports the infrastructure services publish."""
    for name in ("api", "worker"):
        environment = _environment(name)

        assert "@postgres:5432/" in str(environment["DATABASE_URL"]), name
        assert str(environment["REDIS_URL"]).startswith("redis://redis:6379/"), name
        assert str(environment["SQS_QUEUE_URL"]).startswith("http://localstack:4566/"), name
        assert environment["AWS_ENDPOINT_URL"] == "http://localstack:4566", name


def test_the_queue_and_bucket_the_application_uses_are_the_ones_localstack_creates() -> None:
    """The names are written twice - once as the init script's input, once as the URL the two
    processes are given - and nothing else would notice them drifting apart until a job was
    submitted into a queue nobody consumes."""
    localstack = _environment("localstack")

    for name in ("api", "worker"):
        queue_url = str(_environment(name)["SQS_QUEUE_URL"])

        assert queue_url.endswith(f"/{localstack['JOBS_QUEUE_NAME']}"), name
        assert _environment(name)["S3_BUCKET"] == localstack["REPORTS_BUCKET"], name


def test_the_application_waits_for_healthy_infrastructure() -> None:
    """Not for a started container: PostgreSQL answers TCP well before it answers a query, and
    LocalStack's own check is for the queue and the bucket rather than for the process."""
    for name in ("api", "worker"):
        depends = _service(name)["depends_on"]

        for service in INFRASTRUCTURE_SERVICES:
            assert depends[service]["condition"] == "service_healthy", f"{name} -> {service}"


def test_migrations_run_once_before_the_application_starts() -> None:
    """guidelines §19's ordering, in the one place that can enforce it locally.

    **Neither process migrates.** Two of them racing `alembic upgrade head` against one
    database is a lock fight at best and a partly applied schema at worst, and in the
    deployment this is a separate task that must exit 0 before the new revision starts.
    """
    for name in ("api", "worker"):
        depends = _service(name)["depends_on"]

        assert depends["migrate"]["condition"] == "service_completed_successfully", name
        assert "alembic" not in " ".join(_service(name)["command"]), name

    assert _service("migrate")["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_the_migration_task_is_given_nothing_but_a_database() -> None:
    """A migration needs no LLM key, no queue, no bucket and no Redis, and giving it none is
    guidelines §13's least-privilege table at the one point where it costs nothing."""
    assert set(_environment("migrate")) == {"DATABASE_URL"}


def test_the_migration_task_does_not_restart() -> None:
    """A migration that failed should stay failed and be read, not retried in a loop against a
    database it has already half-changed."""
    assert _service("migrate")["restart"] == "no"


def test_the_api_reports_its_own_health_from_inside_the_container() -> None:
    """`/health` answers 503 when a dependency is down (ARCHITECTURE.md decision 31), and the
    check has to fail then - an unhealthy task that reports healthy is never replaced."""
    check = " ".join(_service("api")["healthcheck"]["test"])

    assert "/health" in check
    assert "python" in check, "the runtime image has no curl, so the check must use python"


def test_the_worker_gets_fargates_maximum_graceful_stop_opportunity() -> None:
    """ARCHITECTURE.md §19's worker row: SIGTERM stops the next receive and lets the
    invocation in flight return, checkpointing per node as it goes.

    **120s here, and the same number is what §19 requires of the production `stopTimeout`.**
    It is Fargate's maximum opportunity to reach the next checkpoint, not a guarantee that every
    node finishes in that time and deliberately not a twenty-minute job drain.

    Asserted here because the two numbers have to stay one number. It is **mitigation, not a
    fix**: a node still running at 120s is killed exactly as it was at 30s, the message is
    never deleted either way, and the case redelivery cannot cover - a hard kill on the final
    delivery - remains the Phase 5 sweep's (ADR 0010 decision 9).
    """
    assert _service("worker")["stop_grace_period"] == "120s"


def test_the_application_services_are_opt_in_and_the_infrastructure_is_not() -> None:
    """`docker compose up -d --wait` still starts infrastructure and nothing else, because
    that is the command the `postgres`, `integration` and `redis` layers are documented
    against and those layers run from the host. Nothing about them should build an image."""
    for name in APPLICATION_SERVICES:
        assert _service(name)["profiles"] == ["app"], name

    for name in INFRASTRUCTURE_SERVICES:
        assert "profiles" not in _service(name), name
