"""
WHY THIS FILE EXISTS
    Who is calling, and what they are allowed to do. Every route that touches job data goes
    through here; `/health` is the single exception, and it is the only one
    (guidelines §16, ARCHITECTURE.md §13).

    **The sharpest reason this exists in Phase 2 rather than Phase 5** is the approval
    endpoint: approving a report is an authorization decision, and it is the backstop the
    whole prompt-injection defense leans on. A gate anyone can open is not a gate, and
    shipping one unauthenticated would make that argument fictional for three phases.

    Three properties are load-bearing and easy to lose:

      * **Keys are stored hashed.** What the process holds is `sha256(key)`, so an operator
        reading the environment, a log line, or a crash dump never sees a working
        credential. The comparison is `hmac.compare_digest`, because a plain `==` on a
        secret leaks its prefix through timing.
      * **Identity comes from the credential, never from the request body.** A reviewer's
        name in a payload is a claim; a key that maps to a `user_id` is evidence. That
        `user_id` is what reaches `audit_events.actor`, which is the whole point of the
        gate recording who opened it (guidelines §9).
      * **Two roles, because there are exactly two things a caller can do.** A `submitter`
        submits and reads its own jobs; a `reviewer` may also decide at the gate. A
        submitter presenting a valid key to the gate gets `403`, and guidelines §18 requires
        a test for exactly that.

    Phase 5 replaces the key lookup with a Cognito JWT and nothing else: everything
    downstream already reads a `user_id` and a role rather than a key, so the change is
    confined to `identity_from()` below.

WHO CALLS IT
    routes/api.py, as FastAPI dependencies. app.py builds the key table once at startup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Literal, get_args

Role = Literal["submitter", "reviewer"]
"""guidelines §16's two roles. A third would need a third thing a caller can do."""

ROLES: frozenset[str] = frozenset(get_args(Role))


@dataclass(frozen=True)
class Identity:
    """An authenticated caller: who they are, and what they may do.

    This is what the whole system downstream reads - `jobs.user_id`, `audit_events.actor`,
    and the ownership check - so Phase 5's JWT has to produce exactly this and nothing more.
    """

    user_id: str
    role: Role

    @property
    def is_reviewer(self) -> bool:
        return self.role == "reviewer"


class AuthConfigError(RuntimeError):
    """The key table itself is unusable. Raised at startup, never per request: a service
    that cannot authenticate anyone should fail to boot rather than refuse every caller."""


def load_api_keys(raw: str | None) -> dict[str, Identity]:
    """Parse the hashed-key table, or say precisely what is wrong with it.

    The shape is `{"<sha256 hex of the key>": {"user_id": "...", "role": "reviewer"}}` -
    the same JSON Secrets Manager will hold under `AUTH_KEYS_SECRET_ID` in Phase 5, read
    from the environment until then (guidelines §16).

    Validated here, once, so a typo in a role is a failed startup rather than a caller who
    is mysteriously forbidden.
    """
    if not raw:
        raise AuthConfigError("AUTH_KEYS is not set, so no caller could ever authenticate")
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AuthConfigError(f"AUTH_KEYS is not valid JSON: {error}") from None
    if not isinstance(parsed, dict) or not parsed:
        raise AuthConfigError("AUTH_KEYS must be a non-empty object of hashed key -> identity")

    table: dict[str, Identity] = {}
    for digest, entry in parsed.items():
        if not isinstance(entry, dict) or "user_id" not in entry or "role" not in entry:
            raise AuthConfigError(f"AUTH_KEYS entry {digest[:8]}... needs a user_id and a role")
        role = entry["role"]
        if role not in ROLES:
            raise AuthConfigError(f"AUTH_KEYS entry {digest[:8]}... has an unknown role {role!r}")
        table[str(digest).lower()] = Identity(user_id=str(entry["user_id"]), role=role)
    return table


def hash_key(key: str) -> str:
    """What is stored, and what is compared. Never the key itself."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def identity_from(header: str | None, keys: dict[str, Identity]) -> Identity | None:
    """The caller behind an `Authorization` header, or None if there is not one.

    None covers every way of failing to authenticate - absent header, wrong scheme, unknown
    key - because the caller learns the same thing from all three: `401`. Telling them
    which one it was would be free reconnaissance.
    """
    if not header:
        return None
    scheme, _, key = header.partition(" ")
    if scheme.lower() != "bearer" or not key.strip():
        return None

    digest = hash_key(key.strip())
    for known, identity in keys.items():
        # Constant time, so a wrong key cannot be sharpened one character at a time.
        if hmac.compare_digest(known, digest):
            return identity
    return None
