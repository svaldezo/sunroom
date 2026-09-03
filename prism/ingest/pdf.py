"""PDF. Page boundaries are preserved as locators so citations stay real."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..models import Medium, Source
from .base import Ingested, Parser, looks_like_heading


class PdfParser(Parser):
    medium = Medium.PDF
    extensions = (".pdf",)

    def parse(self, path_or_text: str, *, title: Optional[str] = None) -> Ingested:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("PDF support needs: pip install 'prism[pdf]'") from exc

        p = Path(path_or_text)
        reader = PdfReader(str(p))
        chunks, sections, cursor = [], [], 0
        src_id_holder: list[str] = []

        src = Source(title=title or p.stem, medium=self.medium, uri=str(p))
        src_id_holder.append(src.id)

        for i, page in enumerate(reader.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if not page_text:
                continue
            block = page_text + "\n\n"
            sections.append(self.section(
                f"Page {i}", 1, src.id, cursor, cursor + len(block),
                locator=f"p. {i}", physical=True, page=i,
            ))
            chunks.append(block)
            cursor += len(block)

        src.text = "".join(chunks)

        # Pages are physical. Headings inside them are the actual argument
        # structure, so detect and record them as logical sections.
        logical = []
        cursor = 0
        lines = src.text.split("\n")
        for i, line in enumerate(lines):
            start = cursor
            cursor += len(line) + 1
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if looks_like_heading(line, nxt):
                logical.append(self.section(
                    line.strip(), 1, src.id, start, cursor,
                    locator=f"§ {line.strip()}", physical=False,
                ))
        for i, sec in enumerate(logical):
            if sec.span:
                sec.span.end = (logical[i + 1].span.start
                                if i + 1 < len(logical) and logical[i + 1].span
                                else len(src.text))
        sections.extend(logical)
        sections.sort(key=lambda s: (s.span.start if s.span else 0, s.physical))

        if not title:
            meta_title = (reader.metadata or {}).get("/Title") if reader.metadata else None
            first_line = src.text.strip().splitlines()[0].strip() if src.text.strip() else ""
            best = str(meta_title).strip() if meta_title else ""
            if not best and 8 <= len(first_line) <= 120:
                best = first_line
            if best:
                src.title = best[:160]
        src.meta["pages"] = len(reader.pages)
        src.finalize()
        return Ingested(src, sections)
