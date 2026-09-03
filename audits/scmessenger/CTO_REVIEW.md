# CTO Review — SCMessenger Core Security Audit

**For:** CTO review
**Date:** 2026-09-02
**Scope:** 9 security/correctness-critical functions across the SCMessenger Rust core (crypto, ratchet, message wire format, onion routing, identity).
**Method:** Strict hands-off, read-only audit. Each function was reviewed by two independent free-tier models (panel) whose per-claim verdicts were tallied deterministically; **every finding below carries a unanimous panel verdict (5/5 or 6/6 = 100%)**. Total cost: $0.00.
**Caveat (please read):** This is AI-assisted triage at very high panel agreement, not a substitute for human verification against the live code paths. Each item names the exact behavior; confirm impact in the calling context before fixing. Items marked **[confirmed by analyst]** were additionally spot-checked against the source.

---

## Executive summary

**1 critical, 5 high, 3 medium-severity findings, 1 function cleared.** The highest-priority item is a **cryptographic downgrade in `verify_bundle`**: post-quantum (ML-DSA) authentication is enforced only when the *untrusted peer's bundle says it has ML-DSA fields* — an attacker can strip those fields and force the weaker legacy Ed25519-only path. Second is **`safety_number`**, the user-facing fingerprint: its digit derivation has modulo bias and silently discards 8 of 32 hash bytes, so the number carries materially less entropy than 60 displayed digits would suggest. Several **parsers accept unbounded bincode deserialization of untrusted wire data** (resource-exhaustion DoS) and one double-ratchet path has **no cap on skipped-key growth**.

**Reachability revision (analyst pass 2, see §3):** the critical `verify_bundle` downgrade and the `negotiate_suite`/PQ-hybrid findings are **not live-network-exploitable as written** — the bundle-verification and bundle-storage paths have **zero production call sites** in this workspace (hybrid sessions are unreachable in the current wiring), and `Ratchet::encrypt`'s underflow is **retracted** (`next_message_key` always increments before the subtract). Live, pre-auth issues remain the **bincode decode DoS** (#4/#8) and the **`safety_number`** display defects. Net standing severity: **0 critical, 2 high-ish DoS (pre-auth), 3 medium, 4 low/hardening, 1 cleared** — details and file:line evidence in the Reachability section.

No finding was acted on — this was a read-only audit. Fix recommendations are one-liners for your engineering team to validate.

---

## Findings, severity-ranked

### 1. CRITICAL (revised → **MEDIUM, reframed**) — `verify_bundle`: ML-DSA downgrade by field stripping
`core/src/identity/keys.rs` — panel 5/5 unanimous, **reframed by call-site verification — see Reachability §3A**.
- Dual Ed25519+ML-DSA verification is required only when *both* `mldsa_public` and `mldsa_signature` are present **in the untrusted bundle** — true as code structure.
- **But: not live-exploitable as written.** (a) `verify_bundle` has **zero production call sites** in this workspace (only unit/integration tests) — verification is never invoked on any live path. (b) Even if it ran, stripping ML-DSA fields from a **current v3-signed bundle fails verification**: the legacy fallback checks v2-then-v1 domain separators (keys.rs:505–546), and a v3 signature verifies under neither → the stripped bundle is rejected. The v1 fallback (which omits `supported_suites`) only accepts legacy v1-format bundles.
- **Related (real):** the legacy v1 signature input **omits `supported_suites`** — true at keys.rs:528–536, but only exploitable against legacy v1-format bundles.
- **Related (real):** `created_at` is signed but **never validated** — no freshness check anywhere.
- **The actual finding:** bundle ingestion + verification is **unwired** — no production code calls `save_contact_bundle` or `verify_bundle` (and the mobile FFI exposes neither), so `get_contact_bundle` always returns `None` and the PQ-hybrid path is unreachable. When the hybrid path is wired (the obvious direction), verification must be enforced at ingestion with a freshness check.
- **Fix (revised):** call `verify_bundle` at the (to-be-wired) ingestion point; require ML-DSA by *version/policy*, never by field presence; drop or gate the v1 fallback; validate `created_at` against a freshness window.

### 2. HIGH (revised → **MEDIUM**) — `safety_number`: biased, non-uniform fingerprint
`core/src/identity/keys.rs` — panel 6/6 unanimous, math confirmed; **severity revised — see Reachability §3B**.
- `val = u16 % 100000` over `0..=65535`: the modulo is an **identity** (u16 never reaches 100000), so each group is uniform over `0..=65535` — displayed `%05d`, the leading digit is **never 7–9** and 6 appears only for 60000–65535. Non-uniform display, but not "modulo bias" in the strict sense.
- Only the **first 24 of 32 hash bytes** are read (`offset = (group*2) % 24`); hash bytes 24–31 are silently discarded. **[confirmed by analyst]**
- Net: the 60 displayed digits are 12 × 16 bits = **192 bits** of entropy (not the ~199 bits 60 uniform digits would imply) — still above Signal's ~100-bit (30-digit) benchmark, so a practical MITM-collision break is not realistic; the defect is real correctness/UX weakness in a user-facing trust indicator.
- **Fix:** map wide, unbiased chunks of the **full** 32-byte hash to digits (e.g. base-10 over more bytes) and use all hash bytes.

### 3. HIGH (revised → **LOW**) — `Ratchet::decrypt`: skipped-key growth + no DH-point validation
`core/src/crypto/ratchet.rs` — panel 5/5 unanimous, **two claims since retracted by call-site verification — see Reachability §1**.
- ~~No upper bound on the message-number gap~~ **RETRACTED** — gap is capped at `MAX_SKIP_KEYS = 256` (ratchet.rs:36, bail at :1108); the prompt asserted the unboundedness without ever showing the panel the capped `get_message_key`.
- ~~`skipped_keys` cache grows without a cap~~ **RETRACTED** — capped at 256 with eviction (ratchet.rs:1118).
- `X25519PublicKey::from(raw bytes)` does not reject all-zero/low-order DH points — **real but sender-authenticated** (their_dh is inside the signed envelope); keep as hardening, severity LOW.
- **Fix (revised):** validate the DH point; cap receiving-side DH-ratchet steps (see Reachability §1). The clone-and-commit trial pattern was confirmed correct — no state loss.

### 4. HIGH — `decode_wire_signed_envelope`: unbounded bincode on untrusted input
`core/src/message/codec.rs` — panel 5/5 unanimous, **DoS claim stands; format-confusion sub-claim refined — see Reachability §3C**.
- `bincode::deserialize` with **default options** (no `options().with_limit(...)` anywhere in the codec) on attacker-controlled bytes, **before** signature verification (iron_core.rs:3402 → verify :3413/:3420) → length-prefixed fields can request allocations far exceeding the input (amplified panic/abort from a ≤256 KB packet). Input is capped at `MAX_MESSAGE_SIZE = 256 KB` (codec.rs:243–245) — bounded, but still attacker-reachable and pre-auth. **[confirmed by analyst]**
- A V2-tagged buffer whose V2 parse fails is retried as **V1 on the whole buffer including the tag** — real, but **partly load-bearing**: a legitimate V1 serialization whose first byte is `0x02` (== `WIRE_TAG_V2`) must fall through to V1, and any accepted interpretation is signature-bound. Confusion risk is sender-chosen, LOW.
- **Fix:** deserialize with a hard size limit (`bincode::options().with_limit(...)`); keep the fallthrough but re-tag explicitly so a V1 envelope starting with `0x02` cannot be double-interpreted.

### 5. HIGH (revised → **LOW**) — `decrypt_message_ratcheted_v2`: PQ anti-stripping gated behind confirmation
`core/src/crypto/encrypt.rs` — panel 5/5 unanimous, **severity revised down by call-site verification — see Reachability §2**.
- `validate_pq_fields_present` runs only while `peer_confirmed` — true as code structure, but the pre-confirmation window is one message and every field is signature-bound; a network attacker cannot strip anything. Severity LOW.
- PQ fields are processed **before the AEAD/auth step** (mutates session state pre-auth) — real ordering smell; contained by the trial-adoption pattern. Severity LOW (hardening).
- Transcript hash is verified only pre-confirmation — and silently skipped when either side carries no hash. Sender-authenticated only. Severity LOW.
- **Fix (revised):** move PQ-field processing after AEAD success; don't skip the transcript check when a hash is expected; add low-order point validation. Replay is handled downstream by `session.decrypt` — not a defect here.

### 6. MEDIUM-HIGH (revised → **LOW**) — `Ratchet::encrypt`: message-number "underflow"
`core/src/crypto/ratchet.rs` — panel 5/5 unanimous, **underflow claim retracted — see Reachability §3D**.
- ~~`message_number = chain.index - 1` underflows if `index` is 0 on the first encrypt~~ **RETRACTED** — `Chain::new` starts `index: 0` but `next_message_key` increments **before** `encrypt` subtracts (ratchet.rs:133–139, 793–797): first call → index 1 → message_number 0. No reachable path leaves `index == 0` at the subtract.
- Derived `message_key` is not zeroized after use — **real, LOW** (RatchetKey has no Drop/zeroize; the chain key itself must persist in memory regardless).
- Random per-message nonces confirmed acceptable — not a defect.
- **Fix (revised):** zeroize derived keys; the `checked_sub` hardening is optional (defense-in-depth only).

### 7. MEDIUM (revised → **LOW, latent**) — `negotiate_suite`: transcript delimiter collision + downgrade-by-weakest
`core/src/crypto/negotiation.rs` — panel 5/5 unanimous, **not reachable in current production wiring — see Reachability §3E**.
- Transcript is `our_suites || 0xFF || their_suites || 0xFF || suite || pubs`; suite IDs are `u8` — a list containing `0xFF` would collide with the delimiter. **But no bundle this codebase produces can contain `0xFF`**: `sign_bundle` advertises only `[0x01, 0x03]` (keys.rs:418); `0xFF`/`0xFE` appear only in a unit test (negotiation.rs:98). Latent robustness bug only.
- `max()` of the intersection lets the weaker/older peer dictate the suite — **intentional, documented interop policy** (keys.rs:395–418 comment + dedicated fallback tests). Attack-relevant only via bundle tampering, which is gated on the unwired `verify_bundle` (§3A).
- Unknown-suite dispatch: a suite outside `0x01/0x02/0x03` silently falls to the classical `else` branch (session_manager.rs:141–149) instead of erroring — real robustness gap, bundle-tampering-only.
- **Fix (revised):** length-prefix the transcript fields; error on unknown suites; decide the downgrade policy explicitly (the interop fallback is deliberate — the delimiter collision and silent-classical dispatch are not).

### 8. MEDIUM — `peel_layer`: destination detected by ciphertext length, no authenticated final-hop marker
`core/src/privacy/onion.rs` — panel 5/5 unanimous, **severity confirmed MEDIUM — see Reachability §3F**.
- `encrypted_routing_info.is_empty()` (ciphertext *length*) decides "I am the destination" (onion.rs:453, 479–482) — no authenticated final-hop marker. The hybrid branch *does* carry an explicit `is_destination` flag; the classical branch relies on emptiness.
- Decrypted routing info is parsed with unbounded bincode (onion.rs:487+) — default options, input-bounded, same declared-length nuance as #4.
- Payload AEAD is not bound to the routing info.
- **Reachable:** the relay peel path is a network ingress (`peel_onion_layer` exposed via mobile bridge :1836 and wasm RPC) — any peer can send onion bytes; impact stays **capped at medium**: an off-path attacker **cannot** read the payload (AEAD under the relay's key), and destination-confusion only makes the relay self-address envelopes it can already decrypt.
- **Fix:** use an authenticated sentinel for the final hop; bind the payload AEAD to the routing info; bound the bincode parse.

### 9. REVIEWED — CLEAR — `construct_onion`
`core/src/privacy/onion.rs` — panel 5/5 unanimous.
- Audited to the same bar: **no nonce reuse** (each layer derives its own shared secret), counter width sufficient, no KDF-separation leak, no layer-boundary peel, ephemerals fresh per layer. **No defect found.** No action required.

---

## Suggested review order

1. **Unbounded bincode parsers** (`decode_wire_signed_envelope` #4, `peel_layer` #8) — the only **pre-auth, network-reachable** DoS surface (decode happens before signature verification; default bincode options allow declared-length allocation amplification inside the 256 KB input gate). Fix first with `bincode::options().with_limit(...)`.
2. **Bundle/hybrid subsystem wiring** (`verify_bundle` #1, `negotiate_suite` #7, PQ-hybrid parts of #3/#5) — not live-exploitable today because `verify_bundle`/`save_contact_bundle` have **zero production call sites**, but the moment the hybrid path is wired, the downgrade-by-field-presence, the suites-unsigned v1 fallback, the silent-classical dispatch, and the missing freshness check all become live. Decide policy now: enforce `verify_bundle` at ingestion, require ML-DSA by policy not presence, add freshness.
3. **`safety_number` (#2)** — user-facing; live in iOS/Android screens today. Display non-uniformity and the dropped 8-byte hash tail are real; fix by mapping the full 32-byte hash. Not an urgent security break (192 bits ≥ Signal's ~100-bit benchmark).
4. **Hardening items (LOW):** low-order DH-point validation + receiving-side step cap (#3); key zeroization (#6); PQ-fields-after-AEAD ordering + unconditional stripping policy for hybrid sessions (#5); transcript-hash check when expected (#5); classical onion final-hop sentinel + payload-routing binding (#8).
5. **`Ratchet::encrypt` underflow (#6)** — retracted; no action beyond optional `checked_sub` defense-in-depth.

## How this was produced

- `harness verify --converge` on the free tier: 2-model panel (gemma-4-31b, minimax-m3 — the free models that reliably emit JSON) + judge (north-mini-code), per-claim structured verdicts, deterministic unanimity tally.
- Every finding = **unanimous panel agreement** (5/5 or 6/6 = 100%), with per-claim mean confidence 0.9–1.0.
- **Severity revisions and retractions in this document are ANALYST output (passes 1–2), not panel output** — they came from tracing the real call graph, constants, signature boundaries, and entry points in the SCMessenger source. Each revision cites file:line so engineering can re-check.

---

## Reachability (call-site verification) — read-only, no SCMessenger files touched

This section answers, from the real call graph, whether each panel finding is reachable in the live flow as written and whether its severity stands. §1–§2 cover #3 (`Ratchet::decrypt`) and #5 (`decrypt_message_ratcheted_v2`); §3 covers the remaining panel findings. All revisions are analyst verification against the source, not panel output.

### The only live path into ratchet decrypt

- `iron_core.rs:3383` `receive_message` is the sole network ingress for ratcheted traffic.
- It **verifies the sender's Ed25519 signature before any decrypt**: `verify_envelope` (V1) at `iron_core.rs:3413`, `verify_envelope_v2` (V2) at `iron_core.rs:3420`, then `decrypt_with_ratchet_fallback` at `iron_core.rs:3483`.
- The V1/V2 ratchet decrypts are called from **nowhere else** in `core/src` (only the fallback and unit tests).

### What the signature covers (decisive for several findings)

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

### §3 — Analyst pass 2: the remaining six panel findings

**The single most important structural fact this pass established:** the entire **bundle / PQ-hybrid subsystem is unwired in production**. `save_contact_bundle` has **zero production call sites** in the whole workspace (only `core/tests/integration_e00_ratchet_wiring.rs`), the mobile FFI (`mobile_bridge.rs`) exposes **no** bundle or `verify_bundle` API, and `verify_bundle` itself has zero production callers. Therefore `get_contact_bundle` (`iron_core.rs:977`, `:3477`) always returns `None`, the send path falls back to **classical V1** (`encrypt.rs:565`), and an incoming V2/hybrid envelope **bails** at hybrid init (`encrypt.rs:659`, "missing keys/bundles"). Every finding that presupposes the hybrid/bundle path (#1, #7, and the PQ-hybrid parts of #3/#5) is **latent until that path is wired**.

**§3A — #1 `verify_bundle` (CRITICAL → MEDIUM, reframed).** The field-presence downgrade is real as code (`keys.rs:454` `has_mldsa` gates the dual check) but not live: verification never runs (no callers), and even if it ran, stripping ML-DSA fields from a **current v3 bundle fails** — the legacy fallback tries the v2 then v1 domain separators (`keys.rs:504`–`546`), and a v3 signature verifies under neither, so the stripped bundle is rejected. The v1 fallback (which does **not** bind `supported_suites`, `keys.rs:522`–`536`) only accepts legacy v1-format bundles. `created_at` is signed but never validated (no freshness anywhere). **Action:** wire `verify_bundle` at the ingestion point that will populate bundles; require ML-DSA by policy/version, not field presence; gate or drop the v1 fallback; add a freshness window.

**§3B — #2 `safety_number` (HIGH → MEDIUM).** Math confirmed against the live code (`keys.rs:542`–`580`): 12 groups read bytes `0..=23` of the 32-byte hash — **bytes 24–31 never read**; each group is a uniform u16 (`0..=65535`) so `% 100000` is an identity and the `%05d` output's leading digit is **never 7–9**. Function is live and user-facing: FFI export `mobile_bridge.rs:3706`, rendered by the iOS/Android contact and verify-safety-number screens. Effective entropy = 12 × 16 = **192 bits** — below the ~199 bits 60 uniform digits would imply, but still above Signal's ~100-bit (30-digit) benchmark, so this is a real correctness/UX weakness in a trust indicator, not a break of the MITM margin.

**§3C — #4 `decode_wire_signed_envelope` (HIGH stands, refined).** Decode-before-verify confirmed at `iron_core.rs:3409` → verify `:3413`/`:3420` — reachable by any unauthenticated peer. Input gate exists (`MAX_MESSAGE_SIZE = 256 KB`, `codec.rs:243`–`245`), but every `bincode::deserialize` uses **default options** (no `with_limit` anywhere in the codec), so a ≤256 KB packet with a huge length-prefix can attempt a far larger allocation (potential panic/abort) — the DoS stands, bounded-input. The V2→V1 fallthrough (`codec.rs:296`–`308`) is real but **partly load-bearing** (legitimate V1 envelopes whose first byte is `0x02` == `WIRE_TAG_V2` need it) and any accepted interpretation is signature-bound → confusion risk is sender-chosen, LOW. **Action:** `bincode::options().with_limit(...)`; make the fallthrough explicit.

**§3D — #6 `Ratchet::encrypt` underflow (MEDIUM-HIGH → LOW, retracted).** `Chain::new` starts `index: 0` (`ratchet.rs:111`) but `next_message_key` increments **before** `encrypt` computes `chain.index - 1` (`ratchet.rs:133`–`139`, subtract at `:797`): first call → index 1 → message number 0. **No reachable state leaves `index == 0` at the subtract** — the claim is retracted. The non-zeroized `message_key` is real but LOW (RatchetKey has no Drop/zeroize; the chain key must persist in memory anyway).

**§3E — #7 `negotiate_suite` (MEDIUM → LOW, latent).** Call sites are `session_manager.rs:115`/`:165`, both inside hybrid session creation — reachable only when both peers' bundles exist, i.e. **not in current production wiring** (§3 head). Delimiter collision needs a suite list containing `0xFF`, and no bundle this codebase produces can contain it (`sign_bundle` advertises `[0x01, 0x03]`, `keys.rs:418`; `0xFF`/`0xFE` appear only in a unit test, `negotiation.rs:98`). Downgrade-by-weakest is the **documented, intentional** interop fallback (`keys.rs:395`–`418`, dedicated tests). Real robustness gaps that survive: unknown suite values silently dispatch to the classical branch (`session_manager.rs:141`–`149`) and the transcript format is delimiter-ambiguous in principle — both only exploitable via the bundle tampering that the (unwired) `verify_bundle` is meant to stop.

**§3F — #8 `peel_layer` (MEDIUM stands).** Reachable: `peel_onion_layer` is a network-facing relay entry (`iron_core.rs:2645`, `mobile_bridge.rs:1836`, wasm RPC) that any peer can hit with onion bytes before any authentication (onion anonymity is by design). Confirmed in code: classical-branch destination is `encrypted_routing_info.is_empty()` (`onion.rs:453`, `479`–`482`) — no authenticated marker, unlike the hybrid branch's explicit `is_destination` flag; routing parse uses default-options bincode (`onion.rs:487`+). Medium cap holds: payload stays AEAD under the relay's derived key, so destination-confusion and empty-routing "oracles" let a sender make a relay self-address envelopes the sender itself encrypted — no off-path confidentiality break.

### Bottom line for engineering

| Finding | As written | After call-site verification (analyst) |
|---|---|---|
| #1 `verify_bundle` ML-DSA downgrade | CRITICAL | **MEDIUM (reframed)** — verification unwired (0 prod callers); v3 bundles can't be stripped (domain sep); v1 fallback + no freshness are the real residue |
| #2 `safety_number` | HIGH | **MEDIUM** — math confirmed & live in apps; 192 bits still ≥ Signal benchmark |
| #3 `Ratchet::decrypt` gap/cache DoS | HIGH | **LOW** — caps exist (256); claims retracted; sender-only |
| #3 low-order DH point | HIGH | **LOW** — real, sender-authenticated; hardening |
| #4 `decode_wire` unbounded bincode | HIGH | **HIGH (stands)** — pre-auth reachable; 256 KB gate; default bincode options |
| #4 V2→V1 fallthrough | HIGH | **LOW** — load-bearing for `0x02`-leading V1; signature-bound |
| #5 PQ-stripping window | HIGH | **LOW** — one-message window, fully signature-bound, path unwired |
| #5 PQ-fields-before-AEAD | HIGH | **LOW** — ordering hygiene; contained by trial pattern |
| #6 `Ratchet::encrypt` underflow | MED-HIGH | **LOW (retracted)** — index always ≥ 1 at subtract |
| #7 `negotiate_suite` | MEDIUM | **LOW (latent)** — hybrid path unwired; 0xFF unreachable; fallback intentional |
| #8 `peel_layer` | MEDIUM | **MEDIUM (stands)** — pre-auth relay ingress; no off-path break |

**Net for engineering:** the only live, pre-auth network issues are the **bincode decode DoS (#4/#8)** — fix those with bounded bincode first. Next, decide the **bundle/hybrid policy now** (#1/#7/#5) so the wiring work doesn't ship the downgrade, freshness, and silent-classical gaps as live bugs. `safety_number` (#2) is a quick correctness fix on a user-facing screen. Everything else is LOW hardening.
- Full per-claim data and the earlier working notes: `audits/scmessenger/audit_report.md` and `audits/scmessenger/_runs/` (gitignored raw JSON).
