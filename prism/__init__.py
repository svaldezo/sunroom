"""
prism — ingest anything, understand it once, render it in any medium.

    from prism import Prism

    p = Prism()
    doc = p.add("lecture.pdf", collection="ANTH266")
    print(p.render(doc.id, "diagram").artifact)
"""
from __future__ import annotations

from typing import Any, Optional

from .fidelity import FidelityReport, check, citations
from .ingest import ingest, supported
from .llm import get_client
from .models import (
    Edge,
    Medium,
    Node,
    NodeKind,
    Relation,
    RenderResult,
    RenderUnit,
    Source,
    Span,
    Understanding,
)
from .render import RENDERERS, catalog, get_renderer
from .store import Repository
from .understand import understand

__version__ = "0.1.0"


class Prism:
    """Convenience facade over ingest -> understand -> store -> render -> check."""

    def __init__(self, *, provider: Optional[str] = None, repository: Optional[Repository] = None):
        self.client = get_client(provider)
        self.repo = repository or Repository()

    def add(self, target: str, *, collection: Optional[str] = None,
            title: Optional[str] = None, medium: Optional[Medium] = None,
            force: bool = False) -> Understanding:
        ing = ingest(target, title=title, medium=medium)
        if not force:
            existing = self.repo.find_by_checksum(ing.source.checksum)
            if existing:
                return existing
        u = understand(ing, client=self.client, collection=collection)
        self.repo.save(u)
        return u

    def get(self, understanding_id: str) -> Optional[Understanding]:
        return self.repo.get(understanding_id)

    def render(self, understanding_id: str, renderer: str, **options: Any) -> RenderResult:
        u = self.repo.get(understanding_id)
        if not u:
            raise KeyError(f"no such document: {understanding_id}")
        result = get_renderer(renderer, self.client).render(u, **options)
        self.repo.save_render(result, source_checksum=u.source.checksum)
        return result

    def verify(self, understanding_id: str, result: RenderResult) -> FidelityReport:
        u = self.repo.get(understanding_id)
        if not u:
            raise KeyError(f"no such document: {understanding_id}")
        return check(u, result)

    def search(self, query: str, **kw: Any):
        return self.repo.search(query, **kw)

    def media(self) -> dict[str, Any]:
        return {"input": supported(), "output": catalog()}


__all__ = [
    "Prism", "Understanding", "Source", "Span", "Node", "Edge", "NodeKind",
    "Relation", "RenderResult", "RenderUnit", "Medium", "Repository",
    "ingest", "understand", "get_renderer", "catalog", "check", "citations",
    "FidelityReport", "supported", "RENDERERS", "__version__",
]
