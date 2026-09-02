"""
show_matches.py -- prints every field that section-scoping DID find a
candidate for, with the actual sentence, so you can read whether the
matches are genuinely correct, not just present. Same discipline as
show_findings.py, but focused on what NOW works after the fix, to verify
the 0-mismatch result isn't hiding new false positives.
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

    printed_header = False
    for spec in pack.applicable(profile):
        obs = find_candidate(canonical_text, text_sha, spec)
        outcome = validate_field(obs, spec)

        if obs is None:
            continue  # only showing fields where a candidate was actually found

        if not printed_header:
            print(f"\n{'='*90}")
            print(f"{doc_id}")
            print(f"{'='*90}")
            printed_header = True

        print(f"\n  [{spec.field_id}] {outcome['verdict']} / {outcome['code']}")
        print(f"    detector: {obs['detector']}")
        print(f"    text: {obs['raw_text'][:140]!r}")