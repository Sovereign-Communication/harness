"""Core engine: panel+judge verification with an enforced spend governor.

A faithful port of SCMessenger's scripts/fusion_lite.py philosophy: a
hand-rolled replacement for OpenRouter's "Fusion", using plain chat
completions with NO tools key, so worst-case cost is a true, precomputable
ceiling. Six hard refusals are preserved:

  1. No 'tools' key in any payload.
  2. Preflight worst-case cost computed before any network call.
  3. BYOK org-prefix denylist (incl. the anthropic/ P0 block).
  4. Key must have a finite spend limit.
  5. Mid-batch fail-closed on actual cumulative spend.
  6. Key-identity label check (--expect-key-label).
"""
import sys
import time

from .config import (
    OPENROUTER_CHAT_URL, OPENROUTER_KEY_URL, OPENROUTER_MODELS_URL,
    BYOK_DENYLIST_PREFIXES, DEFAULT_MAX_COST, DEFAULT_MAX_TOKENS,
)
from ._http import Transport  # noqa: F401  (documenting the transport seam)


class HarnessError(Exception):
    """A fatal refusal or failure. Message is user-presentable."""


def eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)


def estimate_prompt_tokens(text):
    return int(len(text.split()) * 1.5) + 50


def extract_content_and_cost(resp):
    """Pull content, finish_reason, actual cost, is_byok from a completion.

    Falls back to the reasoning trace when a model emitted reasoning but no
    visible content — reasoning models otherwise "succeed" with empty output
    and a paid call is discarded with nothing to show for it.
    """
    try:
        message = resp["choices"][0]["message"]
        content = message.get("content")
        if not (content or "").strip():
            reasoning = message.get("reasoning")
            if (reasoning or "").strip():
                content = ("[NOTE] model returned no content; showing reasoning trace instead.\n\n"
                           + reasoning)
        finish_reason = resp["choices"][0].get("finish_reason", "unknown")
        cost = resp.get("usage", {}).get("cost", 0.0)
        is_byok = resp.get("usage", {}).get("is_byok", False)
        return content, finish_reason, cost, is_byok
    except (KeyError, IndexError, TypeError):
        return None, None, 0.0, False


class SpendGovernor:
    """Enforces the guarantees that make sub-cent runs a *guarantee*, not a hope."""

    def __init__(self, transport, api_key, expect_key_label=None,
                 max_cost=DEFAULT_MAX_COST):
        self.transport = transport
        self.api_key = api_key
        self.expect_key_label = expect_key_label
        self.max_cost = max_cost
        self.spent = 0.0
        self.key_info = None
        self._pricing_cache = {}

    # 4 + 6
    def verify_key(self):
        try:
            info = self.transport.get(OPENROUTER_KEY_URL, self.api_key)
        except Exception as e:
            raise HarnessError(f"could not verify key limit: {e}")
        data = info.get("data", {})
        limit = data.get("limit")
        label = data.get("label", "<no label>")
        if limit is None:
            raise HarnessError(
                f"key '{label}' has NO spend limit configured. Refusing to run.")
        remaining = data.get("limit_remaining", 0)
        eprint(f"[OK] using key '{label}', limit=${limit}, remaining=${remaining:.6f} "
               f"(resets: {data.get('limit_reset')})")
        if self.expect_key_label is not None and self.expect_key_label not in label:
            raise HarnessError(
                f"expected key label containing '{self.expect_key_label}' but this key's "
                f"actual label is '{label}'. Refusing to run -- fix the key source, or drop "
                f"--expect-key-label if this key is actually correct.")
        self.key_info = {
            "label": label, "limit": limit, "remaining": remaining,
            "limit_reset": data.get("limit_reset"),
        }
        return self.key_info

    def key_status(self):
        if not self.key_info:
            self.verify_key()
        return dict(self.key_info, session_spent=self.spent)

    # 3
    def check_byok(self, model_id):
        for prefix in BYOK_DENYLIST_PREFIXES:
            if model_id.startswith(prefix):
                raise HarnessError(
                    f"model '{model_id}' matches BYOK denylist prefix '{prefix}'. Refusing.")

    # 1
    def assert_no_tools(self, payload, label):
        if "tools" in payload:
            raise HarnessError(
                f"payload for {label} contains a 'tools' key -- refusing to send.")

    # 2
    def fetch_pricing(self, model_ids):
        missing = [m_ for m_ in model_ids if m_ not in self._pricing_cache]
        if missing:
            try:
                models = self.transport.get(OPENROUTER_MODELS_URL, self.api_key, timeout=20)
            except Exception as e:
                raise HarnessError(f"could not fetch model pricing: {e}")
            by_id = {m_["id"]: m_.get("pricing", {}) for m_ in models.get("data", [])}
            for mid in missing:
                if mid not in by_id:
                    raise HarnessError(f"model '{mid}' not found in live OpenRouter model list.")
                p = by_id[mid]
                try:
                    # OpenRouter pricing fields are already PER-TOKEN dollar prices
                    # (e.g. "0.00000001" = $0.01/M). Do NOT divide by 1e6 again —
                    # an earlier version did and undercounted cost ~1,000,000x.
                    self._pricing_cache[mid] = (
                        float(p.get("prompt", "0")), float(p.get("completion", "0")))
                except (TypeError, ValueError):
                    raise HarnessError(f"could not parse pricing for '{mid}': {p}")
        return {m_: self._pricing_cache[m_] for m_ in model_ids}

    def preflight(self, prompt_text, calls):
        """calls = [(label, model, max_tokens, extra_input)] -> (total, breakdown).

        Worst-case: every call maxes its max_tokens. extra_input accounts for
        tokens a later call consumes beyond the base prompt (e.g. the judge
        reading panel outputs). Returns the true ceiling, checked against
        self.max_cost before any network call.
        """
        models = [m_ for _, m_, _, _ in calls]
        pricing = self.fetch_pricing(models)
        prompt_tokens = estimate_prompt_tokens(prompt_text)
        total = 0.0
        breakdown = []
        for label, model, max_tokens, extra in calls:
            pp, cp = pricing[model]
            cost = (prompt_tokens + extra) * pp + max_tokens * cp
            breakdown.append((label, model, cost))
            total += cost
        if total > self.max_cost:
            raise HarnessError(
                f"worst-case estimate ${total:.6f} exceeds ceiling ${self.max_cost:.6f}. Refusing.")
        return total, breakdown

    # 5
    def record_actual(self, cost, label):
        self.spent += cost
        if self.spent > self.max_cost:
            raise HarnessError(
                f"actual running cost ${self.spent:.6f} exceeded ceiling "
                f"${self.max_cost:.6f} (after '{label}'). Aborting.")


def chat(transport, api_key, model, messages, max_tokens, reasoning_effort="low",
         governor=None):
    if governor:
        governor.check_byok(model)
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if reasoning_effort and reasoning_effort != "none":
        payload["reasoning"] = {"effort": reasoning_effort}
    if governor:
        governor.assert_no_tools(payload, model)
    return transport.post(OPENROUTER_CHAT_URL, api_key, payload)


def panel_judge(*, transport, api_key, governor, prompt, panel, judge, max_tokens=None,
                reasoning_effort="low", task_id=None, ledger=None):
    """N independent cheap takes + 1 judge synthesis. Returns a result dict."""
    max_tokens = max_tokens or DEFAULT_MAX_TOKENS

    for _m in list(panel) + [judge]:
        governor.check_byok(_m)

    calls = [(m_, m_, max_tokens, 0) for m_ in panel]
    calls.append((f"{judge} (judge)", judge, max_tokens + 50, len(panel) * max_tokens + 100))
    total_estimate, breakdown = governor.preflight(prompt, calls)
    eprint("[preflight] worst-case cost breakdown:")
    for label, model, cost in breakdown:
        eprint(f"  {label}: ${cost:.6f}")
    eprint(f"[preflight] TOTAL worst-case: ${total_estimate:.6f} "
           f"(ceiling: ${governor.max_cost:.6f})")

    panel_results = []
    for model in panel:
        eprint(f"[panel] calling {model} ...")
        t0 = time.time()
        status, resp = chat(transport, api_key, model,
                            [{"role": "user", "content": prompt}], max_tokens,
                            reasoning_effort, governor)
        elapsed = time.time() - t0
        if status != 200:
            err = resp.get("error", {}).get("message", str(resp))
            eprint(f"[panel] {model} FAILED ({status}): {err} -- skipping, continuing.")
            continue
        content, finish_reason, cost, is_byok = extract_content_and_cost(resp)
        if is_byok:
            raise HarnessError(f"{model} came back is_byok=true despite denylist pass. "
                               f"Add its org-prefix to BYOK_DENYLIST_PREFIXES.")
        governor.record_actual(cost, model)
        eprint(f"[panel] {model}: cost=${cost:.6f}, finish_reason={finish_reason}, {elapsed:.1f}s")
        if finish_reason == "length":
            eprint(f"[panel] WARNING: {model} truncated by --max-tokens.")
        panel_results.append({
            "model": model, "content": content, "finish_reason": finish_reason,
            "cost": cost, "truncated": finish_reason == "length",
        })

    if not panel_results:
        raise HarnessError("all panel calls failed. Aborting.")

    judge_prompt = (
        f"{len(panel_results)} independent models were asked the same question. Synthesize "
        f"their answers into a single clear recommendation. Note where they agree, where they "
        f"disagree, and give a final verdict. Under 150 words.\n\n")
    for r in panel_results:
        note = " [NOTE: cut off by token limit, may be incomplete]" if r["truncated"] else ""
        judge_prompt += f"--- Model: {r['model']}{note} ---\n{r['content']}\n\n"

    eprint(f"[judge] calling {judge} ...")
    status, resp = chat(transport, api_key, judge,
                        [{"role": "user", "content": judge_prompt}], max_tokens + 50,
                        reasoning_effort, governor)
    judge_content = None
    judge_cost = 0.0
    if status != 200:
        err = resp.get("error", {}).get("message", str(resp))
        eprint(f"[judge] FAILED ({status}): {err} -- raw panel outputs only.")
    else:
        judge_content, _, judge_cost, is_byok = extract_content_and_cost(resp)
        if is_byok:
            raise HarnessError(f"judge model {judge} came back is_byok=true.")
        governor.record_actual(judge_cost, judge)

    eprint(f"\n[TOTAL] actual cost this run: ${governor.spent:.6f} "
           f"(ceiling: ${governor.max_cost:.6f})")

    result = {
        "panel_results": panel_results,
        "judge_model": judge,
        "judge_synthesis": judge_content,
        "verdict": judge_content or "[raw panel outputs only -- no synthesis available]",
        "estimated_worst_case_cost": total_estimate,
        "actual_cost": governor.spent,
        "max_cost_ceiling": governor.max_cost,
    }
    if ledger and task_id:
        ledger.append("complete", task_id=task_id, event_note="panel_judge",
                      model=judge, cost=governor.spent, status="ok")
    return result