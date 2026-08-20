"""
WHY THIS FILE EXISTS
    The operator sweep for jobs that stopped without saying so:

        python scripts/reconcile_jobs.py                                  # dry run, the default
        python scripts/reconcile_jobs.py --apply --actor alice@example.com

    ADR 0010 decision 9 left one condition uncovered and said so: a worker killed hard on its
    last delivery leaves a `queued` or `running` row behind while its message goes to the
    dead-letter queue, and nothing was going to notice. This is the sweep that notices, and
    [ADR 0021](../docs/adr/0021-stale-job-reconciliation-and-dlq-recovery.md) is the record.

    Three things here are the decision rather than plumbing.

    **Dry run is the default, and `--apply` is a word you have to type.** The whole value of a
    reconciler is that it changes durable state, which is also the whole risk, and the report it
    prints in dry-run mode names every candidate, the evidence, the proposed action and the
    reason for it. Reading that report before running it again with `--apply` costs one command.

    **`--actor` is required to apply**, exactly as it is in `scripts/reexport_job.py` and for the
    same reason: `audit_events.actor` has a CHECK refusing `unknown`, because a durable row
    changed with no identity behind it is not a repair, it is a mystery. A dry run needs none,
    because it writes nothing.

    **The decisions are not in here.** `operations.py` holds them, so they can be driven by a
    test with no database, no queue and no clock - this file reads arguments, builds three
    collaborators and prints a table.

WHO CALLS IT
    A person holding the worker's database credentials. It needs `DATABASE_URL`; `SQS_QUEUE_URL`
    is optional and its absence narrows what the sweep can conclude rather than stopping it. It
    needs **no LLM key, no Tavily key and no Redis** - nothing here can reach a model.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime

import operations
from config import Config, load_config, required
from database.queries import create_database_engine
from jobqueue import JobQueue, QueueError, build_queue

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ERRORED = 2
"""Three codes, for the reason `eval/gate.py` has three: a run that could not start and a run in
which one candidate blew up are different facts, and a wrapper that could not tell them apart
would retry the wrong one."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="reconcile_jobs",
        description=(
            "Inspect queued and running jobs that have stopped making progress, and repair "
            "their rows from durable state (ADR 0021). Dry run unless --apply is given."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without it nothing is changed and the report says what would be.",
    )
    parser.add_argument(
        "--actor",
        help="Who is running this. Required with --apply; written to audit_events.actor.",
    )
    parser.add_argument(
        "--job-id",
        help="Inspect one job instead of sweeping. The age filter still applies.",
    )
    parser.add_argument(
        "--min-age-seconds",
        type=int,
        help="Override STALE_JOB_MIN_AGE_SECONDS. 0 inspects every unfinished job.",
    )
    parser.add_argument(
        "--dlq-limit",
        type=int,
        default=100,
        help="How many dead-letter messages to read for evidence. Default 100.",
    )
    return parser.parse_args(argv)


def dead_lettered_job_ids(queue: JobQueue, *, limit: int) -> list[str]:
    """Which jobs have a message sitting in the dead-letter queue.

    **This is the evidence that lets a job be recorded as failed, and it is the only one.** The
    read is non-destructive: every message is released straight back, so the queue an alarm is
    watching is not emptied by looking at it.

    A queue with no redrive policy has no dead-letter queue, and then this is empty - which
    means nothing can be failed by this sweep at all. That is the safe direction for the
    absence: a deployment that redelivers forever has no "no delivery is coming back" to prove.
    """
    dead_letters = queue.dead_letter_queue()
    if dead_letters is None:
        logger.warning("the job queue has no redrive policy; no job can be shown to be orphaned")
        return []
    messages = operations.read_dead_letter_messages(dead_letters, limit=limit)
    return [message.job_id for message in messages]


def build_queue_if_configured(config: Config) -> JobQueue | None:
    """The job queue, or None when this deployment has not been told about one.

    None narrows the sweep rather than stopping it: without a queue there is no dead-letter
    evidence, so nothing is failed, and no message can be sent, so nothing is re-enqueued. Both
    of those candidates are reported as `skipped` with their evidence, which is what an operator
    needs to see.
    """
    if config.sqs_queue_url is None:
        logger.warning("SQS_QUEUE_URL is unset; running without queue evidence")
        return None
    return build_queue(
        config.sqs_queue_url, region=config.aws_region, endpoint_url=config.aws_endpoint_url
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config()
    logging.basicConfig(level=config.log_level, format="%(levelname)s %(message)s")

    if args.apply and not args.actor:
        logger.error("--apply needs --actor: an audit row cannot say a machine repaired a job")
        return EXIT_REFUSED

    min_age = (
        config.stale_job_min_age_seconds if args.min_age_seconds is None else args.min_age_seconds
    )
    if min_age < 0:
        logger.error("--min-age-seconds cannot be negative")
        return EXIT_REFUSED

    engine = create_database_engine(required(config.database_url, "DATABASE_URL"))
    queue = build_queue_if_configured(config)
    try:
        dead_lettered = (
            dead_lettered_job_ids(queue, limit=args.dlq_limit) if queue is not None else []
        )
    except QueueError:
        # Without this read the sweep would silently lose its only route to `failed`, and a
        # partial answer that looks complete is worse than a refusal an operator can retry.
        logger.exception("the dead-letter queue could not be read")
        engine.dispose()
        return EXIT_REFUSED

    try:
        with operations.checkpoint_reader(
            required(config.database_url, "DATABASE_URL")
        ) as checkpoints:
            candidates = operations.select_candidates(
                engine, min_age_seconds=min_age, now=datetime.now(UTC), job_id=args.job_id
            )
            reconciler = operations.reconciler_for(
                engine,
                checkpoints,
                actor=args.actor or "dry-run",
                apply=bool(args.apply),
                queue=queue,
                dead_lettered=dead_lettered,
            )
            results = reconciler.sweep(candidates)
    finally:
        engine.dispose()

    return report(results, applied=bool(args.apply), inspected=len(results))


def report(results: list[operations.Result], *, applied: bool, inspected: int) -> int:
    """Print the table and choose the exit code.

    An empty sweep is success and says so: "nothing is stale" is the answer this command exists
    to give most of the time, and a tool that looked alarmed about it would stop being run.
    """
    for result in results:
        print(result.line())

    errored = [result for result in results if result.outcome == "errored"]
    changed = [result for result in results if result.applied]
    pending = [result for result in results if result.mutating and not result.applied]

    print(
        f"\n{inspected} candidate(s) inspected; "
        f"{len(changed)} repaired; {len(pending)} awaiting --apply; {len(errored)} errored"
    )
    if pending and not applied:
        print("Nothing was written. Re-run with --apply --actor <you> to carry these out.")
    return EXIT_ERRORED if errored else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
