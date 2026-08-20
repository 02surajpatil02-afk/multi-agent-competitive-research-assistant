"""
WHY THIS FILE EXISTS
    Stand-ins for the external services, so retry, backoff, validation, budget, and SSRF
    logic can be tested without a network call. Guidelines §18 asks for exactly this: the
    deterministic parts of the system get deterministic tests.

    The fakes are scripted rather than clever. Each entry is either something the service
    "returns" or an exception it "raises", popped in order - which makes a test read as
    the sequence of things that went wrong, and makes an unexpected extra request an
    immediate failure rather than a silent pass.

    DNS is faked too. Without that, every SSRF test would do a real lookup, which is both
    a network call and a test whose result depends on what a public resolver says today.

    `imported_modules` is the one thing here that is not a stand-in. It reads a module's
    imports so a test can pin a boundary the type system cannot express: the five agents
    reach the web only through tools/, so `httpx` or `tavily` appearing in an agent's
    imports is itself the failure.

WHO CALLS IT
    tests/test_llm_client.py, tests/test_check_model.py, tests/test_tools_search.py,
    tests/test_tools_fetch.py, tests/test_tools_validation.py, and the five
    tests/test_agents_*.py files.
"""

from __future__ import annotations

import ast
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from threading import Lock
from types import ModuleType
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError
from openai import APIStatusError, APITimeoutError, InternalServerError, RateLimitError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

_URL = "https://example.invalid/v1/chat/completions"


@dataclass
class FakeFunction:
    name: str


@dataclass
class FakeToolCall:
    function: FakeFunction


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] | None = None


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeUsage:
    """The provider's own token counts, shaped like the block `llm_client` reads."""

    prompt_tokens: int
    completion_tokens: int


@dataclass
class FakeCompletion:
    choices: list[FakeChoice]
    usage: FakeUsage | None = None
    """Absent by default, because a real response may omit it and the client has to cope.
    A test that cares about tokens says so by passing `usage=`."""


def completion(
    content: str | None = None,
    tool_called: str | None = None,
    usage: tuple[int, int] | None = None,
) -> FakeCompletion:
    """One scripted response. `tool_called` names the tool the model chose, if any.

    `usage` is (prompt_tokens, completion_tokens) - the provider's numbers, which is where
    the telemetry's token counts come from.
    """
    tool_calls = [FakeToolCall(FakeFunction(tool_called))] if tool_called else None
    return FakeCompletion(
        [FakeChoice(FakeMessage(content=content, tool_calls=tool_calls))],
        usage=None if usage is None else FakeUsage(*usage),
    )


Router = Callable[[dict[str, Any]], Any]
"""Answers a request from the request itself, instead of from its position in a script.

Needed since ADR 0002: the Researcher's extraction calls run concurrently, so two requests
pop from a positional script in whatever order the pool happened to schedule them, and
"this page fails, that one succeeds" stops being a statement a script can make.
"""


@dataclass
class FakeCompletions:
    """Records every request it is asked to make, and answers it from the script or router.

    Concurrent callers reach this, so bookkeeping is guarded: recording the call, taking the
    next scripted answer, and the ran-out check are one critical section. Without it two
    threads can pop the same entry or both find the script empty. The lock is released
    before the answer is raised or built, so a scripted exception is still raised on the
    thread that asked for it.
    """

    script: list[Any]
    answer: Router | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def create(self, **kwargs: Any) -> Any:
        with self._lock:
            self.calls.append(kwargs)
            if self.answer is None and not self.script:
                raise AssertionError(
                    f"the client made {len(self.calls)} requests; the test scripted fewer"
                )
            scripted = None if self.answer is not None else self.script.pop(0)

        # The router runs outside the lock, on purpose: it is handed the request, and a test
        # that uses it to prove two extraction calls overlap needs both threads inside it at
        # the same time. Holding the lock across it would serialise exactly what is under test.
        chosen = self.answer(kwargs) if self.answer is not None else scripted

        if isinstance(chosen, Exception):
            raise chosen
        if isinstance(chosen, str):
            return completion(chosen)
        return chosen


@dataclass
class FakeChat:
    completions: FakeCompletions


class FakeOpenAI:
    """Shaped like the one attribute path the code under test uses.

    Two ways to answer. A positional `*script` is the default and reads as "these things
    happen, in this order", which is what most tests want. `answer=` hands each request to a
    function instead, for the tests where calls overlap and order is not the test's point.
    """

    def __init__(self, *script: Any, answer: Router | None = None) -> None:
        self.completions = FakeCompletions(script=list(script), answer=answer)
        self.chat = FakeChat(completions=self.completions)


def _request() -> httpx.Request:
    return httpx.Request("POST", _URL)


def rate_limit_error(retry_after: str | None = None) -> RateLimitError:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(429, headers=headers, request=_request())
    return RateLimitError("rate limited", response=response, body=None)


def timeout_error() -> APITimeoutError:
    return APITimeoutError(request=_request())


def server_error() -> InternalServerError:
    response = httpx.Response(500, request=_request())
    return InternalServerError("upstream is unwell", response=response, body=None)


def status_error(status_code: int) -> APIStatusError:
    """A 4xx that retrying cannot fix - a bad key, a bad request, an unknown model."""
    response = httpx.Response(status_code, request=_request())
    return APIStatusError("rejected", response=response, body=None)


# --- The tool boundary -------------------------------------------------------------


@dataclass
class FakeTavily:
    """Shaped like the one method tools/search.py calls."""

    script: list[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def search(self, query: str, **kwargs: Any) -> Any:
        self.calls.append({"query": query, **kwargs})
        if not self.script:
            raise AssertionError(f"search made {len(self.calls)} calls; the test scripted fewer")
        answer = self.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@dataclass
class FakeCache:
    """A recording ToolCache. Not a Redis stand-in - it exists to prove the boundary
    consults the interface and stores what it says it stores, nothing more."""

    stored: dict[str, str] = field(default_factory=dict)
    gets: list[str] = field(default_factory=list)
    sets: list[tuple[str, int]] = field(default_factory=list)

    def get(self, key: str) -> str | None:
        self.gets.append(key)
        return self.stored.get(key)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self.sets.append((key, ttl_seconds))
        self.stored[key] = value


def patch_dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    """Make hostname resolution deterministic, and keep it off the network.

    A host missing from the mapping raises gaierror, which is what an unresolvable name
    does in production.
    """

    def resolve(host: str, *_args: Any, **_kwargs: Any) -> list[Any]:
        if host not in mapping:
            raise socket.gaierror(f"fake DNS has no record for {host!r}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def imported_modules(module: ModuleType) -> set[str]:
    """The top-level module names `module` imports, read from its source.

    A boundary test needs this because an agent that never calls httpx in the happy path
    still fails the contract if it can: "the Researcher and the Fact-Checker import from
    tools/ and never from tavily or httpx" is a statement about the import list, so that is
    what gets asserted.
    """
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    names: set[str] = set()

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module.split(".")[0])
    return names


def mock_http(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """An httpx.Client wired to a handler instead of a socket.

    follow_redirects stays off because tools/fetch.py follows them itself - that is the
    only way to re-validate every hop.
    """
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def pdf_bytes(*pages: str, title: str = "") -> bytes:
    """A real PDF carrying `pages` as extractable text, for the fetch tests.

    Real bytes through the real parser, because the thing worth testing is that pypdf gets
    text out of a document `fetch` retrieved - a stubbed extractor would assert only that the
    stub was called.

    `_add_object` is private to pypdf and is the only way to attach a content stream to a
    page; the writer has no public equivalent at the pinned 6.15.0. It is used here and
    nowhere else, and an upgrade that moves it fails these tests loudly, which is the right
    way to find out.
    """
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)  # noqa: SLF001
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}  # noqa: SLF001
        )
    if title:
        writer.add_metadata({"/Title": title})
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@dataclass
class FakeS3:
    """Shaped like the two boto3 S3 methods `artifacts.py` calls, and nothing else.

    **It is a fake *client*, not a fake `ArtifactStore`.** Tests wrap it in the real
    `ArtifactStore`, the way they wrap `FakeOpenAI` in the real `LLMClient`, so the retry
    schedule, the object key, the JSON body and the `ArtifactError` translation under test are
    the production ones. A fake store would assert only that a stub was called.

    `script` is popped per attempt, exactly like `FakeCompletions`: an entry that is an
    exception is raised and anything else is treated as a success. That is what makes
    "the first write fails and the retry succeeds" a sentence a test can write.
    """

    script: list[Any] = field(default_factory=list)
    objects: dict[str, bytes] = field(default_factory=dict)
    puts: list[dict[str, Any]] = field(default_factory=list)
    presigns: list[dict[str, Any]] = field(default_factory=list)

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.puts.append(kwargs)
        if self.script:
            answer = self.script.pop(0)
            if isinstance(answer, Exception):
                raise answer
        self.objects[str(kwargs["Key"])] = bytes(kwargs["Body"])
        return {}

    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        self.presigns.append({"operation": operation, **kwargs})
        if self.script:
            answer = self.script.pop(0)
            if isinstance(answer, Exception):
                raise answer
        params = kwargs["Params"]
        return (
            f"https://{params['Bucket']}.s3.example.invalid/{params['Key']}"
            f"?X-Amz-Expires={kwargs['ExpiresIn']}"
        )

    def body(self, key: str) -> Any:
        """The object at `key`, decoded. Raises `KeyError` when nothing was written there,
        which is what a test asserting "no artifact exists" wants to see."""
        return json.loads(self.objects[key].decode("utf-8"))


def s3_error(code: str = "ServiceUnavailable") -> ClientError:
    """What S3 raises when it is having the bad day ADR 0009 sizes the retry against."""
    return ClientError({"Error": {"Code": code, "Message": "S3 is unwell"}}, "PutObject")


@dataclass
class SentMessage:
    """One message a fake queue accepted, with the two FIFO attributes that carry ADR 0010."""

    body: dict[str, str]
    group_id: str
    deduplication_id: str


class FakeQueue:
    """`jobqueue.JobQueue`'s surface, in memory, including at-least-once delivery.

    It exists so the offline suite can assert what the API *enqueued* without LocalStack: that
    exactly one message was produced, that its group id is the job id, that its deduplication id
    is the documented key, and above all that the body carries identifiers and nothing else.
    The same properties are re-asserted against real SQS in the `integration` layer.

    **A received message is in flight, not gone.** That is the property the worker is written
    against (ADR 0010 decision 6): it deletes on three outcomes and on nothing else, so every
    other path has to leave a message that comes back. `redeliver()` is what the visibility
    timeout does, made explicit - a test says when it lapses, because a fake that redelivered on
    its own would turn a failing message into an infinite loop in the drain helpers.

    `receive_count` counts deliveries of the same message, which is what `ApproximateReceiveCount`
    carries and what tells the worker a delivery is the last one before the dead-letter queue.

    `fail_next` makes the next send raise, which is the only way to reach ADR 0010 decision
    10's `503 enqueue_failed` from a test.
    """

    def __init__(self, dead_letters: FakeQueue | None = None) -> None:
        self.sent: list[SentMessage] = []
        self.deleted: list[str] = []
        self.visibility_extensions: list[tuple[str, int]] = []
        self.renewal_script: list[Exception | None] = []
        self.fail_next = False
        self._inbox: list[SentMessage] = []
        self._in_flight: list[SentMessage] = []
        self._receives: dict[str, int] = {}
        # Phase 5 block C. A queue with no dead-letter queue is the honest default: most tests
        # here are about the worker, which never reads one.
        self._dead_letters = dead_letters

    # --- what the API calls ---

    def send_start(self, *, job_id: str, user_id: str, idempotency_key: str) -> None:
        self._accept(
            {"job_id": job_id, "user_id": user_id, "idempotency_key": idempotency_key},
            group_id=job_id,
            deduplication_id=idempotency_key,
        )

    def send_resume(
        self, *, job_id: str, user_id: str, idempotency_key: str, calls_used: int
    ) -> None:
        self._accept(
            {"job_id": job_id, "user_id": user_id, "idempotency_key": idempotency_key},
            group_id=job_id,
            deduplication_id=f"{job_id}:{calls_used}",
        )

    def _accept(self, body: dict[str, str], *, group_id: str, deduplication_id: str) -> None:
        from jobqueue import QueueError

        if self.fail_next:
            self.fail_next = False
            raise QueueError("the fake queue was told to fail this send")

        # SQS's own deduplication window, in its simplest honest form: a message with a
        # deduplication id already seen is accepted and discarded (ADR 0010 decision 4). SQS
        # forgets after five minutes and this never does, which is deliberate - a test that
        # depended on the window reopening would be a test of the clock.
        if any(message.deduplication_id == deduplication_id for message in self.sent):
            return
        message = SentMessage(body=body, group_id=group_id, deduplication_id=deduplication_id)
        self.sent.append(message)
        self._inbox.append(message)

    # --- what the worker calls ---

    def receive(self) -> Any:
        from jobqueue import JobMessage

        if not self._inbox:
            return None
        message = self._inbox.pop(0)
        self._in_flight.append(message)
        self._receives[message.deduplication_id] = (
            self._receives.get(message.deduplication_id, 0) + 1
        )
        return JobMessage(
            job_id=message.body["job_id"],
            user_id=message.body["user_id"],
            idempotency_key=message.body["idempotency_key"],
            # One receipt handle per message rather than per delivery. Real SQS issues a new
            # one each time; nothing here distinguishes them, and the deduplication id is the
            # name a test can recognise a message by.
            receipt_handle=message.deduplication_id,
            receive_count=self._receives[message.deduplication_id],
        )

    def receive_batch(
        self, *, max_messages: int, wait_seconds: int, visibility_timeout_s: int
    ) -> list[Any]:
        """The block C read: several at once, carrying the two FIFO attributes.

        Every message it hands out is in flight afterwards, exactly as `receive()` leaves one -
        which is what makes "inspect and release" and "hold while replaying" different things a
        test can tell apart.
        """
        from jobqueue import JobMessage

        batch: list[JobMessage] = []
        while self._inbox and len(batch) < max_messages:
            message = self._inbox.pop(0)
            self._in_flight.append(message)
            self._receives[message.deduplication_id] = (
                self._receives.get(message.deduplication_id, 0) + 1
            )
            batch.append(
                JobMessage(
                    job_id=message.body["job_id"],
                    user_id=message.body["user_id"],
                    idempotency_key=message.body["idempotency_key"],
                    receipt_handle=message.deduplication_id,
                    receive_count=self._receives[message.deduplication_id],
                    group_id=message.group_id,
                    deduplication_id=message.deduplication_id,
                )
            )
        self.visibility_extensions.extend(
            (message.receipt_handle, visibility_timeout_s) for message in batch
        )
        return batch

    def release(self, message: Any) -> None:
        """Hand a delivery straight back, which is what a zero visibility timeout does."""
        self.extend_visibility(message, visibility_timeout_s=0)
        returning = [
            held for held in self._in_flight if held.deduplication_id == message.receipt_handle
        ]
        self._in_flight = [
            held for held in self._in_flight if held.deduplication_id != message.receipt_handle
        ]
        self._inbox.extend(returning)

    def resend(self, message: Any) -> None:
        """Put a message from another queue onto this one, unchanged.

        **It bypasses the deduplication memory above, and that is the behaviour rather than a
        shortcut.** SQS forgets a deduplication id after five minutes and a dead-lettered
        message is at least three failed deliveries old, so a replay really is accepted. A fake
        that remembered forever would make every replay a silent no-op and would prove the
        opposite of what the test is asking.
        """
        from jobqueue import QueueError

        if not message.group_id or not message.deduplication_id:
            raise QueueError(f"the message for job {message.job_id} has no FIFO attributes")
        resent = SentMessage(
            body=message.body(),
            group_id=message.group_id,
            deduplication_id=message.deduplication_id,
        )
        self.sent.append(resent)
        self._inbox.append(resent)

    def dead_letter_queue(self) -> FakeQueue | None:
        return self._dead_letters

    def delete(self, message: Any) -> None:
        self.deleted.append(message.receipt_handle)
        self._in_flight = [
            held for held in self._in_flight if held.deduplication_id != message.receipt_handle
        ]

    def extend_visibility(self, message: Any, *, visibility_timeout_s: int) -> None:
        """Record the heartbeat operation, optionally raising a scripted queue failure."""
        self.visibility_extensions.append((message.receipt_handle, visibility_timeout_s))
        if self.renewal_script:
            failure = self.renewal_script.pop(0)
            if failure is not None:
                raise failure

    # --- what a test asks ---

    def redeliver(self) -> int:
        """The visibility timeout lapsing: everything received and not deleted becomes visible
        again. Answers how many messages came back, so a test can assert that a failure left
        one rather than merely that the next receive found something."""
        returning = self._in_flight
        self._in_flight = []
        self._inbox.extend(returning)
        return len(returning)

    def in_flight(self) -> list[SentMessage]:
        """Received, not deleted, and not yet redelivered - the messages SQS would give back."""
        return list(self._in_flight)

    def pending(self) -> list[SentMessage]:
        """Waiting to be received. What a worker would pick up next."""
        return list(self._inbox)

    def bodies(self) -> list[dict[str, str]]:
        return [message.body for message in self.sent]

    def only(self) -> SentMessage:
        assert len(self.sent) == 1, f"expected one message, got {len(self.sent)}"
        return self.sent[0]
