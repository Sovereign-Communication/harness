# 4-Dimensional Harness Audit — Round 2 Rubric

## Dimensions (each out of 10)

A = **Security** — controls, trust boundaries, fail-closed, credential/spend safety
R = **Reliability** — correctness under error/429/empty/truncation/load/edge cases
SM = **Structural hygiene & maintainability** — layering, ownership, test coverage, lint
SD = **Documentation & release integrity** — docs, changelog, exit codes, MCP parity, freshness

## Scoring convention

- 10 = defect-free on this dimension across the whole product (every check fully satisfied)
- 9.5 = at most one minor, non-safety, non-regression issue below "fully satisfied"
- Each dimension averages the scores of its checks; the dimension score is the average,
  reported to one decimal. The bar is 9.5+ on *all four* dimensions.

## What each dimension measures

1. **A — Security**
   - No `tools` key ever sent in any payload, checked at the one transport/chat seam.
   - Spend ceiling enforced as a guarantee — preflight before every billable call, mid-batch fail-closed, ceiling never silently raised.
   - BYOK handling correct: hard-blocked prefix denylist + learned-prefix rotation, paid-BYOK fail-closed, free-BYOK usable.
   - Key identity / label gate correct (exact match, label never echoed into errors).
   - Verification gate isolation: shlex tokenization + shell=False + timeout, verify_cmd validated before live run.
   - File mutation safety: atomic write, symlink refusal, path containment, mode preservation, failed-run rewind.
   - MCP boundary: allow_verify/allow_write explicit per-call or server config; allowed_roots containment.
   - Consent fail-closed: unparseable/empty/redirect treated as defer, never dispatched.
   - Continuation tamper/identity gates: gate_id bound, tampered/mismatched state refused before key/model/file.

2. **R — Reliability**
   - HTTP transient handling: 429/5xx retry with Retry-After honor and capped backoff; POST only retried before body consumed.
   - Reasoning parameter: auto omit for non-reasoning, capped low for reasoning, retry-once-without on rejection.
   - Output usability: empty/reasoning-only/truncated treated as protocol condition, not mined for content/votes/consent.
   - Panel rotation on any failed seat; judge/specialist fallback when judge/specialist fails; structured tally authoritative even if lanes fail.
   - Apply rotation on error/BYOK/reasoning-only/readiness-defer; broken-gate detection on *identical real* gate outputs only; continuation history without verify_output must not trigger it.
   - Capability deferral: partial preserved in state, never written to tree; resume continues from partial + original baseline.
   - Multi-file batch: one governed session per file, shared budget, fail-fast, batch envelope even on file-1 death, shared-gate verdict from last file's real outcome.
   - Ledger: append-only hash chain, cross-process lock with retry, torn-line quarantine, rebase under lock to prevent fork, repair truncates to valid prefix, segmentation on rotation.
   - Config: range validation on all numeric settings, unknown-key warning, hard ceiling enforcement, config load inside CLI fail-clean path.

3. **SM — Structural hygiene & maintainability**
   - Layering/ownership: data flows one way CLI/MCP -> engines -> lanes -> primitives.
   - One owner per policy: spend in spend.py, transport+usability in chat.py, gate identity in continuation.py, prompt text in prompts.py, disk/gate in filesafety.py, quiet in output.py, result shapes in results.py, composition in session.py, pool ordering in capability.py.
   - No facade/re-export shim (no harness/core.py and none may regrow).
   - Single source of truth for each shape: result vocabulary one def site; engine flags one definition; apply request vocabulary one owner.
   - Engine/router construction owned by session.py only.
   - Test tree mirrors product tree: one test module per owner; shared fakes; hermetic.
   - Architecture test enforces import direction and no-reexport and construction-locality at test time.
   - Ruff hygiene: E/F/W only, no lint warnings.
   - No dead code: validators/lanes with zero real callers are deleted or wired.

4. **SD — Documentation & release integrity**
   - READMEs accurate: CLI surface, exit codes, config table, free-tier defaults, bench format, MCP tool list, convergence/claims/sovereignty docs.
   - Architecture/security/threat docs current and consistent with the code.
   - CHANGELOG follows Keep a Changelog + semver 0.x, [Unreleased] section current.
   - Version single-sourced from pyproject.toml; CLI help text matches reality.
   - MCP parity: CLI feature reachable from MCP server with same defaults where intended.
   - Model freshness: shipped default pool/model ids validated against live catalog; stale id hard-fatals at pricing lookup and is CI-checkable.
   - Dogfood/self-hosting loop documented and wired (ground -> verify -> apply).
   - Contribution guide reflects current module map and ground rules.
