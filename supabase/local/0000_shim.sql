-- A minimal stand-in for the parts of Supabase the migrations depend on.
--
-- This file is NEVER applied to a Supabase project -- Supabase provides all of
-- it already. It exists so the real migrations, and in particular the RLS
-- policies, can be applied to a plain Postgres in CI and tested by actually
-- impersonating the anon and authenticated roles. Security rules that are
-- never executed are just comments.

create schema if not exists auth;
create schema if not exists storage;

do $$ begin create role anon nologin; exception when duplicate_object then null; end $$;
do $$ begin create role authenticated nologin; exception when duplicate_object then null; end $$;
do $$ begin create role service_role nologin bypassrls; exception when duplicate_object then null; end $$;

grant usage on schema public to anon, authenticated, service_role;

-- Supabase's real default privileges are permissive: every table created in
-- `public` is granted to anon and authenticated automatically. Reproducing that
-- here rather than a stricter guess is the whole point -- it is what makes the
-- lockdown in 0002_rls.sql a test of something instead of a formality.
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;

create table if not exists auth.users (
  id                uuid primary key default gen_random_uuid(),
  email             text unique,
  created_at        timestamptz not null default now()
);
grant usage on schema auth to anon, authenticated, service_role;
grant select on auth.users to service_role;

-- Supabase derives auth.uid() from the request JWT, which PostgREST puts in
-- the `request.jwt.claims` GUC. Same contract here, so a test can become a
-- user with: set local request.jwt.claims = '{"sub":"<uuid>"}'
create or replace function auth.uid() returns uuid
language sql stable as $$
  select nullif(
    coalesce(
      current_setting('request.jwt.claim.sub', true),
      (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
    ), '')::uuid
$$;
grant execute on function auth.uid() to anon, authenticated, service_role;

create table if not exists storage.buckets (
  id                 text primary key,
  name               text not null,
  public             boolean not null default false,
  file_size_limit    bigint,
  allowed_mime_types text[]
);

create table if not exists storage.objects (
  id         uuid primary key default gen_random_uuid(),
  bucket_id  text references storage.buckets(id),
  name       text not null,
  owner      uuid,
  created_at timestamptz not null default now(),
  metadata   jsonb not null default '{}'::jsonb
);
alter table storage.objects enable row level security;
grant usage on schema storage to anon, authenticated, service_role;
grant select, insert, delete on storage.objects to authenticated;
grant all on storage.objects, storage.buckets to service_role;

create or replace function storage.foldername(name text) returns text[]
language sql immutable as $$
  select (string_to_array(name, '/'))[1:greatest(array_length(string_to_array(name,'/'),1)-1, 0)]
$$;
grant execute on function storage.foldername(text) to anon, authenticated, service_role;
