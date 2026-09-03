"""
Explainer — the graphics format.

Deliberately not video. Full video generation means shot composition, motion
and voice sync; the quality ceiling today is low enough that it would be the
thing that makes people distrust everything else in the product. What survives
is the part that actually carries the explanation: a sequence of figures, each
with a caption that says only what the source says.

Figures come from the graph, so they are faithful by construction. Illustration
briefs are gated on `concreteness` -- an abstract relation drawn as a literal
picture teaches something false.
"""
from __future__ import annotations

from typing import Any

from ..cite import Citation
from ..models import Understanding
from .base import Format, Part


class ExplainerFormat(Format):
    name = "explainer"
    label = "Visual explainer"
    job = "See how the parts fit together."
    uses = ("diagram", "glossary")
    requires = ("nodes",)
    coverage_target = 0.35

    def subtitle_for(self, u: Understanding) -> str:
        return "Figures and captions"

    def build(self, u: Understanding, *, max_figures: int = 8, **_: Any) -> list[Part]:
        parts: list[Part] = []

        # Figure 1: whatever structure the document actually has.
        for mode in ("flow", "mindmap", "causal"):
            try:
                units = self.units(u, "diagram", mode=mode)
            except ValueError:
                units = []
            if not units:
                continue
            from ..render import get_renderer
            result = get_renderer("diagram", self._client).render(u, mode=mode)
            nodes = [n for n in (u.node(i) for x in result.units for i in x.derived_from)
                     if n and not n.is_scaffold]
            if not nodes:
                continue
            parts.append(Part(
                role="figure", title=self._figure_title(mode, u),
                body=result.artifact,
                derived_from=[n.id for n in nodes],
                meta={"kind": "diagram", "mode": mode, "language": "mermaid",
                      "caption": self._caption(mode, nodes)},
            ))
            if len(parts) >= 3:
                break

        # Illustration briefs, only where depiction would not mislead.
        depictable = [n for n in u.depictable(0.55) if not n.is_scaffold][:max_figures]
        for n in depictable:
            parts.append(Part(
                role="illustration", title=n.label,
                body=n.body or n.label,
                derived_from=[n.id],
                meta={"kind": "image_brief", "concreteness": n.concreteness,
                      "brief": f"A clear, literal illustration of {n.label.lower()}: "
                               f"{(n.body or '')[:160]}. No text in the image."},
            ))

        abstract = [n for n in u.salient(30)
                    if not n.is_scaffold and n.concreteness < 0.35][:6]
        if abstract:
            parts.append(Part(
                role="not_illustrated", title="Deliberately not illustrated",
                body="\n".join(f"- **{n.label}** — too abstract to depict without "
                               f"implying something the source does not say "
                               f"(concreteness {n.concreteness:.2f})" for n in abstract),
                derived_from=[n.id for n in abstract],
            ))
        return parts

    @staticmethod
    def _figure_title(mode: str, u: Understanding) -> str:
        return {"flow": "Figure — the sequence",
                "mindmap": "Figure — how the parts nest",
                "causal": "Figure — what drives what"}.get(mode, "Figure")

    @staticmethod
    def _caption(mode: str, nodes) -> str:
        # Labels are clause fragments and often already end in punctuation;
        # joining them raw produced "…redistribution,, Reciprocity."
        top = ", ".join(
            lab for lab in
            (n.label.strip().rstrip(",;:.").strip() for n in nodes[:4]) if lab)
        return {"flow": f"The stages in order: {top}.",
                "mindmap": f"How the material divides: {top}.",
                "causal": f"The relations the source asserts between {top}."}.get(mode, top)

    def assemble(self, u: Understanding, parts: list[Part],
                 citations: list[Citation], **_: Any) -> str:
        out = [f"# {u.source.title} — visual explainer", ""]
        fig = 0
        for part in parts:
            if part.meta.get("kind") == "diagram":
                fig += 1
                out += [f"## Figure {fig}. {part.title.split('—')[-1].strip()}", "",
                        "```mermaid", part.body, "```", "",
                        f"*{part.meta.get('caption', '')}*{self.markers(part.footnotes)}", ""]
            elif part.meta.get("kind") == "image_brief":
                out += [f"### {part.title}", "",
                        part.body + self.markers(part.footnotes), "",
                        f"> **Illustration brief:** {part.meta['brief']}", ""]
            else:
                out += [f"## {part.title}", "",
                        part.body + self.markers(part.footnotes), ""]
        return "\n".join(out) + self.sources_section(citations)
