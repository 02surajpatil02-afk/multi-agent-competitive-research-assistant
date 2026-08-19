"""
WHY THIS FILE EXISTS
    The API entrypoint: `uvicorn app:app --reload` (CLAUDE.md commands). It builds what the
    routes need - the config, the database engine, a checkpoint *reader*, the queue, the key
    table, the Redis health probe and the artifact presigner - and hands them over as one
    object. Nothing here decides anything; the decisions are in routes/api.py and
    routes/auth.py.

    **Everything is built once, at startup, and a failure here is fatal on purpose.** A
    missing `AUTH_KEYS`, an unreachable database URL, or a malformed key table should stop
    the process rather than surface as a confusing 500 on the first request that needs it -
    which is the same rule config.py already follows for environment variables.

    `create_application()` takes its collaborators as arguments so a test can drive the real
    routes against a temporary database and a scripted model. The module-level `app` is the
    production wiring of that same function, and importing this module therefore requires
    the environment to be set - as any entrypoint does.

WHO CALLS IT
    uvicorn, in development and in the API container. Tests call `create_application()`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.engine import Engine

from artifacts import build_artifact_store
from config import Config, load_config, required
from database.queries import create_database_engine
from graph.state import state_serde
from jobqueue import JobQueue, build_queue
from redisstore import RedisHealth, build_redis
from routes.api import ApiError, RedisProbe, ReportPresigner, RouteDeps, router
from routes.auth import Identity, load_api_keys

logger = logging.getLogger(__name__)


def create_application(
    *,
    config: Config,
    engine: Engine,
    checkpoints: BaseCheckpointSaver[Any],
    queue: JobQueue,
    keys: dict[str, Identity] | None = None,
    redis: RedisProbe | None = None,
    artifacts: ReportPresigner | None = None,
    on_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """Wire the routes to their collaborators.

    `keys` is normally parsed from `config.auth_keys`; a caller passes its own only in a
    test, where the point is which role is calling rather than how the table was loaded.

    `redis` is the `/health` probe (step 21) and is optional for the same reason: a test that
    is not about health should not have to supply one, and an absent probe reports healthy
    rather than failing a check nobody configured.

    `artifacts` is what `GET /jobs/{id}/report` signs a URL with (step 22a), optional on the
    same terms. It is a **presigner**: the API's task role may sign for an object and the
    worker's writes one, and the Protocol the route layer declares is that half.

    `on_shutdown` releases what the caller opened, once uvicorn has drained its requests. It
    exists for one measured reason, found when the API was first containerised (step 22b/c):
    the checkpoint reader's `ConnectionPool` was never closed, and psycopg's own finalizer
    then waits **5 seconds per pool thread** at interpreter exit - four threads, so twenty
    seconds against a ten-second stop grace period. The container logged a clean uvicorn
    shutdown and was then SIGKILLed with 137 every single time. A test asserts this fires.
    """

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        """Nothing to do on the way up - everything is built before this function is called.
        The way down is the half that had been missing."""
        yield
        if on_shutdown is not None:
            on_shutdown()

    application = FastAPI(title="Competitive Research API", lifespan=lifespan)
    application.state.deps = RouteDeps(
        config=config,
        engine=engine,
        checkpoints=checkpoints,
        queue=queue,
        keys=load_api_keys(config.auth_keys) if keys is None else keys,
        redis=redis,
        artifacts=artifacts,
    )
    application.include_router(router)

    @application.exception_handler(ApiError)
    def _api_error(_request: Request, error: Exception) -> JSONResponse:
        """Every deliberate failure, in the one documented shape (guidelines §12)."""
        assert isinstance(error, ApiError)
        return error.response()

    @application.exception_handler(RequestValidationError)
    def _invalid_request(_request: Request, error: Exception) -> JSONResponse:
        """A body FastAPI could not parse, in that same shape rather than its own.

        "One shape, everywhere" is only true if the framework's default 422 does not leak
        through it - and its detail carries the rejected input, which an error body must not.
        """
        return ApiError(400, "invalid_request", "The request body is not valid").response()

    @application.exception_handler(Exception)
    def _unexpected(request: Request, error: Exception) -> JSONResponse:
        """Anything nobody planned for, in that same shape rather than a bare `500`.

        The concrete case is a gate decision whose resume raised (ADR 0007 invariant 8): the
        reviewer needs a stable code to branch on and a body that says which job it was, and
        what they got instead was FastAPI's default `Internal Server Error`. The reason goes
        to the log, where it belongs - a stack trace, an internal path, or a fragment of SQL
        in a response body is the leak guidelines §16 exists to stop.

        The fix for this response is to send the same request again: the decision is already
        recorded, so the retry continues the job from the checkpoint rather than restarting it.
        """
        job_id = request.path_params.get("job_id")
        logger.error("unhandled failure serving %s", request.url.path, exc_info=error)
        return ApiError(
            500,
            "internal_error",
            "The request could not be completed",
            job_id if isinstance(job_id, str) else None,
        ).response()

    return application


def _checkpoint_reader(database_url: str) -> tuple[PostgresSaver, ConnectionPool[Any]]:
    """A `PostgresSaver` the API only ever reads through (ADR 0012 decisions 1 and 3).

    **`setup()` is deliberately not called here.** It is DDL for tables LangGraph owns, and
    guidelines §13's least-privilege table gives the API no reason to run migrations. The worker
    calls it at startup; this process attaches to what the worker created.

    The pool lives as long as the process, which is why this is not a context manager - but it
    **is returned alongside the saver**, because "as long as the process" has to include the
    end of the process. A pool nobody closes leaves psycopg waiting five seconds per thread at
    interpreter exit, which is longer than a container's stop grace period; `_build` hands the
    close to the lifespan above. It is small for the same reason the worker's is: the API's
    reads are one row.

    `state_serde()` is shared with the worker unchanged - it exists precisely because "the
    answer does not depend on where the bytes go", and using it is what lets this process read
    what the worker wrote.
    """
    pool: ConnectionPool[Any] = ConnectionPool(
        database_url,
        connection_class=Connection[DictRow],
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True, "row_factory": dict_row},
        # Stated rather than defaulted: psycopg is changing this default and warns about it, and
        # the answer here is not the worker's. `postgres_checkpointer` opens lazily because it is
        # a context manager; this pool is opened at startup because a first request should not
        # pay for a connection, and it is closed by the lifespan above.
        open=True,
    )
    return PostgresSaver(pool, serde=state_serde()), pool


def _build() -> FastAPI:  # pragma: no cover - exercised by running the server
    """The production wiring. **No graph, no LLM client, no Tavily key** (ADR 0012).

    What it needs is `DATABASE_URL`, `AUTH_KEYS`, `SQS_QUEUE_URL` and - since step 22a -
    `S3_BUCKET`, because `GET /jobs/{id}/report` signs a URL for an object in it. A missing LLM
    credential can no longer stop this process from starting, which is what makes guidelines
    §13's least-privilege table a property of the code rather than of an intended deployment.
    """
    config = load_config()
    database_url = required(config.database_url, "DATABASE_URL")
    queue_url = required(config.sqs_queue_url, "SQS_QUEUE_URL")
    bucket = required(config.s3_bucket, "S3_BUCKET")
    checkpoints, pool = _checkpoint_reader(database_url)
    return create_application(
        config=config,
        engine=create_database_engine(database_url),
        checkpoints=checkpoints,
        queue=build_queue(
            queue_url, region=config.aws_region, endpoint_url=config.aws_endpoint_url
        ),
        # One method of one client, for `/health` alone. The API caches nothing and takes no
        # rate-limit token: those belong to the process that fetches pages and calls models.
        redis=RedisHealth(build_redis(config.redis_url)),
        # Presigning only. The API holds no report bytes and writes no object; the worker is
        # the process with `PutObject` (guidelines §13).
        #
        # **Signed against the address the client will use**, which is not always the address
        # this process would call. SigV4 covers the host, so it has to be right before the
        # signature exists rather than patched into the URL afterwards. Against real AWS both
        # are None and this reads exactly as it did.
        artifacts=build_artifact_store(
            bucket,
            region=config.aws_region,
            endpoint_url=config.s3_public_endpoint_url or config.aws_endpoint_url,
        ),
        # The one thing this process opens that will not close itself. Without it the container
        # never exits on SIGTERM - see `create_application`.
        on_shutdown=pool.close,
    )


def __getattr__(name: str) -> FastAPI:
    """`uvicorn app:app --reload` builds the application; importing this module does not.

    The command in CLAUDE.md asks for a module attribute, and a module attribute assigned at
    import time would mean reading the environment, opening a connection pool, and building a
    graph as a side effect of `import app` - which a test that only wants
    `create_application` should not pay for, and which turns a missing variable into an
    import error in places that never intended to start a server.
    """
    if name == "app":
        return _build()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
