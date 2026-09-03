"""
Brief and Study guide — the two text formats.

They are different products, not two lengths of one. A brief is linear and
argued: you read it once, start to finish. A guide is dense and scannable: you
come back to it. Collapsing them produces a mediocre both.

Both matter most for the media -> text direction. A two-hour lecture becomes a
readable document where every claim links to the second it was said.
"""
from __future__ import annotations

import re
from typing import Any

from ..cite import Citation
from ..models import NodeKind, Relation, Understanding
from .base import Format, Part


def _bullet(node) -> str:
    """
    A label that is just the first words of the body adds nothing. Bold it only
    when it is a real handle -- otherwise the bullet reads twice.
    """
    body = re.sub(r"\s+", " ", node.body or node.label).strip()
    label = node.label.strip().rstrip(",")
    if not label or body.lower().startswith(label.lower()):
        return body
    return f"**{label}** — {body}"


class BriefFormat(Format):
    name = "brief"
    label = "Brief"
    job = "Understand it once, start to finish."
    uses = ("summary", "narration")
    requires = ("nodes",)
    coverage_target = 0.6

    def subtitle_for(self, u: Understanding) -> str:
        kind = {"audio": "from a recording", "video": "from a recording",
                "pdf": "from a document"}.get(u.source.medium.value, "")
        return f"Brief {kind}".strip()

    def build(self, u: Understanding, **_: Any) -> list[Part]:
        parts: list[Part] = []
        top = [n for n in u.salient(14) if not n.is_scaffold]
        if not top:
            return []

        parts.append(Part(
            role="thesis", title="In short",
            body=u.summary or " ".join(n.body for n in top[:3]),
            derived_from=[n.id for n in top[:5]],
        ))

        for unit in self.units(u, "narration"):
            parts.append(Part(
                role="section", title=unit.meta.get("segment", "") or "",
                body=unit.content, derived_from=unit.derived_from, units=[unit],
            ))

        quantities = [n for n in u.of_kind(NodeKind.QUANTITY) if not n.is_scaffold]
        if quantities:
            parts.append(Part(
                role="figures", title="Figures cited",
                body="\n".join(f"- {n.body}" for n in quantities[:10]),
                derived_from=[n.id for n in quantities[:10]],
            ))

        # What the source leaves open is part of understanding it.
        open_qs = [n for n in u.of_kind(NodeKind.QUESTION) if not n.is_scaffold]
        contested = [e for e in u.edges_of(Relation.CONTRADICTS)]
        if open_qs or contested:
            lines = [f"- {n.body}" for n in open_qs[:6]]
            for e in contested[:4]:
                a, b = u.node(e.source), u.node(e.target)
                if a and b:
                    lines.append(f"- The source sets “{a.label}” against “{b.label}”.")
            parts.append(Part(
                role="open", title="Left open",
                body="\n".join(lines),
                derived_from=[n.id for n in open_qs[:6]],
            ))

        rows = self.timeline(u)
        if rows:
            # Ground the timeline in the nodes it names. A navigation aid still
            # points at the source, so it should cite it like anything else.
            labels = {label for _, label in rows[:24]}
            node_ids = [n.id for n in u.nodes if n.label in labels]
            parts.append(Part(
                role="timeline", title="Where this was said",
                body="\n".join(f"- `{loc}` — {label}" for loc, label in rows[:24]),
                derived_from=node_ids[:24], asserts=False,
                meta={"rows": rows[:24]},
            ))
        return parts

    def assemble(self, u: Understanding, parts: list[Part],
                 citations: list[Citation], **_: Any) -> str:
        out = [f"# {u.source.title}", "", f"*{self.subtitle_for(u)}*", ""]
        for part in parts:
            if part.title:
                out.append(f"## {part.title}")
                out.append("")
            out.append(part.body + self.markers(part.footnotes))
            out.append("")
        return "\n".join(out) + self.sources_section(citations)


class GuideFormat(Format):
    name = "guide"
    label = "Study guide"
    job = "Come back to it when you need the detail."
    uses = ("glossary", "summary", "diagram")
    requires = ("nodes",)
    coverage_target = 0.7

    def subtitle_for(self, u: Understanding) -> str:
        return "Reference sheet"

    def build(self, u: Understanding, **_: Any) -> list[Part]:
        parts: list[Part] = []

        top = [n for n in u.salient(8) if not n.is_scaffold]
        if top:
            parts.append(Part(
                role="orientation", title="What this covers",
                body="\n".join(f"- {_bullet(n)}" for n in top[:6]),
                derived_from=[n.id for n in top[:6]],
            ))

        glossary = self.units(u, "glossary")
        if glossary:
            parts.append(Part(
                role="terms", title="Terms",
                body="\n".join(
                    f"- **{x.meta.get('term', '')}** — {x.content}" for x in glossary),
                derived_from=[nid for x in glossary for nid in x.derived_from],
                units=glossary,
            ))

        for proc, steps in u.processes():
            parts.append(Part(
                role="process", title=f"Process — {proc.label}",
                body="\n".join(f"{i}. {s.body or s.label}"
                               for i, s in enumerate(steps, start=1)),
                derived_from=[proc.id] + [s.id for s in steps],
            ))

        claims = [n for n in u.of_kind(NodeKind.CLAIM)
                  if not n.is_scaffold][:14]
        if claims:
            parts.append(Part(
                role="claims", title="Key claims",
                body="\n".join(f"- {n.body}" for n in claims),
                derived_from=[n.id for n in claims],
            ))

        causal = u.edges_of(Relation.CAUSES, Relation.ENABLES, Relation.DEPENDS_ON)
        if causal:
            lines = []
            for e in causal[:10]:
                a, b = u.node(e.source), u.node(e.target)
                if a and b:
                    lines.append(f"- {a.label} → *{e.relation.value.replace('_', ' ')}* → {b.label}")
            if lines:
                parts.append(Part(
                    role="relations", title="How it connects", body="\n".join(lines),
                    derived_from=[i for e in causal[:10] for i in (e.source, e.target)],
                ))

        quantities = [n for n in u.of_kind(NodeKind.QUANTITY) if not n.is_scaffold]
        if quantities:
            parts.append(Part(
                role="numbers", title="Numbers to know",
                body="\n".join(f"- {n.body}" for n in quantities[:10]),
                derived_from=[n.id for n in quantities[:10]],
            ))
        return parts

    def assemble(self, u: Understanding, parts: list[Part],
                 citations: list[Citation], **_: Any) -> str:
        out = [f"# {u.source.title} — study guide", ""]
        for part in parts:
            out += [f"## {part.title}", "", part.body + self.markers(part.footnotes), ""]
        return "\n".join(out) + self.sources_section(citations)
