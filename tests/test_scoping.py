"""
Every query the corpus store runs must be scoped to one account.

This is enforced twice, because one enforcement is a promise and two is a
property:

  * `test_no_unscoped_sql` reads the source with the AST and fails if any
    statement touching a per-account table is missing a user_id predicate. It
    catches the query you add next month and forget to scope.
  * the behavioural tests below actually put two accounts in one database and
    try, from each one, to see the other's work through every public method.

The AST check exists because behavioural tests only cover the methods someone
remembered to write a test for, and the whole risk here is the method nobody
thought about.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re
import tempfile

import pytest

from prism.formats.base import Deliverable, Part
from prism.ingest import ingest
from prism.llm import MockClient
from prism.models import RenderResult, RenderUnit
from prism.store import Repository
from prism.store.base import NotFound
from prism.understand import understand

SCOPED_TABLES = {"understandings", "nodes", "renders", "deliverables",
                 "review_state", "collections", "jobs", "usage_events",
                 "usage_current"}

REPO_SRC = pathlib.Path(inspect.getfile(Repository))


def _sql_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every SQL string passed to a cursor call, joined across concatenation."""
    out: list[tuple[int, str]] = []

    def literal(node: ast.AST) -> str | None:
        # Adjacent string literals are one JoinedStr-free Constant after
        # parsing, but explicit `a + b` concatenation is a BinOp.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = literal(node.left), literal(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute)
                and fn.attr in {"execute", "executemany"}):
            continue
        for arg in node.args[:1]:
            text = literal(arg)
            if text:
                out.append((node.lineno, text))
    return out


def _assignments(tree: ast.AST) -> list[tuple[int, str]]:
    """`sql = "..."` then `sql += "..."` -- the dynamic-filter pattern."""
    buckets: dict[str, list[str]] = {}
    lines: dict[str, int] = {}
    for node in ast.walk(tree):
        target = value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
            target, value = node.target, node.value
        if not isinstance(target, ast.Name) or target.id != "sql":
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            buckets.setdefault("sql", []).append(value.value)
            lines.setdefault("sql", node.lineno)
        elif isinstance(value, ast.BinOp):
            for side in (value.left, value.right):
                if isinstance(side, ast.Constant) and isinstance(side.value, str):
                    buckets.setdefault("sql", []).append(side.value)
                    lines.setdefault("sql", node.lineno)
    return [(lines[k], " ".join(v)) for k, v in buckets.items()]


def test_no_unscoped_sql():
    tree = ast.parse(REPO_SRC.read_text())
    problems = []
    for lineno, sql in _sql_strings(tree):
        low = " ".join(sql.lower().split())
        if not re.search(r"\b(select|insert|update|delete)\b", low):
            continue
        if not any(re.search(rf"\b{t}\b", low) for t in SCOPED_TABLES):
            continue
        if "nodes_fts" in low and "user_id" not in low:
            continue                      # the FTS shadow table has no user_id
        if "pragma" in low:
            continue
        if "user_id" not in low:
            problems.append(f"{REPO_SRC.name}:{lineno}: {low[:110]}")
    assert not problems, (
        "these statements touch per-account tables without a user_id "
        "predicate:\n  " + "\n  ".join(problems))


def test_repository_cannot_be_built_without_an_account():
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(ValueError):
            Repository(pathlib.Path(d) / "x.db", user_id="")


# --------------------------------------------------------------------------

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"

DOC = """# Kinship and Obligation

Descent is the socially recognized link between a person and their ancestors.
It is the principle by which membership in a group is transmitted.

Affinity is the relationship created by marriage rather than by birth.
Alliance theory treats marriage as an exchange between groups.
"""


@pytest.fixture()
def two_accounts(tmp_path):
    db = tmp_path / "shared.db"
    alice = Repository(db, user_id=ALICE)
    bob = Repository(db, user_id=BOB)

    src = tmp_path / "kin.md"
    src.write_text(DOC)
    u = understand(ingest(str(src)), client=MockClient(), collection="ANTH266")
    alice.save(u)

    other = tmp_path / "other.md"
    other.write_text(DOC.replace("Kinship", "Bob's Private Notes"))
    v = understand(ingest(str(other)), client=MockClient(), collection="PRIVATE")
    bob.save(v)
    return alice, bob, u, v


def test_list_shows_only_your_own(two_accounts):
    alice, bob, u, v = two_accounts
    assert [d["id"] for d in alice.list()] == [u.id]
    assert [d["id"] for d in bob.list()] == [v.id]


def test_get_by_id_refuses_another_account(two_accounts):
    alice, bob, u, v = two_accounts
    assert alice.get(v.id) is None
    assert bob.get(u.id) is None
    # And the id-prefix convenience path must not become a back door.
    assert alice.get(v.id[:12]) is None


def test_checksum_dedupe_does_not_leak_across_accounts(tmp_path):
    """
    Two people uploading the same file each get their own copy.

    Returning Alice's document to Bob because the bytes matched would be both a
    data leak and a correctness bug: Alice deleting it would empty Bob's shelf.
    """
    db = tmp_path / "shared.db"
    src = tmp_path / "same.md"
    src.write_text(DOC)
    u = understand(ingest(str(src)), client=MockClient())
    Repository(db, user_id=ALICE).save(u)

    bob = Repository(db, user_id=BOB)
    assert bob.find_by_checksum(u.source.checksum) is None


def test_collections_are_private(two_accounts):
    alice, bob, _, _ = two_accounts
    assert [c["name"] for c in alice.collections()] == ["ANTH266"]
    assert [c["name"] for c in bob.collections()] == ["PRIVATE"]


def test_search_does_not_cross_accounts(two_accounts):
    alice, bob, _, _ = two_accounts
    assert alice.search("descent")
    for hit in alice.search("descent"):
        assert "Private" not in hit["title"]
    for hit in bob.search("descent"):
        assert "Kinship" not in hit["title"]


def test_nodes_in_collection_is_private(two_accounts):
    alice, bob, _, _ = two_accounts
    assert bob.nodes_in("ANTH266") == []
    assert alice.nodes_in("PRIVATE") == []


def test_renders_and_deliverables_are_private(two_accounts):
    alice, bob, u, _ = two_accounts
    result = RenderResult(
        understanding_id=u.id, renderer="summary", tier="production",
        format="markdown", artifact="hello",
        units=[RenderUnit(kind="line", content="hello", derived_from=[])])
    alice.save_render(result, u.source.checksum)
    deliverable = Deliverable(
        understanding_id=u.id, format="brief", tier="production",
        artifact="hi", parts=[Part(role="thesis", title="t", body="b",
                                   derived_from=[])])
    alice.save_deliverable(deliverable, u.source.checksum)

    assert alice.renders_for(u.id) and not bob.renders_for(u.id)
    assert alice.latest_render(u.id, "summary") is not None
    assert bob.latest_render(u.id, "summary") is None
    assert alice.latest_deliverable(u.id, "brief") is not None
    assert bob.latest_deliverable(u.id, "brief") is None
    assert len(alice.all_renders()) == 1 and bob.all_renders() == []
    assert len(alice.all_deliverables()) == 1 and bob.all_deliverables() == []


def test_delete_only_touches_your_own(two_accounts):
    alice, bob, u, v = two_accounts
    assert alice.delete(v.id) is False
    assert bob.get(v.id) is not None
    assert alice.delete(u.id) is True
    assert alice.get(u.id) is None


def test_review_refuses_a_node_you_do_not_own(two_accounts):
    alice, bob, u, v = two_accounts
    node = v.nodes[0].id
    with pytest.raises(NotFound):
        alice.schedule(node, None, correct=True)
    assert alice.due() == []


def test_review_queue_is_private(two_accounts):
    alice, bob, u, v = two_accounts
    alice.schedule(u.nodes[0].id, "ANTH266", correct=False)
    assert len(alice.due()) == 1
    assert bob.due() == []


def test_deleting_a_document_removes_its_review_state(two_accounts):
    """A card whose passage is gone would be unanswerable and unfixable."""
    alice, _, u, _ = two_accounts
    alice.schedule(u.nodes[0].id, "ANTH266", correct=False)
    assert alice.due()
    alice.delete(u.id)
    assert alice.due() == []
