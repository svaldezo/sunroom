"""
IR -> retrieval practice.

Item *form* is chosen by node kind, deterministically:
    definition -> term<->meaning both directions
    quantity   -> cloze on the figure
    process    -> step ordering
    claim      -> cloze on the load-bearing term, or short answer
    causal edge-> "what follows from X"

Each item carries its node id, so review state accrues against the IR itself
and follows the learner across every source in the collection.
"""
from __future__ import annotations

import re
from typing import Any

from ..models import NodeKind, Relation, RenderUnit, Understanding
from ..understand.validate import definiens
from .base import Renderer

STOP = {
    "the", "a", "an", "of", "and", "or", "to", "in", "is", "are", "was", "were",
    "that", "this", "it", "as", "for", "on", "with", "by", "at", "from", "be",
    "not", "but", "they", "their", "its", "which", "than", "then", "when",
}


def _keyword(text: str) -> str | None:
    """The most distinctive content word -- the one worth blanking."""
    words = re.findall(r"\b[A-Za-z][A-Za-z\-']{4,}\b", text)
    ranked = sorted(
        (w for w in words if w.lower() not in STOP),
        key=lambda w: (w[0].isupper(), len(w)), reverse=True,
    )
    return ranked[0] if ranked else None


class RetrievalRenderer(Renderer):
    name = "retrieval"
    tier = "production"
    format = "json"
    description = "Spaced-repetition items with provenance, typed by node kind."
    requires = ("testable",)

    coverage_target = 0.6
    #: Citations are embedded per item in the JSON, so a trailing markdown
    #: footnote section would only corrupt the payload.
    cites_in_artifact = False

    @staticmethod
    def _leaks(prompt: str, answer: str) -> bool:
        """A card that shows its own answer teaches nothing."""
        a = re.sub(r"\s+", " ", str(answer or "")).strip().lower().strip(".,;:")
        p = re.sub(r"\s+", " ", prompt).strip().lower()
        return bool(a) and len(a) > 3 and a in p

    @staticmethod
    def _mask(text: str, term: str, alt: list[str] | None = None) -> str:
        """Hide the term (and its aliases) so a meaning->term card stays honest."""
        out = text
        for variant in [term, *(alt or [])]:
            v = variant.strip()
            if len(v) < 3:
                continue
            out = re.sub(rf"\b{re.escape(v)}\b", "______", out, flags=re.I)
        return out

    def build(self, u: Understanding, *, limit: int = 40,
              difficulty_ceiling: float = 1.0, **_: Any) -> list[RenderUnit]:
        units: list[RenderUnit] = []
        pool = [n for n in u.testable() if n.difficulty <= difficulty_ceiling]
        pool.sort(key=lambda n: -n.salience)

        for node in pool:
            if len(units) >= limit:
                break
            for item in self._items_for(u, node):
                # last-line guard: whatever the provider or node kind, an item
                # that reveals its own answer is dropped rather than shipped.
                if self._leaks(item.content, item.meta.get("answer", "")):
                    continue
                if not str(item.meta.get("answer", "")).strip():
                    continue
                units.append(item)

        # process-ordering items
        for proc, steps in u.processes():
            if len(steps) < 3 or len(units) >= limit:
                continue
            # the process node itself may be scaffolding; its STEPS are real
            units.append(RenderUnit(
                kind="order",
                content=f"Put the stages of {proc.label} in order.",
                derived_from=[proc.id] + [s.id for s in steps],
                meta={"answer": [s.label for s in steps],
                      "scrambled": [s.label for s in sorted(steps, key=lambda s: s.label)]},
            ))

        # causal items
        for e in u.edges_of(Relation.CAUSES, Relation.ENABLES)[:6]:
            a, b = u.node(e.source), u.node(e.target)
            if not a or not b or len(units) >= limit:
                continue
            units.append(RenderUnit(
                kind="short_answer",
                content=f"What follows from: {a.label}?",
                derived_from=[a.id, b.id],
                meta={"answer": b.body or b.label, "relation": e.relation.value},
            ))
        return units[:limit]

    def _items_for(self, u: Understanding, node) -> list[RenderUnit]:
        # PDF extraction leaves hard line breaks mid-sentence; a card is one
        # continuous prompt, so collapse them.
        text = re.sub(r"\s+", " ", (node.body or node.label)).strip()
        out: list[RenderUnit] = []

        if node.kind is NodeKind.DEFINITION:
            meaning = node.meta.get("definiens") or definiens(node.label, text)
            out.append(RenderUnit(
                kind="recall", content=f"What does “{node.label}” mean?",
                derived_from=[node.id],
                meta={"answer": meaning, "direction": "term→meaning"},
            ))
            masked = self._mask(meaning, node.label, node.aliases)
            if "______" not in masked and node.label.lower() in meaning.lower():
                return out          # cannot hide the term; one direction only
            out.append(RenderUnit(
                kind="recall", content=f"Which term is this? “{masked}”",
                derived_from=[node.id],
                meta={"answer": node.label, "direction": "meaning→term"},
            ))
            return out

        if node.kind is NodeKind.QUANTITY:
            m = re.search(r"\d[\d,.]*\s*(?:%|percent)?", text)
            if m:
                out.append(RenderUnit(
                    kind="cloze",
                    content=text[: m.start()] + "______" + text[m.end():],
                    derived_from=[node.id], meta={"answer": m.group(0).strip()},
                ))
                return out

        key = _keyword(text)
        if key and len(text) > 40:
            # blank EVERY occurrence -- one leftover instance gives it away
            out.append(RenderUnit(
                kind="cloze",
                content=re.sub(rf"\b{re.escape(key)}\b", "______", text, flags=re.I),
                derived_from=[node.id], meta={"answer": key},
            ))
        else:
            out.append(RenderUnit(
                kind="short_answer", content=f"Explain: {node.label}",
                derived_from=[node.id], meta={"answer": text},
            ))
        return out

    def assemble(self, u: Understanding, units: list[RenderUnit], **options: Any) -> str:
        import json

        from ..cite import citation_for

        return json.dumps({
            "deck": u.source.title,
            "collection": u.collection,
            "understanding_id": u.id,
            "source": {"title": u.source.title, "medium": u.source.medium.value,
                       "uri": u.source.uri},
            "items": [{
                "id": x.id, "type": x.kind, "prompt": x.content,
                "answer": x.meta.get("answer"),
                "nodes": x.derived_from,
                "footnotes": x.meta.get("footnotes", []),
                # A card without a followable source is a card you cannot check.
                "cite": [citation_for(u, s).to_dict()
                         for s in u.spans_for(x.derived_from)[:3]],
            } for x in units],
        }, indent=2, ensure_ascii=False)
