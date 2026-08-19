"""PostgreSQL execution ownership for one worker job.

The graph deliberately commits each node event in its own transaction, and LangGraph commits its
checkpoint separately.  A transaction-scoped lock therefore cannot cover one execution.  This
module holds a session-scoped advisory lock on one dedicated SQLAlchemy connection instead.  The
connection is never used for application writes; its lifetime is the ownership lifetime.

SQLite is the offline test store, not a deployed worker store.  It receives a no-op ownership
handle so the service-free suite can exercise worker control flow; the real cross-process property
is tested against PostgreSQL in ``tests/test_database_postgres.py``.
"""

from __future__ import annotations

import hashlib
import logging
from types import TracebackType

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

_LOCK_PERSON = b"research-job"
"""BLAKE2 domain separation for this advisory-lock key space (at most 16 bytes)."""


def job_execution_lock_key(job_id: str) -> int:
    """Map a job id deterministically into PostgreSQL's signed 64-bit advisory-key space.

    The database stores job ids as text, so deriving from the complete UTF-8 value also behaves
    safely if a non-UUID legacy id is encountered.  A 64-bit hash can theoretically collide; the
    consequence is conservative false serialization of two unrelated jobs, never two owners of one
    job.  There is no truncation scheme that can inject arbitrary text into a 64-bit key without
    that same finite-space property.
    """
    digest = hashlib.blake2b(job_id.encode("utf-8"), digest_size=8, person=_LOCK_PERSON).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


class JobExecutionLock:
    """One session-scoped PostgreSQL advisory lock and the session that owns it."""

    def __init__(self, engine: Engine, job_id: str) -> None:
        self._engine = engine
        self.job_id = job_id
        self.key = job_execution_lock_key(job_id)
        self._connection: Connection | None = None
        self._acquired = False
        self.backend_pid: int | None = None

    @property
    def acquired(self) -> bool:
        return self._acquired

    def __enter__(self) -> JobExecutionLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def try_acquire(self) -> bool:
        """Try once without blocking a PostgreSQL backend on another worker.

        The worker owns retry timing so it can keep its SQS heartbeat alive and stop promptly on
        SIGTERM or lease loss.  Keeping the same connection while waiting also means a successful
        attempt has an unambiguous session to retain for the complete graph execution.
        """
        if self._acquired:
            return True
        if self._engine.dialect.name != "postgresql":
            self._acquired = True
            return True

        if self._connection is None:
            self._connection = self._engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            )

        row = self._connection.execute(
            sa.text(
                "SELECT pg_try_advisory_lock(:key) AS acquired, pg_backend_pid() AS backend_pid"
            ),
            {"key": self.key},
        ).one()
        self.backend_pid = int(row.backend_pid)
        self._acquired = bool(row.acquired)
        if self._acquired:
            logger.info(
                "job %s: PostgreSQL execution lock acquired (key=%d backend=%d)",
                self.job_id,
                self.key,
                self.backend_pid,
            )
        return self._acquired

    def release(self) -> None:
        """Release normally, never returning a possibly locked session to the pool.

        PostgreSQL itself releases the advisory lock when the backend/process disappears.  On a
        normal path we unlock explicitly because ``Connection.close()`` returns the physical
        session to SQLAlchemy's pool rather than closing that session.
        """
        connection = self._connection
        if connection is None:
            self._acquired = False
            return

        try:
            if self._acquired:
                unlocked = bool(
                    connection.execute(
                        sa.text("SELECT pg_advisory_unlock(:key)"), {"key": self.key}
                    ).scalar_one()
                )
                if not unlocked:
                    logger.error(
                        "job %s: PostgreSQL reported the execution lock was not owned on release",
                        self.job_id,
                    )
        except Exception:
            # Do not put a session whose lock state is unknown back into the pool.  If the backend
            # has gone away PostgreSQL has already released the lock; invalidation makes SQLAlchemy
            # replace this physical connection before anybody else can borrow it.
            connection.invalidate()
            logger.exception(
                "job %s: execution-lock release failed; session invalidated", self.job_id
            )
        finally:
            connection.close()
            self._connection = None
            self._acquired = False
            logger.info("job %s: PostgreSQL execution lock released", self.job_id)


def job_execution_lock(engine: Engine, job_id: str) -> JobExecutionLock:
    """Database-boundary factory used by the worker and replaced by narrow test doubles."""
    return JobExecutionLock(engine, job_id)
