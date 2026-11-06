r"""driver-core: verified extraction -> Jev decision -> deterministic action.

This module is the single owner of the version number. ``pyproject.toml``
reads it (``[tool.setuptools.dynamic]``) and ``GET /health`` reports it, so
the built wheel, the running service and this docstring cannot drift apart.

What each version changed, one line each; the mechanisms they introduced are
documented where those mechanisms live.

* ``3.5.0``: the standalone 3.5.0 reconciliation -- the ``extractors/``
  package plus guardrails, Jev audit context, locate and verdicts.  The
  harness-local patches remain deliberate; see ``docs/driver.md``'s vendoring
  table before replacing any file.
* ``3.4.0``: the audit log's record vocabulary has one owner (``audit.py``'s
  ``KIND_*`` constants); producers import the names. Nothing written changed.
* ``3.3.0``: a declared command tokenises with no escapes -- whitespace
  separates, quotes group, a backslash is never an escape
  (``wiring.parse_argv``) -- so an unquoted absolute Windows path works. One
  behaviour change: a POSIX ``\ `` escape must now be quoted.
* ``3.2.0``: a host can declare the token it will present (``DRIVER_TOKEN``);
  a per-process random token remains the default, and the token is never
  echoed (``config.Settings``). Also: ``serve(port=0)`` gets an ephemeral
  port, and the service announces the address it actually bound.
* ``3.1.0``: each fact has one owner -- the live tiers, the driver's sources,
  and the wire ``schema`` meaning (``docs/design.md``).
* ``3.0.0``: the perception tiers are reachable from the product (declared
  sources register real ones), and ``schema`` is required on ``POST /step``.
* ``2.0.0``: a consent is a capability for one exact ``(action, params)``
  pair; the wildcard grant is gone from this package's vocabulary.

The wire contract -- ``STOP_REASONS``, the keys of
:meth:`~driver_core.driver.StepResult.to_dict`, refusal-is-``HTTP 200
ok:false``, and the request body's field set -- remains the same additive
shape it has had since 2.x. Post-action verification adds the
``verification`` result and the ``verification_failed`` reason; both are
absent work for a driver with the feature off.
"""

__version__ = "3.5.0"
