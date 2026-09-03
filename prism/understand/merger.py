"""
Cross-chunk consolidation.

Chunk overlap and repetition mean the same unit of meaning gets extracted more
than once. Merging keeps ONE node and unions its provenance -- so a merged node
ends up better cited than either original, not worse.
"""
from __future__ import annotations

import difflib
import re
from collections import defaultdict

from ..models import Edge, Node

STOP = {
    "the", "a", "an", "of", "and", "or", "to", "in", "is", "are", "was", "were",
    "that", "this", "it", "as", "for", "on", "with", "by", "at", "from", "be",
}
WORD = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 2}


def _similar(a: Node, b: Node) -> float:
    if a.kind != b.kind:
        return 0.0
    ta, tb = _tokens(a.body or a.label), _tokens(b.body or b.label)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    ratio = difflib.SequenceMatcher(None, a.label.lower(), b.label.lower()).ratio()
    return max(jaccard, ratio * 0.9)


def merge_nodes(nodes: list[Node], edges: list[Edge], threshold: float = 0.82
                ) -> tuple[list[Node], list[Edge], dict[str, str]]:
    """Returns (merged_nodes, rewritten_edges, old_id -> canonical_id)."""
    buckets: dict[str, list[Node]] = defaultdict(list)
    for n in nodes:
        buckets[n.kind.value].append(n)

    remap: dict[str, str] = {}
    kept: list[Node] = []

    for _, group in buckets.items():
        group.sort(key=lambda n: (-n.salience, n.id))
        canon: list[Node] = []
        for node in group:
            match = next((c for c in canon if _similar(c, node) >= threshold), None)
            if match is None:
                canon.append(node)
                remap[node.id] = node.id
                continue
            remap[node.id] = match.id
            # union provenance -- this is the point of merging
            for sid in node.provenance:
                if sid not in match.provenance:
                    match.provenance.append(sid)
            match.salience = max(match.salience, node.salience)
            match.concreteness = max(match.concreteness, node.concreteness)
            match.confidence = max(match.confidence, node.confidence)
            if len(node.body) > len(match.body):
                match.body = node.body
            if node.label.lower() != match.label.lower() and node.label not in match.aliases:
                match.aliases.append(node.label)
            if match.order is None:
                match.order = node.order
        kept.extend(canon)

    seen: set[tuple[str, str, str]] = set()
    rewritten: list[Edge] = []
    for e in edges:
        s, t = remap.get(e.source, e.source), remap.get(e.target, e.target)
        if s == t:
            continue
        edge = Edge(source=s, target=t, relation=e.relation,
                    provenance=e.provenance, confidence=e.confidence)
        if edge.key in seen:
            continue
        seen.add(edge.key)
        rewritten.append(edge)

    kept.sort(key=lambda n: -n.salience)
    return kept, rewritten, remap
