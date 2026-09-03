"""
node_select.py -- the ONLY module in this system that may consult a model.

Confining the model to one file is deliberate. 02_BUILD_SPEC states the rule as
a discipline ("the model proposes and the code decides"); this module turns it
into an invariant that cannot be violated by accident:

    select_node() returns a NODE, chosen from a list the code built.
    The model's entire output is an INDEX into that list.

Consequences, each of which is a property rather than a promise:

  1. Zero fabrication surface. The model emits an integer. It cannot emit a
     value, a unit, a verdict, or a span. The worst case is that it picks the
     wrong existing subsection -- a ranking error, never an invented fact.

  2. Never reached on the absence decision. Whether a field is absent is
     decided in validate.py before this is called. The model only ever answers
     "where should this have been stated", never "is it stated". This is the
     structural defence against the failure 02_BUILD_SPEC names: "It tells you
     that the spin was about 16,000 x g ... The model has now erased the gap."
     It cannot erase a gap it is never shown.

  3. Span stability survives model drift. Spans are still derived from sentence
     and section boundaries. The model chooses WHICH node, never where a span
     starts, so finding_id stays stable.

  4. Determinism is inherited, not added. Wrap the client in
     model_cache.CachedModelClient and the second run is a cache hit. The
     cached payload is a small integer, so there is nothing to drift.

  5. model=None is not a degraded mode, it is the same code path. The
     deterministic branch runs first regardless; the model is consulted only
     where that branch is genuinely ambiguous.

On the response format: option 0 means "the methods section as a whole". This is
NOT an escape hatch that lets the model decide absence -- absence is settled
before this runs. It is a structural answer meaning "no subsection is a better
place than any other", and it selects a node the code would have fallen back to
anyway. Withholding it would force the model to over-commit to a subsection on
papers whose headings are junk, which is precisely where it should not.

PROMPT GENERALISATION -- the constraint that shaped this file:

The prompt carries no few-shot examples, no domain hints, and nothing drawn from
the dev papers. Everything interpolated is either machine-derived from the field
pack or read out of the document being analysed. That matters because the
held-out set is a different assay class: a prompt tuned on nine papers would
carry their vocabulary into papers that do not share it.

The subtler risk is the CHOICE SET. Node titles are our own extraction
artifacts, and on this corpus they are frequently merged ("EXPERIMENTAL
PROCEDURES Mice"), truncated at the header pattern's 60-char limit, or promoted
from table content ("Gene Primer Primer sequences Reference"). A titles-only
prompt inherits every one of those defects and degrades to noise on a paper we
parse badly. So each node also carries a short snippet of its own body text:
whatever the heading looks like, the content underneath still describes what the
section is about. This is the cheap version of what PageIndex does when its
index model summarises each node.
"""
from __future__ import annotations

import re
from typing import Optional

from contract import FieldSpec, ModelClient
from sections import Node, match_nodes_by_category

# Cap the candidate list so the prompt stays small and bounded. Papers in this
# corpus carry 2-15 subsections, so this is not currently binding.
_MAX_NODES = 24
_SNIPPET_CHARS = 160

_PROMPT_TEMPLATE = """The detail below is NOT stated anywhere in this methods \
section. Say where a reader would have expected to find it.

{field_block}

Parts of the methods section:
0. the methods section as a whole -- choose this if no part below is clearly \
the right place
{node_list}

Reply with the number only."""

_FIRST_INT = re.compile(r"\d+")
_WHITESPACE = re.compile(r"\s+")


def _describe_field(spec: FieldSpec) -> str:
    """Everything the pack knows about this field, machine-derived.

    13 of the 27 fields carry no `description`, which left a third of calls
    showing the model nothing but a dotted id. FieldSpec already holds
    dimension, canonical_unit, enum_values and provenance; none of it was
    reaching the prompt. Reading those out is free, needs no hand-authored text,
    and works for any pack -- unlike writing 13 descriptions ourselves, which
    would be content fitted to this particular pack.
    """
    lines = [
        f"Detail: {spec.field_id.replace('.', ' ').replace('_', ' ')}",
        f"Field id: {spec.field_id}",
        f"Kind: {spec.type}",
    ]
    if spec.dimension:
        unit = f", normally reported in {spec.canonical_unit}" if spec.canonical_unit else ""
        lines.append(f"Quantity of: {spec.dimension}{unit}")
    if spec.enum_values:
        lines.append(f"Expected one of: {', '.join(spec.enum_values)}")
    if spec.provenance:
        cited = ", ".join(
            f"{p.get('guideline', '?')} item {p.get('item', '?')}"
            for p in spec.provenance
        )
        lines.append(f"Required by: {cited}")
    description = _WHITESPACE.sub(" ", (spec.description or "")).strip()
    if description:
        lines.append(f"Note: {description}")
    return "\n".join(lines)


def _snippet(node: Node, canonical_text: str | None) -> str:
    """First words of a node's body, as a hedge against a corrupted title."""
    if canonical_text is None:
        return ""
    body = _WHITESPACE.sub(" ", canonical_text[node.start:node.end]).strip()
    if not body:
        return ""
    if len(body) <= _SNIPPET_CHARS:
        return body
    cut = body[:_SNIPPET_CHARS]
    return cut[:cut.rfind(" ")] + " ..." if " " in cut else cut + " ..."


def _build_prompt(
    spec: FieldSpec,
    nodes: list[Node],
    canonical_text: str | None = None,
) -> str:
    entries = []
    for index, node in enumerate(nodes, start=1):
        entries.append(f"{index}. {node.title}")
        snippet = _snippet(node, canonical_text)
        if snippet:
            entries.append(f"   {snippet}")
    return _PROMPT_TEMPLATE.format(
        field_block=_describe_field(spec),
        node_list="\n".join(entries),
    )


def _parse_index(response: str, n_nodes: int) -> Optional[int]:
    """First integer in the response, bounds-checked against 0..n_nodes.

    Returns -1 for the "methods section as a whole" option, otherwise a 0-based
    index into the node list. Anything unparseable or out of range returns None
    and the caller falls back to its deterministic choice. The raw string is
    never trusted.
    """
    if not response:
        return None
    match = _FIRST_INT.search(response)
    if match is None:
        return None
    value = int(match.group(0))
    if value == 0:
        return -1
    if 1 <= value <= n_nodes:
        return value - 1
    return None


def select_node(
    spec: FieldSpec,
    nodes: list[Node],
    model: Optional[ModelClient] = None,
    canonical_text: Optional[str] = None,
) -> tuple[Optional[Node], str]:
    """Pick the subsection where `spec` should have been stated.

    Returns (node, detector). None means "no subsection is better than the
    section as a whole"; the caller then anchors at the parent level. The
    detector records how the choice was made, so every model-influenced finding
    is auditable from the output alone.
    """
    # Fewer than two options is not a choice. Never spend a call on it.
    if not nodes:
        return None, "node:no-subsections"
    if len(nodes) == 1:
        return nodes[0], "node:only-one"

    matched = match_nodes_by_category(nodes, spec.field_id)

    # Unambiguous deterministic hit -- do not spend a call on it.
    if len(matched) == 1:
        return matched[0], "node:det-unique"

    # Ambiguous (several candidates) or blind (none). Both are cases where the
    # keyword table is known to be weak, and both are where a model helps.
    if model is None:
        if matched:
            return matched[0], f"node:det-first-of-{len(matched)}"
        return None, "node:det-none"

    candidates = nodes[:_MAX_NODES]
    prompt = _build_prompt(spec, candidates, canonical_text)
    try:
        response = model.complete(prompt, max_tokens=8)
    except Exception:
        # A model failure must never take down a deterministic pipeline.
        return (matched[0], "node:model-error-fellback") if matched else (None, "node:model-error")

    index = _parse_index(response, len(candidates))
    if index is None:
        return (matched[0], "node:model-unparsed") if matched else (None, "node:model-unparsed")
    if index == -1:
        return None, "node:model-whole-section"

    return candidates[index], f"node:model-{index + 1}of{len(candidates)}"
