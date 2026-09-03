"""
Parse every diagram the corpus produces with the real mermaid parser.

The engine's own fidelity metrics cannot see this class of defect: a mind map
with two roots is perfectly grounded, covers the source completely, and is
still unrenderable. Only mermaid can tell you that, so ask mermaid.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prism.ingest import ingest  # noqa: E402
from prism.llm import get_client  # noqa: E402
from prism.render import get_renderer  # noqa: E402
from prism.understand import understand  # noqa: E402

CORPUS = Path(__file__).resolve().parents[1] / "examples" / "corpus"
STATIC = Path(__file__).resolve().parents[1] / "prism" / "web" / "static"

MERMAID = STATIC / "mermaid.min.js"


async def main() -> int:
    from playwright.async_api import async_playwright

    sources = sorted(p for p in CORPUS.iterdir() if p.is_file())
    diagrams: list[tuple[str, str, str]] = []
    for path in sources:
        u = understand(ingest(str(path)), client=get_client())
        for mode in ("auto", "flow", "mindmap", "causal", "concept"):
            art = get_renderer("diagram").render(u, mode=mode).artifact
            if art.strip() and not art.startswith("%%"):
                diagrams.append((path.name, mode, art))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        page = await browser.new_page()
        await page.set_content('<!doctype html><meta charset="utf-8">')
        await page.add_script_tag(path=str(MERMAID))
        await page.evaluate("() => window.mermaid.initialize({startOnLoad: false})")
        bad = []
        for name, mode, art in diagrams:
            ok = await page.evaluate(
                "async (src) => { try { await window.mermaid.parse(src); return null; }"
                " catch (e) { return String(e.message || e); } }", art)
            if ok:
                bad.append({"source": name, "mode": mode,
                            "error": ok.splitlines()[0][:160],
                            "head": "\n".join(art.splitlines()[:4])})
        await browser.close()

    print(f"{len(diagrams) - len(bad)}/{len(diagrams)} diagrams parse")
    if bad:
        print(json.dumps(bad, indent=2)[:4000])
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
