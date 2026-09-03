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

### 3. HIGH (revised → **LOW**) — `Ratchet::decrypt`: skipped-key growth + no DH-point validation
`core/src/crypto/ratchet.rs` — panel 5/5 unanimous, **two claims since retracted by call-site verification — see Reachability §1**.
- ~~No upper bound on the message-number gap~~ **RETRACTED** — gap is capped at `MAX_SKIP_KEYS = 256` (ratchet.rs:36, bail at :1108); the prompt asserted the unboundedness without ever showing the panel the capped `get_message_key`.
- ~~`skipped_keys` cache grows without a cap~~ **RETRACTED** — capped at 256 with eviction (ratchet.rs:1118).
- `X25519PublicKey::from(raw bytes)` does not reject all-zero/low-order DH points — **real but sender-authenticated** (their_dh is inside the signed envelope); keep as hardening, severity LOW.
- **Fix (revised):** validate the DH point; cap receiving-side DH-ratchet steps (see Reachability §1). The clone-and-commit trial pattern was confirmed correct — no state loss.

### 4. HIGH — `decode_wire_signed_envelope`: unbounded bincode on untrusted input
`core/src/message/codec.rs` — panel 5/5 unanimous. **[confirmed by analyst]**
- `bincode::deserialize` on attacker-controlled bytes with no size/allocation limit → length-prefixed fields can force large allocations (DoS).
- A V2-tagged buffer whose V2 parse fails is retried as **V1 on the whole buffer including the tag** — format-confusion risk.
- **Fix:** deserialize with a hard size limit (`bincode::options()`); remove the V2→V1 fallthrough or re-tag explicitly.

### 5. HIGH (revised → **LOW**) — `decrypt_message_ratcheted_v2`: PQ anti-stripping gated behind confirmation
`core/src/crypto/encrypt.rs` — panel 5/5 unanimous, **severity revised down by call-site verification — see Reachability §2**.
- `validate_pq_fields_present` runs only while `peer_confirmed` — true as code structure, but the pre-confirmation window is one message and every field is signature-bound; a network attacker cannot strip anything. Severity LOW.
- PQ fields are processed **before the AEAD/auth step** (mutates session state pre-auth) — real ordering smell; contained by the trial-adoption pattern. Severity LOW (hardening).
- Transcript hash is verified only pre-confirmation — and silently skipped when either side carries no hash. Sender-authenticated only. Severity LOW.
- **Fix (revised):** move PQ-field processing after AEAD success; don't skip the transcript check when a hash is expected; add low-order point validation. Replay is handled downstream by `session.decrypt` — not a defect here.

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
2. **Unbounded bincode parsers** (`decode_wire_signed_envelope`, `peel_layer`) — attacker-reachable DoS on raw network input **before** signature verification. (Severity unchanged by reachability analysis.)
3. **`safety_number`** — user-facing; shipped fingerprints are weaker than they appear.
4. **`Ratchet::encrypt` underflow** — real, cheap fix (`checked_sub`); low-order point validation on `Ratchet::decrypt` as hardening.
5. **`Ratchet::decrypt` gap/cache growth and `decrypt_message_ratcheted_v2` PQ-stripping** — revised to LOW by call-site verification (caps exist; single signature-authenticated ingress). Hardening, not live holes — see Reachability section.
6. **`negotiate_suite`** — decide the downgrade policy explicitly (interop fallback is intentional; the delimiter collision is not).

## How this was produced

- `harness verify --converge` on the free tier: 2-model panel (gemma-4-31b, minimax-m3 — the free models that reliably emit JSON) + judge (north-mini-code), per-claim structured verdicts, deterministic unanimity tally.
- Every finding = **unanimous panel agreement** (5/5 or 6/6 = 100%), with per-claim mean confidence 0.9–1.0.

---

## Reachability (call-site verification) — read-only, no SCMessenger files touched

This section answers, from the real call graph, whether findings **#3 (`Ratchet::decrypt`)** and **#5 (`decrypt_message_ratcheted_v2`)** are reachable in the live flow as written. Both are reached through a **single, signature-authenticated ingress**, which materially lowers their exposure.

### The only live path into ratchet decrypt

- `iron_core.rs:3383` `receive_message` is the sole network ingress for ratcheted traffic.
- It **verifies the sender's Ed25519 signature before any decrypt**: `verify_envelope` (V1) at `iron_core.rs:3413`, `verify_envelope_v2` (V2) at `iron_core.rs:3420`, then `decrypt_with_ratchet_fallback` at `iron_core.rs:3483`.
- The V1/V2 ratchet decrypts are called from **nowhere else** in `core/src` (only the fallback and unit tests).

### What the signature covers (decisive for both findings)

- V1: `sign_envelope` signs `bincode(Envelope)` — every field, including `ratchet_dh_public` / `ratchet_message_number` (`encrypt.rs:858`–`874`).
- V2: `sign_envelope_v2` signs `0x02 || bincode(EnvelopeV2)` — the version tag **plus** the ratchet header, PQ KEM fields, and transcript hash (`encrypt.rs:923`–`949`).
- **Consequence:** a network attacker (MITM, relay, eavesdropper) cannot substitute, strip, or re-version any field findings #3 and #5 attack. Every such field is bound to the authenticated sender.

### §1 — Finding #3 (`Ratchet::decrypt`): severity does **not** stand as written → LOW

- **Two of the three claims are retracted.** The panel was told "there is no upper bound on the message-number gap" and "`skipped_keys` has no size cap" — both are false. The caps live in `get_message_key`, which the 04b prompt never quoted (it only paraphrased it): `MAX_SKIP_KEYS = 256` (`ratchet.rs:36`) with a clean `bail!` when the gap exceeds it (`ratchet.rs:1108`), and eviction of the oldest entry when the cache exceeds 256 (`ratchet.rs:1118`). The sending side is also capped at `MAX_RATCHET_STEPS = 10_000` (`ratchet.rs:39`, checked at :792). **5/5 unanimity on a false premise carries no evidentiary weight** — this is the same prompt-assertion class we already caught in 06b (paraphrase) and 08 (claim_2 misstatement).
- **Reachability:** only the authenticated sender can drive the gap — `their_dh` and `message_number` are inside the signed envelope. A network attacker cannot reach this path at all.
- **What survives (LOW):** receiving-side DH-ratchet steps in `handle_dh_ratchet_trial` increment `dh_step_count` with **no** `MAX_RATCHET_STEPS` check (`ratchet.rs:968`), while `encrypt` has one — an asymmetry (info-level). A malicious *sender* can also flood the 256-entry cache and evict a legitimate peer's out-of-order keys (availability) — but such a sender already has simpler DoS options, so this adds no power. The low-order/zero DH-point gap is real missing validation, but `their_dh` is signature-bound, so only the authenticated sender can supply it; keep it as a hardening item.

### §2 — Finding #5 (`decrypt_message_ratcheted_v2`): severity does **not** stand as written → LOW

- **The pre-confirmation window is one message.** `peer_confirmed` flips true on the first successful decrypt (`encrypt.rs:339`; also the ratchet success paths at `ratchet.rs:886` and :1006).
- **Nothing can be stripped in that window.** The signature binds every PQ field and the transcript hash (`encrypt.rs:923`–`949`), and PQ bootstrap material isn't carried on this path at all — the code comment at `encrypt.rs:307`–`311` states the bootstrap ciphertext is consumed at session setup (`init_as_receiver_hybrid*`), not here. "Stripping" would require the authenticated *sender* to omit its own PQ steps — and a malicious sender already controls the session, gaining no confidentiality it didn't have.
- **Transcript check gap (LOW):** the pre-confirmation hash comparison (`encrypt.rs:295`–`303`) is silently skipped when either side carries no hash (`if let (Some, Some)`). This only matters post-sender-restart (the fallback path from `encrypt.rs:663`), and even then the sender is authenticated — defense-in-depth, not an exposure.
- **Mutation-before-AEAD (LOW):** `handle_incoming_pq_fields` runs before `session.decrypt` (`encrypt.rs:326`–`336` vs ~:349), so a message that later fails AEAD can still have mutated session state. The trial-adoption pattern (`ratchet.rs:870`–`1019`) tolerates a poisoned pending secret by trying every candidate, so impact is contained. Worth fixing as ordering hygiene, not an active hole.

### Not affected by this section

- **#4 (`decode_wire_signed_envelope`) and #8 (`peel_layer`) DoS claims stand as written**: bincode decode happens on the raw wire bytes *before* signature verification (`iron_core.rs:3413`/`3420`), so the unbounded-parse surface is reachable by any unauthenticated peer.

### Bottom line for engineering

| Finding | As written | After call-site verification |
|---|---|---|
| #3 `Ratchet::decrypt` gap/cache DoS | HIGH | **LOW** — caps exist (256); claims retracted; sender-only reachability |
| #3 low-order DH point | HIGH | **LOW** — real, but sender-authenticated; harden with validation |
| #5 PQ-stripping window | HIGH | **LOW** — one-message window, fully signature-bound |
| #5 PQ-fields-before-AEAD | HIGH | **LOW** — ordering hygiene, contained by trial pattern |
| #4 / #8 unbounded bincode | HIGH / MED | **UNCHANGED** — reachable pre-auth on raw wire input |

Review-order impact: the two findings to prioritize fastest remain **#1 `verify_bundle` (critical)** and the **unauthenticated decode DoS (#4/#8)**. #3 and #5 can drop behind those — they are hardening items, not live holes.
- Full per-claim data and the earlier working notes: `audits/scmessenger/audit_report.md` and `audits/scmessenger/_runs/` (gitignored raw JSON).
