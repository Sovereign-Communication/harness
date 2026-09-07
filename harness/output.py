"""Human-facing stderr output: one owner for progress chatter and --quiet.

Every module prints progress through ``eprint`` so the ``--quiet`` flag (and
the MCP server, which must keep stdout clean) is enforced in exactly one
place. Warnings and fatals are always audible.
"""
import sys

# --quiet: suppress progress chatter but never warnings/fatals.
QUIET = False
_AUDIBLE_PREFIXES = ("[warn]", "[FATAL]", "[claims-lint]", "[BYOK]")


def eprint(*a, **kw):
    if QUIET and a and not str(a[0]).startswith(_AUDIBLE_PREFIXES):
        return
    print(*a, file=sys.stderr, **kw)
