"""
HTTP API over the engine.

Two design choices are worth stating, because everything else follows from
them.

**Every render response carries the resolved source spans for each output
unit.** The interface's job is to make provenance visible -- click a flashcard,
see the sentence it came from highlighted in the original -- and that only works
if the trace ships with the output rather than requiring a second round trip per
unit.

**No route can reach a corpus without an identity.** Routes take a `store`
dependency, and the only way to build one is from a verified principal. A route
that forgets to authenticate does not get an unscoped store; it fails to start.
That is deliberate: a cross-tenant read is the one bug here that cannot be
walked back after it happens.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..accounts import (
    QuotaExceeded,
    accounts,
    estimate_ingest,
    looks_like_anthropic_key,
)
from ..accounts import check as check_quota
from ..accounts import keys as keyvault
from ..auth import Principal
from ..auth.deps import current_principal, current_store, require_worker
from ..cite import citation_for, to_anki_tsv, to_bibtex, to_csl_json, to_markdown
from ..config import SETTINGS
from ..fidelity import check, check_deliverable
from ..formats import ask as tutor_ask
from ..formats import catalog as format_catalog
from ..formats import get_format
from ..ingest import supported
from ..ingest.fetch import RefusedInput, classify
from ..jobs import jobs
from ..llm import LLMError, client_for
from ..models import RenderResult, Understanding
from ..net.outbound import UnsafeURL
from ..render import catalog, get_renderer
from ..storage import StorageError, check_upload, put, signed_upload
from ..store.base import NotFound

log = logging.getLogger("sunroom")
STATIC = Path(__file__).parent / "static"


def start_inline_worker():
    """
    Drain the queue from inside this process, when this process is long-lived.

    On Vercel the queue is driven by cron plus the chained worker calls, because
    a function has no background to run in -- a thread started there is killed
    the moment the response is sent. Anywhere with a real process (a container,
    `prism serve`, a laptop) that arrangement is silly: there is a perfectly
    good background right here, and requiring a separate cron for local
    development means the first thing anyone sees is a job that sits at
    "Queued" forever.
    """
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return lambda: None
    if os.environ.get("SUNROOM_INLINE_WORKER", "1") not in ("1", "true", "yes"):
        return lambda: None

    from ..jobs.runner import run_slice

    done = threading.Event()

    def loop() -> None:
        while not done.is_set():
            try:
                result = run_slice()
            except Exception as exc:              # noqa: BLE001
                log.warning("worker: %s", exc)
                result = None
            # Busy while there is work, patient when there is not.
            done.wait(0.2 if result is not None else 2.0)

    threading.Thread(target=loop, name="sunroom-worker", daemon=True).start()
    log.info("inline worker started")
    return done.set

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Refuse to serve a production deployment that is configured unsafely.

    Booting anyway would mean an open API, or a database that silently discards
    every write. Both are worse than a failed deploy, and a failed deploy is the
    only one of the three that anybody notices.
    """
    problems = SETTINGS.preflight()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        raise RuntimeError(
            "Refusing to start with an unsafe production configuration:\n  - "
            + "\n  - ".join(problems))
    stop = start_inline_worker()
    try:
        yield
    finally:
        stop()


app = FastAPI(title="Sunroom", version="1.0.0", docs_url=None, redoc_url=None,
              lifespan=lifespan)

if SETTINGS.allowed_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=SETTINGS.allowed_origins,
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# ------------------------------------------------------------ rate limiting --

_hits: dict[str, list[float]] = {}


def _caller(request: Request) -> str:
    """Who to count this request against.

    The account, when we can tell -- read out of the token without verifying
    it, which is all bucketing needs: forging a `sub` only changes which bucket
    you are throttled in, and the request still has to survive real
    authentication a moment later.

    Keying on the token itself is the tempting shortcut and it is wrong in both
    directions. Supabase mints a new JWT on every refresh, so a user's limit
    reset roughly hourly and on every tab focus; and two tokens can share a
    tail, which would let one account throttle another.
    """
    header = request.headers.get("authorization") or ""
    if header.startswith("Bearer "):
        parts = header[7:].split(".")
        if len(parts) == 3:
            try:
                pad = parts[1] + "=" * (-len(parts[1]) % 4)
                sub = json.loads(base64.urlsafe_b64decode(pad)).get("sub")
            except (ValueError, TypeError, json.JSONDecodeError):
                sub = None                 # malformed: fall through to the token
            if isinstance(sub, str) and sub:
                return f"u:{sub}"
        return "t:" + hashlib.sha256(header.encode()).hexdigest()[:32]
    return "ip:" + (request.client.host if request.client else "anon")


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """
    A crude per-caller cap.

    In-process, so on a serverless platform each instance counts separately and
    the real ceiling is higher than the number suggests. It is not a defence
    against a distributed attack and is not meant to be -- it is there so one
    stuck client cannot spend an account's month in a retry loop.
    """
    if SETTINGS.rate_limit_per_min <= 0 or not request.url.path.startswith("/api/"):
        return await call_next(request)

    ident = _caller(request)
    now = time.monotonic()
    window = _hits.setdefault(ident, [])
    window[:] = [t for t in window if now - t < 60.0]
    if len(window) >= SETTINGS.rate_limit_per_min:
        return JSONResponse(
            status_code=429,
            content={"detail": "You're going a bit fast. Try again in a moment."},
            headers={"Retry-After": "10"})
    window.append(now)
    if len(_hits) > 4096:               # keep the dict from growing forever
        for k in [k for k, v in _hits.items() if not v][:2048]:
            _hits.pop(k, None)
    return await call_next(request)


# --------------------------------------------------------- error translation --

@app.exception_handler(QuotaExceeded)
async def _quota(_r: Request, exc: QuotaExceeded):
    return JSONResponse(status_code=402,
                        content={"detail": str(exc), "code": "quota"})


@app.exception_handler(NotFound)
async def _notfound(_r: Request, exc: NotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(RefusedInput)
async def _refused(_r: Request, exc: RefusedInput):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(UnsafeURL)
async def _unsafe(_r: Request, exc: UnsafeURL):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(StorageError)
async def _storage(_r: Request, exc: StorageError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(LLMError)
async def _llm(_r: Request, exc: LLMError):
    # The upstream message can carry request ids and internal detail; log it,
    # and tell the user the one thing they can act on.
    log.warning("model error: %s", exc)
    return JSONResponse(
        status_code=503 if exc.retryable else 502,
        content={"detail": ("The model is unavailable right now. Try again in "
                            "a moment.") if exc.retryable
                 else "That request could not be completed."})


# ------------------------------------------------------------------ models --

class AddRequest(BaseModel):
    """What the Add sheet posts. `source` is text, a link, or an upload key."""
    source: str = ""
    kind: Optional[str] = None            # text | url | storage | path
    title: Optional[str] = None
    collection: Optional[str] = None
    filename: Optional[str] = None
    force: bool = False


class ReviewRequest(BaseModel):
    node_id: str
    collection: Optional[str] = None
    correct: bool


class AskRequest(BaseModel):
    question: str


class KeyRequest(BaseModel):
    api_key: str = Field(default="", max_length=400)


class SignRequest(BaseModel):
    filename: str
    size: int


class EstimateRequest(BaseModel):
    chars: int = 0


# ------------------------------------------------------------------ helpers --

def _load(store, doc_id: str) -> Understanding:
    u = store.get(doc_id)
    if not u:
        # Identical to the response for a document that belongs to someone
        # else. Distinguishing the two turns a list of ids into a census.
        raise HTTPException(404, "No such document.")
    return u


def _unit_payload(u: Understanding, result: RenderResult) -> list[dict[str, Any]]:
    out = []
    for unit in result.units:
        spans = u.spans_for(unit.derived_from)
        out.append({
            "id": unit.id, "kind": unit.kind, "content": unit.content,
            "meta": unit.meta,
            "nodes": [{"id": n.id, "label": n.label, "kind": n.kind.value}
                      for n in (u.node(i) for i in unit.derived_from) if n],
            "footnotes": unit.meta.get("footnotes", []),
            "spans": [
                {"id": s.id, "start": s.start, "end": s.end,
                 "locator": s.locator, "t_start": s.t_start,
                 "excerpt": s.excerpt(u.source, 400),
                 "citation": citation_for(u, s).to_dict()}
                for s in spans],
        })
    return out


def _part_payload(u: Understanding, d) -> list[dict[str, Any]]:
    out = []
    for part in d.parts:
        spans = u.spans_for(part.derived_from)
        out.append({
            "id": part.id, "role": part.role, "title": part.title,
            "body": part.body, "meta": part.meta, "asserts": part.asserts,
            "footnotes": part.footnotes,
            "nodes": [{"id": n.id, "label": n.label, "kind": n.kind.value}
                      for n in (u.node(i) for i in part.derived_from) if n],
            "spans": [{"id": s.id, "start": s.start, "end": s.end,
                       "locator": s.locator,
                       "citation": citation_for(u, s).to_dict()} for s in spans],
        })
    return out


# ------------------------------------------------------------------- meta --

@app.get("/api/health")
def health() -> dict[str, Any]:
    """Cheap enough for a load balancer; honest enough to be worth checking."""
    return {"ok": True, "env": SETTINGS.env, "store": SETTINGS.store,
            "multi_user": SETTINGS.multi_user, "provider": SETTINGS.provider}


@app.get("/api/config")
def public_config() -> dict[str, Any]:
    """What the browser needs before anyone has signed in."""
    return {"supabase_url": SETTINGS.supabase_url,
            "supabase_anon_key": SETTINGS.supabase_anon_key,
            "multi_user": SETTINGS.multi_user,
            "max_upload_mb": SETTINGS.max_upload_bytes // (1024 * 1024)}


@app.get("/api/media")
def media(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"input": supported(), "output": catalog(),
            "formats": format_catalog(), "provider": SETTINGS.provider}


@app.get("/api/formats")
def formats(principal: Principal = Depends(current_principal)
            ) -> list[dict[str, Any]]:
    return format_catalog()


# --------------------------------------------------------------------- me --

@app.get("/api/me")
def me(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    acc = accounts().ensure(principal.user_id, principal.email)
    use = accounts().usage(principal.user_id)
    return {"id": acc.id, "email": acc.email, "is_admin": acc.is_admin,
            "byo_key": bool(acc.has_byo_key), "byo_key_hint": acc.byo_key_hint,
            "usage": use.to_dict()}


@app.get("/api/usage")
def usage(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"current": accounts().usage(principal.user_id).to_dict(),
            "recent": accounts().recent(principal.user_id, limit=30)}


@app.put("/api/settings/api-key")
def set_api_key(req: KeyRequest,
                principal: Principal = Depends(current_principal)
                ) -> dict[str, Any]:
    key = (req.api_key or "").strip()
    if not looks_like_anthropic_key(key):
        raise HTTPException(
            400, "That does not look like an Anthropic key. They start with "
                 "sk-ant- and are about 100 characters long.")
    try:
        hint = accounts().set_api_key(principal.user_id, key)
    except keyvault.KeyError_ as exc:
        log.error("key vault: %s", exc)
        raise HTTPException(503, "Keys cannot be stored right now.") from None
    return {"saved": True, "hint": hint}


@app.delete("/api/settings/api-key")
def clear_api_key(principal: Principal = Depends(current_principal)
                  ) -> dict[str, bool]:
    accounts().clear_api_key(principal.user_id)
    return {"cleared": True}


# ---------------------------------------------------------------- library --

@app.get("/api/collections")
def collections(store=Depends(current_store)) -> list[dict[str, Any]]:
    # A collection is created as a side effect of adding a source to it, so an
    # empty one is a leftover from a deleted document rather than something the
    # person made on purpose. Don't clutter the rail with it.
    return [c for c in store.collections() if c.get("documents")]


@app.get("/api/documents")
def documents(collection: Optional[str] = None,
              store=Depends(current_store)) -> list[dict[str, Any]]:
    return store.list(collection)


@app.get("/api/documents/{doc_id}")
def document(doc_id: str, store=Depends(current_store)) -> dict[str, Any]:
    u = _load(store, doc_id)
    return {
        "id": u.id, "title": u.source.title, "medium": u.source.medium.value,
        "uri": u.source.uri, "collection": u.collection, "summary": u.summary,
        "text": u.source.text, "stats": u.stats(), "meta": u.meta,
        "sections": [
            {"id": s.id, "title": s.title, "level": s.level,
             "physical": s.physical,
             "start": s.span.start if s.span else 0,
             "end": s.span.end if s.span else 0,
             "locator": s.span.locator if s.span else None}
            for s in u.sections],
        "nodes": [
            {"id": n.id, "kind": n.kind.value, "label": n.label, "body": n.body,
             "salience": n.salience, "concreteness": n.concreteness,
             "difficulty": n.difficulty, "section": bool(n.meta.get("section")),
             "spans": [{"start": sp.start, "end": sp.end, "locator": sp.locator}
                       for sp in (u.span(i) for i in n.provenance) if sp]}
            for n in sorted(u.nodes, key=lambda n: -n.salience)],
        "edges": [{"source": e.source, "target": e.target,
                   "relation": e.relation.value} for e in u.edges],
        "renders": store.renders_for(u.id),
    }


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str, store=Depends(current_store)) -> dict[str, bool]:
    return {"deleted": store.delete(doc_id)}


@app.get("/api/search")
def search(q: str, collection: Optional[str] = None,
           store=Depends(current_store)) -> list[dict[str, Any]]:
    return store.search(q[:200], collection=collection)


# ------------------------------------------------------------- adding work --

@app.post("/api/estimate")
def estimate(req: EstimateRequest,
             principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """
    What this will cost, before anyone commits to it.

    Shown in the Add sheet so a 400-page PDF is a decision rather than a
    surprise. It is an estimate from length, and the interface says so.
    """
    est = estimate_ingest(max(0, int(req.chars)))
    use = accounts().usage(principal.user_id)
    return {"estimate": est.to_dict(), "usage": use.to_dict(),
            "affordable": use.byo or
            (use.billable_tokens + est.total_tokens) <= use.budget}


@app.post("/api/documents", status_code=202)
def add_document(req: AddRequest,
                 principal: Principal = Depends(current_principal),
                 store=Depends(current_store)) -> dict[str, Any]:
    """
    Queue an ingest and return immediately.

    This used to run the whole pipeline inline, which was fine on a laptop and
    impossible on a platform that stops a function after a minute. The client
    gets a job to poll; the worker does the reading.
    """
    kind = (req.kind or classify(req.source or "")).strip().lower()
    if kind == "path" and SETTINGS.multi_user:
        raise HTTPException(
            400, "This deployment does not read files by path. Upload the file "
                 "instead.")
    if not (req.source or "").strip():
        raise HTTPException(400, "Add a file, a link, or some text first.")

    # Refuse up front when the month's budget cannot cover it, rather than
    # halfway through and after spending most of it.
    if kind == "text":
        est = estimate_ingest(len(req.source))
        check_quota(principal.user_id, needed=est.total_tokens)
    else:
        check_quota(principal.user_id)

    spec = {"kind": kind, "value": req.source, "title": req.title,
            "collection": req.collection, "filename": req.filename,
            "force": bool(req.force), "user_id": principal.user_id}
    job = jobs().create(principal.user_id,
                        title=req.title or (req.filename or "New source"),
                        input=spec, message="Queued")
    return {"job": job.to_dict()}


@app.get("/api/jobs")
def list_jobs(active: bool = False,
              principal: Principal = Depends(current_principal)
              ) -> list[dict[str, Any]]:
    return [j.to_dict() for j in jobs().list(principal.user_id, active_only=active)]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, principal: Principal = Depends(current_principal)
            ) -> dict[str, Any]:
    job = jobs().get(principal.user_id, job_id)
    if not job:
        raise HTTPException(404, "No such job.")

    # Where nothing can run a worker on a schedule, the poll drives the queue.
    #
    # Vercel's Hobby plan allows one cron a day and rejects the deployment
    # outright if vercel.json asks for more, so an app relying on a per-minute
    # cron does not merely run slowly there -- it does not deploy, and if the
    # schedule is quietly relaxed to daily instead, every job sits at "Queued"
    # with nothing to explain why. The browser is already polling this endpoint
    # while a job runs, so the work rides along on traffic that exists anyway.
    #
    # This claims from the shared queue rather than this job specifically:
    # whatever is next is what needs doing, and the queue's leases already make
    # concurrent claims safe. Usage is billed to the job's own account, so
    # nobody pays for a slice their poll happened to drive.
    if SETTINGS.poll_nudge_seconds > 0 and job.status in ("queued", "running"):
        try:
            from ..jobs.runner import run_slice
            run_slice(budget_seconds=SETTINGS.poll_nudge_seconds)
        except Exception as exc:                      # noqa: BLE001
            # A poll that reports status must keep reporting status; the job
            # already survives a failed slice by expiring its lease.
            log.warning("poll nudge: %s", exc)
        job = jobs().get(principal.user_id, job_id) or job

    return job.to_dict()


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str, principal: Principal = Depends(current_principal)
               ) -> dict[str, bool]:
    return {"cancelled": jobs().cancel(principal.user_id, job_id)}


# ---------------------------------------------------------------- uploads --

@app.post("/api/uploads/sign")
def sign_upload(req: SignRequest,
                principal: Principal = Depends(current_principal)
                ) -> dict[str, Any]:
    """
    A ticket to PUT bytes straight to storage.

    The file never passes through this function: a 90 MB recording would exceed
    the request body limit, the memory budget and the time budget all at once.
    """
    check_upload(req.filename, req.size)
    return signed_upload(principal.user_id, req.filename, req.size).to_dict()


@app.put("/api/uploads/{key:path}")
async def local_upload(key: str, request: Request,
                       principal: Principal = Depends(current_principal)
                       ) -> dict[str, Any]:
    """
    The local-development upload target, used when there is no Supabase Storage
    to sign against. The prefix check is what stops one account writing into
    another's folder.
    """
    if not key.startswith(f"{principal.user_id}/"):
        raise HTTPException(403, "That upload path is not yours.")
    body = await request.body()
    if len(body) > SETTINGS.max_upload_bytes:
        raise HTTPException(413, "That file is too large.")
    put(key, body)
    return {"key": key, "bytes": len(body)}


# ------------------------------------------------------------------ making --

@app.post("/api/documents/{doc_id}/render/{renderer}")
def render(doc_id: str, renderer: str, options: Optional[dict[str, Any]] = None,
           principal: Principal = Depends(current_principal),
           store=Depends(current_store)) -> dict[str, Any]:
    u = _load(store, doc_id)
    try:
        r = get_renderer(renderer, client_for(principal.user_id))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None
    try:
        result = r.render(u, **(options or {}))
    except ValueError as exc:
        # the renderer refused content it cannot honestly represent
        raise HTTPException(409, str(exc)) from None

    store.save_render(result, source_checksum=u.source.checksum)
    report = check(u, result)
    return {
        "renderer": result.renderer, "tier": result.tier,
        "format": result.format, "artifact": result.artifact,
        "citations_count": len(result.meta.get("citations", [])),
        "citations": result.meta.get("citations", []),
        "units": _unit_payload(u, result),
        "fidelity": {
            "passed": report.passed, "grounding": report.grounding,
            "coverage": report.coverage, "overlap": report.mean_overlap,
            "stale": report.stale,
            "coverage_target": result.meta.get("coverage_target", 0.25),
            "summary": report.summary(),
            "findings": [{"severity": f.severity.value, "code": f.code,
                          "message": f.message, "unit_id": f.unit_id,
                          "detail": f.detail} for f in report.findings]},
    }


@app.post("/api/documents/{doc_id}/format/{fmt}")
def make_format(doc_id: str, fmt: str, options: Optional[dict[str, Any]] = None,
                principal: Principal = Depends(current_principal),
                store=Depends(current_store)) -> dict[str, Any]:
    u = _load(store, doc_id)
    try:
        formatter = get_format(fmt, client_for(principal.user_id))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None
    try:
        deliverable = formatter.make(u, **(options or {}))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None

    store.save_deliverable(deliverable, source_checksum=u.source.checksum)
    report = check_deliverable(u, deliverable)
    return {
        "id": deliverable.id, "format": deliverable.format,
        "label": formatter.label, "job": formatter.job,
        "tier": deliverable.tier, "title": deliverable.title,
        "subtitle": deliverable.subtitle, "artifact": deliverable.artifact,
        "artifact_format": deliverable.artifact_format,
        "uses": deliverable.meta.get("uses", []),
        "citations": deliverable.citations,
        "parts": _part_payload(u, deliverable),
        "fidelity": {
            "passed": report.passed, "grounding": report.grounding,
            "coverage": report.coverage, "overlap": report.mean_overlap,
            "stale": report.stale,
            "coverage_target": deliverable.meta.get("coverage_target", 0.25),
            "summary": report.summary(),
            "findings": [{"severity": f.severity.value, "code": f.code,
                          "message": f.message} for f in report.findings]},
    }


@app.post("/api/documents/{doc_id}/ask")
def ask_tutor(doc_id: str, req: AskRequest,
              principal: Principal = Depends(current_principal),
              store=Depends(current_store)) -> dict[str, Any]:
    """The live half of the Tutor format."""
    u = _load(store, doc_id)
    question = (req.question or "").strip()[:2000]
    if not question:
        raise HTTPException(400, "Ask something first.")
    return tutor_ask(u, question, client_for(principal.user_id)).to_dict()


# ------------------------------------------------------------- attribution --

@app.get("/api/documents/{doc_id}/spans")
def spans(doc_id: str, store=Depends(current_store)) -> list[dict[str, Any]]:
    """Every cited region of the source, with its resolvable anchor."""
    u = _load(store, doc_id)
    by_span: dict[str, list[str]] = {}
    for n in u.nodes:
        for sid in n.provenance:
            by_span.setdefault(sid, []).append(n.id)
    # Section spans cover a whole page or heading. They are correct provenance
    # but useless as highlights -- one of them swallows every sentence inside
    # it -- so the reader needs to tell them apart.
    section_spans = {s.span.id for s in u.sections if s.span}
    out = []
    for s in sorted(u.spans, key=lambda s: s.start):
        c = citation_for(u, s)
        out.append({**c.to_dict(), "nodes": by_span.get(s.id, []),
                    "node_count": len(by_span.get(s.id, [])),
                    "is_section": s.id in section_spans,
                    "length": s.end - s.start})
    return out


@app.get("/api/documents/{doc_id}/trace")
def trace(doc_id: str, offset: Optional[int] = None,
          span_id: Optional[str] = None,
          store=Depends(current_store)) -> dict[str, Any]:
    """
    Reverse attribution: given a place in the source, what was made from it?

    Forward tracing (output -> source) proves an output is honest. Reverse
    tracing (source -> outputs) is what a person actually asks when reading:
    "did anything I generated use this paragraph, and did it get it right?"
    """
    u = _load(store, doc_id)
    if span_id:
        hits = [s for s in u.spans if s.id == span_id]
    elif offset is not None:
        hits = [s for s in u.spans if s.start <= offset < s.end]
    else:
        raise HTTPException(400, "pass offset or span_id")
    if not hits:
        return {"spans": [], "nodes": [], "outputs": []}

    span_ids = {s.id for s in hits}
    nodes = [n for n in u.nodes if span_ids & set(n.provenance)]
    node_ids = {n.id for n in nodes}

    outputs, seen = [], set()
    # Formats first -- they are what the person made. Components are the
    # machinery underneath and are shown after.
    for deliverable in store.deliverable_payloads(u.id):
        for part in deliverable.parts:
            if not (node_ids & set(part.derived_from)):
                continue
            key = ("format:" + deliverable.format, part.role, part.title)
            if key in seen:
                continue
            seen.add(key)
            outputs.append({
                "renderer": deliverable.format, "kind_group": "format",
                "tier": deliverable.tier, "render_id": deliverable.id,
                "unit_id": part.id, "kind": part.role,
                "content": (part.title or part.body)[:400],
                "answer": part.meta.get("answer")})

    for result in store.latest_renders(u.id):
        for unit in result.units:
            if not (node_ids & set(unit.derived_from)):
                continue
            key = (result.renderer, unit.kind, unit.content)
            if key in seen:
                continue
            seen.add(key)
            outputs.append({
                "renderer": result.renderer, "kind_group": "component",
                "tier": result.tier, "render_id": result.id, "unit_id": unit.id,
                "kind": unit.kind, "content": unit.content[:400],
                "answer": unit.meta.get("answer")})
    return {
        "spans": [citation_for(u, s).to_dict() for s in hits],
        "nodes": [{"id": n.id, "kind": n.kind.value, "label": n.label,
                   "body": n.body, "salience": n.salience} for n in nodes],
        "outputs": outputs,
    }


@app.get("/api/documents/{doc_id}/citations")
def citations(doc_id: str, store=Depends(current_store)) -> list[dict[str, Any]]:
    u = _load(store, doc_id)
    return [citation_for(u, s).to_dict()
            for s in sorted(u.spans, key=lambda s: s.start)]


@app.get("/api/documents/{doc_id}/export/{fmt}")
def export(doc_id: str, fmt: str, renderer: Optional[str] = None,
           principal: Principal = Depends(current_principal),
           store=Depends(current_store)) -> dict[str, Any]:
    """Export the attribution set, or one rendering, in a portable format."""
    u = _load(store, doc_id)
    cites = [citation_for(u, s) for s in sorted(u.spans, key=lambda s: s.start)]

    if fmt == "bibtex":
        return {"filename": f"{doc_id}.bib", "mime": "text/plain",
                "content": to_bibtex(u, cites)}
    if fmt == "csl":
        return {"filename": f"{doc_id}.csl.json", "mime": "application/json",
                "content": to_csl_json(u, cites)}
    if fmt == "markdown":
        return {"filename": f"{doc_id}.md", "mime": "text/markdown",
                "content": f"# {u.source.title}\n\n{u.summary}\n\n"
                           f"## Sources\n\n{to_markdown(cites)}"}
    if fmt == "anki":
        result = store.latest_render(u.id, "retrieval")
        if not result:
            result = get_renderer("retrieval",
                                  client_for(principal.user_id)).render(u)
            store.save_render(result, source_checksum=u.source.checksum)
        rows = []
        for unit in result.units:
            sp = u.spans_for(unit.derived_from)
            rows.append((unit.content, str(unit.meta.get("answer", "")),
                         citation_for(u, sp[0]) if sp else None))
        return {"filename": f"{doc_id}.anki.tsv",
                "mime": "text/tab-separated-values",
                "content": to_anki_tsv(rows)}
    if fmt == "artifact" and renderer:
        result = store.latest_render(u.id, renderer)
        if not result:
            raise HTTPException(404, f"no stored {renderer} rendering")
        ext = {"mermaid": "mmd", "json": "json",
               "markdown": "md"}.get(result.format, "txt")
        return {"filename": f"{doc_id}.{renderer}.{ext}", "mime": "text/plain",
                "content": result.artifact}
    raise HTTPException(400, f"unknown export format: {fmt}")


@app.get("/api/audit")
def audit(store=Depends(current_store)) -> dict[str, Any]:
    """Fidelity across every stored deliverable and rendering in the corpus."""
    from ..formats.base import Deliverable

    problems: list[dict[str, Any]] = []
    clean: list[dict[str, Any]] = []
    ok = 0

    def entry(row, kind, name, report) -> dict[str, Any]:
        return {"render_id": row["id"], "renderer": name, "kind": kind,
                "tier": row["tier"], "document": row["title"],
                "understanding": row["understanding"],
                "collection": row["collection"], "created_at": row["created_at"],
                "passed": report.passed, "grounding": report.grounding,
                "coverage": report.coverage, "overlap": report.mean_overlap,
                "stale": bool(row["source_checksum"]
                              and row["source_checksum"] != row["checksum"]),
                "findings": [{"severity": f.severity.value, "code": f.code,
                              "message": f.message} for f in report.findings]}

    def sort(e: dict[str, Any]) -> None:
        nonlocal ok
        if e["passed"] and not e["findings"] and not e["stale"]:
            ok += 1
            clean.append(e)
        else:
            problems.append(e)

    deliverables = store.all_deliverables()
    for row in deliverables:
        u = store.get(row["understanding"])
        if not u:
            continue
        payload = row["payload"]
        d = (Deliverable.model_validate(payload) if isinstance(payload, dict)
             else Deliverable.model_validate_json(payload))
        sort(entry(row, "format", row["format"], check_deliverable(u, d)))

    rows = store.all_renders()
    for row in rows:
        u = store.get(row["understanding"])
        if not u:
            continue
        payload = row["payload"]
        r = (RenderResult.model_validate(payload) if isinstance(payload, dict)
             else RenderResult.model_validate_json(payload))
        sort(entry(row, "component", row["renderer"], check(u, r)))

    clean.sort(key=lambda e: e["created_at"] or "", reverse=True)
    return {"total": len(rows) + len(deliverables), "clean": ok,
            "problems": problems, "clean_items": clean}


@app.get("/api/stale")
def stale(store=Depends(current_store)) -> list[dict[str, Any]]:
    return store.stale_renders()


# ------------------------------------------------------------------ review --

@app.get("/api/review")
def review_due(collection: Optional[str] = None,
               principal: Principal = Depends(current_principal),
               store=Depends(current_store)) -> list[dict[str, Any]]:
    """
    Due cards, presented as the retrieval renderer wrote them.

    Reading label/body straight off the IR node looked equivalent and was not:
    a node's label is a prefix of its body, so every card showed its own
    answer. The renderer already solved that -- review has to go through it
    rather than around it.
    """
    rows = store.due(collection)
    cache: dict[str, tuple[Any, dict[str, Any]]] = {}
    out: list[dict[str, Any]] = []

    for row in rows:
        uid = row.get("understanding")
        if uid and uid not in cache:
            u = store.get(uid)
            index: dict[str, Any] = {}
            if u:
                result = store.latest_render(uid, "retrieval")
                if result is None:
                    try:
                        result = get_renderer(
                            "retrieval", client_for(principal.user_id)).render(u)
                        store.save_render(result,
                                          source_checksum=u.source.checksum)
                    except ValueError:
                        result = None
                for unit in (result.units if result else []):
                    for nid in unit.derived_from:
                        index.setdefault(nid, unit)
            cache[uid] = (u, index)

        u, index = cache.get(uid, (None, {}))
        unit = index.get(row["node_id"])
        card = {"node_id": row["node_id"], "due_at": row["due_at"],
                "lapses": row["lapses"], "reps": row.get("reps", 0),
                "kind": unit.kind if unit else row["kind"],
                "document": u.source.title if u else "", "understanding": uid}
        if unit:
            answer = unit.meta.get("answer")
            card["prompt"] = unit.content
            card["answer"] = (" → ".join(answer) if isinstance(answer, list)
                              else str(answer or ""))
            card["citations"] = ([citation_for(u, s).to_dict()
                                  for s in u.spans_for(unit.derived_from)[:2]]
                                 if u else [])
        else:
            # No card exists for this node. Ask rather than leak: show the
            # label as a prompt and keep the body hidden as the answer.
            card["prompt"] = f"What do you remember about: {row['label']}?"
            card["answer"] = row["body"]
            card["citations"] = []

        # One guarantee, applied to both paths: a card never shows its own
        # answer. The renderer already avoids it, and the fallback above avoids
        # it too -- right up until a node's label and body are the same string,
        # which happens for a heading that carries its own meaning. Checking the
        # finished card is the only version of this rule that cannot be
        # sidestepped by a future third path.
        if _gives_itself_away(card):
            continue
        out.append(card)
    return out


def _gives_itself_away(card: dict[str, Any]) -> bool:
    prompt = " ".join((card.get("prompt") or "").lower().split())
    answer = " ".join((card.get("answer") or "").lower().split())
    if not prompt or not answer:
        return True
    return answer in prompt


@app.post("/api/review")
def review(req: ReviewRequest, store=Depends(current_store)) -> dict[str, Any]:
    return store.schedule(req.node_id, req.collection, correct=req.correct)


# ------------------------------------------------------------------ worker --

@app.post("/api/worker/run")
def worker_run(max_slices: int = 1,
               _: bool = Depends(require_worker)) -> dict[str, Any]:
    """
    Advance the queue.

    Called by the cron schedule and, right after a job is created, by the app
    itself -- so nobody waits for the next tick to see their document start
    being read. Authenticated with a shared secret, because an open worker
    endpoint is a free way to make the server spend money.
    """
    from ..jobs.runner import run_slice

    done = []
    for _i in range(max(1, min(max_slices, 5))):
        result = run_slice()
        if result is None:
            break
        done.append(result.to_dict())
    return {"ran": len(done), "results": done, "pending": jobs().pending_count()}


# -------------------------------------------------------------------- app --

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/favicon.ico")
def favicon() -> Response:
    return FileResponse(STATIC / "favicon-32.png")


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")
