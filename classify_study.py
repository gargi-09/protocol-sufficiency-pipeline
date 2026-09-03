"""
classify_study.py -- Step 1 of the pipeline (02_BUILD_SPEC.py).

Decides which StudyFeatures apply to a paper, deterministically, with an
evidence span attached to every feature we set True. This gates everything
downstream via pack.applicable(profile), so getting it wrong is the single
costliest mistake in the whole system:

- Too permissive -> flag an absent IACUC approval on a paper with no
  animals. The README says this does more damage to a reader's trust than
  ten real gaps missed.
- Too restrictive -> silently score zero on a whole field group, with no
  warning at all.

DESIGN CHOICE (mine, on top of the spec): deterministic pattern evidence is
required before a feature is accepted as True. A model *could* be added
later as a secondary signal for ambiguous cases, but it would still need at
least one deterministic evidence span to corroborate it -- "model proposes,
code decides" applies here too, not just in the validation step where the
spec states it explicitly.

Known, named limitation (see 02_BUILD_SPEC.py "Known traps"): has_qpcr must
be False for semi-quantitative RT-PCR on a gel, even though "PCR" appears in
the text, because MIQE doesn't apply to that method. This classifier
deliberately matches only the literal substring "qPCR", not bare "PCR", to
avoid exactly that false positive.

has_drug_treatment FIX (found via real testing on Hosseini's paper):
"treated with" alone is too broad. Hosseini's paper treats extracted DNA
with hydroquinone and sodium bisulfite for a methylation assay -- reagent
processing of a specimen, not drug treatment of a living subject -- and the
naive pattern set has_drug_treatment=True incorrectly, which then wrongly
applied treatment.concentration/duration/vehicle to a paper that has none
of those experiments. An earlier attempt tried detecting the SUBJECT of
"treated" via proximity (specimen noun vs. living-subject noun), but failed:
"DNA from tumour and blood cells was treated" puts "cells" linguistically
closer to "treated" than "DNA" is, even though DNA is the real subject --
a real parsing problem regex can't solve reliably. Checking the OBJECT of
"treated with" instead (what's actually being applied) sidesteps this.
Still a seed list, not exhaustive -- same honest limitation as
05_vagueness.yaml itself.
"""
from __future__ import annotations

import re

from contract import Span, StudyFeature, StudyProfile
from extract import find_methods_section

# Every feature except has_drug_treatment uses a flat pattern list -- first
# match wins. has_drug_treatment is handled separately below, since it needs
# the reagent-exclusion check on top of pattern matching.
_FEATURE_PATTERNS: dict[StudyFeature, list[str]] = {
    # A species noun alone over-triggers: it also names an antibody host
    # ("goat anti-mouse HRP") and a feeder cell line ("mouse fibroblast feeder
    # cells"). Both are excluded below in _find_animal_span rather than here,
    # because the exclusion depends on what surrounds the match.
    "has_animals": [
        r"\bmice\b", r"\bmouse\b", r"\brats?\b", r"\bmurine\b",
    ],
    "has_cell_lines": [
        r"reporter\s+(?:T\s+)?cells?", r"\btransfect\w*",
        r"primary\s+\w+\s+cultures?", r"cell\s+lines?",
        r"cells?\s+were\s+(?:cultured|maintained|grown|seeded|plated)",
        r"cell\s+culture",
    ],
    # MIQE applies to quantitative PCR, not to endpoint or semi-quantitative
    # RT-PCR read on a gel -- 02_BUILD_SPEC names that trap explicitly, so bare
    # "PCR" and "RT-PCR" are deliberately absent. But the literal "qPCR" alone
    # was too narrow: it missed "qRT-PCR" (Shein) and "real time PCR" (Yadav),
    # both of which are quantitative and both of which MIQE covers.
    "has_qpcr": [
        r"\bq(?:RT-)?PCR\b", r"\bRT-qPCR\b",
        r"\breal[- ]?time\s+(?:RT-)?PCR\b", r"\bquantitative\s+(?:real[- ]?time\s+)?PCR\b",
        r"\bTaqMan\b", r"\bSYBR\b",
    ],
    "has_western_blot": [
        r"western\s+blot", r"\bimmunoblot\w*",
    ],
    # Stem, not the full noun: Xiong's methods say "Immunohistochemical
    # Staining" and never "immunohistochemistry".
    "has_ihc": [
        r"\bimmunohistochem\w*", r"\bIHC\b",
    ],
    "has_flow_cytometry": [
        r"flow\s+cytometr\w*", r"\bFACS\b",
        r"fluorescence[- ]activated\s+cell",
        # Annexin V and propidium-iodide apoptosis readouts are flow assays in
        # practice, and Du's methods describe the assay without ever naming the
        # instrument. Lower confidence than the cues above: PI also has
        # microscopy uses, so this is the entry most likely to over-trigger.
        r"annexin\s*[- ]?V", r"propidium\s+iodide",
    ],
    # "xenograft" alone missed Du, whose subsection is "Tumor-bearing mouse
    # models" and which never uses the word.
    "has_xenograft": [
        r"\bxenograft\w*\b", r"\btumou?r-bearing\b",
        r"(?:subcutaneous|orthotopic|intraperitoneal)\w*\s+(?:injection|implant\w*)",
        r"inject\w*\s+(?:subcutaneously|orthotopically)",
    ],
}

# Contexts where a species noun does NOT indicate animal experiments.
# "anti-mouse" is an antibody host; "mouse fibroblast" is a feeder line.
_ANIMAL_NON_SUBJECT = re.compile(
    r"anti[-\s]?(?:mouse|rat|murine)"
    r"|(?:mouse|murine|rat)\s+(?:fibroblast|embryonic\s+fibroblast|feeder|"
    r"monoclonal|polyclonal|IgG|antibod\w*|serum|origin)",
    re.IGNORECASE,
)

# Unambiguous drug-treatment language -- checked first, no exclusion needed,
# since these phrasings only ever describe dosing a living subject.
_DRUG_TREATMENT_UNAMBIGUOUS_PATTERNS = [
    r"at\s+(?:the\s+)?indicated\s+concentrations?",
    r"graded\s+concentrations?\s+of",
    r"blocking\s+\w+\s+antibody",
    # Cytokine and ligand stimulation is dosing a living system, and it is how
    # Yadav and Du administer their compounds. Neither uses "treated with".
    r"stimulated\s+with",
    # Noun form. Xiong heads a subsection "Treatment with Pharmacologic
    # Agents" and never writes "treated with".
    r"treatment\s+with\s+(?:pharmacolog|\w+\s+(?:agent|inhibitor|drug))",
    r"\b(?:administered|dosed)\s+(?:with\s+)?",
    r"\b(?:IC50|EC50)\b",
]

# Seed list of common molecular-biology specimen-processing reagents.
# "Treated with X" where X is one of these is reagent processing of a
# specimen (DNA/RNA/protein), not drug treatment of a living subject.
_NON_DRUG_REAGENT_TOKENS = (
    r"bisulfite|bisulphite|hydroquinone|proteinase\s*K|RNase|DNase|"
    r"phenol|chloroform|ethanol\s*precipitation|NaOH|sodium\s+hydroxide"
)

_WET_LAB_FALLBACK_PATTERNS = [
    r"\bcultured\b", r"\bdissected\b", r"\bextracted\b",
    r"\bstained\b", r"\bpurified\b", r"\bperfused\b",
]


_MIN_SCAN_WIDTH = 800


def _scan_region(canonical_text: str) -> tuple[int, int]:
    """Where to look for study features.

    02_BUILD_SPEC opens Step 1 with "Read the methods section", and we were
    reading the whole document. That produced two confirmed false positives:
    Xiong's has_animals fired on "murine cancer cells in mice" inside a
    REFERENCE LIST entry, and Dos et al's fired on a feeder-cell sentence that
    sits before the Methods heading.

    Falls back to the whole document when the window looks implausible, so a
    paper we cannot segment degrades to the old behaviour rather than to no
    features at all.
    """
    start, end = find_methods_section(canonical_text)
    if end - start < _MIN_SCAN_WIDTH:
        return 0, len(canonical_text)
    return start, end


def _first_match_span(text: str, patterns: list[str], offset: int = 0) -> tuple[int, int] | None:
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return offset + m.start(), offset + m.end()
    return None


def _find_animal_span(text: str, offset: int = 0) -> tuple[int, int] | None:
    """A species mention that is not an antibody host or a feeder cell line.

    Checked per occurrence rather than per pattern: Yadav names both an
    "Anti-mouse horseradish peroxidase" conjugate and "Timed mated
    Sprague-Dawley rats", so rejecting the paper on the first match would lose a
    genuine animal study, and accepting it would let Shein through on an
    antibody label alone.
    """
    for pat in _FEATURE_PATTERNS["has_animals"]:
        for m in re.finditer(pat, text, re.IGNORECASE):
            window = text[max(0, m.start() - 30):m.end() + 30]
            if _ANIMAL_NON_SUBJECT.search(window):
                continue
            return offset + m.start(), offset + m.end()
    return None


def _find_drug_treatment_span(text: str, offset: int = 0) -> tuple[int, int] | None:
    """has_drug_treatment's special-cased check. Tries unambiguous
    phrasings first; only falls back to 'treated with' after confirming
    the object isn't a known specimen-processing reagent.
    """
    span = _first_match_span(text, _DRUG_TREATMENT_UNAMBIGUOUS_PATTERNS, offset)
    if span:
        return span

    for m in re.finditer(r"treated\s+with", text, re.IGNORECASE):
        window = text[m.end():m.end() + 60]
        if re.search(_NON_DRUG_REAGENT_TOKENS, window, re.IGNORECASE):
            continue  # reagent processing of a specimen, not drug treatment
        return offset + m.start(), offset + m.end()
    return None


def classify_study(canonical_text: str, text_sha: str) -> StudyProfile:
    features: dict[StudyFeature, bool] = {}
    evidence: dict[StudyFeature, Span] = {}

    # Features are read from the Methods section, not the whole paper. Evidence
    # spans stay absolute offsets into canonical text.
    scan_start, scan_end = _scan_region(canonical_text)
    region = canonical_text[scan_start:scan_end]

    for feature, patterns in _FEATURE_PATTERNS.items():
        if feature == "has_animals":
            span = _find_animal_span(region, scan_start)
        else:
            span = _first_match_span(region, patterns, scan_start)
        if span:
            features[feature] = True
            evidence[feature] = Span(start=span[0], end=span[1], text_sha=text_sha)
        else:
            features[feature] = False

    drug_span = _find_drug_treatment_span(region, scan_start)
    if drug_span:
        features["has_drug_treatment"] = True
        evidence["has_drug_treatment"] = Span(start=drug_span[0], end=drug_span[1], text_sha=text_sha)
    else:
        features["has_drug_treatment"] = False

    if any(v for k, v in features.items() if k != "has_wet_lab"):
        features["has_wet_lab"] = True
        for k, v in evidence.items():
            evidence["has_wet_lab"] = v
            break
    else:
        span = _first_match_span(region, _WET_LAB_FALLBACK_PATTERNS, scan_start)
        if span:
            features["has_wet_lab"] = True
            evidence["has_wet_lab"] = Span(start=span[0], end=span[1], text_sha=text_sha)
        else:
            features["has_wet_lab"] = False

    return StudyProfile(features=features, evidence=evidence)


if __name__ == "__main__":
    import json

    from contract import canonicalize

    with open("papers/wang2015_trem2_cell.txt", "r", encoding="utf-8") as f:
        raw = f.read()

    canonical_text, text_sha = canonicalize(raw)
    profile = classify_study(canonical_text, text_sha)

    with open("gold/wang2015_trem2_cell.json", "r", encoding="utf-8") as f:
        gold = json.load(f)
    gold_profile = gold["profile"]

    print(f"{'feature':<22} {'predicted':>10} {'gold':>8} {'match':>7}")
    print("-" * 52)
    all_match = True
    for feature in list(_FEATURE_PATTERNS.keys()) + ["has_drug_treatment"]:
        predicted = profile.features[feature]
        expected = gold_profile.get(feature)
        ok = predicted == expected
        all_match = all_match and ok
        print(f"{feature:<22} {str(predicted):>10} {str(expected):>8} {'OK' if ok else 'MISMATCH':>7}")
    predicted = profile.features["has_wet_lab"]
    expected = gold_profile.get("has_wet_lab")
    ok = predicted == expected
    all_match = all_match and ok
    print(f"{'has_wet_lab':<22} {str(predicted):>10} {str(expected):>8} {'OK' if ok else 'MISMATCH':>7}")

    print()
    print("ALL FEATURES MATCH GOLD" if all_match else "SOME MISMATCHES -- see above")