"""
IR -> illustrated glossary (word to picture).

The `concreteness` field earns its keep here. An abstract relation rendered as
a literal picture teaches something false, and the user came precisely because
they cannot tell. So: concrete nodes get a depiction prompt, abstract nodes get
an explicit analogy that is LABELLED as an analogy, and nothing gets silently
illustrated.

Image generation is a pluggable backend. With none configured, the renderer
emits fully-formed prompts rather than pretending.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from ..models import NodeKind, RenderUnit, Understanding
from ..understand.validate import definiens
from .base import Renderer

SYSTEM = """You write image briefs for a visual glossary used in learning material.

For CONCRETE terms: describe a single, literal, unambiguous scene showing the
thing itself. No text in the image. No metaphor.

For ABSTRACT terms: you may propose a visual analogy, but you must state what
the analogy gets WRONG. A learner who mistakes the analogy for the thing has
been taught an error.

Also write a plain-language gloss: one sentence, no jargon, no circularity
(never define a term using the term)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "gloss": {"type": "string"},
                    "image_prompt": {"type": "string"},
                    "is_analogy": {"type": "boolean"},
                    "analogy_breaks_down": {"type": "string"},
                },
                "required": ["ref", "gloss", "image_prompt"],
            },
        }
    },
    "required": ["entries"],
}

ImageBackend = Callable[[str], str]   # prompt -> url/path


class GlossaryRenderer(Renderer):
    name = "glossary"
    tier = "production"
    format = "markdown"
    description = "Terms as plain-language glosses plus image briefs, gated on concreteness."
    requires = ()
    coverage_target = 0.3
    MIN_TERMS = 2

    def __init__(self, client=None, image_backend: Optional[ImageBackend] = None):
        super().__init__(client)
        self.image_backend = image_backend

    def build(self, u: Understanding, *, limit: int = 25,
              concreteness_floor: float = 0.45, **_: Any) -> list[RenderUnit]:
        terms = [n for n in u.definitions()
                 if not n.is_scaffold and n.kind is NodeKind.DEFINITION][:limit]
        if len(terms) < self.MIN_TERMS:
            # Evaluation finding: falling back to "most salient concepts" built
            # glossaries out of section headings, whose gloss was the heading
            # restated -- circular entries that teach nothing. A medium that
            # does not fit the content should say so, not fabricate.
            raise ValueError(
                f"only {len(terms)} defined term(s) in this document — a "
                f"glossary needs at least {self.MIN_TERMS}. Try 'summary' or "
                f"'retrieval' instead."
            )

        briefs = self._briefs(u, terms)
        units: list[RenderUnit] = []

        for node in terms:
            brief = briefs.get(node.id, {})
            depictable = node.concreteness >= concreteness_floor
            # strip the "X is ..." lead-in so the entry defines rather than echoes
            gloss = brief.get("gloss") or node.meta.get("definiens") \
                or definiens(node.label, node.body or node.label)
            prompt = brief.get("image_prompt", "") if depictable else ""

            image = ""
            if prompt and self.image_backend:
                try:
                    image = self.image_backend(prompt)
                except Exception as exc:      # a failed image must not kill the render
                    image = f"<image generation failed: {exc}>"

            units.append(RenderUnit(
                kind="entry",
                content=gloss,
                derived_from=[node.id],
                meta={
                    "term": node.label,
                    "concreteness": node.concreteness,
                    "depictable": depictable,
                    "image_prompt": prompt,
                    "image": image,
                    "is_analogy": bool(brief.get("is_analogy")),
                    "analogy_breaks_down": brief.get("analogy_breaks_down", ""),
                    "aliases": node.aliases,
                },
            ))
        return units

    def _briefs(self, u: Understanding, terms) -> dict[str, dict[str, Any]]:
        if not terms or self.client.name == "mock":
            return {
                n.id: {
                    "gloss": n.meta.get("definiens") or definiens(n.label, n.body or n.label),
                    "image_prompt": (
                        f"A clear, literal illustration of {n.label.lower()}: "
                        f"{(n.body or '')[:140]}"
                    ) if n.concreteness >= 0.45 else "",
                    "is_analogy": n.concreteness < 0.45,
                }
                for n in terms
            }

        listing = "\n".join(
            f"{n.id} | {n.label} | concreteness={n.concreteness:.2f} | {n.body[:200]}"
            for n in terms
        )
        payload = self.client.structured(
            system=SYSTEM,
            prompt=(f'Terms from "{u.source.title}". Use the id as `ref`.\n\n{listing}'),
            schema=SCHEMA, max_tokens=4096,
        )
        return {e["ref"]: e for e in payload.get("entries", []) if e.get("ref")}

    def assemble(self, u: Understanding, units: list[RenderUnit], **options: Any) -> str:
        lines = [f"# {u.source.title} — illustrated glossary", ""]
        for unit in units:
            m = unit.meta
            lines.append(f"### {m['term']}")
            if m.get("aliases"):
                lines.append(f"*also: {', '.join(m['aliases'][:3])}*")
            lines += ["", unit.content + self.markers(unit.meta.get("footnotes", [])), ""]
            if m.get("image"):
                lines += [f"![{m['term']}]({m['image']})", ""]
            elif m.get("image_prompt"):
                lines += [f"> **Image brief:** {m['image_prompt']}", ""]
            else:
                lines += [f"> _Not illustrated — too abstract to depict honestly "
                          f"(concreteness {m['concreteness']:.2f})._", ""]
            if m.get("analogy_breaks_down"):
                lines += [f"> ⚠️ **Where the analogy fails:** {m['analogy_breaks_down']}", ""]
        return "\n".join(lines) + self.appendix(options.get("_citations", []))
