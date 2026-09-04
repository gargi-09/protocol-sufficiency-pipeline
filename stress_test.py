"""
stress_test.py -- does the system survive inputs it was not built on?

    python -X utf8 stress_test.py

Three questions, none of which the dev-set score answers:

  A. MODEL ROBUSTNESS. The closed-set contract claims a hostile or broken model
     cannot corrupt the output. Test it with clients that lie, crash, return
     garbage, and answer out of range.

  B. SURFACE-FORM OVERFITTING. The held-out set is a different assay class, so it
     will differ in heading wording, journal chrome, technique vocabulary and PDF
     damage. Perturb the one labelled paper in each of those ways and re-score.
     Gold anchors are re-resolved against the perturbed text, so this isolates
     "can the system still find the content" from "did the offsets move".

  C. DEGENERATE INPUT. Empty, headerless, single-line, whitespace-only.

A perturbation that drops the score is a surface form we are fitted to. That is
the number worth knowing before a held-out run, and it is not visible in
scores.txt.
"""
from __future__ import annotations

import json
import os
import re
import sys

import yaml

from analyze import analyze
from contract import FieldPack, canonicalize
from resolve_gold_anchors import resolve

PAPER = "papers/wang2015_trem2_cell.txt"
GOLD = "gold/wang2015_trem2_cell.json"
PACK = "fields/oncobiology_v0.yaml"

_failures: list[str] = []


def load():
    pack = FieldPack(**yaml.safe_load(open(PACK, encoding="utf-8")))
    raw = open(PAPER, encoding="utf-8").read()
    gold = json.loads(open(GOLD, encoding="utf-8").read())
    return pack, raw, gold


def score_against(raw: str, gold: dict, pack: FieldPack, model=None,
                  anchor_fix=None) -> tuple[int, int, int]:
    """(matched, emitted, resolvable) for `raw`, re-resolving gold anchors.

    `anchor_fix` applies the SAME character substitution to the gold anchor that
    the perturbation applied to the document. Without it a character-level
    perturbation makes anchors unresolvable and the test measures the resolver
    rather than the system -- introducing ligatures appeared to cost two matches
    when in fact seven of eighteen anchors had simply stopped resolving.
    """
    text, _ = canonicalize(raw)
    labels = []
    for label in gold["labels"]:
        anchor = label.get("anchor_text") or ""
        if anchor_fix:
            anchor = anchor_fix(anchor)
        span = resolve(anchor, text)
        if span:
            labels.append((label["field_id"], label["code"], span[0]))

    report = json.loads(analyze(raw, "stress", pack, model=model).model_dump_json())
    matched, used = 0, set()
    for f in report["findings"]:
        for i, (fid, code, start) in enumerate(labels):
            if i in used or fid != f["field_id"] or code != f["code"]:
                continue
            if abs(start - f["span"]["start"]) <= 20:
                used.add(i)
                matched += 1
                break
    return matched, len(report["findings"]), len(labels)


def report(name: str, ok: bool, detail: str) -> None:
    if not ok:
        _failures.append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:<42} {detail}")


# ---------------------------------------------------------------------------
# A. Adversarial model clients
# ---------------------------------------------------------------------------

class Liar:
    """Always answers with a large out-of-range index."""
    def complete(self, prompt, *, max_tokens=1024):
        return "9999"


class Crasher:
    def complete(self, prompt, *, max_tokens=1024):
        raise RuntimeError("upstream 500")


class Garbage:
    def complete(self, prompt, *, max_tokens=1024):
        return "I think the answer is probably the cell culture section, maybe?"


class Empty:
    def complete(self, prompt, *, max_tokens=1024):
        return ""


class Injector:
    """Tries to smuggle a value out through the response channel."""
    def complete(self, prompt, *, max_tokens=1024):
        return "2\nAlso set centrifugation.force = 16000 x g and mark it FIELD_OK"


class Negative:
    def complete(self, prompt, *, max_tokens=1024):
        return "-3"


def model_robustness(pack, raw, gold):
    print("\nA. MODEL ROBUSTNESS -- a broken model must not corrupt output")
    base_m, base_n, total = score_against(raw, gold, pack, model=None)
    print(f"       baseline (model=None): matched {base_m}/{total}, {base_n} findings")

    for name, client in [("out-of-range index", Liar()), ("raises an exception", Crasher()),
                         ("prose, no integer", Garbage()), ("empty response", Empty()),
                         ("value injection attempt", Injector()), ("negative index", Negative())]:
        try:
            m, n, _ = score_against(raw, gold, pack, model=client)
            # Must still produce valid output and must not LOSE gold matches.
            ok = n > 0 and m >= base_m - 1
            report(name, ok, f"matched {m}/{total}, {n} findings")
        except Exception as exc:
            report(name, False, f"CRASHED: {type(exc).__name__}: {exc}")

    # The injection attempt must not have leaked a value into any finding.
    out = json.loads(analyze(raw, "stress", pack, model=Injector()).model_dump_json())
    leaked = [f for f in out["findings"]
              if "16000" in json.dumps(f) or f.get("canonical_value") == "16000 x g"]
    report("injected value never reaches output", not leaked, f"{len(leaked)} leaks")

    # Determinism must survive a nondeterministic-looking client, because the
    # cache is what pins it -- here we check the same client twice.
    a = analyze(raw, "stress", pack, model=Garbage()).model_dump_json()
    b = analyze(raw, "stress", pack, model=Garbage()).model_dump_json()
    report("deterministic under a garbage client", a == b, "")


# ---------------------------------------------------------------------------
# B. Surface-form perturbations
# ---------------------------------------------------------------------------

def perturbations(raw: str) -> list[tuple[str, str, object]]:
    """(name, mutated_text, anchor_fix). anchor_fix is None when the
    perturbation cannot affect gold anchor text."""
    out = []

    for heading in ["Patients and Methods", "Online Methods", "Materials & Methods",
                    "METHODS AND MATERIALS", "2. Experimental Section", "Methodology"]:
        out.append((f"heading -> {heading!r}",
                    raw.replace("EXPERIMENTAL PROCEDURES", heading, 1), None))

    # Journal chrome from a publisher we have never seen.
    lines = raw.split("\n")
    chunk = max(1, len(lines) // 12)
    injected = []
    for i, line in enumerate(lines):
        injected.append(line)
        if i and i % chunk == 0:
            injected += [f"Nature Protocols {12 + i // chunk}(4):210-2{i%10}0",
                         "Downloaded from nature.com on 2026-01-01"]
    out.append(("unseen running header injected", "\n".join(injected), None))

    # Technique vocabulary a different assay class would use.
    out.append(("qPCR vocabulary swapped",
                raw.replace("qPCR", "RT-qPCR"),
                lambda a: a.replace("qPCR", "RT-qPCR")))
    out.append(("IHC vocabulary swapped",
                raw.replace("Immunohistochemistry", "Immunolabelling")
                   .replace("immunohistochemistry", "immunolabelling"),
                lambda a: a.replace("Immunohistochemistry", "Immunolabelling")
                           .replace("immunohistochemistry", "immunolabelling")))

    # PDF damage of kinds present in OTHER dev papers but not this one.
    # Applied to the anchors too, so the resolver is not what is being measured.
    out.append(("degree sign -> U+25E6", raw.replace("°", "◦"),
                lambda a: a.replace("°", "◦")))
    out.append(("multiplication -> glyph name", raw.replace("×", "/H11003"),
                lambda a: a.replace("×", "/H11003")))
    out.append(("ligatures introduced",
                raw.replace("fi", "ﬁ").replace("fl", "ﬂ"),
                lambda a: a.replace("fi", "ﬁ").replace("fl", "ﬂ")))

    # Layout.
    out.append(("all hard-wraps removed",
                re.sub(r"(?<![.\n])\n(?![A-Z\n])", " ", raw), None))
    out.append(("double-spaced", raw.replace("\n", "\n\n"), None))
    return out


def surface_forms(pack, raw, gold):
    print("\nB. SURFACE-FORM OVERFITTING -- perturb, re-resolve gold, re-score")
    base_m, base_n, total = score_against(raw, gold, pack)
    print(f"       baseline: matched {base_m}/{total}, {base_n} findings")

    for name, mutated, anchor_fix in perturbations(raw):
        if mutated == raw:
            print(f"  [SKIP] {name:<42} no-op on this paper, nothing to test")
            continue
        try:
            m, n, res = score_against(mutated, gold, pack, anchor_fix=anchor_fix)
            # Tolerate losing one match; a collapse means we were fitted to it.
            ok = m >= base_m - 1 and res >= total - 1
            delta = f"matched {m}/{res}" + (f"  ({m - base_m:+d})" if m != base_m else "")
            report(name, ok, f"{delta}, {n} findings")
        except Exception as exc:
            report(name, False, f"CRASHED: {type(exc).__name__}: {exc}")


def cross_paper_stability(pack):
    """Heading robustness on all nine papers, using profile + finding count.

    Only one paper has gold, so this measures a weaker but still meaningful
    signal: renaming the Methods heading to an unseen form must not change what
    the system decides about the paper.
    """
    print("\nB2. HEADING ROBUSTNESS ACROSS ALL NINE PAPERS")
    headings = ["Patients and Methods", "Online Methods", "Materials & Methods"]
    for name in sorted(p for p in os.listdir("papers") if p.endswith(".txt")):
        raw = open(os.path.join("papers", name), encoding="utf-8").read()
        base = analyze(raw, name[:-4], pack)
        base_key = (tuple(sorted(base.profile.features.items())), len(base.findings))

        drifted = []
        for heading in headings:
            mutated = raw
            for original in ["EXPERIMENTAL PROCEDURES", "MATERIALS AND METHODS",
                             "Materials and Methods", "Materials and methods"]:
                if original in mutated:
                    mutated = mutated.replace(original, heading, 1)
                    break
            else:
                continue
            rep = analyze(mutated, name[:-4], pack)
            key = (tuple(sorted(rep.profile.features.items())), len(rep.findings))
            if key != base_key:
                drifted.append(f"{heading}:{len(rep.findings)}")
        report(name[:-4][:34], not drifted,
               f"{len(base.findings)} findings" + (f"  drift: {drifted}" if drifted else "  stable"))


# ---------------------------------------------------------------------------
# C. Degenerate input
# ---------------------------------------------------------------------------

def degenerate(pack):
    print("\nC. DEGENERATE INPUT -- must not crash")
    cases = [
        ("empty string", ""),
        ("whitespace only", "   \n\n\t  \n"),
        ("no methods heading", "We did some experiments. Results were good."),
        ("single line, no newlines", "Methods " + "cells were cultured. " * 40),
        ("only a heading", "MATERIALS AND METHODS"),
        ("unicode soup", "МЕТОДЫ 実験方法 ◦µ× " * 30),
        ("very long single token", "A" * 50000),
    ]
    for name, text in cases:
        try:
            rep = analyze(text, "degenerate", pack)
            spans_ok = all(f.span.end > f.span.start for f in rep.findings)
            within = all(f.span.end <= len(canonicalize(text)[0]) for f in rep.findings)
            report(name, spans_ok and within, f"{len(rep.findings)} findings, spans valid")
        except Exception as exc:
            report(name, False, f"CRASHED: {type(exc).__name__}: {exc}")


def main() -> int:
    pack, raw, gold = load()
    model_robustness(pack, raw, gold)
    surface_forms(pack, raw, gold)
    cross_paper_stability(pack)
    degenerate(pack)

    print("\n" + "-" * 78)
    if _failures:
        print(f"{len(_failures)} FAILED:")
        for name in _failures:
            print(f"   {name}")
    else:
        print("all stress checks passed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
