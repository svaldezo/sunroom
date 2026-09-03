"""
Renderer contract.

A renderer reads the IR and emits RenderUnits. It never sees the raw source.
Two consequences, both load-bearing:

  1. Adding an output medium is ONE class, not one pipeline per input type.
     N parsers x M renderers, not N*M converters.
  2. Every unit must declare `derived_from`. A unit that names no nodes cannot
     be traced to the source, and the fidelity checker will flag it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from ..cite import Citation, index_citations, to_markdown
from ..llm import LLMClient, get_client
from ..models import RenderResult, RenderUnit, Understanding


class Renderer(ABC):
    name: str
    tier: str = "production"        # production | beta | experimental
    format: str = "text"
    description: str = ""
    #: IR-level prerequisites. Checked before rendering so a medium fails loudly
    #: rather than emitting something hollow.
    requires: tuple[str, ...] = ()
    #: Fraction of the document's salient nodes this medium is expected to
    #: carry. A diagram showing a slice is not the same failure as a summary
    #: that dropped 80% of the argument, so the bar is per-renderer.
    coverage_target: float = 0.25

    def __init__(self, client: Optional[LLMClient] = None):
        self._client = client

    @property
    def client(self) -> LLMClient:
        if self._client is None:
            self._client = get_client()
        return self._client

    @abstractmethod
    def build(self, u: Understanding, **options: Any) -> list[RenderUnit]: ...

    def assemble(self, u: Understanding, units: list[RenderUnit], **options: Any) -> str:
        body = "\n\n".join(
            f"{unit.content}{self.markers(unit.meta.get('footnotes', []))}"
            for unit in units
        )
        return body + self.appendix(options.get("_citations", []))

    def check_requirements(self, u: Understanding) -> list[str]:
        missing = []
        for req in self.requires:
            probe = getattr(u, req, None)
            got = probe() if callable(probe) else probe
            if not got:
                missing.append(req)
        return missing

    #: Whether the assembled artifact should carry a numbered citation
    #: appendix. Mermaid is the exception -- appending prose to a diagram
    #: source breaks the parser, so its citations ride in `meta` only.
    cites_in_artifact: bool = True

    @staticmethod
    def markers(nums: list[int]) -> str:
        return "".join(f"[^{n}]" for n in nums)

    def appendix(self, citations: list[Citation]) -> str:
        if not citations or not self.cites_in_artifact:
            return ""
        return "\n\n---\n\n### Sources\n\n" + to_markdown(citations)

    def render(self, u: Understanding, **options: Any) -> RenderResult:
        missing = self.check_requirements(u)
        if missing:
            raise ValueError(
                f"'{self.name}' needs {', '.join(missing)} in the IR and this "
                f"document has none. Re-run understanding, or pick another medium."
            )
        units = self.build(u, **options)

        # Attribution is not optional and not a renderer's private business:
        # index it once, here, so every medium gets it the same way.
        citations, per_unit = index_citations(u, units)
        for unit in units:
            unit.meta["footnotes"] = per_unit.get(unit.id, [])

        options = {**options, "_citations": citations}
        return RenderResult(
            understanding_id=u.id,
            renderer=self.name,
            tier=self.tier,
            format=self.format,
            units=units,
            artifact=self.assemble(u, units, **options),
            meta={"options": {k: v for k, v in options.items() if k != "_citations"},
                  "source_checksum": u.source.checksum,
                  "coverage_target": self.coverage_target,
                  "citations": [c.to_dict() for c in citations]},
        )
