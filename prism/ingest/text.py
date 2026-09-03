"""Plain text and markdown. Markdown headings become the section outline."""
from __future__ import annotations

import re
from typing import Optional

from ..models import Medium, Source
from .base import Ingested, Parser, as_path

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def blank_fences(text: str) -> str:
    """
    Replace fenced code with spaces of identical length.

    Evaluation finding: code blocks were being extracted as prose claims. They
    cannot simply be deleted -- every span offset downstream would shift -- so
    they are blanked in place, which removes the content and preserves the map.
    """
    return FENCE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


class TextParser(Parser):
    medium = Medium.TEXT
    extensions = (".txt",)

    def parse(self, path_or_text: str, *, title: Optional[str] = None) -> Ingested:
        p = as_path(path_or_text)
        if p:
            text, name = p.read_text(encoding="utf-8", errors="replace"), p.stem
        else:
            text, name = path_or_text, (title or "Untitled")
        src = Source(
            title=title or name, medium=self.medium,
            uri=str(p) if p else None, text=text,
        ).finalize()
        return Ingested(src)


class MarkdownParser(Parser):
    medium = Medium.MARKDOWN
    extensions = (".md", ".markdown")

    def parse(self, path_or_text: str, *, title: Optional[str] = None) -> Ingested:
        p = as_path(path_or_text)
        if p:
            text, name = p.read_text(encoding="utf-8", errors="replace"), p.stem
        else:
            text, name = path_or_text, (title or "Untitled")

        src = Source(
            title=title or name, medium=self.medium,
            uri=str(p) if p else None, text=text,
        ).finalize()

        src.text = blank_fences(src.text)

        matches = list(HEADING.finditer(text))
        # Prefer the document's own H1 over the filename stem: "procedural.md"
        # is a filesystem detail, "Flotation Protocol..." is what it is called.
        if not title and matches and int(len(matches[0].group(1))) == 1:
            src.title = matches[0].group(2).strip()[:160]
        src.finalize()
        sections = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections.append(self.section(
                m.group(2), len(m.group(1)), src.id, start, end,
                locator=f"§ {m.group(2)}",
            ))
        return Ingested(src, sections)
