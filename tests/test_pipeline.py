"""End-to-end tests. All offline via the mock provider — no key, no spend."""
from __future__ import annotations

import pytest

# The environment is configured in conftest.py, before any test module is
# imported -- setting it here would race whichever module pytest imports first.
from prism import Prism, Repository, catalog, check, ingest, understand  # noqa: E402
from prism.fidelity import check_deliverable  # noqa: E402
from prism.formats import (
    ask,  # noqa: E402
    get_format,  # noqa: E402
)
from prism.formats import catalog as format_catalog  # noqa: E402
from prism.models import Medium, NodeKind, RenderUnit  # noqa: E402
from prism.render import get_renderer  # noqa: E402

DOC = """# Kinship and Obligation

Kinship is a system that organizes obligation between people. It is not merely
a record of biological descent.

## How delayed exchange works

First, a household produces a surplus of food. Then, that surplus is distributed
outward to kin living outside the household. Finally, the receiving household is
bound to reciprocate at some later date.

Because reciprocity is delayed, the obligation persists across seasons. Roughly
60 percent of surveyed households reported an outstanding obligation.
"""


@pytest.fixture(scope="module")
def doc():
    return understand(ingest(DOC, title="Kinship", medium=Medium.MARKDOWN),
                      collection="TEST")


# -- ingest ---------------------------------------------------------------

def test_raw_text_does_not_touch_filesystem():
    """Long raw text must not be stat()'d as a path (ENAMETOOLONG)."""
    ing = ingest("x " * 5000, title="Long")
    assert ing.source.text


def test_markdown_sections():
    ing = ingest(DOC, title="K", medium=Medium.MARKDOWN)
    assert [s.title for s in ing.sections] == ["Kinship and Obligation",
                                               "How delayed exchange works"]


def test_transcript_timestamps_survive():
    srt = "1\n00:00:01,000 --> 00:00:04,000\nObligation outlasts the household.\n"
    ing = ingest(srt, title="Pod", medium=Medium.AUDIO)
    assert ing.sections[0].span.t_start == 1.0
    assert ing.sections[0].span.t_end == 4.0


# -- IR -------------------------------------------------------------------

def test_every_node_is_grounded(doc):
    """The core invariant: no node without a resolvable source span."""
    span_ids = {s.id for s in doc.spans}
    for node in doc.nodes:
        assert node.provenance, f"{node.label} has no provenance"
        assert all(sid in span_ids for sid in node.provenance)


def test_spans_resolve_to_real_text(doc):
    for span in doc.spans:
        assert 0 <= span.start < span.end <= len(doc.source.text)
        assert span.excerpt(doc.source).strip()


def test_structural_queries(doc):
    assert doc.nodes and doc.edges
    assert doc.roots()
    assert doc.testable()
    assert all(isinstance(n.kind, NodeKind) for n in doc.nodes)


def test_merge_unions_provenance():
    from prism.models import Node
    a = Node(kind=NodeKind.CLAIM, label="Reciprocity is delayed",
             body="Reciprocity is delayed across seasons.", provenance=["s1"], salience=0.6)
    b = Node(kind=NodeKind.CLAIM, label="Reciprocity is delayed",
             body="Reciprocity is delayed across seasons.", provenance=["s2"], salience=0.4)
    from prism.understand import merge_nodes
    kept, _, remap = merge_nodes([a, b], [])
    assert len(kept) == 1
    assert set(kept[0].provenance) == {"s1", "s2"}
    assert remap[b.id] == a.id


# -- renderers ------------------------------------------------------------

def _render_or_skip(doc, name):
    """A renderer may refuse content it cannot honestly represent."""
    try:
        return get_renderer(name).render(doc)
    except ValueError as exc:
        pytest.skip(f"{name} declined: {exc}")


@pytest.mark.parametrize("name", [r["name"] for r in catalog()])
def test_every_renderer_produces_grounded_output(doc, name):
    result = _render_or_skip(doc, name)
    assert result.units, f"{name} produced nothing"
    assert result.artifact.strip()
    for unit in result.units:
        assert unit.derived_from, f"{name} emitted an ungrounded unit"
        assert all(doc.node(nid) for nid in unit.derived_from)


@pytest.mark.parametrize("name", [r["name"] for r in catalog()])
def test_every_renderer_passes_fidelity(doc, name):
    report = check(doc, _render_or_skip(doc, name))
    assert report.passed, [f.message for f in report.findings]
    assert report.grounding == 1.0


def test_diagram_is_deterministic(doc):
    """No model call means byte-identical output across runs."""
    a = get_renderer("diagram").render(doc).artifact
    b = get_renderer("diagram").render(doc).artifact
    assert a == b and a.startswith(("flowchart", "mindmap"))


def test_tiers_are_declared():
    tiers = {r["name"]: r["tier"] for r in catalog()}
    assert tiers["comic"] == "experimental"
    assert tiers["slides"] == "beta"
    assert tiers["diagram"] == "production"


# -- fidelity -------------------------------------------------------------

def test_checker_catches_ungrounded_output(doc):
    result = get_renderer("summary").render(doc)
    result.units.append(RenderUnit(kind="fabricated",
                                   content="Kinship was invented in 1873.",
                                   derived_from=[]))
    report = check(doc, result)
    assert not report.passed
    assert any(f.code == "ungrounded" for f in report.findings)


def test_checker_catches_dangling_node(doc):
    result = get_renderer("summary").render(doc)
    result.units.append(RenderUnit(kind="bad", content="Something",
                                   derived_from=["nod_doesnotexist"]))
    report = check(doc, result)
    assert not report.passed
    assert any(f.code == "dangling_node" for f in report.findings)


def test_checker_flags_drift(doc):
    result = get_renderer("summary").render(doc)
    real = doc.salient(1)[0].id
    result.units.append(RenderUnit(
        kind="drifted",
        content="Quantum chromodynamics governs baryon confinement inside hadrons.",
        derived_from=[real],
    ))
    report = check(doc, result)
    assert any(f.code == "possible_drift" for f in report.findings)


def test_checker_detects_stale_render(doc):
    result = get_renderer("summary").render(doc)
    result.meta["source_checksum"] = "stale-checksum"
    report = check(doc, result)
    assert report.stale
    assert any(f.code == "stale" for f in report.findings)


# -- repository -----------------------------------------------------------

def test_repository_roundtrip_and_search():
    p = Prism()
    u = p.add(DOC, collection="ROUNDTRIP", title="K2", force=True)
    back = p.get(u.id)
    assert back and len(back.nodes) == len(u.nodes)
    assert p.search("obligation", collection="ROUNDTRIP")


def test_stale_render_detection_via_store():
    repo = Repository()
    u = understand(ingest(DOC, title="Stale", medium=Medium.MARKDOWN), collection="S")
    repo.save(u)
    result = get_renderer("summary").render(u)
    repo.save_render(result, source_checksum="old-checksum")
    assert any(r["understanding"] == u.id for r in repo.stale_renders())


def test_review_scheduling_advances():
    repo = Repository()
    u = understand(ingest(DOC, title="SM2", medium=Medium.MARKDOWN), collection="SM2")
    repo.save(u)
    nid = u.nodes[0].id
    first = repo.schedule(nid, "SM2", correct=True)
    second = repo.schedule(nid, "SM2", correct=True)
    assert second["reps"] == 2 and second["interval"] > first["interval"]
    lapsed = repo.schedule(nid, "SM2", correct=False)
    assert lapsed["lapses"] == 1 and lapsed["ease"] < second["ease"]


def test_glossary_declines_rather_than_fabricating(doc):
    """
    Regression: the glossary used to fall back to 'most salient concepts',
    which built entries out of section headings whose gloss restated the
    heading. Refusing is the correct behaviour.
    """
    from prism.models import NodeKind
    defs = [n for n in doc.nodes
            if n.kind is NodeKind.DEFINITION and not n.meta.get("section")]
    if len(defs) >= 2:
        pytest.skip("document has real definitions")
    with pytest.raises(ValueError, match="glossary needs at least"):
        get_renderer("glossary").render(doc)


def test_no_flashcard_reveals_its_own_answer(doc):
    """Regression: 40 cards across the corpus contained their own answers."""
    result = get_renderer("retrieval").render(doc)
    for unit in result.units:
        answer = str(unit.meta.get("answer", "")).strip().lower()
        assert answer, "card with no answer"
        assert answer not in unit.content.strip().lower()


def test_every_span_has_a_human_locator(doc):
    """Regression: unstructured text produced byte-offset citations."""
    for span in doc.spans:
        assert span.locator, "span with no human-readable locator"


def test_no_orphan_nodes(doc):
    """Regression: plain text left 90% of nodes unlinked."""
    linked = {e.source for e in doc.edges} | {e.target for e in doc.edges}
    orphans = [n.label for n in doc.nodes if n.id not in linked]
    assert not orphans, orphans


def test_diagram_units_are_unique(doc):
    result = get_renderer("diagram").render(doc)
    contents = [u.content.strip() for u in result.units]
    assert len(contents) == len(set(contents))


def test_scaffolding_never_reaches_output(doc):
    """
    Regression: synthesized process nodes and section anchors became
    flashcards, producing 'The ______ described under "______".'
    """
    for name in ("retrieval", "glossary"):
        try:
            result = get_renderer(name).render(doc)
        except ValueError:
            continue
        for unit in result.units:
            for nid in unit.derived_from:
                node = doc.node(nid)
                if node and node.kind.value in ("claim", "definition", "quantity"):
                    assert not node.is_scaffold, f"{name} used scaffolding: {node.label}"


# -- attribution ----------------------------------------------------------

def test_every_span_yields_a_citation(doc):
    from prism.cite import citation_for
    for span in doc.spans:
        c = citation_for(doc, span)
        assert c.quote.strip(), "citation with no quote"
        assert c.locator, "citation with no locator"
        assert c.anchor, "citation with no anchor"


def test_citation_quote_is_verbatim(doc):
    """The quote must be exactly what the source says — never a paraphrase."""
    from prism.cite import citation_for
    for span in doc.spans:
        c = citation_for(doc, span)
        assert c.quote in doc.source.text


def test_pdf_anchor_carries_the_page():
    import os
    path = os.path.join(os.path.dirname(__file__), "..",
                        "examples", "corpus", "textbook_chapter.pdf")
    if not os.path.exists(path):
        pytest.skip("sample pdf missing")
    from prism.cite import citation_for
    u = understand(ingest(os.path.abspath(path)))
    anchors = [citation_for(u, s).anchor for s in u.spans]
    assert any("#page=" in a for a in anchors), "no PDF page anchors"
    pages = {citation_for(u, s).page for s in u.spans if s.page}
    assert pages <= {1, 2, 3} and pages


def test_media_anchor_carries_the_timestamp():
    srt = ("1\n00:00:12,000 --> 00:00:18,000\n"
           "Reciprocity is delayed and the obligation persists across seasons.\n")
    from prism.cite import citation_for
    u = understand(ingest(srt, title="Pod", medium=Medium.AUDIO))
    u.source.uri = "https://example.org/ep12.mp3"
    anchors = [citation_for(u, s).anchor for s in u.spans]
    assert any("#t=12" in a for a in anchors), anchors


def test_web_anchor_is_a_text_fragment():
    from prism.cite import citation_for
    u = understand(ingest("<h1>Brief</h1><p>Heritage tourism generated an "
                          "estimated 3.2 billion dollars in 2025.</p>",
                          title="Brief", medium=Medium.HTML))
    u.source.uri = "https://example.org/brief"
    cites = [citation_for(u, s) for s in u.spans]
    frags = [c for c in cites if c.anchor_kind == "textfragment"]
    assert frags, "no text fragment anchors"
    assert all("#:~:text=" in c.anchor for c in frags)


def test_text_fragment_encodes_long_quotes_as_a_range():
    from prism.cite import text_fragment
    long_quote = " ".join(f"word{i}" for i in range(30))
    frag = text_fragment(long_quote)
    assert frag.count(",") == 1, "long quote should become start,end"
    short = text_fragment("a short quote here")
    assert "," not in short


def test_every_artifact_carries_its_sources(doc):
    """A rendering a reader cannot check is not finished."""
    for name in [r["name"] for r in catalog()]:
        renderer = get_renderer(name)
        try:
            result = renderer.render(doc)
        except ValueError:
            continue
        assert result.meta["citations"], f"{name} produced no citations"
        if renderer.cites_in_artifact:
            assert "### Sources" in result.artifact, f"{name} artifact has no sources"
        elif result.format == "json":
            # structured output embeds attribution per item instead
            assert '"cite"' in result.artifact and '"anchor"' in result.artifact


def test_units_carry_footnote_numbers(doc):
    result = get_renderer("summary").render(doc)
    for unit in result.units:
        assert unit.meta.get("footnotes"), "unit with no footnote mapping"


def test_repository_is_thread_safe():
    """Regression: a shared sqlite connection broke every concurrent request."""
    import threading
    repo = Repository()
    u = understand(ingest(DOC, title="Threads", medium=Medium.MARKDOWN),
                   collection="THREADS")
    repo.save(u)
    errors: list[str] = []

    def worker(i: int) -> None:
        try:
            for _ in range(5):
                repo.list()
                repo.search("obligation")
                repo.get(u.id)
                repo.schedule(u.nodes[0].id, "THREADS", correct=bool(i % 2))
        except Exception as exc:            # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_diagram_labels_are_mermaid_safe(doc):
    """Regression: brackets rewritten as parens broke mind map syntax."""
    artifact = get_renderer("diagram").render(doc).artifact
    body = "\n".join(artifact.splitlines()[1:])
    if artifact.startswith("mindmap"):
        assert "(" not in body.replace("root((", "").replace("))", "")


def test_tutor_questions_keep_proper_nouns(doc):
    """
    Regression: seed questions ran `label.lower()`, which turned a name in the
    middle of a label into lowercase ("…in malinowski's"). Only a first word the
    source itself uses lowercased may be lowered.
    """
    fmt = get_format("tutor")
    names = {w for w in doc.source.text.split()
             if w[:1].isupper() and w.lower() not in doc.source.text}
    for part in fmt.build(doc):
        if not part.title:
            continue
        for name in names:
            bare = name.strip(".,;:?\u2019'\"")
            if not bare or len(bare) < 4:
                continue
            assert bare.lower() not in part.title.split(), \
                f"{bare} was lowercased in {part.title!r}"


def test_mindmap_has_exactly_one_root(doc):
    """
    Regression: branches were emitted at the same indent as `root((title))`,
    which mermaid reads as a second root and refuses to parse. The failure was
    invisible from the engine's side -- grounding and coverage were both
    perfect -- and only showed up as raw source in the reader.
    """
    artifact = get_renderer("diagram").render(doc).artifact
    if not artifact.startswith("mindmap"):
        return
    lines = [ln for ln in artifact.splitlines()[1:] if ln.strip()]
    indents = [len(ln) - len(ln.lstrip()) for ln in lines]
    top = min(indents)
    assert indents.count(top) == 1, (
        "a mermaid mindmap takes one root; "
        f"found {indents.count(top)} lines at indent {top}")
    assert lines[0].strip().startswith("root(("), \
        "the shallowest line must be the root node"


def test_declared_mode_matches_the_syntax_emitted(doc):
    """
    Regression: `_mindmap` fell back to a concept map when the hierarchy was
    thin, but `build` still stamped mode="mindmap" onto the fallback units, so
    `assemble` wrapped flowchart node syntax in a mindmap header. Unparseable,
    and invisible to grounding and coverage.
    """
    renderer = get_renderer("diagram")
    for mode in ("auto", "flow", "mindmap", "causal", "concept"):
        artifact = renderer.render(doc, mode=mode).artifact
        if not artifact.strip() or artifact.startswith("%%"):
            continue
        header = artifact.splitlines()[0].strip()
        assert header in ("mindmap", "flowchart TD"), header
        body = "\n".join(artifact.splitlines()[1:])
        if header == "mindmap":
            assert "-->" not in body and "-.->" not in body, \
                "mindmap header over flowchart edges"
        else:
            assert "root((" not in body


def test_mindmap_does_not_repeat_the_title_as_a_branch(doc):
    """The document title is already the root; repeating it wastes a level."""
    artifact = get_renderer("diagram").render(doc).artifact
    if not artifact.startswith("mindmap"):
        return
    lines = [ln.strip() for ln in artifact.splitlines()[1:] if ln.strip()]
    root = lines[0][len("root(("):-2].strip().casefold()
    assert root not in {ln.casefold() for ln in lines[1:]}


def test_review_cards_come_from_the_renderer(doc):
    """
    Regression: the review queue read label/body straight off the IR node.
    A node's label is a prefix of its body, so every card shown to a learner
    contained its own answer — the exact defect the renderer already fixed.
    """
    result = get_renderer("retrieval").render(doc)
    index = {}
    for unit in result.units:
        for nid in unit.derived_from:
            index.setdefault(nid, unit)
    tested = 0
    for node in doc.testable()[:10]:
        unit = index.get(node.id)
        if not unit:
            continue
        tested += 1
        answer = str(unit.meta.get("answer", "")).strip().lower()
        assert answer and answer not in unit.content.strip().lower()
    assert tested, "no reviewable cards produced"


# -- formats --------------------------------------------------------------

FORMAT_NAMES = [f["name"] for f in format_catalog()]


def _make_or_skip(doc, name):
    try:
        return get_format(name).make(doc)
    except ValueError as exc:
        pytest.skip(f"{name} declined: {exc}")


@pytest.mark.parametrize("name", FORMAT_NAMES)
def test_every_format_produces_cited_parts(doc, name):
    d = _make_or_skip(doc, name)
    assert d.parts, f"{name} produced nothing"
    assert d.artifact.strip()
    assert d.citations, f"{name} carries no citations"
    for part in d.parts:
        if part.role in ("timing", "boundary"):
            continue
        assert part.derived_from, f"{name}/{part.role} cites nothing"
        assert all(doc.node(i) for i in part.derived_from)


@pytest.mark.parametrize("name", FORMAT_NAMES)
def test_every_format_passes_fidelity(doc, name):
    report = check_deliverable(doc, _make_or_skip(doc, name))
    assert report.passed, [f.message for f in report.findings]
    assert report.grounding == 1.0


def test_formats_are_composed_from_renderers():
    """A format is a recipe, not new machinery."""
    component_names = {r["name"] for r in catalog()}
    for row in format_catalog():
        for used in row["uses"]:
            assert used in component_names, f"{row['name']} uses unknown {used}"


def test_scaffolding_parts_are_marked_non_asserting(doc):
    """
    A part whose BODY is an instruction ("project the figure and walk it")
    legitimately uses words the source never used, and marking it keeps the
    drift check sharp instead of firing on every lesson.

    A part whose body quotes the source still asserts, even when it carries
    teacher notes alongside — the notes are metadata, the body is the claim.
    """
    d = _make_or_skip(doc, "lesson")
    instructional_moves = {"figure", "practice", "check"}
    instructional = [p for p in d.parts
                     if p.role == "timing" or p.meta.get("move") in instructional_moves]
    assert instructional, "lesson produced no instructional parts"
    assert all(not p.asserts for p in instructional), \
        [p.title for p in instructional if p.asserts]

    quoting = [p for p in d.parts if p.meta.get("move") == "explain"]
    assert all(p.asserts for p in quoting), "source-quoting parts must be checked"


def test_activity_covers_several_practice_types(doc):
    d = _make_or_skip(doc, "activity")
    types = {p.meta.get("type") for p in d.parts}
    assert "drill" in types
    assert len(types) >= 2, types


def test_activity_cards_do_not_leak_answers(doc):
    d = _make_or_skip(doc, "activity")
    for part in d.parts:
        answer = part.meta.get("answer")
        if not answer or isinstance(answer, (list, dict)):
            continue
        assert str(answer).strip().lower() not in part.body.strip().lower()


def test_podcast_alternates_two_voices(doc):
    d = _make_or_skip(doc, "podcast")
    speakers = [line.split(":", 1)[0] for p in d.parts
                for line in p.body.splitlines()
                if line.startswith(("HOST:", "EXPERT:"))]
    assert len(speakers) >= 4
    assert {"HOST", "EXPERT"} <= set(speakers)


def test_explainer_produces_figures_not_video(doc):
    d = _make_or_skip(doc, "explainer")
    kinds = {p.meta.get("kind") for p in d.parts}
    assert "diagram" in kinds
    assert d.artifact_format == "markdown"


def test_tutor_declines_what_the_source_does_not_cover(doc):
    covered = ask(doc, "What is reciprocity?")
    assert covered.covered and covered.citations
    outside = ask(doc, "What is the boiling point of tungsten?")
    assert not outside.covered
    assert "does not cover" in outside.text.lower()


def test_tutor_answers_are_grounded(doc):
    answer = ask(doc, "What does kinship organize?")
    if answer.covered:
        assert answer.citations
        for c in answer.citations:
            assert c.quote in doc.source.text


def test_brief_from_a_recording_carries_a_timeline():
    """Media -> text is the underserved direction; timestamps make it usable."""
    srt = ("1\n00:00:04,000 --> 00:00:10,000\n"
           "Kinship organizes obligation between households.\n\n"
           "2\n00:00:10,000 --> 00:00:16,000\n"
           "Reciprocity is the expectation that a gift creates a counter-obligation.\n")
    u = understand(ingest(srt, title="Ep", medium=Medium.AUDIO))
    d = get_format("brief").make(u)
    timeline = [p for p in d.parts if p.role == "timeline"]
    assert timeline, "no timeline for a time-based source"
    assert "00:00" in timeline[0].body


def test_deliverable_citations_are_verbatim(doc):
    for name in FORMAT_NAMES:
        try:
            d = get_format(name).make(doc)
        except ValueError:
            continue
        for c in d.citations:
            assert c["quote"] in doc.source.text, name


def test_renderer_requirements_fail_loudly():
    empty = understand(ingest("   ", title="Empty", medium=Medium.TEXT))
    with pytest.raises(ValueError, match="needs"):
        get_renderer("retrieval").render(empty)
