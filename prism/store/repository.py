"""
The SQLite corpus store -- local installs, development, and the test suite.

This is the object the user actually owns: many sources, organized however they
like (by class, by project, by whim), each stored as a persistent Understanding
that can be re-rendered into any medium at any time, and queried across.

Every instance belongs to exactly one account. `self.uid` is spliced into every
query, and there is no method that takes a user id -- reading somebody else's
document is not a bug you can write here.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..config import SETTINGS
from ..formats.base import Deliverable
from ..models import Node, RenderResult, Understanding
from .base import LOCAL_USER, NotFound
from .db import connect

#: How soon a missed card returns. Short enough to be the same sitting, long
#: enough that a few other cards come between you and the answer you just saw.
RELEARN_SECONDS = 9 * 60


def _now() -> str:
    """Microsecond-resolution UTC, so "latest" is never a coin flip."""
    return datetime.now(timezone.utc).isoformat()


class Repository:
    """
    Corpus store for one account.

    Connections are thread-local. A single shared sqlite3 connection raises
    "SQLite objects created in a thread can only be used in that same thread"
    the moment the web server serves two requests on different worker threads
    -- which it does as soon as anyone actually uses the app. WAL mode (set in
    the schema) lets the per-thread connections read concurrently while one
    writes.
    """

    backend = "sqlite"

    def __init__(self, path=None, *, user_id: str = LOCAL_USER):
        if not user_id:
            raise ValueError("a Repository always belongs to an account")
        self.path = path or SETTINGS.db_path
        self.user_id = str(user_id)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        connect(self.path).close()          # ensure schema exists once

    @property
    def uid(self) -> str:
        return self.user_id

    def for_user(self, user_id: str) -> "Repository":
        """A view of the same file scoped to a different account."""
        return Repository(self.path, user_id=user_id)

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.path)
            self._local.conn = conn
        return conn

    # -- collections -------------------------------------------------------
    def ensure_collection(self, name: str, kind: str = "collection") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO collections(user_id, name, kind) VALUES (?,?,?)",
            (self.uid, name, kind))
        self.conn.commit()

    def collections(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT c.name, c.kind, COUNT(u.id) AS documents
            FROM collections c
            LEFT JOIN understandings u
                   ON u.collection = c.name AND u.user_id = c.user_id
            WHERE c.user_id = ?
            GROUP BY c.name, c.kind ORDER BY c.name
        """, (self.uid,)).fetchall()
        return [dict(r) for r in rows]

    # -- understandings ----------------------------------------------------
    def save(self, u: Understanding) -> str:
        with self._write_lock:
            return self._save(u)

    def _save(self, u: Understanding) -> str:
        if u.collection:
            self.ensure_collection(u.collection)
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO understandings
                (id, user_id, source_id, title, medium, uri, checksum,
                 collection, summary, payload, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id, id) DO UPDATE SET
                payload=excluded.payload, summary=excluded.summary,
                collection=excluded.collection, checksum=excluded.checksum,
                updated_at=excluded.updated_at
        """, (u.id, self.uid, u.source.id, u.source.title, u.source.medium.value,
              u.source.uri, u.source.checksum, u.collection, u.summary,
              u.model_dump_json(), now))

        self.conn.execute(
            "DELETE FROM nodes WHERE user_id = ? AND understanding = ?",
            (self.uid, u.id))
        self.conn.executemany("""
            INSERT INTO nodes (id, user_id, understanding, kind, label, body,
                               salience, difficulty, concreteness, confidence)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, [(n.id, self.uid, u.id, n.kind.value, n.label, n.body,
               n.salience, n.difficulty, n.concreteness, n.confidence) for n in u.nodes])
        self.conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
        self.conn.commit()
        return u.id

    def get(self, understanding_id: str) -> Optional[Understanding]:
        row = self.conn.execute(
            "SELECT payload FROM understandings WHERE user_id = ? AND id = ?",
            (self.uid, understanding_id)).fetchone()
        if not row:
            row = self.conn.execute(
                "SELECT payload FROM understandings "
                "WHERE user_id = ? AND id LIKE ? LIMIT 2",
                (self.uid, f"{understanding_id}%")).fetchone()
        return Understanding.model_validate_json(row["payload"]) if row else None

    def find_by_checksum(self, checksum: str) -> Optional[Understanding]:
        row = self.conn.execute(
            "SELECT payload FROM understandings WHERE user_id = ? AND checksum = ? "
            "LIMIT 1", (self.uid, checksum)).fetchone()
        return Understanding.model_validate_json(row["payload"]) if row else None

    def list(self, collection: Optional[str] = None) -> list[dict[str, Any]]:
        sql = ("SELECT id, title, medium, collection, updated_at, "
               "(SELECT COUNT(*) FROM nodes n "
               " WHERE n.user_id = u.user_id AND n.understanding = u.id) AS nodes "
               "FROM understandings u WHERE u.user_id = ?")
        args: tuple = (self.uid,)
        if collection:
            sql += " AND collection = ?"
            args = (self.uid, collection)
        sql += " ORDER BY updated_at DESC"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def delete(self, understanding_id: str) -> bool:
        with self._write_lock:
            # SQLite only cascades when foreign keys are on for this connection,
            # and a stray render outliving its document would show up in Checks
            # as a permanently unfixable finding.
            self.conn.execute("PRAGMA foreign_keys=ON")
            for table in ("renders", "deliverables", "nodes"):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE user_id = ? AND understanding = ?",
                    (self.uid, understanding_id))
            self.conn.execute(
                "DELETE FROM review_state WHERE user_id = ? AND node_id IN "
                "(SELECT id FROM nodes WHERE user_id = ? AND understanding = ?)",
                (self.uid, self.uid, understanding_id))
            cur = self.conn.execute(
                "DELETE FROM understandings WHERE user_id = ? AND id = ?",
                (self.uid, understanding_id))
            self.conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
            self.conn.commit()
        return cur.rowcount > 0

    # -- cross-document queries -------------------------------------------
    @staticmethod
    def _fts_query(raw: str) -> str:
        """
        Turn what a person typed into something FTS5 will accept.

        FTS5 MATCH is a query language, not a search box: an apostrophe, an
        ampersand or an unbalanced quote is a syntax error, which reached the
        user as a 500 the first time anyone searched for "Malinowski's". Each
        bare term is quoted (making it a literal), quoted phrases are kept as
        phrases, and a trailing word gets a prefix wildcard so search feels
        live while you type.
        """
        terms = re.findall(r'"([^"]*)"|(\S+)', raw or "")
        parts: list[str] = []
        for phrase, word in terms:
            token = (phrase or word).strip()
            token = re.sub(r'["\x00]', " ", token).strip()
            if not token:
                continue
            parts.append('"' + token + '"')
        if not parts:
            return ""
        if not raw.rstrip().endswith(('"', " ")):
            parts[-1] = parts[-1] + "*"     # prefix-match the word being typed
        return " ".join(parts)

    def search(self, query: str, *, collection: Optional[str] = None,
               kinds: Optional[Iterable[str]] = None, limit: int = 25) -> list[dict[str, Any]]:
        """Full-text across the whole corpus -- the thing a per-session tool can't do."""
        sql = """
            SELECT n.id, n.understanding, n.kind, n.label, n.body, n.salience,
                   u.title, u.collection
            FROM nodes_fts f
            JOIN nodes n ON n.rowid = f.rowid
            JOIN understandings u ON u.id = n.understanding AND u.user_id = n.user_id
            WHERE nodes_fts MATCH ? AND n.user_id = ?
        """
        match = self._fts_query(query)
        if not match:
            return []
        args: list[Any] = [match, self.uid]
        if collection:
            sql += " AND u.collection = ?"
            args.append(collection)
        if kinds:
            ks = list(kinds)
            sql += f" AND n.kind IN ({','.join('?' * len(ks))})"
            args += ks
        sql += " ORDER BY n.salience DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def nodes_in(self, collection: str, kind: Optional[str] = None) -> list[Node]:
        sql = ("SELECT n.* FROM nodes n "
               "JOIN understandings u ON u.id = n.understanding AND u.user_id = n.user_id "
               "WHERE n.user_id = ? AND u.collection = ?")
        args: list[Any] = [self.uid, collection]
        if kind:
            sql += " AND n.kind = ?"
            args.append(kind)
        sql += " ORDER BY n.salience DESC"
        rows = self.conn.execute(sql, args).fetchall()
        return [Node(id=r["id"], kind=r["kind"], label=r["label"], body=r["body"],
                     salience=r["salience"], difficulty=r["difficulty"],
                     concreteness=r["concreteness"], confidence=r["confidence"])
                for r in rows]

    # -- renders -----------------------------------------------------------
    def save_render(self, result: RenderResult, source_checksum: str = "") -> str:
        with self._write_lock:
            return self._save_render(result, source_checksum)

    def _save_render(self, result: RenderResult, source_checksum: str = "") -> str:
        # An explicit microsecond timestamp, not CURRENT_TIMESTAMP. SQLite's
        # default has one-second resolution, so three renders produced in the
        # same second tied on created_at and "latest" returned whichever the
        # planner reached first -- re-rendering twice quickly showed the older
        # one. Postgres's now() has microseconds; this makes the two agree.
        self.conn.execute("""
            INSERT OR REPLACE INTO renders
                (id, user_id, understanding, renderer, tier, format, payload,
                 source_checksum, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (result.id, self.uid, result.understanding_id, result.renderer,
              result.tier, result.format, result.model_dump_json(),
              source_checksum, _now()))
        self.conn.commit()
        return result.id

    def renders_for(self, understanding_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, renderer, tier, format, source_checksum, created_at "
            "FROM renders WHERE user_id = ? AND understanding = ? "
            "ORDER BY created_at DESC", (self.uid, understanding_id)).fetchall()
        return [dict(r) for r in rows]

    def render_payloads(self, understanding_id: str) -> list[RenderResult]:
        rows = self.conn.execute(
            "SELECT payload FROM renders WHERE user_id = ? AND understanding = ? "
            "ORDER BY created_at DESC", (self.uid, understanding_id)).fetchall()
        return [RenderResult.model_validate_json(r["payload"]) for r in rows]

    def latest_render(self, understanding_id: str, renderer: str) -> Optional[RenderResult]:
        row = self.conn.execute(
            "SELECT payload FROM renders WHERE user_id = ? AND understanding = ? "
            "AND renderer = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (self.uid, understanding_id, renderer)).fetchone()
        return RenderResult.model_validate_json(row["payload"]) if row else None

    def latest_renders(self, understanding_id: str) -> list[RenderResult]:
        """
        The current rendering of each medium, not the full history.

        Every render is stored, so a document rendered five times had five
        stored copies of the same diagram -- and reverse tracing showed the
        same output unit five times over.
        """
        rows = self.conn.execute("""
            SELECT payload FROM renders
            WHERE user_id = ? AND understanding = ?
              AND id IN (SELECT id FROM renders r2
                         WHERE r2.user_id = renders.user_id
                           AND r2.understanding = renders.understanding
                           AND r2.renderer = renders.renderer
                         ORDER BY r2.created_at DESC, r2.rowid DESC LIMIT 1)
            ORDER BY created_at DESC
        """, (self.uid, understanding_id)).fetchall()
        return [RenderResult.model_validate_json(r["payload"]) for r in rows]

    def all_renders(self) -> list[dict[str, Any]]:
        """Latest rendering per (document, medium) — the corpus as it stands."""
        rows = self.conn.execute("""
            SELECT r.id, r.renderer, r.tier, r.format, r.created_at,
                   r.source_checksum, r.payload,
                   u.id AS understanding, u.title, u.collection, u.checksum
            FROM renders r
            JOIN understandings u ON u.id = r.understanding AND u.user_id = r.user_id
            WHERE r.user_id = ? AND r.rowid IN (
                SELECT rowid FROM renders r2
                WHERE r2.user_id = r.user_id
                  AND r2.understanding = r.understanding AND r2.renderer = r.renderer
                ORDER BY r2.created_at DESC, r2.rowid DESC LIMIT 1)
            ORDER BY u.title, r.renderer
        """, (self.uid,)).fetchall()
        return [dict(r) for r in rows]

    def stale_renders(self) -> list[dict[str, Any]]:
        """Renders whose source has changed since they were produced.

        This is the sync property that one-shot generation cannot offer.
        """
        rows = self.conn.execute("""
            SELECT r.id, r.renderer, u.id AS understanding, u.title
            FROM renders r
            JOIN understandings u ON u.id = r.understanding AND u.user_id = r.user_id
            WHERE r.user_id = ?
              AND r.source_checksum IS NOT NULL
              AND r.source_checksum != ''
              AND r.source_checksum != u.checksum
        """, (self.uid,)).fetchall()
        return [dict(r) for r in rows]

    # -- deliverables ------------------------------------------------------
    def save_deliverable(self, d: Deliverable, source_checksum: str = "") -> str:
        with self._write_lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO deliverables
                    (id, user_id, understanding, format, tier, payload,
                     source_checksum, created_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (d.id, self.uid, d.understanding_id, d.format, d.tier,
                  d.model_dump_json(), source_checksum, _now()))
            self.conn.commit()
        return d.id

    def latest_deliverable(self, understanding_id: str, fmt: str) -> Optional[Deliverable]:
        row = self.conn.execute(
            "SELECT payload FROM deliverables WHERE user_id = ? AND understanding = ? "
            "AND format = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (self.uid, understanding_id, fmt)).fetchone()
        return Deliverable.model_validate_json(row["payload"]) if row else None

    def deliverables_for(self, understanding_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, format, tier, source_checksum, created_at FROM deliverables "
            "WHERE user_id = ? AND understanding = ? ORDER BY created_at DESC",
            (self.uid, understanding_id)).fetchall()
        return [dict(r) for r in rows]

    def deliverable_payloads(self, understanding_id: str) -> list[Deliverable]:
        """
        The current deliverable of each format, deserialized.

        The reverse-trace route used to reach into `repo().conn` and run its own
        SELECT, which is fine right up until the store is Postgres and there is
        no `.conn` to reach into. Anything that needs rows belongs behind a
        method on the store.
        """
        rows = self.conn.execute("""
            SELECT payload FROM deliverables
            WHERE user_id = ? AND understanding = ?
              AND rowid IN (SELECT rowid FROM deliverables d2
                            WHERE d2.user_id = deliverables.user_id
                              AND d2.understanding = deliverables.understanding
                              AND d2.format = deliverables.format
                            ORDER BY d2.created_at DESC, d2.rowid DESC LIMIT 1)
            ORDER BY created_at DESC
        """, (self.uid, understanding_id)).fetchall()
        return [Deliverable.model_validate_json(r["payload"]) for r in rows]

    def all_deliverables(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT d.id, d.format, d.tier, d.created_at, d.source_checksum, d.payload,
                   u.id AS understanding, u.title, u.collection, u.checksum
            FROM deliverables d
            JOIN understandings u ON u.id = d.understanding AND u.user_id = d.user_id
            WHERE d.user_id = ? AND d.rowid IN (
                SELECT rowid FROM deliverables d2
                WHERE d2.user_id = d.user_id
                  AND d2.understanding = d.understanding AND d2.format = d.format
                ORDER BY d2.created_at DESC, d2.rowid DESC LIMIT 1)
            ORDER BY u.title, d.format
        """, (self.uid,)).fetchall()
        return [dict(r) for r in rows]

    # -- review scheduling -------------------------------------------------
    def schedule(self, node_id: str, collection: Optional[str], *, correct: bool) -> dict[str, Any]:
        """SM-2. Review state belongs to the corpus, so it accrues across sources."""
        with self._write_lock:
            return self._schedule(node_id, collection, correct=correct)

    def _schedule(self, node_id: str, collection: Optional[str], *, correct: bool) -> dict[str, Any]:
        # Review state keys on a node id supplied by the client. Without this
        # check, posting somebody else's node id would create a row in your
        # schedule referencing a document you cannot see -- harmless-looking,
        # and a way to confirm that a given id exists.
        owns = self.conn.execute(
            "SELECT 1 FROM nodes WHERE user_id = ? AND id = ? LIMIT 1",
            (self.uid, node_id)).fetchone()
        if not owns:
            raise NotFound(f"no such passage: {node_id}")
        row = self.conn.execute(
            "SELECT * FROM review_state WHERE user_id = ? AND node_id = ?",
            (self.uid, node_id)).fetchone()
        ease = row["ease"] if row else 2.5
        interval = row["interval"] if row else 0
        reps = row["reps"] if row else 0
        lapses = row["lapses"] if row else 0

        if correct:
            reps += 1
            interval = 1 if reps == 1 else (6 if reps == 2 else max(1, round(interval * ease)))
            ease = min(2.8, ease + 0.1)
            delay = interval * 86400.0
        else:
            # A missed card comes back in this session, not tomorrow. Plain SM-2
            # sends a lapse to a one-day interval, which means the one thing you
            # just demonstrated you do not know is the one thing you cannot
            # practise. `interval` stays 0 so the next correct answer starts the
            # ladder again from the bottom.
            reps, lapses, interval = 0, lapses + 1, 0
            ease = max(1.3, ease - 0.2)
            delay = RELEARN_SECONDS

        due = datetime.now(timezone.utc).timestamp() + delay
        due_at = datetime.fromtimestamp(due, tz=timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO review_state
                (user_id, node_id, collection, ease, interval, reps, lapses, due_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id, node_id) DO UPDATE SET
                ease=excluded.ease, interval=excluded.interval, reps=excluded.reps,
                lapses=excluded.lapses, due_at=excluded.due_at
        """, (self.uid, node_id, collection, ease, interval, reps, lapses, due_at))
        self.conn.commit()
        return {"node_id": node_id, "ease": ease, "interval": interval,
                "reps": reps, "lapses": lapses, "due_at": due_at}

    def due(self, collection: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        # A card in relearning is due within minutes, not days, and the point of
        # a short step is that it comes back in the same sitting. Including the
        # relearning horizon here is what makes that happen without the client
        # having to keep its own session queue.
        horizon = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + RELEARN_SECONDS,
            tz=timezone.utc).isoformat()
        sql = ("SELECT r.node_id, r.due_at, r.lapses, r.reps, "
               "n.label, n.body, n.kind, n.understanding "
               "FROM review_state r "
               "JOIN nodes n ON n.id = r.node_id AND n.user_id = r.user_id "
               "WHERE r.user_id = ? AND (r.due_at <= ? OR "
               "  (r.interval = 0 AND r.lapses > 0 AND r.due_at <= ?))")
        args: list[Any] = [self.uid, now, horizon]
        if collection:
            sql += " AND r.collection = ?"
            args.append(collection)
        # Oldest first. Ordering by lapses put the card you just missed straight
        # back in front of you, with the answer still on screen a moment ago.
        sql += " ORDER BY r.due_at ASC, r.lapses DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
