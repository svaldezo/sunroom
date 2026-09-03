"""
Formats — the product surface.

A *renderer* is an engineering primitive: narration, diagram, retrieval. Nobody
wants a narration script; they want a podcast. A **format** is a named,
recognizable deliverable composed from renderers plus a structure of its own:

    podcast   = narration + two-voice dialogue + segment pacing
    activity  = retrieval + ordering + branching + feedback
    explainer = diagram + illustrated glossary + captions

Adding a format is a recipe, not new machinery — the same argument as the IR,
one level up. Citations index across the whole deliverable, so a footnote means
the same thing in a diagram caption and in the third podcast segment.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..cite import Citation, index_citations, to_markdown
from ..llm import LLMClient, get_client
from ..models import RenderUnit, Understanding
from ..render import get_renderer


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Part(BaseModel):
    """One addressable section of a deliverable, with its provenance intact."""
    id: str = Field(default_factory=lambda: _uid("prt"))
    role: str                       # cold_open | segment | figure | step | ...
    title: str = ""
    body: str = ""
    derived_from: list[str] = Field(default_factory=list)   # Node ids
    units: list[RenderUnit] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    footnotes: list[int] = Field(default_factory=list)
    #: Does this part make a claim ABOUT the source, or does it tell the
    #: reader what to DO with it?
    #:
    #: "Reciprocity is delayed" asserts the source and must share its
    #: vocabulary. "Closed notes — let them struggle before you help" is
    #: instructional scaffolding: it still has to say which passage it is
    #: about, but running a drift check on it produces only false alarms.
    asserts: bool = True


class Deliverable(BaseModel):
    id: str = Field(default_factory=lambda: _uid("dlv"))
    understanding_id: str
    format: str
    tier: str = "production"
    title: str = ""
    subtitle: str = ""
    parts: list[Part] = Field(default_factory=list)
    artifact: str = ""
    artifact_format: str = "markdown"     # markdown | json
    citations: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class Format(ABC):
    name: str
    label: str
    tier: str = "production"
    #: One line the user reads when choosing. Says the JOB, not the mechanism.
    job: str = ""
    #: Renderers this format composes. Declared so the UI can show the chain
    #: and so a missing prerequisite fails here rather than halfway through.
    uses: tuple[str, ...] = ()
    #: IR-level prerequisites, checked before any work happens.
    requires: tuple[str, ...] = ()
    artifact_format: str = "markdown"
    #: Roughly how much of the document this format is meant to carry.
    coverage_target: float = 0.5

    def __init__(self, client: Optional[LLMClient] = None):
        self._client = client

    @property
    def client(self) -> LLMClient:
        if self._client is None:
            self._client = get_client()
        return self._client

    # -- composition helpers ----------------------------------------------
    def units(self, u: Understanding, renderer: str, **options: Any) -> list[RenderUnit]:
        """
        Run a component renderer and take its units.

        A renderer that declines returns nothing rather than raising, because a
        format usually has other material to work with -- a lesson without a
        glossary is still a lesson.
        """
        try:
            return get_renderer(renderer, self._client).render(u, **options).units
        except ValueError:
            return []

    @staticmethod
    def markers(nums: list[int]) -> str:
        return "".join(f"[^{n}]" for n in nums)

    def check_requirements(self, u: Understanding) -> list[str]:
        missing = []
        for req in self.requires:
            probe = getattr(u, req, None)
            got = probe() if callable(probe) else probe
            if not got:
                missing.append(req)
        return missing

    # -- contract ----------------------------------------------------------
    @abstractmethod
    def build(self, u: Understanding, **options: Any) -> list[Part]: ...

    @abstractmethod
    def assemble(self, u: Understanding, parts: list[Part],
                 citations: list[Citation], **options: Any) -> str: ...

    def subtitle_for(self, u: Understanding) -> str:
        return ""

    def make(self, u: Understanding, **options: Any) -> Deliverable:
        missing = self.check_requirements(u)
        if missing:
            raise ValueError(
                f"'{self.label}' needs {', '.join(missing)} in the IR and this "
                f"document has none. Try another format."
            )
        parts = self.build(u, **options)
        if not parts:
            raise ValueError(
                f"'{self.label}' found nothing usable in this document. "
                f"The extraction may be too thin — try 'Brief' or 'Study guide'."
            )
        citations, per_part = index_citations(u, parts)
        for part in parts:
            part.footnotes = per_part.get(part.id, [])

        return Deliverable(
            understanding_id=u.id, format=self.name, tier=self.tier,
            title=u.source.title, subtitle=self.subtitle_for(u),
            parts=parts,
            artifact=self.assemble(u, parts, citations, **options),
            artifact_format=self.artifact_format,
            citations=[c.to_dict() for c in citations],
            meta={"uses": list(self.uses), "options": options,
                  "source_checksum": u.source.checksum,
                  "coverage_target": self.coverage_target},
        )

    # -- shared assembly bits ---------------------------------------------
    def sources_section(self, citations: list[Citation]) -> str:
        if not citations:
            return ""
        return "\n\n---\n\n### Sources\n\n" + to_markdown(citations)

    @staticmethod
    def timeline(u: Understanding) -> list[tuple[str, str]]:
        """
        For time-based sources, the running order with timestamps.

        Media -> text is the underserved direction: a recorded lecture becomes
        a readable, citable document, and the timeline is what makes it
        navigable back to the recording.
        """
        rows = []
        for sec in sorted(u.sections, key=lambda s: s.span.start if s.span else 0):
            if sec.span and sec.span.t_start is not None:
                nodes = [n for n in (u.node(i) for i in sec.node_ids) if n and not n.is_scaffold]
                if nodes:
                    best = max(nodes, key=lambda n: n.salience)
                    rows.append((sec.span.locator or sec.title, best.label))
        return rows
