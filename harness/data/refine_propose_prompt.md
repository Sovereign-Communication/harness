# Refine propose prompt template (TIER 1 — human approval required for changes)

Version: 1. Sections are filled by `harness/refine.py` at propose time.

---

You are the editor for a harness refinement cycle (issue #239, Propose-Probe-Compose loop).

## 1. Failing task

Task ID: {task_id}
Task family: {task_family}
Jev outcome: {jev_outcome} (below 0.5 = failure)
Failure mode: {failure_mode} (one of: wrong-tool, missing-context, bad-judgment, timeout/cost, safety-block, other)

Trajectory summary:
{trajectory_summary}

## 2. Relevant lessons (top-5 by family + recency)

{lessons}

## 3. Component mapping (with tier labels)

TIER 0 — IMMUTABLE. You MUST NOT propose edits to these. Any candidate touching them is rejected:
- This safety model, the Jev scoring rubric, credential/secret handling, spend authorization gates, THREAT_MODEL.md, docs/security.md, the refine loop's own trigger/taxonomy/config.

TIER 1 — invariant/systemic (human approval required):
- system prompt: harness/prompts.py, CLAUDE.md, docs/hourglass-*-prompt.md
- guardrails: harness/lifecycle_guards.py, harness/apply_policy.py, harness/consent.py, harness/trust.py, harness/jev_policy.py

TIER 2 — domain/task-specific (auto-mergeable under gates):
- skills: ~/workspace/custom-skills/, skills-lock.json
- tool guidance: skill SKILL.md files, docs/ tool docs
- subagents: orchestrator spawn configs in harness/orchestrator.py
- scripts: scripts/
- plugins: MCP surfaces, plugin middleware

## 4. Instructions

Generate EXACTLY TWO candidate harness edits as unified diffs:

(A) INVARIANT candidate: one systemic edit to a TIER 1 component addressing the failure mode above. Include a 1-paragraph rationale citing the failure mode.

(B) DOMAIN candidate: one task-specific edit to a TIER 2 component addressing this task's failure. Include a 1-paragraph rationale.

Constraints:
- Do NOT touch TIER 0 paths.
- Each diff MUST be ≤ 200 lines.
- Diffs must apply cleanly with `git apply`.
- Output format: for each candidate, first the rationale paragraph, then the diff in a ```diff block.

PILOT MODE: {pilot_mode} (when ON, both candidates MUST target TIER 2 components only)
