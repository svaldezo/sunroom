"""
IR validation — the quality gate between extraction and everything downstream.

Evaluation finding: a bad *term* poisons three renderers at once. When the
extractor labelled "The return is expected, but the timing is left open" as a
DEFINITION with the term "The return", the glossary produced a circular entry
and the flashcard deck produced a card containing its own answer.

Neither renderer was wrong. The IR was. So the guard belongs here, at the
boundary, where it protects every current and future renderer -- not patched
into each one. This runs regardless of provider, because a frontier model
emits junk terms too, just less often.
"""
from __future__ import annotations

import re

from ..models import Node, NodeKind

# A definition's term must be a NAME, not a clause opener.
BAD_TERM_START = re.compile(
    r"^(the|a|an|this|that|these|those|it|they|he|she|we|you|his|her|their|its|"
    r"and|but|or|so|then|because|since|although|while|if|when|where|which|who|"
    r"there|here|such|some|any|each|every|most|many|both|one|two|first|second|"
    r"finally|next|also|however|therefore|thus|roughly|about|approximately|"
    r"neither|either|none|nothing|anything|something|everything|no|not|"
    r"my|our|its|whose|what|why|how|whether|instead|rather|still|yet)\b",
    re.I,
)
PRONOUN_ONLY = re.compile(r"^(it|they|this|that|these|those|he|she|we|you)$", re.I)
MARKUP = re.compile(r"[#*_`|>\[\]]")
SENTENCE_END = re.compile(r"[.!?]\s+\S")


def _is_valid_term(term: str) -> bool:
    t = term.strip().strip(".,;:—-").strip()
    if not t or len(t) < 3:
        return False
    words = t.split()
    if len(words) > 5:                      # a term is a handle, not a clause
        return False
    if PRONOUN_ONLY.match(t):
        return False
    if BAD_TERM_START.match(t):
        return False
    if SENTENCE_END.search(t):              # spans a sentence boundary
        return False
    if sum(c.isalpha() for c in t) < len(t) * 0.6:
        return False
    return True


def _is_junk(node: Node) -> bool:
    """Nodes that are markup artifacts rather than content."""
    body = (node.body or node.label).strip()
    if len(body) < 12:
        return True
    markup_ratio = len(MARKUP.findall(body)) / max(len(body), 1)
    if markup_ratio > 0.06:                 # heading soup, table pipes, fences
        return True
    letters = sum(c.isalpha() for c in body)
    if letters < len(body) * 0.55:
        return True
    return False


def _handle(body: str) -> str:
    """A short, readable handle for a node that has no proper name."""
    text = re.sub(r"\s+", " ", MARKUP.sub("", body)).strip()
    words = text.split()
    handle = " ".join(words[:9]).rstrip(".,;:")
    return re.sub(r"\s+(and|but|or|so|because|which|that|the|a|an|of|to|in|is|are|"
                  r"with|for|from|by|as)$", "", handle, flags=re.I).strip() or text[:80]


def _tidy_label(label: str, body: str) -> str:
    """A label is a handle. Strip markup and trailing connectives."""
    lbl = MARKUP.sub("", label).strip().strip(".,;:—-").strip()
    lbl = re.sub(r"\s+", " ", lbl)
    lbl = re.sub(r"\s+(and|but|or|so|because|which|that|the|a|an|of|to|in|is|are)$", "",
                 lbl, flags=re.I).strip()
    if not lbl:
        lbl = re.sub(r"\s+", " ", MARKUP.sub("", body)).strip()[:80]
    return lbl[:120]


#: Leading markup that belongs to the document's formatting, not its meaning.
MARKUP = re.compile(r"^\s*(?:#{1,6}\s+|>\s+|[-*+]\s+|\d+[.)]\s+)")


def _strip_markup(node: Node) -> bool:
    """Remove leading markup from a node's label and body, in place."""
    changed = False
    for field in ("label", "body"):
        value = getattr(node, field) or ""
        cleaned = MARKUP.sub("", value).strip()
        # A list marker carries order, which the STEP nodes rely on, so only
        # strip it where it is decoration: headings and quotes.
        if node.kind is NodeKind.STEP and cleaned != value.strip():
            continue
        if cleaned and cleaned != value:
            setattr(node, field, cleaned)
            changed = True
    return changed


def validate(nodes: list[Node]) -> tuple[list[Node], dict[str, int]]:
    """
    Returns (cleaned nodes, counts of what was corrected).

    Definitions with an unusable term are DEMOTED to claims rather than dropped
    -- the content is still true, it just isn't a definition. Dropping it would
    lose coverage; keeping it as a definition would poison the glossary.
    """
    stats = {"dropped_junk": 0, "demoted_definitions": 0, "relabelled": 0,
             "demarked": 0}
    kept: list[Node] = []

    for node in nodes:
        # Markup is how the source was written, not part of what it says. A
        # node whose body starts "## Forms of reciprocity" puts those hashes in
        # every brief, card front and diagram label downstream.
        if _strip_markup(node):
            stats["demarked"] += 1

        if _is_junk(node):
            stats["dropped_junk"] += 1
            continue

        if node.kind is NodeKind.DEFINITION and not _is_valid_term(node.label):
            # The label WAS the bogus term ("That loss"), so it has to be
            # rebuilt from the body -- keeping it would leave a claim whose
            # handle is a sentence fragment, which then leaks into diagrams,
            # slides, and card fronts.
            node.kind = NodeKind.CLAIM
            node.label = _handle(node.body or node.label)
            stats["demoted_definitions"] += 1

        tidy = _tidy_label(node.label, node.body)
        if tidy != node.label:
            node.label = tidy
            stats["relabelled"] += 1

        # a definition's body should be the definiens, not the whole sentence
        if node.kind is NodeKind.DEFINITION:
            node.meta["definiens"] = definiens(node.label, node.body)

        kept.append(node)

    return kept, stats


DEFINIENS = re.compile(
    r"^\s*(?P<term>.{2,60}?)\s+(?:is|are|was|were|refers to|means|denotes|"
    r"is defined as|is the|is a|is an)\s+(?P<rest>.+)$",
    re.I | re.S,
)


def definiens(term: str, body: str) -> str:
    """
    Strip the 'X is ...' lead-in so a glossary entry reads as a definition
    rather than restating the term back at the learner.
    """
    body = body.strip()
    m = DEFINIENS.match(body)
    if m and m.group("term").strip().lower().rstrip(".") in (
        term.strip().lower(), f"the {term.strip().lower()}"
    ):
        rest = m.group("rest").strip()
        return rest[0].upper() + rest[1:] if rest else body
    return body
