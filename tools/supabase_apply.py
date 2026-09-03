"""
Apply the Sunroom schema to a real Supabase project over HTTPS.

The usual route is `supabase db push`, which speaks Postgres on port 5432/6543.
Where those ports are blocked -- a locked-down CI runner, a sandbox, a corporate
network -- this does the same work through the Management API, which is plain
HTTPS on 443 and gets through.

It shows you what is on the database before it removes anything, resets the
public schema, applies the migrations in order, and then verifies the result by
querying the database rather than by trusting that the statements returned 200.

    export SUPABASE_ACCESS_TOKEN=sbp_...          # Account -> Access Tokens
    python tools/supabase_apply.py --project-ref abcdefghijklmnop --inspect
    python tools/supabase_apply.py --project-ref abcdefghijklmnop --apply

Nothing is dropped without --apply. --inspect is read-only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.supabase.com/v1"

# Tables 0001 creates, in the order a reader would expect them.
EXPECTED = [
    "accounts", "collections", "understandings", "nodes", "renders",
    "deliverables", "review_state", "jobs", "usage_events", "usage_current",
]

RESET = """
-- The trigger lives on auth.users, which the public-schema drop does not reach.
drop trigger if exists on_auth_user_created on auth.users;
drop function if exists public.handle_new_user() cascade;

drop schema if exists public cascade;
create schema public;

-- Supabase's own defaults for a fresh project. Without these, PostgREST loses
-- its way into the schema and every request answers "schema must be one of".
alter schema public owner to pg_database_owner;
comment on schema public is 'standard public schema';
grant usage on schema public to postgres, anon, authenticated, service_role;
grant all on schema public to postgres, service_role;
"""

VERIFY_TABLES = """
select c.relname as tablename,
       c.relrowsecurity as rowsecurity,
       c.relforcerowsecurity as forced
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r'
order by 1;
"""

VERIFY_POLICIES = """
select schemaname, tablename, policyname, cmd
from pg_policies
where schemaname in ('public','storage')
order by schemaname, tablename, policyname;
"""

VERIFY_GRANTS = """
select table_name, grantee, string_agg(distinct privilege_type, ',' order by privilege_type) as privs
from information_schema.role_table_grants
where table_schema = 'public' and grantee in ('anon','authenticated')
group by 1, 2 order by 1, 2;
"""

VERIFY_ACCOUNT_COLS = """
select column_name
from information_schema.column_privileges
where table_schema = 'public' and table_name = 'accounts'
  and grantee = 'authenticated' and privilege_type = 'SELECT'
order by column_name;
"""

INVENTORY = """
select
  (select coalesce(json_agg(json_build_object('name', tablename, 'rls', rowsecurity) order by tablename), '[]'::json)
     from pg_tables where schemaname = 'public') as tables,
  (select coalesce(json_agg(matviewname order by matviewname), '[]'::json)
     from pg_matviews where schemaname = 'public') as matviews,
  (select coalesce(json_agg(table_name order by table_name), '[]'::json)
     from information_schema.views where table_schema = 'public') as views,
  (select coalesce(json_agg(p.proname order by p.proname), '[]'::json)
     from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public') as functions,
  (select coalesce(json_agg(json_build_object('id', id, 'public', public) order by id), '[]'::json)
     from storage.buckets) as buckets,
  (select count(*) from storage.objects) as storage_objects,
  (select count(*) from auth.users) as auth_users
"""


def probe_rest(ref: str, anon_key: str) -> bool:
    """Ask PostgREST the questions an attacker would, with the public key.

    Everything above reads catalogue tables and concludes what the database
    *would* do. This is the same project answering over the wire, as an
    anonymous visitor holding the key that ships in the browser -- which is the
    only version of the question that has ever been wrong.
    """
    base = f"https://{ref}.supabase.co/rest/v1"
    hdr = {"apikey": anon_key, "Authorization": f"Bearer {anon_key}"}

    def get(path: str) -> tuple[int, str]:
        req = urllib.request.Request(base + path, headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode(errors="replace")[:300]
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")[:300]
        except urllib.error.URLError as e:
            return 0, str(e.reason)

    print("\n  probing the live API as an anonymous visitor")
    print("  " + "-" * 60)

    # Classifying each answer, rather than trusting a single canary request.
    #
    # A rejected key makes every table answer 401, and 401 reads as "refused"
    # -- so a typo would print a clean bill of health for a wide-open database.
    # The first version of this guarded against that by probing GET /rest/v1/,
    # which turns out to be reserved for service_role: a perfectly good anon key
    # is told "Invalid API key" there, and the guard failed a healthy project.
    #
    # The real signal is a Postgres SQLSTATE in the body. 42501 means the
    # request reached the database and the database refused it -- which is
    # simultaneously proof the key works and proof the table is locked. No
    # SQLSTATE and an "Invalid API key" message means the key never got that
    # far, and nothing else in this function means anything.
    reached_db, missing, leaked = False, [], []

    for table in EXPECTED:
        code, body = get(f"/{table}?select=*&limit=1")

        if '"code":"42501"' in body.replace(" ", "") or "permission denied" in body:
            reached_db = True
            print(f"  {'  ok  '} anon GET /{table:<15} -> refused (42501)")
            continue
        if code == 404 or "PGRST205" in body or "does not exist" in body:
            missing.append(table)
            continue
        if "Invalid API key" in body:
            print(f"   FAIL  the anon key was rejected at /{table}. Settings -> "
                  "API -> anon public.")
            return False
        if code == 200:
            reached_db = True
            empty = body.strip() in ("[]", "")
            # 200 [] is RLS filtering everything out, which is safe. 200 with
            # rows is the failure this whole file exists to catch.
            print(f"  {' FAIL ' if not empty else '  ok  '} anon GET /{table:<15}"
                  f" -> 200 {'[] (RLS returned nothing)' if empty else body}")
            if not empty:
                leaked.append(table)
            continue
        print(f"   FAIL  anon GET /{table} -> unexpected {code} {body}")
        leaked.append(table)

    if missing:
        print(f"\n   FAIL  {len(missing)} table(s) are not there at all: "
              f"{', '.join(missing)}")
        print("         the migrations have not been applied to this project.")
        return False
    if not reached_db:
        print("\n   FAIL  nothing proved the key reached the database, so none "
              "of the refusals above can be trusted.")
        return False

    # Materialized views cannot carry RLS, so a grant is the only thing between
    # this one and the whole world. It is the object that actually leaked.
    code, body = get("/usage_monthly?select=*&limit=1")
    bad = code == 200 and body.strip() not in ("[]", "")
    print(f"  {' FAIL ' if bad else '  ok  '} anon cannot read the usage rollup -> {code}")
    if bad:
        leaked.append("usage_monthly")

    code, body = get("/accounts?select=byo_key_ct&limit=1")
    bad = code == 200 and "byo_key_ct" in body
    print(f"  {' FAIL ' if bad else '  ok  '} the encrypted key column is unreadable -> {code}")
    if bad:
        leaked.append("accounts.byo_key_ct")

    req = urllib.request.Request(
        base + "/collections", method="POST",
        data=json.dumps({"name": "probe-should-never-exist"}).encode(),
        headers={**hdr, "Content-Type": "application/json",
                 "Prefer": "return=representation"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            wrote = r.status in (200, 201)
    except urllib.error.HTTPError as e:
        wrote, _ = False, e.read()
    except urllib.error.URLError:
        wrote = False
    print(f"  {' FAIL ' if wrote else '  ok  '} anon cannot write")
    if wrote:
        leaked.append("write")

    return not leaked


class Api:
    def __init__(self, token: str, ref: str):
        self.token, self.ref = token, ref

    def sql(self, query: str) -> list[dict]:
        body = json.dumps({"query": query}).encode()
        req = urllib.request.Request(
            f"{API}/projects/{self.ref}/database/query", data=body, method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read() or b"[]")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:2000]
            raise SystemExit(f"\n  SQL failed ({e.code}): {detail}\n") from None
        except urllib.error.URLError as e:
            raise SystemExit(f"\n  cannot reach {API}: {e.reason}\n") from None

    def project(self) -> dict:
        req = urllib.request.Request(
            f"{API}/projects/{self.ref}",
            headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SystemExit(
                    "  the access token was refused. It must be a personal "
                    "access token (starts with sbp_) from\n"
                    "  https://supabase.com/dashboard/account/tokens -- not the "
                    "anon or service-role key.") from None
            raise SystemExit(f"  cannot read project {self.ref}: {e.code}") from None


def show_inventory(api: Api) -> dict:
    inv = api.sql(INVENTORY)[0]
    print("\n  currently on the database")
    print("  " + "-" * 60)
    tables = inv["tables"]
    if tables:
        for t in tables:
            print(f"    table    {t['name']:<28} rls={'on' if t['rls'] else 'OFF'}")
    else:
        print("    (no tables in public)")
    for v in inv["views"]:
        print(f"    view     {v}")
    for m in inv["matviews"]:
        print(f"    matview  {m}")
    fns = inv["functions"]
    if fns:
        print(f"    functions {len(fns)}: {', '.join(fns[:8])}"
              + (" …" if len(fns) > 8 else ""))
    for b in inv["buckets"]:
        print(f"    bucket   {b['id']:<28} public={b['public']}")
    print(f"    storage objects: {inv['storage_objects']}")
    print(f"    auth users:      {inv['auth_users']}")
    print()
    return inv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-ref", required=True)
    ap.add_argument("--token", default=os.environ.get("SUPABASE_ACCESS_TOKEN", ""))
    ap.add_argument("--inspect", action="store_true", help="report only, change nothing")
    ap.add_argument("--apply", action="store_true", help="reset public and apply migrations")
    ap.add_argument("--purge-storage", action="store_true",
                    help="also delete every object and bucket in storage")
    ap.add_argument("--purge-users", action="store_true",
                    help="also delete every row in auth.users (all sign-ins)")
    ap.add_argument("--probe-only", action="store_true",
                    help="verify a project using only its public anon key")
    ap.add_argument("--anon-key", default=os.environ.get("SUPABASE_ANON_KEY", ""),
                    help="the project's public anon key; probes the live API with it")
    args = ap.parse_args()

    # The anon key is public -- it ships in the browser -- so this path needs
    # no secret at all. It is also the truest check there is: the real project,
    # over the wire, answering the questions a stranger would ask.
    if args.probe_only:
        if not args.anon_key:
            print("  --probe-only needs --anon-key (Settings -> API -> anon public).")
            return 2
        print(f"\n  project: {args.project_ref}")
        good = probe_rest(args.project_ref, args.anon_key)
        print("\n  the schema is live and closed." if good
              else "\n  SOMETHING IS WRONG — see above.")
        return 0 if good else 1

    if not args.token:
        print("  no access token. Set SUPABASE_ACCESS_TOKEN or pass --token.\n"
              "  Create one at https://supabase.com/dashboard/account/tokens")
        return 2
    if not (args.inspect or args.apply):
        print("  pass --inspect to look, --apply to change, or --probe-only "
              "to verify with just the anon key.")
        return 2

    api = Api(args.token, args.project_ref)
    proj = api.project()
    print(f"\n  project: {proj.get('name')} ({args.project_ref}) "
          f"region {proj.get('region')} status {proj.get('status')}")

    before = show_inventory(api)

    if args.inspect:
        if args.anon_key and not probe_rest(args.project_ref, args.anon_key):
            print("\n  the live API leaked rows to an anonymous caller.")
            return 1
        print("\n  --inspect: nothing was changed.")
        return 0

    # ---- remove -----------------------------------------------------------
    if args.purge_storage and before["buckets"]:
        print("  clearing storage …")
        api.sql("delete from storage.objects; delete from storage.buckets;")
    if args.purge_users and before["auth_users"]:
        print("  clearing auth.users …")
        api.sql("delete from auth.users;")

    print("  resetting the public schema …")
    api.sql(RESET)

    # ---- apply ------------------------------------------------------------
    for name in ("0001_core.sql", "0002_rls.sql"):
        path = ROOT / "supabase" / "migrations" / name
        print(f"  applying {name} ({path.stat().st_size} bytes) …")
        api.sql(path.read_text())

    # ---- verify, by asking the database -----------------------------------
    print("\n  verifying")
    print("  " + "-" * 60)
    ok = True

    rows = api.sql(VERIFY_TABLES)
    got = {r["tablename"]: (r["rowsecurity"], r["forced"]) for r in rows}
    for t in EXPECTED:
        present = t in got
        rls, forced = got.get(t, (False, False))
        # `enable` alone is bypassed by the table owner -- which is the role a
        # dashboard query or a psql session on the pooler runs as.
        good = present and rls is True and forced is True
        print(f"  {'  ok  ' if good else ' FAIL '} {t:<20} "
              f"{'present' if present else 'MISSING':<8} "
              f"rls={'on' if rls else 'OFF'} forced={'yes' if forced else 'NO'}")
        ok &= good
    extra = sorted(set(got) - set(EXPECTED))
    if extra:
        print(f"         also present: {', '.join(extra)}")

    pol = api.sql(VERIFY_POLICIES)
    pub = [p for p in pol if p["schemaname"] == "public"]
    sto = [p for p in pol if p["schemaname"] == "storage"]
    print(f"  {'  ok  ' if len(pub) >= 10 else ' FAIL '} policies on public: {len(pub)}")
    print(f"  {'  ok  ' if len(sto) >= 3 else ' FAIL '} policies on storage.objects: {len(sto)}")
    ok &= len(pub) >= 10 and len(sto) >= 3

    # The escalation bug: a table-level write grant makes column revokes a
    # no-op. Assert on the grants themselves, not on the SQL that set them.
    grants = api.sql(VERIFY_GRANTS)
    writes = [g for g in grants
              if any(p in g["privs"] for p in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"))]
    print(f"  {'  ok  ' if not writes else ' FAIL '} anon/authenticated hold no write grant"
          + (f" — {writes}" if writes else ""))
    ok &= not writes

    anon_reach = [g for g in grants if g["grantee"] == "anon"]
    print(f"  {'  ok  ' if not anon_reach else ' FAIL '} anon holds nothing"
          + (f" — {anon_reach}" if anon_reach else ""))
    ok &= not anon_reach

    cols = {r["column_name"] for r in api.sql(VERIFY_ACCOUNT_COLS)}
    leaked = cols & {"byo_key_ct"}
    print(f"  {'  ok  ' if not leaked else ' FAIL '} the encrypted key column is not readable"
          f" (authenticated sees: {', '.join(sorted(cols)) or 'nothing'})")
    ok &= not leaked

    buckets = api.sql("select id, public from storage.buckets where id = 'sources'")
    good_bucket = bool(buckets) and buckets[0]["public"] is False
    print(f"  {'  ok  ' if good_bucket else ' FAIL '} the sources bucket exists and is private")
    ok &= good_bucket

    trg = api.sql("select tgname from pg_trigger where tgname = 'on_auth_user_created'")
    print(f"  {'  ok  ' if trg else ' FAIL '} new sign-ups get an accounts row")
    ok &= bool(trg)

    if args.anon_key:
        ok &= probe_rest(args.project_ref, args.anon_key)
    else:
        print("\n  (pass --anon-key to also probe the live API as an anonymous visitor)")

    print()
    print("  the schema is live." if ok else "  SOMETHING IS WRONG — see the failures above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
