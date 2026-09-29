"""Suite-wide test isolation. Importing this package must happen before any test
module builds a ledger, so everything here runs at import time.

The problem this solves
-----------------------
``harness.config.load_settings()`` resolves ``ledger_path`` from the
``HARNESS_LEDGER`` environment variable and falls back to
``~/.config/harness/ledger.jsonl`` when that is unset. Roughly 141 hermetic
test modules call ``load_settings()`` (directly or through
``harness.session.ledger_for``) without overriding the path, and any of them
that appends a ``jev_eval`` / ``plan_verdict`` / ``brief_built`` event lands in
the operator's real, append-only, hash-chained ledger. That ledger is
evidence: it is what ``harness cost``, ``harness log`` and the audit read to
say what was actually spent and judged. On 2026-09-28 it held 67,526 events of
which 53,214 were ``jev_eval`` and 43,439 of those were zero-cost fallbacks --
i.e. the majority of the evidence was fake, written by a test run.

The hash chain is never rewritten here. The fix is prevention, not repair:
point the default at a temp file so the suite has nowhere real to write.

Why this file, and why it needs ``-t .``
---------------------------------------
``unittest discover -s tests`` sets the top-level directory to ``tests/``
itself, imports the test modules as top-level names, and never imports this
package -- so nothing here would run. Discovery must be invoked as
``python -m unittest discover -t . -s tests`` for the package to load first.
That form is also what the test modules already assume: they import their
shared helpers as ``from tests._fake import ...``, which only resolves when
``tests`` is a package rooted at the repository root.

Enforcement, not just redirection
---------------------------------
Redirection alone would be a convention. The guard here is a tripwire: the
real ledger's ``(size, mtime)`` is recorded at import, checked at interpreter
exit, and a mismatch prints the offending path and kills the process with a
non-zero status. A future test that hardcodes the real path therefore fails
the run loudly, whatever order it runs in -- a per-test guard could be passed
by a polluter that ran before it.

Escape hatch: export ``HARNESS_LEDGER`` yourself to point the suite at a
sandbox you own. An explicit value is honoured; only the real default is
overridden.
"""

import atexit
import os
import sys
import tempfile

_REAL_LEDGER_DIR = os.path.expanduser(os.path.join("~", ".config", "harness"))
_REAL_LEDGER = os.path.join(_REAL_LEDGER_DIR, "ledger.jsonl")

# Never let a value inherited from the operator's shell leak in as the
# suite's target: HARNESS_LEDGER is exactly how the real ledger gets chosen
# when nobody sets it, and a stale export would make the tripwire below
# compare the sandbox against itself.
_SANDBOX_DIR = tempfile.mkdtemp(prefix="harness-test-ledger-")
_SANDBOX_LEDGER = os.path.join(_SANDBOX_DIR, "ledger.jsonl")

# A ledger that already exists before the suite starts, under the real config
# dir, is the one thing that must not grow. Record it now, before any test
# module has been imported.
_before = None
if os.path.exists(_REAL_LEDGER):
    st = os.stat(_REAL_LEDGER)
    _before = (st.st_size, st.st_mtime_ns)

# An explicit HARNESS_LEDGER is respected so that a developer can aim the
# whole suite at a sandbox they control. Anything else -- including the real
# default that load_settings() would compute -- is redirected.
if not os.environ.get("HARNESS_LEDGER"):
    os.environ["HARNESS_LEDGER"] = _SANDBOX_LEDGER


def _fingerprint(path):
    """(size, mtime_ns) for `path`, or None when it does not exist."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


@atexit.register
def _assert_real_ledger_untouched():
    """Fail the run, loudly, if the suite wrote to the operator's ledger.

    Registered with atexit rather than as a TestCase so it is order
    independent: a polluter that runs first cannot pre-empt the check.
    """
    after = _fingerprint(_REAL_LEDGER)
    if after != _before:
        sys.stderr.write(
            "\n"
            "=" * 72 + "\n"
            "LEDGER ISOLATION BREACH: the test suite wrote to the real ledger.\n"
            f"  path : {_REAL_LEDGER}\n"
            f"  before: {_before}\n"
            f"  after : {after}\n"
            "Every ledger-writing code path reached here must be redirected to a\n"
            "temp file. Do not add a test that hardcodes this path; set\n"
            "settings.ledger_path to a tmp directory instead.\n"
            "The hash chain is deliberately NOT rewritten -- this is evidence\n"
            "corruption, and hiding it would destroy the property that makes the\n"
            "ledger worth keeping.\n"
            + "=" * 72 + "\n"
        )
        # A non-zero exit here is what turns the breach into a red suite.
        os._exit(1)


@atexit.register
def _report_sandbox():
    """Print where the suite's events actually went.

    Without this the isolation is invisible: a green run looks identical
    whether it wrote 0 events or 40,000, and the whole regression this fixes
    was invisible for exactly that reason.
    """
    try:
        written = _sandbox_event_count()
    except OSError:
        return
    sys.stderr.write(
        f"[ledger-isolation] suite ledger: {_SANDBOX_LEDGER} "
        f"({written} events); real ledger untouched: {_REAL_LEDGER}\n"
    )
    _cleanup()


def _sandbox_event_count():
    if not os.path.exists(_SANDBOX_LEDGER):
        return 0
    with open(_SANDBOX_LEDGER, encoding="utf-8", errors="replace") as fh:
        return sum(1 for line in fh if line.strip())


def _cleanup():
    """Remove the sandbox directory. Best effort; never raises at exit."""
    import shutil

    shutil.rmtree(_SANDBOX_DIR, ignore_errors=True)


# Exposed for tests/test_ledger_isolation.py so the guard can assert against
# the same values this module installed rather than recomputing them.
REAL_LEDGER = _REAL_LEDGER
SANDBOX_LEDGER = _SANDBOX_LEDGER
