"""
WHY THIS FILE EXISTS
    What is in the dead-letter queue, and what the database thinks about each of those jobs:

        python scripts/inspect_dlq.py

    It is the first command the DLQ alarm's runbook entry asks for (docs/runbook.md), and it is
    the only one of the three block C tools that cannot change anything at all.

    Three things here are the decision rather than plumbing.

    **Reading a message does not consume it.** SQS has no peek - receiving is what makes a
    message invisible - so every message read here is released straight back with a zero
    visibility timeout. The queue an alarm is watching is not quietly emptied by looking at it,
    and the next person sees what this person saw.

    **A message is shown beside its job's durable state, because on its own it says almost
    nothing.** The body is three identifiers by design (ADR 0010 decision 3), so "job X failed
    three deliveries" only becomes actionable next to "and X is `running` with a terminal
    checkpoint" or "and X finished twenty minutes ago". The correlation is what turns the list
    into a decision about which of the other two tools to reach for.

    **Nothing is printed that is not an identifier.** The question text, the report, the
    reviewer's note and every credential stay where they are; what this prints is a job id, a
    status, a delivery count and the shape of the message.

WHO CALLS IT
    A person holding the deployment's database and queue credentials. It needs `DATABASE_URL`
    and `SQS_QUEUE_URL`, and **no LLM key, no Tavily key and no Redis**.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.engine import Engine

import operations
from config import load_config, required
from database import queries
from database.queries import create_database_engine
from jobqueue import JobMessage, JobQueue, QueueError, build_queue

logger = logging.getLogger(__name__)

EXIT_EMPTY = 0
EXIT_REFUSED = 1
EXIT_MESSAGES_PRESENT = 2
"""`2` is not a failure of this command - it ran perfectly. It is the answer "there are messages
in the dead-letter queue", separated from "there are none" so a wrapper can branch on it without
parsing the table below."""


@dataclass(frozen=True)
class Entry:
    """One dead-letter message, beside what the database says about its job."""

    job_id: str
    kind: str
    deliveries: int
    status: str | None
    completed: bool
    checkpoint: str

    def line(self) -> str:
        return (
            f"{self.job_id}  {self.kind:<7}  deliveries={self.deliveries}  "
            f"row={self.status or 'missing':<18}  "
            f"{'finished' if self.completed else 'unfinished':<10}  "
            f"checkpoint={self.checkpoint}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="inspect_dlq",
        description=(
            "List the jobs queue's dead-letter messages beside the durable state of each job. "
            "Reads only: every message is released back onto the queue."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=100, help="How many messages to read. Default 100."
    )
    return parser.parse_args(argv)


def describe(
    engine: Engine, checkpoints: BaseCheckpointSaver[Any], messages: list[JobMessage]
) -> list[Entry]:
    """Correlate each message with its job row and checkpoint. Reads nothing else."""
    entries: list[Entry] = []
    for message in messages:
        job = queries.read_job(engine, message.job_id)
        view = operations.read_checkpoint(checkpoints, message.job_id)
        entries.append(
            Entry(
                job_id=message.job_id,
                kind=operations.message_kind(message),
                deliveries=message.receive_count,
                status=None if job is None else str(job.status),
                completed=job is not None and job.completed_at is not None,
                checkpoint=_checkpoint_summary(view),
            )
        )
    return entries


def _checkpoint_summary(view: operations.CheckpointView | None) -> str:
    """One word for what the checkpoint holds, because that is what decides the next command."""
    if view is None:
        return "none"
    if view.waiting_at_gate:
        return "awaiting-reviewer"
    if view.terminal:
        return f"terminal:{view.status}"
    return f"unfinished:{view.status}"


def dead_letter_queue(queue: JobQueue) -> JobQueue | None:
    dead_letters = queue.dead_letter_queue()
    if dead_letters is None:
        logger.warning("the job queue has no redrive policy, so it has no dead-letter queue")
    return dead_letters


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config()
    logging.basicConfig(level=config.log_level, format="%(levelname)s %(message)s")

    database_url = required(config.database_url, "DATABASE_URL")
    queue = build_queue(
        required(config.sqs_queue_url, "SQS_QUEUE_URL"),
        region=config.aws_region,
        endpoint_url=config.aws_endpoint_url,
    )
    try:
        dead_letters = dead_letter_queue(queue)
        if dead_letters is None:
            return EXIT_REFUSED
        messages = operations.read_dead_letter_messages(dead_letters, limit=args.limit)
    except QueueError:
        logger.exception("the dead-letter queue could not be read")
        return EXIT_REFUSED

    engine = create_database_engine(database_url)
    try:
        with operations.checkpoint_reader(database_url) as checkpoints:
            entries = describe(engine, checkpoints, messages)
    finally:
        engine.dispose()

    for entry in entries:
        print(entry.line())
    print(f"\n{len(entries)} dead-letter message(s)")
    if entries:
        print(
            "Repair a stale row with scripts/reconcile_jobs.py; put a recoverable message back "
            "with scripts/replay_dlq.py --job-id <id>."
        )
    return EXIT_MESSAGES_PRESENT if entries else EXIT_EMPTY


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
