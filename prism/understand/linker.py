"""
Structural linking.

The extractor sees one chunk at a time and misses document-level structure.
This pass adds the relations that are inferable from position and form rather
than from reading -- cheap, deterministic, and safe to assert.
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..models import Edge, Node, NodeKind, Relation, Understanding

WORD = re.compile(r"[a-z0-9']+")
STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "is", "are", "that", "this", "it", "as", "for"}


def _tokens(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 3}


def _first_span_start(u: Understanding, node: Node) -> int:
    spans = [u.span(s) for s in node.provenance]
    starts = [s.start for s in spans if s]
    return min(starts) if starts else 10**9


def link(u: Understanding) -> Understanding:
    existing = {e.key for e in u.edges}

    def add(src: str, tgt: str, rel: Relation, conf: float = 0.7,
            prov: list[str] | None = None) -> None:
        if src == tgt:
            return
        edge = Edge(source=src, target=tgt, relation=rel, confidence=conf,
                    provenance=prov or [])
        if edge.key not in existing:
            existing.add(edge.key)
            u.edges.append(edge)

    positions = {n.id: _first_span_start(u, n) for n in u.nodes}

    # 1. Attach every node to the section that contains it, and give each
    #    section a CONCEPT node so the outline is part of the graph.
    section_nodes: dict[str, Node] = {}
    #: A heading and the definition it introduces are the SAME concept.
    #: Evaluation finding: "## Structuration" and the extracted definition of
    #: structuration became two nodes, so mind maps drew the branch twice and
    #: the glossary and card deck disagreed about which node was canonical.
    existing_by_key = {n.key: n for n in u.nodes if not n.meta.get("section")}

    for sec in u.sections:
        if not sec.span:
            continue
        twin = existing_by_key.get(sec.title.strip().lower().rstrip("."))
        if twin is not None and not sec.physical:
            twin.meta["section"] = True
            twin.meta["has_content"] = True     # not scaffolding: it says something
            twin.meta["level"] = sec.level
            twin.meta["physical"] = sec.physical
            twin.salience = max(twin.salience, 0.6)
            if sec.span.id not in {sp.id for sp in u.spans}:
                u.spans.append(sec.span)
            if sec.span.id not in twin.provenance:
                twin.provenance.append(sec.span.id)
            section_nodes[sec.id] = twin
            continue
        anchor = Node(
            kind=NodeKind.CONCEPT,
            label=sec.title[:120],
            body=f"Section: {sec.title}",
            provenance=[sec.span.id],
            salience=0.6,
            concreteness=0.2,
            confidence=0.95,
            meta={"section": True, "level": sec.level, "physical": sec.physical},
        )
        if sec.span.id not in {s.id for s in u.spans}:
            u.spans.append(sec.span)
        section_nodes[sec.id] = anchor
        u.nodes.append(anchor)

    for sec in u.sections:
        if not sec.span or sec.id not in section_nodes:
            continue
        parent = section_nodes[sec.id]
        for n in u.nodes:
            if n.id == parent.id or n.meta.get("section"):
                continue
            pos = positions.get(n.id, 10**9)
            if sec.span.start <= pos < sec.span.end:
                sec.node_ids.append(n.id)
                add(n.id, parent.id, Relation.PART_OF, 0.9, [sec.span.id])

    # nest sections by heading level
    stack: list[tuple[int, Node]] = []
    for sec in u.sections:
        node = section_nodes.get(sec.id)
        if not node:
            continue
        while stack and stack[-1][0] >= sec.level:
            stack.pop()
        if stack:
            add(node.id, stack[-1][1].id, Relation.PART_OF, 0.95)
        stack.append((sec.level, node))

    # 2. Steps: order them, chain PRECEDES, and bind them to a process.
    steps = sorted(u.of_kind(NodeKind.STEP), key=lambda n: positions.get(n.id, 0))
    for i, s in enumerate(steps):
        if s.order is None:
            s.order = i + 1
    for a, b in zip(steps, steps[1:]):  # noqa: B905  pairwise; lengths differ by one
        add(a.id, b.id, Relation.PRECEDES, 0.8)

    processes = u.of_kind(NodeKind.PROCESS)
    if steps and not processes:
        # Name it for the section the steps live in. Falling back to the source
        # title produced cards reading "Explain: procedural" -- the filename,
        # which tells a learner nothing.
        first_pos = positions.get(steps[0].id, 0)
        host = next(
            (sec for sec in u.sections
             if sec.span and not sec.physical
             and sec.span.start <= first_pos < sec.span.end),
            None,
        )
        proc_name = (host.title if host else u.source.title)[:120]
        proc = Node(
            kind=NodeKind.PROCESS,
            label=proc_name,
            body=f"The procedure described under “{proc_name}”.",
            provenance=[sid for s in steps[:3] for sid in s.provenance],
            salience=0.8, concreteness=0.4, confidence=0.6,
            meta={"synthesized": True},
        )
        u.nodes.append(proc)
        processes = [proc]
    for s in steps:
        owner = min(
            processes,
            key=lambda p: abs(positions.get(p.id, 0) - positions.get(s.id, 0)),
            default=None,
        )
        if owner:
            add(s.id, owner.id, Relation.PART_OF, 0.75)

    # 3. Definitions define the concept that shares their label.
    by_key: dict[str, list[Node]] = defaultdict(list)
    for n in u.nodes:
        by_key[n.key].append(n)
        for alias in n.aliases:
            by_key[alias.strip().lower()].append(n)
    for d in u.of_kind(NodeKind.DEFINITION):
        for other in by_key.get(d.key, []):
            if other.id != d.id and other.kind is NodeKind.CONCEPT:
                add(d.id, other.id, Relation.DEFINES, 0.85)

    # 4. Causal hints from the extractor, resolved to the nearest prior node.
    ordered = sorted(u.nodes, key=lambda n: positions.get(n.id, 0))
    for i, n in enumerate(ordered):
        if not n.meta.get("causal_hint") or i == 0:
            continue
        prev = ordered[i - 1]
        if prev.meta.get("section") or n.meta.get("section"):
            continue
        if _tokens(prev.body) & _tokens(n.body):
            add(prev.id, n.id, Relation.CAUSES, 0.5)

    # 5. Term mentions: any node whose text uses a defined term elaborates on it.
    #
    #    Evaluation finding: for PDFs and transcripts the ONLY structure was
    #    physical (pages, timestamps), so every concept diagram came out as a
    #    wall of disconnected boxes. Anchoring nodes to the terms they invoke
    #    gives every document a concept graph independent of its container.
    from .validate import _is_valid_term

    terms: list[tuple[Node, re.Pattern]] = []
    for t in u.of_kind(NodeKind.DEFINITION, NodeKind.CONCEPT):
        if t.meta.get("section") or not _is_valid_term(t.label):
            continue
        names = [t.label] + [a for a in t.aliases if _is_valid_term(a)]
        pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b", re.I)
        terms.append((t, pattern))

    for node in u.nodes:
        if node.meta.get("section"):
            continue
        text = f"{node.label} {node.body}"
        for term_node, pattern in terms:
            if term_node.id == node.id:
                continue
            if pattern.search(text):
                add(node.id, term_node.id, Relation.ELABORATES, 0.6,
                    term_node.provenance[:1])

    # 6. Strong lexical overlap between claims -> ELABORATES (conservative).
    claims = [n for n in u.of_kind(NodeKind.CLAIM) if len(n.body) > 40][:60]
    toks = {c.id: _tokens(c.body) for c in claims}
    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            ta, tb = toks[a.id], toks[b.id]
            if not ta or not tb:
                continue
            overlap = len(ta & tb) / min(len(ta), len(tb))
            if overlap >= 0.6:
                lo, hi = (a, b) if a.salience >= b.salience else (b, a)
                add(hi.id, lo.id, Relation.ELABORATES, 0.45)

    return u
