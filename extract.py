"""
extract.py -- Step 3 (candidate extraction) and Step 5 (location).

find_candidate() proposes a span for a field that IS stated; find_absence_anchor()
locates where a reader should look for one that is not. Both derive spans from
sentence and section boundaries only, never from free-form model output, which is
what makes finding_id stable across runs.

REAL BUG FOUND AND FIXED: relevance-checking was only happening in
validate.py, AFTER extraction had already committed to one candidate.
This meant rejecting a bad top-scoring candidate could never recover a
genuinely correct one sitting in the same subsection with a slightly
lower keyword score. Confirmed on Dos et al's cellline.source_or_rrid:
the real ATCC/#CRL-2505 sentence exists in the exact subsection searched,
but a different sentence scored marginally higher on generic keywords and
was chosen instead, then correctly rejected by validation with nothing
to fall back to. Fixed by ranking ALL candidate sentences by score, then
trying them in order until one passes relevance (for fields with a
defined marker set), instead of committing to the single top scorer.
"""
from __future__ import annotations

import re

from contract import FieldSpec, Span
from sections import build_tree, find_subsections, find_relevant_subsections
from node_select import select_node
from vagueness import get_trigger_lexicon
from relevance import check_relevance

# Methods-section boundaries.
#
# These were previously uppercase literals matched case-sensitively, which found
# a heading in only 3 of the 9 dev papers -- the other 6 use mixed case
# ("Materials and Methods") and silently fell back to searching the whole
# document, title block and reference list included.
#
# Case-insensitivity alone is not enough, because the bare word also occurs in
# running prose ("methods described earlier (Visvanathan et al., 2017)") and in
# figure legends ("as per the protocol given under 'Materials and Methods.'").
# So a heading must occupy a line of its own, optionally numbered and optionally
# followed by a colon. That rejects both without needing a per-paper exception.
_METHODS_HEADERS = [
    r"experimental procedures?",
    r"materials and methods",
    r"methods and materials",
    r"methods",
]

# Enumerating heading forms is a generalisation trap: the list above misses
# "Patients and Methods" (standard in clinical oncology), "Online Methods"
# (Nature family), "Materials & Methods", "Subjects and Methods",
# "Experimental Section" and "Methodology". Rather than growing the list one
# journal at a time, fall back to a SHAPE rule -- a short line, optionally
# numbered, whose head ends in a methods-ish word. Tried only after the
# specific forms above, so precision is unaffected when they match.
_METHODS_HEADER_GENERIC = (
    r"^[ \t]*(?:\d+(?:\.\d+)*\.?[ \t]*)?"
    r"(?:[A-Za-z&][A-Za-z& ]{0,44})?"
    r"\b(?:methods?|methodology|experimental[ \t]+(?:procedures?|section))\b"
    r"[ \t]*:?[ \t]*$"
)

# A structured abstract can carry a bare "Methods" heading on its own line.
# Requiring the resulting window to hold real content rejects it: an abstract's
# methods block runs a few hundred characters before "Results", whereas the
# smallest true Methods section in the dev set is 3,123 characters.
_MIN_METHODS_CHARS = 400

# Any of these legitimately terminates Methods. "results" is included because
# most journals order Methods before Results, and without it the Methods window
# swallowed the entire Results section -- which is how figure-legend and
# results-narrative sentences were reaching the candidate pool.
_METHODS_END_HEADERS = [
    r"results and discussion",
    r"results",
    r"discussion",
    r"references",
    r"bibliography",
    r"acknowledge?ments?",
    r"conflicts? of interest",
    r"author contributions?",
    r"data availability",
    r"supplementary (?:material|information|data)",
]


def _heading_pattern(word: str) -> str:
    """A heading alone on its line: optional numbering, optional trailing colon."""
    return rf"^[ \t]*(?:\d+(?:\.\d+)*\.?[ \t]*)?{word}[ \t]*:?[ \t]*$"


def find_methods_section(text: str) -> tuple[int, int]:
    """Locate the Methods section as (start, end) offsets into canonical text.

    Falls back to the whole document when no heading is found. That is the
    honest answer for a paper whose structure we cannot read, but it is an
    expensive miss: every absence anchor for that paper then degrades toward the
    title block, so the fallback firing is worth logging in a diagnostic pass.
    """
    patterns = [_heading_pattern(w) for w in _METHODS_HEADERS]
    patterns.append(_METHODS_HEADER_GENERIC)

    # Ordered, not min(): specific headings take precedence over the generic
    # shape rule, so a paper containing both resolves to the specific one.
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            start = m.start()
            end = _find_section_end(text, start)
            if end - start >= _MIN_METHODS_CHARS:
                return start, end
    return 0, len(text)


def _find_section_end(text: str, start: int) -> int:
    """Earliest terminator after `start`, or end of document.

    min(), not list order: any of these legitimately terminates Methods, so the
    nearest one wins. The old code returned the first pattern in list order,
    which gave the wrong boundary for a paper whose References precede its
    Discussion.
    """
    ends = []
    for word in _METHODS_END_HEADERS:
        m = re.search(_heading_pattern(word), text[start:], re.IGNORECASE | re.MULTILINE)
        if m and m.start() > 0:
            ends.append(start + m.start())
    return min(ends) if ends else len(text)


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def split_sentences(text: str, base_offset: int = 0) -> list[tuple[str, int, int]]:
    sentences = []
    pos = 0
    for part in _SENTENCE_SPLIT.split(text):
        start = text.find(part, pos)
        if start == -1:
            start = pos
        end = start + len(part)
        sentences.append((part, base_offset + start, base_offset + end))
        pos = end
    return sentences


_COMPOUND_KEYWORD_REMAP = {
    "cellline": ["cell line", "cell lines"],
}


def field_keywords(field_id: str) -> list[tuple[str, int]]:
    parts = re.split(r"[._]", field_id)
    weighted = []
    for p in parts:
        if not p:
            continue
        if p in _COMPOUND_KEYWORD_REMAP:
            for kw in _COMPOUND_KEYWORD_REMAP[p]:
                weighted.append((kw, 1))
        else:
            weighted.append((p, 2))
    return weighted


_HAS_DIGIT = re.compile(r"\d")


def _ranked_candidates(sentences, weighted_keywords, spec):
    """Returns ALL scoring candidates, sorted highest-score first, not
    just the single winner. This is what lets extraction try a
    second-best candidate when the top one fails relevance.
    """
    scored = []
    for sent_text, start, end in sentences:
        low = sent_text.lower()
        score = sum(weight for kw, weight in weighted_keywords if kw.lower() in low)
        if score == 0:
            continue
        if spec.type == "quantity" and not _HAS_DIGIT.search(sent_text):
            continue
        scored.append((score, sent_text, start, end))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def _first_relevant(scored_candidates, spec):
    """Walks ranked candidates in order, returns the first that passes
    relevance (or the first one at all, if no marker set exists for this
    field -- check_relevance returns True by default in that case).
    """
    for score, sent_text, start, end in scored_candidates:
        if check_relevance(spec.field_id, sent_text):
            return (sent_text, start, end)
    return None


def find_candidate(canonical_text: str, text_sha: str, spec: FieldSpec) -> dict | None:
    m_start, m_end = find_methods_section(canonical_text)
    methods_text = canonical_text[m_start:m_end]

    subsections = find_subsections(methods_text, m_start, full_text=canonical_text)
    weighted_keywords = field_keywords(spec.field_id)
    matching_subsections = find_relevant_subsections(subsections, spec.field_id)

    best_overall = None
    best_score_overall = -1
    detector_label = None

    for sub in matching_subsections:
        sub_text = canonical_text[sub["start"]:sub["end"]]
        sentences = split_sentences(sub_text, base_offset=sub["start"])
        ranked = _ranked_candidates(sentences, weighted_keywords, spec)
        found = _first_relevant(ranked, spec)
        if found:
            sent_text, start, end = found
            score = ranked[0][0] if ranked else 0
            # Use the score of whichever candidate we actually accepted,
            # not just the top one, to compare fairly across subsections.
            accepted_score = next((s for s, t, st, en in ranked if t == sent_text), 0)
            if accepted_score > best_score_overall:
                best_score_overall = accepted_score
                best_overall = found
                detector_label = f"section:{sub['header']}"

    if best_overall is None:
        sentences = split_sentences(methods_text, base_offset=m_start)
        ranked = _ranked_candidates(sentences, weighted_keywords, spec)
        found = _first_relevant(ranked, spec)
        if found:
            best_overall = found
            detector_label = "keyword:fallback-whole-methods"

    if best_overall is None:
        found = _trigger_driven_candidate(canonical_text, m_start, m_end, spec)
        if found:
            best_overall = found
            detector_label = "trigger:fills-slot"

    if best_overall is None:
        return None

    sent_text, start, end = best_overall
    return {
        "raw_text": sent_text.strip(),
        "span": Span(start=start, end=end, text_sha=text_sha),
        "detector": detector_label,
    }


def _trigger_driven_candidate(canonical_text, m_start, m_end, spec):
    """Last-resort retrieval: find a sentence holding a trigger for THIS slot.

    Keyword retrieval requires the field's own vocabulary in the sentence, and
    the canonical vagueness cases do not contain it. treatment.duration is
    gold-labelled GAP_VAGUE on "Reporter cells were assessed after overnight
    incubation." -- a sentence with neither "treatment" nor "duration" in it, so
    it scored zero and was never a candidate. The gap was unreachable no matter
    how good Layer 2 became.

    The typed trigger table already knows which slots each trigger can fill, so
    it can drive retrieval as well as validation: for a field of dimension
    `time`, any sentence containing a time-filling trigger is a candidate.

    Deliberately a FALLBACK, reached only when keyword retrieval found nothing.
    Running it alongside the keyword pass would let a trigger anywhere in
    Methods outrank a sentence that genuinely discusses the field.

    GAP_VAGUE triggers ONLY -- deferrals are excluded, and that exclusion is
    load-bearing. A vagueness trigger names the slot it fills: "overnight" IS a
    duration, so the sentence containing it is genuinely about duration. A
    deferral is a property of the sentence, not of any one slot -- "the culture
    was generated as previously described" does not mean the cell line's NAME is
    deferred, it means the derivation procedure is. Allowing deferrals here took
    GAP_DEFERRED_TO_REF from 1 to 27 across nine papers, because every
    identifier and text field with no keyword hit grabbed the first
    "as previously described" sentence in Methods.
    """
    lexicon = get_trigger_lexicon()
    for sent_text, start, end in split_sentences(
        canonical_text[m_start:m_end], base_offset=m_start
    ):
        if not sent_text.strip():
            continue
        for trigger in lexicon.find_all(sent_text):
            if trigger.code != "GAP_VAGUE":
                continue
            if trigger.can_fill(spec.dimension, spec.type):
                return sent_text, start, end
    return None


# ---------------------------------------------------------------------------
# Step 5 for ABSENT fields.
#
# 02_BUILD_SPEC: "For an absent finding, put the span on the sentence that must
# contain the value. If there is no such sentence, put the span on the
# paragraph or on the section. [...] If you have no anchor, remove the finding
# and write it to the log."
#
# Nothing below proposes a VALUE. These functions only locate the place a
# reader should look, for a field we have already concluded is absent. They are
# deliberately more permissive than find_candidate -- no digit filter, no
# relevance gate -- because the bar for "where should I look" is lower than the
# bar for "is this a usable value".
# ---------------------------------------------------------------------------

# An anchor has to be readable. The sentence splitter turns a numbered heading
# ("2. Materials and Methods") into a two-character sentence "2.", and three
# findings were anchored on that bare numeral -- a location that, in
# 02_BUILD_SPEC's words, "costs attention and gives nothing back". Require at
# least one real word and enough characters to orient a reader.
_MIN_ANCHOR_CHARS = 10
_ANCHOR_HAS_WORD = re.compile(r"[A-Za-z]{3,}")


def _is_substantive(text: str) -> bool:
    return len(text) >= _MIN_ANCHOR_CHARS and bool(_ANCHOR_HAS_WORD.search(text))


def _tighten(sent_text: str, start: int) -> tuple[str, int, int] | None:
    """Trim surrounding whitespace and move the span onto the trimmed text.

    Returns None for a blank sentence. This is what guarantees we never hand
    contract.Span a zero-width range, which it rejects outright.
    """
    stripped = sent_text.strip()
    if not stripped:
        return None
    offset = sent_text.index(stripped)
    return stripped, start + offset, start + offset + len(stripped)


def _first_non_empty_sentence(sentences) -> tuple[str, int, int] | None:
    """First sentence carrying enough text to serve as a location."""
    for sent_text, start, _end in sentences:
        tightened = _tighten(sent_text, start)
        if tightened and _is_substantive(tightened[0]):
            return tightened
    return None


# A deterministic keyword hit worth at least this much is trusted over the
# model. field_keywords weights a full field-name fragment 2 and a remapped
# compound ("cell line") 1, so a floor of 2 means "at least one whole fragment
# of this field's own name appears in the sentence".
#
# Earned by a measured regression: with the model attached, wang2015's
# treatment.vehicle moved from span delta 0 to delta 687. The deterministic
# scan had already found "...subtracted from background (vehicle controls)"
# exactly, scoring 2 on "vehicle" -- and the gate consulted the model anyway,
# which redirected to a subsection whose first sentence was worse. Asking a
# model to improve on an answer that is already right can only lose.
_CONFIDENCE_FLOOR = 2


def _scored_best_sentence(sentences, weighted_keywords):
    """Same as _best_scoring_sentence, but also returns the winning score."""
    best, best_score = None, 0
    for sent_text, start, _end in sentences:
        low = sent_text.lower()
        score = sum(weight for kw, weight in weighted_keywords if kw.lower() in low)
        if score <= best_score:
            continue
        tightened = _tighten(sent_text, start)
        if tightened:
            best, best_score = tightened, score
    return best, best_score


def _best_scoring_sentence(sentences, weighted_keywords) -> tuple[str, int, int] | None:
    """Relaxed sibling of _ranked_candidates, for anchoring only.

    Strict `>` on the score means ties break on document order, so this is
    deterministic for a given text.
    """
    best = None
    best_score = 0
    for sent_text, start, _end in sentences:
        low = sent_text.lower()
        score = sum(weight for kw, weight in weighted_keywords if kw.lower() in low)
        if score <= best_score:
            continue
        tightened = _tighten(sent_text, start)
        if tightened:
            best_score = score
            best = tightened
    return best


def _as_anchor(hit: tuple[str, int, int], text_sha: str, detector: str) -> dict:
    anchor_text, start, end = hit
    return {
        "anchor_text": anchor_text,
        "span": Span(start=start, end=end, text_sha=text_sha),
        "detector": detector,
    }


def find_absence_anchor(
    canonical_text: str,
    text_sha: str,
    spec: FieldSpec,
    model=None,
) -> dict | None:
    """Locate where a reader should look for a field that is not stated.

    This is a walk up the document tree. Pick the deepest node that ought to
    contain the field, then climb toward the root until a node yields a
    sentence:

        subsection -> methods -> document

    Each level is tried twice: first for a sentence scoring on the field's
    keywords, then for the node's own first sentence. The second attempt is the
    one that usually fires for an absent field, and that is not a weakness --
    for an absent field the field's vocabulary is by definition NOT in the text,
    so "the first sentence of the right subsection" is the correct answer.
    Measured: scoring subsection bodies against field keywords returned zero for
    every field tested on wang2015, because "mycoplasma" appears nowhere in a
    paper that never mentions mycoplasma.

    Replaces a hardcoded 5-rung ladder. Same behaviour when the tree is two
    levels deep, but it now generalises to any depth for free.

    `model`, when supplied, is used ONLY to choose the subsection -- see
    node_select. Returns None only when the document contains no non-empty
    sentence at all; the caller must then drop the finding, since the contract
    rejects zero-width spans and 02_BUILD_SPEC says a finding with no location
    "is worse than silence".
    """
    m_start, m_end = find_methods_section(canonical_text)
    weighted_keywords = field_keywords(spec.field_id)

    root = build_tree(canonical_text, m_start, m_end)
    methods = root.children[0]

    # Confidence floor, checked ONLY when a model is attached.
    #
    # If the deterministic keyword scan already has a confident answer, take it
    # and spend no call. Gated on `model is not None` deliberately: the
    # model=None path must stay byte-identical, so this can only ever remove a
    # model override, never change the deterministic result.
    if model is not None:
        confident, score = _scored_best_sentence(
            split_sentences(canonical_text[m_start:m_end], base_offset=m_start),
            weighted_keywords,
        )
        if confident and score >= _CONFIDENCE_FLOOR:
            return _as_anchor(
                confident, text_sha, f"anchor:det-confident(score={score}):no-model-call"
            )

    node, how = select_node(
        spec, methods.children, model=model, canonical_text=canonical_text
    )
    chain = list(node.walk_up()) if node is not None else [methods, root]

    for level in chain:
        sentences = split_sentences(
            canonical_text[level.start:level.end], base_offset=level.start
        )
        hit = _best_scoring_sentence(sentences, weighted_keywords)
        if hit:
            return _as_anchor(hit, text_sha, f"anchor:{how}:kw:{level.title[:34]}")
        hit = _first_non_empty_sentence(sentences)
        if hit:
            return _as_anchor(hit, text_sha, f"anchor:{how}:head:{level.title[:34]}")

    return None