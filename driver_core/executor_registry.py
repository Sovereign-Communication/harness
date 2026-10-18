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
