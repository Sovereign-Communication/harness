# Issue flush report - 2026-11-05 (order c)

Generated from a primary read at 2026-11-05T11:10:11Z; the issue list was re-read at 2026-11-05T11:10:12Z before writing and held 80 open issues (pull requests excluded).

## Reads this report rests on

| Code | Method | Time (UTC, 2026-11-05) | What it establishes |
|---|---|---|---|
| R1 | `gh api repos/Treystu/harness/issues?state=open&per_page=100 --paginate` | 2026-11-05T11:10:11Z | the set of open issues, their titles and state |
| R2 | `git log --all --format='%H %s'` then `git merge-base --is-ancestor <sha> HEAD` per cited commit | 10:14-10:17 | whether a cure is really in `lane/burndown` ancestry (a commit on another branch is not a cure) |
| R3 | focused `python -m unittest` runs listed per row | 10:19-10:22 | the cure's own tests actually execute green on this head |
| R4 | `gh api repos/Treystu/harness/commits/<head>/check-runs` and `.../actions/runs/<id>/jobs` | 10:16 | CI state: every job `steps=0` with the billing-lock annotation |
| R5 | file and code reads (`git show --stat`, direct file reads) | 10:20-10:22 | artifacts that exist in the tree, independent of commit subjects |
| R6 | independent conformance re-read: `gh api .../contents/<path>?ref=<head>`, `.../compare/<sha>...<head>`, `.../git/trees/<head>?recursive=1` | 10:42 | a second reader, not the generator, re-derives the population and checks each cited commit and artifact against the pushed head |

Every row below carries the read codes it actually used. A row whose basis is only R1+R2 has not been shown to have a cure; it is recorded as still open rather than guessed at.

## Dispositions (all open issues)

Totals: 80 open issues | landed on lane=23 | landed-partial=4 | owner-only=9 | still-open=43 | tracker=1

| # | Issue | Disposition | Basis | Read |
|---|---|---|---|---|
| 270 | [driver] Add launch_app action to vocabulary | landed on lane (unmerged) | cure f2e7194 in lane ancestry; acceptance bullets present; focused suite | R1,R2,R3(test_launch_app) |
| 271 | [driver] NL planner: natural language to action plan | still-open | deferred with #273 by the recorded S4 re-scope; no code on lane | R1,R2 |
| 272 | [driver] Vision verification for computer control | landed-partial: code landed, S3 calibration open | fd653f4 + 25 hermetic tests; live corpus and freeze not claimed | R1,R2,R3(test_vision_verify) |
| 273 | [driver] GUI wiring: /driver/nl endpoint + confirmation card | still-open | blocked on the operator launch pack and frozen protocol; no launch-plan path | R1,R2 |
| 275 | Hourglass/HUL: integrate Freebuff mission-mode findings (full dossier) | still-open | no lane commit; Freebuff mission-mode dossier not integrated | R1,R2 |
| 277 | dogfood: structured round 2 - checklist pass with filed report (GUI/MCP/website weighted) | still-open | round 2 carried as recorded evidence only; no cure commit in ancestry | R1,R2 |
| 280 | [efficiency] Calibration-driven first-rung selection + earlier rotation | still-open | no lane commit; calibration-driven first-rung not implemented | R1,R2 |
| 281 | [bench] Published benchmark evidence via Proof Bench pipeline | still-open | no lane commit; Proof Bench publication not done | R1,R2 |
| 283 | [gui-tests] Jest tests for IPC handlers / main-process logic | still-open | no lane commit; Jest IPC coverage absent | R1,R2 |
| 284 | [mcp] Tool-description lint: When/What-in/What-out formula, wired into driver_core audit | landed on lane (unmerged) | cure 89c7e9c in lane ancestry; description lint enforced | R1,R2,R3(test_issue284_mcp_descriptions) |
| 285 | [mcp] Agent-eval layer: tool selection/chaining scored by pass-rate | still-open | no lane commit; agent-eval layer absent | R1,R2 |
| 286 | [mcp] Structured file logging + additive-only tool versioning policy | landed on lane (unmerged) | cure 07cb153 in lane ancestry; file-only JSONL log with credential scrubbing | R1,R2,R3(test_issue286_mcp_logging) |
| 289 | [build] Migrate dependency management to uv (old requirements stay until uv CI is green) | still-open | no lane commit; dependency management unchanged | R1,R2 |
| 290 | [docs] Hermetic/audit triage runbook: environment failures vs real failures | landed on lane (unmerged) | docs/audit-triage.md present in tree; cited 617e6c6 is NOT in lane ancestry, so the artifact is the evidence; the commit is not | R1,R5(docs/audit-triage.md) |
| 291 | [refine] Execute the Phase 0 pilot: 20 cycles (TIER 2 only, no auto-merge) | still-open | canon records pilot ACTIVE 0/20; no lane commit; the REFINE-LOOP complete row covers the #240 scaffold only | R1,R2,R5(canon) |
| 292 | [driver-core] Sync vendored driver_core v3.4.0 to standalone v3.5.0 + CI drift check | landed on lane (unmerged) | sync 76fb1cf in lane ancestry; vendored 3.5.0; drift check green | R1,R2,R3(driver_core 450, drift) |
| 294 | [provider-onboarding] Completion: pane browser test, 50 live key validations, verifier sweep,... | owner-only | needs 50 live key validations and a pane browser test; operator keys | R1,R2 |
| 295 | [burndown] TRACKER: full completion burndown — all remaining work, waved and ordered | tracker (stays open) | the lane's tracker by design; closes when no lane item remains | R1 |
| 299 | fix(server): cap POST body size before reading — return 413 over the limit | still-open | server.py carries a 413 for the pairing body only; no general POST cap found | R1,R5(harness/server.py) |
| 300 | fix(driver_core): check_batch docstring overstates the irreversible-mix guarantee it enforces | landed on lane (unmerged) | cure 832a059 in lane ancestry; check_batch refuses irreversible mixes | R1,R2,R3(driver_core 450) |
| 301 | ci: driver_core is outside the ruff scope — dead import (F401) sits unseen on main | landed on lane (unmerged) | the #301 cure reached only the unlanded branch codex/301-driver-core-ruff-scope-20261026 (b5f92fd, not in lane ancestry), so ci.yml linted scripts but not driver_core; cured in this pass as 75375ae in lane ancestry, lane head lints driver_core and ruff is clean | R1,R2,R3(ruff),R5(ci.yml) |
| 302 | harden(apply): screen refusal/non-code model output at intake; stop crediting model health fo... | still-open | no lane commit; apply intake screening not implemented | R1,R2 |
| 303 | fix(site-check): non-http(s) schemes in prose prompts bypass the scheme gate via extractor no... | still-open | no verified scheme-gate artifact this pass | R1,R2 |
| 304 | feat(economics): include Fireworks in automatic cheapest-capable routing with dynamic price c... | landed on lane (unmerged) | Fireworks cheapest-capable routing present; route suite green | R1,R3(test_economics_fireworks_route) |
| 306 | ops(fireworks): place the Fireworks key into the harness config environment and complete EV-8... | owner-only | needs the Fireworks key in the operator config and the EV-8 live call | R1,R2 |
| 308 | [planning] Split waist.py by planning responsibility | still-open | no lane commit | R1,R2 |
| 309 | [jev] Separate domain evaluators from shared policy dispatch | still-open | no lane commit | R1,R2 |
| 310 | [provision] Split admission, approval, and execution by trust boundary | still-open | no lane commit | R1,R2 |
| 311 | [local-fit] Exercise training and export extra in hermetic CI | still-open | no lane commit | R1,R2 |
| 313 | eval(models): evaluate Inception Mercury 3 (diffusion LLM) for cheapest-capable routing | still-open | no lane commit; model evaluation not run | R1,R2 |
| 314 | feat(jev): offline Jev-like judge modules (local inference backends) | owner-only | offline judges are a policy choice; parked by the ledger | R1,R2 |
| 318 | route(selection): continuous recursive self-improving model selection — always-on auditing, d... | still-open | no lane commit | R1,R2 |
| 319 | route(models): automated model intake pipeline — all OpenRouter models auto-screened into pools | still-open | no lane commit | R1,R2 |
| 325 | [jev][MED] v1 assessment gate (5-aspect suite + flexibility rule) has no code counterpart | landed on lane (unmerged) | cure 0aaad81 in lane ancestry; assessment gate code and suite | R1,R2 |
| 328 | [cli][MINOR] no version surface: --version / -v / version all exit 2 | still-open | no lane commit | R1,R2 |
| 329 | [cli] polish: desktop usage line + shared --out/--quiet/--events/--no-color flags missing on ... | still-open | no lane commit | R1,R2 |
| 332 | [audit] inconsistent fallback model labeling: is_fallback=True with model=jev-latest vs model... | still-open | no lane commit | R1,R2 |
| 334 | [mcp][LOW] log_judgment reports ok when zero items were live-judged | still-open | no lane commit | R1,R2 |
| 335 | [mcp][LOW] tool annotations inaccurate: read_only_hint missing on read-only tools, absent on ... | still-open | no lane commit | R1,R2 |
| 336 | ledger: self-pruning via anchored compaction + automatic canary updates | still-open | no lane commit | R1,R2 |
| 337 | ledger: validated criteria-prompt library for interrogating the ledger across models | still-open | no lane commit | R1,R2 |
| 340 | [economics][MEDIUM] harness-reported costs drift \u00b115\u201340% off measured spend; #318 m... | still-open | no lane commit | R1,R2 |
| 342 | [research] Implement the missing shared model-proposed ResearchPlan and native Jev review lif... | still-open | no lane commit | R1,R2 |
| 343 | [web] Reject unrelated hits individually instead of accepting a mixed target-site result batch | still-open | no lane commit | R1,R2 |
| 344 | [lifecycle] Bind resumed web context to the current request and retrieval decision | still-open | no lane commit | R1,R2 |
| 345 | [spend] Attribute request caps independently of unrelated concurrent session spending | still-open | no lane commit | R1,R2 |
| 346 | [planning] Exclude completed planning calls from the remaining execution allowance | still-open | no lane commit | R1,R2 |
| 380 | [tracking, low priority] Jev-free judgment backend: Fusion Lite panels replacing Jev gates | owner-only | gate-swap proposal; parked, owner decision | R1,R2 |
| 381 | [tracking, low priority] Calibrate Fusion Lite panels against Jev on golden sets before any g... | owner-only | panel calibration before any gate swap; parked, owner decision | R1,R2 |
| 382 | [tracking, low priority] Session-start model picker: choose preset/custom set easily at launch | still-open | no lane commit | R1,R2 |
| 407 | [visibility-site] public receipt-backed model statistics with opt-in ingestion | owner-only | deploy/publication decision reserved to the owner | R1,R2 |
| 412 | [gui] Verify first-use auth handoff live UX (audit: OPEN) | still-open | live GUI auth handoff not verified; no lane commit | R1,R2 |
| 413 | [driver] Verify loopback-browser round-trip end to end (audit: NOT VERIFIED) | still-open | loopback browser round-trip not verified; no lane commit | R1,R2 |
| 414 | [mcp] Verify installed MCP runtime identity via real-client check (audit: NOT VERIFIED) | still-open | installed MCP runtime identity not verified; no lane commit | R1,R2 |
| 415 | [gui] Live model/Jev semantic approvals via GUI (audit: NOT RUN) | still-open | live GUI semantic approvals not run; no lane commit | R1,R2 |
| 416 | [docs] GUI first-use acceptance doc + serve/auth doc corrections | still-open | no lane commit | R1,R2 |
| 417 | [driver-core] Verify must identify which audit store/chain it verified | landed on lane (unmerged) | cure 0f9716e in lane ancestry; verify names store, scope and chain head | R1,R2,R3(tests/test_issue417_verify_identity.py) |
| 418 | [desktop] Preserve and recover long-running requests after restarts | still-open | no lane commit | R1,R2 |
| 419 | [desktop] Separate local server health from backend and request status | still-open | no lane commit | R1,R2 |
| 420 | [site] Use the TypeSafe System One contract for route judgments | still-open | no lane commit | R1,R2 |
| 422 | [research] Verify citation metadata and claim-to-source support | landed-partial: verifier landed, GUI wiring open | dc0b359 slice in lane ancestry; citation verifier suite green | R1,R2,R3(test_citation_verify) |
| 423 | [audit] Add a resumable full-source Jev audit command | still-open | 843a512 is a wip slice; the resumable command is not complete | R1,R2 |
| 424 | [docs] Align System One feature claims with shipped status | owner-only | canon feature claims are the owner's to align; parked | R1,R2 |
| 425 | [driver-core] Refuse append when an existing audit chain is invalid | landed on lane (unmerged) | cure 21bfe18 in lane ancestry; append refuses an invalid chain | R1,R2,R3(driver_core 450) |
| 426 | [security] Enforce public-network policy for browser evidence rendering | landed on lane (unmerged) | cure present and re-verified at 0234946 in lane ancestry; egress proxy guards | R1,R2,R3(test_site_evidence) |
| 427 | [mission] Surface malformed receipt and Jev-evaluation records | landed on lane (unmerged) | cure 7a234f0 in lane ancestry; malformed receipt surfaced | R1,R2,R3(tests/test_issue427_mission_integrity.py) |
| 428 | [economics] Reject negative endpoint override and cache prices | landed on lane (unmerged) | cure 5a8bd77 in lane ancestry; negative override refused, prices cached | R1,R2,R3(test_economics_fireworks_source) |
| 429 | [audit] Refuse or recover truncated JEV judgment state before append | landed on lane (unmerged) | cure a43b8a0 in lane ancestry; truncated judgment refused or recovered | R1,R2,R3(tests/test_issue429_judgment_state.py) |
| 430 | [lifecycle] Stop repeated retries after validated restart or consent refusal | landed on lane (unmerged) | cure d1710c6 in lane ancestry; restart refusal recorded once | R1,R2,R3(tests/test_issue430_restart_refusal.py) |
| 432 | [proposal] Voice workstream for Harness: reusable transcription core (PROPOSAL ONLY - deferra... | owner-only | proposal only; deferral decision pending | R1,R2 |
| 433 | [gate] Lucas Gate v2 in the driving-agent loop is not on main (runner unlanded, profile absen... | owner-only | semantic acceptance of Lucas Gate v2 is owner-reserved; parked | R1,R2 |
| 434 | [verification] Overlay-style verification (run a suite against a composed tree) has no named ... | landed on lane (unmerged) | cures e7e4131/dc5b4bb/92986a2 in lane ancestry; overlay verification surface | R1,R2,R3(test_overlay) |
| 438 | [provision] Headless key provisioning for driver_core verification use (keyed=false blocks mo... | landed-partial: keyless provisioning landed, keyed e2e needs a live key | cure cbaa52c in lane ancestry; health/verify report the key split | R1,R2,R3(driver_core 450) |
| 439 | [driver-core] run_probe is declared read-only but refused unless write authority is enabled | landed on lane (unmerged) | cure c9fdbbc in lane ancestry; probe refusal and path scoping fixed | R1,R2,R3(driver_core 450) |
| 440 | [ci] Approved driving-hygiene CI job is not on main (deterministic sub-checks, Lucas-approved... | landed on lane (unmerged) | cure 7bec5a3 in lane ancestry; driving-hygiene job plus fixtures | R1,R2,R3(test_driving_hygiene) |
| 443 | [ci] Cover declared Python 3.14 support in the hermetic matrix | landed on lane (unmerged) | cure 959f0a9 in lane ancestry; py3.14 leg in the hermetic matrix | R1,R2,R5(ci.yml) |
| 445 | Baseline pyright debt in core modules makes the changed-file typecheck leg fail on any PR tha... | landed on lane (unmerged) | cure 1b3869b in lane ancestry (config/errors/fireworks/service/spend); pyright is not installed here, so the leg stays locally unproven | R1,R2 |
| 446 | test_check_fresh_emits_tier_snapshot_freshness errors on every head including main - hermetic... | landed on lane (unmerged) | cure ad98937 plus refresh path 7627e5a/6a4988d/a8c6be5 all in lane ancestry | R1,R2,R3(test_economics_cli) |
| 447 | Muse preset: panel/apply/verify lanes calling chat() directly still transport via OpenRouter ... | landed on lane (unmerged) | cures cbbfc31/7c96ea1 in lane ancestry; router dispatch for chat call sites | R1,R2,R3(test_router_meta_backend) |
| 448 | [desktop] Honor read-only workspace audits and preflight the full scope | landed-partial: routing/scoping slices landed, GUI preflight and per-call receipts open | 9050c96 and slices in lane ancestry | R1,R2,R3(test_read_only_audit_448, test_workspace_audit_routing_448) |

## Tracker items the lane owes

These are not GitHub issues; they are the rows the tracker (#295) and the dispatch put on this lane. Each has its own disposition and read.

| Tracker row | Disposition | Basis | Read |
|---|---|---|---|
| (a) MAIN-TO-GREEN: six red legs on main `5eb9c363` | landed on lane: one shared root cause identified; cure present | the five hermetic legs die on the snapshot-freshness test (cure `ad98937`); the audit leg's D12 abort is downstream of that same `SystemExit`; no second cause found | R2, R3(test_economics_cli 24 OK), `gh run view 41576644039 --log-failed` 2026-11-05 |
| (a) verification rail | blocked on owner (SR-1, SR-2) | hosted Actions answers every job with `steps=0` and the billing-lock annotation; the CircleCI port mapping is recorded and pinned, the config is deliberately unwritten until the VCS connection exists | R4, `GET /branches/main/protection` 2026-11-05 (11 contexts, strict, enforce_admins) |
| (b) S1 seams (#292, #300) | landed on lane | driver_core 3.5.0 sync `76fb1cf`, check_batch `832a059`; the dispatch's 49-seam reading predates the sync | R2, R3(driver_core 450 OK, conformance 26 OK) |
| (b) S2 #270 launch_app behind HARNESS_NL_CONTROL | landed on lane, default off | issue row 270 | R1, R2, R3 |
| (b) queue bus + laptop watcher | landed on lane `90a1f81` | per-repo state machine, supervised watcher, driven end to end | R3(test_queue_bus 23 OK), driven CLI run 2026-11-05 |
| (b) S3 #272 gate pack calibration (8 fields, bar 95) | blocked on owner (SR-3) | code and pack landed (`fd653f4`); the corpus needs install screenshots plus a live key, neither on this host | R2, R3(test_vision_verify), register A6 |
| (b) TM1 / TM2 / TM3 | blocked on owner (SR-4) | supervised ladder needs the owner present per launch; nothing has been run, and no run is claimed | tracker state, register A5 |
| (b) FLUSH-0..FLUSH-3 (saturation loop) | all four landed on lane (unmerged) | `7c2f094` contracts; `17adbf4`/`d6adf51` loop, Jev-only CLI and path resolution; hardening `d85540b` refuses an empty aspect list and non-Jev instruments before the judge; `c53c914` moves the CLI import to module scope after the architecture guard caught the deferred import; FLUSH-2 `787bb8f` adds panel/deterministic identity rotation, the oscillation `combined_cure_packet`, and judge-version score rebaselining, with CLI `--extended-instruments`, `instrument_verdicts`, `close` and per-pass `judge_version`; FLUSH-3 `0ba31ee` adds the cold-run instrument (basis taken only from that instrument), MCP `flush_run` over both campaign and canary suite, and the fail-closed canary gate with rotation and held-out scoring; `65a8dac` ships `packs/flush_canary_suite.json` (8 canaries, 3 held out) and audit rule D13 that runs it | R1, R2, R3(test_flush_loop 34 OK, test_architecture 9 OK), driven CLI rotation/oscillation/calibration 2026-11-05 |
| (c) dissemination adoption + issue flush | landed in this pass | this report plus `docs/audit/DISSEMINATION_ADOPTION_2026-11-05.md`, both committed on `lane/burndown` | R1, R2, R3(consistency test) |
| (d) instrument amendment drafts | drafted in this pass, awaiting ratification | `docs/audit/INSTRUMENT_AMENDMENTS_2026-11-05.md`; drafts only, nothing entered a canon | file read |
| Readiness statement upkeep | landed | dated statements on PR #442 at 2026-11-05 10:04 and 10:09 | `gh pr view 442 --json comments` |

## Where the recorded state and the repo disagree

1. **The dispatch says 49 seam failures remain; the tree says zero.** The 49-seam reading was taken before the 3.5.0 reconciliation landed (`76fb1cf`). Resolution: the repo read wins; driver_core is 450 OK with 1 skip and conformance is 26 OK at this head. No seam cure was redone, and the dispatch number is not repeated as current anywhere in this report.
2. **A #301 commit exists, but not on the lane.** `b5f92fd` (driver_core added to the ruff scope) lives only on `codex/301-driver-core-ruff-scope-20261026`; the lane's `ci.yml` linted `scripts` but not `driver_core`, so the issue's own defect was live on the lane while a commit looked like a cure. Resolution: the one-line cure is landed in this pass and the row is recorded as landed on lane, unmerged, not as already-fixed.
3. **The tracker's historical open counts (57 -> 54 -> 53) do not match today's 80.** Those counts come from the closed 2026-10-25/26 campaign window and its `docs/issue-plans/queue.json` bookkeeping; today's 80 is the live API read, and issues filed since (for example #446-#448) are in it. Resolution: 80 is the count of record for this report; the older numbers are not restated as current.
4. **The canon marks `REFINE-LOOP` complete while #291 (its Phase 0 pilot execution) is open at 0/20.** The canon is explicit that `complete` covers the #240 scaffold and that the civil cycle itself was not part of it. Resolution: #291 stays open; no canon change is proposed here, and the disagreement is named rather than smoothed.
5. **`#290`'s cited cure commit is not in lane ancestry, though its artifact is.** `617e6c6` is absent from `lane/burndown`, but `docs/audit-triage.md` is present with the runbook content. Resolution: the row is dispositioned on the artifact read, and the missing commit is stated instead of being papered over by the citation.
6. **Checked and found consistent (no disagreement):** the ledger's claim that `#446`'s cure is `6a4988d` with refresh path `a8c6be5` (both in lane ancestry); its `Open at triage: 80 / closed this pass: 0` (still 80 open, still 0 closed by the lane); and the previous status comment's `#417 6 OK` (6 OK again this pass).

## Independent conformance re-read (R6, 2026-11-05T10:42Z, head 7831920)

A separate verifier - written for this check, not the generator that produced the table - read this report from the pushed head through the contents API, re-derived the open-issue population from the issues API, checked every cited commit's ancestry through the compare API (32 distinct SHAs) and every cited artifact through the tree API (22 tokens). It reported six disagreements; each is classified here and resolved, and the numbers were not moved to match the prose.

1. **Four rows cited a module shorthand that does not resolve as a path** (`#417`, `#427`, `#429`, `#430`: `test_issue417` and so on). The artifacts exist as `tests/test_issue417_verify_identity.py`, `tests/test_issue427_mission_integrity.py`, `tests/test_issue429_judgment_state.py` and `tests/test_issue430_restart_refusal.py`. Resolution: those Read cells now carry the full path, so a reader can check them without guessing. The dispositions do not change - the artifacts were present all along.
2. **`#301` cited only the unlanded commit and not its own cure.** The row named `b5f92fd` but not `75375ae`, the commit that actually cured it on this lane. Resolution: the row now cites the cure and labels `b5f92fd` as not in lane ancestry, which is what the compare API says.
3. **`#290` and `#301` deliberately cite a commit outside lane ancestry.** That is counter-evidence, not a cure claim: `617e6c6` is declared absent and the row rests on the artifact `docs/audit-triage.md` (3,983 bytes at this head, read via the contents API); `b5f92fd` is declared absent while the cure is `75375ae`. The verifier's first rule - every cited SHA must be an ancestor - was too strict. It is corrected to exempt a SHA the row explicitly declares unlanded, while still requiring a landed row to rest on an ancestor commit or a verified artifact. This is a rule correction, not a lowered bar: no row gained or lost a disposition through it.

Recomputation after the corrections: population 80/80 with no missing or extra row; counts unchanged at 23 landed on lane, 4 landed-partial, 9 owner-only, 43 still-open, 1 tracker; every cited cure resolves to an ancestor of the head; every cited artifact resolves in the tree.

## What this report does not claim

- **No CI-green claim.** Hosted Actions is billing-locked (R4): every job reports `steps=0` with the lock annotation. There is no check-run evidence for this head, and none is asserted.
- **No closure.** A row is `landed on lane`, never `closed`: closure needs a merge SHA into `main`, and merges are owner-reserved. The count of issues closed by this report is zero.
- **No adoption beyond what is committed.** The dissemination surface names only artifacts that exist on `lane/burndown`, pinned by a test.
- **No owner-action rows cleared.** SR-1..SR-4 remain owner blockers and are restated, not re-derived.

