import subprocess, json
from pathlib import Path

nums = [270,271,272,273,275,277,280,281,282,283,284,285,286,287,288,289,290,
        291,292,293,294,295,299,300,301,302,303,304,306,308,309,310,311,313,314,
        318,319,325,332,334,335,336,337,340,342,343,344,345,346,365,377,378,379,
        380,381,382,407,409,411,412,413,414,415,416,417,418,419,
        420,421,422,423,424,425,426,427,428,429,430,431]

out = Path("docs") / "status_batch_2026-10-28.md"
out.parent.mkdir(parents=True, exist_ok=True)
lines = []
lines.append("# Prompt-scoped issue live status — 2026-10-28\n\n")
lines.append("One `gh issue view` call per issue (title + state). "
             "Cached locally as issue_<n>.json for the ones already fetched this session.\n\n")

for n in nums:
    p = Path(f"issue_{n}.json")
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            title = d.get("title") or "???"
            state = d.get("state") or "???"
        except Exception as e:
            title, state = "PARSE-ERROR", str(e)
    else:
        try:
            raw = subprocess.check_output(
                ["gh", "issue", "view", str(n), "--repo",
                 "Sovereign-Communication/harness",
                 "--json", "title,state"],
                text=True, stderr=subprocess.PIPE)
            d = json.loads(raw)
            title = d.get("title") or "???"
            state = d.get("state") or "???"
        except Exception as e:
            title, state = "FETCH-ERROR", str(e)[:120]
    lines.append(f"- #{n}: {state} — {title}\n")

out.write_text("\n".join(lines), encoding="utf-8")
print("Wrote", out)
print("Lines:", len(lines))
