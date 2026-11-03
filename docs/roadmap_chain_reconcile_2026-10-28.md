# Roadmap chain reconciliation — 2026-10-28


Source: `docs/jev-roadmap.md` dependency/acceptance register table.


- register rows parsed: 61

- cycles detected: 0

- topo-orderable issues: 61 of 61

- prompt-chain edges checked: 34

- edges verified in roadmap register: 0

- edges NOT matching roadmap register: 34


### Edges not matching roadmap register


- 345 -> 346: roadmap says #345 prereqs=[]; #346 present in register=False

- 346 -> 342: roadmap says #346 prereqs=['345']; #342 present in register=False

- 342 -> 343: roadmap says #342 prereqs=['345', '346', '324']; #343 present in register=False

- 343 -> 344: roadmap says #343 prereqs=['342']; #344 present in register=False

- 344 -> 308: roadmap says #344 prereqs=['342']; #308 present in register=False

- 345 -> 346: roadmap says #345 prereqs=[]; #346 present in register=False

- 346 -> 342: roadmap says #346 prereqs=['345']; #342 present in register=False

- 342 -> 309: roadmap says #342 prereqs=['345', '346', '324']; #309 present in register=False

- 345 -> 340: roadmap says #345 prereqs=[]; #340 present in register=False

- 340 -> 280: roadmap says #340 prereqs=['345']; #280 present in register=False

- 280 -> 318: roadmap says #280 prereqs=['302', '340']; #318 present in register=False

- 318 -> 319: roadmap says #318 prereqs=['340', '280', '333']; #319 present in register=False

- 345 -> 340: roadmap says #345 prereqs=[]; #340 present in register=False

- 340 -> 280: roadmap says #340 prereqs=['345']; #280 present in register=False

- 280 -> 337: roadmap says #280 prereqs=['302', '340']; #337 present in register=False

- 340 -> 281: roadmap says #340 prereqs=['345']; #281 present in register=False

- 340 -> 294: roadmap says #340 prereqs=['345']; #294 present in register=False

- 340 -> 304: roadmap says #340 prereqs=['345']; #304 present in register=False

- 340 -> 313: roadmap says #340 prereqs=['345']; #313 present in register=False

- 345 -> 336: roadmap says #345 prereqs=[]; #336 present in register=False

- 336 -> 337: roadmap says #336 prereqs=['333', '345']; #337 present in register=False

- 284 -> 335: roadmap says #284 prereqs=[]; #335 present in register=False

- 335 -> 285: roadmap says #335 prereqs=[]; #285 present in register=False

- 301 -> 292: roadmap says #301 prereqs=['278']; #292 present in register=False

- 292 -> 270: roadmap says #292 prereqs=['301']; #270 present in register=False

- 270 -> 272: roadmap says #270 prereqs=['292', '300']; #272 present in register=False

- 272 -> 271: roadmap says #272 prereqs=['270']; #271 present in register=False

- 271 -> 273: roadmap says #271 prereqs=['270', '324']; #273 present in register=False

- 325 -> 291: roadmap says #325 prereqs=['324']; #291 present in register=False

- 325 -> 314: roadmap says #325 prereqs=['324']; #314 present in register=False

- 377 -> 378: roadmap says #377 prereqs=[]; #378 present in register=False

- 378 -> 382: roadmap says #378 prereqs=[]; #382 present in register=False

- 377 -> 379: roadmap says #377 prereqs=[]; #379 present in register=False

- 380 -> 381: roadmap says #380 prereqs=[]; #381 present in register=False


## Prompt-scoped issues present in roadmap register


- count present in register: 47

- present: #270, #271, #272, #273, #275, #277, #280, #281, #283, #284, #285, #286, #287, #289, #290, #291, #292, #293, #294, #295, #299, #300, #301, #302, #303, #304, #306, #308, #309, #310, #311, #313, #314, #318, #319, #325, #332, #334, #335, #336, #337, #340, #342, #343, #344, #345, #346


## Prompt-scoped issues NOT in roadmap register table


- count missing: 30

- missing: #365, #377, #378, #379, #380, #381, #382, #407, #409, #411, #412, #413, #414, #415, #416, #417, #418, #419, #420, #421, #422, #423, #424, #425, #426, #427, #428, #429, #430, #431


Note: missing-from-register does NOT mean missing from the repo — many of these are prompt-named NEW issues (#412-431) that postdate the roadmap register, or issues whose register row uses a different parse shape. The live `gh issue view` status batch (docs/status_batch_2026-10-28.md) is the authoritative per-issue state for this pass.
