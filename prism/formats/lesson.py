"""
Lesson — a teachable session, not a document.

The other formats produce something to consume. A lesson produces something to
*run*: objectives, a sequence with timings, what the teacher does at each step,
what the learner produces, and how you check whether it worked.

Composed from the other formats rather than from renderers directly, which is
the point of having a composition layer at all.
"""
from __future__ import annotations

from typing import Any

from ..cite import Citation
from ..models import NodeKind, Understanding
from .base import Format, Part

# Rough minutes per activity item, used only to sanity-check the plan length.
MINUTES = {"explain": 8, "figure": 4, "practice": 10, "check": 6, "discuss": 8}


class LessonFormat(Format):
    name = "lesson"
    label = "Lesson"
    job = "Teach it to someone else."
    uses = ("summary", "diagram", "glossary", "retrieval")
    requires = ("nodes",)
    coverage_target = 0.6

    def subtitle_for(self, u: Understanding) -> str:
        return "Session plan"

    def build(self, u: Understanding, *, minutes: int = 50, **_: Any) -> list[Part]:
        content = [n for n in u.salient(30) if not n.is_scaffold]
        if len(content) < 3:
            return []
        parts: list[Part] = []

        # Objectives are written from what the source can actually support.
        objectives = []
        for n in u.definitions()[:2]:
            if not n.is_scaffold:
                objectives.append((f"Define {n.label} in your own words.", n))
        for proc, _steps in u.processes()[:1]:
            objectives.append((f"Carry out {proc.label.lower()} in the right order.", proc))
        for n in [x for x in content if x.kind is NodeKind.CLAIM][:2]:
            objectives.append((f"Explain why {n.label.lower()}.", n))
        if not objectives:
            objectives = [(f"Summarize {u.source.title}.", content[0])]

        parts.append(Part(
            role="objectives", title="By the end, learners can",
            body="\n".join(f"{i}. {text}" for i, (text, _) in enumerate(objectives, 1)),
            derived_from=[n.id for _, n in objectives], asserts=False,
            meta={"count": len(objectives)},
        ))

        terms = [n for n in u.definitions() if not n.is_scaffold][:6]
        if terms:
            parts.append(Part(
                role="prior", title="Vocabulary to front-load",
                body="\n".join(f"- **{n.label}** — {n.meta.get('definiens') or n.body}"
                               for n in terms),
                derived_from=[n.id for n in terms],
            ))

        # ---- the sequence -------------------------------------------------
        opening = content[:3]
        parts.append(Part(
            role="segment", title="Open — the question the material answers",
            body="\n".join(f"- {n.body}" for n in opening),
            derived_from=[n.id for n in opening],
            meta={"move": "explain", "minutes": MINUTES["explain"],
                  "teacher": "Pose the problem before naming it. Do not define yet.",
                  "learner": "Write one sentence predicting the answer."},
        ))

        figures = self.units(u, "diagram")
        if figures:
            nodes = [n for n in (u.node(i) for x in figures for i in x.derived_from)
                     if n and not n.is_scaffold][:8]
            parts.append(Part(
                role="segment", title="Show — the structure",
                body="Project the figure and walk it in the order the source gives.",
                derived_from=[n.id for n in nodes], units=figures,
                asserts=False,
                meta={"move": "figure", "minutes": MINUTES["figure"],
                      "teacher": "Trace the arrows aloud; name each node as you reach it.",
                      "learner": "Redraw it from memory with the projector off."},
            ))

        middle = content[3:9]
        if middle:
            parts.append(Part(
                role="segment", title="Explain — the core claims",
                body="\n".join(f"- {n.body}" for n in middle),
                derived_from=[n.id for n in middle],
                meta={"move": "explain", "minutes": MINUTES["explain"],
                      "teacher": "One claim at a time; stop for a question after each.",
                      "learner": "Note which claim they would argue with."},
            ))

        drills = self.units(u, "retrieval", limit=8)
        if drills:
            parts.append(Part(
                role="segment", title="Practice — retrieval, not review",
                body="\n".join(f"- {x.content}" for x in drills[:6]),
                derived_from=[i for x in drills[:6] for i in x.derived_from],
                units=drills[:6], asserts=False,
                meta={"move": "practice", "minutes": MINUTES["practice"],
                      "teacher": "Closed notes. Let them struggle before you help.",
                      "learner": "Answer from memory, then check the citation.",
                      "answers": [x.meta.get("answer") for x in drills[:6]]},
            ))

        # What to argue about. Prefer the source's own open questions; then
        # anything it explicitly sets against something else; then its most
        # abstract claims. A discussion prompt with no source behind it is an
        # ungrounded unit, and the fidelity check is right to fail it.
        from ..models import Relation
        contested = [n for n in content if n.kind is NodeKind.QUESTION][:3]
        if not contested:
            for e in u.edges_of(Relation.CONTRADICTS, Relation.CONTRASTS_WITH)[:3]:
                a, b = u.node(e.source), u.node(e.target)
                if a and b and not a.is_scaffold:
                    contested.append(a)
        if not contested:
            contested = sorted(
                (n for n in content if n.kind is NodeKind.CLAIM),
                key=lambda n: (n.concreteness, -n.salience),
            )[:3]

        if contested:
            parts.append(Part(
                role="segment", title="Discuss — what the source leaves open",
                body="\n".join(
                    f"- {n.body} — where would this stop holding?" for n in contested),
                derived_from=[n.id for n in contested],
                meta={"move": "discuss", "minutes": MINUTES["discuss"],
                      "teacher": "Do not resolve it. Make the disagreement explicit.",
                      "learner": "Take a position and give one reason."},
            ))

        checks = drills[6:10] if len(drills) > 6 else drills[:3]
        parts.append(Part(
            role="assessment", title="Check — did it land",
            body="\n".join(f"{i}. {x.content}" for i, x in enumerate(checks, 1))
                 or "1. Restate the main claim and cite where it appears.",
            derived_from=[i for x in checks for i in x.derived_from],
            units=list(checks), asserts=False,
            meta={"move": "check", "minutes": MINUTES["check"],
                  "answers": [x.meta.get("answer") for x in checks],
                  "rubric": ["States the claim accurately",
                             "Uses the source's own terms correctly",
                             "Points to where in the source it comes from"]},
        ))

        planned = sum(p.meta.get("minutes", 0) for p in parts)
        parts.append(Part(
            role="timing", title="Timing",
            body=f"Planned: {planned} minutes against a {minutes}-minute session."
                 + ("" if planned <= minutes
                    else f" **Over by {planned - minutes} minutes** — cut the "
                         f"discussion segment or move practice to homework."),
            derived_from=[], asserts=False,     # a claim about the plan, not the source
            meta={"planned": planned, "budget": minutes},
        ))
        return parts

    def assemble(self, u: Understanding, parts: list[Part],
                 citations: list[Citation], **_: Any) -> str:
        out = [f"# {u.source.title} — lesson plan", "",
               f"*{self.subtitle_for(u)}*", ""]
        for part in parts:
            out.append(f"## {part.title}")
            if part.meta.get("minutes"):
                out.append(f"*{part.meta['minutes']} min*")
            out += ["", part.body + self.markers(part.footnotes), ""]
            if part.meta.get("teacher"):
                out.append(f"> **Teacher:** {part.meta['teacher']}")
            if part.meta.get("learner"):
                out.append(f"> **Learner produces:** {part.meta['learner']}")
            if part.meta.get("rubric"):
                out += ["", "**Rubric**"] + [f"- {r}" for r in part.meta["rubric"]]
            out.append("")
        return "\n".join(out) + self.sources_section(citations)
