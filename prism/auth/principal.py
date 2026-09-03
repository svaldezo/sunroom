"""
Who a request is from.

A Principal is produced only by verifying a token (or by explicit local-mode
configuration). Nothing else in the application constructs one, so "is this
request authenticated" is answered by whether you have one at all rather than
by a boolean somebody has to remember to check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..store.base import LOCAL_USER


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str = ""
    is_admin: bool = False
    # 'supabase' when a real token was verified, 'local' for a single-user
    # deployment with auth switched off, 'worker' for the job runner.
    via: str = "supabase"
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def is_local(self) -> bool:
        return self.via == "local"

    def __str__(self) -> str:                       # for logs
        return f"{self.email or self.user_id[:8]} ({self.via})"


def anonymous() -> Optional[Principal]:
    """There is no anonymous principal. Kept as a name so the intent is loud."""
    return None


def local_principal(email: str = "local@sunroom") -> Principal:
    """The one account a no-auth deployment has."""
    return Principal(user_id=LOCAL_USER, email=email, is_admin=True, via="local")
