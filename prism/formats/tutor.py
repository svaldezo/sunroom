"""
Tutor — the only responsive format.

Every other deliverable is fixed the moment it is generated. A tutor adapts to
what the person actually did not understand, which is why it is the format
people reach for at eleven at night.

It is also the format where invention hurts most, so the answer is assembled
from IR nodes and every sentence arrives with the passage it came from. When no
node supports the question, it says so instead of improvising -- a tutor that
bluffs is worse than no tutor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..cite import Citation, citation_for
from ..llm import LLMClient
from ..models import Node, NodeKind, Understanding
from .base import Format, Part

WORD = re.compile(r"[a-z0-9']+")
STOP = {
    "the", "a", "an", "of", "and", "or", "to", "in", "is", "are", "was", "were",
    "that", "this", "it", "as", "for", "on", "with", "by", "at", "from", "be",
    "what", "why", "how", "when", "where", "who", "does", "do", "did", "can",
    "explain", "tell", "me", "about", "i", "you", "my", "please", "mean", "means",
}

ANSWER_SYSTEM = """You answer a learner's question using ONLY the supplied points
from one source document.

- Assert nothing the points do not assert. No outside knowledge, no examples of
  your own, no numbers that are not given.
- If the points do not answer the question, say plainly that this source does
  not cover it, and say what it does cover that is closest.
- Cite by point id in square brackets after each sentence that uses it.
- Answer in three sentences or fewer unless the question needs a list."""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "used": {"type": "array", "items": {"type": "string"}},
        "covered": {"type": "boolean",
                    "description": "false if the source does not address this"},
    },
    "required": ["answer", "used", "covered"],
}


def _tokens(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 2}


def _dedupe(citations: list[Citation]) -> list[Citation]:
    seen, out = set(), []
    for c in citations:
        if c.span_id in seen:
            continue
        seen.add(c.span_id)
        out.append(c)
    return out


@dataclass
class Answer:
    question: str
    text: str
    covered: bool
    nodes: list[Node] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question, "answer": self.text, "covered": self.covered,
            "nodes": [{"id": n.id, "kind": n.kind.value, "label": n.label}
                      for n in self.nodes],
            "citations": [c.to_dict() for c in self.citations],
        }


def retrieve(u: Understanding, question: str, k: int = 6) -> list[Node]:
    """Lexical retrieval over the IR. Cheap, and it never invents a match."""
    q = _tokens(question)
    if not q:
        return []
    scored: list[tuple[float, Node]] = []
    for n in u.nodes:
        if n.is_scaffold:
            continue
        t = _tokens(f"{n.label} {n.body}")
        if not t:
            continue
        overlap = len(q & t)
        if not overlap:
            continue
        score = overlap / len(q) + 0.25 * n.salience
        if n.kind in (NodeKind.DEFINITION, NodeKind.CONCEPT):
            score += 0.15
        scored.append((score, n))
    scored.sort(key=lambda x: -x[0])
    return [n for _, n in scored[:k]]


def ask(u: Understanding, question: str, client: Optional[LLMClient] = None,
        k: int = 6) -> Answer:
    nodes = retrieve(u, question, k)
    if not nodes:
        # Name the topics, not three whole claims — a decline should read like
        # a person telling you what the document is about.
        topics = [n.label for n in u.definitions() if not n.is_scaffold][:4]
        if not topics:
            topics = [n.label for n in u.of_kind(NodeKind.CONCEPT)
                      if not n.is_scaffold][:4]
        if not topics:
            topics = [n.label[:40] for n in u.salient(3) if not n.is_scaffold]
        return Answer(
            question=question, covered=False,
            text=("This source does not cover that. It is about "
                  + ", ".join(topics) + "."),
        )

    if client is not None and client.name != "mock":
        listing = "\n".join(f"{n.id} | {n.kind.value} | {n.body or n.label}"
                            for n in nodes)
        payload = client.structured(
            system=ANSWER_SYSTEM,
            prompt=f'Source: "{u.source.title}"\n\nQuestion: {question}\n\n'
                   f"Points:\n{listing}",
            schema=ANSWER_SCHEMA, max_tokens=900,
        )
        text = (payload.get("answer") or "").strip()
        used = [n for n in nodes if n.id in set(payload.get("used", []))] or nodes[:3]
        if text:
            return Answer(question=question, text=text,
                          covered=bool(payload.get("covered", True)), nodes=used,
                          citations=_dedupe([citation_for(u, s)
                                             for s in u.spans_for([n.id for n in used])])[:4])

    # Offline: quote the strongest matching points verbatim rather than
    # paraphrasing them, which is the honest fallback.
    used = nodes[:3]
    text = " ".join((n.body or n.label).strip().rstrip(".") + "." for n in used)
    return Answer(
        question=question, text=text, covered=True, nodes=used,
        citations=_dedupe([citation_for(u, s)
                           for s in u.spans_for([n.id for n in used])])[:4],
    )


class TutorFormat(Format):
    name = "tutor"
    label = "Tutor"
    job = "Ask it anything, and check every answer."
    uses = ()
    requires = ("nodes",)
    coverage_target = 0.3

    def subtitle_for(self, u: Understanding) -> str:
        return "Grounded Q&A over this source"

    def build(self, u: Understanding, *, questions: int = 8, **_: Any) -> list[Part]:
        """
        The deliverable is a starting point, not a transcript: the questions
        this source can actually answer, each with a worked answer and its
        citation. The live conversation happens through `ask()`.
        """
        parts: list[Part] = []
        seeds = self._seed_questions(u, questions)
        for q, nodes in seeds:
            answer = ask(u, q, self._client)
            parts.append(Part(
                role="exchange", title=q, body=answer.text,
                derived_from=[n.id for n in (answer.nodes or nodes)],
                meta={"question": q, "covered": answer.covered},
            ))

        outside = [n.label for n in u.salient(6) if not n.is_scaffold]
        parts.append(Part(
            role="boundary", title="What this source cannot answer",
            body=("Questions outside " + ", ".join(outside[:4]) +
                  " will be answered with “this source does not cover that”, "
                  "rather than guessed."),
            derived_from=[], asserts=False,
        ))
        return parts

    @staticmethod
    def _decap(label: str, text: str) -> str:
        """Lower a label's first word only when the source treats it as common.

        `label.lower()` turned "Roughly 80 percent of kula voyages in
        Malinowski's" into "…in malinowski's". Lowercasing only the first
        character is nearly right, but wrong when the label opens on a name. So
        ask the source: if the first word also appears lowercased somewhere in
        it, it is an ordinary word and can be lowered; if it never does, it is a
        proper noun and is left alone.
        """
        if not label:
            return label
        head = label.split(maxsplit=1)[0].strip("\u2019'\".,;:()")
        if not head or not head[0].isupper() or head.isupper():
            return label
        if re.search(rf"(?<![.!?\u2019'\"]\s)\b{re.escape(head.lower())}\b", text):
            return label[0].lower() + label[1:]
        return label

    def _seed_questions(self, u: Understanding, limit: int) -> list[tuple[str, list[Node]]]:
        out: list[tuple[str, list[Node]]] = []
        text = u.source.text
        for n in u.definitions()[:4]:
            if not n.is_scaffold:
                out.append((f"What does {n.label} mean?", [n]))
        for proc, steps in u.processes()[:2]:
            out.append((f"How does {self._decap(proc.label, text)} work?", [proc] + steps))
        for n in u.of_kind(NodeKind.QUANTITY)[:2]:
            if not n.is_scaffold:
                out.append((f"What are the numbers on {self._decap(n.label, text)}?", [n]))
        for n in u.salient(20):
            if len(out) >= limit:
                break
            if not n.is_scaffold and n.kind is NodeKind.CLAIM:
                out.append((f"Why does the source say {self._decap(n.label, text)}?", [n]))
        return out[:limit]

    def assemble(self, u: Understanding, parts: list[Part],
                 citations: list[Citation], **_: Any) -> str:
        out = [f"# {u.source.title} — tutor", "",
               "*Every answer is assembled from this source and cites it. "
               "Questions it cannot answer are declined, not guessed.*", ""]
        for part in parts:
            if part.role == "boundary":
                out += ["---", "", f"**{part.title}**", "", part.body, ""]
                continue
            out += [f"**Q. {part.title}**", "",
                    part.body + self.markers(part.footnotes), ""]
        return "\n".join(out) + self.sources_section(citations)
