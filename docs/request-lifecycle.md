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
| `answer` | ordinary questions, Q&A, stable small requests | bounded simple-answer lifecycle; deterministic current clock questions use the machine clock directly |
| `simple-action` | read-only single-step check with a URL/host target (e.g. site up?) | public-target preflight, Jev Choice, one bounded probe, one typed Jev Noul; zero OpenRouter calls |
| `workspace-inspection` | an answer or review grounded in the selected local repository | bounded local file/AST context and answer review; no web retrieval or writes |
| `plan` | repo work, multi-step execution | hourglass plan lane (existing) |

## Literal greeting shortcut

A narrowly matched command whose entire requested output is a greeting (for
example, `test - say hello and stop`) returns `Hello.` before intent, web, or
model routing. The result costs zero and is saved in chat history. Questions
or prompts with additional requested content keep their normal route.

## Stable simple answers and clock requests

Stable, no-tool questions enter `run_simple_request`: it keeps only relevant
recent history, asks Jev for a bounded answer strategy, performs at most one
generation call, then asks Jev to review the answer when time permits. Jev
outage degrades to a direct answer with fallback provenance; it does not
trigger repo intake, search, or planning. Requests that require current data,
tools, file access, or execution stay on their specialized lane.

A native Jev answer-sufficiency score at or above the configured threshold,
with no plan or iteration signal, is the terminal review for that answer.
The hourglass does not call a second generative completion judge after a Jev
pass. It makes another answer attempt only when Jev explicitly requests one,
and the round limit stays bounded. A low score without an iteration request
returns the candidate with an honest `needs_iteration` result instead of
spending another answer call speculatively. A useful answer returned during a
Jev outage is labeled unverified.

Questions such as “audit this repo” route to `workspace-inspection` before the
ordinary-answer lane. That route reads only the selected workspace and builds
the bounded local brief; it does not search the web. The explicit local-workspace
signal overrides an advisory web route from Jev, while Jev still reviews the
generated answer against the retained brief. A file-specific question gets a
targeted brief (up to 8 files / 1,800 estimated tokens); a repository-wide
review gets a balanced sample of up to 20 guidance, design, manifest, source,
and test files (up to 4,800 estimated tokens). Its prompt context includes a
bounded repository inventory and tells the model to state which areas were not
inspected. Neither profile sends the full repository to a model or claims an
exhaustive audit.

If chat execution is cancelled or interrupted, Harness writes a bounded,
atomic checkpoint under the session's `chat_checkpoints` directory. It retains
the original prompt, selected route, completed stage, bounded context and
provenance, any generated candidate, known spend, and unknown-usage reserve.
An exact “continue” resumes the interrupted stage; a cancelled review reuses
the saved answer rather than generating it again. Repository context is
revalidated against the selected root, relevant file set and file hashes;
retained web evidence expires after five minutes. A new prompt supersedes that
session's checkpoint. “Continue” with no saved interruption returns locally
without a provider call and asks for the task to be restated.
Answer-shaped imperative arithmetic and unit conversions (such as “calculate,”
“compute,” and “convert”) use this same bounded answer lane. The structured
`post_review_passed` flag is true only for a native Jev pass with sufficient
answer support and no plan or iteration request.

A self-referential question about the current date or time is answered from
the machine clock before provider setup. An explicit timezone abbreviation
such as HST uses its fixed UTC offset and is named in the response, which
states that no lookup was run. This path has
zero model, Jev, and network calls. Questions about event times (for example,
when a meeting starts) remain ordinary requests.

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

The workspace-route, early-stop, and resumable-chat contracts are covered by
`tests/test_jev_answer_lifecycle.py`, `tests/test_simple_request_lifecycle.py`,
and `tests/test_chat_checkpoint.py`. These are a targeted #207 follow-up; they
do not replace the outstanding full small-task matrix or establish GUI/CLI/MCP
surface parity by themselves.
