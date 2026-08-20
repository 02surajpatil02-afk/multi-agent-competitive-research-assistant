"""
WHY THIS FILE EXISTS
    The Cognito half of `routes/auth.py` decides who may approve a research report, so what it
    refuses matters more than what it accepts. Every case below is a token that is *almost*
    valid - the right shape, the right claims, one thing wrong - because a verifier that
    accepts a well-formed forgery fails exactly the same way as no verifier at all.

    **These tests sign real tokens with a real RSA key and verify real signatures.** An RSA
    key pair is generated once per session, its public half is served as a static JWKS
    document, and nothing opens a socket: the whole point of `JwksCache` taking a callable is
    that a test supplies the key set and production supplies an HTTP fetch. A test that mocked
    the verification would prove that the mock returns what it was told to.

    The four families here, and the attack each closes:

    | Family | What it would let through |
    |---|---|
    | Signature and algorithm | A token anyone can write - `alg: none`, or an HMAC forgery |
    | Issuer and client | A valid token from a pool or an application the attacker controls |
    | Expiry and claim shape | A token valid last year, or an id token used as an access token |
    | Groups | A caller with a real account and no role, deciding at the gate |

    The key-set cache has its own family, because two of its rules are security properties
    rather than performance ones: a fetch that fails must refuse everyone rather than trust
    anyone, and an unknown key id must not turn every forged token into an outbound request.

WHO CALLS IT
    pytest, as part of the offline suite. It reaches no network and needs no AWS.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from routes.auth import (
    CognitoAuthenticator,
    CognitoSettings,
    Identity,
    JwksCache,
    build_authenticator,
    role_from_groups,
)

REGION = "ap-south-1"
POOL_ID = "ap-south-1_Example99"
CLIENT_ID = "4f9example5client8id2"
KEY_ID = "kid-in-the-pool"
SUBJECT = "11111111-1111-4111-8111-111111111111"

SETTINGS = CognitoSettings(region=REGION, user_pool_id=POOL_ID, client_id=CLIENT_ID)


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    """One key pair for the whole session. Generating RSA keys is the slow part of this file,
    and no test needs a second identity - the ones that need a *wrong* key make it themselves."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(key: rsa.RSAPrivateKey, kid: str = KEY_ID) -> dict[str, Any]:
    """The public half, in the shape Cognito publishes at `/.well-known/jwks.json`."""
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {"keys": [{**public, "kid": kid, "alg": "RS256", "use": "sig"}]}


_DEFAULT_GROUPS = object()


def _claims(*, groups: Any = _DEFAULT_GROUPS, **overrides: Any) -> dict[str, Any]:
    """A valid Cognito access token's payload, before whatever a test breaks about it.

    `groups` is spelled out rather than passed through `overrides` because the claim's real
    name contains a colon, and a keyword argument cannot.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": SUBJECT,
        "iss": SETTINGS.issuer,
        "client_id": CLIENT_ID,
        "token_use": "access",
        "cognito:groups": ["reviewer"] if groups is _DEFAULT_GROUPS else groups,
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    return payload


def _b64(document: dict[str, Any]) -> bytes:
    """One JWT segment: compact JSON, base64url, no padding."""
    raw = json.dumps(document, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _token(key: rsa.RSAPrivateKey, *, kid: str = KEY_ID, **overrides: Any) -> str:
    return jwt.encode(_claims(**overrides), key, algorithm="RS256", headers={"kid": kid})


def _authenticator(
    key: rsa.RSAPrivateKey,
    *,
    settings: CognitoSettings = SETTINGS,
    fetch: Callable[[], dict[str, Any]] | None = None,
) -> CognitoAuthenticator:
    source = fetch if fetch is not None else (lambda: _jwks(key))
    return CognitoAuthenticator(settings, JwksCache(source))


def _header(token: str) -> str:
    return f"Bearer {token}"


# --- The token that should work ------------------------------------------------------------


def test_a_valid_access_token_produces_the_same_identity_a_key_would(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """The property that made this a small change: everything downstream reads a `user_id` and
    a role, so a verified token and a hashed key produce the identical object."""
    caller = _authenticator(signing_key).identity(_header(_token(signing_key)))

    assert caller == Identity(user_id=SUBJECT, role="reviewer")


def test_the_submitter_group_maps_to_the_submitter_role(signing_key: rsa.RSAPrivateKey) -> None:
    token = _token(signing_key, groups=["submitter"])

    assert _authenticator(signing_key).identity(_header(token)) == Identity(
        user_id=SUBJECT, role="submitter"
    )


def test_a_caller_in_both_groups_is_a_reviewer(signing_key: rsa.RSAPrivateKey) -> None:
    """A reviewer may do everything a submitter may, so the superset wins rather than the
    order the groups happen to arrive in."""
    token = _token(signing_key, groups=["submitter", "reviewer"])

    caller = _authenticator(signing_key).identity(_header(token))
    assert caller is not None and caller.role == "reviewer"


def test_an_unrelated_group_changes_nothing(signing_key: rsa.RSAPrivateKey) -> None:
    token = _token(signing_key, groups=["analytics", "reviewer"])

    caller = _authenticator(signing_key).identity(_header(token))
    assert caller is not None and caller.role == "reviewer"


# --- Signature and algorithm ----------------------------------------------------------------


def test_a_token_signed_by_another_key_is_refused(signing_key: rsa.RSAPrivateKey) -> None:
    """The whole point. The claims are perfect and the signature is not this pool's."""
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    forged = _token(attacker)

    assert _authenticator(signing_key).identity(_header(forged)) is None


def test_a_tampered_payload_is_refused(signing_key: rsa.RSAPrivateKey) -> None:
    """The signature covers the payload, so promoting yourself to reviewer invalidates it.

    Both halves come from real tokens this pool signed: a submitter's header and signature, and
    a reviewer's payload. Only the pairing is forged, which is the realistic attack.
    """
    submitter = _token(signing_key, groups=["submitter"]).split(".")
    reviewer = _token(signing_key).split(".")

    swapped = f"{submitter[0]}.{reviewer[1]}.{submitter[2]}"

    assert _authenticator(signing_key).identity(_header(swapped)) is None


def test_an_unsigned_token_is_refused(signing_key: rsa.RSAPrivateKey) -> None:
    """`alg: none` is the oldest JWT attack and is closed by naming the algorithm here rather
    than reading it from the token."""
    unsigned = jwt.encode(_claims(), "", algorithm="none", headers={"kid": KEY_ID})

    assert _authenticator(signing_key).identity(_header(unsigned)) is None


def test_a_token_signed_with_the_public_key_as_an_hmac_secret_is_refused(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """Algorithm confusion: the public key is public, so if HS256 were accepted anyone could
    sign a token with it. The `alg` in the header is compared to RS256, never used."""
    public_pem = signing_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    # Built by hand rather than with `jwt.encode`, which refuses to use a PEM key as an HMAC
    # secret - a defence in the library. The attacker writing this token has no such scruples,
    # so the test has to produce what they would actually send.
    signing_input = b".".join(
        (
            _b64({"alg": "HS256", "kid": KEY_ID, "typ": "JWT"}),
            _b64(_claims()),
        )
    )
    signature = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    confused = b".".join((signing_input, base64.urlsafe_b64encode(signature).rstrip(b"="))).decode()

    assert _authenticator(signing_key).identity(_header(confused)) is None


def test_a_token_naming_a_key_the_pool_does_not_publish_is_refused(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    assert _authenticator(signing_key).identity(_header(_token(signing_key, kid="other"))) is None


# --- Issuer, client, expiry and claim shape -------------------------------------------------


def test_a_token_from_a_different_pool_is_refused(signing_key: rsa.RSAPrivateKey) -> None:
    """An attacker can create their own Cognito pool in their own account in five minutes. The
    issuer check is what makes that useless here."""
    elsewhere = f"https://cognito-idp.{REGION}.amazonaws.com/{REGION}_Attacker1"

    assert _authenticator(signing_key).identity(_header(_token(signing_key, iss=elsewhere))) is None


def test_a_token_for_a_different_app_client_is_refused(signing_key: rsa.RSAPrivateKey) -> None:
    """Same pool, different application. Cognito access tokens carry no `aud`, so `client_id`
    is the audience check and it has to be made by hand."""
    token = _token(signing_key, client_id="a-different-client")

    assert _authenticator(signing_key).identity(_header(token)) is None


def test_an_expired_token_is_refused(signing_key: rsa.RSAPrivateKey) -> None:
    now = int(time.time())
    token = _token(signing_key, iat=now - 7200, exp=now - 3600)

    assert _authenticator(signing_key).identity(_header(token)) is None


def test_an_id_token_is_refused_where_an_access_token_is_required(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """ADR 0020 decision 3: one token type is accepted, so there is one claim shape on the
    authorization path rather than two."""
    token = _token(signing_key, token_use="id")

    assert _authenticator(signing_key).identity(_header(token)) is None


@pytest.mark.parametrize("missing", ["exp", "iat", "iss", "sub", "token_use", "client_id"])
def test_a_token_missing_any_required_claim_is_refused(
    missing: str, signing_key: rsa.RSAPrivateKey
) -> None:
    claims = _claims()
    del claims[missing]
    token = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": KEY_ID})

    assert _authenticator(signing_key).identity(_header(token)) is None


def test_a_valid_token_with_no_role_group_authenticates_nobody(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """A real account in the pool with neither group. It gets the same answer an unknown key
    gets, for the same reason: which half of the credential was wrong is not the caller's."""
    for groups in ([], ["analytics"], None):
        token = _token(signing_key, groups=groups)
        assert _authenticator(signing_key).identity(_header(token)) is None


# --- The header itself -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic something", "not-a-header", "Bearer not.a.jwt"],
)
def test_a_header_that_is_not_a_bearer_token_is_refused(
    header: str | None, signing_key: rsa.RSAPrivateKey
) -> None:
    assert _authenticator(signing_key).identity(header) is None


# --- The key set cache ------------------------------------------------------------------------


class _CountingFetch:
    def __init__(self, document: dict[str, Any] | Exception) -> None:
        self.document = document
        self.calls = 0

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        if isinstance(self.document, Exception):
            raise self.document
        return self.document


def test_the_key_set_is_fetched_once_and_reused(signing_key: rsa.RSAPrivateKey) -> None:
    fetch = _CountingFetch(_jwks(signing_key))
    authenticator = _authenticator(signing_key, fetch=fetch)

    for _ in range(5):
        assert authenticator.identity(_header(_token(signing_key))) is not None

    assert fetch.calls == 1


def test_a_key_set_that_cannot_be_fetched_authenticates_nobody(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """Fail closed. No key means no verified signature, and trusting a token nobody could check
    is the only worse answer than refusing a caller who should have been let in."""
    fetch = _CountingFetch(RuntimeError("cognito is unreachable"))

    caller = _authenticator(signing_key, fetch=fetch).identity(_header(_token(signing_key)))

    assert caller is None
    assert fetch.calls >= 1


def test_a_rotated_key_is_picked_up_without_waiting_for_the_ttl(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """What a rotation looks like from here: a valid token signed by a key this process has
    never seen. Waiting for the hour-long TTL would reject every caller until it expired."""
    now = 0.0
    published: dict[str, Any] = _jwks(signing_key, kid="old-key")
    cache = JwksCache(lambda: published, clock=lambda: now)
    authenticator = CognitoAuthenticator(SETTINGS, cache)

    assert authenticator.identity(_header(_token(signing_key))) is None

    # The pool rotates, and the cooldown - not the hour-long TTL - is what has to pass.
    published = _jwks(signing_key)
    now = 400.0
    assert authenticator.identity(_header(_token(signing_key))) is not None


def test_an_unknown_key_id_cannot_be_used_to_drive_outbound_requests(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """The other half of the rule above. "Unknown key id" is also what a forged token looks
    like, so a stream of them must not become a stream of requests to Cognito."""
    # Twenty forged tokens spread over more than half an hour, so the TTL is not what is doing
    # the work: without the cooldown every one of them would be its own request.
    ticks = iter([float(step * 100) for step in range(30)])
    fetch = _CountingFetch(_jwks(signing_key))
    authenticator = CognitoAuthenticator(SETTINGS, JwksCache(fetch, clock=lambda: next(ticks)))

    for _ in range(20):
        assert authenticator.identity(_header(_token(signing_key, kid="forged"))) is None

    assert fetch.calls <= 8, f"{fetch.calls} fetches for 20 forged tokens"


# --- Choosing a mode ----------------------------------------------------------------------------


def test_the_default_mode_is_the_key_table_so_local_behaviour_is_unchanged() -> None:
    from config import load_config
    from routes.auth import ApiKeyAuthenticator, hash_key

    table = f'{{"{hash_key("k")}": {{"user_id": "{SUBJECT}", "role": "reviewer"}}}}'
    made = build_authenticator(load_config({"AUTH_KEYS": table}))

    assert isinstance(made, ApiKeyAuthenticator)
    assert made.identity("Bearer k") == Identity(user_id=SUBJECT, role="reviewer")


def test_cognito_mode_builds_a_verifier_and_reaches_nothing_to_do_it() -> None:
    """Startup opens no connection: the signing keys are read on the first token, so a
    deployment with no callers yet makes no outbound request."""
    from config import load_config

    made = build_authenticator(
        load_config(
            {
                "AUTH_MODE": "cognito",
                "COGNITO_USER_POOL_ID": POOL_ID,
                "COGNITO_CLIENT_ID": CLIENT_ID,
                "AWS_REGION": REGION,
            }
        )
    )

    assert isinstance(made, CognitoAuthenticator)
    assert made.settings.issuer == f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
    assert made.settings.jwks_url.endswith("/.well-known/jwks.json")


def test_cognito_mode_refuses_to_start_without_a_pool_or_a_client() -> None:
    """A service that cannot authenticate anyone should fail to boot rather than refuse every
    caller - the rule `load_api_keys` has followed since Phase 2."""
    from config import load_config

    for env in (
        {"AUTH_MODE": "cognito", "COGNITO_CLIENT_ID": CLIENT_ID},
        {"AUTH_MODE": "cognito", "COGNITO_USER_POOL_ID": POOL_ID},
    ):
        with pytest.raises(ValueError, match="COGNITO_"):
            build_authenticator(load_config(env))


def test_an_unknown_auth_mode_is_refused_at_startup() -> None:
    from config import load_config

    with pytest.raises(ValueError, match="AUTH_MODE"):
        load_config({"AUTH_MODE": "oauth"})


def test_groups_that_are_not_a_list_of_strings_carry_no_role() -> None:
    """The claim comes from a token, so it is read defensively even though a real pool always
    sends a list."""
    for groups in (None, "reviewer", 7, [7], {"reviewer": True}):
        assert role_from_groups(groups) is None
