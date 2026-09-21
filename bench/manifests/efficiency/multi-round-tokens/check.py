from parse_csv import totals_by_category

rows = [
    {"category": "food", "amount": 12.50},
    {"category": "fuel", "amount": 30.00},
    {"category": "food", "amount": 7.25},
    {"category": "fuel", "amount": 10.101},
]
out = totals_by_category(rows)
assert out.get("food") == 19.75, out
assert abs(out.get("fuel", 0) - 40.10) < 0.001, out
assert totals_by_category([]) == {}
print("multi-round-tokens: ok")
