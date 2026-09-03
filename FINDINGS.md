# Evaluation findings

Eight synthetic sources across six formats × seven renderers = 56 conversions,
scored on grounding, coverage, drift, and a set of quality probes the fidelity
checker does not cover.

**Baseline: 20 defects. After fixes: 0.** Test suite grew 31 → 35, all passing.

| metric | before | after |
|---|--:|--:|
| Flashcards containing their own answer | 40 | 0 |
| Circular glossary entries | 25 | 0 |
| Orphan nodes (worst source) | 90% | 0% |
| Spans with a human-readable citation (worst source) | 0% | 100% |
| Sources whose diagram was disconnected boxes | 3 | 0 |
| Renderer/source pairs with quality issues | 20 | 0 |

---

## What the harness measures that the fidelity checker cannot

The checker answers *"is this traceable?"*. It said **100% grounding on every
one of the 56 conversions from the very first run** — and the output was still
full of defects. A perfectly grounded flashcard that reveals its own answer is
correctly cited and useless.

So the harness probes for utility: answer leakage, circular definitions, cloze
items with no blank, duplicate units, disconnected diagrams, thin narration
segments, orphaned nodes, label quality, citation coverage.

**This is the single most transferable result.** Provenance is necessary and
nowhere near sufficient, and the two need separate instruments.

---

## The defects, and what each one taught

### 1. One bad node type broke three renderers at once
The extractor labelled *"The return is expected, but the timing is left open"*
as a DEFINITION with the term **"The return"**. Downstream: the glossary made a
circular entry, the deck made a card containing its answer, the diagram made a
box labelled with a sentence fragment.

None of the renderers was wrong. **The IR was.** The fix is a validation gate at
the extraction boundary (`understand/validate.py`) that rejects clause-opener
terms and demotes bad definitions to claims. One guard, every renderer
protected — including ones not written yet.

*This is the architectural argument for the IR, demonstrated rather than
asserted.*

### 2. Unstructured prose had the worst citations
Plain text with no headings produced **zero** sections, so every citation
degraded to a byte offset (`chars 1180-1240`) and 90% of nodes were unlinked.
The most common input format had the least usable output.

Fix: synthesize paragraph blocks as citation anchors, add a preamble section for
text before the first heading, and never let a raw offset reach a user.
→ locator coverage 0% → 100%, orphans 90% → 0%.

### 3. Physical structure was masquerading as argument structure
PDF pages and transcript timestamps were being treated as an outline, so mind
maps were rooted on *"Page 1 / Page 2 / Page 3"* — the container, not the
content.

Fix: `Section.physical` distinguishes the two. Physical divisions make
excellent citations and terrible hierarchies. Then term-mention linking gives
every document a concept graph independent of its container.

Best result of the round: citations now read **`p. 3 · § 4.2 The Kula Ring`** —
the page tells you where to look, the heading tells you where you are.

### 4. A sentence splitter cost a whole renderer
`(?<![0-9])(?<=[.!?])` looks like it protects "1." — it does not. Both
lookbehinds anchor at the *same* position, so neither sees the digit. Numbered
procedures were shredded, no STEP nodes were produced, and every flowchart
silently degraded to a mind map.

Nothing failed. Nothing warned. The output was just quietly worse. **Found only
because the harness compared diagram mode against document shape.**

### 5. Refusing beats fabricating
Documents with no definitions still got a glossary — built from section
headings, whose "definition" restated the heading. The renderer now declines
with a reason and a suggestion.

A medium that does not fit the content should say so. In a tool whose users
convert media *because they cannot evaluate the content*, fabrication is the
worst possible failure.

### 6. Scaffolding leaked to the learner
A synthesized process node named for the *filename* produced the card
*"The ______ described under '______'."* — nonsense, perfectly grounded.
`Node.is_scaffold` now keeps structural bookkeeping out of learner-facing
output, and processes are named for their section.

### 7. Coverage is renderer-relative
A flowchart carrying 44% of a document is on target; a summary carrying 44% is
broken. Each renderer now declares `coverage_target`, and the UI colors the
chip against it. A fixed bar was generating noise, and noisy warnings get
ignored — which is how a real one gets missed.

---

## UI defects (found by driving the browser)

1. **A blocked CDN took the entire app down.** Mermaid failed to load, and
   `mermaid.initialize()` at the top of the script aborted the whole block —
   blank interface, no corpus, no error. Mermaid is now vendored locally *and*
   feature-detected. **An optional dependency must never be able to break the
   required path.**
2. **`scrollIntoView()` walks every scrollable ancestor** and dragged the medium
   tabs and fidelity bar off-screen on every click. Replaced with explicit
   `scrollTop` on one pane.
3. Diagram units displayed raw Mermaid source; now show the nodes they draw.
4. Card fronts carried markdown asterisks into a plain-text surface.
5. Documents were titled by filename (*"procedural"*) instead of their own H1
   (*"Flotation Protocol for Archaeobotanical Samples"*).

---

## Honest limits of this round

- **No API key was available, so extraction ran on the heuristic mock.** What is
  validated here is the *pipeline*: ingest, structure, linking, rendering,
  fidelity, UI. IR **quality** under a real model is untested and remains the
  primary risk.
- Several fixes improved the mock extractor itself. That is not wasted — it is
  the offline fallback path — but a frontier model will surface a different
  defect distribution, probably subtler.
- Drift detection is still lexical. It catches invention, not distortion.
- The corpus is synthetic and small (8 sources, ~1.5k chars each). No
  multi-chunk documents, no scanned PDFs, no non-English text, no adversarial
  or contradictory sources.

## Next round

1. Run the same harness with `ANTHROPIC_API_KEY` set and diff the defect
   distribution. The mock's failures were structural; the model's will be
   semantic.
2. Add a real long document (50+ pages) to exercise chunking and cross-chunk
   merge — currently the least tested path.
3. Replace lexical drift with entailment checking.
4. Add contradictory sources to one collection; cross-document merge is
   unimplemented and that is the interesting case.


---

# Round 2 — attribution and the full UI

Adding real source links and driving all five views surfaced five more defects,
two of them serious.

| defect | severity | why it mattered |
|---|---|---|
| SQLite connection shared across request threads | **critical** | Every API call failed the moment two arrived on different worker threads. Worked in testing only because early requests happened to reuse one thread. |
| Review queue bypassed the renderer | **serious** | Cards read `label`/`body` straight off the IR node. A node's label is a prefix of its body, so **every card in the review session showed its own answer** — the exact bug fixed in round 1, reappearing through a second code path. |
| Section spans swallowed sentence spans | major | The Reader highlighted 3 passages in a document with 23 cited ones: a page-level span covered everything inside it. Fixed by highlighting narrowest-first. |
| Mermaid injected a full-page error graphic into `<body>` | major | A parse failure escaped its container and covered the app. `mermaid.parse()` validates without side effects. |
| Diagram labels rewrote `[` as `(` | major | Traded one Mermaid shape token for another and broke every mind map. |
| Reverse trace showed each output once per historical render | minor | Five stored diagrams meant the same unit listed five times. |

## The lesson worth keeping

**The same defect class reappeared through a different code path.** Round 1
fixed answer leakage in the retrieval renderer. Round 2 found it again in the
review queue, which read the IR directly instead of going through the renderer
— it looked equivalent and was not.

The fix is architectural rather than another patch: **every learner-facing
surface goes through a renderer.** The IR is the source of truth for meaning;
the renderer is the source of truth for how meaning is presented. A view that
reaches past the renderer will re-derive every mistake the renderer already
solved.

## What attribution added

- `prism/cite.py` — medium-specific anchors (PDF page, W3C text fragment,
  media timestamp, line number), each a dereferenceable standard
- Citations indexed once per rendering in the renderer base class, so every
  medium gets footnotes the same way rather than each reinventing it
- Reverse tracing: source passage → nodes → every output across all media
- Export as Markdown, BibTeX, CSL-JSON, Anki (with live links in the cards)

Test suite 35 → 46. Evaluation harness still reports **0 issues**.


---

# Round 3 — formats

The seven renderers were components, not products. This round added the layer a
person actually chooses from, and found three more defects — one of which had
been silently degrading output since round 1.

| defect | severity | why it mattered |
|---|---|---|
| Heading/definition merge marked content nodes as scaffolding | **serious** | Merging "## Structuration" with its definition set `section: True`, and every renderer filters scaffolding — so the definition vanished from the glossary, the card deck, and the tutor. The tutor answered "this source does not cover that" about a term the source defines in a heading. |
| Instructional text failed the drift check | major | "Closed notes — let them struggle before you help" shares no vocabulary with the source, so every lesson and activity warned. The fix is a real distinction, not a threshold: a part either **asserts something about the source** (drift applies) or **tells you what to do with it** (it must still cite, but drift does not apply). |
| Ungrounded discussion prompts | minor | When a source raises no explicit questions, the lesson emitted a generic prompt citing nothing. Now it falls back to contested claims, then to the most abstract ones. |

## What the format layer proved

**Composition surfaced a bug that seven renderers had hidden.** No renderer was
individually wrong about scaffolding — each filtered it correctly. It took a
format asking "what can this document teach?" for the missing definitions to
become visible.

**The asserts/scaffolding distinction is the interesting one.** A deliverable is
not uniformly a claim about its source. A lesson plan mixes quoted claims with
teaching instructions; an activity mixes source content with task framing. Both
must cite what they are about; only one can be checked for vocabulary drift.
Without that distinction the checker either fires constantly or is turned off —
and a check people turn off is worse than no check.

## State

- **7 formats × 8 sources = 56 deliverables**, 100% grounding on every one
- Test suite 46 → **70**
- Evaluation harness: **0 issues** across formats and components
