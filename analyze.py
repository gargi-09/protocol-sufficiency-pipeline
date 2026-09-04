"""
analyze.py -- the single entry point the harness calls.

02_BUILD_SPEC: "The harness calls this function and no other function."
03_contract.py fixes the signature; this module implements it and nothing else.

Purity, per 01_README.md's mandatory requirements:
  - No disk writes. analyze() returns a Report; writing predictions/*.json is
    the caller's job (run_all_predictions.py).
  - No network. Nothing here calls out.
  - No global state. The one piece of module-level state that used to exist in
    this call graph -- validate.py's vagueness lexicon, built at import time
    from a CWD-relative path -- is now a lazily built, memoised, immutable
    singleton resolved against its own file location. See
    vagueness.get_vagueness_checker.

The `model` parameter reaches exactly one place: node_select.select_node, which
chooses WHICH subsection an absent field should have been stated in. It returns
an index into a list the code built, so the model can never emit a value, a
unit, a verdict, or a span. Every verdict in this system is still produced by
regex, lexicon, or arithmetic.

analyze(..., model=None) runs the same code path with the deterministic branch
only. That is the honest floor 01_README.md asks to see reported ("it is not a
failure condition"), and it is what every prediction in predictions/ is
currently generated with.
"""
from __future__ import annotations

from typing import Optional

from contract import (
    FieldPack,
    Finding,
    ModelClient,
    Report,
    Span,
    canonicalize,
    finding_id,
)
from classify_study import classify_study
from extract import find_absence_anchor, find_candidate
from validate import validate_field

# Only these two verdicts carry a gap. contract.Finding refuses to be
# constructed for FIELD_OK or FIELD_NA, so they must not reach it.
_REPORTABLE_VERDICTS = ("FIELD_ABSENT", "FIELD_UNRESOLVED")


def analyze(
    raw_text: str,
    doc_id: str,
    pack: FieldPack,
    model: Optional[ModelClient] = None,
) -> Report:
    """Steps 1-6 of 02_BUILD_SPEC, for one paper.

    Pure and deterministic: the same (raw_text, doc_id, pack) always yields the
    same Report, including the same finding_id values.
    """
    canonical_text, text_sha = canonicalize(raw_text)

    # Step 1 -- the study profile gates every field below it.
    profile = classify_study(canonical_text, text_sha)

    findings: list[Finding] = []

    # Step 2 -- loop over FIELDS, not over the text. An absent field produces no
    # text to extract, so only this direction can find one.
    for spec in pack.applicable(profile):
        # Step 3 -- propose a candidate. May be None; that is a real outcome.
        obs = find_candidate(canonical_text, text_sha, spec)

        # Step 4 -- the code decides. validate_field accepts None by design.
        outcome = validate_field(obs, spec)

        if outcome["verdict"] not in _REPORTABLE_VERDICTS:
            continue

        # Step 5 -- location. Every finding must carry a real span.
        detail = {"reason": outcome["reason"]}

        if obs is not None:
            span = obs["span"]
            # Step 3: "Make a record of the detector that made each observation.
            # You will need this record later." The absent branch below already
            # recorded its anchor's detector; the observation branch computed one
            # and threw it away, which is the half of the record the spec
            # actually asks for. Kept under a separate key from anchor_detector
            # because they answer different questions -- what found the value,
            # versus what found the place the value should have been.
            detail["detector"] = obs["detector"]
            # Step 5, present-but-unusable branch: "put the span on the
            # incorrect phrase." When the validator identified a trigger it
            # returns that trigger's offsets within the candidate, so rebase
            # them onto canonical text and narrow the span. A sentence-wide
            # span misses the scorer's 20-character tolerance even when the
            # field and code are both right.
            phrase = outcome.get("offset")
            if phrase is not None:
                start = span.start + phrase[0]
                end = span.start + phrase[1]
                if span.start <= start < end <= span.end:
                    span = Span(start=start, end=end, text_sha=text_sha)
                    detail["phrase"] = canonical_text[start:end]
        else:
            # The field is absent, so there is no observed text to point at.
            # Anchor on the place a reader should look instead. Dropping the
            # finding here would forfeit GAP_ABSENT entirely, and that is
            # roughly two thirds of the gold labels.
            anchor = find_absence_anchor(canonical_text, text_sha, spec, model=model)
            if anchor is None:
                # No non-empty sentence anywhere in the document. Unreachable
                # for any real paper; the contract rejects a zero-width span
                # and 02_BUILD_SPEC says a finding with no location is worse
                # than silence, so drop it rather than fabricate one.
                continue
            span = anchor["span"]
            detail["anchor_text"] = anchor["anchor_text"]
            detail["anchor_detector"] = anchor["detector"]

        findings.append(
            Finding(
                finding_id=finding_id(doc_id, spec.field_id, outcome["code"], span),
                field_id=spec.field_id,
                code=outcome["code"],
                span=span,
                verdict=outcome["verdict"],
                # No observation means no observed value. Keep raw_text null
                # rather than echoing the anchor sentence, which would imply we
                # found the value there. The anchor lives in detail instead,
                # mirroring the gold files' own "anchor_text" vocabulary.
                raw_text=obs["raw_text"] if obs else None,
                sensitivity=spec.sensitivity,
                detail=detail,
            )
        )

    # Step 6 -- assemble and return. Serialising to disk is the caller's job.
    return Report(
        doc_id=doc_id,
        text_sha=text_sha,
        field_pack_version=pack.version,
        profile=profile,
        findings=findings,
    )
