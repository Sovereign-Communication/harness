from clamp import clamp

assert clamp(5, 0, 3) == 3
assert clamp(-2, 0, 3) == 0
assert clamp(2, 0, 3) == 2
assert clamp(0, 1, 2) == 1
print("clamp: ok")