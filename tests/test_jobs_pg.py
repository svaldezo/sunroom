"""
The queue on Postgres, where the concurrency is real.

The SQLite tests prove the logic. These prove the thing SQLite cannot: that
several workers hitting the same queue at the same moment -- which is exactly
what a serverless platform does when it scales out -- each get a different job,
and never the same one twice.
"""
from __future__ import annotations

import os
import threading
import uuid

import pytest

DSN = os.environ.get("SUNROOM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="SUNROOM_TEST_DSN not set")


@pytest.fixture()
def pg(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from prism.config import SETTINGS
    from prism.jobs.queue import Jobs
    from prism.store import pg as pgmod

    monkeypatch.setattr(SETTINGS, "store", "postgres")
    monkeypatch.setattr(SETTINGS, "database_url", DSN)
    pgmod.close_pool()

    users: list[str] = []

    def account() -> str:
        uid = str(uuid.uuid4())
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute("INSERT INTO auth.users (id,email) VALUES (%s,%s)",
                      (uid, f"{uid[:8]}@test.local"))
        users.append(uid)
        return uid

    yield Jobs(backend="postgres"), account

    with psycopg.connect(DSN, autocommit=True) as c:
        for uid in users:
            c.execute("DELETE FROM auth.users WHERE id = %s", (uid,))
            c.execute("DELETE FROM accounts WHERE id = %s", (uid,))
    pgmod.close_pool()


def test_concurrent_workers_never_claim_the_same_job(pg):
    """
    Ten jobs, eight workers racing. Every job goes to exactly one worker.

    `FOR UPDATE SKIP LOCKED` is what makes this true. Without it two workers
    read the same row, both update it, and the same document is extracted twice
    at twice the cost -- a bug that only appears under load, which is to say in
    front of users.
    """
    q, account = pg
    uid = account()
    ids = {q.create(uid, title=f"j{i}",
                    input={"kind": "text", "value": "x"}).id for i in range(10)}

    claimed: list[str] = []
    lock = threading.Lock()

    def worker():
        while True:
            job = q.claim(owner=f"w{threading.get_ident()}")
            if job is None:
                return
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == len(set(claimed)), "a job was claimed twice"
    assert set(claimed) == ids


def test_claim_skips_a_job_under_a_live_lease(pg):
    q, account = pg
    uid = account()
    q.create(uid, title="a", input={"kind": "text", "value": "x"})
    assert q.claim(owner="w1", lease_seconds=600) is not None
    assert q.claim(owner="w2") is None


def test_expired_lease_is_reclaimed_on_postgres(pg):
    q, account = pg
    uid = account()
    job = q.create(uid, title="a", input={"kind": "text", "value": "x"})
    q.claim(owner="dead", lease_seconds=-5)
    again = q.claim(owner="alive")
    assert again is not None and again.id == job.id


def test_jobs_are_scoped_to_their_owner_on_postgres(pg):
    q, account = pg
    a, b = account(), account()
    job = q.create(a, title="a", input={"kind": "text", "value": "x"})
    assert q.get(a, job.id) is not None
    assert q.get(b, job.id) is None
    assert q.cancel(b, job.id) is False
    assert q.cancel(a, job.id) is True
