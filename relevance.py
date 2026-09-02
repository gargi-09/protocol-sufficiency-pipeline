"""
relevance.py -- shared relevance markers, used by BOTH extraction (to pick
a better candidate when the top keyword match fails relevance) and
validation (as a final gate, especially for fields with no matching
subsection to rank alternatives within).

REAL BUG FOUND (via testing after wiring relevance into validate.py only):
Dos et al's cellline.source_or_rrid correctly REJECTED a wrong sentence
(a Cellosaurus database mention), but the actual correct answer -- the
ATCC/#CRL-2505 sentence -- sits in the SAME subsection and was never
tried, because extraction had already committed to its single top-scoring
candidate before validation ever ran. Rejecting a bad candidate doesn't
recover a good one sitting right next to it unless extraction itself
knows to keep trying. Fixed by moving relevance-checking into extraction's
ranking loop directly.
"""
from __future__ import annotations

import re

_RELEVANCE_MARKERS: dict[str, list[tuple[str, bool]]] = {
    "animal.strain": [
        (r"[A-Z][a-z]+-[A-Z][a-z]+", False),
        (r"\b[A-Z]\d+[A-Z]{2,}\b", False),
        (r"\bBALB/c\b", True),
        (r"\bC57BL\b", True),
        (r"\bnude\b", True),
    ],
    "cellline.authentication": [
        (r"short tandem repeat", True),
        (r"\bSTR\b", False),
        (r"karyotyp", True),
        (r"fingerprint", True),
        (r"authenticat", True),
    ],
    "cellline.source_or_rrid": [
        (r"\bATCC\b", False),
        (r"\bRRID\b", False),
        (r"\bCVCL_", False),
        (r"#[A-Z0-9-]{4,}", False),
        (r"purchased from", True),
        (r"obtained from", True),
        (r"provided by", True),
    ],
    "cellline.mycoplasma": [
        (r"mycoplasma.{0,40}(test|negative|screen|free|confirm)", True),
        (r"(test|screen).{0,40}mycoplasma", True),
    ],
}


def check_relevance(field_id: str, raw_text: str) -> bool:
    """Returns True if the text contains field-specific evidence, or if
    no marker set is defined for this field (an honest 'not checked yet'
    default, not a silent pass disguised as a real check).
    """
    markers = _RELEVANCE_MARKERS.get(field_id)
    if markers is None:
        return True
    for pattern, ignore_case in markers:
        flags = re.IGNORECASE if ignore_case else 0
        if re.search(pattern, raw_text, flags):
            return True
    return False