# syntax=docker/dockerfile:1

# WHY THIS FILE EXISTS
#     **One image, two entrypoints** (ARCHITECTURE.md §19, decision 25). The API runs
#     `uvicorn app:app` and the worker runs `python -m worker`, and the only difference
#     between the two containers is the command and the environment. Two images would double
#     the build, the scan and the tag bookkeeping to separate code that is already separated
#     by module - and the two processes share `config.py`, `schemas.py`, `database/` and
#     `graph/state.py`, so the shared half is most of it.
#
#     The cost is stated rather than hidden: the API image carries agent code it never
#     executes. What stops that mattering is not the image, it is ADR 0012 - the API
#     constructs no graph and no `LLMClient`, and starts with no LLM or Tavily credential in
#     its environment at all. Code that is present and unreachable is a size argument;
#     credentials that are present and unused would be a security one.
#
#     **Two stages, for one reason: uv does not belong in the runtime image.** The first
#     stage installs the locked dependencies into /opt/venv, and the second copies that
#     directory and the application source. What is left behind is uv itself, its cache, and
#     every build artefact - so the process that serves HTTP has a package manager it cannot
#     run and a lockfile it cannot resolve.
#
#     **The dependency layer is keyed on `pyproject.toml` and `uv.lock` alone**, which is why
#     they are copied before the source: editing `app.py` rebuilds one small layer rather
#     than reinstalling psycopg, langgraph and boto3.
#
#     **`uv sync --frozen` is what makes the build reproducible.** The lockfile pins every
#     transitive version and its hash, and `--frozen` refuses to update it - so an image
#     built today and one built next month install the same bytes, or fail loudly instead of
#     drifting.
#
#     **Nothing here reads a secret and nothing here writes one.** There is no ARG carrying a
#     credential, no `.env` (`.dockerignore` keeps it out of the context, and no COPY names
#     it), and no AWS or LLM key. Every value the two processes need arrives as an
#     environment variable at run time, which is what lets one image serve local Compose and
#     a deployment without being rebuilt.
#
# WHO USES IT
#     `docker compose --profile app build`, which tags it `competitive-research:local` and
#     runs it as three services: `migrate`, `api` and `worker`.

# --- Stage 1: the locked dependencies -------------------------------------------------

# `python:3.13-slim` because pyproject.toml requires >= 3.13, and slim because every
# dependency here ships a manylinux wheel - `psycopg[binary]` bundles libpq, `pypdf` is pure
# Python - so the image needs no compiler and no apt package. The tag floats within 3.13 on
# purpose, the same way docker-compose.yml pins `postgres:16-alpine`: a patch release arrives
# with a rebuild rather than with an edit here.
FROM python:3.13-slim AS dependencies

# uv is this project's package manager - `uv.lock` is committed and is the source of truth
# for every version. Copying the binary from its own published image is cheaper than
# installing it with pip, and it is pinned like any other dependency.
COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    # Compile to .pyc at build time rather than on the first request of every container.
    UV_COMPILE_BYTECODE=1 \
    # Copy rather than hardlink out of the cache: the cache and the venv are on different
    # layers here, and a hardlink across them does not survive.
    UV_LINK_MODE=copy \
    # Use the interpreter this image already has. Without it uv may download a managed
    # CPython, and the venv would then point at a path the runtime stage does not copy.
    UV_PYTHON_DOWNLOADS=never

WORKDIR /src

# The two files that decide what gets installed, and nothing else - so this layer is
# invalidated by a dependency change and by nothing else.
COPY pyproject.toml uv.lock ./

# `--no-dev` leaves pytest, ruff and mypy out of the runtime image: they are how the code is
# checked, never how it runs. `--no-install-project` installs the dependencies only; the
# application itself arrives as source in the next stage, because the repository root is the
# import root that `uvicorn app:app` and `python -m worker` both expect.
RUN uv sync --frozen --no-dev --no-install-project

# --- Stage 2: the runtime -------------------------------------------------------------

FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    # Nothing in the image is writable by the runtime user, and a container that cannot cache
    # bytecode should not try to on every import. The dependencies are already compiled above.
    PYTHONDONTWRITEBYTECODE=1 \
    # The virtual environment, on PATH, so `uvicorn`, `python` and `alembic` are the ones
    # installed from the lockfile.
    PATH="/opt/venv/bin:$PATH" \
    # **The import root, stated rather than assumed.** `uvicorn app:app` and `python -m worker`
    # resolve from the working directory, so they never needed this - but `python
    # scripts/reexport_job.py` does not: running a file by path puts *its own directory* on
    # `sys.path`, not the working directory, so `from artifacts import ...` failed inside the
    # image with `ModuleNotFoundError`. On a developer's machine the same command works because
    # `uv sync` installs this project into the local virtual environment; the image deliberately
    # installs only the dependencies. Found by running ADR 0009's recovery path in a container
    # (Phase 5 block C), and the reason it matters is that in a deployment there is no host to
    # run these tools from - the image is the only place they can run.
    PYTHONPATH=/app

# A non-root user with a fixed uid. Fixed rather than allocated because a numeric uid is what
# a host or an orchestrator can be told about, and `--system` because this account exists to
# run one process and never to log in.
RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin app

COPY --from=dependencies /opt/venv /opt/venv

# The repository root is the import root (pyproject.toml's `[tool.setuptools]`), so the
# working directory is the flat layout as it appears in the repository.
WORKDIR /app

# **Named paths rather than `COPY . .`** - the image should hold what the two processes
# import and nothing that happens to be next to it. Ownership stays with root: the
# application only reads its own source, so files it cannot rewrite are one fewer thing an
# injected page could aim at.
COPY app.py worker.py config.py schemas.py llm_client.py bedrock.py jobqueue.py \
     redisstore.py artifacts.py operations.py alembic.ini ./
COPY agents/ ./agents/
COPY database/ ./database/
COPY graph/ ./graph/
COPY routes/ ./routes/
COPY tools/ ./tools/

# Four scripts, deliberately - the operator recovery tools, and nothing else. Each is
# application code that re-projects durable state, and none of them can reach a model.
#
# **They are in the image because in a deployment there is no host to run them from.** RDS and
# ElastiCache have no public address and sit in subnets with no route off the VPC, so a laptop
# cannot reach the database at all: the only way to run one of these is as a one-off task in
# the same VPC, from this image (infra/ecs.tf's `ops` task definition, docs/runbook.md).
#
# `check_model.py` calls a real model endpoint and `measure_jobs.py` runs real jobs and writes
# `measurements/`. Both are development tools and stay out.
COPY scripts/__init__.py scripts/reexport_job.py scripts/reconcile_jobs.py \
     scripts/inspect_dlq.py scripts/replay_dlq.py ./scripts/

USER app

# **No EXPOSE.** It is metadata only, and in a two-entrypoint image it is metadata that lies:
# `docker compose ps` prints an exposed port against the worker row, and the worker serves
# nothing. Which port is open is the command's decision - `uvicorn --port 8000` - and which
# port is reachable is Compose's, in the one service that publishes one.

# The default is the API, because it is the process that starts with the fewest credentials.
# All three Compose services set `command:` explicitly, so this is what a bare `docker run`
# gets and never what a deployment relies on.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
