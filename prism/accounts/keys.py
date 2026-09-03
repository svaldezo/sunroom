"""
Encrypting a user's own API key at rest.

A key pasted into the settings sheet is a live credential that spends the
person's money. Storing it in a column as plaintext means every future
backup, log dump, read replica and support query is a credential leak.

Fernet (AES-128-CBC + HMAC-SHA256, timestamped) from `cryptography`, with the
key derived from SUNROOM_SECRET_KEY so there is exactly one secret to manage
and rotate. The derivation is HKDF with a fixed info string: the same master
secret can therefore also key other things later without one purpose's
ciphertext being decryptable by another's key.
"""
from __future__ import annotations

import base64
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..config import SETTINGS

INFO = b"sunroom/byo-api-key/v1"


class KeyError_(RuntimeError):
    """Encryption is unavailable or the ciphertext will not open."""


def _fernet(secret: Optional[str] = None) -> Fernet:
    raw = (secret if secret is not None else SETTINGS.secret_key) or ""
    if len(raw) < 32:
        raise KeyError_(
            "SUNROOM_SECRET_KEY must be at least 32 characters to store a "
            "user's API key. Generate one with: openssl rand -base64 48")
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                   info=INFO).derive(raw.encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def seal(api_key: str, *, secret: Optional[str] = None) -> bytes:
    if not api_key or not api_key.strip():
        raise KeyError_("empty key")
    return _fernet(secret).encrypt(api_key.strip().encode())


def unseal(ciphertext: bytes, *, secret: Optional[str] = None) -> str:
    if not ciphertext:
        raise KeyError_("no stored key")
    try:
        return _fernet(secret).decrypt(bytes(ciphertext)).decode()
    except InvalidToken:
        # Almost always a rotated SUNROOM_SECRET_KEY. Say so, because the
        # alternative is a user staring at "invalid API key" for their
        # perfectly good key.
        raise KeyError_(
            "stored key could not be decrypted -- SUNROOM_SECRET_KEY has "
            "probably changed since it was saved. Ask the user to re-enter it."
        ) from None


def hint(api_key: str) -> str:
    """The tail of a key, for 'sk-ant-...QK9f' in the settings sheet."""
    k = (api_key or "").strip()
    return k[-4:] if len(k) >= 8 else ""


def looks_like_anthropic_key(api_key: str) -> bool:
    """
    A cheap shape check so an obvious paste error fails at the form rather than
    on the first ingest, twenty seconds later, as an opaque upstream 401.
    """
    k = (api_key or "").strip()
    return k.startswith("sk-ant-") and len(k) >= 40 and k.isascii() and " " not in k
