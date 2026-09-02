from __future__ import annotations

import re

from contract import FieldSpec
from vagueness import VaguenessChecker, load_vagueness_lexicon
from relevance import check_relevance


_FRACTION_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_TEMPERATURE_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(°C|C\b|celsius)", re.IGNORECASE)
_CONCENTRATION_UNIT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mM|µM|uM|nM|M|mol/L|ng/ml|mg/ml|µg/ml|ug/ml)", re.IGNORECASE
)

_DIMENSION_UNIT_PATTERNS = {
    "concentration": _CONCENTRATION_UNIT,
    "temperature": _TEMPERATURE_UNIT,
}

_DEFERRAL_TO_REF_PATTERN = re.compile(
    r"as\s+(?:previously\s+)?described|according to (?:the )?manufacturer|"
    r"per manufacturer'?s? (?:instructions|protocol)|as in (?:ref\.?|reference) ?\d+|"
    r"following (?:the )?standard protocol",
    re.IGNORECASE,
)
_DEFERRAL_TO_DISPLAY_PATTERN = re.compile(
    r"(?:the\s+)?indicated (?:times?|concentrations?|doses?|conditions?)|as indicated|"
    r"(?:see|shown in) (?:Fig|Figure|Table|Supplementary)",
    re.IGNORECASE,
)

_vagueness_checker = VaguenessChecker(load_vagueness_lexicon())


def check_deferral_or_vague(raw_text: str) -> dict | None:
    """Runs BEFORE type-specific validation, for every field type."""
    if _DEFERRAL_TO_REF_PATTERN.search(raw_text):
        return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_DEFERRED_TO_REF",
                "reason": f"{raw_text!r} defers to another source (e.g. 'as previously described')"}
    if _DEFERRAL_TO_DISPLAY_PATTERN.search(raw_text):
        return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_DEFERRED_TO_DISPLAY",
                "reason": f"{raw_text!r} defers to a figure/table, not a stated value"}
    return None


def validate_quantity(raw_text: str, spec: FieldSpec) -> dict:
    correct_pattern = _DIMENSION_UNIT_PATTERNS.get(spec.dimension)
    if correct_pattern and correct_pattern.search(raw_text):
        return {"verdict": "FIELD_OK", "code": None,
                "reason": f"{raw_text!r} matches expected dimension {spec.dimension}"}
    if _FRACTION_UNIT.search(raw_text) and spec.dimension != "fraction":
        return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_DIMENSION_MISMATCH",
                "reason": f"{raw_text!r} is a fraction, field needs {spec.dimension}"}
    return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_UNPARSEABLE",
            "reason": f"{raw_text!r} did not parse as {spec.dimension}"}


def validate_identifier(raw_text: str, spec: FieldSpec) -> dict:
    # Relevance is now checked twice by design, not redundantly: once
    # inside extract.py's ranking (so a bad top candidate can be skipped
    # in favor of a better one), and again here, as a final gate for
    # fields where extraction had NO alternative candidate to fall back
    # to. This second check is defense-in-depth, not dead code.
    if not check_relevance(spec.field_id, raw_text):
        return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_UNPARSEABLE",
                "reason": f"{raw_text!r} is topically nearby but contains no "
                          f"field-specific evidence for {spec.field_id}"}
    return {"verdict": "FIELD_OK", "code": None, "reason": f"{raw_text!r} treated as a real identifier"}


def validate_text(raw_text: str, spec: FieldSpec) -> dict:
    if not check_relevance(spec.field_id, raw_text):
        return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_UNPARSEABLE",
                "reason": f"{raw_text!r} is topically nearby but contains no "
                          f"field-specific evidence for {spec.field_id}"}
    return {"verdict": "FIELD_OK", "code": None, "reason": f"{raw_text!r} treated as usable text"}


def validate_enum(raw_text: str, spec: FieldSpec) -> dict:
    if spec.enum_values and raw_text.strip() in spec.enum_values:
        return {"verdict": "FIELD_OK", "code": None, "reason": f"{raw_text!r} is a valid enum value"}
    return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_UNPARSEABLE",
            "reason": f"{raw_text!r} not in declared enum_values {spec.enum_values}"}


def validate_boolean(raw_text: str, spec: FieldSpec) -> dict:
    return {"verdict": "FIELD_OK", "code": None, "reason": f"{raw_text!r} treated as boolean present"}


def validate_sequence(raw_text: str, spec: FieldSpec) -> dict:
    return {"verdict": "FIELD_OK", "code": None, "reason": f"{raw_text!r} treated as sequence present"}


_VALIDATORS_BY_TYPE = {
    "quantity": validate_quantity,
    "identifier": validate_identifier,
    "text": validate_text,
    "enum": validate_enum,
    "boolean": validate_boolean,
    "sequence": validate_sequence,
}


def validate_field(observation: dict | None, spec: FieldSpec) -> dict:
    if observation is None:
        return {"verdict": "FIELD_ABSENT", "code": "GAP_ABSENT", "reason": "no candidate text found"}

    raw_text = observation["raw_text"]

    deferral_result = check_deferral_or_vague(raw_text)
    if deferral_result is not None:
        return deferral_result

    vagueness_result = _vagueness_checker.check(raw_text)
    if vagueness_result is not None:
        return vagueness_result

    validator = _VALIDATORS_BY_TYPE.get(spec.type)
    if validator is None:
        return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_UNPARSEABLE",
                "reason": f"no validator registered for type {spec.type!r}"}

    return validator(raw_text, spec)