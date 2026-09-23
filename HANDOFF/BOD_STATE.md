# BOD_STATE — Harness Board of Directors

**Updated:** 2026-09-22
**Doctrine:** `.claude/skills/bod/SKILL.md` (sovereignty + consent, cost-bounded cheapest capable, one owner, fail closed, dependency-light + brand-neutral).
**Mechanics:** 5-model `harness verify` panel + judge concurrence, unanimity 5/5, $0.10 ceiling, fails closed.

## Rulings

| Date | Proposal | Votes | Judge | Cost | Outcome |
|---|---|---|---|---|---|
| 2026-09-22 | R1 (orig): adopt "Claude Code" as primary lane | 0/3 approve (3-seat CLI panel) | REJECT | ~$0.0007 | **REJECTED** on brand neutrality; resubmitted tool-neutral as R1 |
| 2026-09-22 | R1: tool-neutral tiered agent lane (AGENTS.md canon, thin tool adapters, top tier orchestrates only, HUL mission packs) | 3/4 | REJECT | $0.0021 | **REJECTED** — dissent `openai/gpt-5-mini`: no consent/ledger clause |
| 2026-09-22 | R2: completion = hard gates + sentiment-bucket Jev bar; Jev may only lower code axes | 2/3 (1 panel failure) | APPROVE | $0.0016 | **REJECTED** — dissent `openai/gpt-5-mini`: "Jev lowering axes is a second policy actor" |
| 2026-09-22 | R3: every change to `main` via PR with green CI (no direct pushes) | 3/4 | REJECT | $0.0010 | **REJECTED** — dissent `openai/gpt-4o-mini` read it as allowing direct pushes (misreading recorded as-is) |
| 2026-09-22 | R4: accept PR #68 preview degrade (heuristic, labeled, never confirmed) | 3/4 | REJECT | $0.0015 | **REJECTED** — "violates fail closed" → fail-closed default restored, degrade opt-in only |
| 2026-09-22 | R5: keep Ling out of free apply pool until DF-LING-1 fixed + live evidence | 3/4 | APPROVE | $0.0010 | **REJECTED** — dissent `openai/gpt-4o-mini` (access/consent) |
| 2026-09-22 | R6: paid failover only if ceiling-bounded, ledgered, disclosed | 4/4 | APPROVE | $0.0014 | **DEFERRED** — only 4 of 5 seats filled |

### Session notes (2026-09-22)

- Panel: requested `deepseek/deepseek-v4.1-flash`, `inclusionai/ling-3.0-flash`, `openai/gpt-5-mini`, `google/gemini-3.8-flash`, `openai/gpt-4o-mini`; judge `z-ai/glm-5.3-flash`; convened through `service.run_verify` (the one verify owner) because `harness verify --panel` is a dead flag (`DF-CLI-2`). Gemini was silently dropped and `required_panelists` capped at 4 (`DF-BOD-1`) — so no run could reach 5/5 as written.
- Total Board spend ≈ $0.009 across 7 runs. Artifacts: `tmp/claude/bod/board-r*.json` (local).
- Operator note: practices R1–R3 are already canon (merged through reviewed, CI-green PRs); the Board did not ratify them. Re-convene after `DF-BOD-1` is fixed, with R1/R2 revised to state consent + ledger explicitly.

## Pending proposals (for the operator to convene)

1. Adopt Claude Code as the primary implementer lane, with Opus restricted to orchestration and Haiku/Sonnet tiers for work (CLAUDE-LANE).
2. Redefine phase completion as "hard gates + sentiment-bucket Jev bar pass" (JEV-BAR), with Jev allowed to lower but never raise code-owned axes.
