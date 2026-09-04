"""
CachedModelClient -- wraps any ModelClient (per contract.py's Protocol) with
a persistent, content-addressed cache.

Why this exists, and why it's the first thing built:

1. Determinism (mandatory requirement in 02_BUILD_SPEC.py): "Run your code
   two times on the same paper. The finding_id values must be the same...
   you must keep the model responses in a cache with the hash of the prompt
   as the key, and read them again from the cache." This class IS that
   cache, built once, used everywhere a model is called.

2. Cost: the trial's vendor card has a $200/month ceiling. If the second of
   two required runs re-calls the API for every prompt, that's a doubled
   bill for zero new information. With this wrapper, the second run is a
   cache hit end to end and costs $0.

Design notes:
- Keyed on hash(prompt + max_tokens), not just prompt, because the same
  prompt text truncated differently is a different observed response.
- Cache lives on disk as one JSON file. This class is NOT part of
  analyze() -- analyze() must be a pure function (03_contract.py's
  ModelClient is injected as a parameter, never constructed inside a
  module). The cache belongs to the client construction, which happens
  once, outside analyze(), so the purity constraint on analyze() itself
  is untouched.
- Never silently swallows a cache-write failure: if the cache can't be
  persisted, that's worth knowing about immediately, not discovering when
  the "two identical runs" check fails.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional, Protocol


class ModelClient(Protocol):
    def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
        ...


class CachedModelClient:
    """Drop-in ModelClient. Wrap your real client with this once, at the
    top level, and pass THIS into analyze(), never the raw client.
    """

    def __init__(self, inner: ModelClient, cache_path: str = "cache/model_cache.json"):
        self.inner = inner
        self.cache_path = cache_path
        self._cache: dict[str, str] = {}
        # Hit/miss accounting, so "the cache is working" is a number rather than
        # an assumption. chars_saved is a proxy for prompt tokens avoided.
        self.hits = 0
        self.misses = 0
        self.chars_saved = 0
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.cache_path):
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                self._cache = json.load(f)
        except (OSError, ValueError) as exc:
            # A truncated or corrupt cache used to raise here and take the whole
            # pipeline down -- the cache is an optimisation, so failing to read
            # it must never be fatal. But it is not free either: an empty cache
            # means every prompt gets re-called and re-paid for, so say so
            # loudly rather than starting a silent spend.
            print(f"WARNING: could not read {self.cache_path} ({exc}). "
                  f"Continuing with an empty cache -- every prompt will be "
                  f"re-called and re-billed.")
            self._cache = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)

        # Merge with whatever is on disk NOW rather than writing our own
        # snapshot. Two runs whose lifetimes overlap each hold an independent
        # dict loaded at construction, so a plain overwrite makes the last
        # writer discard everything the other one paid for -- silently, and
        # visible only as a surprise bill on the next run. Entries are
        # content-addressed on the prompt hash, so a merge can never conflict:
        # the same key always maps to the same response.
        #
        # `--fresh` still clears the cache; it deletes the file outright rather
        # than writing an empty one, so intentional invalidation is unaffected.
        merged: dict[str, str] = {}
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    merged = json.load(f)
            except (OSError, ValueError):
                # A corrupt or half-written cache must not take the run down;
                # the worst case is re-calling prompts we already had.
                merged = {}
        merged.update(self._cache)
        self._cache = merged

        # Write to a temp file then replace, so a crash mid-write never
        # corrupts the existing cache.
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, indent=2, sort_keys=True)
        os.replace(tmp, self.cache_path)

    @staticmethod
    def _key(prompt: str, max_tokens: int) -> str:
        payload = f"{max_tokens}\x1f{prompt}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
        key = self._key(prompt, max_tokens)
        if key in self._cache:
            self.hits += 1
            self.chars_saved += len(prompt)
            return self._cache[key]

        self.misses += 1
        response = self.inner.complete(prompt, max_tokens=max_tokens)
        self._cache[key] = response
        self._save()
        return response

    def stats(self) -> dict:
        """For your own visibility while building -- how much is cached,
        so you can see cost protection actually working as you iterate.
        """
        return {
            "cached_prompts": len(self._cache),
            "cache_path": self.cache_path,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": f"{self.hits / (self.hits + self.misses):.0%}" if (self.hits + self.misses) else "n/a",
            "prompt_chars_avoided": self.chars_saved,
        }