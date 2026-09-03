"""
Chunking that preserves character offsets.

Offsets are non-negotiable: every extracted node has to point back to an exact
region of the source, so chunks carry their own absolute position.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import SETTINGS

PARA = re.compile(r"\n\s*\n")


@dataclass
class Chunk:
    index: int
    text: str
    start: int
    end: int


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[Chunk]:
    size = size or SETTINGS.chunk_chars
    overlap = overlap or SETTINGS.chunk_overlap
    if not text.strip():
        return []

    # Split on paragraph boundaries, then greedily pack up to `size`.
    bounds, cursor = [], 0
    for m in PARA.finditer(text):
        bounds.append((cursor, m.start()))
        cursor = m.end()
    bounds.append((cursor, len(text)))
    paras = [(s, e) for s, e in bounds if text[s:e].strip()]

    chunks: list[Chunk] = []
    cur_start = paras[0][0] if paras else 0
    cur_end = cur_start
    for _start, e in paras:
        if cur_end > cur_start and (e - cur_start) > size:
            chunks.append(Chunk(len(chunks), text[cur_start:cur_end], cur_start, cur_end))
            cur_start = max(cur_start, cur_end - overlap)
            # snap the overlap back to a whitespace boundary
            ws = text.find(" ", cur_start)
            if 0 <= ws < cur_end:
                cur_start = ws + 1
        cur_end = e
    if cur_end > cur_start:
        chunks.append(Chunk(len(chunks), text[cur_start:cur_end], cur_start, cur_end))
    return chunks
