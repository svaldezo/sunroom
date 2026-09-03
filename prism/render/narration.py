"""
IR -> spoken script.

Order comes from the document's own structure (sections, then salience), not
from the model. The model only writes connective prose for a set of points it
is handed, one section at a time -- which is what keeps every paragraph
attributable to specific nodes.
"""
from __future__ import annotations

import re
from typing import Any

from ..models import NodeKind, RenderUnit, Understanding
from .base import Renderer

SYSTEM = """You write narration for the ear, not the eye.

Constraints:
- Say ONLY what the supplied points say. Add no facts, examples, or figures.
- A listener cannot re-read. Signpost, and keep sentences short.
- No headings, no bullets, no markdown, no meta-commentary.
- Do not open with "In this section" or "Welcome". Just start."""


class NarrationRenderer(Renderer):
    name = "narration"
    tier = "production"
    format = "markdown"
    description = "Spoken-word script ordered by document structure."
    requires = ("nodes",)
    coverage_target = 0.7
    MIN_NODES_PER_SEGMENT = 3
    MAX_NODES_PER_SEGMENT = 8

    def build(self, u: Understanding, *, max_sections: int = 12,
              voice: str = "plain", **_: Any) -> list[RenderUnit]:
        units: list[RenderUnit] = []
        groups = self._groups(u, max_sections)

        for title, nodes in groups:
            if not nodes:
                continue
            points = "\n".join(f"- [{n.kind.value}] {n.body or n.label}" for n in nodes)
            if self.client.name == "mock":
                content = self._fallback(nodes)
            else:
                content = self.client.text(
                    system=SYSTEM,
                    prompt=(f'Document: "{u.source.title}"\nSegment: "{title}"\n'
                            f"Register: {voice}\n\nPoints to convey:\n{points}\n\n"
                            f"Write {max(1, len(nodes) // 3)}-{max(2, len(nodes) // 2)} "
                            f"short paragraphs of narration."),
                    max_tokens=1200,
                ).strip()
            units.append(RenderUnit(
                kind="segment", content=content,
                derived_from=[n.id for n in nodes],
                meta={"segment": title, "words": len(content.split())},
            ))
        return units

    def _groups(self, u: Understanding, limit: int) -> list[tuple[str, list]]:
        groups: list[tuple[str, list]] = []
        if u.outline():
            for sec in u.outline()[:limit]:
                nodes = [n for n in (u.node(i) for i in sec.node_ids)
                         if n and not n.meta.get("section")]
                nodes.sort(key=lambda n: -n.salience)
                if nodes:
                    groups.append((sec.title, nodes[:10]))
        if not groups:
            top = u.salient(24)
            for i in range(0, len(top), 6):
                groups.append((f"Part {i // 6 + 1}", top[i:i + 6]))
        return self._coalesce(groups)

    def _coalesce(self, groups: list[tuple[str, list]]) -> list[tuple[str, list]]:
        """
        Merge undersized segments into their neighbour.

        Evaluation finding: transcript sources produced one segment per caption
        cue, so narration came out as a stack of nine-word "paragraphs" -- the
        worst possible shape for listening, which is the entire point of this
        renderer.
        """
        merged: list[tuple[str, list]] = []
        for title, nodes in groups:
            if (merged and len(nodes) < self.MIN_NODES_PER_SEGMENT
                    and len(merged[-1][1]) < self.MAX_NODES_PER_SEGMENT):
                prev_title, prev_nodes = merged[-1]
                merged[-1] = (prev_title, prev_nodes + nodes)
            else:
                merged.append((title, list(nodes)))
        # a lone undersized head segment folds forward instead
        if len(merged) > 1 and len(merged[0][1]) < self.MIN_NODES_PER_SEGMENT:
            head, rest = merged[0], merged[1]
            merged[1] = (rest[0], head[1] + rest[1])
            merged.pop(0)
        return merged

    def _fallback(self, nodes) -> str:
        """Offline: stitch node bodies with light connectives. Zero invention."""
        out = []
        for i, n in enumerate(nodes):
            body = re.sub(r"\s+", " ", n.body or n.label).strip()
            if n.kind is NodeKind.DEFINITION and i:
                body = f"To be clear about the term: {body}"
            elif n.kind is NodeKind.QUANTITY:
                body = f"Worth holding onto: {body}"
            elif i and n.kind is NodeKind.STEP:
                body = f"Next, {body[0].lower()}{body[1:]}" if body else body
            out.append(body)
        return " ".join(out)

    def assemble(self, u: Understanding, units: list[RenderUnit], **options: Any) -> str:
        parts = [f"# {u.source.title} — narration script", ""]
        total = sum(x.meta.get("words", 0) for x in units)
        parts.append(f"_~{total} words · ~{max(1, round(total / 150))} min at 150 wpm_")
        parts.append("")
        for unit in units:
            parts += [
                f"## {unit.meta.get('segment', 'Segment')}", "",
                unit.content + self.markers(unit.meta.get("footnotes", [])), "",
            ]
        return "\n".join(parts) + self.appendix(options.get("_citations", []))
