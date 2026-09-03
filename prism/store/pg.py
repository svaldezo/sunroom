"""
The Postgres corpus store -- Supabase in production.

Same interface as the SQLite store, same guarantee: an instance belongs to one
account and `self.uid` is bound into every statement. The differences that
matter:

  * Full-text search is a stored generated `tsvector` column with a GIN index
    rather than an FTS5 shadow table, so there is no rebuild step and no way
    for the index to drift from the rows.
  * Connections come from a pool with a short idle timeout, because a serverless
    invocation may be frozen mid-request and a long-lived socket to Supabase's
    pooler is a connection you are paying for and not using.
  * Writes are transactional per call. SQLite got away with a process-wide write
    lock; here concurrent writers are the normal case.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from ..config import SETTINGS
from ..formats.base import Deliverable
from ..models import Node, RenderResult, Understanding
from .base import LOCAL_USER, NotFound, StoreError
from .repository import RELEARN_SECONDS

_pool = None
_pool_lock = threading.Lock()


def _dsn() -> str:
    dsn = SETTINGS.database_url
    if not dsn:
        raise StoreError("DATABASE_URL is not set")
    # Supabase hands out postgres:// URLs; psycopg wants postgresql://.
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    return dsn


def pool():
    """
    One pool per process.

    Sized small on purpose: Vercel runs many concurrent instances of the same
    function, and Supabase's connection limit is shared across all of them. The
    transaction pooler (port 6543) is the right target in production; a big
    per-instance pool against the direct port is how a deploy takes the database
    down at the worst possible moment.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool
                size = int(os.environ.get("SUNROOM_PG_POOL", "4"))
                _pool = ConnectionPool(
                    _dsn(), min_size=0, max_size=size, timeout=15.0,
                    max_idle=60.0, kwargs={"autocommit": True},
                    open=True, name="sunroom")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _row_to_dict(cur, row) -> dict[str, Any]:
    return {d.name: v for d, v in zip(cur.description, row, strict=True)}


def _rows(cur) -> list[dict[str, Any]]:
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _iso(v) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _isod(d: dict[str, Any]) -> dict[str, Any]:
    """Timestamps leave the store as ISO strings, matching the SQLite store."""
    return {k: _iso(v) for k, v in d.items()}


class PgRepository:
    """Corpus store for one account, backed by Postgres."""

    backend = "postgres"

    def __init__(self, *, user_id: str = LOCAL_USER, dsn: Optional[str] = None):
        if not user_id:
            raise ValueError("a Repository always belongs to an account")
        self.user_id = str(user_id)
        self._dsn = dsn

    @property
    def uid(self) -> str:
        return self.user_id

    def for_user(self, user_id: str) -> "PgRepository":
        return PgRepository(user_id=user_id, dsn=self._dsn)

    # -- plumbing ----------------------------------------------------------
    def _conn(self):
        if self._dsn:
            import psycopg
            return psycopg.connect(self._dsn, autocommit=True)
        return pool().connection()

    def _exec(self, sql: str, args: tuple = ()) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(sql, args)

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(sql, args)
            return [_isod(r) for r in _rows(cur)]

    def _one(self, sql: str, args: tuple = ()) -> Optional[dict[str, Any]]:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _scalar(self, sql: str, args: tuple = ()) -> Any:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None

    # -- collections -------------------------------------------------------
    def ensure_collection(self, name: str, kind: str = "collection") -> None:
        self._exec(
            "INSERT INTO collections (user_id, name, kind) VALUES (%s,%s,%s) "
            "ON CONFLICT (user_id, name) DO NOTHING",
            (self.uid, name, kind))

    def collections(self) -> list[dict[str, Any]]:
        return self._all("""
            SELECT c.name, c.kind, COUNT(u.id)::int AS documents
            FROM collections c
            LEFT JOIN understandings u
                   ON u.collection = c.name AND u.user_id = c.user_id
            WHERE c.user_id = %s
            GROUP BY c.name, c.kind ORDER BY c.name
        """, (self.uid,))

    # -- understandings ----------------------------------------------------
    def save(self, u: Understanding) -> str:
        if u.collection:
            self.ensure_collection(u.collection)
        payload = u.model_dump_json()
        rows = [(n.id, self.uid, u.id, n.kind.value, n.label, n.body,
                 n.salience, n.difficulty, n.concreteness, n.confidence)
                for n in u.nodes]
        with self._conn() as c:
            with c.transaction(), c.cursor() as cur:
                cur.execute("""
                    INSERT INTO understandings
                      (id, user_id, source_id, title, medium, uri, checksum,
                       collection, summary, payload, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                    ON CONFLICT (user_id, id) DO UPDATE SET
                      payload = excluded.payload, summary = excluded.summary,
                      collection = excluded.collection,
                      checksum = excluded.checksum, updated_at = now()
                """, (u.id, self.uid, u.source.id, u.source.title,
                      u.source.medium.value, u.source.uri, u.source.checksum,
                      u.collection, u.summary, payload))
                cur.execute(
                    "DELETE FROM nodes WHERE user_id = %s AND understanding = %s",
                    (self.uid, u.id))
                if rows:
                    cur.executemany("""
                        INSERT INTO nodes (id, user_id, understanding, kind,
                                           label, body, salience, difficulty,
                                           concreteness, confidence)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, rows)
        return u.id

    def get(self, understanding_id: str) -> Optional[Understanding]:
        payload = self._scalar(
            "SELECT payload FROM understandings WHERE user_id = %s AND id = %s",
            (self.uid, understanding_id))
        if payload is None:
            payload = self._scalar(
                "SELECT payload FROM understandings "
                "WHERE user_id = %s AND id LIKE %s LIMIT 1",
                (self.uid, f"{understanding_id}%"))
        if payload is None:
            return None
        return Understanding.model_validate(payload)

    def find_by_checksum(self, checksum: str) -> Optional[Understanding]:
        payload = self._scalar(
            "SELECT payload FROM understandings "
            "WHERE user_id = %s AND checksum = %s LIMIT 1",
            (self.uid, checksum))
        return Understanding.model_validate(payload) if payload is not None else None

    def list(self, collection: Optional[str] = None) -> list[dict[str, Any]]:
        sql = ("SELECT u.id, u.title, u.medium, u.collection, u.updated_at, "
               "(SELECT COUNT(*)::int FROM nodes n "
               "  WHERE n.user_id = u.user_id AND n.understanding = u.id) AS nodes "
               "FROM understandings u WHERE u.user_id = %s")
        args: list[Any] = [self.uid]
        if collection:
            sql += " AND u.collection = %s"
            args.append(collection)
        sql += " ORDER BY u.updated_at DESC"
        return self._all(sql, tuple(args))

    def delete(self, understanding_id: str) -> bool:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "DELETE FROM review_state WHERE user_id = %s AND node_id IN "
                "(SELECT id FROM nodes WHERE user_id = %s AND understanding = %s)",
                (self.uid, self.uid, understanding_id))
            cur.execute(
                "DELETE FROM understandings WHERE user_id = %s AND id = %s",
                (self.uid, understanding_id))
            return cur.rowcount > 0

    # -- cross-document queries -------------------------------------------
    def search(self, query: str, *, collection: Optional[str] = None,
               kinds: Optional[Iterable[str]] = None,
               limit: int = 25) -> list[dict[str, Any]]:
        """
        Full-text across the whole corpus -- the thing a per-session tool
        cannot do.

        `websearch_to_tsquery` rather than `to_tsquery` because the input is a
        person typing in a search box: it accepts bare words, quoted phrases and
        OR without throwing a syntax error at someone who typed an apostrophe.
        """
        sql = """
            SELECT n.id, n.understanding, n.kind, n.label, n.body, n.salience,
                   u.title, u.collection,
                   ts_rank(n.fts, q) AS rank
            FROM nodes n
            JOIN understandings u
              ON u.id = n.understanding AND u.user_id = n.user_id,
                 websearch_to_tsquery('english', %s) q
            WHERE n.user_id = %s AND n.fts @@ q
        """
        args: list[Any] = [query, self.uid]
        if collection:
            sql += " AND u.collection = %s"
            args.append(collection)
        if kinds:
            ks = list(kinds)
            if ks:
                sql += " AND n.kind = ANY(%s)"
                args.append(ks)
        sql += " ORDER BY rank DESC, n.salience DESC LIMIT %s"
        args.append(limit)
        rows = self._all(sql, tuple(args))
        for r in rows:
            r.pop("rank", None)
        return rows

    def nodes_in(self, collection: str, kind: Optional[str] = None) -> list[Node]:
        sql = ("SELECT n.id, n.kind, n.label, n.body, n.salience, n.difficulty, "
               "       n.concreteness, n.confidence "
               "FROM nodes n "
               "JOIN understandings u "
               "  ON u.id = n.understanding AND u.user_id = n.user_id "
               "WHERE n.user_id = %s AND u.collection = %s")
        args: list[Any] = [self.uid, collection]
        if kind:
            sql += " AND n.kind = %s"
            args.append(kind)
        sql += " ORDER BY n.salience DESC"
        return [Node(**r) for r in self._all(sql, tuple(args))]

    # -- renders -----------------------------------------------------------
    def save_render(self, result: RenderResult, source_checksum: str = "") -> str:
        self._exec("""
            INSERT INTO renders (id, user_id, understanding, renderer, tier,
                                 format, payload, source_checksum)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id, id) DO UPDATE SET
              payload = excluded.payload,
              source_checksum = excluded.source_checksum,
              created_at = now()
        """, (result.id, self.uid, result.understanding_id, result.renderer,
              result.tier, result.format, result.model_dump_json(),
              source_checksum))
        return result.id

    def renders_for(self, understanding_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, renderer, tier, format, source_checksum, created_at "
            "FROM renders WHERE user_id = %s AND understanding = %s "
            "ORDER BY created_at DESC", (self.uid, understanding_id))

    def render_payloads(self, understanding_id: str) -> list[RenderResult]:
        rows = self._all(
            "SELECT payload FROM renders WHERE user_id = %s AND understanding = %s "
            "ORDER BY created_at DESC", (self.uid, understanding_id))
        return [RenderResult.model_validate(r["payload"]) for r in rows]

    def latest_render(self, understanding_id: str,
                      renderer: str) -> Optional[RenderResult]:
        payload = self._scalar(
            "SELECT payload FROM renders WHERE user_id = %s AND understanding = %s "
            "AND renderer = %s ORDER BY created_at DESC LIMIT 1",
            (self.uid, understanding_id, renderer))
        return RenderResult.model_validate(payload) if payload is not None else None

    def latest_renders(self, understanding_id: str) -> list[RenderResult]:
        rows = self._all("""
            SELECT DISTINCT ON (renderer) payload
            FROM renders WHERE user_id = %s AND understanding = %s
            ORDER BY renderer, created_at DESC
        """, (self.uid, understanding_id))
        return [RenderResult.model_validate(r["payload"]) for r in rows]

    def all_renders(self) -> list[dict[str, Any]]:
        return self._all("""
            SELECT r.id, r.renderer, r.tier, r.format, r.created_at,
                   r.source_checksum, r.payload,
                   u.id AS understanding, u.title, u.collection, u.checksum
            FROM (
              SELECT DISTINCT ON (understanding, renderer) *
              FROM renders WHERE user_id = %s
              ORDER BY understanding, renderer, created_at DESC
            ) r
            JOIN understandings u ON u.id = r.understanding AND u.user_id = r.user_id
            ORDER BY u.title, r.renderer
        """, (self.uid,))

    def stale_renders(self) -> list[dict[str, Any]]:
        return self._all("""
            SELECT r.id, r.renderer, u.id AS understanding, u.title
            FROM renders r
            JOIN understandings u ON u.id = r.understanding AND u.user_id = r.user_id
            WHERE r.user_id = %s
              AND r.source_checksum IS NOT NULL AND r.source_checksum <> ''
              AND r.source_checksum <> u.checksum
        """, (self.uid,))

    # -- deliverables ------------------------------------------------------
    def save_deliverable(self, d: Deliverable, source_checksum: str = "") -> str:
        self._exec("""
            INSERT INTO deliverables (id, user_id, understanding, format, tier,
                                      payload, source_checksum)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id, id) DO UPDATE SET
              payload = excluded.payload,
              source_checksum = excluded.source_checksum,
              created_at = now()
        """, (d.id, self.uid, d.understanding_id, d.format, d.tier,
              d.model_dump_json(), source_checksum))
        return d.id

    def latest_deliverable(self, understanding_id: str,
                           fmt: str) -> Optional[Deliverable]:
        payload = self._scalar(
            "SELECT payload FROM deliverables WHERE user_id = %s "
            "AND understanding = %s AND format = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (self.uid, understanding_id, fmt))
        return Deliverable.model_validate(payload) if payload is not None else None

    def deliverables_for(self, understanding_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, format, tier, source_checksum, created_at "
            "FROM deliverables WHERE user_id = %s AND understanding = %s "
            "ORDER BY created_at DESC", (self.uid, understanding_id))

    def deliverable_payloads(self, understanding_id: str) -> list[Deliverable]:
        rows = self._all("""
            SELECT DISTINCT ON (format) payload
            FROM deliverables WHERE user_id = %s AND understanding = %s
            ORDER BY format, created_at DESC
        """, (self.uid, understanding_id))
        return [Deliverable.model_validate(r["payload"]) for r in rows]

    def all_deliverables(self) -> list[dict[str, Any]]:
        return self._all("""
            SELECT d.id, d.format, d.tier, d.created_at, d.source_checksum,
                   d.payload, u.id AS understanding, u.title, u.collection,
                   u.checksum
            FROM (
              SELECT DISTINCT ON (understanding, format) *
              FROM deliverables WHERE user_id = %s
              ORDER BY understanding, format, created_at DESC
            ) d
            JOIN understandings u ON u.id = d.understanding AND u.user_id = d.user_id
            ORDER BY u.title, d.format
        """, (self.uid,))

    # -- review ------------------------------------------------------------
    def schedule(self, node_id: str, collection: Optional[str], *,
                 correct: bool) -> dict[str, Any]:
        owns = self._scalar(
            "SELECT 1 FROM nodes WHERE user_id = %s AND id = %s LIMIT 1",
            (self.uid, node_id))
        if not owns:
            raise NotFound(f"no such passage: {node_id}")

        row = self._one(
            "SELECT ease, interval, reps, lapses FROM review_state "
            "WHERE user_id = %s AND node_id = %s", (self.uid, node_id))
        ease = row["ease"] if row else 2.5
        interval = row["interval"] if row else 0
        reps = row["reps"] if row else 0
        lapses = row["lapses"] if row else 0

        if correct:
            reps += 1
            interval = (1 if reps == 1 else
                        6 if reps == 2 else max(1, round(interval * ease)))
            ease = min(2.8, ease + 0.1)
            delay = timedelta(days=interval)
        else:
            reps, lapses, interval = 0, lapses + 1, 0
            ease = max(1.3, ease - 0.2)
            delay = timedelta(seconds=RELEARN_SECONDS)

        due_at = datetime.now(timezone.utc) + delay
        self._exec("""
            INSERT INTO review_state
              (user_id, node_id, collection, ease, interval, reps, lapses, due_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id, node_id) DO UPDATE SET
              ease = excluded.ease, interval = excluded.interval,
              reps = excluded.reps, lapses = excluded.lapses,
              due_at = excluded.due_at
        """, (self.uid, node_id, collection, ease, interval, reps, lapses, due_at))
        return {"node_id": node_id, "ease": ease, "interval": interval,
                "reps": reps, "lapses": lapses, "due_at": due_at.isoformat()}

    def due(self, collection: Optional[str] = None,
            limit: int = 50) -> list[dict[str, Any]]:
        sql = ("SELECT r.node_id, r.due_at, r.lapses, r.reps, "
               "       n.label, n.body, n.kind, n.understanding "
               "FROM review_state r "
               "JOIN nodes n ON n.id = r.node_id AND n.user_id = r.user_id "
               "WHERE r.user_id = %s AND (r.due_at <= now() OR "
               "  (r.interval = 0 AND r.lapses > 0 "
               "   AND r.due_at <= now() + %s * interval '1 second'))")
        args: list[Any] = [self.uid, RELEARN_SECONDS]
        if collection:
            sql += " AND r.collection = %s"
            args.append(collection)
        sql += " ORDER BY r.due_at ASC, r.lapses DESC LIMIT %s"
        args.append(limit)
        return self._all(sql, tuple(args))

    def close(self) -> None:
        # The pool outlives any one repository; closing it here would break
        # every other request in the process.
        return None
