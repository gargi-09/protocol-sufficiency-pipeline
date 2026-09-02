"""
run_all_predictions.py -- the full pipeline, Steps 1-6, across every real
paper in papers/. Builds actual contract.Finding objects (not plain dicts),
wraps them in a Report, writes predictions/<doc_id>.json for each paper.

This is the first time the entire system runs end to end across the whole
dataset at once, not just Wang 2015 or a diagnostic summary. Also prints a
breakdown by GapCode across the whole run, so vagueness detection's actual
contribution is visible, not just assumed to be working because the code
exists.
"""
import os
import yaml
from collections import Counter

from contract import FieldPack, Finding, Report, canonicalize, finding_id
from classify_study import classify_study
from extract import find_candidate
from validate import validate_field

with open("fields/oncobiology_v0.yaml", "r", encoding="utf-8") as f:
    pack_data = yaml.safe_load(f)
pack = FieldPack(**pack_data)

os.makedirs("predictions", exist_ok=True)

overall_code_counts = Counter()
overall_verdict_counts = Counter()
per_paper_summary = []

for filename in sorted(os.listdir("papers")):
    if not filename.endswith(".txt"):
        continue

    doc_id = filename.replace(".txt", "")
    with open(f"papers/{filename}", "r", encoding="utf-8") as f:
        raw = f.read()

    canonical_text, text_sha = canonicalize(raw)
    profile = classify_study(canonical_text, text_sha)
    applicable = pack.applicable(profile)

    findings = []
    for spec in applicable:
        obs = find_candidate(canonical_text, text_sha, spec)
        outcome = validate_field(obs, spec)
        overall_verdict_counts[outcome["verdict"]] += 1
        if outcome["code"]:
            overall_code_counts[outcome["code"]] += 1

        if outcome["verdict"] not in ("FIELD_ABSENT", "FIELD_UNRESOLVED"):
            continue  # FIELD_OK produces no Finding -- nothing to report

        span = obs["span"] if obs else None
        if span is None:
            continue  # no real anchor -- skip rather than fabricate a location

        f_id = finding_id(doc_id, spec.field_id, outcome["code"], span)
        findings.append(Finding(
            finding_id=f_id,
            field_id=spec.field_id,
            code=outcome["code"],
            span=span,
            verdict=outcome["verdict"],
            raw_text=obs["raw_text"] if obs else None,
            sensitivity=spec.sensitivity,
            detail={"reason": outcome["reason"]},
        ))

    report = Report(
        doc_id=doc_id,
        text_sha=text_sha,
        field_pack_version=pack_data["version"],
        profile=profile,
        findings=findings,
    )

    with open(f"predictions/{doc_id}.json", "w", encoding="utf-8") as f:
        f.write(report.model_dump_json(indent=2))

    per_paper_summary.append((doc_id, len(applicable), len(findings)))

print(f"{'doc_id':<70} {'applicable':>10} {'findings':>10}")
print("-" * 92)
for doc_id, n_applicable, n_findings in per_paper_summary:
    print(f"{doc_id[:68]:<70} {n_applicable:>10} {n_findings:>10}")

print(f"\n\nVerdict breakdown across ALL papers:")
for verdict, count in overall_verdict_counts.most_common():
    print(f"  {verdict:<20} {count}")

print(f"\nGapCode breakdown across ALL papers:")
for code, count in overall_code_counts.most_common():
    print(f"  {code:<28} {count}")