"""
Audio / video via transcript. Timestamps survive into Span.t_start/t_end, so a
claim rendered into any other medium can still cite "at 00:14:02 they said…".

Accepts .srt / .vtt directly. Raw media files are routed to a transcription
backend (Whisper locally, or a hosted API) via `transcribe`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ..models import Medium, Source
from .base import Ingested, Parser, as_path

TS = re.compile(
    r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})\s*-->\s*"
    r"(?P<h2>\d{2}):(?P<m2>\d{2}):(?P<s2>\d{2})[,.](?P<ms2>\d{3})"
)
MEDIA_EXT = (".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mkv", ".webm")


def _secs(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _clock(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class TranscriptParser(Parser):
    medium = Medium.AUDIO
    extensions = (".srt", ".vtt") + MEDIA_EXT

    def parse(self, path_or_text: str, *, title: Optional[str] = None) -> Ingested:
        p = as_path(path_or_text)
        if p and p.suffix.lower() in MEDIA_EXT:
            raw = self.transcribe(p)
        elif p:
            raw = p.read_text(encoding="utf-8", errors="replace")
        else:
            raw = path_or_text

        src = Source(
            title=title or (p.stem if p else "Transcript"),
            medium=self.medium,
            uri=str(p) if p else None,
        )

        parts, sections, cursor = [], [], 0
        cur_start = cur_end = None
        buffer: list[str] = []

        def flush() -> None:
            nonlocal cursor, buffer, cur_start, cur_end
            if not buffer or cur_start is None:
                buffer = []
                return
            block = " ".join(buffer).strip() + "\n\n"
            sec = self.section(
                _clock(cur_start), 1, src.id, cursor, cursor + len(block),
                locator=f"@ {_clock(cur_start)}", physical=True,
            )
            if sec.span:
                sec.span.t_start, sec.span.t_end = cur_start, cur_end
            sections.append(sec)
            parts.append(block)
            cursor += len(block)
            buffer = []

        for line in raw.splitlines():
            line = line.strip()
            m = TS.search(line)
            if m:
                flush()
                cur_start = _secs(m["h"], m["m"], m["s"], m["ms"])
                cur_end = _secs(m["h2"], m["m2"], m["s2"], m["ms2"])
            elif line and not line.isdigit() and line != "WEBVTT":
                buffer.append(line)
        flush()

        if not parts:  # plain transcript with no timestamps
            src.text = raw
        else:
            src.text = "".join(parts)
        src.finalize()
        return Ingested(src, sections)

    # ------------------------------------------------------------------
    def transcribe(self, path: Path) -> str:
        """
        Hook for speech-to-text. Local faster-whisper if present, else raise
        with a clear instruction rather than silently producing empty text.
        """
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                f"{path.name} needs transcription. Install faster-whisper, or "
                f"pass an .srt/.vtt transcript instead."
            ) from exc

        model = WhisperModel("base", compute_type="int8")
        segments, _ = model.transcribe(str(path))
        lines = []
        for i, seg in enumerate(segments, start=1):
            lines += [
                str(i),
                f"{_clock(seg.start)},000 --> {_clock(seg.end)},000",
                seg.text.strip(),
                "",
            ]
        return "\n".join(lines)
