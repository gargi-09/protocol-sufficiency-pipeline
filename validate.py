from __future__ import annotations

import re

from contract import FieldSpec
from vagueness import clause_bounds, get_trigger_lexicon
from sections import _category_pattern
from relevance import check_relevance


_NUM = r"\d[\d,]*(?:\.\d+)?"

# A cell-line token sitting between the magnitude and its unit: "5 x 10^6 EC109
# cells". Required to contain BOTH a digit and a letter, which admits EC109,
# A549, 4T1 and MDA-MB-231 while rejecting the ordinary words that would
# otherwise let a magnitude bind to a far-away unit -- "100 ml of cells" must
# not read as 100 cells.
_LINE_TOKEN = r"(?=[A-Za-z0-9./-]*\d)(?=[A-Za-z0-9./-]*[A-Za-z])[A-Za-z0-9./-]+"

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
    rf"({_NUM}(?:\s*[×x✕]\s*10\s*\^?\s*\d+)?)\s*(?:\s{_LINE_TOKEN})?"
    rf"\s*(cells?|passages?|copies)\b"
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
            low = _magnitude(match.group(1) or "")
            if low is None:
                continue
            # group(2) is NOT reliably the upper bound. The unit pattern is
            # embedded in `form`, so its own capture groups follow group 1 --
            # and for a UNION dimension the non-matching branch's groups are
            # all None. `time_or_mass` against "mice weighing 20-25 g" matched
            # the mass branch, left the time branch's group 2 as None, and
            # None.replace() raised AttributeError, which this handler did not
            # catch: analyze() died on standard ARRIVE 2a phrasing. Take the
            # first group after the low value that reads as a number instead of
            # trusting an index. Unit groups ("g", "min") never parse, so they
            # are skipped for free.
            high = next(
                (value for value in
                 (_magnitude(group or "") for group in match.groups()[1:])
                 if value is not None),
                None,
            )
            if high is None:
                continue
            if low < high:
                return True
    return False


# --------------------------------------------------------------------------
# plausible_range -> GAP_OUT_OF_RANGE
#
# The pack declares plausible_range on four fields and contract.FieldSpec has
# carried the attribute since 0.1, but nothing read it, so GAP_OUT_OF_RANGE was
# structurally unreachable -- a supplied constraint on four fields silently
# ignored. 02_BUILD_SPEC puts this on the code side of the line it draws:
# "To check a range is a comparison. The model does none of these operations."
#
# Comparing against the range needs the value in the pack's canonical_unit, so
# each conversion is keyed on (dimension, canonical_unit) rather than dimension
# alone. That is deliberate: a future pack that declares culture.temperature in
# C instead of K gets NO key and therefore no range check, rather than a
# silent +273.15 against the wrong baseline. Failing to check beats checking
# against a unit we only assumed.
#
# This is a within-dimension scale conversion, not the rpm -> x g conversion the
# spec forbids. C and K measure the same dimension with a known offset; rpm and
# x g do not, which is why that case stays a GAP_DIMENSION_MISMATCH above.
_CANONICAL_CONVERSION = {
    # _TEMPERATURE_UNIT only matches Celsius forms, so a match is always C.
    ("temperature", "K"): lambda n: n + 273.15,
    ("fraction", "percent"): lambda n: n,
    ("count", "cells"): lambda n: n,
    ("relative_centrifugal_force", "g_rcf"): lambda n: n,
}

# "1 x 10^6", and the flattened "1 x 10 6" that PDF extraction leaves behind
# when it drops the superscript.
_SCIENTIFIC = re.compile(rf"({_NUM})\s*[×x✕]\s*10\s*\^?\s*(\d+)", re.IGNORECASE)


def _embedded_in_identifier(raw_text: str, start: int) -> bool:
    """True if the number at `start` is part of a token that contains letters.

    "EC109 cells were injected" read as a count of 109 cells, so
    xenograft.cell_number returned a confident verdict about a cell-line name.
    A lookbehind in _NUM cannot express this: MDA-MB-231 puts a hyphen, not a
    letter, immediately left of the digits, and excluding the hyphen too would
    break the "1000-4000" range forms, whose second number legitimately follows
    one. Python's re has no variable-length lookbehind, so walk the token
    instead -- the run of alphanumerics and hyphens to the left. A letter
    anywhere in that run means these digits belong to a name.

    Same class as the "rat" inside "proliferation" fix in sections.py: a match
    that is textually real and semantically nothing.
    """
    index = start
    while index > 0 and (raw_text[index - 1].isalnum() or raw_text[index - 1] == "-"):
        index -= 1
    return any(character.isalpha() for character in raw_text[index:start])


def _magnitude(token: str) -> float | None:
    """Numeric value of a captured token, or None if it is not a number."""
    token = token.strip()
    scientific = _SCIENTIFIC.fullmatch(token)
    if scientific:
        return float(scientific.group(1).replace(",", "")) * 10 ** int(scientific.group(2))
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def _magnitude_spans(match: re.Match) -> list[tuple[float, int]]:
    """(value, offset) for each group of `match` that reads as a number.

    The number is group 1 for most patterns but group 3 for _COUNT_UNIT's
    "passage N" alternative, and _RCF_UNIT's bare "rcf" alternative captures no
    number at all. Scanning the groups keeps the index out of the call sites.
    """
    spans: list[tuple[float, int]] = []
    for index in range(1, (match.re.groups or 0) + 1):
        token = match.group(index)
        if token is None:
            continue
        value = _magnitude(token)
        if value is not None:
            spans.append((value, match.start(index)))
    return spans


def _dimension_matches(pattern: re.Pattern, raw_text: str) -> list[re.Match]:
    """Matches whose magnitude is a real quantity rather than part of a name.

    A match with no readable magnitude is kept: a bare "rcf" states the unit and
    no value, which is a different failure from a value we refused to read.
    """
    kept: list[re.Match] = []
    for match in pattern.finditer(raw_text):
        spans = _magnitude_spans(match)
        if not spans or any(not _embedded_in_identifier(raw_text, offset)
                            for _, offset in spans):
            kept.append(match)
    return kept


def _canonical_values(pattern: re.Pattern, raw_text: str, spec: FieldSpec) -> list[float]:
    """Every readable value of the field's dimension, in its canonical unit."""
    convert = _CANONICAL_CONVERSION.get((spec.dimension, spec.canonical_unit))
    if convert is None:
        return []
    return [convert(value)
            for match in _dimension_matches(pattern, raw_text)
            for value, offset in _magnitude_spans(match)
            if not _embedded_in_identifier(raw_text, offset)]


def _check_plausible_range(pattern: re.Pattern, raw_text: str, spec: FieldSpec) -> dict:
    """FIELD_OK, or GAP_OUT_OF_RANGE if no stated value could be the real one.

    Called only once the dimension already matched, so the question here is
    narrower than "is this the right kind of value" -- it is "could this number
    be this field's value at all".

    A candidate sentence routinely carries several values of one dimension:
    "grown to 80% confluence in 5% CO2" holds two fractions and only the second
    is the CO2 fraction. So ANY value inside the range returns FIELD_OK, and the
    gap is emitted only when EVERY readable value of the dimension is outside
    it. Firing on the first out-of-range number instead would turn correct
    protocols into findings, and a false GAP_OUT_OF_RANGE is worse than a missed
    one: it does not merely add noise, it replaces a correct FIELD_OK.
    """
    ok = {"verdict": "FIELD_OK", "code": None,
          "reason": f"{raw_text!r} matches expected dimension {spec.dimension}"}
    if spec.plausible_range is None:
        return ok

    low, high = float(spec.plausible_range[0]), float(spec.plausible_range[1])
    values = _canonical_values(pattern, raw_text, spec)
    if not values:
        # Dimension matched but no magnitude is readable -- an unregistered
        # canonical unit, or a unit-only match like a bare "rcf". Not evidence
        # of implausibility, so keep the pre-existing verdict.
        return ok
    if any(low <= value <= high for value in values):
        return ok

    stated = ", ".join(f"{value:g}" for value in values)
    unit = spec.canonical_unit or spec.dimension
    return {"verdict": "FIELD_UNRESOLVED", "code": "GAP_OUT_OF_RANGE",
            "reason": f"{raw_text!r} states {spec.dimension} of {stated} {unit}, "
                      f"outside the plausible range {low:g}-{high:g} {unit}"}

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
        # _dimension_matches, not .search: a magnitude buried in a cell-line
        # name is not a statement of this dimension at all.
        if _dimension_matches(correct_pattern, raw_text):
            # Right dimension. The remaining question is whether the number
            # could be this field's value -- the pack's plausible_range.
            return _check_plausible_range(correct_pattern, raw_text, spec)

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