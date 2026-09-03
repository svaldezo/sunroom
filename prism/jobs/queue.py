"""
The job queue.

Ingesting a lecture recording takes minutes. A serverless function gets tens of
seconds. That mismatch is the single hardest constraint in this deployment, and
this module is the answer to it: a job carries its own progress, is advanced a
slice at a time by whoever picks it up, and is safe to interrupt at any point.

Three properties do the work:

  * **Leases, not locks.** A worker claims a job until a deadline. If it is
    frozen mid-slice -- which is exactly what a serverless platform does to an
    instance -- the lease expires and the next worker resumes from the last
    persisted checkpoint. Nothing has to notice the death.
  * **Claiming is one atomic statement.** `UPDATE ... WHERE status = 'queued'
    AND id = (SELECT ... FOR UPDATE SKIP LOCKED)` means two workers racing for
    the same job cannot both win, without a separate lock table.
  * **Attempts are counted.** A job that crashes the worker every time is a job
    that would otherwise be retried forever, so after `max_job_attempts` it
    fails with its error preserved.

Both backends implement this, because the tests run on SQLite and a queue that
is only exercised in production is a queue that does not work.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..config import SETTINGS

QUEUED, RUNNING, DONE, FAILED, CANCELLED = (
    "queued", "running", "done", "failed", "cancelled")
LIVE = (QUEUED, RUNNING)

#: How long a claim is good for. Longer than a slice, so a worker that is merely
#: slow is not stolen from; short enough that a dead one is picked up promptly.
LEASE_SECONDS = 180


def _now() -> datetime:
    return datetime.now(timezone.utc)


def worker_id() -> str:
    """Who holds a lease -- useful when a job is stuck and you want to know where."""
    return f"{os.environ.get('VERCEL_REGION') or socket.gethostname()}/{os.getpid()}"


@dataclass
class Job:
    id: str
    user_id: str
    kind: str = "ingest"
    status: str = QUEUED
    understanding: Optional[str] = None
    title: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    total_steps: int = 0
    done_steps: int = 0
    message: str = ""
    error: Optional[str] = None
    attempts: int = 0
    created_at: str = ""
    updated_at: str = ""
    finished_at: Optional[str] = None

    @property
    def is_finished(self) -> bool:
        return self.status in (DONE, FAILED, CANCELLED)

    @property
    def progress(self) -> float:
        if self.status == DONE:
            return 1.0
        if not self.total_steps:
            return 0.0
        return min(1.0, self.done_steps / self.total_steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "understanding": self.understanding, "title": self.title,
            "total_steps": self.total_steps, "done_steps": self.done_steps,
            "progress": round(self.progress, 3), "message": self.message,
            "error": self.error, "attempts": self.attempts,
            "created_at": self.created_at, "finished_at": self.finished_at,
        }


def _loads(v) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    if not v:
        return {}
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return {}


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, datetime) else (str(v) if v else "")


class Jobs:
    """Queue operations. Backend-agnostic; one class, two placeholder styles."""

    def __init__(self, *, backend: Optional[str] = None):
        self.backend = backend or SETTINGS.store
        self._pg = self.backend == "postgres"

    # -- plumbing ----------------------------------------------------------
    def _q(self, sql: str) -> str:
        return sql if self._pg else sql.replace("%s", "?")

    def _run(self, sql: str, args: tuple = (), *, fetch: str = "none"):
        sql = self._q(sql)
        if self._pg:
            from ..store.pg import pool
            with pool().connection() as c, c.cursor() as cur:
                cur.execute(sql, args)
                if fetch == "none":
                    return cur.rowcount
                cols = [d.name for d in cur.description]
                rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
                if fetch != "one":
                    return rows
                return rows[0] if rows else None
        from ..store.db import connect
        conn = connect(SETTINGS.db_path)
        try:
            cur = conn.execute(sql, args)
            if fetch == "none":
                conn.commit()
                return cur.rowcount
            rows = [dict(r) for r in cur.fetchall()]
            conn.commit()
            if fetch != "one":
                return rows
            return rows[0] if rows else None
        finally:
            conn.close()

    def _row_to_job(self, row: Optional[dict]) -> Optional[Job]:
        if not row:
            return None
        return Job(
            id=str(row["id"]), user_id=str(row["user_id"]),
            kind=row.get("kind") or "ingest", status=row["status"],
            understanding=row.get("understanding"), title=row.get("title") or "",
            input=_loads(row.get("input")), state=_loads(row.get("state")),
            total_steps=int(row.get("total_steps") or 0),
            done_steps=int(row.get("done_steps") or 0),
            message=row.get("message") or "", error=row.get("error"),
            attempts=int(row.get("attempts") or 0),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
            finished_at=_iso(row.get("finished_at")) or None,
        )

    def _json(self, value: dict) -> Any:
        return json.dumps(value)

    # -- creating ----------------------------------------------------------
    def create(self, user_id: str, *, kind: str = "ingest", title: str = "",
               input: Optional[dict] = None, total_steps: int = 0,
               message: str = "") -> Job:
        job_id = str(uuid.uuid4())
        cast = "::jsonb" if self._pg else ""
        self._run(
            f"INSERT INTO jobs (id, user_id, kind, status, title, input, state, "
            f" total_steps, message) "
            f"VALUES (%s,%s,%s,%s,%s,%s{cast},%s{cast},%s,%s)",
            (job_id, user_id, kind, QUEUED, title, self._json(input or {}),
             self._json({}), total_steps, message))
        job = self.get(user_id, job_id)
        assert job is not None
        return job

    # -- reading -----------------------------------------------------------
    def get(self, user_id: str, job_id: str) -> Optional[Job]:
        """Scoped: asking for another account's job is indistinguishable from
        asking for one that does not exist."""
        return self._row_to_job(self._run(
            "SELECT * FROM jobs WHERE user_id = %s AND id = %s",
            (user_id, job_id), fetch="one"))

    def list(self, user_id: str, *, limit: int = 20,
             active_only: bool = False) -> list[Job]:
        sql = "SELECT * FROM jobs WHERE user_id = %s"
        args: list[Any] = [user_id]
        if active_only:
            sql += " AND status IN ('queued','running')"
        sql += " ORDER BY created_at DESC LIMIT %s"
        args.append(limit)
        rows = self._run(sql, tuple(args), fetch="all") or []
        return [j for j in (self._row_to_job(r) for r in rows) if j]

    def pending_count(self) -> int:
        rows = self._run(
            "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')",
            fetch="all") or []
        return int(rows[0]["n"]) if rows else 0

    # -- claiming ----------------------------------------------------------
    def claim(self, *, owner: Optional[str] = None,
              lease_seconds: int = LEASE_SECONDS) -> Optional[Job]:
        """
        Take the oldest runnable job, atomically.

        Runnable means queued, or running with an expired lease -- the second
        case is how a job recovers from a worker that was frozen or killed
        mid-slice, which on a serverless platform is routine rather than
        exceptional.
        """
        owner = owner or worker_id()
        now = _now()
        until = now + timedelta(seconds=lease_seconds)

        if self._pg:
            row = self._run("""
                UPDATE jobs SET status = 'running', lease_by = %s,
                                lease_until = %s, attempts = attempts + 1
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE status = 'queued'
                       OR (status = 'running' AND lease_until < %s)
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1)
                RETURNING *
            """, (owner, until, now), fetch="one")
            return self._row_to_job(row)

        # SQLite has no SKIP LOCKED, but it also has one writer at a time, so a
        # conditional UPDATE on the id we just read is enough: the loser's
        # rowcount comes back 0 and it simply looks again.
        for _ in range(5):
            rows = self._run(
                "SELECT id FROM jobs WHERE status = 'queued' "
                " OR (status = 'running' AND lease_until < %s) "
                "ORDER BY created_at LIMIT 1", (now.isoformat(),), fetch="all") or []
            if not rows:
                return None
            job_id = rows[0]["id"]
            changed = self._run(
                "UPDATE jobs SET status = 'running', lease_by = %s, "
                " lease_until = %s, attempts = attempts + 1 "
                "WHERE id = %s AND (status = 'queued' OR lease_until < %s)",
                (owner, until.isoformat(), job_id, now.isoformat()))
            if changed:
                return self._row_to_job(self._run(
                    "SELECT * FROM jobs WHERE id = %s", (job_id,), fetch="one"))
        return None

    # -- advancing ---------------------------------------------------------
    def checkpoint(self, job: Job, *, state: Optional[dict] = None,
                   done_steps: Optional[int] = None,
                   total_steps: Optional[int] = None,
                   message: Optional[str] = None,
                   understanding: Optional[str] = None,
                   lease_seconds: int = LEASE_SECONDS) -> None:
        """
        Persist progress and extend the lease.

        Called after every slice of work, because the checkpoint is the only
        thing standing between a killed worker and starting over.
        """
        sets, args = [], []
        cast = "::jsonb" if self._pg else ""
        if state is not None:
            sets.append(f"state = %s{cast}")
            args.append(self._json(state))
            job.state = state
        if done_steps is not None:
            sets.append("done_steps = %s")
            args.append(done_steps)
            job.done_steps = done_steps
        if total_steps is not None:
            sets.append("total_steps = %s")
            args.append(total_steps)
            job.total_steps = total_steps
        if message is not None:
            sets.append("message = %s")
            args.append(message)
            job.message = message
        if understanding is not None:
            sets.append("understanding = %s")
            args.append(understanding)
            job.understanding = understanding
        until = _now() + timedelta(seconds=lease_seconds)
        sets.append("lease_until = %s")
        args.append(until if self._pg else until.isoformat())
        if not self._pg:
            sets.append("updated_at = %s")
            args.append(_now().isoformat())
        args.append(job.id)
        self._run(f"UPDATE jobs SET {', '.join(sets)} WHERE id = %s", tuple(args))

    def release(self, job: Job, *, message: str = "") -> None:
        """Put a partly-done job back on the queue for the next worker."""
        now = _now()
        self._run(
            "UPDATE jobs SET status = 'queued', lease_until = NULL, "
            " lease_by = NULL, message = %s"
            + ("" if self._pg else ", updated_at = %s") +
            " WHERE id = %s AND status = 'running'",
            ((message or job.message, job.id) if self._pg
             else (message or job.message, now.isoformat(), job.id)))

    def finish(self, job: Job, *, understanding: Optional[str] = None,
               message: str = "") -> None:
        now = _now()
        self._run(
            "UPDATE jobs SET status = 'done', finished_at = %s, "
            " understanding = COALESCE(%s, understanding), message = %s, "
            " lease_until = NULL, lease_by = NULL, done_steps = total_steps "
            "WHERE id = %s",
            (now if self._pg else now.isoformat(), understanding, message, job.id))
        job.status = DONE

    def fail(self, job: Job, error: str, *, retryable: bool = True) -> None:
        """
        Fail, or hand back for another attempt.

        The attempt ceiling matters: without it a job that reliably kills its
        worker is retried until someone notices the bill.
        """
        give_up = (not retryable) or job.attempts >= SETTINGS.max_job_attempts
        now = _now()
        if give_up:
            self._run(
                "UPDATE jobs SET status = 'failed', error = %s, finished_at = %s, "
                " lease_until = NULL, lease_by = NULL WHERE id = %s",
                (error[:2000], now if self._pg else now.isoformat(), job.id))
            job.status = FAILED
        else:
            self._run(
                "UPDATE jobs SET status = 'queued', error = %s, "
                " lease_until = NULL, lease_by = NULL WHERE id = %s",
                (error[:2000], job.id))
            job.status = QUEUED
        job.error = error

    def cancel(self, user_id: str, job_id: str) -> bool:
        """Only the owner, and only a job that has not finished."""
        return bool(self._run(
            "UPDATE jobs SET status = 'cancelled', finished_at = %s, "
            " lease_until = NULL, lease_by = NULL "
            "WHERE user_id = %s AND id = %s AND status IN ('queued','running')",
            ((_now() if self._pg else _now().isoformat()), user_id, job_id)))

    def is_cancelled(self, job_id: str) -> bool:
        row = self._run("SELECT status FROM jobs WHERE id = %s", (job_id,),
                        fetch="one")
        return bool(row and row["status"] == CANCELLED)


_jobs: Optional[Jobs] = None


def jobs() -> Jobs:
    global _jobs
    if _jobs is None or _jobs.backend != SETTINGS.store:
        _jobs = Jobs()
    return _jobs
