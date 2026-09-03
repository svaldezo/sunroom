"""
Chunk -> partial IR.

The critical step is quote grounding: the model returns a verbatim quote, we
locate it in the source, and that character range becomes the node's Span. A
node whose quote cannot be found is dropped, not guessed. That single rule is
what makes every downstream rendering auditable.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..llm import LLMClient
from ..models import Edge, Node, NodeKind, Relation, Source, Span
from .chunker import Chunk
from .prompts import EXTRACT_PROMPT, EXTRACT_SCHEMA, EXTRACT_SYSTEM

WS = re.compile(r"\s+")


@dataclass
class Extraction:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def _locate(haystack: str, needle: str, window_start: int, window_end: int) -> Optional[tuple[int, int]]:
    """Find a quote in the source. Exact first, then whitespace-normalized, then fuzzy."""
    if not needle:
        return None
    window = haystack[window_start:window_end]

    idx = window.find(needle)
    if idx >= 0:
        return window_start + idx, window_start + idx + len(needle)

    # whitespace-insensitive match
    flat_needle = WS.sub(" ", needle).strip()
    mapping, flat = [], []
    for i, ch in enumerate(window):
        if ch.isspace():
            if flat and flat[-1] == " ":
                continue
            flat.append(" ")
            mapping.append(i)
        else:
            flat.append(ch)
            mapping.append(i)
    flat_s = "".join(flat)
    idx = flat_s.find(flat_needle)
    if idx >= 0 and flat_needle:
        start = mapping[idx]
        end = mapping[min(idx + len(flat_needle) - 1, len(mapping) - 1)] + 1
        return window_start + start, window_start + end

    # last resort: fuzzy, and only if the match is strong
    matcher = difflib.SequenceMatcher(None, window, needle, autojunk=False)
    block = matcher.find_longest_match(0, len(window), 0, len(needle))
    if block.size >= max(24, int(len(needle) * 0.6)):
        return window_start + block.a, window_start + block.a + block.size
    return None


class Extractor:
    def __init__(self, client: LLMClient):
        self.client = client

    def run(self, source: Source, chunk: Chunk) -> Extraction:
        result = self.client.structured(
            system=EXTRACT_SYSTEM,
            prompt=EXTRACT_PROMPT.format(
                title=source.title, medium=source.medium.value, text=chunk.text,
            ),
            schema=EXTRACT_SCHEMA,
            max_tokens=8192,
        )
        return self._materialize(source, chunk, result)

    def _materialize(self, source: Source, chunk: Chunk, payload: dict[str, Any]) -> Extraction:
        out = Extraction()
        ref_to_id: dict[str, str] = {}

        for raw in payload.get("nodes", []) or []:
            # MockClient hands back offsets directly; a real model hands back a quote.
            if "start" in raw and "end" in raw:
                loc = (chunk.start + int(raw["start"]), chunk.start + int(raw["end"]))
            else:
                loc = _locate(source.text, (raw.get("quote") or "").strip(),
                              chunk.start, chunk.end)
            if not loc:
                out.dropped.append(raw.get("label", "<unlabeled>"))
                continue

            try:
                kind = NodeKind(raw.get("kind", "claim"))
            except ValueError:
                kind = NodeKind.CLAIM

            span = Span(source_id=source.id, start=loc[0], end=loc[1])
            node = Node(
                kind=kind,
                label=(raw.get("label") or raw.get("body", ""))[:120].strip(),
                body=(raw.get("body") or "").strip(),
                provenance=[span.id],
                salience=float(raw.get("salience", 0.5) or 0.5),
                difficulty=float(raw.get("difficulty", 0.5) or 0.5),
                concreteness=float(raw.get("concreteness", 0.5) or 0.5),
                confidence=float(raw.get("confidence", 0.8) or 0.8),
                order=raw.get("order"),
                meta={"chunk": chunk.index, "causal_hint": bool(raw.get("causal"))},
            )
            out.spans.append(span)
            out.nodes.append(node)
            if raw.get("ref"):
                ref_to_id[str(raw["ref"])] = node.id

        for raw in payload.get("edges", []) or []:
            s, t = ref_to_id.get(str(raw.get("source"))), ref_to_id.get(str(raw.get("target")))
            if not s or not t or s == t:
                continue
            try:
                rel = Relation(raw.get("relation", "elaborates"))
            except ValueError:
                continue
            out.edges.append(Edge(
                source=s, target=t, relation=rel,
                confidence=float(raw.get("confidence", 0.8) or 0.8),
            ))
        return out
