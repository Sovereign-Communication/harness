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
    chat, extract_content_and_cost, HarnessError, estimate_prompt_tokens, _extract_json, eprint,
)
from .consent import probe_consent, consent_renew

MAX_FILE_LINES = 500
MAX_INSTRUCTION_CHARS = 1000
MAX_SNIPPET_CHARS = 2000
VERIFY_TIMEOUT = 300
VERIFY_FEEDBACK_CHARS = 6000
MAX_APPLY_ROUNDS = 3
CAPABILITY_MARKER = "HARNESS_DEFER:"

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
    otherwise treat the whole response as the file body.
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
    return text


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
                      continuation=None):
        lang = os.path.splitext(file_path)[1].lstrip(".")
        lines = [
            "You are making a single, scoped code change.",
            f"File: {file_path} (language: {lang or 'text'})",
            f"The file is {original.count(chr(10)) + 1} lines. Output the COMPLETE new file "
            f"content -- preserve all unchanged parts exactly.",
            "",
            f"INSTRUCTION: {instruction}",
            f"EDIT SNIPPET (intent anchor): {edit_snippet or 'none'}",
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
                      rounds, history, cost):
        return {
            "status": "deferred",
            "task_id": task_id,
            "category": category,
            "reason": reason,
            "file": file_path,
            "remaining_scope": remaining_scope,
            "rounds": rounds,
            "cost": cost,
            "continuation": {
                "file_path": file_path,
                "task_id": task_id,
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
                   max_rotations=None, continuation=None):
        """Apply a scoped edit with a verification loop, sovereignty gate,
        capability deferral, rotation, and continuation support."""
        continuation = continuation or {}
        if file_path is None:
            file_path = continuation.get("file_path")
        if file_path is None:
            raise HarnessError("apply requires file (or a continuation with file_path)")
        file_path = os.path.abspath(file_path)
        instruction = instruction or continuation.get("remaining_scope") or ""
        if not instruction:
            raise HarnessError("apply requires an instruction")
        if task_id is None:
            task_id = continuation.get("task_id") or uuid.uuid4().hex[:8]
        max_tokens = max_tokens or 4096
        task_max_cost = task_max_cost or self.default_task_max_cost
        max_rot = max_rotations if max_rotations is not None else self.default_max_rotations
        reasoning = reasoning_effort or self.reasoning_effort
        renew = self.default_renew_consent if renew_consent is None else renew_consent

        if not os.path.exists(file_path):
            raise HarnessError(f"file not found: {file_path}")
        if _line_count(file_path) > MAX_FILE_LINES:
            raise HarnessError(
                f"file is >{MAX_FILE_LINES} lines; out of scope. Escalate to a "
                f"multi-file/architecture path instead.")
        if len(instruction) > MAX_INSTRUCTION_CHARS:
            raise HarnessError(f"instruction exceeds {MAX_INSTRUCTION_CHARS} chars.")
        if edit_snippet and len(edit_snippet) > MAX_SNIPPET_CHARS:
            raise HarnessError(f"edit snippet exceeds {MAX_SNIPPET_CHARS} chars.")

        model = model or self.router.apply_model
        want_consent = self.default_require_consent if require_consent is None else require_consent

        with open(file_path, "r", encoding="utf-8") as f:
            original = f.read()

        if want_consent and not continuation:
            consent = probe_consent(
                transport=self.transport, api_key=self.api_key, governor=self.governor,
                task_id=task_id, task=instruction[:1500], model=self.router.judge,
                ledger=self.ledger, required=True)
            if consent["decision"] != "accept":
                return {"status": "consent_blocked", "task_id": task_id, **consent}

        self.ledger.append("dispatch_start", task_id=task_id, model=model,
                           continuation=bool(continuation))
        backup = None
        rounds = list(continuation.get("history") or [])
        history = list(continuation.get("history") or [])
        current_content = original
        failed_models = set()
        rotations = 0
        pp, cp = self.governor.fetch_pricing([model])[model]

        for round_no in range(1, max_rounds + 1):
            # Continued consensus: re-check consent before each round.
            if renew:
                cr = consent_renew(
                    transport=self.transport, api_key=self.api_key, governor=self.governor,
                    task_id=task_id, task=instruction[:1500], model=self.router.judge,
                    ledger=self.ledger, required=True)
                if cr["decision"] != "accept":
                    self.ledger.append("defer_midtask", task_id=task_id, category="consent",
                                       reason=cr["reason"], model=self.router.judge)
                    return self._defer_result(
                        task_id=task_id, file_path=file_path, category="consent",
                        reason=cr["reason"], remaining_scope=instruction,
                        rounds=rounds, history=history, cost=self.governor.spent)

            round_ctx = None
            if rounds:
                last = rounds[-1]
                tail = (last.get("verify_output") or "")[-VERIFY_FEEDBACK_CHARS:]
                round_ctx = (
                    "Your previous attempt was applied but did not pass verification.\n"
                    f"Verification command: {verify_cmd}\n"
                    f"Last {VERIFY_FEEDBACK_CHARS} chars of output:\n```\n{tail}\n```\n\n"
                    "Return the corrected COMPLETE file content.")

            # ---- model rotation on error ----
            candidates = []
            for m_ in ([model] + self.router.apply_pool):
                if m_ not in candidates:
                    candidates.append(m_)
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
            model_used = None
            while attempt_model is not None:
                prompt = self._apply_prompt(file_path, instruction, edit_snippet,
                                            current_content, round_ctx, continuation)
                a_pp, a_cp = self.governor.fetch_pricing([attempt_model])[attempt_model]
                est = estimate_prompt_tokens(prompt)
                per_round = est * a_pp + max_tokens * a_cp
                if per_round * (max_rounds - round_no + 1) > task_max_cost:
                    raise HarnessError(
                        f"task worst-case ${per_round * (max_rounds - round_no + 1):.6f} exceeds "
                        f"--task-max-cost ${task_max_cost:.6f}. Refusing.")
                self.governor.preflight(prompt, [("apply", attempt_model, max_tokens, 0)])
                status, resp = chat(self.transport, self.api_key, attempt_model,
                                    [{"role": "user", "content": prompt}], max_tokens,
                                    reasoning, self.reasoning_token_budget, self.governor)
                if status != 200:
                    err = resp.get("error", {}).get("message", str(resp))
                    failed_models.add(attempt_model)
                    rotations += 1
                    eprint(f"[apply] {attempt_model} FAILED ({status}): {err} -- rotating.")
                else:
                    content, _, cost, is_byok = extract_content_and_cost(resp)
                    if is_byok and not self.governor.is_free(attempt_model):
                        # Paid BYOK route: spend is invisible to the tracked key.
                        self.governor.record_byok(attempt_model)
                        failed_models.add(attempt_model)
                        rotations += 1
                        eprint(f"[apply] {attempt_model} is BYOK-routed (paid); recorded and rotating.")
                    else:
                        model_used = attempt_model
                        break
                if rotations > max_rot:
                    break
                attempt_model = None
                for m_ in candidates:
                    if m_ not in failed_models:
                        attempt_model = m_
                        break

            if model_used is None:
                rounds.append({"round": round_no, "model": model or attempt_model,
                               "status": "api_error",
                               "error": resp.get("error", {}).get("message", str(resp))
                               if resp else "no model reachable",
                               "verify_output": "", "cost": 0.0})
                break

            self.governor.record_actual(cost, model_used)

            # ---- capability-blocker deferral ----
            if CAPABILITY_MARKER in content:
                head, _, tail = content.partition(CAPABILITY_MARKER)
                partial = _extract_file_content(head).strip("\n")
                info = _extract_json(tail)
                remaining = (info or {}).get("remaining_scope") or instruction
                reason = (info or {}).get("reason") or "model reached its capability limit"
                if partial and partial != current_content:
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
                    history=history, cost=self.governor.spent)

            new_content = _extract_file_content(content)
            changed = new_content != current_content

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
                               passed=(rc == 0), model=model_used)
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
        esc = self.router.escalation(override=allow_escalation)
        if esc and rounds and (rounds[-1].get("status") in ("verify_failed", "api_error")):
            self.ledger.append("escalate", task_id=task_id, from_model=model, to_model=esc["model"])
            last = rounds[-1]
            tail = (last.get("verify_output") or "")[-VERIFY_FEEDBACK_CHARS:]
            round_ctx = (
                "A cheaper model exhausted its retry budget without passing verification.\n"
                f"Verification command: {verify_cmd}\n"
                f"Last {VERIFY_FEEDBACK_CHARS} chars:\n```\n{tail}\n```\n\n"
                "Return the corrected COMPLETE file content.")
            prompt = self._apply_prompt(file_path, instruction, edit_snippet,
                                        current_content, round_ctx, continuation)
            self.governor.preflight(prompt, [("escalation", esc["model"], max_tokens, 0)])
            status, resp = chat(self.transport, self.api_key, esc["model"],
                                [{"role": "user", "content": prompt}], max_tokens,
                                "high", self.reasoning_token_budget, self.governor)
            if status == 200:
                content, _, cost, is_byok = extract_content_and_cost(resp)
                if is_byok and not self.governor.is_free(esc["model"]):
                    self.governor.record_byok(esc["model"])
                    content = None  # unusable: spend would be invisible
                self.governor.record_actual(cost, esc["model"])
                new_content = _extract_file_content(content)
                changed = new_content != current_content
                if changed:
                    if backup is None:
                        backup = self._backup(file_path, task_id, "esc")
                    _atomic_write(file_path, new_content)
                    current_content = new_content
                rc, out = self.run_verify(verify_cmd)
                if rc == 0 and changed:
                    self.ledger.append("complete", task_id=task_id, model=esc["model"],
                                       rounds="escalation", status="ok")
                    rounds.append({"round": "escalation", "model": esc["model"], "status": "ok",
                                   "changed": True, "verify_passed": True, "cost": cost,
                                   "verify_output": ""})
                    return {"status": "ok", "task_id": task_id, "changed": True,
                            "backup": backup, "rounds": rounds, "cost": self.governor.spent,
                            "rotations": rotations,
                            "verify": {"command": verify_cmd, "passed": True}, "escalated": True}
                rounds.append({"round": "escalation", "model": esc["model"],
                               "status": "verify_failed", "changed": changed,
                               "verify_passed": False, "cost": cost, "verify_output": out})
            else:
                rounds.append({"round": "escalation", "model": esc["model"], "status": "api_error",
                               "error": resp.get("error", {}).get("message", str(resp)),
                               "verify_output": "", "cost": 0.0})

        self.ledger.append("abort", task_id=task_id, model=model, reason="verify rounds exhausted",
                           rotations=rotations)
        last_out = ""
        for r in reversed(rounds):
            if r.get("verify_output"):
                last_out = r["verify_output"][-VERIFY_FEEDBACK_CHARS:]
                break
        return {"status": "verify_failed", "task_id": task_id, "backup": backup,
                "rounds": rounds, "cost": self.governor.spent, "rotations": rotations,
                "verify": {"command": verify_cmd, "passed": False, "output_tail": last_out},
                "continuation": {
                    "file_path": file_path, "task_id": task_id,
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