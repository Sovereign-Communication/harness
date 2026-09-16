# HANDOFF — Model selection, reasoning allocation, and rotation policy

**Date:** 2026-09-13
**From:** Buffy (Freebuff lane, SCMessenger recovery session)
**For:** Harness maintainers
**Operator rulings driving this (2026-09-13):**
1. "Default to the smartest models for the $" (DeepSeek V4.1 Flash named as better value than V3-era ids).
2. "Use even bigger/better models when work is hard and warrants it."
3. "Do not use gemini 2.5 pro (outdated generation) — update ALL models we are using."
4. "GLM 5.3 Flash is excellent performance for the $."
5. "Anytime we have a model fail, it's time to rotate" (rotation already native; see 4).
6. "Stop using reasoning if it's not needed here, or if it is, then appropriately allocate tokens."
7. Use the OpenRouter Daily rankings to inform rotation candidates.

All claims below were verified live this session (OpenRouter catalog, daily
rankings API, and single-call probes with actual costs). Evidence files:
SCMessenger `tmp/review/MODEL_PROBE_20260913.json`,
`tmp/review/REASONING_OFF_PROBE_20260913.json`, `tmp/review/HEAVY_PROBE_20260913.json`.

---

## 1. THE CORE DEFECT: `"off"` currently means "provider default" (reasoning ON)

`harness/chat.py`, `_effort_to_send()` (lines ~154-168):

```python
e = (reasoning_effort or "auto").lower()
if e in ("off", "none"):
    return None          # <-- BUG: omits the reasoning key entirely
```

For reasoning-native models (deepseek/*, z-ai/glm-*, moonshotai/kimi-*),
OMITTING the reasoning key = provider default = **reasoning ON**. At vote-scale
token budgets the model spends everything on hidden reasoning and returns
reasoning-only output — the exact failure that killed SCMessenger BoD runs
bod-e3238cd5 / R2b (3/5 votes) and repeatedly rotated deepseek-v4.1-flash out
of panels.

**OpenRouter contract (verified against their docs today):**
`"reasoning": {"effort": "none"}` DISABLES reasoning. `"none"` is a documented
effort value ("Disables reasoning entirely"). Some routes are `mandatory: true`
and return HTTP 400 "Reasoning is mandatory for this endpoint and cannot be
disabled" — the existing param-rejection retry already handles those correctly.

**RECOMMENDED PATCH (chat.py):**

```python
def _effort_to_send(reasoning_effort, model_id):
    e = (reasoning_effort or "auto").lower()
    if e in ("off", "none"):
        return "none"          # send explicit disable instead of omitting
    if e == "auto":
        return "low" if looks_reasoning(model_id) else None
    if e == "on":
        return "high"
    return e if e in ("low", "medium", "high") else None
```

and in `_build_reasoning_param`, when effort == "none", send
`{"effort": "none"}` (no max_tokens needed — cap is meaningless for a disable).
`_chat_reservation_slots` can stay as-is ("none" is a single-call request).

**Live proof (probe, 2026-09-13):** with `{"effort":"none"}` sent raw:
- deepseek/deepseek-v4.1-flash: 200, parseable JSON vote, 63 completion tokens, $0.000055, 1.8s (was: reasoning-only fail)
- moonshotai/kimi-k3: 200, parseable, 67 tokens, $0.0014, 2.2s (was: reasoning-only fail)
- z-ai/glm-5.3-flash: 400 mandatory-reasoning (expected; route is mandatory)
- openai/gpt-5-mini: 400 mandatory-reasoning (expected)

## 2. REASONING ALLOCATION POLICY (ruling 6)

Reasoning is a billed resource. Allocate by task shape, not by model habit:

| Task shape | reasoning_effort | max_tokens | Rationale |
|---|---|---|---|
| Structured votes / JSON extraction / classification | `off` (post-patch) or `auto` (pre-patch) | 4096 | Hidden thinking starves the visible JSON; votes are cheap decisions |
| Judge synthesis / code review / RCAs | `auto` (low for hinted ids) | 8192 | Needs some depth, bounded |
| Deep adjudication of hard/security-relevant proposals | `medium` on heavy tier | 8192+ | The "hard work warrants it" case (ruling 2) |

Panel lanes should pass the budget explicitly: vote lanes max_tokens >= 4096
proved sufficient for every model probed; 600-700 (an earlier ceiling) starved
even non-reasoning emitters into truncation.

## 3. VERIFIED MODEL SLATES (rulings 1, 3, 4, 7)

Sources: live capability cache refresh (445 ids, 2026-09-13), OpenRouter daily
rankings API (`GET /api/v1/datasets/rankings-daily`, full 30-day data pulled),
and per-call probes with actual cost. Prices $/Mtok in/out.

### Vote/panel tier (reasoning OFF post-patch; all verified parseable)
| Model | Price in/out | Verified cost/vote | Notes |
|---|---|---|---|
| deepseek/deepseek-v4-flash | 0.048/0.096 | $0.00005 | cheapest verified emitter |
| deepseek/deepseek-v4.1-flash | 0.150/0.600 | $0.000055 | operator pick; rankings #3 (1.4T tok/day, climbing from #7) — VOTE-usable ONLY post-patch |
| openai/gpt-5.6-luna | 0.200/1.200 | $0.00032 | rankings #1 by daily volume (3.9T tok/day 2026-09-12) |
| openai/gpt-4o-mini | 0.150/0.600 | $0.0002 | long-standing reliable voter |
| inclusionai/ling-3.0-flash | 0.021/0.063 | $0.00006 | cheapest input |
| openai/gpt-5-mini | 0.250/2.000 | $0.0014 | needs >=2048 budget (mandatory reasoning); verified parseable at 2048 |
| google/gemini-3.8-flash | 0.750/3.750 | $0.0022 | current Gemini generation; slow (31s) but reliable; Supersedes banned gemini-2.5-pro |
| moonshotai/kimi-k3 | 2.648/13.283 | $0.0014 | verified post-patch; high output price — judge/rotation use, not bulk votes |

### Deep-think tier (ruling 2 — "bigger/better when hard")
| Model | Price in/out | Verified behavior | Notes |
|---|---|---|---|
| z-ai/glm-5.3-flash | 0.075/0.250 | $0.0006/vote at 2048 budget; spends ~1132 tokens thinking | operator-endorsed value; rankings #5 (1.3T/day); reasoning MANDATORY — never send effort:none |
| deepseek/deepseek-v4-pro | 1.600/3.200 | dual-mode verified: effort:none -> $0.00012 clean 58-token vote | pro tier at 2.5x cheaper output than gpt-4.1 |
| openai/gpt-5.6-sol | 2.000/10.000 | $0.0026/vote | flagship-class |
| openai/gpt-4.1 | 2.000/8.000 | fast (1.5-2.7s), strong voter | keep as rotation fallback |

### DROPPED per operator rulings (evidence on file)
- **google/gemini-2.5-pro** — ruling 3 (outdated generation; also truncated under budget in two live panels).
- **openai/gpt-5** (non-mini) — superseded by gpt-5.6 line; reasoning-only output at vote budgets.
- **deepseek/deepseek-v3.2**, **deepseek/deepseek-chat (V3.1)** — superseded by V4 generation; v3.1 strictly dominated (0.257/1.029 vs 0.150/0.600).
- **ibm-granite/granite-4.0-h-micro**, **meta-llama/llama-3.1-8b** — oldest gen, weakest verified output.
- **qwen3.5-plus / kimi-k2.6** — superseded (qwen3.8-max, kimi-k3 in rankings).

## 4. ROTATION (ruling 5) — already native; two fixes worth making

Rotation IS native: `router.py` is a "cheap-first routing ladder with model
pools, rotation, and gated escalation," and `panel.py` rotates failed members
to the next pool model, with reasoning-param-rejection retries in `chat.py`.
Two evidence-backed refinements:

1. **Silent learned-BYOK filtering.** `spend.py: record_byok()` persists org
   prefixes (e.g. `google/`, `m/`) to `~/.config/harness/byok_prefixes.json`
   on the first paid-BYOK-routed response, and `panel.py` then filters those
   orgs from the pool with **no log line**. This silently shrank a 6-model
   pool to 4 seats this session and confused dispatch accounting. FIX: emit a
   visible `[panel] <model> skipped: learned-BYOK org filter` line (and the
   matching ledger event) when the filter removes a member.
2. **Ledger strike gating is opaque from outside.** `capability.py: order_pool`
   demotes/gates models after 2 unusable-output strikes — correct policy, but
   invisible to callers reconciling "why did my pool lose members?" FIX: log
   the ordered pool with strike/demotion annotations at dispatch time.

## 5. RANKINGS-DRIVEN CANDIDATE REFRESH (ruling 7)

The daily rankings API is authoritative and cheap (one GET, our existing key):
`GET https://openrouter.ai/api/v1/datasets/rankings-daily` (data rows:
`date`, `model_permaslug`, `total_tokens`, 30 days, ~1530 rows). Today's
verification confirms operator picks by traffic: deepseek-v4.1-flash #3 and
climbing, glm-5.3-flash #5, gpt-5.6-luna #1, plus gemini-3.8-flash as the
current Gemini generation. RECOMMENDATION: a periodic (weekly) job that pulls
rankings, intersects with the capability cache, probes new top-10 entrants
with the one-vote probe pattern, and files pool-change proposals — keeping
rotation candidates evidence-driven instead of folklore-driven.

## 6. IMPLEMENTATION CHECKLIST

- [x] chat.py: map "off" -> explicit `{"effort": "none"}` (section 1) + unit tests incl. mandatory-route 400 retry path. *(implemented 2026-09-14: `harness/chat.py` + `tests/test_reasoning_disable.py`)*
- [x] panel.py/spend.py: log learned-BYOK filtering and strike gating visibly (section 4). *(implemented 2026-09-14: `pool_filtered` events + stderr notes; `tests/test_pool_visibility.py`)*
- [x] Update DEFAULT_PANEL_PAID / heavy pools to the verified slates (section 3) — canonical ids validated against the 445-id cache 2026-09-13. *(implemented 2026-09-14: `harness/config.py`; policy pinned hermetically in `tests/test_model_slates.py`; live revalidation stays with `harness capabilities --check-shipped`)*
- [x] Vote lanes: max_tokens >= 4096; deep-think lanes: >= 8192 with `auto`/`medium`. *(implemented 2026-09-14: `config.effective_lane_policy` -- ONE owner. Documented deviation: the convergence specialist resolves at the 4096 vote/JSON floor, not 8192, because a synthesis-sized budget defeats the window-aware vote trim on small-context specialists.)*
- [x] Weekly rankings-refresh job (section 5) with the one-vote probe as gate. *(implemented 2026-09-14: `harness/rankings.py`, `harness rankings [--probe]`, `.github/workflows/rankings.yml` -- scheduled runs are read-only; probes are operator-gated.)*
- [ ] After chat.py patch: re-run the SCMessenger BoD panel with v4.1-flash restored to the vote pool (it is judge-only today purely because of the pre-patch behavior). *(live acceptance run -- requires the operator key; see the acceptance checklist in the repo close-out notes.)*

— Verified end-to-end this session in SCMessenger scripts/bod_governance.py:
paid panel of 5 verified emitters, 5/5 parseable votes, APPROVED 5/5
(bod-bbb49423), judge synthesis parseable, actual cost $0.0038 against the
$0.10 ceiling, with reasoning allocation per section 2.
