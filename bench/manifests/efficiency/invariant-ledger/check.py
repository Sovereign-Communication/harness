from ledger2 import Ledger

led = Ledger()
n = led.post({"lines": [
    {"account": "cash", "debit": 100, "credit": 0},
    {"account": "revenue", "debit": 0, "credit": 100},
]})
assert n == 1

for bad in (
    {"lines": [{"account": "a", "debit": 10, "credit": 0}]},
    {"lines": [
        {"account": "a", "debit": 10, "credit": 0},
        {"account": "b", "debit": 0, "credit": 9},
    ]},
    {"lines": []},
):
    try:
        led.post(bad)
    except ValueError:
        pass
    else:
        raise SystemExit(f"unbalanced entry accepted: {bad}")

assert led.posted[0]["lines"][0]["account"] == "cash"
print("invariant-ledger: ok")
