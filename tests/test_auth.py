"""
Token verification, mostly from the attacker's side.

The happy path here is one test. The rest are forgeries, because a verifier that
accepts a valid token is easy and a verifier that rejects everything else is the
entire job.
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from prism.auth.verify import AuthError, TokenVerifier, bearer

SECRET = "test-jwt-secret-at-least-32-characters-long"
AUD = "authenticated"
ISS = "https://proj.supabase.co/auth/v1"
SUB = "11111111-1111-1111-1111-111111111111"


def hs(**over) -> str:
    claims = {"sub": SUB, "aud": AUD, "iss": ISS, "role": "authenticated",
              "email": "a@x.test", "exp": int(time.time()) + 3600,
              "iat": int(time.time())}
    claims.update(over)
    return jwt.encode(claims, SECRET, algorithm="HS256")


@pytest.fixture()
def v() -> TokenVerifier:
    return TokenVerifier(jwt_secret=SECRET, audience=AUD, issuer=ISS)


# -- the one happy path ----------------------------------------------------

def test_accepts_a_real_token(v):
    p = v.verify(hs())
    assert p.user_id == SUB
    assert p.email == "a@x.test"
    assert p.via == "supabase"


# -- forgeries -------------------------------------------------------------

def test_rejects_alg_none(v):
    """The oldest JWT attack: claim there is no signature and hope."""
    token = jwt.encode({"sub": SUB, "aud": AUD, "iss": ISS,
                        "role": "authenticated",
                        "exp": int(time.time()) + 3600},
                       key="", algorithm="none")
    with pytest.raises(AuthError):
        v.verify(token)


def _forge_hs256(payload: dict, secret: bytes) -> str:
    """
    Sign a token by hand.

    PyJWT refuses to use a PEM as an HMAC secret, which is a good guard in a
    signing library and useless here: the attacker is not using PyJWT. Building
    the token with hmac directly is the only way to find out what our verifier
    does when handed the real thing.
    """
    import base64
    import hashlib
    import hmac
    import json

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps(payload).encode())
    signing_input = header + b"." + body
    sig = b64(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return (signing_input + b"." + sig).decode()


def test_rejects_algorithm_confusion():
    """
    An RS256 verifier must not accept an HS256 token signed with the public key.

    If the verifier takes `alg` from the token, an attacker signs with the
    public key -- which is public -- and the server dutifully checks it as an
    HMAC secret. Fixing the algorithm set by configuration is what closes it.
    """
    from cryptography.hazmat.primitives import serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)

    forged = _forge_hs256(
        {"sub": SUB, "aud": AUD, "iss": ISS, "role": "authenticated",
         "exp": int(time.time()) + 3600},
        pub_pem)

    # A project configured for asymmetric keys only: no shared secret at all.
    asym_only = TokenVerifier(jwt_secret="", jwks_url="https://example.invalid/jwks",
                              audience=AUD, issuer=ISS)
    with pytest.raises(AuthError):
        asym_only.verify(forged)

    # And a project that has *both* configured -- the realistic case during a
    # migration -- must still not check an asymmetric token with the HMAC path.
    both = TokenVerifier(jwt_secret=SECRET, jwks_url="https://example.invalid/jwks",
                         audience=AUD, issuer=ISS)
    with pytest.raises(AuthError):
        both.verify(forged)


def test_rejects_a_token_signed_with_the_wrong_secret(v):
    token = jwt.encode({"sub": SUB, "aud": AUD, "iss": ISS,
                        "role": "authenticated",
                        "exp": int(time.time()) + 3600},
                       "not-the-secret", algorithm="HS256")
    with pytest.raises(AuthError):
        v.verify(token)


def test_rejects_expired(v):
    with pytest.raises(AuthError):
        v.verify(hs(exp=int(time.time()) - 600))


def test_rejects_wrong_audience(v):
    with pytest.raises(AuthError):
        v.verify(hs(aud="someone-else"))


def test_rejects_wrong_issuer(v):
    """A token from a different Supabase project is not a token for this one."""
    with pytest.raises(AuthError):
        v.verify(hs(iss="https://other.supabase.co/auth/v1"))


def test_rejects_the_anon_key(v):
    """
    The Supabase anon key is a JWT signed with the same secret and shipped to
    every browser. It must not be usable as a session.
    """
    anon = jwt.encode({"role": "anon", "iss": "supabase", "aud": AUD,
                       "exp": int(time.time()) + 999999}, SECRET,
                      algorithm="HS256")
    with pytest.raises(AuthError):
        v.verify(anon)


def test_rejects_a_token_with_no_subject(v):
    with pytest.raises(AuthError):
        v.verify(hs(sub=None))


def test_rejects_missing_expiry(v):
    claims = {"sub": SUB, "aud": AUD, "iss": ISS, "role": "authenticated"}
    with pytest.raises(AuthError):
        v.verify(jwt.encode(claims, SECRET, algorithm="HS256"))


@pytest.mark.parametrize("junk", [
    "", "abc", "a.b", "a.b.c.d", "Bearer x", "....", "null",
    "eyJhbGciOiJIUzI1NiJ9..",
])
def test_rejects_junk(v, junk):
    with pytest.raises(AuthError):
        v.verify(junk)


def test_admin_flag_comes_from_app_metadata_only(v):
    """
    app_metadata is server-controlled in Supabase; user_metadata is not -- a
    user can write their own. Reading admin from the wrong one is self-service
    privilege escalation.
    """
    assert v.verify(hs(user_metadata={"is_admin": True})).is_admin is False
    assert v.verify(hs(app_metadata={"is_admin": True})).is_admin is True


def test_error_message_does_not_explain_itself(v):
    """A verifier that says why is an oracle for tuning the next attempt."""
    with pytest.raises(AuthError) as e:
        v.verify(hs(exp=int(time.time()) - 600))
    assert str(e.value) == "not signed in"


# -- header parsing --------------------------------------------------------

@pytest.mark.parametrize("header,expected", [
    ("Bearer abc.def.ghi", "abc.def.ghi"),
    ("bearer abc.def.ghi", "abc.def.ghi"),
    ("BEARER   abc.def.ghi  ", "abc.def.ghi"),
    ("Basic abc", ""),
    ("abc.def.ghi", ""),
    ("Bearer", ""),
    ("", ""),
    (None, ""),
])
def test_bearer_parsing(header, expected):
    assert bearer(header) == expected
