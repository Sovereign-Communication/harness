import order


def slugify(name):
    return name.strip().lower().replace(" ", "-")


order.slugify = slugify

assert order.save_slug({"name": "Test Item"}) == "test-item.json"
assert order.encode({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
lines = open("order.py", encoding="utf-8").read().splitlines()
imports = [l for l in lines if l.startswith(("import ", "from "))]
assert imports[0].startswith("import json"), \
    f"stdlib import must come first, got: {imports}"
print("import-order: ok")
