from dedupe import dedupe

assert dedupe([1, 2, 2, 3, 1]) == [1, 2, 3]
assert dedupe(["a", "a", "b"]) == ["a", "b"]
assert dedupe([]) == []
# order of first occurrence must be preserved (a set breaks this)
assert dedupe([3, 1, 2, 1, 3]) == [3, 1, 2]
print("dedupe: ok")