"""Ingest contract: any medium in, one normalized Source + Section outline out."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from ..models import Medium, Section, Source, Span

HEADING_MAX = 70


def looks_like_heading(line: str, following: str = "") -> bool:
    """
    A heading in extracted PDF text has no markup to give it away: it is a
    short line, it does not end in sentence punctuation, and what follows
    starts a new sentence.

    Evaluation finding: without this, "4.2 The Kula Ring" fused with the
    sentence beneath it and produced flashcards reading "4.2 The Kula Ring The
    kula is a system of ceremonial exchange described by ______".
    """
    line = line.strip()
    if not (3 <= len(line) <= HEADING_MAX):
        return False
    if line[-1] in ".!?,;:":
        return False
    if following and not (following.lstrip()[:1].isupper() or following.lstrip()[:1].isdigit()):
        return False
    words = line.split()
    if len(words) > 12:
        return False
    # numbered ("4.1 Forms of Reciprocity") or title-cased
    if re.match(r"^\d+(\.\d+)*[.)]?\s+\S", line):
        return True
    caps = sum(1 for w in words if w[:1].isupper())
    return caps >= max(1, len(words) - 2)


# Every parser takes a string that might be a path or might be the document
# itself, and resolves it here. That makes this function the single place where
# a user-supplied string can become a filesystem read -- so it is also the
# single place to stop one.
#
# The refusal has to live here rather than in the caller. An earlier version
# refused `kind=path` up in the request handler and considered the job done;
# the security suite then posted `kind=text` with the value "/etc/passwd" and
# got the file back, because the *parser* resolved it independently, exactly as
# it was written to. A choke point only helps if the check is at the choke
# point.
_allowed_roots: ContextVar[Optional[tuple[Path, ...]]] = ContextVar(
    "ingest_allowed_roots", default=None)


@contextmanager
def only_read_from(*roots: Path):
    """
    Restrict path resolution to these directories for the duration of a block.

    Used by the job runner in a multi-user deployment: uploads and downloaded
    URLs are staged into a temporary directory, and that directory is the only
    place ingestion may read from. Anything else -- /etc/passwd,
    /proc/self/environ, the application's own source -- resolves to None
    whatever route the string arrived by.
    """
    resolved = tuple(Path(r).resolve() for r in roots if r)
    token = _allowed_roots.set(resolved)
    try:
        yield
    finally:
        _allowed_roots.reset(token)


def as_path(candidate: str) -> Optional[Path]:
    """Return a Path only if `candidate` plausibly is one, exists, and is
    somewhere this context is allowed to read.

    Raw document text gets passed to parsers too, and long text blows up
    os.stat with ENAMETOOLONG -- so guard before touching the filesystem.
    """
    if not candidate or len(candidate) > 4096 or "\n" in candidate:
        return None
    try:
        p = Path(candidate).expanduser()
        if not (p.exists() and p.is_file()):
            return None
        roots = _allowed_roots.get()
        if roots is None:
            return p                       # unrestricted: CLI and local use
        real = p.resolve()
        # `is_relative_to` on the *resolved* path, so a symlink pointing out of
        # the staging directory does not count as being inside it.
        return p if any(real.is_relative_to(root) for root in roots) else None
    except (OSError, ValueError):
        return None


class Ingested:
    def __init__(self, source: Source, sections: Optional[list[Section]] = None):
        self.source = source
        self.sections = sections or []


class Parser(ABC):
    """A parser owns one input medium. Adding an input type = adding one of these."""

    medium: Medium
    extensions: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, path_or_text: str, *, title: Optional[str] = None) -> Ingested: ...

    def handles(self, path: str) -> bool:
        if not path or len(path) > 4096 or "\n" in path:
            return False
        try:
            return Path(path).suffix.lower() in self.extensions
        except (OSError, ValueError):
            return False

    @staticmethod
    def section(title: str, level: int, source_id: str, start: int, end: int,
                locator: Optional[str] = None, physical: bool = False,
                page: Optional[int] = None) -> Section:
        return Section(
            title=title,
            level=level,
            physical=physical,
            page=page,
            span=Span(source_id=source_id, start=start, end=end, locator=locator),
        )
