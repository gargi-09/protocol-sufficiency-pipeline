"""
Scoring harness. Do not edit -- this is what your submission is measured with.

Usage:
    python score.py --pred predictions/ --gold data/gold/ --pack fields/oncobiology_v0.yaml

Reports per-field and per-slice precision / recall / F1. There is deliberately
no single headline number: the slices differ by more than an order of magnitude
in base rate, and an aggregate is dominated by FIELD_ABSENT.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Gold label format (one JSON file per document):
# {
#   "doc_id": "...",
#   "text_sha": "...",
#   "profile": {"has_animals": true, ...},
#   "labels": [
#       {"field_id": "treatment.duration",
#        "verdict": "FIELD_UNRESOLVED",
#        "code": "GAP_DEFERRED_TO_REF",
#        "span": {"start": 1204, "end": 1226},
#        "blocking": true}
#   ]
# }
#
# "blocking" is the annotator's judgement that a replicating lab could not
# proceed without asking. It is not scored for correctness -- it is used to
# compute the false-positive rate on non-blocking findings, which is the
# adoption-critical number.

SPAN_TOLERANCE = 20   # characters; a finding within this of gold counts as located


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def prf(self) -> tuple[float, float, float]:
        p = self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0
        r = self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0
        f = 2 * p * r / (p + r) if (p + r) else 0.0
        return p, r, f

    @property
    def support(self) -> int:
        return self.tp + self.fn


def spans_match(a: dict, b: dict) -> bool:
    return abs(a["start"] - b["start"]) <= SPAN_TOLERANCE


def score_document(pred: dict, gold: dict) -> dict[str, Counts]:
    """Match on (field_id, code) with span tolerance."""
    buckets: dict[str, Counts] = defaultdict(Counts)

    gold_open = list(gold["labels"])
    matched_gold = set()

    for f in pred["findings"]:
        key = f["field_id"]
        hit = None
        for i, g in enumerate(gold_open):
            if i in matched_gold:
                continue
            if g["field_id"] != f["field_id"]:
                continue
            if g["code"] != f["code"]:
                continue
            if not spans_match(g["span"], f["span"]):
                continue
            hit = i
            break
        if hit is not None:
            matched_gold.add(hit)
            buckets[key].tp += 1
            buckets["ALL"].tp += 1
            buckets[f"code:{f['code']}"].tp += 1
        else:
            buckets[key].fp += 1
            buckets["ALL"].fp += 1
            buckets[f"code:{f['code']}"].fp += 1

    for i, g in enumerate(gold_open):
        if i not in matched_gold:
            buckets[g["field_id"]].fn += 1
            buckets["ALL"].fn += 1
            buckets[f"code:{g['code']}"].fn += 1

    return buckets


def slice_absent_vs_unresolved(all_buckets: dict[str, Counts]) -> dict[str, Counts]:
    """The slice that matters: can the system find present-but-unusable values?

    Any checklist tool scores well on GAP_ABSENT. Almost none score above zero
    on the GAP_VAGUE / GAP_UNPARSEABLE / GAP_DEFERRED_* family, because the
    field is filled. That family is the whole point of this exercise.
    """
    ABSENT_CODES = {"GAP_ABSENT"}
    UNUSABLE_CODES = {
        "GAP_VAGUE",
        "GAP_UNPARSEABLE",
        "GAP_DIMENSION_MISMATCH",
        "GAP_OUT_OF_RANGE",
        "GAP_RANGE_NOT_POINT",
        "GAP_DEFERRED_TO_REF",
        "GAP_DEFERRED_TO_DISPLAY",
    }

    absent = Counts()
    unusable = Counts()
    for key, c in all_buckets.items():
        if not key.startswith("code:"):
            continue
        code = key.split(":", 1)[1]
        if code in ABSENT_CODES:
            target = absent
        elif code in UNUSABLE_CODES:
            target = unusable
        else:
            # A new code was added without updating this slice. Fail loudly
            # rather than silently folding it into one bucket.
            raise ValueError(
                f"unclassified gap code {code!r}; add it to ABSENT_CODES or "
                f"UNUSABLE_CODES in score.py"
            )
        target.tp += c.tp
        target.fp += c.fp
        target.fn += c.fn
    return {"ABSENT": absent, "PRESENT_BUT_UNUSABLE": unusable}


def false_positive_rate_on_nonblocking(preds, golds) -> float:
    """Of the findings emitted, what fraction correspond to gold labels the
    annotator marked non-blocking, or to nothing at all?

    This is the adoption metric. A scientist who sees three irrelevant findings
    in their first paper stops reading the output, and no recall number
    recovers from that.
    """
    emitted = 0
    noise = 0
    for doc_id, pred in preds.items():
        gold = golds.get(doc_id)
        if gold is None:
            continue
        blocking_spans = [
            g["span"] for g in gold["labels"] if g.get("blocking", False)
        ]
        for f in pred["findings"]:
            emitted += 1
            if not any(spans_match(bs, f["span"]) for bs in blocking_spans):
                noise += 1
    return noise / emitted if emitted else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True, type=Path)
    ap.add_argument("--gold", required=True, type=Path)
    ap.add_argument("--pack", required=True, type=Path)
    args = ap.parse_args()

    pack = yaml.safe_load(args.pack.read_text())
    known_fields = {f["field_id"] for f in pack["fields"]}

    preds = {p.stem: json.loads(p.read_text()) for p in args.pred.glob("*.json")}
    golds = {p.stem: json.loads(p.read_text()) for p in args.gold.glob("*.json")}

    missing = set(golds) - set(preds)
    if missing:
        print(f"WARNING: no prediction for {len(missing)} gold documents: "
              f"{sorted(missing)[:5]}")

    totals: dict[str, Counts] = defaultdict(Counts)
    for doc_id, gold in golds.items():
        pred = preds.get(doc_id, {"findings": []})
        for k, c in score_document(pred, gold).items():
            totals[k].tp += c.tp
            totals[k].fp += c.fp
            totals[k].fn += c.fn

    def show(title: str, keys: list[str]) -> None:
        print(f"\n{title}")
        print(f"{'key':<38} {'P':>6} {'R':>6} {'F1':>6} {'n':>6}")
        print("-" * 66)
        for k in keys:
            c = totals[k]
            p, r, f1 = c.prf()
            print(f"{k:<38} {p:>6.3f} {r:>6.3f} {f1:>6.3f} {c.support:>6}")

    show("BY GAP CODE", sorted(k for k in totals if k.startswith("code:")))
    show("BY FIELD", sorted(k for k in totals if k in known_fields))

    print("\nPRIMARY SLICE")
    print(f"{'slice':<38} {'P':>6} {'R':>6} {'F1':>6} {'n':>6}")
    print("-" * 66)
    for name, c in slice_absent_vs_unresolved(totals).items():
        p, r, f1 = c.prf()
        print(f"{name:<38} {p:>6.3f} {r:>6.3f} {f1:>6.3f} {c.support:>6}")

    fpr = false_positive_rate_on_nonblocking(preds, golds)
    print(f"\nFalse-positive rate on non-blocking findings: {fpr:.3f}")

    unknown = {
        f["field_id"]
        for p in preds.values() for f in p["findings"]
        if f["field_id"] not in known_fields
    }
    if unknown:
        print(f"\nERROR: predictions reference fields not in the pack: {sorted(unknown)}")


if __name__ == "__main__":
    main()
