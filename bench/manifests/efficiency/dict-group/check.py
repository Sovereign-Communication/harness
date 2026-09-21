from groups import group_by

assert group_by([]) == {}
assert group_by([("a", 1), ("b", 2), ("a", 3)]) == {"a": [1, 3], "b": [2]}
assert group_by([("x", "p"), ("x", "q"), ("y", "r"), ("x", "s")]) == \
    {"x": ["p", "q", "s"], "y": ["r"]}
print("dict-group: ok")
