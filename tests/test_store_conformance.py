"""
The same tests against both backends.

SQLite and Postgres are only interchangeable if something actually checks that
they behave the same, and the failure mode when they diverge is the worst kind:
green tests locally, wrong answers in production. Every test in this file is
parameterised over both stores and skips Postgres only when there is no database
to talk to.

Set SUNROOM_TEST_DSN to run the Postgres half:

    supabase/local/apply.sh
    SUNROOM_TEST_DSN=postgresql://postgres@127.0.0.1:5432/sunroom_test pytest
"""
from __future__ import annotations

import os
import uuid

import pytest

from prism.formats.base import Deliverable, Part
from prism.ingest import ingest
from prism.llm import MockClient
from prism.models import RenderResult, RenderUnit
from prism.store import Repository
from prism.store.base import NotFound
from prism.understand import understand

DSN = os.environ.get("SUNROOM_TEST_DSN", "")

DOC = """# Exchange and Obligation

Reciprocity is the mutual give and take between parties of roughly equal standing.
Redistribution requires a center that collects and then disburses.
Market exchange sets prices through the interaction of supply and demand.

## Forms

Generalized reciprocity involves giving without a specified expectation of return.
Balanced reciprocity involves an explicit expectation of equivalent return.
"""


def _pg(user_id: str):
    psycopg = pytest.importorskip("psycopg")
    from prism.store.pg import PgRepository
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            pass
    except Exception as exc:                       # pragma: no cover
        pytest.skip(f"no test database: {exc}")
    return PgRepository(user_id=user_id, dsn=DSN)


@pytest.fixture(params=["sqlite", "postgres"])
def store_factory(request, tmp_path):
    """Returns make(user_id) -> store, plus a clean slate for each backend."""
    if request.param == "postgres":
        if not DSN:
            pytest.skip("SUNROOM_TEST_DSN not set")
        psycopg = pytest.importorskip("psycopg")
        # Fresh accounts per test; the schema is shared, the data is not.
        ids = {}

        def make(name: str):
            if name not in ids:
                uid = str(uuid.uuid4())
                with psycopg.connect(DSN, autocommit=True) as c:
                    c.execute("INSERT INTO auth.users (id, email) VALUES (%s,%s) "
                              "ON CONFLICT DO NOTHING", (uid, f"{name}@test.local"))
                    c.execute("INSERT INTO accounts (id, email) VALUES (%s,%s) "
                              "ON CONFLICT DO NOTHING", (uid, f"{name}@test.local"))
                ids[name] = uid
            return _pg(ids[name])
        yield make
        with psycopg.connect(DSN, autocommit=True) as c:
            for uid in ids.values():
                c.execute("DELETE FROM auth.users WHERE id = %s", (uid,))
                c.execute("DELETE FROM accounts WHERE id = %s", (uid,))
    else:
        db = tmp_path / "corpus.db"
        ids: dict[str, str] = {}

        def make(name: str):
            ids.setdefault(name, str(uuid.uuid5(uuid.NAMESPACE_DNS, name)))
            return Repository(db, user_id=ids[name])
        yield make


@pytest.fixture()
def doc(tmp_path):
    p = tmp_path / "exchange.md"
    p.write_text(DOC)
    return understand(ingest(str(p)), client=MockClient(), collection="ANTH266")


def test_save_and_get_round_trip(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    got = s.get(doc.id)
    assert got is not None
    assert got.id == doc.id
    assert got.source.title == doc.source.title
    assert len(got.nodes) == len(doc.nodes)
    assert len(got.spans) == len(doc.spans)


def test_list_reports_node_counts(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    rows = s.list()
    assert len(rows) == 1
    assert rows[0]["nodes"] == len(doc.nodes)
    assert rows[0]["collection"] == "ANTH266"


def test_collections_count_documents(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    assert [(c["name"], c["documents"]) for c in s.collections()] == [("ANTH266", 1)]


def test_search_finds_a_term(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    hits = s.search("reciprocity")
    assert hits, "full-text search returned nothing"
    assert all("understanding" in h and "label" in h for h in hits)


def test_search_survives_punctuation(store_factory, doc):
    """A search box takes whatever someone types, including a stray quote."""
    s = store_factory("alice")
    s.save(doc)
    for query in ["malinowski's", 'reciprocity "give and take"', "a & b", "or"]:
        s.search(query)          # must not raise


def test_search_scoped_to_a_collection(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    assert s.search("reciprocity", collection="ANTH266")
    assert s.search("reciprocity", collection="NOPE") == []


def test_search_reflects_an_update(store_factory, doc):
    """The index must not survive the row it describes."""
    s = store_factory("alice")
    s.save(doc)
    assert s.search("redistribution")
    doc.nodes = [n for n in doc.nodes if "edistribution" not in n.body]
    s.save(doc)
    assert not [h for h in s.search("redistribution")
                if "edistribution" in h["body"]]


def test_nodes_in_collection(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    nodes = s.nodes_in("ANTH266")
    assert len(nodes) == len(doc.nodes)
    assert all(n.label for n in nodes)


def test_render_history_and_latest(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    for i in range(3):
        s.save_render(RenderResult(
            understanding_id=doc.id, renderer="summary", tier="production",
            format="markdown", artifact=f"v{i}",
            units=[RenderUnit(kind="line", content=f"v{i}", derived_from=[])]),
            doc.source.checksum)
    latest = s.latest_render(doc.id, "summary")
    assert latest is not None and latest.artifact == "v2"
    # One current rendering per medium, not the whole history.
    assert len(s.latest_renders(doc.id)) == 1
    assert len(s.all_renders()) == 1


def test_stale_render_detected_when_source_changes(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    s.save_render(RenderResult(
        understanding_id=doc.id, renderer="summary", tier="production",
        format="markdown", artifact="x",
        units=[RenderUnit(kind="line", content="x", derived_from=[])]),
        "an-old-checksum")
    stale = s.stale_renders()
    assert [r["understanding"] for r in stale] == [doc.id]


def test_deliverable_round_trip(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    d = Deliverable(understanding_id=doc.id, format="brief", tier="production",
                    artifact="the brief",
                    parts=[Part(role="thesis", title="In short",
                                body="Reciprocity is mutual.", derived_from=[])])
    s.save_deliverable(d, doc.source.checksum)
    got = s.latest_deliverable(doc.id, "brief")
    assert got is not None and got.parts[0].title == "In short"
    assert len(s.all_deliverables()) == 1


def test_delete_removes_everything_downstream(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    s.save_render(RenderResult(
        understanding_id=doc.id, renderer="summary", tier="production",
        format="markdown", artifact="x",
        units=[RenderUnit(kind="line", content="x", derived_from=[])]),
        doc.source.checksum)
    s.save_deliverable(Deliverable(
        understanding_id=doc.id, format="brief", tier="production", artifact="x",
        parts=[Part(role="thesis", title="t", body="b", derived_from=[])]),
        doc.source.checksum)
    assert s.delete(doc.id) is True
    assert s.get(doc.id) is None
    assert s.all_renders() == []
    assert s.all_deliverables() == []
    assert s.search("reciprocity") == []


def test_review_ladder(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    node = doc.nodes[0].id
    first = s.schedule(node, "ANTH266", correct=True)
    assert first["interval"] == 1 and first["reps"] == 1
    second = s.schedule(node, "ANTH266", correct=True)
    assert second["interval"] == 6 and second["reps"] == 2
    lapse = s.schedule(node, "ANTH266", correct=False)
    assert lapse["lapses"] == 1 and lapse["interval"] == 0
    assert lapse["ease"] < second["ease"]


def test_missed_card_comes_back_in_the_same_session(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    s.schedule(doc.nodes[0].id, "ANTH266", correct=False)
    assert [r["node_id"] for r in s.due()] == [doc.nodes[0].id]


def test_correct_card_is_not_due_again_today(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    s.schedule(doc.nodes[0].id, "ANTH266", correct=True)
    assert s.due() == []


def test_review_rejects_an_unknown_node(store_factory, doc):
    s = store_factory("alice")
    s.save(doc)
    with pytest.raises(NotFound):
        s.schedule("node_does_not_exist", None, correct=True)


def test_two_accounts_never_see_each_other(store_factory, doc, tmp_path):
    alice, bob = store_factory("alice"), store_factory("bob")
    alice.save(doc)

    other = tmp_path / "bob.md"
    other.write_text(DOC.replace("Exchange", "Bob Private"))
    bobs = understand(ingest(str(other)), client=MockClient(), collection="B")
    bob.save(bobs)

    assert [d["id"] for d in alice.list()] == [doc.id]
    assert [d["id"] for d in bob.list()] == [bobs.id]
    assert alice.get(bobs.id) is None
    assert bob.get(doc.id) is None
    assert alice.find_by_checksum(bobs.source.checksum) is None
    assert bob.search("reciprocity") and all(
        h["title"] != doc.source.title for h in bob.search("reciprocity"))
    assert alice.delete(bobs.id) is False
    assert bob.get(bobs.id) is not None


def test_checksum_dedupe_is_per_account(store_factory, doc):
    alice, bob = store_factory("alice"), store_factory("bob")
    alice.save(doc)
    assert alice.find_by_checksum(doc.source.checksum) is not None
    assert bob.find_by_checksum(doc.source.checksum) is None
