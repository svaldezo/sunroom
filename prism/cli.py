"""prism — ingest anything, understand it once, render it in any medium."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import SETTINGS
from .fidelity import check, check_deliverable, citations
from .formats import ask as tutor_ask
from .formats import catalog as format_catalog
from .formats import get_format
from .ingest import ingest, supported
from .llm import get_client
from .models import Medium
from .render import catalog, get_renderer
from .store import open_store
from .understand import understand


def _echo(msg: str = "") -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------

def cmd_add(args) -> int:
    repo = open_store()
    client = get_client(args.provider)
    medium = Medium(args.medium) if args.medium else None

    ing = ingest(args.target, title=args.title, medium=medium)
    if not ing.source.text.strip():
        _echo("error: nothing extractable from that source.")
        return 1

    existing = repo.find_by_checksum(ing.source.checksum)
    if existing and not args.force:
        _echo(f"already ingested, unchanged: {existing.id}  (--force to re-run)")
        print(existing.id)
        return 0

    _echo(f"ingesting {ing.source.title} [{ing.source.medium.value}] "
          f"via {client.name}")
    u = understand(ing, client=client, collection=args.collection, progress=_echo)
    repo.save(u)
    _echo(f"saved {u.id}  ·  {json.dumps(u.stats())}")
    print(u.id)
    return 0


def cmd_ls(args) -> int:
    repo = open_store()
    if args.collections:
        for c in repo.collections():
            print(f"{c['name']:<24} {c['kind']:<12} {c['documents']} doc(s)")
        return 0
    rows = repo.list(args.collection)
    if not rows:
        _echo("nothing ingested yet — try: prism add <file> -c <collection>")
        return 0
    for r in rows:
        print(f"{r['id']}  {r['medium']:<9} {str(r['collection'] or '-'):<14} "
              f"{r['nodes']:>4} nodes  {r['title'][:48]}")
    return 0


def cmd_show(args) -> int:
    repo = open_store()
    u = repo.get(args.id)
    if not u:
        _echo(f"not found: {args.id}")
        return 1
    print(f"{u.source.title}  [{u.source.medium.value}]")
    print(f"collection: {u.collection or '-'}   id: {u.id}")
    print(f"stats: {json.dumps(u.stats())}")
    if u.summary:
        print(f"\n{u.summary}\n")
    for n in u.salient(args.limit):
        print(f"  {n.kind.value:<11} sal={n.salience:.2f} conc={n.concreteness:.2f}  {n.label[:60]}")
    return 0


def cmd_render(args) -> int:
    repo = open_store()
    u = repo.get(args.id)
    if not u:
        _echo(f"not found: {args.id}")
        return 1

    renderer = get_renderer(args.renderer, get_client(args.provider))
    if renderer.tier != "production" and not args.allow_beta:
        _echo(f"warning: '{args.renderer}' is {renderer.tier} tier — "
              f"verify output against the source.")
    options = dict(kv.split("=", 1) for kv in args.option or [])
    for k, v in list(options.items()):
        if v.isdigit():
            options[k] = int(v)

    result = renderer.render(u, **options)
    repo.save_render(result, source_checksum=u.source.checksum)

    report = check(u, result)
    _echo(report.summary())
    for f in report.findings:
        _echo(f"  {f.severity.value:<5} {f.code}: {f.message}")

    if args.out:
        Path(args.out).write_text(result.artifact, encoding="utf-8")
        _echo(f"wrote {args.out}")
    else:
        print(result.artifact)
    return 0 if report.passed else 2


def cmd_make(args) -> int:
    """Produce a named deliverable — the thing a person actually asks for."""
    repo = open_store()
    u = repo.get(args.id)
    if not u:
        _echo(f"not found: {args.id}")
        return 1

    fmt = get_format(args.format, get_client(args.provider))
    options = dict(kv.split("=", 1) for kv in args.option or [])
    for k, v in list(options.items()):
        if v.isdigit():
            options[k] = int(v)

    deliverable = fmt.make(u, **options)
    repo.save_deliverable(deliverable, source_checksum=u.source.checksum)

    report = check_deliverable(u, deliverable)
    _echo(f"{fmt.label}: {len(deliverable.parts)} part(s), "
          f"{len(deliverable.citations)} citation(s)")
    _echo(report.summary())
    for f in report.findings:
        _echo(f"  {f.severity.value:<5} {f.code}: {f.message}")

    if args.out:
        Path(args.out).write_text(deliverable.artifact, encoding="utf-8")
        _echo(f"wrote {args.out}")
    else:
        print(deliverable.artifact)
    return 0 if report.passed else 2


def cmd_formats(args) -> int:
    for row in format_catalog():
        uses = ", ".join(row["uses"]) or "—"
        print(f"  {row['name']:<10} {row['label']:<18} {row['job']}")
        print(f"  {'':<10} components: {uses}")
    return 0


def cmd_ask(args) -> int:
    repo = open_store()
    u = repo.get(args.id)
    if not u:
        _echo(f"not found: {args.id}")
        return 1
    answer = tutor_ask(u, args.question, get_client(args.provider))
    print(answer.text)
    if answer.citations:
        print()
        for c in answer.citations[:3]:
            # Two passages can share a locator, so show the quote as well --
            # otherwise identical-looking lines read as a duplication bug.
            snippet = " ".join(c.quote.split())[:90]
            print(f"  [{c.locator}] “{snippet}”")
            print(f"      {c.anchor}")
    return 0 if answer.covered else 3


def cmd_check(args) -> int:
    repo = open_store()
    u = repo.get(args.id)
    if not u:
        _echo(f"not found: {args.id}")
        return 1
    rows = repo.renders_for(u.id)
    if not rows:
        _echo("no renders stored for this document.")
        return 0
    from .models import RenderResult
    failed = False
    for row in rows:
        raw = repo.conn.execute("SELECT payload FROM renders WHERE id = ?", (row["id"],)).fetchone()
        result = RenderResult.model_validate_json(raw["payload"])
        report = check(u, result)
        print(report.summary())
        for f in report.findings:
            print(f"  {f.severity.value:<5} {f.code}: {f.message}")
        failed = failed or not report.passed
    stale = repo.stale_renders()
    if stale:
        print(f"\n{len(stale)} render(s) stale against a changed source:")
        for s in stale:
            print(f"  {s['renderer']:<12} {s['title'][:50]}")
    return 2 if failed else 0


def cmd_cite(args) -> int:
    repo = open_store()
    u = repo.get(args.id)
    if not u:
        _echo(f"not found: {args.id}")
        return 1
    from .models import RenderResult
    row = repo.conn.execute("SELECT payload FROM renders WHERE id = ?", (args.render,)).fetchone()
    if not row:
        _echo(f"no such render: {args.render}")
        return 1
    result = RenderResult.model_validate_json(row["payload"])
    for c in citations(u, result, args.unit):
        loc = c["locator"] or "-"
        print(f"[{loc}] {c['excerpt']}")
    return 0


def cmd_search(args) -> int:
    repo = open_store()
    hits = repo.search(args.query, collection=args.collection, limit=args.limit)
    if not hits:
        _echo("no matches.")
        return 0
    for h in hits:
        print(f"{h['kind']:<11} {h['title'][:26]:<26} {h['label'][:56]}")
    return 0


def cmd_review(args) -> int:
    repo = open_store()
    due = repo.due(args.collection, limit=args.limit)
    if not due:
        _echo("nothing due.")
        return 0
    for d in due:
        print(f"{d['due_at'][:10]}  lapses={d['lapses']}  {d['label'][:60]}")
    return 0


def cmd_serve(args) -> int:
    try:
        from .web import serve
    except ImportError:
        _echo("web interface needs: pip install fastapi uvicorn")
        return 1
    _echo(f"prism ui → http://{args.host}:{args.port}")
    serve(args.host, args.port)
    return 0


def cmd_media(args) -> int:
    print("FORMATS (what you ask for)")
    for row in format_catalog():
        print(f"  {row['name']:<10} {row['job']}")
    print("\nINPUT")
    for medium, exts in supported().items():
        print(f"  {medium:<10} {', '.join(exts) or '(raw text)'}")
    print("\nCOMPONENTS (what formats are built from)")
    for r in catalog():
        print(f"  {r['tier']:<13} {r['name']:<10} {r['format']:<9} {r['description']}")
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="prism",
        description="Ingest anything, understand it once, render it in any medium.",
    )
    p.add_argument("--provider", choices=["auto", "anthropic", "mock"], default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="ingest a source and build its understanding")
    a.add_argument("target", help="file path, URL, or raw text")
    a.add_argument("-c", "--collection")
    a.add_argument("-t", "--title")
    a.add_argument("-m", "--medium", choices=[m.value for m in Medium])
    a.add_argument("--force", action="store_true", help="re-understand even if unchanged")
    a.set_defaults(func=cmd_add)

    ls_cmd = sub.add_parser("ls", help="list documents or collections")
    ls_cmd.add_argument("-c", "--collection")
    ls_cmd.add_argument("--collections", action="store_true")
    ls_cmd.set_defaults(func=cmd_ls)

    s = sub.add_parser("show", help="inspect a document's IR")
    s.add_argument("id")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.set_defaults(func=cmd_show)

    r = sub.add_parser("render", help="render a document into another medium")
    r.add_argument("id")
    r.add_argument("renderer")
    r.add_argument("-o", "--out")
    r.add_argument("-O", "--option", action="append", metavar="K=V")
    r.add_argument("--allow-beta", action="store_true")
    r.set_defaults(func=cmd_render)

    c = sub.add_parser("check", help="fidelity-check stored renders")
    c.add_argument("id")
    c.set_defaults(func=cmd_check)

    ci = sub.add_parser("cite", help="trace one output unit back to the source")
    ci.add_argument("id")
    ci.add_argument("render")
    ci.add_argument("unit")
    ci.set_defaults(func=cmd_cite)

    se = sub.add_parser("search", help="full-text search across the corpus")
    se.add_argument("query")
    se.add_argument("-c", "--collection")
    se.add_argument("-n", "--limit", type=int, default=25)
    se.set_defaults(func=cmd_search)

    rv = sub.add_parser("review", help="what is due for retrieval practice")
    rv.add_argument("-c", "--collection")
    rv.add_argument("-n", "--limit", type=int, default=25)
    rv.set_defaults(func=cmd_review)

    mk = sub.add_parser("make", help="produce a deliverable in a named format")
    mk.add_argument("id")
    mk.add_argument("format", choices=[r["name"] for r in format_catalog()])
    mk.add_argument("-o", "--out")
    mk.add_argument("-O", "--option", action="append", metavar="K=V")
    mk.set_defaults(func=cmd_make)

    fm = sub.add_parser("formats", help="list the deliverable formats")
    fm.set_defaults(func=cmd_formats)

    ak = sub.add_parser("ask", help="ask the tutor a question about a document")
    ak.add_argument("id")
    ak.add_argument("question")
    ak.set_defaults(func=cmd_ask)

    m = sub.add_parser("media", help="list supported input and output media")
    m.set_defaults(func=cmd_media)

    w = sub.add_parser("serve", help="run the web interface")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "provider", None):
        SETTINGS.provider = args.provider
    try:
        return args.func(args)
    except (ValueError, KeyError, RuntimeError) as exc:
        _echo(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
