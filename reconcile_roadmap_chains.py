"""
Recompute the dependency chains from docs/jev-roadmap.md (the canon) so the
overnight BURNDOWN ordering is grounded in the file, not only the prompt list.
Writes a local reconciliation note + a machine-readable row set for the ledger.
"""
import re
from pathlib import Path

ROADMAP = Path("docs") / "jev-roadmap.md"
OUT = Path("docs") / "roadmap_chain_reconcile_2026-10-28.md"
OUT.parent.mkdir(parents=True, exist_ok=True)

text = ROADMAP.read_text(encoding="utf-8")

# ---- Pull the dependency/acceptance register table ----
# Looks like:
# | [#347](...) | Config | none | ... |
# | [#327](...) | Gate runner | none | ... |
table_rows = re.findall(
    r'\|\s*\[?#?(\d+)\]?\s*\]\(?[^)]*\)?\s*\|\s*([^\|]+)\|\s*([^\|]+)\|\s*([^\n]+)',
    text,
)

rows = {}
for num, owner, prereqs, acceptance in table_rows:
    num = num.strip()
    owner = owner.strip()
    prereqs = prereqs.strip()
    acceptance = acceptance.strip()
    rows[num] = (owner, prereqs, acceptance)

print("Parsed", len(rows), "register rows from roadmap table")

# ---- Build explicit dependency graph ----
def parse_prereqs(prereqs):
    # "none" or "#345,#346,#324" or "#345,#346,#324" with spaces
    if prereqs.lower() == "none":
        return []
    parts = re.split(r'[,\s]+', prereqs)
    return [p.lstrip("#") for p in parts if p.lstrip("#").isdigit()]

graph = {}
for num, (owner, prereqs, acceptance) in rows.items():
    graph[num] = parse_prereqs(prereqs)

# ---- Detect cycles (simple DFS) ----
WHITE, GRAY, BLACK = 0, 1, 2
color = {n: WHITE for n in graph}
cycles = []

def dfs(u, stack):
    color[u] = GRAY
    stack.append(u)
    for v in graph.get(u, []):
        if v not in color:
            continue
        if color[v] == GRAY:
            # found cycle
            idx = stack.index(v)
            cycles.append(stack[idx:] + [v])
        elif color[v] == WHITE:
            dfs(v, stack)
    stack.pop()
    color[u] = BLACK

for n in graph:
    if color.get(n, BLACK) == WHITE:
        dfs(n, [])

print("Cycles found:", len(cycles))
for c in cycles:
    print("  cycle:", " -> ".join(c))

# ---- Topological sort (Kahn) on the subgraph of named issues ----
in_degree = {n: 0 for n in graph}
for u, deps in graph.items():
    for v in deps:
        if v in in_degree:
            in_degree[v] = in_degree.get(v, 0)  # ensure present
for u, deps in graph.items():
    for v in deps:
        if v in in_degree:
            pass  # v's in-degree counted from u->v? We'll recompute cleanly below

# Clean in-degree: edge u -> v means u depends on v (v should come first)
in_degree = {n: 0 for n in graph}
for u, deps in graph.items():
    for v in deps:
        if v in in_degree:
            in_degree[u] = in_degree.get(u, 0) + 1

queue = [n for n, d in in_degree.items() if d == 0]
order = []
while queue:
    n = queue.pop(0)
    order.append(n)
    for m, deps in graph.items():
        if n in deps and m in in_degree:
            in_degree[m] -= 1
            if in_degree[m] == 0 and m not in order and m not in queue:
                queue.append(m)

print("Topo order length:", len(order), "of", len(graph))

# ---- Highlight prompt-scoped issues and their deps ----
prompt_set = {
    270,271,272,273,275,277,280,281,283,284,285,286,287,289,290,291,292,293,294,
    295,299,300,301,302,303,304,306,308,309,310,311,313,314,318,319,325,332,334,
    335,336,337,340,342,343,344,345,346,365,377,378,379,380,381,382,
    407,409,411,
    412,413,414,415,416,417,418,419,
    420,421,422,423,424,425,426,427,428,429,430,431,
}
prompt_in_roadmap = sorted(prompt_set & set(graph.keys()))
missing_from_roadmap = sorted(prompt_set - set(graph.keys()))
print("Prompt-scoped issues present in roadmap register:", len(prompt_in_roadmap))
print("Prompt-scoped issues NOT in roadmap register table:", missing_from_roadmap)

# ---- Write reconciliation note ----
out_lines = []
out_lines.append("# Roadmap chain reconciliation — 2026-10-28\n\n")
out_lines.append("Source: `docs/jev-roadmap.md` dependency/acceptance register table.\n")
out_lines.append("Parsed register rows: " + str(len(rows)) + "\n")
out_lines.append("Cycles detected: " + str(len(cycles)) + "\n")
if cycles:
    for c in cycles:
        out_lines.append("- cycle: " + " -> ".join(c) + "\n")
out_lines.append("Topo-orderable issues: " + str(len(order)) + " of " + str(len(graph)) + "\n")
out_lines.append("Prompt-scoped issues present in register: " + str(len(prompt_in_roadmap)) + "\n")
out_lines.append("Prompt-scoped issues missing from register: " + str(len(missing_from_roadmap)) + "\n")
if missing_from_roadmap:
    out_lines.append("- missing: " + ", ".join("#" + str(m) for m in missing_from_roadmap) + "\n")
out_lines.append("\n## Prompt-scoped issues and their roadmap prerequisites\n\n")
for n in prompt_in_roadmap:
    owner, prereqs, acceptance = rows[n]
    parsed = parse_prereqs(prereqs)
    parsed_str = ", ".join("#" + p for p in parsed) if parsed else "none"
    out_lines.append(f"### #{n} | owner: {owner}\n")
    out_lines.append(f"- roadmap prerequisites: {parsed_str}\n")
    out_lines.append(f"- acceptance (truncated): {acceptance[:240]}{'...' if len(acceptance)>240 else ''}\n\n")

out_lines.append("## Chain groupings (prompt prompt list vs roadmap)\n\n")
out_lines.append("The prompt lists explicit chains (e.g. #345 -> #346 -> #342 -> #343/#344 -> #308, #309).\n")
out_lines.append("We cross-check those edges against the roadmap register where both endpoints are present.\n\n")

prompt_chains = [
    [345, 346, 342, 343, 344, 308],
    [345, 346, 342, 309],
    [345, 340, 280, 318, 319],
    [345, 340, 280, 337],
    [340, 281],
    [340, 294],
    [340, 304],
    [340, 313],
    [345, 336, 337],
    [284, 335, 285],
    [301, 292, 270, 272, 271, 273],
    [325, 291],
    [325, 314],
    [377, 378, 382],
    [377, 379],
    [380, 381],
]

verified_edges = 0
missing_edges = []
for chain in prompt_chains:
    for a, b in zip(chain, chain[1:]):
        if a in graph and b in graph:
            if b in graph[a]:
                verified_edges += 1
            else:
                missing_edges.append((a, b))
        else:
            missing_edges.append((a, b))

out_lines.append(f"- prompt-chain edges checked: {sum(len(c)-1 for c in prompt_chains)}\n")
out_lines.append(f"- edges verified in roadmap register: {verified_edges}\n")
out_lines.append(f"- edges NOT matching roadmap register: {len(missing_edges)}\n")
if missing_edges:
    for a, b in missing_edges[:40]:
        ga = graph.get(a, [])
        gb = graph.get(b, [])
        out_lines.append(f"  - {a} -> {b}: roadmap says #{a} prereqs={ga}; #{b} present={b in graph}\n")
    if len(missing_edges) > 40:
        out_lines.append(f"  ... ({len(missing_edges)-40} more)\n")

OUT.write_text("\n".join(out_lines), encoding="utf-8")
print("Wrote", OUT)
print("Chars:", len(OUT.read_text()))
