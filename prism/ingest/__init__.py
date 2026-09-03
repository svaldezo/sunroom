"""
Ingest registry.

Every input medium is one Parser. `ingest()` dispatches by extension or scheme
and always returns the same normalized shape, so the understanding pipeline
never knows or cares what the source was.
"""
from __future__ import annotations

from typing import Optional

from ..models import Medium
from .audio import TranscriptParser
from .base import Ingested, Parser, only_read_from
from .pdf import PdfParser
from .text import MarkdownParser, TextParser
from .web import WebParser

PARSERS: list[Parser] = [
    MarkdownParser(), PdfParser(), WebParser(), TranscriptParser(), TextParser(),
]

BY_MEDIUM = {p.medium: p for p in PARSERS}


def parser_for(target: str) -> Parser:
    if target.startswith(("http://", "https://")):
        return BY_MEDIUM[Medium.HTML]
    for p in PARSERS:
        if p.handles(target):
            return p
    return BY_MEDIUM[Medium.TEXT]


def ingest(target: str, *, title: Optional[str] = None,
           medium: Optional[Medium] = None) -> Ingested:
    """Ingest a path, URL, or raw string into a normalized Source + outline."""
    p = BY_MEDIUM[medium] if medium else parser_for(target)
    return p.parse(target, title=title)


def supported() -> dict[str, list[str]]:
    return {p.medium.value: list(p.extensions) for p in PARSERS}


__all__ = ["ingest", "parser_for", "supported", "Ingested", "Parser",
           "PARSERS", "only_read_from"]
