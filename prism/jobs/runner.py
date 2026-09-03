"""
Running an ingest in slices.

The job moves through three phases, and only one of them is expensive:

    ingest ──▶ extract ──▶ assemble ──▶ done
              (sliced)

`ingest` parses the source and writes an Understanding with no nodes yet, so the
document appears in the library immediately, marked as still being read.
`extract` is the model work: it processes chunks until its time budget runs out,
persists what it got, and hands the job back. `assemble` deduplicates and links
across the whole node set, which is the one thing that cannot be sliced -- but
it is also cheap, so it does not need to be.

The time budget is the crux. A worker stops when the slice deadline approaches
*rather than* when the platform kills it, because a checkpoint written by us is
worth more than a partial one written by nobody.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..accounts import QuotaExceeded
from ..config import SETTINGS
from ..ingest import ingest as ingest_source
from ..ingest.base import only_read_from
from ..ingest.fetch import RefusedInput
from ..llm import LLMError, client_for
from ..net.outbound import UnsafeURL
from ..store import open_store
from ..understand.chunker import chunk_text
from ..understand.pipeline import assemble, extract_into, prepare
from .queue import Job, Jobs, jobs

PHASE_INGEST, PHASE_EXTRACT, PHASE_ASSEMBLE = "ingest", "extract", "assemble"

#: How many chunks to hand the extractor at once. Matches the concurrency, so
#: one batch is one round trip's worth of parallelism.
BATCH = max(1, SETTINGS.max_concurrency)


class Cancelled(Exception):
    """The owner cancelled this job while it was running."""


@dataclass
class SliceResult:
    job_id: str
    status: str
    phase: str
    done_steps: int
    total_steps: int
    understanding: Optional[str] = None
    error: Optional[str] = None
    seconds: float = 0.0

    @property
    def more_to_do(self) -> bool:
        return self.status in ("queued", "running")

    def to_dict(self) -> dict[str, Any]:
        return {"job": self.job_id, "status": self.status, "phase": self.phase,
                "done": self.done_steps, "total": self.total_steps,
                "understanding": self.understanding, "error": self.error,
                "seconds": round(self.seconds, 2),
                "more": self.more_to_do}


def resolve_input(spec: dict[str, Any]):
    """
    Turn a job's stored input into something `ingest()` can take.

    Kept separate so the security tests can hit it directly: this is the
    function that decides whether a user-supplied string is allowed to become a
    filesystem path or an outbound HTTP request.
    """
    from ..ingest.fetch import materialize

    return materialize(spec)


def run_slice(*, budget_seconds: Optional[float] = None,
              owner: Optional[str] = None,
              q: Optional[Jobs] = None,
              now: Callable[[], float] = time.monotonic) -> Optional[SliceResult]:
    """
    Claim one job and advance it for as long as the budget allows.

    Returns None when there was nothing to do. Otherwise the result says
    whether the job finished or wants another slice, which is what the worker
    endpoint uses to decide whether to chain.
    """
    q = q or jobs()
    budget = budget_seconds if budget_seconds is not None else SETTINGS.slice_seconds
    started = now()
    job = q.claim(owner=owner)
    if job is None:
        return None

    try:
        return _advance(job, q, budget, started, now)
    except Cancelled:
        return SliceResult(job.id, "cancelled", job.state.get("phase", ""),
                           job.done_steps, job.total_steps,
                           understanding=job.understanding,
                           seconds=now() - started)
    except QuotaExceeded as exc:
        # Not retryable: it will fail identically until the month rolls over or
        # the person adds a key, and retrying just burns attempts.
        q.fail(job, str(exc), retryable=False)
        return SliceResult(job.id, "failed", job.state.get("phase", ""),
                           job.done_steps, job.total_steps, error=str(exc),
                           seconds=now() - started)
    except RefusedInput as exc:
        # The input itself is wrong. It will be exactly as wrong next time, so
        # retrying only burns attempts and delays the message the user needs.
        q.fail(job, str(exc), retryable=False)
        return SliceResult(job.id, "failed", job.state.get("phase", ""),
                           job.done_steps, job.total_steps, error=str(exc),
                           seconds=now() - started)
    except UnsafeURL as exc:
        q.fail(job, str(exc), retryable=False)
        return SliceResult(job.id, "failed", job.state.get("phase", ""),
                           job.done_steps, job.total_steps, error=str(exc),
                           seconds=now() - started)
    except LLMError as exc:
        q.fail(job, f"model: {exc}", retryable=exc.retryable)
        return SliceResult(job.id, job.status, job.state.get("phase", ""),
                           job.done_steps, job.total_steps, error=str(exc),
                           seconds=now() - started)
    except Exception as exc:                       # noqa: BLE001
        q.fail(job, f"{type(exc).__name__}: {exc}")
        return SliceResult(job.id, job.status, job.state.get("phase", ""),
                           job.done_steps, job.total_steps, error=str(exc),
                           seconds=now() - started)


def _advance(job: Job, q: Jobs, budget: float, started: float,
             now: Callable[[], float]) -> SliceResult:
    store = open_store(job.user_id)
    client = client_for(job.user_id, job_id=job.id)
    state = dict(job.state)
    phase = state.get("phase", PHASE_INGEST)

    def check_cancel() -> None:
        if q.is_cancelled(job.id):
            raise Cancelled()

    def spent() -> float:
        return now() - started

    # -- phase 1: parse the source ----------------------------------------
    if phase == PHASE_INGEST:
        check_cancel()
        q.checkpoint(job, message="Reading your source…")
        target, medium, readable = resolve_input(job.input)
        # Whatever the input was, ingestion may now read from exactly one
        # directory -- the staging directory we just created -- or from nowhere
        # at all. A parser that resolves a path itself gets None instead.
        roots = (readable,) if readable else ()
        with only_read_from(*roots):
            ingested = ingest_source(target, title=job.input.get("title") or None,
                                     medium=medium)
        if not ingested.source.text.strip():
            q.fail(job, "there is no text in that source", retryable=False)
            return SliceResult(job.id, "failed", phase, 0, 0,
                               error="no extractable text", seconds=spent())

        collection = job.input.get("collection") or None
        u, chunks = prepare(ingested, collection=collection)

        # Same file, same account, already understood: hand back what exists
        # rather than paying to read it twice.
        existing = store.find_by_checksum(u.source.checksum)
        if existing is not None and not job.input.get("force"):
            q.finish(job, understanding=existing.id,
                     message="You already had this one")
            return SliceResult(job.id, "done", phase, 1, 1,
                               understanding=existing.id, seconds=spent())

        store.save(u)
        state = {"phase": PHASE_EXTRACT, "understanding": u.id,
                 "next_chunk": 0, "dropped": 0, "chunks": len(chunks)}
        q.checkpoint(job, state=state, total_steps=len(chunks) + 1,
                     done_steps=0, understanding=u.id,
                     message=f"Reading {len(chunks)} section(s)…")
        phase = PHASE_EXTRACT

    # -- phase 2: extract, in slices --------------------------------------
    if phase == PHASE_EXTRACT:
        u = store.get(state["understanding"])
        if u is None:
            q.fail(job, "the document disappeared mid-ingest", retryable=False)
            return SliceResult(job.id, "failed", phase, job.done_steps,
                               job.total_steps, error="document missing",
                               seconds=spent())

        chunks = chunk_text(u.source.text)
        total = len(chunks)
        nxt = int(state.get("next_chunk", 0))
        dropped = int(state.get("dropped", 0))

        did_a_batch = False
        while nxt < total:
            check_cancel()
            # Stop before the deadline, not on it: a batch takes a few seconds,
            # and starting one with two seconds left means losing that work.
            #
            # But always run at least one batch per claim. Without that, a
            # budget smaller than one batch produces a job that is re-queued
            # forever having done nothing, burns an attempt each time, and
            # eventually "fails" with no error to show -- a livelock that looks
            # like a hang. Overshooting one batch is strictly better.
            if did_a_batch and spent() > max(0.0, budget - _batch_estimate(state)):
                break
            batch = chunks[nxt:nxt + BATCH]
            t0 = now()
            dropped += len(extract_into(u, batch, client))
            nxt += len(batch)
            state.update({"next_chunk": nxt, "dropped": dropped,
                          "last_batch_seconds": round(now() - t0, 2)})
            store.save(u)
            did_a_batch = True
            q.checkpoint(job, state=state, done_steps=nxt, total_steps=total + 1,
                         message=f"Read {nxt} of {total} section(s)")

        if nxt < total:
            q.release(job, message=f"Read {nxt} of {total} section(s)")
            return SliceResult(job.id, "queued", PHASE_EXTRACT, nxt, total + 1,
                               understanding=u.id, seconds=spent())

        state["phase"] = PHASE_ASSEMBLE
        q.checkpoint(job, state=state, done_steps=total,
                     message="Working out how it fits together…")
        phase = PHASE_ASSEMBLE

    # -- phase 3: assemble -------------------------------------------------
    check_cancel()
    u = store.get(state["understanding"])
    if u is None:
        q.fail(job, "the document disappeared mid-ingest", retryable=False)
        return SliceResult(job.id, "failed", phase, job.done_steps,
                           job.total_steps, error="document missing",
                           seconds=spent())

    u = assemble(u, client, dropped=int(state.get("dropped", 0)))
    store.save(u)
    q.finish(job, understanding=u.id,
             message=f"Ready — {len(u.nodes)} passages")
    return SliceResult(job.id, "done", PHASE_ASSEMBLE, job.total_steps,
                       job.total_steps, understanding=u.id, seconds=spent())


def _batch_estimate(state: dict[str, Any]) -> float:
    """
    How long to leave in the budget for one more batch.

    Measured from the last batch when there is one, because a 40-page PDF and a
    three-line note do not take the same time per chunk, and a fixed guess is
    wrong for one of them. Floors and ceilings keep a single freak batch from
    making the estimate useless.
    """
    last = float(state.get("last_batch_seconds") or 0.0)
    return min(30.0, max(6.0, last * 1.35))


def drain(*, max_slices: int = 100, budget_seconds: Optional[float] = None,
          q: Optional[Jobs] = None) -> list[SliceResult]:
    """
    Run the queue until it is empty. The long-lived worker's main loop, and the
    way the tests exercise a multi-slice job end to end.
    """
    q = q or jobs()
    out: list[SliceResult] = []
    for _ in range(max_slices):
        result = run_slice(budget_seconds=budget_seconds, q=q)
        if result is None:
            break
        out.append(result)
    return out
