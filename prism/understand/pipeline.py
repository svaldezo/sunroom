"""
The understanding pipeline: Source -> Understanding.

    ingest -> chunk -> extract (parallel) -> merge -> link -> summarize

Run once per source. Everything after this point reads the IR and never the
raw text again.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from ..config import SETTINGS
from ..ingest import Ingested
from ..llm import LLMClient, get_client
from ..models import Understanding
from .chunker import chunk_text
from .extractor import Extractor
from .linker import link
from .merger import merge_nodes
from .validate import validate

Progress = Callable[[str], None]


def prepare(ingested: Ingested, *, collection: Optional[str] = None
            ) -> tuple[Understanding, list]:
    """
    Phase one: everything that costs nothing.

    Builds the Understanding shell -- source, sections, locator scaffolding --
    and works out the chunks to be extracted. Split out from `understand` so a
    long job can do this once, persist it, and then be resumed a few chunks at
    a time by whichever worker picks it up next.
    """
    u = Understanding(source=ingested.source, sections=list(ingested.sections),
                      collection=collection)
    _ensure_sections(u)
    return u, chunk_text(ingested.source.text)


def extract_into(u: Understanding, chunks, client: LLMClient) -> list[str]:
    """
    Phase two, for some subset of the chunks. Returns the dropped quotes.

    Additive and idempotent per chunk: the caller records which chunk indices
    are done, so a worker that dies mid-slice loses at most the chunks it had
    in flight, never the ones already persisted.
    """
    extractor = Extractor(client)
    dropped: list[str] = []

    def work(ch):
        return extractor.run(u.source, ch)

    if len(chunks) > 1 and SETTINGS.max_concurrency > 1:
        with ThreadPoolExecutor(max_workers=SETTINGS.max_concurrency) as pool:
            results = list(pool.map(work, chunks))
    else:
        results = [work(ch) for ch in chunks]

    for r in results:
        u.nodes.extend(r.nodes)
        u.edges.extend(r.edges)
        u.spans.extend(r.spans)
        dropped.extend(r.dropped)
    return dropped


def assemble(u: Understanding, client: LLMClient, *,
             dropped: int = 0, progress: Optional[Progress] = None
             ) -> Understanding:
    """
    Phase three: validate, dedupe, link, summarize.

    Pure CPU apart from one summarization call, and it must run over the whole
    node set at once -- deduplication across chunks is the entire point -- so it
    is the one phase that cannot be sliced.
    """
    say = progress or (lambda _msg: None)

    u.nodes, vstats = validate(u.nodes)
    kept_ids = {n.id for n in u.nodes}
    u.edges = [e for e in u.edges if e.source in kept_ids and e.target in kept_ids]
    if any(vstats.values()):
        say("validated: " + ", ".join(f"{k}={v}" for k, v in vstats.items() if v))

    u.nodes, u.edges, remap = merge_nodes(u.nodes, u.edges)
    for sec in u.sections:
        sec.node_ids = list({remap.get(i, i) for i in sec.node_ids})
    say(f"merged: {len(u.nodes)} node(s) after dedupe")

    u = link(u)
    say(f"linked: {len(u.edges)} edge(s) total")

    _apply_locators(u)
    u.summary = _summarize(u, client)
    u.meta["dropped_ungrounded"] = dropped
    u.meta["validation"] = vstats
    u.meta["provider"] = client.name
    # keep only spans still referenced by a node or section
    live = {sid for n in u.nodes for sid in n.provenance}
    live |= {s.span.id for s in u.sections if s.span}
    u.spans = [s for s in u.spans if s.id in live]
    return u


def understand(
    ingested: Ingested,
    *,
    client: Optional[LLMClient] = None,
    collection: Optional[str] = None,
    progress: Optional[Progress] = None,
) -> Understanding:
    """The whole pipeline, start to finish. Used by the CLI and the tests."""
    client = client or get_client()
    say = progress or (lambda _msg: None)

    u, chunks = prepare(ingested, collection=collection)
    say(f"chunking: {len(chunks)} chunk(s), {len(ingested.source.text):,} chars")

    dropped = extract_into(u, chunks, client)
    say(f"extracted: {len(u.nodes)} node(s), {len(u.edges)} edge(s)"
        + (f", {len(dropped)} dropped for ungrounded quotes" if dropped else ""))

    return assemble(u, client, dropped=len(dropped), progress=progress)


PARA_SPLIT = __import__("re").compile(r"\n\s*\n")


def _ensure_sections(u: Understanding) -> None:
    """
    Guarantee that every character of the source falls inside some section.

    Evaluation finding: plain prose with no headings produced ZERO sections, so
    every citation degraded to a byte offset ("chars 1180-1240") and 90% of
    nodes ended up unlinked. Both are fatal for a tool whose whole promise is
    "you can check where this came from".

    Two repairs:
      - no sections at all  -> synthesize paragraph blocks ("¶ 3")
      - text before the first heading -> add a preamble section
    Both are marked `physical`, so they serve as citation anchors without
    pretending to be the document's argument structure.
    """
    from ..models import Section, Span

    text = u.source.text
    if not text.strip():
        return

    if not u.sections:
        cursor, blocks = 0, []
        for i, part in enumerate(PARA_SPLIT.split(text), start=1):
            start = text.find(part, cursor)
            if start < 0 or not part.strip():
                cursor += len(part)
                continue
            end = start + len(part)
            cursor = end
            blocks.append(Section(
                title=f"Paragraph {i}", level=1, physical=True,
                span=Span(source_id=u.source.id, start=start, end=end,
                          locator=f"\u00b6 {i}"),
            ))
        u.sections = blocks
        return

    first = min((s.span.start for s in u.sections if s.span), default=0)
    if first > 40 and text[:first].strip():
        u.sections.insert(0, Section(
            title="Preamble", level=1, physical=True,
            span=Span(source_id=u.source.id, start=0, end=first, locator="\u00b6 opening"),
        ))


def _apply_locators(u: Understanding) -> None:
    """
    Give every span a human-readable citation from the section that contains it:
    "p. 14", "@ 00:14:02", "§ Methods". Without this a citation is a byte offset,
    which is useless to a person checking whether the output is honest.
    """
    sections = [s for s in u.sections if s.span]
    if not sections:
        return
    sections.sort(key=lambda s: s.span.start)  # type: ignore[union-attr]
    for span in u.spans:
        # Coordinates are assigned to EVERY span, including ones that already
        # carry a display locator -- otherwise section spans end up with a
        # citation label but no followable link.
        containing_all = [s for s in sections
                          if s.span and s.span.start <= span.start < s.span.end]
        page_sec = next((s for s in containing_all if s.page), None)
        if page_sec and span.page is None:
            span.page = page_sec.page
        if span.line is None:
            span.line = u.source.text.count("\n", 0, span.start) + 1

        if span.locator:
            continue
        containing = [s for s in sections
                      if s.span and s.span.start <= span.start < s.span.end]
        if not containing:
            # never fall through to a raw byte offset in front of a user
            nearest = min(sections, key=lambda s: abs(s.span.start - span.start))  # type: ignore[union-attr]
            span.locator = f"near {nearest.span.locator or nearest.title}"  # type: ignore[union-attr]
            continue

        # A PDF span sits inside both a page and a heading. The page is how a
        # reader finds it; the heading is how they know where they are. Give
        # them both, narrowest first.
        physical = [s for s in containing if s.physical]
        logical = [s for s in containing if not s.physical]
        parts = []
        for group in (physical, logical):
            if group:
                best = min(group, key=lambda s: s.span.end - s.span.start)  # type: ignore[union-attr]
                parts.append(best.span.locator or f"§ {best.title}")  # type: ignore[union-attr]
                if best.span.t_start is not None and span.t_start is None:  # type: ignore[union-attr]
                    span.t_start = best.span.t_start  # type: ignore[union-attr]
                    span.t_end = best.span.t_end      # type: ignore[union-attr]
        span.locator = " · ".join(dict.fromkeys(parts))


def _summarize(u: Understanding, client: LLMClient) -> str:
    # Scaffolding ("Section: 00:00:00") is structure, not content, and it was
    # leaking into the opening line of every brief.
    top = [n for n in u.salient(24) if not n.is_scaffold][:12]
    if not top:
        return ""
    if client.name == "mock":
        return " ".join(n.body for n in top[:3])[:600]
    bullets = "\n".join(f"- [{n.kind.value}] {n.body}" for n in top)
    return client.text(
        system="Write a tight abstract of a document from its extracted key "
               "points. Assert only what the points support. No preamble.",
        prompt=f'Document: "{u.source.title}"\n\nKey points:\n{bullets}\n\n'
               f"Write 3-5 sentences.",
        max_tokens=600,
    ).strip()
