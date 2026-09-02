"""
extract.py -- Step 3, generalized.

[Keep existing docstring, add:]

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
from sections import find_subsections, find_relevant_subsections
from relevance import check_relevance

_METHODS_HEADERS = [r"EXPERIMENTAL PROCEDURES", r"MATERIALS AND METHODS", r"\bMETHODS\b"]
_METHODS_END_HEADERS = [r"\bDISCUSSION\b", r"\bREFERENCES\b", r"ACKNOWLEDGMENTS?"]


def find_methods_section(text: str) -> tuple[int, int]:
    start = None
    for pat in _METHODS_HEADERS:
        m = re.search(pat, text)
        if m:
            start = m.start()
            break
    if start is None:
        return 0, len(text)
    end = len(text)
    for pat in _METHODS_END_HEADERS:
        m = re.search(pat, text[start:])
        if m:
            end = start + m.start()
            break
    return start, end


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

    subsections = find_subsections(methods_text, m_start)
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
        return None

    sent_text, start, end = best_overall
    return {
        "raw_text": sent_text.strip(),
        "span": Span(start=start, end=end, text_sha=text_sha),
        "detector": detector_label,
    }


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
    for sent_text, start, _end in sentences:
        tightened = _tighten(sent_text, start)
        if tightened:
            return tightened
    return None


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
    canonical_text: str, text_sha: str, spec: FieldSpec
) -> dict | None:
    """Locate where a reader should look for a field that is not stated.

    Ladder, most specific first:
      1. best keyword-scoring sentence inside a subsection whose header matches
         the field's category
      2. first sentence of that subsection, if nothing in it scores
      3. best keyword-scoring sentence anywhere in Methods
      4. first sentence of Methods
      5. first sentence of the document

    Returns None only when the document contains no non-empty sentence at all.
    The caller must then drop the finding: a zero-width span is rejected by the
    contract, and 02_BUILD_SPEC is explicit that a finding with no location
    "is worse than silence".
    """
    m_start, m_end = find_methods_section(canonical_text)
    methods_text = canonical_text[m_start:m_end]
    weighted_keywords = field_keywords(spec.field_id)

    subsections = find_subsections(methods_text, m_start)
    matching = find_relevant_subsections(subsections, spec.field_id)

    # 1 -- most specific: the topical sentence inside a topical subsection.
    for sub in matching:
        sentences = split_sentences(
            canonical_text[sub["start"]:sub["end"]], base_offset=sub["start"]
        )
        hit = _best_scoring_sentence(sentences, weighted_keywords)
        if hit:
            return _as_anchor(hit, text_sha, f"anchor:section-keyword:{sub['header']}")

    # 2 -- the subsection matched by header, but nothing inside it scored.
    if matching:
        sub = matching[0]
        sentences = split_sentences(
            canonical_text[sub["start"]:sub["end"]], base_offset=sub["start"]
        )
        hit = _first_non_empty_sentence(sentences)
        if hit:
            return _as_anchor(hit, text_sha, f"anchor:section-head:{sub['header']}")

    methods_sentences = split_sentences(methods_text, base_offset=m_start)

    # 3 -- no topical subsection; fall back to the topical sentence in Methods.
    hit = _best_scoring_sentence(methods_sentences, weighted_keywords)
    if hit:
        return _as_anchor(hit, text_sha, "anchor:methods-keyword")

    # 4 -- nothing topical anywhere; anchor on the section itself.
    hit = _first_non_empty_sentence(methods_sentences)
    if hit:
        return _as_anchor(hit, text_sha, "anchor:methods-head")

    # 5 -- no Methods section resolved at all.
    hit = _first_non_empty_sentence(split_sentences(canonical_text, base_offset=0))
    if hit:
        return _as_anchor(hit, text_sha, "anchor:document-head")

    return None