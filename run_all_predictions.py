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


def build_model(model_id: str | None):
    """A live client, or None for the deterministic layer alone.

    Constructed HERE, outside analyze(), because 03_contract.py requires the
    client to be injected and never built inside a module. Wrapped in the
    prompt-hash cache so a repeat run costs nothing and finding_ids are pinned.
    """
    if not model_id:
        return None
    from anthropic_client import AnthropicClient, cache_path_for
    from model_cache import CachedModelClient

    inner = AnthropicClient(model=model_id)
    return CachedModelClient(inner, cache_path=cache_path_for(inner.model))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run analyze() over every paper and write predictions/.")
    parser.add_argument("--model", default=None,
                        help="attach a live model (default: none -- the shipped configuration)")
    parser.add_argument("--out", default=None,
                        help="output directory (default: predictions/, or "
                             "predictions_model/ when --model is set)")
    args = parser.parse_args()

    # Never let a model run overwrite the shipped model=None deliverable by
    # accident -- that set is what scores.txt reports and what a grader
    # reproduces with no credentials.
    out_dir = args.out or (PREDICTIONS_DIR if not args.model else "predictions_model")

    # analyze() is handed the pack; it must not read it off disk itself.
    with open(PACK_PATH, "r", encoding="utf-8") as f:
        pack = FieldPack(**yaml.safe_load(f))

    os.makedirs(out_dir, exist_ok=True)
    model = build_model(args.model)
    print(f"model: {args.model or 'None (deterministic layer only)'}"
          f"   ->  {out_dir}/\n")

    code_counts: Counter[str] = Counter()
    rows = []

    for filename in sorted(os.listdir(PAPERS_DIR)):
        if not filename.endswith(".txt"):
            continue

        doc_id = filename.replace(".txt", "")
        with open(os.path.join(PAPERS_DIR, filename), "r", encoding="utf-8") as f:
            raw = f.read()

        report = analyze(raw, doc_id, pack, model=model)

        out_path = os.path.join(out_dir, f"{doc_id}.json")
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
