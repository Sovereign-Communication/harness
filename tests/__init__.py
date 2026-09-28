"""Test package — and the suite's ledger isolation switch.

The unit suite deliberately exercises ledger-writing paths (`jev_eval`,
`plan_verdict`, `brief_built`, cost events). Without an explicit
``HARNESS_LEDGER`` those writes land in whatever ledger the ambient
environment resolves to, which on an operator machine is the *real*
``~/.config/harness/ledger.jsonl`` — the evidence store the suite must never
touch. The live ledger had collected tens of thousands of zero-cost fallback
``jev_eval`` events that way, and because the chain is append-only they cannot
be removed without rewriting it.

So the suite pins one fresh temporary path for the whole process, before any
test constructs a ledger. This module is imported as soon as any test module
does ``from tests._fake import ...`` (the parent package is imported first),
which happens during discovery, i.e. before the first test runs.

An ambient ``HARNESS_LEDGER`` is deliberately *overridden* rather than
respected: a private ledger is the right default for a live lane run, and
exactly the wrong outcome for a test run pointed at it. A test that needs a
specific ledger sets ``HARNESS_LEDGER`` for its own duration (see
``tests/test_cost_cli.py``).
"""

import os
import tempfile

#: Directory holding the suite's ledger. Created once per process.
LEDGER_ISOLATION_DIR = tempfile.mkdtemp(prefix="harness-test-ledger-")

#: The path every test in this run resolves unless it says otherwise.
LEDGER_ISOLATION_PATH = os.path.join(LEDGER_ISOLATION_DIR, "ledger.jsonl")

os.environ["HARNESS_LEDGER"] = LEDGER_ISOLATION_PATH
