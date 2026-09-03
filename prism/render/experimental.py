"""
Beta and experimental renderers.

Shipped in the registry and clearly tiered rather than hidden. The user sees
what a medium's quality actually is before they trust its output -- which
matters more here than in most products, because the whole reason someone
converts a medium is that they cannot yet evaluate the content themselves.
"""
from __future__ import annotations

from typing import Any

from ..models import RenderUnit, Understanding
from .base import Renderer

PANEL_SYSTEM = """You adapt explanatory material into comic panels.

Each panel: one visual beat, one caption. The caption may only assert what the
supplied point asserts. Where the point is abstract, show a person reasoning
about it rather than inventing a literal scene. Keep characters consistent by
describing them identically in every panel that includes them."""

PANEL_SCHEMA = {
    "type": "object",
    "properties": {
        "panels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "caption": {"type": "string"},
                    "art_direction": {"type": "string"},
                },
                "required": ["ref", "caption", "art_direction"],
            },
        }
    },
    "required": ["panels"],
}


class ComicRenderer(Renderer):
    """Panel script + art direction. Image generation is a separate backend."""
    name = "comic"
    tier = "experimental"
    format = "markdown"
    description = "Panel-by-panel script. Experimental: abstract material adapts poorly."
    requires = ("nodes",)

    def build(self, u: Understanding, *, panels: int = 12, **_: Any) -> list[RenderUnit]:
        nodes = [n for n in u.salient(panels) if not n.meta.get("section")]
        if self.client.name == "mock":
            return [RenderUnit(
                kind="panel", content=(n.body or n.label), derived_from=[n.id],
                meta={"art_direction": f"Depict: {n.label}", "panel": i + 1},
            ) for i, n in enumerate(nodes)]

        listing = "\n".join(f"{n.id} | {n.label} | {n.body[:200]}" for n in nodes)
        payload = self.client.structured(
            system=PANEL_SYSTEM,
            prompt=f'Adapt "{u.source.title}" into {panels} panels. Use ids as `ref`.\n\n{listing}',
            schema=PANEL_SCHEMA, max_tokens=4096,
        )
        return [RenderUnit(
            kind="panel", content=p.get("caption", ""), derived_from=[p["ref"]],
            meta={"art_direction": p.get("art_direction", ""), "panel": i + 1},
        ) for i, p in enumerate(payload.get("panels", [])) if p.get("ref")]

    def assemble(self, u: Understanding, units: list[RenderUnit], **options: Any) -> str:
        lines = [f"# {u.source.title} — panel script",
                 "", "> ⚠️ **Experimental tier.** Verify against the source before use.", ""]
        for unit in units:
            lines += [f"**Panel {unit.meta.get('panel')}**",
                      f"- *Art:* {unit.meta.get('art_direction', '')}",
                      f"- *Caption:* {unit.content}"
                      + self.markers(unit.meta.get("footnotes", [])), ""]
        return "\n".join(lines) + self.appendix(options.get("_citations", []))


class SlidesRenderer(Renderer):
    name = "slides"
    tier = "beta"
    format = "markdown"
    description = "Section-per-slide outline with speaker notes."
    requires = ("nodes",)

    def build(self, u: Understanding, *, max_slides: int = 20, **_: Any) -> list[RenderUnit]:
        units: list[RenderUnit] = []
        groups: list[tuple[str, list]] = []
        if u.outline():
            for sec in u.outline()[:max_slides]:
                nodes = [n for n in (u.node(i) for i in sec.node_ids)
                         if n and not n.meta.get("section")]
                if nodes:
                    groups.append((sec.title, sorted(nodes, key=lambda n: -n.salience)[:5]))
        else:
            top = u.salient(max_slides * 4)
            groups = [(f"Point {i//4 + 1}", top[i:i+4]) for i in range(0, len(top), 4)]

        for title, nodes in groups:
            bullets = "\n".join(f"- {n.label}" for n in nodes)
            notes = " ".join(n.body for n in nodes)
            units.append(RenderUnit(
                kind="slide", content=f"## {title}\n\n{bullets}",
                derived_from=[n.id for n in nodes], meta={"notes": notes},
            ))
        return units

    def assemble(self, u: Understanding, units: list[RenderUnit], **options: Any) -> str:
        out = [f"# {u.source.title}", "", "---", ""]
        for unit in units:
            notes = unit.meta.get("notes", "") + self.markers(unit.meta.get("footnotes", []))
            out += [unit.content, "", f"???\n{notes}", "", "---", ""]
        return "\n".join(out) + self.appendix(options.get("_citations", []))


class SummaryRenderer(Renderer):
    name = "summary"
    tier = "production"
    format = "markdown"
    description = "Tiered summary: one line, one paragraph, full outline."
    requires = ("nodes",)

    def build(self, u: Understanding, **_: Any) -> list[RenderUnit]:
        top = [n for n in u.salient(30) if not n.is_scaffold][:15]
        units = [RenderUnit(kind="abstract", content=u.summary or top[0].body,
                            derived_from=[n.id for n in top[:5]])]
        for root in u.roots()[:8]:
            children = [c for c in (u.node(i) for i in u.hierarchy().get(root.id, [])) if c]
            children.sort(key=lambda n: -n.salience)
            body = "\n".join(f"  - {c.label}" for c in children[:6])
            units.append(RenderUnit(
                kind="outline", content=f"- **{root.label}**" + (f"\n{body}" if body else ""),
                derived_from=[root.id] + [c.id for c in children[:6]],
            ))
        return units
