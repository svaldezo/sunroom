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


-- ---------------------------------------------------------------------
-- 0001_core.sql
-- ---------------------------------------------------------------------

-- Sunroom core schema.
--
-- Every table that holds corpus data carries user_id and is protected by RLS.
-- The application also scopes every query by user_id in code (see
-- prism/store/pg.py) -- RLS is the second lock, not the only one, because a
-- bug in one is unlikely to be a bug in both.

-- Supabase keeps extensions in `extensions`, not `public`, because anything in
-- public is exposed through the REST API's schema and pg_trgm brings ~30
-- functions with EXECUTE granted to PUBLIC. Naming no schema puts them in
-- public on a project where they are not already installed.
create schema if not exists extensions;
create extension if not exists "pgcrypto" with schema extensions;
create extension if not exists "pg_trgm" with schema extensions;

-- So `gin_trgm_ops` below resolves wherever pg_trgm actually lives. Not SET
-- LOCAL: outside a transaction that is a warning and a no-op, and this file is
-- applied both standalone (supabase db push) and inside the wrapping
-- transaction of RESET_AND_APPLY.sql -- which is exactly how the standalone
-- path broke while the pasted one kept working.
set search_path = public, extensions;

-- ---------------------------------------------------------------- accounts --

-- Mirrors auth.users. Supabase owns identity; this row owns everything about
-- the person that is Sunroom's business (plan, quota, their own API key).
create table if not exists public.accounts (
  id            uuid primary key references auth.users(id) on delete cascade,
  email         text,
  created_at    timestamptz not null default now(),
  -- monthly model budget in input+output tokens; null means "use the default"
  token_budget  bigint,
  -- an encrypted Anthropic key: when present the account spends its own money
  -- and the budget does not apply
  byo_key_ct    bytea,
  byo_key_hint  text,                       -- last 4 chars, for the settings UI
  is_admin      boolean not null default false,
  meta          jsonb not null default '{}'::jsonb
);

create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.accounts (id, email) values (new.id, new.email)
  on conflict (id) do nothing;
  return new;
end $$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ------------------------------------------------------------- collections --

create table if not exists public.collections (
  user_id     uuid not null references public.accounts(id) on delete cascade,
  name        text not null,
  kind        text not null default 'collection',
  created_at  timestamptz not null default now(),
  meta        jsonb not null default '{}'::jsonb,
  primary key (user_id, name)
);

-- ---------------------------------------------------------- understandings --

create table if not exists public.understandings (
  id          text not null,
  user_id     uuid not null references public.accounts(id) on delete cascade,
  source_id   text not null,
  title       text not null,
  medium      text not null,
  uri         text,
  checksum    text,
  collection  text,
  summary     text not null default '',
  payload     jsonb not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  primary key (user_id, id),
  foreign key (user_id, collection)
    references public.collections(user_id, name) on delete set null
);
create index if not exists idx_und_user_updated
  on public.understandings (user_id, updated_at desc);
create index if not exists idx_und_user_collection
  on public.understandings (user_id, collection);
-- Deduplication is per account: two people uploading the same PDF each get
-- their own copy, because one of them deleting it must not affect the other.
create index if not exists idx_und_user_checksum
  on public.understandings (user_id, checksum);

-- ------------------------------------------------------------------ nodes --

-- Denormalized out of the payload so the corpus is queryable across documents.
create table if not exists public.nodes (
  id            text not null,
  user_id       uuid not null references public.accounts(id) on delete cascade,
  understanding text not null,
  kind          text not null,
  label         text not null,
  body          text not null,
  salience      double precision,
  difficulty    double precision,
  concreteness  double precision,
  confidence    double precision,
  -- Postgres has no FTS5 external-content table, so the search vector is a
  -- stored generated column: always in step with the row, no rebuild step.
  fts           tsvector generated always as (
                  setweight(to_tsvector('english', coalesce(label, '')), 'A') ||
                  setweight(to_tsvector('english', coalesce(body, '')),  'B')
                ) stored,
  primary key (user_id, id),
  foreign key (user_id, understanding)
    references public.understandings(user_id, id) on delete cascade
);
create index if not exists idx_nodes_user_und  on public.nodes (user_id, understanding);
create index if not exists idx_nodes_user_kind on public.nodes (user_id, kind);
create index if not exists idx_nodes_fts       on public.nodes using gin (fts);
create index if not exists idx_nodes_label_trgm
  on public.nodes using gin (label gin_trgm_ops);

-- ---------------------------------------------------- renders/deliverables --

create table if not exists public.renders (
  id              text not null,
  user_id         uuid not null references public.accounts(id) on delete cascade,
  understanding   text not null,
  renderer        text not null,
  tier            text not null,
  format          text not null,
  payload         jsonb not null,
  source_checksum text,
  created_at      timestamptz not null default now(),
  primary key (user_id, id),
  foreign key (user_id, understanding)
    references public.understandings(user_id, id) on delete cascade
);
create index if not exists idx_renders_latest
  on public.renders (user_id, understanding, renderer, created_at desc);

create table if not exists public.deliverables (
  id              text not null,
  user_id         uuid not null references public.accounts(id) on delete cascade,
  understanding   text not null,
  format          text not null,
  tier            text not null,
  payload         jsonb not null,
  source_checksum text,
  created_at      timestamptz not null default now(),
  primary key (user_id, id),
  foreign key (user_id, understanding)
    references public.understandings(user_id, id) on delete cascade
);
create index if not exists idx_deliv_latest
  on public.deliverables (user_id, understanding, format, created_at desc);

-- ----------------------------------------------------------------- review --

create table if not exists public.review_state (
  user_id     uuid not null references public.accounts(id) on delete cascade,
  node_id     text not null,
  collection  text,
  ease        double precision not null default 2.5,
  interval    integer not null default 0,
  reps        integer not null default 0,
  lapses      integer not null default 0,
  due_at      timestamptz,
  primary key (user_id, node_id)
);
create index if not exists idx_review_due on public.review_state (user_id, due_at);

-- ------------------------------------------------------------------- jobs --

-- Ingestion is long. A serverless invocation is short. So a job records its
-- own progress and is advanced a slice at a time by whatever worker picks it
-- up next; see prism/jobs/.
create table if not exists public.jobs (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references public.accounts(id) on delete cascade,
  kind          text not null default 'ingest',
  status        text not null default 'queued',
    -- queued | running | done | failed | cancelled
  understanding text,
  title         text not null default '',
  -- what to ingest: a storage object, a URL, or pasted text
  input         jsonb not null default '{}'::jsonb,
  -- accumulating partial result between slices
  state         jsonb not null default '{}'::jsonb,
  total_steps   integer not null default 0,
  done_steps    integer not null default 0,
  message       text not null default '',
  error         text,
  attempts      integer not null default 0,
  -- a claim that expires, so a worker that dies mid-slice does not wedge a job
  lease_until   timestamptz,
  lease_by      text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  finished_at   timestamptz
);
create index if not exists idx_jobs_user on public.jobs (user_id, created_at desc);
-- The claim query: runnable jobs, oldest first.
create index if not exists idx_jobs_claimable
  on public.jobs (status, lease_until) where status in ('queued', 'running');

-- ------------------------------------------------------------------ usage --

-- Append-only. Every model call lands here, so a bill can always be explained.
create table if not exists public.usage_events (
  id             bigserial primary key,
  user_id        uuid not null references public.accounts(id) on delete cascade,
  at             timestamptz not null default now(),
  kind           text not null,               -- extract | summarize | ask | ...
  model          text not null default '',
  input_tokens   bigint not null default 0,
  output_tokens  bigint not null default 0,
  -- true when the account paid with its own key, so it does not count to quota
  byo            boolean not null default false,
  job_id         uuid,
  meta           jsonb not null default '{}'::jsonb
);
create index if not exists idx_usage_user_at on public.usage_events (user_id, at desc);

-- The quota check runs on every model call, so it must not scan the log.
create materialized view if not exists public.usage_monthly as
  select user_id,
         date_trunc('month', at) as month,
         sum(input_tokens + output_tokens) filter (where not byo) as billable_tokens,
         sum(input_tokens + output_tokens) as total_tokens,
         count(*) as calls
  from public.usage_events
  group by 1, 2;
create unique index if not exists idx_usage_monthly on public.usage_monthly (user_id, month);

-- Live counter maintained by trigger; the materialized view is for reporting.
create table if not exists public.usage_current (
  user_id          uuid not null references public.accounts(id) on delete cascade,
  month            date not null,
  billable_tokens  bigint not null default 0,
  total_tokens     bigint not null default 0,
  calls            bigint not null default 0,
  primary key (user_id, month)
);

create or replace function public.bump_usage()
returns trigger language plpgsql as $$
begin
  insert into public.usage_current (user_id, month, billable_tokens, total_tokens, calls)
  values (new.user_id,
          date_trunc('month', new.at)::date,
          case when new.byo then 0 else new.input_tokens + new.output_tokens end,
          new.input_tokens + new.output_tokens,
          1)
  on conflict (user_id, month) do update set
    billable_tokens = public.usage_current.billable_tokens + excluded.billable_tokens,
    total_tokens    = public.usage_current.total_tokens + excluded.total_tokens,
    calls           = public.usage_current.calls + 1;
  return new;
end $$;

drop trigger if exists on_usage_event on public.usage_events;
create trigger on_usage_event
  after insert on public.usage_events
  for each row execute function public.bump_usage();

-- --------------------------------------------------------------- updated_at --

create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

drop trigger if exists touch_understandings on public.understandings;
create trigger touch_understandings before update on public.understandings
  for each row execute function public.touch_updated_at();

drop trigger if exists touch_jobs on public.jobs;
create trigger touch_jobs before update on public.jobs
  for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------
-- 0002_rls.sql
-- ---------------------------------------------------------------------

-- Row Level Security.
--
-- The application already scopes every query by user_id (prism/store/pg.py
-- makes it structurally impossible to build a query without one). These
-- policies are the second lock: if the anon or authenticated key ever reaches
-- these tables directly -- from the browser, from a misconfigured PostgREST
-- call, from a future feature written in a hurry -- the database still refuses
-- to hand over another account's rows.
--
-- The service role bypasses RLS by design; that is the role the API server
-- uses, which is exactly why the in-code scoping has to be the primary lock.

-- Enabling RLS and FORCING it are two different things, and the difference is
-- not academic: `enable` is bypassed by the table's owner, so a psql session on
-- the pooler, a migration, or a query typed into the dashboard reads every
-- account's rows. `force` closes that.
--
-- One list drives both. An earlier version of this file kept two lists and they
-- drifted -- `collections` was enabled but never forced, and nothing noticed
-- until the policies were executed as a real role (tests/test_rls.py).
do $$
declare t text;
begin
  foreach t in array array['accounts','collections','understandings','nodes',
                           'renders','deliverables','review_state','jobs',
                           'usage_events','usage_current']
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format('alter table public.%I force  row level security', t);
  end loop;
end $$;

do $$
declare t text;
begin
  foreach t in array array['collections','understandings','nodes','renders',
                           'deliverables','review_state','jobs']
  loop
    execute format('drop policy if exists own_rows on public.%I', t);
    execute format($f$
      create policy own_rows on public.%I
        for all
        using (user_id = (select auth.uid()))
        with check (user_id = (select auth.uid()))
    $f$, t);
  end loop;
end $$;

-- Writes do not come from the browser. Every write in this application goes
-- through the API, which checks a quota, deduplicates by checksum, and
-- denormalizes nodes out of the payload -- a direct client write would skip all
-- of it. So `authenticated` gets SELECT and nothing else, and the server (which
-- holds the service role) does the writing.
--
-- This has to be a table-level revoke. An earlier version of this file revoked
-- only the sensitive COLUMNS of `accounts` while a table-level UPDATE grant was
-- still in place, and Postgres quietly ignores column revokes in that case: the
-- security test caught a signed-in user setting their own `is_admin` to true.
-- Revoke across every relation KIND, not just tables. An earlier version of
-- this file enumerated the nine tables by name, and `usage_monthly` -- a
-- materialized view -- kept Supabase's default grant as a result. Materialized
-- views cannot carry RLS, so anon could read every account's token usage and
-- call counts over the REST API. It was verified exploitable on a live project.
--
-- r=table p=partitioned v=view m=materialized view f=foreign table. Listing
-- object kinds by hand is what caused the hole; let the catalogue list them.
do $$
declare r record;
begin
  for r in
    select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind in ('r','p','v','m','f')
  loop
    execute format('revoke all on public.%I from anon, authenticated', r.relname);
  end loop;
end $$;

-- Then grant back exactly what a signed-in browser may read. `usage_monthly` is
-- deliberately absent: an aggregate across accounts with no RLS is not
-- something to hand out, and the server reads it with the service role.
do $$
declare t text;
begin
  foreach t in array array['collections','understandings','nodes',
                           'renders','deliverables','review_state','jobs',
                           'usage_events','usage_current']
  loop
    execute format('grant select on public.%I to authenticated', t);
  end loop;
end $$;

-- Objects added later should not arrive pre-granted either. These defaults are
-- the mechanism that produced the leak above, and left alone they would produce
-- the next one on the next table somebody adds.
alter default privileges in schema public revoke all on tables    from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated;

-- `accounts` is granted column by column rather than table-wide, because the
-- encrypted key must never leave the server -- and a column-level REVOKE is
-- silently ineffective while a table-level grant stands, which is the same trap
-- that let a user set their own is_admin in an earlier draft.
revoke all on public.accounts from anon, authenticated;
grant select (id, email, created_at, token_budget, byo_key_hint, is_admin, meta)
  on public.accounts to authenticated;

-- An account may read its own row. It may not write one at all.
drop policy if exists own_account on public.accounts;
create policy own_account on public.accounts
  for select using (id = (select auth.uid()));

-- Usage is readable by its owner and written only by the server, so a user
-- cannot zero their own meter.
drop policy if exists own_usage on public.usage_events;
create policy own_usage on public.usage_events
  for select using (user_id = (select auth.uid()));

drop policy if exists own_usage_current on public.usage_current;
create policy own_usage_current on public.usage_current
  for select using (user_id = (select auth.uid()));

-- Nothing at all reaches these without a session.
revoke all on public.usage_events, public.usage_current, public.accounts from anon;

-- ------------------------------------------------------------------ storage --

-- Source files live under a per-account prefix: sources/<uid>/<object>.
-- The policies below are what stop a signed-in user from listing or fetching
-- somebody else's uploads by guessing a path.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('sources', 'sources', false, 104857600, null)
on conflict (id) do update set
  public = false,
  file_size_limit = excluded.file_size_limit;

do $$
begin
  execute 'drop policy if exists sources_own_read on storage.objects';
  execute 'drop policy if exists sources_own_write on storage.objects';
  execute 'drop policy if exists sources_own_delete on storage.objects';
exception when insufficient_privilege then
  raise notice 'skipping storage policies (insufficient privilege outside Supabase)';
end $$;

do $$
begin
  execute $p$
    create policy sources_own_read on storage.objects
      for select to authenticated
      using (bucket_id = 'sources'
             and (storage.foldername(name))[1] = (select auth.uid())::text)
  $p$;
  execute $p$
    create policy sources_own_write on storage.objects
      for insert to authenticated
      with check (bucket_id = 'sources'
                  and (storage.foldername(name))[1] = (select auth.uid())::text)
  $p$;
  execute $p$
    create policy sources_own_delete on storage.objects
      for delete to authenticated
      using (bucket_id = 'sources'
             and (storage.foldername(name))[1] = (select auth.uid())::text)
  $p$;
exception when others then
  raise notice 'skipping storage policies: %', sqlerrm;
end $$;


-- ---------------------------------------------------------------- functions --

-- Trigger functions are reachable at /rest/v1/rpc/<name>, because PUBLIC holds
-- EXECUTE on new functions by default. Calling one outside a trigger errors
-- out, so this is hardening rather than a fix -- but a SECURITY DEFINER
-- function strangers can invoke is not worth leaving out on the argument that
-- today's body happens to be harmless.
revoke all on function public.handle_new_user() from public, anon, authenticated;

-- A mutable search_path lets anyone who can create objects choose which
-- `usage_current` these write to.
alter function public.bump_usage()       set search_path = public, pg_catalog;
alter function public.touch_updated_at() set search_path = public, pg_catalog;

-- Supabase installs this event trigger on some projects; it is theirs, not
-- ours, but it arrives with the same PUBLIC execute grant.
do $$
begin
  execute 'revoke all on function public.rls_auto_enable() from public, anon, authenticated';
exception when undefined_function then null;
end $$;


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
