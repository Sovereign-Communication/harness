from balanced import is_balanced

assert is_balanced("") is True
assert is_balanced("()") is True
assert is_balanced("((()))") is True
assert is_balanced("()()()") is True
assert is_balanced("(()))") is False   # closing without opening
assert is_balanced(")(()") is False    # opens while closed
assert is_balanced("(()(") is False
assert is_balanced("(a(b)c)") is True  # ignore non-paren chars
print("balanced: ok")