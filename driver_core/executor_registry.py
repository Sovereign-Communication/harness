"""The closed registry of action executors.

An executor is the code that actually does a thing. The registry is
deliberately closed: a name that is not registered has no handler, and
:meth:`ExecutorRegistry.get` returns ``None`` rather than a default. That
makes "this action has no implementation" an explicit, visible condition at
the point of use instead of a silent no-op that looks exactly like success.

The read-only executors are built in because observing is always safe and
should never require a deployment step to turn on. The mutating and
irreversible executors are built in too, but **only when the operator has
declared that this machine may be written to** (``DRIVER_ALLOW_WRITE``).
Registration is a capability, and a default-on capability to delete files is
not something a library should hand to every caller that constructs a
``Driver``.

Registration and consent are two independent gates and both are required.
Registration answers *whether the capability exists at all*; consent answers
*whether this particular call, with these particular resolved parameters, was
approved*. Neither implies the other: a registered executor with no matching
consent refuses, and a matching consent against an unregistered executor also
refuses. The second case is the more useful of the two, because it is the
one that means "this build cannot do that, whatever anyone asks".

The optional native Windows backend is also driver-local, requires both
``DRIVER_ALLOW_WRITE`` and ``DRIVER_NATIVE_INPUT``, and reports only the input
kinds it can actually perform. An embedding application may continue to
register its own process-wide backend through :mod:`driver_core.osal`.

OS contact lives in exactly one module for the same reason the host
project enforces it: a place that reaches for ``subprocess`` in one file and
``os`` in another grows a different idea of what a subprocess is on each
platform, and the divergence only shows up on the platform you did not test
on. :mod:`driver_core.osal` is that module, and the boundary test holds
everything else out of it. Every handler below reaches the outside world
*through* it and never around it.
"""
import os

from . import osal
from .errors import ExecutorError


def _refused(detail):
    """Turn a refusal into a failure the executor tier will record.

    Raising is the correct signal here. An executor that *returned* would be
    recorded as a successful action with an unhappy output dictionary, and a
    caller branching on ``execution.ok`` would proceed -- which is precisely
    the failure mode a refusal exists to prevent.
    """
    raise ExecutorError(detail)


def _sent(kind, result):
    """Common tail for the input-backed handlers."""
    ok, detail = result
    if not ok:
        _refused(detail)
    return {"kind": kind, "sent": True}


def _click(action, params, *, input_backend=osal.USE_REGISTERED_INPUT_BACKEND):
    """``click``: activate a declared, resolved element."""
    return _sent("click", osal.send_input(
        "click", target=params["target"], backend=input_backend))


def _type_text(action, params, *, input_backend=osal.USE_REGISTERED_INPUT_BACKEND):
    """``type_text``: type a literal string into the focused element."""
    return _sent("type", osal.send_input(
        "type", value=params["text"], backend=input_backend))


def _press_key(action, params, *, input_backend=osal.USE_REGISTERED_INPUT_BACKEND):
    """``press_key``: send a declared key combination."""
    return _sent("key", osal.send_input(
        "key", value=params["keys"], backend=input_backend))


def _focus(action, params, *, input_backend=osal.USE_REGISTERED_INPUT_BACKEND):
    """``focus``: bring a declared window or element to the foreground."""
    return _sent("focus", osal.send_input(
        "focus", target=params["target"], backend=input_backend))


def _scroll(action, params, *, input_backend=osal.USE_REGISTERED_INPUT_BACKEND):
    """``scroll``: scroll the focused scrollable region."""
    return _sent("scroll", osal.send_input(
        "scroll", value=params["direction"], backend=input_backend))


def _submit_irreversible(
        action, params, *, input_backend=osal.USE_REGISTERED_INPUT_BACKEND):
    """``submit_irreversible``: activate a control that cannot be undone.

    A distinct input kind rather than a ``click``, so a backend is told the
    truth about what it is being asked to do. Consent for this has already
    been checked against the exact target and then spent on use, so by the
    time this handler runs there is exactly one approved, unrepeatable
    activation of exactly one named control.
    """
    return _sent("submit", osal.send_input(
        "submit", target=params["target"], backend=input_backend))


def _write_file(action, params):
    """``write_file``: back up, then replace atomically.

    Driver-core performs this one itself. There is no backend to register
    and no platform capability to withhold: a backup-and-atomic-replace is
    the same operation everywhere, and it is implemented once, in
    :mod:`driver_core.osal`, where the platform differences (a read-only
    destination on Windows, a file another process holds open) are declared
    rather than discovered.
    """
    return osal.atomic_write(params["path"], params["content"])


def _delete_file(action, params):
    """``delete_file``: remove one file, having refused every near miss.

    No backup, because the action is declared ``IRREVERSIBLE`` and a backup
    would make it recoverable -- which would mean the classification, and
    the consent model built on it, were both wrong.
    """
    return osal.remove_file(params["path"])


#: The mutating and irreversible executors this package can perform itself.
#: Registered as a group so the gate is one switch with one meaning, rather
#: than six independently configurable half-capabilities that an operator has
#: to reason about in combination.
WRITE_EXECUTORS = {
    "click": _click,
    "type_text": _type_text,
    "press_key": _press_key,
    "focus": _focus,
    "scroll": _scroll,
    "write_file": _write_file,
    "delete_file": _delete_file,
    "submit_irreversible": _submit_irreversible,
}

#: Executors that reach the machine through ``osal.send_input`` and are
#: therefore inert until a host registers an input backend for its platform.
INPUT_BACKED = ("click", "type_text", "press_key", "focus", "scroll",
                "submit_irreversible")

INPUT_KIND_FOR_EXECUTOR = {
    "click": "click",
    "type_text": "type",
    "press_key": "key",
    "focus": "focus",
    "scroll": "scroll",
    "submit_irreversible": "submit",
}


def input_backend_needs(action, registry):
    """Return any input capability required before ``action`` may run."""
    if action.executor not in INPUT_BACKED:
        return []

    platform = osal.platform_name()
    backend = registry.input_backend_for(platform)
    if backend is None:
        return [f"register an input backend for {platform} "
                f"(driver_core.osal.register_input_backend); no registered "
                f"backend exists, so {action.name} is declared, not executable"]

    kind = INPUT_KIND_FOR_EXECUTOR[action.executor]
    if not osal.input_backend_supports(backend, kind):
        return [f"input backend for {platform} does not support {kind!r}, "
                f"so {action.name} is declared, not executable"]
    return []


def executability(action, registry):
    """Whether ``action`` can run on this driver *right now*.

    Declared is not executable: the vocabulary names every action, but an
    executor that is not registered, or an input-backed one with no backend
    on this platform, refuses at the end of the pipeline. Surfacing that up
    front is what keeps a host from presenting a "click" it can never
    perform. Returns ``(executable, needs)`` where ``needs`` names what is
    missing, in operator terms, and is empty when the action can run.
    """
    needs = []
    if registry.get(action.executor) is None:
        needs.append(f"executor {action.executor!r} is not registered."
                     f"{registry.policy_note}")
    needs.extend(input_backend_needs(action, registry))
    return (not needs), needs


#: Appended to the "not registered" refusal when writes are switched off, so
#: the refusal tells the operator which of the two reasons applies instead
#: of leaving them to guess between a wiring fault and a policy decision.
WRITE_DISABLED_NOTE = (
    " Actions with a side effect are not registered because "
    "DRIVER_ALLOW_WRITE is off; nothing in this build can perform them."
)


class ExecutorRegistry:
    """A closed name -> handler map."""

    def __init__(self, handlers=None, *, policy_note="",
                 input_backend=osal.USE_REGISTERED_INPUT_BACKEND):
        self._handlers = dict(handlers or {})
        #: Extra text appended to the "not registered" refusal.
        self.policy_note = policy_note
        #: A driver-local backend, or the process registration seam when the
        #: sentinel is left in place. Explicit None means no backend.
        self._input_backend = osal.freeze_input_backend(input_backend)

    @property
    def input_backend(self):
        return self._input_backend

    def input_backend_for(self, platform):
        if self.input_backend is osal.USE_REGISTERED_INPUT_BACKEND:
            return osal.input_backends().get(platform)
        return self.input_backend

    def register(self, name, handler):
        if not callable(handler):
            raise ExecutorError(f"executor {name!r} is not callable")
        if name in self._handlers:
            raise ExecutorError(f"executor {name!r} is already registered")
        self._handlers[name] = handler
        return self

    def get(self, name):
        """The handler, or ``None``. There is intentionally no default."""
        return self._handlers.get(name)

    def names(self):
        return tuple(sorted(self._handlers))

    def __contains__(self, name):
        return name in self._handlers

    def __len__(self):
        return len(self._handlers)

    def clone(self):
        return ExecutorRegistry(self._handlers, policy_note=self.policy_note,
                                input_backend=self.input_backend)


def _noop(action, params):
    """`no_action`: the declared way to say "nothing is required here".

    Returning a real result rather than doing nothing is intentional. "The
    driver decided no action was needed" is an outcome worth recording, and
    it is distinguishable in the log from the driver never having run.
    """
    return {"action": action.name, "note": "no action required"}


# --------------------------------------------------------------------------
# read-only probes
# --------------------------------------------------------------------------

#: Program subcommands a probe may run with write authority off. The list
#: is read-only *by construction* rather than by review: ``status``, ``log``
#: and ``diff`` never write to the repository. ``branch``, ``config`` and
#: ``remote`` are absent because each has a writing spelling, and a probe
#: that could create a branch is not a probe.
PROBE_PREFIXES = (
    ("git", "status"),
    ("git", "log"),
    ("git", "diff"),
    ("git", "show"),
    ("git", "rev-parse"),
    ("git", "ls-files"),
    ("git", "describe"),
    ("git", "blame"),
    ("git", "grep"),
)

#: An operator's own declaration of read-only command shapes, as
#: ``;``-separated argv prefixes (for example ``python scripts/check.py``).
#: This is how a headless verifier runs the repo's own check script without
#: holding write authority: the operator declares the exact shape, and the
#: probe refuses everything else. Never a shell string -- the value is
#: tokenised on whitespace and passed to ``osal.run`` as argv.
PROBE_ENV = "DRIVER_PROBE_ALLOWLIST"

#: git spellings that write a file even under a reading subcommand.
PROBE_FORBIDDEN_OPTIONS = ("--output",)

#: Bound on one probe. A read-only check that has not finished in thirty
#: seconds is a check whose answer nobody is waiting for, and the action
#: must not become a way to occupy the driver.
PROBE_TIMEOUT = 30.0


def _head(token):
    """The comparable program name: basename, case-folded, ``.exe`` off."""
    name = os.path.basename(str(token)).lower()
    return name[:-4] if name.endswith(".exe") else name


def probe_prefixes(env=None):
    """The built-in read-only shapes plus the operator's declared ones."""
    env = os.environ if env is None else env
    declared = []
    for entry in str(env.get(PROBE_ENV, "")).split(";"):
        tokens = tuple(token for token in entry.split() if token)
        if tokens:
            declared.append(tokens)
    return tuple(PROBE_PREFIXES) + tuple(declared)


def probe_argv(command, env=None):
    """Validate one probe shape and return its argv list.

    Refusals happen before a process starts, and they name the shape that
    was refused. A string is refused outright rather than tokenised: the
    vocabulary says ``command``, and a caller taught to pass a string is a
    caller one refactor away from a shell.
    """
    if isinstance(command, (str, bytes)) or not isinstance(command, (list, tuple)):
        raise ExecutorError(
            "run_probe takes an argv list, not a command string; the string "
            "form is refused because tokenising it here would make this "
            "executor a second, weaker argv parser")
    if not command:
        raise ExecutorError("run_probe needs a non-empty argv")
    if any(not isinstance(token, str) or not token for token in command):
        raise ExecutorError("run_probe argv tokens must be non-empty strings")
    argv = list(command)
    head = _head(argv[0])
    allowed = False
    for prefix in probe_prefixes(env):
        if len(argv) < len(prefix) or head != _head(prefix[0]):
            continue
        if all(token == declared for token, declared
               in zip(argv[1:], prefix[1:])):
            allowed = True
            break
    if not allowed:
        raise ExecutorError(
            f"probe shape {argv!r} is not allowlisted; declared read-only "
            f"shapes: {_progress_shapes(env)}")
    for token in argv[1:]:
        if any(token == option or token.startswith(option + "=")
               for option in PROBE_FORBIDDEN_OPTIONS):
            raise ExecutorError(
                f"probe option {token!r} writes a file; a read-only probe "
                f"cannot use it")
    return argv


def _progress_shapes(env=None):
    return [" ".join(prefix) for prefix in probe_prefixes(env)]


def _run_probe(action, params, *, env=None):
    """``run_probe``: run one declared read-only command, no shell.

    A probe is the verifier's substitute for write authority: it can ask a
    live machine a question and record the answer without being able to
    change the answer. The refusal surface is therefore data (the declared
    shapes) and the execution is ``osal.run`` -- an argv list through the
    one OS-boundary module, never a shell.
    """
    argv = probe_argv(params.get("command"), env=env)
    result = osal.run(argv, timeout=PROBE_TIMEOUT)
    if result.reason == "not_found":
        raise ExecutorError(f"probe program not found: {argv[0]}")
    if result.reason == "os_error":
        raise ExecutorError(f"probe could not start: {result.error}")
    exit_code = 124 if result.reason == "timeout" else result.returncode
    output = {
        "argv": argv,
        "exit_code": exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if result.reason == "timeout":
        output["timed_out"] = True
    return output


def _read_value(action, params, state=None):
    """`read_value`: pull one field out of the current verified state."""
    if state is None:
        raise ExecutorError(
            "read_value needs a verified state; it cannot read from nothing")
    field = params.get("field")
    if field not in state:
        raise ExecutorError(f"field {field!r} is not in the current state")
    return {"field": field, "value": state[field]}


def _observe(action, params, state=None):
    """`observe`: report that a capture happened and what it yielded."""
    return {
        "action": action.name,
        "fields": sorted(state or {}),
        "observed": state is not None,
    }


def build_read_only_registry(state_provider=None, *,
                             input_backend=osal.USE_REGISTERED_INPUT_BACKEND):
    """The registry containing only the safe built-ins.

    ``state_provider`` is a zero-argument callable returning the current
    verified state, which is how the built-in readers see the extraction
    without the executor tier holding a copy of it.
    """
    def _state():
        return state_provider() if state_provider else None

    registry = ExecutorRegistry({
        "no_action": _noop,
        "read_value": lambda a, p: _read_value(a, p, _state()),
        "observe": lambda a, p: _observe(a, p, _state()),
        # A probe is an observation with an exit code, so it belongs with the
        # read-only built-ins and must not wait for DRIVER_ALLOW_WRITE. The
        # 2026-10-28 oversight audit found the vocabulary advertising it as
        # read-only while the write gate refused it.
        "run_probe": _run_probe,
    }, input_backend=input_backend)
    return registry


def build_driver_registry(state_provider=None, *, allow_write=False,
                          input_backend=osal.USE_REGISTERED_INPUT_BACKEND):
    """What a :class:`~driver_core.driver.Driver` runs with.

    Read-only executors always. The mutating and irreversible ones only when
    ``allow_write`` -- which is how a caller with no consent model at all,
    and no way to present one to a human, ends up with a driver that can
    observe a machine and nothing more. That is the right default for a
    library: building a ``Driver`` should not silently confer the ability to
    delete files.
    """
    registry = build_read_only_registry(state_provider,
                                        input_backend=input_backend)
    input_backend = registry.input_backend
    if allow_write:
        for name, handler in WRITE_EXECUTORS.items():
            if name in INPUT_BACKED and input_backend is not \
                    osal.USE_REGISTERED_INPUT_BACKEND:
                def handler_with_backend(action, params, _handler=handler):
                    return _handler(action, params,
                                    input_backend=input_backend)
                handler = handler_with_backend
            registry.register(name, handler)
    else:
        registry.policy_note = WRITE_DISABLED_NOTE
    return registry
