"""
What a corpus store has to be able to do.

The single most important property of this interface is that a Repository is
constructed *for one account* and there is no method that takes a user id. The
scoping is not something a caller can forget, because there is nowhere to
forget it -- a query for another person's document is not a bug you can write
here, it is a method that does not exist.

`ANY_USER` exists for the single-user case (local CLI, a private deployment)
and for the worker's job-claiming pass, which by definition looks across
accounts before it knows whose job it picked up. It is spelled loudly on
purpose.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Protocol, runtime_checkable

from ..formats.base import Deliverable
from ..models import Node, RenderResult, Understanding

# The account id used when the deployment has no accounts at all.
LOCAL_USER = "00000000-0000-0000-0000-000000000000"


@runtime_checkable
class CorpusStore(Protocol):
    """The corpus of one account."""

    user_id: str

    # -- collections -------------------------------------------------------
    def ensure_collection(self, name: str, kind: str = "collection") -> None: ...
    def collections(self) -> list[dict[str, Any]]: ...

    # -- understandings ----------------------------------------------------
    def save(self, u: Understanding) -> str: ...
    def get(self, understanding_id: str) -> Optional[Understanding]: ...
    def find_by_checksum(self, checksum: str) -> Optional[Understanding]: ...
    def list(self, collection: Optional[str] = None) -> list[dict[str, Any]]: ...
    def delete(self, understanding_id: str) -> bool: ...

    # -- cross-document queries -------------------------------------------
    def search(self, query: str, *, collection: Optional[str] = None,
               kinds: Optional[Iterable[str]] = None,
               limit: int = 25) -> list[dict[str, Any]]: ...
    def nodes_in(self, collection: str,
                 kind: Optional[str] = None) -> list[Node]: ...

    # -- renders -----------------------------------------------------------
    def save_render(self, result: RenderResult,
                    source_checksum: str = "") -> str: ...
    def renders_for(self, understanding_id: str) -> list[dict[str, Any]]: ...
    def render_payloads(self, understanding_id: str) -> list[RenderResult]: ...
    def latest_render(self, understanding_id: str,
                      renderer: str) -> Optional[RenderResult]: ...
    def latest_renders(self, understanding_id: str) -> list[RenderResult]: ...
    def all_renders(self) -> list[dict[str, Any]]: ...
    def stale_renders(self) -> list[dict[str, Any]]: ...

    # -- deliverables ------------------------------------------------------
    def save_deliverable(self, d: Deliverable,
                         source_checksum: str = "") -> str: ...
    def latest_deliverable(self, understanding_id: str,
                           fmt: str) -> Optional[Deliverable]: ...
    def deliverables_for(self, understanding_id: str) -> list[dict[str, Any]]: ...
    def deliverable_payloads(self, understanding_id: str) -> list[Deliverable]: ...
    def all_deliverables(self) -> list[dict[str, Any]]: ...

    # -- review ------------------------------------------------------------
    def schedule(self, node_id: str, collection: Optional[str], *,
                 correct: bool) -> dict[str, Any]: ...
    def due(self, collection: Optional[str] = None,
            limit: int = 50) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


class StoreError(RuntimeError):
    """A storage failure that the API should translate, not leak."""


class NotFound(StoreError):
    """Asked for something this account does not have.

    Deliberately indistinguishable from 'exists but belongs to someone else':
    telling the two apart is how you turn a list of ids into a census of other
    people's libraries.
    """
