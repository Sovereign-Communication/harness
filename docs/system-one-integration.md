# System One & Jev Architecture Integration Plan for Harness

> **Canonical operational plan:** [jev-roadmap.md](jev-roadmap.md) only (`JEV-Pn-*` + `HUL-*`, STATUS, DoD, schedule).  
> This document is **architecture rationale** — not a tracker. Milestones below map to canon IDs; if status disagrees, **the canon wins**. Live client work started in `#PR-Jev-Live` / PR #34.

## Executive Summary

TypeSafe AI's release of **Jev** and the **System One** model paradigm shifts the focus of AI infrastructure from conversational text generation to high-speed, machine-native decision execution. Standard LLMs operate under a "System Two" mode—slow, sequential, token-by-token reasoning. System One models, by contrast, evaluate structured decisions (choices, classifications, scores) in parallel with millisecond latency and low cost using techniques like **Reinforcement Learning for Calibrated Decisions (RLCD)**.

This document details how **Harness**—our cost-bounded multi-model verification engine—can incorporate key lessons from System One models to enhance its router, autonomy ledger, and spend governance mechanisms.

---

## 1. Key Lessons from System One / Jev

1. **Decisions Over Text:** Traditional LLM workflows parse unstructured text or JSON tool calls, introducing latency, token cost, and schema parsing errors. System One models evaluate pre-defined decision enums directly in model logits.
2. **Parallel Inference & Speed:** By eliminating generative token decoding, decision evaluation drops from several seconds to 70–500ms.
3. **Calibrated Confidence:** Through RLCD, System One models output statistical confidence scores that faithfully mirror actual accuracy probabilities, enabling deterministic confidence gating.
4. **Machine-Native Efficiency:** Eliminating heavy system prompts and verbose output tokens reduces call costs by up to 400x ($0.042/1M input tokens, zero cost output tokens).

---

## 2. Integration Strategy for Harness

While Harness focuses on code application, editing, and verification using external models, it routinely makes meta-decisions:
- Should a model **accept**, **decline**, or **defer** a task?
- Is a proposed edit confident enough to warrant running a high-cost test gate?
- How should a multi-model jury panel reach consensus efficiently?

We propose incorporating System One paradigms into Harness across three architectural pillars:

```
                      +---------------------------------------+
                      | Incoming Task / Modification Request |
                      +---------------------------------------+
                                          |
                                          v
                    +-------------------------------------------+
                    |  1. Fast-Path Pre-Flight Triage          |
                    |     (System One Decision Pass: ~100ms)    |
                    +-------------------------------------------+
                                          |
                        +-----------------+-----------------+
                        |                                   |
                [High Confidence]                   [Low Confidence]
                        |                                   |
                        v                                   v
          +---------------------------+        +---------------------------+
          | Standard Generative Edit  |        | Instant HARNESS_DEFER     |
          | & Verification Gate       |        | Handoff to Tier 2/3       |
          +---------------------------+        +---------------------------+
                        |
                        v
          +---------------------------+
          | 2. Calibrated Ledger Log  |
          |    & Jury Panel Check     |
          +---------------------------+
```

### Pillar 1: Fast-Path Pre-Flight Triage
* **Current State:** Harness uses chat completions pre-flight prompts to ask whether a model accepts or defers work.
* **System One Enhancement:** Implement a standardized, micro-prompted decision contract (or integrate fast System One models like Jev for triage). The pre-flight pass classifies complexity (e.g. `SIMPLE_EDIT`, `COMPLEX_REFACTOR`, `OUT_OF_SCOPE`) and confidence score in a single pass without token streaming.

### Pillar 2: Confidence-Driven Deferral Gating
* **Current State:** Deferral happens when a model explicitly emits `HARNESS_DEFER` or hits rate/error limits.
* **System One Enhancement:** Leverage calibrated confidence scores. If a model's self-assessed confidence falls below a configured threshold (e.g., `< 0.70`), Harness immediately triggers an automated `HARNESS_DEFER` handoff to a higher-capability model tier **before** spending budget on writing files or executing test gates.

### Pillar 3: Ultra-Lean Machine-Native Jury Panels
* **Current State:** Multi-model panels generate full text advisory verdicts, increasing total token burn.
* **System One Enhancement:** For `--verify-only` or jury consensus steps, request single-token or enum-constrained responses (e.g., `PASS`, `FAIL`, `DEFER`) with associated confidence scores. This drops panel verification latency to sub-second speeds and reduces cost to near zero.

---

## 3. Detailed Technical Specifications

### 3.1 Fast-Path Decision & Consent Schema Contract

System One decision interfaces replace unstructured natural language reasoning with strict JSON schema definitions for decisions and calibrated confidence bounds:

```json
{
  "decision": "accept" | "decline" | "defer" | "redirect",
  "confidence": 0.85,
  "reason": "Task matches AST scope and context window constraints.",
  "redirect_model": null,
  "scope_suggestion": null
}
```

#### Field Specifications:
- `decision` (string, required): One of `"accept"`, `"decline"`, `"defer"`, or `"redirect"`.
- `confidence` (float, optional): A calibrated probability value \([0.0, 1.0]\) indicating the model's confidence in its ability to execute the task successfully.
- `reason` (string, required): Brief concise rationale for the decision.
- `redirect_model` (string or null, optional): An alternative model identifier if redirecting.
- `scope_suggestion` (string or null, optional): A suggested narrower task scope if redirecting.

### 3.2 Confidence Calibration & Deferral Gating

Standard LLM confidence is notoriously poorly calibrated (often displaying overconfident hallucinations). Through **Reinforcement Learning for Calibrated Decisions (RLCD)**, System One models produce probabilities that correspond directly to observed accuracy rates.

Harness integrates this via the following deferral rules:
1. **Implicit Deferral on Low Confidence**: If a model returns `"decision": "accept"`, but its `confidence` score is below the configured threshold (e.g. `confidence < 0.70`), Harness intercepts the acceptance and treats it as an automatic `HARNESS_DEFER` handoff.
2. **Autonomy Ledger Evidence**: The ledger records the self-assessed confidence, reason, and any automated threshold override in the hash-chained JSONL file, enabling longitudinal tracking of model calibration and under/overconfidence metrics.

### 3.3 Fast Jury Panel Protocol (Enum-Constrained Consensus)

For advisory panel verification (`--verify-only` or multi-model voting), generative text synthesis generates excess tokens. Under the System One protocol:
- Panelists evaluate task claims or code diffs and emit single-token enum selections:
  ```json
  {
    "verdict": "pass" | "fail" | "defer",
    "confidence": 0.92,
    "claim_id": "syntax_clean"
  }
  ```
- The Harness `convergence` engine aggregates logits/choices without requiring multi-paragraph judge synthesis, reducing panel evaluation latency from ~10s to <1s.

---

## 4. Implementation Roadmap & Milestones

Status after Freebuff `#PR-Jev-Live` audit (2026-09-20). Detailed work items live in [jev-roadmap.md](jev-roadmap.md).

- [x] **Milestone 1: Architectural Foundation & Decision Contracts**
  - Publish `docs/system-one-integration.md` defining JSON decision schemas, calibration mechanics, and integration boundaries.
  - Link architecture in repository `README.md`.
- [ ] **Milestone 2: Confidence Extraction & Ledger Recording** — `JEV-P2-consent-confidence`
  - Update `harness.consent.probe_consent` to parse optional calibrated `confidence` floats from model output.
  - Record `confidence` into ledger consent events (`consent_accept`, `consent_defer`).
- [ ] **Milestone 3: Automated Confidence-Gated Deferrals** — `JEV-P2-min-confidence`
  - Wire `HARNESS_MIN_CONFIDENCE` (settings default `0.70`) into consent/apply abstention via `sliding_scale.should_abstain`.
  - Automatically escalate/rotate to next tier model if reported confidence is below threshold.
- [~] **Milestone 4: Native Jev / System One Provider Endpoints** — `JEV-P0-*` / `JEV-P1-*`
  - Partial: `harness/jev.py` posts to `https://api.typesafe.ai/v1/systemone` with local fallback; agent lane only.
  - Remaining: honest cost/parse/questions (P0); ONE policy owner wired across CLI/MCP/waist/apply (P1); ledger + spend.

---

## Conclusion
Integrating System One decision mechanics allows Harness to preserve its zero-dependency, cost-bounded philosophy while drastically cutting latency and preventing wasteful execution cycles on low-confidence attempts.
