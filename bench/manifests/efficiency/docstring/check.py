from docs import celsius_to_fahrenheit

assert celsius_to_fahrenheit.__doc__ == \
    "Convert a Celsius temperature to Fahrenheit."
assert celsius_to_fahrenheit(0) == 32
assert celsius_to_fahrenheit(100) == 212
assert abs(celsius_to_fahrenheit(37) - 98.6) < 1e-9
print("docstring: ok")
