"""
Corpus storage.

`open_store(user_id)` is the only thing application code should call. It picks
SQLite or Postgres from configuration and, either way, hands back a store bound
to exactly one account.
"""
from __future__ import annotations

from typing import Optional

from ..config import SETTINGS
from .base import LOCAL_USER, CorpusStore, NotFound, StoreError
from .repository import RELEARN_SECONDS, Repository


def open_store(user_id: str = LOCAL_USER, *, backend: Optional[str] = None):
    """A corpus store for one account."""
    backend = backend or SETTINGS.store
    if backend == "postgres":
        from .pg import PgRepository
        return PgRepository(user_id=user_id)
    return Repository(user_id=user_id)


__all__ = ["Repository", "CorpusStore", "NotFound", "StoreError", "LOCAL_USER",
           "RELEARN_SECONDS", "open_store"]
