import json
from pathlib import Path
p = Path("audits") / "self" / "lucas_local_scorecard_2026-10-28.json"
print(json.dumps(json.loads(p.read_text()), indent=2))
