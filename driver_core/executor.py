"""The action tier: deterministic executors, and no model anywhere in it.

Everything above this module decides. This module does. That separation is
the whole reason a System One model is safe to put in a loop that touches a
real machine: the model names an action from a closed vocabulary, and the
code that carries it out contains no model, no interpretation, and no
judgement about what the parameters should have been.

Three properties make that safe enough to run unattended:

**Executors are named, declared, and closed.** ``observe``, ``no_action`` and
``read_value`` are built in. The mutating and irreversible executors are
registered only where ``DRIVER_ALLOW_WRITE`` is on, and an unregistered
executor name is a refusal, not a fallback.

**Consent is a capability for one exact ``(action, params)`` pair, and
nothing else.** There is no wildcard. A confirmation captured for ``click``
does not authorise ``delete_file``; one captured for ``delete_file`` on one
path does not authorise it on another; and a blanket ``"*"`` authorises
nothing at all, because a grant nobody attached a path to cannot be a grant
to delete that path. The capability is reusable, which is what makes "click
Next five times" safe under one confirmation -- but reuse is only ever of the
*same pair*, and that reusability is why mutating and irreversible actions
are treated differently. See :meth:`Consent.covers`.

**Dry run is the default shape, not a flag to remember.** Every execution
returns a record of what it *would* have done, and a run configured for dry
mode performs no side effect while still producing the identical record, so
the dry run is a real rehearsal rather than a different code path that
happens to skip the interesting part.
"""
from . import osal
from .actions import IRREVERSIBLE, READ_ONLY
from .audit import KIND_ACTION, required
from .errors import ConsentError, ExecutorError, VocabularyError

#: Declared parameter normalisers, by name. An ``Action`` names one; this is
#: where the name becomes a function. The indirection exists so that
#: :mod:`driver_core.actions` stays a pure declaration and every piece of
#: platform-dependent semantics stays inside :mod:`driver_core.osal`.
#:
#: Normalisation happens *before* consent is checked and before anything is
#: displayed, which is the whole point: the operator is shown the resolved
#: form, so the form they consent to is the form that runs. Doing it during
#: comparison instead would quietly widen every grant.
PARAM_NORMALISERS = {
    "resolve_path": osal.resolve_path,
}


def normalise_params(action, params):
    """Apply an action's declared normalisers to its parameters.

    Fails loudly on an unknown normaliser name. A declaration that names a
    transformation nobody implements would otherwise leave the parameter
    unresolved and compare the unresolved form against a resolved consent,
    which fails closed -- correct by luck rather than by design.
    """
    checked = dict(params)
    for name, normaliser in (action.normalisers or {}).items():
        if normaliser not in PARAM_NORMALISERS:
            raise VocabularyError(
                f"action {action.name!r}: parameter {name!r} declares normaliser "
                f"{normaliser!r}, which is not implemented "
                f"(declared: {sorted(PARAM_NORMALISERS)})")
        if name in checked:
            checked[name] = PARAM_NORMALISERS[normaliser](checked[name])
    return checked


class ExecutionResult:
    """The record of one action, produced whether or not it had an effect."""

    __slots__ = ("action", "action_class", "params", "ok", "detail", "output",
                 "dry_run", "cost")

    def __init__(self, action, action_class, params, ok, detail="", output=None,
                 dry_run=False, cost=0.0):
        self.action = action
        self.action_class = action_class
        self.params = params
        self.ok = ok
        self.detail = detail
        self.output = output
        self.dry_run = dry_run
        self.cost = float(cost or 0.0)

    def to_dict(self):
        return {
            "action": self.action,
            "class": self.action_class,
            "params": self.params,
            "ok": self.ok,
            "detail": self.detail,
            "output": self.output,
            "dry_run": self.dry_run,
            "cost_usd": round(self.cost, 9),
        }

    def __repr__(self):
        return f"ExecutionResult({self.action!r}, ok={self.ok})"


class Consent:
    """A capability for one exact ``(action, params)`` pair.

    The law, in full:

    ==================  ==========================================
    class               requirement
    ==================  ==========================================
    ``READ_ONLY``       none; observation is not a consequence
    ``MUTATING``        exact action and exact params
    ``IRREVERSIBLE``    exact action and exact params, once
    ==================  ==========================================

    **There is no wildcard, for either mutating or irreversible.** Not
    because a wildcard is hard to police, but because it is meaningless:
    somebody who typed "yes" without being shown a path has not agreed to
    delete that path, and a rule that pretends otherwise is teaching the
    operator that green checkboxes mean less than they appear to.

    Reuse is what makes one confirmation for a run of clicks sensible, and
    reuse is safe here because a click is bounded and repeatable. The
    asymmetry with ``IRREVERSIBLE`` is therefore deliberate and must not be
    simplified away: :meth:`spend` consumes an irreversible consent on use,
    because a standing grant cannot mean "yes, delete this path, now and
    whenever this driver next runs".

    :attr:`params` is the parameter set the person was shown, already
    resolved -- consent for ``~/notes`` does not match a proposal of
    ``/home/x/notes``, because the operator is shown the resolved path.
    """

    __slots__ = ("granted", "action", "params", "by", "_spent")

    def __init__(self, granted, action="*", params=None, by="operator"):
        self.granted = bool(granted)
        self.action = action
        self.params = dict(params or {})
        self.by = by
        #: Irreversible actions this consent has already authorised.
        self._spent = set()

    def covers(self, action_name, action_class, params):
        """Whether this consent authorises exactly this action, right now."""
        if action_class == READ_ONLY:
            return True
        if not self.granted:
            return False
        # No wildcard. This single line is the difference between "a
        # capability for one pair" and "a permission", and the whole design
        # rests on it being here.
        if self.action != action_name:
            return False
        if self.params != dict(params or {}):
            return False
        if action_class == IRREVERSIBLE and action_name in self._spent:
            # Already used. A fresh confirmation is required, every time.
            return False
        return True

    def spend(self, action_name):
        """Consume an irreversible consent so it cannot be re-used.

        Called only after the action actually succeeded, and never during a
        dry run: a rehearsal performed no consequence, so it has consumed
        nothing. A *failed* irreversible action equally consumes nothing --
        which is why every irreversible executor here refuses before it
        commits, rather than part-way through.
        """
        self._spent.add(action_name)

    @property
    def spent(self):
        """Irreversible actions this consent has already authorised."""
        return frozenset(self._spent)

    def to_dict(self):
        return {"granted": self.granted, "action": self.action,
                "params": self.params, "by": self.by}

    def __repr__(self):
        return f"Consent(granted={self.granted}, action={self.action!r})"


#: The default when a caller supplies nothing.
#:
#: Its ``"*"`` is now a wildcard that authorises *nothing*, so this grants
#: read-only work and refuses every mutating and irreversible action --
#: which is exactly the behaviour that makes it a safe default for a caller
#: that only ever observes.
NO_CONSENT_NEEDED = Consent(True, "*", by="system")


class Executor:
    """The model-free action tier."""

    def __init__(self, *, vocabulary, registry, audit, dry_run=False,
                 clock=None):
        self.vocabulary = vocabulary
        self.registry = registry
        self.dry_run = bool(dry_run)
        #: Required, not optional. This class runs actions against the
        #: machine, and an action that runs unrecorded is the failure the
        #: whole log exists to make visible.
        self.audit = required(audit, "An Executor")
        self._clock = clock

    def resolve(self, action_name, params):
        """Validate an action name and its parameters against the vocabulary.

        Three steps in a fixed order: the name must be declared, the
        parameters must match the declaration exactly, and only then are they
        normalised. Both halves raise rather than default. This is the last
        point at which an undeclared name can be caught, and a name that
        reaches an executor unvalidated is a name the rest of the system has
        no reason to distrust.

        Normalisation comes last so that everything downstream -- the consent
        check, the executor, the audit record, and anything shown to the
        operator -- sees one form. See :func:`normalise_params`.
        """
        action = self.vocabulary.resolve(action_name)
        checked = action.check_params(params or {})
        return action, normalise_params(action, checked)

    def check_consent(self, action, params, consent):
        if action.action_class == READ_ONLY:
            return True
        if consent is None:
            raise ConsentError(
                f"action {action.name!r} is {action.action_class} and no "
                f"consent was supplied")
        if consent.covers(action.name, action.action_class, params):
            return True
        if consent.action == "*":
            raise ConsentError(
                f"consent (granted for {consent.action!r}) does not authorise "
                f"{action.name!r}; there is no wildcard grant, because nobody "
                f"was shown what {action.name!r} with {params} would do. "
                f"Re-confirm for this exact action and these exact params")
        raise ConsentError(
            f"consent (granted for {consent.action!r} "
            f"{consent.params}) does not authorise {action.name!r} with "
            f"params {params}; re-confirmation is required")

    def execute(self, action_name, params=None, *, consent=NO_CONSENT_NEEDED,
                step_id="step"):
        """Perform one declared action, or explain why it was not performed."""
        action, checked = self.resolve(action_name, params or {})
        self.check_consent(action, checked, consent)

        handler = self.registry.get(action.executor)
        if handler is None:
            # An action that declares an executor nobody registered is a
            # wiring fault or a policy decision, and it is refused, loudly,
            # rather than skipped -- a silent skip would look like a
            # successful no-op. The registry's note says which of the two
            # this is, so the refusal is actionable rather than a puzzle.
            raise ExecutorError(
                f"action {action.name!r} names executor {action.executor!r}, "
                f"which is not registered; registered: "
                f"{sorted(self.registry.names())}.{self.registry.policy_note}")

        if self.dry_run:
            result = ExecutionResult(
                action.name, action.action_class, checked, True,
                detail="dry run: no side effect was performed", output=None,
                dry_run=True)
        else:
            result = self._invoke(handler, action, checked)

        # An irreversible consent is consumed only by a consequence that
        # actually happened. A dry run performed none, and a failed one was
        # refused before it committed, so neither spends anything.
        if (result.ok and not result.dry_run
                and action.action_class == IRREVERSIBLE
                and consent is not None):
            consent.spend(action.name)

        if self.audit is not None:
            self.audit.append(
                KIND_ACTION, step_id=step_id, action=action.name,
                action_class=action.action_class, ok=result.ok,
                detail=result.detail, dry_run=result.dry_run,
                consent=consent.to_dict() if consent else None)
        return result

    def _invoke(self, handler, action, params):
        try:
            output = handler(action, params)
        except Exception as exc:
            return ExecutionResult(action.name, action.action_class, params,
                                   False, detail=f"executor failed: {exc}")
        return ExecutionResult(action.name, action.action_class, params, True,
                               output=output)
