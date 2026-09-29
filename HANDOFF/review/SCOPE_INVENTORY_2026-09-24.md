# Harness legacy handoff classification manifest

**Date:** 2026-09-24
**Snapshot:** all files present below the owner `HANDOFF/` directory at manifest generation time.
**Change boundary:** documentation only; no legacy file was edited, moved, deleted, or migrated.

<!-- HANDOFF-SCOPE-BEGIN -->
scope: Harness
owner: Sovereign-Communication/Harness
purpose: Harness-only findings and remediation handoff
foreign_material: NONE
boundary: No foreign-repository findings, evidence, status, or remediation are included.
<!-- HANDOFF-SCOPE-END -->

This owner-local manifest records 11 discovered handoff paths for Harness. It is an inventory, not a migration.

## Classification rule

1. Enumerate every regular file recursively below `HANDOFF/`.
2. Read UTF-8 bytes and inspect both the repository-relative path and document body for a foreign-product alias.
3. Classify `quarantined` when either path or body contains a foreign-product alias; preserve the original bytes and do not use the file as evidence.
4. Otherwise run the owner-local scope gate on the actual file. Classify `owner-valid` only on a pass; classify `blocked-by-metadata` on any gate failure or unreadable bytes.
5. Do not mass-migrate. A future migration requires owner approval of this manifest, a deterministic transformation rule, and a fresh gate run on the resulting bytes.

The exact raw path register is kept in the OC operations index. Foreign-named quarantined paths are represented here by stable IDs so this owner handoff does not import a foreign alias.

## Counts

- `owner-valid`: **4**
- `quarantined`: **2**
- `blocked-by-metadata`: **5**

## Path ledger

| ID / path | Classification | Reason | SHA-256 |
|---|---|---|---|
| `HANDOFF/BOD_STATE.md` | `blocked-by-metadata` | `gate-fail` | `d42fd5531175f6c668330a9cfd59c580e9df7d4a7f07cb83af9d1fab1c502cce` |
| `HANDOFF/CEO_STATE.md` | `blocked-by-metadata` | `gate-fail` | `7e2c0ce49f64fc7bd9e69b20277c8f94e6a6c0c6e0520851029aad8a1ca24029` |
| `HAR-Q-9666d1a34721` (path hash `9666d1a34721df7b8675d1a8537b03cf1ec0d27968f597e3f0106e7ef024d83c`) | `quarantined` | `foreign-alias-in-bytes` | `43f33275fb2b9e1c8a30bef6eff7141dcc9ffdbf2bdb98cbc1db861255dcd337` |
| `HANDOFF/CTO_STATE.md` | `blocked-by-metadata` | `gate-fail` | `428fc0509fb6bac4c47bedf4b9c88e56d468e800042f676dc33c87dcf84c50b7` |
| `HAR-Q-ac8ba69cc1f5` (path hash `ac8ba69cc1f5294f340f3a9ffe262b47dfb02f0b96c5ad15d69cbe9c6e74d20b`) | `quarantined` | `foreign-alias-in-bytes` | `fd135c41ede3e18c9fd8140e951dd103d0c2e626ef14a4087feaa206b64c0328` |
| `HANDOFF/JEV_LOG_PROMOTION_DRAFT.md` | `blocked-by-metadata` | `gate-fail` | `7f9c44c36d75e93ee467f338435d0a8907abb63a2fc0db95dd9928e926877680` |
| `HANDOFF/review/OC_FASTDRIVER_AVAILABILITY_RCA_2026-09-24.md` | `owner-valid` | `gate-pass` | `8a1c31d9fb40a5815acb82377befcdcc9bcedcac749158eb4553c11cf1e4bf78` |
| `HANDOFF/review/OC_FASTDRIVER_CONTRACT_DEFECT_2026-09-24.md` | `owner-valid` | `gate-pass` | `94b5afcc0e1f64c422b904eb1edf033257c05c3d9711f6f490ed9b29a8e51db0` |
| `HANDOFF/review/SCOPE_INVENTORY_2026-09-24.md` | `owner-valid` | `gate-pass-after-write` | `26e34621f5e1833baae62282866c0e33d0dffdafe053ff62f51daadc495d4866` |
| `HANDOFF/review/SCOPE_OWNERSHIP_RCA_2026-09-24.md` | `owner-valid` | `gate-pass` | `bec63112b82e7ef6e6a2d2a9d78b2932bdc196393ea8ff08d74e76aef5bcf5ea` |
| `HANDOFF/todo/P1_HARNESS_JEV_COMPLETION_GATE_AND_P2_REPAIR_2026-09-21.md` | `blocked-by-metadata` | `gate-fail` | `aa905596e961e7dd848f860c96bea105b6b5784370adb2d9e5e9f079a96ba09a` |

## Disposition

- No quarantined file is copied, committed, published, or treated as owner evidence.
- No blocked-by-metadata file is repaired in this pass; its owner must supply metadata and approve a deterministic migration.
- The owner-valid rows are the only rows eligible for later owner-local publication, subject to fresh staged-index and CI checks.
- Manifest path: `HANDOFF/review/SCOPE_INVENTORY_2026-09-24.md`.
