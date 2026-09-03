"""
Renderer registry.

Adding an output medium is: write a Renderer, add it here. Nothing upstream
changes, and it works for every input type already supported -- that is the
whole reason the IR exists.
"""
from __future__ import annotations

from typing import Optional

from ..llm import LLMClient
from .base import Renderer
from .diagram import DiagramRenderer
from .experimental import ComicRenderer, SlidesRenderer, SummaryRenderer
from .glossary import GlossaryRenderer
from .narration import NarrationRenderer
from .retrieval import RetrievalRenderer

RENDERERS: dict[str, type[Renderer]] = {
    # production
    "narration": NarrationRenderer,
    "diagram": DiagramRenderer,
    "glossary": GlossaryRenderer,
    "retrieval": RetrievalRenderer,
    "summary": SummaryRenderer,
    # beta
    "slides": SlidesRenderer,
    # experimental
    "comic": ComicRenderer,
}


def get_renderer(name: str, client: Optional[LLMClient] = None) -> Renderer:
    if name not in RENDERERS:
        raise KeyError(f"Unknown renderer '{name}'. Available: {', '.join(sorted(RENDERERS))}")
    return RENDERERS[name](client)


def catalog() -> list[dict[str, str]]:
    rows = []
    for name, cls in RENDERERS.items():
        rows.append({
            "name": name, "tier": cls.tier, "format": cls.format,
            "description": cls.description,
        })
    order = {"production": 0, "beta": 1, "experimental": 2}
    return sorted(rows, key=lambda r: (order.get(r["tier"], 9), r["name"]))


__all__ = ["RENDERERS", "get_renderer", "catalog", "Renderer"]
