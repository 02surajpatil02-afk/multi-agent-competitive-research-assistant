"""
WHY THIS FILE EXISTS
    `test_container_image.py` reads three files and compares them to each other. That catches
    a wrong declaration and nothing else - it cannot tell you whether the image starts, whether
    the API can reach PostgreSQL by the name Compose gives it, or whether the worker refuses to
    run without its credentials. Those are properties of a running container, so they are
    checked against one.

    Five things here cannot be learned any other way:

    **The image starts as both processes.** One image, two entrypoints (ARCHITECTURE.md §19) is
    only a claim until `uvicorn app:app` and `python -m worker` have both come up from it.

    **`/health` answers from outside the container.** Which means the port is published, the
    server is bound to 0.0.0.0 rather than to loopback, and both checks it reports are honest -
    `db` needs a query against the Compose PostgreSQL and `redis` needs a ping.

    **The API starts with no LLM credential** (docs/adr/0012-*.md). Checked in the process
    environment of the running container, which is the only place that settles it.

    **The worker refuses to start when a provider variable is missing**, naming it. The
    alternative is a worker that starts, takes a message, and fails a job that has already been
    paid for.

    **The worker really reaches all four stores.** Its "ready" line is written after the queue's
    attributes were read, after Redis answered, and after the checkpointer created its own
    tables - so the line itself is the assertion. S3 is asked separately, because nothing
    contacts it at startup.

    **No model and no search endpoint is ever called.** Nothing here submits a job, and the
    first test refuses to run against a stack holding real credentials.

WHO CALLS IT
    pytest, and only when TEST_CONTAINER_STACK is set - the same opt-in the `postgres`,
    `integration` and `redis` layers use, so plain `pytest` stays offline.

        docker compose --profile app build
        export LLM_BASE_URL=https://llm.invalid/v1 LLM_MODEL=test-model
        export LLM_API_KEY=test TAVILY_API_KEY=test
        docker compose --profile app up -d --wait
        TEST_CONTAINER_STACK=1 pytest -m container

    In PowerShell the variables are set first with `$env:NAME = "..."`.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

pytestmark = pytest.mark.container

_ROOT = Path(__file__).resolve().parent.parent

SKIP_REASON = (
    "TEST_CONTAINER_STACK is not set. Build the image, start the `app` profile with fake "
    "provider credentials, then set it - see this module's header for the three commands."
)

UNREACHABLE_LLM_HOST = ".invalid"
"""RFC 2606 reserves `.invalid`, so a base URL ending in it cannot resolve. The smoke stack is
started with one deliberately: these tests must never be able to reach a real model, and a
check is worth more than a promise when the cost of being wrong is billed usage."""

FAKE_PROVIDER_CREDENTIALS = {
    "LLM_BASE_URL": f"https://llm{UNREACHABLE_LLM_HOST}/v1",
    "LLM_MODEL": "test-model",
    "LLM_API_KEY": "test",
    "TAVILY_API_KEY": "test",
}
"""What a worker this file starts itself is given. Spelled out rather than inherited, because
`docker compose run` resolves the worker's pass-through variables from `.env` - which on a
developer's machine is exactly where the real credentials are."""

REPORT_URL_TTL_S = 900
"""artifacts.PRESIGNED_URL_TTL_S, restated because what is under test is the URL the container
signs rather than the constant this process can import."""

PROBE_JOB_ID = "00000000-0000-4000-8000-00000000c0de"
"""The job id the presign case writes an object under. A real UUID, so the key is shaped like
every other one, and a fixed one, so repeated runs overwrite rather than accumulate - which is
the same property ADR 0009's re-export relies on."""


def _compose(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """One `docker compose` invocation against this repository's stack."""
    return subprocess.run(
        ["docker", "compose", "--profile", "app", *arguments],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=check,
        timeout=180,
    )


def _in_container(service: str, *command: str) -> str:
    """Run a command in a service that is already up, and answer its stdout."""
    return _compose("exec", "-T", service, *command).stdout


def _one_off(
    service: str, *command: str, environment: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run a command in a **new** container built from the same service definition.

    `--no-deps` because the stack is already up: without it Compose would re-run the migration
    every time one of these is called. The exit status is returned rather than raised on,
    because two of these cases are about a process refusing to start.
    """
    overrides = [f"--env={name}={value}" for name, value in (environment or {}).items()]
    finished = _compose("run", "--rm", "--no-deps", *overrides, service, *command, check=False)
    return finished.returncode, finished.stdout + finished.stderr


def _docker(*arguments: str) -> str:
    """One `docker` invocation against a container this file started itself. Compose has no
    verb for "stop this one detached container and tell me how it exited"."""
    finished = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, check=True, timeout=180
    )
    return finished.stdout.strip()


def _logs(container: str) -> str:
    """Both of a container's streams. `docker logs` replays stdout on stdout and stderr on
    stderr, and every line either process writes - uvicorn's and `logging`'s alike - is on the
    second one, so reading only the first reads nothing."""
    finished = subprocess.run(
        ["docker", "logs", container], capture_output=True, text=True, check=True, timeout=60
    )
    return finished.stdout + finished.stderr


@contextmanager
def _detached(service: str, environment: dict[str, str]) -> Iterator[str]:
    """A container of this service's own, started detached and removed afterwards.

    A throwaway rather than the one the stack is running, because these two cases stop the
    process they are testing - and the tests after them still need an API to talk to. `--rm`
    is deliberately not used: the exit status is the assertion, and a removed container has
    none.

    `environment` is required rather than optional so a caller has to say what the container
    is given: `docker compose run` resolves the worker's pass-through variables from `.env`,
    which on a developer's machine holds real credentials.
    """
    overrides = [f"--env={name}={value}" for name, value in environment.items()]
    container = _compose("run", "-d", "--no-deps", *overrides, service).stdout.strip()
    try:
        yield container.splitlines()[-1]
    finally:
        _docker("rm", "-f", container.splitlines()[-1])


def _wait_for_log(container: str, line: str, *, seconds: int = 60) -> str:
    """Block until the container has said it is up. A signal sent before then would be testing
    a different shutdown than the one that matters."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        logs = _logs(container)
        if line in logs:
            return logs
        time.sleep(1)
    raise AssertionError(f"{container} never logged {line!r}: {_logs(container)}")


def _containers(*, running_only: bool = True) -> list[dict[str, Any]]:
    """`docker compose ps`, one parsed object per container. It prints one JSON document per
    line rather than one array, which is why this is a comprehension and not a single load."""
    printed = _compose("ps", *([] if running_only else ["-a"]), "--format", "json").stdout
    return [cast(dict[str, Any], json.loads(line)) for line in printed.splitlines() if line]


SECRET_SHAPED = ("KEY", "SECRET", "TOKEN", "PASSWORD")
"""Variable names whose *values* must never reach a log, an assertion message or a report.

A failing test prints what it compared, and pytest prints the whole local variable when that
comparison is a dict - so a container's environment has to arrive already unreadable. Names
are the only thing anything here needs to assert about.
"""

REDACTED = "<redacted>"


def _service_environment(service: str) -> dict[str, str]:
    """The process environment of the running container, **with every credential redacted**.

    The names are what the tests below check - which variables a process has and which it does
    not - and a name is not a secret. The values of the secret-shaped ones are replaced here,
    at the boundary, rather than at each use: a redaction that has to be remembered is one that
    will be forgotten in the test that fails.
    """
    printed = _in_container(service, "env")
    entries = (line.partition("=") for line in printed.splitlines() if "=" in line)
    return {
        name: REDACTED if any(word in name for word in SECRET_SHAPED) else value
        for name, _, value in entries
    }


@pytest.fixture(scope="session", autouse=True)
def stack() -> None:
    """Skip the whole module unless the stack is up, the way every other marked layer does."""
    if not os.environ.get("TEST_CONTAINER_STACK"):
        pytest.skip(SKIP_REASON, allow_module_level=True)


@pytest.fixture(scope="session")
def api_base_url() -> str:
    """Where the published API answers on the host. `API_PORT` moves it, so it is read rather
    than assumed - the same reason `POSTGRES_PORT` exists."""
    return f"http://localhost:{os.environ.get('API_PORT', '8000')}"


# --- 0. The stack under test ------------------------------------------------------------


def test_the_worker_under_test_cannot_reach_a_real_model() -> None:
    """**The guard on every other test in this file.** A worker holding real credentials would
    make this layer capable of spending money, and the whole point of a smoke test is that it
    proves the wiring rather than the pipeline. Started as the header documents, the worker's
    endpoint does not resolve, so a node that ran would fail on DNS rather than on a bill."""
    base_url = _service_environment("worker").get("LLM_BASE_URL", "")

    # The value is deliberately not in the message: this assertion fails exactly when the
    # variable holds something real, which is the one case where echoing it would be wrong.
    assert UNREACHABLE_LLM_HOST in base_url, (
        "the worker container's LLM_BASE_URL is not a non-routable address, so this layer "
        "could reach a real model. Restart the stack with the fake provider credentials in "
        "this module's header before running it."
    )


# --- 1. One image, two entrypoints ------------------------------------------------------


def test_both_processes_run_from_the_same_image() -> None:
    """ARCHITECTURE.md decision 25, as running containers rather than as a declaration."""
    images = {
        service: json.loads(_compose("images", "--format", "json", service).stdout)
        for service in ("api", "worker")
    }

    assert images["api"][0]["ID"] == images["worker"][0]["ID"]
    assert images["api"][0]["Repository"] == images["worker"][0]["Repository"]


def test_the_image_can_import_both_entrypoints() -> None:
    """The closure `test_container_image.py` can only approximate: every module `app.py` and
    `worker.py` reach, transitively, is present in the image and importable from /app."""
    status, output = _one_off("api", "python", "-c", "import app, worker; print('both')")

    assert status == 0, output
    assert "both" in output


def test_the_running_containers_are_not_root() -> None:
    """The Dockerfile's `USER`, as the kernel sees it. A uid rather than a name, because a uid
    is what a host or an orchestrator can be told about."""
    for service in ("api", "worker"):
        assert _in_container(service, "id", "-u").strip() == "10001", service


def test_the_running_containers_carry_no_env_file_and_no_tests() -> None:
    """`.dockerignore`'s two claims, checked inside the filesystem that resulted from it."""
    status, output = _one_off(
        "api", "sh", "-c", "ls -a /app | grep -E '^(\\.env|tests)$' || echo clean"
    )

    assert status == 0, output
    assert "clean" in output


def test_only_the_api_publishes_a_port() -> None:
    """The worker serves nothing, so nothing on the host may reach it."""
    assert _compose("port", "api", "8000").stdout.strip()
    assert not _compose("port", "worker", "8000", check=False).stdout.strip()


def test_the_api_is_published_to_this_machine_and_not_to_the_network() -> None:
    """FINDING 3, as the binding the kernel actually made.

    `docker compose ps` reports the published address, and `0.0.0.0:8000` would mean this API -
    whose key table is two keys published in `.env.example` - is answering on every interface
    the developer's machine has. On a café or conference network that is the whole LAN.
    """
    published = _compose("port", "api", "8000").stdout.strip()

    assert published.startswith("127.0.0.1:"), published
    assert "0.0.0.0" not in published


# --- 2. Migrations ----------------------------------------------------------------------


def test_the_migration_task_ran_and_exited_cleanly() -> None:
    """guidelines §19's ordering: `alembic upgrade head` is its own one-off task, and `api` and
    `worker` only start once it has exited 0."""
    migrate = [row for row in _containers(running_only=False) if row["Service"] == "migrate"]

    assert migrate, "the migrate service never ran"
    assert migrate[0]["ExitCode"] == 0
    assert migrate[0]["State"] == "exited"


def test_the_schema_is_at_head_in_the_compose_database() -> None:
    """Which is the migration having actually landed, rather than the task having exited."""
    status, output = _one_off("migrate", "alembic", "current")

    assert status == 0, output
    assert "(head)" in output


# --- 3. The API -------------------------------------------------------------------------


def test_health_answers_from_outside_the_container(api_base_url: str) -> None:
    """The published port, the bind address, and both dependency checks in one request.

    `db: true` needs a real query against the Compose PostgreSQL and `redis: true` needs a
    ping, so a healthy body here is the API reaching two stores by their service names.
    """
    response = httpx.get(f"{api_base_url}/health", timeout=10)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"db": True, "redis": True, "checkpoints": True},
    }


def test_health_reports_the_checkpoint_store_the_worker_created() -> None:
    """FINDING 4's readiness half, in the one deployment where it is observable.

    The API reads LangGraph's tables and owns none of them: Alembic never touches them and this
    process never calls `setup()` (ADR 0012). So on a database that has been migrated but never
    had a worker start against it they do not exist - a deployment in which no job can ever
    run, which is what the check reports. Here a worker *has* started, so it is `True`, and the
    fact that it is a key at all is what makes the empty case visible rather than silent.
    """
    body = httpx.get(f"http://localhost:{os.environ.get('API_PORT', '8000')}/health").json()

    assert body["checks"]["checkpoints"] is True
    assert "worker ready on" in _compose("logs", "worker").stdout


def test_the_api_container_holds_no_llm_or_search_credential() -> None:
    """docs/adr/0012-*.md, in the one place that settles it: the running process environment.

    The image carries agent code this process never executes. What makes that acceptable is
    that it could not call a model if it tried.
    """
    environment = _service_environment("api")

    for variable in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "TAVILY_API_KEY"):
        assert variable not in environment, variable


def test_a_report_url_the_api_signs_can_be_downloaded_from_this_machine() -> None:
    """FINDING 1, end to end: the worker writes inside the network and a host client downloads.

    `GET /jobs/{id}/report` answers a 15-minute presigned URL (ADR 0009 decision 3), and the
    client that follows it is a reviewer's browser - outside the Compose network by definition.
    Signed against `AWS_ENDPOINT_URL` the URL named `localstack:4566`, a hostname that resolves
    on that network and nowhere else, so every download failed with a DNS error.

    It cannot be repaired after the fact: SigV4 covers the host, so rewriting the URL breaks
    the signature. The address has to be the client's *before* the signature exists, which is
    what `S3_PUBLIC_ENDPOINT_URL` is for - and this test is the proof, because it fetches the
    URL the API produced with an ordinary HTTP client on the host.

    **The write and the read stay on opposite sides.** The object is put by the worker, which
    is the process with `PutObject` (guidelines §13), through the internal endpoint. The API
    only signs.
    """
    written = _one_off(
        "worker",
        "python",
        "-c",
        "import os;from artifacts import build_artifact_store;"
        "store=build_artifact_store(os.environ['S3_BUCKET'],region=os.environ['AWS_REGION'],"
        "endpoint_url=os.environ['AWS_ENDPOINT_URL']);"
        f"print(store.put_report('{PROBE_JOB_ID}',{{'probe':'container-smoke'}}))",
        environment=FAKE_PROVIDER_CREDENTIALS,
    )
    assert written[0] == 0, written[1]
    assert f"reports/{PROBE_JOB_ID}.json" in written[1]

    signed = _one_off(
        "api",
        "python",
        "-c",
        "import os;from artifacts import build_artifact_store;"
        "store=build_artifact_store(os.environ['S3_BUCKET'],region=os.environ['AWS_REGION'],"
        "endpoint_url=os.environ['S3_PUBLIC_ENDPOINT_URL']);"
        f"print(store.presign('{PROBE_JOB_ID}')[0])",
    )
    assert signed[0] == 0, signed[1]
    url = next(line for line in signed[1].splitlines() if line.startswith("http"))

    assert "localstack:4566" not in url  # the internal name would resolve nowhere out here
    assert f"X-Amz-Expires={REPORT_URL_TTL_S}" in url

    downloaded = httpx.get(url, timeout=10)

    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.json() == {"probe": "container-smoke"}


def test_only_the_api_is_told_where_a_client_reaches_the_bucket() -> None:
    """The split that keeps the fix from leaking into the write path.

    The worker's `PutObject` is a real call from inside the network and must keep using the
    internal address; the public one exists only to be signed into a URL. So the variable is on
    the API and on nothing else - and the worker still has the internal endpoint it writes
    through.
    """
    assert "S3_PUBLIC_ENDPOINT_URL" in _service_environment("api")
    assert "S3_PUBLIC_ENDPOINT_URL" not in _service_environment("worker")
    assert _service_environment("worker")["AWS_ENDPOINT_URL"] == "http://localstack:4566"


def test_the_api_reads_the_compose_database() -> None:
    """`/health` already proves a connection. This proves the schema the migration created is
    the one this process is looking at, which is the half a health check cannot see."""
    status, output = _one_off(
        "api",
        "python",
        "-c",
        "import os,sqlalchemy as sa;from database.queries import create_database_engine;"
        "engine=create_database_engine(os.environ['DATABASE_URL']);"
        "print(sorted(sa.inspect(engine).get_table_names()))",
    )

    assert status == 0, output
    for table in ("jobs", "findings", "claims", "claim_sources", "audit_events"):
        assert f"'{table}'" in output, table


# --- 4. The worker ----------------------------------------------------------------------


def test_the_worker_reports_ready_against_the_queue_and_redis() -> None:
    """One log line, and it is written last on purpose.

    Everything before it had to succeed: the queue's attributes were read and passed ADR 0010
    decision 8's inequality, Redis answered `check_redis`, and the Postgres checkpointer ran
    `setup()`. So this line is three stores reached by their Compose names.
    """
    logs = _compose("logs", "worker").stdout

    assert "worker ready on http://localstack:4566/" in logs
    assert "redis at redis://redis:6379/0" in logs


def test_the_worker_reaches_the_report_bucket() -> None:
    """The one store nothing contacts at startup: `build_artifact_store` opens no connection,
    so a bucket that could not be reached would surface at export time - after a job had paid
    for its whole pipeline."""
    status, output = _one_off(
        "worker",
        "python",
        "-c",
        "import os,boto3;print(boto3.client('s3',region_name=os.environ['AWS_REGION'],"
        "endpoint_url=os.environ['AWS_ENDPOINT_URL']).head_bucket("
        "Bucket=os.environ['S3_BUCKET'])['ResponseMetadata']['HTTPStatusCode'])",
    )

    assert status == 0, output
    assert "200" in output


def test_the_worker_refuses_to_start_without_a_search_credential() -> None:
    """`worker.required_credentials`, from the container rather than from a unit test.

    A worker that started without it would take a message and fail a job at the first
    Researcher node - after the plan had been paid for. Failing here costs one log line.
    """
    status, output = _one_off("worker", environment={"TAVILY_API_KEY": ""})

    assert status != 0
    assert "TAVILY_API_KEY" in output


def test_the_worker_refuses_to_start_without_a_model() -> None:
    """The same statement for the other half of the provider set, so the check is not passing
    for one variable by accident."""
    status, output = _one_off("worker", environment={"LLM_API_KEY": ""})

    assert status != 0
    assert "LLM_API_KEY" in output


def test_the_worker_stays_up_when_its_configuration_is_complete() -> None:
    """The counterpart to the two refusals: the same image, the same command, everything set,
    and a process that is still running rather than restarting in a loop."""
    worker = [row for row in _containers() if row["Service"] == "worker"]

    assert worker, "the worker container is not running"
    assert worker[0]["State"] == "running"


# --- 5. Shutdown ------------------------------------------------------------------------


STOP_TIMEOUT_S = 120
"""The same maximum graceful-stop opportunity Compose and future Fargate provide the worker.

An idle worker may still be inside its 20-second SQS long poll when SIGTERM arrives; the signal
flag cannot cancel that socket read. Giving the probe exactly 20 seconds made ordinary scheduling
jitter look like a shutdown failure. `docker stop` returns as soon as the process exits, so this
ceiling does not make a healthy test wait two minutes. It also makes the probe test the deployed
contract rather than a stricter number the service never promised.
"""


def test_the_api_exits_when_it_is_asked_to_stop() -> None:
    """SIGTERM, and a process that is gone rather than one that logged that it was going.

    **This is a regression test.** The first containerised API logged a complete uvicorn
    shutdown and then sat there: `_build` opened a checkpoint-reader `ConnectionPool` that
    nothing closed, and psycopg waits five seconds per pool thread at interpreter exit. Every
    stop ended in SIGKILL and exit 137, which would stall every rolling deploy and every
    rollback - the recovery path guidelines §19 depends on.
    """
    with _detached("api", {}) as container:
        _wait_for_log(container, "Application startup complete")

        _docker("stop", "--timeout", str(STOP_TIMEOUT_S), container)

        assert _docker("inspect", container, "--format", "{{.State.ExitCode}}") == "0"
        assert "Application shutdown complete" in _logs(container)


def test_the_worker_stops_at_a_checkpoint_boundary_and_exits() -> None:
    """The graceful-shutdown contract (ARCHITECTURE.md §11), in a container.

    SIGTERM stops the worker taking new work and lets the **node** in flight finish if possible,
    checkpointing before it starts no next node. The 120-second stop grace period is the platform's
    maximum best-effort opportunity, not a bound every node is guaranteed to meet;
    `tests/test_worker.py` is where the node-boundary behaviour itself is driven.

    With an empty queue there is no graph node in flight, though a 20-second SQS long poll may be.
    This shows the half a container can get wrong: the signal is handled, the poll returns, the
    process exits itself, and the runtime never has to kill it. Exit 0 is the assertion; the log
    line says which contract produced it.
    """
    with _detached("worker", FAKE_PROVIDER_CREDENTIALS) as container:
        _wait_for_log(container, "worker ready on")

        _docker("stop", "--timeout", str(STOP_TIMEOUT_S), container)

        logs = _logs(container)
        assert _docker("inspect", container, "--format", "{{.State.ExitCode}}") == "0"
        assert "signal 15 received; stopping at the next checkpoint boundary" in logs
        assert "worker stopped after handling 0 messages" in logs
