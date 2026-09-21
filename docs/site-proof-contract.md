# Proof Bench / site track — cross-session cooperation contract (`SITE-*`)

**Status:** canon for the Proof Bench website track (promoted by this PR).
**Isolation:** this track runs on `feat/site-proof-bench`; it never edits the
Jev-directed-escalation WIP files (`jev_policy.py` internals,
`sliding_scale.py`, `apply_policy.py`, `escalation.py`). Coexistence is at the
data-contract level below.

## Ownership

| Concern | Owner | Rule |
|---|---|---|
| Escalation *producers* (`decide_probe_verify_escalate`, `should_abstain`, `de_escalation_target_rung`, `escalation_condensed_context`) | Jev-directed-escalation session | SITE track consumes via the ledger only — never imports their internals |
| Ledger `escalate` event shape | Joint, additive-only | Existing fields are frozen; new fields are optional additions (see below) |
| Ledger → public-bundle sanitization | `harness/site_export.py` (SITE) | The ONE boundary owner between private evidence and the public site |
| Route/marketplace pack (`evaluate_model_route`) | SITE (extends `jev_policy`; no changes to their `evaluate_route`) | 0-hallucination: choice ⊆ declared ladder, else `is_fallback` + null model |
| Public site, worker, consent flow, local UI remake | SITE | Static-first; local mode works with zero cloud dependency |

## Ledger event contract (the coalescing seam)

The site consumes only ledger events. The `escalate` event today (v1) is:

```
{"event": "escalate", "task_id": …, "from_model": …, "to_model": …}
```

**Pointer request to the escalation session** (additive, optional, all
nullable): `directed_by: "jev"|"verify_lane"`, `jev_confidence: float`,
`target_rung: int`, `condensed_context_chars: int` (length only — never
content). Their wiring already produces these signals on `ApplyState`; when
they land them on the event, the site's cascade views gain a confidence
column with zero schema break.

**Tolerance rule (pinned by tests):** `site_export` accepts v1 events
(missing fields render as `directed_by: "verify_lane"`, null confidence) and
v2 events identically. Neither session blocks the other; merge order is
irrelevant.

## `bundle-v1` at a glance (full schema: `harness/site_export.py`)

- Identity: `bundle_id` = sha256-16 of the canonical core *including* the
  consent block (a swapped consent record can never ride a seen bundle_id
  past the worker's dedupe) and *excluding* `generated_at` (re-export of an
  unchanged ledger is idempotent).
- Costs: generative cost from `model_result.cost`; Jev cost from
  `jev_eval.cost` — reported separately, never merged.
- Run depth: `entry_tier` (dispatch model tier) and
  `deepest_tier_reached` (max tier the ladder actually reached). The gap
  between them is the escalation story the site tells.
- Escalation warrant: `lower_rung_rounds` / `lower_rung_verify_failures` /
  `jev_confidence_at_handoff` — proof that cheaper rungs were genuinely
  exhausted before a pricier rung ran.
- Privacy: no prompt text, no paths, no gate output, no error text, no
  caller identity. Task ids only as truncated SHA-256. Secret-shaped
  content in the serialized bundle refuses the export outright.
- Chain: bundle carries `chain.verified_claim` + `head_hash` + `entries`,
  verified at export time on the contributor's machine. A receiving server
  cannot re-verify a chain it does not hold — the site's methodology page
  says exactly what the claim does and does not prove.

## Freeze/approval convention

Both sessions adopt the FRP gate the escalation session proved live
(`freeze_pack_draft(approved=False)` refuses): public-affecting artifacts —
site bundles, tier-guide content, aggregate snapshots — require an explicit
operator approval step (CLI flag, consent record, or freeze call), never an
implicit default.

## Escalation of planning questions

Either session may ask the other for a confidence boost before acting below
99% certainty — via a pointer request recorded in this file (for schema/
contract decisions) or via the operator (for scope decisions). No session
guesses at the other's in-flight contracts.
