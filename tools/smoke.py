"""
Boot the application the way it is deployed, and use it over HTTP.

A green unit suite and an application that will not start are perfectly
compatible states, and only one of them gets discovered by users. This starts a
real uvicorn process against a real socket, signs in with a real token, walks
the whole path -- add a source, wait for the job, read it, make a format, ask a
question, practise a card, check the audit -- and asserts on what comes back.

Run it directly:

    python tools/smoke.py
    python tools/smoke.py --dsn postgresql://...      # against Postgres

Exit code 0 means the thing works when started from cold.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "smoke-jwt-secret-at-least-32-characters-long!!"
ISS = "https://smoke.supabase.co/auth/v1"
ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
WORKER = "smoke-worker-secret"

DOC = """# Exchange and Obligation

Reciprocity is the mutual give and take between parties of roughly equal standing.
It is the dominant mode in societies without centralized authority.
Redistribution requires a center that collects and then disburses.

## Forms of reciprocity

Generalized reciprocity involves giving without a specified expectation of return.
It predominates within households and among close kin.
Balanced reciprocity involves an explicit expectation of equivalent return.
Negative reciprocity is the attempt to get something for nothing.
"""

PASS, FAIL = "  ok  ", " FAIL "
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL} {name}" + (f"  — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def token(sub: str, email: str) -> str:
    import jwt
    return jwt.encode(
        {"sub": sub, "aud": "authenticated", "iss": ISS, "role": "authenticated",
         "email": email, "exp": int(time.time()) + 3600},
        SECRET, algorithm="HS256")


class Client:
    def __init__(self, base: str, tok: str = "", worker: str = ""):
        self.base, self.tok, self.worker = base, tok, worker

    def __call__(self, method: str, path: str, body=None, expect=(200, 202)):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.tok}"} if self.tok else {}),
                     **({"X-Worker-Secret": self.worker} if self.worker else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read() or b"null")
                return r.status, payload
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")


def seed_identities(dsn: str) -> None:
    """Stand in for Supabase Auth.

    On a real project the only way to hold a valid token is to have signed in,
    which means auth.users already has the row and the trigger already made the
    accounts row. Against a local Postgres nothing has done that, so the first
    request dies on the accounts foreign key -- and it dies *sometimes*, because
    whether the row exists depends on whether the test suite ran first. Seeding
    here makes this script mean the same thing whatever ran before it.
    """
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        for uid, email in ((ALICE, "alice@smoke.test"), (BOB, "bob@smoke.test")):
            c.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s) "
                      "ON CONFLICT (id) DO NOTHING", (uid, email))


def wait_for(base: str, seconds: float = 45.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("SUNROOM_TEST_DSN", ""))
    ap.add_argument("--port", type=int, default=8399)
    ap.add_argument("--keep", action="store_true", help="leave the server running")
    args = ap.parse_args()

    home = Path(tempfile.mkdtemp(prefix="sunroom-smoke-"))
    env = {
        **os.environ,
        "PRISM_HOME": str(home),
        "PRISM_PROVIDER": "mock",
        "SUNROOM_ENV": "development",
        "SUNROOM_SECRET_KEY": secrets.token_urlsafe(48),
        "SUNROOM_WORKER_SECRET": WORKER,
        "SUNROOM_RATE_LIMIT": "0",
        "PRISM_CHUNK_CHARS": "900",
        "SUPABASE_URL": "https://smoke.supabase.co",
        "SUPABASE_ANON_KEY": "smoke-anon-key",
        "SUPABASE_JWT_SECRET": SECRET,
    }
    if args.dsn:
        env["SUNROOM_STORE"] = "postgres"
        env["DATABASE_URL"] = args.dsn
        seed_identities(args.dsn)
    else:
        env["SUNROOM_STORE"] = "sqlite"

    base = f"http://127.0.0.1:{args.port}"
    print(f"starting on {base} ({env['SUNROOM_STORE']})")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "prism.web.api:app",
         "--host", "127.0.0.1", "--port", str(args.port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    try:
        if not wait_for(base):
            print("server never became healthy; output follows:")
            proc.terminate()
            print(proc.communicate(timeout=10)[0][-4000:])
            return 1

        alice = Client(base, token(ALICE, "alice@smoke.test"))
        bob = Client(base, token(BOB, "bob@smoke.test"))
        anon = Client(base)

        # -- it is closed by default ----------------------------------------
        code, _ = anon("GET", "/api/documents")
        check("closed without a session", code == 401, f"got {code}")
        code, _ = Client(base, "not.a.token")("GET", "/api/documents")
        check("closed to a junk token", code == 401, f"got {code}")

        # -- and open with one ----------------------------------------------
        code, me = alice("GET", "/api/me")
        check("signs in", code == 200 and me.get("id") == ALICE, str(me))
        check("has a budget", (me.get("usage") or {}).get("budget", 0) > 0)

        # -- the whole ingest path ------------------------------------------
        code, res = alice("POST", "/api/documents",
                          {"source": DOC * 4, "kind": "text",
                           "title": "Exchange", "collection": "SMOKE"})
        check("queues an ingest", code == 202 and res.get("job"), str(res))
        job_id = (res.get("job") or {}).get("id")

        deadline, job = time.time() + 120, {}
        while time.time() < deadline:
            _, job = alice("GET", f"/api/jobs/{job_id}")
            if job.get("status") in ("done", "failed"):
                break
            time.sleep(1)
        check("the job finishes", job.get("status") == "done",
              f"{job.get('status')}: {job.get('error')}")
        doc_id = job.get("understanding")
        check("the job produced a document", bool(doc_id))
        if not doc_id:
            raise SystemExit(1)

        # -- and the result is real -----------------------------------------
        _, doc = alice("GET", f"/api/documents/{doc_id}")
        check("the document has passages", len(doc.get("nodes", [])) > 3,
              str(len(doc.get("nodes", []))))
        check("the document has sections", len(doc.get("sections", [])) > 0)
        check("the document has a summary", bool(doc.get("summary")))

        _, spans = alice("GET", f"/api/documents/{doc_id}/spans")
        check("every passage is citable", bool(spans) and all(s.get("anchor") for s in spans))

        _, brief = alice("POST", f"/api/documents/{doc_id}/format/brief")
        check("makes a brief", bool(brief.get("parts")), str(brief)[:200])
        check("the brief is fully grounded",
              (brief.get("fidelity") or {}).get("grounding") == 1.0,
              str((brief.get("fidelity") or {}).get("grounding")))
        check("the brief cites the source",
              any(p.get("spans") for p in brief.get("parts", [])))

        _, ans = alice("POST", f"/api/documents/{doc_id}/ask",
                       {"question": "What is reciprocity?"})
        check("answers a question", bool(ans))
        _, off = alice("POST", f"/api/documents/{doc_id}/ask",
                       {"question": "What is the capital of France?"})
        check("declines what the source does not cover",
              bool(off.get("declined")) or "does not cover" in json.dumps(off).lower(),
              json.dumps(off)[:160])

        # Schedule several: a card whose prompt would contain its own answer is
        # withheld by design, so asserting on one particular node would be
        # asserting on which node happened to make a good card.
        nodes = [n["id"] for n in doc["nodes"][:6]]
        codes = [alice("POST", "/api/review",
                       {"node_id": n, "correct": False})[0] for n in nodes]
        check("schedules cards", all(c == 200 for c in codes), str(codes))
        _, due = alice("GET", "/api/review")
        check("cards come back due", bool(due), str(due)[:120])
        check("every card is answerable",
              all(c.get("prompt") and c.get("answer") for c in due))
        check("no card contains its own answer",
              all(c["answer"].lower() not in c["prompt"].lower() for c in due),
              json.dumps([c for c in due
                          if c["answer"].lower() in c["prompt"].lower()])[:200])

        _, audit = alice("GET", "/api/audit")
        check("the audit sees the work", audit.get("total", 0) >= 1)
        check("nothing failed its checks", not audit.get("problems"),
              json.dumps(audit.get("problems"))[:200])

        # -- tenancy ---------------------------------------------------------
        _, bobs = bob("GET", "/api/documents")
        check("another account sees an empty library", bobs == [], str(bobs))
        code, _ = bob("GET", f"/api/documents/{doc_id}", expect=(404,))
        check("another account cannot read the document", code == 404, f"got {code}")
        code, out = bob("DELETE", f"/api/documents/{doc_id}")
        check("another account cannot delete it", out == {"deleted": False}, str(out))
        code, _ = bob("GET", f"/api/jobs/{job_id}")
        check("another account cannot see the job", code == 404, f"got {code}")

        # -- the worker endpoint ---------------------------------------------
        code, _ = anon("POST", "/api/worker/run")
        check("the worker refuses strangers", code == 401, f"got {code}")
        code, _ = alice("POST", "/api/worker/run")
        check("a user token does not open the worker", code == 401, f"got {code}")
        code, _ = Client(base, worker=WORKER)("POST", "/api/worker/run")
        check("the worker accepts its secret", code == 200, f"got {code}")

        # -- dangerous input --------------------------------------------------
        code, _ = alice("POST", "/api/documents",
                        {"source": "/etc/passwd", "kind": "path"})
        check("refuses a filesystem path", code == 400, f"got {code}")
        code, _ = alice("POST", "/api/uploads/sign",
                        {"filename": "payload.exe", "size": 10})
        check("refuses an unsupported upload", code == 400, f"got {code}")

        # -- and it is still there afterwards ---------------------------------
        check("the server is still up", wait_for(base, 5))

    finally:
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(home, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: " + ", ".join(failures))
        return 1
    print("smoke: everything answered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
