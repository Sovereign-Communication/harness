"""
Cross-check the prompt's dependency chains against the roadmap register table,
and write a reconciliation note. Safe: reads docs/jev-roadmap.md only.
"""
import re
from pathlib import Path

ROADMAP = Path("docs") / "jev-roadmap.md"
OUT = Path("docs") / "roadmap_chain_reconcile_2026-10-28.md"
OUT.parent.mkdir(parents=True, exist_ok=True)

text = ROADMAP.read_text(encoding="utf-8", errors="replace")

# ---- Parse the register table more carefully ----
# Format (from the file):
# | [#347](url) | Config | none | acceptance... |
# Acceptance cell runs to end of line (no embedded pipes on the same row).
lines = text.splitlines()
register = {}
in_register = False
for ln in lines:
    if "### Dependency and acceptance register" in ln:
        in_register = True
        continue
    if in_register and ln.startswith("| "):
        # stop when we hit a non-table line after the register (e.g. a heading)
        m = re.match(r'\|\s*\[#(\d+)\]\([^)]+\)\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*(.+?)\s*\|$',
                     ln)
        if m:
            num, owner, prereqs, acceptance = m.group(1), m.group(2).strip(), m.group(3).strip(), m.group(4).strip()
            register[num] = (owner, prereqs, acceptance)
        else:
            # Maybe we've left the table
            if ln.strip() and not ln.startswith("|"):
                in_register = False

print("Parsed register rows:", len(register))

def prereq_list(s):
    if not s or s.lower() == "none":
        return []
    return [x.strip().lstrip("#") for x in re.split(r"[,\s]+", s) if x.strip().lstrip("#").isdigit()]

graph = {n: prereq_list(p) for n, (_, p, _) in register.items()}

# ---- Detect cycles ----
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
            idx = stack.index(v)
            cycles.append(stack[idx:] + [v])
        elif color[v] == WHITE:
            dfs(v, stack)
    stack.pop()
    color[u] = BLACK
for n in list(graph):
    if color.get(n, BLACK) == WHITE:
        dfs(n, [])
print("Cycles found:", len(cycles))
for c in cycles:
    print("  cycle:", " -> ".join("#"+x for x in c))

# ---- Topological order (Kahn) ----
indeg = {n: 0 for n in graph}
for u, deps in graph.items():
    for v in deps:
        if v in indeg:
            indeg[u] += 1
queue = [n for n, d in indeg.items() if d == 0]
order = []
while queue:
    n = queue.pop(0)
    order.append(n)
    for m, deps in graph.items():
        if n in deps and m in indeg:
            indeg[m] -= 1
            if indeg[m] == 0 and m not in order and m not in queue:
                queue.append(m)
print("Topo order:", len(order), "of", len(graph))
print("Not orderable (no zero-indeg start):", sorted(set(graph) - set(order)))

# ---- Prompt chains vs roadmap ----
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
n_edges = sum(len(c)-1 for c in prompt_chains)
verified = 0
missing = []
for c in prompt_chains:
    for a, b in zip(c, c[1:]):
        a_s, b_s = str(a), str(b)
        if a_s in graph and b_s in graph and b_s in graph[a_s]:
            verified += 1
        else:
            missing.append((a, b, graph.get(a_s, []), b in graph))
print(f"Prompt-chain edges: {n_edges}; verified in roadmap: {verified}; not-verified: {len(missing)}")

out = []
out.append("# Roadmap chain reconciliation — 2026-10-28\n\n")
out.append("Source: `docs/jev-roadmap.md` dependency/acceptance register table.\n\n")
out.append(f"- register rows parsed: {len(register)}\n")
out.append(f"- cycles detected: {len(cycles)}\n")
if cycles:
    for c in cycles:
        out.append(f"  - cycle: {' -> '.join('#'+x for x in c)}\n")
out.append(f"- topo-orderable issues: {len(order)} of {len(graph)}\n")
if set(graph) - set(order):
    out.append(f"- not orderable (no zero-indeg start): {sorted(int(x) for x in (set(graph)-set(order)))}\n")
out.append(f"- prompt-chain edges checked: {n_edges}\n")
out.append(f"- edges verified in roadmap register: {verified}\n")
out.append(f"- edges NOT matching roadmap register: {len(missing)}\n\n")
if missing:
    out.append("### Edges not matching roadmap register\n\n")
    for a, b, prereqs, b_present in missing[:60]:
        out.append(f"- {a} -> {b}: roadmap says #{a} prereqs={prereqs}; #{b} present in register={b_present}\n")
    if len(missing) > 60:
        out.append(f"  ... ({len(missing)-60} more)\n")
out.append("\n## Prompt-scoped issues present in roadmap register\n\n")
prompt_set = {
    270,271,272,273,275,277,280,281,283,284,285,286,287,289,290,291,292,293,294,
    295,299,300,301,302,303,304,306,308,309,310,311,313,314,318,319,325,332,334,
    335,336,337,340,342,343,344,345,346,365,377,378,379,380,381,382,
    407,409,411,412,413,414,415,416,417,418,419,
    420,421,422,423,424,425,426,427,428,429,430,431,
}
present = sorted(int(n) for n in register if n.isdigit() and int(n) in prompt_set)
out.append(f"- count present in register: {len(present)}\n")
out.append(f"- present: {', '.join('#'+str(n) for n in present)}\n\n")
missing_from_reg = sorted(prompt_set - set(int(n) for n in register if n.isdigit()))
out.append("## Prompt-scoped issues NOT in roadmap register table\n\n")
out.append(f"- count missing: {len(missing_from_reg)}\n")
out.append(f"- missing: {', '.join('#'+str(n) for n in missing_from_reg)}\n\n")
out.append("Note: missing-from-register does NOT mean missing from the repo — many of these are "
            "prompt-named NEW issues (#412-431) that postdate the roadmap register, or issues whose "
            "register row uses a different parse shape. The live `gh issue view` status batch "
            "(docs/status_batch_2026-10-28.md) is the authoritative per-issue state for this pass.\n")

OUT.write_text("\n".join(out), encoding="utf-8")
print("Wrote", OUT, "chars:", len(OUT.read_text()))
