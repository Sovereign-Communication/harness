from leap import is_leap

assert is_leap(2000) is True   # divisible by 400
assert is_leap(1900) is False  # divisible by 100 but not 400
assert is_leap(2024) is True
assert is_leap(2023) is False
assert is_leap(2400) is True
print("leap: ok")