"""
sections.py -- lightweight structural tree, one level deep.

Inspired by PageIndex's core insight (github.com/VectifyAI/PageIndex):
navigate a document's own structure instead of flat similarity/keyword
search over the whole text.

REAL, TESTED BUGS FOUND AND FIXED (via a full diagnostic pass across all
8 real dev-set papers plus Wang 2015 -- 68 fields initially came back
FIELD_ABSENT despite the correct sentence existing somewhere in Methods):

1. MDPI-style running page headers ("Cancers 2026, 18, 2452 4 of 29")
   were matching the header pattern. Fixed with _PAGE_FOOTER_PATTERN.

2. Abbreviations-list entries ("PC Prostate cancer") looked structurally
   identical to real headers. First filter attempt checked word count,
   which was backwards -- verified directly, it let 3-word abbreviation
   entries through while excluding a real 2-word header. Fixed to check
   the actual acronym shape instead.

3. THE BIG ONE: multi-line wrapped headers were detected as TWO separate
   headers with nothing real between them. Confirmed on Xiong: "Cell
   Culture, Treatment with Pharmacologic Agents, and" / "Transient
   Transfection of STAT3 siRNA" is ONE header wrapped across two PDF
   lines. The old code treated line 2 as a new header immediately after
   line 1, so line 1's "body" was empty -- the real content that
   followed was silently orphaned under a subsection nobody ever
   searched. Fixed by merging consecutive header matches when there's
   no real text between them (just the line break).

   Accepted tradeoff, stated plainly: if two genuinely SEPARATE headers
   happen to be adjacent with no body text between them (rare), they'll
   get merged into one combined label. This is cosmetic -- no content is
   lost, since the body boundary still correctly extends to the NEXT
   real header -- but the merged header's name will look odd. Chosen
   because the alternative (the original bug) silently discarded real
   content, which is strictly worse.

4. find_relevant_subsection() only ever returned the FIRST subsection
   whose header matched a field's category keywords. If that first
   match was a coincidental, wrong-context hit (confirmed on Dos et al:
   several animal.* fields resolved to "IC50 Half-maximal inhibitory
   concentration" purely by chance keyword overlap), no other
   potentially-correct subsection was ever tried. Fixed by returning
   ALL matching subsections, so extract.py can search across all of
   them and keep the best-scoring result, not just the first guess.

This is still a seed-level heuristic, not a robust parser, tuned against
real, tested failures on real papers, not guessed.
"""
from __future__ import annotations

import re

_PAGE_FOOTER_PATTERN = re.compile(
    r"^\S+\s+\d{4},?\s+\d+,?\s+\d+\s+\d+\s+of\s+\d+$"
)

_ABBREVIATION_ENTRY_PATTERN = re.compile(r"^[A-Z]{2,6}\s+[A-Z][a-z]")

_HEADER_LINE_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?([A-Z][A-Za-z0-9 ,\-/]{2,60})(?<![.!?])$",
    re.MULTILINE,
)

_CATEGORY_HEADER_KEYWORDS = {
    "animal": ["animal", "mice", "mouse", "rat", "subjects", "in vivo"],
    "cellline": ["cell culture", "cell line", "cells", "cell establishment"],
    "culture": ["cell culture", "culture conditions"],
    "treatment": ["treatment", "pharmacologic", "drug", "reagent"],
    "qpcr": ["pcr", "qpcr", "rna", "gene expression"],
    "stats": ["statistic", "analysis"],
    "centrifugation": ["centrifug", "fractionation", "extraction"],
    "antibody": ["antibod", "western", "immunohistochemistry", "staining"],
    "assay": ["assay", "protocol"],
}


def find_subsections(methods_text: str, methods_start_offset: int) -> list[dict]:
    """Splits the Methods section into (header, body_start, body_end)
    chunks. Returns absolute offsets into the ORIGINAL canonical_text.
    Merges wrapped multi-line headers before establishing boundaries --
    see module docstring, bug #3.
    """
    raw_matches = []
    for m in _HEADER_LINE_PATTERN.finditer(methods_text):
        full_line = m.group(0).strip()
        title_only = m.group(1).strip()
        if _PAGE_FOOTER_PATTERN.match(full_line):
            continue
        if _ABBREVIATION_ENTRY_PATTERN.match(title_only):
            continue
        raw_matches.append({"title": title_only, "start": m.start(), "end": m.end()})

    # Merge consecutive header matches with nothing real between them --
    # they're one header wrapped across lines, not two subsections.
    merged = []
    i = 0
    while i < len(raw_matches):
        group_title = raw_matches[i]["title"]
        group_start = raw_matches[i]["start"]
        group_body_start = raw_matches[i]["end"]
        j = i + 1
        while j < len(raw_matches):
            gap = methods_text[group_body_start:raw_matches[j]["start"]]
            if len(gap.strip()) == 0:
                group_title += " " + raw_matches[j]["title"]
                group_body_start = raw_matches[j]["end"]
                j += 1
            else:
                break
        merged.append({"title": group_title, "start": group_start, "body_start": group_body_start})
        i = j

    subsections = []
    for idx, sub in enumerate(merged):
        body_start = sub["body_start"]
        body_end = merged[idx + 1]["start"] if idx + 1 < len(merged) else len(methods_text)
        subsections.append({
            "header": sub["title"],
            "start": methods_start_offset + body_start,
            "end": methods_start_offset + body_end,
        })
    return subsections


def find_relevant_subsections(subsections: list[dict], field_id: str) -> list[dict]:
    """Returns ALL subsections whose header matches the field's category
    keywords, not just the first. Fixed bug #4: searching only the first
    match meant a coincidental wrong-context hit could permanently hide
    the real answer sitting in a different, also-matching subsection.
    Returns [] if no subsection matches at all -- caller should fall back
    to the whole Methods section, not fail silently.
    """
    category = field_id.split(".")[0]
    keywords = _CATEGORY_HEADER_KEYWORDS.get(category, [])
    if not keywords:
        return []

    matches = []
    for sub in subsections:
        header_lower = sub["header"].lower()
        if any(kw in header_lower for kw in keywords):
            matches.append(sub)
    return matches