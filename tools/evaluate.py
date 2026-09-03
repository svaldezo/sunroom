#!/usr/bin/env python3
"""
Evaluation harness: every source x every renderer, with quality probes.

The fidelity checker answers "is this output traceable?". This harness answers
"is this output any good?" -- which needs different probes, because a perfectly
grounded flashcard that leaks its own answer is still a broken flashcard.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("PRISM_PROVIDER", "mock")

from prism import catalog, check, ingest, understand  # noqa: E402
from prism.fidelity import check_deliverable  # noqa: E402
from prism.formats import catalog as format_catalog  # noqa: E402
from prism.formats import get_format  # noqa: E402
from prism.models import Understanding  # noqa: E402
from prism.render import get_renderer  # noqa: E402

CORPUS = Path(__file__).resolve().parent.parent / "examples" / "corpus"

WORD = re.compile(r"[a-z0-9']+")
FRAGMENT_START = re.compile(r"^(and|but|or|so|then|because|which|that|it|this|the same)\b", re.I)


# ---------------------------------------------------------------- probes ---

def probe_ingest(ing) -> dict:
    src = ing.source
    return {
        "chars": len(src.text),
        "sections": len(ing.sections),
        "sections_with_locator": sum(1 for s in ing.sections if s.span and s.span.locator),
        "timestamped": sum(1 for s in ing.sections if s.span and s.span.t_start is not None),
        "empty": not src.text.strip(),
    }


def probe_ir(u: Understanding) -> dict:
    stats = u.stats()
    labels = [n.label for n in u.nodes if not n.meta.get("section")]
    linked = {e.source for e in u.edges} | {e.target for e in u.edges}
    orphans = [n for n in u.nodes if n.id not in linked]

    # a label should be a HANDLE, not a chopped sentence
    fragments = [lb for lb in labels if FRAGMENT_START.match(lb)]
    long_labels = [lb for lb in labels if len(lb.split()) > 8]
    dup_labels = len(labels) - len({lb.lower() for lb in labels})

    span_locators = sum(1 for s in u.spans if s.locator)
    return {
        **{k: v for k, v in stats.items()},
        "nodes": len(u.nodes),
        "orphan_nodes": len(orphans),
        "orphan_rate": round(len(orphans) / len(u.nodes), 3) if u.nodes else 0,
        "label_fragments": len(fragments),
        "label_fragment_rate": round(len(fragments) / len(labels), 3) if labels else 0,
        "labels_over_8_words": len(long_labels),
        "duplicate_labels": dup_labels,
        "span_locator_rate": round(span_locators / len(u.spans), 3) if u.spans else 0,
        "dropped_ungrounded": u.meta.get("dropped_ungrounded", 0),
        "mean_salience": round(statistics.mean([n.salience for n in u.nodes]), 3) if u.nodes else 0,
        "mean_concreteness": round(statistics.mean([n.concreteness for n in u.nodes]), 3) if u.nodes else 0,
    }


def probe_render(u: Understanding, result) -> dict:
    units = result.units
    contents = [x.content.strip() for x in units]
    issues: list[str] = []

    if not units:
        issues.append("no_units")
    if len(result.artifact.strip()) < 60:
        issues.append("stub_artifact")
    dupes = len(contents) - len(set(contents))
    if dupes:
        issues.append(f"duplicate_units:{dupes}")

    # renderer-specific quality
    extra: dict = {}
    if result.renderer == "retrieval":
        leaks = blanks = empty_answers = 0
        for x in units:
            ans = str(x.meta.get("answer", "") or "")
            if not ans.strip():
                empty_answers += 1
                continue
            if x.kind in ("cloze", "recall", "short_answer"):
                a = ans.strip().lower()
                p = x.content.strip().lower()
                if a and len(a) > 3 and a in p:
                    leaks += 1
            if x.kind == "cloze" and "______" not in x.content:
                blanks += 1
        extra = {"answer_leaks": leaks, "cloze_missing_blank": blanks,
                 "empty_answers": empty_answers}
        if leaks:
            issues.append(f"answer_leak:{leaks}")
        if empty_answers:
            issues.append(f"empty_answer:{empty_answers}")
        if blanks:
            issues.append(f"cloze_no_blank:{blanks}")

    if result.renderer == "diagram":
        art = result.artifact
        mode = units[0].meta.get("mode") if units else None
        head_ok = art.startswith(("flowchart", "mindmap", "graph"))
        # crude mermaid lint: unbalanced brackets break rendering entirely
        bad = [ln for ln in art.splitlines()
               if ln.count("[") != ln.count("]") or ln.count("(") != ln.count(")")]
        connectors = len([ln for ln in art.splitlines() if "-->" in ln or "-.-" in ln])
        extra = {"mermaid_header_ok": head_ok, "unbalanced_lines": len(bad),
                 "mode": mode, "graph_lines": connectors}
        if not head_ok:
            issues.append("bad_mermaid_header")
        if bad:
            issues.append(f"unbalanced_mermaid:{len(bad)}")
        # a mind map encodes structure by indentation, so arrows are not expected
        if mode != "mindmap" and connectors == 0 and len(units) > 1:
            issues.append("diagram_has_no_edges")
        if mode == "mindmap" and len(units) < 3:
            issues.append("mindmap_too_sparse")

    if result.renderer == "glossary":
        illustrated = sum(1 for x in units if x.meta.get("image_prompt"))
        # circular == the definition leans on the term it is defining
        circular = sum(
            1 for x in units
            if re.search(rf"\b{re.escape(x.meta.get('term', 'zzz'))}\b",
                         x.content, re.I)
        )
        extra = {"entries": len(units), "illustrated": illustrated,
                 "illustrated_rate": round(illustrated / len(units), 3) if units else 0,
                 "circular_glosses": circular}
        if circular:
            issues.append(f"circular_gloss:{circular}")

    if result.renderer == "narration":
        words = sum(len(x.content.split()) for x in units)
        extra = {"words": words,
                 "mean_segment_words": round(words / len(units), 1) if units else 0}
        if any(len(x.content.split()) < 12 for x in units):
            issues.append("thin_segment")

    return {"units": len(units), "artifact_chars": len(result.artifact),
            "issues": issues, **extra}


# ------------------------------------------------------------------ run ---

def probe_format(u: Understanding, d) -> dict:
    """Quality probes a fidelity check cannot make."""
    issues: list[str] = []
    parts = d.parts
    if not parts:
        issues.append("no_parts")
    if len(d.artifact.strip()) < 120:
        issues.append("stub_artifact")
    if not d.citations:
        issues.append("no_citations")

    bodies = [p.body.strip() for p in parts if p.body.strip()]
    dupes = len(bodies) - len(set(bodies))
    if dupes:
        issues.append(f"duplicate_parts:{dupes}")

    ungrounded = [p for p in parts
                  if not p.derived_from and p.role not in ("timing", "boundary")]
    if ungrounded:
        issues.append(f"ungrounded_parts:{len(ungrounded)}")

    # Every asserting part must be checkable: it has to cite something.
    uncited = [p for p in parts if p.asserts and not p.derived_from]
    if uncited:
        issues.append(f"uncited_assertion:{len(uncited)}")

    # Scaffolding leaking into a body reads as a bug to a user.
    leaked = [p for p in parts if "Section:" in p.body or "described under" in p.body]
    if leaked:
        issues.append(f"scaffold_leak:{len(leaked)}")

    extra: dict = {}
    if d.format == "activity":
        types = {p.meta.get("type") for p in parts}
        extra = {"activity_types": sorted(t for t in types if t)}
        answers = [p for p in parts if p.meta.get("answer")]
        leaks = 0
        for p in answers:
            a = str(p.meta["answer"]).strip().lower()
            if a and len(a) > 3 and a in p.body.strip().lower():
                leaks += 1
        extra["answer_leaks"] = leaks
        if leaks:
            issues.append(f"answer_leak:{leaks}")
    if d.format == "podcast":
        speakers = sum(1 for p in parts for line in p.body.splitlines()
                       if line.startswith(("HOST:", "EXPERT:")))
        extra = {"turns": speakers}
        if speakers < 4:
            issues.append("too_few_turns")
    if d.format == "explainer":
        figs = sum(1 for p in parts if p.meta.get("kind") == "diagram")
        extra = {"figures": figs}
        if not figs:
            issues.append("no_figures")
    if d.format == "lesson":
        planned = next((p.meta.get("planned") for p in parts if p.role == "timing"), 0)
        extra = {"planned_minutes": planned}

    return {"parts": len(parts), "citations": len(d.citations),
            "artifact_chars": len(d.artifact), "issues": issues, **extra}


def run() -> dict:
    sources = sorted(CORPUS.iterdir())
    renderers = [r["name"] for r in catalog()]
    formats = [f["name"] for f in format_catalog()]
    report: dict = {"sources": {}, "matrix": [], "formats": [], "issues": []}

    for path in sources:
        t0 = time.perf_counter()
        ing = ingest(str(path))
        t_ingest = time.perf_counter() - t0

        t0 = time.perf_counter()
        u = understand(ing, collection="EVAL")
        t_understand = time.perf_counter() - t0

        entry = {
            "file": path.name,
            "medium": u.source.medium.value,
            "ingest": probe_ingest(ing),
            "ir": probe_ir(u),
            "ms_ingest": round(t_ingest * 1000, 1),
            "ms_understand": round(t_understand * 1000, 1),
            "renders": {},
            "formats": {},
        }

        for name in renderers:
            row = {"source": path.name, "renderer": name}
            try:
                t0 = time.perf_counter()
                result = get_renderer(name).render(u)
                row["ms"] = round((time.perf_counter() - t0) * 1000, 1)
                rep = check(u, result)
                probes = probe_render(u, result)
                row.update({
                    "ok": True,
                    "passed": rep.passed,
                    "grounding": round(rep.grounding, 3),
                    "coverage": rep.coverage,
                    "overlap": rep.mean_overlap,
                    "findings": [f.code for f in rep.findings],
                    **probes,
                })
            except ValueError as exc:
                # a renderer refusing content it cannot honestly represent
                row.update({"ok": False, "passed": True, "declined": True,
                            "error": str(exc), "issues": []})
            except Exception as exc:
                row.update({"ok": False, "passed": False,
                            "error": f"{type(exc).__name__}: {exc}",
                            "issues": ["exception"]})
            entry["renders"][name] = row
            report["matrix"].append(row)
            for issue in row.get("issues", []):
                report["issues"].append(f"{path.name}/{name}: {issue}")
            if not row.get("passed", False):
                report["issues"].append(
                    f"{path.name}/{name}: FIDELITY {row.get('findings') or row.get('error')}")

        for name in formats:
            row = {"source": path.name, "format": name}
            try:
                t0 = time.perf_counter()
                deliverable = get_format(name).make(u)
                row["ms"] = round((time.perf_counter() - t0) * 1000, 1)
                rep = check_deliverable(u, deliverable)
                row.update({
                    "ok": True, "passed": rep.passed,
                    "grounding": round(rep.grounding, 3),
                    "coverage": rep.coverage, "overlap": rep.mean_overlap,
                    "findings": [f.code for f in rep.findings],
                    **probe_format(u, deliverable),
                })
            except ValueError as exc:
                row.update({"ok": False, "passed": True, "declined": True,
                            "error": str(exc), "issues": []})
            except Exception as exc:
                row.update({"ok": False, "passed": False,
                            "error": f"{type(exc).__name__}: {exc}",
                            "issues": ["exception"]})
            entry["formats"][name] = row
            report["formats"].append(row)
            for issue in row.get("issues", []):
                report["issues"].append(f"{path.name}/{name}: {issue}")
            if not row.get("passed", False):
                report["issues"].append(
                    f"{path.name}/{name}: FIDELITY {row.get('findings') or row.get('error')}")

        report["sources"][path.name] = entry

    return report


def render_report(report: dict) -> str:
    out = ["# prism evaluation", ""]
    out.append("## Sources")
    out.append("")
    out.append("| source | medium | chars | secs | nodes | orphan% | frag% | loc% | drop | ms |")
    out.append("|---|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for name, s in report["sources"].items():
        ir, ig = s["ir"], s["ingest"]
        out.append(
            f"| {name} | {s['medium']} | {ig['chars']} | {ig['sections']} | {ir['nodes']} | "
            f"{ir['orphan_rate']:.0%} | {ir['label_fragment_rate']:.0%} | "
            f"{ir['span_locator_rate']:.0%} | {ir['dropped_ungrounded']} | "
            f"{s['ms_understand']:.0f} |")

    out += ["", "## Formats (grounding / coverage / overlap)", ""]
    formats = [f["name"] for f in format_catalog()]
    out.append("| source | " + " | ".join(formats) + " |")
    out.append("|---" * (len(formats) + 1) + "|")
    for name, s_ in report["sources"].items():
        cells = []
        for f in formats:
            row = s_["formats"][f]
            if row.get("declined"):
                cells.append("—")
            elif not row.get("ok"):
                cells.append("ERR")
            else:
                mark = "" if row["passed"] and not row["issues"] else "!"
                cells.append(f"{row['grounding']:.0%}/{row['coverage']:.0%}/{row['overlap']:.2f}{mark}")
        out.append(f"| {name} | " + " | ".join(cells) + " |")

    out += ["", "## Components (grounding / coverage / overlap)", ""]
    renderers = [r["name"] for r in catalog()]
    out.append("| source | " + " | ".join(renderers) + " |")
    out.append("|---" * (len(renderers) + 1) + "|")
    for name, s in report["sources"].items():
        cells = []
        for r in renderers:
            row = s["renders"][r]
            if row.get("declined"):
                cells.append("—")
            elif not row.get("ok"):
                cells.append("ERR")
            else:
                mark = "" if row["passed"] and not row["issues"] else "!"
                cells.append(f"{row['grounding']:.0%}/{row['coverage']:.0%}/{row['overlap']:.2f}{mark}")
        out.append(f"| {name} | " + " | ".join(cells) + " |")

    out += ["", f"## Issues ({len(report['issues'])})", ""]
    for issue in report["issues"]:
        out.append(f"- {issue}")
    return "\n".join(out)


if __name__ == "__main__":
    rep = run()
    Path("eval_report.json").write_text(json.dumps(rep, indent=2))
    md = render_report(rep)
    Path("eval_report.md").write_text(md)
    print(md)
    sys.exit(0)
