"""
The account record: who someone is to Sunroom, what they may spend, and what
they have spent.

Deliberately separate from the corpus store. The corpus is the thing a user
owns; this is the thing the operator owns *about* a user, and the two have
different access rules -- a user can delete every document they have and their
usage history stays, because it is how a bill gets explained.

Works over both backends through the same narrow set of statements, because the
alternative is a second place for SQLite and Postgres to quietly disagree.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from ..config import SETTINGS
from ..store.base import LOCAL_USER
from . import keys as keyvault


@dataclass
class Account:
    id: str
    email: str = ""
    token_budget: Optional[int] = None
    byo_key_hint: str = ""
    has_byo_key: bool = False
    is_admin: bool = False
    created_at: str = ""
    meta: dict[str, Any] = None            # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.meta is None:
            self.meta = {}

    @property
    def budget(self) -> int:
        return (self.token_budget if self.token_budget is not None
                else SETTINGS.default_token_budget)


@dataclass
class Usage:
    billable_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    budget: int = 0
    byo: bool = False

    @property
    def remaining(self) -> int:
        if self.byo:
            return -1                       # not metered
        return max(0, self.budget - self.billable_tokens)

    @property
    def fraction(self) -> float:
        if self.byo or self.budget <= 0:
            return 0.0
        return min(1.0, self.billable_tokens / self.budget)

    def to_dict(self) -> dict[str, Any]:
        return {"used": self.billable_tokens, "total": self.total_tokens,
                "calls": self.calls, "budget": self.budget,
                "remaining": self.remaining, "fraction": round(self.fraction, 4),
                "byo": self.byo, "unlimited": self.byo}


def _month() -> str:
    return date.today().replace(day=1).isoformat()


class Accounts:
    """Account and usage records. Backend-agnostic."""

    def __init__(self, *, backend: Optional[str] = None):
        self.backend = backend or SETTINGS.store
        self._pg = self.backend == "postgres"

    # -- plumbing ----------------------------------------------------------
    def _q(self, sql: str) -> str:
        """One statement, two placeholder styles."""
        return sql if self._pg else sql.replace("%s", "?")

    def _exec(self, sql: str, args: tuple = ()) -> int:
        sql = self._q(sql)
        if self._pg:
            from ..store.pg import pool
            with pool().connection() as c, c.cursor() as cur:
                cur.execute(sql, args)
                return cur.rowcount
        from ..store.db import connect
        conn = connect(SETTINGS.db_path)
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        sql = self._q(sql)
        if self._pg:
            from ..store.pg import pool
            with pool().connection() as c, c.cursor() as cur:
                cur.execute(sql, args)
                cols = [d.name for d in cur.description]
                out = []
                for row in cur.fetchall():
                    d = dict(zip(cols, row, strict=True))
                    out.append({k: (v.isoformat() if isinstance(v, (datetime, date))
                                    else v) for k, v in d.items()})
                return out
        from ..store.db import connect
        conn = connect(SETTINGS.db_path)
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def _one(self, sql: str, args: tuple = ()) -> Optional[dict[str, Any]]:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # -- accounts ----------------------------------------------------------
    def ensure(self, user_id: str, email: str = "") -> Account:
        """
        Fetch the account, creating it if this is the first request.

        Supabase's trigger already inserts a row on signup; this is the belt to
        that braces, and the only path that exists at all for SQLite. It never
        overwrites an existing row, so it cannot be used to reset someone's
        budget by signing in again.
        """
        self._exec(
            "INSERT INTO accounts (id, email) VALUES (%s, %s) "
            "ON CONFLICT (id) DO NOTHING", (user_id, email or None))
        if email:
            self._exec(
                "UPDATE accounts SET email = %s WHERE id = %s AND "
                "(email IS NULL OR email = '')", (email, user_id))
        acc = self.get(user_id)
        if acc is None:                                    # pragma: no cover
            raise RuntimeError(f"could not create account {user_id}")
        return acc

    def get(self, user_id: str) -> Optional[Account]:
        row = self._one(
            "SELECT id, email, token_budget, byo_key_hint, is_admin, meta, "
            "       created_at, "
            "       CASE WHEN byo_key_ct IS NULL THEN 0 ELSE 1 END AS has_key "
            "FROM accounts WHERE id = %s", (user_id,))
        if not row:
            return None
        meta = row.get("meta") or {}
        if isinstance(meta, str):
            meta = json.loads(meta or "{}")
        return Account(
            id=str(row["id"]), email=row.get("email") or "",
            token_budget=row.get("token_budget"),
            byo_key_hint=row.get("byo_key_hint") or "",
            has_byo_key=bool(row.get("has_key")),
            is_admin=bool(row.get("is_admin")),
            created_at=str(row.get("created_at") or ""), meta=meta)

    def set_budget(self, user_id: str, tokens: Optional[int]) -> None:
        """Operator-only. Never reachable from a user-facing route."""
        self._exec("UPDATE accounts SET token_budget = %s WHERE id = %s",
                   (tokens, user_id))

    # -- bring-your-own key ------------------------------------------------
    def set_api_key(self, user_id: str, api_key: str) -> str:
        ct = keyvault.seal(api_key)
        h = keyvault.hint(api_key)
        self._exec(
            "UPDATE accounts SET byo_key_ct = %s, byo_key_hint = %s WHERE id = %s",
            (ct, h, user_id))
        return h

    def clear_api_key(self, user_id: str) -> None:
        self._exec(
            "UPDATE accounts SET byo_key_ct = NULL, byo_key_hint = NULL "
            "WHERE id = %s", (user_id,))

    def api_key(self, user_id: str) -> Optional[str]:
        """
        The plaintext key, decrypted. Only the model client calls this, and it
        never puts the result anywhere but an SDK constructor.
        """
        row = self._one("SELECT byo_key_ct FROM accounts WHERE id = %s", (user_id,))
        ct = row and row.get("byo_key_ct")
        if not ct:
            return None
        return keyvault.unseal(ct)

    # -- usage -------------------------------------------------------------
    def record(self, user_id: str, *, kind: str, model: str = "",
               input_tokens: int = 0, output_tokens: int = 0,
               byo: bool = False, job_id: Optional[str] = None,
               meta: Optional[dict[str, Any]] = None) -> None:
        """
        Append one model call.

        Append-only and never aggregated destructively, so any month's bill can
        be reconstructed from the events that produced it.
        """
        payload = json.dumps(meta or {})
        if self._pg:
            self._exec(
                "INSERT INTO usage_events (user_id, kind, model, input_tokens, "
                " output_tokens, byo, job_id, meta) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (user_id, kind, model, input_tokens, output_tokens, byo,
                 job_id, payload))
            return
        # SQLite has no trigger here; keep the rollup in step by hand.
        self._exec(
            "INSERT INTO usage_events (user_id, at, kind, model, input_tokens, "
            " output_tokens, byo, job_id, meta) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (user_id, datetime.now(timezone.utc).isoformat(), kind, model,
             input_tokens, output_tokens, 1 if byo else 0, job_id, payload))
        billable = 0 if byo else input_tokens + output_tokens
        self._exec(
            "INSERT INTO usage_current (user_id, month, billable_tokens, "
            " total_tokens, calls) VALUES (%s,%s,%s,%s,1) "
            "ON CONFLICT (user_id, month) DO UPDATE SET "
            " billable_tokens = usage_current.billable_tokens + %s, "
            " total_tokens = usage_current.total_tokens + %s, "
            " calls = usage_current.calls + 1",
            (user_id, _month(), billable, input_tokens + output_tokens,
             billable, input_tokens + output_tokens))

    def usage(self, user_id: str) -> Usage:
        acc = self.get(user_id) or Account(id=user_id)
        month = _month()
        row = self._one(
            "SELECT billable_tokens, total_tokens, calls FROM usage_current "
            "WHERE user_id = %s AND month = %s",
            (user_id, month if not self._pg else date.fromisoformat(month)))
        return Usage(
            billable_tokens=int((row or {}).get("billable_tokens") or 0),
            total_tokens=int((row or {}).get("total_tokens") or 0),
            calls=int((row or {}).get("calls") or 0),
            budget=acc.budget, byo=acc.has_byo_key)

    def recent(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._all(
            "SELECT at, kind, model, input_tokens, output_tokens, byo "
            "FROM usage_events WHERE user_id = %s ORDER BY at DESC LIMIT %s",
            (user_id, limit))


_accounts: Optional[Accounts] = None


def accounts() -> Accounts:
    global _accounts
    if _accounts is None or _accounts.backend != SETTINGS.store:
        _accounts = Accounts()
    return _accounts


def local_account_id() -> str:
    return LOCAL_USER
