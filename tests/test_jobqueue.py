"""Offline tests for SQS operations that belong behind the JobQueue boundary."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from jobqueue import (
    SQS_CONNECT_TIMEOUT_S,
    SQS_READ_TIMEOUT_S,
    SQS_TOTAL_ATTEMPTS,
    JobMessage,
    JobQueue,
    OwnershipLost,
    QueueError,
    build_queue,
    sqs_call_envelope_seconds,
)


class _Client:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[dict[str, Any]] = []

    def change_message_visibility(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure


def _message() -> JobMessage:
    return JobMessage(
        job_id="11111111-1111-4111-8111-111111111111",
        user_id="22222222-2222-4222-8222-222222222222",
        idempotency_key="job-key",
        receipt_handle="secret-receipt-handle",
        receive_count=1,
    )


def _client_error(code: str, message: str = "failed") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "ChangeMessageVisibility")


def test_visibility_extension_stays_inside_the_queue_boundary() -> None:
    client = _Client()
    queue = JobQueue("https://queue.invalid/jobs.fifo", client=client)

    queue.extend_visibility(_message(), visibility_timeout_s=1800)

    assert client.calls == [
        {
            "QueueUrl": "https://queue.invalid/jobs.fifo",
            "ReceiptHandle": "secret-receipt-handle",
            "VisibilityTimeout": 1800,
        }
    ]


@pytest.mark.parametrize(
    ("code", "description"),
    [
        ("ReceiptHandleIsInvalid", "bad receipt"),
        ("MessageNotInflight", "message is not in flight"),
        ("InvalidParameterValue", "The receipt handle has expired"),
    ],
)
def test_invalid_or_expired_receipt_handles_are_ownership_loss(code: str, description: str) -> None:
    queue = JobQueue(
        "https://queue.invalid/jobs.fifo",
        client=_Client(_client_error(code, description)),
    )

    with pytest.raises(OwnershipLost, match="no longer recognises"):
        queue.extend_visibility(_message(), visibility_timeout_s=1800)


def test_a_transient_visibility_failure_remains_retryable() -> None:
    queue = JobQueue(
        "https://queue.invalid/jobs.fifo",
        client=_Client(_client_error("ServiceUnavailable")),
    )

    with pytest.raises(QueueError) as raised:
        queue.extend_visibility(_message(), visibility_timeout_s=1800)

    assert not isinstance(raised.value, OwnershipLost)
    assert "secret-receipt-handle" not in str(raised.value)


def test_queue_sdk_waits_and_attempts_are_explicitly_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    client = _Client()

    def fake_client(service: str, **kwargs: Any) -> _Client:
        captured["service"] = service
        captured.update(kwargs)
        return client

    monkeypatch.setattr("jobqueue.boto3.client", fake_client)

    queue = build_queue(
        "https://queue.invalid/jobs.fifo",
        region="ap-south-1",
        endpoint_url="http://localstack:4566",
    )

    assert queue._client is client
    config = captured["config"]
    assert config.connect_timeout == SQS_CONNECT_TIMEOUT_S == 5
    assert config.read_timeout == SQS_READ_TIMEOUT_S == 25
    assert config.retries == {
        "total_max_attempts": SQS_TOTAL_ATTEMPTS,
        "mode": "standard",
    }
    assert sqs_call_envelope_seconds() == 93


def test_receive_records_a_conservative_monotonic_lease_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReceivingClient:
        def receive_message(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Messages": [
                    {
                        "Body": ('{"job_id":"j","user_id":"u","idempotency_key":"key"}'),
                        "ReceiptHandle": "receipt",
                        "Attributes": {"ApproximateReceiveCount": "2"},
                    }
                ]
            }

    monkeypatch.setattr("jobqueue.time.monotonic", lambda: 123.5)
    message = JobQueue("https://queue.invalid/jobs.fifo", client=ReceivingClient()).receive()

    assert message is not None
    assert message.received_at_monotonic == 123.5
    assert message.receive_count == 2
