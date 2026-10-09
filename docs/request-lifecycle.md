# Request lifecycle: simple requests get simple executions

Issues #200-#207. Status: #200 (classifier), #201 (escalation gate), #202
(probe tier), #203 (capability preflight) implemented in the first lane;
#204 (429 circuit breaker), #205 (refusal-loop guard + phase memo), #206
(honest terminal output) implemented in the second lane
(`harness/lifecycle_guards.py`, wired into the waist ladder/re-plan and
the agent refusal renderer); #207 closes when all siblings land with the
incident replay below re-verified.

## Tiers

User-workflow tiers are separate from model-routing tiers:

| Tier | When | What runs |
|---|---|---|
| `answer` | ordinary questions, Q&A, small lookups | chat lane (existing) |
| `simple-action` | read-only single-step check with a URL/host target (e.g. site up?) | exactly one bounded probe, zero model calls |
| `plan` | repo work, multi-step execution | hourglass plan lane (existing) |

## Order of operations for every freeform request

1. **Intent** (`classify_prompt_intent`): action verb + URL/host classifies
   as `simple-action` even in question form (#200). Genuine questions stay
   conversational; repo directives stay `edit`. Question form alone never
   outranks an explicit verb plus a target.
2. **Capability preflight** (`run_simple_action`): deterministic checks
   (target present, https, no credentials, default port, all resolved
   addresses globally public) run before any model call (#203). A known
   mismatch returns an honest terminal result plus a `simple_action`
   ledger event with zero model/decomposition calls.
3. **Bounded execution** (`probe_public_site` in `harness/web.py`): one
   HTTPS probe, ~8s budget, DNS pinned through connection, TLS hostname
   validation, no redirects followed, no cookies/auth, small body cap
   (#202). No repo triage, DAG, or waist work. Verdicts: `up` (2xx),
   `denied` (401/402/403/407 — host reachable), `redirect` (3xx reported,
   destination not claimed), `timeout`, `network`, `refused` (policy).
4. **Escalation gate** (`_escalation_authorized`): `auto_apply=True` alone
   never authorizes plan entry; an explicit execution directive or an
   armed paid key plus a trigger (defer marker, `plan_required`, explicit
   directive) is required (#201).
5. **Honest result**: the verdict comes from observed probe evidence, never
   from search snippets. Search results are not uptime evidence.

## Incident replay (2026-10-07 uptime check)

Prompt: "check if freeoffgridcalculator.com is up - respond yes/no".

- Before: classified `conversation`, entered the answer loop, escalated to
  the plan lane on the vacuous `auto_apply` gate, re-planned on repeated
  missing-web refusals — 65+ model calls, minutes, user-cancelled, zero
  value.
- After: classified `simple-action`, one bounded probe, honest answer in
  ~2s with zero model calls. Live receipt 2026-10-09: `denied`
  (HTTPS 403 in 0.38s — host reachable, page access denied), matching the
  prior bounded-GET observation recorded in the canon. Do not treat that
  receipt as current status on later runs; re-probe.

## Hermetic coverage

`tests/test_request_lifecycle.py` (no network, no model spend): classifier
neighbors on both sides, probe status/timeout/redirect/SSRF/cancel
 verdicts, detached-probe honest failure with zero model calls, GUI
 (`force_conversation`) path selecting `simple-action`, and gate clauses
 (`auto_apply` alone never authorizes).
