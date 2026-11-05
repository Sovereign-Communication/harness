# Dissemination adoption - 2026-11-05 (order c)

What this lane adopted from the 2026-11-04 hackathon execution audit, where it
lives, and what it has **not** adopted. Every count here is the count in
[`ISSUE_FLUSH_2026-11-05.md`](ISSUE_FLUSH_2026-11-05.md), read at
2026-11-05T18:21:38Z; `tests/test_dissemination_adoption.py` fails if the two
disagree, so this page cannot drift from the report it summarises.

## 1. Register location (R5)

The lane's assumption register lives in
[`BURNDOWN_LEDGER_2026-11-03.md`](BURNDOWN_LEDGER_2026-11-03.md), section
"Assumption register (R5: assumption, owner, verify-by, method)", rows A1-A7.
Each row carries its assumption, its owner, a verify-by date and the method that
would settle it. No row is overdue at 2026-11-05; an overdue row is a blocker in
the lane status.

The flush report adds a second, finer register: read codes R1-R10 that state, for
each row, which primary read its disposition rests on and when that read was
taken. A row with only R1+R2 is recorded as still open rather than guessed at.

## 2. Board rows (the disposition classes)

The board is the disposition table in the flush report. Its counts at
2026-11-05T18:21:38Z:

| Class | Count | What it means here |
|---|---|---|
| landed on lane (unmerged) | 21 | a cure is in `lane/burndown` ancestry and its focused tests execute green; closure still needs a merge SHA |
| landed on main (merged) | 2 | a row whose artifact is already merged on `main`; the lane owes nothing further and recommends closing it now |
| landed-partial | 4 | a slice is landed and the remaining scope is named in the row |
| owner-only | 9 | needs an owner decision or owner-held credential; no lane commit can close it |
| still-open | 43 | no cure shown to be on the lane; recorded, not guessed at |
| tracker | 1 | #295 itself, which stays open by design |
| **total open issues** | **80** | the live `GET /repos/Treystu/harness/issues?state=all` read, pull requests excluded |
| closed by this pass | 0 | merges are owner-reserved; a lane row is never reported as closed |

Waved/ordered campaign bookkeeping (`docs/issue-plans/`) is **not** the board
for this lane: its historical counts (57 → 54 → 53) describe a closed campaign
window and disagree with today's 80. The flush report names that disagreement
and resolves it in favour of the live read instead of restating an old number.

## 3. Flush counts

80 open issues, 80 dispositioned rows, 0 unclassified, 0 duplicated, 0 closed.
Every row carries a primary read of the issue itself (R7 state and R8 latest
comment, both stamped 18:21-18:30Z). The report's own totals line is the source;
this page repeats it for readers who will not open the table.

## 4. Remedies adopted, and where each one actually landed

Each remedy from the execution audit is listed with the artifact that carries it
on `lane/burndown`, or with the steering request that names the missing owner
input. Nothing here is adopted on an intention.

| Remedy | Status | Artifact or missing input |
|---|---|---|
| R4 — every durable external-state claim carries its read | adopted | the flush report's read stack R1-R10 with method and timestamp per code; the ledger's dated sections cite the run id and head they read; the CircleCI mapping carries its reproduction commands |
| R5 — assumption register with verification dates | adopted | ledger rows A1-A7 (assumption, owner, verify-by, method); re-read each pass and reported as overdue or not |
| R1 — deliverables are checked against their plan | adopted | `docs/circleci-port.md` is pinned to `ci.yml` by `tests/test_circleci_port_mapping.py`; the queue launcher and watcher are pinned by `tests/test_queue_bus.py` and `tests/test_queue_launcher.py`; this page is pinned to the flush report by `tests/test_dissemination_adoption.py` |
| R7 — a non-author verdict, not the producer's log | partially adopted | one independent conformance re-read of the flush report exists (R6 in that report, head `7831920`). No non-author verdict covers the queue launcher or the vision pack, so those stay producer-graded. **STEERING REQUEST SR-5:** a second reader for those deliverables |

The four remedies above are the ones this page's first version recorded as
assigned to the lane. Their source - the 2026-11-04 hackathon execution audit -
is **not in this repository**, so this page cannot re-enumerate the audit's
numbering from the thing itself; it inherits the selection rather than deriving
it. **STEERING REQUEST SR-6:** the audit document, so the remedy list can be
checked exhaustively instead of inherited.

## 5. Adopter-facing links (all committed on `lane/burndown`)

- Issue flush report: [`docs/audit/ISSUE_FLUSH_2026-11-05.md`](ISSUE_FLUSH_2026-11-05.md) — refreshed in the ninth pass; its landing commit is named in the ledger's ninth-pass section
- Plan of record and register: [`docs/audit/BURNDOWN_LEDGER_2026-11-03.md`](BURNDOWN_LEDGER_2026-11-03.md)
- CI rail contract: [`docs/circleci-port.md`](../circleci-port.md)
- Instrument amendment drafts: [`docs/audit/INSTRUMENT_AMENDMENTS_2026-11-05.md`](INSTRUMENT_AMENDMENTS_2026-11-05.md)
- Lane surfaces: PR #442 (standing draft, status comments) and tracker #295

## 6. What is explicitly NOT adopted

- **FLUSH-0..FLUSH-3**: no longer unadopted in code — `7c2f094`, `17adbf4`,
  `d6adf51`, `d85540b`, `c53c914`, `787bb8f`, `0ba31ee` and the canary gate
  `65a8dac` are all in lane ancestry and `tests.test_flush_loop` is 42 OK. What
  is still not adopted is a merge into `main` and a live judge campaign at
  scale; the canary corpus runs from `packs/flush_canary_suite.json`.
- **S3 calibration**: the vision gate code and pack exist (`fd653f4`,
  `tests.test_driver_core.test_vision_verify` 25 OK), the corpus does not. Needs
  real installed-FreeBuff screenshots and a live key (SR-3).
- **TM1/TM2/TM3**: supervised, not run. Needs the owner present per launch and a
  calibration freeze this lane must not perform (SR-4).
- **The instrument amendment drafts**: written as drafts, awaiting the owner's
  ratification; nothing has entered a canon.
- **Any CI-green claim**: hosted Actions is billing-locked, every job reports
  `steps=0`, so no check-run evidence for this head exists and none is claimed.

## 7. Read for this page

Counts and states read from the flush report at 2026-11-05T18:21:38Z, which was
itself built from `gh api repos/Treystu/harness/issues?state=all&per_page=100 --paginate`
(18:21:38Z), one `gh api .../issues/<n>/comments?per_page=100 --paginate` read
per row (18:22-18:30Z), and `git cat-file -e` plus `git merge-base --is-ancestor`
for every cited SHA (18:24-18:26Z, head `d0f74a6`). The register rows were read
from `BURNDOWN_LEDGER_2026-11-03.md` in the same working tree.
