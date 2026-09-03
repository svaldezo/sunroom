"""
The job queue and the sliced ingest.

The queue exists for one reason: a serverless function is killed long before a
long ingest finishes. So the tests that matter are the ones that kill it. Each
of these simulates a worker dying, being frozen, racing another worker, or
running out of time mid-document, and checks that the user still ends up with a
complete, correct Understanding.
"""
from __future__ import annotations

import itertools

import pytest

from prism.accounts import accounts
from prism.jobs.queue import DONE, FAILED, QUEUED, RUNNING, Jobs
from prism.jobs.runner import drain, run_slice
from prism.store import open_store

# Long enough to need several chunks at the test chunk size.
PARA = ("Reciprocity is the mutual give and take between parties of roughly "
        "equal standing. Redistribution requires a center that collects and "
        "then disburses it. Market exchange sets prices through supply and "
        "demand alone.\n\n")
LONG_DOC = "# Exchange\n\n" + PARA * 40

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"


@pytest.fixture()
def q(isolated_home, monkeypatch):
    """A queue over a fresh SQLite file, with small chunks so jobs slice."""
    from prism.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "chunk_chars", 900)
    monkeypatch.setattr(SETTINGS, "chunk_overlap", 100)
    monkeypatch.setattr(SETTINGS, "max_concurrency", 2)
    accounts().ensure(ALICE, "alice@test")
    accounts().ensure(BOB, "bob@test")
    return Jobs(backend="sqlite")


def make_job(q, user=ALICE, text=LONG_DOC, **input_over):
    spec = {"kind": "text", "value": text, "title": "Exchange",
            "collection": "ANTH266"}
    spec.update(input_over)
    return q.create(user, title="Exchange", input=spec)


# -- claiming --------------------------------------------------------------

def test_claim_returns_one_job_and_marks_it_running(q):
    job = make_job(q)
    claimed = q.claim(owner="w1")
    assert claimed is not None and claimed.id == job.id
    assert claimed.status == RUNNING
    assert claimed.attempts == 1


def test_two_workers_cannot_claim_the_same_job(q):
    make_job(q)
    first = q.claim(owner="w1")
    second = q.claim(owner="w2")
    assert first is not None
    assert second is None, "a second worker took a job that was already claimed"


def test_claim_returns_none_when_the_queue_is_empty(q):
    assert q.claim(owner="w1") is None


def test_an_expired_lease_is_reclaimed(q):
    """
    The whole point of leases. A frozen serverless instance never releases
    anything, so a job it was holding has to become claimable on its own.
    """
    job = make_job(q)
    q.claim(owner="dead-worker", lease_seconds=-1)      # already expired
    again = q.claim(owner="live-worker")
    assert again is not None and again.id == job.id
    assert again.attempts == 2


def test_a_live_lease_is_respected(q):
    make_job(q)
    q.claim(owner="w1", lease_seconds=300)
    assert q.claim(owner="w2") is None


# -- slicing ---------------------------------------------------------------

def test_a_long_document_takes_several_slices(q):
    """
    With a budget that runs out mid-document, the job must come back queued
    with its progress saved, not fail and not start over.
    """
    job = make_job(q)
    # A clock that jumps 30s per reading: the first batch fits, the next does not.
    clock = itertools.count(0, 30).__next__
    first = run_slice(budget_seconds=20, q=q, now=clock)
    assert first is not None
    assert first.status == QUEUED, "a partial job should be re-queued"
    assert 0 < first.done_steps < first.total_steps

    after = q.get(ALICE, job.id)
    assert after is not None and after.status == QUEUED
    assert after.state["phase"] == "extract"
    assert after.state["next_chunk"] == first.done_steps


def test_slices_resume_and_the_result_is_complete(q):
    """A document read in pieces must equal one read in a single pass."""
    make_job(q)
    results = drain(q=q, budget_seconds=0.0001)          # one batch per slice
    assert results[-1].status == DONE
    assert len(results) > 2, "expected the job to need several slices"

    store = open_store(ALICE)
    u = store.get(results[-1].understanding)
    assert u is not None
    assert u.nodes and u.summary
    assert u.meta.get("validation") is not None, "assembly phase did not run"
    # Every node still points at real text in the source.
    for node in u.nodes[:20]:
        for span_id in node.provenance:
            span = next(s for s in u.spans if s.id == span_id)
            assert u.source.text[span.start:span.end].strip()


def test_a_worker_that_dies_mid_job_loses_only_its_own_slice(q):
    """
    Kill the worker after it has checkpointed once, then let another take over.
    The finished document must be whole.
    """
    job = make_job(q)
    clock = itertools.count(0, 30).__next__
    run_slice(budget_seconds=20, q=q, now=clock)         # one batch, then stop
    mid = q.get(ALICE, job.id)
    assert mid is not None and mid.status == QUEUED
    progress = mid.done_steps
    assert progress > 0

    results = drain(q=q)
    assert results[-1].status == DONE
    u = open_store(ALICE).get(results[-1].understanding)
    assert u is not None and len(u.nodes) > 0
    final = q.get(ALICE, job.id)
    assert final is not None and final.done_steps >= progress


def test_progress_only_moves_forward(q):
    make_job(q)
    seen = [r.done_steps for r in drain(q=q, budget_seconds=0.0001)]
    assert seen == sorted(seen), f"progress went backwards: {seen}"


# -- results ---------------------------------------------------------------

def test_a_finished_job_points_at_the_document(q):
    job = make_job(q)
    results = drain(q=q)
    done = q.get(ALICE, job.id)
    assert done is not None
    assert done.status == DONE
    assert done.understanding == results[-1].understanding
    assert done.progress == 1.0
    assert open_store(ALICE).get(done.understanding) is not None


def test_the_document_appears_before_it_is_finished(q):
    """
    A source shows up in the library as soon as it is parsed, so a long ingest
    is visible progress rather than an empty screen.
    """
    make_job(q)
    clock = itertools.count(0, 30).__next__
    run_slice(budget_seconds=20, q=q, now=clock)
    assert len(open_store(ALICE).list()) == 1


def test_an_empty_source_fails_clearly_and_is_not_retried(q):
    job = q.create(ALICE, title="empty",
                   input={"kind": "text", "value": "   "})
    result = run_slice(q=q)
    assert result is not None and result.status == "failed"
    after = q.get(ALICE, job.id)
    assert after is not None and after.status == FAILED
    assert "nothing in that text" in (after.error or "").lower()


def test_the_same_file_twice_is_not_read_twice(q):
    """Paying to understand a document you already have is a bug, not a policy."""
    make_job(q)
    first = drain(q=q)[-1]
    make_job(q)
    second = drain(q=q)[-1]
    assert second.understanding == first.understanding
    assert len(open_store(ALICE).list()) == 1


def test_two_accounts_uploading_the_same_file_get_their_own(q):
    make_job(q, user=ALICE)
    a = drain(q=q)[-1]
    make_job(q, user=BOB)
    b = drain(q=q)[-1]
    assert a.understanding != b.understanding
    assert len(open_store(ALICE).list()) == 1
    assert len(open_store(BOB).list()) == 1


# -- failure handling ------------------------------------------------------

def test_a_job_is_retried_then_given_up_on(q, monkeypatch):
    from prism.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "max_job_attempts", 3)

    def boom(*a, **k):
        raise RuntimeError("extractor exploded")

    monkeypatch.setattr("prism.jobs.runner.extract_into", boom)
    job = make_job(q)
    for _ in range(5):
        if run_slice(q=q) is None:
            break
    after = q.get(ALICE, job.id)
    assert after is not None
    assert after.status == FAILED
    assert after.attempts == 3, "should stop at the attempt ceiling"
    assert "exploded" in (after.error or "")


def test_quota_failure_is_not_retried(q, monkeypatch):
    """It would fail identically every time; retrying just burns attempts."""
    from prism.accounts import QuotaExceeded
    from prism.accounts.store import Usage

    def over(*a, **k):
        raise QuotaExceeded(Usage(billable_tokens=10, budget=5))

    monkeypatch.setattr("prism.jobs.runner.extract_into", over)
    job = make_job(q)
    run_slice(q=q)
    after = q.get(ALICE, job.id)
    assert after is not None
    assert after.status == FAILED and after.attempts == 1
    assert "limit" in (after.error or "").lower()


# -- ownership -------------------------------------------------------------

def test_a_job_is_only_visible_to_its_owner(q):
    job = make_job(q, user=ALICE)
    assert q.get(ALICE, job.id) is not None
    assert q.get(BOB, job.id) is None
    assert [j.id for j in q.list(BOB)] == []


def test_only_the_owner_can_cancel(q):
    job = make_job(q, user=ALICE)
    assert q.cancel(BOB, job.id) is False
    assert q.get(ALICE, job.id).status == QUEUED
    assert q.cancel(ALICE, job.id) is True
    assert q.get(ALICE, job.id).status == "cancelled"


def test_cancelling_stops_a_running_job(q):
    job = make_job(q)
    clock = itertools.count(0, 30).__next__
    run_slice(budget_seconds=20, q=q, now=clock)
    q.cancel(ALICE, job.id)
    result = run_slice(q=q)
    assert result is None or result.status == "cancelled"
    assert q.get(ALICE, job.id).status == "cancelled"


def test_a_cancelled_job_is_not_claimed_again(q):
    job = make_job(q)
    q.cancel(ALICE, job.id)
    assert q.claim(owner="w1") is None


# -- input handling --------------------------------------------------------

def test_a_path_input_is_refused_in_a_multi_user_deployment(q, monkeypatch, tmp_path):
    """
    The single most dangerous input. On a multi-user deployment a path is
    /etc/passwd, /proc/self/environ, or the application's own bundled source.
    """
    from prism.config import SETTINGS
    secret = tmp_path / "secret.md"
    secret.write_text("# private\n\nnot yours\n")
    monkeypatch.setattr(SETTINGS, "supabase_url", "https://x.supabase.co")
    monkeypatch.setattr(SETTINGS, "supabase_jwt_secret", "s" * 40)
    assert SETTINGS.multi_user

    job = q.create(ALICE, title="x", input={"kind": "path", "value": str(secret)})
    run_slice(q=q)
    after = q.get(ALICE, job.id)
    assert after is not None and after.status == FAILED
    assert "does not read files by path" in (after.error or "")


def test_an_unknown_input_kind_is_refused(q):
    job = q.create(ALICE, title="x", input={"kind": "exec", "value": "rm -rf /"})
    run_slice(q=q)
    assert q.get(ALICE, job.id).status == FAILED


def test_oversized_pasted_text_is_refused(q, monkeypatch):
    from prism.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "max_text_chars", 100)
    job = make_job(q, text="x" * 500)
    run_slice(q=q)
    after = q.get(ALICE, job.id)
    assert after is not None and after.status == FAILED
    assert "limit" in (after.error or "")
