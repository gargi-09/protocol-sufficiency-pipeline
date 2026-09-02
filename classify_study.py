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

# Every feature except has_drug_treatment uses a flat pattern list -- first
# match wins. has_drug_treatment is handled separately below, since it needs
# the reagent-exclusion check on top of pattern matching.
_FEATURE_PATTERNS: dict[StudyFeature, list[str]] = {
    "has_animals": [
        r"\bmice\b", r"\bmouse\b", r"\brats?\b",
    ],
    "has_cell_lines": [
        r"reporter\s+(?:T\s+)?cells?", r"\btransfected\b",
        r"primary\s+\w+\s+cultures?", r"cell\s+line",
    ],
    "has_qpcr": [
        r"\bqPCR\b",
    ],
    "has_western_blot": [
        r"western\s+blot",
    ],
    "has_ihc": [
        r"\bimmunohistochemistry\b", r"\bIHC\b",
    ],
    "has_flow_cytometry": [
        r"flow\s+cytometr\w*", r"\bFACS\b",
        r"fluorescence[- ]activated\s+cell",
    ],
    "has_xenograft": [
        r"\bxenograft\b",
    ],
}

# Unambiguous drug-treatment language -- checked first, no exclusion needed,
# since these phrasings only ever describe dosing a living subject.
_DRUG_TREATMENT_UNAMBIGUOUS_PATTERNS = [
    r"at\s+(?:the\s+)?indicated\s+concentrations?",
    r"graded\s+concentrations?\s+of",
    r"blocking\s+\w+\s+antibody",
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


def _first_match_span(text: str, patterns: list[str]) -> tuple[int, int] | None:
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.start(), m.end()
    return None


def _find_drug_treatment_span(text: str) -> tuple[int, int] | None:
    """has_drug_treatment's special-cased check. Tries unambiguous
    phrasings first; only falls back to 'treated with' after confirming
    the object isn't a known specimen-processing reagent.
    """
    span = _first_match_span(text, _DRUG_TREATMENT_UNAMBIGUOUS_PATTERNS)
    if span:
        return span

    m = re.search(r"treated\s+with", text, re.IGNORECASE)
    if not m:
        return None

    window = text[m.end():m.end() + 60]
    if re.search(_NON_DRUG_REAGENT_TOKENS, window, re.IGNORECASE):
        return None  # this is reagent processing of a specimen, not drug treatment

    return m.start(), m.end()


def classify_study(canonical_text: str, text_sha: str) -> StudyProfile:
    features: dict[StudyFeature, bool] = {}
    evidence: dict[StudyFeature, Span] = {}

    for feature, patterns in _FEATURE_PATTERNS.items():
        span = _first_match_span(canonical_text, patterns)
        if span:
            features[feature] = True
            evidence[feature] = Span(start=span[0], end=span[1], text_sha=text_sha)
        else:
            features[feature] = False

    drug_span = _find_drug_treatment_span(canonical_text)
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
        span = _first_match_span(canonical_text, _WET_LAB_FALLBACK_PATTERNS)
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