# Dissemination adoption - 2026-11-05 (order c)

What this lane adopted from the 2026-11-04 hackathon execution audit, where it
lives, and what it has **not** adopted. Every count here is the count in
[`ISSUE_FLUSH_2026-11-05.md`](ISSUE_FLUSH_2026-11-05.md); `tests/test_dissemination_adoption.py`
fails if the two disagree, so this page cannot drift from the report it
summarises.

## 1. Register location (R5)

The lane's assumption register lives in
[`BURNDOWN_LEDGER_2026-11-03.md`](BURNDOWN_LEDGER_2026-11-03.md), section
"Assumption register (R5: assumption, owner, verify-by, method)", rows A1-A7.
Each row carries its assumption, its owner, a verify-by date and the method that
would settle it. No row is overdue at 2026-11-05; an overdue row is a blocker in
the lane status.

The flush report adds a second, finer register: five read codes (R1-R5) that
state, for each row, which primary read its disposition rests on and when that
read was taken. A row with only R1+R2 is recorded as still open rather than
guessed at.

## 2. Board rows (the disposition classes)

The board is the disposition table in the flush report. Its counts at
2026-11-05:

| Class | Count | What it means here |
|---|---|---|
| landed on lane (unmerged) | 23 | a cure is in `lane/burndown` ancestry and its focused tests execute green; closure still needs a merge SHA |
| landed-partial | 4 | a slice is landed and the remaining scope is named in the row |
| owner-only | 9 | needs an owner decision or owner-held credential; no lane commit can close it |
| still-open | 43 | no cure shown to be on the lane; recorded, not guessed at |
| tracker | 1 | #295 itself, which stays open by design |
| **total open issues** | **80** | the live `GET /repos/Treystu/harness/issues?state=open` read |
| closed by this pass | 0 | merges are owner-reserved; a lane row is never reported as closed |

Waved/ordered campaign bookkeeping (`docs/issue-plans/`) is **not** the board
for this lane: its historical counts (57 → 54 → 53) describe a closed campaign
window and disagree with today's 80. The flush report names that disagreement
and resolves it in favour of the live read instead of restating an old number.

## 3. Flush counts

80 open issues, 80 dispositioned rows, 0 unclassified, 0 duplicated, 0 closed.
The report's own totals line is the source; this page repeats it for readers who
will not open the table.

## 4. Remedies adopted (R1, R4, R5, R7)

| Remedy | Where it is adopted |
|---|---|
| R4 — every durable external-state claim carries its read | the flush report's five read codes with timestamps; the CircleCI mapping's reproduction commands; the ledger's dated sections cite the run id and head they read |
| R5 — assumption register with verification dates | ledger rows A1-A7; the flush report's per-row read codes |
| R1 — deliverables are checked against their plan | `docs/circleci-port.md` pins the port mapping to `ci.yml` in `tests/test_circleci_port_mapping.py`; `docs/queue-bus` behaviour is pinned by `tests/test_queue_bus.py`; this page is pinned to the flush report |
| R7 — a non-author verdict, not the producer's log | **not adopted here.** No non-author conformance read of this lane's deliverables exists yet; the reports say so rather than claiming otherwise |

## 5. Adopter-facing links (all committed on `lane/burndown`)

- Issue flush report: [`docs/audit/ISSUE_FLUSH_2026-11-05.md`](ISSUE_FLUSH_2026-11-05.md)
- Plan of record and register: [`docs/audit/BURNDOWN_LEDGER_2026-11-03.md`](BURNDOWN_LEDGER_2026-11-03.md)
- CI rail contract: [`docs/circleci-port.md`](../circleci-port.md)
- Lane surfaces: PR #442 (standing draft, status comments) and tracker #295

## 6. What is explicitly NOT adopted (no artifact behind it)

- The saturation / flush loop rows FLUSH-0..FLUSH-3: commissioned and slotted,
  no code.
- S3 calibration corpus and protocol freeze: the code and pack exist, the
  corpus needs installed-FreeBuff screenshots plus a live key.
- TM1/TM2/TM3: supervised, not run.
- The instrument amendment drafts: written as drafts on 2026-11-05, awaiting the
  owner's ratification; nothing has entered a canon.
- Any CI-green claim: hosted Actions is billing-locked, so no check-run evidence
  for this head exists.

## 7. Read for this page

Counts read from
[`ISSUE_FLUSH_2026-11-05.md`](ISSUE_FLUSH_2026-11-05.md) at 2026-11-05T10:23Z;
the report itself was generated from
`gh api repos/Treystu/harness/issues?state=open&per_page=100 --paginate` at
2026-11-05T10:23:02Z. The register rows were read from
`BURNDOWN_LEDGER_2026-11-03.md` in the same working tree.
