"""EV-0 -- benchmark capability evidence, ingested and normalized.

Why this module exists
----------------------
``/api/v1/benchmarks`` publishes capability evidence that did not exist here at
all (DF-EV-1): Artificial Analysis ``coding_index``/``agentic_index``/
``intelligence_index``, Design Arena, and OpenRouter's own tau-bench / GPQA /
web-search evals with ``accuracy`` and ``avg_cost_per_task``.

Shape follows the source, so one ingest normalizes two very different feeds
onto one row shape and no consumer has to branch on which publisher produced a
row.  The untouched payload is retained in ``raw`` so a field this schema does
not model yet is never lost.

This is evidence in, evidence out.  Nothing here ranks, scores, or decides a
route: the cost-per-capability index that consumes these rows is ``EV-1``, and
until it lands this module legitimately has no in-repo consumer -- the ingest
IS the deliverable, not a stub for one.

Part 2 of 4 in EV-0's evidence layer: ``endpoint_pricing``,
``benchmark_ingest`` (this module), ``discount_probe``, ``discount_gate``.
Canon lives in ``docs/jev-roadmap.md`` (``EV-*``).
"""
from .config import BENCHMARK_SOURCES, OPENROUTER_BENCHMARKS_URL
from .errors import HarnessError
from .events import emit
from .validation import optional_float


def fetch_benchmarks(transport, api_key, *, source=None, task_type=None,
                     timeout=30):
    """One GET for the unified benchmark feed.

    Shape follows the source: Artificial Analysis rows carry index fields
    (0-100), OpenRouter rows carry ``accuracy``/``avg_cost_per_task`` for a
    named ``benchmark_type``. Both are normalized onto one row shape so a
    caller never branches on source; the untouched payload is kept in
    ``raw`` so a future field is never lost to this module's schema.
    """
    if source is not None and source not in BENCHMARK_SOURCES:
        raise HarnessError(f"unknown benchmark source {source!r}; "
                           f"expected one of {', '.join(BENCHMARK_SOURCES)}")
    url = OPENROUTER_BENCHMARKS_URL
    params = []
    if source:
        params.append(f"source={source}")
    if task_type:
        params.append(f"task_type={task_type}")
    if params:
        url = f"{url}?{'&'.join(params)}"
    payload = transport.get(url, api_key, timeout=timeout)
    if not isinstance(payload, dict):
        raise HarnessError(f"benchmark feed returned {type(payload).__name__}, "
                           f"not an object")
    if payload.get("error"):
        raise HarnessError("benchmark feed returned an error: "
                           f"{payload['error'].get('message', payload['error'])}")
    raw_rows = payload.get("data")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise HarnessError("benchmark feed returned no data rows")

    rows = [_normalize_benchmark(r) for r in raw_rows if isinstance(r, dict)]
    # A row with no model identity cannot be joined to anything downstream,
    # so it is dropped rather than carried as a None slug.
    rows = [r for r in rows if r.get("model_permaslug")]
    if not rows:
        raise HarnessError("benchmark feed returned only malformed rows")
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    emit("economics_benchmarks", rows=len(rows),
         as_of=meta.get("as_of"), sources=sorted({r["source"] for r in rows}))
    return {"rows": rows, "meta": meta, "source": source, "task_type": task_type}


def _normalize_benchmark(raw):
    """One benchmark row onto a shape that does not depend on its source."""
    return {
        "source": raw.get("source"),
        "model_permaslug": raw.get("model_permaslug"),
        "display_name": raw.get("display_name"),
        # Artificial Analysis indices (0-100).
        "coding_index": optional_float(raw.get("coding_index")),
        "agentic_index": optional_float(raw.get("agentic_index")),
        "intelligence_index": optional_float(raw.get("intelligence_index")),
        # OpenRouter's own evaluations.
        "benchmark_type": raw.get("benchmark_type"),
        "accuracy": optional_float(raw.get("accuracy")),
        "accuracy_stddev": optional_float(raw.get("accuracy_stddev")),
        "avg_cost_per_task": optional_float(raw.get("avg_cost_per_task")),
        "total_tasks": raw.get("total_tasks"),
        "last_run_timestamp": raw.get("last_run_timestamp"),
        "raw": raw,
    }


def benchmarks_by_model(report):
    """Group a benchmark report by permaslug for O(1) lookup per model."""
    grouped = {}
    for row in report.get("rows", []):
        slug = row.get("model_permaslug")
        if slug:
            grouped.setdefault(slug, []).append(row)
    return grouped
