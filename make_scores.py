"""
make_scores.py -- generate scores.txt, the deliverable-3 table.

    python -X utf8 make_scores.py

Deliverable 3 asks for "a table of scores from 06_score.py on the dev set, with
the scores of your model=None ablation". This runs the real 06_score.py -- never
a reimplementation of it -- once per (prediction set x reference set) and writes
the result with the provenance of each panel stated inline.

Everything here is derived. Nothing in scores.txt is typed by hand, so it cannot
drift from the predictions the way a pasted table does.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

SCORE = "score.py"
PACK = "fields/oncobiology_v0.yaml"
OUT = "scores.txt"

# (prediction dir, label, how a grader reproduces it)
PREDICTION_SETS = [
    ("predictions", "model=None  (deterministic layer only)",
     "reproducible with no credentials: python run_all_predictions.py"),
    ("predictions_model", "model=claude-opus-5  (node selection only)",
     "python run_all_predictions.py --model claude-opus-5 "
     "-- replays from cache/ if the cache is present, else needs an API key"),
]

# (gold dir, label, caveat)
REFERENCE_SETS = [
    ("gold_resolved", "graders' supplied gold",
     "1 paper, 18/18 anchors resolved. No `blocking` flags, so the "
     "false-positive-rate line is NOT interpretable here -- it pins at 1.000 "
     "regardless of accuracy. See defects_found.md D1."),
    ("gold_r3_resolved", "R3 annotations (secondary reference)",
     "6 papers, 49 of 83 labels scoreable -- 25 carry no anchor_text at all "
     "and 9 do not resolve against the paper. Different provenance from the "
     "gold above, and two self-flagged inconsistencies: defects_found.md E1-E2."),
]


def run_scorer(pred_dir: str, gold_dir: str) -> str:
    result = subprocess.run(
        [sys.executable, "-X", "utf8", SCORE,
         "--pred", pred_dir, "--gold", gold_dir, "--pack", PACK],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return f"(scorer failed)\n{result.stderr.strip()}"
    return result.stdout.rstrip()


def label_mix(gold_dir: str) -> str:
    counts: dict[str, int] = {}
    for name in sorted(os.listdir(gold_dir)):
        if not name.endswith(".json"):
            continue
        data = json.loads(open(os.path.join(gold_dir, name), encoding="utf-8").read())
        for label in data["labels"]:
            counts[label["code"]] = counts.get(label["code"], 0) + 1
    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    return (f"{total} labels: "
            + ", ".join(f"{k.replace('GAP_', '')} {v}" for k, v in ordered))


def main() -> int:
    missing = [d for d, _, _ in PREDICTION_SETS if not os.path.isdir(d)]
    for d in missing:
        print(f"note: {d}/ not present, skipping that panel")

    lines: list[str] = [
        "# Scores from 06_score.py -- generated, do not edit by hand.",
        "#",
        "#   python -X utf8 make_scores.py",
        "#",
        "# -X utf8 is required on Windows: score.py reads its inputs with",
        "# Path.read_text() and no encoding=, so it falls back to the locale codec",
        "# and dies on the non-Latin-1 characters our predictions quote.",
        "# See defects_found.md D0.",
        "#",
        "# Prerequisites:",
        "#   python run_all_predictions.py",
        "#   python run_all_predictions.py --model claude-opus-5      (optional)",
        "#   python resolve_gold_anchors.py",
        "#   python resolve_gold_anchors.py --gold \"R3 Dev Set\" --out gold_r3_resolved",
        "",
    ]

    for gold_dir, gold_label, gold_caveat in REFERENCE_SETS:
        if not os.path.isdir(gold_dir):
            continue
        for pred_dir, pred_label, pred_note in PREDICTION_SETS:
            if not os.path.isdir(pred_dir):
                continue
            lines += [
                "=" * 74,
                f" {pred_label}",
                f" against: {gold_label}  ({gold_dir}/)",
                "=" * 74,
                f" reference: {gold_caveat}",
                f" label mix: {label_mix(gold_dir)}",
                f" reproduce: {pred_note}",
                "",
                run_scorer(pred_dir, gold_dir),
                "",
                "",
            ]

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"wrote {OUT} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
