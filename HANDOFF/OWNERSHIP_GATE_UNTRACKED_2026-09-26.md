# Harness owner handoff — the ownership gate is not in version control, and one cross-lane document is already on main

<!-- HANDOFF-SCOPE-BEGIN -->
scope: Harness
owner: Sovereign-Communication/harness
purpose: Harness-only findings and remediation handoff
foreign_material: NONE
boundary: No foreign-repository findings, evidence, status, or remediation are included.
<!-- HANDOFF-SCOPE-END -->

This is an assist-only handoff. The owning repository retains all decisions, edits, merges, and publication authority.

## A note on how this document is worded

This report describes a cross-lane violation that is already present in this repository. The governance rule it reports on is correct, and this document respects it: **the other product lane is never named here.** The violating material is cited by file path, line number, and commit SHA, which is unambiguous and independently checkable. Any reader can recover the exact strings with one grep against the file named below.

This document also reports a third defect in the gate itself: it has no waiver mechanism, so a document that legitimately must reference the other product cannot be stamped compliant. See *Third defect* below. That limitation is why this report is worded the way it is.

## Pinned state

- Measured `origin/main` at `2cf24b5`, 2026-09-26. 364 commits of history. **Re-verified 2026-09-27: `origin/main` is still `2cf24b5`.** Nothing in this repository has moved, so every measurement in this document is still current and there is no drift to reconcile.
- **69** remote branches enumerated via `git ls-remote --heads`; all 69 fetched and inspected. This was first published as 64; five branches have appeared since. The finding is unchanged and is in fact stronger — see Part 1.
- All work was read-only from an isolated clone. No working tree in this repository or in the operations workspace was modified, nothing was deployed, no host was contacted.

---

## Part 1 — the gate exists on one disk and nowhere else

The handoff-ownership gate for this repository exists as **uncommitted files in a single working tree**. It is not on a branch, not on any remote, and not in any commit.

| Piece | State |
|---|---|
| `scripts/validate_handoff_scope.py` (12,034 bytes, `sha256:185fbb0788c6a1b385516242a8b1035ab72530e128def2e375303eb1b37051fe`) | **untracked** (`??`) in the shared working tree |
| `tests/test_handoff_scope.py` | **untracked** (`??`) |
| the `handoff-scope` job in `.github/workflows/ci.yml` | **uncommitted modification** to a tracked file |

Inspection across every remote ref:

- `scripts/validate_handoff_scope.py` — present on **0 of 69** remote branches.
- `tests/test_handoff_scope.py` — present on **0 of 69** remote branches.
- Branches whose `.github/workflows/ci.yml` invokes the gate — **0 of 69**.
- `origin/main` — has **no `scripts/` directory at all**, and no `validate_handoff_scope.py` anywhere in its tree.

**Stronger than first published, not merely unchanged.** The earlier version reported that 0 of 64 branches carried the two files. Re-enumerating all 69 shows something wider: **no remote branch in this repository has a `scripts/` directory at all.** The control is not merely unmerged — the directory that would hold it does not exist on any remote ref, so there is no branch to fast-forward and no pull request that could be opened from work already done.

**This corrects an earlier characterisation.** The gate was previously described as living on local branches ahead of `main`, which would have implied a branch was waiting to be merged. That is not the case. There is nothing mergeable, and no pull request could be opened from existing work. The correct diagnosis is stronger than "unmerged": the control exists only as uncommitted files on one machine, and is one lost disk away from being gone permanently.

### Two traps for whoever lands it

**Trap 1 — the script and its test must land together.** `origin/main`'s CI runs:

```
python -W error::ResourceWarning -m unittest discover -s tests -v
```

That discovers every module under `tests/`. If `tests/test_handoff_scope.py` were committed *without* `scripts/validate_handoff_scope.py`, the suite would fail at import — under `-W error::ResourceWarning` — and take the whole test job red. Land both, or neither.

**Trap 2 — the shared working tree's `main` is stale.** It sits at `ff5dc8a`, which is **58 commits behind** `origin/main` at `2cf24b5`. Anything committed from that working tree builds on a base 58 commits behind the merge target, which will produce a large, conflict-heavy diff unrelated to the gate. Branch from a freshly fetched `origin/main`, not from that tree's `main`.

---

## Part 2 — what CI on this repository's `main` enforces

Three workflows exist: `ci.yml`, `rankings.yml`, `site.yml`. `ci.yml` runs:

- `ruff` lint over `harness tests examples/oc_handoff`
- `compileall` over the same paths
- the hermetic unittest discovery quoted above
- `ruff` over `audits`, then the 4-dimensional audit
- a live model-freshness check, conditional on a secret being set
- `harness.cli capabilities --check-shipped`
- sdist and wheel build, `twine check`, version parity across `pyproject` / `harness.__version__` / MCP
- a clean-venv wheel smoke-install and a serve + MCP stdio smoke test

That is a substantial pipeline, and it is entirely silent on ownership. **No workflow on `main` performs any handoff-ownership or scope check.** The only occurrences of the word "handoff" in `ci.yml` are the ruff path `examples/oc_handoff`, which is a lint target and not a governance control.

---

## Part 3 — a cross-lane document is already merged

The gate has never run. Running its logic — a byte-identical copy of the untracked script, `sha256:185fbb07…`, against a clean checkout of `origin/main` — over every path its own `is_handoff_path` rule classifies as a handoff produces **9** documents, not the 7 under `HANDOFF/`: the rule also catches any `.md` outside `HANDOFF/` whose filename contains "handoff".

**All 9 fail, and all 9 fail identically** — zero `HANDOFF-SCOPE-BEGIN` markers, so the gate short-circuits on missing metadata and never reaches its alias check. That means the alias evidence below is an independent scan, not a gate verdict.

### The one real violation

`docs/MODEL_SELECTION_HANDOFF_2026-09-13.md` is on `main` today. It contains **five substantive references to the other product lane**, none of them incidental:

| Line | Content |
|---|---|
| 4 | Authorship provenance: the document was written in a recovery session belonging to the other product lane |
| 17 | **Foreign evidence** — names an evidence file that lives in the other lane's working tree (`tmp/review/MODEL_PROBE_20260913.json`) |
| 35-37 | **Foreign finding** — attributes a specific model-rotation failure to board runs conducted in the other lane, naming run identifiers |
| 154 | **Foreign remediation** — an *unchecked* action item addressed to this repository's maintainers, requiring a panel to be re-run in the other lane |
| 156-158 | **Foreign verification receipt** — claims end-to-end verification in a script belonging to the other lane, with a run id, a vote tally and a dollar cost |

Introduced by commit `34c761c` (2026-09-13) and last updated by `3b35316` (2026-09-15). Commit subjects are deliberately not quoted here, because the introducing commit's own subject line contains the foreign product's name.

**In fairness, and this matters: the document's subject matter genuinely belongs to this repository.** It is addressed to this repository's maintainers and it diagnoses a real defect in `harness/chat.py` (`_effort_to_send`, where a reasoning setting of `"off"` omits the key entirely and therefore means *provider default*, i.e. reasoning on). Every actionable code claim in it is about this codebase.

That is precisely why it slipped through. The contamination is in provenance, evidence pointers and follow-up actions, not in the technical subject. A reviewer skimming for "is this about our code?" would answer yes, correctly, and move on. A textual alias detector would have caught it — which is the argument for the gate, not against it.

### Three references elsewhere that are *not* violations

Reported for completeness, because they show the gate's detector is blunt and the distinction matters:

- `HANDOFF/CTO_HANDOFF_HARNESS_JEV_FREEBUFF_AUDIT_2026-09-21.md:10` — a **disclaimer**: "Not in this repo: …", pointing readers to the other repository.
- `HANDOFF/FREEBUFF_TRANSITION.md:84` — a **log path** in a dogfood instruction.
- `HANDOFF/FREEBUFF_TRANSITION.md:99` — the **ownership boundary itself**: a rule that no product code from the other lane appears in this repository's pull requests.

All three are correct, desirable text. A textual detector cannot distinguish a disclaimer from a leak. This is a real limitation of the control, and the reason a waiver mechanism matters.

---

## The exposure, stated plainly

**The protection this session relied on for every handoff is one lost machine from gone.** Every handoff delivered through this lane has been validated by a hand-placed, byte-identical copy of a script that exists nowhere in version control. There is no CI enforcement, no peer review of the control, no history, and no way to recover it from the remote. The control is one `rm` away from ceasing to exist, and nothing would fail.

**The gap is not theoretical. It has already produced exactly the failure it exists to catch.** One document carrying another lane's evidence, findings, remediation and receipt is merged on `main` right now. That is the precise harm the gate was written to prevent, and it happened while the gate sat untracked. A control that has never run has no track record, and this one already has a miss it never got to see.

---

## What landing the gate would protect — and what it would not

**It would protect PR #94 and every future handoff. It would protect nothing retroactively.**

The existing 9 documents stay exactly as they are; the gate does not reach backwards. What it buys is that the next handoff — and the one after that — is machine-validated at commit time rather than by a manual, out-of-band check that nobody else can reproduce.

Landing it is not a drop-in, and the lane should expect this much work:

1. **9 of 9 existing documents fail** the gate. Any pull request touching one of them goes red until it is stamped or waived.
2. **The three boundary-statement references in Part 3 would also fail** the textual detector, despite being correct. They need rewording or a waiver.
3. **The cross-lane document needs real remediation, not a waiver.** The foreign evidence pointers, finding, action item and receipt should be split out, leaving this repository's reasoning intact. Waiving it would defeat the control.
4. **Order matters.** Commit the script, the test and the CI job together; then remediate the 9; only then does the gate go green and start protecting anything. Committing the CI job alone turns every handoff-touching pull request red.

### Third defect: no waiver mechanism

The gate contains **zero** references to waivers or exemptions. It hard-requires the literal `foreign_material: NONE` in every scope block and rejects any foreign alias outside it.

**This is no longer a design question. The comparison repository has landed it, so the remedy is a port.** In the commit its maintainers title *"feat(handoff): add an auditable, per-document waiver register"*, the register is a tracked, per-document, owner-signed, dated JSON file at the repository root, audited on every run, and it fails if a waiver names a path that is not a tracked handoff or names a document that no longer trips the detector. The gate reading it grew from 448 to 991 lines and gained 109 references to waivers, together with a `--self-test` mode that fails when the classifier itself drifts, and a self-locating guard that refuses to run outside the repository that contains it.

Two consequences for this repository. First, the recommendation is now concrete: read that commit and adapt the register, rather than designing a second shape for it. Second, the argument is stronger than when this was first written. The pattern was adopted elsewhere within a day of this finding being raised, which is evidence that the failure mode below is real and observed rather than hypothetical.

Without an equivalent, this repository has a strictly worse failure mode than having no gate: it cannot record a legitimate cross-lane reference at all, so the only available responses are to obfuscate or to bypass. The three documents in Part 3 are already proof that correct text will trip the detector.

### What the gate would not protect against

It is a textual control. It cannot distinguish a disclaimer from a leak, and it cannot detect foreign material that never uses the other product's name. It is a floor under a discipline, not a substitute for one.

---

## Requested owner actions

1. Treat the gate as **untracked work in progress, not a pending merge**. Nothing is waiting to be merged; the three files must be committed forward onto a fresh branch off a current `origin/main`.
2. Recover the control into version control before anything else on this list. The three pieces are the script, its test, and the `handoff-scope` job. They must land as one commit, off a freshly fetched base, not off the 58-commit-stale local `main`.
3. Add a per-document waiver register. **This is a port, not a design.** The comparison repository has landed one in a single tracked commit, and the mechanism — audited JSON register, owner-signed and dated, failing on a stale or untracked waiver — is described under *Third defect*. Read it and adapt it; do not invent a second shape for the same control.
4. Remediate `docs/MODEL_SELECTION_HANDOFF_2026-09-13.md` by splitting the foreign evidence, finding, action item and receipt out of it. Do not waive this one.
5. Stamp or waive the other 8 documents the gate classifies as handoffs, so the gate can be turned on without a red backlog.
6. Only after 1-5: enable the `handoff-scope` job on `main`.

**Does the waiver landing change what this document asks for?** No, not in substance, and the ordering is unaffected. Actions 1, 2, 4, 5 and 6 are untouched by it. Action 3 changes from *design a mechanism* to *port an existing one* — smaller, better specified, and no longer a judgement call this lane has to make. Landing the register early does not let the job be enabled early, because the 9 documents still have to be stamped or waived before the job can go green, which is what action 6 already sequences.

## Limits of this handoff

- **The Android-style caveat does not apply here, but a different one does:** the gate's behaviour was established by running a byte-identical copy of the untracked script against a clean checkout of `origin/main`, not by the gate executing in this repository's CI, which it has never done.
- **The cross-lane finding is an independent scan.** Because all 9 documents fail on missing metadata first, the gate never reached its alias check. The alias evidence in Part 3 is mine, not the gate's verdict.
- **No statement here is a merge, an approval, or a sign-off.** The owning repository decides what lands, and this lane's own uncommitted work is deliberately excluded from any deliverable produced alongside this document.
