"""
build_all.py -- one command: run the pipeline, generate every deliverable, check it.

    python -X utf8 build_all.py                 # no API key needed, no spend
    python -X utf8 build_all.py --with-model    # also regenerate the model run

Stages, in dependency order:

  1. predictions/            deliverable 2, model=None
  2. predictions_model/      optional model run (only with --with-model)
  3. gold_resolved/          gold anchors -> spans, so score.py can run at all
  4. gold_r3_resolved/       the secondary reference, kept separate
  5. scores.txt              deliverable 3, both configurations
  6. verify.py               19 acceptance checks from 01_README / 03_contract
  7. stress_test.py          perturbation + adversarial-input suite

Then it prints a deliverables checklist and exits non-zero if anything is
missing or any check failed, so "did the whole thing work" is one exit code
rather than seven things to read.

Scoring an EXISTING predictions_model/ is free -- the model panel appears in
scores.txt without spending anything. --with-model regenerates it, which
replays from cache/ when the cache is present and otherwise calls the API.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

PY = [sys.executable, "-X", "utf8"]

_failures: list[str] = []


def stage(number: str, title: str) -> None:
    print(f"\n{'=' * 72}\n {number}. {title}\n{'=' * 72}")


def run(argv: list[str], label: str, tail: int = 6, optional: bool = False) -> bool:
    result = subprocess.run(PY + argv, capture_output=True,
                            encoding="utf-8", errors="replace")
    output = (result.stdout or "").rstrip().splitlines()
    for line in output[-tail:]:
        print(f"   {line}")
    if result.returncode != 0:
        err = (result.stderr or "").strip().splitlines()
        for line in err[-4:]:
            print(f"   ! {line}")
        if not optional:
            _failures.append(label)
        return False
    return True


def count_json(directory: str) -> int:
    if not os.path.isdir(directory):
        return 0
    return sum(1 for name in os.listdir(directory) if name.endswith(".json"))


def has_api_key() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        from anthropic_client import _load_env_file
        return "ANTHROPIC_API_KEY" in _load_env_file()
    except Exception:
        return False


def main() -> int:
    with_model = "--with-model" in sys.argv

    stage("1", "predictions/  (deliverable 2, model=None)")
    run(["run_all_predictions.py"], "run_all_predictions", tail=8)

    stage("2", "predictions_model/  (optional model run)")
    if with_model:
        if has_api_key():
            run(["run_all_predictions.py", "--model", "claude-opus-5"],
                "run_all_predictions --model", tail=8)
        else:
            print("   skipped: --with-model given but no ANTHROPIC_API_KEY found")
    elif os.path.isdir("predictions_model"):
        print(f"   reusing existing predictions_model/ "
              f"({count_json('predictions_model')} files) -- free to score")
    else:
        print("   skipped: pass --with-model to generate it (costs ~$0.23)")

    stage("3", "gold_resolved/  (gold anchor_text -> spans)")
    run(["resolve_gold_anchors.py"], "resolve_gold_anchors", tail=3)

    stage("4", "gold_r3_resolved/  (secondary reference)")
    run(["resolve_gold_anchors.py", "--gold", "R3 Dev Set",
         "--out", "gold_r3_resolved"], "resolve R3", tail=3, optional=True)

    stage("5", "scores.txt  (deliverable 3)")
    run(["make_scores.py"], "make_scores", tail=2)

    stage("6", "verify.py  (acceptance criteria)")
    run(["verify.py"], "verify", tail=3)

    stage("7", "stress_test.py  (robustness)")
    run(["stress_test.py"], "stress_test", tail=3)

    # ---------------------------------------------------------------- report
    stage("*", "DELIVERABLES")

    checks: list[tuple[bool, str, str]] = [
        (count_json("predictions") == 9,
         "2. predictions/", f"{count_json('predictions')} JSON files (expect 9)"),
        (os.path.exists("scores.txt") and os.path.getsize("scores.txt") > 0,
         "3. scores.txt", ""),
        (os.path.exists("WRITEUP.md"),
         "4. WRITEUP.md",
         f"{len(open('WRITEUP.md', encoding='utf-8').read().split())} words"
         if os.path.exists("WRITEUP.md") else "missing"),
        (os.path.exists("defects_found.md"),
         "   defects_found.md", ""),
        (os.path.exists("README.md"),
         "   README.md", ""),
        (os.path.exists("requirements.txt"),
         "   requirements.txt", ""),
    ]

    # Deliverable 3 wants the primary result AND the model=None ablation.
    #
    # Count distinct CONFIGURATIONS, not panels. Counting panels was wrong: two
    # reference sets times one configuration is also two panels, so the check
    # passed while reporting only model=None.
    configurations: set[str] = set()
    if os.path.exists("scores.txt"):
        for line in open("scores.txt", encoding="utf-8").read().splitlines():
            if line.startswith(" model="):
                configurations.add(line.strip().split("  ")[0])

    checks.append((bool(configurations), "3. scored configurations",
                   ", ".join(sorted(configurations)) or "none"))

    for ok, name, detail in checks:
        if not ok:
            _failures.append(name.strip())
        print(f"   [{'OK' if ok else '--'}] {name:<32} {detail}")

    # A single configuration is a legitimate submission -- 01_README.md says the
    # model=None number "is not a failure condition" -- so this is a warning,
    # never a build failure. Failing a build for not having spent money on an
    # optional run would be the wrong default.
    if len(configurations) < 2:
        print("\n   NOTE: only one configuration in scores.txt. Deliverable 3 asks "
              "for the\n         primary result plus the model=None ablation. Run "
              "with --with-model\n         to add the second, or state in WRITEUP.md "
              "that model=None is the\n         shipped configuration and the only "
              "one reported.")

    # Anything left for a human to fill in?
    if os.path.exists("WRITEUP.md"):
        todos = [i + 1 for i, line in
                 enumerate(open("WRITEUP.md", encoding="utf-8").read().splitlines())
                 if "TODO" in line]
        if todos:
            print(f"\n   WRITEUP.md still has TODO on lines: "
                  f"{', '.join(map(str, todos))}")

    print(f"\n{'-' * 72}")
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for name in _failures:
            print(f"   {name}")
        return 1
    print("pipeline ran clean and every generated deliverable is present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
