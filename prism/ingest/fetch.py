"""
Turning a user's "add a source" request into something safe to ingest.

Everything a person can hand the ingester arrives here first. The job is to
decide what a supplied string is *allowed* to become, and the answer depends on
who is running the deployment:

    kind      what it is                 multi-user   local
    -------   ------------------------   ----------   -----
    text      pasted prose               yes          yes
    url       a link                     yes*         yes*
    storage   an object they uploaded    yes          n/a
    path      a filesystem path          NEVER        yes

    * through the SSRF-safe fetcher, never a raw client

`path` is the one that matters. A multi-user deployment accepting a path is
accepting `/etc/passwd`, `/proc/self/environ`, and on a serverless platform the
bundled source of the application itself. It is refused outright, not sanitized,
because sanitizing paths is a game nobody wins.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

from ..config import SETTINGS
from ..models import Medium
from ..net.outbound import fetch

KINDS = ("text", "url", "storage", "path")

# A storage object key: <uuid>/<filename>. Anything else is not one of ours.
STORAGE_KEY = re.compile(
    r"^[0-9a-fA-F-]{36}/[A-Za-z0-9][A-Za-z0-9._ -]{0,180}$")

SUFFIX_MEDIUM = {
    ".md": Medium.MARKDOWN, ".markdown": Medium.MARKDOWN,
    ".pdf": Medium.PDF, ".html": Medium.HTML, ".htm": Medium.HTML,
    ".txt": Medium.TEXT,
    ".srt": Medium.AUDIO, ".vtt": Medium.AUDIO, ".mp3": Medium.AUDIO,
    ".m4a": Medium.AUDIO, ".wav": Medium.AUDIO, ".mp4": Medium.AUDIO,
    ".mov": Medium.AUDIO, ".mkv": Medium.AUDIO, ".webm": Medium.AUDIO,
}


class RefusedInput(ValueError):
    """The input is not something this deployment will read."""


def classify(raw: str) -> str:
    """
    What a bare string looks like, for the convenience API and the CLI.

    Never used to *grant* the path kind in a multi-user deployment -- see
    `materialize`, which refuses it regardless of what this returns.
    """
    s = (raw or "").strip()
    if s.startswith(("http://", "https://")):
        return "url"
    if len(s) < 4096 and "\n" not in s and Path(s).suffix.lower() in SUFFIX_MEDIUM:
        return "path"
    return "text"


def medium_for(name: str) -> Optional[Medium]:
    return SUFFIX_MEDIUM.get(Path(name or "").suffix.lower())


def materialize(spec: dict[str, Any]) -> tuple[str, Optional[Medium], Optional[Path]]:
    """
    Produce (target, medium, readable_root) for `ingest()`, or refuse.

    `target` is either raw text or a path to a file *this function created* in a
    temporary directory. The third element is that directory, or None when there
    is no file involved -- the caller passes it to `only_read_from()` so that a
    parser cannot resolve any other string on disk. Returning it rather than
    leaving the caller to guess is what keeps the guarantee attached to the
    thing it guards.
    """
    kind = (spec.get("kind") or "").strip().lower()
    value = spec.get("value") or ""
    if kind not in KINDS:
        raise RefusedInput(f"unknown source kind {kind!r}")

    if kind == "text":
        text = str(value)
        if len(text) > SETTINGS.max_text_chars:
            raise RefusedInput(
                f"that is {len(text):,} characters; the limit is "
                f"{SETTINGS.max_text_chars:,}")
        if not text.strip():
            raise RefusedInput("there is nothing in that text")
        # No file is involved, so nothing on disk may be read at all.
        return text, medium_for(spec.get("filename") or "") or Medium.MARKDOWN, None

    if kind == "url":
        got = fetch(str(value), max_bytes=min(SETTINGS.max_upload_bytes,
                                              25 * 1024 * 1024))
        suffix = Path(got.final_url.split("?")[0]).suffix.lower()
        medium = SUFFIX_MEDIUM.get(suffix)
        if medium in (Medium.PDF, Medium.AUDIO) or "pdf" in got.content_type:
            # Binary formats have to reach the parser as a file.
            tmp = Path(tempfile.mkdtemp(prefix="sunroom-url-"))
            name = Path(got.final_url.split("?")[0]).name or "download"
            dest = tmp / _safe_name(name)
            dest.write_bytes(got.body)
            return str(dest), medium, tmp
        return got.text, medium or Medium.HTML, None

    if kind == "storage":
        return _from_storage(str(value), spec)

    # kind == "path"
    if SETTINGS.multi_user:
        raise RefusedInput(
            "this deployment does not read files by path; upload the file "
            "instead")
    p = Path(str(value)).expanduser()
    if not p.is_file():
        raise RefusedInput(f"no such file: {value}")
    return str(p), medium_for(p.name), p.parent


def _safe_name(name: str) -> str:
    """
    A filename we are willing to create.

    Only the basename, no separators, no leading dot, and a length cap. The
    input is attacker-chosen: `../../../../tmp/x` and `..%2f..%2fetc%2fpasswd`
    both arrive here looking like ordinary filenames.
    """
    base = os.path.basename(str(name).replace("\\", "/")).strip()
    base = base.replace("\x00", "")
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).lstrip(".") or "source"
    return base[:120]


def _from_storage(key: str,
                  spec: dict[str, Any]) -> tuple[str, Optional[Medium], Path]:
    """
    Download an object the user uploaded, from their own prefix.

    The key is checked against the shape we issue *and* against the owner in
    the job, so a job cannot be created that reads out of another account's
    folder even if the key is guessed correctly.
    """
    key = key.strip().lstrip("/")
    if not STORAGE_KEY.match(key):
        raise RefusedInput("that upload reference is not valid")
    owner = str(spec.get("user_id") or "")
    if owner and not key.startswith(f"{owner}/"):
        raise RefusedInput("that upload belongs to a different account")

    from ..storage import download

    data, filename = download(key)
    tmp = Path(tempfile.mkdtemp(prefix="sunroom-obj-"))
    dest = tmp / _safe_name(filename or key.split("/")[-1])
    dest.write_bytes(data)
    return str(dest), medium_for(dest.name), tmp
