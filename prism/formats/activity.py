"""
Activity — practice until you can do it.

The least commoditized format and the one with the best evidence behind it.
Five activity types, each chosen by what the IR actually contains rather than
by preference:

    drill     recall and cloze practice        <- claims, definitions, numbers
    sequence  put the stages in order          <- processes with steps
    sort      classify items into categories   <- is_a / part_of hierarchy
    apply     a scenario needing the concept   <- examples and causal claims
    roleplay  a simulation with decisions      <- processes + contested claims

Role-play is a simulation, not a quiz: a situation, a role, decision points,
and a debrief that maps every decision back to the passage it tests.
"""
from __future__ import annotations

import json
import random
from typing import Any

from ..cite import Citation
from ..models import NodeKind, Relation, Understanding
from .base import Format, Part

ROLEPLAY_SYSTEM = """You write short professional role-play simulations for learners.

A simulation has a situation, one role the learner occupies, three to five
decision points, and a debrief. Every decision point must turn on something the
supplied points actually assert -- never on invented facts, names, or numbers.
Options should be genuinely arguable; avoid one obviously correct answer.
The debrief explains which source point bears on each decision."""

ROLEPLAY_SCHEMA = {
    "type": "object",
    "properties": {
        "situation": {"type": "string"},
        "role": {"type": "string"},
        "goal": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "prompt": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "consequence": {"type": "string"},
                },
                "required": ["ref", "prompt", "options"],
            },
        },
    },
    "required": ["situation", "role", "decisions"],
}


class ActivityFormat(Format):
    name = "activity"
    label = "Activity"
    job = "Practice it until you can do it."
    uses = ("retrieval", "diagram")
    requires = ("testable",)
    artifact_format = "json"
    coverage_target = 0.5

    TYPES = ("drill", "sequence", "sort", "apply", "roleplay")

    def subtitle_for(self, u: Understanding) -> str:
        return "Practice set"

    def build(self, u: Understanding, *, types: str | None = None,
              limit: int = 20, **_: Any) -> list[Part]:
        wanted = set((types or ",".join(self.TYPES)).split(","))
        parts: list[Part] = []

        if "drill" in wanted:
            parts += self._drill(u, limit)
        if "sequence" in wanted:
            parts += self._sequence(u)
        if "sort" in wanted:
            parts += self._sort(u)
        if "apply" in wanted:
            parts += self._apply(u)
        if "roleplay" in wanted:
            parts += self._roleplay(u)
        return parts

    # ------------------------------------------------------------------
    def _drill(self, u: Understanding, limit: int) -> list[Part]:
        units = self.units(u, "retrieval", limit=limit)
        return [Part(
            role="drill", title=unit.kind.replace("_", " ").title(),
            body=unit.content, derived_from=unit.derived_from, units=[unit],
            meta={"type": "drill", "item_kind": unit.kind,
                  "answer": unit.meta.get("answer")},
        ) for unit in units]

    def _sequence(self, u: Understanding) -> list[Part]:
        out = []
        for proc, steps in u.processes():
            if len(steps) < 3:
                continue
            scrambled = [s.label for s in steps]
            random.Random(proc.id).shuffle(scrambled)  # noqa: S311  (shuffling distractors, not keys)
            out.append(Part(
                role="sequence", title=f"Order the stages — {proc.label}",
                body="Put these in the order the source gives.",
                derived_from=[proc.id] + [s.id for s in steps], asserts=False,
                meta={"type": "sequence", "scrambled": scrambled,
                      "answer": [s.label for s in steps]},
            ))
        return out

    def _sort(self, u: Understanding) -> list[Part]:
        """Categories come from IS_A / PART_OF parents with several children."""
        tree = u.hierarchy()
        buckets: dict[str, list[str]] = {}
        for parent, children in tree.items():
            node = u.node(parent)
            if not node or node.is_scaffold:
                continue
            kids = [c for c in (u.node(i) for i in children)
                    if c and not c.is_scaffold and c.kind is not NodeKind.STEP]
            if len(kids) >= 2:
                buckets[node.label] = [k.label for k in kids[:6]]
        if len(buckets) < 2:
            return []
        items = [(item, cat) for cat, items_ in buckets.items() for item in items_]
        random.Random(u.id).shuffle(items)  # noqa: S311  (shuffling distractors, not keys)
        node_ids = [n.id for n in u.nodes if n.label in dict(items)]
        return [Part(
            role="sort", title="Sort each item under the right heading",
            body="Every item belongs to exactly one of the categories.",
            derived_from=node_ids[:24], asserts=False,
            meta={"type": "sort", "categories": list(buckets),
                  "items": [i for i, _ in items],
                  "answer": {i: c for i, c in items}},
        )]

    def _apply(self, u: Understanding) -> list[Part]:
        out = []
        causal = u.edges_of(Relation.CAUSES, Relation.ENABLES, Relation.DEPENDS_ON)
        for e in causal[:4]:
            a, b = u.node(e.source), u.node(e.target)
            if not a or not b or a.is_scaffold or b.is_scaffold:
                continue
            out.append(Part(
                role="apply", title="Apply it",
                body=f"A case where “{a.label}” holds. What should you expect, "
                     f"and why does the source say so?",
                derived_from=[a.id, b.id], asserts=False,
                meta={"type": "apply", "answer": b.body or b.label,
                      "relation": e.relation.value},
            ))
        examples = [n for n in u.of_kind(NodeKind.EXAMPLE) if not n.is_scaffold][:3]
        for n in examples:
            out.append(Part(
                role="apply", title="Work the example",
                body=f"The source gives this case: {n.body}. Which principle "
                     f"does it illustrate, and where would it break down?",
                derived_from=[n.id], asserts=False,
                meta={"type": "apply", "answer": n.body},
            ))
        return out

    def _roleplay(self, u: Understanding) -> list[Part]:
        """A simulation grounded in what the document actually claims."""
        processes = u.processes()
        claims = [n for n in u.of_kind(NodeKind.CLAIM) if not n.is_scaffold]
        anchors = ([steps for _, steps in processes][0] if processes else [])[:5] \
            or claims[:5]
        if len(anchors) < 3:
            return []

        if self.client.name != "mock":
            listing = "\n".join(f"{n.id} | {n.body or n.label}" for n in anchors)
            payload = self.client.structured(
                system=ROLEPLAY_SYSTEM,
                prompt=f'Source: "{u.source.title}".\n\nPoints:\n{listing}',
                schema=ROLEPLAY_SCHEMA, max_tokens=3000,
            )
            if payload.get("decisions"):
                return [Part(
                    role="roleplay",
                    title=f"Simulation — {payload.get('role', 'practitioner')}",
                    body=payload.get("situation", ""),
                    derived_from=[n.id for n in anchors], asserts=False,
                    meta={"type": "roleplay", "role": payload.get("role", ""),
                          "goal": payload.get("goal", ""),
                          "decisions": payload["decisions"]},
                )]

        # Offline: a decision at each stage, each tied to one real point.
        decisions = [{
            "ref": n.id,
            "prompt": f"You reach the point where this matters: “{n.label}”. "
                      f"What do you do, and what are you relying on?",
            "options": ["Follow what the source prescribes",
                        "Take the faster route and accept the risk",
                        "Stop and gather more information"],
            "consequence": n.body or n.label,
        } for n in anchors]
        return [Part(
            role="roleplay",
            title=f"Simulation — working through {u.source.title}",
            body=(f"You are responsible for applying “{u.source.title}” in a real "
                  f"case. Work through each decision, then read the debrief and "
                  f"check your reasoning against the cited passage."),
            derived_from=[n.id for n in anchors], asserts=False,
            meta={"type": "roleplay", "role": "practitioner",
                  "goal": "Apply the material without losing what matters.",
                  "decisions": decisions},
        )]

    # ------------------------------------------------------------------
    def assemble(self, u: Understanding, parts: list[Part],
                 citations: list[Citation], **_: Any) -> str:
        by_span = {c.span_id: c for c in citations}

        def cites_for(part: Part) -> list[dict[str, Any]]:
            return [by_span[s.id].to_dict()
                    for s in u.spans_for(part.derived_from)[:3] if s.id in by_span]

        return json.dumps({
            "activity": u.source.title,
            "understanding_id": u.id,
            "source": {"title": u.source.title, "medium": u.source.medium.value,
                       "uri": u.source.uri},
            "sections": [{
                "id": p.id, "type": p.meta.get("type", p.role), "title": p.title,
                "prompt": p.body, "meta": {k: v for k, v in p.meta.items()
                                           if k != "type"},
                "footnotes": p.footnotes,
                "cite": cites_for(p),
            } for p in parts],
        }, indent=2, ensure_ascii=False)
