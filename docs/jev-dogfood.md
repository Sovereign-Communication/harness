# JEV-P4 dogfood + freeze evidence (ops)

**Canon:** [jev-roadmap.md](jev-roadmap.md) Phase 4. This file records the
**command surface** and **evidence template** for the with/without-jev
dogfood box and the model-pin / threshold-freeze box. Do not mark those
canon checkboxes without a filled template (or an explicit deferred note).

Hermetic unit tests stay hermetic. Live dogfood uses **cheap paid** OpenRouter
rungs for apply/plan seats (operator model policy); TypeSafe Jev is the
bounded judgment seat only.

---

## 1. With vs without Jev — comparison command surface

Jev is on when a key resolves (`~/.config/scmorc/jev.env`,
`~/.config/harness/jev.env`, or `HARNESS_JEV_KEY` / `TYPESAFE_API_KEY` /
`JEV_API_KEY`). Unkeyed lanes run explicit `is_fallback` local structural
checks.

**JEV-P4 disable switch:** `HARNESS_JEV_DISABLE=1` forces the unkeyed path
even when a key file is present (dogfood A/B only — never leave it set in
operator config).

### Apply lane (same goal twice)

```powershell
# A — with Jev (key resolved; structural gate + ledger jev_eval)
$env:PYTHONPATH = "C:\Users\SCM\Documents\GitHub\Harness-p4"
# ensure HARNESS_JEV_DISABLE is NOT set
C:\Users\SCM\Documents\GitHub\Harness\.venv\Scripts\python.exe -m harness.cli `
  apply --file <target> --instruction "<instruction>" `
  --verify "<gate>" --max-cost 0.05 --out p4_dogfood_with_jev.json

# B — without Jev (explicit fallback path)
$env:HARNESS_JEV_DISABLE = "1"
C:\Users\SCM\Documents\GitHub\Harness\.venv\Scripts\python.exe -m harness.cli `
  apply --file <target> --instruction "<instruction>" `
  --verify "<gate>" --max-cost 0.05 --out p4_dogfood_without_jev.json
Remove-Item Env:HARNESS_JEV_DISABLE
```

### Plan lane (hourglass surface)

```powershell
# A — with Jev
harness plan --goal "<goal>" --decompose-llm --confirm --task-max-cost 0.02 `
  --out p4_plan_with_jev.json

# B — without Jev
$env:HARNESS_JEV_DISABLE = "1"
harness plan --goal "<goal>" --decompose-llm --confirm --task-max-cost 0.02 `
  --out p4_plan_without_jev.json
Remove-Item Env:HARNESS_JEV_DISABLE
```

### Post-run analytics (ledger + jev calibration)

```powershell
harness ledger report --json --out p4_ledger_report.json
# report["jev_calibration"] is the advisory jev_eval ↔ verify join
```

---

## 2. Evidence template (fill before checking the P4 dogfood box)

Copy into the PR body or `docs/` note. Empty fields = box stays unchecked.

| Field | With Jev (A) | Without Jev (B) |
|---|---|---|
| Date / operator | | |
| Command surface (apply/plan) | | |
| Goal / instruction | | |
| Status (`ok`/`defer`/…) | | |
| Verify gate + pass/fail | | |
| Session cost (USD) | | |
| Jev cost in envelope / ledger | | |
| `structural.is_fallback` | | |
| `structural.model` | | |
| `structural.verdict` | | |
| Ledger `jev_eval` count (task_id) | | |
| Notes (rate limits, ceiling, retries) | | |

**Pass-rate delta:** (A ok-runs / A attempts) − (B ok-runs / B attempts)  
**Cost delta:** mean(A session cost + jev cost) − mean(B session cost)

---

## 3. Model pin + threshold freeze procedure

Product path (already live):

| Knob | Owner | Default | Freeze action |
|---|---|---|---|
| `settings.jev_model` | `config.py` / `JevEvaluator` | `jev-latest` | Write the **observed** model id (e.g. `jev-1.13.0`) into `~/.config/harness/config.json` or `HARNESS_JEV_MODEL` |
| `settings.min_confidence` | same + `ApplyRequest` / consent / jev verdict | `0.70` | Write the calibrated threshold into config or `HARNESS_MIN_CONFIDENCE` |
| Runtime helper | `harness.config.freeze_jev_settings` | — | Applies a freeze snapshot on a live Settings object for tests / operator scripts |

### Procedure (do not skip)

1. Run live TypeSafe smoke + at least one keyed dogfood battery.
2. Read `harness ledger report` → `jev_calibration` (advisory buckets +
   review notes). Calibration **never** auto-retunes thresholds.
3. Choose freeze values:
   - **Model pin:** the `structural.model` / ledger `jev_eval.model` that
     actually served the battery (not the `jev-latest` alias).
   - **Threshold:** `min_confidence` consistent with calibration buckets
     (raise if high-supported false-pass; do not silently lower bars).
4. Persist:
   ```json
   { "jev_model": "jev-1.13.0", "min_confidence": 0.70 }
   ```
   in the operator `config.json`, or set `HARNESS_JEV_MODEL` /
   `HARNESS_MIN_CONFIDENCE`.
5. Re-run hermetic gates + live smoke; confirm envelope `model` equals the
   pin (not the alias) on the next keyed call.
6. Record freeze evidence (values + date + smoke cost tokens) in the PR /
   STATUS note. Only then may the canon **Model pin + threshold freeze**
   checkbox be marked true.

Helper (tests / scripts):

```python
from harness.config import load_settings, freeze_jev_settings
s = load_settings()
print(freeze_jev_settings(s, jev_model="jev-1.13.0", min_confidence=0.70))
# {"jev_model": "jev-1.13.0", "min_confidence": 0.70, "jev_model_is_pinned": True}
```

---

## 4. Minimal live TypeSafe smoke (PR evidence)

```powershell
$env:HARNESS_JEV_LIVE_SMOKE = "1"
$env:PYTHONPATH = "C:\Users\SCM\Documents\GitHub\Harness-p4"
C:\Users\SCM\Documents\GitHub\Harness\.venv\Scripts\python.exe -m unittest `
  tests.test_jev_smoke.LiveJevSmokeTests -v
```

Paste the printed JSON (`model`, `verdict`, tokens, cost, `is_fallback`)
into the PR body. Do **not** spend large OpenRouter budgets on P4 dogfood.

---

## 5. Live evidence captured on `feat/jev-p4-ops-exit` (2026-09-21)

Not a filled A/B pass-rate table — that box stays **unchecked** until a
multi-run comparison is recorded. These are the minimal operator probes.

### TypeSafe live smoke (`HARNESS_JEV_LIVE_SMOKE=1`)

```json
{"diff": {"cost": 0.016548, "input_tokens": 394, "is_fallback": false, "model": "jev-1.13.0", "output_tokens": 21, "verdict": "fail"}, "generic": {"cost": 0.013146, "input_tokens": 313, "is_fallback": false, "model": "jev-1.13.0", "output_tokens": 36, "verdict": "fail"}}
```

- Non-fallback keyed path; observed model **`jev-1.13.0`** (alias resolved).
- Cost = `input_tokens * 42 / 1e6` (394→0.016548, 313→0.013146).

### Tiny paid plan dogfood (keyed Jev + cheap apply rung)

```text
harness plan --goal "Rename local variable in docs example for clarity" \
  --decompose-llm --no-confirm --task-max-cost 0.02 --out /tmp/p4_plan.json
```

| Field | Value |
|---|---|
| status | `planned` |
| decompose | `llm:deepseek/deepseek-v4-flash` |
| recommended_model / ladder head | `deepseek/deepseek-v4-flash` → `deepseek/deepseek-v4.1-flash` → `inclusionai/ling-3.0-flash` |
| jev triage | site=`route`, model=`jev-1.13.0`, route=`free-distill`, `is_fallback=false`, cost=$0.017892, input_tokens=426 |
| composed_worst_case | $0.010181 (ceiling $0.02 task / node $0.01) |

**Freeze implication:** live envelope model is `jev-1.13.0` while
`settings.jev_model` default remains `jev-latest` — freeze pin to the
observed id after calibration per §3.

---

## 6. Out of scope for this ops file

- `JEV-P2-jury` remains **deferred** (not invented here).
- Provider brand hardcoding — resolve apply/plan seats via `MS-*` / ladders.
- HUL dual-budget / mission driver — Track B product phases.
