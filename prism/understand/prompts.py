"""Extraction prompts and the JSON schemas the model is forced into."""

EXTRACT_SYSTEM = """You extract the semantic structure of a source document into a graph.

You are NOT summarizing. You are decomposing meaning into addressable units so
the same content can later be rendered as audio, diagrams, images, or practice
questions without going back to the original text.

Rules:
- Every node must be grounded: `quote` MUST be an exact, verbatim substring of
  the provided text. Never paraphrase inside `quote`. If you cannot quote it,
  do not emit the node.
- `body` is a self-contained restatement that makes sense with no surrounding
  context. A reader who sees only `body` must understand the point.
- Prefer several precise nodes over one broad one.
- `concreteness` drives whether a visual renderer will try to depict this.
  1.0 = a physical, picturable thing. 0.0 = a purely abstract relation.
  Be honest: a bad illustration of an abstract idea teaches the wrong thing.
- `salience` is centrality to THIS source, not general importance.
- Emit edges only where the text actually asserts the relation.

Node kinds: concept, definition, claim, process, step, example, quantity, event, question
Relations: is_a, part_of, defines, exemplifies, causes, enables, precedes,
contrasts_with, supports, contradicts, depends_on, measures, elaborates"""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Local id, e.g. n1"},
                    "kind": {"type": "string", "enum": [
                        "concept", "definition", "claim", "process", "step",
                        "example", "quantity", "event", "question"]},
                    "label": {"type": "string", "description": "Short handle, max 8 words"},
                    "body": {"type": "string", "description": "Self-contained statement"},
                    "quote": {"type": "string", "description": "EXACT verbatim substring of the source"},
                    "salience": {"type": "number"},
                    "difficulty": {"type": "number"},
                    "concreteness": {"type": "number"},
                    "confidence": {"type": "number"},
                    "order": {"type": "integer", "description": "Position, for steps only"},
                },
                "required": ["ref", "kind", "label", "body", "quote"],
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "relation": {"type": "string", "enum": [
                        "is_a", "part_of", "defines", "exemplifies", "causes",
                        "enables", "precedes", "contrasts_with", "supports",
                        "contradicts", "depends_on", "measures", "elaborates"]},
                    "confidence": {"type": "number"},
                },
                "required": ["source", "target", "relation"],
            },
        },
    },
    "required": ["nodes"],
}

EXTRACT_PROMPT = """Extract the semantic graph from this excerpt of "{title}" ({medium}).

<text>
{text}
</text>

Remember: every `quote` must appear verbatim in the text above."""

MERGE_SYSTEM = """You are consolidating a semantic graph extracted chunk-by-chunk
from one document. The same idea often appears in several chunks under different
wording. Identify which nodes refer to the SAME underlying unit of meaning.

Merge only true duplicates. Two claims about the same topic are not duplicates
unless they assert the same thing. When in doubt, keep them separate."""

MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "canonical": {"type": "string"},
                    "duplicates": {"type": "array", "items": {"type": "string"}},
                    "label": {"type": "string"},
                },
                "required": ["canonical", "duplicates"],
            },
        },
    },
    "required": ["groups"],
}
