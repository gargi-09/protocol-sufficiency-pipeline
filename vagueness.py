"""
vagueness.py -- checks candidate text against the vagueness lexicon
(05_vagueness.yaml), the piece the README ranks as highest-priority to
get right: "recall on the group of fields that are present but not usable."

Three categories from the lexicon, checked in order:
1. qualitative_quantity -- a word standing in for a number ("high speed")
2. relative_condition -- a value defined by observation, not a number
   ("overnight", "until confluent")
3. deferred_reference / deferred_display -- already centralized in
   validate.py's check_deferral_or_vague(). NOT duplicated here.

This is a SEED lexicon, explicitly stated as such in the YAML's own
comments -- "a pure lexicon will not get you far." This function will
miss any vague phrasing not on the list. That's a known, stated limit,
not a bug to chase into a rabbit hole.
"""
from __future__ import annotations

import re
import yaml


def load_vagueness_lexicon(path: str = "vagueness.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _compile_word_list(words: list[str]) -> re.Pattern:
    # Longest phrases first, so "maximum speed" matches before "speed"
    # would (if "speed" alone were ever on the list -- it isn't here, but
    # this ordering rule generalizes safely regardless).
    escaped = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(escaped) + r")\b", re.IGNORECASE)


class VaguenessChecker:
    def __init__(self, lexicon: dict):
        self._qualitative_pattern = _compile_word_list(lexicon["qualitative_quantity"])
        self._relative_pattern = _compile_word_list(lexicon["relative_condition"])

    def check(self, raw_text: str) -> dict | None:
        """Returns a result dict if vague phrasing is found, else None
        (meaning: fall through, this text isn't vague by the lexicon).
        """
        m = self._qualitative_pattern.search(raw_text)
        if m:
            return {
                "verdict": "FIELD_UNRESOLVED",
                "code": "GAP_VAGUE",
                "reason": f"{raw_text!r} contains {m.group(0)!r}, a qualitative "
                          f"substitute for a quantity, not an actual value",
            }

        m = self._relative_pattern.search(raw_text)
        if m:
            return {
                "verdict": "FIELD_UNRESOLVED",
                "code": "GAP_VAGUE",
                "reason": f"{raw_text!r} contains {m.group(0)!r}, a relative "
                          f"condition defined by observation, not a stated value",
            }

        return None