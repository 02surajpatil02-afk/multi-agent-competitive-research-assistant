"""
WHY THIS FILE EXISTS
    The one place that talks to S3, the way `jobqueue.py` is the one place that talks to SQS
    and `redisstore.py` the one place that talks to Redis. The export node writes an artifact,
    the API presigns one, and the operator's re-export script writes one - and none of the
    three imports boto3 or knows what an object key looks like.

    Four things here are decisions rather than plumbing.

    **The object key is derived from the job id and nothing else**: `reports/{job_id}.json`.
    One job has one artifact, so a re-export overwrites rather than accumulating a second copy
    of the same approved body, and "which object is this job's?" is arithmetic rather than a
    lookup. That matters for the recovery path ADR 0009 decision 4 defines: the script re-runs
    the write against the same key, so running it twice is the same as running it once.

    **The retry schedule is this module's, not the caller's** - 10s per attempt, two retries at
    2s and 8s (guidelines §17's artifact row, ADR 0009 decision 1). It lives here because there
    are two callers, the export node and the re-export script, and a schedule copied into both
    is a schedule that drifts in one of them. boto3's own retries are switched off for the same
    reason `llm_client.py` switches off the OpenAI SDK's: the schedules in guidelines §17 are
    the only ones allowed to run, and two retry layers multiply into nine attempts nobody wrote
    down.

    **Exhaustion raises, and the caller decides what that means.** `ArtifactError` carries no
    failure vocabulary of its own - `export_write_failed` is a *job* outcome and belongs with
    the other reasons in the graph, exactly as `redisstore.RedisRateLimiter` answers `False`
    and leaves `rate_limiter_unavailable` to `llm_client`.

    **Presigning is not a write and gets no retry.** `generate_presigned_url` signs locally and
    reaches nothing, which is what lets the API hand out a URL without ever holding report
    bytes (ARCHITECTURE.md §8, guidelines §12) - and what makes it safe on a request path.

WHO CALLS IT
    `worker.py` builds one and the export node writes through it; `app.py` builds one and
    `GET /jobs/{id}/report` presigns through it; `scripts/reexport_job.py` builds one to
    recover an artifact whose first write was exhausted. Nothing else imports boto3 for S3.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from time import sleep
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

OPERATION_TIMEOUT_S = 10.0
"""How long one `PutObject` may take (guidelines §17's artifact row, ADR 0009 decision 1).

Applied to the connect and the read separately, because botocore has no single call bound: a
connect that hangs and a response that never finishes are different failures with the same
consequence, and leaving either unset means the operating system's default decides how long a
job waits.
"""

WRITE_BACKOFF_S: tuple[float, ...] = (2.0, 8.0)
"""guidelines §17: two retries, at 2s and 8s. The list length is the retry count, which is the
same shape `llm_client._TRANSPORT_BACKOFF_S` uses - a schedule and its count cannot disagree
when one is read off the other.

**Not widened on the way to production.** ADR 0009 sizes the whole failure against it: three
attempts over roughly twenty seconds is what S3 has to miss for a job to lose its artifact,
and the recovery for that is an operator's re-export rather than a fourth attempt.
"""

PRESIGNED_URL_TTL_S = 15 * 60
"""guidelines §12's `GET /jobs/{id}/report`: a 15-minute presigned URL. Long enough to start a
download on a slow connection, short enough that a URL in someone's shell history stops
working the same afternoon."""


class ArtifactError(RuntimeError):
    """A write or a presign that did not happen.

    It exists so callers do not catch `ClientError` and `BotoCoreError` separately, and so the
    export node can turn an exhausted write into `export_write_failed` without importing
    botocore - the same job `jobqueue.QueueError` does for the queue.
    """


def object_key(job_id: str) -> str:
    """Where this job's artifact lives in the bucket. Deterministic, and derived from the job
    id alone, so a re-export writes the object the first attempt was going to write."""
    return f"reports/{job_id}.json"


class ArtifactStore:
    """The report bucket, as the three callers use it.

    A thin object rather than module functions for the reason `JobQueue` is one: it holds a
    boto3 client and a bucket name, and passing both to every call would put the same two
    arguments in three signatures.
    """

    def __init__(self, bucket: str, *, client: Any) -> None:
        self._bucket = bucket
        self._client = client

    @property
    def bucket(self) -> str:
        return self._bucket

    def put_report(self, job_id: str, report: Mapping[str, Any]) -> str:
        """Write the approved body, and answer the key it landed under.

        Three attempts on the schedule above, then `ArtifactError`. The body is the same JSON
        that is already durable in `jobs.report_json`, so this call is a projection of a row
        rather than the only copy of anything - which is what makes ADR 0009's recovery a
        re-export rather than a re-run.
        """
        key = object_key(job_id)
        body = json.dumps(report).encode("utf-8")

        for attempt, delay in enumerate((*WRITE_BACKOFF_S, None), start=1):
            try:
                self._client.put_object(
                    Bucket=self._bucket, Key=key, Body=body, ContentType="application/json"
                )
            except (ClientError, BotoCoreError) as error:
                if delay is None:
                    raise ArtifactError(
                        f"could not write {key} after {attempt} attempts"
                    ) from error
                logger.warning(
                    "job %s: artifact write attempt %d failed (%s), retrying in %.1fs",
                    job_id,
                    attempt,
                    error,
                    delay,
                )
                sleep(delay)
            else:
                logger.info("job %s: artifact written to s3://%s/%s", job_id, self._bucket, key)
                return key

        raise AssertionError("unreachable: the last attempt either returns or raises")

    def presign(self, job_id: str) -> tuple[str, datetime]:
        """A time-limited URL for this job's artifact, and when it stops working.

        **It does not check that the object is there**, and the caller must not ask it to.
        `jobs.exported_at` is the durable answer to "does the artifact exist?" since ADR 0009
        decision 1, so a `HeadObject` here would be a second, slower, racier copy of a fact the
        row already carries - and it would put a network call on a request path that otherwise
        has none.
        """
        try:
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": object_key(job_id)},
                ExpiresIn=PRESIGNED_URL_TTL_S,
            )
        except (ClientError, BotoCoreError) as error:
            raise ArtifactError(f"could not presign the artifact for {job_id}") from error
        return str(url), datetime.now(UTC) + timedelta(seconds=PRESIGNED_URL_TTL_S)


def build_artifact_store(
    bucket: str, *, region: str, endpoint_url: str | None = None
) -> ArtifactStore:
    """The production client, and the one knob LocalStack needs.

    `endpoint_url` is None against real AWS and the LocalStack address locally - the only
    difference between the two, which is what makes the `integration`-marked S3 tests worth
    running: the same client, the same calls, a different address.

    **boto3's own retries are off** (`total_max_attempts=1`). Left on, its `standard` mode
    would retry underneath `put_report`'s schedule and turn three attempts into nine, on a
    timetable nothing in guidelines §17 describes. One retry layer, written down once.
    """
    client = boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url,
        config=BotoConfig(
            connect_timeout=OPERATION_TIMEOUT_S,
            read_timeout=OPERATION_TIMEOUT_S,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )
    return ArtifactStore(bucket, client=client)
