"""
Supabase token verification.

Supabase issues two kinds of access token depending on how the project is
configured, and a deployment can move between them without telling you:

  * **HS256**, signed with the project's JWT secret. The legacy default, still
    what most projects have. Verified with a shared secret.
  * **ES256/RS256**, signed with a rotating key published at
    `/auth/v1/.well-known/jwks.json`. The current default for new projects.
    Verified against the JWKS, fetched on demand and cached, re-fetched once
    when a token arrives with an unfamiliar `kid` so a key rotation does not
    take the app down until the cache expires.

Both are supported and the algorithm is chosen from the token header, with one
rule that matters more than the rest: **the set of acceptable algorithms is
fixed by configuration, never read from the token.** A verifier that trusts the
header's `alg` accepts `alg: none`, and accepts an HS256 token signed with the
*public* key of an RS256 pair. Those are the two classic JWT forgeries and both
are closed here by construction.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

import jwt
from jwt import PyJWKClient

from ..config import SETTINGS
from .principal import Principal

# Anything outside this set is refused before a signature is even checked.
SYMMETRIC = {"HS256", "HS384", "HS512"}
ASYMMETRIC = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
ALLOWED = SYMMETRIC | ASYMMETRIC

# Supabase access tokens carry aud=authenticated for a signed-in user.
EXPECTED_AUDIENCE = "authenticated"

JWKS_TTL = 600.0
JWKS_MIN_REFETCH = 30.0     # a floor, so an unknown kid cannot become a DoS


class AuthError(Exception):
    """The request did not carry a usable identity.

    The message is deliberately coarse. Telling a caller whether a token was
    expired, malformed, or signed with the wrong key is a free oracle for
    anyone probing.
    """

    def __init__(self, message: str = "not signed in", *, detail: str = ""):
        super().__init__(message)
        self.detail = detail or message


class TokenVerifier:
    """Verifies Supabase access tokens. One instance per process."""

    def __init__(self, *, jwt_secret: str = "", jwks_url: str = "",
                 audience: str = EXPECTED_AUDIENCE,
                 issuer: str = ""):
        self.jwt_secret = jwt_secret
        self.jwks_url = jwks_url
        self.audience = audience
        self.issuer = issuer
        self._jwks: Optional[PyJWKClient] = None
        self._jwks_at = 0.0
        self._lock = threading.Lock()

    # -- key material ------------------------------------------------------
    def _jwk_client(self, *, force: bool = False) -> PyJWKClient:
        if not self.jwks_url:
            raise AuthError(detail="no JWKS url configured")
        now = time.monotonic()
        with self._lock:
            stale = (self._jwks is None
                     or now - self._jwks_at > JWKS_TTL
                     or (force and now - self._jwks_at > JWKS_MIN_REFETCH))
            if stale:
                self._jwks = PyJWKClient(self.jwks_url, cache_keys=True,
                                         lifespan=int(JWKS_TTL), timeout=5)
                self._jwks_at = now
            return self._jwks

    # -- verification ------------------------------------------------------
    def verify(self, token: str) -> Principal:
        if not token or token.count(".") != 2:
            raise AuthError(detail="malformed token")

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError(detail=f"bad header: {exc}") from None

        alg = header.get("alg", "")
        # The header names the algorithm; it does not get to choose it. This is
        # what stops `alg: none` and the HS256-signed-with-the-RSA-public-key
        # confusion attack, both of which are only possible when the verifier
        # takes its instructions from the thing it is verifying.
        if alg not in ALLOWED:
            raise AuthError(detail=f"unsupported algorithm {alg!r}")

        claims = (self._verify_asymmetric(token, header)
                  if alg in ASYMMETRIC else self._verify_symmetric(token, alg))

        sub = claims.get("sub")
        if not sub:
            raise AuthError(detail="token has no subject")
        # A Supabase anon key is itself a valid JWT signed with the same secret,
        # with role=anon and no subject. Refusing anything that is not a real
        # end-user session closes the "paste the public anon key as a bearer
        # token" path.
        role = claims.get("role", "")
        if role not in ("authenticated", "service_role"):
            raise AuthError(detail=f"token role {role!r} is not a user session")

        return Principal(
            user_id=str(sub),
            email=str(claims.get("email") or ""),
            is_admin=bool((claims.get("app_metadata") or {}).get("is_admin")),
            via="supabase",
            claims=claims,
        )

    def _options(self) -> dict[str, Any]:
        return {
            "require": ["exp", "sub"],
            "verify_exp": True,
            "verify_aud": bool(self.audience),
            "verify_iss": bool(self.issuer),
            "verify_signature": True,
        }

    def _verify_symmetric(self, token: str, alg: str) -> dict[str, Any]:
        if not self.jwt_secret:
            raise AuthError(detail="HS256 token but no JWT secret configured")
        try:
            return jwt.decode(
                token, self.jwt_secret, algorithms=sorted(SYMMETRIC),
                audience=self.audience or None, issuer=self.issuer or None,
                options=self._options(), leeway=10)
        except jwt.PyJWTError as exc:
            raise AuthError(detail=str(exc)) from None

    def _verify_asymmetric(self, token: str, header: dict[str, Any]) -> dict[str, Any]:
        last: Optional[Exception] = None
        # Two passes: the cached JWKS, then a forced refetch. A key rotation
        # should cost one extra fetch, not an outage.
        for force in (False, True):
            try:
                key = self._jwk_client(force=force).get_signing_key_from_jwt(token)
            except (jwt.PyJWTError, urllib.error.URLError, TimeoutError,
                    json.JSONDecodeError) as exc:
                last = exc
                continue
            try:
                return jwt.decode(
                    token, key.key, algorithms=sorted(ASYMMETRIC),
                    audience=self.audience or None, issuer=self.issuer or None,
                    options=self._options(), leeway=10)
            except jwt.PyJWTError as exc:
                # A signature failure is final: refetching keys will not help,
                # and retrying would double the work on every forged token.
                raise AuthError(detail=str(exc)) from None
        raise AuthError(detail=f"could not fetch signing key: {last}")


_verifier: Optional[TokenVerifier] = None
_verifier_lock = threading.Lock()


def verifier() -> TokenVerifier:
    """The process-wide verifier, built from configuration."""
    global _verifier
    if _verifier is None:
        with _verifier_lock:
            if _verifier is None:
                base = SETTINGS.supabase_url
                _verifier = TokenVerifier(
                    jwt_secret=SETTINGS.supabase_jwt_secret,
                    jwks_url=f"{base}/auth/v1/.well-known/jwks.json" if base else "",
                    issuer=f"{base}/auth/v1" if base else "",
                )
    return _verifier


def reset_verifier() -> None:
    """Drop the cached verifier. For tests and for a config reload."""
    global _verifier
    _verifier = None


def bearer(header_value: Optional[str]) -> str:
    """Pull the token out of an Authorization header, tolerantly but strictly."""
    if not header_value:
        return ""
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()
