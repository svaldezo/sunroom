"""
Fidelity checking.

The differentiator. A user converts a medium precisely because they cannot yet
evaluate the content -- which means they cannot catch an error in the output.
So correctness has to be structural, not vibes.

Four checks:
  1. GROUNDING  -- every unit names IR nodes that resolve to real source spans.
  2. COVERAGE   -- how much of the source's salient content survived.
  3. DRIFT      -- lexical overlap between a unit and the spans it claims.
                   Cheap, catches whole-cloth invention, not subtle distortion.
  4. STALENESS  -- was this rendered against the current version of the source.

Check 3 is deliberately a floor, not a proof. Semantic entailment checking is
the obvious next layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..models import RenderResult, Understanding

WORD = re.compile(r"[a-z0-9']+")
STOP = {
    "the", "a", "an", "of", "and", "or", "to", "in", "is", "are", "was", "were",
    "that", "this", "it", "as", "for", "on", "with", "by", "at", "from", "be",
    "not", "but", "they", "their", "its", "which", "than", "then", "when", "you",
    "we", "can", "will", "would", "there", "what", "how", "so", "if", "into",
}


def _tokens(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 2}


class Severity(str, Enum):
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


@dataclass
class Finding:
    severity: Severity
    code: str
    message: str
    unit_id: Optional[str] = None
    detail: dict = field(default_factory=dict)


@dataclass
class FidelityReport:
    renderer: str
    total_units: int
    grounded_units: int
    coverage: float                 # 0-1 of salient IR nodes represented
    mean_overlap: float             # 0-1 lexical overlap with cited spans
    stale: bool
    findings: list[Finding] = field(default_factory=list)

    @property
    def grounding(self) -> float:
        return self.grounded_units / self.total_units if self.total_units else 0.0

    @property
    def passed(self) -> bool:
        return not any(f.severity is Severity.ERROR for f in self.findings)

    def summary(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return (f"[{mark}] {self.renderer}: grounding {self.grounding:.0%} "
                f"({self.grounded_units}/{self.total_units}) · "
                f"coverage {self.coverage:.0%} · overlap {self.mean_overlap:.2f}"
                + (" · STALE" if self.stale else ""))


def check(u: Understanding, result: RenderResult, *,
          drift_floor: float = 0.12, coverage_floor: Optional[float] = None,
          salient_n: int = 20) -> FidelityReport:
    # each renderer declares what share of the document it is meant to carry
    if coverage_floor is None:
        coverage_floor = float(result.meta.get("coverage_target", 0.25))
    findings: list[Finding] = []
    grounded = 0
    overlaps: list[float] = []
    covered: set[str] = set()

    for unit in result.units:
        if not unit.derived_from:
            findings.append(Finding(
                Severity.ERROR, "ungrounded",
                "Unit cites no IR nodes — nothing in the source supports it.",
                unit.id, {"content": unit.content[:160]},
            ))
            continue

        resolved = [nid for nid in unit.derived_from if u.node(nid)]
        missing = [nid for nid in unit.derived_from if not u.node(nid)]
        if missing:
            findings.append(Finding(
                Severity.ERROR, "dangling_node",
                f"Unit references {len(missing)} node id(s) not in the IR.",
                unit.id, {"missing": missing[:5]},
            ))
        if not resolved:
            continue

        spans = u.spans_for(resolved)
        if not spans:
            findings.append(Finding(
                Severity.ERROR, "no_provenance",
                "Unit's nodes have no source spans — provenance chain is broken.",
                unit.id, {"nodes": resolved[:5]},
            ))
            continue

        grounded += 1
        covered.update(resolved)

        # drift: does the output share vocabulary with what it claims to cite?
        # Instructional scaffolding is exempt -- it legitimately introduces
        # words the source never used ("closed notes", "let them struggle").
        if unit.meta.get("asserts") is False:
            continue
        cited = " ".join(s.excerpt(u.source, 600) for s in spans)
        answer = str(unit.meta.get("answer", ""))
        produced = f"{unit.content} {answer}"
        pt, ct = _tokens(produced), _tokens(cited)
        if pt:
            overlap = len(pt & ct) / len(pt)
            overlaps.append(overlap)
            if overlap < drift_floor and len(pt) > 6:
                findings.append(Finding(
                    Severity.WARN, "possible_drift",
                    f"Only {overlap:.0%} of this unit's vocabulary appears in the "
                    f"source it cites. Verify before trusting.",
                    unit.id, {"overlap": round(overlap, 3),
                              "content": unit.content[:160],
                              "cited": cited[:200]},
                ))

    salient = {n.id for n in u.salient(salient_n) if not n.meta.get("section")}
    coverage = len(salient & covered) / len(salient) if salient else 1.0
    if coverage < coverage_floor:
        dropped = [n.label for n in u.salient(salient_n)
                   if n.id in salient - covered][:6]
        findings.append(Finding(
            Severity.WARN, "low_coverage",
            f"Only {coverage:.0%} of the document's most salient points made it "
            f"into this rendering.",
            None, {"omitted": dropped},
        ))

    stale = bool(result.meta.get("source_checksum")
                 and result.meta["source_checksum"] != u.source.checksum)
    if stale:
        findings.append(Finding(
            Severity.WARN, "stale",
            "The source has changed since this was rendered. Re-render to resync.",
        ))

    if not result.units:
        findings.append(Finding(
            Severity.ERROR, "empty", "Renderer produced no output units."))

    return FidelityReport(
        renderer=result.renderer,
        total_units=len(result.units),
        grounded_units=grounded,
        coverage=round(coverage, 4),
        mean_overlap=round(sum(overlaps) / len(overlaps), 4) if overlaps else 0.0,
        stale=stale,
        findings=findings,
    )


def check_deliverable(u: Understanding, deliverable, **kw) -> FidelityReport:
    """
    Run the same grounding, drift and coverage checks over a format.

    A deliverable is parts rather than units, but the contract is identical:
    anything shown to a person must name the IR nodes it came from.
    """
    from ..models import RenderResult, RenderUnit

    units = [
        RenderUnit(id=p.id, kind=p.role, content=f"{p.title}\n{p.body}",
                   derived_from=p.derived_from,
                   meta={**p.meta, "asserts": p.asserts})
        for p in deliverable.parts
        # timing/boundary parts address the reader, not the source
        if p.role not in ("timing", "boundary")
    ]
    shim = RenderResult(
        understanding_id=u.id, renderer=deliverable.format, tier=deliverable.tier,
        format=deliverable.artifact_format, units=units,
        artifact=deliverable.artifact,
        meta={"source_checksum": deliverable.meta.get("source_checksum"),
              "coverage_target": deliverable.meta.get("coverage_target", 0.25)},
    )
    return check(u, shim, **kw)


def citations(u: Understanding, result: RenderResult, unit_id: str) -> list[dict]:
    """Resolve one output unit all the way back to quotable source text."""
    unit = next((x for x in result.units if x.id == unit_id), None)
    if not unit:
        return []
    return [{
        "locator": s.locator or f"chars {s.start}-{s.end}",
        "t_start": s.t_start,
        "excerpt": s.excerpt(u.source),
        "source": u.source.title,
        "uri": u.source.uri,
    } for s in u.spans_for(unit.derived_from)]
