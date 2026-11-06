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

- #272 VISION VERIFICATION (cure fd653f4, 2026-11-04): implemented the issue's acceptance contract rather than marking the planned code as done. `driver_core/vision_verify.py:337` `VisionVerifier` captures via `ScreenSource` and the existing Jev transport; launch prompt/threshold/polls, text prompt, 2s capture/5s verdict budgets, and the fail-closed timeout path are at `:56-57`, `:383-391`; `driver_core/driver.py:413-445` gates successful non-dry-run `launch_app`/`type_text` with `stopped_at="verification"` and `verification_failed`; `driver_core/config.py:203-206,357` makes it a default-off `DRIVER_VISION_VERIFY` opt-in requiring `DRIVER_SCREEN`; audit/stop surfaces are at `driver_core/audit.py:60`, `driver_core/server.py:57`, and `harness/perception_client.py:92,98`. The versioned `launch-verify@1.0.0` pack (`driver_core/vision_verify.py:200`) scores 8 fields as Choice+Score/16 questions at a 95 floor, fail-closed. Evidence: 25 hermetic tests in `tests/test_driver_core/test_vision_verify.py`. The S3 live calibration corpus/freeze is not claimed: it needs screenshots from the installed FreeBuff and a live key, both absent here; named follow-up on #272. Also repaired three stale audit anchors shifted by this change (`reports/agent-desktop-audit-2026-11-01.md`, `tests/test_agent_desktop_audit_report.py`, fd653f4). Close on merge SHA only.

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

## 2026-11-04 - #292 closeout: driver_core 3.5.0 pin and live drift check

The 3.5.0 reconciliation itself was already landed at `76fb1cf` and is an
ancestor of the current tip; `driver_core.__version__` is 3.5.0 and
`driver_core/__init__.py` resolves to this checkout. The recorded WIP
`a30dbf6` is an earlier, superseded attempt: it predates the later lane work
and is not merged. Its nine named failure classes are green at the tip:
125 focused vision/audit/declaration/service/consent-ledger tests, 26
conformance tests, and `driver_core --json verify` reporting a verified
2,233-record chain.

The remaining acceptance gap was the live drift check and the stale vendoring
pin, both now closed at code head `39695db`:

- `driver_core/UPSTREAM.json` pins repository `Treystu/driver-core`, version
  `3.5.0`, commit `47d4362`, and the raw HTTPS source.
- `scripts/check_driver_core_drift.py` compares the vendored `__version__`
  with the pin offline (`:82`) and, with `--remote`, reads the standalone raw
  `driver_core/__init__.py` (`:69`) and fails if the versions differ; a fetch
  failure is a drift failure, never a pass (`:129` CLI).
- `.github/workflows/ci.yml:120-122` runs the remote check in checkout
  hygiene.
- `tests/test_driver_core_drift.py` has 11 hermetic drop-fails cases
  (`:23-119`): shipping pin equality, planted pin/vendored/remote mismatch,
  remote fetch failure, missing version, malformed pin, CLI exit codes, and
  docs/CI wiring.
- `docs/driver.md:346` now names 3.5.0 as vendored; `:363-368` records the
  measured 3.4.0-to-3.5.0 delta and the parallel-line reconciliation;
  `:388-394` documents the live check. `driver_core/__init__.py:10` carries
  the 3.5.0 changelog entry.

Verification at `39695db`: full hermetic suite **5,543 tests OK (skipped=73)**
in 480.837s; driver_core 425 OK (1 skipped); conformance 26; router 26;
packaging 5; architecture 9; citation 23; D7 pin 2; drift 11; ruff clean in
CI scope, driver_core and audits; repo_cards clean; secret scan 0 hits; live
remote check returned upstream 3.5.0 == vendored 3.5.0. No `[Unreleased]`
CHANGELOG entry.

CI at `39695db`: run 41819905848, 15 jobs, zero steps, every runnable job
failed and audit/installed-package were skipped; the annotation is still the
owner billing lock ("The job was not started because your account is locked
due to a billing issue"). No code signal; protection was not relaxed.

## 2026-11-04 - #272 vision verification (lane/burndown)

Implemented the acceptance contract against the tip, not the issue text.
`VisionVerifier` (`driver_core/vision_verify.py:337`) captures through
`ScreenSource` and asks the existing Jev transport the exact issue questions:
`Is the application '<app_name>' visible and focused?` at 0.85 with polls at
2s and 5s, and `Is the text '<text>' visible in the screenshot?` at the same
bar immediately after typing. Capture is capped at 2s and a verdict at 5s
(`:56-57`); either overrun is a timed-out attempt, never a pass. The gate in
`driver_core/driver.py:413-445` runs only for a successful non-dry-run
`launch_app` or `type_text`, and a failed check returns
`stopped_at="verification"` / `verification_failed` rather than hiding behind
executor success. `DRIVER_VISION_VERIFY` is a third default-off opt-in
requiring `DRIVER_SCREEN` (`driver_core/config.py:203-206,357`); with either
off there is no capture, Jev call, audit record or `verification` value, and
the pre-existing action path is unchanged. The audit kind is
`driver_core/audit.py:60`; the wire reason and stop tier are
`driver_core/server.py:57` and `harness/perception_client.py:92,98`.

The versioned `launch-verify@1.0.0` pack
(`driver_core/vision_verify.py:200`) scores the eight launch fields as a Choice
plus 0-3 Score each (16 questions), passes only on manifest agreement with a
score at the 95 floor, and fails closed on missing, undeclared, invalid or
`not_visible` answers. Screenshot bytes are never written to the audit chain.

Evidence: 25 hermetic tests in `tests/test_driver_core/test_vision_verify.py`,
including both prompts and bars, the 2s/5s polls, slow capture and slow
verdict, malformed answers, flag-off doing no work, pack floor/agreement, the
sentinel non-leak, Driver integration and dry-run behavior. The change shifted
three audit-report anchors, and the full suite caught them: the report and its
executable guard now point at `harness/perception_client.py:183` and
`docs/driver.md:347,360` (`fd653f4`).

Gates at `fd653f4`: driver_core 450 OK (1 skipped), conformance 26, router 26
(no direct `chat()` Muse id), packaging 5, architecture 9, citation 23, D7 pin
2, repo_cards up to date, ruff clean in the CI scope plus `driver_core` and
`audits`, driver_core drift 3.5.0, secret scan 0 hits. Full hermetic suite at
the head carrying this row: **5,568 tests OK (skipped=73)**, exit 0. It follows
a 5,568-test run at `1322a65` whose only failures were the three stale anchors
above, repaired before this run.

S3 disposition, recorded rather than dressed as a pass: the live calibration
corpus and freeze are not produced on this lane. They require screenshots from
the installed FreeBuff build and a live key; neither is present. Named
follow-up on #272: capture the eight launch fields from an installed FreeBuff,
freeze the corpus alongside `launch-verify@1.0.0`, then run the live per-field
scoring and attach the receipts. #272 remains open until a merge SHA.

CI read at the head carrying this row: recorded on #272 as-is; no protection
was relaxed.

Re-verified at `0234946` on 2026-11-04, with no code redo: the enforcement is
still on the renderer path -- `harness/site_evidence.py:39` validates the
initial HTTPS URL, `:220` is the loopback CONNECT proxy with one-resolution
address pinning, `:355` revalidates every browser request, `:428` launches
Chromium behind that proxy, `:441` blocks service workers and `:443` installs
`context.route("**/*")`. Focused evidence: `tests.test_site_evidence` 23 OK
(`EgressProxyTests:435`, `RequestGuardTests:526`, `EgressWiringTests:571`) and
`tests.test_web` 78 OK, 101 total. The module docstring now states exactly that
boundary. #426 remains open until a merge SHA; there is no new code or
follow-up disguised as a landing.

## 2026-11-04 - #273 S4 queue note: blocked before code

The recorded S4 re-scope (issue comment 2026-11-03T01:24:32Z) defers the NL
endpoint with #271 and requires a manifest-declared launch plan,
assignment-bound preauthorization, a fail-closed pre-launch checklist and one
real queue item launched under a frozen protocol. The tip has no launch-plan
path: `harness/server.py:924` `run_driver_task` accepts goal/target/schema and
max steps; `:1167` `RUNNERS` has no `/driver/nl` and `:2249` only dispatches
the driver task; `harness/task_lifecycle.py:222` sends only goal, max_steps and
target; `harness/mission_record.py:209` validates a mission spec with no step
list. There is no `missions/` directory in this worktree and no operator queue
pack or frozen launch protocol. Named follow-up on #273: the operator supplies
that pack and freeze; #271 stays deferred. No implementation is claimed, and
this is a disposition, not a pass.

Re-checked once this session: `tools/harness-verify` absent, no
`route-contract` reference, no `generality` specimen; H1/H2 stay parked.

## 2026-11-05 - main 5eb9c363 leg-by-leg read, the rail, and the assumption register

Primary reads this pass (2026-11-05, this host):

- base main is `5eb9c363` (`git fetch origin main` + `git log`); its check-runs
  (`GET /commits/5eb9c363/check-runs`) are 6 failure / 8 success / 1 skipped of
  15. The six red legs: `audit`; `hermetic suite` on macos-latest-3.11,
  ubuntu-22.04-3.9, ubuntu-latest-3.11, ubuntu-latest-3.13 and
  windows-latest-3.11. `changed-file typecheck (pyright)` is the skip.
- All six red legs have ONE cause, and the `audit` leg is downstream rather
  than independent (`gh run view 41576644039 --log-failed`): the five hermetic
  legs fail only on `tests/test_economics_cli.py:226`
  `test_check_fresh_emits_tier_snapshot_freshness` -> `_cmd_economics` ->
  `harness/cli.py:1094 sys.exit(2)` because the shipped snapshot aged out
  (`SystemExit: 2`). The audit leg logs
  `[FATAL] D12: D12 cannot obtain current-tree coverage (traced battery exited 1)`
  and `ABORTED: battery failed under trace; no baseline written` with the same
  `SystemExit` in that trace. Curing the test therefore clears D12 as well; no
  second code cause is hiding behind it.
- Cure is already on this lane: `ad98937` (#446) takes the test off the wall
  clock with a just-captured fixture snapshot and `7627e5a`/`6a4988d` wire the
  refresh writer the freshness message advertises. Verified locally this pass:
  `python -m unittest tests.test_economics_cli` -> 24 tests OK.
- The Actions rail is dark, not merely slow (`GET
  /commits/0f9716e/check-runs` + `/actions/runs/42001069443/jobs`): 15 checks
  created, every job `steps=0`, `started_at == completed_at` to the second, and
  the annotation reads "The job was not started because your account is locked
  due to a billing issue." Main's 2026-11-03 run 41576644039 did execute, so
  the lock is current, not historical. Hosted Actions cannot verify any head
  until the owner clears it; the local battery is the interim rail and the
  CircleCI port is the durable one.
- Branch protection was NOT relaxed and is intact (`GET
  /branches/main/protection`): 11 required contexts (Checkout hygiene, audit,
  the five hermetic legs, the three installed-package legs, package),
  `strict: true`, `enforce_admins: true`. A port that changes these contexts is
  a protection change and stays owner-reserved.

### Assumption register (R5: assumption, owner, verify-by, method)

| # | Assumption | Owner | Verify by | Method |
|---|---|---|---|---|
| A1 | The six red legs on main clear with the #446 cure, with no second cause behind them | lane | 2026-11-08 | check-runs on a fresh main head after the merge on the Actions rail, or the CircleCI rail on main |
| A2 | Hosted Actions stays unusable until the owner clears the billing lock | Treystu | 2026-11-07 | steps count + annotation on a fresh lane run (API); a job that executes voids this row |
| A3 | The CircleCI VCS connection is pending the owner's act, so a ported config stays inert until then | Treystu | 2026-11-10 | CircleCI pipeline list after the connection is reported |
| A4 | The 11 required contexts above are the merge bar and are unchanged by the port | Treystu | 2026-11-06 | `/branches/main/protection` read before and after any port landing |
| A5 | The queue-bus design of record is the four invariants named in the dispatch (per-repo queue folders; queued -> claimed -> running -> checkpointed -> done\|blocked\|parked; watcher dormant on an empty queue; launches supervised until calibration freeze) | Treystu | 2026-11-08 | owner confirmation or the out-of-repo design record; the detail is not in this repo |
| A6 | The S3 calibration corpus needs screenshots from an installed FreeBuff plus a live key, neither present on this host | lane | 2026-11-08 | presence check for the screenshot corpus and a keyed driver health read |
| A7 | D12 stays unmeasurable on this host (py3.14 records zero harness lines under trace; py3.11 cannot finish the traced battery inside its cap) | lane | 2026-11-10 | a D12 run that writes a baseline locally; until then it is a recorded limitation, never a pass |

No row above is overdue at this date. An overdue row is a blocker in the lane
status; none of them blocks work this pass.

## 2026-11-05 - queue bus + laptop watcher, vision driver item 4 (90a1f81)

The dispatch's item 4 ("build the queue bus + laptop watcher to the gated
design") had no code in the tree: no `queue` module, no watcher, and the S4
note's own re-scope had recorded that the launch path had no queue contract at
all. `harness/queue_bus.py` is now that contract, built to the four invariants
the dispatch names and nothing beyond them.

- **Per-repo queue folders.** `<root>/<repo>/{items,checkpoints,events.jsonl}`
  plus a per-repo `.lock`; repo and item ids are validated to a single path
  segment, so a crafted id cannot leave the queue root.
- **The state machine.** `queued -> claimed -> running -> checkpointed ->
  done|blocked|parked` is a table, not a convention: an unlisted move is
  refused by name, `done` is reachable only from `checkpointed` (a pass with no
  resume point has no evidence it got anywhere), and `done` is final so a
  resume cannot replay a completed pass. `blocked`/`parked` are holds, not
  verdicts; only an operator requeue returns them, recorded with the operator's
  name, and that requeue clears the claim.
- **Ownership.** Every move after the claim must come from the claim's owner; a
  second watcher cannot advance another actor's item, which is what makes
  `claimed` mean anything.
- **Audit.** One append-only event per move (from, to, actor, reason, attempt)
  with `fsync`, items written through `osal.atomic_write_text` under the repo
  lock, and a corrupt event line fails closed instead of being skipped.
- **Watcher.** Dormant on an empty queue with no event per idle poll; one item
  per step so a stuck launch cannot swallow the queue; never writes `done`;
  writes a failed launch as `blocked` with the error text; and refuses to claim
  at all unless the run is explicitly `authorized`, because D3 keeps launches
  supervised until calibration is frozen and the TM proof is complete.

Surface: `harness queue status|enqueue|watch --root ...` intercepts before
settings load (like `serve`/`desktop`/`media`/`driver`), so a laptop watcher
starts on a machine with no provider key. Exit 0 = read or claimed, 1 =
refused, 3 = supervised hold.

Evidence, real path driven (this host, 2026-11-05): `queue status` on an empty
root printed `empty (no repos)` and exited 0; `queue enqueue` created
`harness/tm3-item-1` in `queued`; `queue watch` without `--authorized` printed
the supervised hold and exited 3, leaving the item `queued`; `queue watch
--authorized` claimed and started it (`claimed ... nothing was started`
without a launcher) and `queue status --json` then reported `running=1` with
`items/tm3-item-1.json` and `events.jsonl` on disk. Hermetic:
`tests.test_queue_bus` 23 OK, `tests.test_cli` 64 OK, `ruff check harness
tests scripts examples/oc_handoff` clean. The parser-surface pin gained
`queue` in the early-intercept set (the assertion stays an exact equality).

Not claimed: nothing here launches an application. The launcher is a
caller-supplied hook; TM3 (one real queue item under the frozen protocol) is
still ahead of us and still supervised. The module-map row is in
CONTRIBUTING.md so the D10 check sees it.

## 2026-11-05 - CircleCI port mapping of record (d21985a, TRACK B slice 1)

TRACK B says the port's config work starts now and asks for the 11-check
mapping to be recorded. The port itself cannot be validated yet -- there is no
CircleCI VCS connection, no `jev-gates` context, and no machine runner -- so a
`.circleci/config.yml` written today would be an untested rail. This slice
lands what can be verified instead, and refuses to imply the rest exists.

`docs/circleci-port.md` records, from primary reads: the 11 required contexts
(`GET /branches/main/protection`, 2026-11-05), the GitHub job id that carries
each, the planned CircleCI job name, and the gate commands each job must run;
the six lane-only jobs that are ported for parity but are NOT required
contexts today (including the new py3.14 leg); the four activation
preconditions; and the rule that switching the protection contexts is an
owner-reserved protection change, never part of a port.

`tests/test_circleci_port_mapping.py` (8 OK) pins the mapping against
`.github/workflows/ci.yml`: a renamed job, a dropped matrix leg, or a changed
gate command fails the suite, so the port cannot be built against a workflow
that no longer exists. Ruff clean.

Blocked on the owner (register rows A2/A3): the Actions lock and the CircleCI
connection. Neither blocks other lane work.

## 2026-11-05 - #301 cure landed from the flush (driver_core back in the ruff scope)

The flush read found the Lane's `ci.yml` linting `scripts` but not
`driver_core`, so the dead import #301 reports was live on the lane while a
commit that appeared to cure it (`b5f92fd`) sat only on
`codex/301-driver-core-ruff-scope-20261026` and never reached this branch. The
one-line cure is restored and the scope now reads `harness tests
examples/oc_handoff scripts driver_core`; `python -m ruff check driver_core`
passes on this head. #301 therefore moves from "commit exists somewhere" to
"landed on lane, unmerged" in the flush report, which is the disposition the
evidence supports.

Reminder for the next scope edit: this list has already lost `driver_core` once
to an edit that added `scripts`. Both belong in it.

## 2026-11-05 - order (c) delivered: issue flush + dissemination adoption

`docs/audit/ISSUE_FLUSH_2026-11-05.md` dispositions all 80 open issues and the
tracker rows the lane owes, each with the read it rests on: 23 landed on lane
(unmerged), 4 landed-partial, 9 owner-only, 43 still-open, 1 tracker; zero
closed, because closure needs a merge SHA. Reads are coded R1-R5 with
timestamps, and a row with only the issue-list and ancestry reads is recorded
as still open rather than guessed at.

`docs/audit/DISSEMINATION_ADOPTION_2026-11-05.md` is the adopter-facing
summary: register location (rows A1-A7), board rows with those counts, the
remedies actually adopted (R4/R5/R1) and the one explicitly not adopted (R7,
no non-author conformance read exists yet), and the list of things no artifact
backs. `tests/test_dissemination_adoption.py` (10 OK) fails if the summary's
counts drift from the report's table or if any row loses its read.

No CI-green claim appears in either document: the rail is billing-locked, so
this head has no check-run evidence and none is asserted.

## 2026-11-05 - order (d) drafts filed (drafts only, no canon change)

`docs/audit/INSTRUMENT_AMENDMENTS_2026-11-05.md` carries the four drafts the
dissemination directive assigned to this lane: the v7 verification extension
(all external state, `(read: method, timestamp)`) with the non-code finish rule
(R1 sheet plus R7 non-author verdict); the v7 audit debt resolved as **re-audit
on first-run specimens** rather than a re-issue, with four named specimens from
this lane and the explicit statement that the audit itself was NOT run for want
of a judge-capable run (no score is recorded); the Assessment Gate conformance
amendment written against `docs/JEV_GATE_POLICY_V1.md` as it exists here; and
the Prompt Accuracy Gate extension, whose canonical text is not in this tree, so
two of its five checks are left to be supplied rather than invented.

Nothing entered a canon. The Lucas Gate v2 and the Jev house method are left
unchanged, as the directive requires.

## 2026-11-05 - queue launcher: a claimed item reaches the real launch gate

The queue bus and watcher landed earlier at `90a1f81`, but `harness queue
watch --authorized` built its watcher with **no launcher**, so a claimed item
was recorded as `claimed; no launcher configured, so nothing was started` and
never reached the driver. Order (b)'s requirement -- an item that would trigger
a launch goes through the real launch path and stays subject to the existing
launch gate -- was therefore unmet at the CLI surface even though the module's
own invariants held.

`launch_item()` now supplies the launcher. An item asks for a launch only by
carrying `{"launch_app": <declared name>}` in its payload; anything else is
claimed for the lane and starts nothing. When a launch is requested the
launcher builds the driver from settings (`driver_from_settings`, so the audit
chain is the configured one), composes `HARNESS_NL_CONTROL` onto
`launch_app_enabled` with the same host mapping #270 uses, and executes the
action through `Executor` -- which means write authority, the launch switch,
the code-owned allowlist and per-action consent all apply unchanged. A closed
gate or an unlisted app is a refusal, not a quiet no-op: the launcher raises
and the watcher records the item `blocked` in the gate's own words. A refusal
that arrives as `ExecutionResult(ok=False)` is raised too, so the launcher can
never report a launch that did not happen.

Evidence, real path, this host 2026-11-05:

- gate unset: `queue status` on an empty root -> `empty (no repos)`, exit 0;
  `queue watch` without `--authorized` -> `supervised_hold ...`, exit 3, item
  still `queued`.
- `--authorized` with the launch gate closed -> `claimed: launch failed: action
  'launch_app' names executor 'launch_app', which is not registered ...`, item
  `blocked`, zero spawns.
- `--authorized`, gate on, unlisted app -> `launch refused for 'calc': ... not
  in the declared launch allowlist; declared: ['freebuff', 'notepad']`, item
  `blocked`, zero spawns.
- gate on, allowlisted app, OS hand-off substituted at `subprocess.Popen` ->
  `launched notepad: started`, item `running` owned by `laptop-watcher`,
  attempt 1, argv `['cmd', '/c', 'start', '', 'notepad.exe']`, `shell=False`,
  events `queued -> claimed -> running`.

Tests: `tests/test_queue_launcher.py` (5) alongside the unchanged
`tests/test_queue_bus.py` (23). The design of record is still assumption row
A5 -- the four invariants named in the dispatch, owner confirmation due
2026-11-08 -- and this slice adds no invariant beyond them.

## 2026-11-05 - FLUSH-0: the saturation contracts, no live calls

Order (d)'s four instrument drafts are all filed (`docs/audit/INSTRUMENT_AMENDMENTS_2026-11-05.md`);
the register's next unblocked item is therefore the commission's first flush
phase. FLUSH-0's line in the commission of 2026-11-04 reads: "contracts + gap
register + ledger events, no live calls. Acceptance:
`test_pass_refused_without_delta`; `test_whitespace_delta_is_not_a_delta`;
`test_register_has_no_noted_state`."

`harness/flush_loop.py` is those contracts and nothing else -- no judge, no
panel, no rotation, no network, no process. What it enforces rather than
documents:

- **Rule 0.** A pass is admitted only when its information set carries a
  semantic delta: a new or changed evidence record, a changed target digest, a
  new angle id or a new instrument id. Formatting churn -- line endings,
  trailing whitespace, blank lines -- canonicalizes to the same digest and the
  pass is refused by name (`PassRefused`), because two passes over one
  information set is one pass counted twice. Leading indentation is kept: in
  code and quoted blocks it is structure, not churn.
- **The gap register has no "noted" state.** A finding leaves only as
  `closed` (a landed cure *and* the read that re-scored the changed artifact;
  either one missing is refused) or `decision_ready` (a packet and the named
  owner act). `register.note(...)` exists solely so the attempt gets one loud
  answer.
- **Materiality and the tail.** A finding counts when it moves an axis, blocks
  a user-visible behavior, violates an invariant or changes a ship decision;
  everything else sits in a per-bucket tail and promotes at five in one bucket.
- **Ledger events.** One JSON line per move, fsynced, and a corrupt line fails
  closed instead of being skipped.

Tests: `tests/test_flush_loop.py`, 12 tests including the three acceptance
names verbatim. `python -m unittest tests.test_flush_loop` -> `Ran 12 tests`,
`OK`. Ruff clean on both files.

Scope discipline: FLUSH-1 (core loop, Jev-only rotation, CLI), FLUSH-2 (panel
and deterministic rotation, oscillation breaker, calibration pinning) and
FLUSH-3 (cold run, MCP, canary gate) are not started -- this slice builds the
layer they call, and a loop added now would be an untested claim about
saturation, which is the thing the loop exists to prevent.

## 2026-11-05 (second pass) - FLUSH-0 landed; the second main red re-read

Reads this pass:

- FLUSH-0 on this lane: `7c2f094`, `python -m unittest discover -s tests -t .`
  -> `Ran 5644 tests in 366.741s`, `OK (skipped=73)`, exit 0 (local battery,
  this host, 2026-11-05). 5632 before, 12 new.
- Main's `audit` leg carries a SECOND red the first pass folded into the
  snapshot cause. Its log line is
  `D10 FAIL CONTRIBUTING current -> stale: []; uncovered: ['meta']`
  (`/actions/runs/41576644039/jobs/125721807595`, read 2026-11-05): the
  `harness/meta.py` map row is missing on main. The cure is already on this
  lane, `47869e8` ("...; cover meta in the map"), and `sd_contributing()`
  reads `1.0 CONTRIBUTING covers every public module` here. D12's failure is
  still downstream of the aged-snapshot test alone.
- The same rule then caught this pass's own module: adding `harness/flush_loop.py`
  made D10 read `uncovered: ['flush_loop']`. A new module without a map row is
  exactly the defect D10 exists to catch, so the slice was not finished until
  the row landed (`7a0b45b`).
- A7 re-confirmed on this host, not assumed: the local audit run
  (`python audits/self/audit.py`, py3.14) exits 0 with `R13 pass`,
  `D10 pass`, and `D12 skip (fail-open)` whose note is
  `ERROR: harness/__init__.py:11 was not executed; recorded lines: []` --
  zero harness lines recorded under `trace`, the py3.14 behaviour A7 names.
  D12 is therefore a recorded limitation here, never a local pass; the merge
  gate for it is the rail.
- Nothing in this pass touches branch protection, and no CI-green claim is
  made: on `7c2f094` the hosted check-runs are 13 failure / 2 skipped with
  `steps=[0]` (billing lock), read 2026-11-05T12:38:32Z.

## 2026-11-05 (third pass) - FLUSH-1 landed; the interface edge and two loop guards

FLUSH-1 was already pushed in the previous session (`17adbf4`, path fix
`d6adf51`) and posted as landed. Re-running the full battery found a
regression the focused slice had missed: `_cmd_flush` imported `flush_loop`
inside the function, and `test_no_deferred_policy_imports_outside_main`
refused the deferred policy edge. The import is now at module scope
(`c53c914`); `tests.test_architecture` 9 OK.

The loop was also re-read for FLUSH-1's own scope. Two ways to manufacture
a clean pass existed: an empty `aspects` list reached no judge but still
counted as an expansion, and any instrument id (for example `panel`) counted
as a new rotation although FLUSH-1 is Jev-only. Both refusals now happen
before the judge or the ledger (`d85540b`):

- `test_a_pass_without_an_aspect_is_refused` - empty aspects refused, no
  judge call, no event.
- `test_flush_one_refuses_a_non_jev_instrument` - panel refused by name,
  no judge call, no event.

Evidence on `c53c914`:

- `python -m unittest tests.test_flush_loop` -> 28 OK.
- `python -m unittest tests.test_architecture` -> 9 OK.
- `python -m unittest discover -s tests -t .` -> `Ran 5660 tests in
  635.686s`, `OK (skipped=73)`, exit 0 (this host).
- `python -m ruff check harness tests` -> All checks passed.
- Driven CLI: `python -m harness.cli flush --campaign ... --json` ->
  `stop_ready=true`, `stopped=true`, streak 2, cold run on pass 2, events
  `pass_admitted, pass_admitted, stop`. A campaign declaring `["panel"]`
  exits 1 with the FLUSH-1 refusal and writes no ledger.
- No CI-green claim: hosted Actions remains `steps=[0]` under the billing
  lock (SR-1); this is local execution only.

The issue-flush report row now records FLUSH-0/1 as landed on lane and
FLUSH-2/3 open. FLUSH-2 remains next; the prior pass's audit read is not
claimed as met here.

## 2026-11-05 (fourth pass) - FLUSH-2 landed: rotation, oscillation breaker, calibration pin

FLUSH-2 is complete on `lane/burndown` at `787bb8f`, on top of the
already-landed FLUSH-1 head `d92a73c`. The default loop is still Jev-only; the
new instruments and the escalation path are explicit opt-ins.

What the code now enforces:

- **Instrument rotation by identity.** `PANEL_INSTRUMENT` and
  `DETERMINISTIC_INSTRUMENT` join `JEV_INSTRUMENT`; `FLUSH_ONE_INSTRUMENTS`
  remains the constructor default and `FLUSH_TWO_INSTRUMENTS` is the opt-in
  set. `FlushLoop` accepts a `judges` mapping, keys every verdict and
  repeated-call guard by `(aspect, angle, instrument, information digest)`,
  and logs the sorted instrument set on each admitted pass. A declared
  instrument with no judge, or outside `supported_instruments`, refuses
  before `begin_pass`/`record_pass`.
- **Oscillation breaker.** Closing a material finding records its cure; a
  later verdict that reopens it records a regression edge. A repeated suffix
  cycle of length 1-3 in one aspect becomes exactly one
  `combined_cure_packet` carrying the cycle and the latest cure per member;
  the packet is written as `oscillation_escalated`, the finding stays open,
  and the campaign refuses to claim a stop. The CLI breaks at the escalation
  rather than spending another pass.
- **Calibration pinning.** `pin_judge_version` pins the judge identity; when
  it changes, `scores` rebaselines (to supplied control scores or empty until
  the new judge answers) and `calibration_rebaselined` records both score
  sets. The gap register is untouched: no finding is discarded, reopened or
  re-derived merely because the calibration changed.
- **CLI surface.** `harness flush --extended-instruments` enables the
  panel/deterministic rotation; `instrument_verdicts` supplies their
  verdicts; per-pass `close` specs replay a cure and its read; per-pass
  `judge_version` changes the pin. The default surface still rejects a
  non-Jev instrument by name before the ledger.

Evidence:

- `python -m unittest tests.test_flush_loop tests.test_architecture` ->
  `Ran 43 tests`, `OK`; the FLUSH-2 file contributes 34 tests, including
  `test_oscillation_cycle_escalates_to_combined_packet` and
  `test_version_change_rebaselines_scores_not_findings` by name.
- `python -m unittest discover -s tests -t .` at `787bb8f` ->
  `Ran 5666 tests in 460.732s`, `OK (skipped=73)`; the FLUSH-1 baseline was
  5660, so the six new tests run and no existing test regressed.
- Driven CLI, rotation: `harness flush --extended-instruments` exits 0 with
  `verdicts=3`, scores `{completeness: 96.0, grounding: 90.0,
  risk_honesty: 95.0}`, and `pass_admitted.instruments =
  [deterministic, jev, panel]`. The same campaign without the flag exits 1
  with the FLUSH-1 refusal and writes no ledger.
- Driven CLI, oscillation: three passes over `gap-1` (first open, then two
  closes/reopens) exit 0 with one escalation: `combined_cure_packet`,
  `cycle=["gap-1"]`, `cures={"gap-1":"cure-2"}`, `seen=2`; the ledger has one
  `oscillation_escalated` event and `stop_ready=false` names the escalating
  packet.
- Driven CLI, calibration: a `jev-1` pass with `completeness: 96` and an open
  `gap-cal`, followed by a `jev-2` pass, exits 0 with
  `judge_version=jev-2`, `scores={completeness:70.0}`, a
  `calibration_rebaselined` event (`96.0 -> {}` before the new judge answers),
  and `gap-cal` still open.
- `python -m ruff check harness tests` -> All checks passed.
- Credential/secret scan on the `787bb8f` diff -> 0 hits.
- No CI-green claim: hosted Actions on `787bb8f` has all 15 checks at
  `steps=0` under the owner billing lock (SR-1); this is local execution
  only. Base `main` remains `5eb9c363`; merge is owner-reserved.

Drift recorded: FLUSH-1 was already landed at `d92a73c`; this pass extended
it rather than replaying it. FLUSH-3 (cold run, MCP, canary gate) remains
open.
## 2026-11-05 (fourth pass) - FLUSH-2 non-author conformance read; relaunch-takeover line

A different session authored FLUSH-2 (`787bb8f`) and its evidence record
(`65147cb`). This pass is the non-author conformance read those rules require,
against the commission's FLUSH-2 row -- panel + deterministic rotation,
oscillation breaker, calibration pinning -- rather than against the commit
message:

- Rotation is keyed `(aspect, angle, instrument, identity)`: the same aspect
  under the same angle through a different instrument is new work, and the
  same question twice is marked `repeated` instead of counted. The default
  loop still supports one instrument and refuses others by name, so FLUSH-1's
  refusal survives into FLUSH-2 (`test_flush_one_refuses_a_non_jev_instrument`).
- `OscillationBreaker` refuses a regression with no recorded cure, then looks
  for a repeated suffix of length at most three; `A, B, A, B` escalates once
  as the combined packet `A, B`, naming both cures. A repeat detection of the
  same aspect+cycle returns `None`, and `stop_ready()` refuses saturation
  while an escalated cycle is unclosed.
- `pin_judge_version` is silent on the first pin and, on a change, writes
  `calibration_rebaselined` with scores before/after; the register is
  untouched -- "rebaselines scores, not findings", as the row states.
- No conformance defect found, so nothing is claimed as cured here. The
  evidence this pass re-ran on the same head agrees with the record:
  `tests.test_flush_loop tests.test_architecture` -> `Ran 43 tests`, `OK`;
  full battery -> `Ran 5666 tests in 421.424s`, `OK (skipped=73)`, exit 0.

### Relaunch-takeover readiness line

Required by the oversight steering of 2026-11-05T12:58Z (item 7). Read from
the ledger repo, not assumed: `GET /repos/Treystu/slow-lane-ledger/contents/harness`
lists `ci`, `handoff.md`, `log`, `plans`, `readiness`, `reviews` and no
retrospective file, so

| Retrospective received | Spec filed | Shadow runs | Duty state |
|---|---|---|---|
| not received (read 2026-11-05) | not filed | 0 / 5 | GPT lane (no takeover asserted) |

Takeover stays supervised; the assignment-bound unattended grant still waits
on the ready-to-test milestone and is not asserted here.
## 2026-11-05 (fifth pass) - FLUSH-3 accepted, and the gate it was missing

FLUSH-3 landed at `0ba31ee` from another session; this pass is the non-author
conformance read against the commission's row -- cold-run instrument, MCP
surface, planted-defect canaries -- plus the one thing that row requires and
the tree did not have.

What the code does, read rather than assumed:

- The cold run is an **instrument**, not a claim: a pass that lists
  `cold-run` marks its own pass as the campaign's cold run, and the basis it
  records comes only from that instrument's verdict (`flush_loop.py:770-775`),
  so a Jev verdict cannot stand in as the cold reader's name.
- `flush_run` exists on the MCP surface and serves **both** faces: a campaign
  replay and a canary-suite gate, with allowed-root and `allow_write` gating
  around them (schema at `mcp_schemas.py:724`).
- `run_canary_gate` fails closed on a judge error, a missing planted id, an
  empty suite and an unknown held-out id; it scores the held-out set and
  requires every executed case clean. Rotation is deterministic
  (`rotation=N`), so the canary order changes between runs while a run stays
  reproducible.
- The acceptance names are present and pass: `test_canary_loop_finds_all_planted_defects`,
  `test_mcp_and_cli_read_same_campaign_state`.

The gap this pass closed (`65a8dac`): **no canary suite shipped**, so the gate
could only ever run from unit tests -- a CI gate in name. Now
`packs/flush_canary_suite.json` carries eight planted defects (a claim with no
read, a delta-free pass, a tail flood never promoted, a panel counted as Jev,
an unattributed cold run, a "noted" disposition, a one-pass stop, and evidence
laundering), three of them held out from the score, and audit rule **D13**
runs that file through the same gate the CLI and MCP serve. Tampering a
held-out case both fails the gate and moves the score below 1.0, pinned by
`test_a_suite_that_drops_a_planted_defect_fails_closed`.

Executed on `65a8dac` (local, this host, 2026-11-05):

- `python -m unittest tests.test_flush_loop` -> `Ran 42 tests`, `OK`.
- `python -m ruff check harness audits tests` -> `All checks passed!`.
- `sd_flush_canaries()` -> `1.0 shipped canary suite passes as a gate; score
  1.00 on 3 held-out case(s)`.
- Full battery: `Ran 5674 tests in 397.002s`, `OK (skipped=73)`, exit 0
  (5672 on the pre-D13 head).
- Hosted check-runs on this head remain `steps=0` under the billing lock
  (SR-1): no CI claim, every check above is local execution.

## 2026-11-05 (seventh pass) - S2 #270 driven: the launch gate proven at the OS

Primary read of this plan first, not of a status comment: the rows above record
S1 closed at `9d6fac1` (vision-extractor arity, then
`_INPUT_CAPABILITY_LOCK`), `#292` closed at `39695db` (pin 3.5.0 in
`driver_core/UPSTREAM.json`, `scripts/check_driver_core_drift.py`, CI wiring,
11 drift tests), `#300` closed at `832a059` (`check_batch`), and `#272`
implemented at `fd653f4` with its corpus still blocked (SR-3). So the highest
open item in recorded order was S2 `#270`, and this pass drove it rather than
asserted it.

**Closure re-read on `827a4b6`** (local, 2026-11-05T15:36Z):
`python -m unittest tests.test_driver_core.test_vendoring_seams
tests.test_driver_core.test_launch_app tests.test_driver_core.test_vision_verify
tests.test_nl_control_flag tests.test_driver_core_drift` ->
`Ran 63 tests`, `OK`. The assumption register was re-read the same session:
A1-A7 carry verify-by dates of 2026-11-06 or later, so **no row is overdue** on
2026-11-05 and no row is a blocker today.

**The driven launch.** The real path is the queue's own launcher, not a unit
double: `harness queue enqueue --payload '{"launch_app": "notepad"}'` then
`harness queue watch --steps 1 --authorized`, which builds
`launch_item()` -> `load_settings()` -> `driver_from_settings()` ->
`driver.executor.execute("launch_app", {"app_name": "notepad"},
consent=Consent(True, ...))` -> `driver_core.osal.launch_app` -> a real process.
`--authorized` is the supervised-launch flag; no calibration was frozen. Three
supervised drives, each quoted as observed (this host, 2026-11-05T15:39-15:44Z,
`tasklist /FI "IMAGENAME eq notepad.exe"` read before and after every case):

1. **Defaults (nothing set).** `claimed: launch failed: action 'launch_app'
   names executor 'launch_app', which is not registered; registered:
   ['no_action', 'observe', 'read_value', 'run_probe']. Actions with a side
   effect are not registered because DRIVER_ALLOW_WRITE is off; nothing in this
   build can perform them.` Item `state=blocked`; no `notepad.exe`.
2. **`DRIVER_ALLOW_WRITE=1`, NL flag off.** The refusal moves to the second
   gate and names it: `... Application launching is not registered because
   DRIVER_LAUNCH_APP is off (the Harness host maps HARNESS_NL_CONTROL onto it);
   write authority alone does not enable it.` Item `state=blocked`; no
   `notepad.exe`. This is `#270`'s default-off requirement, observed rather than
   inferred: with write authority open, the launch capability is still absent
   and the operator is told which switch is closed.
3. **`DRIVER_ALLOW_WRITE=1 HARNESS_NL_CONTROL=1`.** `claimed: launched notepad:
   started`; item `state=running`; `tasklist` then showed `Notepad.exe 27112
   Console 2 124,740 K`. The process was closed by its own PID
   (`taskkill /PID 27112 /F` -> `SUCCESS`, and a follow-up `tasklist` filter
   reported `No tasks are running which match the specified criteria`), and the
   next pass touched nothing else.

So the host flag does exactly one thing: it opens the driver's launch switch.
Off, the vocabulary refuses and nothing starts; on, the allowlisted program
starts and the queue records it running.

**The defect the drive found, and its cure.** A refused launch was recorded
`blocked` in the item but *returned* from `Watcher.step` as `CLAIMED`, so
`harness queue watch` printed `claimed: launch failed: ...` and exited **0** --
the queue's own docstring promises `1 for a refusal or error`, and a script
reading the exit code would take a launch that never happened for a started
one. `Watcher.BLOCKED` now names that outcome (`harness/queue_bus.py`), the CLI
returns 1 for it, and the re-drive on the new head shows `blocked: launch
failed: ...` with `watch_exit=1` for the refused item and `claimed: no launch
requested; claimed for the lane` with `plain_item_exit=0` for an item that asks
for no launch. The one test that had pinned `CLAIMED` on a refusal was corrected
with the reason inline, and `QueueWatchExitCodeTests` pins both exit codes.

Verification on the slice head (local, this host, 2026-11-05T18:12Z):

- `python -m unittest tests.test_queue_launcher tests.test_queue_bus` ->
  `Ran 28 tests`, `OK`.
- Full battery: `Ran 5676 tests in 374.131s`, `OK (skipped=73)`, exit 0
  (5674 on the pre-slice head; the two new exit-code tests are the delta).
- `python -m ruff check harness audits tests` -> `All checks passed!`.
- Hosted check-runs remain unusable under the billing lock (SR-1), so no CI
  claim is made for this head; every result above is local execution.

Still open in `#270`'s neighbourhood, recorded rather than silently carried:
`tasklist` cannot distinguish a window that opened from one that failed to
paint, so the launch proof is "the process exists", not "the app is usable";
`#272`'s corpus needs a real installed FreeBuff screenshot set (SR-3) and the TM
ladder needs the owner's supervised receipts (SR-4), so launches stay supervised
and calibration stays unfrozen.

## 2026-11-05 (ninth pass) - order (c): the flush rebuilt from the issues themselves

Order (c) asked for a flush verified from the things themselves, not from this
lane's prior comments. The report was rebuilt at `f608b46` from these reads
(all 2026-11-05, this host): `gh api
"repos/Treystu/harness/issues?state=all&per_page=100" --paginate` at 18:21:38Z
(144 issues returned, 80 open, 64 closed); one `gh api
.../issues/<n>/comments?per_page=100 --paginate` read per row at 18:22-18:30Z;
`git cat-file -e <sha>^{commit}` plus `git merge-base --is-ancestor <sha> HEAD`
for every cited SHA at 18:24-18:26Z on head `d0f74a6`; focused suites at
18:27:10Z (driver_core 455 OK skipped=1, launch_app 15, vision_verify 25,
vendoring seams 5, drift 11, queue_bus 23, queue_launcher 7, fireworks route 28,
dissemination page 10).

What the primary read changed in the report:

- Every row now carries the issue's ask, the disposition, the artifact by branch
  and commit SHA, the proof, and the live state with a close recommendation.
  Totals: 21 landed on lane, 2 landed on main (merged), 4 landed-partial,
  9 owner-only, 43 still-open, 1 tracker, 80 rows, 0 closed.
- **#290 and #304 were labelled lane work that `main` had already merged.**
  `963b6db` (the audit-triage runbook) and `dfdd419` (Fireworks routing and its
  route suite) are both ancestors of `origin/main`, so those two rows moved to
  *landed on main (merged)* and now recommend closing them. The lane gains no
  credit for work it did not do.
- **#270 is recorded as driven, not merely landed** (the seventh pass's three
  supervised queue drives), and #292/#300 carry their executed suites with the
  counts above.
- The adoption page's counts, its remedies table and its "not adopted" list were
  corrected to match: FLUSH-0..FLUSH-3 are no longer "no code", S2 is no longer
  "only landed", and remedy R7 is recorded as *partially* adopted with a new
  **SR-5** naming the missing non-author verdict on the code deliverables.
- `tests/test_dissemination_adoption.py` was rewritten for the new seven-column
  contract: it now also fails if a row lacks an artifact, a proof read or a
  state, or if a landed row does not cite `lane/burndown` and a commit.

Nothing was closed: every row reads `open` and closure is a merge. #449 was not
merged, branch protection was not touched, launches stayed supervised and
calibration was not frozen.
