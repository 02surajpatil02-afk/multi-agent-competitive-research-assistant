"""
WHY THIS FILE EXISTS
    The one place that talks to SQS. Everything above it - the API that enqueues and the worker
    that consumes - deals in `JobMessage`, never in a boto3 response shape, for the same reason
    `tools/` is one boundary rather than a call scattered through the agents: argument
    validation, the message contract, and the FIFO attributes have one home.

    Four things here are decisions rather than plumbing.

    **The message is a pointer and never state** (ADR 0010 decision 3, §20 row 8). Three
    identifiers go on the wire and nothing else - no question text, no reviewer note, no report,
    no graph state. The worker reads everything else from Postgres and the checkpoint, which is
    what makes a redelivery a resume rather than a restart, and what keeps untrusted user text
    out of the queue.

    **`attempt` is deliberately absent.** §11 and gl §12 both put it in the body; an SQS body is
    immutable once sent, so a field inside it cannot count redeliveries. The number that can is
    `ApproximateReceiveCount`, read at receive time, which is what `JobMessage.receive_count`
    carries.

    **FIFO is load-bearing, not a preference** (ADR 0010 decision 4). `MessageGroupId = job_id`
    means two messages for one job are never processed at once, whatever mix of starts, resumes,
    retries and redeliveries produced them - which is exactly the single-writer precondition
    ADR 0005's `_write_findings` is allowed to assume. A change to a standard queue would break
    it silently, so `is_fifo()` exists and the worker refuses to start without it.

    **The deduplication id is the gate-visit key, again.** A start message dedupes on the job's
    `idempotency_key`; a resume dedupes on `f"{job_id}:{calls_used}"` - ADR 0007's visit key. So
    a reviewer retrying the same decision inside SQS's five-minute window produces one message,
    which is the retry semantics ADR 0007 invariant 2 already asks for. One key, three places.

WHO CALLS IT
    routes/api.py enqueues; worker.py receives, deletes, and checks the queue's attributes at
    startup. Nothing else imports boto3.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

LONG_POLL_SECONDS = 20
"""The maximum SQS allows. One request covers twenty seconds of an empty queue instead of
twenty requests that each answer "nothing" (ADR 0010 decision 6)."""

MESSAGES_PER_RECEIVE = 1
"""One worker, one job (ARCHITECTURE.md §11's worker table). Asking for more would hold a
second message invisible while the first runs for twenty minutes."""


class QueueError(RuntimeError):
    """A send or receive that did not happen.

    It exists so callers do not have to catch `ClientError` and `BotoCoreError` separately, and
    so `POST /jobs` can turn "the row is committed and the message is not" into ADR 0010
    decision 10's `503 enqueue_failed` without importing botocore.
    """


@dataclass(frozen=True)
class JobMessage:
    """One pointer message, plus what SQS says about this delivery of it.

    The three identifiers are the body (ADR 0010 decision 3). `user_id` and `idempotency_key`
    are redundant against the `jobs` row the worker reads anyway; they are kept because they let
    a log line identify a message without a database read, which is the state you are in when
    the database is the thing that is broken.

    `receipt_handle` and `receive_count` are **not** in the body. They belong to a delivery
    rather than to a job: the handle is what `delete()` needs, and the count is what tells the
    worker this is the final delivery before the dead-letter queue.
    """

    job_id: str
    user_id: str
    idempotency_key: str
    receipt_handle: str
    receive_count: int

    def body(self) -> dict[str, str]:
        """Exactly what goes on the wire. Identifiers only."""
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "idempotency_key": self.idempotency_key,
        }


class JobQueue:
    """The SQS job queue, as the two entrypoints use it.

    A thin object rather than four module functions because it holds one boto3 client and one
    queue URL, and passing both to every call would put the same two arguments in five
    signatures.
    """

    def __init__(self, queue_url: str, *, client: Any) -> None:
        self._url = queue_url
        self._client = client

    def send_start(self, *, job_id: str, user_id: str, idempotency_key: str) -> None:
        """Enqueue a new job.

        Deduplicated on the job's `idempotency_key`, which is belt-and-braces behind the
        `UNIQUE` constraint that refuses the duplicate first (ADR 0010 decision 4).
        """
        self._send(
            body={"job_id": job_id, "user_id": user_id, "idempotency_key": idempotency_key},
            group_id=job_id,
            deduplication_id=idempotency_key,
        )

    def send_resume(
        self, *, job_id: str, user_id: str, idempotency_key: str, calls_used: int
    ) -> None:
        """Enqueue a resume after a reviewer decided.

        **The decision is not in here.** It is already durable in `audit_events`, keyed by the
        same `(job_id, calls_used)` the deduplication id uses, and the worker reads it from
        there (ADR 0011 decision 2). What travels is which job to look at.
        """
        self._send(
            body={"job_id": job_id, "user_id": user_id, "idempotency_key": idempotency_key},
            group_id=job_id,
            deduplication_id=f"{job_id}:{calls_used}",
        )

    def _send(self, *, body: dict[str, str], group_id: str, deduplication_id: str) -> None:
        """One message, with the two FIFO attributes that carry ADR 0010 decision 4.

        A failure raises `QueueError`. It must not be swallowed: the job row is already
        committed by the time this runs, so a silent failure would leave a job that says it is
        queued with nothing to pick it up (ADR 0010 decision 10).
        """
        try:
            self._client.send_message(
                QueueUrl=self._url,
                MessageBody=json.dumps(body),
                MessageGroupId=group_id,
                MessageDeduplicationId=deduplication_id,
            )
        except (ClientError, BotoCoreError) as error:
            raise QueueError(f"could not enqueue {body['job_id']}") from error

    def receive(self) -> JobMessage | None:
        """Long-poll for one message, or None when the wait ended empty.

        A receive failure raises rather than returning None: "the queue answered and had
        nothing" and "the queue could not be reached" are different facts, and a worker that
        conflated them would spin silently against a broken endpoint.
        """
        try:
            response = self._client.receive_message(
                QueueUrl=self._url,
                MaxNumberOfMessages=MESSAGES_PER_RECEIVE,
                WaitTimeSeconds=LONG_POLL_SECONDS,
                AttributeNames=["ApproximateReceiveCount"],
            )
        except (ClientError, BotoCoreError) as error:
            raise QueueError(f"could not receive from {self._url}") from error

        messages = response.get("Messages") or []
        if not messages:
            return None
        return _parse(messages[0])

    def delete(self, message: JobMessage) -> None:
        """Acknowledge one message. The worker calls this on exactly three outcomes and on
        nothing else (ADR 0010 decision 6), which is what makes redelivery the retry."""
        try:
            self._client.delete_message(QueueUrl=self._url, ReceiptHandle=message.receipt_handle)
        except (ClientError, BotoCoreError) as error:
            raise QueueError(f"could not delete a message from {self._url}") from error

    def attributes(self) -> dict[str, str]:
        """`VisibilityTimeout`, `FifoQueue` and `RedrivePolicy`, as SQS reports them.

        Read once at worker startup. ADR 0010 decision 8 derives the visibility timeout from the
        runtime bound rather than asserting it, and a derived number nothing checks is a number
        that drifts - so the worker checks it against the queue it is actually attached to.
        """
        try:
            response = self._client.get_queue_attributes(QueueUrl=self._url, AttributeNames=["All"])
        except (ClientError, BotoCoreError) as error:
            raise QueueError(f"could not read the attributes of {self._url}") from error
        return dict(response.get("Attributes") or {})


def build_queue(queue_url: str, *, region: str, endpoint_url: str | None = None) -> JobQueue:
    """The production client, and the one knob LocalStack needs.

    `endpoint_url` is None against real AWS and the LocalStack address locally. It is the only
    difference between the two, which is what makes the local integration tests worth running.

    Retries are boto3's `standard` mode at 3 attempts: a send that fails transiently should not
    become a `503` on the first packet loss, and a bound that is not stated is a bound nobody
    can check (global CLAUDE.md §7).
    """
    client = boto3.client(
        "sqs",
        region_name=region,
        endpoint_url=endpoint_url,
        config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
    )
    return JobQueue(queue_url, client=client)


def is_fifo(attributes: dict[str, str]) -> bool:
    """Whether this queue gives ADR 0005 its single writer (ADR 0010 decision 4)."""
    return attributes.get("FifoQueue") == "true"


def visibility_timeout(attributes: dict[str, str]) -> int:
    """How long one delivery stays invisible, in seconds."""
    return int(attributes.get("VisibilityTimeout", 0))


def max_receive_count(attributes: dict[str, str]) -> int | None:
    """How many deliveries before the dead-letter queue, or None if there is no redrive policy.

    None is a real answer and the worker treats it as "no delivery is ever final", because
    without a redrive policy a message is redelivered forever and there is no DLQ to finalise
    a job into.
    """
    policy = attributes.get("RedrivePolicy")
    if not policy:
        return None
    try:
        return int(json.loads(policy)["maxReceiveCount"])
    except (ValueError, KeyError, TypeError):
        logger.warning("the queue has a RedrivePolicy this cannot read: %s", policy)
        return None


def _parse(raw: dict[str, Any]) -> JobMessage:
    """One SQS message into a `JobMessage`, refusing anything that is not the documented shape.

    A body missing an identifier is a bug in whoever sent it, and failing here means the message
    is not deleted and the malformed send is visible - which is better than a worker that
    proceeds with an empty `job_id`.
    """
    try:
        body = json.loads(raw["Body"])
    except (ValueError, KeyError) as error:
        raise QueueError("a queue message body is not JSON") from error

    missing = [field for field in ("job_id", "user_id", "idempotency_key") if not body.get(field)]
    if missing:
        raise QueueError(f"a queue message is missing {', '.join(missing)}")

    attributes = raw.get("Attributes") or {}
    return JobMessage(
        job_id=str(body["job_id"]),
        user_id=str(body["user_id"]),
        idempotency_key=str(body["idempotency_key"]),
        receipt_handle=str(raw["ReceiptHandle"]),
        # 1 rather than 0 when SQS does not say: the first delivery is delivery one, and
        # guessing low here would only ever make the worker treat a final delivery as ordinary.
        receive_count=int(attributes.get("ApproximateReceiveCount", 1)),
    )
