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
