# Closing report — issue program, batch 1

> **Historical snapshot:** this report records the 54-open state before #276
> landed (`f54f8aa`). Current inventory: #295. The #276 row below is no longer
> accurate as an open item; see [`276.md`](276.md) for its receipt. #331 also
> landed later (`52f51b9`, PR #371); its receipt is [`331.md`](331.md), and
> #277's named prerequisites are now complete.

**Date:** 2026-10-25  
**Repository state at this report's cut:** `main` `4155b687`; merge-result CI [run 40145105145](https://github.com/Sovereign-Communication/harness/actions/runs/40145105145) 12/12 green.  
**Open issues:** 54. **Waved:** 29. **Not yet waved:** 25.  
**Execution view:** [`queue.json`](queue.json) (58 generated, 4 complete, 54 queued).

This is the campaign accounting: what landed with receipts, every issue closed with its disposition, and one line per open issue naming its wave, blocker and unblocker. It is deliberately blunt about the difference between a queue position and a hold.

## Landed list

All five are merge commits on `main`. `#339` was campaign evidence outside the 58-issue queue; the other four were queue items.

| Issue | Merge commit | Landed as | Receipt |
|---|---|---|---|
| [#339](https://github.com/Sovereign-Communication/harness/issues/339) | `76a95f6` | PR #339 | Efficiency evidence: $0.01209 spent against $0.10 authorized; 174/175 requests succeeded; peak deepseek pricing avoided. |
| [#330](https://github.com/Sovereign-Communication/harness/issues/330) | `0737e62` | PR #364 | Dogfood `cancel_check` forwarded into the verify phase; 12/12 dogfood and 99/99 affected tests. |
| [#347](https://github.com/Sovereign-Communication/harness/issues/347) | `4d9fea4` | PR #365 | Config/Jev precedence fixed; a disabled Jev preview makes zero provider calls; 9 regression tests. |
| [#327](https://github.com/Sovereign-Communication/harness/issues/327) | `8c4324a` | PR #366 | Checkout-root gate resolution; 13 tests; foreign-CWD `compileall` compiles the real tree. |
| [#278](https://github.com/Sovereign-Communication/harness/issues/278) | `4155b68` | PR #367 | One external-tool resolver and strict preflight; 84 focused tests; full suite 5,080 (72 skipped); audit 10.00; D12 98%. |

## Closed issues and dispositions

| Issue | State | Merge | Disposition |
|---|---|---|---|
| #339 | closed | `76a95f6` (PR #339) | Implemented and merged as efficiency evidence. No separate close comment existed on the issue, so this row is its disposition record. The live run stayed inside the $0.10 authorization and selected the cheaper apply model; the receipt is the issue body and commit, not a projection. |
| #330 | closed | `0737e62` (PR #364) | Implemented and merged. `_run_claims_verify` accepts and forwards `cancel_check`, and a contract test pins the verify phase so it cannot silently become unreachable again. No close comment existed; this row supplies the disposition. |
| #347 | closed | `4d9fea4` (PR #365) | Implemented and merged; the issue carries a closure receipt comment. Explicit Jev override precedence is fixed and a disabled preview makes zero provider calls (9 regressions). |
| #327 | closed | `8c4324a` (PR #366) | Implemented and merged; the issue carries a closure receipt comment. Gate argv and paths resolve from the repo root, missing targets are refused, and 13 tests cover the foreign-CWD case. |
| #278 | closed | `4155b68` (PR #367) | Implemented and merged; the issue carries a closure receipt comment. 84 focused tests, 5,080-test full suite (72 skipped), audit 10.00, D12 98%. Policy v1 returned `investigate` on rule 6; the documented disposition is `proceed with note` because the investigation found no actual risk. |

No other issue was closed during this campaign. The tracker #295 remains open by design.

## Open issues by wave

A prerequisite is a blocker; a wave is order, not a hold. Only #333, #306 and the Wave 5 operator flag are explicit holds.

### W0 — finish the correctness base (0 measured sessions) (2 open)

| Issue | Blocker | What unblocks it |
|---|---|---|
| [#276](https://github.com/Sovereign-Communication/harness/issues/276) fix(audit): measure D12 coverage against the merge-base at CI time; disposition S5 ruff resolution | No open prerequisite; merely queued. | Queue it. |
| [#277](https://github.com/Sovereign-Communication/harness/issues/277) dogfood: structured round 2 - checklist pass with filed report (GUI/MCP/website weighted) | blocked by #276 | Land #276. |

### W1 — unblock contributors and make failures loud (8 open)

| Issue | Blocker | What unblocks it |
|---|---|---|
| [#279](https://github.com/Sovereign-Communication/harness/issues/279) [cli] harness doctor: one-command environment preflight | No open prerequisite; merely queued. | Queue it. |
| [#284](https://github.com/Sovereign-Communication/harness/issues/284) [mcp] Tool-description lint: When/What-in/What-out formula, wired into driver_core audit | No open prerequisite; merely queued. | Queue it. |
| [#286](https://github.com/Sovereign-Communication/harness/issues/286) [mcp] Structured file logging + additive-only tool versioning policy | No open prerequisite; merely queued. | Queue it. |
| [#287](https://github.com/Sovereign-Communication/harness/issues/287) [ci] cancel-in-progress + paths filters: skip the hermetic matrix for docs/site-only changes | blocked by #276 | Land #276. |
| [#290](https://github.com/Sovereign-Communication/harness/issues/290) [docs] Hermetic/audit triage runbook: environment failures vs real failures | blocked by #276 | Land #276. |
| [#299](https://github.com/Sovereign-Communication/harness/issues/299) fix(server): cap POST body size before reading — return 413 over the limit | No open prerequisite; merely queued. | Queue it. |
| [#301](https://github.com/Sovereign-Communication/harness/issues/301) ci: driver_core is outside the ruff scope — dead import (F401) sits unseen on main | No open prerequisite; merely queued. | Queue it. |
| [#303](https://github.com/Sovereign-Communication/harness/issues/303) fix(site-check): non-http(s) schemes in prose prompts bypass the scheme gate via extractor normalization (minor) | No open prerequisite; merely queued. | Queue it. |

### W2 — make write paths safe and atomic (5 open)

| Issue | Blocker | What unblocks it |
|---|---|---|
| [#270](https://github.com/Sovereign-Communication/harness/issues/270) [driver] Add launch_app action to vocabulary | blocked by #292, #300 | Land #292, #300. |
| [#271](https://github.com/Sovereign-Communication/harness/issues/271) [driver] NL planner: natural language to action plan | blocked by #270 | Land #270. |
| [#272](https://github.com/Sovereign-Communication/harness/issues/272) [driver] Vision verification for computer control | blocked by #270 | Land #270. |
| [#273](https://github.com/Sovereign-Communication/harness/issues/273) [driver] GUI wiring: /driver/nl endpoint + confirmation card | blocked by #271, #272, #299 | Land #271, #272, #299. |
| [#300](https://github.com/Sovereign-Communication/harness/issues/300) fix(driver_core): check_batch docstring overstates the irreversible-mix guarantee it enforces | No open prerequisite; merely queued. | Queue it. |

### W3 — widen test surfaces and reduce review cost (5 open)

| Issue | Blocker | What unblocks it |
|---|---|---|
| [#282](https://github.com/Sovereign-Communication/harness/issues/282) [gui-tests] Playwright smoke layer: 5 critical journeys, single worker | No open prerequisite; merely queued. Identifying the actual GUI runtime is acceptance work, not a hold. | Queue it and identify the runtime as the first acceptance step. |
| [#283](https://github.com/Sovereign-Communication/harness/issues/283) [gui-tests] Jest tests for IPC handlers / main-process logic | blocked by #282 | Land #282. |
| [#285](https://github.com/Sovereign-Communication/harness/issues/285) [mcp] Agent-eval layer: tool selection/chaining scored by pass-rate | blocked by #284, #335, #333, #334; #333 is an owner decision | Land #284, #335, #333, #334. |
| [#288](https://github.com/Sovereign-Communication/harness/issues/288) [ci] Type checking (pyright/pyrefly) in CI for new code only | blocked by #276 | Land #276. |
| [#289](https://github.com/Sovereign-Communication/harness/issues/289) [build] Migrate dependency management to uv (old requirements stay until uv CI is green) | blocked by #276 | Land #276. |

### W4 — prove economics and dogfood depth (8 open)

| Issue | Blocker | What unblocks it |
|---|---|---|
| [#275](https://github.com/Sovereign-Communication/harness/issues/275) Hourglass/HUL: integrate Freebuff mission-mode findings (full dossier) | blocked by #342, #344, #273 | Land #342, #344, #273. |
| [#280](https://github.com/Sovereign-Communication/harness/issues/280) [efficiency] Calibration-driven first-rung selection + earlier rotation | blocked by #302, #340 | Land #302, #340. |
| [#281](https://github.com/Sovereign-Communication/harness/issues/281) [bench] Published benchmark evidence via Proof Bench pipeline | blocked by #340, #277 | Land #340, #277. |
| [#291](https://github.com/Sovereign-Communication/harness/issues/291) [refine] Execute the Phase 0 pilot: 20 cycles (TIER 2 only, no auto-merge) | blocked by #276, #325, #340 | Land #276, #325, #340. |
| [#292](https://github.com/Sovereign-Communication/harness/issues/292) [driver-core] Sync vendored driver_core v3.4.0 to standalone v3.5.0 + CI drift check | blocked by #301 | Land #301. |
| [#293](https://github.com/Sovereign-Communication/harness/issues/293) [tests] Cover the 6 suite-unexecuted edge lines left by #274 | blocked by #276 | Land #276. |
| [#302](https://github.com/Sovereign-Communication/harness/issues/302) harden(apply): screen refusal/non-code model output at intake; stop crediting model health for missing-marker responses | No open prerequisite; merely queued. | Queue it. |
| [#304](https://github.com/Sovereign-Communication/harness/issues/304) feat(economics): include Fireworks in automatic cheapest-capable routing with dynamic price checking — completes EV-8 | blocked by #340; the live EV-8 call is additionally operator-gated on #306 | Land #340, then perform the live EV-8 call once #306 is available. |

### W5 — experiment lane (owner-gated) (1 open)

| Issue | Blocker | What unblocks it |
|---|---|---|
| [#294](https://github.com/Sovereign-Communication/harness/issues/294) [provider-onboarding] Completion: pane browser test, 50 live key validations, verifier sweep, final Jev gate | blocked by #282, #277, #340; the final router flag is operator-owned after evidence | Land #282, #277, #340, then the operator decides the router flag. |

### Unwaved — in the register, not yet waved (25 open)

| Issue | Blocker | What unblocks it |
|---|---|---|
| [#295](https://github.com/Sovereign-Communication/harness/issues/295) [burndown] TRACKER: full completion burndown — all remaining work, waved and ordered | Tracker for the whole burndown; it closes when every remaining item has a receipt, not on a code change. | Land the remaining slices or record their operator holds; keep the per-item receipts in this tracker. |
| [#306](https://github.com/Sovereign-Communication/harness/issues/306) ops(fireworks): place the Fireworks key into the harness config environment and complete EV-8 verification | **Operator-gated**: the Freebuff laptop key is unavailable, so the live EV-8 Fireworks receipt cannot be produced. | Operator supplies or authorizes the Freebuff laptop key, then runs EV-8 against the two Fireworks models and files `audits/self/dogfood/EV8_FIREWORKS_RECEIPT.md`. EV-8 (#304) also waits on #340. |
| [#308](https://github.com/Sovereign-Communication/harness/issues/308) [planning] Split waist.py by planning responsibility | blocked by #346, #342 | Land #346, #342. |
| [#309](https://github.com/Sovereign-Communication/harness/issues/309) [jev] Separate domain evaluators from shared policy dispatch | blocked by #342 | Land #342. |
| [#310](https://github.com/Sovereign-Communication/harness/issues/310) [provision] Split admission, approval, and execution by trust boundary | blocked by #276 | Land #276. |
| [#311](https://github.com/Sovereign-Communication/harness/issues/311) [local-fit] Exercise training and export extra in hermetic CI | blocked by #276 | Land #276. |
| [#313](https://github.com/Sovereign-Communication/harness/issues/313) eval(models): evaluate Inception Mercury 3 (diffusion LLM) for cheapest-capable routing | blocked by #340 | Land #340. |
| [#314](https://github.com/Sovereign-Communication/harness/issues/314) feat(jev): offline Jev-like judge modules (local inference backends) | blocked by #325, #309 | Land #325, #309. |
| [#318](https://github.com/Sovereign-Communication/harness/issues/318) route(selection): continuous recursive self-improving model selection — always-on auditing, dynamic as needed | blocked by #340, #280, #333; #333 is an owner decision | Land #340, #280, #333. |
| [#319](https://github.com/Sovereign-Communication/harness/issues/319) route(models): automated model intake pipeline — all OpenRouter models auto-screened into pools | blocked by #318 | Land #318. |
| [#325](https://github.com/Sovereign-Communication/harness/issues/325) [jev][MED] v1 assessment gate (5-aspect suite + flexibility rule) has no code counterpart | No open prerequisite; merely queued. | Queue it. |
| [#328](https://github.com/Sovereign-Communication/harness/issues/328) [cli][MINOR] no version surface: --version / -v / version all exit 2 | No open prerequisite; merely queued. | Queue it. |
| [#329](https://github.com/Sovereign-Communication/harness/issues/329) [cli] polish: desktop usage line + shared --out/--quiet/--events/--no-color flags missing on defer/media/driver | No open prerequisite; merely queued. | Queue it. |
| [#332](https://github.com/Sovereign-Communication/harness/issues/332) [audit] inconsistent fallback model labeling: is_fallback=True with model=jev-latest vs model=local-heuristic | No open prerequisite; merely queued. | Queue it. |
| [#333](https://github.com/Sovereign-Communication/harness/issues/333) [mcp][MEDIUM] defer_work fabricates success for nonexistent task_ids; phantom entries pollute the ledger | **Owner decision**, not an engineering task: in-process registry vs ledger-backed registry. The canon records `undecided`. | Operator ruling on the registry architecture. Direct dependents: #285, #318, #336 (and transitively #337). |
| [#334](https://github.com/Sovereign-Communication/harness/issues/334) [mcp][LOW] log_judgment reports ok when zero items were live-judged | No open prerequisite; merely queued. | Queue it. |
| [#335](https://github.com/Sovereign-Communication/harness/issues/335) [mcp][LOW] tool annotations inaccurate: read_only_hint missing on read-only tools, absent on mutating ones | No open prerequisite; merely queued. | Queue it. |
| [#336](https://github.com/Sovereign-Communication/harness/issues/336) ledger: self-pruning via anchored compaction + automatic canary updates | blocked by #333, #345; #333 is an owner decision | Land #333, #345. |
| [#337](https://github.com/Sovereign-Communication/harness/issues/337) ledger: validated criteria-prompt library for interrogating the ledger across models | blocked by #336, #318, #340 | Land #336, #318, #340. |
| [#340](https://github.com/Sovereign-Communication/harness/issues/340) [economics][MEDIUM] harness-reported costs drift ±15–40% off measured spend; #318 metric must use measured | blocked by #345 | Land #345. |
| [#342](https://github.com/Sovereign-Communication/harness/issues/342) [research] Implement the missing shared model-proposed ResearchPlan and native Jev review lifecycle | blocked by #345, #346 | Land #345, #346. |
| [#343](https://github.com/Sovereign-Communication/harness/issues/343) [web] Reject unrelated hits individually instead of accepting a mixed target-site result batch | blocked by #342 | Land #342. |
| [#344](https://github.com/Sovereign-Communication/harness/issues/344) [lifecycle] Bind resumed web context to the current request and retrieval decision | blocked by #342 | Land #342. |
| [#345](https://github.com/Sovereign-Communication/harness/issues/345) [spend] Attribute request caps independently of unrelated concurrent session spending | No open prerequisite; merely queued. | Queue it. |
| [#346](https://github.com/Sovereign-Communication/harness/issues/346) [planning] Exclude completed planning calls from the remaining execution allowance | blocked by #345 | Land #345. |

## The two explicit holds

### #333 — owner decision (in-process registry vs ledger-backed)

The canon records this as **undecided**. It is not an engineering gap, a missing test, or a queue-order problem, and no slice in this campaign decides it. #285, #318 and #336 name #333 as a prerequisite, and #337 inherits it through #336/#318. Until the operator rules, landing those issues would pick an architecture by accident. **Unblocker:** an operator ruling; then the dependent slices can be scheduled.

### #306 — operator-gated on the Freebuff laptop key

#306 remains open because the live Fireworks run needs the Freebuff laptop key. `harness/fireworks_probe.py` is already scaffolded; the missing artifact is `audits/self/dogfood/EV8_FIREWORKS_RECEIPT.md` from live calls to `accounts/fireworks/models/deepseek-v3p2` and `accounts/fireworks/models/glm-5p1`. EV-8 (#304) additionally waits on #340. **Unblocker:** operator supplies/authorizes the key and runs the probe; no code change in this repository can manufacture the receipt.

### #294 — Wave 5, operator-owned router flag

#294 is blocked by #282, #277 and #340 in that order. Even after its evidence lands, the router flag remains the operator's decision. It is not held now; it is simply behind prerequisites and then an explicit owner gate.

## Requested outcomes

| Outcome | State | Evidence or remaining work |
|---|---|---|
| Public results page matches main | complete in source | `site/public/results/index.html` now lists the five landed rows with merge commits, 54 open issues, 5,080 tests (72 skipped), audit 10.00, D12 98%, and main `4155b687`. |
| Execution queue matches main | complete in source | `docs/issue-plans/queue.json`: `#330`/`#347`/`#327`/`#278` marked complete, `open_issue_count: 54`, snapshot 2026-10-25. |
| Closing accounting written | complete | this report: landed list, five closed dispositions, 54 per-issue wave/blocker/unblocker rows, explicit #333/#306 holds. |
| Deployment | **not deployed** | The only deploy path is `.github/workflows/site.yml` on a `main` push. The last site run (`40135845847`, on `4d9fea4`) logged `CF_API_TOKEN not configured; skipping deploy`; the repository reports zero Actions secrets. Until this change merges and a token is configured, the page exists only in the repository and local build, not on a public host. |
| main green | green at the base; this change pending | `4155b687` is 12/12 on merge-result CI. This docs/site change must pass local gates and PR CI before merge; no merge bypass. |
| #276 not started | kept | no commit, test, or queue status changed for #276. It remains the first W0 item and the next chain item. |
| #333 not decided | kept | recorded as owner decision; no registry architecture chosen or implemented. |
| #306 untouched | kept | no key, probe run, or EV-8 receipt created. |

## Boundaries and limitations

- No metric on the public page is invented: every number is tied to a local command receipt, a merge commit, or the named CI run.
- The site workflow was inspected rather than assumed: it requires `CF_API_TOKEN`; this repository has none, so deployment is skipped by design, not failed. Claiming otherwise would be false.
- `#295` carries the closure receipts for the five landed items; this report is the campaign-level summary, not a parallel plan.
- `main` must stay green. The queue/report changes are documentation and static-site only; they still go through the normal PR + local gates + CI path.

## Next item

The next chain item is **#276** (merge-base D12 measurement plus an explicit S5 ruff disposition). It is deliberately not started here. `#277` stays behind `#276` and #331; `#333` and `#306` stay with their owners.
