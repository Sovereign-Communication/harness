# Burndown ledger - 2026-11-03 (lane/burndown, re-derived; #448 slice updated 2026-11-04)

Open issues at triage time: 80 (re-derived 2026-11-04; #448 was filed after the 79-open pass). Classes: CURABLE NOW (cure on lane, awaiting owner-reserved merge), EVIDENCE ONLY (recorded, no code act), OWNER-RESERVED (owner decision or act), PARKED (stay parked per dispatch), BACKLOG (real work, recorded order).

Closure rule: an issue closes only with a per-issue evidence disposition citing a merge SHA. No issue is closed by this ledger; the counts below are the state at this pass.

## CURABLE NOW (cure on lane head, not yet on main)
- #446 snapshot freshness test and the report job (cure 6a4988d, refresh path a8c6be5). Close on merge SHA only.
- #445 baseline pyright debt (cure 1b3869b).
- #447 Muse chat() Meta transport (cure cbbfc31).
- #434 overlay verification (cures e7e4131, dc5b4bb, 92986a2; 17/17 green).
- #443 Python 3.14 matrix, #440 driving-hygiene CI, #439 read-only probe: dispositions found already cured on the lane; close on merge SHA.
- #448 PARTIAL (slice on lane, 2026-11-04): read-only audit intent outranks mutation keywords, whole-workspace scope no longer collapses to module-stem matches, an explicit no-tools instruction suppresses retrieval, and HTTP 402 keeps its cause (`account_funding` / `route_or_model_restriction` / `unknown`) with a valid-only next step. Tests: `tests/test_read_only_audit_448.py` (17 cases) plus the affected suites. Remaining on the item: wire the chat/GUI path to the already-landed source-audit engine (`harness/source_audit.py`, CLI `harness source-audit`, MCP `source_audit`) instead of the workspace-inspection answer loop, cost preflight with cap-raise approval surfaced in the GUI, per-call receipt display with the known/unknown split, and the `jev-latest` consent path. Close on merge SHA only.

- Agent-desktop audit completion (#d) — COMPLETE 2026-11-04: B6 completed as an executable stale-ref test (`tests/test_agent_desktop_audit_report.py`, 11 cases) guarding the report's cited path:line refs, the eight-field Part C mapping and the Part D targets; Part C fit mapping written (AX route measured non-viable: 8/8 launch-gate fields absent, 13 AX nodes/3 refs; vision route the only candidate; no field modelled in-repo); Part D amendment list written as proposals with exact targets (docs/jev-roadmap.md, docs/driver.md, #272, #270, future audits/ checksum rule) — deliberately not applied per the interim record's "amendments are a separate, later direction"; report file `reports/agent-desktop-audit-2026-11-01.md` created (did not exist; session had died mid-B6). B1-B5 carried as labelled recorded evidence (tooling absent from the worktree; not re-run).
- #438 HEADLESS KEY PROVISIONING (lane head, 2026-11-04): verified on the real surface, not the issue text. `driver_core` health emits `keyed` plus `key_provisioning` (variable `DRIVER_JEV_API_KEY`, docs anchor `docs/driver.md#headless-key-provisioning`, no value), the CLI prints the same sentence, `verify` reports `budget.spent_usd`, and docs/driver.md states the keyless (health/verify/vocabulary) vs keyed (extraction/Jev) split. This pass fixed two doc defects found while verifying: a duplicated `## Status` heading, and no README pointer to the headless path (added next to the installed-binary list). Evidence: live `python -m driver_core health` / `--json health` output, `tests.test_driver_core.test_service` + `test_auth` (72 green, including the verify-spend assertion at test_service.py:146). SPLIT: the keyed end-to-end observe/verify run needs a live TypeSafe key, which this machine does not hold; recorded on #438.
- #448 WORKSPACE-AUDIT CHAIN (lane head, 2026-11-04): read-only workspace audits no longer land in the ledger-chain lane. `request_intent.is_workspace_audit_request` splits the two questions, `task_lifecycle._handle_workspace_audit` scopes the tree (`repo_scope.audit_scope`: names and sizes only, exclusion reasons, truncation flagged), prices the complete run with a stated 25% buffer (`workspace_audit.estimate_audit_run`), and returns `ready` / `approval_required` (the exact raise, never an automatic cap change) / `blocked` with the #423 handoff command. Nothing paid is dispatched from the lane. Evidence: `tests/test_workspace_audit_routing_448.py` (13 cases). Remaining split: the GUI lane preflights and hands off; executing the engine still needs the operator's pack files, which no setting supplies.
- #447 ROUTER SLICE (lane head, 2026-11-04): the Meta API is a backend in the routing table (`routing_table.backend_for_model` / `backend_registry` / `route_for_model`), dispatch rides the existing route path through the new `chat.chat_routed` (resolve_route -> chat_for_route), the judge, panel-vote and convergence-specialist call sites now use it, and `sliding_scale.preset_model_ladder` makes the ladders preset-aware (committed Muse ids or the ordinary ladder, never a cross-provider fallback by accident). Evidence: `tests/test_router_meta_backend.py` (26 cases) plus 52 affected suites green. Drive complete: apply_policy (attempt and escalation), attest, capability, consent, both lifecycles, escalation and rankings now dispatch through `chat_routed`, audited by AST so a new direct `chat()` call in any of those modules fails the suite; the router reads the resolved backend's key surface (`settings.meta_key` / `settings.fireworks_key`) before the lane's threaded key. Only `chat.py`'s internal reasoning retry and the OpenRouter branch inside `chat_for_route` still call `chat()`. Close #447 on the router shape only with a merge SHA.

## REPORT-JOB DISPOSITION (from the #446 cure)
The `report` job in `.github/workflows/rankings.yml` was red because `harness economics --check-fresh` exits 2 on an expired artifact and the job fails on that exit code. The cure owns both surfaces: the job now runs `harness economics --refresh-snapshot` before the freshness check, and the delivery step opens a reviewable PR (never a direct commit) when the artifact moved. Local evidence on the lane: `packs/tiers.snapshot.json` carries `captured_at 2026-11-03T11:00:23Z`, `max_age_days 14`, and `python -m harness.cli economics --check-fresh --json` exits 0 with `warning: null`. The job's red is therefore cured on the lane and needs the merge, not new work.

## BACKLOG (recorded order; real work)
- URGENT QUEUE H1/H2 (#295 comments 6605149634, 6605150240) — parked with evidence (6617316504); surfaces are not in this repo; re-check once per session.
- #438 provisioning chain and Batch 0 remainder.
- S1 fast-track wave (#300 + #292) — #300 COMPLETE 2026-11-04 at 832a059: `check_batch` now refuses an irreversible action mixed with anything else, matching its docstring; tests cover read-only mix, mutating mix, two irreversibles, all-reversible accepted and a lone irreversible accepted; 403 driver_core tests + 26 conformance tests green; no production caller exists because `Driver.step` takes one decision per step — recorded on the issue per the option-A acceptance clause. #292 WIP: vendoring runbook landed in docs/driver.md with the measured upstream delta (66 files, +4918/-1987; `consent_ledger.py` removed upstream; `extractors.py` -> `extractors/`); a wholesale copy was reverted after it dropped the #438/#439/#300 local patches and broke 18 tests — the sync must be a three-way merge preserving them; three-way attempt preserved at backup/292-sync-wip-20261104 = a30dbf6 (upstream 3.5.0 imports green at 403 tests; 9 failures/40 errors remain at the vision API, capability lock and golden-audit seams); lane tip restored to 3.4.0 and green; next action is resuming from the backup ref at the vision extractor API seam.
- Agent-desktop audit completion (B6 stale-ref test, Parts C/D, report file).
- Fast-track wave S1 (#292, #300), S2 (#270), S3 (#272), S4 (#273), then TM1-TM3 (specs on #292/#300/#270/#272/#273).
- Clean Burndown v4 batches in order.
- Other open items (#427, #428 done on lane, #429, #430, #425, #426, #417, #419, #418, #422, #420, #416, #412-415, #413, #414, #423, #421, #382, #381, #380, #346, #345, #344, #343, #342, #340, #337, #336, #335, #334, #332, #329, #328, #325, #319, #318, #313, #311, #310, #309, #308, #304, #303, #302, #301, #299, #294, #291, #290, #289, #286, #285, #284, #283, #281, #280, #277, #275, #271): real work, sequenced after the wave; re-verify each before any closure.

## EVIDENCE ONLY
- #290 triage runbook, #277 dogfood round 2, #295 tracker (stays open), #292 driver_core sync (active wave S1).

## OWNER-RESERVED
- #407 deploy decision; OpenRouter account/credits; PyPI credentials; release acts; main branch protection (11 required checks) never relaxed.
- Merges to main (base main red, run 41576644039; merges owner-reserved).
- #433 Lucas Gate v2 in the driving loop (semantic acceptance is owner-reserved).

## PARKED (stay parked)
- #306, #432, #433 semantic acceptance, #407 deploy, #424, #314 parity bars, gate swaps.

## Counts
- Open at triage: 80 (2026-11-04 re-derivation). Closed this pass: 0 (no per-issue evidence for closure yet landed on main). Opens stay below closes once the cures above merge; until then the ledger records the path, not a closure.

## 2026-11-04 - #292 driver_core 3.5.0 sync (lane/burndown)

Vendored driver_core reports 3.5.0, reconciled file by file with the
harness patches intact: #438 key provisioning, #439 probe/refusal
hardening, #300 check_batch, the driver-local native input backend and
the image admission guards. consent_ledger kept deliberately; upstream
3.5.0 has no replacement seam.

Landing commit 76fb1cf. Gates: driver_core 403 OK (1 skipped),
conformance 26 OK, router 26 OK, packaging 5 OK, ruff clean on harness
tests, secret scan clean, subprocess import resolves to the checkout.
Evidence posted on #292; status mirrored to #442 and #295.
