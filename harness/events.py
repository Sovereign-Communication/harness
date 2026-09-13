"""Typed progress event stream: the ONE owner of live run telemetry.

Every lane used to speak only through human-readable stderr
(``output.eprint``). That is fine for a human at a terminal, but a UI (the
web server, an MCP host, a wrapper script) cannot parse prose it does not
own. This module gives every lane a second, structured voice: typed JSON
events fanned out to registered sinks.

    {"ts": 1789316400.1, "seq": 7, "type": "panel_call",
     "task_id": "a1b2c3d4", "model": "google/gemma-4-31b-it:free"}

Design rules:

- **Advisory only.** Events are telemetry, never control flow: no lane may
  branch on the bus, and the bus must never change a run's outcome. A broken
  sink is dropped (with one audible stderr warning), never raised into the
  caller's lane.
- **Zero-cost when nobody listens.** With no sinks registered (the default
  CLI behavior), ``emit`` returns before touching the clock -- progress
  events cost nothing for plain CLI runs.
- **Sinks are callables taking one dict.** ``add_jsonl_sink`` wires the
  common case: append-only JSONL on disk (the ``--events FILE`` flag), the
  shape a file-tailing UI or the future web server's SSE bridge consumes.
- **Thread-safe.** Panel seats run concurrently; seq assignment and sink
  iteration happen under one lock. Sinks themselves are invoked under that
  lock -- keep sinks fast; the JSONL sink buffers and flushes per line.

Event vocabulary (one place, so producers stay consistent):

  preflight        worst-case cost reserved before any call
  attempt_start    one model attempt begins (apply lane)
  panel_call       one panel seat begins
  panel_vote       one panel seat produced a usable vote
  judge_call       judge synthesis begins
  judge_result     judge synthesis finished (status mirrors the envelope)
  rotation         a model is being replaced: reason is machine-readable
  readiness        forced self-check verdict (confident|defer)
  consent_request  a consent probe begins
  consent_result   the parsed sovereign decision (accept/decline/defer/redirect)
  gate_start       verification gate begins
  gate_end         verification gate finished (passed bool, rc)
  escalation_rung  an escalation ladder rung begins
  spend_check      key verified / budget state (never the key label)
  bench_task       one bench task starts/finishes
  terminal         the run reached a terminal status

``task_id`` is optional (some lanes, e.g. the reasoning retry inside chat(),
have no task context); consumers must tolerate its absence.
"""
import json
import sys
import threading
import time

# Guard against pathological sink counts; a UI registers one or two sinks.
MAX_SINKS = 8

_lock = threading.Lock()
_sinks = []
_seq = 0

# Module-global for a one-time broken-sink warning; assigned only at module
# level or inside emit() via the global statement (ruff F823 guard).
_warned_sink = None


def emit(event_type, **fields):
    """Emit one event to every registered sink. Never raises.

    With no sinks (the default CLI run) this is a single ``if`` -- the
    free-tier guarantee that telemetry changes nothing about cost or outcome
    includes not even touching the clock.
    """
    if not _sinks:
        return
    global _seq, _warned_sink
    with _lock:
        _seq += 1
        event = {"ts": time.time(), "seq": _seq, "type": event_type}
        event.update(fields)
        dead = []
        for sink in _sinks:
            try:
                sink(event)
            except Exception as exc:  # a broken sink must not break a run
                if _warned_sink is not sink:
                    print(f"[events] sink dropped ({exc})", file=sys.stderr)
                    _warned_sink = sink
                dead.append(sink)
        for sink in dead:
            _sinks.remove(sink)


def add_sink(sink):
    """Register a sink (callable taking one event dict). Returns the sink on
    success, or None when the registration was refused (full, duplicate)."""
    with _lock:
        if sink in _sinks or len(_sinks) >= MAX_SINKS:
            return None
        _sinks.append(sink)
    return sink


def remove_sink(sink):
    with _lock:
        if sink in _sinks:
            _sinks.remove(sink)
            return True
    return False


def sink_count():
    with _lock:
        return len(_sinks)


class JsonlSink:
    """Append one JSON object per line to a file. Opened lazily on the first
    event so ``--events`` never creates a file for a run that emits nothing;
    flushed per line so a tailing UI sees progress live."""

    def __init__(self, path):
        self.path = path
        self._fh = None

    def __call__(self, event):
        if self._fh is None:
            self._fh = open(self.path, "a", encoding="utf-8")
        self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def add_jsonl_sink(path):
    """Wire a JSONL file sink (the ``--events FILE`` flag). Returns the sink,
    or None when a sink for that exact path is already registered."""
    with _lock:
        if any(isinstance(s, JsonlSink) and s.path == path for s in _sinks):
            return None
    return add_sink(JsonlSink(path))
