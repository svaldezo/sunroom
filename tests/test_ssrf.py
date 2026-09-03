"""
The URL fetcher, from the attacker's side.

"Paste a link" hands an untrusted person the server's network position. These
tests are the list of things that position is worth to them.
"""
from __future__ import annotations

import pytest

import prism.net.outbound as outbound
from prism.net.outbound import UnsafeURL, resolve_public, validate_url


@pytest.mark.parametrize("url", [
    # loopback, in every spelling that usually gets missed
    "http://127.0.0.1/", "http://127.1/", "http://localhost/",
    "http://[::1]/", "http://0.0.0.0/",
    "http://[::ffff:127.0.0.1]/",
    # private ranges
    "http://10.0.0.1/", "http://192.168.1.1/", "http://172.16.0.1/",
    "http://[fd00::1]/",
    # cloud metadata -- the actual prize
    "http://169.254.169.254/latest/meta-data/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://[fe80::1]/",
    # internal-looking names
    "http://db.internal/", "http://thing.local/", "http://api.localhost/",
])
def test_rejects_private_and_internal(url):
    with pytest.raises(UnsafeURL):
        validate_url(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://x/", "ftp://x/", "data:text/html,x",
    "jar:http://x!/", "dict://127.0.0.1:11211/", "", "not a url",
])
def test_rejects_non_http_schemes(url):
    with pytest.raises(UnsafeURL):
        validate_url(url)


def test_rejects_embedded_credentials():
    """
    `https://trusted.example@169.254.169.254/` reads as trusted.example to a
    person skimming it and resolves to the metadata service.
    """
    with pytest.raises(UnsafeURL):
        validate_url("http://example.com@127.0.0.1/")


def test_rejects_an_overlong_url():
    with pytest.raises(UnsafeURL):
        validate_url("https://example.com/" + "a" * 5000)


def test_ipv4_mapped_ipv6_is_unwrapped():
    """::ffff:10.0.0.1 is 10.0.0.1 wearing a hat; is_global says otherwise."""
    with pytest.raises(UnsafeURL):
        resolve_public("::ffff:10.0.0.1")


def test_unresolvable_host_is_refused():
    with pytest.raises(UnsafeURL):
        resolve_public("no-such-host.invalid")


def test_public_literal_is_allowed():
    assert resolve_public("93.184.215.14") == ["93.184.215.14"]


def test_a_name_resolving_to_a_private_address_is_refused(monkeypatch):
    """
    The DNS half of SSRF: attacker.example resolving to 10.0.0.5. Nothing about
    the URL looks wrong, so only checking the resolved address catches it.
    """
    import socket as s

    def fake(host, port, **kw):
        return [(s.AF_INET, s.SOCK_STREAM, s.IPPROTO_TCP, "", ("10.0.0.5", port))]

    monkeypatch.setattr(outbound.socket, "getaddrinfo", fake)
    with pytest.raises(UnsafeURL, match="private"):
        validate_url("https://attacker.example/")


def test_one_private_answer_poisons_the_whole_name(monkeypatch):
    """
    Round-robin DNS returning one public and one private address must be
    refused: taking the public one just means the attack succeeds on a retry.
    """
    import socket as s

    def fake(host, port, **kw):
        return [
            (s.AF_INET, s.SOCK_STREAM, s.IPPROTO_TCP, "", ("93.184.215.14", port)),
            (s.AF_INET, s.SOCK_STREAM, s.IPPROTO_TCP, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(outbound.socket, "getaddrinfo", fake)
    with pytest.raises(UnsafeURL):
        validate_url("https://roundrobin.example/")
