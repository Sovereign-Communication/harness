# SCMessenger — Read-Only AI Security Audit (Handoff)

**Date:** 2026-09-02
**Method:** `harness verify` — rotating panel (3 cheap free models) + structured judge verdict per function. **Strict hands-off: no files were read-write touched, no edits applied.** All input was function source extracted read-only from the working tree.
**Cost:** $0.00 (free tier, all 9 functions).
**Caveat:** These are **AI-generated candidate findings** from free-tier consensus — a triage signal, not a verified CVE report. Each should be confirmed by a human against the actual code before acting. Items marked **[confirmed by analyst]** were independently spot-checked against the source in this repo.

Functions audited (9):

| # | Function | File | Agreement | Conf. | Verdict |
|---|---|---|---|---|---|
| 1 | `negotiate_suite` | `core/src/crypto/negotiation.rs` | high | 0.85 | Transcript ambiguity + downgrade |
| 2 | `decrypt_message_ratcheted_v2` | `core/src/crypto/encrypt.rs` | high | 0.91 | PQ-stripping pre-confirm + transcript scope (converged) |
| 3 | `Ratchet::encrypt` | `core/src/crypto/ratchet.rs` | high | 0.9 | Index underflow / key handling |
| 4 | `Ratchet::decrypt` | `core/src/crypto/ratchet.rs` | high | 5/5 | Gap DoS + skipped-cache growth + low-order point (converged) |
| 5 | `decode_wire_signed_envelope` | `core/src/message/codec.rs` | high | 0.85 | Unbounded bincode + format fallthrough |
| 6 | `construct_onion` | `core/src/privacy/onion.rs` | high | 5/5 | CLEARED — construction sound (converged) |
| 7 | `peel_layer` | `core/src/privacy/onion.rs` | medium | 0.9 | Destination-oracle + unbounded bincode (converged) |
| 8 | `safety_number` | `core/src/identity/keys.rs` | 6/6 | 1.0 | Modulo bias + dropped hash tail + low entropy (converged) |
| 9 | `verify_bundle` | `core/src/identity/keys.rs` | high | 0.85 | ML-DSA downgrade |

---

## Findings by function

### 1. `negotiate_suite` — HIGH agreement, 0.85
Suite negotiation + transcript hash.
- **Transcript ambiguity/collision from single-byte `0xFF` delimiter.** `material` is `our_suites || 0xFF || their_suites || 0xFF || suite || pubs`. Suite IDs are `u8`, and the test suite itself uses `0xFF`/`0xFE` as valid suite IDs — so a suite list containing `0xFF` collides with the delimiter, producing ambiguous (non-injective) encodings that could yield the same transcript from different inputs.
- **Downgrade risk in selection rule.** `max()` of the intersection means the weaker/older peer dictates the suite; a peer advertising only `0x01` always wins even if both support `0x03`. (The code's own regression test *wants* 0x01 fallback for interop, so this is a policy question, not a clear bug — but it means "max of intersection" gives the low side veto.)
- Panel also flagged: suite lists are not authenticated in the binding; recommends length-prefixed encoding + deterministic selection.

### 2. `decrypt_message_ratcheted_v2` — LOW agreement, 0.5, **DEFER**
Panel not fully aligned (flag as lower-confidence).
- **No monotonic message-number check** — the decrypt path does not reject out-of-order/replayed message numbers before processing, raising replay concerns. (Compare with `Ratchet::decrypt`, which uses a skipped-keys cache; this layer may or may not be covered downstream.)
- **PQ anti-stripping scoped inside `peer_confirmed`.** PQ field validation only runs for `is_pq_hybrid() && peer_confirmed`; the panel suggests the stripping check should not be conditional on confirmation.
- **Transcript hash only verified before confirmation**, not for ongoing messages.

### 3. `Ratchet::encrypt` — HIGH agreement, 0.9
- **Potential `chain.index - 1` underflow** if the sending chain index is ever 0 on first encrypt (`u32` underflow panics in debug, wraps in release). Should be `checked_sub` or index initialized at 1.
- Nonce generated fresh via `OsRng` per message (acceptable) — panel suggests a counter or `thread_rng` for speed; not a correctness issue.
- Derived `message_key` is not zeroized after use.

### 4. `Ratchet::decrypt` — NONE agreement, 0.2, **DEFER (inconclusive)**
Panel could not reach consensus. Candidate concerns (unverified):
- Message-number bounds (potential DoS via excessive ratchet steps / huge gaps).
- Trial-adoption clone pattern possible state loss on out-of-order keys.
**Recommend human review; do not treat as confirmed.**

### 5. `decode_wire_signed_envelope` — HIGH agreement, 0.85 **[confirmed by analyst]**
- **Unbounded bincode deserialization of untrusted bytes** — no `bincode::Options` size limit, so length-prefixed fields in attacker-controlled input can cause large allocations (resource exhaustion / DoS).
- **V2 → V1 fallthrough on parse failure** is a format-confusion risk: a V2-tagged buffer that fails V2 parse is retried as V1 (potentially as the whole buffer including the tag). Recommend removing the fallthrough.

### 6. `construct_onion` — LOW agreement, 0.2, **DEFER (inconclusive)**
Panel disagreed on nonce reuse and layer-metadata ambiguity. No reliable conclusion. Needs deeper review.

### 7. `peel_layer` — agreement unknown (embedded verdict: medium/0.85)
- **Decryption-oracle / destination-detection concern.** The classical branch treats `encrypted_routing_info.is_empty()` as "I am the destination" and returns the payload *without* an explicit "is final hop" authenticator. If an attacker can submit a layer with empty routing-info ciphertext, a relay may early-exit as destination. (Nuance: payload is still AEAD-encrypted under the relay's key, so an off-path attacker can't read it — but the early-exit / missing "destination authenticity" marker is worth confirming.)
- **Unbounded bincode parse of next-hop routing data.**
- Recommend a sentinel/authenticated "final hop" marker rather than empty-ciphertext inference.

### 8. `safety_number` — HIGH agreement, 0.95 **[confirmed by analyst]**
- **Modulo bias + low entropy.** `val = u16::from_be_bytes([h[o], h[o+1]]) % 100000` over `0..=65535` means digit groups in `[65536, 99999]` are never reachable — heavy modulo bias, so not every 5-digit group is equally likely.
- **Only the first 24 hash bytes are used** (`offset = (group*2) % 24`), so groups 12–23 recycle the same bytes cyclically; entropy collapses and later groups add ~nothing.
- Genuinely weak for a user-verifiable identity fingerprint. Should use more hash material (e.g. Signal's approach: base-10 over more bytes / `% 10000` on wider chunks) and avoid cyclic byte reuse.

### 9. `verify_bundle` — HIGH agreement, 0.85 **[confirmed by analyst]**
- **Critical downgrade.** ML-DSA is only required when *both* `mldsa_public` and `mldsa_signature` are present in the untrusted bundle. An attacker can strip both fields to force the legacy Ed25519-only path (v1/v2), downgrading away the PQ authenticity guarantee. Requirement should be policy/version-based, not field-presence-based.
- **[confirmed]** The **v1 signature input omits `supported_suites`** (v2/v3 include it). On a legacy bundle, an attacker could alter the suites list without invalidating the signature.
- No freshness/replay protection on `created_at`.

---

## Consensus iteration (iterations 2–3)

The four lower-confidence audits (02, 04, 06, 07) were re-run with a **model rotation** (panel = `gemma-4-31b` + `minimax-m3`, the two free models that reliably emit JSON; judge = `north-mini-code`) and **clarified prompts** (structured per-claim verdicts; verbatim source for 06/07). All four now converge (5/5 panel agreement per function). Per-claim panel data is in `_runs/*_it2.json` / `*_it3.json`.

### 02 `decrypt_message_ratcheted_v2` — CONVERGED (high, 0.91)
**Real issues:** PQ-stripping check (`validate_pq_fields_present`) only runs while `peer_confirmed`, so a PQ-stripped envelope is not detected before confirmation; the transcript hash is verified only pre-confirmation; PQ fields are processed *before* the AEAD/auth step, so a forged message could mutate session state before authentication.
**Not defects:** replay is delegated to `session.decrypt` (monotonic + skipped-keys); the `message_number > 0` cadence exclusion is correct (msg 0 is the already-consumed bootstrap).

### 04 `Ratchet::decrypt` — CONVERGED (5/5)
**Real issues:** no upper bound on the message-number gap (hostile/desynced peer can force huge skipped-key storage / excessive ratchet steps → DoS); `skipped_keys` cache grows unbounded; `X25519PublicKey::from(raw bytes)` does not reject all-zero/low-order DH points.
**Not defects:** the clone-and-commit (trial-adoption) pattern is correct — it does not lose state on failure.

### 06 `construct_onion` — CONVERGED (5/5) — CLEARED
All five claims **False** once the relay-wrap loop was quoted verbatim: no nonce reuse (each layer derives its own `shared_secret`; nonce counters 0/1 are used only within one secret), the single-byte counter is sufficient (≤2 uses per secret), no KDF-separation leak, a relay cannot peel more than its own layer, and ephemerals are fresh per layer. **The onion construction is sound on these axes.** The earlier panel split was an artifact of a paraphrased prompt.

### 07 `peel_layer` — CONVERGED (medium, 0.9)
**Real issues:** destination detection relies on `encrypted_routing_info.is_empty()` (ciphertext length, not decrypted+authenticated plaintext) with no authenticated final-hop marker; the decrypted routing info is parsed with bincode without a size limit (memory DoS); the payload AEAD is not bound to the routing info.
**Impact cap:** claim_2 — an off-path attacker **cannot** read the payload via the shortcut (it stays AEAD-authenticated under the relay's key), so severity of the oracle is ≤ medium, not a confidentiality break.

### Converged re-audits (01, 03, 05, 08, 09) — `--converge` run

Same panel (gemma+minimax) + convergence tally. **5/5 unanimous = 100% per claim.**

| # | Function | Panel | Result (defect claims unanimous) |
|---|---|---|---|
| 1 | `negotiate_suite` | 5/5 (1.0) | 0xFF delimiter collision; downgrade-by-weakest; unauthenticated suite lists; no suite-value validation. (Deterministic behavior confirmed — no non-determinism defect.) |
| 3 | `Ratchet::encrypt` | 5/5 (1.0) | `index-1` underflow; `message_key` not zeroized. (Random per-message nonce confirmed acceptable.) |
| 5 | `decode_wire_signed_envelope` | 5/5 (1.0) | Unbounded bincode; V2→V1 format confusion. (Tag guard + length checks + empty/huge guards confirmed present.) |
| 9 | `verify_bundle` | 5/5 (1.0) | ML-DSA downgrade; v1 omits `supported_suites`; no `created_at` freshness. (Key self-consistency enforced — no key-confusion.) |
| 8 | `safety_number` | **6/6 (1.0)** | Modulo bias + dropped hash tail + insufficient entropy (converged) |

### 08 `safety_number` — CONVERGED (6/6, 100%)
Resolved by a two-part fix: (1) the original claim_2 was **factually wrong** — `(group*2) % 24` over the 12 groups reads hash bytes 0–23 exactly once each (nothing is re-read); the real defect is that **hash bytes 24–31 are never read**. (2) Claims were rephrased as unambiguous **defect propositions** so `real:true` = defect present. Final unanimous verdict:
- **Real defects:** modulo bias (`% 100000` over u16 0–65535); dropped 8-byte hash tail (only 24 of 32 bytes used); independent entropy loss beyond the bias; overall **insufficient entropy** for visual MITM comparison.
- **Not defects (confirmed correct):** order-independence (sorting works); 32-byte length validation (present).

Fix recommendation: map wider, unbiased chunks of the full 32-byte hash to digits (e.g. base-10 over more bytes, `% 10000` on wider values) and use all hash bytes.

## Prioritized action list (proposed, unverified)

1. **`verify_bundle`** — enforce PQ verification by version/policy, not by presence of untrusted ML-DSA fields; sign `supported_suites` in all versions. *(5/5 converged, most severe)*
2. **`safety_number`** — fix modulo bias + dropped 8-byte hash tail before shipping to users (all 6 claims converged).
3. **`decode_wire_signed_envelope`** — bound bincode with size limits; remove V2→V1 fallthrough.
4. **`Ratchet::encrypt`** — guard `index - 1` underflow; zeroize message key.
5. **`negotiate_suite`** — length-prefix the transcript delimiters; decide explicit downgrade policy.
6. **`Ratchet::decrypt`** — cap the message-number gap and bound/prune `skipped_keys`; reject low-order/all-zero DH points.
7. **`decrypt_message_ratcheted_v2`** — move PQ-stripping validation outside the `peer_confirmed` gate; process PQ fields only after AEAD auth.
8. **`peel_layer`** — replace the empty-ciphertext destination check with an authenticated final-hop marker; bound the bincode parse. *(Impact capped: not an off-path confidentiality break.)*
9. **`construct_onion`** — **cleared** after verbatim-source review; no nonce/key-reuse or layer-boundary defect found.

## Raw data
Full JSON verdicts per function are in `audits/scmessenger/_runs/` (gitignored): `NN_<function>_v2.json`. Prompts used are in `audits/scmessenger/prompts/` (gitignored). Re-run any audit with:
```bash
harness verify --prompt-file audits/scmessenger/prompts/09_verify_bundle.txt \
  --reasoning-effort off --max-tokens 1400 --out verdict.json
```

---

## Round 4 re-audit (2026-09-05) — post-merge Harness (PR #1, gemma judge, deterministic tally)

**Harness progression since round 3:** unreliable judge demoted from default (north-mini 0.73 → gemma 1.00); specialist ladder ordered by *observed* ledger track record (GLM-5.2's free-tier 0/22 was upstream saturation, not capability — paid slug verified 200/strict-JSON); shortfall vs disagreement correctly separated (CEO handoff bug fixed); full panel participation enforced before convergence; malformed panel output can never count as a vote; context-budget guard; parallel panel fan-out (~3x latency); claim grounding lint with source_refs.

**SCMessenger round-4 results (9 functions, free tier, $0.00, panel 3/3 gemma+minimax+nemotron, judge gemma):**

| # | Function | R3 verdict | R4 tally | R4 gate | Delta |
|---|---|---|---|---|---|
| 01 | negotiate_suite | high 0.85 | 4/5 claims real (3R/0R each); claims 2,3 split | DEFER | stricter — transcript-delimiter (c1), selection-veto (c4), unauthenticated-suites (c5) unanimous-real |
| 02 | decrypt_ratcheted_v2 | DEFER | PQ-stripping (c2) 2R/0R real; replay (c1) cleared 0/2 | DEFER (2/3 panel, shortfall) | clarified: PQ pre-confirm window confirmed real; no-replay-check cleared |
| 03 | Ratchet::encrypt | high 0.9 | 5/5 claims real, 3R/0R on four | DEFER (c1 2R/1R split only) | underflow retracted in R3 reachability; panel still flags key-zeroize + related |
| 04 | Ratchet::decrypt | DEFER→converged | gap-DoS (c1), skipped-cache (c2), trial-adoption (c4), rng (c5) 3R/0R | DEFER (c3 split) | now claim-resolved: real defects named precisely vs R1 vagueness |
| 05 | decode_wire_signed_envelope | high 0.85 [confirmed] | 5/5 claims real (c1 2R/1R) | DEFER (c1 split) | bincode DoS + V2→V1 fallthrough again unanimous (c2-c5) |
| 06 | construct_onion | CLEARED (5/5) | 5/5 claims not_real, 3R/0NR, conf 0.97-0.98 | **PASS 100%** | **identical verdict — stable across rounds, judged by different judge/panel** |
| 07 | peel_layer | medium 0.9 | dest-oracle (c1) 2R/0R real; c2 split 1R/1R | DEFER (2/3, shortfall) | destination-oracle + unbounded bincode hold |
| 08 | safety_number | 6/6 real | 3R/0R on modulo-bias, cyclic-reuse, entropy; reassurance claims cleared 0/3 | **PASS 100%** | **matches R3 6/6 exactly — claim structure now split correctly (2a/2b)** |
| 09 | verify_bundle | high 0.85→MEDIUM (reachability) | 5/5 claims real 3R/0R, conf 0.98-0.997 | **PASS 100%** | defect claims unanimous; severity stays MEDIUM per unwired call-graph |

**Round-over-round conclusions:**
1. **Verdicts are reproducible across judges and harness versions** — construct_onion (cleared), safety_number (all defects real), verify_bundle (all defect claims real) agree with round 3 despite a completely different judge lane and deterministic tally replacing judge prose.
2. **The stricter gate moves disagreement to where it lives.** R4 defers are driven by one split claim per function (e.g. 01-claim_3, 04-claim_3, 05-claim_1), not vague "low agreement" — actionable clarification targets.
3. **Net standing severity unchanged from the reachability pass:** 0 critical / 2 pre-auth DoS (bincode decode) / safety_number display defects / verify_bundle unwired-but-real when hybrid lands. Round 4 adds confidence (unanimous claim-level votes) without changing a single severity grade.

### Round-4 full results table (agreement / confidence / deferral / participation)

| # | Function | Agreement | Confidence | Deferred | Panel | Shortfall |
|---|---|---|---|---|---|---|
| 01 | negotiate_suite | low | 0.60 | yes | 3/3 | no |
| 02 | decrypt_ratcheted_v2 | low | 0.80 | yes | 2/3 | **yes** |
| 03 | Ratchet::encrypt | low | 0.80 | yes | 3/3 | no |
| 04 | Ratchet::decrypt | low | 0.60 | yes | 3/3 | no |
| 05 | decode_wire_signed_envelope | low | 0.80 | yes | 3/3 | no |
| 06 | construct_onion | high | 1.00 | **no** | 3/3 | no |
| 07 | peel_layer | low | 0.80 | yes | 2/3 | **yes** |
| 08 | safety_number | high | 1.00 | **no** | 3/3 | no |
| 09 | verify_bundle | high | 1.00 | **no** | 3/3 | no |

Note on the gate: R4 "confidence" is the deterministic claim-tally convergence
rate, not a judge's prose sentiment. A DEFER means at least one claim's panel
vote split — each such split claim is named in the table above (the R3 runs
could not produce this granularity because the judge prose was the verdict).

### Did auditing improve or regress between rounds?

**Improved — measurably, on four axes:** (1) *Verdict stability:* three of nine
functions reproduce their round-3 verdict exactly under a different judge and a
deterministic tally replacing judge prose — evidence the signal comes from the
panel, not the summarizer. (2) *Honesty:* R3 reported judge-sentiment
"high/0.85" on functions whose panels actually split; R4 defers those and names
the split claim. Lower headline pass-rate, higher information content.
(3) *Shortfall semantics:* the CEO-reported bug (2/3 responders mislabeled as
disagreement, confidence 0.0) is fixed — runs 02 and 07 correctly report
`panel_shortfall: true` with responder agreement preserved. (4) *Cost/latency:*
$0.00 again, with parallel panel fan-out (~3x) and bounded 429 backoff
(retry-after capped at 6s). **No regression observed.** The one soft spot:
judge gemma was parsed on all 9 runs (1.00) versus north-mini's 0.73 R3 rate,
and defers are now single-claim clarifications rather than inconclusive blobs —
the intended trajectory.

---

## SCMessenger progression across audit rounds (2026-09-02 → 2026-09-05)

Built entirely from stored evidence: `_runs/` JSONs (47 historical runs +
9 v4 runs), the round-1/2/3 report sections above, and `CTO_REVIEW.md`'s
call-site-verified severity table. No live audit was re-run for this section.
**SCMessenger source fact:** the audited files (`core/src/{crypto,identity,
message,privacy}`) have had **zero commits since 2026-08-25** (last touch
`13d30fd2`) — every verdict change between rounds is a change in *audit
measurement*, not in the code under audit.

### Verdict trajectory per finding (R1 sentiment → analyst-verified → R4 deterministic)

| # | Finding | R1 severity | Post-reachability (analyst) | R4 panel verdict |
|---|---|---|---|---|
| 1 | `verify_bundle` ML-DSA downgrade | CRITICAL | MEDIUM (reframed — verification unwired, 0 prod callers) | **defect real 3/3 unanimous (0.997)** — code-level claim confirmed; severity stays MEDIUM per unwired call graph |
| 2 | `safety_number` biased fingerprint | HIGH | MEDIUM (live in apps) | **defect real 3/3 unanimous (0.99)** — modulo bias + dropped hash tail reproduce exactly |
| 3 | `Ratchet::decrypt` gap/cache DoS | HIGH | LOW (retracted — caps exist, sender-only) | **⚠ discrepancy:** R4 panel votes c1/c2 "real" 3/3, directly contradicting the analyst retraction. Needs one clarification pass (claim text vs `MAX_SKIP_KEYS` constant) before either verdict is trusted |
| 4 | `decode_wire` unbounded bincode | HIGH | **HIGH (stands, pre-auth reachable)** | **real 3/3 unanimous (0.917)** — the single highest-priority open item, confirmed across every round |
| 5 | `decrypt_v2` PQ-stripping window | HIGH | LOW (one-message window, signature-bound, unwired) | real 2/2 (0.675) — code claim holds, severity consensus LOW |
| 6 | `Ratchet::encrypt` underflow | MED-HIGH | LOW (retracted — index always ≥ 1) | panel splits c1 2R/1NR — consistent with retraction; no new evidence |
| 7 | `negotiate_suite` downgrade/0xFF | MEDIUM | LOW (latent, hybrid unwired) | c1/c4/c5 real 3/3 — code claims confirmed, latency-mitigated |
| 8 | `peel_layer` destination oracle | MEDIUM | **MEDIUM (stands, pre-auth relay ingress)** | c1 real 2/2 (0.98) — open, confirmed |
| 9 | `construct_onion` | CLEAR | CLEAR | **5/5 not_real, identical across rounds** — the audit reproduces a clean bill under three different judge configurations |

### Convergence / deferral / shortfall trends

- **R1:** judge-sentiment only; 4 of 9 functions `agree=unknown` (judge returned
  nothing parseable) and 3 defers. The "unknown" outcomes were *shortfalls
  mislabeled as absence of opinion* — the CEO handoff bug in its raw form.
- **R2–R3 (rotation + consensus rounds):** participation improved
  (`unknown` → 0 across all converged runs), but judge prose remained the
  verdict; confidence values (0.85–0.97) did not distinguish panel unanimity
  from judge verbosity.
- **R4:** deterministic claim tally. Shortfall is now an explicit, separate
  field (runs 02, 07: `panel_shortfall=true`, 2/3) and every defer names the
  exact split claim. First round where a 3/3 vote means 3/3.

### Fixed vs still open (as of 2026-09-05)

**Fixed/resolved in the audit record (not in SCMessenger code — none of the
audited files have changed since 2026-08-25):** #6 underflow retracted;
#3 gap-DoS downgraded LOW (but see the R4 discrepancy above); #5 PQ-window
downgraded LOW; #1 reframed to MEDIUM/unwired; #7 latent.
**Still open, confirmed live by R4:** #4 bincode DoS (HIGH, pre-auth — the
priority fix), #8 peel_layer destination oracle (MEDIUM), #2 safety_number
entropy defects (MEDIUM, user-facing). **Decision needed:** #1/#5/#7 bundle-
and-hybrid policy before any wiring work ships the downgrade/freshness gaps.

### Explicit verdict: did SCMessenger's verified posture improve?

**Mixed, trending improved — with one honest caveat.**
- *Improved:* every severity call now rests on unanimous, claim-level,
  reproducible panel evidence instead of single-judge prose; the top open
  item (#4) is confirmed by 3/3 across rounds; one function (#9) demonstrates
  the audit clears clean code consistently. Confidence is now earned, not
  asserted.
- *Regressed (measurement, not code):* R4 surfaced a genuine **contradiction
  with the analyst retraction on #3** — the panel votes the gap/cache claims
  real while the reachability pass retracted them. Until that single claim is
  reconciled (constant-level check against `MAX_SKIP_KEYS` in the live file),
  #3's standing severity is *disputed*, not LOW.
- *Unchanged (the code itself):* SCMessenger's audited surface is byte-stable
  since 2026-08-25; its true security posture moves only when engineering acts
  on #4, #8, and #2. The audit's job — making those impossible to ignore — is
  measurably better at it each round.
