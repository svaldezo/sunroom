"""
IR -> Mermaid.

The purest expression of the architecture: this renderer calls NO model. The
graph is already the diagram. That makes it deterministic, free, instant, and
100% faithful by construction -- there is no generation step in which anything
could be invented.
"""
from __future__ import annotations

import re
from typing import Any

from ..models import NodeKind, Relation, RenderUnit, Understanding
from .base import Renderer

ARROWS = {
    Relation.CAUSES: ("-->", "causes"),
    Relation.ENABLES: ("-->", "enables"),
    Relation.PRECEDES: ("-->", ""),
    Relation.DEPENDS_ON: ("-.->", "depends on"),
    Relation.CONTRASTS_WITH: ("-.-", "vs"),
    Relation.SUPPORTS: ("-->", "supports"),
    Relation.CONTRADICTS: ("-.->", "contradicts"),
    Relation.PART_OF: ("-->", ""),
    Relation.IS_A: ("-->", "is a"),
    Relation.DEFINES: ("-->", "defines"),
    Relation.EXEMPLIFIES: ("-.->", "e.g."),
    Relation.MEASURES: ("-->", "measures"),
    Relation.ELABORATES: ("-.->", ""),
}

SHAPES = {
    NodeKind.PROCESS: ("[[", "]]"),
    NodeKind.STEP: ("[", "]"),
    NodeKind.CLAIM: ("(", ")"),
    NodeKind.DEFINITION: ("[/", "/]"),
    NodeKind.CONCEPT: ("([", "])"),
    NodeKind.EXAMPLE: (">", "]"),
    NodeKind.QUANTITY: ("[(", ")]"),
    NodeKind.EVENT: ("{{", "}}"),
    NodeKind.QUESTION: ("{", "}"),
}


#: Characters that mean something structural to Mermaid. Rewriting brackets
#: into parentheses (the previous approach) simply traded one shape token for
#: another and broke mind maps, whose nodes use parentheses for shapes.
MERMAID_UNSAFE = str.maketrans({
    "[": "", "]": "", "(": "", ")": "", "{": "", "}": "",
    '"': "'", "|": "/", "<": "", ">": "", "#": "", ";": ",", ":": " —",
    "\\": "/", "`": "'",
})


def _safe(text: str, limit: int = 58) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip(".,;:")
    text = text.translate(MERMAID_UNSAFE).strip()
    text = re.sub(r"\s+", " ", text)
    return (text[: limit - 1] + "…") if len(text) > limit else text


def _mid(node_id: str) -> str:
    return "n" + node_id.replace("nod_", "")[:10]


class DiagramRenderer(Renderer):
    name = "diagram"
    tier = "production"
    format = "mermaid"
    description = "Structure as a Mermaid graph. Deterministic — no model call."
    #: A diagram legitimately shows a slice, not the whole document, so it is
    #: not judged against the same coverage bar as a summary.
    coverage_target = 0.15
    #: Appending a footnote list to Mermaid source breaks the parser, so the
    #: diagram's citations ride in `meta` and are shown beside it instead.
    cites_in_artifact = False

    MODES = ("auto", "flow", "mindmap", "causal", "concept")

    def build(self, u: Understanding, *, mode: str = "auto", max_nodes: int = 40,
              **_: Any) -> list[RenderUnit]:
        mode = self._resolve(u, mode)
        builder = {
            "flow": self._flow, "mindmap": self._mindmap,
            "causal": self._causal, "concept": self._concept,
        }[mode]
        units = builder(u, max_nodes)
        for unit in units:
            unit.meta.setdefault("mode", mode)
        return units

    #: below this a "causal map" is two boxes and an arrow, which reads as an
    #: authoritative claim about the document's logic on almost no evidence
    MIN_CAUSAL_EDGES = 3
    MIN_MINDMAP_BRANCHES = 3
    MIN_FLOW_STEPS = 2

    def _resolve(self, u: Understanding, mode: str) -> str:
        if mode != "auto":
            if mode not in self.MODES:
                raise ValueError(f"mode must be one of {self.MODES}")
            return mode
        if any(len(steps) >= self.MIN_FLOW_STEPS for _, steps in u.processes()):
            return "flow"
        causal = u.edges_of(Relation.CAUSES, Relation.ENABLES, Relation.DEPENDS_ON,
                            Relation.CONTRADICTS, Relation.SUPPORTS)
        if len(causal) >= self.MIN_CAUSAL_EDGES:
            return "causal"
        # only worth a mind map if there is real (non-physical) hierarchy
        tree = u.hierarchy()
        logical = [parent for parent in tree
                   if not u.is_physical(parent) and len(tree[parent]) > 1]
        if len(logical) >= self.MIN_MINDMAP_BRANCHES or (logical and not causal):
            return "mindmap"
        return "concept"

    # -- modes -------------------------------------------------------------
    def _flow(self, u: Understanding, max_nodes: int) -> list[RenderUnit]:
        units: list[RenderUnit] = []
        for proc, steps in u.processes():
            if len(steps) < self.MIN_FLOW_STEPS:
                continue
            lines = [f"%% {proc.label}"]
            for a, b in zip(steps, steps[1:]):  # noqa: B905  pairwise; lengths differ by one
                lines.append(f"    {_mid(a.id)}[{_safe(a.label)}] --> {_mid(b.id)}[{_safe(b.label)}]")
            if len(steps) == 1:
                lines.append(f"    {_mid(steps[0].id)}[{_safe(steps[0].label)}]")
            units.append(RenderUnit(
                kind="subgraph", content="\n".join(lines),
                derived_from=[proc.id] + [s.id for s in steps],
                meta={"process": proc.label},
            ))
        return units[:max_nodes]

    def _mindmap(self, u: Understanding, max_nodes: int) -> list[RenderUnit]:
        tree, units, budget = u.hierarchy(), [], [max_nodes]
        seen: set[str] = set()          # a node with two parents drew twice

        def walk(node_id: str, depth: int) -> None:
            if budget[0] <= 0 or depth > 4 or node_id in seen:
                return
            seen.add(node_id)
            node = u.node(node_id)
            if not node or node.meta.get("physical"):
                return
            budget[0] -= 1
            units.append(RenderUnit(
                kind="branch",
                # Indent from 4, not 2. `assemble` puts `root((title))` at
                # indent 2; a branch at the same indent is a SECOND root, which
                # mermaid rejects outright -- every mind map was silently
                # falling back to raw source in the reader.
                content="  " * (depth + 2) + _safe(node.label),
                derived_from=[node.id], meta={"depth": depth},
            ))
            children = sorted(
                (c for c in tree.get(node_id, [])
                 if u.node(c) and not u.is_physical(c)),
                key=lambda c: -(u.node(c).salience if u.node(c) else 0),
            )
            for child in children[:8]:
                walk(child, depth + 1)

        # The document's own title is already the mind-map root; repeating it as
        # the first branch buries every real branch a level deeper for nothing.
        title = _safe(u.source.title).strip().casefold()
        roots = u.roots()[:4]
        for root in roots:
            node = u.node(root.id)
            if node and _safe(node.label).strip().casefold() == title:
                for child in sorted(
                        (c for c in tree.get(root.id, []) if u.node(c)
                         and not u.is_physical(c)),
                        key=lambda c: -(u.node(c).salience if u.node(c) else 0))[:8]:
                    walk(child, 0)
                seen.add(root.id)
                continue
            walk(root.id, 0)
        if len(units) < 2:
            # Fall back to a concept map -- and say so. `build` stamps the
            # requested mode onto any unit that has not claimed one, so a
            # silent fallback used to emit flowchart node syntax wrapped in a
            # `mindmap` header, which mermaid cannot parse at all.
            fallback = self._concept(u, max_nodes)
            for unit in fallback:
                unit.meta["mode"] = "concept"
            return fallback
        return units

    def _causal(self, u: Understanding, max_nodes: int) -> list[RenderUnit]:
        rels = (Relation.CAUSES, Relation.ENABLES, Relation.DEPENDS_ON,
                Relation.CONTRADICTS, Relation.SUPPORTS, Relation.CONTRASTS_WITH)
        units = []
        for e in sorted(u.edges_of(*rels), key=lambda e: -e.confidence)[:max_nodes]:
            a, b = u.node(e.source), u.node(e.target)
            if not a or not b:
                continue
            arrow, label = ARROWS.get(e.relation, ("-->", e.relation.value))
            sa, ea = SHAPES.get(a.kind, ("[", "]"))
            sb, eb = SHAPES.get(b.kind, ("[", "]"))
            edge = f"{arrow}|{label}|" if label else arrow
            units.append(RenderUnit(
                kind="edge",
                content=f"    {_mid(a.id)}{sa}{_safe(a.label)}{ea} {edge} "
                        f"{_mid(b.id)}{sb}{_safe(b.label)}{eb}",
                derived_from=[a.id, b.id],
                meta={"relation": e.relation.value, "confidence": e.confidence},
            ))
        return units

    def _concept(self, u: Understanding, max_nodes: int) -> list[RenderUnit]:
        """
        Fallback. Draws whatever relations exist so the result is a graph rather
        than a wall of disconnected boxes; only isolates the rest if nothing
        connects at all.
        """
        units: list[RenderUnit] = []
        drawn: set[str] = set()
        for e in sorted(u.edges, key=lambda e: -e.confidence):
            if len(units) >= max_nodes:
                break
            a, b = u.node(e.source), u.node(e.target)
            if not a or not b or a.meta.get("physical") or b.meta.get("physical"):
                continue
            arrow, label = ARROWS.get(e.relation, ("-->", ""))
            sa, ea = SHAPES.get(a.kind, ("[", "]"))
            sb, eb = SHAPES.get(b.kind, ("[", "]"))
            edge = f"{arrow}|{label}|" if label else arrow
            units.append(RenderUnit(
                kind="edge",
                content=f"    {_mid(a.id)}{sa}{_safe(a.label)}{ea} {edge} "
                        f"{_mid(b.id)}{sb}{_safe(b.label)}{eb}",
                derived_from=[a.id, b.id], meta={"relation": e.relation.value},
            ))
            drawn |= {a.id, b.id}

        for n in u.salient(max_nodes):
            if len(units) >= max_nodes:
                break
            if n.id in drawn or n.meta.get("physical"):
                continue
            sh, eh = SHAPES.get(n.kind, ("[", "]"))
            units.append(RenderUnit(
                kind="node", content=f"    {_mid(n.id)}{sh}{_safe(n.label)}{eh}",
                derived_from=[n.id],
            ))
        return units

    # -- assembly ----------------------------------------------------------
    def assemble(self, u: Understanding, units: list[RenderUnit], **options: Any) -> str:
        if not units:
            return "%% no structure extracted"
        mode = units[0].meta.get("mode", "concept")
        if mode == "mindmap":
            body = "\n".join(x.content for x in units)
            return f"mindmap\n  root(({_safe(u.source.title, 40)}))\n{body}"
        if mode == "flow":
            blocks = []
            for i, unit in enumerate(units):
                head, *rest = unit.content.split("\n")
                blocks.append(f"  subgraph s{i}[{_safe(unit.meta.get('process', 'Process'), 40)}]\n"
                              + "\n".join(rest) + "\n  end")
            return "flowchart TD\n" + "\n".join(blocks)
        return "flowchart TD\n" + "\n".join(x.content for x in units)
