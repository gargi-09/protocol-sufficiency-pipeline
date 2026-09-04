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

    # ---------------------------------------------------------------------
    # Added because the default was doing real damage.
    #
    # check_relevance returns True for any field with no marker set, and
    # validate_text / validate_identifier read that as FIELD_OK. With markers
    # for only four fields, 23 of 27 could not produce a finding at all no
    # matter what their candidate sentence said. Measured consequences on
    # wang2015, both at span delta ZERO -- located perfectly, then discarded:
    #
    #   treatment.vehicle      FIELD_OK on "...subtracted from background
    #                          (vehicle controls)." A vehicle is mentioned; it
    #                          is never composed. Gold: GAP_ABSENT.
    #   animal.ethics_approval FIELD_OK on "All mice were bred and housed in
    #                          the same animal facility." No approval in it.
    #
    # Every pattern below is standard domain vocabulary -- reagent names,
    # statistical test names, housekeeping genes, guideline terms. None is
    # drawn from a specific paper in this corpus.
    # ---------------------------------------------------------------------

    "animal.ethics_approval": [
        (r"\bIACUC\b", False),
        (r"institutional animal care", True),
        (r"animal (?:studies|experiments?|protocols?|research).{0,40}approv", True),
        (r"approv.{0,40}(?:animal|ethic|institutional)", True),
        (r"ethics? (?:committee|approval|board)", True),
        (r"\b(?:protocol|permit|licen[cs]e)\s*(?:no\.?|number|#)\s*\S+", True),
    ],
    "animal.sample_size_justification": [
        (r"power (?:analysis|calculation)", True),
        (r"sample size.{0,40}(?:determin|calculat|justif|estimat|based)", True),
        (r"(?:determin|calculat|estimat).{0,40}sample size", True),
        (r"to (?:achieve|detect).{0,30}power", True),
    ],
    "treatment.vehicle": [
        # A vehicle is identified by naming the carrier, not by the word itself.
        (r"\bDMSO\b", False),
        (r"dimethyl\s*sulf", True),
        (r"\b(?:ethanol|methanol|chloroform|acetone|corn oil|olive oil)\b", True),
        (r"\b(?:saline|PBS|HBSS|water)\b.{0,30}(?:vehicle|control|carrier)", True),
        (r"vehicle.{0,40}(?:consist|compris|contain|prepar|dissolv|%)", True),
        (r"dissolved in\b", True),
        (r"\b\d+(?:\.\d+)?\s*%\s*(?:DMSO|ethanol|methanol)", True),
    ],
    "stats.test_named": [
        (r"\b(?:ANOVA|MANOVA|ANCOVA)\b", False),
        (r"\bt-?test\b", True),
        (r"student'?s? t", True),
        (r"\bMann-?Whitney\b", True),
        (r"\bWilcoxon\b", True),
        (r"\bKruskal-?Wallis\b", True),
        (r"\bchi-?squared?\b", True),
        (r"\bFisher'?s? exact\b", True),
        (r"\blog-?rank\b", True),
        (r"\bKaplan-?Meier\b", True),
        (r"\b(?:Tukey|Bonferroni|Dunnett|Sidak|Holm)\b", True),
        (r"\b(?:linear|logistic|Cox) regression\b", True),
        (r"\bSpearman\b|\bPearson\b", True),
    ],
    "qpcr.reference_genes": [
        (r"\b(?:GAPDH|ACTB|B2M|HPRT1?|TBP|RPLP0|PPIA|UBC|18S|28S|5S|U6)\b", False),
        (r"beta[- ]?actin|β[- ]?actin", True),
        (r"housekeeping gene", True),
        (r"(?:normali[sz]ed|normali[sz]ation).{0,40}(?:to|against|using)", True),
        (r"(?:endogenous|internal) (?:control|reference)", True),
    ],
    "xenograft.tumour_measurement_method": [
        (r"\bcalip", True),
        (r"(?:length|L)\s*[×x*]\s*(?:width|W)", True),
        (r"width\s*\^?\s*2|W\s*\^?\s*2|W²", True),
        (r"tumou?r volume.{0,40}(?:calculat|determin|formula|=)", True),
        (r"\b(?:bioluminescen|IVIS|micro-?CT|MRI|ultrasound)\w*", True),
    ],
    "cellline.name": [
        # A cell line is named by a designation token: letters plus digits, or
        # a known-shape identifier. "cells" alone is not a name.
        (r"\b[A-Z][A-Za-z]{0,6}[- ]?\d{1,4}[A-Za-z]?\b", False),
        (r"\b(?:HeLa|Jurkat|Vero|Raji|K562|MCF7|A549|PC3|LNCaP)\b", False),
        (r"cell line[s]?\s+(?:named|designated|called)", True),
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