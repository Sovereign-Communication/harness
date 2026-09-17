# System One & Jev Architecture Integration Plan for Harness

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

## 3. Implementation Roadmap

- [ ] **Phase 1: Architecture Spec & Docs** (Current PR)
  - Publish `docs/system-one-integration.md` defining the decision contracts and confidence gating interfaces.
- [ ] **Phase 2: Router Confidence Gating**
  - Add confidence score parsing to model responses and consent checks.
  - Implement configurable confidence thresholds for `HARNESS_DEFER`.
- [ ] **Phase 3: Fast Jury Decision Protocol**
  - Update multi-model panel protocols to support zero-token / enum-only decision verdicts.
- [ ] **Phase 4: Optional Jev / System One Provider Integration**
  - Add native support in OpenRouter / provider routing for Jev endpoints for pure triage tasks.

---

## Conclusion
Integrating System One decision mechanics allows Harness to preserve its zero-dependency, cost-bounded philosophy while drastically cutting latency and preventing wasteful execution cycles on low-confidence attempts.
