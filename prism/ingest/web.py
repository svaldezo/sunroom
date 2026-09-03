"""HTML / URL. Strips chrome, keeps heading structure."""
from __future__ import annotations

from typing import Optional

from ..models import Medium, Source
from .base import Ingested, Parser, as_path


class WebParser(Parser):
    medium = Medium.HTML
    extensions = (".html", ".htm")

    def parse(self, path_or_text: str, *, title: Optional[str] = None) -> Ingested:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("HTML support needs: pip install 'prism[web]'") from exc

        raw, uri = path_or_text, None
        if path_or_text.startswith(("http://", "https://")):
            # Through the guarded fetcher, never a bare client. A user-supplied
            # URL is a request the server makes from inside its own network;
            # see prism/net/outbound.py for what that is worth to an attacker.
            from ..net.outbound import fetch as safe_fetch
            got = safe_fetch(path_or_text)
            uri, raw = got.final_url, got.text
        else:
            p = as_path(path_or_text)
            if p:
                raw, uri = p.read_text(encoding="utf-8", errors="replace"), str(p)

        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "aside", "form"]):
            tag.decompose()

        doc_title = title or (soup.title.string.strip() if soup.title and soup.title.string else "Untitled")
        src = Source(title=doc_title, medium=self.medium, uri=uri)

        parts, sections, cursor = [], [], 0
        for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote"]):
            txt = el.get_text(" ", strip=True)
            if not txt:
                continue
            block = txt + "\n\n"
            if el.name.startswith("h"):
                sections.append(self.section(
                    txt, int(el.name[1]), src.id, cursor, cursor + len(block), locator=f"§ {txt}",
                ))
            parts.append(block)
            cursor += len(block)

        src.text = "".join(parts)
        src.finalize()

        # A heading section must span everything up to the NEXT heading.
        # Evaluation finding: sections that covered only their own heading text
        # left 40% of nodes unattached and half the spans without a locator.
        for i, sec in enumerate(sections):
            if not sec.span:
                continue
            sec.span.end = (sections[i + 1].span.start
                            if i + 1 < len(sections) and sections[i + 1].span
                            else len(src.text))
        return Ingested(src, sections)
