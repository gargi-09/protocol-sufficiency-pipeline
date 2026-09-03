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
from collections import Counter
from dataclasses import dataclass, field as dc_field
from functools import lru_cache

_PAGE_FOOTER_PATTERN = re.compile(
    r"^\S+\s+\d{4},?\s+\d+,?\s+\d+\s+\d+\s+of\s+\d+$"
)

_ABBREVIATION_ENTRY_PATTERN = re.compile(r"^[A-Z]{2,6}\s+[A-Z][a-z]")

# -- Guards against chrome and body text masquerading as headers -------------
#
# _PAGE_FOOTER_PATTERN above only covers the MDPI shape ("Cancers 2026, 18,
# 2452 4 of 29"). It matches none of the Nature, PMC, Cell, JBC or Neoplasia
# footers in the dev set. The three guards below are shape-based rather than
# publisher-specific, so they do not need a new entry per journal.

# A heading ending in a 3+ digit run is a running header carrying a page
# number: "JAK/STAT Pathway Involved in Survival of Neurons31832" (10x in
# Yadav). 3+ digits rather than 1+ so that "Table 1" and "Figure 2" survive.
_TRAILING_PAGE_NUMBER = re.compile(r"\s*\d{3,}$")

# A heading is a noun phrase; a clause has a finite verb. Rejecting candidates
# that contain one separates real headers from wrapped body lines promoted by
# the line pattern -- confirmed on "METTL3 was demonstrated in HCT116 and LoVo
# cells" (Sun) and "SH-SY5Y cells were either unstimulated" (Yadav). No real
# header in the dev set contains a finite verb.
_FINITE_VERB = re.compile(
    r"\b(?:was|were|is|are|am|be|been|being|has|have|had|do|does|did"
    r"|will|would|shall|should|can|could|may|might|must)\b",
    re.IGNORECASE,
)

# Headings do not open subordinate clauses. Catches "After incubation for 24
# hours, nonmigrated cells in the upper" (Xiong), which contains no finite verb
# and so slips past the guard above.
_SUBORDINATOR_START = re.compile(
    r"^(?:after|before|when|while|whereas|although|though|because|since"
    r"|if|unless|thus|hence|therefore|however|moreover|furthermore"
    r"|then|next|finally|briefly)\b",
    re.IGNORECASE,
)

# Digits normalised out, so consecutive page numbers collapse to one key and a
# running header is recognisable by repetition alone -- whatever it says, and
# for any publisher.
_DIGITS = re.compile(r"\d+")


def _is_chrome(full_line: str, title: str) -> bool:
    """True if this candidate is page furniture or body text, not a header."""
    if _PAGE_FOOTER_PATTERN.match(full_line):
        return True
    if _ABBREVIATION_ENTRY_PATTERN.match(title):
        return True
    if _TRAILING_PAGE_NUMBER.search(title):
        return True
    if _FINITE_VERB.search(title):
        return True
    if _SUBORDINATOR_START.match(title):
        return True
    return False

_HEADER_LINE_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?([A-Z][A-Za-z0-9 ,\-/]{2,60})(?<![.!?])$",
    re.MULTILINE,
)

_CATEGORY_HEADER_KEYWORDS = {
    "animal": ["animal", "mice", "mouse", "rat", "subjects", "in vivo"],
    # "transfect" added because a paper's cell-line provenance is routinely
    # stated in its transfection section rather than under a cell-culture
    # heading -- Du's source ("...Medical College (PCRC). The cells were
    # cultured in high-glucose DMEM") sits inside "Transfection", and no Du
    # heading contains a cellline keyword at all. Safe only because
    # match_nodes_by_category now ranks by distinct-hit count: a heading like
    # "Cell culture and transfection" scores 2 and still beats a bare
    # "Transfection" scoring 1.
    "cellline": ["cell culture", "cell line", "cells", "cell establishment",
                 "transfect"],
    # "culture" added as a stem: the category is named for it, and the previous
    # list could not match "Cell Culture", "Culture Conditions" or "Ex Vivo
    # Microglia Cultures" -- the last of which is a real, confirmed miss.
    "culture": ["cell culture", "culture"],
    "treatment": ["treatment", "pharmacologic", "drug", "reagent"],
    "qpcr": ["pcr", "qpcr", "rna", "gene expression"],
    "stats": ["statistic", "analysis"],
    # Papers almost never head a subsection "Centrifugation". Measured across
    # the corpus, spin steps live inside lysis, immunoprecipitation and
    # protein-extraction sections -- Sun's are under "Western blot" and "RNA
    # associated immunoprecipitation". Four of seven papers state no spin in
    # their methods at all, so for those no keyword can help and GAP_ABSENT
    # anchored at the section level is the honest answer.
    "centrifugation": ["centrifug", "fractionation", "extraction", "lysis",
                       "lysate", "immunoprecipitat", "homogen", "subcellular"],
    "antibody": ["antibod", "western", "immunohistochemistry", "staining"],
    "assay": ["assay", "protocol"],
    # This key was absent entirely, so _category_pattern returned None and all
    # three xenograft.* fields fell straight to the methods head on every
    # paper -- while Du carried a "Tumor-bearing mouse models" subsection that
    # was never consulted.
    "xenograft": ["xenograft", "tumor-bearing", "tumour-bearing", "tumor model",
                  "tumour model", "mouse model", "animal model", "in vivo",
                  "implant"],
}


@lru_cache(maxsize=None)
def _category_pattern(category: str) -> re.Pattern | None:
    """Compile a category's keywords with a LEFT word boundary only.

    Plain substring containment was matching "rat" inside "prolifeRATion",
    "nonmigRATed" and "demonstRATed", which routed animal.* fields to
    cell-proliferation and transwell subsections -- 8 bad anchors, verified.

    The boundary is left-only, not both sides, because several entries are
    deliberate stems: "centrifug" must still reach centrifugation/centrifuged,
    "antibod" must reach antibody/antibodies, "statistic" must reach
    statistical/statistics. Requiring the match to START at a word boundary is
    what kills the false positives; requiring it to END at one would break the
    stems. Residual risk is a stem matching an unintended longer word
    ("rat" -> "rather"), which no methods heading in the corpus contains.
    """
    keywords = _CATEGORY_HEADER_KEYWORDS.get(category, [])
    if not keywords:
        return None
    alternation = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    return re.compile(rf"\b(?:{alternation})", re.IGNORECASE)


_PERIODICITY_TOLERANCE = 0.35


def _running_header_lines(text: str) -> set[str]:
    """Digit-normalised lines that recur at PERIODIC intervals.

    Page furniture is the one kind of chrome detectable without any
    per-publisher knowledge, but "repeats" alone is not the right test -- it
    suppressed a real header. In wang2015 the subsection heading "Mice" occurs
    three times (once as the heading, twice incidentally), and dropping it cost
    the paper its only animal subsection.

    Repetition is not the signature; REGULAR SPACING is. A running header
    recurs once per page, so its gaps are near-uniform. Measured:

        Yadav running header  gaps 10288, 9340, 10486, 8754   spread 0.07
        wang2015 "Mice"       gaps 4914, 17844                spread 0.57

    So: at least three occurrences (two gaps are the minimum needed to judge
    regularity at all) and a coefficient of variation under the tolerance.
    Errors land on the safe side -- an irregular running header survives here
    and may be caught by another guard, whereas a wrongly dropped heading costs
    a subsection outright.

    Computed over the WHOLE document, not the Methods window: narrowing the
    window leaves only one or two page breaks inside it, and restricting this to
    Methods text was measured to suppress exactly zero lines.
    """
    positions: dict[str, list[int]] = {}
    offset = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            positions.setdefault(_DIGITS.sub("#", stripped), []).append(offset)
        offset += len(line) + 1

    running = set()
    for key, hits in positions.items():
        if len(hits) < 3:
            continue
        gaps = [b - a for a, b in zip(hits, hits[1:])]
        mean = sum(gaps) / len(gaps)
        if mean <= 0:
            continue
        variance = sum((g - mean) ** 2 for g in gaps) / len(gaps)
        if (variance ** 0.5) / mean <= _PERIODICITY_TOLERANCE:
            running.add(key)
    return running


def find_subsections(
    methods_text: str,
    methods_start_offset: int,
    full_text: str | None = None,
) -> list[dict]:
    """Splits the Methods section into (header, body_start, body_end)
    chunks. Returns absolute offsets into the ORIGINAL canonical_text.
    Merges wrapped multi-line headers before establishing boundaries --
    see module docstring, bug #3.
    """
    candidates = []
    for m in _HEADER_LINE_PATTERN.finditer(methods_text):
        full_line = m.group(0).strip()
        title_only = m.group(1).strip()
        if _is_chrome(full_line, title_only):
            continue
        candidates.append({"title": title_only, "start": m.start(), "end": m.end()})

    # Drop candidates whose line recurs at page-break intervals elsewhere in the
    # document -- see _running_header_lines for why periodicity, not repetition,
    # is the test.
    repeats = _running_header_lines(full_text if full_text is not None else methods_text)
    raw_matches = [
        c for c in candidates if _DIGITS.sub("#", c["title"]) not in repeats
    ]

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


@dataclass
class Node:
    """One node of the document tree.

    Inspired by PageIndex (github.com/VectifyAI/PageIndex): index a document by
    its own structure and navigate that, instead of flat search over the whole
    text. Two things carry over cleanly from their design -- the unit is a
    natural section rather than a fixed-size chunk, and the structure is derived
    from layout with no model involved. Their *retrieval* half does not carry
    over: they search the tree for content that exists, whereas our hardest case
    is a field that is absent, which by definition leaves no text to retrieve.
    So here the tree is not a search index, it is an ADDRESS SPACE FOR ABSENCES
    -- it names the places where a missing item ought to have been stated, so a
    gap can be given a location at all.

    Deliberately shallow: document -> methods -> subsection. Deep enough for the
    anchor walk, and honest about what was built. No node summaries, no
    multi-hop descent.
    """
    node_id: str
    title: str
    start: int                      # body start, absolute canonical offset
    end: int                        # body end, exclusive
    depth: int
    parent: "Node | None" = dc_field(default=None, repr=False, compare=False)
    children: list["Node"] = dc_field(default_factory=list, repr=False)

    def walk_up(self):
        """This node, then each ancestor. The anchor ladder is exactly this."""
        node = self
        while node is not None:
            yield node
            node = node.parent


def build_tree(
    canonical_text: str,
    methods_start: int,
    methods_end: int,
) -> Node:
    """Document -> methods -> subsections, from layout alone. No model.

    The methods boundary is passed in rather than computed here so that
    sections.py stays free of extract.py's heading patterns (extract already
    imports from this module; the reverse would be circular).
    """
    root = Node(
        node_id="0", title="document", start=0,
        end=len(canonical_text), depth=0,
    )
    methods = Node(
        node_id="0.0", title="methods", start=methods_start,
        end=methods_end, depth=1, parent=root,
    )
    root.children.append(methods)

    subsections = find_subsections(
        canonical_text[methods_start:methods_end],
        methods_start,
        full_text=canonical_text,
    )
    for index, sub in enumerate(subsections):
        methods.children.append(Node(
            node_id=f"0.0.{index}",
            title=sub["header"],
            start=sub["start"],
            end=sub["end"],
            depth=2,
            parent=methods,
        ))
    return root


def _category_hit_count(title: str, category: str) -> int:
    """How many DISTINCT category keywords a title matches."""
    return sum(
        1 for kw in _CATEGORY_HEADER_KEYWORDS.get(category, [])
        if re.search(r"\b" + re.escape(kw), title, re.IGNORECASE)
    )


def match_nodes_by_category(nodes: list[Node], field_id: str) -> list[Node]:
    """Nodes whose header matches the field's category, BEST MATCH FIRST.

    Ranked, not in document order. Xiong's stats.test_named matched both "Cell
    Cycle Analysis Using Propidium Iodide" and "Statistical Analysis", and
    taking the first in document order anchored the finding on a cell-count
    sentence. "Statistical Analysis" matches two category keywords (statistic,
    analysis) against the other's one, so counting distinct hits picks it.

    Sorted with a stable key, so ties still break on document order and the
    result stays deterministic.
    """
    category = field_id.split(".")[0]
    pattern = _category_pattern(category)
    if pattern is None:
        return []
    matches = [node for node in nodes if pattern.search(node.title)]
    return sorted(matches, key=lambda n: -_category_hit_count(n.title, category))


def find_relevant_subsections(subsections: list[dict], field_id: str) -> list[dict]:
    """Returns ALL subsections whose header matches the field's category
    keywords, not just the first. Fixed bug #4: searching only the first
    match meant a coincidental wrong-context hit could permanently hide
    the real answer sitting in a different, also-matching subsection.
    Returns [] if no subsection matches at all -- caller should fall back
    to the whole Methods section, not fail silently.
    """
    pattern = _category_pattern(field_id.split(".")[0])
    if pattern is None:
        return []
    return [sub for sub in subsections if pattern.search(sub["header"])]