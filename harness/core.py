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

On top of the original engine this adds:

  * Reasoning/effort modes fully flushed out (auto / off / low / medium /
    high), a cap on reasoning-token spend so reasoning models leave room for
    a real answer, and a retry-without-reasoning fallback for providers that
    reject the parameter.
  * Live free-model discovery + validation, so hardcoded slugs that have
    gone stale are caught against the real /models list.
  * Panel rotation: a failing panel member is replaced by the next model in
    the pool instead of just being skipped.
  * A structured judge verdict (agreement / confidence / disagreements /
    defer) so consensus is measured, not just asserted.
"""
import json
import sys
import time

from .config import (
    OPENROUTER_CHAT_URL, OPENROUTER_KEY_URL, OPENROUTER_MODELS_URL,
    BYOK_DENYLIST_PREFIXES, BYOK_PREFIXES_PATH, load_byok_prefixes,
    save_byok_prefixes, DEFAULT_MAX_COST, DEFAULT_MAX_TOKENS,
)
from ._http import Transport  # noqa: F401  (documenting the transport seam)


class HarnessError(Exception):
    """A fatal refusal or failure. Message is user-presentable."""


def eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)


def estimate_prompt_tokens(text):
    return int(len(text.split()) * 1.5) + 50


def _extract_json(text):
    """Extract the first balanced {...} object from arbitrary model output."""
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


REASONING_FALLBACK_PREFIX = "[NOTE] model returned no content"


def extract_content_and_cost(resp):
    """Pull content, finish_reason, actual cost, is_byok from a completion.

    Falls back to the reasoning trace when a model emitted reasoning but no
    visible content — reasoning models otherwise "succeed" with empty output
    and a paid call is discarded with nothing to show for it. Callers that need
    *real* content (e.g. apply, which writes output to a file) must treat
    anything starting with REASONING_FALLBACK_PREFIX as "no usable output"
    rather than content.
    """
    try:
        message = resp["choices"][0]["message"]
        content = message.get("content")
        if not (content or "").strip():
            reasoning = message.get("reasoning")
            if (reasoning or "").strip():
                content = (REASONING_FALLBACK_PREFIX + "; showing reasoning trace instead.\n\n"
                           + reasoning)
        finish_reason = resp["choices"][0].get("finish_reason", "unknown")
        cost = resp.get("usage", {}).get("cost", 0.0)
        is_byok = resp.get("usage", {}).get("is_byok", False)
        return content, finish_reason, cost, is_byok
    except (KeyError, IndexError, TypeError):
        return None, None, 0.0, False


# ------------------------- reasoning / effort -------------------------

_REASONING_HINTS = ("reason", "thinking", "inkling", "qwq", "r1", "o3", "o4",
                    "deepseek", "kimi", "glm-4.6", "glm-5.6", "minimax-reason")
_EFFORT_VALUES = ("auto", "off", "none", "low", "medium", "high", "on")
_REASONING_PARAM_ERR_HINTS = ("reasoning", "unsupported parameter",
                              "unknown parameter", "unexpected parameter")


def looks_reasoning(model_id):
    """Heuristic: does this model id smell like a reasoning model?"""
    m = model_id.lower()
    return any(h in m for h in _REASONING_HINTS)


def _effort_to_send(reasoning_effort, model_id):
    """Resolve the reasoning effort string to send, or None to omit.

    "auto" sends a capped low effort for reasoning-named models and omits the
    key entirely for everyone else. "off"/"none" always omit.
    """
    e = (reasoning_effort or "auto").lower()
    if e in ("off", "none"):
        return None
    if e == "auto":
        return "low" if looks_reasoning(model_id) else None
    if e == "on":
        return "high"
    if e in ("low", "medium", "high"):
        return e
    return None


def _build_reasoning_param(model_id, reasoning_effort, max_tokens, budget):
    """Return the reasoning dict to embed in the payload, or None."""
    effort = _effort_to_send(reasoning_effort, model_id)
    if effort is None:
        return None
    cap = max(1, int(max_tokens * budget))
    return {"effort": effort, "max_tokens": cap}


class SpendGovernor:
    """Enforces the guarantees that make sub-cent runs a *guarantee*, not a hope."""

    def __init__(self, transport, api_key, expect_key_label=None,
                 max_cost=DEFAULT_MAX_COST, byok_prefixes_path=BYOK_PREFIXES_PATH):
        self.transport = transport
        self.api_key = api_key
        self.expect_key_label = expect_key_label
        self.max_cost = max_cost
        self.spent = 0.0
        self.key_info = None
        self._pricing_cache = {}
        self._models = None
        self._byok_path = byok_prefixes_path
        self._learned_byok = set(load_byok_prefixes(byok_prefixes_path))

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
        """Hard block: raises for prefixes that must NEVER run (P0)."""
        for prefix in BYOK_DENYLIST_PREFIXES:
            if model_id.startswith(prefix):
                raise HarnessError(
                    f"model '{model_id}' matches BYOK denylist prefix '{prefix}'. Refusing.")

    def learned_blocked(self, model_id):
        """True if this org-prefix was observed routing via BYOK on this account."""
        return any(model_id.startswith(p) for p in self._learned_byok)

    def record_byok(self, model_id):
        """Persist an observed BYOK org-prefix so future runs skip it."""
        prefix = model_id.split("/", 1)[0] + "/"
        self._learned_byok.add(prefix)
        try:
            save_byok_prefixes(self._byok_path, self._learned_byok)
        except OSError:
            pass

    def is_free(self, model_id):
        """True if the model's per-token pricing is zero (no spend to leak)."""
        pp, cp = self.fetch_pricing([model_id])[model_id]
        return pp == 0.0 and cp == 0.0

    # 1
    def assert_no_tools(self, payload, label):
        if "tools" in payload:
            raise HarnessError(
                f"payload for {label} contains a 'tools' key -- refusing to send.")

    # 2
    def fetch_pricing(self, model_ids):
        missing = [m_ for m_ in model_ids if m_ not in self._pricing_cache]
        if missing:
            models = self.fetch_models()
            by_id = {m_["id"]: m_ for m_ in models}
            for mid in missing:
                if mid not in by_id:
                    raise HarnessError(f"model '{mid}' not found in live OpenRouter model list.")
                p = by_id[mid].get("pricing", {})
                try:
                    # OpenRouter pricing fields are already PER-TOKEN dollar prices
                    # (e.g. "0.00000001" = $0.01/M). Do NOT divide by 1e6 again —
                    # an earlier version did and undercounted cost ~1,000,000x.
                    self._pricing_cache[mid] = (
                        float(p.get("prompt", "0")), float(p.get("completion", "0")))
                except (TypeError, ValueError):
                    raise HarnessError(f"could not parse pricing for '{mid}': {p}")
        return {m_: self._pricing_cache[m_] for m_ in model_ids}

    def fetch_models(self):
        """Live GET /models, cached per governor instance."""
        if self._models is None:
            try:
                self._models = self.transport.get(OPENROUTER_MODELS_URL, self.api_key,
                                                  timeout=20).get("data", [])
            except Exception as e:
                raise HarnessError(f"could not fetch model list: {e}")
        return self._models

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


# ------------------------- live discovery -------------------------

def _is_free_model(m):
    mid = m.get("id", "")
    p = m.get("pricing", {})
    return mid.endswith(":free") or (str(p.get("prompt")) == "0" and str(p.get("completion")) == "0")


def discover_free_models(transport, api_key, prefer=None, limit=40):
    """Return the ordered list of free model ids, curated preference first.

    prefer is an ordered list of ids to rank highest; other free models are
    appended after. Any prefer entry that is no longer free/existing is
    dropped. Sorted by name for a stable tail.
    """
    gov = SpendGovernor(transport, api_key)
    models = gov.fetch_models()
    free = [m_["id"] for m_ in models if _is_free_model(m_)]
    free_set = set(free)
    ordered = []
    for pid in (prefer or []):
        if pid in free_set and pid not in ordered:
            ordered.append(pid)
    for mid in sorted(free):
        if mid not in ordered:
            ordered.append(mid)
    return ordered[:limit]


def resolve_models(transport, api_key, model_ids):
    """Return only the ids that exist on the live model list."""
    gov = SpendGovernor(transport, api_key)
    ids = {m_["id"] for m_ in gov.fetch_models()}
    return [m_ for m_ in model_ids if m_ in ids]


# ------------------------- chat -------------------------

def chat(transport, api_key, model, messages, max_tokens, reasoning_effort="auto",
         reasoning_token_budget=0.4, governor=None):
    """One chat completion with the spend governor's payload guards.

    Reasoning is only included when the effort mode calls for it (auto => only
    for reasoning-named models). If a provider rejects the reasoning
    parameter, we retry once without it.
    """
    if governor:
        governor.check_byok(model)

    def build(with_reasoning):
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if with_reasoning:
            rp = _build_reasoning_param(model, reasoning_effort, max_tokens,
                                        reasoning_token_budget)
            if rp:
                payload["reasoning"] = rp
        if governor:
            governor.assert_no_tools(payload, model)
        return payload

    want_reasoning = _effort_to_send(reasoning_effort, model) is not None
    status, resp = transport.post(OPENROUTER_CHAT_URL, api_key, build(want_reasoning))
    if want_reasoning and status != 200:
        err = str(resp.get("error", {}).get("message", resp)).lower()
        if any(h in err for h in _REASONING_PARAM_ERR_HINTS):
            eprint(f"[retry] {model} rejected reasoning param; retrying without it.")
            return transport.post(OPENROUTER_CHAT_URL, api_key, build(False))
    return status, resp


# ------------------------- panel + judge -------------------------

def _parse_consensus(judge_text):
    """Parse the judge's JSON verdict; fail softly to unknown on bad output."""
    parsed = _extract_json(judge_text)
    if not parsed:
        return {"agreement": "unknown", "confidence": None, "disagreements": [],
                "defer": False, "verdict": judge_text or ""}
    verdict = parsed.get("verdict")
    agreement = str(parsed.get("agreement", "unknown")).lower()
    if agreement not in ("high", "medium", "low", "none"):
        agreement = "unknown"
    conf = parsed.get("confidence")
    try:
        conf = None if conf is None else float(conf)
        conf = max(0.0, min(1.0, conf))
    except (TypeError, ValueError):
        conf = None
    disagreements = parsed.get("disagreements") or []
    if not isinstance(disagreements, list):
        disagreements = []
    defer = bool(parsed.get("defer", False)) or agreement in ("low", "none")
    return {
        "verdict": verdict or judge_text or "",
        "agreement": agreement,
        "confidence": conf,
        "disagreements": disagreements,
        "defer": defer,
    }


# ------------------------- convergence specialist -------------------------
#
# The judge synthesizes the panel into a verdict. The convergence specialist is
# a distinct step for STRUCTURED claims audits: it reads the panel's per-claim
# JSON verdicts and renders the final convergence report. It defaults to the
# judge model when not overridden. Panel agreement is computed deterministically
# too -- a claim CONVERGES only when every panelist that answered it agrees on
# `real` (5/5 unanimous == 100% on that claim).


def extract_claim_verdicts(content):
    """Return the parsed per-claim dict from a panelist's structured response."""
    parsed = _extract_json(content)
    if not isinstance(parsed, dict):
        return {}
    return {k: v for k, v in parsed.items()
            if isinstance(v, dict) and "real" in v}


def tally_convergence(panel_results, claim_polarity=None):
    """Deterministic per-claim consensus from the panel's per-claim JSON verdicts.

    POLARITY CONVENTION: a claim is a DEFECT proposition -- `real: true` means
    the stated defect genuinely exists in the code. This makes statement-truth
    and defect-presence readings coincide, so identical substance yields
    identical votes across models.

    claim_polarity maps a claim id to "defect" (default) or "reassurance". A
    REASSURANCE claim asserts something is CORRECT ("X is order-independent");
    models encode agreement with it inconsistently (some read real:true as
    "the statement holds", others as "a defect exists"), so a reassurance
    claim can NEVER be normalized reliably -- identical substance could split
    the tally. Reassurance claims are therefore EXCLUDED from the convergence
    gate and reported separately, so they cannot split an otherwise-unanimous
    defect tally.

    For each defect claim, gather every panelist's `real` vote. A claim
    CONVERGES when all panelists that answered it agree (5/5 == 100%).
    """
    claim_polarity = claim_polarity or {}
    buckets = {}
    order = {}
    for r in panel_results:
        verdicts = extract_claim_verdicts(r.get("content") or "")
        for cid, v in verdicts.items():
            kind = "reassurance" if claim_polarity.get(cid, "defect") == "reassurance" else "defect"
            if cid not in buckets:
                buckets[cid] = {"kind": kind, "votes": {"real": 0, "not_real": 0},
                                "confidences": [], "models": []}
                order[cid] = kind
            c = buckets[cid]
            c["votes"]["real" if v.get("real") else "not_real"] += 1
            if v.get("confidence") is not None:
                try:
                    c["confidences"].append(float(v["confidence"]))
                except (TypeError, ValueError):
                    pass
            c["models"].append(r.get("model"))

    per_claim = {}
    reassurance = {}
    converged_claims = 0
    defect_total = 0
    for cid, kind in order.items():
        c = buckets[cid]
        votes = c["votes"]
        total = votes["real"] + votes["not_real"]
        unanimous = total > 0 and (votes["real"] == 0 or votes["not_real"] == 0)
        majority = "real" if votes["real"] >= votes["not_real"] else "not_real"
        mean_conf = round(sum(c["confidences"]) / len(c["confidences"]), 3) if c["confidences"] else None
        entry = {"verdict": majority, "unanimous": unanimous, "voted_by": total,
                 "confidence": mean_conf, "votes": votes}
        if kind == "defect":
            defect_total += 1
            if unanimous:
                converged_claims += 1
            per_claim[cid] = entry
        else:
            # Reassurance: reported but never gates convergence (polarity of
            # `real` is convention-dependent across models).
            entry["note"] = "reassurance claim: real:true means the stated " \
                             "correctness holds; excluded from the convergence gate"
            reassurance[cid] = entry
    return {
        "converged": defect_total > 0 and converged_claims == defect_total,
        "converged_claims": converged_claims,
        "total_claims": defect_total,
        "convergence_rate": round(converged_claims / defect_total, 3) if defect_total else None,
        "claims": per_claim,
        "reassurance": reassurance,
    }


def run_convergence_specialist(transport, api_key, governor, panel_results, model,
                               max_tokens=1200, reasoning_effort="auto",
                               reasoning_token_budget=0.4):
    """A dedicated 'convergence specialist' renders the final verdict from the
    panel's per-claim JSON (defaults to the judge model when not overridden).
    Falls back to the deterministic tally if the specialist call fails."""
    lines = [
        "You are a convergence specialist. N independent models each reviewed the same claims "
        "and emitted per-claim verdicts {\"claim\":{\"real\":bool,\"confidence\":..}}. "
        "Produce the FINAL convergence consensus as ONE JSON object, no prose:",
        "{\"converged\":true|false,\"agreement\":\"high|medium|low|none\","
        "\"confidence\":<0-1>,\"claims\":{\"<claim>\":{\"verdict\":\"real|not_real\","
        "\"converged\":true|false,\"confidence\":<0-1>}}}",
        "A claim is converged only when every model that answered agrees on its verdict. "
        "Claims are DEFECT propositions: real:true means the stated defect genuinely exists. "
        "Do not invent claims or models.",
    ]
    for r in panel_results:
        body = (r.get("content") or "")[:2000]
        lines.append(f"--- Model: {r.get('model')} ---\n{body}")
    prompt = "\n".join(lines)

    governor.preflight(prompt, [("convergence", model, max_tokens, 0)])
    status, resp = chat(transport, api_key, model, [{"role": "user", "content": prompt}],
                        max_tokens, reasoning_effort, reasoning_token_budget, governor)
    if status != 200:
        return {"status": "error", "model": model,
                "error": resp.get("error", {}).get("message", str(resp))}
    content, _, cost, is_byok = extract_content_and_cost(resp)
    if is_byok and not governor.is_free(model):
        governor.record_byok(model)
        return {"status": "error", "model": model, "error": "paid BYOK route; no specialist verdict"}
    governor.record_actual(cost, model)
    return {"status": "ok", "model": model,
            "specialist": _extract_json(content) or {},
            "raw": content, "cost": cost}


def panel_judge(*, transport, api_key, governor, prompt, panel, judge, max_tokens=None,
                reasoning_effort="auto", reasoning_token_budget=0.4, task_id=None,
                ledger=None, max_panelists=3, run_convergence=False,
                convergence_model=None, claim_polarity=None,
                capability_profiles=None, report=None):
    """Rotating panel of independent cheap takes + 1 structured judge verdict.

    panel is an ordered pool; members that fail are replaced by the next model
    in the pool until max_panelists succeed or the pool is exhausted.
    """
    max_tokens = max_tokens or DEFAULT_MAX_TOKENS
    panel_pool = list(panel)

    for _m in panel_pool + [judge]:
        governor.check_byok(_m)  # P0: raise on mistralai//anthropic/

    # Capability-aware ordering: when profiles are supplied, order the free
    # panel so the MORE capable model is tried first (cost is equal on the free
    # tier), using reliability as the tiebreaker. Degrades gracefully to the
    # given order if ordering empties the pool (e.g. all hard-gated out).
    if capability_profiles:
        from .capability import order_pool
        task = "structured" if run_convergence else "default"
        ordered = order_pool(panel_pool, capability_profiles, report,
                             task=task, free_tier=True)
        if ordered:
            panel_pool = ordered

    # Rotate out any org-prefix previously observed routing via BYOK (paid).
    panel_pool = [m_ for m_ in panel_pool if not governor.learned_blocked(m_)]
    judge_blocked = governor.learned_blocked(judge)
    target = max(1, min(max_panelists, len(panel_pool)))

    judge_max_tokens = max(768, max_tokens + 200)
    calls = [(m_, m_, max_tokens, 0) for m_ in panel_pool[:target]]
    calls.append((f"{judge} (judge)", judge, judge_max_tokens, target * max_tokens + 100))
    total_estimate, breakdown = governor.preflight(prompt, calls)
    eprint("[preflight] worst-case cost breakdown:")
    for label, model, cost in breakdown:
        eprint(f"  {label}: ${cost:.6f}")
    eprint(f"[preflight] TOTAL worst-case: ${total_estimate:.6f} "
           f"(ceiling: ${governor.max_cost:.6f})")

    panel_results = []
    tried = 0
    for model in panel_pool:
        if len(panel_results) >= target:
            break
        tried += 1
        eprint(f"[panel] calling {model} ...")
        t0 = time.time()
        status, resp = chat(transport, api_key, model,
                            [{"role": "user", "content": prompt}], max_tokens,
                            reasoning_effort, reasoning_token_budget, governor)
        elapsed = time.time() - t0
        if status != 200:
            err = resp.get("error", {}).get("message", str(resp))
            eprint(f"[panel] {model} FAILED ({status}): {err} -- rotating to next model.")
            continue
        content, finish_reason, cost, is_byok = extract_content_and_cost(resp)
        if is_byok:
            if governor.is_free(model):
                # Free BYOK routes cost $0; nothing to leak. Use it, with a note.
                eprint(f"[panel] {model} is BYOK-routed but free (cost 0); accepting.")
            else:
                governor.record_byok(model)
                eprint(f"[panel] {model} is BYOK-routed (paid); recorded and rotating.")
                continue
        governor.record_actual(cost, model)
        eprint(f"[panel] {model}: cost=${cost:.6f}, finish_reason={finish_reason}, {elapsed:.1f}s")
        if finish_reason == "length":
            eprint(f"[panel] WARNING: {model} truncated by --max-tokens.")
        panel_results.append({
            "model": model, "content": content, "finish_reason": finish_reason,
            "cost": cost, "truncated": finish_reason == "length",
        })
        if ledger and task_id:
            ledger.append("model_result", task_id=task_id, event_note="panel",
                          model=model, task_type="structured" if run_convergence else "panel",
                          json_expected=run_convergence,
                          json_ok=bool(extract_claim_verdicts(content)) if run_convergence else None,
                          status="ok")

    if not panel_results:
        raise HarnessError("all panel calls failed. Aborting.")

    judge_prompt = (
        f"{len(panel_results)} independent models were asked the same question. Synthesize "
        f"their answers. Respond with a SINGLE JSON object and nothing else:\n"
        f"{{\"verdict\": \"<clear final recommendation>\", "
        f"\"agreement\": \"high\"|\"medium\"|\"low\"|\"none\", "
        f"\"confidence\": <0.0 to 1.0>, "
        f"\"disagreements\": [\"<each point where models disagree>\"], "
        f"\"defer\": true|false}}\n"
        f"Set defer=true when the panel cannot reach enough agreement to make a reliable call "
        f"(the work should be deferred rather than guessed). Do not paper over disagreement.\n\n")
    for r in panel_results:
        note = " [NOTE: cut off by token limit, may be incomplete]" if r["truncated"] else ""
        body = r["content"] if len(r["content"]) <= 1500 else r["content"][:1500] + "\n...[truncated]"
        judge_prompt += f"--- Model: {r['model']}{note} ---\n{body}\n\n"

    judge_content = None
    judge_cost = 0.0
    if judge_blocked:
        eprint(f"[judge] {judge} routes via paid BYOK on this account; raw panel outputs only.")
    else:
        eprint(f"[judge] calling {judge} ...")
        status, resp = chat(transport, api_key, judge,
                            [{"role": "user", "content": judge_prompt}], judge_max_tokens,
                            reasoning_effort, reasoning_token_budget, governor)
        if status != 200:
            err = resp.get("error", {}).get("message", str(resp))
            eprint(f"[judge] FAILED ({status}): {err} -- raw panel outputs only.")
        else:
            judge_content, _, judge_cost, is_byok = extract_content_and_cost(resp)
            if is_byok:
                if governor.is_free(judge):
                    eprint("[judge] BYOK-routed but free (cost 0); accepting.")
                else:
                    governor.record_byok(judge)
                    eprint("[judge] BYOK-routed (paid); raw panel outputs only.")
                    judge_content = None
            governor.record_actual(judge_cost, judge)
            if ledger and task_id and judge_content is not None:
                ledger.append("model_result", task_id=task_id, event_note="judge",
                              model=judge, task_type="structured" if run_convergence else "judge",
                              json_expected=True,
                              json_ok=_extract_json(judge_content) is not None,
                              status="ok")

    consensus = _parse_consensus(judge_content) if judge_content else {
        "agreement": "unknown", "confidence": None, "disagreements": [],
        "defer": True, "verdict": "[raw panel outputs only -- no synthesis available]",
    }

    eprint(f"\n[TOTAL] actual cost this run: ${governor.spent:.6f} "
           f"(ceiling: ${governor.max_cost:.6f})")

    # Optional structured-claims convergence step: a dedicated specialist
    # renders the final verdict from the panel's per-claim JSON (defaults to the
    # judge model), and the deterministic tally gives the ground-truth 5/5 rate.
    convergence_spec = None
    if run_convergence:
        spec_model = convergence_model or judge
        tally = tally_convergence(panel_results, claim_polarity=claim_polarity)
        spec = run_convergence_specialist(
            transport, api_key, governor, panel_results, spec_model,
            max_tokens=judge_max_tokens, reasoning_effort=reasoning_effort,
            reasoning_token_budget=reasoning_token_budget)
        spec["tally"] = tally
        # Override judge's agreement/confidence with the ground-truth convergence
        # rate when the panel fully converged (5/5 unanimous == 100%).
        if tally["converged"]:
            consensus["agreement"] = "high"
            consensus["confidence"] = tally["convergence_rate"] or 0.0
        convergence_spec = spec

    result = {
        "panel_results": panel_results,
        "panel_tried": tried,
        "judge_model": judge,
        "judge_synthesis": judge_content,
        "verdict": consensus["verdict"],
        "consensus": {k: consensus[k] for k in
                      ("agreement", "confidence", "disagreements", "defer")},
        "estimated_worst_case_cost": total_estimate,
        "actual_cost": governor.spent,
        "max_cost_ceiling": governor.max_cost,
    }
    if convergence_spec is not None:
        result["convergence"] = convergence_spec
    if ledger and task_id:
        ledger.append("complete", task_id=task_id, event_note="panel_judge",
                      model=judge, cost=governor.spent, status="ok",
                      agreement=consensus["agreement"])
    return result