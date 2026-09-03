"""
The RLS policies, executed.

Everything else in this suite reaches the database through the service role,
which bypasses RLS by design -- so none of it touches a single policy. These
tests become `anon` and `authenticated` for real, the way PostgREST does when a
browser calls the database directly, and check what the database actually hands
back.

A policy that is never executed is a comment. This is the file that makes them
rules.

Needs a database with the migrations applied:

    ./supabase/local/apply.sh postgresql://postgres@127.0.0.1:5432/sunroom_test
    SUNROOM_TEST_DSN=postgresql://postgres@127.0.0.1:5432/sunroom_test pytest -q tests/test_rls.py
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

DSN = os.environ.get("SUNROOM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="SUNROOM_TEST_DSN not set")

ALICE = "aaaaaaaa-0000-4000-8000-000000000001"
BOB = "bbbbbbbb-0000-4000-8000-000000000002"


@pytest.fixture(scope="module")
def db():
    psycopg = pytest.importorskip("psycopg")
    conn = psycopg.connect(DSN, autocommit=True)

    # Two accounts with a document each. Written as the owner, which is how the
    # server writes -- the point of the tests is what happens on the way out.
    for uid, email in ((ALICE, "alice@rls.test"), (BOB, "bob@rls.test")):
        conn.execute("INSERT INTO auth.users (id,email) VALUES (%s,%s) "
                     "ON CONFLICT (id) DO NOTHING", (uid, email))
        conn.execute("INSERT INTO public.accounts (id,email,byo_key_ct,byo_key_hint) "
                     "VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET byo_key_ct = "
                     "excluded.byo_key_ct", (uid, email, b"SEALED-" + uid.encode(), "9999"))
        conn.execute("INSERT INTO public.understandings "
                     "(id,user_id,source_id,title,medium,payload) "
                     "VALUES (%s,%s,'s',%s,'text','{}'::jsonb) "
                     "ON CONFLICT DO NOTHING",
                     (f"u-{uid[:8]}", uid, f"{email} private doc"))
    yield conn
    conn.close()


def as_role(db, role: str, sub: str | None, sql: str, args=()):
    """Run one statement as `role`, with `sub` as the signed-in user."""
    with db.transaction(force_rollback=True) as _:
        cur = db.cursor()
        if sub:
            cur.execute("SELECT set_config('request.jwt.claims', %s, true)",
                        (json.dumps({"sub": sub, "role": role}),))
        cur.execute(f"SET LOCAL ROLE {role}")
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def denied(db, role, sub, sql, args=()):
    """True if the database refused. Anything else is a leak."""
    import psycopg
    try:
        as_role(db, role, sub, sql, args)
        return False
    except psycopg.errors.InsufficientPrivilege:
        return True


# ------------------------------------------------------------ reading ------

def test_a_signed_in_user_reads_their_own_rows(db):
    rows = as_role(db, "authenticated", ALICE,
                   "SELECT id FROM public.understandings")
    assert [r[0] for r in rows] == [f"u-{ALICE[:8]}"]


def test_a_signed_in_user_cannot_read_another_account(db):
    # Not "permission denied" -- the row simply is not there, which is what
    # makes RLS safe to leave on: nothing about Bob is disclosed, not even
    # that he exists.
    rows = as_role(db, "authenticated", ALICE,
                   "SELECT id FROM public.understandings WHERE user_id = %s", (BOB,))
    assert rows == []


def test_naming_the_row_directly_does_not_help(db):
    rows = as_role(db, "authenticated", ALICE,
                   "SELECT * FROM public.understandings WHERE id = %s", (f"u-{BOB[:8]}",))
    assert rows == []


def test_a_session_with_no_user_reads_nothing(db):
    assert as_role(db, "authenticated", None,
                   "SELECT id FROM public.understandings") == []


def test_anon_cannot_reach_the_corpus_at_all(db):
    assert denied(db, "anon", None, "SELECT id FROM public.understandings")


# ------------------------------------------------------------ writing ------

@pytest.mark.parametrize("table", [
    "collections", "understandings", "nodes", "renders",
    "deliverables", "review_state", "jobs", "usage_events", "usage_current",
])
def test_a_signed_in_user_cannot_write_anywhere(db, table):
    # Every write goes through the API, which checks a quota and denormalizes
    # nodes out of the payload. A direct client write would skip all of it.
    assert denied(db, "authenticated", ALICE,
                  f"DELETE FROM public.{table}"), f"{table} is writable"


def test_a_user_cannot_make_themselves_an_admin(db):
    # This is the bug an earlier draft shipped: a column-level REVOKE is
    # silently ignored while a table-level UPDATE grant stands.
    assert denied(db, "authenticated", ALICE,
                  "UPDATE public.accounts SET is_admin = true WHERE id = %s", (ALICE,))
    assert as_role(db, "authenticated", ALICE,
                   "SELECT is_admin FROM public.accounts WHERE id = %s", (ALICE,))[0][0] is False


def test_a_user_cannot_raise_their_own_budget(db):
    assert denied(db, "authenticated", ALICE,
                  "UPDATE public.accounts SET token_budget = 999999999 WHERE id = %s",
                  (ALICE,))


def test_a_user_cannot_zero_their_own_meter(db):
    assert denied(db, "authenticated", ALICE, "DELETE FROM public.usage_current")
    assert denied(db, "authenticated", ALICE, "DELETE FROM public.usage_events")


# ------------------------------------------------------------ secrets ------

def test_the_encrypted_key_is_not_readable_by_its_owner(db):
    # Not even Alice's own. The server holds the only path to it, because the
    # browser has no business decrypting anything.
    assert denied(db, "authenticated", ALICE,
                  "SELECT byo_key_ct FROM public.accounts WHERE id = %s", (ALICE,))


def test_select_star_on_accounts_is_refused_rather_than_trimmed(db):
    assert denied(db, "authenticated", ALICE, "SELECT * FROM public.accounts")


def test_an_account_sees_its_own_row_and_no_other(db):
    rows = as_role(db, "authenticated", ALICE,
                   "SELECT id, email, byo_key_hint FROM public.accounts")
    assert [str(r[0]) for r in rows] == [ALICE]


def test_anon_cannot_see_that_anyone_has_an_account(db):
    assert denied(db, "anon", None, "SELECT id FROM public.accounts")


# ------------------------------------------------------------ storage ------

def test_uploads_are_walled_off_by_path(db):
    db.execute("INSERT INTO storage.objects (bucket_id,name,owner) "
               "VALUES ('sources', %s, %s) ON CONFLICT DO NOTHING",
               (f"{BOB}/bobs-thesis.pdf", BOB))
    rows = as_role(db, "authenticated", ALICE,
                   "SELECT name FROM storage.objects WHERE bucket_id = 'sources'")
    assert all(BOB not in r[0] for r in rows), "another account's uploads are listable"


def test_a_user_cannot_write_into_someone_elses_prefix(db):
    import psycopg
    with pytest.raises((psycopg.errors.InsufficientPrivilege,
                        psycopg.errors.RaiseException, psycopg.Error)):
        as_role(db, "authenticated", ALICE,
                "INSERT INTO storage.objects (bucket_id,name) VALUES ('sources', %s)",
                (f"{BOB}/planted.pdf",))


# ------------------------------------------------- the fence is really on ---

def test_every_corpus_table_has_rls_forced(db):
    # `enable` can be bypassed by the table owner; `force` cannot. A migration
    # run as the owner should not be able to read across accounts either.
    rows = db.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY relname").fetchall()
    off = [r[0] for r in rows if not (r[1] and r[2])]
    assert not off, f"RLS not forced on: {off}"


def test_the_new_user_trigger_creates_an_account(db):
    uid = str(uuid.uuid4())
    db.execute("INSERT INTO auth.users (id,email) VALUES (%s,%s)", (uid, f"{uid[:8]}@t.test"))
    got = db.execute("SELECT email FROM public.accounts WHERE id = %s", (uid,)).fetchone()
    assert got, "signing up did not create an accounts row"


# ------------------------------------- everything reachable, not just tables ---

def test_no_relation_of_any_kind_is_granted_to_anon(db):
    """The check that was missing.

    `usage_monthly` is a materialized view. Materialized views cannot carry
    RLS, and the first lockdown enumerated tables, so it kept Supabase's
    permissive default grant -- anon could read every account's token usage
    over the REST API. Verified exploitable on a live project.

    The old verification missed it too, and that is the more useful half:
    information_schema.role_table_grants does not report materialized views at
    all, so the query meant to prove the lockdown was blind to the object that
    leaked. This one reads pg_class, which knows about every relation kind.
    """
    rows = db.execute(
        "SELECT c.relname, c.relkind, array_to_string(c.relacl, ' ') AS acl "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','f')"
    ).fetchall()
    assert rows, "no relations found -- the migrations did not run"
    leaked = [(r[0], r[1], r[2]) for r in rows if r[2] and "anon=" in r[2]]
    assert not leaked, f"anon holds privileges on: {leaked}"


def test_only_the_expected_relations_are_readable_by_a_signed_in_user(db):
    allowed = {"collections", "understandings", "nodes", "renders",
               "deliverables", "review_state", "jobs",
               "usage_events", "usage_current", "accounts"}
    rows = db.execute(
        "SELECT c.relname, array_to_string(c.relacl, ' ') AS acl "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','f')"
    ).fetchall()
    granted = {r[0] for r in rows if r[1] and "authenticated=" in r[1]}
    assert not granted - allowed, f"unexpectedly readable: {granted - allowed}"


def test_the_usage_rollup_is_not_reachable_by_a_signed_in_user_either(db):
    # It aggregates across accounts and has no RLS to filter it. Only the
    # server, which holds the service role, has any business reading it.
    assert denied(db, "authenticated", ALICE,
                  "SELECT * FROM public.usage_monthly")
    assert denied(db, "anon", None, "SELECT * FROM public.usage_monthly")
