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
| 8 | `safety_number` | `core/src/identity/keys.rs` | high | 0.95 | Modulo bias + low entropy |
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

## Prioritized action list (proposed, unverified)

1. **`verify_bundle`** — enforce PQ verification by version/policy, not by presence of untrusted ML-DSA fields; sign `supported_suites` in all versions. *(most severe, high agreement)*
2. **`safety_number`** — fix modulo bias + cyclic byte reuse before shipping to users.
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