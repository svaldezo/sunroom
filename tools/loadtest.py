"""
What happens when several people use it at once.

Not a benchmark. The question is narrower and more useful: under concurrency,
does anything go *wrong* -- a job claimed twice, a document leaking between
accounts, a connection pool exhausted, latency that quietly becomes a timeout.
A p99 is interesting; a cross-tenant read at p99 is the only result that
matters.

    python tools/loadtest.py                       # SQLite
    python tools/loadtest.py --dsn postgresql://…  # Postgres
    python tools/loadtest.py --users 20 --docs 3
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "load-jwt-secret-at-least-32-characters-long!!!"
ISS = "https://load.supabase.co/auth/v1"

PARA = ("Reciprocity is the mutual give and take between parties of roughly "
        "equal standing. Redistribution requires a center that collects and "
        "then disburses. Market exchange sets prices through supply and "
        "demand.\n\n")


def token(sub: str, email: str) -> str:
    import jwt
    return jwt.encode(
        {"sub": sub, "aud": "authenticated", "iss": ISS, "role": "authenticated",
         "email": email, "exp": int(time.time()) + 7200},
        SECRET, algorithm="HS256")


def call(base: str, method: str, path: str, tok: str, body=None, timeout=60):
    req = urllib.request.Request(
        base + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {tok}"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null"), time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), time.perf_counter() - t0
    except Exception as e:                                        # noqa: BLE001
        return 0, {"detail": str(e)}, time.perf_counter() - t0


def wait_for(base: str, seconds: float = 45.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=12)
    ap.add_argument("--docs", type=int, default=2, help="documents per user")
    ap.add_argument("--reads", type=int, default=25, help="read calls per user")
    ap.add_argument("--dsn", default=os.environ.get("SUNROOM_TEST_DSN", ""))
    ap.add_argument("--port", type=int, default=8402)
    ap.add_argument("--workers", type=int, default=2, help="uvicorn processes")
    args = ap.parse_args()

    home = Path(tempfile.mkdtemp(prefix="sunroom-load-"))
    env = {**os.environ,
           "PRISM_HOME": str(home), "PRISM_PROVIDER": "mock",
           "SUNROOM_ENV": "development",
           "SUNROOM_SECRET_KEY": secrets.token_urlsafe(48),
           "SUNROOM_WORKER_SECRET": "load-worker",
           "SUNROOM_RATE_LIMIT": "0",
           "PRISM_CHUNK_CHARS": "1200",
           "SUPABASE_URL": "https://load.supabase.co",
           "SUPABASE_ANON_KEY": "anon", "SUPABASE_JWT_SECRET": SECRET}
    users = [str(uuid.uuid4()) for _ in range(args.users)]

    if args.dsn:
        import psycopg
        env["SUNROOM_STORE"] = "postgres"
        env["DATABASE_URL"] = args.dsn
        with psycopg.connect(args.dsn, autocommit=True) as c:
            for uid in users:
                c.execute("INSERT INTO auth.users (id,email) VALUES (%s,%s) "
                          "ON CONFLICT DO NOTHING", (uid, f"{uid[:8]}@load.test"))
    else:
        env["SUNROOM_STORE"] = "sqlite"

    base = f"http://127.0.0.1:{args.port}"
    print(f"{args.users} users x {args.docs} docs, {args.workers} server "
          f"process(es), store={env['SUNROOM_STORE']}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "prism.web.api:app",
         "--host", "127.0.0.1", "--port", str(args.port),
         "--workers", str(args.workers), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT)

    timings: dict[str, list[float]] = {}
    codes: Counter = Counter()
    leaks: list[str] = []
    lock = threading.Lock()

    def record(name: str, code: int, dt: float) -> None:
        with lock:
            timings.setdefault(name, []).append(dt)
            codes[code] += 1

    def session(idx: int, uid: str) -> None:
        tok = token(uid, f"{uid[:8]}@load.test")
        mine: list[str] = []

        for d in range(args.docs):
            body = {"source": f"# Doc {idx}-{d}\n\n" + PARA * 12,
                    "kind": "text", "title": f"u{idx} doc{d}",
                    "collection": f"C{idx}"}
            code, res, dt = call(base, "POST", "/api/documents", tok, body)
            record("POST /documents", code, dt)
            if code == 202:
                mine.append(res["job"]["id"])

        # Wait for this user's jobs, then hammer reads while others are still
        # ingesting -- reads competing with writes is the realistic shape.
        deadline = time.time() + 180
        docs: list[str] = []
        while time.time() < deadline and mine:
            code, jobs, dt = call(base, "GET", "/api/jobs", tok)
            record("GET /jobs", code, dt)
            done = {j["id"]: j for j in (jobs or [])
                    if j["status"] in ("done", "failed")}
            if all(j in done for j in mine):
                docs = [done[j]["understanding"] for j in mine
                        if done[j]["status"] == "done"]
                break
            time.sleep(0.7)

        for _ in range(args.reads):
            code, out, dt = call(base, "GET", "/api/documents", tok)
            record("GET /documents", code, dt)
            # The property that matters: never anybody else's work.
            if code == 200:
                titles = {d["title"] for d in out}
                foreign = {t for t in titles if not t.startswith(f"u{idx} ")}
                if foreign:
                    with lock:
                        leaks.append(f"user {idx} saw {sorted(foreign)[:3]}")
            if docs:
                code, _, dt = call(base, "GET", f"/api/documents/{docs[0]}", tok)
                record("GET /documents/{id}", code, dt)
                code, _, dt = call(base, "GET", "/api/search?q=reciprocity", tok)
                record("GET /search", code, dt)

    t0 = time.perf_counter()
    try:
        if not wait_for(base):
            print("server never became healthy")
            return 1
        threads = [threading.Thread(target=session, args=(i, uid))
                   for i, uid in enumerate(users)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0

        # -- what actually happened -------------------------------------
        print(f"\nwall clock: {elapsed:.1f}s")
        print(f"status codes: {dict(sorted(codes.items()))}")
        print(f"\n{'call':26} {'n':>5} {'p50':>8} {'p95':>8} {'max':>8}")
        for name, xs in sorted(timings.items()):
            xs = sorted(xs)
            p50 = statistics.median(xs)
            p95 = xs[min(len(xs) - 1, int(len(xs) * 0.95))]
            print(f"{name:26} {len(xs):>5} {p50*1000:>7.0f}ms "
                  f"{p95*1000:>7.0f}ms {xs[-1]*1000:>7.0f}ms")

        ok = True
        bad = {c: n for c, n in codes.items() if c not in (200, 202)}
        if bad:
            print(f"\nFAIL  non-success responses: {bad}")
            ok = False
        if leaks:
            print(f"\nFAIL  cross-account reads: {leaks[:5]}")
            ok = False
        if not ok:
            return 1
        print("\nno errors, no cross-account reads")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
