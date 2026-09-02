"""
run_all_papers.py -- smoke test: run Steps 1+2+3+4 against every real paper
in papers/, right now, to see if the pipeline generalizes or breaks.
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

    try:
        canonical_text, text_sha = canonicalize(raw)
        profile = classify_study(canonical_text, text_sha)
        applicable = pack.applicable(profile)

        n_ok, n_absent, n_unresolved = 0, 0, 0
        for spec in applicable:
            obs = find_candidate(canonical_text, text_sha, spec)
            outcome = validate_field(obs, spec)
            if outcome["verdict"] == "FIELD_OK":
                n_ok += 1
            elif outcome["verdict"] == "FIELD_ABSENT":
                n_absent += 1
            else:
                n_unresolved += 1

        print(f"{doc_id:<35} applicable={len(applicable):>2}  OK={n_ok:>2}  ABSENT={n_absent:>2}  UNRESOLVED={n_unresolved:>2}")
    except Exception as e:
        print(f"{doc_id:<35} CRASHED: {type(e).__name__}: {e}")