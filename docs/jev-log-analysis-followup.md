# Follow-up addendum — Jev log-factor analysis (post-current-mission)

**Role of this file:** deferred operator planning brief. It is **not** the operational plan.

| | |
|---|---|
| **Canon (unchanged)** | [jev-roadmap.md](jev-roadmap.md) — STATUS, DoD, playbooks only |
| **This file** | Follow-up product shape for **after** the current mission reaches an honest exit / operator schedules this work |
| **Conflict rule** | If anything here disagrees with the canon, **the canon wins** |
| **STATUS rows** | **None.** Do not add `JEV-LOG-*` to canon STATUS until this track is explicitly promoted |

**Do not start implementation from this file while other worktrees/sessions hold WIP** (P4 / P5 / HUL / Freebuff). This addendum is a parking plan so the current plan stays single-threaded.

---

## 1. Problem (bounded)

Operator has runtime log dumps (first dogfood: `C:\temp\logsSCMessenger.txt`) and wants **structured analysis**, not chat prose.

Product shape the operator confirmed:

1. **JSON only** — no narrative synthesis from Jev.
2. **Pre-defined buckets** — items are classified into operator-approved categories.
3. **Sentiment / attention score per item** — typed score (or score-like level), not free text.
4. **Two-seat pipeline:**
   - **Cheap generative pass** (MS ladder / cheapest capable rung) → discover / propose **relevant factors** and draft buckets for this log class.
   - **Jev** → audit items against the **approved** pack: bucket choice + score; code owns parsing and aggregation.

Jev does **not** invent buckets, paths, or actions. Same 0-hallucination rule as `JEV-P5` issue-sort.

---

## 2. What already ships (reuse — do not fork)

| Seam | On origin (as of follow-up drafting) | Use for log analysis |
|---|---|---|
| `JevEvaluator.evaluate(state, questions)` | P0 | Typed noul/choice/score over **distilled** state |
| `JevPolicy` + `policy_for` | P1 | ONE owner: preflight, ledger `jev_eval`, structural envelope |
| `harness/jev_packs.py` + `validate_operator_pack` / `issue_sort_question_pack` / `match_keywords` | P5 | Operator bucket packs; keyword fallback; criteria = operator labels only |
| `JevPolicy.evaluate_issue_sort` + CLI `harness issue-sort` | P5 face | Closest product pattern for “bucket this text” |
| `JEV_MAX_INPUT_TOKENS` (~1024) + context-pack distill (~1200 chars) | P1/P3 | Never feed raw multi-hundred-KB logs to live Jev |
| HUL-A `mission init\|status\|resume` + receipts / `jev_evals.jsonl` | HUL-A | Optional evidence pack around a dogfood run |
| MS / `sliding_scale` / router ladders | MS-* (partially open) | Stage-B cheap factor-discovery seat — **not** Jev |
| FRP process rules | docs done | Swap-grade, verdicts, evidence, bounds; builder ≠ sole grader |

**Gap this follow-up fills:** no log parser, no log-factor score packs, no policy site for multi-item log audit, no end-to-end “factor pass → Jev data” product path.

---

## 3. Target architecture (single pass, JSON out)

```text
  raw log dump (e.g. C:\temp\logsSCMessenger.txt)
           |
           v
  [A] CODE — parse + extract candidate items
        (levels, modules, events, WARN/ERROR lines,
         message ids, peers — no model spend)
           |
           v
  [B] CHEAP GENERATIVE (MS ladder) — once per log class
        propose: factor list + operator pack draft
        (bucket ids, labels, kind, keywords,
         score dimension + level criteria)
           |
           v
  [C] OPERATOR — approve / edit pack  (FRP-authz)
        pack is immutable for the Jev pass
           |
           v
  [D] JEV via JevPolicy — per item (or per factor-chunk)
        choice: operator bucket ids only
        score:  operator-defined sentiment/attention levels
        envelope: structural + cost + is_fallback + site=
           |
           v
  [E] CODE — aggregate JSON artifact
        counts by bucket, score distributions,
        unmatched items, fallback rate, cost, evidence refs
```

**Optional (explicitly not “jev only”):** a generative seat may write a human report **from** the JSON. Jev never owns that step.

### Stage contracts

| Stage | Owner | Output | Spend |
|---|---|---|---|
| A parse/extract | Code | `items[]`: `{id, text, module, level, ts, evidence}` + mechanical tallies | $0 |
| B factor pass | Cheap generative (MS) | `pack_draft` JSON matching operator-pack schema + proposed score criteria | cheap paid rung, bounded |
| C approve | Operator | `pack.json` frozen for the run | $0 |
| D Jev audit | `JevPolicy` | per-item `{bucket, path_id?, score, structural}` | TypeSafe input-priced; unkeyed → keyword only + `is_fallback=true` |
| E aggregate | Code | `analysis.json` (+ optional HUL receipts) | $0 |

### Pack schema extension (follow-up product work)

Build on P5 operator packs; add **one** score dimension (do not replace P5):

```json
{
  "id": "scmessenger-ops-log-v1",
  "buckets": {
    "transport": {
      "label": "Transport / swarm health",
      "kind": "trouble_area",
      "path_id": "log/transport",
      "keywords": ["dial", "swarm", "listener", "negotiation", "yamux"],
      "suggested_next_action": "review dial/relay policy",
      "attention": "high"
    },
    "delivery": {
      "label": "Outbox / inbox delivery",
      "kind": "trouble_area",
      "path_id": "log/delivery",
      "keywords": ["outbox", "inbox", "delivered", "history"],
      "suggested_next_action": "trace message lifecycle",
      "attention": "medium"
    }
  },
  "score": {
    "id": "sentiment",
    "instructions": "Rate operational severity/attention for this log item from the stated levels only.",
    "levels": [
      "benign — routine info, no operator attention",
      "elevated — degraded but bounded behavior",
      "actionable — likely defect or policy issue",
      "critical — security, data loss, or hard outage signal"
    ]
  }
}
```

**Rules:**

- Choice criteria keys/labels = operator buckets only (existing P5 parse contract).
- Score criteria = operator level strings only; Jev returns score + probability distribution — code maps levels to ordinals if needed.
- `path_id` / `suggested_next_action` always come from the pack, never from the model.
- Unmatched / unkeyed / out-of-pack → `bucket=null`, `is_fallback=true`, keyword match only when keywords hit; never invent a bucket.

---

## 4. Proposed follow-up IDs (promotion into canon only)

Use these IDs **only after** this addendum is promoted into `docs/jev-roadmap.md`. Until then they are planning labels, not STATUS rows.

| ID | Work | Primary modules | Gate evidence |
|---|---|---|---|
| `JEV-LOG-schema` | Operator log-pack schema + validation (P5 pack + `score` block) | `harness/jev_packs.py` (extend, one owner) | `tests/test_jev_log_pack.py` |
| `JEV-LOG-parse` | Code-owned log item extractor + mechanical tallies | `harness/log_items.py` (or similar) | hermetic fixture on real SCMessenger log sample |
| `JEV-LOG-factor-pass` | Cheap generative factor/bucket draft → pack proposal adapter | small helper + MS resolve; **no brand hardcoding** | fixture pack draft; human-approve step documented |
| `JEV-LOG-judgment` | `JevPolicy.evaluate_log_item` / batch: choice + score via packs | `harness/jev_policy.py` only | `tests/test_jev_log_judgment.py` — keyed/unkeyed, 0-hallucination, ledger once per call |
| `JEV-LOG-envelope` | Aggregate JSON artifact + `structural.site=log_factor` | policy + thin CLI/MCP | envelope keys stable; cost honest |
| `JEV-LOG-cli` | Thin `harness log-judgment --pack … --items …` (or mission face) | `harness/cli.py` | calls policy owner only |
| `JEV-LOG-dogfood` | Single-pass run on `C:\temp\logsSCMessenger.txt` | artifacts + receipts | JSON + cost + fallback rate recorded |

**Suggested site string:** `log_factor` (ledger + structural).  
**Suggested branch when scheduled:** `feat/jev-log-factor-analysis` in a fresh worktree off `origin/main` — **not** inside active P4/HUL worktrees.

---

## 5. Single-pass success definition (dogfood)

A **successful jev-side single pass** on the SCMessenger log means all of the following are true in the JSON artifact:

1. **Coverage:** every extracted WARN/ERROR (and a defined sample of INFO events) is either bucketed or explicitly `unmatched` — no silent drops.
2. **Taxonomy integrity:** every `bucket` value ∈ operator pack keys; every non-fallback score level ∈ pack `score.levels`.
3. **Fallback honesty:** `is_fallback` counts reported; unkeyed runs never claim live judgment.
4. **Evidence:** each item keeps code-owned refs (line index / module / event name); Jev reasons are advisory only.
5. **Mechanical baseline:** code-owned tallies (level counts, top modules, event histogram) present **without** model spend — Jev adds judgment on top, not instead.
6. **Cost envelope:** input tokens + `$ = tokens * 42 / 1e6` recorded; preflight respected (chunking if items exceed ceiling).
7. **FRP-swap-grade:** pack draft from the cheap seat is **not** self-approved; operator or independent inspect freezes the pack before Stage D.

**Not required for “jev single-pass value”:** narrative report, HUL complete flag, multi-round until-limits loop, or panel/judge synthesis.

---

## 6. First dogfood target (already scouted)

| Item | Fact |
|---|---|
| Path | `C:\temp\logsSCMessenger.txt` (absolute; outside Harness tree) |
| Size | ~525 KB / ~2681 lines |
| Levels | INFO ~2135 · WARN ~372 · ERROR 1 |
| Hot modules | `transport::swarm`, CLI, `yamux`, contacts, ledger_entry, relay_custody, dial_policy, routing, history, inbox |
| Candidate factor themes | transport/dial/relay drift; connection_limits; custody retention; BLE adapter missing; history/delivery orphans; ledger hex migration; auto-reply policy; peer backoff |

Stage B’s job is to turn themes into a **concrete pack draft**, not to re-audit SCMessenger **code** (that remains `audits/scmessenger/` via `harness verify` — different track).

---

## 7. Promotion path (keeps current plan clean)

| Phase | Action | When |
|---|---|---|
| **Now** | This file only. **No** canon STATUS edits. **No** worktree for log work. | Operator holds WIP elsewhere |
| **Current mission exit** | Canon Exit rows true **or** operator explicitly schedules this track ahead of remaining HUL/P4 | After gates green / operator call |
| **Promotion** | Add a short canon section + STATUS rows (`JEV-LOG-*`, status `open`) **in one docs PR** using the IDs above | Only when starting |
| **Implementation** | One phase per PR, worktree off `origin/main`, gates before merge; FRP process rules; no provider brands in phase PRs | After promotion |
| **Never** | Parallel STATUS in this file; second `jev_policy`; raw-log live Jev; model-invented buckets | Always |

### Suggested canon pointer (text to paste at promotion — do not apply yet)

```markdown
### Follow-up track — Jev log-factor analysis (`JEV-LOG-*`)

Deferred operator analysis: code extracts log items → cheap seat proposes
operator pack (buckets + score levels) → operator freezes pack → Jev
choice+score via `jev_policy` → code aggregates JSON. JSON only; no
narrative from Jev. Details: `docs/jev-log-analysis-followup.md`.
First dogfood: `C:\temp\logsSCMessenger.txt`.
```

---

## 8. Explicit non-goals (this addendum)

- Replacing or delaying current canon phases (P4, HUL-B/C/D, MS, open Freebuff WIP).
- Free-form “summarize this log” chat behavior.
- Second Jev client, private packs outside `jev_policy` / `jev_packs`.
- Treating keyword fallback as live sentiment.
- Hardcoding model brands in phase code (resolve via MS / ladders).
- Marking HUL complete because a log JSON exists.

---

## 9. Operator checklist when this becomes active

1. Confirm no other session holds Harness WIP that would collide.
2. Fetch `origin/main`; read canon STATUS only for truth.
3. Promote pointer + STATUS rows (docs PR) — or operator runs an open-problem HUL pack **without** product IDs if they only want dogfood once.
4. Freeze a first pack (Stage C) from a cheap factor pass on the real log sample.
5. Run Stage D with key present; record cost + fallback rate.
6. Publish `analysis.json` + receipts; decide promote-to-product vs stay operator tooling.

---

## 10. One-line summary

**After the current mission:** keep using Jev as a **typed auditor over pre-defined operator buckets + sentiment/attention scores**; a **cheap generative pass** only proposes the factor/pack; **code** owns parse + aggregate; **JSON only**. Current canon plan stays unchanged until this track is explicitly promoted.

---

*Draft date: 2026-09-21. Non-canonical. Canon: [jev-roadmap.md](jev-roadmap.md).*
