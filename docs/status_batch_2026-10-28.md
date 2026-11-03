# Prompt-scoped issue live status — 2026-10-28


One `gh issue view` call per issue (title + state). Cached locally as issue_<n>.json for the ones already fetched this session.


- #270: OPEN — [driver] Add launch_app action to vocabulary

- #271: OPEN — [driver] NL planner: natural language to action plan

- #272: OPEN — [driver] Vision verification for computer control

- #273: OPEN — [driver] GUI wiring: /driver/nl endpoint + confirmation card

- #275: OPEN — Hourglass/HUL: integrate Freebuff mission-mode findings (full dossier)

- #277: OPEN — dogfood: structured round 2 - checklist pass with filed report (GUI/MCP/website weighted)

- #280: OPEN — [efficiency] Calibration-driven first-rung selection + earlier rotation

- #281: OPEN — [bench] Published benchmark evidence via Proof Bench pipeline

- #282: CLOSED — [gui-tests] Playwright smoke layer: 5 critical journeys, single worker

- #283: OPEN — [gui-tests] Jest tests for IPC handlers / main-process logic

- #284: OPEN — [mcp] Tool-description lint: When/What-in/What-out formula, wired into driver_core audit

- #285: OPEN — [mcp] Agent-eval layer: tool selection/chaining scored by pass-rate

- #286: OPEN — [mcp] Structured file logging + additive-only tool versioning policy

- #287: CLOSED — [ci] cancel-in-progress + paths filters: skip the hermetic matrix for docs/site-only changes

- #288: CLOSED — [ci] Type checking (pyright/pyrefly) in CI for new code only

- #289: OPEN — [build] Migrate dependency management to uv (old requirements stay until uv CI is green)

- #290: OPEN — [docs] Hermetic/audit triage runbook: environment failures vs real failures

- #291: OPEN — [refine] Execute the Phase 0 pilot: 20 cycles (TIER 2 only, no auto-merge)

- #292: OPEN — [driver-core] Sync vendored driver_core v3.4.0 to standalone v3.5.0 + CI drift check

- #293: OPEN — [tests] Cover the 6 suite-unexecuted edge lines left by #274

- #294: OPEN — [provider-onboarding] Completion: pane browser test, 50 live key validations, verifier sweep, final Jev gate

- #295: OPEN — [burndown] TRACKER: full completion burndown â€” all remaining work, waved and ordered

- #299: OPEN — fix(server): cap POST body size before reading â€” return 413 over the limit

- #300: OPEN — fix(driver_core): check_batch docstring overstates the irreversible-mix guarantee it enforces

- #301: OPEN — ci: driver_core is outside the ruff scope â€” dead import (F401) sits unseen on main

- #302: OPEN — harden(apply): screen refusal/non-code model output at intake; stop crediting model health for missing-marker responses

- #303: OPEN — fix(site-check): non-http(s) schemes in prose prompts bypass the scheme gate via extractor normalization (minor)

- #304: OPEN — feat(economics): include Fireworks in automatic cheapest-capable routing with dynamic price checking â€” completes EV-8

- #306: OPEN — ops(fireworks): place the Fireworks key into the harness config environment and complete EV-8 verification

- #308: OPEN — [planning] Split waist.py by planning responsibility

- #309: OPEN — [jev] Separate domain evaluators from shared policy dispatch

- #310: OPEN — [provision] Split admission, approval, and execution by trust boundary

- #311: OPEN — [local-fit] Exercise training and export extra in hermetic CI

- #313: OPEN — eval(models): evaluate Inception Mercury 3 (diffusion LLM) for cheapest-capable routing

- #314: OPEN — feat(jev): offline Jev-like judge modules (local inference backends)

- #318: OPEN — route(selection): continuous recursive self-improving model selection â€” always-on auditing, dynamic as needed

- #319: OPEN — route(models): automated model intake pipeline â€” all OpenRouter models auto-screened into pools

- #325: OPEN — [jev][MED] v1 assessment gate (5-aspect suite + flexibility rule) has no code counterpart

- #332: OPEN — [audit] inconsistent fallback model labeling: is_fallback=True with model=jev-latest vs model=local-heuristic

- #334: OPEN — [mcp][LOW] log_judgment reports ok when zero items were live-judged

- #335: OPEN — [mcp][LOW] tool annotations inaccurate: read_only_hint missing on read-only tools, absent on mutating ones

- #336: OPEN — ledger: self-pruning via anchored compaction + automatic canary updates

- #337: OPEN — ledger: validated criteria-prompt library for interrogating the ledger across models

- #340: OPEN — [economics][MEDIUM] harness-reported costs drift \u00b115\u201340% off measured spend; #318 metric must use measured

- #342: OPEN — [research] Implement the missing shared model-proposed ResearchPlan and native Jev review lifecycle

- #343: OPEN — [web] Reject unrelated hits individually instead of accepting a mixed target-site result batch

- #344: OPEN — [lifecycle] Bind resumed web context to the current request and retrieval decision

- #345: OPEN — [spend] Attribute request caps independently of unrelated concurrent session spending

- #346: OPEN — [planning] Exclude completed planning calls from the remaining execution allowance

- #365: MERGED — release: land rendered site evidence, durable context, 0.5.0 packaging, and #347

- #377: OPEN — [tracking, low priority] Session model allowlist: restrict a session to an explicit model set (e.g. Meta-only)

- #378: OPEN — [tracking, low priority] Model presets + custom include/exclude sets (JSON/text file)

- #379: OPEN — [tracking, low priority] Direct Meta Model API provider (api.meta.ai) alongside OpenRouter/Fireworks

- #380: OPEN — [tracking, low priority] Jev-free judgment backend: Fusion Lite panels replacing Jev gates

- #381: OPEN — [tracking, low priority] Calibrate Fusion Lite panels against Jev on golden sets before any gate swap

- #382: OPEN — [tracking, low priority] Session-start model picker: choose preset/custom set easily at launch

- #407: OPEN — [visibility-site] public receipt-backed model statistics with opt-in ingestion

- #409: MERGED — docs: record #306 blocker and synchronize burndown state

- #411: MERGED — ci: type-check changed Python files in PRs

- #412: OPEN — [gui] Verify first-use auth handoff live UX (audit: OPEN)

- #413: OPEN — [driver] Verify loopback-browser round-trip end to end (audit: NOT VERIFIED)

- #414: OPEN — [mcp] Verify installed MCP runtime identity via real-client check (audit: NOT VERIFIED)

- #415: OPEN — [gui] Live model/Jev semantic approvals via GUI (audit: NOT RUN)

- #416: OPEN — [docs] GUI first-use acceptance doc + serve/auth doc corrections

- #417: OPEN — [driver-core] Verify must identify which audit store/chain it verified

- #418: OPEN — [desktop] Preserve and recover long-running requests after restarts

- #419: OPEN — [desktop] Separate local server health from backend and request status

- #420: OPEN — [site] Use the TypeSafe System One contract for route judgments

- #421: OPEN — [consent] Emit one rotation event for each unusable model

- #422: ??? — [research] Verify citation metadata and claim-to-source support

- #423: ??? — [audit] Add a resumable full-source Jev audit command

- #424: OPEN — [docs] Align System One feature claims with shipped status

- #425: OPEN — [driver-core] Refuse append when an existing audit chain is invalid

- #426: OPEN — [security] Enforce public-network policy for browser evidence rendering

- #427: OPEN — [mission] Surface malformed receipt and Jev-evaluation records

- #428: OPEN — [economics] Reject negative endpoint override and cache prices

- #429: OPEN — [audit] Refuse or recover truncated JEV judgment state before append

- #430: OPEN — [lifecycle] Stop repeated retries after validated restart or consent refusal

- #431: OPEN — docs: reconcile Jev research and full-source audit plans
