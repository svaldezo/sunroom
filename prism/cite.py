"""
Attribution.

A citation that a reader cannot follow is decoration. Every output unit
resolves to a `Citation` carrying three things:

  1. a human locator      -- "p. 3 · § 4.2 The Kula Ring"
  2. the exact quote      -- verbatim, never paraphrased
  3. a resolvable anchor  -- a link that opens the original AT that place

The anchor is medium-specific, and each form is a real, dereferenceable
standard rather than an internal convention:

  PDF          file:///…/chapter.pdf#page=3          (PDF Open Parameters)
  Web page     https://…#:~:text=prefix-,exact,-suffix  (W3C Text Fragments)
  Audio/video  https://…#t=372.5                     (Media Fragments URI)
  Text/MD      file:///…/notes.md#L42                (line anchor)
  Anything     prism://<understanding>/<span>        (in-app, always works)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote as urlquote

from .models import Medium, Source, Span, Understanding

WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return WS.sub(" ", text).strip()


def _clock(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _file_url(uri: str) -> str:
    p = Path(uri)
    try:
        return p.resolve().as_uri()
    except (ValueError, OSError):
        return "file://" + urlquote(str(p))


def _is_web(uri: Optional[str]) -> bool:
    return bool(uri and uri.startswith(("http://", "https://")))


# --------------------------------------------------------------------------

@dataclass
class Citation:
    span_id: str
    source_title: str
    medium: str
    locator: str
    quote: str
    anchor: str                      # the deep link
    anchor_kind: str                 # pdf | textfragment | media | line | internal
    uri: Optional[str] = None
    page: Optional[int] = None
    timestamp: Optional[float] = None
    line: Optional[int] = None
    start: int = 0
    end: int = 0

    def label(self) -> str:
        return f"{self.source_title} — {self.locator}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id, "source": self.source_title,
            "medium": self.medium, "locator": self.locator, "quote": self.quote,
            "anchor": self.anchor, "anchor_kind": self.anchor_kind,
            "uri": self.uri, "page": self.page, "timestamp": self.timestamp,
            "line": self.line, "start": self.start, "end": self.end,
        }


# --------------------------------------------------------------------------

def text_fragment(quote: str, before: str = "", after: str = "") -> str:
    """
    Build a W3C Text Fragment (`#:~:text=`), which browsers resolve by
    scrolling to and highlighting the passage.

    Long quotes are expressed as `start,end` rather than one enormous exact
    match, because an exact fragment fails if a single character differs.
    Prefix and suffix disambiguate a phrase that appears more than once.
    """
    q = _norm(quote)
    if not q:
        return ""

    def enc(part: str) -> str:
        return urlquote(part, safe="")

    words = q.split()
    if len(words) > 12:
        head = " ".join(words[:5])
        tail = " ".join(words[-5:])
        core = f"{enc(head)},{enc(tail)}"
    else:
        core = enc(q)

    parts = []
    pre = " ".join(_norm(before).split()[-4:])
    suf = " ".join(_norm(after).split()[:4])
    if pre:
        parts.append(f"{enc(pre)}-,")
    parts.append(core)
    if suf:
        parts.append(f",-{enc(suf)}")
    return "#:~:text=" + "".join(parts)


def anchor_for(source: Source, span: Span) -> tuple[str, str]:
    """Returns (anchor, kind) — the deepest link this medium supports."""
    uri = source.uri
    internal = f"prism://{source.id}/{span.id}"

    # time-based media: Media Fragments URI
    if span.t_start is not None and uri:
        base = uri if _is_web(uri) else _file_url(uri)
        return f"{base}#t={span.t_start:g}", "media"

    if not uri:
        return internal, "internal"

    if source.medium is Medium.PDF and span.page:
        return f"{_file_url(uri)}#page={span.page}", "pdf"

    if source.medium is Medium.HTML and _is_web(uri):
        return uri, "textfragment"        # fragment appended by `citation_for`

    if span.line:
        return f"{_file_url(uri)}#L{span.line}", "line"

    return _file_url(uri), "internal"


def citation_for(u: Understanding, span: Span) -> Citation:
    quote = span.quote(u.source)
    anchor, kind = anchor_for(u.source, span)

    if kind == "textfragment":
        before, after = span.context(u.source)
        anchor = anchor + text_fragment(quote, before, after)

    locator = span.locator or f"chars {span.start}–{span.end}"
    if span.t_start is not None and "@" not in locator:
        locator = f"{_clock(span.t_start)} · {locator}"

    return Citation(
        span_id=span.id, source_title=u.source.title, medium=u.source.medium.value,
        locator=locator, quote=quote, anchor=anchor, anchor_kind=kind,
        uri=u.source.uri, page=span.page, timestamp=span.t_start,
        line=span.line, start=span.start, end=span.end,
    )


def citations_for(u: Understanding, node_ids: Iterable[str]) -> list[Citation]:
    return [citation_for(u, s) for s in u.spans_for(node_ids)]


def index_citations(u: Understanding, blocks: Iterable[Any]
                    ) -> tuple[list[Citation], dict[str, list[int]]]:
    """
    Number every distinct cited span once across a whole deliverable.

    Shared by renderers and formats so a footnote means the same thing whether
    it appears in a diagram caption or the third segment of a podcast. `blocks`
    is anything with `.id` and `.derived_from`.
    """
    order: dict[str, int] = {}
    citations: list[Citation] = []
    per_block: dict[str, list[int]] = {}
    for block in blocks:
        marks: list[int] = []
        for span in u.spans_for(getattr(block, "derived_from", []) or []):
            if span.id not in order:
                order[span.id] = len(citations) + 1
                citations.append(citation_for(u, span))
            marks.append(order[span.id])
        per_block[block.id] = marks
    return citations, per_block


# ------------------------------------------------------------------ export --

def _slug(text: str, limit: int = 28) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())[:limit] or "source"


def to_markdown(citations: list[Citation], *, style: str = "footnote") -> str:
    """Markdown footnotes with live links."""
    if not citations:
        return ""
    out = []
    for i, c in enumerate(citations, start=1):
        out.append(f"[^{i}]: [{c.source_title} — {c.locator}]({c.anchor}) "
                   f"“{_norm(c.quote)[:240]}”")
    return "\n".join(out)


def to_bibtex(u: Understanding, citations: list[Citation]) -> str:
    key = _slug(u.source.title)
    kind = {"pdf": "book", "html": "online", "audio": "misc",
            "video": "misc"}.get(u.source.medium.value, "misc")
    lines = [f"@{kind}{{{key},",
             f"  title = {{{u.source.title}}},",
             f"  note = {{ingested via prism; {len(citations)} cited passage(s)}},"]
    if u.source.uri:
        lines.append(f"  url = {{{u.source.uri}}},")
    pages = sorted({c.page for c in citations if c.page})
    if pages:
        lines.append(f"  pages = {{{', '.join(str(p) for p in pages)}}},")
    lines.append("}")
    return "\n".join(lines)


def to_csl_json(u: Understanding, citations: list[Citation]) -> str:
    kind = {"pdf": "book", "html": "webpage", "audio": "broadcast",
            "video": "motion_picture"}.get(u.source.medium.value, "document")
    entry: dict[str, Any] = {
        "id": _slug(u.source.title), "type": kind, "title": u.source.title,
    }
    if u.source.uri:
        entry["URL"] = u.source.uri
    locs = [c.locator for c in citations]
    if locs:
        entry["note"] = "cited: " + "; ".join(dict.fromkeys(locs))
    return json.dumps([entry], indent=2)


def to_anki_tsv(rows: list[tuple[str, str, Citation | None]]) -> str:
    """
    Anki import: front, back, source. Tabs separate fields, so they are
    stripped; the citation link rides along in the third field.
    """
    out = []
    for front, back, cite in rows:
        src = ""
        if cite:
            src = f'<a href="{cite.anchor}">{cite.source_title} — {cite.locator}</a>'
        out.append("\t".join(
            _norm(x).replace("\t", " ") if isinstance(x, str) else x
            for x in (front, back, src)
        ))
    return "\n".join(out)
