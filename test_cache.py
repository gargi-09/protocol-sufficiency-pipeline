from model_cache import CachedModelClient

class FakeClient:
    def __init__(self):
        self.real_calls = 0
    def complete(self, prompt, *, max_tokens=1024):
        self.real_calls += 1
        return f'response to: {prompt[:20]}'

inner = FakeClient()
cached = CachedModelClient(inner, cache_path='cache/test_cache.json')

r1 = cached.complete('does this paper mention qPCR?', max_tokens=50)
r2 = cached.complete('does this paper mention animals?', max_tokens=50)

r1_again = cached.complete('does this paper mention qPCR?', max_tokens=50)
r2_again = cached.complete('does this paper mention animals?', max_tokens=50)

assert r1 == r1_again
assert r2 == r2_again
print('Real API calls made:', inner.real_calls, '(expected 2)')
print('Deterministic:', r1 == r1_again and r2 == r2_again)
print(cached.stats())