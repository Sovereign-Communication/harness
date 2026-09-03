# CTO Review — SCMessenger Core Security Audit

**For:** CTO review
**Date:** 2026-09-02
**Scope:** 9 security/correctness-critical functions across the SCMessenger Rust core (crypto, ratchet, message wire format, onion routing, identity).
**Method:** Strict hands-off, read-only audit. Each function was reviewed by two independent free-tier models (panel) whose per-claim verdicts were tallied deterministically; **every finding below carries a unanimous panel verdict (5/5 or 6/6 = 100%)**. Total cost: $0.00.
**Caveat (please read):** This is AI-assisted triage at very high panel agreement, not a substitute for human verification against the live code paths. Each item names the exact behavior; confirm impact in the calling context before fixing. Items marked **[confirmed by analyst]** were additionally spot-checked against the source.

---

## Executive summary

**1 critical, 5 high, 3 medium-severity findings, 1 function cleared.** The highest-priority item is a **cryptographic downgrade in `verify_bundle`**: post-quantum (ML-DSA) authentication is enforced only when the *untrusted peer's bundle says it has ML-DSA fields* — an attacker can strip those fields and force the weaker legacy Ed25519-only path. Second is **`safety_number`**, the user-facing fingerprint: its digit derivation has modulo bias and silently discards 8 of 32 hash bytes, so the number carries materially less entropy than 60 displayed digits would suggest. Several **parsers accept unbounded bincode deserialization of untrusted wire data** (resource-exhaustion DoS) and one double-ratchet path has **no cap on skipped-key growth**.

No finding was acted on — this was a read-only audit. Fix recommendations are one-liners for your engineering team to validate.

---

## Findings, severity-ranked

### 1. CRITICAL — `verify_bundle`: ML-DSA downgrade by field stripping
`core/src/identity/keys.rs` — panel 5/5 unanimous.
- Dual Ed25519+ML-DSA verification is required only when *both* `mldsa_public` and `mldsa_signature` are present **in the untrusted bundle**. Strip both fields → legacy Ed25519-only path. The post-quantum authenticity guarantee is silently dropped.
- **Related (medium):** the legacy v1 signature input **omits `supported_suites`** (v2/v3 include it), so on a v1 bundle the suites list can be altered without invalidating the signature. **[confirmed by analyst]**
- **Related (medium):** `created_at` is authenticated but no freshness bound — an old bundle can be replayed.
- **Fix:** require ML-DSA by *version/policy*, never by presence of fields in the untrusted bundle; sign `supported_suites` in every version; add a freshness window.

### 2. HIGH — `safety_number`: biased, low-entropy fingerprint
`core/src/identity/keys.rs` — panel 6/6 unanimous.
- `val = u16 % 100000` over `0..=65535`: groups in `[65536, 99999]` never occur — heavy modulo bias. **[confirmed by analyst]**
- Only the **first 24 of 32 hash bytes** are read (`offset = (group*2) % 24`); hash bytes 24–31 are silently discarded — an independent entropy loss. **[confirmed by analyst]**
- Net: the 60 displayed digits carry materially less entropy than 60 uniform digits, weakening the visual MITM check.
- **Fix:** map wide, unbiased chunks of the full 32-byte hash to digits (e.g. base-10 over more bytes, `% 10000` on wider values) and use all hash bytes.

### 3. HIGH — `Ratchet::decrypt`: unbounded skipped-key growth + no DH-point validation
`core/src/crypto/ratchet.rs` — panel 5/5 unanimous.
- No upper bound on the message-number gap: a hostile/desynced peer can force huge skipped-key storage and excessive ratchet steps (memory/CPU DoS).
- `skipped_keys` cache grows without a cap.
- `X25519PublicKey::from(raw bytes)` does not reject all-zero/low-order DH points.
- **Fix:** cap the gap and bound/prune `skipped_keys`; validate the DH point. (The clone-and-commit trial pattern itself was confirmed correct — no state loss.)

### 4. HIGH — `decode_wire_signed_envelope`: unbounded bincode on untrusted input
`core/src/message/codec.rs` — panel 5/5 unanimous. **[confirmed by analyst]**
- `bincode::deserialize` on attacker-controlled bytes with no size/allocation limit → length-prefixed fields can force large allocations (DoS).
- A V2-tagged buffer whose V2 parse fails is retried as **V1 on the whole buffer including the tag** — format-confusion risk.
- **Fix:** deserialize with a hard size limit (`bincode::options()`); remove the V2→V1 fallthrough or re-tag explicitly.

### 5. HIGH — `decrypt_message_ratcheted_v2`: PQ anti-stripping gated behind confirmation
`core/src/crypto/encrypt.rs` — panel 5/5 unanimous.
- `validate_pq_fields_present` runs only while `peer_confirmed` — a PQ-stripped envelope arriving *before* confirmation is not detected.
- PQ fields are processed **before the AEAD/auth step**, so a forged message can mutate session state pre-authentication.
- Transcript hash is verified only pre-confirmation.
- **Fix:** apply stripping validation unconditionally for hybrid sessions; process PQ fields only after AEAD success. (Replay is handled downstream by `session.decrypt` — not a defect here.)

### 6. MEDIUM-HIGH — `Ratchet::encrypt`: message-number underflow
`core/src/crypto/ratchet.rs` — panel 5/5 unanimous.
- `message_number = chain.index - 1` underflows (`u32`) if `index` is 0 on the first encrypt (panic in debug, wrap in release).
- Derived `message_key` is not zeroized after use.
- Random per-message nonces confirmed acceptable — not a defect.
- **Fix:** `checked_sub` or initialize the index at 1; zeroize the message key.

### 7. MEDIUM — `negotiate_suite`: transcript delimiter collision + downgrade-by-weakest
`core/src/crypto/negotiation.rs` — panel 5/5 unanimous.
- Transcript is `our_suites || 0xFF || their_suites || 0xFF || suite || pubs`; suite IDs are `u8` and the test suite itself uses `0xFF` — a suite list containing `0xFF` collides with the delimiter → ambiguous (non-injective) transcripts. **[confirmed by analyst]**
- `max()` of the intersection lets the weaker/older peer dictate the suite (advertise only `0x01` → force `0x01` even when both support `0x03`). The code's own tests want this fallback for interop — it is a policy decision, but the low side currently holds veto.
- **Fix:** length-prefix the fields; decide an explicit downgrade policy with authenticated suite lists.

### 8. MEDIUM — `peel_layer`: destination detected by ciphertext length, no authenticated final-hop marker
`core/src/privacy/onion.rs` — panel 5/5 unanimous.
- `encrypted_routing_info.is_empty()` (ciphertext *length*) decides "I am the destination" — no authenticated final-hop marker.
- Decrypted routing info is parsed with unbounded bincode (DoS).
- Payload AEAD is not bound to the routing info.
- **Impact capped at medium:** an off-path attacker **cannot** read the payload via this path — it stays AEAD-authenticated under the relay's key.
- **Fix:** use an authenticated sentinel for the final hop; bound the bincode parse.

### 9. REVIEWED — CLEAR — `construct_onion`
`core/src/privacy/onion.rs` — panel 5/5 unanimous.
- Audited to the same bar: **no nonce reuse** (each layer derives its own shared secret), counter width sufficient, no KDF-separation leak, no layer-boundary peel, ephemerals fresh per layer. **No defect found.** No action required.

---

## Suggested review order

1. **`verify_bundle` downgrade (critical)** — impacts the authenticity guarantee of every PQ-hybrid session established from an untrusted bundle. Gate or disable until fixed.
2. **`safety_number`** — user-facing; shipped fingerprints are weaker than they appear.
3. **Unbounded bincode parsers** (`decode_wire_signed_envelope`, `peel_layer`) — attacker-reachable DoS on network input.
4. **`Ratchet::decrypt` skipped-key growth / low-order points** and **`Ratchet::encrypt` underflow**.
5. **`decrypt_message_ratcheted_v2` PQ-stripping scope** — confirm no pre-confirmation PQ-stripping window is reachable in the live session flow.
6. **`negotiate_suite`** — decide the downgrade policy explicitly (interop fallback is intentional; the delimiter collision is not).

## How this was produced

- `harness verify --converge` on the free tier: 2-model panel (gemma-4-31b, minimax-m3 — the free models that reliably emit JSON) + judge (north-mini-code), per-claim structured verdicts, deterministic unanimity tally.
- Every finding = **unanimous panel agreement** (5/5 or 6/6 = 100%), with per-claim mean confidence 0.9–1.0.
- Full per-claim data and the earlier working notes: `audits/scmessenger/audit_report.md` and `audits/scmessenger/_runs/` (gitignored raw JSON).
