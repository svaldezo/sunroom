"""
Where uploaded files live.

Two backends, one interface. Supabase Storage in production; the local
filesystem when there is no Supabase, so the whole upload path is exercised by
the tests and by anyone running this on their own machine.

The important design choice is that **a file never passes through the API
function's body**. The browser asks for a signed upload URL, PUTs the bytes
straight to storage, and then tells us the key. A 90 MB lecture recording
travelling through a serverless function would hit the request-body limit,
blow the memory budget, and burn the invocation's whole time budget on I/O
that the storage service does better.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import SETTINGS

# What a person may upload. Anything else is refused at the point the upload is
# requested, so the bytes are never even accepted.
ALLOWED_SUFFIXES = {
    ".pdf", ".txt", ".md", ".markdown", ".html", ".htm",
    ".srt", ".vtt", ".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mkv", ".webm",
}

CONTENT_TYPES = {
    ".pdf": "application/pdf", ".txt": "text/plain",
    ".md": "text/markdown", ".markdown": "text/markdown",
    ".html": "text/html", ".htm": "text/html",
    ".srt": "text/plain", ".vtt": "text/vtt",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
    ".mp4": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm",
}


class StorageError(RuntimeError):
    """Storage refused, or is not reachable."""


@dataclass
class UploadTicket:
    key: str
    url: str
    method: str = "PUT"
    headers: Optional[dict] = None
    token: str = ""
    expires_in: int = 900

    def to_dict(self) -> dict:
        return {"key": self.key, "url": self.url, "method": self.method,
                "headers": self.headers or {}, "token": self.token,
                "expires_in": self.expires_in}


def safe_filename(name: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base.replace("\x00", "")).lstrip(".")
    return (base or "source")[:120]


def check_upload(filename: str, size: int) -> str:
    """Validate before a single byte is accepted. Returns the safe filename."""
    name = safe_filename(filename)
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise StorageError(
            f"Sunroom cannot read {suffix or 'that kind of file'} yet. It "
            f"handles {', '.join(sorted(ALLOWED_SUFFIXES))}.")
    if size <= 0:
        raise StorageError("that file is empty")
    if size > SETTINGS.max_upload_bytes:
        raise StorageError(
            f"that file is {size // 1_000_000} MB; the limit is "
            f"{SETTINGS.max_upload_bytes // 1_000_000} MB")
    return name


def key_for(user_id: str, filename: str) -> str:
    """
    Per-account prefix. This is also what the storage RLS policy keys on, so
    the path layout is a security boundary, not just tidiness.
    """
    return f"{user_id}/{int(time.time())}-{safe_filename(filename)}"


def _supabase() -> bool:
    return bool(SETTINGS.supabase_url and SETTINGS.supabase_service_key)


# -- Supabase --------------------------------------------------------------

def _sb_request(method: str, path: str, *, body=None, headers=None,
                timeout: float = 30.0):
    import httpx

    url = f"{SETTINGS.supabase_url}/storage/v1{path}"
    hdrs = {"Authorization": f"Bearer {SETTINGS.supabase_service_key}",
            "apikey": SETTINGS.supabase_service_key}
    hdrs.update(headers or {})
    with httpx.Client(timeout=timeout, trust_env=False) as c:
        resp = c.request(method, url, headers=hdrs, content=body)
    if resp.status_code >= 400:
        raise StorageError(f"storage {resp.status_code}: {resp.text[:200]}")
    return resp


def _sb_signed_upload(key: str) -> UploadTicket:
    resp = _sb_request("POST", f"/object/upload/sign/{SETTINGS.storage_bucket}/"
                               f"{urllib.parse.quote(key)}",
                       headers={"Content-Type": "application/json"},
                       body=json.dumps({}).encode())
    data = resp.json()
    # Supabase returns a relative signed path; make it absolute for the browser.
    signed = data.get("url") or ""
    url = (f"{SETTINGS.supabase_url}/storage/v1{signed}"
           if signed.startswith("/") else signed)
    return UploadTicket(key=key, url=url, method="PUT",
                        token=data.get("token", ""))


def _sb_download(key: str) -> bytes:
    resp = _sb_request("GET", f"/object/{SETTINGS.storage_bucket}/"
                              f"{urllib.parse.quote(key)}", timeout=120.0)
    return resp.content


def _sb_delete(key: str) -> None:
    _sb_request("DELETE", f"/object/{SETTINGS.storage_bucket}/"
                          f"{urllib.parse.quote(key)}")


# -- local filesystem ------------------------------------------------------

def _local_root() -> Path:
    root = SETTINGS.home / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _local_path(key: str) -> Path:
    """
    Resolve a key inside the upload root, and refuse anything that escapes it.

    The key reaches this function from a request. `..%2f..%2f` normalises to a
    path outside the root, and `Path.resolve()` is what turns that into
    something checkable.
    """
    root = _local_root().resolve()
    candidate = (root / key).resolve()
    if not candidate.is_relative_to(root):
        raise StorageError("that upload reference is not valid")
    return candidate


# -- the interface ---------------------------------------------------------

def signed_upload(user_id: str, filename: str, size: int) -> UploadTicket:
    """A ticket the browser can PUT bytes to, straight to storage."""
    name = check_upload(filename, size)
    key = key_for(user_id, name)
    if _supabase():
        return _sb_signed_upload(key)
    # Locally there is no signing service, so the API accepts the bytes itself.
    return UploadTicket(key=key, url=f"/api/uploads/{urllib.parse.quote(key)}",
                        method="PUT")


def put(key: str, data: bytes) -> None:
    """Used by the local backend's upload endpoint and by the tests."""
    if _supabase():
        _sb_request("POST", f"/object/{SETTINGS.storage_bucket}/"
                            f"{urllib.parse.quote(key)}", body=data)
        return
    dest = _local_path(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def download(key: str) -> tuple[bytes, str]:
    """Fetch an object. Returns (bytes, original filename)."""
    filename = key.split("/")[-1]
    if _supabase():
        return _sb_download(key), filename
    src = _local_path(key)
    if not src.is_file():
        raise StorageError("that upload is no longer available")
    return src.read_bytes(), filename


def delete(key: str) -> None:
    try:
        if _supabase():
            _sb_delete(key)
            return
        path = _local_path(key)
        if path.is_file():
            path.unlink()
    except StorageError:
        pass          # a leftover object is not worth failing a delete over


def purge_user(user_id: str) -> None:
    """Everything an account uploaded, for account deletion."""
    if _supabase():
        return                                    # handled by cascade policies
    folder = _local_root() / user_id
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
