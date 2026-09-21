import slowmath

assert slowmath.fib(28) == 317811
# The cache must actually be applied: without lru_cache this assert fails
# fast (the naive result is still correct), with it the repeat call is free.
assert slowmath.fib.cache_info().hits >= 1, "lru_cache was not applied"
print("lru-cache: ok")
