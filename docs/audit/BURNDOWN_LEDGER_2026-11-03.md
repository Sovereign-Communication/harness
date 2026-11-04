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

## 2026-11-04 - #284 MCP tool-description formula (lane/burndown)

Every served MCP tool description now states When / In / Out, enforced
by driver_core.audit.lint_tool_descriptions when the catalog is served.
Descriptions only: no tool behavior changes. Landing commit 89c7e9c.

Gates: #284 focused 7 OK, MCP 110 OK (6 skipped), driver_core 403 OK
(1 skipped), conformance 26 OK, router 26 OK, packaging 5 OK,
architecture 9 OK, ruff clean on harness tests, secret scan clean.
Evidence posted on #284; status mirrored to #442 and #295.

## 2026-11-04 - #286 MCP structured logging + additive-only policy

Each tools/call appends one JSON line (tool, scrubbed args, outcome,
duration, optional error) to HARNESS_MCP_LOG or an explicit
mcp_log_path; no path means no file. Credential-shaped values and
secret-named fields are redacted before disk, and the logging path
writes only to the file, never stdio. Additive-only tool versioning is
documented in the module docstring and docs/mcp.md. Landing commit
07cb153.

Gates: #286 focused 6 OK, MCP 110 OK (6 skipped), MCP driver 12 OK,
driver_core 403 OK (1 skipped), conformance 26 OK, router 26 OK,
packaging 5 OK, architecture 9 OK, ruff clean on harness tests,
secret scan clean. Evidence posted on #286; status mirrored to #442
and #295.

## 2026-11-04 - #430 terminal restart/consent refusal state

A validated restart or stale-consent refusal becomes one durable
blocked/reapproval_required state (request id, assignment hash,
completed stages, checkpoint, prior results, consent identity, reason,
next action). Repeated identical refusals deduplicate and dispatch
nothing; checkpoint resume is distinct, request/plan-bound, and never
reuses expired consent. The production restart decision attaches the
terminal state. Landing commit 9a0037b.

Gates: #430 focused 10 OK, hourglass consent 23 OK, hourglass Jev 42
OK, driver_core 403 OK (1 skipped), conformance 26 OK, router 26 OK,
packaging 5 OK, architecture 9 OK, ruff clean on harness tests,
secret scan clean. Evidence posted on #430; status mirrored to #442
and #295.

## 2026-11-04 - #425 audit-chain append refusal (record completion)

Implementation 21bfe18, docs 5766356. Re-verified on the lane tip rather
than taken from the session record. Opening or extending a log validates one
locked snapshot -- genesis pointer, sequence, every previous link, every
record digest -- and refuses before any byte is written; append re-reads and
revalidates under the same sidecar lock as verify. The persisted format and
ChainVerdict output are unchanged.

Gates at 8b48d52: driver_core 409 OK (1 skipped), conformance 26 OK, router
26 OK, packaging 5 OK, architecture 9 OK, ruff clean on harness tests, secret
scan of 5766356..8b48d52 0 hits, subprocess import provenance resolving to
this working copy (child process import printed
worktrees\e3283ca2\Harness\driver_core\__init__.py). The first re-verification
was red -- 408 tests with one failure -- and the failure was the #292 vision
hermeticity defect below, not the audit-chain change; after the cure at
7ee0bf0 the full set above is the re-run receipt.

Self-audit record, closed honestly: there is NO completed four-dimension
verdict from this pass. The mechanical dimensions stand at A 13/13 and R
14/14 from the session that died mid-pass, and the surviving partial artifact
(`.lane-triage/round2_scores.partial-run.json`) records mechanical scores
A/R/SM 10.0 and SD 8.18, Jev 7.5 on all four dimensions, jev_gate_pass false,
is_fallback false, model jev-1.14.0. Those are per-dimension `--dim` calls:
Jev is shown one dimension's evidence at a time, so the isolation artifact --
not a system verdict -- is what the partial shows. The saved full-run
baseline (A/R/SM/SD 10.0, gate true) is unchanged and was NOT refreshed by
this pass. Two bounded attempts at a fresh four-dimension run produced no
verdict (the first was killed at the 300 s tool cap; the second ran ~6
minutes with only the two ledger-quarantine lines in its log and was
stopped), so no verdict is claimed. No solo dimension was re-run to shop for
a better number, and no [Unreleased] CHANGELOG entry was added (release
flatten contract).

## 2026-11-04 - #292 regression: the vision suite read the live desktop

Found while re-verifying the #425 gate set: driver_core came back 408 tests
with one red, and the same test passed on base main. `VisionExtractor` binds
`osal.describe_screen` at construction and merges the platform facts *after*
the model's state, so `LocalHttpVisionSmokeTests` asserted against whatever
window was focused on the machine running it; it passed where the foreground
window was empty and failed on a desktop with a real browser in front. That
is a hermeticity defect in the test, not a flaky assertion, and it dates to
the 3.5.0 reconcile (76fb1cf) rather than to #425.

Cure 7ee0bf0: the smoke test injects the description the extractor's own
contract says a test supplies, and a guard test patches the real describer to
raise so the suite fails if the injection is dropped. Gates: vision 33 OK;
driver_core 409 OK (1 skipped); ruff clean.

## 2026-11-04 - #426 public-network policy in the evidence browser

`render_site_evidence` validated only the initial URL, then let Chromium
follow redirects and load subresources unconstrained while the docstring
claimed private-address redirects were never followed. The renderer now runs
the whole session behind a loopback CONNECT proxy (`harness/site_evidence.py`):
each destination is resolved once, every answer must be globally public (the
same `harness.web._validate_public_addresses` predicate the fetch lane uses),
only HTTPS on port 443 is tunnelled, and the tunnel opens to the validated
address -- so a DNS answer cannot change between validation and connection.
Chromium is launched with `<-loopback>` so its implicit localhost bypass
cannot skip the proxy, service workers are blocked, and a route guard
revalidates every request (redirects, iframes, scripts, images, fetches,
popups) before a tunnel is requested. The module docstring now states the
boundary that is enforced.

Tests are hermetic (no public internet, no wall-clock timing): loopback, IPv6
loopback, private, IPv6 link-local and metadata authorities are refused with
zero connections; a public-to-private DNS change connects only to the
validated address; a loopback listener receives nothing; the route guard
aborts each disallowed request type and caches a refused host; the browser
wiring asserts the proxy, `<-loopback>` bypass, service-worker block and
route guard. Gates: site_evidence 23 OK (was 15), web 78 OK, ruff clean.
Limitation recorded: no real-browser smoke on this machine (playwright absent
from both interpreters), so the enforcement is proven at the proxy and route
seams, not against a live Chromium.

Landing commits 7ee0bf0 + 8b48d52. Evidence posted on #426; status mirrored
to #442 and #295.

## 2026-11-04 - #270 launch_app behind a declared boundary

`driver_core` gains `Action("launch_app", MUTATING, "launch_app",
("app_name",))`. The issue's `execute_launch_app` in `executor.py` is stale
against the tip: handlers live in `executor_registry.py` and the OS boundary is
`osal.py`, so the action lands there. The executor resolves the logical name
against code-owned `APP_ALLOWLIST` (`notepad`, `freebuff`; one spelling per
platform) and starts only that declared value through `osal.launch_app`:
Windows `cmd /c start "" <exe>` (the empty title is required), macOS `open -a`,
Linux `xdg-open`; no shell, stdio discarded, and a launcher that has not handed
off in `LAUNCH_TIMEOUT` = 3.0 s is killed and the action fails.

Two independent gates. `DRIVER_ALLOW_WRITE` registers write executors; starting
an application additionally needs `DRIVER_LAUNCH_APP` (default off).
`driver_core` reads only its own environment namespace, so the Harness host maps
`HARNESS_NL_CONTROL` onto `DRIVER_LAUNCH_APP` in `harness/server.py`; write
authority alone never confers launching. `ExecutorRegistry.note_for` makes the
refusal name the gate that is actually closed, so a writes-on / launch-off build
does not blame the write switch.

15 hermetic tests in `tests/test_driver_core/test_launch_app.py`: declaration and
parameter shape, code-owned allowlist, per-OS argv and `shell=False`, a value
outside the declaration is refused, per-platform gap, unknown platform, empty
declaration, the 3 s kill, default-off, two-gate registration, the per-gate note,
consent still required, and `Driver` wiring. Drop-fails: removing the launch gate
reds the gate tests, `shell=True` reds the argv test, and dropping the timeout
kill reds the handoff test. No real application is launched on this machine; the
OS boundary is proven at the argv and kill seams.

The vocabulary addition shifts the fixed audit run's decision probabilities from
14 to 15 actions, so `test_audit_compat` now pins `CURRENT_CHAIN` for byte
stability and keeps `GOLDEN_CHAIN` for the pre-owner compatibility claim; the
conformance action count moves to 15. A pre-existing `F821` on the opt-in
native-input path (`osal.native_input_backend` referenced a `_WindowsWindowApi`
that does not exist anywhere) now fails closed with a named `RuntimeError`, with
a test; found by widening ruff to `driver_core`.

Gates at f2c9faa: driver_core 425 OK (1 skipped), conformance 26, router 26,
packaging 5, architecture 9, ruff clean (CI scope and `driver_core`), secret scan
0 hits.

Queue note, same day: owner steering posted on #442 at 2026-11-03 23:36 HST
(D3 decided; STEP 0 is the Jev inclusivity audit report; the main-to-green
drive runs on the CircleCI port because Actions is billing-locked). That
steering supersedes the #272 next-item order: the report comes first, then the
main-green drive, then the remaining plan items. The report is not on the lane
-- no file in `reports/`, no commit, no issue comment -- so it is recorded as
the immediate next item, not as delivered.

## 2026-11-04 - STEP 0 Jev inclusivity audit

Report: `reports/jev-inclusivity-audit-2026-11-04.md`, audited at 89cd74c.
Eight surfaces carry file:line anchors: decision-gate v1 fidelity (the
composer applies all eight rules and gates on `advances_goal`, never Choice
confidence), the operative prompts (clean), the direct TypeSafe wire path (one
evaluator, one policy factory, no `chat` or provider routing), the three
distinct bars, receipts, the Lucas Gate v2 rate, and the #325 flexibility
rule. Two findings are not passes: Lucas Gate v2 has 0/10 machine receipts
over the last ten lane-driving commits (the prose score lines carry no
`artifact_sha256`), and the steering's "Rule 8" has no definition on the
lane. Cure: a supersession note in `docs/jev-calibration.md` and
`tests/test_jev_doc_fidelity.py`, which fails if the note is dropped, the
policy sentence is removed, or an operative prompt states a confidence gate.


## 2026-11-04 - main-to-green drive (lane head 43134f0)

Replayed base main's failing legs at the lane head instead of trusting the
annotation. Full hermetic suite at 43134f0: 5509 tests OK (skipped=73) in
383.170s (`python -W error::ResourceWarning -m unittest discover -s tests -v`),
exit 0.

- Main's hermetic red was #446's wall-clock freshness test. The lane cure is
  already present (6a4988d, with the refresh path a8c6be5): the test no longer
  reads the clock and `harness/routing_table.py:165` names the command that
  actually refreshes the artifact. The current test is
  `tests/test_economics_cli.py:332`; it passes in the 5509-test run.
- The bench proof inherited the operator's `mcp_allowed_roots`; the hermetic
  seam now clears it at `tests/test_cli.py:696`.
- D7 was real and lane-exposed: the resolver only knew Harness modules, so it
  called the actual `docs/driver.md` driver_core migration note stale.
  `audits/self/audit.py:1179` now resolves the sibling package and its
  extractors; `tests/test_audit_doc_modules.py:20,27` is the drop-fails pin
  (invented names still fail closed).
- D10 named three public modules with no module-map row. Added
  `CONTRIBUTING.md:290` (`restart_policy`), `:318` (`mcp_log`) and `:349`
  (`workspace_audit`).
- D12 is NOT green locally and is recorded as a limitation, not a pass. On
  Python 3.14 the stdlib trace records zero harness lines
  (`harness/__init__.py:11`; `changed-line coverage was not measured --
  failing closed`), and on 3.11 the traced battery runs roughly one test per
  second and cannot finish 5,509 tests inside its 1200s cap on this Windows
  host. The audit leg in CI is Ubuntu 3.11; it was skipped by the billing
  lock. Mechanical A/R/SM are 10.0; SD is 7.5 with D7/D10 now cured and D12
  still unmeasured locally.
- Gate set at 43134f0: driver_core 425 OK (1 skipped), conformance 26,
  router 26 (`tests.test_router_meta_backend`), packaging 5, architecture 9,
  ruff clean in the CI scope and for `driver_core` and `audits`, repo_cards
  clean, secret scan 0 hits.
- CI at 43134f0: run 41815506012, 15 jobs, zero steps, every runnable job
  failed and audit/package were skipped. The annotation on the new repo home
  is still "The job was not started because your account is locked due to a
  billing issue." No code signal; not a regression.

## 2026-11-04 - #422 citation identity and claim provenance (slice)

`harness/citation_verify.py` is the deterministic, hermetic verifier for
bibliography identity and claim-to-source provenance. DOI metadata comes from
an injected resolver (a callable or `resolve(doi)` object), so tests never
touch the network. Supplied title, authors, year, venue and version are
compared field by field: a DOI or core-identity mismatch is `contradicted`,
a venue/version difference is `partially_supported`, and a missing resolved
field, resolver error, rate limit, paywall or conflicting registry answer is
`not_verifiable`. A DOI-less source keeps its stable URL and stays unknown.

Claim evidence must be a lifecycle-admitted `SourceRecord` with an exact
passage (full-text SHA-256, 2,000-character bounded excerpt) plus a locator
and retrieval timestamp. Jev may advise semantic support but cannot supply
provenance; without it the claim stays `not_verifiable`. The citation-verified
label is code-gated by `audit_citations`: every citation and every claim must
be strictly `supported`, and unresolved or partial items are listed rather
than dropped. `tests/test_citation_verify.py` has 23 hermetic cases covering
exact match, wrong DOI, title/author/year mismatch, preprint versus published,
partial and contradicted support, missing passage, paywall, resolver timeout,
rate limit, metadata conflict, DOI-less sources, hash pinning and the label
gate. Module row: `CONTRIBUTING.md:333`.

Split, recorded: the issue's live DOI/publisher registry adapter and the
#342/#344 research-lifecycle wiring are not on the lane yet, so #422 remains
open. This slice proves the verifier contract and the label gate; it does not
claim an end-to-end report.

Gates: citation slice 23 OK, ruff clean, D7 doc-module pin 2 OK.
