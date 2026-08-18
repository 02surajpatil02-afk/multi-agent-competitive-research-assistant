"""
WHY THIS FILE EXISTS
    ADR 0009 decision 4: recovering a job whose artifact write was exhausted. The report is
    finished, approved, gate-passed and durable in `jobs.report_json`; what is missing is the
    S3 object and therefore the presigned URL. Re-running the graph would re-bill research and
    synthesis for a storage error, which ARCHITECTURE.md §8 rules out in terms - so recovery is
    a **re-export of work that already exists**, and this is it.

    Four things here are the decision rather than plumbing.

    **It is a script and not a route.** `POST /jobs/{id}/export` would widen guidelines §12's
    fixed endpoint surface, and add an authorization question - reviewer? submitter? a third
    role? - for an event that needs S3 to be unavailable for roughly twenty seconds at the
    moment a reviewer approves. Who may run this is answered by who holds the worker's
    credentials: a database write and the bucket prefix, strictly less than `reviewer`. ADR
    0009 decision 6 records what would change that answer.

    **`--actor` is mandatory, and that is the whole accountability story.**
    `audit_events.actor` has a CHECK refusing `unknown` and blank precisely so a row cannot say
    a machine did something a person did. The `export_attempted` and `export_result` rows this
    writes carry the operator's identity, so a recovered export is exactly as auditable as an
    original one.

    **It never rewrites the job's status.** The job stays `failed`, forever, with a
    downloadable artifact - which reads like a contradiction and is not: `GET /jobs/{id}/report`
    keys on `exported_at` rather than on the status (ADR 0009 decision 3), so the artifact is
    reachable without anyone editing history. The job did fail at export; the artifact was
    recovered afterwards. `finish_job` is never called from here.

    **It constructs no graph, no `LLMClient` and no tool, and needs no Redis.** There is
    nothing to decide and nothing to fetch: the body is a row, the write is one `PutObject`,
    and the stamp is one `UPDATE`. `tests/test_reexport_job.py` asserts that by reading this
    module's imports rather than by inspection, because "it does not build a graph" is a
    statement about what it can reach, not about what it happens to call.

WHO CALLS IT
    A person holding the worker's credentials:

        python scripts/reexport_job.py <job_id> --actor alice@example.com

    It needs `DATABASE_URL`, `S3_BUCKET`, `AWS_REGION`, and `AWS_ENDPOINT_URL` against
    LocalStack. It needs no LLM key, no Tavily key, no queue and no Redis.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from sqlalchemy.engine import Engine

from artifacts import ArtifactError, ArtifactStore, build_artifact_store, object_key
from config import Config, load_config, required
from database import queries
from database.queries import create_database_engine

logger = logging.getLogger(__name__)

RECOVERABLE = (
    "ADR 0009 decision 1's recoverable set is `report_json IS NOT NULL AND exported_at IS NULL`"
)
"""Why a refusal below is a refusal. Stated once, so both refusal messages can name it."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The two arguments, with `--actor` required rather than defaulted.

    Argparse enforcing it is deliberate: a default would be a machine identity for something a
    person did, and the `ck_audit_events_actor` CHECK exists to make that impossible one layer
    further down. Failing here means failing before anything is read, let alone written.
    """
    parser = argparse.ArgumentParser(
        prog="reexport_job",
        description="Re-export an approved report whose artifact write was exhausted (ADR 0009).",
    )
    parser.add_argument("job_id", help="The job whose artifact is missing.")
    parser.add_argument(
        "--actor",
        required=True,
        help="Who is running this. Written to audit_events.actor; blank and 'unknown' are refused.",
    )
    return parser.parse_args(argv)


def reexport(engine: Engine, artifacts: ArtifactStore, *, job_id: str, actor: str) -> int:
    """Re-run the artifact write for one job. 0 when the artifact now exists.

    The order is the export node's, minus everything that is not the artifact:

        export_attempted (the operator)  ->  PutObject  ->  exported_at + export_result

    `exported_at` is stamped strictly after the write returns, for the reason ADR 0009 decision
    1 gives: the column means "the object exists", and a stamp that ran first would be a claim
    about something that had not happened yet.
    """
    job = queries.read_job(engine, job_id)
    if job is None:
        logger.error("no job %s", job_id)
        return 1

    if job.report_json is None:
        # Nothing was ever approved and stored, so there is nothing to re-export. Re-running
        # the pipeline is emphatically not this script's job (ARCHITECTURE.md §8).
        logger.error("job %s has no stored report to re-export. %s", job_id, RECOVERABLE)
        return 1

    if job.exported_at is not None:
        logger.error(
            "job %s was exported at %s; its artifact already exists. %s",
            job_id,
            job.exported_at,
            RECOVERABLE,
        )
        return 1

    logger.info(
        "job %s is %s with a stored report and no artifact; re-exporting as %s",
        job_id,
        job.status,
        actor,
    )
    report: dict[str, Any] = job.report_json
    queries.record_export_attempt(
        engine, job_id=job_id, actor=actor, claims_checked=len(report.get("claims", []))
    )

    try:
        key = artifacts.put_report(job_id, report)
    except ArtifactError:
        # Recorded under the operator, so a recovery that also failed leaves a row rather than
        # only a line on somebody's terminal. Nothing about the job changes: it was already
        # terminal, and it stays exactly as terminal as it was.
        logger.exception("job %s: the re-export write was exhausted", job_id)
        queries.record_artifact_failed(engine, job_id=job_id, actor=actor, key=object_key(job_id))
        return 1

    queries.record_artifact_written(engine, job_id=job_id, actor=actor, key=key)
    logger.info(
        "job %s: artifact recovered at %s. The job stays %s (ADR 0009 decision 4)",
        job_id,
        key,
        job.status,
    )
    return 0


def build(config: Config) -> tuple[Engine, ArtifactStore]:
    """The two things this script talks to, and nothing else.

    No queue, no Redis, no checkpointer, no graph. The narrow set is the point: the failure
    being recovered is a storage error, and everything that could re-bill a model is absent
    from this process by construction.
    """
    engine = create_database_engine(required(config.database_url, "DATABASE_URL"))
    artifacts = build_artifact_store(
        required(config.s3_bucket, "S3_BUCKET"),
        region=config.aws_region,
        endpoint_url=config.aws_endpoint_url,
    )
    return engine, artifacts


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config()
    logging.basicConfig(level=config.log_level, format="%(levelname)s %(message)s")

    engine, artifacts = build(config)
    try:
        return reexport(engine, artifacts, job_id=args.job_id, actor=args.actor)
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
