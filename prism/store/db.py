"""
SQLite schema -- the local and test store.

It carries `user_id` on every table even though a local install has exactly one
user. That is deliberate: the tests run against SQLite, and a store with no
notion of accounts cannot catch a cross-account leak. The two implementations
are only interchangeable if they are the same shape.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


class LegacyDatabase(RuntimeError):
    """An old single-user database that needs an explicit migration."""


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS collections (
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT DEFAULT 'collection',
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    meta        TEXT DEFAULT '{}',
    PRIMARY KEY (user_id, name)
);

CREATE TABLE IF NOT EXISTS understandings (
    id          TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    title       TEXT NOT NULL,
    medium      TEXT NOT NULL,
    uri         TEXT,
    checksum    TEXT,
    collection  TEXT,
    summary     TEXT DEFAULT '',
    payload     TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_und_collection ON understandings(user_id, collection);
CREATE INDEX IF NOT EXISTS idx_und_checksum   ON understandings(user_id, checksum);

-- Nodes are denormalized out of the payload so the corpus is queryable
-- across documents: "every definition in ANTH266", "what do I keep missing".
CREATE TABLE IF NOT EXISTS nodes (
    id            TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    understanding TEXT NOT NULL,
    kind          TEXT NOT NULL,
    label         TEXT NOT NULL,
    body          TEXT NOT NULL,
    salience      REAL, difficulty REAL, concreteness REAL, confidence REAL,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, understanding)
        REFERENCES understandings(user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_nodes_und  ON nodes(user_id, understanding);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(user_id, kind);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    label, body, content='nodes', content_rowid='rowid'
);

CREATE TABLE IF NOT EXISTS renders (
    id            TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    understanding TEXT NOT NULL,
    renderer      TEXT NOT NULL,
    tier          TEXT NOT NULL,
    format        TEXT NOT NULL,
    payload       TEXT NOT NULL,
    source_checksum TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, understanding)
        REFERENCES understandings(user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_renders_und ON renders(user_id, understanding);

CREATE TABLE IF NOT EXISTS deliverables (
    id            TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    understanding TEXT NOT NULL,
    format        TEXT NOT NULL,
    tier          TEXT NOT NULL,
    payload       TEXT NOT NULL,
    source_checksum TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, understanding)
        REFERENCES understandings(user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_deliv_und ON deliverables(user_id, understanding);

-- Spaced repetition state lives with the corpus, not the render.
CREATE TABLE IF NOT EXISTS review_state (
    user_id     TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    collection  TEXT,
    ease        REAL DEFAULT 2.5,
    interval    INTEGER DEFAULT 0,
    reps        INTEGER DEFAULT 0,
    lapses      INTEGER DEFAULT 0,
    due_at      TEXT,
    PRIMARY KEY (user_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_review_due ON review_state(user_id, due_at);

-- Ingestion is long and a serverless invocation is short, so a job records its
-- own progress and is advanced a slice at a time. Mirrors public.jobs.
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'ingest',
    status        TEXT NOT NULL DEFAULT 'queued',
    understanding TEXT,
    title         TEXT NOT NULL DEFAULT '',
    input         TEXT NOT NULL DEFAULT '{}',
    state         TEXT NOT NULL DEFAULT '{}',
    total_steps   INTEGER NOT NULL DEFAULT 0,
    done_steps    INTEGER NOT NULL DEFAULT 0,
    message       TEXT NOT NULL DEFAULT '',
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    lease_until   TEXT,
    lease_by      TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_user  ON jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, lease_until);

CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    email         TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    token_budget  INTEGER,
    byo_key_ct    BLOB,
    byo_key_hint  TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    meta          TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS usage_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        TEXT NOT NULL,
    at             TEXT DEFAULT CURRENT_TIMESTAMP,
    kind           TEXT NOT NULL,
    model          TEXT NOT NULL DEFAULT '',
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    byo            INTEGER NOT NULL DEFAULT 0,
    job_id         TEXT,
    meta           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_events(user_id, at DESC);

CREATE TABLE IF NOT EXISTS usage_current (
    user_id         TEXT NOT NULL,
    month           TEXT NOT NULL,
    billable_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    calls           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, month)
);
"""

LEGACY_TABLES = ("collections", "understandings", "nodes", "renders",
                 "deliverables", "review_state")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def needs_rebuild(conn: sqlite3.Connection) -> bool:
    """A pre-accounts database: has the tables but not the user_id column."""
    return any("user_id" not in cols
               for cols in (_columns(conn, t) for t in LEGACY_TABLES) if cols)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    if needs_rebuild(conn):
        conn.close()
        raise LegacyDatabase(
            f"{path} predates per-account storage. Run `prism migrate-local "
            f"--db {path}` to move it forward, or point PRISM_HOME at a new "
            f"directory to start fresh.")
    conn.executescript(SCHEMA)
    return conn
