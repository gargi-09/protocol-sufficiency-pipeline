"""
mock_client.py -- stand-in ModelClient implementations. No API key, no network.

03_contract.py requires that the model client be injected: "We must be able to
change the client, and to run the function with model=None." These mocks are
what make that claim checkable without credentials -- they let anyone (us, or
the graders) exercise the model path end to end and confirm the injection point,
the prompt-hash cache, and the parse discipline all work.

None of these is a model. They are deterministic stand-ins used to test the
plumbing and to bound what node selection can buy before spending real tokens.
Each satisfies contract.ModelClient: a single complete(prompt, *, max_tokens).
"""
from __future__ import annotations

import re


class EchoClient:
    """Always answers the same index. Proves the plumbing, nothing else.

    Useful as a worst case: if the pipeline still produces valid, deterministic
    output when the model answers "1" to everything, then no downstream stage is
    quietly depending on the model being right.
    """

    def __init__(self, answer: str = "1") -> None:
        self.answer = answer
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        return self.answer


class ScriptedClient:
    """Replays a fixed list of answers in order, then falls back to "1".

    For tests that need a specific sequence of choices without a real model.
    """

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
        self.prompts.append(prompt)
        answer = self.answers[self.calls] if self.calls < len(self.answers) else "1"
        self.calls += 1
        return answer


_WORD = re.compile(r"[a-z]{4,}")
_STOPWORDS = {
    "with", "from", "that", "this", "were", "which", "ують", "field", "value",
    "used", "using", "must", "should", "their", "there", "than", "then",
    "have", "been", "does", "each", "such", "also", "into", "when", "will",
    "note", "candidate", "designation", "equivalent", "provide", "given",
}


class WordOverlapClient:
    """Picks the subsection whose title shares the most words with the field.

    A deliberately dumb stand-in for the reasoning a real model would do. It is
    NOT a model and must never be reported as one -- it has no world knowledge,
    so it cannot connect "mycoplasma" to "Ex Vivo Microglia Cultures" the way a
    model can. It exists to show the selection path doing something non-trivial
    and to give a floor: whatever a real model scores, it should beat this.

    Deterministic, so it is also usable as a regression fixture.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS}

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
        self.calls += 1
        self.prompts.append(prompt)

        # Only the Field: and Meaning: lines. Taking the whole preamble would
        # feed generic words ("methods", "section", "subsection") into the
        # comparison, which scores every option identically and silently
        # degrades this to EchoClient.
        field_block = " ".join(
            line for line in prompt.split("Subsections:")[0].splitlines()
            if line.startswith(("Field:", "Meaning:"))
        )
        wanted = self._tokens(field_block)
        options = re.findall(r"^(\d+)\. (.+)$", prompt, re.MULTILINE)
        if not options:
            return "1"

        best_number, best_score = options[0][0], -1
        for number, title in options:
            score = len(wanted & self._tokens(title))
            if score > best_score:
                best_number, best_score = number, score
        return best_number
