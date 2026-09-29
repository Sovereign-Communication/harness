## Lane 3 verification: PASS-WITH-NITS

**Post-merge review.** All numbers below describe merge commit **`8e5a100098552d4f320ccb443c8d933fdda9e5ac`** (PR #117, merged 2026-09-28T17:31:37Z), verified in a clean detached worktree with a private `HARNESS_LEDGER` and a fresh CPython **3.11.15** venv. The head did not move during the run. I approve nothing and merged nothing; this is the Lane 3 record only.

### What shipped, and is main safe

`.github/workflows/publish.yml` (+158), `docs/releasing.md` (+72), `tests/test_publish_workflow.py` (+278). Nothing under `harness/` was touched — `git diff --stat 8e5a100^1 8e5a100` is those three files and nothing else, so no runtime behaviour changed and **main is safe**. The new workflow is inert until someone pushes a `v*` tag or dispatches it.

### OIDC trusted publishing, judged on its merits

**Triggers.** `push` on `tags: ["v*"]`, plus `workflow_dispatch`. There is no `pull_request` and no branch trigger, so a PR cannot reach this workflow at all.

**`permissions` scoping.** Top level is `contents: read`. The `publish` job adds exactly one grant, `id-token: write`, and nothing else — the job block carries `set(contents, id-token)` and no third scope. That is the minimum surface trusted publishing can work with, and it is the correct design: the OIDC assertion is minted per-run, exchanged for a short-lived upload token, and there is nothing stored to steal.

**Long-lived secret?** None. No `secrets.` reference anywhere in the workflow, no `TWINE_USERNAME`/`TWINE_PASSWORD`, no `pypi_api_token`; the action is invoked with `packages-dir` and `attestations` only. `id-token: write` is the entire credential surface, and a missing publisher registration surfaces as a 403 at pypi.org rather than as a silent no-op. This is the property the PR claims and it holds.

**Can an untrusted ref or a fork reach the publish step?** No, for two independent reasons. (1) A fork pushing `v*` produces a workflow run in the *fork's* repository context, whose OIDC subject names the fork; pypi.org's publisher registration is bound to this repository and workflow, so the exchange is refused. (2) `workflow_dispatch` can only be triggered by someone who already has write access to this repository — it is not an escalation path, it is a re-run of a path the same person could reach by pushing a tag. There is no `secrets.` value to exfiltrate even if a step were compromised.

Two nits, neither of which changes the verdict:

- **No `environment:` on the publish job.** The upload step is gated on `dry_run`, which defaults to `true` — a good second leg — but on a `workflow_dispatch` with `dry_run: false` the upload proceeds with no human approval step. Adding `environment: release` (with required reviewers) would put a person between a writer and a permanent PyPI release. Recommendation, not a blocker.
- **Tag parity is vacuous on the dispatch path when `tag` is omitted.** The three-way parity check reads `RELEASE_TAG` from `inputs.tag`, which is `required: false`. Dispatch with `dry_run: false` and an empty `tag` input and the step prints "no tag in context ... tag parity not checked" and **passes**, so the guard the workflow's own comment calls three-way reduces to two-way on exactly the path a human can trigger without a tag. Again a footgun for the operator rather than an attacker — the version still comes from code-reviewed `pyproject.toml` — but the comment overstates what the step guarantees. `test_tag_version_parity_is_enforced` asserts the *text* of the assertion and so does not catch this.
- **Minor:** `pypa/gh-action-pypi-publish@release/v1` is a mutable tag, not a SHA pin, in the one job holding `id-token: write`. `test_publish_action_is_a_pinned_version_ref` only requires the ref to match `v\d+`, so `release/v1` passes. This matches the convention in `ci.yml`, so it is a nit rather than an inconsistency.

### Gate table (fresh 3.11.15 venv, private ledger)

| Gate | Result |
|---|---|
| `ruff check harness tests examples/oc_handoff` | **OK** — All checks passed |
| `ruff check audits` | **OK** — All checks passed |
| `compileall harness tests examples/oc_handoff audits` | **OK** — rc=0 |
| `unittest -p "test_[a-j]*.py"` (1344 tests) | **1 failure**, 9 skipped — see below |
| `unittest -p "test_[k-z]*.py"` | **OK** |
| `audits/self/audit.py` | **FAIL — BAR NOT MET** (R = 9.29 < 9.50) |
| live `jev-phase --phase PLAT-CI-MATRIX` | **97.25 / 85**, `can_mark_complete=True`, all 8 hard gates green, semantic 90.83 live (`fallback=False, model=jev-1.13.0`) |

**The audit is red, and this verdict is not a clean sweep.** The sole failing check is **R13 "full suite green hermetically" (score 0.0)**, which is what pulls R to 9.29/10 = 13/14 and takes the gate to `BAR NOT MET`; the same condition produces the one sub-10 half-A score. R13 goes red for exactly one reason: `tests/test_judge.py::ShippedModelFreshnessTests::test_shipped_ids_resolve_in_live_catalog` fails with

```
AssertionError: Lists differ: ['inclusionai/ling-3.0-flash-fin:free'] != []
- ['inclusionai/ling-3.0-flash-fin:free'] : stale shipped ids (run `capabilities --check-shipped`)
```

That model has been retired from the live OpenRouter catalog but is still listed as a shipped id at `harness/config.py:213` and `harness/config.py:236` on `main`. The failure is **invisible in CI** because the `test` job sets no key, so the freshness gate self-skips there. **This is pre-existing on `main` and is not caused by #117** — this diff touches neither `harness/config.py` nor `tests/test_judge.py`. It is a real defect on `main` that Lane 3 will file separately; the fix is to drop the retired id from the shipped list, not to weaken the test.

I picked `PLAT-CI-MATRIX` as the live phase because it is the CI-lane phase contract. Worth noting for canon hygiene: its `required_files` are `ci.yml` and `.gitattributes`, so `publish.yml` is invisible to the phase registry and `tests/test_publish_workflow.py` is not named by any `PHASE_CONTRACTS` entry. #117 claims no STATUS row, so nothing in the phase scoring knows this workflow exists. Not a defect in the PR — a gap in the registry.

### Coverage of the new lines

`tests/test_publish_workflow.py` is 278 lines, 11 tests, all passing (`Ran 11 tests in 0.002s / OK`). The coverage is real rather than decorative: the module hand-rolls a stdlib-only YAML subset reader because the project declares zero runtime dependencies and PyPI/YAML is not in the `dev` extras, and — importantly — the reader **raises on any shape it does not understand** rather than returning a partial result. That matters because a reader that silently stopped matching would turn all 11 tests into no-ops that always pass, which is the exact failure mode the module docstring says it exists to prevent. The tests pin: no `secrets.`/token/username reference, top-level permissions read-only, job permissions exactly `{contents, id-token}`, `dry_run` defaulting to `true`, the `v*` tag trigger, the upload step's guard referencing both `dry_run` and `workflow_dispatch`, the absence of a build matrix, the tag-parity assertion text, and that `docs/releasing.md` names the publisher, the 403, and the rehearsal path. The new lines in `publish.yml` are executed by these assertions rather than merely present.

**Verdict: PASS-WITH-NITS.** The OIDC design is sound, minimal, and correctly unreachable from an untrusted ref or a fork; main is unaffected at runtime. The two nits worth acting on are the absent `environment:` approval gate and the tag-parity bypass on a tagless dispatch. The red audit is a pre-existing stale-model defect on `main`, not a #117 regression.
