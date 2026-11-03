import re, json
from pathlib import Path

p = Path("harness") / "mcp_schemas.py"
text = p.read_text(encoding="utf-8")

names = re.findall(r'"name":\s*"([^"]+)"', text)
# TOOL_SCHEMAS is the first contiguous block; but the file has no other "name" patterns
# so names already == the schema list
counts = {}
for n in names:
    counts[n] = counts.get(n, 0) + 1

print("Schema name count:", len(names))
print("Unique names:", len(set(names)))
print("lucas_gate_v2 present:", "lucas_gate_v2" in set(names))
print("Duplicate names:", {k:v for k,v in counts.items() if v != 1})
print(json.dumps(names, indent=1))
