"""The driver: one owner for the whole sequence.

This is the seam. Everything above it is a decision about *how* a step
proceeds; everything below it is a capability that knows how to do one
thing. :class:`Driver` is the only class that knows the order, and it is the
object a host project talks to -- which is what makes the integration
contract a single method with a plain-data result rather than a negotiation
between five subsystems.

The order is the design, so it is worth stating plainly:

1. **Capture** a structured source if one can answer; pixels only as a
   last resort.
2. **Extract** through N independent extractors.
3. **Tally** deterministically, and refuse anything that is not unanimous
   among those who answered.
4. **Gate** on the tally, before any state reaches a model.
5. **Decide** with Jev, over the declared action vocabulary only.
6. **Gate** on confidence and on the guards.
7. **Execute** with a model-free executor, under consent, with parameters
   that came from the caller and not from the model.
8. **Escalate** if any gate said no.

The provenance of the *parameters* is as load-bearing as the provenance of
the decision. The model is allowed to name an action out of a closed
vocabulary; it is never allowed to supply a path, a URL or a string to type,
because those are the things a model will confidently invent. They arrive
from the caller -- the same place consent arrives from -- which is what lets
an operator be shown the action *and* its parameters as a single pair and
agree to exactly that.

Each step's verdict is carried forward rather than recomputed, so a blocked
extraction can never be followed by a decision that assumed it succeeded --
the pipeline returns at the first refusal instead of continuing with a
half-built state.

A blocked step is a **normal, successful outcome**. ``StepResult.ok`` is
false and the result explains why; that is the system working, not failing,
and the audit log records it as a first-class event.
"""
import uuid

from . import policy
from .actions import DEFAULT_VOCABULARY
from .audit import (
    KIND_CAPTURE, KIND_ESCALATION, KIND_EXTRACTION, KIND_REFUSAL, AuditLog,
    required, same_chain,
)
from .budget import Budget
from .consensus import tally
from .config import Settings, default_audit_path, load_settings
from .errors import (
    ConsentError, OsalError, PerceptionUnavailable, PersistentStateUnavailable,
    VocabularyError,
)
from . import osal
from .executor import Executor
from .executor_registry import build_driver_registry, executability
from .extractors import ExtractorPool
from .jev_client import JevClient
from .perception import GUI, select_capture
from .states import SCREEN_SCHEMA
from .vision_verify import VisionVerifier
from .wiring import configured_pools, configured_sources


class StepResult:
    """Everything one step produced, in a form a host project can consume.

    Deliberately plain data. A caller on the far side of a process boundary
    needs to branch on *why* a step stopped, and the six reasons are distinct
    enough to matter: a shortfall and a disagreement and a low confidence all
    call for different responses from the code that owns the screen.
    """

    __slots__ = ("step_id", "ok", "stopped_at", "reason", "detail", "capture",
                 "agreement", "decision", "execution", "receipt", "cost", "needs",
                 "verification")

    def __init__(self, step_id, *, ok, stopped_at, reason=None, detail="",
                 capture=None, agreement=None, decision=None, execution=None,
                 receipt=None, cost=0.0, needs=(), verification=None):
        self.step_id = step_id
        self.ok = ok
        self.stopped_at = stopped_at
        self.reason = reason
        self.detail = detail
        self.capture = capture
        self.agreement = agreement
        self.decision = decision
        self.execution = execution
        self.receipt = receipt
        self.cost = float(cost or 0.0)
        self.needs = list(needs)
        self.verification = verification

    def to_dict(self):
        """The wire form -- the contract a host adapter serialises."""
        return {
            "step_id": self.step_id,
            "ok": self.ok,
            "stopped_at": self.stopped_at,
            "reason": self.reason,
            "detail": self.detail,
            "capture": self.capture.summary() if self.capture else None,
            "agreement": self.agreement.to_dict() if self.agreement else None,
            "decision": self.decision.to_dict() if self.decision else None,
            "execution": self.execution.to_dict() if self.execution else None,
            "receipt": self.receipt,
            "cost_usd": round(self.cost, 9),
            "needs": list(self.needs),
            "verification": (self.verification.to_dict()
                             if getattr(self.verification, "to_dict", None)
                             else self.verification),
        }

    def __repr__(self):
        return f"StepResult({self.step_id!r}, ok={self.ok}, at={self.stopped_at})"


class Driver:
    """The one owner of the pipeline.

    Collaborators are injected rather than constructed, which is what keeps
    the host integration honest: a caller supplies its own budget, its own
    audit log and its own executors, and cannot accidentally end up with two
    of any of them.
    """

    def __init__(self, *, audit, settings=None, budget=None,
                 vocabulary=DEFAULT_VOCABULARY, pool=None, jev=None,
                 executor=None, sources=None, screen=None, pools=None,
                 input_backend=None, vision=None):
        self.settings = settings or load_settings()
        self.budget = budget or Budget(self.settings.run_ceiling_usd,
                                       step_ceiling_usd=self.settings.step_ceiling_usd)
        #: Required, not optional, and not defaulted to a path. A driver
        #: runs actions, so where its chain goes is a decision its caller
        #: makes and can see -- :func:`driver_from_settings` is the pairing
        #: the CLI and the service both use.
        self.audit = required(audit, "A Driver")
        self.vocabulary = vocabulary

        # Each collaborator has exactly one owner. ``None`` means "use what
        # the settings declare" and anything else means "this is what there
        # is" -- so a caller who passes ``sources=()`` gets a driver that
        # cannot observe rather than a driver that merged the declared
        # sources underneath, and a caller who passes ``pools`` gets exactly
        # the pools they passed.
        #: The fallback pool, used for a target whose class is undeclared.
        self.pool = pool or ExtractorPool([])
        #: Pools keyed by declared target class. This is where "three of the
        #: four classes never reach the vision tier" is enforced on the
        #: extraction side: a class is only ever handed the pool that
        #: declared it, so a vision pool is not a candidate for a ``dom``
        #: target rather than merely being discouraged.
        #
        # Built before the sources on purpose: whether the screen source
        # should take a screenshot is a fact about the pool that will read
        # the capture, and asking the pool afterwards would mean either
        # capturing and discarding, or a second place that has to know what
        # a vision pool is.
        self.pools = (configured_pools(self.settings, budget=self.budget,
                                       audit=self.audit)
                      if pools is None else dict(pools))

        gui_pool = self.pools.get(GUI)
        self.sources = list(
            configured_sources(
                self.settings,
                reads_captures=(gui_pool is None or gui_pool.reads_captures))
            if sources is None else sources)
        if screen is not None and not any(s is screen for s in self.sources):
            # A screen source passed separately joins the one list rather
            # than becoming a second handle onto it. Two handles are how the
            # same pixels got captured twice and the vision tier got reported
            # twice; ``screen`` below is now derived, not stored.
            self.sources.append(screen)

        self.jev = jev or JevClient(self.settings, budget=self.budget,
                                    audit=self.audit)
        #: Post-action vision verification, when the operator opted in. An
        #: injected verifier is the caller's explicit choice; otherwise the
        #: flag pair decides. The gate is pixels, so it is never built from
        #: ``DRIVER_VISION_VERIFY`` alone: a driver that has not admitted
        #: screen pixels has not admitted a screenshot either. The vision
        #: pool itself declares ``pixels=False`` (its slots read the window
        #: description), so the verifier gets its own pixel source rather
        #: than silently reusing a words-only one.
        self.vision = vision
        if (self.vision is None and self.settings.vision_verify_enabled
                and self.settings.screen_enabled):
            self.vision = VisionVerifier(
                self.settings, budget=self.budget, audit=self.audit,
                source=(self.screen
                        if self.screen is not None
                        and getattr(self.screen, "pixels", False) else None))
        # Executors with a side effect are registered only when the operator
        # has declared this machine may be written to. A caller that builds
        # a Driver and passes no settings gets an observer.
        #
        # A caller that brings its own executor brings it across the same
        # line the one built here stands on: it has to record to *this*
        # driver's chain, or the action would land in a log of its own that
        # nothing links to the decision that caused it. Checking only our
        # own constructor would have left the seam beside it open, which is
        # where an unrecorded action would actually arrive from.
        if executor is not None:
            same_chain(getattr(executor, "audit", None), self.audit,
                       type(executor).__name__)
        if (input_backend in (None, osal.USE_REGISTERED_INPUT_BACKEND)
                and self.settings.allow_write
                and self.settings.native_input_enabled):
            # The native backend is never installed as process-global state.
            # Each driver gets only the backend its own explicit settings
            # authorize, so a second driver cannot inherit this capability.
            input_backend = osal.native_input_backend()
        self.executor = executor or Executor(
            vocabulary=vocabulary,
            registry=build_driver_registry(
                input_backend=input_backend,
                allow_write=self.settings.allow_write,
                launch_app=self.settings.launch_app_enabled),
            dry_run=self.settings.dry_run,
            audit=self.audit)

    def register_executor(self, name, handler):
        """Add one executor to this driver's closed registry."""
        self.executor.registry.register(name, handler)
        return self

    # -- the pipeline ----------------------------------------------------

    def step(self, target, *, schema=None, consent=None, step_id=None,
             prefer=(), require_stable=True, params=None):
        """Run one full step. Returns a :class:`StepResult`; never raises for
        a blocked or degraded condition.

        ``params`` are the action's parameters, and they come from the
        *caller* -- never from the decision tier. That provenance is the
        point: a model naming an action is safe because the vocabulary is
        closed, but a model supplying a path or a URL is the exact failure
        this design exists to prevent. Keeping the parameters on the
        caller's side of the line is also what lets them be shown to an
        operator and consented to as a single ``(action, params)`` pair.
        """
        step_id = step_id or uuid.uuid4().hex[:12]
        schema = schema or SCREEN_SCHEMA

        # 1. capture
        try:
            capture = self._capture(target, prefer)
        except PerceptionUnavailable as exc:
            return self._stop(step_id, "capture", "no_capture", str(exc),
                              needs=self._capture_needs())
        self.audit.append(KIND_CAPTURE, step_id=step_id, **capture.summary())

        # 2 + 3. extract and tally
        pool = self._pool_for(capture)
        votes = pool.run(capture, schema)
        agreement = tally(votes, schema, quorum=self.settings.quorum,
                          min_agreement=self.settings.min_agreement)
        receipt = agreement.receipt(schema.identity(), step_id)
        self.audit.append(KIND_EXTRACTION, step_id=step_id, **receipt)

        # 4. gate before any model sees the state
        gate = policy.check_agreement(
            agreement, quorum=self.settings.quorum,
            min_agreement=self.settings.min_agreement)
        if not gate.passed:
            return self._stop(step_id, "agreement", gate.reason, gate.detail,
                              capture=capture, agreement=agreement,
                              receipt=receipt, cost=agreement.cost)

        # 5. decide
        decision = self.jev.decide(
            agreement.state, self.vocabulary, receipt=receipt,
            step_id=step_id,
            # What this build can actually do, not merely what it declares. A
            # model offered an action with no handler can only ever be refused,
            # and a refusal the model had no way to avoid is a fault in what it
            # was offered rather than in what it chose.
            performable=self.executor.registry.names(),
            # The frame history's judgment, when the vision tier has one.
            # The decision tier's stability guard is fed compared captures
            # instead of re-asking (and re-billing) a single-frame guess;
            # ``None`` leaves the question in place, untouched.
            stability=pool.stability())

        # 6. gate on the decision
        proposed = self._proposed(decision)
        decision_gate = policy.check_decision(
            decision, threshold=self.settings.confidence_threshold,
            require_stable=require_stable,
            # The per-action ladder, when the operator opted in: the decided
            # action's consequence class selects the rung, which can only
            # raise the bar the threshold already sets. Without the flag the
            # ladder is not passed and the gate is exactly what it was.
            action_class=(proposed.action_class if proposed is not None
                          else None),
            ladder=(self.settings.confidence_ladder
                    if self.settings.jev_ladder else None))
        if not decision_gate.passed:
            if not (decision_gate.confirmable
                    and self._confirmed(proposed, params, consent)):
                self.audit.append(KIND_ESCALATION, step_id=step_id,
                                  reason=decision_gate.reason,
                                  detail=decision_gate.detail)
                return self._stop(step_id, "decision", decision_gate.reason,
                                  decision_gate.detail, capture=capture,
                                  agreement=agreement, decision=decision,
                                  receipt=receipt, cost=decision.cost)
            # The ladder's "or a confirmation step": a consent bound to this
            # exact proposed (action, params) satisfies the confirmation
            # rung below the act rung, so the step proceeds. The record says
            # a human was asked -- a below-rung action must never read as a
            # clean pass in the log -- and every gate above stays untouched:
            # a confirmation substitutes for the confidence rung and for
            # nothing else.
            self.audit.append(KIND_ESCALATION, step_id=step_id,
                              action=proposed.name, reason="review_confirmed",
                              detail=f"consent satisfied the confirmation "
                                     f"rung: {decision_gate.detail}")

        # 7. execute -- no model past this line
        #    A model naming an action the vocabulary does not declare is a
        #    refusal, not a crash. It is reachable in principle (a caller
        #    injecting a hand-built envelope, a validation rule drifting),
        #    and the correct response is the same one as any other blocked
        #    step: stop, name it, log it, hand it back.
        try:
            action = self.vocabulary.resolve(decision.recommended_action)
        except VocabularyError as exc:
            return self._stop(step_id, "execution", "undeclared_action", str(exc),
                              capture=capture, agreement=agreement,
                              decision=decision, receipt=receipt,
                              cost=decision.cost)
        guard_cost = 0.0
        try:
            # Missing consent is the first thing a caller can fix, so it is
            # named before parameter validation can report a different fault.
            if (action.requires_consent and consent is None
                    and params is not None):
                raise ConsentError(
                    f"action {action.name!r} is {action.action_class} and no "
                    f"consent was supplied")
            if self.settings.jev_guardrails and action.requires_consent:
                # The guardrail pack judges the proposed action against the
                # observed screen at the execution gate. It gates acting,
                # never observing: a read-only action is safe to repeat, so
                # the pack is not asked about one. Anything short of a pass
                # is a refusal with the verdict named -- except a review,
                # which the same bound consent that satisfies the ladder's
                # confirmation rung may satisfy here. A block is never
                # confirmable: the remedy for a hazard is a different
                # action, not a braver yes.
                verdict = self.jev.guard(action.name, agreement.state,
                                         step_id=step_id)
                guard_cost = verdict.cost
                if not verdict.passed:
                    if not (verdict.confirmable
                            and self._confirmed(action, params, consent)):
                        return self._stop(
                            step_id, "execution", "execution_refused",
                            f"guardrail {verdict.verdict}: {verdict.detail}",
                            capture=capture, agreement=agreement,
                            decision=decision, receipt=receipt,
                            cost=decision.cost + guard_cost)
                    self.audit.append(
                        KIND_ESCALATION, step_id=step_id, action=action.name,
                        reason="review_confirmed",
                        detail=f"consent satisfied the guardrail review: "
                               f"{verdict.detail}")
            execution = self.executor.execute(
                action.name, _params_for(action, params),
                consent=consent, step_id=step_id)
        except PersistentStateUnavailable:
            # Durable replay-state failures must reach the REST boundary as
            # server errors, not as ordinary action refusals.
            raise
        except Exception as exc:
            return self._stop(step_id, "execution", "execution_refused", str(exc),
                              capture=capture, agreement=agreement,
                              decision=decision, receipt=receipt,
                              cost=decision.cost + guard_cost,
                              needs=executability(
                                  action, self.executor.registry)[1])

        # An executor that refused did not do the thing. The pipeline
        # reaching the end is not the same as the action having happened,
        # and a host that branches on ``ok`` must never be told a click
        # occurred when no backend was registered to perform it.
        #
        # This is the same refusal, and the same already-declared reason, as
        # an executor that raised -- just reported at the layer the caller
        # actually reads. The execution record still travels with the result
        # so the failure is inspectable rather than merely announced.
        if not execution.ok:
            return self._stop(step_id, "execution", "execution_refused",
                              execution.detail, capture=capture,
                              agreement=agreement, decision=decision,
                              execution=execution, receipt=receipt,
                              cost=decision.cost + guard_cost,
                              needs=executability(
                                  action, self.executor.registry)[1])

        # 8. verify the visible result, when the operator opted in. The
        #    executor's ``ok`` says the action was performed; it does not say
        #    the application painted or the typed string appeared. A failed
        #    verification is therefore a refusal with its own stop tier, not
        #    a note appended to a successful action. A dry run performed no
        #    effect, so there is nothing visible to verify.
        verification = None
        verification_cost = 0.0
        if (execution.ok and not execution.dry_run and self.vision is not None
                and action.name in ("launch_app", "type_text")):
            if action.name == "launch_app":
                verification = self.vision.verify_application(
                    str(execution.params.get("app_name", "")),
                    step_id=step_id)
            else:
                verification = self.vision.verify_text(
                    str(execution.params.get("text", "")), step_id=step_id)
            verification_cost = float(getattr(verification, "cost", 0.0))
            if not verification.passed:
                detail = (f"post-action {action.name} verification failed: "
                          f"{verification.reason}")
                self.audit.append(
                    KIND_ESCALATION, step_id=step_id,
                    reason="verification_failed", action=action.name,
                    detail=detail)
                return self._stop(
                    step_id, "verification", "verification_failed", detail,
                    capture=capture, agreement=agreement, decision=decision,
                    execution=execution, receipt=receipt,
                    cost=(agreement.cost + decision.cost + guard_cost
                          + verification_cost),
                    verification=verification)

        return StepResult(step_id, ok=True, stopped_at="executed",
                          capture=capture, agreement=agreement,
                          decision=decision, execution=execution,
                          receipt=receipt, verification=verification,
                          cost=(agreement.cost + decision.cost + guard_cost
                                + verification_cost))

    @property
    def screen(self):
        """The screen source among :attr:`sources`, or ``None``.

        Derived, never stored. The vision tier is the last resort by
        construction -- it is in the one ordered list, and
        :func:`select_capture` filters that list by target class -- so a
        second field holding "the screen one" could only ever disagree with
        the list it was supposed to be describing.
        """
        return next((s for s in self.sources if s.name == "screen"), None)

    def _capture(self, target, prefer):
        """One capture, through the ordered tier chain.

        ``self.sources`` is the whole chain, screen included, and it is
        filtered by target class rather than consulted in a fixed order --
        that filtering is the guarantee, not this loop.
        """
        return select_capture(target, self.sources, prefer=prefer)

    def _pool_for(self, capture):
        """The extractor pool for this capture's target class.

        Falls back to the undeclared-class pool. Refusing here rather than
        downstream means the "no vision for a structured class" rule holds
        even when a caller wires an unusually generous pool map.
        """
        target_class = getattr(capture, "target_class", None)
        if target_class is not None and target_class in self.pools:
            return self.pools[target_class]
        return self.pool

    def _proposed(self, decision):
        """The decided action's declaration, or ``None`` for a name the
        vocabulary does not declare.

        ``None`` means the confidence ladder does not refine this decision
        and no confirmation can be established -- the right answers for a
        name that is refused at the execution gate exactly as before: a
        class nothing was declared for is not one to invent a threshold or
        a confirmation for here.
        """
        try:
            return self.vocabulary.resolve(decision.recommended_action)
        except VocabularyError:
            return None

    def _confirmed(self, action, params, consent):
        """Whether a human's consent confirms this exact proposed action.

        The confirmation *is* the executor's consent check and nothing else:
        the matching rules have one owner, and this delegates to them exactly
        as the wrong-action check does. "Confirmed" therefore means what the
        consent machinery means -- granted, bound to this action name and
        these resolved parameters, unspent for an irreversible -- and a
        proposal that cannot even be resolved is not one that was confirmed.

        Only a consent-requiring action can be confirmed. An action that
        needs no consent cannot borrow one, so a review band on a read-only
        action stays a stop rather than being waved through by a consent
        that was never needed in the first place.
        """
        if action is None or consent is None or not action.requires_consent:
            return False
        try:
            _, resolved = self.executor.resolve(action.name, params or {})
            self.executor.check_consent(action, resolved, consent)
        except (VocabularyError, ConsentError, OsalError):
            # Each of these is a declared refusal of the machinery being
            # asked -- undeclared params, an unresolvable path, a consent
            # bound elsewhere -- and all three answer the same way here:
            # nothing was confirmed, so the stop stands as it did before.
            return False
        return True

    def _capture_needs(self):
        """What a ``no_capture`` stop is waiting on, in operator terms."""
        from .executor_registry import executability
        configured = sorted({s.name for s in self.sources}) or ["none"]
        needs = [
            "configure a perception source for the target class (set "
            "DRIVER_CLI_COMMAND, DRIVER_MCP_COMMAND with DRIVER_MCP_TOOL, "
            "DRIVER_DOM_URL, or DRIVER_SCREEN=1); configured now: "
            + ", ".join(configured)]
        blocked = []
        for action in self.vocabulary.actions():
            if action.requires_consent:
                ok, _why = executability(action, self.executor.registry)
                if not ok:
                    blocked.append(action.name)
        if blocked:
            needs.append(
                "to act (not just observe): register an input backend "
                "(driver_core.osal.register_input_backend) and/or set "
                "DRIVER_ALLOW_WRITE=1; declared but not executable now: "
                + ", ".join(blocked))
        return needs

    def _stop(self, step_id, stopped_at, reason, detail, **kwargs):
        self.audit.append(KIND_REFUSAL, step_id=step_id, stopped_at=stopped_at,
                          reason=reason, detail=detail)
        return StepResult(step_id, ok=False, stopped_at=stopped_at,
                          reason=reason, detail=detail, **kwargs)


def _params_for(action, supplied):
    """The parameters for one action, taken from the caller and never the model.

    The decision tier names an action; it does not supply arguments, because
    a model inventing a path or a URL is precisely the failure this design
    exists to prevent. They arrive from the caller instead -- the same place
    the consent arrives from, which is what makes them one thing to be shown
    and agreed to rather than two that can drift apart.

    An action that declares no parameters and is handed some refuses rather
    than ignoring them. A parameter nobody declared is the same class of
    problem as an undeclared action: something outside this contract is
    trying to influence execution, and quietly discarding it would hide that.
    """
    supplied = dict(supplied or {})
    if not action.params:
        if supplied:
            raise VocabularyError(
                f"action {action.name!r} takes no parameters, but "
                f"{sorted(supplied)} were supplied")
        return {}
    return action.check_params(supplied)


def driver_from_settings(settings=None):
    """A driver holding the chain its own settings declare.

    The pairing lives here and nowhere else. The two facts are "where does
    this run's chain go" and "a driver that acts needs a chain", and until
    this existed each entry point wrote both of them out: the CLI, and the
    service twice over. Three copies of a rule is the problem this package
    keeps removing, and leaving it here would have been removing it from one
    file and adding it to three.

    It lives beside ``Driver`` rather than in :mod:`wiring` because
    ``driver`` already imports ``wiring``; the other direction would be a
    cycle. Everything a caller would otherwise want to vary -- the log, the
    collaborators, the vocabulary -- is what :class:`Driver` takes directly.
    """
    settings = settings or load_settings()
    return Driver(settings=settings,
                  audit=AuditLog(settings.audit_path or default_audit_path()))


__all__ = ["Driver", "StepResult", "Settings", "load_settings", "Budget",
           "AuditLog", "driver_from_settings"]
