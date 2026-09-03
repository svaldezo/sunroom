"""
Format registry — the product surface.

Renderers stay available as components, but this is what a person chooses from,
because these are things you can ask for in a sentence.
"""
from __future__ import annotations

from typing import Optional

from ..llm import LLMClient
from .activity import ActivityFormat
from .audio import PodcastFormat
from .base import Deliverable, Format, Part
from .lesson import LessonFormat
from .text import BriefFormat, GuideFormat
from .tutor import Answer, TutorFormat, ask, retrieve
from .visual import ExplainerFormat

FORMATS: dict[str, type[Format]] = {
    "brief": BriefFormat,
    "guide": GuideFormat,
    "podcast": PodcastFormat,
    "explainer": ExplainerFormat,
    "activity": ActivityFormat,
    "tutor": TutorFormat,
    "lesson": LessonFormat,
}

#: Display order — the three that should be excellent first, then the rest.
ORDER = ("brief", "activity", "podcast", "guide", "explainer", "tutor", "lesson")


def get_format(name: str, client: Optional[LLMClient] = None) -> Format:
    if name not in FORMATS:
        raise KeyError(f"Unknown format '{name}'. Available: {', '.join(ORDER)}")
    return FORMATS[name](client)


def catalog() -> list[dict[str, object]]:
    rows = []
    for name in ORDER:
        cls = FORMATS[name]
        rows.append({
            "name": name, "label": cls.label, "job": cls.job, "tier": cls.tier,
            "uses": list(cls.uses), "artifact_format": cls.artifact_format,
        })
    return rows


__all__ = ["FORMATS", "ORDER", "get_format", "catalog", "Format", "Deliverable",
           "Part", "Answer", "ask", "retrieve"]
