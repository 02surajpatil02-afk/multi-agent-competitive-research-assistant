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
    orders starts, resumes, retries and redeliveries for one job. FIFO cannot prevent an expired
    in-flight delivery from overlapping its redelivery, so ADR 0016's PostgreSQL execution lock is
    the final enforcement of ADR 0005's single-writer precondition. A standard queue would still
    discard the intended per-job ordering, so `is_fifo()` exists and the worker refuses it.

    **The deduplication id is the gate-visit key, again.** A start message dedupes on the job's
    `idempotency_key`; a resume dedupes on `f"{job_id}:{calls_used}"` - ADR 0007's visit key. So
    a reviewer retrying the same decision inside SQS's five-minute window produces one message,
    which is the retry semantics ADR 0007 invariant 2 already asks for. One key, three places.

WHO CALLS IT
    routes/api.py enqueues; worker.py receives, renews visibility, deletes, and checks the
    queue's attributes at startup. Since Phase 5 block C, operations.py and the three operator
    scripts read the dead-letter queue through `receive_batch`/`release`, resolve it from this
    queue's own redrive policy, and put one message back with `resend` - which is why those
    three operations live here rather than in a tool. Nothing else imports boto3.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

LONG_POLL_SECONDS = 20
"""The maximum SQS allows. One request covers twenty seconds of an empty queue instead of
twenty requests that each answer "nothing" (ADR 0010 decision 6)."""

SQS_CONNECT_TIMEOUT_S = 5
SQS_READ_TIMEOUT_S = LONG_POLL_SECONDS + 5
SQS_TOTAL_ATTEMPTS = 3
"""Finite queue-client waits, including visibility renewal.

The read timeout has to exceed the legitimate 20-second long poll; five seconds is the network
margin already used for other infrastructure calls. Three total attempts preserve the existing
retry policy while making its meaning unambiguous to botocore.
"""

SQS_STANDARD_MAX_BACKOFF_S = 20
"""Botocore standard mode's truncated exponential-backoff ceiling.

For three total attempts the two possible sleeps are bounded by one and two seconds; the 20-second
ceiling is recorded here so increasing the attempt count cannot silently invalidate the renewal-call
budget derived below.  Standard mode does not consume a service ``Retry-After`` header.
"""


def sqs_call_envelope_seconds() -> float:
    """Conservative wall-clock budget for one boto SQS operation.

    Each attempt may spend both its connect and read timeout.  Between attempts botocore standard
    mode uses full-jitter ``min(2**attempt, 20)``; summing each maximum gives a deterministic upper
    envelope suitable for deciding whether another visibility renewal can still finish safely.
    """
    retry_backoff = sum(
        min(2**attempt, SQS_STANDARD_MAX_BACKOFF_S) for attempt in range(SQS_TOTAL_ATTEMPTS - 1)
    )
    return float(SQS_TOTAL_ATTEMPTS * (SQS_CONNECT_TIMEOUT_S + SQS_READ_TIMEOUT_S) + retry_backoff)


MESSAGES_PER_RECEIVE = 1
"""One worker, one job (ARCHITECTURE.md §11's worker table). Asking for more would hold a
second message invisible while the first runs for twenty minutes."""


class QueueError(RuntimeError):
    """A send or receive that did not happen.

    It exists so callers do not have to catch `ClientError` and `BotoCoreError` separately, and
    so `POST /jobs` can turn "the row is committed and the message is not" into ADR 0010
    decision 10's `503 enqueue_failed` without importing botocore.
    """


class OwnershipLost(QueueError):
    """SQS says this receipt handle no longer owns an in-flight delivery.

    A transient service failure and an invalid receipt handle have different recovery rules. The
    former may be retried while the current visibility window remains; the latter means another
    consumer may already own the delivery, so the worker must stop at its next durable checkpoint.
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
    group_id: str | None = None
    deduplication_id: str | None = None
    """The two FIFO attributes SQS assigned when the message was sent, when they were asked for.

    `receive()` does not ask for them: the worker routes on durable state and has no use for
    either. `receive_batch()` does, because Phase 5 block C's dead-letter replay puts a message
    back on the job queue **as the message it already was** - the same `MessageGroupId` and the
    same `MessageDeduplicationId` - rather than minting a new one and losing ADR 0010 decision
    4's ordering and ADR 0007's gate-visit key with it.

    None means "not asked for", which is why `resend` refuses rather than inventing a default.
    """

    received_at_monotonic: float = 0.0
    """Conservative start of the receive call that created this receipt.

    SQS starts visibility no later than the response.  Timing from before ``ReceiveMessage`` may
    underestimate the available lease (especially during a long poll), but can never overestimate
    it.  Directly constructed test messages use ``0`` and the worker substitutes its own current
    monotonic time.
    """

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
        receive_started_at = time.monotonic()
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
        return _parse(messages[0], received_at_monotonic=receive_started_at)

    def delete(self, message: JobMessage) -> None:
        """Acknowledge one message. The worker calls this on exactly three outcomes and on
        nothing else (ADR 0010 decision 6), which is what makes redelivery the retry."""
        try:
            self._client.delete_message(QueueUrl=self._url, ReceiptHandle=message.receipt_handle)
        except (ClientError, BotoCoreError) as error:
            raise QueueError(f"could not delete a message from {self._url}") from error

    def receive_batch(
        self, *, max_messages: int, wait_seconds: int, visibility_timeout_s: int
    ) -> list[JobMessage]:
        """Read up to `max_messages` in one call, carrying their FIFO attributes.

        **This exists for the dead-letter queue, and `receive()` stays exactly as it was.** The
        worker takes one message at a time and routes on durable state; an operator inspecting a
        DLQ needs to see what is in it, and a replay needs the group and deduplication ids the
        message was sent with (ADR 0021 decision 6).

        `visibility_timeout_s` is the caller's choice because the two callers want opposite
        things. Inspection asks for a short window and calls `release()` immediately, so a
        read-only look does not hide the queue's contents from the next person. A replay asks
        for a window long enough to check durable state and act inside it.

        SQS has no peek: reading is what makes a message invisible, so "inspect without
        consuming" is a read followed by a release rather than a different operation. Nothing
        here deletes anything.

        A message whose body is not the documented shape raises, exactly as `receive()` does -
        the shape is this repository's own, and a malformed one is a bug in whoever sent it.
        """
        try:
            response = self._client.receive_message(
                QueueUrl=self._url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_seconds,
                VisibilityTimeout=visibility_timeout_s,
                AttributeNames=[
                    "ApproximateReceiveCount",
                    "MessageGroupId",
                    "MessageDeduplicationId",
                ],
            )
        except (ClientError, BotoCoreError) as error:
            raise QueueError(f"could not receive from {self._url}") from error

        received_at = time.monotonic()
        return [
            _parse(raw, received_at_monotonic=received_at)
            for raw in (response.get("Messages") or [])
        ]

    def release(self, message: JobMessage) -> None:
        """Make a message visible again immediately, without deleting it.

        Setting the visibility timeout to zero is the documented way to hand a delivery back.
        It is what turns `receive_batch` into an inspection: the operator has seen the message
        and the queue still has it, so the next person - and the alarm - see the same thing.
        """
        self.extend_visibility(message, visibility_timeout_s=0)

    def resend(self, message: JobMessage) -> None:
        """Put a message from another queue onto this one, unchanged.

        **The message that goes back is the message that came out** (ADR 0021 decision 6): the
        same three identifiers, the same `MessageGroupId`, and the same `MessageDeduplicationId`
        it was originally sent with. Minting a new deduplication id would break ADR 0007's
        gate-visit key, under which a resume for one visit is one message however many times it
        is sent; keeping it means a replay is the retry SQS would have performed itself.

        The five-minute deduplication window is not a hazard here, and it is worth saying why: a
        message reaches a dead-letter queue only after three failed deliveries, so its original
        send is far outside that window and this one is accepted rather than silently dropped.

        A message read without its FIFO attributes cannot be resent faithfully, so it is refused
        rather than guessed at.
        """
        if not message.group_id or not message.deduplication_id:
            raise QueueError(
                f"the message for job {message.job_id} was read without its FIFO attributes"
            )
        self._send(
            body=message.body(),
            group_id=message.group_id,
            deduplication_id=message.deduplication_id,
        )

    def dead_letter_queue(self) -> JobQueue | None:
        """The queue this one redrives to, as a `JobQueue`, or None when there is no policy.

        Resolved from this queue's own `RedrivePolicy` rather than from a second environment
        variable, for the reason ADR 0015 gives about the visibility window: a duplicate setting
        is a setting that can disagree with the queue it describes, and an operator pointed at
        the wrong dead-letter queue would draw conclusions from someone else's messages.

        It shares this queue's boto3 client, which is what keeps `jobqueue.py` the one place
        that constructs one - the caller receives a queue object rather than a URL it would have
        to build a client around.
        """
        arn = dead_letter_target_arn(self.attributes())
        if arn is None:
            return None
        try:
            response = self._client.get_queue_url(QueueName=arn.rsplit(":", 1)[-1])
        except (ClientError, BotoCoreError) as error:
            raise QueueError(f"could not resolve the dead-letter queue of {self._url}") from error
        return JobQueue(str(response["QueueUrl"]), client=self._client)

    def extend_visibility(self, message: JobMessage, *, visibility_timeout_s: int) -> None:
        """Renew this delivery's visibility lease without exposing boto3 above this boundary.

        SQS measures the new timeout from this call. An invalid or no-longer-in-flight receipt
        handle is ownership loss, not a transient renewal failure; callers must not keep acting as
        the exclusive worker in that case.
        """
        try:
            self._client.change_message_visibility(
                QueueUrl=self._url,
                ReceiptHandle=message.receipt_handle,
                VisibilityTimeout=visibility_timeout_s,
            )
        except ClientError as error:
            detail = error.response.get("Error") or {}
            code = str(detail.get("Code") or "")
            description = str(detail.get("Message") or "").lower()
            invalid_parameter_handle = code == "InvalidParameterValue" and "receipt" in description
            if code in {"ReceiptHandleIsInvalid", "MessageNotInflight"} or invalid_parameter_handle:
                raise OwnershipLost(
                    f"the queue no longer recognises the delivery for job {message.job_id}"
                ) from error
            raise QueueError(f"could not renew visibility for job {message.job_id}") from error
        except BotoCoreError as error:
            raise QueueError(f"could not renew visibility for job {message.job_id}") from error

    def attributes(self) -> dict[str, str]:
        """`VisibilityTimeout`, `FifoQueue` and `RedrivePolicy`, as SQS reports them.

        Read once at worker startup. The visibility value is the lease duration the heartbeat
        renews, so cadence is derived from the queue the worker is actually attached to rather than
        from a duplicate environment variable (ADR 0015).
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

    Retries are boto3's `standard` mode at 3 total attempts. Connects are bounded at 5 seconds;
    reads at 25 seconds so the legitimate 20-second long poll still fits. These same finite bounds
    cover visibility renewal, so stopping a lease cannot wait on an unbounded SDK operation.
    """
    client = boto3.client(
        "sqs",
        region_name=region,
        endpoint_url=endpoint_url,
        config=BotoConfig(
            connect_timeout=SQS_CONNECT_TIMEOUT_S,
            read_timeout=SQS_READ_TIMEOUT_S,
            retries={"total_max_attempts": SQS_TOTAL_ATTEMPTS, "mode": "standard"},
        ),
    )
    return JobQueue(queue_url, client=client)


def is_fifo(attributes: dict[str, str]) -> bool:
    """Whether this queue preserves ADR 0010's per-job message ordering."""
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


def dead_letter_target_arn(attributes: dict[str, str]) -> str | None:
    """Where a message goes after `maxReceiveCount` deliveries, or None if nowhere.

    The companion of `max_receive_count`, reading the other half of the same policy. None means
    the queue redelivers forever, which is what the worker already treats as "no delivery is
    ever final" - and it also means there is no dead-letter queue for an operator to inspect.
    """
    policy = attributes.get("RedrivePolicy")
    if not policy:
        return None
    try:
        return str(json.loads(policy)["deadLetterTargetArn"])
    except (ValueError, KeyError, TypeError):
        logger.warning("the queue has a RedrivePolicy this cannot read: %s", policy)
        return None


def _parse(raw: dict[str, Any], *, received_at_monotonic: float = 0.0) -> JobMessage:
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
        # None when the caller did not ask for them, which is `receive()`'s case. `resend`
        # refuses a message carrying neither rather than inventing one.
        group_id=attributes.get("MessageGroupId"),
        deduplication_id=attributes.get("MessageDeduplicationId"),
        received_at_monotonic=received_at_monotonic,
    )
