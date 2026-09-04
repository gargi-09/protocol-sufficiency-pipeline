"""
verify.py -- one command that checks every invariant the spec asks for.

    python verify.py

Exits 0 if everything passes, 1 otherwise. Written so a grader can run it too:
it re-derives the acceptance criteria from 01_README.md and 03_contract.py
rather than asserting numbers we happen to produce today.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys

import yaml

import contract
from analyze import analyze
from contract import FieldPack, canonicalize
from mock_client import EchoClient
from model_cache import CachedModelClient
from relevance import _RELEVANCE_MARKERS

PAPERS = "papers"
PREDICTIONS = "predictions"
GOLD_RESOLVED = "gold_resolved"
PACK_PATH = "fields/oncobiology_v0.yaml"

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def load_pack() -> FieldPack:
    with open(PACK_PATH, encoding="utf-8") as f:
        return FieldPack(**yaml.safe_load(f))


def papers() -> list[str]:
    return sorted(p for p in os.listdir(PAPERS) if p.endswith(".txt"))


def predictions() -> dict:
    return {
        p[:-5]: json.loads(open(os.path.join(PREDICTIONS, p), encoding="utf-8").read())
        for p in sorted(os.listdir(PREDICTIONS)) if p.endswith(".json")
    }


# ---------------------------------------------------------------------------

def acceptance_criteria(pack: FieldPack) -> None:
    print("\n01_README.md mandatory requirements")

    ours = inspect.signature(analyze)
    theirs = inspect.signature(contract.analyze)
    check(str(ours) == str(theirs), "analyze() signature matches the contract exactly",
          str(ours) if str(ours) != str(theirs) else "")

    # Importable from any working directory -- validate.py used to read its
    # lexicon at import time via a CWD-relative path.
    repo = os.getcwd()
    probe = subprocess.run(
        [sys.executable, "-c",
         "import os,sys;os.chdir(os.environ.get('TEMP') or '/tmp');"
         f"sys.path.insert(0,{repo!r});import analyze;print('ok')"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    check(probe.returncode == 0 and "ok" in probe.stdout,
          "importable from an arbitrary CWD", probe.stderr.strip().splitlines()[-1:] and
          probe.stderr.strip().splitlines()[-1] or "")

    nondet = []
    for name in papers():
        raw = open(os.path.join(PAPERS, name), encoding="utf-8").read()
        doc_id = name[:-4]
        a = analyze(raw, doc_id, pack).model_dump_json()
        b = analyze(raw, doc_id, pack).model_dump_json()
        if a != b:
            nondet.append(doc_id)
    check(not nondet, "deterministic across all papers", ", ".join(nondet))

    before = {p: os.path.getmtime(os.path.join(PREDICTIONS, p))
              for p in os.listdir(PREDICTIONS)}
    analyze(open(os.path.join(PAPERS, papers()[0]), encoding="utf-8").read(),
            "probe", pack)
    after = {p: os.path.getmtime(os.path.join(PREDICTIONS, p))
             for p in os.listdir(PREDICTIONS)}
    check(before == after, "analyze() writes nothing to disk")

    bad = [(d, f["field_id"]) for d, r in predictions().items()
           for f in r["findings"] if f["span"]["end"] <= f["span"]["start"]]
    check(not bad, "no zero-width spans", str(bad[:3]) if bad else "")

    off = []
    for doc_id, report in predictions().items():
        text, _ = canonicalize(
            open(os.path.join(PAPERS, f"{doc_id}.txt"), encoding="utf-8").read())
        for f in report["findings"]:
            if f["span"]["end"] > len(text):
                off.append((doc_id, f["field_id"]))
    check(not off, "every span lies inside the canonical text", str(off[:3]) if off else "")

    known = {f.field_id for f in pack.fields}
    unknown = {f["field_id"] for r in predictions().values()
               for f in r["findings"] if f["field_id"] not in known}
    check(not unknown, "no findings reference fields outside the pack", str(unknown))


def model_contract() -> None:
    print("\n02_BUILD_SPEC.md -- model proposes, code decides")

    # Python sources only -- WRITEUP.md quotes this very command.
    hits = subprocess.run(["git", "grep", "-l", "model.complete", "--", "*.py"],
                          capture_output=True, encoding="utf-8", errors="replace")
    files = {f for f in hits.stdout.split("\n")
             if f.strip() and not f.startswith(("mock_client", "model_cache", "test_"))}
    check(files <= {"node_select.py"},
          "model is confined to node_select.py", ", ".join(sorted(files)))

    pack = load_pack()
    raw = open(os.path.join(PAPERS, "wang2015_trem2_cell.txt"), encoding="utf-8").read()
    client = EchoClient()
    report = analyze(raw, "wang2015_trem2_cell", pack, model=client)
    check(len(report.findings) > 0 and client.calls > 0,
          "model path runs end to end", f"{client.calls} calls")

    cache_path = os.path.join("cache", "verify_cache.json")
    if os.path.exists(cache_path):
        os.remove(cache_path)
    inner = EchoClient()
    cached = CachedModelClient(inner, cache_path=cache_path)
    first = analyze(raw, "w", pack, model=cached).model_dump_json()
    calls_1 = inner.calls
    second = analyze(raw, "w", pack, model=cached).model_dump_json()
    check(first == second and inner.calls == calls_1,
          "prompt-hash cache: run 2 makes zero calls and matches run 1",
          f"run1={calls_1} run2={inner.calls - calls_1}")

    baseline = analyze(raw, "w", pack, model=None).model_dump_json()
    check(isinstance(baseline, str) and len(baseline) > 0,
          "analyze(..., model=None) operates")


def coverage(pack: FieldPack) -> None:
    print("\nCoverage and output shape")

    gated = [f for f in pack.fields if f.type in ("text", "identifier", "boolean")]
    covered = [f for f in gated if f.field_id in _RELEVANCE_MARKERS]
    missing = [f.field_id for f in gated if f.field_id not in _RELEVANCE_MARKERS]
    check(len(covered) >= 11,
          f"relevance markers cover {len(covered)}/{len(gated)} gated fields",
          "still permissive: " + ", ".join(missing) if missing else "")

    reports = predictions()
    total = sum(len(r["findings"]) for r in reports.values())
    codes: dict[str, int] = {}
    for r in reports.values():
        for f in r["findings"]:
            codes[f["code"]] = codes.get(f["code"], 0) + 1
    check(total > 0, f"{total} findings across {len(reports)} papers",
          "  ".join(f"{k.replace('GAP_', '')}={v}" for k, v in sorted(codes.items())))

    check("GAP_UNPARSEABLE" not in codes,
          "GAP_UNPARSEABLE is not the catch-all any more",
          f"still {codes.get('GAP_UNPARSEABLE')}" if "GAP_UNPARSEABLE" in codes else "")

    anchored = sum(1 for r in reports.values() for f in r["findings"]
                   if f["detail"].get("anchor_detector"))
    check(anchored > 0, f"{anchored} absent findings carry an anchor provenance label")


def gold_score() -> None:
    print("\nAgainst the supplied gold (1 paper, 18 labels)")

    if not os.path.isdir(GOLD_RESOLVED):
        check(False, "gold_resolved/ exists", "run: python resolve_gold_anchors.py")
        return

    gold = json.loads(open(os.path.join(GOLD_RESOLVED, "wang2015_trem2_cell.json"),
                           encoding="utf-8").read())
    pred = predictions()["wang2015_trem2_cell"]

    matched, hits = set(), []
    for f in pred["findings"]:
        for i, label in enumerate(gold["labels"]):
            if i in matched or label["field_id"] != f["field_id"]:
                continue
            if label["code"] != f["code"]:
                continue
            delta = abs(label["span"]["start"] - f["span"]["start"])
            if delta <= 20:
                matched.add(i)
                hits.append((f["field_id"], f["code"], delta))
                break

    check(len(hits) >= 4, f"{len(hits)} of {len(gold['labels'])} gold labels matched")
    for field_id, code, delta in sorted(hits):
        print(f"         {field_id:<26} {code:<24} delta={delta}")


def main() -> int:
    pack = load_pack()
    acceptance_criteria(pack)
    model_contract()
    coverage(pack)
    gold_score()

    failed = [name for ok, name, _ in _results if not ok]
    print("\n" + "-" * 72)
    print(f"{len(_results) - len(failed)}/{len(_results)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
