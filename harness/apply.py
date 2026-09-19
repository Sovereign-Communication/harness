"""Scoped code edits with a verification loop, cost ceiling, and consent.

This is the apply ENGINE: the round loop (edit -> write -> verify -> retry),
sovereignty gates, model rotation, escalation, continuation, and the cost
accounting around them. It deliberately contains no prompt text (see
:mod:`harness.prompts`) and no direct filesystem or gate-execution policy
(see :mod:`harness.filesafety`) -- the engine orchestrates; those modules
own their contracts.

Shape: :meth:`ApplyEngine.apply_edit` validates and freezes its arguments
into one :class:`ApplyRequest` (:meth:`ApplyEngine._prepare`), then
:func:`_apply_edit` runs the phases -- initial consent, per-round renewal,
rotation, merge, gate, escalation, and the honest terminal assembly. Every
round-to-round mutation lives in one :class:`RunState`; every terminal
result is built by :func:`_terminal_result` or :func:`_defer_result`, so a
new outcome field has exactly one place to land.

Ports the good parts of SCMessenger's morph_lite.py (single-file <500-line,
hard cost ceiling) and delegate_task.py (apply -> run a verification gate ->
feed the failure output back -> retry up to N rounds, with a guard against
vacuous success). Adds the sovereignty layer and free-tier iteration:

  * Consent probe before dispatch, plus continued consensus: consent is
    renewed before every verify round, and a mid-task deferral stops the edit
    with partial work preserved.
  * Capability-blocker dovetail: the model is told to do its best, assume
    nothing, and DEFER the remaining work instead of guessing when it hits its
    capability limit (HARNESS_DEFER: marker). Partial work is preserved and
    returned as a continuation state.
  * Rotation on error: if a model errors or rate-limits (429), the harness
    rotates to the next model in the apply pool instead of failing.
  * Continuation mode: a deferred/incomplete task can be resumed by a later
    call (or a different model) from the preserved partial state.
  * Multi-file batch: one governed session per file through the same
    engine/router/gate, sharing the task budget, fail-fast on first error.
"""
import os
import uuid

from .apply_state import ApplyRequest
from .apply_policy import ApplyEngineMixin
from .apply_gate import GatePolicy

from .prompts import (
    MAX_FILE_LINES, MAX_APPLY_ROUNDS,
)
from .filesafety import (_line_count, default_run_verify, validate_target_file,
                          validate_verify_command, VERIFY_TIMEOUT)

from . import trust as trust_policy
from .capability import ordered_pool
from .config import HARD_TASK_MAX_COST, MORPH_MODEL
from .batch import run_batch
from .continuation import validate_continuation
from .errors import HarnessError
from .validation import validate_apply_request



class ApplyEngine(ApplyEngineMixin):
    """Scoped-edit engine: lifecycle + per-round flow.

    This module owns the engine lifecycle -- construction, the public
    ``apply_edit`` entry (validation/freezing into one ApplyRequest), and
    batch dispatch. The per-round machinery (billing, the edit loop,
    consent, rotation, deferrals, escalation) is mixed in verbatim from
    :mod:`harness.apply_policy` so all call sites and tests keep their
    shape. One owner per concern; prompt text and filesystem/gate policy
    stay in their own modules.
    """
    def __init__(self, transport, api_key, governor, ledger, router,
                 default_require_consent=True, run_verify=None,
                 default_renew_consent=True, reasoning_effort="auto",
                 reasoning_token_budget=0.4, default_max_rotations=3,
                 default_task_max_cost=0.10, use_free=False,
                 allowed_roots=None,
                 default_require_diff_authorization=False,
                 default_attest_model=None):
        self.transport = transport
        self.api_key = api_key
        self.governor = governor
        self.ledger = ledger
        self.router = router
        self.default_require_consent = default_require_consent
        self.run_verify = run_verify or default_run_verify
        self.default_renew_consent = default_renew_consent
        self.reasoning_effort = reasoning_effort
        self.reasoning_token_budget = reasoning_token_budget
        self.default_max_rotations = default_max_rotations
        self.default_task_max_cost = default_task_max_cost
        # Diff-bound independent authorization (M4 phase 2): when enabled,
        # every candidate write needs the verifier model's allow FIRST.
        self.default_require_diff_authorization = \
            bool(default_require_diff_authorization)
        # None = the router's judge model verifies (it is already the
        # independent second voice; independence from the apply model is
        # the caller's responsibility when overriding).
        self.default_attest_model = default_attest_model
        # Free-tier flag for capability-aware pool ordering (cheap-first on
        # the paid tier, reliability-first when every model costs $0).
        self.use_free = bool(use_free)
        # Optional filesystem jail for library/CLI apply (MCP already enforces
        # roots). Empty/None leaves the historical unrestricted CLI behavior.
        self.allowed_roots = [
            os.path.realpath(os.path.abspath(r))
            for r in (allowed_roots or [])
            if r
        ]
        self.gate = GatePolicy(ledger, governor, transport, api_key)

    def _enforce_roots(self, file_path):
        """Refuse targets outside configured allowed_roots (realpath)."""
        if not self.allowed_roots:
            return
        real = os.path.realpath(file_path)
        for root in self.allowed_roots:
            if real == root or real.startswith(root + os.sep):
                return
        raise HarnessError(
            f"file_path outside allowed_roots: {file_path} "
            f"(configured roots: {', '.join(self.allowed_roots)})")

    def _step_primary_for_trust(self, report, allow_escalation,
                                verify_only, task_id):
        """Front-loaded escalation on a trust deny: the cheapest escalation
        rung whose own trust band allows the mutation becomes the primary.
        Returns (model, ordered, decision) or None when escalation cannot
        unlock the write -- disarmed, no rungs, or no rung scores allow.
        Pure trust arithmetic: no network, no spend (model_trust reads the
        participation report, not the wire)."""
        allowed = (self.router.allow_escalation if allow_escalation is None
                   else allow_escalation)
        if verify_only or not allowed or not self.router.escalation_pool:
            return None
        caller = getattr(self.ledger, "caller", None)
        host_score, host_reasons = trust_policy.host_trust(report, caller=caller)
        for rung in self.router.escalation_pool:
            m_score, m_reasons = trust_policy.model_trust(rung, report)
            combined = trust_policy.combined_trust(host_score, m_score)
            if trust_policy.gate_for_write_exec(combined) != "allow":
                continue
            correctness = trust_policy.correctness_level(rung, report)
            # An ESCALATION, not a denial: ledger it as its own event type
            # so the analytics never count it as a trust strike against the
            # rung being promoted (that poisoned every rescue rung).
            self.ledger.append(
                "trust_escalation", task_id=task_id, model=rung,
                reason="primary stepped up: primary model trust denied",
                combined=combined, correctness=correctness)
            return rung, {"combined": combined, "correctness": correctness}

    def _route_pool(self, apply_pool=None):
        """Capability-ordered apply pool for THIS request, or (None, None) to
        fall back to the configured pool and model. Per-request: nothing here
        mutates the router, so shared engines cannot leak routing state.
        The pool is filtered to models the catalog knows; an ordering that is
        empty, uninformed (no profiles), or all-unknown never reroutes."""
        pool = list(apply_pool) if apply_pool is not None else list(self.router.apply_pool)
        fetch = getattr(self.governor, "fetch_models", None)
        if fetch is None:
            return None, None
        ordered, profiles = ordered_pool(
            pool, governor=self.governor, ledger=self.ledger,
            task="code", free_tier=self.use_free)
        if profiles is None or not ordered:
            return None, None
        return ordered, profiles

    def apply_edit(self, **kwargs):
        """Apply a scoped edit with a verification loop, sovereignty gate,
        capability deferral, rotation, and continuation support.

        Parameters (all keyword): task_id, file_path, instruction,
        edit_snippet, verify_cmd, max_rounds, require_consent, model,
        max_tokens, task_max_cost, allow_escalation, reasoning_effort,
        renew_consent, max_rotations, continuation, backend, verify_only,
        max_lines, task_runner, apply_pool -- validated and frozen by
        :meth:`_prepare` into an :class:`ApplyRequest`.

        ``apply_pool`` overrides the router's apply pool for THIS request only
        (already capability-ordered by the caller, or ordered here when the
        caller passes the raw pool) -- the router is never mutated.
        """
        return self._apply_edit(self._prepare(kwargs))

    # ---------------- request preparation (argument policy, one place) ----

    def _prepare(self, kwargs):
        """Validate the request and freeze engine defaults into one request
        object. All argument policy lives here exactly once; phases read the
        request and never re-derive defaults."""
        continuation = validate_continuation(kwargs.get("continuation"))
        resumed = bool(continuation)
        # Per-request state must never leak across applies on a shared engine
        # (the MCP server keeps one engine for its whole lifetime): a resume
        # pins this request's gate below, a fresh apply must start unpinned.
        continuation_gate = None
        backend = continuation.get("backend", kwargs.get("backend") or "harness")
        # A saved continuation owns its execution mode. A caller cannot turn a
        # failed, gated apply into a read-only preview and thereby bypass the
        # authoritative verification contract. Reject an explicit preview
        # request against a gated state rather than silently changing semantics.
        verify_only = bool(kwargs.get("verify_only"))
        if resumed:
            saved_verify_only = bool(continuation.get("verify_only", False))
            if verify_only and not saved_verify_only:
                raise HarnessError(
                    "a gated continuation cannot be resumed as verify-only; "
                    "the authoritative verification gate must run")
            verify_only = saved_verify_only
        max_lines = continuation.get("max_lines", kwargs.get("max_lines", MAX_FILE_LINES))
        verify_cmd = kwargs.get("verify_cmd")
        if resumed and verify_only:
            # verify-only is intentionally gate-free; never execute a command
            # merely because an older state happened to carry one.
            verify_cmd = None
        elif resumed:
            saved_verify_cmd = continuation.get("verify_cmd")
            if verify_cmd and verify_cmd != saved_verify_cmd:
                raise HarnessError(
                    "continuation verify_cmd does not match its authoritative verification gate")
            # Gate identity was validated by validate_continuation; here the
            # engine only pins WHICH gate this request may execute (#5).
            verify_cmd = saved_verify_cmd
            continuation_gate = saved_verify_cmd if continuation.get("verify_gate_id") else None
        if verify_cmd:
            # Engine-boundary preflight: always shell-tokenizable. PATH
            # existence stays opt-in (require_executable) so hermetic library
            # stubs work; dogfood/CLI already checks PATH before spend.
            validate_verify_command(verify_cmd, require_executable=False)
        if backend not in ("harness", "morph", "diff"):
            raise HarnessError("backend must be 'harness', 'morph', or 'diff'")
        file_path = kwargs.get("file_path")
        if file_path is None:
            file_path = continuation.get("file_path")
        if file_path is None:
            raise HarnessError("apply requires file (or a continuation with file_path)")
        file_path = os.path.abspath(file_path)
        self._enforce_roots(file_path)
        instruction = kwargs.get("instruction") or continuation.get("remaining_scope") or ""
        edit_snippet = kwargs.get("edit_snippet") or continuation.get("edit_snippet")
        if not instruction:
            raise HarnessError("apply requires an instruction")
        task_id = kwargs.get("task_id")
        if task_id is None:
            task_id = continuation.get("task_id") or uuid.uuid4().hex[:8]
        if resumed and kwargs.get("file_path") is not None:
            # No file retarget (C4): a saved continuation's gate and hash
            # were verified against ITS file. Reusing them on a different
            # file would run the wrong gate over the wrong baseline.
            saved_path = continuation.get("file_path")
            if saved_path and os.path.abspath(kwargs["file_path"]) != \
                    os.path.abspath(saved_path):
                try:
                    self.ledger.append("trust_gate", task_id=task_id,
                                       model=kwargs.get("model"),
                                       reason="continuation file retarget refused",
                                       severity="hostile",
                                       combined=None, correctness=None)
                except Exception:
                    pass
                raise HarnessError(
                    "continuation file_path does not match this run's --file; "
                    "resume the saved file or start a fresh apply")
        max_tokens = kwargs.get("max_tokens") or 4096
        task_max_cost = (self.default_task_max_cost
                         if kwargs.get("task_max_cost") is None
                         else kwargs.get("task_max_cost"))
        max_rot = (kwargs.get("max_rotations")
                   if kwargs.get("max_rotations") is not None
                   else self.default_max_rotations)
        reasoning = kwargs.get("reasoning_effort") or self.reasoning_effort
        renew = (self.default_renew_consent
                 if kwargs.get("renew_consent") is None
                 else kwargs.get("renew_consent"))
        task_start_spent = self.governor.spent

        validate_target_file(file_path)
        # The 500-line rewrite ceiling exists because whole-file rewrites scale
        # with file size. Diff mode (#11) only emits touched hunks, so it is
        # exempt -- this is what makes large files editable on the free tier.
        if backend != "diff" and _line_count(file_path) > max_lines:
            raise HarnessError(
                f"file is >{max_lines} lines; out of scope for whole-file rewrite. "
                f"Use backend='diff' (unified diff) for large files.")
        validated = validate_apply_request(
            max_rounds=kwargs.get("max_rounds", MAX_APPLY_ROUNDS),
            max_tokens=kwargs.get("max_tokens") or 4096,
            task_max_cost=task_max_cost,
            max_rotations=max_rot,
            max_lines=max_lines,
            instruction=instruction,
            edit_snippet=edit_snippet,
            reasoning_effort=reasoning,
            backend=backend,
            hard_task_max_cost=0.25)
        max_lines = validated["max_lines"]
        instruction = validated["instruction"]
        edit_snippet = validated["edit_snippet"]
        max_rounds = validated["max_rounds"]
        max_tokens = validated["max_tokens"]
        task_max_cost = validated["task_max_cost"]
        max_rot = validated["max_rotations"]
        reasoning = validated["reasoning_effort"]
        backend = validated["backend"]

        ordered, profiles = (self._route_pool(kwargs.get("apply_pool"))
                             if backend == "harness" else (None, None))
        # Historical live behavior: with an informed capability ordering, the
        # top-ranked known model leads (primary + rotation head); otherwise the
        # configured model stays primary. Unvalidated ids can never displace a
        # known-working model.
        model = (kwargs.get("model")
                 or (MORPH_MODEL if backend == "morph"
                     else (ordered[0] if profiles is not None
                           else self.router.apply_model)))
        want_consent = (self.default_require_consent
                        if kwargs.get("require_consent") is None
                        else kwargs.get("require_consent"))
        require_auth = (self.default_require_diff_authorization
                        if kwargs.get("require_diff_authorization") is None
                        else kwargs.get("require_diff_authorization"))
        attest_model = (kwargs.get("attest_model")
                        or self.default_attest_model
                        or getattr(self.router, "judge", None))

        # ---- trust gates (bipolar -11..+11, hard) ----
        # The model is known and no file has been read yet: deny before
        # any mutation surface is touched. Denials ledger a trust_gate
        # event (the evidence loop) and raise with the score + guidance.
        _report = self.ledger.participation_report()
        try:
            _trust_decision = trust_policy.check_apply(
                ledger=self.ledger,
                report=_report,
                model=model, resumed=resumed, verify_only=verify_only,
                verify_cmd=verify_cmd, task_max_cost=task_max_cost,
                task_id=task_id, hard_task_cap=HARD_TASK_MAX_COST,
                caller=getattr(self.ledger, "caller", None))
        except HarnessError:
            # The primary's trust band denies mutation (a fresh ledger plus
            # a free model scores preview-only). Auto-escalation's front
            # door: the cheapest escalation rung whose OWN trust allows the
            # write becomes the primary -- the lowest paid rung is the
            # default writer when the cheap tier cannot write at all.
            # Disarmed or no qualifying rung: the original deny stands.
            stepped = self._step_primary_for_trust(
                _report, allow_escalation=kwargs.get("allow_escalation"),
                verify_only=verify_only, task_id=task_id)
            if stepped is None:
                raise
            model, _trust_decision = stepped
            # The stepped rung leads; the original ordering rotates behind.
            ordered = [model] + [m_ for m_ in (ordered or []) if m_ != model]

        with open(file_path, encoding="utf-8") as f:
            original = f.read()

        # The file's verification gate runs in the FILE's directory: a gate
        # the plan author wrote for this file's project (`import util`,
        # `python -m unittest test_util`) resolves against the file's own
        # tree, not the server process's CWD (a GUI workDir is not the CWD).
        file_dir = os.path.dirname(file_path) or None

        def _gated_runner(command, timeout=VERIFY_TIMEOUT):
            return self.run_verify(command, timeout=timeout, cwd=file_dir)

        return ApplyRequest(
            task_id=task_id, file_path=file_path, instruction=instruction,
            edit_snippet=edit_snippet, verify_cmd=verify_cmd, backend=backend,
            verify_only=verify_only, max_lines=max_lines,
            max_rounds=max_rounds,
            max_tokens=max_tokens, task_max_cost=task_max_cost, max_rot=max_rot,
            reasoning=reasoning, renew=renew,
            allow_escalation=kwargs.get("allow_escalation"), model=model,
            ordered=ordered, profiles=profiles, want_consent=want_consent,
            original=original, task_start_spent=task_start_spent,
            continuation=continuation,
            continuation_gate=continuation_gate,
            task_runner=(kwargs.get("task_runner") or _gated_runner),
            cancel_check=kwargs.get("cancel_check"),
            trust_combined=_trust_decision["combined"],
            trust_correctness=_trust_decision["correctness"],
            require_diff_authorization=require_auth,
            attest_model=attest_model)

    def apply_batch(self, files, **kwargs):
        """Run the shared multi-file policy over this engine."""
        return run_batch(self, files, **kwargs)

