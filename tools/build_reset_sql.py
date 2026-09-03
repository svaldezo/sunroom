"""Generate supabase/RESET_AND_APPLY.sql from the migrations, so it cannot drift."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "supabase" / "RESET_AND_APPLY.sql"

HEAD = """\
-- =====================================================================
--  Sunroom — reset this project and install the schema.
--
--  GENERATED FILE. Edit supabase/migrations/*.sql and re-run
--  `python tools/build_reset_sql.py` instead of editing this.
--
--  Paste the whole thing into the Supabase SQL editor and press Run.
--  It is one transaction: if any part fails, none of it happened.
--
--  THIS DELETES EVERYTHING IN THE `public` SCHEMA of this project.
--  It leaves auth.users and storage objects alone -- see the two
--  commented lines below if you want those gone too.
-- =====================================================================

begin;

-- The trigger hangs off auth.users, which dropping `public` does not reach.
drop trigger if exists on_auth_user_created on auth.users;
drop function if exists public.handle_new_user() cascade;

drop schema if exists public cascade;
create schema public;

-- Restore what a fresh Supabase project has. Skip the grants and PostgREST
-- answers "the schema must be one of the following" to every request.
alter schema public owner to pg_database_owner;
comment on schema public is 'standard public schema';
grant usage on schema public to postgres, anon, authenticated, service_role;
grant all   on schema public to postgres, service_role;

-- Dropping the schema also drops the default privileges attached to it, and
-- those are per-schema-OID -- so a reset project would quietly stop behaving
-- like a fresh one, and a table added by hand later would be invisible to
-- PostgREST for no discoverable reason.
--
-- Putting them back also makes the lockdown below mean something. Supabase's
-- real defaults are permissive: every table created in `public` is granted to
-- anon and authenticated automatically. 0002_rls.sql then takes that away. If
-- the reset skipped this, the tables would never have been granted in the
-- first place and the revokes would be revoking nothing.
alter default privileges in schema public grant all on tables    to postgres, anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to postgres, anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to postgres, anon, authenticated, service_role;

-- Uncomment to also wipe uploaded files, or every account that has ever
-- signed in. Neither is needed to install the schema.
-- delete from storage.objects;
-- delete from auth.users;

"""

TAIL = """

commit;

-- =====================================================================
--  Check it took. Every row must say rls = true.
-- =====================================================================
select tablename, rowsecurity as rls
from pg_tables where schemaname = 'public'
order by tablename;

--  Nobody signed in may write directly; anon may not appear at all.
select table_name, grantee,
       string_agg(distinct privilege_type, ',' order by privilege_type) as privileges
from information_schema.role_table_grants
where table_schema = 'public' and grantee in ('anon', 'authenticated')
group by 1, 2 order by 1, 2;

--  The encrypted-key column must NOT be in this list.
select column_name
from information_schema.column_privileges
where table_schema = 'public' and table_name = 'accounts'
  and grantee = 'authenticated' and privilege_type = 'SELECT'
order by column_name;

--  The uploads bucket exists and is private.
select id, public from storage.buckets where id = 'sources';
"""


def section(path: Path) -> str:
    bar = "-" * 69
    return (f"\n-- {bar}\n-- {path.name}\n-- {bar}\n\n"
            + path.read_text().rstrip() + "\n")


def main() -> None:
    body = "".join(section(p) for p in
                   sorted((ROOT / "supabase" / "migrations").glob("*.sql")))
    OUT.write_text(HEAD + body + TAIL)
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes, "
          f"{len(OUT.read_text().splitlines())} lines)")


if __name__ == "__main__":
    main()
