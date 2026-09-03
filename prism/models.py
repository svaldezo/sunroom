"""
The canonical intermediate representation (IR).

This is the core asset of the system. Everything upstream (ingest) normalizes
INTO it; everything downstream (renderers) reads FROM it. No renderer ever
touches raw source text -- it queries structure. That is what makes adding a
new output medium one renderer instead of one bespoke pipeline.

Three layers:
  1. Source + Span   -- where something was said (provenance, always resolvable)
  2. Node + Edge     -- what was said and how the pieces relate (the semantics)
  3. Understanding   -- the container, plus structural queries renderers use
"""
from __future__ import annotations

import hashlib
import uuid
from enum import Enum
from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Layer 1: provenance
# --------------------------------------------------------------------------

class Medium(str, Enum):
    TEXT = "text"
    MARKDOWN = "markdown"
    PDF = "pdf"
    HTML = "html"
    AUDIO = "audio"
    VIDEO = "video"
    SLIDES = "slides"
    IMAGE = "image"


class Source(BaseModel):
    """A single ingested artifact, normalized to plain text plus locators."""
    id: str = Field(default_factory=lambda: _uid("src"))
    title: str
    medium: Medium
    uri: Optional[str] = None
    text: str = ""
    checksum: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)

    def finalize(self) -> "Source":
        self.checksum = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        return self


class Span(BaseModel):
    """
    A resolvable pointer back into a Source. Every claim the system makes must
    be traceable to at least one of these -- that is the whole fidelity story.
    """
    id: str = Field(default_factory=lambda: _uid("spn"))
    source_id: str
    start: int                       # char offset into Source.text
    end: int
    # Human-facing locator, medium-dependent: page 14, 00:12:33, section 2.1
    locator: Optional[str] = None
    t_start: Optional[float] = None  # seconds, for time-based media
    t_end: Optional[float] = None
    #: Structural coordinates used to build a deep link into the original.
    page: Optional[int] = None       # 1-indexed, paginated formats
    line: Optional[int] = None       # 1-indexed, line-oriented formats

    def excerpt(self, source: Source, limit: int = 320) -> str:
        raw = source.text[self.start : self.end].strip()
        return raw if len(raw) <= limit else raw[: limit - 1] + "…"

    def quote(self, source: Source) -> str:
        """The exact text, unabbreviated. This is what gets cited."""
        return source.text[self.start : self.end].strip()

    def context(self, source: Source, window: int = 90) -> tuple[str, str]:
        """Text immediately before and after -- needed for web text fragments."""
        before = source.text[max(0, self.start - window) : self.start]
        after = source.text[self.end : self.end + window]
        return before.strip(), after.strip()


# --------------------------------------------------------------------------
# Layer 2: semantics
# --------------------------------------------------------------------------

class NodeKind(str, Enum):
    CONCEPT = "concept"        # a named thing or idea
    DEFINITION = "definition"  # term + what it means
    CLAIM = "claim"            # an assertion that could be true or false
    PROCESS = "process"        # an ordered procedure or mechanism
    STEP = "step"              # one stage within a process
    EXAMPLE = "example"        # a concrete instance of something abstract
    QUANTITY = "quantity"      # a number with a unit and a referent
    EVENT = "event"            # something time-anchored
    QUESTION = "question"      # an open question the source raises


class Relation(str, Enum):
    IS_A = "is_a"
    PART_OF = "part_of"
    DEFINES = "defines"
    EXEMPLIFIES = "exemplifies"
    CAUSES = "causes"
    ENABLES = "enables"
    PRECEDES = "precedes"
    CONTRASTS_WITH = "contrasts_with"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    DEPENDS_ON = "depends_on"
    MEASURES = "measures"
    ELABORATES = "elaborates"


class Node(BaseModel):
    """One unit of meaning extracted from a source."""
    id: str = Field(default_factory=lambda: _uid("nod"))
    kind: NodeKind
    label: str                                  # short handle, <= ~8 words
    body: str = ""                              # the full statement
    aliases: list[str] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)   # Span ids
    salience: float = 0.5      # how central to the source (0-1)
    difficulty: float = 0.5    # how hard for a newcomer (0-1)
    concreteness: float = 0.5  # 0 = abstract, 1 = depictable -- drives image work
    confidence: float = 0.8    # extractor confidence
    order: Optional[int] = None  # position within a parent process
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """Normalized identity used for cross-chunk deduplication."""
        return self.label.strip().lower().rstrip(".")

    @property
    def is_scaffold(self) -> bool:
        """
        Structural bookkeeping rather than content: section anchors and nodes
        the linker synthesized to hold a graph together.

        Evaluation finding: a synthesized process node whose body read "The
        procedure described under 'Procedure'" became a flashcard, and clozing
        it produced 'The ______ described under "______".' Scaffolding must
        never reach a learner.
        """
        if self.meta.get("has_content"):
            # A heading that IS a real definition ("## Structuration") gets
            # merged into the content node. It anchors a section AND carries
            # meaning -- treating it as scaffolding hid the definition from
            # the glossary, the card deck and the tutor.
            return False
        return bool(self.meta.get("section") or self.meta.get("synthesized"))


class Edge(BaseModel):
    id: str = Field(default_factory=lambda: _uid("edg"))
    source: str            # Node id
    target: str            # Node id
    relation: Relation
    provenance: list[str] = Field(default_factory=list)
    confidence: float = 0.8

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.target, self.relation.value)


class Section(BaseModel):
    """
    The source's own outline -- preserved because order carries meaning.

    `physical` marks a division that is an artifact of the container rather
    than of the argument: PDF pages, transcript timestamps, paragraph blocks.
    They make excellent citation locators and terrible concept hierarchies,
    so renderers that show structure must be able to tell the two apart.
    """
    id: str = Field(default_factory=lambda: _uid("sec"))
    title: str
    level: int = 1
    span: Optional[Span] = None
    node_ids: list[str] = Field(default_factory=list)
    physical: bool = False
    page: Optional[int] = None


# --------------------------------------------------------------------------
# Layer 3: the container
# --------------------------------------------------------------------------

class Understanding(BaseModel):
    """
    The durable, structured representation of one source. This is the thing
    that persists, that gets re-rendered, and that a fidelity check runs against.
    """
    id: str = Field(default_factory=lambda: _uid("und"))
    source: Source
    spans: list[Span] = Field(default_factory=list)
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    summary: str = ""
    collection: Optional[str] = None
    meta: dict[str, Any] = Field(default_factory=dict)

    # -- lookups -----------------------------------------------------------
    def node(self, node_id: str) -> Optional[Node]:
        return self._node_index.get(node_id)

    def span(self, span_id: str) -> Optional[Span]:
        return self._span_index.get(span_id)

    @property
    def _node_index(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    @property
    def _span_index(self) -> dict[str, Span]:
        return {s.id: s for s in self.spans}

    # -- structural queries the renderers actually use ---------------------
    def of_kind(self, *kinds: NodeKind) -> list[Node]:
        wanted = set(kinds)
        return [n for n in self.nodes if n.kind in wanted]

    def salient(self, limit: int = 20, kinds: Optional[Iterable[NodeKind]] = None) -> list[Node]:
        pool = self.nodes if kinds is None else self.of_kind(*kinds)
        return sorted(pool, key=lambda n: -n.salience)[:limit]

    def out_edges(self, node_id: str, relation: Optional[Relation] = None) -> list[Edge]:
        return [
            e for e in self.edges
            if e.source == node_id and (relation is None or e.relation == relation)
        ]

    def in_edges(self, node_id: str, relation: Optional[Relation] = None) -> list[Edge]:
        return [
            e for e in self.edges
            if e.target == node_id and (relation is None or e.relation == relation)
        ]

    def edges_of(self, *relations: Relation) -> list[Edge]:
        wanted = set(relations)
        return [e for e in self.edges if e.relation in wanted]

    def processes(self) -> list[tuple[Node, list[Node]]]:
        """Every PROCESS node with its STEP nodes in order."""
        out: list[tuple[Node, list[Node]]] = []
        for proc in self.of_kind(NodeKind.PROCESS):
            step_ids = [e.source for e in self.in_edges(proc.id, Relation.PART_OF)]
            steps = [n for n in (self.node(i) for i in step_ids) if n is not None]
            steps.sort(key=lambda n: (n.order if n.order is not None else 999, n.label))
            if steps:
                out.append((proc, steps))
        return out

    def hierarchy(self) -> dict[str, list[str]]:
        """parent_node_id -> [child_node_id] via PART_OF / IS_A."""
        tree: dict[str, list[str]] = {}
        for e in self.edges_of(Relation.PART_OF, Relation.IS_A):
            tree.setdefault(e.target, []).append(e.source)
        return tree

    def roots(self, include_physical: bool = False) -> list[Node]:
        """
        Nodes nothing else contains -- the top of the outline.

        Physical anchors (PDF pages, transcript timestamps, paragraph blocks)
        are excluded by default: they are perfect for citations and useless as
        concept hierarchy, and a mind map rooted on "Page 1 / Page 2 / Page 3"
        shows the container instead of the argument.
        """
        contained = {e.source for e in self.edges_of(Relation.PART_OF, Relation.IS_A)}
        roots = [n for n in self.nodes
                 if n.id not in contained and n.kind != NodeKind.STEP
                 and (include_physical or not n.meta.get("physical"))]
        return sorted(roots, key=lambda n: -n.salience)

    def is_physical(self, node_id: str) -> bool:
        node = self.node(node_id)
        return bool(node and node.meta.get("physical"))

    def definitions(self) -> list[Node]:
        return sorted(
            (n for n in self.of_kind(NodeKind.DEFINITION, NodeKind.CONCEPT)
             if not n.meta.get("synthesized")),
            key=lambda n: (-n.salience, n.label.lower()),
        )

    def depictable(self, threshold: float = 0.55) -> list[Node]:
        """Nodes concrete enough that an image would help rather than mislead."""
        return sorted(
            [n for n in self.nodes if n.concreteness >= threshold],
            key=lambda n: -n.concreteness,
        )

    def testable(self) -> list[Node]:
        """Nodes that carry a checkable fact -- the retrieval-practice pool."""
        return [
            n for n in self.of_kind(
                NodeKind.CLAIM, NodeKind.DEFINITION, NodeKind.QUANTITY,
                NodeKind.EVENT, NodeKind.PROCESS,
            )
            if not n.is_scaffold
        ]

    def spans_for(self, node_ids: Iterable[str]) -> list[Span]:
        idx, seen, out = self._span_index, set(), []
        for nid in node_ids:
            node = self.node(nid)
            if not node:
                continue
            for sid in node.provenance:
                if sid in seen:
                    continue
                seen.add(sid)
                if sid in idx:
                    out.append(idx[sid])
        return out

    def outline(self) -> list["Section"]:
        """
        The sections a reader would recognize as the document's structure.

        A PDF now carries both pages and headings; iterating all of them made
        narration emit every passage twice, once under its page and once under
        its heading. Logical structure wins when it exists; physical divisions
        remain available for citations.
        """
        logical = [s for s in self.sections if not s.physical]
        return logical or list(self.sections)

    def stats(self) -> dict[str, int]:
        counts = {k.value: 0 for k in NodeKind}
        for n in self.nodes:
            counts[n.kind.value] += 1
        counts["edges"] = len(self.edges)
        counts["spans"] = len(self.spans)
        return counts


# --------------------------------------------------------------------------
# Render output contract
# --------------------------------------------------------------------------

class RenderUnit(BaseModel):
    """
    One addressable piece of output -- a paragraph of narration, a diagram node,
    a flashcard, a glossary entry. `derived_from` is mandatory: an output unit
    that cannot name the IR nodes it came from is, by definition, unsupported.
    """
    id: str = Field(default_factory=lambda: _uid("unt"))
    kind: str                                    # renderer-specific: "paragraph", "card", ...
    content: str
    derived_from: list[str] = Field(default_factory=list)   # Node ids
    meta: dict[str, Any] = Field(default_factory=dict)


class RenderResult(BaseModel):
    id: str = Field(default_factory=lambda: _uid("rnd"))
    understanding_id: str
    renderer: str
    tier: str = "production"        # production | beta | experimental
    format: str = "text"            # text | markdown | mermaid | json | ssml
    units: list[RenderUnit] = Field(default_factory=list)
    artifact: str = ""              # the assembled output
    meta: dict[str, Any] = Field(default_factory=dict)
