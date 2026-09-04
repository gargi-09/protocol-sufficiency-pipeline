"""
resolve_gold_anchors.py -- turn gold `anchor_text` into the `span` score.py needs.

The supplied gold files carry `anchor_text`; `06_score.py` dereferences
`g["span"]` unconditionally, so the scorer cannot run on them as shipped. The
resolver that closes the gap is referenced in 07_example_labels.json
(`pack_coverage_notes`: "A separate script resolves anchors to offsets against
canonicalized text") but is not in the bundle. This is that script. See
defects_found.md A2.

Two rules that matter:

1. Matching is whitespace-insensitive. `canonicalize()` collapses runs of spaces
   but PRESERVES newlines, while the gold anchors are written as flowing prose.
   At least one anchor does not resolve on an exact match for that reason --
   `"...flat-bottom plate coated with various lipids..."` sits across a line
   break in the paper. See defects_found.md A3.

2. Failures are reported, never skipped. A resolver that silently drops an
   unresolvable anchor inflates every score it then computes, because the
   dropped label cannot become a false negative.

Reads  gold/*.json   (left untouched -- it is supplied data)
Writes gold_resolved/*.json

    python resolve_gold_anchors.py
    python score.py --pred predictions/ --gold gold_resolved/ --pack fields/oncobiology_v0.yaml
"""
from __future__ import annotations

import json
import os
import re
import sys

from contract import canonicalize

GOLD_DIR = "gold"
OUT_DIR = "gold_resolved"
PAPERS_DIR = "papers"

# The R3 annotations use citation-style doc_ids; papers/ uses the source
# filenames. score.py matches predictions to gold by FILE STEM, so a resolved
# file has to be named for the paper, not for the annotation.
DOC_ID_ALIASES = {
    "dossantos2026_ssa_pc_celllines": "Dos et al",
    "du2024_mettl3_escc_scirep": "Du",
    "hosseini2010_bladder_mspcr":
        "Disease_20Markers_20-_202013_20-_20Ali_20Hosseini_20-_20Frequency_20of_20"
        "P16INK4a_20and_20P14ARF_20Genes_20Methylation_20and_20Its_20Impact_20on_20"
        "Bladder_20Cancer",
    "shien2017_nsclc_jak1stat3_mct": "Shein",
    "xiong2008_crc_jakstat3_neoplasia": "Xiong",
    "yadav2005_jakstat_socs3_jbc": "Yadav",
}


def paper_for(doc_id: str) -> str:
    return DOC_ID_ALIASES.get(doc_id, doc_id)


def resolve(anchor: str, text: str) -> tuple[int, int] | None:
    """Offsets of `anchor` in `text`, tolerating whitespace differences."""
    anchor = anchor.strip()
    if not anchor:
        return None

    exact = text.find(anchor)
    if exact != -1:
        return exact, exact + len(anchor)

    # Any run of whitespace in the anchor may be any run of whitespace in the
    # document -- including a newline that canonicalize() kept.
    flexible = r"\s+".join(re.escape(token) for token in anchor.split())
    match = re.search(flexible, text)
    if match:
        return match.start(), match.end()
    return None


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Resolve gold anchor_text into the character spans score.py needs.")
    parser.add_argument("--gold", default=GOLD_DIR,
                        help=f"directory of label files, searched recursively (default: {GOLD_DIR})")
    parser.add_argument("--out", default=OUT_DIR,
                        help=f"output directory (default: {OUT_DIR})")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    total = resolved_count = 0
    unresolved: list[tuple[str, str, str]] = []
    no_anchor: list[tuple[str, str]] = []

    # Recursive: the R3 annotations are nested one directory per paper.
    sources = sorted(
        os.path.join(root, name)
        for root, _dirs, names in os.walk(args.gold)
        for name in names if name.endswith(".json")
    )

    for source in sources:
        filename = os.path.basename(source)
        gold = json.loads(open(source, encoding="utf-8").read())
        if not gold.get("labels"):
            continue  # e.g. the "ambiguity bank" files carry `entries`, not labels

        doc_id = paper_for(gold.get("doc_id") or filename[:-5])

        paper_path = os.path.join(PAPERS_DIR, f"{doc_id}.txt")
        if not os.path.exists(paper_path):
            print(f"SKIP {filename}: no paper at {paper_path}")
            continue

        text, text_sha = canonicalize(open(paper_path, encoding="utf-8").read())

        out_labels = []
        for label in gold.get("labels", []):
            total += 1
            anchor = label.get("anchor_text") or ""
            if not anchor.strip():
                # 25 of the 83 R3 labels carry no anchor_text at all. They are
                # unscoreable by construction rather than a resolver failure, so
                # they are counted separately -- lumping them in with genuine
                # misses would misattribute a data gap to our matching.
                no_anchor.append((doc_id, label.get("field_id", "?")))
                continue
            span = resolve(anchor, text)
            if span is None:
                unresolved.append((doc_id, label.get("field_id", "?"), anchor[:70]))
                continue
            resolved_count += 1
            entry = dict(label)
            entry["span"] = {"start": span[0], "end": span[1], "text_sha": text_sha}
            out_labels.append(entry)

        out = {
            "doc_id": doc_id,
            "text_sha": text_sha,
            "profile": gold.get("profile", {}),
            "labels": out_labels,
        }
        with open(os.path.join(args.out, f"{doc_id}.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        n_blocking = sum(1 for label in out_labels if label.get("blocking"))
        print(f"{doc_id[:46]:<48} {len(out_labels):>3}/{len(gold.get('labels', []))} resolved"
              f"   blocking={n_blocking}")

    print(f"\nresolved {resolved_count}/{total} anchors -> {args.out}/")

    if no_anchor:
        print(f"\nNO ANCHOR TEXT ({len(no_anchor)}) -- unscoreable by construction, "
              f"not a matching failure:")
        for doc_id, field_id in no_anchor:
            print(f"   {doc_id[:26]:<28} {field_id}")

    if unresolved:
        print(f"\nUNRESOLVED ({len(unresolved)}) -- anchor text present but not found "
              f"in the paper. Any score computed below is optimistic:")
        for doc_id, field_id, anchor in unresolved:
            print(f"   {doc_id[:26]:<28} {field_id:<30} {anchor!r}")

    # score.py's false_positive_rate_on_nonblocking reads label["blocking"],
    # which the supplied gold does not carry. Without it every label is treated
    # as non-blocking and the metric pins at 1.000 regardless of accuracy, so
    # say so rather than letting the number be read as a result.
    if not any(
        "blocking" in label
        for filename in os.listdir(args.out) if filename.endswith(".json")
        for label in json.loads(open(os.path.join(args.out, filename), encoding="utf-8").read())["labels"]
    ):
        print("\nNOTE: no label carries a `blocking` flag, so score.py's "
              "false-positive-rate-on-non-blocking is not interpretable here.")

    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
