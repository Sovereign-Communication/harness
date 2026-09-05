"""Scoped code edits with a verification loop, cost ceiling, and consent.

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
"""
import os
import subprocess
import tempfile
import uuid

from .core import (
    chat, extract_content_and_cost, HarnessError, estimate_prompt_tokens, _extract_json,
    REASONING_FALLBACK_PREFIX, eprint, _reported_cost, _chat_reservation_slots,
)
from .config import MORPH_MODEL
from .consent import probe_consent, consent_renew

MAX_FILE_LINES = 500
MAX_INSTRUCTION_CHARS = 1000
MAX_SNIPPET_CHARS = 2000
VERIFY_TIMEOUT = 300
VERIFY_FEEDBACK_CHARS = 6000
MAX_APPLY_ROUNDS = 3
CAPABILITY_MARKER = "HARNESS_DEFER:"
READY_MARKER = "HARNESS_READY:"


def normalize_continuation(state):
    """Return the nested continuation payload, rejecting malformed state."""
    if state is None:
        return {}
    if not isinstance(state, dict):
        raise HarnessError("continuation must be a JSON object")
    if isinstance(state.get("continuation"), dict) and "file_path" not in state:
        return state["continuation"]
    return state


def validate_continuation(state):
    """Validate the authority boundary before any key or model setup.

    A failed gated apply persists ``verification_required`` so a caller cannot
    turn it into a gate-free preview by changing an option while resuming. A
    deferral that happened before a gate was needed (for example consent or a
    capability handoff with no ``verify_cmd``) remains resumable. Older state
    without this field is treated conservatively and requires its saved gate.
    This helper is shared by the library and CLI public paths.
    """
    state = normalize_continuation(state)
    if not state:
        return state
    verify_only = state.get("verify_only", False)
    if not isinstance(verify_only, bool):
        raise HarnessError("continuation verify_only must be a boolean")
    required = state.get("verification_required")
    if required is None:
        required = not verify_only
    elif not isinstance(required, bool):
        raise HarnessError("continuation verification_required must be a boolean")
    verify_cmd = state.get("verify_cmd")
    if verify_cmd is not None and not isinstance(verify_cmd, str):
        raise HarnessError("continuation verify_cmd must be a string")
    if required:
        if not verify_cmd or not verify_cmd.strip():
            raise HarnessError(
                "continuation is missing its authoritative verify_cmd; "
                "a failed apply cannot be resumed without the original verification gate")
        if verify_only:
            raise HarnessError(
                "a gated continuation cannot be resumed as verify-only; "
                "the authoritative verification gate must run")
    return state


_READY_INSTRUCTION = (
    "Your response MUST begin with exactly one line of the form "
    "'HARNESS_READY: confident' or 'HARNESS_READY: defer'. Declare "
    "'HARNESS_READY: defer' (with a short reason on that same line) if you are "
    "not certain you can complete this change correctly -- do not guess. "
    "Declare 'HARNESS_READY: confident' only if you are sure, then output the "
    "COMPLETE new file content on the lines after that marker.")

_DEFER_INSTRUCTION = (
    "Do your best and assume nothing. You have no file or web access; the file "
    "content below is all you can see. If you reach the limit of your capability "
    "to complete this change correctly, do NOT guess or invent. Stop, keep the "
    "partial work, and emit a single line beginning exactly with 'HARNESS_DEFER:' "
    "followed by a JSON object "
    "{\"remaining_scope\": \"what still needs doing\", \"reason\": \"why you can't continue\"}. "
    "The partial file is preserved and the next model continues from it.")

_CONTINUATION_PREAMBLE = (
    "CONTINUATION of a deferred task. A prior model stopped because: {reason}. "
    "Remaining scope to complete: {scope}. Continue from the current file content.")


def default_run_verify(command, timeout=VERIFY_TIMEOUT):
    result = subprocess.run(command, shell=True, capture_output=True, text=True,
                            timeout=timeout)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _line_count(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


def _extract_file_content(text):
    """Pull the new file body from a model response.

    If the response is a single fenced code block, take its contents;
    otherwise treat the whole response as the file body, stripping any stray
    HARNESS_READY marker line -- protocol text must never land in the file.
    """
    if not text:
        return ""
    lines = text.splitlines(keepends=True)
    start = end = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            if start is None:
                start = i
            else:
                end = i
                break
    if start is not None and end is not None:
        return "".join(lines[start + 1:end])
    # Protocol marker lines must never land in the file, wherever the model
    # put them (with or without a reason after the colon, mid-body, etc.).
    return "".join(
        ln for ln in lines if not ln.lstrip().startswith(READY_MARKER)
    )


def _parse_ready(content):
    """Parse the inline HARNESS_READY verdict.

    The marker is usually the first line, but models regularly emit leading
    blank lines (or place it just before the file body) -- search the first
    few lines so a stray preamble never leaks the protocol marker into the
    written file. Returns (decision, reason, rest) where decision is
    'confident', 'defer', or 'missing' (no marker; treat as confident with a
    warning so the verify gate + DEFER backstop still protect us). rest is the
    content with the marker line removed.
    """
    if not content:
        return "missing", "", content or ""
    lines = content.splitlines(keepends=True)
    for i, ln in enumerate(lines[:5]):
        line = ln.strip()
        if line.startswith(READY_MARKER):
            decision_raw = line[len(READY_MARKER):].strip()
            decision, _, reason = decision_raw.partition(" ")
            decision = decision.strip().lower()
            if decision in ("confident", "defer"):
                rest = "".join(lines[:i]) + "".join(lines[i + 1:])
                return decision, reason.strip(), rest
    return "missing", "", content


def _atomic_write(path, content):
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".harness-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ApplyEngine:
    def __init__(self, transport, api_key, governor, ledger, router,
                 default_require_consent=True, run_verify=None,
                 default_renew_consent=True, reasoning_effort="auto",
                 reasoning_token_budget=0.4, default_max_rotations=3,
                 default_task_max_cost=0.05):
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

    def _apply_prompt(self, file_path, instruction, edit_snippet, original, round_ctx=None,
                      continuation=None, backend="harness"):
        lang = os.path.splitext(file_path)[1].lstrip(".")
        if backend == "morph":
            # Preserve MorphLite's contract while keeping the request inside the
            # governed chat path: the model sees instruction/code/update tags and
            # still gets Harness sovereignty, retry feedback, and cost guards.
            parts = []
            if continuation:
                parts.append(_CONTINUATION_PREAMBLE.format(
                    reason=continuation.get("reason") or "unknown",
                    scope=continuation.get("remaining_scope") or "complete the change"))
            parts.extend((
                f"<instruction>{instruction}</instruction>",
                f"<code>{original}</code>",
                f"<update>{edit_snippet or instruction}</update>",
            ))
            if round_ctx:
                parts.append(round_ctx)
            parts.append(
                "Return ONLY the complete transformed file content. Do not add markdown "
                "or explanation. If you cannot complete the change correctly, return "
                "HARNESS_READY: defer <reason> instead of guessing.")
            return "\n\n".join(parts)
        lines = [
            "You are making a single, scoped code change.",
            "The complete current file content is provided below in this prompt; "
            "you do NOT need (and do not have) filesystem or tool access — work "
            "entirely from the content shown here and reply with text only.",
            f"File: {file_path} (language: {lang or 'text'})",
            f"The file is {original.count(chr(10)) + 1} lines. Output the COMPLETE new file "
            f"content -- preserve all unchanged parts exactly.",
            "",
            f"INSTRUCTION: {instruction}",
            f"EDIT SNIPPET (intent anchor): {edit_snippet or 'none'}",
            "",
            _READY_INSTRUCTION,
            "",
            _DEFER_INSTRUCTION,
            "",
            "Respond with ONLY the file content (optionally wrapped in one fenced code block).",
            "",
            "CURRENT FILE CONTENT:",
            f"```\n{original}\n```",
        ]
        if continuation:
            lines.insert(0, _CONTINUATION_PREAMBLE.format(
                reason=continuation.get("reason") or "unknown",
                scope=continuation.get("remaining_scope") or "complete the change"))
        prompt = "\n".join(lines)
        if round_ctx:
            prompt += "\n\n" + round_ctx
        return prompt

    def _defer_result(self, *, task_id, file_path, category, reason, remaining_scope,
                      rounds, history, cost, backend="harness", verify_only=False,
                      max_lines=MAX_FILE_LINES, edit_snippet=None, verify_cmd=None):
        return {
            "status": "deferred",
            "task_id": task_id,
            "category": category,
            "backend": backend,
            "verify_only": verify_only,
            "reason": reason,
            "file": file_path,
            "remaining_scope": remaining_scope,
            "verify_cmd": verify_cmd,
            "verification_required": bool(verify_cmd) and not verify_only,
            "rounds": rounds,
            "cost": cost,
            "continuation": {
                "file_path": file_path,
                "task_id": task_id,
                "backend": backend,
                "verify_only": verify_only,
                "max_lines": max_lines,
                "edit_snippet": edit_snippet,
                "verify_cmd": verify_cmd,
                "verification_required": bool(verify_cmd) and not verify_only,
                "remaining_scope": remaining_scope,
                "reason": reason,
                "history": history,
            },
        }

    def apply_edit(self, *, task_id=None, file_path=None, instruction=None,
                   edit_snippet=None, verify_cmd=None, max_rounds=MAX_APPLY_ROUNDS,
                   require_consent=None, model=None, max_tokens=None,
                   task_max_cost=None, allow_escalation=None,
                   reasoning_effort=None, renew_consent=None,
                   max_rotations=None, continuation=None, backend="harness",
                   verify_only=False, max_lines=MAX_FILE_LINES):
        """Apply a scoped edit with a verification loop, sovereignty gate,
        capability deferral, rotation, and continuation support."""
        continuation = validate_continuation(continuation)
        resumed = bool(continuation)
        backend = continuation.get("backend", backend)
        # A saved continuation owns its execution mode. A caller cannot turn a
        # failed, gated apply into a read-only preview and thereby bypass the
        # authoritative verification contract. Reject an explicit preview
        # request against a gated state rather than silently changing semantics.
        if resumed:
            saved_verify_only = bool(continuation.get("verify_only", False))
            if verify_only and not saved_verify_only:
                raise HarnessError(
                    "a gated continuation cannot be resumed as verify-only; "
                    "the authoritative verification gate must run")
            verify_only = saved_verify_only
        else:
            verify_only = bool(verify_only)
        max_lines = continuation.get("max_lines", max_lines)
        if resumed and verify_only:
            # verify-only is intentionally gate-free; never execute a command
            # merely because an older state happened to carry one.
            verify_cmd = None
        elif resumed:
            saved_verify_cmd = continuation.get("verify_cmd")
            if verify_cmd and verify_cmd != saved_verify_cmd:
                raise HarnessError(
                    "continuation verify_cmd does not match its authoritative verification gate")
            verify_cmd = saved_verify_cmd
        if backend not in ("harness", "morph"):
            raise HarnessError("backend must be 'harness' or 'morph'")
        try:
            max_lines = int(max_lines)
        except (TypeError, ValueError):
            raise HarnessError("max_lines must be an integer")
        if max_lines < 1 or max_lines > MAX_FILE_LINES:
            raise HarnessError(f"max_lines must be between 1 and {MAX_FILE_LINES}")
        if file_path is None:
            file_path = continuation.get("file_path")
        if file_path is None:
            raise HarnessError("apply requires file (or a continuation with file_path)")
        file_path = os.path.abspath(file_path)
        instruction = instruction or continuation.get("remaining_scope") or ""
        edit_snippet = edit_snippet or continuation.get("edit_snippet")
        if not instruction:
            raise HarnessError("apply requires an instruction")
        if task_id is None:
            task_id = continuation.get("task_id") or uuid.uuid4().hex[:8]
        max_tokens = max_tokens or 4096
        task_max_cost = (self.default_task_max_cost if task_max_cost is None
                         else task_max_cost)
        max_rot = max_rotations if max_rotations is not None else self.default_max_rotations
        reasoning = reasoning_effort or self.reasoning_effort
        renew = self.default_renew_consent if renew_consent is None else renew_consent
        task_start_spent = self.governor.spent

        def record_apply_result(model_id, amount, status, **fields):
            """Record every billable apply attempt, including rotated failures."""
            try:
                amount = float(amount or 0.0)
            except (TypeError, ValueError):
                amount = 0.0
            event_fields = {
                "model": model_id, "task_type": "code", "json_expected": False,
                "json_ok": None, "status": status, "cost": amount,
                "tracked_cost": amount, "backend": backend,
            }
            event_fields.update(fields)
            try:
                self.governor.record_actual(amount, model_id)
            except HarnessError:
                # Preserve an auditable attempted charge without claiming it was
                # tracked. The governor remains authoritative for the hard key
                # ceiling, so the ledger's tracked total still equals spent.
                rejected = dict(event_fields)
                rejected["status"] = "rejected"
                rejected["cost"] = 0.0
                rejected["tracked_cost"] = 0.0
                rejected["reported_cost"] = amount
                self.ledger.append("model_result", task_id=task_id,
                                   event_note="apply", **rejected)
                raise
            self.ledger.append("model_result", task_id=task_id,
                               event_note="apply", **event_fields)
            if self.governor.spent - task_start_spent > task_max_cost:
                raise HarnessError(
                    f"actual task cost would exceed --task-max-cost ${task_max_cost:.6f}; refusing")


        if not os.path.exists(file_path):
            raise HarnessError(f"file not found: {file_path}")
        if _line_count(file_path) > max_lines:
            raise HarnessError(
                f"file is >{max_lines} lines; out of scope. Escalate to a "
                f"multi-file/architecture path instead.")
        if len(instruction) > MAX_INSTRUCTION_CHARS:
            raise HarnessError(f"instruction exceeds {MAX_INSTRUCTION_CHARS} chars.")
        if edit_snippet and len(edit_snippet) > MAX_SNIPPET_CHARS:
            raise HarnessError(f"edit snippet exceeds {MAX_SNIPPET_CHARS} chars.")

        model = model or (MORPH_MODEL if backend == "morph" else self.router.apply_model)
        want_consent = self.default_require_consent if require_consent is None else require_consent

        with open(file_path, "r", encoding="utf-8") as f:
            original = f.read()

        if want_consent and not resumed:
            # The model can only make an honest accept/defer call if it sees the
            # work as the dispatcher will run it: the file it is being asked to
            # change, not a bare instruction stripped of context. State the
            # mechanics explicitly — several models otherwise read 'edit file'
            # as requiring direct filesystem access and decline.
            consent_task = (
                "WORK MECHANICS: you do NOT need any tool or filesystem access. "
                "The file content is shown here in this prompt; you will reply "
                "with the complete new file content as plain text, and the "
                "dispatcher writes it and runs an automated verification gate.\n"
                f"FILE {file_path} (first 60 lines shown):\n"
                f"{original[:2400]}\n"
                f"REQUESTED CHANGE: {instruction[:1200]}")
            consent = probe_consent(
                transport=self.transport, api_key=self.api_key, governor=self.governor,
                task_id=task_id, task=consent_task, model=self.router.judge,
                ledger=self.ledger, required=True,
                fallback_pool=self.router.panel_pool)
            if self.governor.spent - task_start_spent > task_max_cost:
                raise HarnessError(
                    f"consent cost exceeded task ceiling ${task_max_cost:.6f}; refusing to dispatch")
            if consent["decision"] != "accept":
                return {"status": "consent_blocked", "task_id": task_id, **consent}

        self.ledger.append("dispatch_start", task_id=task_id, model=model,
                           continuation=bool(continuation))
        backup = None
        rounds = list(continuation.get("history") or [])
        history = list(continuation.get("history") or [])
        current_content = original
        failed_models = set()
        deferred_models = {}
        rotations = 0
        for round_no in range(1, max_rounds + 1):
            # Continued consensus: re-check consent before each round.
            if renew:
                # Skip re-asking models already shown unable to answer the
                # consent probe this run (e.g. reasoning-only emitters): the
                # primary just fails again and the rotation ladder absorbs it.
                consent_unusable = {
                    a["model"] for a in (consent.get("attempts") or [])
                    if a.get("status") == "error"}
                renew_pool = [m_ for m_ in self.router.panel_pool
                              if m_ not in consent_unusable]
                renew_task = (
                    "WORK MECHANICS: you do NOT need any tool or filesystem "
                    "access. The current file content is shown here in this "
                    "prompt; you will reply with the complete new file content "
                    "as plain text, and the dispatcher writes it and runs the "
                    "verification gate.\n"
                    f"FILE {file_path} (first 60 lines shown):\n"
                    f"{current_content[:2400]}\n"
                    f"REQUESTED CHANGE: {instruction[:1200]}")
                cr = consent_renew(
                    transport=self.transport, api_key=self.api_key, governor=self.governor,
                    task_id=task_id, task=renew_task, model=self.router.judge,
                    ledger=self.ledger, required=True,
                    fallback_pool=renew_pool)
                if self.governor.spent - task_start_spent > task_max_cost:
                    raise HarnessError(
                        f"consent renewal exceeded task ceiling ${task_max_cost:.6f}; refusing to continue")
                if cr["decision"] != "accept":
                    self.ledger.append("defer_midtask", task_id=task_id, category="consent",
                                       reason=cr["reason"], model=self.router.judge)
                    return self._defer_result(
                        task_id=task_id, file_path=file_path, category="consent",
                        reason=cr["reason"], remaining_scope=instruction,
                        rounds=rounds, history=history, cost=self.governor.spent,
                        backend=backend, verify_only=verify_only, max_lines=max_lines,
                        edit_snippet=edit_snippet, verify_cmd=verify_cmd)

            round_ctx = None
            gate_broken = False
            if rounds:
                last = rounds[-1]
                tail = (last.get("verify_output") or "")[-VERIFY_FEEDBACK_CHARS:]
                # A gate that fails with the SAME output on consecutive rounds
                # is broken (bad path, missing dependency, wrong interpreter),
                # not something the model can fix by rewording code. Stop
                # instead of burning the remaining rounds on it.
                prev_outputs = [r.get("verify_output") or "" for r in rounds]
                if len(prev_outputs) >= 2 and prev_outputs[-1] == prev_outputs[-2]:
                    rounds.append({"round": round_no, "model": model or "(none)",
                                   "status": "gate_broken",
                                   "reason": "verification gate failed identically "
                                             "on consecutive rounds; the gate itself "
                                             "appears broken, not the edit",
                                   "verify_output": tail, "cost": 0.0})
                    history.append({"round": round_no, "status": "gate_broken"})
                    gate_broken = True
                else:
                    round_ctx = (
                        "Your previous attempt was applied but did not pass verification.\n"
                        f"Verification command: {verify_cmd}\n"
                        f"Last {VERIFY_FEEDBACK_CHARS} chars of output:\n```\n{tail}\n```\n\n"
                        "Return the corrected COMPLETE file content.")
            if gate_broken:
                break

            # ---- model rotation on error ----
            candidates = []
            for m_ in ([model] + self.router.apply_pool):
                if m_ not in candidates:
                    candidates.append(m_)
            # Pools are live-validated at the public routing boundary, but a
            # library caller can still supply stale ids. Exclude those ids from
            # the reservation and rotation sequence rather than discovering the
            # problem only after a failed model has already consumed a call.
            try:
                known_models = {entry.get("id") for entry in self.governor.fetch_models()}
                candidates = [m_ for m_ in candidates if m_ in known_models]
            except HarnessError:
                pass
            attempt_model = None
            for m_ in candidates:
                if m_ not in failed_models:
                    attempt_model = m_
                    break
            if attempt_model is None:
                raise HarnessError("no available apply model (pool exhausted).")

            status = None
            resp = None
            content = None
            cost = 0.0
            ready = "missing"
            last_defer_reason = None
            model_used = None
            while attempt_model is not None:
                prompt = self._apply_prompt(file_path, instruction, edit_snippet,
                                            current_content, round_ctx, continuation,
                                            backend=backend)
                a_pp, a_cp = self.governor.fetch_pricing([attempt_model])[attempt_model]
                est = estimate_prompt_tokens(prompt)
                slots = _chat_reservation_slots(attempt_model, reasoning, 0)
                per_call_estimate = slots * (est * a_pp + max_tokens * a_cp)
                if self.governor.spent - task_start_spent + per_call_estimate > task_max_cost:
                    raise HarnessError(
                        f"task worst-case ${self.governor.spent - task_start_spent + per_call_estimate:.6f} "
                        f"exceeds --task-max-cost ${task_max_cost:.6f}. Refusing.")
                # This reservation is made immediately before every candidate,
                # including dynamically rotated models and reasoning fallbacks.
                # The actual-cost guard below remains authoritative if provider
                # billing exceeds the live pricing estimate.
                self.governor.preflight(
                    prompt,
                    [(f"apply attempt {i + 1}/{slots}", attempt_model, max_tokens, 0)
                     for i in range(slots)],
                )
                status, resp = chat(self.transport, self.api_key, attempt_model,
                                    [{"role": "user", "content": prompt}], max_tokens,
                                    reasoning, self.reasoning_token_budget, self.governor)
                if status != 200:
                    err = (resp.get("error", {}).get("message", str(resp))
                           if isinstance(resp, dict) else str(resp))
                    attempt_cost = _reported_cost(resp)
                    record_apply_result(attempt_model, attempt_cost, "error",
                                        error=err, http_status=status,
                                        retryable=status == 429)
                    failed_models.add(attempt_model)
                    rotations += 1
                    eprint(f"[apply] {attempt_model} FAILED ({status}): {err} -- rotating.")
                else:
                    content, _, cost, is_byok = extract_content_and_cost(resp)
                    if is_byok and not self.governor.is_free(attempt_model):
                        # Paid BYOK route: spend is invisible to the tracked key.
                        self.governor.record_byok(attempt_model)
                        self.ledger.append(
                            "model_result", task_id=task_id, event_note="apply",
                            model=attempt_model, task_type="code", json_expected=False,
                            json_ok=None, status="error", cost=0.0, tracked_cost=0.0,
                            reported_cost=cost, backend=backend, reason="paid BYOK route")
                        failed_models.add(attempt_model)
                        rotations += 1
                        eprint(f"[apply] {attempt_model} is BYOK-routed (paid); recorded and rotating.")
                    elif not content or content.startswith(REASONING_FALLBACK_PREFIX):
                        # No usable output: a reasoning-only response must NOT be
                        # treated as file content (it would corrupt the target).
                        record_apply_result(attempt_model, cost, "error",
                                            reason="no usable content")
                        failed_models.add(attempt_model)
                        rotations += 1
                        eprint(f"[apply] {attempt_model} returned no content (reasoning-only); rotating.")
                    else:
                        ready, ready_reason, content = _parse_ready(content)
                        if ready == "defer":
                            record_apply_result(attempt_model, cost, "deferred",
                                                readiness="defer",
                                                reason=ready_reason or "model declared not ready")
                            self.ledger.append("readiness", task_id=task_id,
                                               model=attempt_model, round=round_no,
                                               decision="defer")
                            # The model declines on capability grounds; rotate to the
                            # next pool model before accepting the deferral.
                            deferred_models[attempt_model] = ready_reason
                            last_defer_reason = ready_reason or "model declared not ready"
                            rotations += 1
                            eprint(f"[apply] {attempt_model} declares HARNESS_READY: defer "
                                   f"({(ready_reason or '')[:70]}) -- rotating.")
                        else:
                            record_apply_result(attempt_model, cost, "ok", readiness=ready)
                            if ready == "missing":
                                eprint(f"[apply] {attempt_model} did not emit HARNESS_READY; "
                                       f"treating as confident (verify + DEFER still guard).")
                            else:
                                self.ledger.append("readiness", task_id=task_id,
                                                   model=attempt_model, round=round_no,
                                                   decision="confident")
                            model_used = attempt_model
                            break
                if rotations > max_rot:
                    break
                attempt_model = None
                for m_ in candidates:
                    if m_ not in failed_models and m_ not in deferred_models:
                        attempt_model = m_
                        break

            if model_used is None:
                if last_defer_reason is not None:
                    # Every reachable model declined; accept the deferral.
                    self.ledger.append("defer_midtask", task_id=task_id, category="readiness",
                                       reason=last_defer_reason, model=model or attempt_model)
                    rounds.append({"round": round_no, "model": model or attempt_model,
                                   "status": "deferred", "reason": last_defer_reason,
                                   "cost": 0.0, "verify_output": ""})
                    history.append({"round": round_no, "model": model or attempt_model,
                                    "status": "deferred", "reason": last_defer_reason})
                    return self._defer_result(
                        task_id=task_id, file_path=file_path, category="readiness",
                        reason=last_defer_reason, remaining_scope=instruction,
                        rounds=rounds, history=history, cost=self.governor.spent,
                        backend=backend, verify_only=verify_only, max_lines=max_lines,
                        edit_snippet=edit_snippet, verify_cmd=verify_cmd)
                rounds.append({"round": round_no, "model": model or attempt_model,
                               "status": "api_error",
                               "error": resp.get("error", {}).get("message", str(resp))
                               if resp else "no model reachable",
                               "verify_output": "",
                               "cost": _reported_cost(resp)})
                break

            # The chosen response was already recorded by record_apply_result;
            # do not charge or ledger it a second time here.

            # ---- capability-blocker deferral ----
            if CAPABILITY_MARKER in content:
                head, _, tail = content.partition(CAPABILITY_MARKER)
                partial = _extract_file_content(head).strip("\n")
                info = _extract_json(tail)
                # Models often defer in prose rather than strict JSON; keep the
                # model's own words when we can't parse structured JSON.
                prose = " ".join(tail.strip().split())[:200] if tail.strip() else ""
                reason = (info or {}).get("reason") or prose or "model reached its capability limit"
                remaining = (info or {}).get("remaining_scope") or prose or instruction
                if partial and partial != current_content and not verify_only:
                    if backup is None:
                        backup = self._backup(file_path, task_id, f"r{round_no}-defer")
                    _atomic_write(file_path, partial + "\n")
                self.ledger.append("defer_midtask", task_id=task_id, category="capability",
                                   reason=reason, model=model_used)
                rounds.append({"round": round_no, "model": model_used, "status": "deferred",
                               "reason": reason, "cost": cost, "verify_output": ""})
                history.append({"round": round_no, "model": model_used, "status": "deferred",
                                "reason": reason})
                return self._defer_result(
                    task_id=task_id, file_path=file_path, category="capability",
                    reason=reason, remaining_scope=remaining, rounds=rounds,
                    history=history, cost=self.governor.spent,
                    backend=backend, verify_only=verify_only, max_lines=max_lines,
                    edit_snippet=edit_snippet, verify_cmd=verify_cmd)


            new_content = _extract_file_content(content)
            changed = new_content != current_content

            if verify_only:
                # MorphLite's --verify-only contract: return the proposed content
                # without mutating the target or running a gate against old code.
                rounds.append({"round": round_no, "model": model_used, "status": "preview",
                               "changed": changed, "verify_passed": None, "cost": cost})
                history.append({"round": round_no, "model": model_used, "status": "preview"})
                self.ledger.append("complete", task_id=task_id, model=model_used,
                                   rounds=round_no, status="preview", backend=backend,
                                   note="verify-only; proposal not written")
                return {"status": "preview", "task_id": task_id, "changed": changed,
                        "proposed_content": new_content, "backup": None,
                        "rounds": rounds, "cost": self.governor.spent,
                        "rotations": rotations, "verify_only": True,
                        "backend": backend}

            if backup is None and changed:
                backup = self._backup(file_path, task_id, round_no)
            if changed:
                _atomic_write(file_path, new_content)
                current_content = new_content

            if not verify_cmd:
                rounds.append({"round": round_no, "model": model_used, "status": "ok",
                               "changed": changed, "verify_passed": None, "cost": cost})
                history.append({"round": round_no, "model": model_used, "status": "ok"})
                self.ledger.append("complete", task_id=task_id, model=model_used,
                                   rounds=round_no, status="ok", note="no verification gate",
                                   rotations=rotations)
                return {"status": "ok", "task_id": task_id, "changed": changed,
                        "backup": backup, "rounds": rounds, "cost": self.governor.spent,
                        "rotations": rotations,
                        "note": "no verification gate supplied"}

            rc, out = self.run_verify(verify_cmd)
            self.ledger.append("verify_round", task_id=task_id, round=round_no,
                               passed=(rc == 0), model=model_used, readiness=ready)
            if rc == 0:
                if not changed:
                    rounds.append({"round": round_no, "model": model_used, "status": "vacuous",
                                   "changed": False, "verify_passed": True, "cost": cost,
                                   "verify_output": "verify passed but no changes were applied"})
                    continue
                rounds.append({"round": round_no, "model": model_used, "status": "ok",
                               "changed": True, "verify_passed": True, "cost": cost,
                               "verify_output": ""})
                history.append({"round": round_no, "model": model_used, "status": "ok"})
                self.ledger.append("complete", task_id=task_id, model=model_used,
                                   rounds=round_no, status="ok", rotations=rotations)
                return {"status": "ok", "task_id": task_id, "changed": True,
                        "backup": backup, "rounds": rounds, "cost": self.governor.spent,
                        "rotations": rotations,
                        "verify": {"command": verify_cmd, "passed": True}}
            rounds.append({"round": round_no, "model": model_used, "status": "verify_failed",
                           "changed": changed, "verify_passed": False, "cost": cost,
                           "verify_output": out})
            history.append({"round": round_no, "model": model_used, "status": "verify_failed"})

        # Cheap model exhausted its retry budget -> optional gated escalation.
        # A proven-broken gate would fail the escalation identically, so skip it.
        esc = self.router.escalation(override=allow_escalation)
        if (not verify_only and verify_cmd and not gate_broken and esc and rounds and
                rounds[-1].get("status") in ("verify_failed", "api_error")):
            self.ledger.append("escalate", task_id=task_id, from_model=model, to_model=esc["model"])
            last = rounds[-1]
            tail = (last.get("verify_output") or "")[-VERIFY_FEEDBACK_CHARS:]
            round_ctx = (
                "A cheaper model exhausted its retry budget without passing verification.\n"
                f"Verification command: {verify_cmd}\n"
                f"Last {VERIFY_FEEDBACK_CHARS} chars:\n```\n{tail}\n```\n\n"
                "Return the corrected COMPLETE file content.")
            prompt = self._apply_prompt(file_path, instruction, edit_snippet,
                                        current_content, round_ctx, continuation,
                                        backend=backend)
            esc_slots = _chat_reservation_slots(esc["model"], "high", 0)
            self.governor.preflight(
                prompt,
                [(f"escalation attempt {i + 1}/{esc_slots}", esc["model"], max_tokens, 0)
                 for i in range(esc_slots)],
            )
            status, resp = chat(self.transport, self.api_key, esc["model"],
                                [{"role": "user", "content": prompt}], max_tokens,
                                "high", self.reasoning_token_budget, self.governor)
            if status != 200:
                err = (resp.get("error", {}).get("message", str(resp))
                       if isinstance(resp, dict) else str(resp))
                record_apply_result(esc["model"], _reported_cost(resp), "error",
                                    escalation=True, error=err, http_status=status)
                rounds.append({"round": "escalation", "model": esc["model"],
                               "status": "api_error", "error": err,
                               "verify_output": "", "cost": _reported_cost(resp)})
            else:
                content, _, cost, is_byok = extract_content_and_cost(resp)
                if is_byok and not self.governor.is_free(esc["model"]):
                    self.governor.record_byok(esc["model"])
                    self.ledger.append(
                        "model_result", task_id=task_id, event_note="escalation",
                        model=esc["model"], task_type="code", json_expected=False,
                        json_ok=None, status="error", cost=0.0, tracked_cost=0.0,
                        reported_cost=cost, backend=backend,
                        reason="paid BYOK route")
                    content = None
                    cost = 0.0
                else:
                    usable = bool(content and not content.startswith(REASONING_FALLBACK_PREFIX))
                    record_apply_result(esc["model"], cost,
                                        "ok" if usable else "error",
                                        escalation=True)
                    if not usable:
                        content = None
                new_content = _extract_file_content(content) if content else current_content
                changed = bool(content) and new_content != current_content
                if changed:
                    if backup is None:
                        backup = self._backup(file_path, task_id, "esc")
                    _atomic_write(file_path, new_content)
                    current_content = new_content
                rc, out = (self.run_verify(verify_cmd) if content
                           else (1, "escalation returned no usable content"))
                if rc == 0 and changed:
                    self.ledger.append("complete", task_id=task_id, model=esc["model"],
                                       rounds="escalation", status="ok")
                    rounds.append({"round": "escalation", "model": esc["model"],
                                   "status": "ok", "changed": True,
                                   "verify_passed": True, "cost": cost,
                                   "verify_output": ""})
                    return {"status": "ok", "task_id": task_id, "changed": True,
                            "backup": backup, "rounds": rounds,
                            "cost": self.governor.spent, "rotations": rotations,
                            "verify": {"command": verify_cmd, "passed": True},
                            "escalated": True}
                rounds.append({"round": "escalation", "model": esc["model"],
                               "status": "verify_failed", "changed": changed,
                               "verify_passed": False, "cost": cost,
                               "verify_output": out})

        self.ledger.append("abort", task_id=task_id, model=model,
                           reason="verify rounds exhausted", rotations=rotations)

        last_out = ""
        for r in reversed(rounds):
            if r.get("verify_output"):
                last_out = r["verify_output"][-VERIFY_FEEDBACK_CHARS:]
                break
        return {"status": "verify_failed", "task_id": task_id, "backup": backup,
                "rounds": rounds, "cost": self.governor.spent, "rotations": rotations,
                "backend": backend, "verify_only": verify_only, "verify_cmd": verify_cmd,
                "verify": {"command": verify_cmd, "passed": False, "output_tail": last_out},
                "continuation": {
                    "file_path": file_path, "task_id": task_id,
                    "backend": backend, "verify_only": verify_only, "max_lines": max_lines,
                    "edit_snippet": edit_snippet, "verify_cmd": verify_cmd,
                    "verification_required": True,
                    "remaining_scope": f"Fix the verification failures for: {instruction}",
                    "reason": "verification did not pass on the free tier; continue and fix",
                    "history": history,
                }}

    def _backup(self, file_path, task_id, round_no):
        d = os.path.join(os.path.dirname(file_path), ".harness-backups")
        try:
            os.makedirs(d, exist_ok=True)
            dest = os.path.join(d, f"{task_id}-r{round_no}-{os.path.basename(file_path)}")
            with open(file_path, "r", encoding="utf-8") as src, \
                    open(dest, "w", encoding="utf-8") as out:
                out.write(src.read())
            return dest
        except OSError:
            return None  # backup failure is non-fatal but not reported silently