"""Scoped code edits with a verification loop, cost ceiling, and consent.

Ports the good parts of SCMessenger's morph_lite.py (single-file <500-line,
hard cost ceiling, verify-only dry-run) and delegate_task.py (apply -> run a
verification gate -> feed the failure output back -> retry up to N rounds,
with a guard against vacuous success where verify passes but nothing changed).
Adds the sovereignty gate (consent probe) and per-task worst-case budgeting.
"""
import os
import subprocess
import tempfile

from .core import chat, extract_content_and_cost, HarnessError, estimate_prompt_tokens
from .consent import probe_consent

MAX_FILE_LINES = 500
MAX_INSTRUCTION_CHARS = 1000
MAX_SNIPPET_CHARS = 2000
VERIFY_TIMEOUT = 300
VERIFY_FEEDBACK_CHARS = 6000
MAX_APPLY_ROUNDS = 3


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
                 default_require_consent=True, run_verify=None):
        self.transport = transport
        self.api_key = api_key
        self.governor = governor
        self.ledger = ledger
        self.router = router
        self.default_require_consent = default_require_consent
        self.run_verify = run_verify or default_run_verify

    def _apply_prompt(self, file_path, instruction, edit_snippet, original, round_ctx=None):
        lang = os.path.splitext(file_path)[1].lstrip(".")
        prompt = (
            f"You are making a single, scoped code change.\n"
            f"File: {file_path} (language: {lang or 'text'})\n"
            f"The file is {original.count(chr(10)) + 1} lines. Output the COMPLETE new file "
            f"content -- preserve all unchanged parts exactly.\n\n"
            f"INSTRUCTION: {instruction}\n"
            f"EDIT SNIPPET (intent anchor): {edit_snippet or 'none'}\n\n"
            f"Respond with ONLY the file content (optionally wrapped in one fenced code block).\n\n"
            f"CURRENT FILE CONTENT:\n```\n{original}\n```")
        if round_ctx:
            prompt += "\n\n" + round_ctx
        return prompt

    def apply_edit(self, *, task_id, file_path, instruction, edit_snippet=None,
                   verify_cmd=None, max_rounds=MAX_APPLY_ROUNDS, require_consent=None,
                   model=None, max_tokens=4096, task_max_cost=0.05,
                   allow_escalation=None, apply_prompt=None):
        """Apply a scoped edit with a verification loop and sovereignty gate."""
        file_path = os.path.abspath(file_path)
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

        if want_consent:
            consent = probe_consent(
                transport=self.transport, api_key=self.api_key, governor=self.governor,
                task_id=task_id, task=instruction[:1500], model=self.router.judge,
                ledger=self.ledger, required=True)
            if consent["decision"] != "accept":
                return {"status": "consent_blocked", "task_id": task_id, **consent}

        self.ledger.append("dispatch_start", task_id=task_id, model=model)
        backup = None
        rounds = []
        current_content = original

        pp, cp = self.governor.fetch_pricing([model])[model]

        for round_no in range(1, max_rounds + 1):
            round_ctx = None
            if rounds:
                last = rounds[-1]
                tail = (last.get("verify_output") or "")[-VERIFY_FEEDBACK_CHARS:]
                round_ctx = (
                    "Your previous attempt was applied but did not pass verification.\n"
                    f"Verification command: {verify_cmd}\n"
                    f"Last {VERIFY_FEEDBACK_CHARS} chars of output:\n```\n{tail}\n```\n\n"
                    "Return the corrected COMPLETE file content.")
            prompt = apply_prompt or self._apply_prompt(
                file_path, instruction, edit_snippet, current_content, round_ctx)

            # Per-task worst-case across the remaining retry budget (true ceiling,
            # computed before any network call this round).
            est = estimate_prompt_tokens(prompt)
            per_round = est * pp + max_tokens * cp
            if per_round * (max_rounds - round_no + 1) > task_max_cost:
                raise HarnessError(
                    f"task worst-case ${per_round * (max_rounds - round_no + 1):.6f} exceeds "
                    f"--task-max-cost ${task_max_cost:.6f}. Refusing.")
            self.governor.preflight(prompt, [("apply", model, max_tokens, 0)])

            status, resp = chat(self.transport, self.api_key, model,
                                [{"role": "user", "content": prompt}], max_tokens,
                                reasoning_effort="low", governor=self.governor)
            if status != 200:
                err = resp.get("error", {}).get("message", str(resp))
                rounds.append({"round": round_no, "model": model, "status": "api_error",
                               "error": err, "verify_output": "", "cost": 0.0})
                continue
            content, _, cost, is_byok = extract_content_and_cost(resp)
            if is_byok:
                raise HarnessError(f"{model} came back is_byok=true.")
            self.governor.record_actual(cost, model)
            new_content = _extract_file_content(content)
            changed = new_content != current_content

            if backup is None and changed:
                backup = self._backup(file_path, task_id, round_no)
            if changed:
                _atomic_write(file_path, new_content)
                current_content = new_content

            if not verify_cmd:
                rounds.append({"round": round_no, "model": model, "status": "ok",
                               "changed": changed, "verify_passed": None, "cost": cost})
                self.ledger.append("complete", task_id=task_id, model=model, rounds=round_no,
                                   status="ok", note="no verification gate")
                return {"status": "ok", "task_id": task_id, "changed": changed,
                        "backup": backup, "rounds": rounds, "cost": self.governor.spent,
                        "note": "no verification gate supplied"}

            rc, out = self.run_verify(verify_cmd)
            self.ledger.append("verify_round", task_id=task_id, round=round_no,
                               passed=(rc == 0), model=model)
            if rc == 0:
                if not changed:
                    # Vacuous success: verify passed but nothing changed. Do NOT accept.
                    rounds.append({"round": round_no, "model": model, "status": "vacuous",
                                   "changed": False, "verify_passed": True, "cost": cost,
                                   "verify_output": "verify passed but no changes were applied"})
                    continue
                rounds.append({"round": round_no, "model": model, "status": "ok",
                               "changed": True, "verify_passed": True, "cost": cost,
                               "verify_output": ""})
                self.ledger.append("complete", task_id=task_id, model=model, rounds=round_no,
                                   status="ok")
                return {"status": "ok", "task_id": task_id, "changed": True,
                        "backup": backup, "rounds": rounds, "cost": self.governor.spent,
                        "verify": {"command": verify_cmd, "passed": True}}
            rounds.append({"round": round_no, "model": model, "status": "verify_failed",
                           "changed": changed, "verify_passed": False, "cost": cost,
                           "verify_output": out})

        # Cheap model exhausted its retry budget -> optional gated escalation.
        esc = self.router.escalation(override=allow_escalation)
        if esc and rounds and rounds[-1].get("changed"):
            self.ledger.append("escalate", task_id=task_id, from_model=model, to_model=esc["model"])
            last = rounds[-1]
            tail = (last.get("verify_output") or "")[-VERIFY_FEEDBACK_CHARS:]
            round_ctx = (
                "A cheaper model exhausted its retry budget without passing verification.\n"
                f"Verification command: {verify_cmd}\n"
                f"Last {VERIFY_FEEDBACK_CHARS} chars:\n```\n{tail}\n```\n\n"
                "Return the corrected COMPLETE file content.")
            prompt = apply_prompt or self._apply_prompt(
                file_path, instruction, edit_snippet, current_content, round_ctx)
            self.governor.preflight(prompt, [("escalation", esc["model"], max_tokens, 0)])
            status, resp = chat(self.transport, self.api_key, esc["model"],
                                [{"role": "user", "content": prompt}], max_tokens,
                                reasoning_effort="high", governor=self.governor)
            if status == 200:
                content, _, cost, is_byok = extract_content_and_cost(resp)
                if is_byok:
                    raise HarnessError(f"{esc['model']} came back is_byok=true.")
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
                            "verify": {"command": verify_cmd, "passed": True}, "escalated": True}
                rounds.append({"round": "escalation", "model": esc["model"],
                               "status": "verify_failed", "changed": changed,
                               "verify_passed": False, "cost": cost, "verify_output": out})
            else:
                rounds.append({"round": "escalation", "model": esc["model"], "status": "api_error",
                               "error": resp.get("error", {}).get("message", str(resp)),
                               "verify_output": "", "cost": 0.0})

        self.ledger.append("abort", task_id=task_id, model=model, reason="verify rounds exhausted")
        last_out = ""
        for r in reversed(rounds):
            if r.get("verify_output"):
                last_out = r["verify_output"][-VERIFY_FEEDBACK_CHARS:]
                break
        return {"status": "verify_failed", "task_id": task_id, "backup": backup,
                "rounds": rounds, "cost": self.governor.spent,
                "verify": {"command": verify_cmd, "passed": False, "output_tail": last_out}}

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