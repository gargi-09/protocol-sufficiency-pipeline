"""
generate_prediction.py -- Steps 5+6, for real. Builds actual contract.Finding
objects (not plain dicts) and writes a real predictions/<doc_id>.json file,
matching the Report schema score.py expects.
"""
import json
import yaml

from contract import FieldPack, Finding, Report, StudyProfile, canonicalize, finding_id
from classify_study import classify_study
from extract import find_candidate
from validate import validate_field

DOC_ID = "wang2015_trem2_cell"  # change this to run on a different paper

with open(f"papers/{DOC_ID}.txt", "r", encoding="utf-8") as f:
    raw = f.read()
canonical_text, text_sha = canonicalize(raw)

profile = classify_study(canonical_text, text_sha)

with open("fields/oncobiology_v0.yaml", "r", encoding="utf-8") as f:
    pack_data = yaml.safe_load(f)
pack = FieldPack(**pack_data)

findings = []
for spec in pack.applicable(profile):
    obs = find_candidate(canonical_text, text_sha, spec)
    outcome = validate_field(obs, spec)

    # FIELD_OK carries no gap -- contract.Finding refuses to be constructed
    # with an empty code set for FIELD_OK, so we simply don't emit a
    # Finding for it. Only ABSENT/UNRESOLVED produce a real Finding.
    if outcome["verdict"] not in ("FIELD_ABSENT", "FIELD_UNRESOLVED"):
        continue

    span = obs["span"] if obs else None
    if span is None:
        # Per the build spec: for an absent field with no anchor sentence
        # found at all, we'd need to fall back to paragraph/section. For
        # now, honestly skip rather than fabricate a zero-width span --
        # the contract explicitly rejects zero-width spans, and a fake
        # span would be worse than no Finding, per the spec's own words:
        # "a finding that has no location costs attention and gives
        # nothing back."
        continue

    f_id = finding_id(DOC_ID, spec.field_id, outcome["code"], span)
    finding = Finding(
        finding_id=f_id,
        field_id=spec.field_id,
        code=outcome["code"],
        span=span,
        verdict=outcome["verdict"],
        raw_text=obs["raw_text"] if obs else None,
        sensitivity=spec.sensitivity,
        detail={"reason": outcome["reason"]},
    )
    findings.append(finding)

report = Report(
    doc_id=DOC_ID,
    text_sha=text_sha,
    field_pack_version=pack_data["version"],
    profile=profile,
    findings=findings,
)

with open(f"predictions/{DOC_ID}.json", "w", encoding="utf-8") as f:
    f.write(report.model_dump_json(indent=2))

print(f"Wrote predictions/{DOC_ID}.json with {len(findings)} findings")