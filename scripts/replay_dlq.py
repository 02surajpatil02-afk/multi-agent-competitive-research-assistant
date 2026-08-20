"""
WHY THIS FILE EXISTS
    Putting one dead-lettered message back on the job queue, deliberately:

        python scripts/replay_dlq.py --job-id <id>                       # dry run, the default
        python scripts/replay_dlq.py --job-id <id> --apply

    Four things here are the decision rather than plumbing.

    **There is no `--all`, and there must not be.** A message reaches the dead-letter queue
    because three deliveries could not make it work. Replaying every such message is how one
    outage becomes the same outage four more times, which is why AWS's own `StartMessageMoveTask`
    was evaluated and not used: it cannot look at a job's durable state before it moves a
    message, and every refusal below is a state where moving one would do nothing or do harm
    ([ADR 0021](../docs/adr/0021-stale-job-reconciliation-and-dlq-recovery.md) decision 6). Naming
    the job is what makes this a decision rather than a reflex.

    **The message that goes back is the message that came out.** Same `MessageGroupId`, same
    `MessageDeduplicationId`, same three identifiers - so ADR 0010 decision 4's per-job ordering
    and ADR 0007's gate-visit key survive the recovery, and the worker handles it exactly as it
    would have handled the original delivery. Nothing here bypasses the worker's durable
    execution path; it only gives the worker the message again.

    **Safety is checked under the same per-job PostgreSQL fence a worker takes**, against state
    reread inside it. A job somebody is actively running is refused - a fourth delivery pushed at
    a live job is the one thing a recovery tool must never do.

    **Dry run is the default.** `--apply` is a word you have to type, and until you do, the
    verdict and its reason are printed and nothing moves.

WHO CALLS IT
    A person holding the deployment's database and queue credentials. It needs `DATABASE_URL`
    and `SQS_QUEUE_URL`, and **no LLM key, no Tavily key and no Redis**.
"""

from __future__ import annotations

import argparse
import logging
import sys

import operations
from config import load_config, required
from database.queries import create_database_engine
from jobqueue import JobMessage, JobQueue, QueueError, build_queue

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ERRORED = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="replay_dlq",
        description=(
            "Put a named job's dead-lettered message back on the jobs queue, if durable state "
            "says that is safe (ADR 0021). Dry run unless --apply is given."
        ),
    )
    parser.add_argument(
        "--job-id",
        action="append",
        required=True,
        metavar="ID",
        help="Which job's message to replay. Repeatable. There is deliberately no --all.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually send. Without it the verdict is printed and nothing moves.",
    )
    parser.add_argument(
        "--limit", type=int, default=100, help="How many messages to read. Default 100."
    )
    return parser.parse_args(argv)


def selected(messages: list[JobMessage], job_ids: list[str]) -> list[JobMessage]:
    """The messages the operator named, and a warning for every name that matched nothing.

    A name that matched nothing is worth saying out loud: the usual causes are a typo and a
    message that has already been replayed, and an operator who assumes the former when it was
    the latter will replay it twice.
    """
    wanted = set(job_ids)
    found = [message for message in messages if message.job_id in wanted]
    for missing in sorted(wanted - {message.job_id for message in found}):
        logger.warning("no dead-letter message names job %s", missing)
    return found


def read_messages(dead_letters: JobQueue, *, limit: int) -> list[JobMessage]:
    """Read the dead-letter queue while **holding** what it returns.

    Unlike `inspect_dlq.py`, nothing is released here: a message this run may replay has to stay
    owned for long enough to check durable state and send it, or two operators running this at
    once could each send the same message. `REPLAY_VISIBILITY_S` is that window, and a message
    this run does not replay simply reappears when it lapses.
    """
    found: list[JobMessage] = []
    while len(found) < limit:
        batch = dead_letters.receive_batch(
            max_messages=min(10, limit - len(found)),
            wait_seconds=1,
            visibility_timeout_s=operations.REPLAY_VISIBILITY_S,
        )
        if not batch:
            break
        found.extend(batch)
    return found


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
        dead_letters = queue.dead_letter_queue()
        if dead_letters is None:
            logger.error("the job queue has no redrive policy, so it has no dead-letter queue")
            return EXIT_REFUSED
        messages = read_messages(dead_letters, limit=args.limit)
    except QueueError:
        logger.exception("the dead-letter queue could not be read")
        return EXIT_REFUSED

    chosen = selected(messages, list(args.job_id))
    if not chosen:
        print("nothing to replay")
        return EXIT_OK

    engine = create_database_engine(database_url)
    try:
        with operations.checkpoint_reader(database_url) as checkpoints:
            replayer = operations.Replayer(
                engine=engine,
                checkpoints=checkpoints,
                queue=queue,
                dead_letters=dead_letters,
                apply=bool(args.apply),
            )
            results = [replayer.replay(message) for message in chosen]
    finally:
        engine.dispose()

    return report(results, applied=bool(args.apply))


def report(results: list[operations.ReplayResult], *, applied: bool) -> int:
    for result in results:
        print(result.line())

    errored = [result for result in results if result.outcome == "errored"]
    replayed = [result for result in results if result.applied]
    pending = [
        result for result in results if result.outcome == "replayable" and not result.applied
    ]

    print(f"\n{len(replayed)} replayed; {len(pending)} awaiting --apply; {len(errored)} errored")
    if pending and not applied:
        print("Nothing moved. Re-run with --apply to send these back onto the jobs queue.")
    return EXIT_ERRORED if errored else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
