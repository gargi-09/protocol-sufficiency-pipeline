"""
generate_predictions.py -- run analyze() on ONE paper, for fast iteration.

The pipeline itself lives in analyze.py; this is only a driver. Pass a doc_id
as argv[1], or edit DOC_ID below.

    python generate_predictions.py "Dos et al"
"""
from __future__ import annotations

import sys

import yaml

from analyze import analyze
from contract import FieldPack

DOC_ID = "wang2015_trem2_cell"  # default when no argv[1] is given


def main() -> None:
    doc_id = sys.argv[1] if len(sys.argv) > 1 else DOC_ID

    with open(f"papers/{doc_id}.txt", "r", encoding="utf-8") as f:
        raw = f.read()
    with open("fields/oncobiology_v0.yaml", "r", encoding="utf-8") as f:
        pack = FieldPack(**yaml.safe_load(f))

    report = analyze(raw, doc_id, pack, model=None)

    with open(f"predictions/{doc_id}.json", "w", encoding="utf-8") as f:
        f.write(report.model_dump_json(indent=2))

    print(f"Wrote predictions/{doc_id}.json with {len(report.findings)} findings")


if __name__ == "__main__":
    main()
