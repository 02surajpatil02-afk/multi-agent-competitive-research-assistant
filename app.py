"""
WHY THIS FILE EXISTS
    The API entrypoint: `uvicorn app:app --reload` (CLAUDE.md commands). It builds the four
    things the routes need - the config, the database engine, the compiled graph, and the
    key table - and hands them over as one object. Nothing here decides anything; the
    decisions are in routes/api.py and routes/auth.py.

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

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from openai import OpenAI
from sqlalchemy.engine import Engine

from config import Config, load_config
from database.queries import create_database_engine
from graph.build import ResearchGraph, build_graph, postgres_checkpointer
from llm_client import LLMClient
from routes.api import ApiError, RouteDeps, router
from routes.auth import Identity, load_api_keys

logger = logging.getLogger(__name__)


def create_application(
    *,
    config: Config,
    engine: Engine,
    graph: ResearchGraph,
    keys: dict[str, Identity] | None = None,
) -> FastAPI:
    """Wire the routes to their collaborators.

    `keys` is normally parsed from `config.auth_keys`; a caller passes its own only in a
    test, where the point is which role is calling rather than how the table was loaded.
    """
    application = FastAPI(title="Competitive Research API")
    application.state.deps = RouteDeps(
        config=config,
        engine=engine,
        graph=graph,
        keys=load_api_keys(config.auth_keys) if keys is None else keys,
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


def _production_graph(config: Config, engine: Engine) -> ResearchGraph:  # pragma: no cover
    """The graph the API resumes at the gate, on the durable checkpointer.

    The pool it opens lives as long as the process, which is why this is not a context
    manager: the API holds it for every gate decision it will ever serve.
    """
    database_url = config.database_url
    if database_url is None:
        raise RuntimeError("DATABASE_URL is required from Phase 2 (CLAUDE.md environment table)")

    checkpointer = postgres_checkpointer(database_url).__enter__()
    client = LLMClient(
        config, client=OpenAI(base_url=config.llm_base_url, api_key=config.llm_api_key)
    )
    return build_graph(config=config, llm=client, db=engine, checkpointer=checkpointer)


def _build() -> FastAPI:  # pragma: no cover - exercised by running the server
    config = load_config()
    if config.database_url is None:
        raise RuntimeError("DATABASE_URL is required from Phase 2 (CLAUDE.md environment table)")
    engine = create_database_engine(config.database_url)
    return create_application(config=config, engine=engine, graph=_production_graph(config, engine))


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
