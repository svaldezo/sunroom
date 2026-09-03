# prism

**Ingest anything. Understand it once. Deliver it in the format someone asked for.**

A working skeleton of the medium-conversion platform. The bet: conversion is a
commodity, and the durable, provenanced, structured representation of a corpus
is the moat.

---

## The architecture in one line

```
N parsers  →  ONE intermediate representation  →  M renderers  →  FORMATS
                                                   (machinery)    (product)
```

Nobody wants a narration script; they want a podcast. A **format** is a named
deliverable composed from renderers plus a structure of its own:

| Format | The job |
|---|---|
| **Brief** | Understand it once, start to finish |
| **Activity** | Practice it until you can do it — drills, ordering, sorting, application, role-play simulation |
| **Podcast** | Learn it while you are doing something else |
| **Study guide** | Come back to it when you need the detail |
| **Visual explainer** | See how the parts fit together — figures and graphics, not video |
| **Tutor** | Ask it anything, and check every answer |
| **Lesson** | Teach it to someone else |

Adding a format is a recipe, not new machinery.

**Media → text is a first-class direction.** A recorded lecture becomes a
readable, citable brief where every claim links to the second it was said.

Not N×M converters. Adding an output medium is **one class**, and it immediately
works for every input type already supported. That is what makes "comprehensive
from day one, with regular updates in the backlog" affordable instead of a
treadmill.

```
  .pdf .md .txt .srt/.vtt          ┌──────────────────┐         narration  (production)
  .html  URL  audio/video   ──────▶│  Understanding   │──────▶  diagram    (production)
                                   │  nodes + edges   │         glossary   (production)
        ingest/                    │  + provenance    │         retrieval  (production)
                                   └──────────────────┘         summary    (production)
                                            │                   slides     (beta)
                                            ▼                   comic      (experimental)
                                     Repository (SQLite)
                                     corpus · search · review state
```

## Quickstart

```bash
pip install -e '.[all]'             # engine + server + Postgres driver

export ANTHROPIC_API_KEY=...        # omit to run the offline mock extractor

prism serve                         # web UI at http://127.0.0.1:8000
prism formats                       # the deliverables you can ask for
prism make und_xxxx brief           # a named deliverable
prism make und_xxxx activity -o act.json
prism ask und_xxxx "what is reciprocity?"
prism media                         # what goes in, what comes out
prism add lecture.pdf -c ANTH266    # ingest + understand
prism ls
prism show und_xxxx                 # inspect the IR
prism render und_xxxx diagram       # deterministic Mermaid, zero model calls
prism render und_xxxx narration -o script.md
prism render und_xxxx retrieval     # flashcards with citations
prism check und_xxxx                # fidelity report on every stored render
prism search "reciprocity" -c ANTH266
```

Python:

```python
from prism import Prism

p = Prism()
doc = p.add("podcast.srt", collection="ANTH266")
result = p.render(doc.id, "diagram")
print(p.verify(doc.id, result).summary())
```

On a laptop that is the whole thing: SQLite in `~/.prism`, no accounts, the job
worker running in the same process. **DEPLOY.md** covers the deployed shape --
Supabase for Postgres, auth and file storage, Vercel for the app -- which is the
same code with a different store, real accounts, and a queue driven by cron.

---

## Deployed shape

```
      browser ──▶ Vercel function ──▶ Supabase Postgres   (corpus, jobs, usage)
         │            (FastAPI)   ├──▶ Supabase Auth      (magic links)
         │                        └──▶ Supabase Storage   (uploaded files)
         │
    magic link                     Vercel cron ──▶ worker ──┐
    from Supabase                                    ▲      │ chains itself
                                                     └──────┘ while work remains
```

Three constraints shaped it, and each has a module that answers it:

**A function gets seconds; an ingest takes minutes.** So ingestion is a job that
carries its own progress and is advanced a slice at a time by whoever picks it
up (`prism/jobs/`). A worker claims a job on a *lease*: if the platform freezes
it mid-slice -- which is routine, not exceptional -- the lease expires and the
next worker resumes from the last checkpoint. Nothing has to notice the death.

**Many people share one database.** So a corpus store is constructed *for one
account* and there is no method that takes a user id (`prism/store/base.py`).
Reading someone else's document is not a bug you can write; it is a method that
does not exist. Row Level Security is the second lock, tested by impersonating
the `anon` and `authenticated` roles against real Postgres.

**"Paste a link" hands a stranger the server's network position.** So outbound
requests validate every resolved address, follow redirects by hand, and connect
to the IP they validated while verifying the certificate against the hostname
(`prism/net/outbound.py`) -- which is what closes DNS rebinding, as opposed to
merely checking and hoping.

---

## Attribution

Every output unit resolves to a citation with three parts: a human locator, the
exact verbatim quote, and **a link that opens the original at that place**.

| source | anchor | standard |
|---|---|---|
| PDF | `file:///…/chapter.pdf#page=3` | PDF Open Parameters |
| Web page | `https://…#:~:text=prefix-,exact,-suffix` | W3C Text Fragments |
| Audio / video | `https://…#t=372.5` | Media Fragments URI |
| Text / Markdown | `file:///…/notes.md#L42` | line anchor |

A flashcard generated from page 3 of a PDF says `p. 3 · § 4.2 The Kula Ring`
and opens the PDF at page 3. A claim taken from a podcast opens the audio at
the second it was said.

Attribution runs in both directions:

- **forward** — output unit → IR nodes → source spans → the original
- **reverse** — select any passage in the Reader and see every node extracted
  from it and every output made from it, across all media

Export the citation set as Markdown footnotes, BibTeX, CSL-JSON, or an Anki
deck whose cards carry live source links.

## The interface

`prism serve` opens Sunroom — a six-view workspace (`/` to search, `n` to add a
source, `Esc` to close):

- **Library** — collections, full-text search, and adding a source by file,
  drag-and-drop, link, or pasted text
- **Read** — the source with every cited passage highlighted; click one to see
  what was extracted from it and everything generated from it
- **Make** — pick a format; every part carries footnote markers and clickable
  source chips, with the fidelity summary above
- **Ask** — ask questions; answers cite the passage, or decline
- **Practice** — spaced repetition over the corpus; each card shows the passage
  it came from and links to the original
- **Checks** — fidelity across everything made, including anything that has gone
  stale against a source since changed

Fonts and Mermaid are vendored locally, so the interface works with no network
at all. Light and dark are both first-class, and the whole thing collapses to a
single column with an off-canvas rail below 48rem.

### Accounts

Sign-in is a magic link, through Supabase Auth. There is no password anywhere in
the product. Each account has a monthly token budget, sees an estimate of what a
source will cost before committing to it, and can paste its own Anthropic key to
lift the limit entirely -- the key is encrypted at rest and never sent back to
the page.

A deployment with no Supabase configured is single-user: no sign-in screen,
everything belongs to one account. That is the local case, and the server
refuses to start that way with `SUNROOM_ENV=production`, because the failure
mode is every visitor sharing one library.

### Brand

The interface is dressed in the Sunroom identity: Fraunces for display, Karla
for text, IBM Plex Mono for locators; a warm paper ground with a single gold
accent; and the "rise" mark — a sun coming up past a sill. `sunroom-assets/`
holds the masters and the two scripts that regenerate every asset.

## Evaluating

```bash
pytest -q                         # unit, integration, tenancy, security
python3 tools/smoke.py            # boots the app cold, uses it over HTTP
python3 tools/e2e.py              # the real interface, in a real browser
python3 tools/loadtest.py         # concurrency, and tenancy under it
python3 tools/evaluate.py         # 8 sources x 7 renderers, quality probes
python3 tools/check_diagrams.py   # every diagram, parsed by real mermaid
```

The suite runs against both stores. Set `SUNROOM_TEST_DSN` and the Postgres half
runs too -- otherwise it skips, and SQLite-only means the production store is
never exercised. `supabase/local/apply.sh` sets up a local database with the
real migrations applied.

Writes `eval_report.md` and `eval_report.json`. It measures what the fidelity
checker cannot: answer leakage, circular definitions, disconnected diagrams,
orphaned nodes, citation coverage, label quality. See **FINDINGS.md** for what
round one turned up — 20 defects, all fixed, and what each one taught.

`check_diagrams.py` exists because the fidelity checker is structurally blind to
one class of defect: a mind map with two roots is perfectly grounded, covers the
source completely, and is still unrenderable. Only a real parser can tell you
that, so it asks one.

## The four ideas that matter

### 1. The IR is the product

`prism/models.py`. Nodes (concept, definition, claim, process, step, example,
quantity, event, question), edges (13 relation types), and **spans** that point
back to exact character ranges in the source.

Renderers never see raw text. They query structure: `u.processes()`,
`u.hierarchy()`, `u.definitions()`, `u.depictable()`, `u.testable()`.

### 2. Every claim is grounded, or it is dropped

The extractor asks the model for a **verbatim quote** and then locates it in the
source (exact → whitespace-normalized → fuzzy). A node whose quote cannot be
found is discarded, not guessed. Character offsets survive chunking, and
locators resolve to `p. 14`, `§ Methods`, or `@ 00:14:02`.

A flashcard generated from a podcast cites the timestamp it came from.

### 3. Fidelity is structural, not vibes

`prism/fidelity/`. Every `RenderUnit` must declare the node ids it derives from.
The checker verifies:

| check | catches |
|---|---|
| **grounding** | output units that cite nothing |
| **dangling nodes** | references to IR nodes that don't exist |
| **drift** | output whose vocabulary doesn't appear in the source it cites |
| **coverage** | how much salient content the rendering dropped |
| **staleness** | renders made against a version of the source that changed |

This matters more here than in most products: the user converted the medium
*because* they cannot yet evaluate the content, so they cannot catch the error
themselves.

### 4. Quality is tiered in public

`production` / `beta` / `experimental` is declared on every renderer and printed
by `prism media`. Comprehensive **coverage** day one; honest **quality** labels.
A confidently-wrong diagram teaches something false.

---

## What's real vs. what's stubbed

**Real and tested** (344 tests, both stores, no key needed)

- Ingest: markdown/text/PDF/HTML/URL/SRT/VTT with offsets and timestamps
- Chunking with absolute offsets, parallel extraction, quote grounding
- Cross-chunk merge (provenance is unioned, so merged nodes cite *more*)
- Structural linking: sections, step ordering, process binding, definitions, causal
- Two corpus stores -- SQLite and Postgres -- held to one conformance suite
- Per-account isolation, enforced in code and again by Row Level Security
- Supabase magic-link auth, verified against the classic JWT forgeries
- Resumable job queue: leases, checkpoints, crash recovery, attempt ceilings
- Token quotas, cost estimates, and encrypted bring-your-own keys
- SSRF-safe URL fetching with connection pinning against DNS rebinding
- All seven renderers, each emitting provenanced units
- Fidelity checker + negative tests proving it catches fabrication and drift
- Seven formats composed from the renderers, each fully cited
- Grounded tutor that declines what the source does not cover
- Web UI (FastAPI + vanilla JS) with click-to-trace provenance
- Evaluation harness scoring formats and components separately
- A smoke test that boots the app cold, and browser tests that drive the UI

**Deliberately stubbed**

- `MockClient` heuristic extraction — runs the whole pipeline with no key. Real
  quality needs `ANTHROPIC_API_KEY`, and this remains the largest untested
  thing in the system: the plumbing around the model is proven, the model's
  output is not.
- `TranscriptParser.transcribe` — hooks faster-whisper if installed
- `GlossaryRenderer.image_backend` — accepts any `prompt → url` callable
- Semantic entailment checking (drift is currently lexical — a floor, not a proof)
- Audio synthesis for the podcast (the script is TTS-ready; no voice yet)
- Image generation for explainer figures (briefs are produced, not rendered)
- LLM-assisted merge (`MERGE_SCHEMA` in `understand/prompts.py` is written, unused)

## Where the hard problems are

1. **IR quality.** Everything downstream inherits it. This is the research
   risk, and it is the one thing the test suite structurally cannot check --
   every test here runs against the offline extractor, so a green suite says
   the machinery works, not that the understanding is good.
2. **Semantic verification.** Lexical drift catches invention, not distortion.
3. **Cross-document merge.** Two sources contradicting each other in one corpus
   is the interesting case and isn't handled yet.
4. **Concreteness calibration.** Under-illustrating is boring; over-illustrating
   teaches falsehoods.

## Layout

```
prism/
  models.py           the IR — read this first
  formats/            the product surface (brief, activity, podcast, …)
  llm.py              provider abstraction, metering, retries, offline mock
  ingest/             one Parser per input medium; fetch.py decides what a
                      user-supplied string is allowed to become
  understand/         chunk → extract → merge → link → summarize
  store/              base.py is the contract; repository.py is SQLite,
                      pg.py is Postgres, and one suite holds both to it
  accounts/           who someone is to Sunroom: quota, usage, encrypted keys
  auth/               token verification and the FastAPI dependencies
  jobs/               the queue and the sliced, resumable runner
  net/                outbound requests made on a user's behalf
  render/             one Renderer per output medium
  fidelity/           grounding, drift, coverage, staleness
  storage.py          uploaded files, Supabase or local disk
  cli.py
  web/                FastAPI + a single-file front end
api/                  Vercel entry points (the app, and the worker)
supabase/migrations/  the schema and its RLS policies
supabase/local/       a shim so those migrations run on plain Postgres in CI
tools/                evaluate, check_diagrams, smoke, e2e, loadtest
examples/corpus/      8 test sources across 6 formats
tests/                344 tests: pipeline, stores, auth, jobs, API, security
```

Adding a medium: **input** → subclass `Parser`, add to `PARSERS`.
**output** → subclass `Renderer`, add to `RENDERERS`. Nothing else changes.
