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

    **Phase 5 block B added the second credential, and it changed nothing downstream** - which
    is what the paragraph above predicted. A Cognito access token is verified here and produces
    the same `Identity`, so `audit_events.actor`, `jobs.user_id` and the ownership check never
    learn that anything happened. What is new is `Authenticator`: one object with one method,
    picked once at startup from `AUTH_MODE`.

    **One mode is live per process, never both** (ADR 0020 decision 2). An API that accepted a
    Cognito token *or* a static key would be exactly as strong as the weaker of the two, and the
    weaker one is a shared secret with no expiry. `api_key` stays the default so every local
    command and the whole offline suite are untouched; the AWS deployment runs `cognito`.

WHO CALLS IT
    routes/api.py, as FastAPI dependencies. app.py builds the authenticator once at startup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast, get_args

import httpx
import jwt

from config import Config, required

logger = logging.getLogger(__name__)

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


def bearer_token(header: str | None) -> str | None:
    """The credential inside an `Authorization: Bearer ...` header, whatever kind it is.

    Both modes take the same header, because both take a bearer credential and the route layer
    should not have to know which one is configured.
    """
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def identity_from(header: str | None, keys: dict[str, Identity]) -> Identity | None:
    """The caller behind an `Authorization` header, or None if there is not one.

    None covers every way of failing to authenticate - absent header, wrong scheme, unknown
    key - because the caller learns the same thing from all three: `401`. Telling them
    which one it was would be free reconnaissance.
    """
    key = bearer_token(header)
    if key is None:
        return None

    digest = hash_key(key)
    for known, identity in keys.items():
        # Constant time, so a wrong key cannot be sharpened one character at a time.
        if hmac.compare_digest(known, digest):
            return identity
    return None


# --- One credential, chosen once at startup -------------------------------------------


class Authenticator(Protocol):
    """Everything the route layer knows about authentication: a header goes in, an `Identity`
    or `None` comes out.

    A Protocol rather than a base class because there is no shared behaviour to inherit - the
    two implementations have nothing in common except this signature, which is the point.
    """

    def identity(self, header: str | None) -> Identity | None: ...


@dataclass(frozen=True)
class ApiKeyAuthenticator:
    """The Phase 2 hashed-key table, unchanged, behind the new interface."""

    keys: dict[str, Identity]

    def identity(self, header: str | None) -> Identity | None:
        return identity_from(header, self.keys)


# --- Cognito access tokens ------------------------------------------------------------


JWKS_TTL_SECONDS = 3600.0
"""How long a fetched key set is reused. Cognito signing keys do not rotate on a schedule, so
an hour is generous; what actually keeps this correct is the unknown-kid refetch below."""

JWKS_REFRESH_COOLDOWN_SECONDS = 300.0
"""The floor between two fetches triggered by an unknown key id.

Without it, a stream of tokens carrying a made-up `kid` would become a stream of outbound
requests - an unauthenticated caller turning this process into a load generator against
Cognito. With it, the worst case is one fetch every five minutes.
"""

JWKS_TIMEOUT_SECONDS = 5.0
"""A bounded fetch, because an unbounded one on the authentication path stalls every request
that needs a key. Failing takes 5 seconds and answers 401; hanging takes forever."""

ACCEPTED_ALGORITHM = "RS256"
"""The only algorithm Cognito signs with, and the only one accepted.

Naming it is what stops the `alg: none` and HMAC-confusion families of attack: a verifier that
takes the algorithm from the token being verified lets the token choose how it is checked.
"""

REQUIRED_CLAIMS = ("exp", "iat", "iss", "sub", "token_use", "client_id")
"""Claims that must be present before any of them is read. A missing `token_use` would
otherwise compare unequal to `"access"` and be refused anyway; requiring it says so once."""


@dataclass(frozen=True)
class CognitoSettings:
    """One user pool and one app client. **None of these three is a secret** - a token is
    verified with a published key against a published issuer, which is why they travel as
    plain environment variables while the LLM key does not."""

    region: str
    user_pool_id: str
    client_id: str

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"


def fetch_jwks(url: str) -> dict[str, Any]:
    """One bounded GET for a public document. No credential is sent and none is needed."""
    response = httpx.get(url, timeout=JWKS_TIMEOUT_SECONDS)
    response.raise_for_status()
    return cast(dict[str, Any], response.json())


class JwksCache:
    """The pool's public keys, fetched on demand and reused.

    Two rules, and each closes a failure the other does not:

    **Refetch when a key id is unknown**, because that is what a key rotation looks like from
    here - a valid token signed by a key this process has never seen. Waiting for the TTL would
    reject every caller until it expired.

    **Never refetch faster than the cooldown**, because "unknown key id" is also what a forged
    token looks like, and an unauthenticated caller must not be able to drive outbound traffic.

    A fetch that fails leaves the cache alone and answers `None`, so authentication fails
    closed: no key, no verified token, no identity. That is the safe direction - the unsafe one
    would be trusting a token nobody could check.
    """

    def __init__(
        self,
        fetch: Callable[[], dict[str, Any]],
        *,
        ttl: float = JWKS_TTL_SECONDS,
        cooldown: float = JWKS_REFRESH_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._cooldown = cooldown
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None

    def key_for(self, kid: str) -> jwt.PyJWK | None:
        now = self._clock()
        if self._fetched_at is None or now - self._fetched_at >= self._ttl:
            self._reload(now)
        if kid in self._keys:
            return self._keys[kid]

        if self._fetched_at is not None and now - self._fetched_at >= self._cooldown:
            self._reload(now)
        return self._keys.get(kid)

    def _reload(self, now: float) -> None:
        try:
            document = self._fetch()
            keys = jwt.PyJWKSet.from_dict(document)
        except Exception:  # noqa: BLE001 - the reason goes to the log, never to the caller
            logger.exception("could not load the token signing keys")
            return
        self._keys = {key.key_id: key for key in keys.keys if key.key_id}
        self._fetched_at = now


class CognitoAuthenticator:
    """A Cognito **access token**, verified, turned into the same `Identity` a key produces.

    **The access token and not the id token** (ADR 0020 decision 3). The id token describes a
    user to the application that signed them in; the access token is the one that says *this
    bearer may call this API*, and it is the one that carries `cognito:groups` and `client_id`.
    Accepting either would mean two claim shapes on one authorization path.

    Six things are checked and each one is load-bearing:

    | Check | The failure it closes |
    |---|---|
    | RS256, named rather than read from the token | `alg: none`, and HMAC confusion |
    | The signature, against the pool's published key | A token anyone can write |
    | `iss` equals this pool's issuer | A token from a Cognito pool the attacker owns |
    | `exp` and `iat` | A token that was valid last year |
    | `token_use == "access"` | An id token presented where an access token is required |
    | `client_id` equals this app client | A token minted for another app in the same pool |

    A role comes from the pool's groups, because that is the only claim a user pool carries that
    an administrator controls and a user cannot set. A token carrying neither group produces no
    identity at all, and the caller gets the same `401` an unknown key gets - for the same
    reason the three key failures share one answer.
    """

    def __init__(self, settings: CognitoSettings, jwks: JwksCache | None = None) -> None:
        self.settings = settings
        self._jwks = jwks if jwks is not None else JwksCache(lambda: fetch_jwks(settings.jwks_url))

    def identity(self, header: str | None) -> Identity | None:
        token = bearer_token(header)
        if token is None:
            return None

        key = self._signing_key(token)
        if key is None:
            return None

        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[ACCEPTED_ALGORITHM],
                issuer=self.settings.issuer,
                # Cognito access tokens carry no `aud`; the audience equivalent is `client_id`,
                # checked below. Leaving PyJWT's audience check on would fail every valid token.
                options={"verify_aud": False, "require": list(REQUIRED_CLAIMS)},
            )
        except jwt.PyJWTError as error:
            logger.info("rejected a token: %s", type(error).__name__)
            return None

        if claims["token_use"] != "access":
            return None
        if not hmac.compare_digest(str(claims["client_id"]), self.settings.client_id):
            return None

        role = role_from_groups(claims.get("cognito:groups"))
        if role is None:
            logger.info("token for %s carries no role group", claims["sub"])
            return None
        return Identity(user_id=str(claims["sub"]), role=role)

    def _signing_key(self, token: str) -> jwt.PyJWK | None:
        """The key this token says signed it - if the pool agrees such a key exists.

        Reading the unverified header is safe and unavoidable: a key id is how a signature is
        looked up, and nothing here is *trusted* from it. The algorithm is checked against the
        one this verifier accepts rather than used, so a token cannot choose its own check.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return None
        if header.get("alg") != ACCEPTED_ALGORITHM:
            return None
        kid = header.get("kid")
        return None if not isinstance(kid, str) else self._jwks.key_for(kid)


def role_from_groups(groups: Any) -> Role | None:
    """The pool group a caller's role comes from, or None when they have neither.

    `reviewer` wins when both are present, because it is the superset: a reviewer may do
    everything a submitter may. Any other group name is ignored rather than refused, so adding
    an unrelated group to a user cannot change what they may do here.
    """
    if not isinstance(groups, list):
        return None
    names = {name for name in groups if isinstance(name, str)}
    if "reviewer" in names:
        return "reviewer"
    if "submitter" in names:
        return "submitter"
    return None


def build_authenticator(config: Config) -> Authenticator:
    """The one credential this process accepts, decided once at startup.

    A missing value fails here rather than at the first request, which is the rule the rest of
    startup already follows: a service that cannot authenticate anyone should not boot.
    """
    if config.auth_mode == "cognito":
        return CognitoAuthenticator(
            CognitoSettings(
                region=config.cognito_region,
                user_pool_id=required(config.cognito_user_pool_id, "COGNITO_USER_POOL_ID"),
                client_id=required(config.cognito_client_id, "COGNITO_CLIENT_ID"),
            )
        )
    return ApiKeyAuthenticator(load_api_keys(config.auth_keys))
