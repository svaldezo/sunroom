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
