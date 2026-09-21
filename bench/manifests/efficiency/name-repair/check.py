from calc import interest, compound

assert interest(100, 2) == 10
assert abs(compound(100, 2) - 110.25) < 1e-9
print("name-repair: ok")
