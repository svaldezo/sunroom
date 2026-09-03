"""
Podcast — a two-voice script built for the ear.

Not "narration with names attached". A conversation has moves a monologue does
not: a cold open that earns attention, a host who asks the question the
listener is already forming, an expert who answers in one idea at a time, and a
recap because a listener cannot scroll back.

Every line still carries its provenance, so the show notes are real citations
rather than a bibliography glued on afterwards.
"""
from __future__ import annotations

import re
from typing import Any

from ..cite import Citation
from ..models import NodeKind, Understanding
from .base import Format, Part

SYSTEM = """You write two-voice audio scripts. HOST asks; EXPERT explains.

Rules:
- Assert only what the supplied points assert. Add no facts, names, or numbers.
- One idea per turn. A listener cannot re-read.
- HOST asks the question a smart newcomer would actually ask next.
- No stage directions, no markdown, no "welcome to the show".
- Contractions and short sentences. This is speech, not prose."""

SCHEMA = {
    "type": "object",
    "properties": {
        "turns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string", "enum": ["HOST", "EXPERT"]},
                    "line": {"type": "string"},
                    "ref": {"type": "string", "description": "id of the point this uses"},
                },
                "required": ["speaker", "line"],
            },
        }
    },
    "required": ["turns"],
}

WPM = 150


class PodcastFormat(Format):
    name = "podcast"
    label = "Podcast"
    job = "Learn it while you are doing something else."
    uses = ("narration", "summary")
    requires = ("nodes",)
    coverage_target = 0.5

    def subtitle_for(self, u: Understanding) -> str:
        return "Two-voice audio script"

    def build(self, u: Understanding, *, segments: int = 5, **_: Any) -> list[Part]:
        top = [n for n in u.salient(40) if not n.is_scaffold]
        if not top:
            return []
        parts: list[Part] = []

        hook = max(top, key=lambda n: n.salience)
        parts.append(Part(
            role="cold_open", title="Cold open",
            body=self._turns(u, [hook], opening=True),
            derived_from=[hook.id],
            meta={"speakers": ["HOST", "EXPERT"]},
        ))

        groups = [x for x in self.units(u, "narration")][:segments]
        if not groups:
            groups = []
            chunk = max(3, len(top) // max(1, segments))
            for _i in range(0, min(len(top), chunk * segments), chunk):
                groups.append(None)

        for i, unit in enumerate(groups, start=1):
            nodes = [n for n in (u.node(x) for x in (unit.derived_from if unit else []))
                     if n and not n.is_scaffold]
            if not nodes:
                continue
            parts.append(Part(
                role="segment",
                title=unit.meta.get("segment", f"Segment {i}") if unit else f"Segment {i}",
                body=self._turns(u, nodes[:6]),
                derived_from=[n.id for n in nodes[:6]],
                meta={"index": i},
            ))

        takeaways = [n for n in top[:4]]
        parts.append(Part(
            role="recap", title="Recap",
            body="HOST: So if someone remembers one thing.\n"
                 + "\n".join(f"EXPERT: {self._speakable(n.body or n.label)}"
                             for n in takeaways),
            derived_from=[n.id for n in takeaways],
        ))
        return parts

    # ------------------------------------------------------------------
    def _turns(self, u: Understanding, nodes, opening: bool = False) -> str:
        if self.client.name != "mock":
            listing = "\n".join(f"{n.id} | {n.kind.value} | {n.body or n.label}"
                                for n in nodes)
            payload = self.client.structured(
                system=SYSTEM,
                prompt=(f'Show topic: "{u.source.title}".\n'
                        f'{"Open the episode with a hook." if opening else "Continue the conversation."}\n\n'
                        f"Points to cover:\n{listing}"),
                schema=SCHEMA, max_tokens=2048,
            )
            turns = payload.get("turns", [])
            if turns:
                return "\n".join(f"{t.get('speaker', 'EXPERT')}: {t.get('line', '')}"
                                 for t in turns if t.get("line"))

        # Offline: alternate host questions with expert answers taken verbatim
        # from the node bodies. Nothing invented, and the shape is right.
        lines: list[str] = []
        if opening:
            lines.append("HOST: Let's start with the part people get wrong.")
        for i, n in enumerate(nodes):
            if not opening or i:
                lines.append(f"HOST: {self._question_for(n, i)}")
            lines.append(f"EXPERT: {self._speakable(n.body or n.label)}")
        return "\n".join(lines)

    #: Offline, a host line that echoes the answer back reads like a bad
    #: transcript. Generic connectives invent nothing and sound like speech.
    CONNECTIVES = (
        "Why does that matter?",
        "How so?",
        "What follows from that?",
        "Can you unpack that?",
        "And what does that change?",
        "Is there more to it?",
    )

    def _question_for(self, node, index: int = 0) -> str:
        label = node.label.rstrip(".").strip()
        if node.kind is NodeKind.DEFINITION and len(label.split()) <= 4:
            return f"What does {label} actually mean?"
        if node.kind is NodeKind.PROCESS and len(label.split()) <= 6:
            return f"How does {label.lower()} work, step by step?"
        if node.kind is NodeKind.QUANTITY:
            return "What do the numbers look like?"
        if node.kind is NodeKind.EXAMPLE:
            return "Can you give me a concrete case?"
        return self.CONNECTIVES[index % len(self.CONNECTIVES)]

    @staticmethod
    def _speakable(text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"[#*_`>]", "", text)
        return text

    # ------------------------------------------------------------------
    def assemble(self, u: Understanding, parts: list[Part],
                 citations: list[Citation], **_: Any) -> str:
        words = sum(len(p.body.split()) for p in parts)
        minutes = max(1, round(words / WPM))
        out = [f"# {u.source.title} — podcast script", "",
               f"*Two voices · ~{words} words · ~{minutes} min at {WPM} wpm*", ""]
        for part in parts:
            out += [f"## {part.title}", "", part.body + self.markers(part.footnotes), ""]

        out += ["---", "", "### Show notes", ""]
        for i, c in enumerate(citations[:20], start=1):
            out.append(f"{i}. [{c.locator}]({c.anchor}) — “{c.quote[:150]}”")
        return "\n".join(out)
