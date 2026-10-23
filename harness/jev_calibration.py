"""Jev decision-gate calibration analysis — tracks issue #168.

MEASUREMENT ONLY. This module changes no active threshold, never calls
``freeze_jev_settings``, and does not alter ``freeze_jev_settings``
behavior. It turns a batch of Jev judgment records into an observed
confidence distribution plus a documented, data-driven threshold
recommendation. The final threshold choice remains the operator's explicit
decision via ``freeze_jev_settings(min_confidence=...)`` in
``harness/config.py``.

Record shape (one dict per judgment)::

    {
        "verdict": str,        # e.g. "proceed" / "needs_improvement" / "escalate"
        "confidence": float,   # 0.0 .. 1.0, required
        "destructive": float,  # is_destructive support, 0.0 .. 1.0 (default 0.0)
        "disposition": str,    # "proceed" | "needs_improvement" | "escalate"
    }

``confidence`` is required and must be a number in [0, 1]; anything else
raises ``ValueError``. ``disposition`` defaults to ``"unknown"`` when
absent so unclassified records still count toward the distribution.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

# Matches the draft spec composition rule: is_destructive.supported >= 0.5
# always escalates, at any confidence.
DESTRUCTIVE_SUPPORT_FLOOR = 0.5


def _percentile(sorted_values: List[float], pct: float) -> float:
    """Linear-interpolation percentile over an already-sorted list."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("cannot take a percentile of an empty list")
    if n == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (n - 1)
    low = int(rank)
    frac = rank - low
    if low + 1 >= n:
        return sorted_values[-1]
    return sorted_values[low] + frac * (sorted_values[low + 1] - sorted_values[low])


def _distribution(values: Sequence[float]) -> Dict[str, Any]:
    vals = sorted(float(v) for v in values)
    n = len(vals)
    return {
        "n": n,
        "min": vals[0],
        "max": vals[-1],
        "mean": sum(vals) / n,
        "p50": _percentile(vals, 50),
        "p95": _percentile(vals, 95),
    }


def _validate_record(record: Mapping[str, Any], index: int) -> Dict[str, Any]:
    if not isinstance(record, Mapping):
        raise ValueError(f"record {index}: expected a mapping, got {type(record).__name__}")
    if "confidence" not in record:
        raise ValueError(f"record {index}: missing required field 'confidence'")
    conf = record["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        raise ValueError(f"record {index}: confidence must be a number, got {conf!r}")
    conf = float(conf)
    if not 0.0 <= conf <= 1.0:
        raise ValueError(f"record {index}: confidence must be in [0, 1], got {conf!r}")
    dest = record.get("destructive", 0.0)
    if isinstance(dest, bool) or not isinstance(dest, (int, float)):
        raise ValueError(f"record {index}: destructive must be a number, got {dest!r}")
    dest = float(dest)
    if not 0.0 <= dest <= 1.0:
        raise ValueError(f"record {index}: destructive must be in [0, 1], got {dest!r}")
    disp = record.get("disposition", "unknown")
    if not isinstance(disp, str) or not disp:
        raise ValueError(f"record {index}: disposition must be a non-empty string")
    return {
        "verdict": str(record.get("verdict", "")),
        "confidence": conf,
        "destructive": dest,
        "disposition": disp,
    }


def analyze_judgments(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Analyze a batch of judgment records into a calibration report.

    The report carries the observed confidence distribution, a
    per-disposition breakdown, the destructive-flag count, and a
    threshold recommendation (see :func:`recommend_threshold`). An empty
    batch yields an advisory-only report, never an exception.
    """
    cleaned = [_validate_record(r, i) for i, r in enumerate(records)]
    report: Dict[str, Any] = {"n": len(cleaned)}
    if not cleaned:
        report["confidence"] = None
        report["by_disposition"] = {}
        report["destructive_flags"] = 0
        report["recommendation"] = None
        report["notes"] = [
            "advisory only: no judgment records supplied; no threshold can be recommended.",
        ]
        return report

    confidences = [r["confidence"] for r in cleaned]
    report["confidence"] = _distribution(confidences)

    by_disp: Dict[str, List[Dict[str, Any]]] = {}
    for r in cleaned:
        by_disp.setdefault(r["disposition"], []).append(r)
    report["by_disposition"] = {
        disp: {
            "n": len(rows),
            "confidence": _distribution([r["confidence"] for r in rows]),
        }
        for disp, rows in sorted(by_disp.items())
    }
    report["destructive_flags"] = sum(
        1 for r in cleaned if r["destructive"] >= DESTRUCTIVE_SUPPORT_FLOOR
    )
    report["recommendation"] = recommend_threshold(cleaned)
    notes = [
        f"{report['n']} judgment records analyzed.",
        (
            f"{report['destructive_flags']} record(s) met the destructive "
            f"support floor ({DESTRUCTIVE_SUPPORT_FLOOR}) and would escalate "
            "at any confidence."
        ),
    ]
    if report["confidence"]["max"] < 0.95:
        notes.append(
            f"observed max confidence {report['confidence']['max']:.2f} "
            "is below the 0.95 fail-closed threshold: at 0.95 the gate "
            "escalates 100% of these records and adds cost without signal."
        )
    report["notes"] = notes
    return report


def recommend_threshold(records: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Recommend a data-supported threshold band from observed judgments.

    Method (documented, not silent):

    1. A fail-closed threshold T means ``confidence >= T`` may proceed and
       anything below escalates. T is only *discriminating* when it falls
       inside the observed confidence band — otherwise the gate is a
       constant function (0.95 on the observed data escalates everything).
    2. Setting T at the observed max is NOT sufficient: the max is a sample
       statistic, not a population guarantee, and a threshold needs labeled
       ground truth (were past escalations correct? would past proceeds have
       been safe?) per the spec's calibration criterion 3.
    3. The module therefore reports the *discriminating band* [p50, p95] of
       observed confidence: any threshold inside this band would have let a
       nontrivial fraction proceed while escalating the rest. Choosing
       inside (or outside) that band — and accepting the resulting
       false-proceed / false-escalate tradeoff — is the operator's freeze
       decision via ``freeze_jev_settings(min_confidence=...)``, informed by
       a labeled review of past escalations.

    Returns None for an empty batch. Never asserts a single "correct"
    threshold value.
    """
    cleaned = [_validate_record(r, i) for i, r in enumerate(records)]
    if not cleaned:
        return None
    dist = _distribution([r["confidence"] for r in cleaned])
    return {
        "method": (
            "discriminating band [p50, p95] of observed confidence; "
            "threshold-at-observed-max is not sufficient (see docstring). "
            "Final choice is the operator's via freeze_jev_settings."
        ),
        "observed_max": dist["max"],
        "discriminating_band": [dist["p50"], dist["p95"]],
        "warning": (
            "Do not freeze a threshold on this band alone: pair it with a "
            "labeled review of past escalations (spec criterion 3) before "
            "calling freeze_jev_settings(min_confidence=...)."
        ),
    }


def format_report(report: Mapping[str, Any]) -> str:
    """Render a calibration report as human-readable text."""
    lines = [f"jev calibration report — n={report['n']}"]
    conf = report.get("confidence")
    if conf is None:
        lines.append("no records; advisory only.")
        lines.extend(f"note: {n}" for n in report.get("notes", []))
        return "\n".join(lines)
    lines.append(
        "confidence: min={min:.2f} max={max:.2f} mean={mean:.2f} "
        "p50={p50:.2f} p95={p95:.2f}".format(**conf)
    )
    for disp, blk in report.get("by_disposition", {}).items():
        c = blk["confidence"]
        lines.append(
            f"  {disp}: n={blk['n']} "
            "min={min:.2f} max={max:.2f} mean={mean:.2f}".format(**c)
        )
    lines.append(f"destructive flags (>= {DESTRUCTIVE_SUPPORT_FLOOR}): "
                 f"{report['destructive_flags']}")
    rec = report.get("recommendation")
    if rec:
        lo, hi = rec["discriminating_band"]
        lines.append(f"discriminating band: [{lo:.2f}, {hi:.2f}]")
        lines.append(f"warning: {rec['warning']}")
    lines.extend(f"note: {n}" for n in report.get("notes", []))
    return "\n".join(lines)
