import threading
from counters import Counter

c = Counter()

# The fix must be structural, not probabilistic: a lock-like object created
# on the instance (acquire/release) is what makes bump() safe under
# concurrency. A read-modify-write without it can pass a single race test
# by luck on fast interpreters -- the gate must not depend on luck.
lock_like = [v for v in vars(c).values()
             if hasattr(v, "acquire") and hasattr(v, "release")]
assert lock_like, "Counter must create a lock in __init__ and use it in bump"


def worker():
    for _ in range(2000):
        c.bump()


threads = [threading.Thread(target=worker) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

assert c.value == 16000, f"counter lost updates: {c.value}"
assert c.bump() == 16001
print("race-guard: ok")
