"""
show_findings.py -- prints the actual candidate text and reasoning for
every finding, across all papers, so you can read what the system
actually decided instead of just counting verdicts.
"""
import os
import yaml
from contract import FieldPack, canonicalize
from classify_study import classify_study
from extract import find_candidate
from validate import validate_field

with open("fields/oncobiology_v0.yaml", "r", encoding="utf-8") as f:
    pack_data = yaml.safe_load(f)
pack = FieldPack(**pack_data)

for filename in sorted(os.listdir("papers")):
    if not filename.endswith(".txt"):
        continue

    doc_id = filename.replace(".txt", "")
    with open(f"papers/{filename}", "r", encoding="utf-8") as f:
        raw = f.read()

    canonical_text, text_sha = canonicalize(raw)
    profile = classify_study(canonical_text, text_sha)

    print(f"\n{'='*90}")
    print(f"{doc_id}")
    print(f"{'='*90}")

    for spec in pack.applicable(profile):
        obs = find_candidate(canonical_text, text_sha, spec)
        outcome = validate_field(obs, spec)

        # Only show the interesting ones -- skip clean FIELD_OK to reduce noise
        if outcome["verdict"] == "FIELD_OK":
            continue

        print(f"\n  [{spec.field_id}] {outcome['verdict']} / {outcome['code']}")
        if obs:
            print(f"    text: {obs['raw_text'][:120]!r}")
        print(f"    why:  {outcome['reason'][:150]}")