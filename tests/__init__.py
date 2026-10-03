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
import socket as _socket
import sys
import tempfile

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

#: Directory holding the suite's ledger. Created once per process.
LEDGER_ISOLATION_DIR = tempfile.mkdtemp(prefix="harness-test-ledger-")

#: The path every test in this run resolves unless it says otherwise.
LEDGER_ISOLATION_PATH = os.path.join(LEDGER_ISOLATION_DIR, "ledger.jsonl")

os.environ["HARNESS_LEDGER"] = LEDGER_ISOLATION_PATH


# ---------------------------------------------------------------------------
# Network guard: the suite is hermetic, so it may talk to loopback and nothing
# else. Every test either patches its network seam or runs a local server; a
# test that forgets (a new auto-fetch behind an old test's patch, say) would
# otherwise reach the real internet, pass on a machine with a network and
# leak a ResourceWarning on the one that has none. Names are resolved and
# connections are made through the two guarded functions below; a non-loopback
# attempt is recorded in ``NETWORK_VIOLATIONS`` and refused with an OSError,
# and ``tests/test_zz_network_guard.py`` (last in discovery order) fails the
# run if anything was recorded -- even when the code under test swallowed the
# error. Set ``HARNESS_TEST_ALLOW_NETWORK=1`` to disable it (live probes).
# ---------------------------------------------------------------------------
NETWORK_VIOLATIONS = []
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def _is_loopback_host(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host is None:
        return True
    host = str(host).strip().lower().strip("[]")
    if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    return host.startswith("127.") or host in {"::ffff:127.0.0.1"}


def _refuse(kind, target):
    NETWORK_VIOLATIONS.append(f"{kind} {target!r}")
    raise OSError(f"tests are hermetic: refused real network {kind} {target!r}")


if os.environ.get("HARNESS_TEST_ALLOW_NETWORK") != "1":
    _real_connect = _socket.socket.connect
    _real_connect_ex = _socket.socket.connect_ex
    _real_getaddrinfo = _socket.getaddrinfo

    def _guarded_connect(self, address):
        if (isinstance(address, tuple) and address
                and not _is_loopback_host(address[0])):
            _refuse("connect", address[:2])
        return _real_connect(self, address)

    def _guarded_connect_ex(self, address):
        if (isinstance(address, tuple) and address
                and not _is_loopback_host(address[0])):
            _refuse("connect", address[:2])
        return _real_connect_ex(self, address)

    def _guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback_host(host):
            try:
                import ipaddress
                ipaddress.ip_address(str(host).strip("[]"))
            except ValueError:
                _refuse("resolve", host)
        return _real_getaddrinfo(host, *args, **kwargs)

    _socket.socket.connect = _guarded_connect
    _socket.socket.connect_ex = _guarded_connect_ex
    _socket.getaddrinfo = _guarded_getaddrinfo
