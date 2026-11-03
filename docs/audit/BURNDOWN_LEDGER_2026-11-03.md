# Burndown ledger - 2026-11-03 (lane/burndown, re-derived)

Open issues at triage time: 79. Classes: CURABLE NOW (cure on lane, awaiting owner-reserved merge), EVIDENCE ONLY (recorded, no code act), OWNER-RESERVED (owner decision or act), PARKED (stay parked per dispatch), BACKLOG (real work, recorded order).

Closure rule: an issue closes only with a per-issue evidence disposition citing a merge SHA. No issue is closed by this ledger; the counts below are the state at this pass.

## CURABLE NOW (cure on lane head 5a8bd77, not yet on main)
- #446 snapshot freshness test (cure 6a4988d; refresh_tier_snapshot). Close on merge SHA only.
- #445 baseline pyright debt (cure 1b3869b).
- #447 Muse chat() Meta transport (cure cbbfc31).
- #434 overlay verification (cures e7e4131, dc5b4bb, 92986a2; 17/17 green).
- #443 Python 3.14 matrix, #440 driving-hygiene CI, #439 read-only probe: dispositions found already cured on the lane; close on merge SHA.

## BACKLOG (recorded order; real work)
- URGENT QUEUE H1/H2 (#295 comments 6605149634, 6605150240).
- #438 provisioning chain and Batch 0 remainder.
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
- Open at triage: 79. Closed this pass: 0 (no per-issue evidence for closure yet landed on main). Opens stay below closes is not yet satisfied by closures; the cures above are the path to it once merged.
