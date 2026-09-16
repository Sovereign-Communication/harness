"""CLI presentation: the ONE owner of result rendering and exit-code surfacing.

Extracted verbatim from cli.py (the cli_parser/mcp_schemas precedent): cli.py
keeps handlers and dispatch; this module owns how a result reaches a human or
a script -- the report/exit-code rendering contract (_emit, _emit_by_status)
and the capabilities table (_print_capabilities_table). No policy lives here
beyond what the moved bodies carried; terminal_exit_code and saturation
advise are imported from their single def sites.
"""
import json
import os
import sys

from .errors import HarnessError
from .output import eprint
from .results import terminal_exit_code
from .saturation import advise


def _emit(result, out, force_json=False):
    """The ONE result emitter: --out gets the JSON file; a piped stdout gets
    machine JSON (the script contract, byte-compatible); a TTY gets the rich
    rendering on stderr PLUS the same machine JSON on stdout -- pretty mode
    adds, it never replaces, so scripts and humans read the same run."""
    from . import render as _render
    text = json.dumps(result, indent=2)
    if out:
        parent = os.path.dirname(os.path.abspath(out))
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            raise HarnessError(f"cannot write --out {out}: {e}") from e
        eprint(f"[OK] result written to {out}")
    else:
        _render.pretty_print(result, out, force_json)
        print(text)


def _emit_by_status(result, out, *, continued=False):
    """Apply results through the ONE exit-code policy (results.py); this
    adds the resume hint a deferred run needs and the saturation advise."""
    # Terminal honesty: a run that exhausted its rounds on 429s / reasoning-
    # only responses says so plainly, with the real options (one policy
    # owner, harness/saturation.py). The result's own rounds are the
    # evidence -- a failed run always carries its api_error rounds there.
    advise(engine_rounds=result.get("rounds"))
    _emit(result, out)
    code = terminal_exit_code(result["status"])
    if code == 3:
        eprint("[apply] task deferred; resume with: harness continue --state <out.json>"
               if not continued else
               "[apply] still deferred; resume again: harness continue --state <out.json>")
    if code:
        sys.exit(code)


def _print_capabilities_table(out):
    """Human table on stderr: stdout stays pure JSON for piping, and --quiet
    suppresses the table while the JSON report still flows."""
    rows = out["models"]
    if not rows:
        eprint("(no models in pools with capability profiles)")
        return
    hdr = f"{'model':<42} {'ctx':>9} {'rsn':>3} {'jd':>4} {'jr':>4} {'cap':>5} {'f-str':>5} {'rel':>5}"
    eprint(hdr)
    eprint("-" * len(hdr))
    for r in rows:
        probe = r.get("probe")
        probe_note = ""
        if probe is not None and probe.get("calls"):
            probe_note = (f"  probe: json={probe['json_ok_rate']} "
                          f"correct={probe['correct_rate']} err={probe['errors']}")
        jd = r["json_declared"]
        jr = r["json_reliable"]
        eprint(f"{r['model']:<42} {r['context']:>9,} {'Y' if r['reasoning'] else 'n':>3} "
               f"{jd:>4.2f} {jr:>4.2f} "
               f"{r['capability']:>5.2f} {r['fitness_structured']:>5.2f} "
               f"{r['reliability_structured']:>5.2f}{probe_note}")
