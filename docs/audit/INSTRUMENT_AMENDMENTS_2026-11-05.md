# Instrument amendment drafts - 2026-11-05 (order d)

**Status: DRAFTS ONLY. Nothing in this file has entered a canon.** Each draft
is filed here for the owner's ratification; no policy document is edited by
this file, and no gate behaves differently because it exists.

Ordered by the 2026-11-04 dissemination directive. Item 5 of that directive
(the Lucas Gate v2 and Jev house method) explicitly requires **no change**, and
none is proposed.

Where the source text could not be read from this repository, the draft says so
instead of paraphrasing from memory:

| Instrument | Source text in this tree? | Read |
|---|---|---|
| Jev Gate Policy v1 | **yes**, `docs/JEV_GATE_POLICY_V1.md` (93 lines) | file read 2026-11-05 |
| Universal lane prompt v7 | **no** | `grep -rn "universal lane prompt"` over `*.md` returns no hit, 2026-11-05 |
| Prompt Accuracy Gate | **no** | same grep for "Prompt Accuracy Gate" returns no hit, 2026-11-05 |

---

## Draft 1 — universal lane prompt v7, verification extension

**Insert into v7's verification step, replacing its GitHub-only scope:**

> Verification covers **every external state the lane asserts**, not only GitHub
> state. Deployments, published artifacts, repository contents, hosted
> dashboards, third-party services and queue/CI systems each carry their own
> read. Every durable claim about such state is written with its read in the
> form `(read: <method>, <timestamp>)`, taken from a primary read of the thing
> itself — the API, the file, the endpoint — never from memory, from a previous
> turn's assertion, or from the prose of a plan or ledger. A claim whose read
> cannot be taken is written as unverified with the reason, and is not counted
> as evidence. When a recorded state and a fresh read disagree, the disagreement
> is named in the report with both sides and the resolution; it is never
> smoothed into a single number.

**Insert into v7's finish rule, for non-code deliverables:**

> "Finished" for a non-code deliverable requires **both** an R1 conformance
> sheet (each element of the plan of record, delivered or not, with the read
> that shows it) **and** an R7 non-author verdict on that sheet. A producer's
> own log or self-assessment is not acceptance, however detailed. A deliverable
> with no conformance sheet has not been finished, only produced.

**Why (from this lane's evidence, not from theory):** the 2026-11-05 flush found
a cure commit for #301 that existed only on an unlanded branch while the defect
was live on the lane — a GitHub-state claim that was true of the repository
object graph and false of the lane. Reading the thing asserted (the lane's
`ci.yml`) rather than the claim (a commit subject) is what caught it.

---

## Draft 2 — v7 audit debt: re-audit on first-run specimens (recommended)

**Recommendation: re-audit, not re-issue.** v7 was dispatched on an owner waiver
after its house Jev audit landed below bar (78 → 86 → 83 → 83). The defect the
scores point at is textual, not scope-level, so a re-audit against real runs
produces more information than a rewrite: a re-issue would be a second
unvalidated text, while an audit on specimens tests the text the lanes are
actually running.

**Specimens (first-run artefacts of the dispatched text, all on this lane):**

1. `docs/audit/ISSUE_FLUSH_2026-11-05.md` — a report whose whole purpose is the
   R4 read discipline the amendment adds; it shows both the strength (80 rows
   with reads) and the failure mode (rows with only the issue-list and ancestry
   reads are recorded as still open rather than scored as cures).
2. `harness/queue_bus.py` + `tests/test_queue_bus.py` — a build deliverable
   where "finished" meant a driven CLI run, not a green unit suite alone.
3. `docs/circleci-port.md` — a deliverable that deliberately refuses to claim an
   artefact it did not build (the config), which is the amendment's "unverified
   with the reason" clause in practice.
4. The 2026-11-05 `#301` finding — a specimen of the exact failure the
   amendment's disagreement clause is written to force into the open.

**Receipts this draft owes and cannot produce here:** the re-audit itself needs
the house Jev method with a judge-capable run. This host has no provider key
configured for that path, so the audit was **not** run and no score is recorded.
Recording a score without the run would be the exact defect the instrument is
meant to prevent.

---

## Draft 3 — Jev Gate Policy v1, Assessment Gate amendment

Source: `docs/JEV_GATE_POLICY_V1.md`, section "Assessment Gate (5-aspect suite
on work product)". That section lists the five aspects, the relevance-first
rule, the flexibility rule (4/5 ≥ 90 with the fifth at 85–89), the single-point
rule and the below-85 block. It does **not** number its steps; the directive
refers to "step 2 (define done)" and "step 4 (aspect scoring)", which are
numbered in the oversight record's fuller text. The amendment below is written
to attach to those two steps wherever they are numbered.

**Amendment (insert, do not renumber the existing rules):**

> **Definition of done comes from the plan of record.** Before scoring, the gate
> names its definition of done: the R1 conformance sheet when one exists (a row
> per element of the approved plan, each with the read that shows it), or the
> plan itself when no sheet exists. The sheet is an input to the assessment, not
> a substitute for it.
>
> **Scoring is element-by-element against that definition.**
> *completeness* = delivered elements / elements in the definition, with every
> undelivered element named; an element with no read counts as **unverified**
> and is excluded from the numerator, never rounded up. *correctness* = per
> delivered element, whether it satisfies the element's own statement — not
> whether the deliverable is generally good. The other three aspects keep their
> current definitions.
>
> The aggregate aspect scores then enter the existing rules unchanged: the
> flexibility rule, the single-point rule, the below-85 block and the 3-round
> stall report all apply to the aggregates. A deliverable that scores well on
> general quality while leaving plan elements unverified fails *completeness* by
> construction.

**Decision Gate: untouched.** No rule in its eight-step order changes, and no
new numeric bar is introduced.

---

## Draft 4 — Prompt Accuracy Gate extension to deliverables of record

Source text not in this tree (see the table above). The extension is therefore
drafted as **the existing five-check shape applied to deliverables at their
delivery points**, with the three mappings the directive names fixed here and
the remaining two checks left explicitly open rather than invented:

| Existing check (shape) | Applied to a deliverable of record |
|---|---|
| anchors — the citations a prompt relies on still say what they claim | **conformance elements** — every element of the plan of record has a read that shows it, and every cited path/line/commit in the deliverable still carries its claim |
| surface currency — the prompt reflects the surface it will run against | **the assembled artefact** — the shipped thing (file, page, report, pipeline) is the one the deliverable describes; a draft that differs from what shipped is a finding |
| mechanical checks — deterministic facts are checked, not asserted | **format and host state** — the artefact exists at its stated location, parses/renders, and the host reports it as the deliverable says (API read, not a screenshot of the producer's log) |
| checks 4 and 5 | **to be supplied from the gate's canonical text before ratification**; not guessed here |

**Binding rule carried over:** this is R1/R7's enforcement form for deliverables,
not a parallel instrument. Where the Jev Assessment Gate applies to the same
deliverable, the Prompt Accuracy Gate's mechanical findings are inputs to its
evidence-quality aspect; neither gate claims the other's verdict.

**Why this is not a fifth gate:** the directive's audit found verification
present where a machine-checkable harness existed and absent where the artefact
was judgment-shaped. This extension makes the existing accuracy gate check the
artefact at hand rather than only the prompt that produced it.

---

## Ratification block (owner)

| Draft | Needs | State |
|---|---|---|
| 1 — v7 verification + finish rule | owner's word; canon change | draft only |
| 2 — v7 re-audit (recommended) or re-issue | owner's word; a judge-capable run to execute | draft only; audit not run, no score recorded |
| 3 — Assessment Gate conformance amendment | owner's word; mapping to the numbered steps in the reference doc | draft only |
| 4 — Prompt Accuracy Gate extension | owner's word; the canonical enumeration of checks 4 and 5 | draft only |

Nothing here changes a gate, a bar, or a canon document until the owner
ratifies the specific draft and marks it landed.
