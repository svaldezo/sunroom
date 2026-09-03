"""
The adversarial pass.

Everything here is written from the position of someone who has a valid account
and wants what is not theirs, or who has no account and wants in. The other test
files check that the product works; this one checks that it does not work in the
ways it must not.

Grouped by what is being attacked, because that is how you notice a gap.
"""
from __future__ import annotations

import os
import time
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "test-jwt-secret-at-least-32-characters-long"
ISS = "https://proj.supabase.co/auth/v1"
ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"

DOC = "# Private\n\nThis paragraph belongs to exactly one person.\n"


def tok(sub=ALICE, email="alice@test", **over) -> str:
    claims = {"sub": sub, "aud": "authenticated", "iss": ISS,
              "role": "authenticated", "email": email,
              "exp": int(time.time()) + 3600}
    claims.update(over)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def auth(sub=ALICE, email="alice@test") -> dict:
    return {"Authorization": f"Bearer {tok(sub, email)}"}


@pytest.fixture()
def client(isolated_home, monkeypatch):
    from prism.auth import reset_verifier
    from prism.config import SETTINGS
    from prism.web.api import app

    monkeypatch.setattr(SETTINGS, "supabase_url", "https://proj.supabase.co")
    monkeypatch.setattr(SETTINGS, "supabase_jwt_secret", SECRET)
    monkeypatch.setattr(SETTINGS, "supabase_anon_key", "anon-key")
    monkeypatch.setattr(SETTINGS, "worker_secret", "worker-secret")
    monkeypatch.setattr(SETTINGS, "rate_limit_per_min", 0)
    reset_verifier()
    with TestClient(app) as c:
        yield c
    reset_verifier()


def owned_doc(client, headers) -> str:
    from prism.jobs.runner import drain
    r = client.post("/api/documents",
                    json={"source": DOC, "kind": "text", "title": "Private"},
                    headers=headers)
    drain()
    return client.get(f"/api/jobs/{r.json()['job']['id']}",
                      headers=headers).json()["understanding"]


# ══ getting in without an account ═══════════════════════════════════════════

@pytest.mark.parametrize("header", [
    None,
    "",
    "Bearer",
    "Bearer ",
    "Basic YWRtaW46YWRtaW4=",
    "Bearer null",
    "Bearer undefined",
    "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.",
])
def test_no_credential_gets_in(client, header):
    headers = {} if header is None else {"Authorization": header}
    assert client.get("/api/documents", headers=headers).status_code == 401


def test_the_public_anon_key_is_not_a_session(client):
    """It is shipped to every browser by design. It must open nothing."""
    anon = jwt.encode({"role": "anon", "iss": "supabase", "aud": "authenticated",
                       "exp": int(time.time()) + 99999}, SECRET, algorithm="HS256")
    assert client.get("/api/documents",
                      headers={"Authorization": f"Bearer {anon}"}).status_code == 401


def test_a_token_from_another_project_is_refused(client):
    other = jwt.encode({"sub": ALICE, "aud": "authenticated",
                        "iss": "https://someone-else.supabase.co/auth/v1",
                        "role": "authenticated", "exp": int(time.time()) + 600},
                       SECRET, algorithm="HS256")
    assert client.get("/api/me",
                      headers={"Authorization": f"Bearer {other}"}).status_code == 401


def test_an_expired_session_stops_working(client):
    # Well past the verifier's clock-skew leeway, which is deliberately a few
    # seconds -- a token five seconds stale is a clock difference, not an attack.
    assert client.get("/api/me", headers={
        "Authorization": "Bearer " + tok(exp=int(time.time()) - 600)}).status_code == 401


def test_signature_stripping_does_not_work(client):
    """Take a valid token, drop the signature, present the rest."""
    good = tok()
    header, payload, _sig = good.split(".")
    for forged in (f"{header}.{payload}.", f"{header}.{payload}.x",
                   f"{header}.{payload}"):
        assert client.get("/api/me", headers={
            "Authorization": f"Bearer {forged}"}).status_code == 401


def test_claims_cannot_be_edited(client):
    """Re-encode a real token's payload with a different subject."""
    import base64
    import json as _json
    good = tok()
    header, payload, sig = good.split(".")
    body = _json.loads(base64.urlsafe_b64decode(payload + "=="))
    body["sub"] = BOB
    tampered = base64.urlsafe_b64encode(
        _json.dumps(body).encode()).decode().rstrip("=")
    assert client.get("/api/me", headers={
        "Authorization": f"Bearer {header}.{tampered}.{sig}"}).status_code == 401


# ══ reaching another account's work ═════════════════════════════════════════

def test_every_document_route_is_scoped(client):
    doc = owned_doc(client, auth(ALICE))
    bob = auth(BOB, "bob@test")
    for method, path in [
        ("GET", f"/api/documents/{doc}"),
        ("GET", f"/api/documents/{doc}/spans"),
        ("GET", f"/api/documents/{doc}/citations"),
        ("GET", f"/api/documents/{doc}/trace?offset=0"),
        ("GET", f"/api/documents/{doc}/export/markdown"),
        ("GET", f"/api/documents/{doc}/export/bibtex"),
        ("GET", f"/api/documents/{doc}/export/anki"),
        ("POST", f"/api/documents/{doc}/format/brief"),
        ("POST", f"/api/documents/{doc}/render/summary"),
    ]:
        r = client.request(method, path, headers=bob, json={})
        assert r.status_code == 404, f"{method} {path} -> {r.status_code}"


def test_an_id_prefix_is_not_a_back_door(client):
    """
    `get` accepts an id prefix as a convenience. That convenience must not
    become a way to enumerate documents by guessing short strings.
    """
    doc = owned_doc(client, auth(ALICE))
    bob = auth(BOB, "bob@test")
    for n in (4, 8, 12):
        assert client.get(f"/api/documents/{doc[:n]}",
                          headers=bob).status_code == 404


def test_the_audit_and_search_never_cross(client):
    owned_doc(client, auth(ALICE))
    bob = auth(BOB, "bob@test")
    assert client.get("/api/search?q=paragraph", headers=bob).json() == []
    assert client.get("/api/audit", headers=bob).json()["total"] == 0
    assert client.get("/api/collections", headers=bob).json() == []
    assert client.get("/api/stale", headers=bob).json() == []
    assert client.get("/api/review", headers=bob).json() == []


def test_review_cannot_reference_a_foreign_passage(client):
    doc = owned_doc(client, auth(ALICE))
    node = client.get(f"/api/documents/{doc}",
                      headers=auth(ALICE)).json()["nodes"][0]["id"]
    r = client.post("/api/review", json={"node_id": node, "correct": True},
                    headers=auth(BOB, "bob@test"))
    assert r.status_code == 404


# ══ the filesystem ══════════════════════════════════════════════════════════

@pytest.mark.parametrize("path", [
    "/etc/passwd", "/proc/self/environ", "../../etc/passwd",
    "~/.ssh/id_rsa", "file:///etc/passwd", "/etc/passwd\x00.md",
    "....//....//etc/passwd",
])
def test_no_path_reaches_the_filesystem(client, path):
    r = client.post("/api/documents", json={"source": path, "kind": "path"},
                    headers=auth())
    assert r.status_code == 400


def test_a_path_disguised_as_text_stays_text(client):
    """
    Sending kind=text with a path must ingest the *string*, not the file. The
    classifier is a convenience; it does not grant access.
    """
    from prism.jobs.runner import drain
    r = client.post("/api/documents",
                    json={"source": "/etc/passwd", "kind": "text",
                          "title": "sneaky"}, headers=auth())
    assert r.status_code == 202
    drain()
    job = client.get(f"/api/jobs/{r.json()['job']['id']}",
                     headers=auth()).json()
    if job["status"] == "done":
        text = client.get(f"/api/documents/{job['understanding']}",
                          headers=auth()).json()["text"]
        assert "root:" not in text
        assert text.strip() == "/etc/passwd"


@pytest.mark.parametrize("key", [
    "../../../etc/passwd",
    "..%2f..%2fetc%2fpasswd",
    "/absolute/path",
    "....//....//etc/shadow",
])
def test_upload_keys_cannot_escape_their_folder(client, key):
    r = client.put(f"/api/uploads/{key}", content=b"x", headers=auth())
    assert r.status_code in (403, 404, 400), f"{key} -> {r.status_code}"


def test_storage_keys_are_checked_against_their_owner(client):
    """
    Even a correctly-shaped key belonging to someone else must be refused --
    the shape check alone would let a guessed uuid through.
    """
    from prism.ingest.fetch import RefusedInput, materialize
    with pytest.raises(RefusedInput):
        materialize({"kind": "storage", "value": f"{BOB}/theirs.pdf",
                     "user_id": ALICE})


def test_a_malicious_filename_cannot_place_a_file(tmp_path):
    from prism.ingest.fetch import _safe_name
    for name in ("../../evil.md", "..\\..\\evil.md", "/etc/passwd",
                 ".bashrc", "a" * 400 + ".md", "x\x00.md"):
        safe = _safe_name(name)
        assert "/" not in safe and "\\" not in safe
        assert not safe.startswith(".")
        assert len(safe) <= 120
        assert "\x00" not in safe


# ══ the network ═════════════════════════════════════════════════════════════

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://127.0.0.1:5432/", "http://localhost:6379/",
    "http://10.0.0.1/", "http://[::1]/",
    "file:///etc/passwd", "gopher://127.0.0.1:11211/",
])
def test_no_internal_address_is_ever_fetched(client, url):
    from prism.jobs.runner import drain
    r = client.post("/api/documents", json={"source": url, "kind": "url"},
                    headers=auth())
    assert r.status_code in (202, 400)
    if r.status_code == 202:
        drain()
        job = client.get(f"/api/jobs/{r.json()['job']['id']}",
                         headers=auth()).json()
        assert job["status"] == "failed", f"{url} was actually fetched"


# ══ spending someone else's money ═══════════════════════════════════════════

def test_the_quota_cannot_be_raised_from_the_api(client):
    """There is no route for it. This asserts one has not appeared."""
    from prism.web.api import app
    paths = [r.path for r in app.routes if hasattr(r, "methods")]
    assert not [p for p in paths if "budget" in p or "quota" in p]

    client.get("/api/me", headers=auth())
    for body in ({"token_budget": 10**9}, {"budget": 10**9},
                 {"is_admin": True}, {"usage": {"used": 0}}):
        r = client.put("/api/settings/api-key", json=body, headers=auth())
        assert r.status_code == 400          # only api_key is accepted


def test_extra_fields_in_a_request_are_ignored(client):
    """A body that carries `user_id` must not be able to redirect the write."""
    from prism.jobs.runner import drain
    r = client.post("/api/documents",
                    json={"source": DOC, "kind": "text", "title": "mine",
                          "user_id": BOB, "id": "und_planted"},
                    headers=auth(ALICE))
    assert r.status_code == 202
    drain()
    assert client.get("/api/documents", headers=auth(BOB, "bob@test")).json() == []
    assert client.get("/api/documents", headers=auth(ALICE)).json()


def test_the_worker_secret_is_compared_in_constant_time():
    """
    A plain `==` returns early on the first wrong byte, which leaks the prefix
    to anyone willing to time a few thousand requests.
    """
    import inspect

    from prism.auth import deps
    src = inspect.getsource(deps.require_worker)
    assert "compare_digest" in src
    assert "==" not in src.split("compare_digest")[0].split("supplied")[-1]


def test_a_quota_refusal_does_not_queue_work(client):
    from prism.accounts import accounts
    client.get("/api/me", headers=auth())
    accounts().set_budget(ALICE, 1)
    r = client.post("/api/documents", json={"source": DOC * 50, "kind": "text"},
                    headers=auth())
    assert r.status_code == 402
    assert client.get("/api/jobs", headers=auth()).json() == []


# ══ injection ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("q", [
    "'; DROP TABLE understandings; --",
    "' OR 1=1 --",
    "%' UNION SELECT payload FROM understandings --",
    "\x00",
    "a" * 5000,
    "MATCH", "NEAR(", '"unbalanced',
])
def test_search_input_cannot_break_the_query(client, q):
    """
    Both stores are parameterised, so this is really testing that odd input is
    *handled* rather than 500ing -- an error page is a fingerprint too.
    """
    doc = owned_doc(client, auth(ALICE))
    r = client.get("/api/search", params={"q": q}, headers=auth(ALICE))
    assert r.status_code == 200, r.text
    # And the corpus is intact afterwards.
    assert client.get(f"/api/documents/{doc}", headers=auth(ALICE)).status_code == 200


@pytest.mark.parametrize("name", [
    "'; DROP TABLE collections; --", "../../x", "<script>alert(1)</script>",
])
def test_collection_names_are_data_not_code(client, name):
    from prism.jobs.runner import drain
    r = client.post("/api/documents",
                    json={"source": DOC, "kind": "text", "collection": name},
                    headers=auth())
    assert r.status_code == 202
    drain()
    assert client.get("/api/collections", headers=auth()).status_code == 200


# ══ what an error says ══════════════════════════════════════════════════════

def test_errors_do_not_leak_internals(client):
    """
    A stack trace, a file path or a connection string in an error body is a map
    of the system drawn by the system.
    """
    bodies = []
    for method, path, payload in [
        ("GET", "/api/documents/und_nope", None),
        ("POST", "/api/documents/und_nope/format/brief", {}),
        ("POST", "/api/documents", {"source": "x", "kind": "nonsense"}),
        ("GET", "/api/documents/und_nope/export/unknown", None),
        ("GET", "/api/jobs/" + str(uuid.uuid4()), None),
    ]:
        r = client.request(method, path, headers=auth(), json=payload)
        bodies.append(r.text)

    joined = " ".join(bodies)
    for leak in ("Traceback", "/root/", "/home/", "site-packages",
                 "postgresql://", "sqlite3.", "psycopg", SECRET,
                 os.environ.get("SUNROOM_SECRET_KEY", "unset-secret")):
        assert leak not in joined, f"error bodies contain {leak!r}"


def test_a_document_that_exists_and_one_that_does_not_look_the_same(client):
    doc = owned_doc(client, auth(ALICE))
    bob = auth(BOB, "bob@test")
    a = client.get(f"/api/documents/{doc}", headers=bob)
    b = client.get("/api/documents/und_definitely_not_real", headers=bob)
    assert a.status_code == b.status_code == 404
    assert a.json() == b.json()


# ══ the stored key ══════════════════════════════════════════════════════════

def test_the_api_key_is_encrypted_at_rest(client):
    """Read the database directly and confirm the plaintext is not in it."""
    import sqlite3

    from prism.config import SETTINGS

    key = "sk-ant-api03-" + "q" * 60
    client.put("/api/settings/api-key", json={"api_key": key}, headers=auth())

    conn = sqlite3.connect(SETTINGS.db_path)
    blob = conn.execute("SELECT byo_key_ct FROM accounts WHERE id = ?",
                        (ALICE,)).fetchone()[0]
    conn.close()
    assert blob, "no ciphertext was stored"
    assert key.encode() not in bytes(blob)
    assert b"sk-ant" not in bytes(blob)


def test_a_rotated_secret_does_not_silently_use_a_wrong_key(client, monkeypatch):
    """
    If SUNROOM_SECRET_KEY changes, the stored key cannot be read. The failure
    has to say so, rather than presenting the user's own good key as invalid.
    """
    from prism.accounts import accounts
    from prism.accounts.keys import KeyError_
    from prism.config import SETTINGS

    client.put("/api/settings/api-key",
               json={"api_key": "sk-ant-api03-" + "r" * 60}, headers=auth())
    monkeypatch.setattr(SETTINGS, "secret_key", "a-completely-different-secret-key-value")
    with pytest.raises(KeyError_, match="SUNROOM_SECRET_KEY"):
        accounts().api_key(ALICE)


# ══ configuration ═══════════════════════════════════════════════════════════

def test_production_refuses_an_unsafe_configuration(monkeypatch):
    """
    The last line of defence: a misconfigured production deploy must fail to
    start rather than come up open.
    """
    from prism.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "env", "production")
    monkeypatch.setattr(SETTINGS, "store", "sqlite")
    monkeypatch.setattr(SETTINGS, "supabase_url", "")
    monkeypatch.setattr(SETTINGS, "secret_key", "")
    monkeypatch.setattr(SETTINGS, "worker_secret", "")
    monkeypatch.setattr(SETTINGS, "provider", "mock")

    problems = SETTINGS.preflight()
    assert len(problems) >= 5

    from fastapi.testclient import TestClient

    from prism.web.api import app
    with pytest.raises(RuntimeError, match="Refusing to start"):
        with TestClient(app):
            pass


def test_single_user_mode_is_refused_in_production(monkeypatch):
    """
    No Supabase in production would mean every visitor shares one library. The
    dependency refuses rather than serving that.
    """
    from fastapi import HTTPException

    from prism.auth.deps import current_principal
    from prism.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "env", "production")
    monkeypatch.setattr(SETTINGS, "supabase_url", "")
    monkeypatch.setattr(SETTINGS, "supabase_jwt_secret", "")
    monkeypatch.setattr(SETTINGS, "supabase_anon_key", "")

    class FakeRequest:
        state = type("S", (), {})()

    with pytest.raises(HTTPException) as e:
        current_principal(FakeRequest(), None)
    assert e.value.status_code == 503
