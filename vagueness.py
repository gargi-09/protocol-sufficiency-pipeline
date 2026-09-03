"""
vagueness.py -- typed, scoped trigger detection for Layer 2.

01_README.md ranks this layer highest: "the recall on the group of fields that
are present but not usable, because that group is the difficult part." The
lexicon in 05_vagueness.yaml is the seed, and its own comments say a pure
lexicon will not get far while a pure classifier will overfire on legitimate
qualitative text. 02_BUILD_SPEC calls finding the line between them "a real
part of this exercise".

The answer taken here is borrowed from clinical NLP: NegEx and its successor
ConText. Their insight is that a trigger word does not flag itself -- it
defines a SCOPE, a bounded region of surrounding text that its meaning applies
to. "Denies" negates its whole clause, not the next word.

Mapping onto this problem:

    NegEx / ConText                 here
    ------------------------------  --------------------------------------------
    trigger term ("denies")         "high speed", "as previously described"
    trigger category (negation,     GapCode (GAP_VAGUE, GAP_DEFERRED_TO_REF,
      uncertainty, historical)        GAP_DEFERRED_TO_DISPLAY)
    scope window                    the clause containing the trigger
    scope terminator ("but")        clause boundary: , ; : and conjunctions
    target concept                  the field's own content in the sentence
    hit = target inside scope       fire the code only if the target is in scope

Flat matching -- what this replaces -- produced a measured false positive: Sun's
antibody.identifier was reported GAP_VAGUE because its candidate sentence
("Rabbit anti-m6A antibody (Beyotime...) ... incubated overnight at 4 C")
contains "overnight". The trigger governs the incubation duration; the antibody
identity sits in a different clause. A trigger fired and its meaning was applied
to the wrong slot.

TWO GATES, not one. Scope alone is not sufficient, so each trigger also carries
the set of slots it could plausibly FILL:

  1. TYPE GATE. "overnight" can occupy a duration slot; it cannot occupy an
     antibody-identity slot, however close it sits. A trigger only fires on a
     field whose declared dimension or type it could actually stand in for.
     This is the gate that kills the false positives above.

  2. SCOPE GATE. Of the triggers that could fill the slot, only those whose
     clause contains the field's own content count.

Deferrals deliberately exclude `quantity`. "Cultured as previously described"
tells a reader where to find a PROCEDURE; it does not supply a number this paper
chose to omit. The supplied gold draws exactly this line -- animal.strain
(identifier) and assay.protocol_parameters (text) are labelled
GAP_DEFERRED_TO_REF, while culture.temperature and culture.co2_fraction in the
same paper, in a sentence containing "as previously described", are labelled
GAP_ABSENT.

Every trigger returns its own character offsets, so Step 5 can put the span on
the offending phrase rather than the whole sentence. The scorer's tolerance is
20 characters; a sentence-wide span misses it even when the code is right.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

# The lexicon ships beside this module. Resolving against __file__ rather than
# the process CWD is what lets analyze() be imported from any working directory.
_DEFAULT_LEXICON_PATH = Path(__file__).resolve().with_name("vagueness.yaml")

# ---------------------------------------------------------------------------
# What each trigger family can stand in for.
#
# Keys are pack `dimension` values plus pack `type` values, so a field matches
# if either its dimension or its type is listed. ANY_SLOT means the trigger
# invalidates whatever it governs regardless of type.
# ---------------------------------------------------------------------------

ANY_SLOT = frozenset({"*"})

# Phrases whose implied slot differs from their lexicon category. Checked before
# the category default.
_PHRASE_SLOTS: dict[str, frozenset[str]] = {
    # Speed language stands in for a spin setting, nothing else.
    "high speed": frozenset({"relative_centrifugal_force"}),
    "low speed": frozenset({"relative_centrifugal_force"}),
    "maximum speed": frozenset({"relative_centrifugal_force"}),
    "full speed": frozenset({"relative_centrifugal_force"}),
    # Duration adverbs.
    "briefly": frozenset({"time", "time_or_mass"}),
    "quickly": frozenset({"time", "time_or_mass"}),
    "rapidly": frozenset({"time", "time_or_mass"}),
    # Thermal conditions.
    "room temperature": frozenset({"temperature"}),
    "on ice": frozenset({"temperature"}),
    # Agitation quality. The pack has no field for it, so these can never fire.
    # Kept rather than deleted: the lexicon entry is real, its inapplicability
    # to this pack is the thing worth recording.
    "gently": frozenset({"agitation"}),
    "thoroughly": frozenset({"agitation"}),
    # Culture-state endpoints stand in for a duration.
    "overnight": frozenset({"time", "time_or_mass"}),
    "until confluent": frozenset({"time", "time_or_mass", "count"}),
    "to confluence": frozenset({"time", "time_or_mass", "count"}),
    "log phase": frozenset({"time", "time_or_mass", "count"}),
    "logarithmic growth": frozenset({"time", "time_or_mass", "count"}),
    "exponential phase": frozenset({"time", "time_or_mass", "count"}),
    # Experimental endpoints -- either a time or a tumour volume.
    "until endpoint": frozenset({"time", "volume"}),
    "until the desired": frozenset({"time", "volume"}),
    "when tumours became palpable": frozenset({"time", "volume"}),
}

# Fallback by lexicon category for anything not named above. "as needed",
# "appropriate", "standard conditions" and "routine conditions" are wholesale
# vagueness and land here.
_CATEGORY_SLOTS: dict[str, frozenset[str]] = {
    "qualitative_quantity": ANY_SLOT,
    "relative_condition": frozenset({"time", "time_or_mass", "count", "volume"}),
}

# The two deferral kinds do NOT behave alike, and conflating them was wrong.
#
# A deferral to a PAPER points at a procedure: "the culture was generated as
# previously described" defers the derivation, not any scalar the paper chose to
# omit. It can stand in for an identity, a protocol or a sequence, but not for a
# quantity. The supplied gold draws exactly this line -- animal.strain
# (identifier) and assay.protocol_parameters (text) are GAP_DEFERRED_TO_REF,
# while culture.temperature and culture.co2_fraction, quantities in a sentence
# containing "as previously described", are GAP_ABSENT.
_DEFERRAL_REF_SLOTS = frozenset({"text", "identifier", "sequence", "boolean", "enum"})

# A deferral to a DISPLAY is a direct slot filler. "at indicated concentration"
# does not defer a procedure -- it says the value itself lives in the figure, and
# it sits in the slot where the number belongs. So it fills any slot, quantities
# included: gold labels treatment.concentration, a quantity field, as
# GAP_DEFERRED_TO_DISPLAY on exactly such a phrase.
_DEFERRAL_DISPLAY_SLOTS = ANY_SLOT

_DEFERRED_TO_REF_PATTERNS = [
    r"as\s+(?:previously\s+)?described",
    r"as\s+described\s+(?:previously|elsewhere|earlier)",
    r"according to (?:the )?manufacturer",
    r"per manufacturer'?s? (?:instructions|protocol)",
    r"manufacturer'?s? (?:instructions|protocol|recommendations)",
    r"as in (?:ref\.?|reference) ?\d+",
    r"following (?:the )?standard protocol",
    r"(?:reported|published|detailed)\s+(?:previously|elsewhere)",
]

_DEFERRED_TO_DISPLAY_PATTERNS = [
    r"(?:the\s+)?indicated (?:times?|concentrations?|doses?|conditions?|amounts?)",
    r"\bas indicated\b",
    r"(?:see|shown in|displayed in|listed in|provided in|given in)\s+"
    r"(?:Fig|Figure|Table|Suppl|Supplementary|Supporting)",
]


@dataclass(frozen=True)
class Trigger:
    """One trigger occurrence, with its position and what it can fill."""
    code: str            # GapCode
    text: str            # matched surface form
    start: int           # offset within the searched text
    end: int
    fills: frozenset     # dimensions/types this trigger can stand in for
    family: str          # for the human-readable reason

    def can_fill(self, dimension: str | None, field_type: str) -> bool:
        if self.fills == ANY_SLOT:
            return True
        return (dimension in self.fills) or (field_type in self.fills)


def load_vagueness_lexicon(path: str | Path | None = None) -> dict:
    """Read the lexicon YAML. Defaults to the copy next to this module."""
    resolved = Path(path) if path is not None else _DEFAULT_LEXICON_PATH
    with open(resolved, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _slots_for(phrase: str, category: str) -> frozenset:
    return _PHRASE_SLOTS.get(phrase.lower(), _CATEGORY_SLOTS.get(category, ANY_SLOT))


def _compile_terms(words: list[str]) -> re.Pattern:
    # Longest first so "maximum speed" wins over any shorter overlapping entry.
    escaped = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(escaped) + r")\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Clause scoping
# ---------------------------------------------------------------------------

# ConText's scope terminators. A clause boundary ends a trigger's reach.
_CLAUSE_BOUNDARY = re.compile(
    r"[;:,]|\b(?:and|or|but|then|whereas|while|however|although|which|"
    r"followed by|prior to|before|after)\b",
    re.IGNORECASE,
)


def clause_bounds(text: str, position: int) -> tuple[int, int]:
    """Start and end of the clause containing `position`.

    Bounded by the nearest clause terminator on each side, or the ends of the
    text. This is ConText's scope window; the terminator list is its
    "termination" set.
    """
    start = 0
    end = len(text)
    for match in _CLAUSE_BOUNDARY.finditer(text):
        if match.end() <= position:
            start = match.end()
        elif match.start() > position:
            end = match.start()
            break
    return start, end


class TriggerLexicon:
    """All triggers -- vagueness and deferral -- in one typed table."""

    def __init__(self, lexicon: dict):
        self._term_patterns: list[tuple[re.Pattern, str, str]] = []
        for category in ("qualitative_quantity", "relative_condition"):
            terms = lexicon.get(category) or []
            if terms:
                self._term_patterns.append(
                    (_compile_terms(terms), "GAP_VAGUE", category)
                )
        self._deferral_patterns = [
            (re.compile("|".join(_DEFERRED_TO_REF_PATTERNS), re.IGNORECASE),
             "GAP_DEFERRED_TO_REF", "deferred_reference", _DEFERRAL_REF_SLOTS),
            (re.compile("|".join(_DEFERRED_TO_DISPLAY_PATTERNS), re.IGNORECASE),
             "GAP_DEFERRED_TO_DISPLAY", "deferred_display", _DEFERRAL_DISPLAY_SLOTS),
        ]

    def find_all(self, raw_text: str) -> list[Trigger]:
        """Every trigger occurrence in `raw_text`, in document order."""
        found: list[Trigger] = []
        for pattern, code, family in self._term_patterns:
            for m in pattern.finditer(raw_text):
                found.append(Trigger(
                    code=code, text=m.group(0), start=m.start(), end=m.end(),
                    fills=_slots_for(m.group(0), family), family=family,
                ))
        for pattern, code, family, slots in self._deferral_patterns:
            for m in pattern.finditer(raw_text):
                found.append(Trigger(
                    code=code, text=m.group(0), start=m.start(), end=m.end(),
                    fills=slots, family=family,
                ))
        found.sort(key=lambda t: (t.start, t.end))
        return found


@lru_cache(maxsize=None)
def get_trigger_lexicon(path: str | None = None) -> TriggerLexicon:
    """Lazily build and memoise the lexicon.

    Memoised rather than module-level so the YAML is read on first *use*, not
    on import -- importing validate.py from an arbitrary CWD can no longer
    fail. The cached object holds only compiled regexes and is never mutated,
    so this cannot introduce run-to-run variation.
    """
    return TriggerLexicon(load_vagueness_lexicon(path))


# Kept for the dev scripts that import the old name.
get_vagueness_checker = get_trigger_lexicon
