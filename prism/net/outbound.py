"""
Fetching a URL that a user supplied.

"Paste a link" is a feature that asks the server to make an HTTP request to an
address an untrusted person chose. On a platform like Vercel or Supabase, the
server sits inside a network that contains cloud metadata endpoints, internal
control planes and the database itself, none of which are reachable from the
public internet -- which is precisely why they are worth reaching from here.
That is SSRF, and it is the most likely way this application gets used against
its own infrastructure.

The defence has four parts, and the fourth is the one usually missed:

  1. **Scheme allowlist.** http and https only, so `file://`, `gopher://` and
     friends never get a chance.
  2. **Address validation.** Every IP a hostname resolves to is checked against
     private, loopback, link-local, multicast and reserved ranges. Link-local
     covers 169.254.169.254, the cloud metadata address.
  3. **Manual redirects.** Each hop is validated the same way. A public URL that
     302s to `http://169.254.169.254/` is the classic bypass, and it works
     against anything that lets its HTTP client follow redirects for it.
  4. **Connection pinning.** We connect to the *IP we validated*, sending the
     hostname for SNI and certificate verification. Without this, an attacker
     controlling DNS answers with a one-second TTL can pass validation with a
     public address and have the actual connection go to a private one --
     DNS rebinding. Validating and then handing the hostname back to the HTTP
     client leaves that window wide open.

Plus a size cap enforced while streaming, so "download this 8GB file" is not a
way to exhaust a function's memory.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlparse, urlunparse

ALLOWED_SCHEMES = ("http", "https")
MAX_REDIRECTS = 4
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_BYTES = 25 * 1024 * 1024

# Hostnames that are never worth resolving, whatever DNS says today.
BLOCKED_HOSTS = {
    "localhost", "metadata", "metadata.google.internal",
    "instance-data", "instance-data.ec2.internal",
}


class UnsafeURL(ValueError):
    """The URL points somewhere the server must not go on a user's behalf."""


class FetchTooLarge(ValueError):
    """The response exceeded the byte cap."""


@dataclass
class Fetched:
    url: str
    final_url: str
    status: int
    content_type: str
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    """
    Reject anything that is not a normal, routable, public address.

    `is_global` alone is not enough: it is False for some ranges we want to
    reject and the mapped-IPv4 case slips past it entirely, which is why
    ::ffff:127.0.0.1 is unwrapped first.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif getattr(ip, "sixtofour", None):
            ip = ip.sixtofour
        elif getattr(ip, "teredo", None):
            ip = ip.teredo[1]
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def resolve_public(host: str, port: int = 443) -> list[str]:
    """Every address a host resolves to, provided they are all public."""
    name = (host or "").strip().strip(".").lower()
    if not name:
        raise UnsafeURL("no host in that URL")
    if name in BLOCKED_HOSTS or name.endswith(".localhost") \
            or name.endswith(".internal") or name.endswith(".local"):
        raise UnsafeURL(f"{host} is not a public address")

    # A bare IP literal never goes through DNS, so check it directly.
    try:
        literal = ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public(literal):
            raise UnsafeURL(f"{host} is not a public address")
        return [str(literal)]

    try:
        infos = socket.getaddrinfo(name, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL(f"could not resolve {host}") from exc

    addrs: list[str] = []
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:                                  # pragma: no cover
            raise UnsafeURL(f"could not resolve {host}") from None
        # Every answer must be public. One private record among several is
        # enough to make the whole name untrustworthy.
        if not _is_public(ip):
            raise UnsafeURL(f"{host} resolves to a private address")
        addrs.append(str(ip))
    if not addrs:
        raise UnsafeURL(f"could not resolve {host}")
    return addrs


def validate_url(raw: str) -> tuple[str, str, int, list[str]]:
    """Parse, check, and resolve. Returns (url, host, port, addresses)."""
    if not raw or len(raw) > 4096:
        raise UnsafeURL("that does not look like a link")
    parts = urlparse(raw.strip())
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURL("only http and https links can be read")
    if not parts.hostname:
        raise UnsafeURL("that link has no host")
    if parts.username or parts.password:
        # user:pass@host is a classic way to make a URL's real host hard to see.
        raise UnsafeURL("links with embedded credentials are not accepted")
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    return raw.strip(), parts.hostname, port, resolve_public(parts.hostname, port)


def fetch(url: str, *, max_bytes: int = DEFAULT_MAX_BYTES,
          timeout: float = DEFAULT_TIMEOUT,
          accept: str = "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
          allowed_types: Optional[Iterable[str]] = None) -> Fetched:
    """
    Fetch a user-supplied URL as safely as this can be done.

    Redirects are followed by hand so every hop is validated, and each hop
    connects to an address we resolved and checked ourselves.
    """
    import httpx

    current = url
    seen: list[str] = []
    with httpx.Client(follow_redirects=False, timeout=timeout,
                      trust_env=False,
                      headers={"User-Agent": "Sunroom/1.0 (+link reader)",
                               "Accept": accept}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            safe_url, host, port, addrs = validate_url(current)
            seen.append(safe_url)

            parts = urlparse(safe_url)
            # Connect to the validated address; present the hostname for SNI and
            # certificate verification. This is the step that closes rebinding.
            netloc = (f"[{addrs[0]}]:{port}" if ":" in addrs[0]
                      else f"{addrs[0]}:{port}")
            pinned = urlunparse((parts.scheme, netloc, parts.path or "/",
                                 parts.params, parts.query, ""))
            try:
                with client.stream(
                        "GET", pinned,
                        headers={"Host": host if port in (80, 443)
                                 else f"{host}:{port}"},
                        extensions={"sni_hostname": host}) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        nxt = resp.headers.get("location")
                        if not nxt:
                            raise UnsafeURL("redirect without a destination")
                        current = str(httpx.URL(safe_url).join(nxt))
                        continue

                    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
                    if allowed_types and ctype and ctype not in allowed_types:
                        raise UnsafeURL(f"that link returned {ctype}, which "
                                        f"Sunroom cannot read")

                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise FetchTooLarge(
                            f"that page is {int(declared) // 1_000_000} MB; the "
                            f"limit is {max_bytes // 1_000_000} MB")

                    body = bytearray()
                    for piece in resp.iter_bytes():
                        body += piece
                        # Enforced while streaming: a server that lies about
                        # content-length, or omits it, must not be able to fill
                        # the function's memory.
                        if len(body) > max_bytes:
                            raise FetchTooLarge(
                                f"that page is over the "
                                f"{max_bytes // 1_000_000} MB limit")
                    resp.raise_for_status()
                    return Fetched(url=url, final_url=safe_url,
                                   status=resp.status_code, content_type=ctype,
                                   body=bytes(body))
            except httpx.HTTPStatusError as exc:
                raise UnsafeURL(
                    f"that link returned {exc.response.status_code}") from None
            except httpx.HTTPError as exc:
                raise UnsafeURL(f"could not read that link: "
                                f"{type(exc).__name__}") from None

    raise UnsafeURL(f"too many redirects ({' -> '.join(seen[:3])}…)")
