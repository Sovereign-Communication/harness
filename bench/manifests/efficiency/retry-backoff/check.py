from retry2 import retry_call

# Succeeds immediately:
assert retry_call(lambda: 42) == 42

# Fails twice, then succeeds: retries with exponential backoff.
calls = {"n": 0}
delays = []


def flaky():
    calls["n"] += 1
    if calls["n"] < 3:
        raise RuntimeError("transient")
    return "ok"


out = retry_call(flaky, retries=5, base_delay=0.01, max_delay=0.2,
                 sleep=delays.append)
assert out == "ok"
assert calls["n"] == 3, calls
assert delays == [0.01, 0.02], delays

# Exhausts retries and re-raises:
def always_fails():
    raise ValueError("permanent")


try:
    retry_call(always_fails, retries=2, base_delay=0.01, sleep=lambda s: None)
except ValueError:
    pass
else:
    raise SystemExit("exhausted retries did not re-raise")
print("retry-backoff: ok")
