from retry import fetch_retries

first = fetch_retries("a")
assert first == 1, first
second = fetch_retries("b")
assert second == 1, f"mutable default leaked state: {second}"
assert fetch_retries("c", seen=[]) == 1
print("arg-default: ok")
