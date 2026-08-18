"""
WHY THIS FILE EXISTS
    `artifacts.py` is the one place that talks to S3, and three of the things it owns are
    decisions rather than plumbing: the object key, the retry schedule, and the presigned
    URL's lifetime. Each is asserted here against the real `ArtifactStore` over a scripted
    client, the way `tests/test_llm_client.py` asserts the LLM schedules against the real
    `LLMClient` over a scripted `FakeOpenAI`.

    **The retry schedule gets the same treatment guidelines §17's other rows already get**:
    the attempt count and the delays, not "it retried". ADR 0009 sizes the whole
    `export_write_failed` failure against exactly three attempts over roughly twenty seconds,
    and its shipping conditions ask for that to be asserted rather than assumed - because a
    schedule nothing checks is a schedule that widens quietly, and a widened one changes how
    often an operator has to run the recovery script.

    **The key is worth a test because a re-export depends on it.** `reports/{job_id}.json` is
    derived from the job id alone, so the recovery path writes the object the first attempt was
    aiming at rather than a second copy beside it. A key that carried a timestamp would look
    harmless and would make every recovery leave litter.

WHO CALLS IT
    pytest. No service, no network, no AWS.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fakes import FakeS3, s3_error

import artifacts as artifacts_module
from artifacts import (
    OPERATION_TIMEOUT_S,
    PRESIGNED_URL_TTL_S,
    WRITE_BACKOFF_S,
    ArtifactError,
    ArtifactStore,
    build_artifact_store,
    object_key,
)

_BUCKET = "research-reports"
_JOB = "3f1d0c9a-1111-4111-8111-111111111111"
_REPORT: dict[str, Any] = {
    "sections": [{"id": "sec1", "heading": "Cloud", "body": "Both firms grew."}],
    "claims": [
        {"claim_id": "c1", "section_id": "sec1", "text": "TCS grew.", "finding_ids": ["f1"]}
    ],
    "sources": [{"url": "https://example.com/f1", "title": "Annual report", "finding_ids": ["f1"]}],
}


@pytest.fixture
def waited(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The backoff, recorded rather than waited. The schedule is the assertion, so it has to
    be observable; sleeping through it would only make the suite ten seconds slower."""
    recorded: list[float] = []
    monkeypatch.setattr(artifacts_module, "sleep", recorded.append)
    return recorded


def _store(*script: object) -> tuple[ArtifactStore, FakeS3]:
    client = FakeS3(script=list(script))
    return ArtifactStore(_BUCKET, client=cast(Any, client)), client


# --- 1. The object key ----------------------------------------------------------------


def test_the_key_is_derived_from_the_job_id_alone() -> None:
    # Deterministic, so a re-export overwrites the object the first attempt was aiming at
    # rather than accumulating a second copy of one approved body (ADR 0009 decision 4).
    assert object_key(_JOB) == f"reports/{_JOB}.json"
    assert object_key(_JOB) == object_key(_JOB)


def test_the_write_lands_under_that_key_with_the_report_as_json(waited: list[float]) -> None:
    store, client = _store()

    key = store.put_report(_JOB, _REPORT)

    assert key == f"reports/{_JOB}.json"
    assert client.body(key) == _REPORT
    assert client.puts[0]["ContentType"] == "application/json"
    assert client.puts[0]["Bucket"] == _BUCKET
    assert waited == []


# --- 2. The retry schedule -------------------------------------------------------------


def test_a_transient_failure_is_retried_on_the_documented_schedule(waited: list[float]) -> None:
    store, client = _store(s3_error())

    store.put_report(_JOB, _REPORT)

    assert len(client.puts) == 2
    assert waited == [2.0]


def test_the_last_of_three_attempts_still_counts(waited: list[float]) -> None:
    store, client = _store(s3_error(), s3_error())

    store.put_report(_JOB, _REPORT)

    assert len(client.puts) == 3
    assert waited == [2.0, 8.0]
    assert client.body(object_key(_JOB)) == _REPORT


def test_three_failures_exhaust_the_write_and_raise(waited: list[float]) -> None:
    """ADR 0009's second shipping condition: the schedule asserted on attempts and delays.

    Three attempts and no fourth. Widening this is what would make `export_write_failed` rarer
    and the recovery script less necessary - which is a decision, not an implementation
    detail, and it is one ADR 0009 already took.
    """
    store, client = _store(s3_error(), s3_error(), s3_error())

    with pytest.raises(ArtifactError, match="after 3 attempts"):
        store.put_report(_JOB, _REPORT)

    assert len(client.puts) == len(WRITE_BACKOFF_S) + 1 == 3
    assert waited == [2.0, 8.0]
    assert client.objects == {}


def test_an_exhausted_write_keeps_the_underlying_error_as_its_cause(waited: list[float]) -> None:
    # `raise ... from error`, so the botocore code survives into the log without any caller
    # having to import botocore to read it.
    store, _ = _store(s3_error("SlowDown"), s3_error("SlowDown"), s3_error("SlowDown"))

    with pytest.raises(ArtifactError) as caught:
        store.put_report(_JOB, _REPORT)

    assert "SlowDown" in str(caught.value.__cause__)


# --- 3. Presigning ---------------------------------------------------------------------


def test_presigning_asks_for_a_fifteen_minute_url() -> None:
    # guidelines §12's documented lifetime, in the call rather than only in a constant.
    store, client = _store()

    url, expires_at = store.presign(_JOB)

    assert client.presigns[0]["operation"] == "get_object"
    assert client.presigns[0]["ExpiresIn"] == PRESIGNED_URL_TTL_S == 900
    assert client.presigns[0]["Params"] == {"Bucket": _BUCKET, "Key": object_key(_JOB)}
    assert object_key(_JOB) in url
    assert 0 < (expires_at - datetime.now(UTC)).total_seconds() <= PRESIGNED_URL_TTL_S


def test_presigning_reaches_nothing_and_writes_nothing() -> None:
    """It signs locally, which is what lets the API hand out a URL without holding report
    bytes - and what makes it safe on a request path that otherwise has no network call.

    It also does not check that the object is there: `jobs.exported_at` is the durable answer
    to that since ADR 0009 decision 1, and a `HeadObject` here would be a slower, racier second
    copy of a fact the row already carries.
    """
    store, client = _store()

    store.presign(_JOB)

    assert client.puts == []
    assert client.objects == {}


def test_a_presign_that_fails_is_an_artifact_error() -> None:
    store, _ = _store(s3_error())

    with pytest.raises(ArtifactError, match="could not presign"):
        store.presign(_JOB)


# --- 4. The client the two entrypoints build -------------------------------------------


def test_the_built_client_carries_the_documented_timeout_and_no_retries_of_its_own() -> None:
    """boto3's own retries are off, and that is the point rather than tidiness.

    Left at `standard`, botocore would retry underneath `put_report`'s schedule and turn three
    attempts into nine, on a timetable nothing in guidelines §17 describes. One retry layer,
    written down once - the same argument `llm_client` makes about the OpenAI SDK's.
    """
    store = build_artifact_store(_BUCKET, region="ap-south-1", endpoint_url="http://localhost:4566")
    config = store._client.meta.config  # noqa: SLF001 - the configuration is what is under test

    assert config.connect_timeout == config.read_timeout == OPERATION_TIMEOUT_S == 10.0
    assert config.retries["total_max_attempts"] == 1
    assert store.bucket == _BUCKET


def test_the_endpoint_override_is_the_only_difference_from_aws() -> None:
    # What makes the `integration`-marked S3 tests worth running: the same client and the same
    # calls, at a different address.
    against_aws = build_artifact_store(_BUCKET, region="ap-south-1")
    against_localstack = build_artifact_store(
        _BUCKET, region="ap-south-1", endpoint_url="http://localhost:4566"
    )

    assert "localhost:4566" in str(against_localstack._client.meta.endpoint_url)  # noqa: SLF001
    assert "amazonaws.com" in str(against_aws._client.meta.endpoint_url)  # noqa: SLF001
