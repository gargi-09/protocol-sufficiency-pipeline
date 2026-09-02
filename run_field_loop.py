"""
run_field_loop.py -- Step 2: the loop itself.

For EVERY field pack.applicable() says applies to this paper, call Step 3
(find_candidate) then Step 4 (validate_field) -- present or absent, always.
No field-specific logic lives here anymore; that's the point of
generalizing Steps 3 and 4 first.
"""
from __future__ import annotations

import yaml

from contract import FieldPack, canonicalize
from classify_study import classify_study
from extract import find_candidate
from validate import validate_field


def run_field_loop(canonical_text: str, text_sha: str, pack: FieldPack, profile) -> list[dict]:
    results = []
    for spec in pack.applicable(profile):
        obs = find_candidate(canonical_text, text_sha, spec)
        outcome = validate_field(obs, spec)
        results.append({
            "field_id": spec.field_id,
            "candidate": obs["raw_text"][:60] if obs else None,
            **outcome,
        })
    return results


if __name__ == "__main__":
    with open("papers/wang2015_trem2_cell.txt", "r", encoding="utf-8") as f:
        raw = f.read()
    canonical_text, text_sha = canonicalize(raw)

    profile = classify_study(canonical_text, text_sha)

    with open("fields/oncobiology_v0.yaml", "r", encoding="utf-8") as f:
        pack_data = yaml.safe_load(f)
    pack = FieldPack(**pack_data)

    applicable = pack.applicable(profile)
    print(f"Step 1 profile says this paper triggers {len(applicable)} of {len(pack.fields)} fields.\n")

    results = run_field_loop(canonical_text, text_sha, pack, profile)

    print(f"{'field_id':<32} {'verdict':<18} {'code':<24} candidate")
    print("-" * 130)
    for r in results:
        print(f"{r['field_id']:<32} {r['verdict']:<18} {str(r['code'] or '-'):<24} {r['candidate']}")