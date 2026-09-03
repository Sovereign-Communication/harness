from median import median

assert median([1, 3, 2]) == 2
assert median([1, 2, 3, 4]) == 2.5
assert median([7]) == 7
assert median([1, 100]) == 50.5
assert median([10, 40, 20, 30, 50]) == 30
print("median: ok")