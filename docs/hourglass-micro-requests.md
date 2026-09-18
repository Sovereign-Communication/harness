# Micro-request split of the frontier eval (token-constrained mode)

Split of `docs/hourglass-frontier-eval.md` into perfectly scoped questions.
Run each as its OWN fresh chat session (no repo access needed — every block
is self-contained). Answer contracts cap output; context caps input.

**Orchestration = the repo's own pattern, applied to this eval:**
- **Gate:** a request "passes" only if its answer is actionable (a verdict,
  a schema, or a named decisive reason — never prose).
- **Consensus:** run tier-F requests twice on a cheap capable model; agreeing
  answers accept; disagreement escalates once to the frontier model (the
  `router.de_escalate_to_rung` / `EscalationDriver` shape).
- **DAG order:** MR-0 → MR-1 → MR-2, MR-3 (gate: their verdicts reorder the
  roadmap) → MR-4..MR-8 in parallel.
- **Budget:** full split ≈ 15-20K tokens in / ~1.5K out across 9 sessions,
  vs ~6K in / unfocused 3-6K out for one monolith prompt — and each answer
  is directly usable.

| ID | Question | Executor | Budget (in/out) |
|---|---|---|---|
| MR-0 | Does a paid tier-0 node with a ~500-line file refuse at the consent probe because consent actuals can exceed the $0.01 tier ceiling? | **local hermetic probe — free** | 0 / 0 |
| MR-1 | Does the shipped per-node routing diff violate any cost/consent guarantee? | frontier, 2× cheap consensus | 2K / 120 |
| MR-2 | Is "never pass a $0 free-tier ceiling as task_max_cost" right? | frontier, 2× cheap | 1.2K / 100 |
| MR-3 | **Does the tier heuristic get to move money before the planner is smart (M1 vs M2 order)?** ← single most important | frontier, 2× cheap + tiebreak | 1.5K / 150 |
| MR-4 | Waist plan-verdict JSON contract | frontier once | 2.5K / 500 |
| MR-5 | Isolation partition rule (shared-tree vs worktree per node) | frontier once | 2K / 250 |
| MR-6 | Governor reservation semantics under parallel workers | frontier once | 2K / 200 |
| MR-7 | Red-team, one guarantee per session (ceilings/BYOK; consent+ledger; brief poisoning) | frontier once each | 1.5K / 100 ×3 |
| MR-8 | What is missing to make this file a reusable `harness brief`? | cheap once | 1K / 80 |
| MR-9 | Draft the contract for diff-bound independent authorization before write (M4) | frontier once | 2.5K / 500 |

---

## MR-0 — local hermetic probe (run by the agent/here, no tokens)

**Claim to test:** on the paid tier, a tier-0 node's $0.01 ceiling is also
the consent probe's budget (`apply_policy._initial_consent` raises
"consent cost exceeded task ceiling" when actuals exceed it). A ~500-line
target makes the consent prompt token-heavy enough that a paid judge could
bill > $0.01 → the node dies at consent, before any edit. If true, the tier
ceiling needs a consent reserve (or consent exempted from the task budget).

**Method:** hermetic unittest with `tests/_fake.py`'s FakeTransport: pricing
that makes a 5K-token consent probe cost $0.011; apply_edit with
`task_max_cost=0.01`; assert whether HarnessError("consent cost exceeded")
raises. Free to run; report the observed behavior.

---

## MR-1 — review the shipped diff against the guarantees (paste-ready)

You are reviewing ONE small diff against five stated invariants. Answer in
≤120 words: verdict PASS or VIOLATION + reasons. Do not restate the diff.

Invariants: (1) no network call without a worst-case preflight inside the
governor; (2) actual spend re-checked against the task ceiling after every
call, fail-closed; (3) routing is per-request — interfaces may pass a pool
but never pre-order it; the engine orders via capability evidence;
(4) malformed/absent route data must degrade to previous behavior, never
fail open; (5) an operator's explicit pin (model / task-max-cost) is never
overridden by tier policy.

```python
def node_apply_kwargs(node_detail=None, explicit_model=None,
                      explicit_task_max_cost=None):
    """Per-request apply kwargs for one planned DAG node. Returns kwargs
    for apply_edit: tier ladder as apply_pool (engine orders it at the
    routing boundary), tier cost ceiling as task_max_cost on paid tiers
    only (a $0 free-tier ceiling is NOT passed: a zero task budget would
    refuse the escalation ladder for hard nodes, spent-start<0.0 is never
    true; free-tier cost discipline is the governor's). Explicit model pin
    wins outright ({}); explicit task-max pin suppresses only the
    ceiling."""
    if explicit_model is not None:
        return {}
    detail = node_detail or {}
    route = detail.get("route") or {}
    kwargs = {}
    ladder = [str(m).strip() for m in (route.get("ladder") or []) if str(m).strip()]
    if ladder:
        kwargs["apply_pool"] = ladder
    if explicit_task_max_cost is None:
        try:
            ceiling = float(route.get("cost_ceiling"))
        except (TypeError, ValueError):
            ceiling = 0.0
        if ceiling > 0.0:
            kwargs["task_max_cost"] = ceiling
    return kwargs
```

Call sites (all three identical in spirit): `run_node` looks up the planned
node detail by node_id, calls the above, passes `apply_pool` through and
binds the returned ceiling over the unset explicit `task_max_cost=None`
(never over a set one); MCP passes `apply_pool` only; the agent lane shares
one route_kwargs between its attempt and healing-retry calls.

## MR-2 — free-tier ceiling policy (paste-ready)

You are deciding ONE cost-policy question. Answer in ≤100 words: verdict +
≤2 reasons. No restatement.

Context: a coding harness routes task nodes to tier ladders. Tier ceilings
on the paid tier bind the per-task budget (today: scout $0.01, standard
$0.04, frontier $0.10). On the free tier every model bills $0.00, so the
computed tier ceiling is 0.0 — and the implementation deliberately does NOT
pass a $0.0 per-task budget, because the engine refuses any state where
`spent_so_far > budget` and would refuse the escalation ladder it needs to
rescue a hard node (escalation would need negative spend). Instead free-tier
discipline is enforced upstream: every attempt is preflighted and billed
$0.0 against the governor's key-level ceiling.

Question: is omitting the $0.0 ceiling correct, or should free-tier nodes
get an explicit escalation budget knob (e.g. allow N paid-escalation
dollars per node, default 0, operator-set)? Verdict: "omit is correct" or
"add the knob".

## MR-3 — the gating question: does the heuristic get to move money? (paste-ready)

You are answering ONE narrow question about a cost-routing design. Do not
restate the context; answer in ≤120 words.

Context: In Harness, a keyword heuristic classifies each decomposed task
node into tiers: tier 0 (scout, $0.01 ceiling), tier 1 (standard, $0.04),
tier 2 (frontier, $0.10). Classification starts at 0.20 and adjusts:
frontier keywords +0.25 each (cap +0.50); standard keywords +0.10 each
(cap +0.30); scout keywords −0.15 each (cap −0.30); >3 target files +0.25;
>150-line churn +0.25; dependency depth +0.10/+0.20; each prior failure
+0.30 (cap +0.60). Score ≥0.65 or ≥2 failures → tier 2; ≥0.35 → tier 1;
else tier 0. As of today the chosen tier's model ladder and ceiling are
actually ENFORCED at execution (previously computed and discarded). A
planned "waist" step would have a frontier model confirm/repair the whole
plan before execution (M2); wiring a cheap-model LLM decomposition with
schema validation is M1.

Question: given this heuristic now decides which ladder spends real money,
what is the highest-probability misclassification that matters (a
tier-2-worthy node silently running on a tier-1 budget), and does that
make M1 a strict prerequisite for M2 — or can M2 safely ship on the
heuristic? Verdict: "M1 first" or "M2 on heuristic is safe", with the
single decisive reason.

Answer contract: verdict + ≤3 bullets. No code. No restatement.

## MR-4 — waist plan-verdict contract (paste-ready)

You are designing ONE JSON contract. Output the schema + one filled example
only, ≤40 lines total, no commentary.

Context: a planning DAG's nodes look like `{"node_id": "task_1",
"instruction": "...", "target_files": ["a.py"], "dependencies": [],
"local_gate": "python -m py_compile a.py", "complexity_tier": 1}`. Before
execution, a frontier model receives a condensed brief (signatures, focused
file windows, grounded claims, explicit unknowns) and must return a verdict
that either approves the plan or repairs it. It may request at most K=2
rounds of additional file windows instead of open-ended reading.

Design the verdict schema: approve | amend (amended nodes) | split
(node replaces/subdivides) | refuse (reason), plus file-window requests and
a bounded rationale. Constraints: strict JSON parseable hermetically; every
amended field re-validates through the same schema as the original; a
refusal must be honest-evidence (cite the brief section that fails), never
vibes.

## MR-5 — isolation partition rule (paste-ready)

You are deciding ONE execution-isolation policy. Answer in ≤150 words: rule
+ the one edge case that breaks it. No restatement.

Context: a DAG executor runs independent nodes in parallel threads over one
working tree; a per-path mutex (process-internal, sorted acquisition)
prevents same-file write races but not semantic interference (two green
nodes, red composition). No VCS assumptions beyond git being installed; PR
creation must be opt-in; stdlib only (git via subprocess is allowed as host
tooling). Nodes carry declared `target_files` only.

Question: define the partition rule — when do parallel nodes share the
tree (locks only) vs get a `git worktree` + branch each? Default lean:
partition by declared-file overlap; worktree only for overlap-free parallel
groups; shared tree otherwise. Confirm or replace, and name the edge case
(undeclared writes? shared config/test files? merge ordering?) that most
threatens the rule.

## MR-6 — composed ceiling under parallelism (paste-ready)

You are reviewing ONE concurrency-safety design. Answer in ≤120 words:
verdict + the single required proof obligation (as a hermetic test name +
assertion). No restatement.

Context: a SpendGovernor enforces: preflight(prompt, [(label, model,
max_tokens, 0)...]) reserves worst-case per call before dispatch;
record_actual(amount, model) after; hard key ceiling refuses everything
once spent would exceed it. A parallel DAG executor (ThreadPoolExecutor,
per-stage batches) dispatches up to W worker calls concurrently, each of
which preflights then records. Panel code already serializes concurrent
calls via reservation slots (`_chat_reservation_slots`). Question: must the
governor add per-model reservation slots for the DAG executor (reserve W ×
per-call worst case at stage start, reconcile after), or is thread-safe
per-call preflight+record sufficient? Name the exact interleaving that
breaks the weaker option if any.

## MR-7 — red-team, one guarantee per session (three paste-ready blocks)

You are red-teaming ONE guarantee of a cost-bounded coding harness. Answer
in ≤80 words: the cheapest attack + the control that stops it (or "accepted
residual"). No restatement.

**MR-7a (ceilings/BYOK):** guarantees — worst-case preflight before any
network call; actuals re-checked per call; BYOK (bring-your-own-key) routes
are learned + denylisted org prefixes; key must carry a finite spend limit.
Attack: how does a malicious or confused MODEL (not user) cause untracked or
ceiling-exceeding spend?

**MR-7b (consent + ledger):** guarantees — consent probe (accept/decline/
defer/redirect) precedes dispatch and renews per round; unparseable consent
fails closed to defer; every decision lands in an append-only hash-chained
JSONL ledger (tamper-EVIDENT). Attack: how does a model get unconsented
work applied, or make the ledger lie?

**MR-7c (brief poisoning):** upcoming design — a condensed "brief"
(signatures, focused file windows, fetched-web sources with
cite-only-these) is escalated to a frontier model that confirms/repairs the
plan without reading the repo. Attack: how does an adversary (web content,
a prior cheap model's output, target-file contents) poison the brief so the
frontier model approves a harmful plan? Name the one control that matters
most.

## MR-8 — `harness brief` gap check (paste-ready)

You are reviewing ONE artifact for reuse. Answer in ≤80 words.

Context: the attached file (docs/hourglass-micro-requests.md) is a
hand-built context pack: role line, self-contained per-question blocks,
answer contracts, budget table. A CLI feature `harness brief` should
generate such packs automatically for any goal/model pair.

Question: which ONE section is missing that would most break automated
reuse (freshness/date anchoring? grounding rules for the pack's own claims?
size budget? machine-readable answer contract?), and what is its minimal
shape?

---

## MR-9 — diff-bound independent authorization before write (paste-ready)

You are drafting ONE verdict contract for a coding harness. Answer with the
contract ONLY: strict JSON schema (field names, types, required/optional,
one-line semantics each) + the 3 binding rules that make it fail-closed. No
prose around it. <=500 words.

Context: two independent red-team tracks converged on the same missing
control. Today a "sovereign" consent probe accepts INTENT (path,
instruction, content-so-far) before each round, but the bytes actually
written are whatever the apply model produces — the final content is never
sovereign-seen — and a poisoned "brief" can launder a harmful requirement
into the plan the frontier model approves. The control that survives both
attacks: a verifier OUTSIDE the brief pipeline authorizes the EXACT
proposed diff, fail-closed, attestation bound to the diff hash; missing
evidence = rejection.

Question: draft that contract. It must cover: who may attest (independent
of the brief pipeline and of the apply model), what is attested (the exact
diff hash — never a summary), when the attestation is checked (before
write, every round), and what happens on missing/malformed/stale
attestation. Keep the schema small enough to validate with the repo's
existing strict-JSON parsers.

---

*Answers flow back into `docs/hourglass-frontier-eval.md` §5's output
format; the operator integrates; local execution (MR-0 and any code change)
stays in this repo per its own gates (hermetic tests, ruff, audit).*

---

## Results (2026-09-17, run through Harness itself)

Executed via a tmp driver over `harness.chat.chat()` (governor guards
intact, per-model $0.05 ceiling): Astra `openai/gpt-6-astra` $0.026344,
Grok `x-ai/grok-4.6` $0.025839, Fable `anthropic/claude-fable-5.1`
$0.035323 — **total $0.0875** (cap $0.25; every call under $0.05).
Fable ran direct (BYOK disabled account-wide by the operator; response
`is_byok=False` verified post-hoc; the hard denylist was bypassed in the
tmp driver ONLY, repo code untouched).

- **MR-0 (local hermetic probe): CONFIRMED.** With consent required, a
  consent probe billing above the task ceiling (3000-token probe at $5/M
  vs a $0.01 ceiling) raises `consent cost exceeded task ceiling;
  refusing to dispatch` — the node dies at consent. No impact on the
  shipped M0 wiring (plan/agent/MCP lanes pass `require_consent=False`),
  but an **M2 design constraint**: per-dispatch consent under per-node
  tier ceilings needs consent cost exempted from the task budget or the
  ceiling must reserve consent headroom.
- **MR-3 (the gating question): judged "M2 first, conditional."** Astra:
  "M2 on heuristic is safe" + condition (execution must honor waist
  promotions — true since M0). Grok: "M1 first" — decisive reason
  refutable (M2 runs pre-execution, so it CAN re-tier). Fable: verdict
  slot empty (reasoning-only response, which the repo's own judge
  discards), but the trace agrees with Astra and adds the strongest
  insight: the failure-escalation ladder (+0.30/failure, 2 failures ->
  tier 2) bounds any misclassification's downside to one wasted cheap
  attempt. 2-1 on verdict, mechanism verified in `sliding_scale.py`.
- Remaining MRs: all of MR-0 through MR-8 have run; results below. MR-9
  (M4 contract draft) is the only open paste-ready block.
- **M1 + M2 shipped same day** (commit `5cc9067`): `--decompose-llm` and
  `--confirm` are live on CLI and MCP, per the MR-3 verdict's conditions.
- **MR-5 + MR-6 consensus obtained and implemented (M3).** Both questions
  ran through Harness as consensus pairs (Astra + Grok, independent
  families, ~$0.05/call): **MR-5** converged on isolation-by-default for
  concurrent nodes (worktree+branch each; serial nodes share the tree;
  topological-order merges with a stable tiebreak; undeclared-write audit;
  conflict → discard, never force-merge) — implemented as **opt-in**
  `--isolate` (a deliberate deviation: real git subprocesses are not
  hermetic, so the default stays shared-tree mutex execution; the rule is
  fully live when enabled). **MR-6** converged unanimously on
  stage-start/real reservations ("stage-start slots required", same
  overspend interleaving named by both) — implemented as
  `SpendGovernor.reserve`/`reconcile` with the outstanding liability
  counted by every preflight, wired into parallel `execute_dag` via
  `NodeReserver`; the proof-obligation test
  (`test_concurrent_dispatch_cannot_overcommit_ceiling`) pins that spent +
  outstanding never exceeds the ceiling. MR-5's stage-gate recommendation
  shipped as `--stage-gate <cmd>` (composed-tree gate after each parallel
  stage; failure stops before dependents).
- **MR-1 (shipped routing diff vs the invariants): VIOLATION, fixed same
  day.** Astra (run 2026-09-18, $0.0225) named invariant (4) broken for
  malformed route data: truthy non-mapping detail/route crash on `.get()`,
  a string ladder silently becomes per-character "model ids", a
  non-iterable ladder raises, and an `inf` ceiling passes through as an
  unbounded task budget. All four confirmed by a $0 hermetic probe against
  the shipped code (unreachable through the plan lanes — routes are
  repo-built and amend re-classifies — but the public seam broke its own
  docstring contract). `node_apply_kwargs` now degrades every malformed
  shape to `{}` (previous behavior; the governor's key-level ceiling still
  binds), pinned by
  `test_node_apply_kwargs_malformed_route_degrades_never_fails_open`. A
  second frontier opinion was skipped per the $0.25 cumulative cap: for a
  mechanically checkable claim the hermetic probe is the stronger check.
- **MR-2 (free-tier ceiling policy): consensus — "omit is correct."**
  Grok ($0.0076) and Astra ($0.0152), independent families, agreeing: a
  $0.0 per-task ceiling would block the escalation ladder without adding
  protection (every free-tier attempt is preflighted and billed $0.0
  against the governor's key-level ceiling), and a paid-escalation knob is
  a separate paid-fallback policy — warranted only if free-tier nodes
  should ever invoke paid models by intent. No code change; the knob idea
  stays open as the R1 follow-up in docs/hourglass-frontier-eval.md.
- **MR-7 / MR-8: run 2026-09-18 after the operator raised the spend cap**
  (the earlier free-tier first pass had failed the actionability gate —
  unrelated content, reasoning-only responses, 429s — and was discarded).
  One frontier call per track, driver governor intact, every response
  `byok=False` verified: MR-7a Astra $0.0243, MR-7b Grok $0.0116, MR-7c
  Astra $0.0155, MR-8 Grok $0.0385 — **$0.0900 total**.
- **MR-7a (ceilings/BYOK): attack already closed by M3.** Astra named the
  parallel preflight race — two concurrent nodes each pass a preflight
  against the full remaining ceiling before either charge posts. That is
  exactly the MR-6 interleaving: `SpendGovernor.reserve` now holds each
  node's worst case before dispatch, every preflight counts outstanding
  liability, and `test_concurrent_dispatch_cannot_overcommit_ceiling`
  pins spent + outstanding ≤ ceiling. The attack text confirmed the
  pre-M3 guarantee set was insufficient; the shipped M3 synchronization
  is the named fix.
- **MR-7b (consent+ledger): REAL gap — consent is intent-level, not
  payload-level.** Grok's bait-and-switch: the sovereign accepts (path,
  instruction, content-so-far) but the written bytes are whatever the
  apply model later produces; renewal re-probes BEFORE each round's
  model call, so final content is never sovereign-seen. The ledger
  truthfully records accepts of intent — it "lies about what was
  approved" without any tampering. Also noted: the hash-chained ledger
  is tamper-EVIDENT only against an external snapshot (already
  documented as a deliberate scope line).
- **MR-7c (brief poisoning): control named independently — and it
  CONVERGES with MR-7b.** Astra: the one control that matters is
  fail-closed, independent authorization of the exact proposed diff
  (not the brief): a verifier outside the brief pipeline inspects the
  real affected code, approval is attested and bound to the diff hash,
  missing evidence = rejection. Poisoning mechanism named: semantic
  laundering — a poisoned input becomes a cheap model's "established
  requirement" summary, cited while the contradicting contract sits
  outside the window; no provenance delimiter defeats it. Two
  independent tracks naming the same control ⇒ the next design
  milestone is **diff-bound independent authorization before write**
  (consent/review v2); contract drafting is MR-9 work, not improvised.
- **MR-8 (`harness brief` gap check): missing section = grounding rules
  for the pack's own claims.** Grok: without them an auto-generated
  brief injects unsourced narrator facts; size budget and prose
  contracts already exist, ledger dates do not bind claims. Minimal
  shape: a required `grounding` object — `sources: [{id, path, sha,
  span|quote}]`, `claims: [{text, source_ids}]`, `unknowns: [str]`;
  uncited assertions are invalid (drop or list as unknowns); models may
  use only cited windows. This is the spec seed for the `harness brief`
  builder.
