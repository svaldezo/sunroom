"""
FastAPI dependencies: who is calling, and what they get to touch.

The shape here is deliberate. `current_principal` is the *only* way a route
obtains an identity, and `current_store` is the only way it obtains a corpus.
Because the store is built from the principal, a route that forgets to
authenticate does not get an unscoped store -- it gets no store at all and
fails to start.

`require_worker` is separate and stricter: the worker endpoint is not a user
route, it is a machine one, and it is authenticated with a shared secret in
constant time.
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from ..accounts import accounts
from ..config import SETTINGS
from ..store import open_store
from .principal import Principal, local_principal
from .verify import AuthError, bearer, verifier

UNAUTHENTICATED = HTTPException(
    status_code=401, detail="Sign in to continue.",
    headers={"WWW-Authenticate": "Bearer"})


def current_principal(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Principal:
    """
    The signed-in account, or a 401.

    In a deployment with no Supabase configured there is exactly one account and
    everyone is it -- that is the local/single-user mode, and it is only allowed
    when `preflight` has not objected, which it does for any production config.
    """
    if not SETTINGS.multi_user:
        if SETTINGS.is_production:
            # Refuse rather than serve everyone the same library. Reaching here
            # in production means the deployment is misconfigured, and the safe
            # failure is no access rather than shared access.
            raise HTTPException(
                status_code=503,
                detail="This deployment is not configured for accounts.")
        return local_principal()

    token = bearer(authorization)
    if not token:
        raise UNAUTHENTICATED
    try:
        principal = verifier().verify(token)
    except AuthError:
        raise UNAUTHENTICATED from None

    if SETTINGS.signup_allowlist and principal.email:
        domain = principal.email.rsplit("@", 1)[-1].lower()
        if domain not in SETTINGS.signup_allowlist:
            raise HTTPException(
                status_code=403,
                detail="This instance is limited to invited domains.")

    # First request from a new account creates its row. Supabase's trigger
    # usually got there first; this covers the case where it did not.
    try:
        accounts().ensure(principal.user_id, principal.email)
    except Exception:                                          # noqa: S110
        # A failed account upsert must not block a request that would otherwise
        # work; the account row is created again on the next call.
        pass
    request.state.principal = principal
    return principal


def current_store(principal: Principal = Depends(current_principal)):
    """This account's corpus. There is no way to ask for anyone else's."""
    return open_store(principal.user_id)


def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    account = accounts().get(principal.user_id)
    if not (principal.is_admin or (account and account.is_admin)):
        # 404 rather than 403: an admin route that announces itself is a map.
        raise HTTPException(status_code=404, detail="Not found")
    return principal


def require_worker(
    x_worker_secret: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> bool:
    """
    Authenticate the job worker.

    Compared with `hmac.compare_digest` because a plain `==` on a secret leaks
    its prefix through timing, and this endpoint can be hammered freely by
    anyone who finds the URL. Vercel's cron sends its own bearer token, so both
    headers are accepted.
    """
    expected = SETTINGS.worker_secret
    if not expected:
        raise HTTPException(status_code=503,
                            detail="Worker is not configured.")
    supplied = x_worker_secret or bearer(authorization)
    if not supplied or not hmac.compare_digest(str(supplied), str(expected)):
        raise HTTPException(status_code=401, detail="Not authorized.")
    return True
