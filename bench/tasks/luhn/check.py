from luhn import luhn_valid

# Known-valid Luhn numbers
assert luhn_valid(79927398713) is True
assert luhn_valid(4532015112830366) is True
# Known-invalid
assert luhn_valid(79927398712) is False
assert luhn_valid(4532015112830365) is False
print("luhn: ok")