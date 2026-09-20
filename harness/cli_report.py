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


def _print_cost_table(report):
    """Render human-readable cost analytics summary on stderr."""
    eprint(f"[cost] Total spend: ${report.get('total_cost', 0.0):.6f} across {report.get('events_count', 0)} events "
           f"({report.get('billable_calls', 0)} billable, {report.get('free_calls', 0)} free)")
    if "savings" in report:
        s = report["savings"]
        eprint(f"[cost] Baseline frontier estimate: ${s.get('baseline_frontier_cost', 0.0):.6f}")
        eprint(f"[cost] Net savings: ${s.get('net_savings', 0.0):.6f} ({s.get('savings_percent', 0.0):.1f}%)")

    by_tier = report.get("by_tier")
    if by_tier:
        eprint("\nSpend by Tier:")
        hdr = f"{'Tier':<8} {'Calls':>8} {'Cost ($)':>12}"
        eprint(hdr)
        eprint("-" * len(hdr))
        for t in ("T0", "T1", "T2", "T3"):
            d = by_tier.get(t, {})
            eprint(f"{t:<8} {d.get('calls', 0):>8} {d.get('cost', 0.0):>12.6f}")

    by_model = report.get("by_model")
    if by_model:
        eprint("\nSpend by Model:")
        hdr = f"{'Model':<40} {'Tier':<6} {'Calls':>8} {'Cost ($)':>12}"
        eprint(hdr)
        eprint("-" * len(hdr))
        for m, d in sorted(by_model.items(), key=lambda item: item[1].get("cost", 0.0), reverse=True):
            eprint(f"{m:<40} {d.get('tier', 'T2'):<6} {d.get('calls', 0):>8} {d.get('cost', 0.0):>12.6f}")

