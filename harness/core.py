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
    defer) so consensus is measured, not just asserted. Structured convergence
    separates responder agreement from fail-closed gate eligibility, so a
    transport shortfall is reported as a shortfall rather than disagreement.
"""
import concurrent.futures
import json
import sys
import threading
import time

from .config import (
    OPENROUTER_CHAT_URL, OPENROUTER_KEY_URL, OPENROUTER_MODELS_URL,
    BYOK_DENYLIST_PREFIXES, BYOK_PREFIXES_PATH, load_byok_prefixes,
    save_byok_prefixes, DEFAULT_MAX_COST, DEFAULT_MAX_TOKENS,
)
from .errors import HarnessError as _BaseHarnessError
from ._http import Transport  # noqa: F401  (documenting the transport seam)


class HarnessError(_BaseHarnessError):  # canonical home: harness.errors
    """A fatal refusal or failure. Message is user-presentable."""
    kind = "harness_error"


class ToolCancelled(Exception):
    """Raised inside a long-running tool when its request id is cancelled
    via notifications/cancelled (#13). Not an error of the work itself."""


# --quiet: suppress progress chatter but never warnings/fatals.
QUIET = False
_AUDIBLE_PREFIXES = ("[warn]", "[FATAL]", "[claims-lint]", "[BYOK]")

def eprint(*a, **kw):

    if QUIET and a and not str(a[0]).startswith(_AUDIBLE_PREFIXES):
        return
    print(*a, file=sys.stderr, **kw)


def estimate_prompt_tokens(text):
    """Approximate the prompt's token count without a tokenizer.

    max(words * 1.5, chars / 4): the words heuristic alone badly undercounts
    symbol-dense source code (JSON, Rust generics), where ~4 chars/token
    dominates. Taking the larger of the two keeps preflight ceilings honest
    in the direction of over- rather than under-estimating cost.
    """
    if not text:
        return 50
    return max(int(len(text.split()) * 1.5) + 50, int(len(text) / 4) + 1)


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
MAX_429_RETRIES = 2
# Backoff base for bounded 429 retries; Retry-After headers take precedence.
RETRY_429_BACKOFF_SECONDS = 0.5
DEFAULT_CONVERGENCE_PANEL_TOKENS = 4096


def _reported_cost(resp):
    """Read a provider-reported cost even when the HTTP response is an error."""
    try:
        return float((resp.get("usage") or {}).get("cost") or 0.0)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _merge_retry_cost(resp, prior_cost):
    """Carry a billable failed reasoning attempt into the retry response.

    ``chat`` keeps its small ``(status, response)`` API, so callers observe one
    response. Adding the prior attempt to ``usage.cost`` makes the governor and
    the ledger charge the complete provider-reported total without silently
    dropping a billable rejected request.
    """
    if not prior_cost or not isinstance(resp, dict):
        return resp
    usage = resp.setdefault("usage", {})
    try:
        current = float(usage.get("cost") or 0.0)
    except (TypeError, ValueError):
        current = 0.0
    usage["cost"] = current + prior_cost
    usage["retry_cost"] = prior_cost
    return resp


def extract_content_and_cost(resp):
    """Pull content, finish_reason, actual cost, is_byok from a completion.

    Falls back to the reasoning trace when a model emitted reasoning but no
    visible content — reasoning models otherwise "succeed" with empty output
    and a paid call is discarded with nothing to show for it. Callers that need
    *real* content (e.g. apply, which writes output to a file) must treat
    anything starting with REASONING_FALLBACK_PREFIX as "no usable output"
    rather than content.

    Usage is extracted independently of the choice body. Providers can return
    a billable, malformed/empty completion; losing that usage value would make
    the governor and the ledger disagree about spend.
    """
    usage = resp.get("usage") if isinstance(resp, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    cost = usage.get("cost", 0.0)
    is_byok = usage.get("is_byok", False)
    try:
        choice = resp["choices"][0]
        message = choice["message"]
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            # This text-only harness must never turn a malformed multimodal/list
            # body into file content or feed it to JSON parsing. Treat it as an
            # unusable response so the caller can rotate/fail closed.
            content = None
        if not (content or "").strip():
            reasoning = message.get("reasoning")
            if isinstance(reasoning, str) and reasoning.strip():
                content = (REASONING_FALLBACK_PREFIX + "; showing reasoning trace instead.\n\n"
                           + reasoning)
        finish_reason = choice.get("finish_reason", "unknown")
        return content, finish_reason, cost, is_byok
    except (AttributeError, KeyError, IndexError, TypeError):
        return None, None, cost, is_byok


# ------------------------- reasoning / effort -------------------------

_REASONING_HINTS = ("reason", "thinking", "inkling", "qwq", "r1", "o3", "o4",
                    "deepseek", "kimi", "glm-4.6", "glm-5.2", "glm-5.6", "minimax-reason")
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


def _chat_reservation_slots(model_id, reasoning_effort="auto", max_429_retries=0):
    """Upper-bound provider calls for one governed logical request.

    A provider may reject a reasoning parameter, causing ``chat`` to make one
    fallback request. Panel calls may also make one bounded 429 retry. The
    preflight reservation must cover both possibilities or the ceiling is only
    approximate for reasoning models.
    """
    reasoning_slots = 2 if _effort_to_send(reasoning_effort, model_id) is not None else 1
    return reasoning_slots * (max(0, int(max_429_retries)) + 1)


class SpendGovernor:
    """Enforces the guarantees that make sub-cent runs a *guarantee*, not a hope."""

    def __init__(self, transport, api_key, expect_key_label=None,
                 max_cost=DEFAULT_MAX_COST, byok_prefixes_path=BYOK_PREFIXES_PATH):
        self.transport = transport
        self.api_key = api_key
        self.expect_key_label = expect_key_label
        self.max_cost = max_cost
        self.spent = 0.0
        self._cost_by_model = {}
        # Fan-out safety (#10): spend mutations happen from panel threads.
        self._spend_lock = threading.RLock()
        self.key_info = None
        self._pricing_cache = {}
        self._pricing_fetched_at = 0.0
        self._models_fetched_at = 0.0
        self._CATALOG_TTL = 900.0  # refresh the live catalog mid-run every 15 min
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
        if self.expect_key_label is not None and label != self.expect_key_label:
            # Exact match (audit #9b): a substring match let a wrong-but-
            # similarly-named key through, and echoing labels into errors is
            # needless exposure. Only a binary match/mismatch is reported.
            raise HarnessError(
                "resolved key label does not exactly match --expect-key-label. "
                "Refusing to run -- fix the key source, or drop --expect-key-label "
                "if this key is actually correct.")
        self.key_info = {
            "label": label, "limit": limit, "remaining": remaining,
            "limit_reset": data.get("limit_reset"),
        }
        return self.key_info

    def key_status(self):
        if not self.key_info:
            self.verify_key()
        return dict(self.key_info, session_spent=self.spent)

    def cost_by_model(self):
        """Actual session spend per model label, for per-run cost reports."""
        return dict(sorted(self._cost_by_model.items(), key=lambda kv: -kv[1]))

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
        missing = [m_ for m_ in model_ids if m_ not in self._pricing_cache
                   or time.time() - self._pricing_fetched_at > self._CATALOG_TTL]
        if missing:
            models = self.fetch_models(refresh=True)
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
            self._pricing_fetched_at = time.time()
        return {m_: self._pricing_cache[m_] for m_ in model_ids}

    def fetch_models(self, refresh=False):
        """Live GET /models, cached per governor instance. The cache expires
        after _CATALOG_TTL so a long run picks up pricing/pool changes
        mid-run instead of trusting a stale snapshot forever (#18)."""
        stale = (self._models is None
                 or time.time() - self._models_fetched_at > self._CATALOG_TTL)
        if refresh or stale:
            try:
                self._models = self.transport.get(OPENROUTER_MODELS_URL, self.api_key,
                                                  timeout=20).get("data", [])
                self._models_fetched_at = time.time()
            except Exception as e:
                if self._models is not None:
                    eprint(f"[warn] model list refresh failed, using cached catalog: {e}")
                else:
                    raise HarnessError(f"could not fetch model list: {e}")
        return self._models

    def preflight(self, prompt_text, calls):
        """calls = [(label, model, max_tokens, extra_input)] -> (total, breakdown).

        Worst-case: every call maxes its max_tokens. extra_input accounts for
        tokens a later call consumes beyond the base prompt (e.g. the judge
        reading panel outputs). The estimate is checked against the *remaining*
        session ceiling, so a later rotation cannot spend through an earlier
        call's budget. Callers should include every bounded replacement they may
        try in ``calls``.
        """
        if not calls:
            # Pre-guard kept for clarity: an empty call list costs nothing.
            return 0.0, []
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
        if self.spent + total > self.max_cost:
            raise HarnessError(
                f"worst-case estimate ${self.spent + total:.6f} exceeds remaining ceiling "
                f"${self.max_cost:.6f}. Refusing.")
        return total, breakdown

    # 5
    def record_actual(self, cost, label):
        """Record a billable response without ever moving ``spent`` over the ceiling."""
        try:
            actual = float(cost or 0.0)
        except (TypeError, ValueError):
            raise HarnessError(f"invalid reported cost {cost!r} (after '{label}').")
        if actual < 0:
            raise HarnessError(f"negative reported cost {actual:.6f} (after '{label}').")
        if self.spent + actual > self.max_cost:
            raise HarnessError(
                f"actual running cost ${self.spent + actual:.6f} would exceed ceiling "
                f"${self.max_cost:.6f} (after '{label}'). Aborting.")
        self.spent += actual
        self._cost_by_model[label] = (self._cost_by_model.get(label, 0.0) + actual)

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
        err = str(resp.get("error", {}).get("message", resp)
                  if isinstance(resp, dict) else resp).lower()
        if any(h in err for h in _REASONING_PARAM_ERR_HINTS):
            eprint(f"[retry] {model} rejected reasoning param; retrying without it.")
            prior_cost = _reported_cost(resp)
            retry_status, retry_resp = transport.post(
                OPENROUTER_CHAT_URL, api_key, build(False))
            return retry_status, _merge_retry_cost(retry_resp, prior_cost)
    return status, resp


# ------------------------- panel + judge -------------------------

def _parse_consensus(judge_text):
    """Parse the judge's JSON verdict; fail softly to unknown on bad output."""
    parsed = _extract_json(judge_text)
    if not parsed:
        # A judge response that cannot be parsed is not an approval. Keep the
        # raw text for diagnosis, but fail closed so callers never interpret
        # malformed prose as a successful synthesis.
        return {"agreement": "unknown", "confidence": None, "disagreements": [],
                "defer": True, "verdict": judge_text or "",
                "defer_reason": "unparseable_judge_output"}
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
# too -- responder unanimity is reported when every valid responder agrees on
# `real`; the returned `converged` field additionally requires full panel
# coverage (5/5 unanimous == 100% at the merge gate).


def extract_claim_verdicts(content):
    """Return only well-formed per-claim votes from a panelist response.

    A structured convergence vote must contain a nested claim object whose
    ``real`` field is an actual JSON boolean. Strings such as ``"true"`` or
    malformed top-level prose are not votes and cannot satisfy participation.
    """
    if not isinstance(content, str):
        return {}
    parsed = _extract_json(content)
    if not isinstance(parsed, dict):
        return {}
    return {k: v for k, v in parsed.items()
            if isinstance(v, dict) and isinstance(v.get("real"), bool)}


def tally_convergence(panel_results, claim_polarity=None, of_panel=None):
    """Compute responder agreement and gate eligibility separately.

    ``real: true`` always means that the stated defect is present. A claim is
    *unanimous* when every valid responder agrees. ``converged`` is stricter:
    every required panel slot must have supplied a valid vote as well. This
    distinction makes a 2/3 shortfall report as high responder agreement with
    an explicit fail-closed coverage shortfall, rather than mislabeling it as
    model disagreement.
    """
    claim_polarity = claim_polarity or {}
    required = len(panel_results) if of_panel is None else max(0, int(of_panel))
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
            c["votes"]["real" if v["real"] else "not_real"] += 1
            if v.get("confidence") is not None:
                try:
                    c["confidences"].append(float(v["confidence"]))
                except (TypeError, ValueError):
                    pass
            c["models"].append(r.get("model"))

    per_claim = {}
    reassurance = {}
    responder_claims = 0
    gate_claims = 0
    defect_total = 0
    claim_shortfall = False
    claim_disagreement = False
    disagreement_claims = []
    for cid, kind in order.items():
        c = buckets[cid]
        votes = c["votes"]
        total = votes["real"] + votes["not_real"]
        responder_unanimous = total > 0 and (votes["real"] == 0 or votes["not_real"] == 0)
        gate_unanimous = (required > 0 and total == required and responder_unanimous)
        # Reassurance claims are informational and intentionally excluded from
        # the defect gate, including its coverage-shortfall calculation.
        if kind == "defect":
            claim_shortfall = claim_shortfall or total < required
        if kind == "defect" and votes["real"] > 0 and votes["not_real"] > 0:
            # Reassurance/informational claims are intentionally outside the
            # defect tally; their polarity must not pollute disagreement output
            # or the defer reason for an otherwise converged audit.
            claim_disagreement = True
            disagreement_claims.append(cid)
        majority = "real" if votes["real"] >= votes["not_real"] else "not_real"
        mean_conf = round(sum(c["confidences"]) / len(c["confidences"]), 3) if c["confidences"] else None
        entry = {
            "verdict": majority,
            # This is responder unanimity. ``converged``/``gate_unanimous`` is
            # the fail-closed merge-gate value when the panel was short.
            "unanimous": responder_unanimous,
            "responder_unanimous": responder_unanimous,
            "converged": gate_unanimous,
            "gate_unanimous": gate_unanimous,
            "voted_by": total,
            "of_panel": required,
            "confidence": mean_conf,
            "votes": votes,
            "missing_votes": max(0, required - total),
            "panel_shortfall": total < required,
        }
        if kind == "defect":
            defect_total += 1
            if responder_unanimous:
                responder_claims += 1
            if gate_unanimous:
                gate_claims += 1
            per_claim[cid] = entry
        else:
            entry["note"] = "reassurance claim: reported separately and excluded from the defect convergence gate"
            reassurance[cid] = entry

    valid_responders = []
    for index, r in enumerate(panel_results):
        verdicts = extract_claim_verdicts(r.get("content") or "")
        if any(claim_polarity.get(cid, "defect") != "reassurance"
               for cid in verdicts):
            valid_responders.append(r.get("model") or f"panel_{index + 1}")
    # Count responding panel slots, not unique model ids. A caller may
    # deliberately include the same model more than once; each slot still
    # needs its own vote for an accurate voted_by/of_panel report.
    voted_by = len(valid_responders)
    responding_models = list(dict.fromkeys(valid_responders))
    panel_shortfall = bool(claim_shortfall or (required > 0 and voted_by < required))
    missing_votes = max(
        [entry["missing_votes"] for entry in per_claim.values()] or
        [max(0, required - voted_by)])
    responder_converged = defect_total > 0 and responder_claims == defect_total
    gate_converged = defect_total > 0 and gate_claims == defect_total
    responder_rate = round(responder_claims / defect_total, 3) if defect_total else None
    gate_rate = round(gate_claims / defect_total, 3) if defect_total else None
    return {
        # ``converged`` is deliberately gate-safe: it cannot be true while a
        # required panel slot or claim vote is missing.
        "converged": gate_converged,
        "responder_converged": responder_converged,
        "converged_claims": gate_claims,
        "responder_converged_claims": responder_claims,
        "total_claims": defect_total,
        # Keep the historical field useful to callers: it measures agreement
        # among responders. ``gate_convergence_rate`` measures coverage-aware
        # eligibility.
        "convergence_rate": responder_rate,
        "gate_convergence_rate": gate_rate,
        "disagreement": claim_disagreement,
        "disagreement_claims": disagreement_claims,
        "panel_shortfall": panel_shortfall,
        "missing_votes": missing_votes,
        "shortfall": {"voted_by": voted_by, "of_panel": required,
                       "missing_votes": missing_votes} if panel_shortfall else None,
        "voted_by": voted_by,
        "of_panel": required,
        "responding_models": responding_models,
        "claims": per_claim,
        "reassurance": reassurance,
    }


def run_convergence_specialist(transport, api_key, governor, panel_results, model,
                               max_tokens=1200, reasoning_effort="auto",
                               reasoning_token_budget=0.4, ledger=None, task_id=None,
                               fallback_pool=None):
    """A dedicated 'convergence specialist' renders the final verdict from the
    panel's per-claim JSON (defaults to the judge model when not overridden).

    The specialist is itself a rotating lane: ``model`` is the primary, then
    ``fallback_pool`` (strongest first -- GLM-5.2 leads the free ladder) is
    tried in order. A candidate is rotated out on any imperfect outcome: HTTP
    error, paid-BYOK route, empty or reasoning-only output, truncation against
    the token cap, or unparseable JSON. Every attempt is preflight-reserved
    before the first call and billed per attempt, so the ceiling stays exact.
    The deterministic tally remains authoritative even if the whole lane fails.
    """
    lines = [
        "You are a convergence specialist. N independent models each reviewed the same claims "
        "and emitted per-claim verdicts {\"claim\":{\"real\":bool,\"confidence\":..}}. "
        "Produce the FINAL convergence consensus as ONE JSON object, no prose:",
        "{\"converged\":true|false,\"agreement\":\"high|medium|low|none\","
        "\"confidence\":<0-1>,\"claims\":{\"<claim>\":{\"verdict\":\"real|not_real\","
        "\"converged\":true|false,\"confidence\":<0-1>}}}",
        "Responder unanimity is present when every model that answered agrees on its verdict. "
        "The merge-gate converged field additionally requires every required panel slot to answer. "
        "Claims are DEFECT propositions: real:true means the stated defect genuinely exists. "
        "Do not invent claims or models.",
        # Resource-cap disclosure: the model must know its budget up front so it
        # can plan to finish inside it instead of truncating mid-JSON.
        f"RESOURCE CAP: your entire response is limited to {max_tokens} output tokens, and "
        f"hidden reasoning counts against it (reasoning itself capped at "
        f"{int(max_tokens * reasoning_token_budget)} tokens). A truncated or reasoning-only "
        "response will be discarded and the task rotated to another model. Do your best "
        "within the cap, assume nothing beyond it, and emit ONLY the JSON object as your "
        "visible content, starting with {.",
    ]
    for r in panel_results:
        # Full per-claim JSON: these are short, structured verdicts, and the
        # preflight reserve (target * panel_tokens) already covers them. A
        # truncated vote can silently drop claims and corrupt the tally.
        lines.append(f"--- Model: {r.get('model')} ---\n{r.get('content') or ''}")
    prompt = "\n".join(lines)

    # Build the rotation ladder: primary first, then fallbacks (deduped,
    # learned-BYOK-blocked models dropped). Unknown fallback models are skipped
    # rather than fatal; the caller-chosen primary must be real.
    candidates = [model]
    for m_ in (fallback_pool or []):
        if m_ and m_ not in candidates:
            candidates.append(m_)
    governor.check_byok(candidates[0])  # P0: raise on mistralai//anthropic/
    usable = []
    for i, m_ in enumerate(candidates):
        if i and governor.learned_blocked(m_):
            eprint(f"[convergence] skipping {m_}: previously routed via BYOK (paid).")
            continue
        try:
            governor.fetch_pricing([m_])
        except HarnessError as e:
            if i == 0:
                raise
            eprint(f"[convergence] skipping fallback {m_}: {e}")
            continue
        usable.append(m_)
    candidates = usable

    # Reserve the whole ladder up front (including each reasoning model's
    # possible no-reasoning retry) so the ceiling is exact before any call.
    calls = []
    for m_ in candidates:
        slots = _chat_reservation_slots(m_, reasoning_effort)
        for s in range(slots):
            calls.append((f"convergence {m_} attempt {s + 1}/{slots}", m_, max_tokens, 0))
    governor.preflight(prompt, calls)

    def _bill_event(m_, ok, ev_status, cost):
        if ledger and task_id:
            ledger.append("model_result", task_id=task_id, event_note="convergence",
                          model=m_, task_type="structured", json_expected=True,
                          json_ok=ok, status=ev_status, cost=cost)

    attempts = []
    total_cost = 0.0
    result = {"status": "error", "model": model,
              "error": "no specialist candidate available", "cost": 0.0}
    for m_ in candidates:
        status, resp = chat(transport, api_key, m_, [{"role": "user", "content": prompt}],
                            max_tokens, reasoning_effort, reasoning_token_budget, governor)
        if status != 200:
            error_cost = _reported_cost(resp)
            if error_cost:
                governor.record_actual(error_cost, m_)
                total_cost += error_cost
            error = (resp.get("error", {}).get("message", str(resp))
                     if isinstance(resp, dict) else str(resp))
            attempts.append({"model": m_, "status": "error", "error": error,
                             "cost": error_cost})
            _bill_event(m_, False, "error", error_cost)
            eprint(f"[convergence] {m_}: HTTP {status}; rotating.")
            result = {"status": "error", "model": m_, "error": error,
                      "cost": total_cost}
            continue
        content, finish, cost, is_byok = extract_content_and_cost(resp)
        if is_byok and not governor.is_free(m_):
            governor.record_byok(m_)
            attempts.append({"model": m_, "status": "error",
                             "error": "paid BYOK route; no specialist verdict", "cost": 0.0})
            _bill_event(m_, False, "error", 0.0)
            eprint(f"[convergence] {m_}: BYOK-routed (paid); rotating.")
            result = {"status": "error", "model": m_,
                      "error": "paid BYOK route; no specialist verdict",
                      "cost": total_cost}
            continue
        governor.record_actual(cost, m_)
        total_cost += cost
        # Imperfect-output triage: a reasoning-only, empty, or truncated body
        # must never be JSON-mined (a reasoning trace can embed JSON-looking
        # text that is not the verdict). Rotate instead.
        unusable = None
        if not content:
            unusable = "empty response"
        elif content.startswith(REASONING_FALLBACK_PREFIX):
            unusable = "reasoning-only output (no visible content)"
        elif finish == "length":
            unusable = "truncated (hit the token cap)"
        if unusable:
            attempts.append({"model": m_, "status": "error", "error": unusable,
                             "cost": cost})
            _bill_event(m_, False, "error", cost)
            eprint(f"[convergence] {m_}: {unusable}; rotating.")
            result = {"status": "error", "model": m_, "error": unusable,
                      "cost": total_cost}
            continue
        parsed = _extract_json(content)
        json_ok = isinstance(parsed, dict) and bool(parsed)
        _bill_event(m_, json_ok, "ok" if json_ok else "error", cost)
        if json_ok:
            attempts.append({"model": m_, "status": "ok", "cost": cost})
            return {"status": "ok", "model": m_, "specialist": parsed,
                    "raw": content, "cost": total_cost, "error": None,
                    "attempts": attempts}
        attempts.append({"model": m_, "status": "error",
                         "error": "no parseable JSON", "cost": cost})
        eprint(f"[convergence] {m_}: no parseable JSON; rotating.")
        result = {"status": "error", "model": m_,
                  "error": "specialist returned no parseable JSON",
                  "cost": total_cost}
    result["attempts"] = attempts
    return result


def panel_judge(*, transport, api_key, governor, prompt, panel, judge, max_tokens=None,
                reasoning_effort="auto", reasoning_token_budget=0.4, task_id=None,
                ledger=None, max_panelists=3, run_convergence=False,
                convergence_model=None, specialist_pool=None, claim_polarity=None,
                capability_profiles=None, report=None, free_tier=None):
    """Rotating panel of independent cheap takes + 1 structured judge verdict.

    panel is an ordered pool; members that fail are replaced by the next model
    in the pool until max_panelists succeed or the pool is exhausted.
    """
    max_tokens = max_tokens or DEFAULT_MAX_TOKENS
    panel_tokens = max_tokens
    if run_convergence:
        # Structured claim JSON is frequently longer than ordinary prose. Keep
        # the caller's lower bound but avoid wasting panel slots on truncation.
        panel_tokens = max(panel_tokens, DEFAULT_CONVERGENCE_PANEL_TOKENS)
    panel_pool = list(panel)

    spec_model = convergence_model or judge
    for _m in panel_pool + [judge, spec_model]:
        governor.check_byok(_m)  # P0: raise on mistralai//anthropic/

    # Capability-aware ordering: when profiles are supplied, order the free
    # panel so the MORE capable model is tried first (cost is equal on the free
    # tier), using reliability as the tiebreaker. Degrades gracefully to the
    # given order if ordering empties the pool (e.g. all hard-gated out).
    if capability_profiles:
        if free_tier is None:
            raise ValueError("panel_judge requires the caller's explicit use_free flag")
        from .capability import order_pool
        task = "structured" if run_convergence else "default"
        ordered = order_pool(panel_pool, capability_profiles, report, ledger=ledger,
                             task=task, free_tier=free_tier)
        if ordered:
            panel_pool = ordered

    # Rotate out any org-prefix previously observed routing via BYOK (paid).
    panel_pool = [m_ for m_ in panel_pool if not governor.learned_blocked(m_)]
    judge_blocked = governor.learned_blocked(judge)
    if not panel_pool:
        raise HarnessError("no available panel models after BYOK filtering")
    target = max(1, min(max_panelists, len(panel_pool)))

    judge_max_tokens = max(768, max_tokens + 200)
    # Reserve for every candidate, bounded 429 retries, and provider reasoning
    # fallbacks. A malformed/rate-limited member may consume a call before a
    # replacement fills its slot; a reasoning rejection may consume a fallback
    # request before the same logical call succeeds.
    calls = []
    for m_ in panel_pool:
        slots = _chat_reservation_slots(m_, reasoning_effort, MAX_429_RETRIES)
        for i in range(slots):
            calls.append((f"{m_} (panel attempt {i + 1}/{slots})", m_, panel_tokens, 0))
    judge_slots = _chat_reservation_slots(judge, reasoning_effort)
    for i in range(judge_slots):
        calls.append((f"{judge} (judge attempt {i + 1}/{judge_slots})", judge,
                      judge_max_tokens, target * panel_tokens + 100))
    if run_convergence:
        spec_slots = _chat_reservation_slots(spec_model, reasoning_effort)
        for i in range(spec_slots):
            calls.append((f"{spec_model} (convergence attempt {i + 1}/{spec_slots})",
                          spec_model, judge_max_tokens, target * panel_tokens + 100))
    total_estimate, breakdown = governor.preflight(prompt, calls)
    eprint("[preflight] worst-case cost breakdown:")
    for label, model, cost in breakdown:
        eprint(f"  {label}: ${cost:.6f}")
    eprint(f"[preflight] TOTAL worst-case: ${total_estimate:.6f} "
           f"(ceiling: ${governor.max_cost:.6f})")

    def _run_panel_slot(model):
        # One panel seat: call, gate, bill, ledger. Thread-safe: the only
        # shared mutable state (governor spend, ledger appends) locks.
            eprint(f"[panel] calling {model} ...")
            t0 = time.time()
            retry_count = 0
            response_cost = 0.0
            response_cost_recorded = False
            while True:
                status, resp = chat(transport, api_key, model,
                                    [{"role": "user", "content": prompt}], panel_tokens,
                                    reasoning_effort, reasoning_token_budget, governor)
                response_cost = _reported_cost(resp)
                response_cost_recorded = False
                if status != 429 or retry_count >= MAX_429_RETRIES:
                    break
                if response_cost:
                    governor.record_actual(response_cost, f"{model} (429 retry)")
                response_cost_recorded = True
                if ledger and task_id:
                    ledger.append("model_result", task_id=task_id, event_note="panel",
                                  model=model, task_type="structured" if run_convergence else "panel",
                                  json_expected=run_convergence,
                                  json_ok=False if run_convergence else None,
                                  status="error", cost=response_cost, retry=True)
                retry_count += 1
                eprint(f"[panel] {model} rate-limited; bounded retry {retry_count}/{MAX_429_RETRIES}.")
                time.sleep(RETRY_429_BACKOFF_SECONDS * retry_count)
            elapsed = time.time() - t0
            if status != 200:
                err = resp.get("error", {}).get("message", str(resp)) if isinstance(resp, dict) else str(resp)
                cost = response_cost
                if cost and not response_cost_recorded:
                    governor.record_actual(cost, model)
                panel_failures.append({"model": model, "reason": err,
                                       "status": status, "cost": cost,
                                        "retries": retry_count})
                if ledger and task_id:
                    ledger.append("model_result", task_id=task_id, event_note="panel",
                                  model=model,
                                  task_type="structured" if run_convergence else "panel",
                                  json_expected=run_convergence,
                                  json_ok=False if run_convergence else None,
                                  status="error", cost=cost, retries=retry_count)
                eprint(f"[panel] {model} FAILED ({status}): {err} -- rotating to next model.")
                return None
            content, finish_reason, cost, is_byok = extract_content_and_cost(resp)
            paid_byok = bool(is_byok and not governor.is_free(model))
            if paid_byok:
                governor.record_byok(model)
                panel_failures.append({"model": model, "reason": "paid BYOK route", "status": "byok",
                                       "cost": 0.0, "reported_cost": cost})
                if ledger and task_id:
                    ledger.append("model_result", task_id=task_id, event_note="panel",
                                  model=model, task_type="structured" if run_convergence else "panel",
                                  json_expected=run_convergence, json_ok=False if run_convergence else None,
                                  status="error", cost=0.0, reported_cost=cost)
                eprint(f"[panel] {model} is BYOK-routed (paid); recorded and rotating.")
                return None
            if not content or not str(content).strip():
                # A successful HTTP status is not a panel vote. Treat blank or
                # schema-less output as a bounded failure so the judge never sees
                # an unusable body and convergence cannot count it as participation.
                governor.record_actual(cost, model)
                panel_failures.append({"model": model, "reason": "empty panel output",
                                        "status": "invalid_output", "cost": cost,
                                        "retries": retry_count})
                if ledger and task_id:
                    ledger.append("model_result", task_id=task_id, event_note="panel",
                                  model=model,
                                  task_type="structured" if run_convergence else "panel",
                                  json_expected=run_convergence,
                                  json_ok=False if run_convergence else None,
                                  status="error", cost=cost, retries=retry_count)
                eprint(f"[panel] {model} returned empty output -- rotating to next model.")
                return None
            governor.record_actual(cost, model)
            eprint(f"[panel] {model}: cost=${cost:.6f}, finish_reason={finish_reason}, {elapsed:.1f}s")
            valid_claims = (bool(extract_claim_verdicts(content)) and
                            finish_reason != "length") if run_convergence else True
            if run_convergence and not valid_claims:
                panel_failures.append({"model": model, "reason": "malformed, missing, or truncated per-claim JSON",
                                        "status": "invalid_output", "cost": cost,
                                        "retries": retry_count})
                if ledger and task_id:
                    ledger.append("model_result", task_id=task_id, event_note="panel",
                                  model=model, task_type="structured", json_expected=True,
                                  json_ok=False, status="error", cost=cost)
                eprint(f"[panel] {model} returned malformed/missing claim JSON -- rotating.")
                return None
            if finish_reason == "length":
                eprint(f"[panel] WARNING: {model} truncated by --max-tokens.")
            return {"ok": True, "result": {
                "model": model, "content": content, "finish_reason": finish_reason,
                "cost": cost, "truncated": finish_reason == "length",
            }, "valid_claims": valid_claims, "cost": cost, "retries": retry_count}

    # Fan out up to `target` seats at once when the transport is safe for
    # concurrent POSTs (audit #10 latency fix); hermetic fakes opt out via
    # the same attribute so their canned ordering stays deterministic.
    _parallel = bool(getattr(transport, 'parallel_safe', False))
    panel_results = []
    panel_failures = []
    candidates = iter(panel_pool)
    tried = 0
    _futures = set()
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=target if _parallel else 1) as _pool:
        for _ in range(target):
            _first = next(candidates, None)
            if _first is None:
                break
            tried += 1
            _futures.add(_pool.submit(_run_panel_slot, _first))
        while _futures and len(panel_results) < target:
            _done, _futures = concurrent.futures.wait(
                _futures, return_when=concurrent.futures.FIRST_COMPLETED)
            for _fut in _done:
                _slot = _fut.result()
                if _slot is None:
                    if len(panel_results) < target:
                        _next_model = next(candidates, None)
                        if _next_model is not None:
                            tried += 1
                            _futures.add(_pool.submit(_run_panel_slot, _next_model))
                    continue
                if _slot["ok"]:
                    panel_results.append(_slot["result"])
                    if ledger and task_id:
                        ledger.append(
                            "model_result", task_id=task_id, event_note="panel",
                            model=_slot["result"]["model"],
                            task_type="structured" if run_convergence else "panel",
                            json_expected=run_convergence,
                            json_ok=_slot["valid_claims"] if run_convergence else None,
                            status="ok", cost=_slot["cost"], retries=_slot["retries"])
                else:
                    panel_failures.append(_slot["failure"])
                    if len(panel_results) < target:
                        _next_model = next(candidates, None)
                        if _next_model is not None:
                            tried += 1
                            _futures.add(_pool.submit(_run_panel_slot, _next_model))
    if not panel_results:
        raise HarnessError("all panel calls failed. Aborting.")

    # Context-budget guard (#14b): untruncated votes are a fidelity win but a
    # context hazard. Cap the assembled prompt at the judge model's usable
    # window when known, trimming the OLDEST panel contributions first (the
    # newest votes carry the most reliable verdicts) and never silently -- the
    # guard always reports what it dropped.
    judge_ctx = None
    try:
        if capability_profiles and judge in capability_profiles:
            judge_ctx = capability_profiles[judge].max_source_tokens
    except AttributeError:
        judge_ctx = None
    if judge_ctx:
        available = max(0, judge_ctx - estimate_prompt_tokens(prompt) - judge_max_tokens)
        budget = int(available * 0.9)
        keep = list(reversed(panel_results))
        dropped = []
        used = 0
        trimmed = []
        for r in keep:
            t = estimate_prompt_tokens(r["content"])
            if used + t > budget and trimmed:
                dropped.append(r["model"])
                continue
            used += t
            trimmed.append(r)
        if dropped:
            eprint(f"[judge] context budget {budget} tokens: dropped oldest votes "
                   f"from {dropped} to stay within {judge}'s window.")
        panel_results = list(reversed(trimmed))

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
        # Never truncate: panel verdicts are structured claims the judge must
        # weigh in full, and the preflight reserve covers their worst case.
        note = " [NOTE: cut off by token limit, may be incomplete]" if r["truncated"] else ""
        judge_prompt += f"--- Model: {r['model']}{note} ---\n{r['content']}\n\n"

    judge_content = None
    judge_cost = 0.0
    judge_synthesis_status = "not_run"
    if judge_blocked:
        judge_synthesis_status = "byok_blocked"
        eprint(f"[judge] {judge} routes via paid BYOK on this account; raw panel outputs only.")
    else:
        eprint(f"[judge] calling {judge} ...")
        status, resp = chat(transport, api_key, judge,
                            [{"role": "user", "content": judge_prompt}], judge_max_tokens,
                            reasoning_effort, reasoning_token_budget, governor)
        if status != 200:
            judge_synthesis_status = f"http_{status}"
            err = resp.get("error", {}).get("message", str(resp)) if isinstance(resp, dict) else str(resp)
            judge_cost = _reported_cost(resp)
            if judge_cost:
                governor.record_actual(judge_cost, judge)
            if ledger and task_id:
                ledger.append("model_result", task_id=task_id, event_note="judge",
                              model=judge,
                              task_type="structured" if run_convergence else "judge",
                              json_expected=True, json_ok=False, status="error",
                              cost=judge_cost, synthesis_status=judge_synthesis_status)
            eprint(f"[judge] FAILED ({status}): {err} -- raw panel outputs only.")
        else:
            raw_judge, _, judge_cost, is_byok = extract_content_and_cost(resp)
            paid_byok = bool(is_byok and not governor.is_free(judge))
            if paid_byok:
                governor.record_byok(judge)
                judge_synthesis_status = "byok_blocked"
                eprint("[judge] BYOK-routed (paid); raw panel outputs only.")
            else:
                governor.record_actual(judge_cost, judge)
                if raw_judge and raw_judge.startswith(REASONING_FALLBACK_PREFIX):
                    judge_synthesis_status = "reasoning_only"
                    judge_content = None
                else:
                    judge_content = raw_judge
                    judge_synthesis_status = "parseable" if _extract_json(raw_judge) is not None else "unparseable"
            eprint(f"[judge] synthesis status: {judge_synthesis_status}")
            if ledger and task_id:
                ledger.append("model_result", task_id=task_id, event_note="judge",
                              model=judge, task_type="structured" if run_convergence else "judge",
                              json_expected=True,
                              json_ok=judge_synthesis_status == "parseable",
                              status="ok" if judge_synthesis_status == "parseable" else "error",
                              cost=0.0 if paid_byok else judge_cost,
                              synthesis_status=judge_synthesis_status)

    consensus = _parse_consensus(judge_content) if judge_content else {
        "agreement": "unknown", "confidence": None, "disagreements": [],
        "defer": True, "verdict": "[raw panel outputs only -- no synthesis available]",
    }

    # Optional structured-claims convergence step: a dedicated specialist
    # renders the final verdict from the panel's per-claim JSON (defaults to the
    # judge model), and the deterministic tally gives the ground-truth 5/5 rate.
    convergence_spec = None
    convergence_tally = None
    if run_convergence:
        convergence_tally = tally_convergence(panel_results, claim_polarity=claim_polarity,
                                              of_panel=target)
        spec = run_convergence_specialist(
            transport, api_key, governor, panel_results, spec_model,
            max_tokens=judge_max_tokens, reasoning_effort=reasoning_effort,
            reasoning_token_budget=reasoning_token_budget, ledger=ledger,
            task_id=task_id, fallback_pool=specialist_pool)
        spec["tally"] = convergence_tally
        # The deterministic tally owns structured convergence. Responder
        # agreement and merge-gate eligibility are separate signals: a short
        # panel may be unanimously aligned while still being ineligible to
        # approve the task. This avoids reporting a transport shortfall as
        # model disagreement.
        if convergence_tally["responder_converged"]:
            consensus["agreement"] = "high"
            consensus["confidence"] = convergence_tally["convergence_rate"] or 0.0
        else:
            consensus["agreement"] = "low" if convergence_tally["disagreement"] else "unknown"
            consensus["confidence"] = convergence_tally["convergence_rate"] or 0.0
        consensus["defer"] = not convergence_tally["converged"]
        consensus["panel_shortfall"] = convergence_tally["panel_shortfall"]
        consensus["missing_votes"] = convergence_tally["missing_votes"]
        consensus["voted_by"] = convergence_tally["voted_by"]
        consensus["of_panel"] = convergence_tally["of_panel"]
        consensus["responder_converged"] = convergence_tally["responder_converged"]
        consensus["gate_converged"] = convergence_tally["converged"]
        consensus["defer_reason"] = ("panel_shortfall" if convergence_tally["panel_shortfall"]
                                      else "responder_disagreement" if convergence_tally["disagreement"]
                                      else None)
        # In structured mode, disagreements are claim-level facts, not the
        # judge's free-form severity/prose list. A shortfall is reported
        # separately and must not be mislabeled as disagreement.
        consensus["disagreements"] = list(convergence_tally["disagreement_claims"])
        # Do not let a judge's prose become the authoritative verdict for a
        # structured audit; it can be absent or semantically inverted. Keep it
        # in judge_synthesis, but expose a deterministic claim summary instead.
        consensus["judge_verdict"] = consensus.get("verdict", "")
        summary = []
        for cid, entry in convergence_tally["claims"].items():
            summary.append(f"{cid}={entry['verdict']} ({entry['voted_by']}/{entry['of_panel']})")
        if convergence_tally["panel_shortfall"]:
            s = convergence_tally["shortfall"]
            summary.append(f"panel shortfall {s['voted_by']}/{s['of_panel']}; merge gate deferred")
        consensus["verdict"] = "Deterministic panel tally: " + ("; ".join(summary) or "no defect claims")
        convergence_spec = spec

    # Print only after every planned call, including the optional specialist,
    # has settled so the human-facing total agrees with the returned result and
    # ledger evidence.
    eprint(f"\n[TOTAL] actual cost this run: ${governor.spent:.6f} "
           f"(ceiling: ${governor.max_cost:.6f})")

    consensus_payload = {k: consensus[k] for k in
                         ("agreement", "confidence", "disagreements", "defer")}
    if convergence_tally is not None:
        consensus_payload.update({
            "panel_shortfall": convergence_tally["panel_shortfall"],
            "missing_votes": convergence_tally["missing_votes"],
            "voted_by": convergence_tally["voted_by"],
            "of_panel": convergence_tally["of_panel"],
            "responder_converged": convergence_tally["responder_converged"],
            "gate_converged": convergence_tally["converged"],
            "defer_reason": consensus.get("defer_reason"),
            "judge_verdict": consensus.get("judge_verdict"),
        })
    result = {
        "panel_results": panel_results,
        "panel_failures": panel_failures,
        "required_panelists": target,
        "panel_tried": tried,
        "judge_model": judge,
        "judge_synthesis": judge_content,
        "judge_synthesis_status": judge_synthesis_status,
        "verdict": consensus["verdict"],
        "consensus": consensus_payload,
        "estimated_worst_case_cost": total_estimate,
        "actual_cost": governor.spent,
        "max_cost_ceiling": governor.max_cost,
    }
    if convergence_spec is not None:
        result["convergence"] = convergence_spec
    if ledger and task_id:
        ledger.append("complete", task_id=task_id, event_note="panel_judge",
                      model=judge, session_spent=governor.spent, status="ok",
                      agreement=consensus["agreement"],
                      voted_by=convergence_tally.get("voted_by") if convergence_tally else None,
                      of_panel=convergence_tally.get("of_panel") if convergence_tally else None)
    return result
