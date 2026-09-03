# Deploying Sunroom

Supabase for the database, auth and file storage; Vercel for the app. About
thirty minutes end to end, most of it waiting for DNS and a first build.

There is also a container path (`Dockerfile`, `docker-compose.yml`) that runs
the identical code with a long-lived worker. Use it if the serverless time
limits ever bite, or to run Sunroom on your own machine. The two are the same
application; only what drives the job queue differs.

---

## Before you start

You need: a Supabase project, a Vercel account, an Anthropic API key, and this
repository in a Git remote Vercel can see.

Generate the two secrets now — you will paste them twice:

```bash
openssl rand -base64 48   # SUNROOM_SECRET_KEY
openssl rand -base64 48   # SUNROOM_WORKER_SECRET
```

`SUNROOM_SECRET_KEY` encrypts users' own API keys at rest. **Keep it.** Change
it and every stored key becomes unreadable — the app says so plainly rather
than telling people their good key is invalid, but they will have to re-enter
it.

---

## 1. Supabase

### Apply the schema

Three ways in. They install the same thing; pick by what your network allows.

**a. The CLI** — the normal route, if ports 5432/6543 are open to you:

```bash
npm install -g supabase
supabase link --project-ref YOUR-PROJECT-REF
supabase db push
```

That runs `supabase/migrations/` in order: `0001_core.sql` creates the tables,
`0002_rls.sql` locks them down.

**b. Paste one file** — needs nothing but a browser. Open
`supabase/RESET_AND_APPLY.sql`, paste the whole thing into the SQL editor, run
it. It is one transaction, so a failure anywhere leaves the project untouched,
and it ends by printing the four verification queries below.

> It begins by dropping the `public` schema. On a project with anything in it,
> that is the point — but read the header first. It leaves `auth.users` and
> uploaded files alone unless you uncomment two lines.

The file is generated from the migrations by `python tools/build_reset_sql.py`;
CI fails if it has drifted from them, so it is never the stale copy.

**c. Over HTTPS** — for a network that blocks the Postgres ports but allows
443, which is most locked-down CI runners and sandboxes:

```bash
export SUPABASE_ACCESS_TOKEN=sbp_...     # dashboard → Account → Access Tokens
python tools/supabase_apply.py --project-ref YOUR-REF --inspect   # read-only
python tools/supabase_apply.py --project-ref YOUR-REF --apply \
    --anon-key "$SUPABASE_ANON_KEY"
```

Once the schema is on, this verifies it using **only the public anon key** —
no secret needed, and it is the truest check there is, because it is the real
project answering over the wire:

```bash
python tools/supabase_apply.py --project-ref YOUR-REF --probe-only \
    --anon-key "$SUPABASE_ANON_KEY"
```

It confirms every table exists (a missing one means the migration never ran),
that an anonymous caller is refused by all of them, that the encrypted-key
column is unreadable, and that a write is rejected. It checks the key is
*accepted* first — otherwise a typo would answer 401 everywhere and print a
clean bill of health for a database that is wide open.

`--inspect` prints everything currently on the database and changes nothing —
worth running first, because `--apply` drops the `public` schema. `--apply`
resets, installs, and then verifies by querying the database rather than by
trusting that the statements returned 200. With `--anon-key` it finishes by
calling the live REST API as an anonymous visitor and checking that every table
refuses it.

### Check it took

```sql
select tablename, rowsecurity from pg_tables
where schemaname = 'public' order by tablename;
```

Every row should say `rowsecurity = true`. If any says false, stop and
re-run `0002_rls.sql` — that column is the difference between a private
library and a shared one.

`rowsecurity = true` is necessary and not sufficient: *enabled* RLS is bypassed
by the table's owner, which is the role a dashboard query runs as. The
migration forces it as well. To prove the policies actually work rather than
merely exist, point the RLS suite at a database that has them:

```bash
SUNROOM_TEST_DSN=postgresql://... pytest -q tests/test_rls.py
```

Those 25 tests become `anon` and `authenticated` for real and check what comes
back — that one account cannot see another's rows, cannot write anywhere,
cannot make itself an admin, and cannot read the encrypted-key column even on
its own row.

### Turn on email sign-in

Authentication → Providers → **Email**: enable it, and turn **Confirm email**
on. Leave "Enable email provider" on and passwords off — Sunroom only uses
magic links.

Authentication → URL Configuration:

- **Site URL**: `https://your-app.vercel.app`
- **Redirect URLs**: add `https://your-app.vercel.app/**`

Getting the redirect URL wrong is the single most common failure here: the
email arrives, the link opens, and the user lands signed out with no
explanation. If that happens, this is why.

> Supabase's built-in email sender is rate-limited to a handful of messages an
> hour — fine for you and a few testers, not for a launch. Before you invite a
> group, put a real SMTP provider in Authentication → Emails → SMTP Settings.

### Storage

The `sources` bucket and its access policies are created by `0002_rls.sql`.
Confirm under Storage that it exists and is **not** public. Files are stored
under `sources/<user-id>/…`, and the policies key on that first path segment,
so the layout is a security boundary rather than tidiness.

### The connection string

Project Settings → Database → Connection string → **Transaction pooler**
(port 6543). Use the pooler, not the direct connection: serverless functions
open many short-lived clients and the direct port runs out of connections
during your first busy hour, which is the worst possible time to find out.

---

## 2. Vercel

Import the repository. Framework preset: **Other**. No build command, no
output directory, root directory blank.

`vercel.json` deliberately does *not* set two things, and both are traps:

- **`runtime`.** That key names a versioned community runtime
  (`@vercel/python@4.3.0`). Naming a built-in one — `"python3.12"` — fails the
  build with `Function Runtimes must have a valid version, for example
  now-php@1.0.0`, which does not mention Python or your file. Built-in runtimes
  are inferred from the extension; the version comes from `.python-version`.
- **`memory`.** It cannot be set from `vercel.json` on any plan. Hobby is fixed
  at 2 GB / 1 vCPU; Pro sets it in the dashboard. A number here is ignored with
  a build-time warning.

`tests/test_deploy_config.py` asserts both, plus the Hobby cron and duration
ceilings, so these are caught by `pytest` rather than by a failed deploy.

Add these environment variables (Settings → Environment Variables), all
environments:

| Variable | Where it comes from |
|---|---|
| `SUNROOM_ENV` | `production` |
| `SUPABASE_URL` | Supabase → Settings → API |
| `SUPABASE_ANON_KEY` | same page — this one is public |
| `SUPABASE_SERVICE_ROLE_KEY` | same page — **secret**, server only |
| `SUPABASE_JWT_SECRET` | same page → JWT Settings |
| `DATABASE_URL` | the transaction-pooler string above |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `PRISM_PROVIDER` | `anthropic` |
| `SUNROOM_SECRET_KEY` | the first secret you generated |
| `SUNROOM_WORKER_SECRET` | the second |

Optional but worth setting on day one:

| Variable | Effect |
|---|---|
| `SUNROOM_TOKEN_BUDGET` | monthly tokens per account (default 2,000,000) |
| `SUNROOM_ALLOWED_EMAIL_DOMAINS` | `umd.edu` — limits who can sign up at all |
| `SUNROOM_SLICE_SECONDS` | keep below the function's `maxDuration` |

### The worker, and the Hobby plan

Something has to drain the job queue. Vercel's Hobby plan allows **one cron a
day** — and it does not merely run your per-minute schedule slowly, it **refuses
the deployment** with "Upgrade to the Pro plan to unlock all Cron Jobs
features". So `vercel.json` ships a daily schedule, which deploys on either
plan. Daily is a backstop for stranded jobs, not what makes ingestion feel live.

Pick one to actually drive it:

- **Hobby, nothing else to run:** set `SUNROOM_POLL_NUDGE=8`. The browser polls
  job status while a job runs, and each poll advances the queue by up to 8
  seconds inside that request. No cron, no external service. Keep it well under
  the function's `maxDuration` (60s), since it is spent inside a request.
- **An uptime pinger** (any plan): `POST https://your-app.vercel.app/api/worker`
  with header `X-Worker-Secret: <SUNROOM_WORKER_SECRET>`, every minute. One
  cheap request, and the most predictable of the three.
- **Pro:** change the schedule in `vercel.json` back to `* * * * *`.

The container path needs none of this — the process outlives the request, so
the worker runs in a background thread.

### Check it came up

```bash
curl https://your-app.vercel.app/api/health
```

Expect `{"ok":true,"env":"production","store":"postgres","multi_user":true,…}`.

If the deployment failed to boot, read the function log. Sunroom refuses to
start on an unsafe production configuration and says exactly which values are
missing — a failed deploy is deliberate there, because the alternative is an
open API or a database that silently discards every write.

Then open the site, sign in with your own email, and add a source. The first
one takes a minute; the tray in the corner shows which section is being read.

---

## 3. First real use

**Make yourself an admin** (there is no route that can grant this — deliberately):

```sql
update public.accounts set is_admin = true where email = 'you@example.com';
```

**Give yourself no limit:**

```sql
update public.accounts set token_budget = null where email = 'you@example.com';
```
…or leave the limit on and add your own Anthropic key in the settings sheet,
which is what you will tell testers to do.

**Watch what it costs:**

```sql
select a.email,
       sum(u.input_tokens + u.output_tokens) as tokens,
       count(*) as calls
from usage_events u join accounts a on a.id = u.user_id
where u.at > now() - interval '7 days'
group by 1 order by 2 desc;
```

---

## Operating it

### If jobs sit queued

The worker is what moves them. In order of likelihood:

1. **Cron is not running.** Vercel Hobby fires crons once a day. Either move to
   Pro, or point any uptime pinger at
   `POST https://your-app.vercel.app/api/worker` with the header
   `X-Worker-Secret: <SUNROOM_WORKER_SECRET>` every minute. It is a single
   cheap request.
2. **`SUNROOM_WORKER_SECRET` differs** between what cron sends and what the app
   expects. The worker answers 401 and nothing tells you unless you look.
3. **Every job is failing.** `select id, error, attempts from jobs where status
   = 'failed' order by created_at desc limit 20;`

To push the queue along by hand:

```bash
curl -X POST https://your-app.vercel.app/api/worker \
  -H "X-Worker-Secret: $SUNROOM_WORKER_SECRET"
```

### If a job is stuck "running"

It is not, for long. A worker claims a job on a lease; if the function was
frozen or killed mid-slice the lease expires and the next worker resumes from
the last checkpoint. Nothing needs to notice the death. If you want to force it:

```sql
update jobs set status = 'queued', lease_until = null
where status = 'running' and lease_until < now();
```

### If someone's ingest costs more than expected

The Add sheet shows an estimate before anything runs, and the per-call quota
check stops a job the moment the meter actually hits the limit — an estimate
that came in low cannot overrun the budget by however much it was wrong by.
Every model call is a row in `usage_events`, so any bill can be reconstructed
from the calls that produced it.

### Backups

Supabase takes daily backups on paid plans. Confirm yours are on before you
have anything you would miss. The corpus is the product: a source can be
re-uploaded, but the understanding built from it cost money to produce.

---

## Verifying a change

```bash
pytest -q                                   # unit, integration, security
pytest -q --dsn                             # (see below for the Postgres half)
python tools/smoke.py                       # boots the app, uses it over HTTP
python tools/check_diagrams.py              # every diagram, real parser
python tools/evaluate.py                    # output quality across the corpus
python tools/loadtest.py --users 25         # concurrency, and tenancy under it
ruff check prism/ api/ tools/ tests/
```

The Postgres half of the suite needs a database. Locally:

```bash
./supabase/local/apply.sh postgresql://postgres@127.0.0.1:5432/sunroom_test
SUNROOM_TEST_DSN=postgresql://postgres@127.0.0.1:5432/sunroom_test pytest -q
```

`supabase/local/0000_shim.sql` stands in for the parts of Supabase the
migrations depend on (`auth.users`, `auth.uid()`, the storage tables), so the
real RLS policies can be applied to a plain Postgres and tested by actually
impersonating the `anon` and `authenticated` roles. Security rules that are
never executed are just comments.

CI runs all of it, including a container build that has to answer
`/api/health` before the workflow passes.

---

## The container path

```bash
docker compose up --build
```

Brings up Postgres with the real migrations applied and the app against it, on
:8000. The difference from serverless: the process outlives a request, so the
worker runs in a background thread and no cron is involved. Set
`SUNROOM_INLINE_WORKER=0` to turn that off and drive the queue externally.

For a single-user install on your own machine, no Supabase needed:

```bash
pip install -e ".[all]"
prism serve
```

Auth is off in that mode and everything belongs to one account — which is why
the app refuses to run that way with `SUNROOM_ENV=production`.

---

## What is not done

Worth knowing before you invite anyone:

- **Output quality under a real model is untested.** Everything in this
  repository has been exercised against the offline heuristic extractor. The
  plumbing is proven; whether the *understanding* is good is the first thing to
  check with a real key, on a document you know well.
- **No email beyond Supabase's default sender.** Fine for a handful of testers,
  rate-limited past that.
- **No billing.** Quotas and bring-your-own-key are the whole cost story.
- **The rate limit is per instance**, so the real ceiling is higher than the
  number suggests. It is there to stop one stuck client, not a distributed
  attack.
- **Anchors are not verified in every viewer.** Text fragments do not work in
  Firefox; PDF page anchors depend on the viewer.
