"""
run_all_predictions.py -- driver. Loads the field pack, calls analyze() once
per paper, writes predictions/<doc_id>.json.

All pipeline logic now lives in analyze.py. This file does the two things
analyze() is forbidden to do by 01_README.md: read the pack off disk, and
write output to disk.

Also prints a per-paper table and a GapCode breakdown across the whole run, so
the code distribution is visible rather than assumed. Note that FIELD_OK is no
longer counted here -- analyze() returns only Findings, and FIELD_OK carries no
Finding by construction. show_findings.py still prints every non-OK outcome
with its reason if you need that view.
"""
from __future__ import annotations

import os
from collections import Counter

import yaml

from analyze import analyze
from contract import FieldPack

PAPERS_DIR = "papers"
PREDICTIONS_DIR = "predictions"
PACK_PATH = "fields/oncobiology_v0.yaml"


def main() -> None:
    # analyze() is handed the pack; it must not read it off disk itself.
    with open(PACK_PATH, "r", encoding="utf-8") as f:
        pack = FieldPack(**yaml.safe_load(f))

    os.makedirs(PREDICTIONS_DIR, exist_ok=True)

    code_counts: Counter[str] = Counter()
    rows = []

    for filename in sorted(os.listdir(PAPERS_DIR)):
        if not filename.endswith(".txt"):
            continue

        doc_id = filename.replace(".txt", "")
        with open(os.path.join(PAPERS_DIR, filename), "r", encoding="utf-8") as f:
            raw = f.read()

        report = analyze(raw, doc_id, pack, model=None)

        out_path = os.path.join(PREDICTIONS_DIR, f"{doc_id}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report.model_dump_json(indent=2))

        for finding in report.findings:
            code_counts[finding.code] += 1

        rows.append((doc_id, len(pack.applicable(report.profile)), len(report.findings)))

    print(f"{'doc_id':<70} {'applicable':>10} {'findings':>10}")
    print("-" * 92)
    for doc_id, n_applicable, n_findings in rows:
        print(f"{doc_id[:68]:<70} {n_applicable:>10} {n_findings:>10}")

    print(f"\nTotal findings across {len(rows)} papers: {sum(code_counts.values())}")
    print("\nGapCode breakdown across ALL papers:")
    for code, count in code_counts.most_common():
        print(f"  {code:<28} {count}")


if __name__ == "__main__":
    main()
