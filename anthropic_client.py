"""
anthropic_client.py -- a real ModelClient, for the one place a model is used.

This is the extension point. `predictions/` and `scores.txt` are generated with
`model=None`; nothing here is required to reproduce them. It exists to show what
wiring a live model looks like, and to make the answer to "can we extend your
work" concrete rather than asserted.

Try it (needs ANTHROPIC_API_KEY in the environment or in a .env file):

    python -X utf8 anthropic_client.py
    python -X utf8 anthropic_client.py --model claude-haiku-4-5
    python -X utf8 anthropic_client.py --doc "Dos et al" --fresh

It runs one paper through analyze() with the model attached and prints every
node the model chose, so you can read its judgement directly. Each model gets
its own cache file, so tiers can be compared back to back without one serving
another's answers -- see cache_path_for().

WHAT THE MODEL IS ASKED. Exactly one question, in node_select.select_node:
"which subsection should have stated this field?" It answers with an index into
a list the code built. It cannot emit a value, a unit, a verdict or a span --
see node_select.py for why that is an invariant rather than a convention.

COST. 31 calls for all nine papers. Measured prompts average ~2.2 kB, and
thinking is on by default, so a full run is roughly 17k input + 6k output
tokens -- about $0.23 on claude-opus-5, $0.09 on sonnet-5, $0.05 on haiku-4-5.
Wrapped in CachedModelClient every repeat run is free, which is also how
02_BUILD_SPEC's determinism requirement is met. $5 of credit covers ~22 full
fresh runs on Opus.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5"

# Per-million-token rates, so the tracker can report real cost instead of
# asking you to supply numbers. Verify before trusting a figure in a report --
# these are a snapshot, and prices are the one thing in here with an expiry.
PRICING_PER_MTOK = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Node selection is a closed-set judgement over a handful of section headings --
# the shallow end of what any current model does. Effort "low" is the documented
# setting for simple and sub-agent tasks, and it matters here because thinking is
# on by default and thinking tokens bill as output. Raise it if you want to
# compare selection quality against cost.
EFFORT = "low"

# A full nine-paper run is 31 calls, ~23k tokens measured. The default budget
# allows four full runs plus experimentation, then refuses rather than
# continuing silently. Override with ANTHROPIC_TOKEN_BUDGET.
DEFAULT_TOKEN_BUDGET = 100_000
WARN_FRACTION = 0.75


class BudgetExceeded(RuntimeError):
    """Raised instead of making a call that would exceed the token budget.

    select_node already catches exceptions from the client and falls back to its
    deterministic choice, so exhausting the budget degrades the run to the
    model=None path rather than killing it. That is the failure mode you want
    from a spend guard.
    """


@dataclass
class Usage:
    """Exact token accounting, read from the API response.

    Token counts come from `message.usage` and are exact. Cost is derived from
    PRICING_PER_MTOK, which is a dated snapshot -- prices are the one thing here
    with an expiry, so `price_per_mtok_in/out` override the table when you want
    to be certain, and an unknown model reports no cost rather than a wrong one.
    """
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    budget: int = DEFAULT_TOKEN_BUDGET
    warn_fraction: float = WARN_FRACTION
    model: str = ""
    price_per_mtok_in: float | None = None
    price_per_mtok_out: float | None = None
    _warned: bool = field(default=False, repr=False)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def check_before_call(self, estimated: int = 500) -> None:
        if self.total + estimated > self.budget:
            raise BudgetExceeded(
                f"token budget exhausted: {self.total:,} used of {self.budget:,}. "
                f"Raise the budget explicitly if this is intended."
            )

    def record(self, usage_obj) -> None:
        self.calls += 1
        self.input_tokens += getattr(usage_obj, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage_obj, "output_tokens", 0) or 0
        threshold = self.budget * self.warn_fraction
        if not self._warned and self.total >= threshold:
            self._warned = True
            print(
                f"\n  WARNING: {self.total:,} tokens used, "
                f"{self.total / self.budget:.0%} of the {self.budget:,} budget "
                f"({self.calls} calls). Further calls will refuse at 100%.\n",
                file=sys.stderr,
            )

    def rates(self) -> tuple[float | None, float | None]:
        """Explicit override first, then the table, then unknown."""
        if self.price_per_mtok_in is not None and self.price_per_mtok_out is not None:
            return self.price_per_mtok_in, self.price_per_mtok_out
        return PRICING_PER_MTOK.get(self.model, (None, None))

    def report(self) -> str:
        lines = [
            f"calls           {self.calls}",
            f"input tokens    {self.input_tokens:,}",
            f"output tokens   {self.output_tokens:,}",
            f"total           {self.total:,} of {self.budget:,} budget "
            f"({self.total / self.budget:.1%})",
        ]
        rate_in, rate_out = self.rates()
        if rate_in is not None and rate_out is not None:
            cost = self.input_tokens / 1e6 * rate_in + self.output_tokens / 1e6 * rate_out
            lines.append(f"estimated cost  ${cost:.4f}   "
                         f"(${rate_in:.2f}/${rate_out:.2f} per MTok, {self.model or 'unknown'})")
        else:
            lines.append(f"estimated cost  no rate on file for {self.model!r} -- "
                         f"set price_per_mtok_in/out")
        return "\n".join("  " + line for line in lines)


def _load_env_file(path: str = ".env") -> list[str]:
    """Minimal KEY=VALUE reader, so a .env works without adding a dependency.

    Returns the names it set, so a caller can report what was found without
    ever printing a value.

    utf-8-sig, not utf-8: Windows PowerShell writes UTF-8 WITH a BOM by
    default, and a BOM makes the FIRST key parse as "\\ufeffANTHROPIC_..." --
    which silently never matches while every later line works fine. That
    produced a real "I added it and it's ignored" failure. Also tolerates
    `export KEY=value` and surrounding quotes.
    """
    env_path = Path(path)
    if not env_path.exists():
        return []
    loaded = []
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip().lstrip("﻿")
        value = value.strip().strip("'\"")
        if key and value:
            os.environ.setdefault(key, value)
            loaded.append(key)
    return loaded


class AnthropicClient:
    """Satisfies contract.ModelClient. Construct once, inject into analyze().

    The SDK client is built lazily so importing this module without a key is
    harmless -- verify.py and the test suite import the package tree freely.
    """

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 token_budget: int = DEFAULT_TOKEN_BUDGET,
                 workspace_id: str | None = None) -> None:
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        self._api_key = api_key
        self._workspace_id = workspace_id
        self._client = None
        self.usage = Usage(model=self.model,
                           budget=int(os.environ.get("ANTHROPIC_TOKEN_BUDGET",
                                                     token_budget)))

    @property
    def calls(self) -> int:
        return self.usage.calls

    def _ensure(self):
        if self._client is None:
            from anthropic import Anthropic  # imported here, not at module load
            _load_env_file()
            key = self._api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError(
                    "No ANTHROPIC_API_KEY found. Put it in the environment or in "
                    "a .env file beside this module. It is never read into code "
                    "or written to any output."
                )

            # An identity-linked API key must also name the workspace it acts
            # in, or every request fails with a 400:
            #   "anthropic-workspace-id is required when authenticating with an
            #    identity-linked API key"
            # The first-party client has no workspace_id parameter (only the AWS
            # client does), so it goes through default_headers. Find the id in
            # the Console under Settings -> Workspaces; it looks like wrkspc_...
            # Plain (non-identity-linked) keys ignore this header, so setting it
            # is harmless either way.
            headers = {}
            workspace = self._workspace_id or os.environ.get("ANTHROPIC_WORKSPACE_ID")
            if workspace:
                headers["anthropic-workspace-id"] = workspace

            self._client = Anthropic(api_key=key, default_headers=headers or None)
        return self._client

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
        """Return raw completion text. Errors propagate to select_node, which
        falls back to its deterministic choice rather than failing the run."""
        client = self._ensure()
        # Refuse BEFORE spending, not after. Rough estimate: ~4 chars per token
        # for the prompt, plus the requested output ceiling.
        self.usage.check_before_call(estimated=len(prompt) // 4 + max_tokens)

        try:
            message = client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                output_config={"effort": EFFORT},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            if "workspace" in str(exc).lower() and not (
                self._workspace_id or os.environ.get("ANTHROPIC_WORKSPACE_ID")
            ):
                raise RuntimeError(
                    "This API key is identity-linked and needs a workspace id. "
                    "Add ANTHROPIC_WORKSPACE_ID=wrkspc_... to your .env "
                    "(Console -> Settings -> Workspaces)."
                ) from exc
            raise
        self.usage.record(getattr(message, "usage", None))
        return "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )


def cache_path_for(model: str) -> str:
    """One cache file per model.

    The cache keys on the prompt, and the prompt is identical across models --
    so a single shared cache would serve Haiku's answers to Opus and make any
    comparison meaningless. Scoping the file per model is what makes
    `--model` safe to run back to back.
    """
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in model)
    return f"cache/model_cache.{safe}.json"


if __name__ == "__main__":
    import argparse

    import yaml

    from analyze import analyze
    from contract import FieldPack
    from model_cache import CachedModelClient

    parser = argparse.ArgumentParser(
        description="Run one paper through analyze() with a live model attached.")
    parser.add_argument("--model", default=None,
                        help=f"model id (default: $ANTHROPIC_MODEL or {DEFAULT_MODEL})")
    parser.add_argument("--doc", default="wang2015_trem2_cell",
                        help="doc_id under papers/ (default: wang2015_trem2_cell)")
    parser.add_argument("--budget", type=int, default=DEFAULT_TOKEN_BUDGET,
                        help=f"token budget (default: {DEFAULT_TOKEN_BUDGET:,})")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore any cached answers for this model and re-ask")
    args = parser.parse_args()

    pack = FieldPack(**yaml.safe_load(open("fields/oncobiology_v0.yaml", encoding="utf-8")))
    raw = open(f"papers/{args.doc}.txt", encoding="utf-8").read()

    inner = AnthropicClient(model=args.model, token_budget=args.budget)
    path = cache_path_for(inner.model)
    if args.fresh and os.path.exists(path):
        os.remove(path)
    client = CachedModelClient(inner, cache_path=path)

    report = analyze(raw, args.doc, pack, model=client)

    print(f"model: {inner.model}   doc: {args.doc}   "
          f"findings: {len(report.findings)}   cache: {path}\n")
    print("subsections the model chose for absent fields:")
    chosen = 0
    for finding in report.findings:
        detector = finding.detail.get("anchor_detector", "")
        if "model-" in detector:
            chosen += 1
            print(f"   {finding.field_id:<28} {detector.split(':', 1)[1]}")
    if not chosen:
        print("   (none -- every field resolved deterministically)")

    again = analyze(raw, args.doc, pack, model=client)

    print("\nUSAGE")
    print(inner.usage.report())
    print("\nCACHE")
    for key, value in client.stats().items():
        print(f"  {key:<22} {value}")
    print(f"\n  {client.misses} live calls, {client.hits} served from cache")
    print(f"  identical output across the two runs: "
          f"{report.model_dump_json() == again.model_dump_json()}")
