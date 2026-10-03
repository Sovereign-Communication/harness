r"""driver-core: verified extraction -> Jev decision -> deterministic action.

This module is the single owner of the version number. ``pyproject.toml`` reads
it (``[tool.setuptools.dynamic]``) and ``GET /health`` reports it, so the built
wheel, the running service and this docstring cannot drift apart.

Version note: ``3.4.0`` gives the audit log's record vocabulary one owner.
``audit.py`` declared seven ``KIND_*`` constants while four modules wrote
their record kinds as bare strings, so a renamed constant would have left
the log writing a kind it no longer declared -- in the one artifact where a
record meaning exactly one thing is the entire point. The producers now
import the names, ``KIND_CONSENT`` is gone because nothing produced it (a
consent is recorded inside the ``action`` record it authorises), and
``states.FREE_CLASSES`` is gone because it duplicated
``observation.STRUCTURED_CLASSES`` with no readers.

Nothing written changes. ``tests/test_audit_compat.py`` re-hashes a chain
recorded before this change and compares a full run byte for byte, so the
claim is checked rather than asserted; a record renamed, a field added or
a record reordered each fail it. ``states.py`` loses a dead constant.

Version note: ``3.3.0`` makes an unquoted absolute Windows path work. The
declared-command tokeniser was POSIX ``shlex``, which reads ``\`` as an escape
outside quotes, so ``C:\Python314\python.exe server.py`` resolved to
``C:Python314python.exe`` -- a path nobody typed, from a source that still
reported itself configured, failing at run time with ``not found``. That left
the ``cli`` and ``mcp`` tiers unusable with an absolute interpreter path on
Windows.

The rule is now total: whitespace separates, quotes group, and **a backslash is
never an escape character. There are no escapes** -- including ``\"``, because
keeping that one exception reproduces the same bug for a path ending in a
separator (``"C:\Users\me\"``). One behaviour change worth stating plainly for
anyone who was relying on it: a POSIX operator using ``\ `` to escape a space
must now quote it. An unquoted argument containing a space is split, and the
resulting ``not found`` names the argv it actually attempted, so the split is
visible in the same line as the failure.

Version note: ``3.2.0`` lets a host supply the token it will present, which is
the missing half of the REST surface. The token check has been there since the
endpoint was, but a host could not hold a token for it: the only way to learn
one was ``serve --print-token``, which bound the port, printed, and exited --
handing back a credential no surviving process could present, so the service
had to be started twice. ``DRIVER_TOKEN`` is now a declared setting; a random
token per process remains the default when the host supplies none, which is the
right default for a caller driving the service in-process and useless for one
integrating over HTTP. A token that is declared blank or under
:data:`~driver_core.config.MIN_TOKEN_LENGTH` characters is refused by name
rather than accepted, because a service bound with a credential too weak to
matter looks exactly like a service that is working.

The token is never echoed: not in ``Settings.redacted()``, not in a ``repr``,
and not in ``GET /health``, which is reachable by anything that can open a
loopback socket. It belongs in a header the caller already holds. What
``/health`` gains instead is ``version``, so a host can tell what it is talking
to before posting a request that means something different in another release.

Two defects in the same lines are fixed. ``serve(port=0)`` now asks the
operating system for an ephemeral port; ``port or settings.port`` treated 0 as
unset and silently handed back 8791 instead. And ``serve`` announces the address
it actually bound rather than the arguments it was given, which is how an
operator was told the service was on ``http://None:8791`` -- printed by the one
branch that was not listening, while the branch that was listening said nothing.

``STOP_REASONS``, the eleven keys of
:meth:`~driver_core.driver.StepResult.to_dict`, and the field set of the request
body are unchanged, and so is the token check itself.

Version note: ``3.1.0`` gives each fact one owner. Three of them had two: the
settings and the driver both said which perception tiers were live (so
``/health`` carried the list twice -- the ``settings.sources`` duplicate is
gone and the top-level ``sources`` is the answer), the driver held its
sources and its screen source as two fields reconciled by a method every
caller had to remember, and "what does this wire ``schema`` mean" was a bare
lookup in the CLI and a lookup-or-refuse in the service. All three now have a
single home, described in ``docs/design.md``. ``POST /step`` is unchanged:
same fields, same ``STOP_REASONS``, same ``StepResult.to_dict()``.

``3.0.0`` made the perception tiers reachable from the product,
and closes the one request that could reach the strongest tier by accident.

* **Declared sources are live.** ``DRIVER_CLI_COMMAND``,
  ``DRIVER_MCP_COMMAND`` with ``DRIVER_MCP_TOOL``, ``DRIVER_DOM_URL`` and
  ``DRIVER_SCREEN`` register real sources on a default ``Driver`` and
  ``Service``. Each tier is off unless its own setting is set, commands are
  tokenised rather than shelled, and ``/health`` reports what is live. Before
  this the tiers existed only for a caller writing Python by hand: a default
  driver reported ``Tried: none`` and every ``POST /step`` ended in
  ``no_capture``.
* **``schema`` is required on ``POST /step``.** Absent, blank or unrecognised
  is a 400 naming the valid names. It used to default to the screen schema
  while leaving the target class *undeclared*, and an undeclared class permits
  any source to answer with pixels last -- so the request a caller made
  without thinking was the one that could reach both the strongest structured
  tier and the vision tier.

Nothing was added to or removed from a response: ``STOP_REASONS``,
refusal-is-``HTTP 200 ok:false``, the eleven keys of
:meth:`~driver_core.driver.StepResult.to_dict` and the field set of the
request body are all identical to 2.x, so a host that already declared
``schema`` is unaffected.

``2.0.0`` was the consent-law change: a consent became a capability for one
exact ``(action, params)`` pair with no wildcard at all, and the ``*`` that
previously stood for "every mutating action" was removed from this package's
vocabulary of grants. The wire contract did not change then either.
"""

__version__ = "3.4.0"
