"""
The HTTP surface.

The first test in this file is the most important one in the suite: it walks
every route the application declares and asserts that it refuses an
unauthenticated caller. It is written as an enumeration rather than a list of
hand-written cases because the risk is not the route somebody remembered to
test -- it is the route somebody adds next month.
"""
from __future__ import annotations

import time
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "test-jwt-secret-at-least-32-characters-long"
ISS = "https://proj.supabase.co/auth/v1"
ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"

DOC = """# Exchange and Obligation

Reciprocity is the mutual give and take between parties of roughly equal standing.
Redistribution requires a center that collects and then disburses.
Market exchange sets prices through the interaction of supply and demand.
"""

# Routes that are public by design, and why.
PUBLIC = {
    ("GET", "/api/health"),      # load balancers cannot sign in
    ("GET", "/api/config"),      # the browser needs it *to* sign in
    ("GET", "/"),                # the app shell
    ("GET", "/favicon.ico"),
    ("GET", "/openapi.json"),
}
# The worker is authenticated, but with a shared secret rather than a session.
WORKER = {("POST", "/api/worker/run")}


def token(sub: str = ALICE, email: str = "alice@test", **over) -> str:
    claims = {"sub": sub, "aud": "authenticated", "iss": ISS,
              "role": "authenticated", "email": email,
              "exp": int(time.time()) + 3600}
    claims.update(over)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def auth(sub: str = ALICE, email: str = "alice@test") -> dict:
    return {"Authorization": f"Bearer {token(sub, email)}"}


@pytest.fixture()
def client(isolated_home, monkeypatch):
    from prism.auth import reset_verifier
    from prism.config import SETTINGS
    from prism.web.api import app

    monkeypatch.setattr(SETTINGS, "supabase_url", "https://proj.supabase.co")
    monkeypatch.setattr(SETTINGS, "supabase_jwt_secret", SECRET)
    monkeypatch.setattr(SETTINGS, "supabase_anon_key", "anon-key")
    monkeypatch.setattr(SETTINGS, "worker_secret", "worker-secret")
    monkeypatch.setattr(SETTINGS, "rate_limit_per_min", 0)   # off unless tested
    reset_verifier()
    with TestClient(app) as c:
        yield c
    reset_verifier()


def sample_paths(route) -> str:
    """Fill in path parameters with values that are syntactically plausible."""
    return (route.path
            .replace("{doc_id}", "und_deadbeef")
            .replace("{job_id}", str(uuid.uuid4()))
            .replace("{renderer}", "summary")
            .replace("{fmt}", "brief")
            .replace("{key:path}", f"{ALICE}/x.md"))


# -- the audit -------------------------------------------------------------

def test_every_route_requires_authentication(client):
    """
    No route reaches a corpus without an identity.

    A new endpoint that forgets its `Depends(current_store)` shows up here as a
    200 where a 401 belongs. Add a genuinely public route and you must add it to
    PUBLIC above, which is a deliberate act rather than an oversight.
    """
    from prism.web.api import app

    leaks = []
    for route in app.routes:
        methods = getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
        for method in methods:
            key = (method, route.path)
            if key in PUBLIC:
                continue
            path = sample_paths(route)
            resp = client.request(method, path, json={})
            if key in WORKER:
                if resp.status_code not in (401, 403):
                    leaks.append(f"{method} {route.path} -> {resp.status_code} "
                                 f"(worker route should demand its secret)")
                continue
            if resp.status_code not in (401, 403):
                leaks.append(f"{method} {route.path} -> {resp.status_code}")
    assert not leaks, "these routes answered without a session:\n  " + \
        "\n  ".join(leaks)


def test_a_forged_token_is_refused_everywhere(client):
    bad = {"Authorization": "Bearer " + jwt.encode(
        {"sub": ALICE, "aud": "authenticated", "iss": ISS,
         "role": "authenticated", "exp": int(time.time()) + 60},
        "wrong-secret", algorithm="HS256")}
    for path in ("/api/documents", "/api/me", "/api/collections", "/api/usage"):
        assert client.get(path, headers=bad).status_code == 401


# -- signed-in basics ------------------------------------------------------

def test_me_creates_the_account_on_first_request(client):
    r = client.get("/api/me", headers=auth())
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == ALICE
    assert body["email"] == "alice@test"
    assert body["byo_key"] is False
    assert body["usage"]["budget"] > 0


def test_library_starts_empty(client):
    assert client.get("/api/documents", headers=auth()).json() == []


def test_health_and_config_do_not_leak_secrets(client):
    cfg = client.get("/api/config").json()
    assert cfg["supabase_anon_key"] == "anon-key"     # public by design
    body = str(client.get("/api/health").json()) + str(cfg)
    assert SECRET not in body
    assert "worker-secret" not in body


# -- the ingest round trip -------------------------------------------------

def ingest(client, headers, text=DOC, **over):
    payload = {"source": text, "kind": "text", "title": "Exchange",
               "collection": "ANTH266"}
    payload.update(over)
    r = client.post("/api/documents", json=payload, headers=headers)
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    from prism.jobs.runner import drain
    drain()
    final = client.get(f"/api/jobs/{job['id']}", headers=headers).json()
    return final


def test_adding_a_source_queues_a_job_and_finishes(client):
    job = ingest(client, auth())
    assert job["status"] == "done"
    assert job["understanding"]
    docs = client.get("/api/documents", headers=auth()).json()
    assert [d["id"] for d in docs] == [job["understanding"]]


def test_a_document_carries_its_own_provenance(client):
    job = ingest(client, auth())
    doc = client.get(f"/api/documents/{job['understanding']}",
                     headers=auth()).json()
    assert doc["nodes"] and doc["sections"]
    spans = client.get(f"/api/documents/{job['understanding']}/spans",
                       headers=auth()).json()
    assert spans and all("anchor" in s for s in spans)


def test_making_a_format_returns_parts_with_citations(client):
    job = ingest(client, auth())
    r = client.post(f"/api/documents/{job['understanding']}/format/brief",
                    headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parts"]
    assert body["fidelity"]["grounding"] == 1.0
    assert any(p["spans"] for p in body["parts"])


def test_asking_a_question_cites_or_declines(client):
    job = ingest(client, auth())
    r = client.post(f"/api/documents/{job['understanding']}/ask",
                    json={"question": "What is reciprocity?"}, headers=auth())
    assert r.status_code == 200
    r2 = client.post(f"/api/documents/{job['understanding']}/ask",
                     json={"question": "What is the capital of France?"},
                     headers=auth())
    assert r2.status_code == 200
    assert r2.json().get("declined") or "does not cover" in str(r2.json()).lower()


def test_deleting_a_document(client):
    job = ingest(client, auth())
    assert client.delete(f"/api/documents/{job['understanding']}",
                         headers=auth()).json() == {"deleted": True}
    assert client.get("/api/documents", headers=auth()).json() == []


# -- tenancy, over HTTP ----------------------------------------------------

def test_one_account_cannot_read_anothers_document(client):
    job = ingest(client, auth(ALICE))
    doc_id = job["understanding"]
    bob = auth(BOB, "bob@test")

    assert client.get(f"/api/documents/{doc_id}", headers=bob).status_code == 404
    assert client.get(f"/api/documents/{doc_id}/spans",
                      headers=bob).status_code == 404
    assert client.get(f"/api/documents/{doc_id}/citations",
                      headers=bob).status_code == 404
    assert client.post(f"/api/documents/{doc_id}/format/brief",
                       headers=bob).status_code == 404
    assert client.post(f"/api/documents/{doc_id}/ask",
                       json={"question": "hi"}, headers=bob).status_code == 404
    assert client.get(f"/api/documents/{doc_id}/export/markdown",
                      headers=bob).status_code == 404


def test_one_account_cannot_delete_anothers_document(client):
    job = ingest(client, auth(ALICE))
    bob = auth(BOB, "bob@test")
    assert client.delete(f"/api/documents/{job['understanding']}",
                         headers=bob).json() == {"deleted": False}
    assert client.get("/api/documents", headers=auth(ALICE)).json()


def test_a_missing_document_and_someone_elses_are_indistinguishable(client):
    """
    Both 404. If "belongs to someone else" answered 403, an attacker could walk
    a list of ids and learn exactly which ones exist.
    """
    job = ingest(client, auth(ALICE))
    bob = auth(BOB, "bob@test")
    theirs = client.get(f"/api/documents/{job['understanding']}", headers=bob)
    missing = client.get("/api/documents/und_nothing_here", headers=bob)
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json()


def test_jobs_are_private(client):
    r = client.post("/api/documents", json={"source": DOC, "kind": "text"},
                    headers=auth(ALICE))
    job_id = r.json()["job"]["id"]
    bob = auth(BOB, "bob@test")
    assert client.get(f"/api/jobs/{job_id}", headers=bob).status_code == 404
    assert client.delete(f"/api/jobs/{job_id}",
                         headers=bob).json() == {"cancelled": False}
    assert client.get("/api/jobs", headers=bob).json() == []


def test_search_does_not_cross_accounts(client):
    ingest(client, auth(ALICE))
    assert client.get("/api/search?q=reciprocity", headers=auth(ALICE)).json()
    assert client.get("/api/search?q=reciprocity",
                      headers=auth(BOB, "bob@test")).json() == []


def test_audit_only_covers_your_own_work(client):
    job = ingest(client, auth(ALICE))
    client.post(f"/api/documents/{job['understanding']}/format/brief",
                headers=auth(ALICE))
    assert client.get("/api/audit", headers=auth(ALICE)).json()["total"] >= 1
    assert client.get("/api/audit",
                      headers=auth(BOB, "bob@test")).json()["total"] == 0


# -- uploads ---------------------------------------------------------------

def test_upload_paths_are_per_account(client):
    r = client.post("/api/uploads/sign",
                    json={"filename": "notes.pdf", "size": 1000},
                    headers=auth(ALICE))
    assert r.status_code == 200
    assert r.json()["key"].startswith(f"{ALICE}/")


def test_cannot_write_into_another_accounts_upload_folder(client):
    r = client.put(f"/api/uploads/{BOB}/planted.md", content=b"hello",
                   headers=auth(ALICE))
    assert r.status_code == 403


@pytest.mark.parametrize("filename", ["x.exe", "x.sh", "x", "x.zip", "x.py"])
def test_unsupported_upload_types_are_refused(client, filename):
    r = client.post("/api/uploads/sign", json={"filename": filename, "size": 10},
                    headers=auth())
    assert r.status_code == 400
    assert "cannot read" in r.json()["detail"]


def test_oversized_upload_is_refused_before_any_bytes(client, monkeypatch):
    from prism.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "max_upload_bytes", 1000)
    r = client.post("/api/uploads/sign",
                    json={"filename": "big.pdf", "size": 10_000_000},
                    headers=auth())
    assert r.status_code == 400 and "limit" in r.json()["detail"]


# -- quota -----------------------------------------------------------------

def test_estimate_is_returned_before_committing(client):
    r = client.post("/api/estimate", json={"chars": 120_000}, headers=auth())
    body = r.json()
    assert body["estimate"]["total_tokens"] > 0
    assert body["estimate"]["usd"] > 0
    assert body["affordable"] is True


def test_a_source_beyond_the_budget_is_refused_up_front(client):
    from prism.accounts import accounts
    client.get("/api/me", headers=auth())              # ensure the account
    accounts().set_budget(ALICE, 10)
    r = client.post("/api/documents", json={"source": DOC, "kind": "text"},
                    headers=auth())
    assert r.status_code == 402
    assert r.json()["code"] == "quota"
    assert client.get("/api/jobs", headers=auth()).json() == []


def test_a_users_own_key_lifts_the_limit(client):
    from prism.accounts import accounts
    client.get("/api/me", headers=auth())
    accounts().set_budget(ALICE, 10)
    r = client.put("/api/settings/api-key",
                   json={"api_key": "sk-ant-api03-" + "a" * 60}, headers=auth())
    assert r.status_code == 200 and r.json()["hint"] == "aaaa"
    assert client.post("/api/documents", json={"source": DOC, "kind": "text"},
                       headers=auth()).status_code == 202


def test_a_nonsense_api_key_is_rejected_at_the_form(client):
    r = client.put("/api/settings/api-key", json={"api_key": "hunter2"},
                   headers=auth())
    assert r.status_code == 400 and "sk-ant-" in r.json()["detail"]


def test_the_stored_key_is_never_returned(client):
    key = "sk-ant-api03-" + "z" * 60
    client.put("/api/settings/api-key", json={"api_key": key}, headers=auth())
    for path in ("/api/me", "/api/usage"):
        assert key not in client.get(path, headers=auth()).text
    assert client.get("/api/me", headers=auth()).json()["byo_key_hint"] == "zzzz"


# -- input safety ----------------------------------------------------------

def test_a_filesystem_path_is_refused(client, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("# not yours")
    r = client.post("/api/documents",
                    json={"source": str(secret), "kind": "path"}, headers=auth())
    assert r.status_code == 400
    assert "path" in r.json()["detail"].lower()


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:5432/",
    "file:///etc/passwd",
])
def test_a_dangerous_url_never_becomes_a_request(client, url):
    """
    Refused when the job runs, not when it is queued -- so the check has to
    survive the trip through the queue, which is exactly where it would be easy
    to lose.
    """
    from prism.jobs.runner import drain
    r = client.post("/api/documents", json={"source": url, "kind": "url"},
                    headers=auth())
    assert r.status_code == 202
    drain()
    job = client.get(f"/api/jobs/{r.json()['job']['id']}", headers=auth()).json()
    assert job["status"] == "failed"


def test_empty_input_is_refused(client):
    assert client.post("/api/documents", json={"source": "   "},
                       headers=auth()).status_code == 400


# -- the worker ------------------------------------------------------------

def test_worker_requires_its_secret(client):
    assert client.post("/api/worker/run").status_code == 401
    assert client.post("/api/worker/run",
                       headers={"X-Worker-Secret": "wrong"}).status_code == 401
    ok = client.post("/api/worker/run",
                     headers={"X-Worker-Secret": "worker-secret"})
    assert ok.status_code == 200


def test_a_user_token_does_not_open_the_worker(client):
    assert client.post("/api/worker/run", headers=auth()).status_code == 401


def test_worker_runs_a_queued_job(client):
    r = client.post("/api/documents", json={"source": DOC, "kind": "text"},
                    headers=auth())
    job_id = r.json()["job"]["id"]
    for _ in range(10):
        out = client.post("/api/worker/run?max_slices=5",
                          headers={"X-Worker-Secret": "worker-secret"}).json()
        if out["ran"] == 0:
            break
    assert client.get(f"/api/jobs/{job_id}",
                      headers=auth()).json()["status"] == "done"


# -- review ----------------------------------------------------------------

def test_review_round_trip(client):
    job = ingest(client, auth())
    doc = client.get(f"/api/documents/{job['understanding']}",
                     headers=auth()).json()
    # Schedule several, so the assertion is about the cards that come back
    # rather than about one particular node surviving the filter.
    nodes = [n["id"] for n in doc["nodes"][:6]]
    for node in nodes:
        assert client.post("/api/review",
                           json={"node_id": node, "correct": False},
                           headers=auth()).status_code == 200
    due = client.get("/api/review", headers=auth()).json()
    assert due, "nothing came back due"
    assert set(c["node_id"] for c in due) <= set(nodes)
    for card in due:
        assert card["prompt"] and card["answer"]
        assert card["answer"].lower() not in card["prompt"].lower(), card


def test_a_card_that_would_show_its_own_answer_is_withheld(client):
    """
    A prompt containing its answer is not a card, it is a sentence. This has
    regressed twice by different routes -- once through the IR, once through a
    heading whose label and body were the same string -- so the check now lives
    on the finished card rather than on any one path to it.
    """
    from prism.web.api import _gives_itself_away
    assert _gives_itself_away({"prompt": "What is reciprocity? Reciprocity is "
                                         "mutual give and take.",
                               "answer": "Reciprocity is mutual give and take."})
    assert _gives_itself_away({"prompt": "What do you remember about: Kinship?",
                               "answer": "Kinship"})
    assert _gives_itself_away({"prompt": "", "answer": "x"})
    assert not _gives_itself_away({"prompt": "What is reciprocity?",
                                   "answer": "Mutual give and take."})


def test_cannot_schedule_another_accounts_passage(client):
    job = ingest(client, auth(ALICE))
    doc = client.get(f"/api/documents/{job['understanding']}",
                     headers=auth(ALICE)).json()
    r = client.post("/api/review",
                    json={"node_id": doc["nodes"][0]["id"], "correct": True},
                    headers=auth(BOB, "bob@test"))
    assert r.status_code == 404


# -- rate limiting ---------------------------------------------------------

def test_rate_limit_returns_429(client, monkeypatch):
    from prism.config import SETTINGS
    from prism.web import api as api_mod

    monkeypatch.setattr(SETTINGS, "rate_limit_per_min", 5)
    api_mod._hits.clear()
    # One token, reused. Minting a fresh one per request made this test depend
    # on whether the clock ticked over a second mid-loop -- `exp` would change,
    # and with it the token -- which is how the caller-identity bug below hid.
    headers = auth()
    codes = [client.get("/api/documents", headers=headers).status_code
             for _ in range(8)]
    assert 429 in codes
    assert codes.count(200) == 5


def test_a_refreshed_token_does_not_reset_the_limit(client, monkeypatch):
    """Supabase mints a new JWT on every refresh, and did so about hourly.

    Keying the limiter on the token meant each refresh handed the same person a
    fresh allowance, so the cap could be lifted indefinitely just by signing in
    again -- which the browser does on its own.
    """
    from prism.config import SETTINGS
    from prism.web import api as api_mod

    monkeypatch.setattr(SETTINGS, "rate_limit_per_min", 5)
    api_mod._hits.clear()
    for _ in range(5):
        client.get("/api/documents", headers=auth())
    # A different token for the same account: different bytes, same person.
    fresh = {"Authorization": f"Bearer {token(ALICE, 'alice@test', exp=int(time.time()) + 7200)}"}
    assert fresh != auth()
    assert client.get("/api/documents", headers=fresh).status_code == 429


def test_one_account_cannot_spend_anothers_allowance(client, monkeypatch):
    from prism.config import SETTINGS
    from prism.web import api as api_mod

    monkeypatch.setattr(SETTINGS, "rate_limit_per_min", 5)
    api_mod._hits.clear()
    for _ in range(8):
        client.get("/api/documents", headers=auth(ALICE, "alice@test"))
    assert client.get("/api/documents",
                      headers=auth(BOB, "bob@test")).status_code == 200


# ------------------------------------------- the queue, driven by polling ---

def test_polling_a_job_advances_it_when_nothing_else_can(client, monkeypatch):
    """Vercel Hobby allows one cron a day, and refuses the deploy if you ask
    for more. Without this, every job on that plan sits at "Queued" forever
    with nothing on screen to explain it."""
    from prism.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "poll_nudge_seconds", 5.0)
    r = client.post("/api/documents",
                    json={"source": "# Title\n\nReciprocity is mutual exchange "
                                    "between parties of equal standing. " * 8,
                          "kind": "text", "title": "Nudge"}, headers=auth())
    assert r.status_code == 202
    job_id = r.json()["job"]["id"]

    # No worker thread, no cron, no /api/worker call -- only the poll the
    # browser makes anyway.
    status = ""
    for _ in range(40):
        status = client.get(f"/api/jobs/{job_id}", headers=auth()).json()["status"]
        if status in ("done", "failed"):
            break
    assert status == "done", f"polling did not finish the job (last: {status})"


def test_polling_does_no_work_when_the_nudge_is_off(client, monkeypatch):
    from prism.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "poll_nudge_seconds", 0.0)
    r = client.post("/api/documents",
                    json={"source": "# T\n\nSomething to read. " * 8,
                          "kind": "text", "title": "Idle"}, headers=auth())
    job_id = r.json()["job"]["id"]
    for _ in range(5):
        body = client.get(f"/api/jobs/{job_id}", headers=auth()).json()
    assert body["status"] == "queued", "the nudge ran while disabled"
