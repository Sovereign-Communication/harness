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
| `simple-action` | read-only single-step check with a URL/host target (e.g. site up?) | public-target preflight, Jev Choice, one bounded probe, one typed Jev Noul; zero OpenRouter calls |
| `plan` | repo work, multi-step execution | hourglass plan lane (existing) |

## Literal greeting shortcut

A narrowly matched command whose entire requested output is a greeting (for
example, `test - say hello and stop`) returns `Hello.` before intent, web, or
model routing. The result costs zero and is saved in chat history. Questions
or prompts with additional requested content keep their normal route.

## Order of operations for other freeform requests

1. **Intent** (`classify_prompt_intent`): action verb + URL/host classifies
   as `simple-action` even in question form (#200). Genuine questions stay
   conversational; repo directives stay `edit`. Question form alone never
   outranks an explicit verb plus a target.
2. **Capability and safety preflight** (`preflight_public_site`): deterministic
   checks (target present, https, no credentials, default port, all resolved
   addresses globally public) run before any Jev call (#203). Resolved
   addresses are pinned and reused for the request. A known
   mismatch returns an honest terminal result plus a `simple_action`
   ledger event with zero Jev, OpenRouter, search, or decomposition calls.
3. **Jev workflow Choice** (`JevPolicy.evaluate_request_workflow`): one
   typed Choice selects among `answer`, `simple-action`, and `plan`. Only a
   `simple-action` choice proceeds; another choice or fallback sends no probe
   and does not escalate to OpenRouter or planning.
4. **Bounded execution** (`probe_public_site` in `harness/web.py`): one
   HTTPS probe, ~8s budget, DNS pinned through connection, TLS hostname
   validation, no redirects followed, no cookies/auth, small body cap
   (#202). No repo triage, DAG, or waist work. Verdicts distinguish `up`
   (2xx), `denied` (401/402/403/407), `redirect` (3xx reported, destination
   not claimed), `rate_limited` (429), `server_error` (5xx), other HTTP
   errors, `timeout`, `network`, and `refused` (policy).
5. **Jev judgment and honest result** (`JevPolicy.evaluate_site_reachability`):
   one typed Noul judges whether the bounded probe received an HTTP
   response. The evidence includes the exact status, its class, the
   code-owned probe verdict, and a short status summary; it never treats a
   4xx/5xx response as proof of page access or origin health. A probability
   >=0.99 may answer Yes; <=0.01 may answer No only when the probe got no HTTP
   status; all other values or missing/fallback judgments remain inconclusive.
   Binary prompts render only `Yes.` or `No.` to the user; the exact HTTP
   status, probe verdict, timing, and Jev probability remain in structured
   result data and the GUI progress trail.
   Jev cannot override target refusal or turn a transport failure into proof
   of downtime. Search results are not uptime evidence. The selected
   simple-action path makes at most two Jev requests, each with a 10s timeout
   and no transport retry, and never calls OpenRouter or a plan lane.
6. **Escalation gate** (`_escalation_authorized`): `auto_apply=True` alone
   never authorizes plan entry; an explicit execution directive or an
   armed paid key plus a trigger (defer marker, `plan_required`, explicit
   directive) is required (#201).

## Incident replay (2026-10-07 uptime check)

Prompt: "check if freeoffgridcalculator.com is up - respond yes/no".

- Before: classified `conversation`, entered the answer loop, escalated to
  the plan lane on the vacuous `auto_apply` gate, re-planned on repeated
  missing-web refusals — 65+ model calls, minutes, user-cancelled, zero
  value.
- After: a keyed request passes public-target preflight, Jev Choice selects
  `simple-action`, one bounded probe runs, and Jev Noul judges the returned
  status; no OpenRouter call or plan handoff. The latest authenticated GUI
  replay on 2026-10-09 returned HTTP 403 in 0.38s; Jev Choice selected
  `simple-action`, Jev Noul returned 0.99, and the user-facing answer was
  `Yes.` End-to-end time was 5.41s for two Jev calls and one HTTP probe. The
  endpoint was reachable but denied page access. This is local, unmerged
  evidence and does not establish page health or future availability.

## Hermetic coverage

`tests/test_request_lifecycle.py` (no network or provider spend): classifier
neighbors on both sides, Jev Choice routing and Jev 0.99/0.01/abstain thresholds, missing/malformed
Jev, probe status/timeout/redirect/SSRF/cancel verdicts, detached-probe
honest failure with zero model calls, GUI (`force_conversation`) path
selecting `simple-action`, and gate clauses (`auto_apply` alone never
authorizes). `tests/test_jev.py` pins the one-shot timeout behavior.
