from __future__ import annotations

import re

from contract import FieldSpec
from vagueness import clause_bounds, get_trigger_lexicon
from sections import _category_pattern
from relevance import check_relevance


_NUM = r"\d[\d,]*(?:\.\d+)?"

_FRACTION_UNIT = re.compile(rf"({_NUM})\s*%")
_TEMPERATURE_UNIT = re.compile(rf"({_NUM})\s*(°C|C\b|celsius)", re.IGNORECASE)
# Molar units are deliberately CASE-SENSITIVE. Under re.IGNORECASE the bare "M"
# alternative matched the "m" of any unit, so "5 min" parsed as 5 molar and
# treatment.concentration returned FIELD_OK on an incubation time -- a false
# FIELD_OK, which is worse than a false finding because it silently suppresses
# one. "mM" likewise matched "mm". The molar M is uppercase by convention, so
# dropping the global flag is the fix; only the per-volume forms need to be
# case-blind (ng/ml vs ng/mL).
_CONC_MOLAR = r"[mµμunpf]?M\b"
_CONC_PER_VOLUME = r"(?i:(?:[nmµμu]?g|U|IU)\s*/\s*[mdc]?[lL]\b)"
_CONCENTRATION_UNIT = re.compile(
    rf"({_NUM})\s*(?:{_CONC_MOLAR}|mol\s*/\s*[Ll]\b|{_CONC_PER_VOLUME})"
)
# Dimensions below had NO pattern at all, so 24 of the 43 GAP_UNPARSEABLE
# verdicts were "we have no way to read this dimension" reported as though the
# value had failed to parse. Seven of nine quantity fields could never return
# FIELD_OK, which is why culture.co2_fraction fired on papers that state
# "5% CO2" verbatim.
_TIME_UNIT = re.compile(
    rf"({_NUM})\s*(seconds?|secs?|s|minutes?|mins?|min|hours?|hrs?|h|days?|d|weeks?|wks?)\b",
    re.IGNORECASE,
)
_MASS_UNIT = re.compile(rf"({_NUM})\s*(ng|µg|μg|ug|mg|g|kg)\b", re.IGNORECASE)
_VOLUME_UNIT = re.compile(
    rf"({_NUM})\s*(mm\s*3|mm³|cm\s*3|cm³|nl|µl|μl|ul|ml|mL|l|L)\b", re.IGNORECASE
)
_COUNT_UNIT = re.compile(
    rf"({_NUM}(?:\s*[×x✕]\s*10\s*\^?\s*\d+)?)\s*(cells?|passages?|copies)\b"
    rf"|\bpassage\s*(?:number\s*)?({_NUM})\b",
    re.IGNORECASE,
)
# Relative centrifugal force. Requires an explicit "x g" or "rcf": a bare "g"
# is indistinguishable from grams.
_RCF_UNIT = re.compile(rf"({_NUM})\s*[×x✕*]\s*g\b|\brcf\b", re.IGNORECASE)
# rpm against an RCF field is the pack's own named example of a dimension
# mismatch. Never converted -- that needs a rotor radius, and 02_BUILD_SPEC is
# explicit that inventing one "makes a number that was never in the record".
_RPM_UNIT = re.compile(rf"({_NUM})\s*rpm\b", re.IGNORECASE)

_DIMENSION_UNIT_PATTERNS = {
    "concentration": _CONCENTRATION_UNIT,
    "temperature": _TEMPERATURE_UNIT,
    "time": _TIME_UNIT,
    "fraction": _FRACTION_UNIT,
    "volume": _VOLUME_UNIT,
    "count": _COUNT_UNIT,
    "relative_centrifugal_force": _RCF_UNIT,
    # ARRIVE 2.0 item 2a is decomposed into one field here but declares a
    # disjunctive pseudo-dimension, so either reading satisfies it. Noted in
    # defects_found.md rather than worked around silently.
    "time_or_mass": re.compile(f"(?:{_TIME_UNIT.pattern})|(?:{_MASS_UNIT.pattern})", re.IGNORECASE),
}

# "and" is NOT a range separator. With it, the dose series "a final
# concentration of 0, 50, 100, and 150 uM" read as the interval 100-150 and
# Xiong's four-point dose-response was reported as GAP_RANGE_NOT_POINT. An
# enumerated series is a complete specification, not a range.
_RANGE_SEPARATOR = r"\s*(?:–|—|-|\bto\b)\s*"


def _states_range(pattern: re.Pattern, raw_text: str) -> bool:
    """True if the text gives an interval where a point value is required.

    02_BUILD_SPEC lists '"4-6 h" where one point is necessary' as one of its six
    canonical failures, and GAP_RANGE_NOT_POINT was dead code until now. Checked
    before the point-value test, because "4-6 h" also contains "6 h".

    Two accepted forms. A bare "and" is excluded from the first because it also
    joins the last two items of an enumerated dose series, but "between X and Y"
    is unambiguously an interval, so it gets its own pattern rather than being
    lost with it.
    """
    unit = pattern.pattern
    forms = (
        rf"({_NUM}){_RANGE_SEPARATOR}(?:{unit})",
        rf"\b(?:between|from)\s+({_NUM})\s+(?:and|to)\s+(?:{unit})",
    )
    for form in forms:
        for match in re.finditer(form, raw_text, re.IGNORECASE):
            try:
                low = float(match.group(1).replace(",", ""))
                high = float(match.group(2).replace(",", ""))
            except (TypeError, ValueError):
                continue
            if low < high:
                return True
    return False

def _target_position(raw_text: str, spec: FieldSpec) -> int | None:
    """Where in the sentence this field's own content sits -- ConText's target.

    Preference order: a value of the field's declared dimension, then the
    field's category vocabulary, then its own name fragments. Returns None when
    the field has no locatable presence in the sentence, in which case scope
    containment cannot be evaluated and the scope gate is skipped.
    """
    pattern = _DIMENSION_UNIT_PATTERNS.get(spec.dimension) if spec.dimension else None
    if pattern:
        m = pattern.search(raw_text)
        if m:
            return m.start()

    category = _category_pattern(spec.field_id.split(".")[0])
    if category:
        m = category.search(raw_text)
        if m:
            return m.start()

    for fragment in re.split(r"[._]", spec.field_id):
        if len(fragment) < 4:
            continue
        m = re.search(r"\b" + re.escape(fragment), raw_text, re.IGNORECASE)
        if m:
            return m.start()
    return None


def check_deferral_or_vague(raw_text: str, spec: FieldSpec) -> dict | None:
    """Layer 2, with ConText's two gates. Runs before type-specific validation.

    Flat matching -- any trigger anywhere in the sentence stamps the field --
    produced measured false positives, e.g. Sun's antibody.identifier reported
    GAP_VAGUE because its sentence ends "...incubated overnight at 4 C". Two
    gates now apply to every candidate trigger:

      TYPE  -- could this trigger stand in for this field's slot at all?
               "overnight" fills a duration, never an antibody identity.
      SCOPE -- is the trigger in the same clause as the field's own content?
               Skipped when the field has no locatable target in the sentence,
               since containment is then undefined and refusing to fire would
               lose real gaps.

    Returns the trigger's OWN offsets in `offset`, so Step 5 can span the
    offending phrase instead of the whole sentence. The scorer's tolerance is
    20 characters and a sentence-wide span misses it even when the code is
    right.
    """
    triggers = get_trigger_lexicon().find_all(raw_text)
    if not triggers:
        return None

    fillable = [t for t in triggers if t.can_fill(spec.dimension, spec.type)]
    if not fillable:
        return None

    target = _target_position(raw_text, spec)
    if target is not None:
        in_scope = []
        for trigger in fillable:
            low, high = clause_bounds(raw_text, trigger.start)
            if low <= target < high:
                in_scope.append(trigger)
        if in_scope:
            fillable = in_scope
        elif not _spans_whole_sentence(fillable):
            return None

    trigger = fillable[0]
    # Span the trigger's CLAUSE, not the trigger token.
    #
    # 02_BUILD_SPEC says "put the span on the incorrect phrase", and the
    # supplied gold shows what that means in practice: every present-but-
    # unusable label anchors a clause or a full sentence, never a bare trigger.
    # "Reporter cells were assessed after overnight incubation." is anchored at
    # the sentence start, 34 characters before "overnight" -- outside the
    # scorer's 20-character tolerance. Narrowing to the token would move us
    # away from gold, not toward it.
    #
    # The clause is also the more faithful reading of ConText: the scope IS the
    # clause, so the region the trigger invalidates is exactly what to point at.
    low, high = clause_bounds(raw_text, trigger.start)
    return {
        "verdict": "FIELD_UNRESOLVED",
        "code": trigger.code,
        "reason": f"{trigger.text!r} ({trigger.family}) occupies the "
                  f"{spec.field_id} slot without supplying a value",
        "offset": (low, high),
        "trigger": trigger.text,
    }


def _spans_whole_sentence(triggers: list) -> bool:
    """True if a deferral is present, which governs the sentence as a whole.

    A procedure-level deferral is not clause-local: "generated as previously
    described" defers everything about the generation, so the scope gate must
    not veto it on clause position alone.
    """
    return any(t.code.startswith("GAP_DEFERRED") for t in triggers)


def validate_quantity(raw_text: str, spec: FieldSpec) -> dict:
    correct_pattern = _DIMENSION_UNIT_PATTERNS.get(spec.dimension)

    if correct_pattern is not None:
        # Range before point: "4-6 h" also contains "6 h".
        if _states_range(correct_pattern, raw_text):
            return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_RANGE_NOT_POINT",
                    "reason": f"{raw_text!r} gives a range where a single "
                              f"{spec.dimension} value is required"}
        if correct_pattern.search(raw_text):
            return {"verdict": "FIELD_OK", "code": None,
                    "reason": f"{raw_text!r} matches expected dimension {spec.dimension}"}

    # rpm in a slot that wants x g. The pack names this case explicitly.
    if spec.dimension == "relative_centrifugal_force" and _RPM_UNIT.search(raw_text):
        return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_DIMENSION_MISMATCH",
                "reason": f"{raw_text!r} gives rpm where {spec.canonical_unit or 'x g'} is "
                          f"required; not interconvertible without a rotor radius"}

    if _FRACTION_UNIT.search(raw_text) and spec.dimension != "fraction":
        return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_DIMENSION_MISMATCH",
                "reason": f"{raw_text!r} is a fraction, field needs {spec.dimension}"}

    if correct_pattern is None:
        # No reader for this dimension. This is the only honest use of
        # GAP_UNPARSEABLE: we are not claiming the value is missing, only that
        # we cannot read it. Should now be unreachable -- every dimension in the
        # pack has a pattern -- and is kept as a guard for a future pack.
        return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_UNPARSEABLE",
                "reason": f"no unit pattern registered for dimension {spec.dimension!r}"}

    # We can read this dimension and no value of it is here. That is absence,
    # not a parse failure. Previously reported as GAP_UNPARSEABLE, a code that
    # appears in zero reference labels across seven annotated papers.
    return {"verdict": "FIELD_ABSENT", "code": "GAP_ABSENT",
            "reason": f"no {spec.dimension} value stated in {raw_text!r}"}


def validate_identifier(raw_text: str, spec: FieldSpec) -> dict:
    # Relevance is now checked twice by design, not redundantly: once
    # inside extract.py's ranking (so a bad top candidate can be skipped
    # in favor of a better one), and again here, as a final gate for
    # fields where extraction had NO alternative candidate to fall back
    # to. This second check is defense-in-depth, not dead code.
    if not check_relevance(spec.field_id, raw_text):
        # Topically nearby with no field-specific evidence means the field is
        # not stated here -- absence, not a parse failure.
        return {"verdict": "FIELD_ABSENT", "code": "GAP_ABSENT",
                "reason": f"{raw_text!r} is topically nearby but contains no "
                          f"field-specific evidence for {spec.field_id}"}
    return {"verdict": "FIELD_OK", "code": None, "reason": f"{raw_text!r} treated as a real identifier"}


def validate_text(raw_text: str, spec: FieldSpec) -> dict:
    if not check_relevance(spec.field_id, raw_text):
        # Topically nearby with no field-specific evidence means the field is
        # not stated here -- absence, not a parse failure.
        return {"verdict": "FIELD_ABSENT", "code": "GAP_ABSENT",
                "reason": f"{raw_text!r} is topically nearby but contains no "
                          f"field-specific evidence for {spec.field_id}"}
    return {"verdict": "FIELD_OK", "code": None, "reason": f"{raw_text!r} treated as usable text"}


# Surface forms that denote an enum value but never appear as the value itself.
# Keyed on the canonical value from the pack, so the TARGETS are pack-derived
# and only the synonyms are authored here. Each is a fact about scientific
# English, not about these nine papers -- "mice" means Mus musculus in any
# corpus -- which is what keeps this from being dev-set tuning.
_ENUM_SURFACE: dict[str, list[str]] = {
    "Mus musculus": [r"\bmice\b", r"\bmouse\b", r"\bmurine\b", r"\bMus\s+musculus\b"],
    "Rattus norvegicus": [r"\brats?\b", r"\bRattus\b"],
    "male": [r"\bmales?\b", r"\bmale\s+(?:mice|rats|animals)\b"],
    "female": [r"\bfemales?\b"],
    "both": [r"\bboth\s+sexes\b", r"\bmale\s+and\s+female\b"],
    "subcutaneous_flank": [r"\bsubcutaneous(?:ly)?\b", r"\bs\.?c\.?\b", r"\bflank\b"],
    "orthotopic": [r"\borthotopic(?:ally)?\b"],
    "intravenous": [r"\bintravenous(?:ly)?\b", r"\bi\.?v\.?\b", r"\btail\s+vein\b"],
    "intraperitoneal": [r"\bintraperitoneal(?:ly)?\b", r"\bi\.?p\.?\b"],
    "biological": [r"\bbiological\s+replicates?\b", r"\bindependent\s+experiments?\b"],
    "technical": [r"\btechnical\s+replicates?\b", r"\bin\s+triplicate\b"],
}


def _match_enum_value(raw_text: str, spec: FieldSpec) -> str | None:
    """Find a declared enum value in the text, by surface form.

    The old check compared the whole candidate SENTENCE against enum_values,
    which can never be true -- so all four enum fields emitted a gap on every
    paper, and always with the wrong code.
    """
    for value in spec.enum_values or []:
        if value in ("not_stated", "other"):
            continue  # sentinels, not something to find in prose
        literal = value.replace("_", " ")
        if re.search(rf"\b{re.escape(literal)}\b", raw_text, re.IGNORECASE):
            return value
        for pattern in _ENUM_SURFACE.get(value, []):
            if re.search(pattern, raw_text, re.IGNORECASE):
                return value
    return None


def validate_enum(raw_text: str, spec: FieldSpec) -> dict:
    value = _match_enum_value(raw_text, spec)
    if value is not None:
        return {"verdict": "FIELD_OK", "code": None,
                "reason": f"{raw_text!r} states {value!r}", "canonical_value": value}
    return {"verdict": "FIELD_ABSENT", "code": "GAP_ABSENT",
            "reason": f"none of {spec.enum_values} is stated in {raw_text!r}"}


def validate_boolean(raw_text: str, spec: FieldSpec) -> dict:
    """A boolean field needs an actual assertion, not a topically nearby line.

    Previously an unconditional FIELD_OK, which meant cellline.mycoplasma could
    never produce a finding -- while its relevance marker set sat in
    relevance.py unused, because nothing here ever called it.
    """
    if check_relevance(spec.field_id, raw_text):
        return {"verdict": "FIELD_OK", "code": None,
                "reason": f"{raw_text!r} asserts {spec.field_id}"}
    return {"verdict": "FIELD_ABSENT", "code": "GAP_ABSENT",
            "reason": f"no statement of {spec.field_id} in {raw_text!r}"}


# Nucleotide runs. Twelve is short for a primer but long enough that ordinary
# prose will not reach it by accident.
_SEQUENCE_CONTENT = re.compile(r"\b[ACGTUacgtu]{12,}\b")


def validate_sequence(raw_text: str, spec: FieldSpec) -> dict:
    """Previously an unconditional FIELD_OK, so qpcr.primer_sequences could
    never produce a finding. A deferral to a table is caught upstream by
    check_deferral_or_vague and never reaches here.
    """
    if _SEQUENCE_CONTENT.search(raw_text):
        return {"verdict": "FIELD_OK", "code": None,
                "reason": f"{raw_text!r} contains a nucleotide sequence"}
    return {"verdict": "FIELD_ABSENT", "code": "GAP_ABSENT",
            "reason": f"no sequence stated in {raw_text!r}"}


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

    trigger_result = check_deferral_or_vague(raw_text, spec)
    if trigger_result is not None:
        return trigger_result

    validator = _VALIDATORS_BY_TYPE.get(spec.type)
    if validator is None:
        return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_UNPARSEABLE",
                "reason": f"no validator registered for type {spec.type!r}"}

    return validator(raw_text, spec)